import os
from functools import partial
from tempfile import TemporaryDirectory

from calibre.gui2 import Dispatcher, error_dialog
from calibre.gui2.actions import InterfaceAction
from calibre.gui2.threaded_jobs import ThreadedJob

ENGINE_KEYS = ('colors', 'mono', 'centering', 'block_indent', 'font_sizes',
               'remove_running', 'justify', 'ocr_language', 'chapter_split')


def convert_pdf_to_epub(book_id, title, authors, pdf_path, epub_path, settings,
                        notifications=None, abort=None, log=None):
    """Runs in a worker thread: PDF -> layout-aware HTML -> EPUB."""
    from calibre.customize.conversion import OptionRecommendation
    from calibre.ebooks.conversion.plumber import Plumber
    from calibre.ebooks.pdf.pdftohtml import PDFTOHTML
    from calibre.utils.logging import default_log
    from calibre_plugins.pdf_to_epub_plugin.xml2html import check_cancelled, convert

    log = log or default_log

    def report(fraction, message):
        check_cancelled(abort)
        if notifications is not None:
            notifications.put((fraction, message))

    with TemporaryDirectory(prefix='pdf2epub_') as work:
        report(0.05, 'Reading PDF layout')
        html_path = convert(pdf_path, work, title=title, author=authors,
                            pdftohtml_exe=PDFTOHTML, log=log.info,
                            opts={k: settings[k] for k in ENGINE_KEYS if k in settings},
                            abort=abort, progress=report, package=True,
                            fixed_output=epub_path if settings.get('fixed_layout') else None)
        if settings.get('fixed_layout'):
            report(1.0, 'EPUB ready')
            return book_id, epub_path
        report(0.5, 'Packaging EPUB')

        HIGH = OptionRecommendation.HIGH
        recs = [
            ('title', title, HIGH),
            ('authors', authors, HIGH),
            ('margin_top', -1, HIGH),      # < 0  => write no margin at all
            ('margin_bottom', -1, HIGH),
            ('margin_left', -1, HIGH),
            ('margin_right', -1, HIGH),
            ('base_font_size', 0, HIGH),
            ('disable_font_rescaling', True, HIGH),
            ('line_height', 0, HIGH),
            ('minimum_line_height', 0, HIGH),
            ('embed_all_fonts', False, HIGH),
            ('subset_embedded_fonts', False, HIGH),
            ('extra_css', '', HIGH),
            ('keep_ligatures', True, HIGH),
            ('change_justification', 'original', HIGH),
            ('remove_paragraph_spacing', False, HIGH),
            ('insert_blank_line', False, HIGH),
            ('smarten_punctuation', False, HIGH),
            ('unsmarten_punctuation', False, HIGH),
            ('enable_heuristics', False, HIGH),
            ('linearize_tables', False, HIGH),
            ('preserve_cover_aspect_ratio', True, HIGH),
            ('no_default_epub_cover', True, HIGH),
        ]
        if settings['chapter_split'] and html_path.endswith('.opf'):
            recs += [('chapter', '', HIGH), ('chapter_mark', 'none', HIGH),
                     ('page_breaks_before', '//*[contains(concat(" ", normalize-space(@class), " "), " chapter-start ")]', HIGH)]
        elif settings['chapter_split']:
            recs += [
                ('chapter', '//h:h1', HIGH),
                ('page_breaks_before', '//h:h1', HIGH),
                ('chapter_mark', 'pagebreak', HIGH),
                ('level1_toc', '//h:h1', HIGH),
                ('level2_toc', '//h:h2', HIGH),
                ('level3_toc', '//h:h3', HIGH),
            ]
        else:
            recs += [('chapter', '', HIGH), ('chapter_mark', 'none', HIGH),
                     ('page_breaks_before', None, HIGH)]
        plumber = Plumber(html_path, epub_path, log,
                          report_progress=lambda fraction, message='': report(0.5 + 0.5 * fraction, message))
        plumber.merge_ui_recommendations(recs)
        plumber.run()
    return book_id, epub_path


def convert_library_book(db, book_id, book_uuid, title, authors, settings,
                         notifications=None, abort=None, log=None):
    from calibre_plugins.pdf_to_epub_plugin.xml2html import check_cancelled

    check_cancelled(abort)
    if db.field_for('uuid', book_id) != book_uuid:
        raise RuntimeError('The selected book is no longer in the library.')
    staging = TemporaryDirectory(prefix='pdf2epub_job_')
    try:
        pdf = os.path.join(staging.name, 'source.pdf')
        epub = os.path.join(staging.name, 'book.epub')
        with open(pdf, 'wb') as stream:
            db.copy_format_to(book_id, 'PDF', stream)
        convert_pdf_to_epub(book_id, title, authors, pdf, epub, settings,
                            notifications=notifications, abort=abort, log=log)
        check_cancelled(abort)
        return staging
    except BaseException:
        staging.cleanup()
        raise


class PDFToEPUBAction(InterfaceAction):
    name = 'PDF to EPUB (Keep Formatting)'
    action_spec = ('PDF → EPUB', None,
                   'Convert selected PDFs to EPUB, preserving formatting and links', None)
    action_type = 'current'

    def genesis(self):
        self.qaction.setIcon(get_icons('images/icon.png'))
        self.qaction.triggered.connect(self.convert_selected)

    def convert_selected(self):
        from calibre_plugins.pdf_to_epub_plugin.config import prefs

        view = self.gui.library_view
        ids = list(view.get_selected_ids())
        if not ids:
            return error_dialog(self.gui, 'No books selected',
                                'Select one or more books that have a PDF.',
                                show=True)

        settings = {k: prefs[k] for k in prefs.defaults}
        db = self.gui.current_db.new_api
        started = skipped = existing = 0
        for book_id in ids:
            if not db.has_format(book_id, 'PDF'):
                skipped += 1
                continue
            if not settings['replace_existing'] and db.has_format(book_id, 'EPUB'):
                existing += 1
                continue
            title = db.field_for('title', book_id) or 'Untitled'
            authors = ' & '.join(db.field_for('authors', book_id) or ())

            book_uuid = db.field_for('uuid', book_id)
            job = ThreadedJob(
                'pdf_to_epub',
                'PDF → EPUB: %s' % title,
                convert_library_book,
                (db, book_id, book_uuid, title, authors, settings),
                {},
                Dispatcher(partial(self._done, db, book_id, book_uuid, settings['replace_existing'])),
            )
            self.gui.job_manager.run_threaded_job(job)
            started += 1

        if not started:
            if existing:
                error_dialog(self.gui, 'EPUB already exists',
                             'Every selected book already has an EPUB. Enable "Replace an '
                             'existing EPUB" in the plugin settings to overwrite.', show=True)
            elif skipped:
                error_dialog(self.gui, 'No PDFs found',
                             'None of the selected books have a PDF format.', show=True)
        else:
            self.gui.status_bar.show_message(
                'Converting %d PDF(s) to EPUB...' % started, 3000)

    def _done(self, db, book_id, book_uuid, replace, job):
        staging = getattr(job, 'result', None)
        try:
            if job.killed:
                return
            if job.failed:
                return self.gui.job_exception(job, dialog_title='PDF → EPUB failed')
            if self.gui.current_db.new_api is not db:
                return error_dialog(self.gui, 'Library changed',
                                    'Switch back to the original library and run the conversion again.', show=True)
            if db.field_for('uuid', book_id) != book_uuid:
                return error_dialog(self.gui, 'Book changed',
                                    'The original book is no longer in this library.', show=True)
            epub = os.path.join(staging.name, 'book.epub')
            try:
                imported = db.add_format(book_id, 'EPUB', epub, replace=replace)
            except Exception as exc:
                return error_dialog(self.gui, 'EPUB import failed', str(exc), show=True)
            if imported:
                self.gui.library_view.model().refresh_ids([book_id])
            else:
                self.gui.status_bar.show_message('Kept the existing EPUB.', 5000)
        finally:
            if staging is not None:
                staging.cleanup()
