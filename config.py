"""Settings for the PDF to EPUB plugin (shown via Preferences > Plugins > Customize)."""
from calibre.utils.config import JSONConfig
from calibre.gui2 import info_dialog

try:
    from qt.core import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
except ImportError:
    from PyQt5.Qt import QCheckBox, QComboBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

prefs = JSONConfig('plugins/pdf_to_epub')
prefs.defaults.update({
    'colors': True,
    'mono': True,
    'centering': True,
    'block_indent': True,
    'font_sizes': True,
    'remove_running': True,
    'justify': 'auto',
    'chapter_split': True,
    'replace_existing': False,
    'ocr_language': 'eng',
    'fixed_layout': False,
})

CHECKS = [
    ('colors', 'Keep coloured text'),
    ('mono', 'Keep monospace (code) text'),
    ('centering', 'Keep centred headings and lines centred'),
    ('block_indent', 'Keep indented blocks (quotations) indented'),
    ('font_sizes', 'Keep small print / larger text relative sizes'),
    ('remove_running', 'Remove running headers, footers and page numbers'),
    ('chapter_split', 'Start each chapter on a new page in reflowable EPUBs'),
    ('fixed_layout', 'Keep the PDF page layout (fixed-layout EPUB)'),
    ('replace_existing', 'Replace an existing EPUB format on the book'),
]


class ConfigWidget(QWidget):
    def __init__(self):
        QWidget.__init__(self)
        layout = QVBoxLayout(self)
        note = QLabel(
            'Reflowable EPUBs let your e-reader adjust text size and margins. Fixed-layout EPUBs '
            'keep each page as it appears in the PDF. Scanned pages use Tesseract if it is installed.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.boxes = {}
        for key, text in CHECKS:
            cb = QCheckBox(text)
            cb.setChecked(bool(prefs[key]))
            layout.addWidget(cb)
            self.boxes[key] = cb
        form = QFormLayout()
        self.just = QComboBox()
        self.just.addItem('Match the PDF (automatic)', 'auto')
        self.just.addItem('Never justify (let the e-reader decide)', 'never')
        self.just.addItem('Always justify', 'always')
        idx = self.just.findData(prefs['justify'])
        self.just.setCurrentIndex(max(idx, 0))
        form.addRow('Text alignment', self.just)
        self.ocr_language = QLineEdit(str(prefs['ocr_language']))
        self.ocr_language.setPlaceholderText('eng or eng+fra')
        form.addRow('OCR languages', self.ocr_language)
        layout.addLayout(form)
        about = QPushButton('About this plugin')
        about.clicked.connect(self.show_about)
        layout.addWidget(about)
        layout.addStretch(1)

    def save_settings(self):
        for key, cb in self.boxes.items():
            prefs[key] = bool(cb.isChecked())
        prefs['justify'] = str(self.just.currentData())
        prefs['ocr_language'] = self.ocr_language.text().strip() or 'eng'

    def show_about(self):
        info_dialog(self, 'About this plugin', get_resources('about.txt').decode('utf-8'), show=True)
