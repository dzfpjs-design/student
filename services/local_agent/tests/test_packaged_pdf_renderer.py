from pathlib import Path
import multiprocessing as mp

import cv2
import pytest
import pypdfium2 as pdfium

from home_study_agent.pdf_renderer import PdfRenderError, _request, packaged_pdf_page_count, packaged_pdf_render_page
from home_study_agent.pdf_render_worker import _watch_parent
from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline


@pytest.fixture
def paper(tmp_path):
    path = tmp_path / 'public.pdf'
    document = pdfium.PdfDocument.new()
    page = document.new_page(72, 72)
    page.close()
    document.save(path)
    document.close()
    return path


def test_real_spawn_count_render_and_no_poppler_pipeline(paper, tmp_path, monkeypatch):
    monkeypatch.setattr('home_study_agent.knowledge_ingestion.textbook_pdf.shutil.which', lambda _: None)
    assert TextbookPdfOcrPipeline._pdf_page_count(paper) == 1
    image = TextbookPdfOcrPipeline._render_page(paper, 1)
    assert image.shape == (150, 150, 3) and image.mean() == 255
    assert not mp.active_children()


def test_invalid_pdf_is_failure_with_no_original_path_or_error_text(tmp_path):
    path = tmp_path / 'private-child-name.pdf'
    path.write_text('malformed private contents')
    with pytest.raises(PdfRenderError, match='^PDF_RENDER_FAILED$'):
        packaged_pdf_page_count(path)
    assert not mp.active_children()


def test_page_range_and_existing_output_fail_closed(paper, tmp_path):
    with pytest.raises(PdfRenderError, match='^PDF_PAGE_OUT_OF_RANGE$'):
        packaged_pdf_render_page(paper, 2, tmp_path/'absent.png')
    old = tmp_path/'original.png'
    old.write_bytes(b'original-user-work')
    with pytest.raises(PdfRenderError, match='^PDF_RENDER_FAILED$'):
        packaged_pdf_render_page(paper, 1, old)
    assert old.read_bytes() == b'original-user-work'
    assert not mp.active_children()


def test_timeout_terminates_only_own_spawn_and_closes_process(paper):
    with pytest.raises(PdfRenderError, match='^PDF_WORKER_TIMEOUT$'):
        _request('count', paper, timeout_seconds=.001)
    assert not mp.active_children()


def test_render_dimension_limit_rejects_without_large_allocation(tmp_path):
    path = tmp_path/'oversize.pdf'
    document = pdfium.PdfDocument.new()
    page = document.new_page(100_000, 100_000)
    page.close()
    document.save(path)
    document.close()
    with pytest.raises(PdfRenderError, match='^PDF_PAGE_IMAGE_TOO_LARGE$'):
        packaged_pdf_render_page(path, 1, tmp_path/'none.png')
    assert not mp.active_children()


def test_original_pipe_eof_exits_watchdog_without_pid_lookup():
    context = mp.get_context('spawn')
    parent, child = context.Pipe(duplex=True)
    stopped = context.Event()
    process = context.Process(target=_watch_parent, args=(child, stopped), daemon=True)
    process.start()
    child.close()
    parent.close()
    try:
        process.join(3)
        assert not process.is_alive() and process.exitcode == 71
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        process.close()
    assert not mp.active_children()
