import subprocess
import tempfile
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from unittest.mock import patch

import xml2html as converter


def text_item(text, left=42, top=25, size=12):
    return converter.Item('text', top, left, len(text) * 5, size, converter.Font(size, ''),
                          [(text, False, False, None)])


class ConversionTests(unittest.TestCase):
    def test_label_sequence_beats_running_header_letters(self):
        doc = converter.Doc()
        for number in range(1, 5):
            page = converter.Page(number, 400, 600)
            page.texts = [text_item('I')]
            if number != 3:
                page.texts.append(text_item(str(number + 14), top=550))
            doc.pages.append(page)
        converter.detect_page_labels(doc)
        self.assertEqual(doc.labels, {'15': 1, '16': 2, '17': 3, '18': 4})

    def test_range_expands_without_changing_the_text(self):
        doc = converter.Doc()
        doc.labels = {'39': 64, '138': 163, '139': 164}
        self.assertEqual(converter.xref_tokens(doc, '138–39', index_mode=True),
                         '<a class="xref" href="#page-163">138</a>–'
                         '<a class="xref" href="#page-164">39</a>')
        self.assertIn('>p. 39<', converter.xref_tokens(doc, 'p. 39'))
        self.assertIsNone(converter.fix_href('other.pdf#39'))

    def test_join_preserves_a_hard_hyphen_without_other_evidence(self):
        doc = converter.Doc()
        block = {'text': 'well-', 'html': 'well-'}
        converter.join_line(doc, block, 'known', 'known')
        self.assertEqual(block, {'text': 'well-known', 'html': 'well-known'})

    def test_small_print_footnote_keeps_continuation_and_backlink(self):
        doc = converter.Doc()
        first = converter.Page(1, 600, 800)
        first.texts = [text_item('Body text.', top=150), text_item('1', left=42, top=401, size=9),
                       text_item('The footnote starts', left=64, top=401, size=9),
                       text_item('(continued on the next page)', left=230, top=730, size=8)]
        second = converter.Page(2, 600, 800)
        second.texts = [text_item('and ends here.', left=64, top=600, size=9)]
        blocks = converter.build_page(doc, first)
        converter.build_page(doc, second)
        note, = (block for block in blocks if block['t'] == 'fn')
        self.assertEqual(note['html'], 'The footnote starts and ends here.')
        body = {'html': 'Body text.<sup>,1</sup>'}
        converter.link_notes([body], [note])
        self.assertIn('href="#fn-1-0"', body['html'])
        self.assertEqual(note['backlink'], 'nr-fn-1-0')

    def test_extractor_rejects_stale_xml_and_checks_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'src.xml').write_text('<pdf2xml/>', encoding='utf-8')
            failed = subprocess.CompletedProcess([], 1, b'', b'bad PDF')
            with patch.object(converter, 'run_command', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'bad PDF'):
                    converter.run_pdftohtml('bad.pdf', directory, 'pdftohtml')
        abort = Event()
        abort.set()
        with self.assertRaises(CancelledError):
            converter.run_command(['unused'], '.', abort)


if __name__ == '__main__':
    unittest.main()
