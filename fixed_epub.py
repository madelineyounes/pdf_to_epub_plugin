import os
from datetime import datetime, timezone
from html import escape
from itertools import groupby
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from .xml2html import check_cancelled, find_renderer, rasterize_page

XHTML = 'http://www.w3.org/1999/xhtml'
EPUB = 'http://www.idpf.org/2007/ops'


def page_name(number):
    return 'page-%d.xhtml' % number


def render_page(page, pages):
    width, height = max(1, round(page.w)), max(1, round(page.h))
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<html xmlns="%s"><head><title>Page %d</title>' % (XHTML, page.number),
        '<meta name="viewport" content="width=%d,height=%d" />' % (width, height),
        '<style>html,body{margin:0;padding:0} body{position:relative;overflow:hidden}'
        'img{display:block;width:100%;height:100%}.line{position:absolute;color:transparent;'
        'white-space:pre;line-height:1}.line a{color:transparent;text-decoration:none}'
        '.line a:focus{outline:2px solid blue}</style></head>',
        '<body style="width:%dpx;height:%dpx">' % (width, height),
        '<img src="page-%d.png" alt="%s" />' % (page.number, '' if page.texts else 'Page %d' % page.number),
    ]
    for item in sorted(page.texts, key=lambda item: (item.top, item.left)):
        segments = []
        for href, group in groupby(item.segs, key=lambda segment: segment[3]):
            text = escape(''.join(segment[0] for segment in group))
            if href and href.startswith('#page-'):
                number = int(href[len('#page-'):])
                href = page_name(number) if number in pages else None
            segments.append('<a href="%s">%s</a>' % (escape(href, quote=True), text) if href else text)
        parts.append('<span class="line" style="left:%gpx;top:%gpx;font-size:%gpx">%s</span>' %
                     (item.left, item.top, item.font.size, ''.join(segments)))
    return ''.join(parts) + '</body></html>'


def outline_list(outline, pages):
    items = []
    for title, number, children in outline:
        if number in pages:
            nested = outline_list(children, pages)
            items.append('<li><a href="%s">%s</a>%s</li>' % (page_name(number), escape(title), nested))
    return '<ol>%s</ol>' % ''.join(items) if items else ''


def write_fixed_epub(doc, pdf_path, work_dir, output, title, author, pdftohtml_exe, abort, progress):
    renderer = find_renderer(pdftohtml_exe)
    if not renderer:
        raise RuntimeError('Fixed-layout EPUB requires pdftoppm (included with desktop Calibre).')
    pages = {page.number for page in doc.pages}
    if not pages:
        raise RuntimeError('No PDF pages could be read.')
    title = escape(title or 'Document')
    author = escape(author)
    nav = outline_list(doc.outline, pages)
    if not nav:
        nav = '<ol><li><a href="%s">%s</a></li></ol>' % (page_name(min(pages)), title)
    pagelist = []
    for page in doc.pages:
        pagelist.append('<li><a href="%s">%s</a></li>' %
                        (page_name(page.number), escape(page.label or str(page.number))))
    navigation = ('<html xmlns="%s" xmlns:epub="%s"><head><title>Contents</title></head><body>'
                  '<nav epub:type="toc"><h1>Contents</h1>%s</nav>'
                  '<nav epub:type="page-list" hidden="hidden"><ol>%s</ol></nav></body></html>'
                  % (XHTML, EPUB, nav, ''.join(pagelist)))
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="book.opf" media-type="application/oebps-package+xml" />'
                 '</rootfiles></container>')
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />']
    spine = []
    cover = next((number for name, number, _ in doc.outline if name.casefold() in ('cover', 'book cover')), None)
    image = os.path.join(work_dir, 'fixed-page.png')

    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        archive.writestr('META-INF/container.xml', container)
        archive.writestr('nav.xhtml', navigation)
        for index, page in enumerate(doc.pages):
            check_cancelled(abort)
            number = page.number
            if progress:
                progress(0.35 + 0.65 * index / len(pages), 'Rendering PDF page %d' % number)
            rasterize_page(pdf_path, number, image, renderer, abort, dpi=150)
            archive.write(image, 'page-%d.png' % number, compress_type=ZIP_STORED)
            archive.writestr(page_name(number), render_page(page, pages))
            manifest.append('<item id="p%d" href="%s" media-type="application/xhtml+xml" />' %
                            (number, page_name(number)))
            properties = ' properties="cover-image"' if number == cover else ''
            manifest.append('<item id="i%d" href="page-%d.png" media-type="image/png"%s />' %
                            (number, number, properties))
            spine.append('<itemref idref="p%d" />' % number)

        package = ('<?xml version="1.0" encoding="utf-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="book-id">urn:uuid:%s</dc:identifier><dc:title>%s</dc:title>%s'
                   '<dc:language>und</dc:language><meta property="dcterms:modified">%s</meta>'
                   '<meta property="rendition:layout">pre-paginated</meta>'
                   '<meta property="rendition:spread">none</meta></metadata>'
                   '<manifest>%s</manifest><spine>%s</spine></package>'
                   % (uuid4(), title, '<dc:creator>%s</dc:creator>' % author if author else '',
                      datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), ''.join(manifest), ''.join(spine)))
        archive.writestr('book.opf', package)
