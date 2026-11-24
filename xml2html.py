"""Layout-aware PDF -> HTML converter built on poppler's `pdftohtml -xml`.

Keeps: italics/bold (from font names and flags), multi-column reading order,
headings (by font size), super/subscripts, footnotes (linked both ways),
numbered references with clickable [n] citations, hyperlinks, images, and
de-hyphenated paragraphs that continue across columns and pages.

Pure Python (stdlib only) so it runs inside calibre or standalone:
    python3 xml2html.py input.pdf output_dir
"""
import html as _html
import csv
import io
import mimetypes
import os
import re
import shutil
import statistics
import subprocess
import sys
from concurrent.futures import CancelledError
from itertools import accumulate
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET
from collections import Counter

TERMINAL_RE = re.compile(r'[.!?…:;]["”’\')\]]*\s*$')
PAGELIST_END_RE = re.compile(r',\s*\d{1,4}(?:\s*[–-]\s*\d{1,4})?(?:\s*,\s*\d{1,4}(?:\s*[–-]\s*\d{1,4})?)*\s*$')
MARKER_RE = re.compile(r'^\s*(\[\d{1,3}\]|\d{1,3}[.)]\s|[•▪◦●■]\s?)')
BULLET_RE = re.compile(r'^[•▪◦●■]\s*')
FN_RE = re.compile(r'^\s*(\d{1,3}|[*†‡§¶]+)(?:[.)])?\s+\S')
NOTE_CONTINUED = re.compile(r'^\(\s*continued on the next page\s*\)$', re.I)
REF_HEAD_RE = re.compile(
    r'\b(references?|bibliograph\w*|works cited|literature cited|endnotes?|notes)\b', re.I)
NUM_RE = re.compile(r'\d{1,3}')
CIT_BRACKET_RE = re.compile(r'\[(\d{1,3}(?:\s*[,;–\-]\s*\d{1,3})*)\]')
CIT_SUP_RE = re.compile(r'\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*')


DEFAULT_OPTS = {
    'colors': True,
    'mono': True,
    'centering': True,
    'block_indent': True,
    'font_sizes': True,
    'justify': 'auto',
    'remove_running': True,
    'ocr_language': 'eng',
    'chapter_split': True,
}

LEADER_RE = re.compile(r'(?:\s?\.){4,}\s*')
ROMAN_RE = re.compile(r'\s*[ivxlc]{1,7}\s*', re.I)


class Font:
    def __init__(self, size, family, color=None):
        self.size = float(size)
        self.family = family or ''
        self.mono = bool(re.search(
            r'courier|mono|consolas|typewriter|lucida\s*console|(?:^|\+)CMTT', self.family, re.I))
        self.color = None
        m = re.match(r'^#?([0-9a-fA-F]{6})$', color or '')
        if m:
            r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
            if max(r, g, b) - min(r, g, b) > 40:
                self.color = '#' + m.group(1).lower()
        self.bold = bool(re.search(
            r'bold|black|heavy|semibold|demi|(?:^|\+)CMBX', self.family, re.I))
        self.italic = bool(re.search(
            r'italic|oblique|slanted|(?:^|\+)CM(?:TI|SL)', self.family, re.I))


class Item:
    def __init__(self, kind, top, left, width, height, font=None, segs=None, src=None):
        self.kind = kind
        self.top, self.left = float(top), float(left)
        self.width, self.height = float(width), float(height)
        self.font = font
        self.segs = segs or []
        self.src = src
        self.text = ''.join(s[0] for s in self.segs)
        self.removed = False
        self.col = 0

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height


class Page:
    def __init__(self, number, width, height):
        self.number, self.w, self.h = number, width, height
        self.texts, self.images = [], []
        self.label = None
        self.label_item = None


class Doc:
    def __init__(self, options=None):
        self.options = dict(DEFAULT_OPTS, **(options or {}))
        self.pages = []
        self.outline = []
        self.navigation = []
        self.body = 12.0
        self.heading_sizes = []
        self.just_hits = 0
        self.just_total = 0
        self.labels = {}
        self.anchored = set()
        self.chapters = {}
        self.words = Counter()
        self.continued_note = None
        self.gl = self.gr = 0


def check_cancelled(abort):
    if abort is not None and abort.is_set():
        raise CancelledError('PDF conversion cancelled')


def run_command(command, cwd, abort):
    check_cancelled(abort)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, creationflags=flags) as process:
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25 if abort is not None else None)
                    break
                except subprocess.TimeoutExpired:
                    check_cancelled(abort)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    check_cancelled(abort)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_pdftohtml(pdf_path, work_dir, exe=None, abort=None):
    exe = exe or shutil.which('pdftohtml') or 'pdftohtml'
    pdf_path = os.path.abspath(pdf_path)
    xml_path = os.path.join(work_dir, 'src.xml')
    base = [exe, '-xml', '-enc', 'UTF-8', '-zoom', '1']
    error = ''
    for extra in (['-fontfullname'], []):
        with TemporaryDirectory(prefix='extract-', dir=work_dir) as scratch:
            try:
                result = run_command(base + extra + [pdf_path, 'src'], scratch, abort)
            except OSError as exc:
                raise RuntimeError('Could not run pdftohtml: %s' % exc) from exc
            source = os.path.join(scratch, 'src.xml')
            if result.returncode == 0 and os.path.isfile(source) and os.path.getsize(source):
                for name in os.listdir(scratch):
                    os.replace(os.path.join(scratch, name), os.path.join(work_dir, name))
                return xml_path
            error = result.stderr.decode('utf-8', 'replace').strip() or 'no XML output'
    raise RuntimeError('pdftohtml failed: %s' % error)


def _segments(el, b=False, i=False, href=None, out=None):
    if out is None:
        out = []
    if el.text:
        out.append((el.text, b, i, href))
    for ch in el:
        nb = b or ch.tag == 'b'
        ni = i or ch.tag == 'i'
        nh = ch.get('href') if ch.tag == 'a' else href
        _segments(ch, nb, ni, nh, out)
        if ch.tail:
            out.append((ch.tail, b, i, href))
    return out


def parse(xml_path, doc):
    with open(xml_path, encoding='utf-8', errors='replace') as stream:
        raw = stream.read()
    raw = re.sub(r'<!DOCTYPE[^>]*>', '', raw)
    raw = re.sub('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]', '', raw)
    root = ET.fromstring(raw.encode('utf-8'))
    fonts = {}
    for pg in root.iter('page'):
        fonts.update((fs.get('id'), Font(fs.get('size', '12'), fs.get('family', ''), fs.get('color')))
                     for fs in pg.findall('fontspec'))
        page = Page(int(pg.get('number', '0')), float(pg.get('width', 600)),
                    float(pg.get('height', 800)))
        for el in pg:
            if el.tag == 'text':
                segs = [(re.sub(r'\s+', ' ', t), b, i, fix_href(h))
                        for t, b, i, h in _segments(el)]
                if not ''.join(s[0] for s in segs).strip():
                    continue
                page.texts.append(Item(
                    'text', el.get('top', 0), el.get('left', 0), el.get('width', 0),
                    el.get('height', 0), fonts.get(el.get('font')) or Font(12, ''), segs))
            elif el.tag == 'image' and el.get('src'):
                page.images.append(Item(
                    'img', el.get('top', 0), el.get('left', 0), el.get('width', 0),
                    el.get('height', 0), src=os.path.basename(el.get('src'))))
        doc.pages.append(page)
    doc.outline = read_outline(root.find('outline'))


def read_outline(element):
    entries = []
    if element is not None:
        for child in element:
            if child.tag == 'item' and child.get('page', '').isdigit() and child.text:
                entries.append((child.text.strip(), int(child.get('page')), read_outline(child)))
            elif child.tag == 'outline' and entries:
                entries[-1][2].extend(read_outline(child))
    return entries


def fix_href(h):
    if not h:
        return h
    m = re.fullmatch(r'(?:src\.html)?#([0-9]+)', h)
    if m:
        return '#page-%d' % int(m.group(1))
    try:
        return h if urlsplit(h).scheme.lower() in ('http', 'https', 'mailto') else None
    except ValueError:
        return None


def find_tesseract():
    configured = os.environ.get('PDF_TO_EPUB_TESSERACT')
    exe = shutil.which(configured or 'tesseract')
    if not exe and not configured and os.name == 'nt':
        exe = shutil.which(os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                                       'Tesseract-OCR', 'tesseract.exe'))
    return exe


def find_renderer(pdftohtml_exe):
    converter = pdftohtml_exe or shutil.which('pdftohtml')
    if converter and os.path.dirname(converter):
        name = 'pdftoppm.exe' if os.name == 'nt' else 'pdftoppm'
        bundled = os.path.join(os.path.dirname(converter), name)
        if os.path.isfile(bundled):
            return os.path.abspath(bundled)
    return shutil.which('pdftoppm')


def rasterize_page(pdf_path, number, output, renderer, abort, dpi=240):
    if os.path.isfile(output):
        os.remove(output)
    command = [renderer, '-f', str(number), '-l', str(number), '-cropbox', '-r', str(dpi),
               '-png', '-singlefile', os.path.abspath(pdf_path), os.path.abspath(output)[:-4]]
    result = run_command(command, os.path.dirname(os.path.abspath(output)), abort)
    if result.returncode or not os.path.isfile(output):
        raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or 'PDF page rendering failed')


def read_ocr(tsv, page):
    groups = {}
    image_width = image_height = 0
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        if row['level'] == '1':
            image_width, image_height = int(row['width']), int(row['height'])
        elif row['level'] == '5' and (row.get('text') or '').strip():
            key = tuple(row[field] for field in ('block_num', 'par_num', 'line_num'))
            groups.setdefault(key, []).append(row)
    if not image_width or not image_height:
        return []
    sx, sy = page.w / image_width, page.h / image_height
    lines = []
    for words in groups.values():
        left = min(int(w['left']) for w in words)
        top = min(int(w['top']) for w in words)
        right = max(int(w['left']) + int(w['width']) for w in words)
        bottom = max(int(w['top']) + int(w['height']) for w in words)
        size = statistics.median(int(w['height']) for w in words) * sy / 0.75
        text = ' '.join(w['text'].strip() for w in words)
        lines.append(Item('text', top * sy, left * sx, (right - left) * sx, (bottom - top) * sy,
                          Font(max(1, size), ''), [(text, False, False, None)]))
    return lines


def ocr_pages(doc, pdf_path, work_dir, pdftohtml_exe, log, abort, progress):
    cover_pages = {page for title, page, _ in doc.outline if title.lower() in ('cover', 'book cover')}
    has_text = any(page.texts for page in doc.pages)
    candidates = []
    for page in doc.pages:
        photos = [im for im in page.images if im.width * im.height >= 0.6 * page.w * page.h]
        readable = sum(char.isalnum() for item in page.texts for char in item.text
                       if 0.12 * page.h < item.top < 0.88 * page.h)
        if photos and readable < 20 and (page.number not in cover_pages or not has_text):
            candidates.append((page, photos))
    if not candidates:
        return
    tesseract = find_tesseract()
    renderer = find_renderer(pdftohtml_exe)
    if not tesseract or not renderer:
        missing = 'Tesseract' if not tesseract else 'pdftoppm'
        log('%s is unavailable. Keeping %d scanned page(s) as images.' % (missing, len(candidates)))
        return
    language = doc.options['ocr_language']
    if not re.fullmatch(r'[\w-]+(?:\+[\w-]+)*', language):
        raise ValueError('Use Tesseract language codes such as eng or eng+fra.')
    log('Running OCR on %d scanned page(s) using %s.' % (len(candidates), language))
    with TemporaryDirectory(prefix='ocr-', dir=work_dir) as scratch:
        image = os.path.join(scratch, 'page.png')
        for index, (page, photos) in enumerate(candidates):
            check_cancelled(abort)
            if progress is not None:
                progress(0.1 + 0.25 * index / len(candidates), 'OCR on PDF page %d' % page.number)
            try:
                rasterize_page(pdf_path, page.number, image, renderer, abort)
                result = run_command([tesseract, image, 'stdout', '-l', language, 'tsv'], scratch, abort)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip())
                lines = read_ocr(result.stdout.decode('utf-8', 'replace'), page)
            except (OSError, RuntimeError, ValueError) as exc:
                log('OCR failed on PDF page %d. Keeping its image. %s' % (page.number, exc))
                continue
            if not lines:
                log('OCR found no text on PDF page %d. Keeping its image.' % page.number)
                continue
            page.texts = lines
            page.images = [im for im in page.images if im not in photos]


def render_item(it, options):
    f = it.font
    merged = []
    # pdftohtml tags styled words itself, and an item's font attribute only
    # describes its first word, so fall back to the font name only if untagged
    tagged = any(s[1] or s[2] for s in it.segs)
    for t, b, i, h in it.segs:
        if not tagged:
            b, i = b or f.bold, i or f.italic
        if merged and merged[-1][1:] == (b, i, h):
            merged[-1] = (merged[-1][0] + t, b, i, h)
        else:
            merged.append((t, b, i, h))
    out = []
    for t, b, i, h in merged:
        s = _html.escape(t, quote=False)
        if not s.strip():
            out.append(s)
            continue
        lead = s[:len(s) - len(s.lstrip())]
        trail = s[len(s.rstrip()):]
        core = s.strip()
        if f.mono and options['mono']:
            core = '<code>%s</code>' % core
        if i:
            core = '<i>%s</i>' % core
        if b:
            core = '<b>%s</b>' % core
        if f.color and options['colors']:
            core = '<span style="color:%s">%s</span>' % (f.color, core)
        if h:
            core = '<a href="%s">%s</a>' % (_html.escape(h, quote=True), core)
        out.append(lead + core + trail)
    return ''.join(out)


class Line:
    kind = 'line'

    def __init__(self, items, options):
        items = sorted(items, key=lambda i: i.left)
        main = max(items, key=lambda i: len(i.text.strip()))
        ms = main.font.size
        parts, texts, prev = [], [], None
        bold_all = True
        for it in items:
            role = ''
            if it is not main and it.font.size <= 0.85 * ms:
                raised = it.bottom < main.bottom - 0.12 * ms
                lowered = it.bottom > main.bottom + 0.1 * ms
                if lowered:
                    role = 'sub'
                elif raised or re.fullmatch(r'[,\s]*[\d*†‡§¶]{1,4}', it.text):
                    role = 'sup'
            h = render_item(it, options)
            t = it.text
            if prev is not None and role != 'sup':
                gap = it.left - prev.right
                if gap > 0.18 * ms and not texts[-1].endswith(' ') and not t.startswith(' '):
                    parts.append(' ')
                    texts.append(' ')
            if role:
                h = '<%s>%s</%s>' % (role, h, role)
            parts.append(h)
            texts.append(t)
            prev = it
            if not role and not (it.font.bold or all(
                    s[1] for s in it.segs if s[0].strip())):
                bold_all = False
        self.html = re.sub(r'\s+', ' ', ''.join(parts)).strip()
        self.text = re.sub(r'\s+', ' ', ''.join(texts)).strip()
        self.toc = bool(LEADER_RE.search(self.text)) and bool(re.search(r'\d+\s*$|[ivxlc]+\s*$', self.text))
        if self.toc:
            self.html = LEADER_RE.sub(' … ', self.html).strip()
            self.text = LEADER_RE.sub(' … ', self.text).strip()
        self.centered = False
        self.top, self.bottom = main.top, main.bottom
        self.left = min(i.left for i in items)
        self.right = max(i.right for i in items)
        self.size = ms
        self.bold_all = bold_all
        first = items[0]
        marker = re.fullmatch(r'\s*(\d{1,3}|[*†‡§¶]+)[.)]?\s*', first.text)
        self.note_label = None
        if marker and len(items) > 1 and items[1].left - first.right > 0.7 * ms:
            self.note_label = marker[1]


def cluster_lines(items, options):
    items = sorted(items, key=lambda i: (i.top, i.left))
    lines = []
    for it in items:
        for ln in reversed(lines[-3:]):
            ov = min(ln['bottom'], it.bottom) - max(ln['top'], it.top)
            if ov >= 0.5 * min(ln['bottom'] - ln['top'], it.height):
                ln['items'].append(it)
                ln['top'] = min(ln['top'], it.top)
                ln['bottom'] = max(ln['bottom'], it.bottom)
                break
        else:
            lines.append({'top': it.top, 'bottom': it.bottom, 'items': [it]})
    return [Line(l['items'], options) for l in lines]


def join_line(doc, block, next_html, next_text):
    left = re.search(r'([^\W\d_]+)([-\u00ad])$', block['text'])
    right = re.match(r'([^\W\d_]+)', next_text)
    separator = ' '
    if left and right and right[1].islower():
        separator = ''
        stem, tail = left[1].casefold(), right[1].casefold()
        if left[2] == '\u00ad' or doc.words[stem + tail] > doc.words[stem + '-' + tail]:
            block['html'] = re.sub(r'[-\u00ad]((?:</\w+>)*)$', r'\1', block['html'])
            block['text'] = block['text'][:-1]
    block['html'] += separator + next_html
    block['text'] += separator + next_text


def analyse(doc):
    c = Counter()
    pages_with = Counter()
    for pg in doc.pages:
        seen = set()
        for t in pg.texts:
            k = round(t.font.size * 2) / 2.0
            c[k] += len(t.text.strip())
            seen.add(k)
            doc.words.update(word.casefold() for word in re.findall(r'[^\W\d_]+(?:-[^\W\d_]+)*', t.text))
        pages_with.update(seen)
    ls, rs = [], []
    for pg in doc.pages:
        ts = [t for t in pg.texts if t.text.strip()]
        if len(ts) >= 8:
            ls.append(min(t.left for t in ts))
            rs.append(max(t.right for t in ts))
    doc.gl = statistics.median(ls) if ls else 0
    doc.gr = statistics.median(rs) if rs else 0
    if c:
        doc.body = c.most_common(1)[0][0]
    tot = sum(c.values()) or 1
    doc.heading_sizes = sorted(
        [s for s, n in c.items()
         if s >= doc.body * 1.12 and s - doc.body >= 1.0 and n < 0.2 * tot
         and (pages_with[s] >= 2 or len(doc.pages) <= 3)], reverse=True)


ROMAN_VALID = re.compile(r'^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$', re.I)
LABEL_RE = re.compile(r'^\s*(?:page\s*)?(\d{1,4}|[ivxlcdm]{1,7})\s*$', re.I)


def is_label(t):
    m = LABEL_RE.match(t)
    if not m:
        return None
    v = m.group(1).lower()
    if v.isdigit() or ROMAN_VALID.match(v):
        return v
    return None


def detect_page_labels(doc):
    tracks = {}
    for page in doc.pages:
        for item in page.texts:
            edge = min(item.top, page.h - item.bottom) / page.h
            if edge > 0.17 or (label := is_label(item.text)) is None:
                continue
            roman = not label.isdigit()
            value = roman_number(label) if roman else int(label)
            pages = tracks.setdefault((roman, page.number - value), {})
            if page.number not in pages or edge < pages[page.number][0]:
                pages[page.number] = (edge, item, label)
    used = set()
    for roman in (False, True):
        supported = [(offset, pages) for (is_roman, offset), pages in tracks.items()
                     if is_roman == roman and len(pages) >= (2 if len(doc.pages) < 8 else 3)]
        if not supported:
            continue
        _, matched = max(supported, key=lambda track: len(track[1]))
        observed = sorted(matched)
        for page in doc.pages:
            if page.number in used:
                continue
            entry = matched.get(page.number)
            if entry:
                _, page.label_item, page.label = entry
            elif not roman and observed[0] < page.number < observed[-1]:
                label_start = int(matched[observed[0]][2])
                page.label = str(label_start + page.number - observed[0])
            if page.label:
                used.add(page.number)
                doc.labels.setdefault(page.label, page.number)


def roman_number(label):
    values = dict(zip('ivxlcdm', (1, 5, 10, 50, 100, 500, 1000)))
    digits = [values[c] for c in label]
    return sum(-n if n < after else n for n, after in zip(digits, digits[1:] + [0]))


def remove_running_text(doc):
    """Drop page numbers and repeating headers/footers."""
    seen = Counter()
    edge = []
    for pg in doc.pages:
        keys = set()
        label = pg.label_item
        header = label if label and label.top < 0.17 * pg.h else None
        for t in pg.texts:
            if t is label or (header and abs(t.top - header.top) < 0.006 * pg.h):
                t.removed = True
                continue
            crop_mark = re.fullmatch(r'\+(?:\s+\+)*', t.text.strip())
            if t.top < 0.07 * pg.h or t.bottom > 0.93 * pg.h or (
                    crop_mark and (t.top < 0.12 * pg.h or t.bottom > 0.88 * pg.h)):
                key = (round(t.top / pg.h, 2), '+' if crop_mark else re.sub(r'\d+', '#', t.text.lower()).strip())
                edge.append((t, key))
                keys.add(key)
        seen.update(keys)
    has_crop_marks = sum(n for (_, text), n in seen.items() if text == '+') >= 3
    for t, key in edge:
        if seen[key] >= 3 or (has_crop_marks and key[1] == '+'):
            t.removed = True


def find_gutters(texts, pw):
    cand = [t for t in texts if 0 < t.width < 0.55 * pw and t.text.strip()]
    if len(cand) < 8:
        return []
    lo = max(0, int(min(t.left for t in cand)))
    hi = min(int(pw), int(max(t.right for t in cand)) + 1)
    if hi <= lo:
        return []
    difference = [0] * (hi - lo + 2)
    for t in cand:
        start, end = max(0, int(t.left) - lo), min(hi - lo + 1, int(t.right) - lo + 1)
        if start < end:
            difference[start] += 1
            difference[end] -= 1
    cover = list(accumulate(difference))
    eps = max(1, int(0.04 * len(cand)))
    min_gap = max(8, int(0.015 * pw))
    runs, x = [], 0
    while x < len(cover):
        if cover[x] <= eps:
            s = x
            while x < len(cover) and cover[x] <= eps:
                x += 1
            if s > 0 and x < len(cover) and x - s >= min_gap:
                runs.append((s + lo, x + lo))
        else:
            x += 1
    gutters = []
    for s, e in runs:
        middle = (s + e) / 2.0
        if min(middle - lo, hi - middle) < 0.19 * pw:
            continue
        left = [t for t in cand if t.right <= s + 1 and t.width >= 0.07 * pw]
        right = [t for t in cand if t.left >= e - 1 and t.width >= 0.07 * pw]
        if len(left) < 4 or len(right) < 4:
            continue
        pairs = sum(1 for a in left if any(
            min(a.bottom, b.bottom) - max(a.top, b.top) > 0.5 * min(a.height, b.height)
            for b in right))
        if pairs >= max(4, min(len(left), len(right)) * 0.25):
            gutters.append((pairs, e - s, middle))
    chosen = []
    for _, _, middle in sorted(gutters, reverse=True):
        if all(abs(middle - point) >= 0.19 * pw for point in chosen):
            chosen.append(middle)
    return sorted(chosen)


def heading_level(doc, L):
    t = L.text
    if not t or len(t) > 200 or re.fullmatch(r'[\d\W]*', t):
        return None
    hs = doc.heading_sizes
    if L.size >= doc.body * 1.12 and L.size - doc.body >= 1.0:
        idx = min(range(len(hs)), key=lambda k: abs(hs[k] - L.size)) if hs else 0
        return min(idx + 1, 3)
    if (L.bold_all and len(t) <= 100 and L.size >= doc.body * 0.95
            and t.rstrip()[-1:] not in ('.', ',', ';', ':', '')):
        return min(min(len(hs), 3) + 1, 4)
    return None


def new_block(t, L, page_no, indent=False):
    return {'t': t, 'html': L.html, 'text': L.text, 'maxsize': L.size, 'top': L.top,
            'page': page_no, 'first': False, 'last': False, 'indent': indent,
            'ref': False, 'nlines': 1, 'centered': L.centered, 'toc': L.toc,
            'bleft': L.left, 'size': L.size}


def split_notes(doc, lines, ph, page_no):
    main, notes = [], []
    current = doc.continued_note
    started = False
    if current and current['page'] < page_no - 1:
        current = doc.continued_note = None
    for line in lines:
        small = line.top > 0.46 * ph and line.size <= 0.92 * doc.body
        if current and NOTE_CONTINUED.fullmatch(line.text):
            doc.continued_note = current
            continue
        marker = FN_RE.match(line.text) if small else None
        label = line.note_label if small else None
        if marker and not label and line.top > 0.55 * ph:
            label = marker[1]
        note_html = strip_marker(line.html, label) if label else None
        previous = doc.continued_note
        if note_html is not None:
            current = {'t': 'fn', 'html': note_html, 'text': line.text, 'page': page_no, 'label': label,
                       'first': False, 'last': False}
            notes.append(current)
            started = True
        elif small and current and (started or current is previous):
            join_line(doc, current, line.html, line.text)
            started = True
        else:
            main.append(line)
        if previous and previous['page'] < page_no and started:
            doc.continued_note = None
    return main, notes


def build_group(doc, items, ph, page_no, region=None):
    texts = [i for i in items if i.kind == 'text']
    imgs = [i for i in items if i.kind == 'img']
    lines = cluster_lines(texts, doc.options)
    lines, notes = split_notes(doc, lines, ph, page_no) if region is None else (lines, [])
    entries = sorted(lines + imgs, key=lambda e: e.top)
    body = doc.body
    blines = [l for l in lines if abs(l.size - body) <= 0.6] or lines
    group_left, group_right, pitch = 0, 0, 1.25 * body
    if blines:
        c = Counter(round(l.left / 2.0) * 2 for l in blines)
        frequent = [x for x, n in c.items() if n >= 2] or list(c)
        group_left = min(frequent)
        rights = sorted(l.right for l in blines)
        group_right = rights[int(0.9 * (len(rights) - 1))]
        d = [b.top - a.top for a, b in zip(blines, blines[1:])]
        d = [x for x in d if 0.8 * body <= x <= 2.2 * body]
        if d:
            pitch = statistics.median(d)
        for l in blines[:-1]:
            doc.just_total += 1
            if abs(l.right - group_right) <= 3:
                doc.just_hits += 1
    cl, cr = group_left, group_right
    if (len(blines) < 4 or cr - cl < 6 * body) and doc.gr - doc.gl > 6 * body:
        cl, cr = doc.gl, doc.gr
    if doc.options['centering'] and cr - cl > 6 * body:
        gc, gw = (cl + cr) / 2.0, cr - cl
        for l in lines:
            lc = (l.left + l.right) / 2.0
            l.centered = (abs(lc - gc) <= 0.03 * gw + 2
                          and (l.left - cl) > 1.5 * l.size
                          and (cr - l.right) > 1.5 * l.size
                          and (l.right - l.left) <= 0.8 * gw)

    blocks, cur, prev = [], None, None

    def flush():
        nonlocal cur
        if cur:
            blocks.append(cur)
            cur = None

    for e in entries:
        if e.kind == 'img':
            flush()
            blocks.append({'t': 'img', 'src': e.src, 'page': page_no, 'first': False,
                           'last': False, 'html': '', 'text': '', 'top': e.top})
            prev = None
            continue
        level = None
        if not BULLET_RE.match(e.text) and (region is None or not re.search(r'\d\s*$', e.text)):
            level = heading_level(doc, e)
        if level:
            tag = 'h%d' % level
            if (cur and cur['t'] == tag and prev is not None and e.centered == prev.centered
                    and (e.top - prev.bottom) < 0.9 * e.size):
                cur['html'] += ' ' + e.html
                cur['text'] += ' ' + e.text
            else:
                flush()
                cur = new_block(tag, e, page_no)
            prev = e
            continue
        indent = (e.left - group_left) > 0.8 * e.size
        if cur and cur['t'] == 'p' and prev is not None:
            is_ref = bool(MARKER_RE.match(cur['text']))
            brk = False
            if e.centered != prev.centered or e.toc or prev.toc:
                brk = True
            elif region and re.search(r'\d\s*$', prev.text):
                brk = True
            elif PAGELIST_END_RE.search(prev.text) and not e.text[:1].isdigit():
                brk = True
            elif (e.top - prev.top) > 1.45 * pitch:
                brk = True
            elif indent and (prev.left - group_left) < 0.5 * e.size and not is_ref:
                brk = True
            elif ((group_right - prev.right) > 2.5 * prev.size and TERMINAL_RE.search(prev.text)
                  and not is_ref
                  and (e.text[:1].isupper() or e.text[:1].isdigit())):
                brk = True
            elif MARKER_RE.match(e.text):
                brk = True
            elif (cur['nlines'] >= 2 and (prev.left - group_left) > 0.8 * e.size
                  and (prev.left - e.left) > 0.8 * e.size):
                brk = True
            if brk:
                flush()
                cur = new_block('p', e, page_no, indent)
            else:
                join_line(doc, cur, e.html, e.text)
                cur['maxsize'] = max(cur['maxsize'], e.size)
                cur['nlines'] += 1
                cur['bleft'] = min(cur['bleft'], e.left)
        else:
            flush()
            cur = new_block('p', e, page_no, indent)
        prev = e
    flush()

    for b in blocks:
        if b['t'] != 'p':
            continue
        if (doc.options['block_indent'] and not b['centered'] and not b['toc'] and b['nlines'] >= 2
                and not MARKER_RE.match(b['text'])):
            rel = b['bleft'] - group_left
            if rel > 1.5 * body:
                b['mleft'] = min(6.0, round(rel / body * 2) / 2.0)
        if doc.options['font_sizes'] and body:
            ratio = round(b['maxsize'] / body / 0.05) * 0.05
            if abs(ratio - 1) >= 0.09:
                b['fs'] = max(0.6, min(2.0, ratio))

        if region == 'index':
            b['pagerefs'] = True
        if region == 'contents':
            b['contents'] = True
    if blocks and region is None:
        blocks[0]['first'] = blocks[0]['t'] == 'p'
        blocks[-1]['last'] = blocks[-1]['t'] == 'p'
    return blocks, notes


def strip_marker(h, label):
    m = re.match(r'^\s*<sup>\s*%s\s*</sup>\s*' % re.escape(label), h)
    if m:
        return h[m.end():]
    m = re.match(r'^\s*%s(?:[.)])?\s+' % re.escape(label), h)
    if m:
        return h[m.end():]
    return None


def link_notes(blocks, notes):
    for note in notes:
        label = re.escape(note['label'])
        pattern = re.compile(r'(<a\b[^>]*>|</a>)|<sup>\s*([^\w]*?)\s*(%s)\s*</sup>' % label)
        for block in blocks:
            inside_link = False
            for match in pattern.finditer(block['html']):
                if match[1]:
                    inside_link = match[1] != '</a>'
                elif not inside_link:
                    note['backlink'] = 'nr-' + note['id']
                    link = '%s<a class="noteref" id="%s" href="#%s"><sup>%s</sup></a>' % (
                        match[2], note['backlink'], note['id'], match[3])
                    start, end = match.span()
                    block['html'] = block['html'][:start] + link + block['html'][end:]
                    break
            if note.get('backlink'):
                break


def build_page(doc, pg, region=None, gutter_template=None):
    texts = [t for t in pg.texts if not t.removed]
    imgs = [im for im in pg.images if im.width > 0 and im.height > 0]
    gutters = gutter_template if gutter_template is not None else find_gutters(texts, pg.w)
    items = sorted(texts + imgs, key=lambda i: (i.top, i.left))

    def label(i):
        if any(i.left < g - 3 and i.right > g + 3 for g in gutters):
            return 'S'
        return sum(1 for g in gutters if i.left + i.width / 2.0 > g)

    groups, buf, mode = [], [], None

    def flush():
        nonlocal buf
        if not buf:
            return
        if mode == 'S':
            groups.append(buf)
        else:
            for c in sorted(set(i.col for i in buf)):
                groups.append([i for i in buf if i.col == c])
        buf = []

    for it in items:
        lab = label(it)
        m = 'S' if lab == 'S' else 'C'
        it.col = 0 if lab == 'S' else lab
        if m != mode:
            flush()
            mode = m
        buf.append(it)
    flush()

    body_blocks, fns = [], []
    for g in groups:
        b, f = build_group(doc, g, pg.h, pg.number, region)
        body_blocks += b
        fns += f

    for index, note in enumerate(fns):
        note['id'] = 'fn-%d-%d' % (pg.number, index)
    link_notes(body_blocks, fns)
    blocks = body_blocks + fns
    if blocks and blocks[0]['t'] not in ('img', 'fn'):
        blocks[0]['html'] = '<span id="page-%d"></span>' % pg.number + blocks[0]['html']
    else:
        blocks.insert(0, {'t': 'anchor', 'page': pg.number, 'html': '', 'text': '', 'first': False, 'last': False})
    doc.anchored.add(pg.number)
    return blocks


def merge_across_breaks(doc, blocks):
    out, last = [], None
    starts = {page for _, page, _ in doc.outline}
    for b in blocks:
        if b['t'] == 'fn':
            out.append(b)
            continue
        if (b['t'] == 'p' and last is not None and b['first'] and last['last']
                and (b['page'] == last['page'] or b['page'] not in starts)
                and not b['indent'] and not MARKER_RE.match(b['text'])
                and not MARKER_RE.match(last['text'])):
            ends = TERMINAL_RE.search(last['text'])
            if (not ends and b['text'][:1].islower()) or last['text'].endswith(('-', ',')):
                join_line(doc, last, b['html'], b['text'])
                last['last'] = b['last']
                continue
        out.append(b)
        last = b if b['t'] == 'p' else None
    return out


def mark_references(blocks):
    in_refs, refs = False, set()
    for b in blocks:
        if b['t'].startswith('h'):
            in_refs = bool(REF_HEAD_RE.search(b['text']))
            continue
        if b['t'] != 'p':
            continue
        m = re.match(r'^\s*\[(\d{1,3})\]', b['text'])
        if not m and in_refs:
            m = re.match(r'^\s*(\d{1,3})[.)]\s', b['text'])
        if in_refs or m:
            b['ref'] = True
        if m and m.group(1) not in refs:
            refs.add(m.group(1))
            b['ref_id'] = m.group(1)
    return refs


TOC_HEAD_RE = re.compile(
    r'^(?:table of |detailed |brief |short )?contents?(?: page)?$', re.I)
PAGEREF_HEAD_RE = re.compile(
    r'^(?:(?:general |subject |author )?index(?: of \w+)?|table of (?:cases|statutes|legislation|'
    r'authorities|abbreviations)|(?:list|table) of (?:cases|statutes|legislation|authorities)|'
    r'cases|statutes|legislation)$', re.I)
PAGE_REF_RE = re.compile(r'\b((?:pp?|pages?)\.?\s+)(\d{1,4})(?:(\s*[–-]\s*)(\d{1,4}))?\b', re.I)
CHAPTER_NAMES = ('one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
                 'fifteen sixteen seventeen eighteen nineteen twenty').split()
CHAPTER_VALUE = {name: str(index) for index, name in enumerate(CHAPTER_NAMES, 1)}
CHAPTER_TOKEN = r'\d{1,3}|[ivxlcdm]{1,7}|' + '|'.join(CHAPTER_NAMES)
CHAP_REF_RE = re.compile(r'\bchapters?\s+(' + CHAPTER_TOKEN + r')\b', re.I)
NUM_TOKEN_RE = re.compile(r'(?<![\w.])(\d{1,4})(?:(\s*[–-]\s*)(\d{1,4}))?(?![\w.])')
ENTRY_RE = re.compile(r'^(?P<t>\S.*?)(?:\s*…\s*|\s+)(?P<n>\d{1,4}|[ivxlcdm]{1,7})(?:\s*[–-]\s*\d{1,4})?$',
                      re.I)

def norm_title(s):
    s = re.sub(r'<[^>]+>', '', s or '').strip()
    s = re.sub(r'^chapter\s+(?:' + CHAPTER_TOKEN + r')\b\s*[.:\-–]?\s*', '', s, flags=re.I)
    s = re.sub(r'^(?:chapter|part|section|appendix)\s+(?:\d+|[ivxlc]+|[a-z])(?=[\s.:\-–]|$)\s*[.:\-–]?\s*',
               '', s, flags=re.I)
    s = re.sub(r'^\d+(?:\.\d+)*[.):]?\s+', '', s)
    return re.sub(r'[\W_]+', '', s.lower())


def _plain(b):
    return re.sub(r'<[^>]+>', '', b.get('html', ''))


def chapter_key(value):
    value = value.casefold()
    if value.isdigit():
        return str(int(value))
    if value in CHAPTER_VALUE:
        return CHAPTER_VALUE[value]
    return str(roman_number(value)) if ROMAN_VALID.fullmatch(value) else None


def navigation_regions(outline):
    regions = {}
    ordered = sorted(outline, key=lambda entry: entry[1])
    for index, (title, page, _) in enumerate(ordered):
        region = 'contents' if TOC_HEAD_RE.fullmatch(title) else 'index' if PAGEREF_HEAD_RE.fullmatch(title) else None
        if region:
            end = ordered[index + 1][1] if index + 1 < len(ordered) else sys.maxsize
            regions[page] = (end, region)
    return regions


def index_gutters(doc, regions):
    templates = {}
    for start, (end, region) in regions.items():
        if region != 'index':
            continue
        positions = []
        for page in doc.pages:
            if start <= page.number < end and page.w:
                gutters = find_gutters([t for t in page.texts if not t.removed], page.w)
                if len(gutters) == 1:
                    positions.append(gutters[0] / page.w)
        if len(positions) >= 3:
            center = statistics.median(positions)
            nearby = [position for position in positions if abs(position - center) <= 0.045]
            if len(nearby) >= max(3, 0.6 * len(positions)):
                templates[start] = statistics.median(nearby)
    return templates


def link_document(doc, blocks):
    """Assign ids to headings, resolve contents entries to their targets and
    prepare the maps used for cross-reference linking."""
    labels = {k: v for k, v in doc.labels.items() if v in doc.anchored}
    heads = []
    for i, b in enumerate(blocks):
        if b['t'].startswith('h'):
            b['id'] = 'sec-%d' % i
            heads.append((i, b, norm_title(b['text'])))
            m = (CHAP_REF_RE.match(b['text'])
                 or re.match(r'^(\d{1,3})[.:]?\s+\S', b['text']))
            if m and b['t'] in ('h1', 'h2') and (key := chapter_key(m[1])):
                doc.chapters.setdefault(key, b['id'])
    by_page = {}
    for i, b in enumerate(blocks):
        if b['t'] in ('p',) or b['t'].startswith('h'):
            by_page.setdefault(b['page'], []).append((i, b))

    def target_outline(entries, top_level=False):
        targets = []
        for title, page, children in entries:
            if page not in doc.anchored:
                continue
            target = 'page-%d' % page
            name = norm_title(title)
            candidates = by_page.get(page, ())
            match = next(((i, b) for i, b in candidates if name and norm_title(b['text']) == name), None)
            if match:
                i, block = match
                target = block.setdefault('id', 'loc-%d' % i)
            else:
                block = candidates[0][1] if candidates else None
            if top_level and block is not None and title.casefold() not in ('cover', 'book cover'):
                first = candidates[0][1]
                first['chapter_start'] = True
            if top_level:
                chapter = CHAP_REF_RE.match(title) or re.match(r'^(\d{1,3})[.:]?\s+\S', title)
                if chapter and (key := chapter_key(chapter[1])):
                    doc.chapters[key] = target
            targets.append((title, target, target_outline(children)))
        return targets

    doc.navigation = target_outline(doc.outline, top_level=True)

    def label_page(label):
        return labels.get(str(label).lower())

    def resolve(idx, title, label):
        want = label_page(label)
        nt = norm_title(title)
        if len(nt) < 2:
            return None
        exact = [(i, b) for i, b, n in heads if n == nt and i != idx]
        loose = [(i, b) for i, b, n in heads
                 if i != idx and len(n) >= 4 and len(nt) >= 4 and (n.startswith(nt) or nt.startswith(n))]
        for cands in (exact, loose):
            if cands:
                if want is not None:
                    cands = sorted((c for c in cands if abs(c[1]['page'] - want) <= 1),
                                   key=lambda c: abs(c[1]['page'] - want))
                else:
                    cands = sorted(cands, key=lambda c: (c[0] <= idx, c[0]))
                if cands:
                    return '#' + cands[0][1]['id']
        if want is not None:
            for pg in (want, want + 1):
                for i, b in by_page.get(pg, []):
                    if i != idx and b['t'] == 'p' and not b.get('toc') and len(nt) >= 3 and norm_title(_plain(b)).startswith(nt):
                        b.setdefault('id', 'loc-%d' % i)
                        return '#' + b['id']
            return '#page-%d' % want
        return None

    def entry(b):
        text = re.sub(r'\s+', ' ', b['text']).strip()
        if len(text) > 160 or b.get('nlines', 1) > 3:
            return None
        m = ENTRY_RE.match(text)
        if not m:
            return None
        t, n = m.group('t').rstrip(' …'), m.group('n')
        if len(t) < 2 or re.fullmatch(r'[\d\W]*', t) or re.search(r'[.!?;]$', t):
            return None
        if not n.isdigit() and (not ROMAN_VALID.match(n) or n.lower() not in labels):
            return None
        return t, n

    has_contents = any(b.get('contents') for b in blocks)
    for i, b in enumerate(blocks):
        if b.get('contents') and (e := entry(b)):
            b['toc'] = b['toc_region'] = True
            target = resolve(i, e[0], e[1])
            if target:
                b['href'] = target
        if has_contents or b['t'] in ('anchor', 'img', 'fn'):
            continue
        txt = b['text'].strip()
        if not (TOC_HEAD_RE.match(txt) and len(txt) < 40):
            continue
        found, miss = [], 0
        for j in range(i + 1, min(len(blocks), i + 600)):
            c = blocks[j]
            if c['t'] == 'h1' and len(found) >= 3:
                break
            if c['t'] != 'p':
                continue
            e = entry(c)
            if e:
                found.append((j, c, e))
                miss = 0
            else:
                miss += 1
                if miss >= 4 and len(found) >= 3:
                    break
        if len(found) >= 3:
            for j, c, e in found:
                if 'href' not in c:
                    h = resolve(j, e[0], e[1])
                    if h:
                        c['href'] = h
                c['toc'] = True
                c['toc_region'] = True

    active, level = False, 9
    for b in blocks:
        if b['t'] in ('anchor', 'img', 'fn'):
            continue
        txt = b['text'].strip()
        if b['t'].startswith('h'):
            lv = int(b['t'][1])
            if PAGEREF_HEAD_RE.match(txt) and not doc.outline:
                active, level = True, lv
            elif active and lv <= level:
                active = False
            continue
        if active and b['t'] == 'p' and not b.get('toc_region'):
            b['pagerefs'] = True
    for i, b in enumerate(blocks):
        if b['t'] == 'p' and b.get('toc') and 'href' not in b and not b.get('pagerefs'):
            e = entry(b)
            if e:
                h = resolve(i, e[0], e[1])
                if h:
                    b['href'] = h


def xref_tokens(doc, tok, index_mode=False):
    """Link 'p 30', 'pp 30-32', 'Chapter 4' (and bare numbers in index mode)."""
    labels, chap = doc.labels, doc.chapters

    def number_link(label, text, abbreviated_from=None):
        if abbreviated_from and len(label) < len(abbreviated_from):
            start = int(abbreviated_from)
            power = 10 ** len(label)
            end = start // power * power + int(label)
            label = str(end if end >= start else end + power)
        page = labels.get(label)
        return '<a class="xref" href="#page-%d">%s</a>' % (page, text) if page else text

    def page_link(match):
        prefix, first, separator, last = match.groups()
        linked = number_link(first, prefix + first)
        if last:
            linked += separator + number_link(last, last, first)
        return linked

    def chap_link(match):
        target = chap.get(chapter_key(match[1]))
        return '<a class="xref" href="#%s">%s</a>' % (target, match[0]) if target else match[0]

    def num_link(match):
        first, separator, last = match.groups()
        linked = number_link(first, first)
        if last:
            linked += separator + number_link(last, last, first)
        return linked

    if index_mode:
        parts = re.split(r'(\([^)]*\)|\[[^\]]*\])', tok)
        return ''.join(x if i % 2 else NUM_TOKEN_RE.sub(num_link, x) for i, x in enumerate(parts))
    tok = PAGE_REF_RE.sub(page_link, tok)
    return CHAP_REF_RE.sub(chap_link, tok) if chap else tok


def _link_nums(s, refs):
    return NUM_RE.sub(
        lambda m: ('<a class="cite" href="#ref-%s">%s</a>' % (m.group(0), m.group(0)))
        if m.group(0) in refs else m.group(0), s)


def linkify(doc, h, refs, sup_cites=False, index_mode=False):
    out, in_a, in_sup = [], 0, False
    for tok in re.split(r'(<[^>]*>)', h):
        if tok.startswith('<'):
            if re.match(r'<a\b', tok):
                in_a += 1
            elif tok == '</a>':
                in_a = max(0, in_a - 1)
            elif tok == '<sup>':
                in_sup = True
            elif tok == '</sup>':
                in_sup = False
        elif not in_a:
            if in_sup:
                if sup_cites and CIT_SUP_RE.fullmatch(tok):
                    tok = _link_nums(tok, refs)
            elif index_mode:
                tok = xref_tokens(doc, tok, True)
            else:
                tok = CIT_BRACKET_RE.sub(
                    lambda m: '[' + _link_nums(m.group(1), refs) + ']', tok)
                if doc.labels or doc.chapters:
                    tok = xref_tokens(doc, tok)
        out.append(tok)
    return ''.join(out)


def render(doc, blocks, refs, title, author, indent_style):
    note_pages = {b['page'] for b in blocks if b['t'] == 'fn'}
    body = []
    in_list = False
    for b in blocks:
        t = b['t']
        sup_cites = bool(refs) and b['page'] not in note_pages
        bullet = t == 'p' and not b.get('pagerefs') and not b['ref'] and BULLET_RE.match(b['text'])
        if bullet and not in_list:
            body.append('<ul>')
        elif in_list and not bullet:
            body.append('</ul>')
        in_list = bool(bullet)
        if t == 'img':
            body.append('<p class="fig"><img src="%s" alt=""/></p>' % _html.escape(b['src']))
        elif t == 'anchor':
            body.append('<div id="page-%d"></div>' % b['page'])
        elif t == 'fn':
            marker = '<sup>%s</sup>' % _html.escape(b['label'])
            if b.get('backlink'):
                marker = '<a href="#%s">%s</a>' % (b['backlink'], marker)
            body.append('<p class="footnote" id="%s">%s %s</p>' %
                        (b['id'], marker, linkify(doc, b['html'], refs)))
        elif t.startswith('h'):
            attrs = ' id="%s"' % b['id'] if b.get('id') else ''
            classes = []
            if b.get('centered'):
                classes.append('c')
            if b.get('chapter_start'):
                classes.append('chapter-start')
            if classes:
                attrs += ' class="%s"' % ' '.join(classes)
            body.append('<%s%s>%s</%s>' % (t, attrs, linkify(doc, b['html'], refs, sup_cites), t))
        else:
            cls, style, attrs = [], [], ''
            if b.get('chapter_start'):
                cls.append('chapter-start')
            if b['ref']:
                cls.append('ref')
                if b.get('ref_id'):
                    attrs = ' id="ref-%s"' % b['ref_id']
            else:
                if indent_style and b['indent']:
                    cls.append('ind')
                if b.get('centered'):
                    cls.append('c')
                if b.get('toc'):
                    cls.append('toc')
                if b.get('mleft'):
                    style.append('margin-left:%gem' % b['mleft'])
                if b.get('fs'):
                    style.append('font-size:%gem' % b['fs'])
            if b.get('id') and not attrs:
                attrs = ' id="%s"' % b['id']
            if b['ref']:
                h = b['html']
            elif b.get('pagerefs'):
                h = linkify(doc, b['html'], refs, sup_cites, index_mode=True)
            elif b.get('href') and '<a ' not in b['html']:
                h = '<a class="toc-link" href="%s">%s</a>' % (_html.escape(b['href'], quote=True), b['html'])
            else:
                h = linkify(doc, b['html'], refs, sup_cites)
            tag = 'li' if bullet else 'p'
            if bullet:
                h = re.sub(r'^(\s*(?:<[^>]+>\s*)*)[•▪◦●■]\s*', r'\1', h, count=1)
            body.append('<%s%s%s%s>%s</%s>' % (tag,
                ' class="%s"' % ' '.join(cls) if cls else '',
                ' style="%s"' % ';'.join(style) if style else '', attrs, h, tag))
    if in_list:
        body.append('</ul>')
    mode = doc.options['justify']
    just = (mode == 'always') or (mode == 'auto' and doc.just_total
                                  and doc.just_hits / float(doc.just_total) > 0.45)
    css = CSS_BASE
    css += ('p { text-align: justify; }\n' if just else '')
    css += (CSS_INDENT if indent_style else CSS_MARGIN)
    css += CSS_TAIL
    head = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<meta charset="utf-8"/><title>%s</title>%s'
            '<link rel="stylesheet" type="text/css" href="style.css"/></head><body>\n'
            % (_html.escape(title or 'Document'),
               '<meta name="author" content="%s"/>' % _html.escape(author, quote=True)
               if author else ''))
    return head + '\n'.join(body) + '\n</body></html>\n', css


CSS_BASE = """\
h1, h2, h3, h4 { margin: 1.2em 0 0.5em; page-break-after: avoid; }
h1 { font-size: 1.6em; } h2 { font-size: 1.35em; } h3 { font-size: 1.15em; } h4 { font-size: 1em; }
sup, sub { line-height: 0; font-size: 0.75em; }
code { font-family: monospace; font-size: 0.9em; }
a.noteref, a.cite, a.toc-link, a.xref { text-decoration: none; }
a.xref { text-decoration: underline; }
.footnote { font-size: 0.85em; margin: 0.2em 0 0.2em 1em; text-indent: 0; text-align: left; }
.ref { font-size: 0.92em; margin: 0 0 0.4em 2em; text-indent: -2em; text-align: left; }
.fig { text-align: center; text-indent: 0; margin: 1em 0; }
img { max-width: 100%; height: auto; }
"""
CSS_MARGIN = "p { margin: 0 0 0.8em 0; text-indent: 0; }\n"
CSS_INDENT = ("p { margin: 0; text-indent: 0; }\n"
              "p.ind { text-indent: 1.4em; }\n")
CSS_TAIL = (".c { text-align: center; text-indent: 0; }\n"
            "p.toc { text-align: left; text-indent: 0; margin: 0 0 0.3em 0; }\n")


def write_package(doc, blocks, work_dir, title, author):
    identifier = 'urn:uuid:' + str(uuid4())
    ncx = ET.Element('ncx', {'xmlns': 'http://www.daisy.org/z3986/2005/ncx/', 'version': '2005-1'})
    head = ET.SubElement(ncx, 'head')
    ET.SubElement(head, 'meta', name='dtb:uid', content=identifier)
    ET.SubElement(ET.SubElement(ncx, 'docTitle'), 'text').text = title
    counter = 0

    def add_points(parent, entries):
        nonlocal counter
        for label, target, children in entries:
            counter += 1
            point = ET.SubElement(parent, 'navPoint', id='toc-%d' % counter)
            ET.SubElement(ET.SubElement(point, 'navLabel'), 'text').text = label
            ET.SubElement(point, 'content', src='book.html#' + target)
            add_points(point, children)

    add_points(ET.SubElement(ncx, 'navMap'), doc.navigation)
    ET.ElementTree(ncx).write(os.path.join(work_dir, 'toc.ncx'), encoding='utf-8', xml_declaration=True)

    opf = ET.Element('package', {'xmlns': 'http://www.idpf.org/2007/opf', 'version': '2.0',
                                 'unique-identifier': 'bookid'})
    metadata = ET.SubElement(opf, 'metadata', {'xmlns:dc': 'http://purl.org/dc/elements/1.1/'})
    ET.SubElement(metadata, 'dc:identifier', id='bookid').text = identifier
    ET.SubElement(metadata, 'dc:title').text = title
    if author:
        ET.SubElement(metadata, 'dc:creator').text = author
    ET.SubElement(metadata, 'dc:language').text = 'und'
    manifest = ET.SubElement(opf, 'manifest')

    def item(item_id, filename, media_type):
        ET.SubElement(manifest, 'item', {'id': item_id, 'href': filename, 'media-type': media_type})

    item('book', 'book.html', 'application/xhtml+xml')
    item('style', 'style.css', 'text/css')
    item('toc', 'toc.ncx', 'application/x-dtbncx+xml')
    images = sorted({b['src'] for b in blocks if b['t'] == 'img'})
    for index, name in enumerate(images):
        item('image-%d' % index, name, mimetypes.guess_type(name)[0] or 'application/octet-stream')
    ET.SubElement(ET.SubElement(opf, 'spine', toc='toc'), 'itemref', idref='book')
    output = os.path.join(work_dir, 'book.opf')
    ET.ElementTree(opf).write(output, encoding='utf-8', xml_declaration=True)
    return output


def convert(pdf_path, work_dir, title='', author='', pdftohtml_exe=None, log=None, opts=None,
            abort=None, progress=None, package=False, fixed_output=None):
    """Convert pdf_path into work_dir/book.html (+ style.css and images)."""
    log = log or (lambda *a: None)
    os.makedirs(work_dir, exist_ok=True)
    log('Extracting text and layout from the PDF...')
    xml_path = run_pdftohtml(pdf_path, work_dir, pdftohtml_exe, abort)
    doc = Doc(opts)
    parse(xml_path, doc)
    ocr_pages(doc, pdf_path, work_dir, pdftohtml_exe, log, abort, progress)
    detect_page_labels(doc)
    if fixed_output is not None:
        from .fixed_epub import write_fixed_epub
        write_fixed_epub(doc, pdf_path, work_dir, fixed_output, title, author, pdftohtml_exe, abort, progress)
        return fixed_output
    if not any(page.texts or page.images for page in doc.pages):
        raise RuntimeError('The PDF contains no extractable text or images.')
    analyse(doc)
    if doc.options['remove_running']:
        remove_running_text(doc)
    log('Rebuilding %d pages (body font %.1fpt)...' % (len(doc.pages), doc.body))
    blocks = []
    regions = navigation_regions(doc.outline)
    templates = index_gutters(doc, regions)
    end, region = 0, None
    template = None
    for pg in doc.pages:
        check_cancelled(abort)
        if pg.number >= end:
            region = template = None
        if pg.number in regions:
            end, region = regions[pg.number]
            template = templates.get(pg.number)
        blocks.extend(build_page(doc, pg, region, [template * pg.w] if template is not None else None))
    blocks = merge_across_breaks(doc, blocks)
    refs = mark_references(blocks)
    link_document(doc, blocks)
    log('Linked %d contents entries, %d page labels known.' % (
        sum(1 for b in blocks if b.get('href')), len(doc.labels)))
    paras = [b for b in blocks if b['t'] == 'p']
    indent_style = bool(paras) and sum(1 for b in paras if b['indent']) / float(len(paras)) > 0.4
    html_text, css = render(doc, blocks, refs, title, author, indent_style)
    out_html = os.path.join(work_dir, 'book.html')
    with open(out_html, 'w', encoding='utf-8') as f:
        f.write(html_text)
    with open(os.path.join(work_dir, 'style.css'), 'w', encoding='utf-8') as f:
        f.write(css)
    return write_package(doc, blocks, work_dir, title or 'Document', author) if package and doc.navigation else out_html


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit('usage: xml2html.py input.pdf output_dir')
    print(convert(os.path.abspath(sys.argv[1]), sys.argv[2], log=print))
