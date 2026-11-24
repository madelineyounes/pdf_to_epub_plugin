from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build():
    root = Path(__file__).resolve().parent
    output = root / 'dist' / 'pdf-to-epub-plugin.zip'
    output.parent.mkdir(exist_ok=True)
    sources = ('__init__.py', 'config.py', 'ui.py', 'xml2html.py', 'fixed_epub.py',
               'about.txt', 'images/icon.png', 'plugin-import-name-pdf_to_epub_plugin.txt')
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for name in sources:
            archive.write(root / name, name)
    return output


if __name__ == '__main__':
    print(build())
