# PDF to EPUB (Keep Formatting)

For Kim, the cutest lawyer ever. I made this for you.

Convert PDFs in your Calibre library to EPUB. Reflowable EPUBs let you adjust text size and margins. Fixed-layout EPUBs keep the original page appearance, including columns, tables, and artwork.

## Install and convert a PDF

1. Download `pdf-to-epub-plugin.zip` from the GitHub release.
2. In Calibre, open **Preferences → Plugins → Load plugin from file** and select the ZIP.
3. Restart Calibre. If you do not see **PDF → EPUB**, add the action under **Preferences → Toolbars & menus**.
4. Select the book or books you want to convert and click **PDF → EPUB**. Calibre adds the EPUB to the same book record and keeps the PDF.

Open **Customize plugin** to choose fixed layout, change the OCR language, or allow replacement of existing EPUBs.

## Convert scanned pages

Install Tesseract and the language data you need. Calibre looks for Tesseract on `PATH` and in its usual Program Files location on Windows. For a custom installation, set `PDF_TO_EPUB_TESSERACT` to the full executable path before starting Calibre. [Tesseract installation guide](https://tesseract-ocr.github.io/tessdoc/Installation.html)

The default OCR language is `eng`. Enter multiple language codes with `+`, such as `eng+fra`. OCR runs locally on pages that look scanned. When OCR is unavailable or cannot read a page, the plugin keeps its extracted image. Complex tables and artwork are best kept in fixed layout. Review recognized text and inferred reading order before relying on the reflowable EPUB for citations.

## Build from source

Run `python build_plugin.py` to create `dist/pdf-to-epub-plugin.zip`. The build includes the empty import marker that Calibre uses to name the plugin package. The example PDFs stay local.

Read the [code walkthrough](docs/how-it-works.md) for a diagram and an explanation of each step. The original toolbar icon is available under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
