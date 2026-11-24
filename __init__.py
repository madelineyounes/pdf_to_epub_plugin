from calibre.customize import InterfaceActionBase


class PDFToEPUBPlugin(InterfaceActionBase):
    name = 'PDF to EPUB (Keep Formatting)'
    description = ('Convert PDFs to reflowable or fixed-layout EPUBs with bookmarks and optional OCR. '
                   'For Kim, the cutest lawyer ever. I made this for you.')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Madeline Younes'
    version = (2, 1, 0)
    minimum_calibre_version = (5, 0, 0)

    actual_plugin = 'calibre_plugins.pdf_to_epub_plugin.ui:PDFToEPUBAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.pdf_to_epub_plugin.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
