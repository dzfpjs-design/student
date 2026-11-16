"""Bounded packaged PDF rendering, with the existing Poppler path retained."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
from pathlib import Path
import shutil
from time import monotonic

from .pdf_render_worker import pdf_worker


class PdfRenderError(RuntimeError):
    pass


def pdf_renderer_available() -> bool:
    try:
        return bool((shutil.which("pdfinfo") and shutil.which("pdftoppm")) or
                    (importlib.util.find_spec("pypdfium2") and importlib.util.find_spec("pypdfium2_raw")))
    except (ImportError, ValueError, OSError):
        return False


def _request(operation: str, source: Path, *, page_number=None, output=None,
             timeout_seconds: float) -> dict:
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    worker = context.Process(target=pdf_worker,
                             args=(child, operation, str(source), page_number, str(output) if output else None),
                             daemon=True)
    started = False
    result = None
    error = None
    deadline = monotonic() + timeout_seconds
    try:
        worker.start()
        started = True
        child.close()
        while monotonic() < deadline:
            if parent.poll(min(.05, max(0, deadline - monotonic()))):
                try:
                    result = json.loads(parent.recv_bytes(2048).decode("ascii"))
                except (EOFError, OSError, ValueError, UnicodeError):
                    raise PdfRenderError("PDF_WORKER_PROTOCOL_INVALID") from None
                break
            if not worker.is_alive():
                raise PdfRenderError("PDF_WORKER_EXITED_WITHOUT_RESULT")
        if result is None:
            raise PdfRenderError("PDF_WORKER_TIMEOUT")
        worker.join(max(0, min(2, deadline - monotonic())))
        if worker.is_alive() or worker.exitcode != 0:
            raise PdfRenderError("PDF_WORKER_CLOSURE_UNCONFIRMED")
        if type(result) is not dict or result.get("ok") is not True:
            codes = {"PDF_RENDERER_UNAVAILABLE", "PDF_SOURCE_SIZE_INVALID", "PDF_PAGE_COUNT_INVALID",
                     "PDF_PAGE_OUT_OF_RANGE", "PDF_PAGE_IMAGE_TOO_LARGE", "PDF_PAGE_IMAGE_INVALID",
                     "PDF_OPERATION_INVALID", "PDF_RESOURCE_CLOSURE_FAILED", "PDF_RENDER_FAILED"}
            code = result.get("code") if type(result) is dict else None
            raise PdfRenderError(code if code in codes else "PDF_WORKER_PROTOCOL_INVALID")
        expected_keys = {"ok", "page_count"} if operation == "count" else {"ok", "page_count", "width", "height", "png_bytes"}
        if set(result) != expected_keys:
            raise PdfRenderError("PDF_WORKER_PROTOCOL_INVALID")
        if type(result.get("page_count")) is not int or not 0 < result["page_count"] <= 100_000:
            raise PdfRenderError("PDF_WORKER_PROTOCOL_INVALID")
        if operation == "render":
            if (not all(type(result.get(k)) is int and result[k] > 0 for k in ("width", "height", "png_bytes"))
                    or result["width"] * result["height"] > 24_000_000
                    or result["png_bytes"] > 18 * 1024 * 1024
                    or not Path(output).is_file() or Path(output).stat().st_size != result["png_bytes"]):
                raise PdfRenderError("PDF_WORKER_PROTOCOL_INVALID")
    except PdfRenderError as failure:
        error = failure
    except Exception:
        error = PdfRenderError("PDF_WORKER_START_OR_IO_FAILED")
    finally:
        child.close()
        parent.close()
        if started:
            if worker.is_alive():
                worker.terminate()
                worker.join(2)
            if worker.is_alive():
                worker.kill()
                worker.join(2)
            if worker.is_alive():
                error = PdfRenderError("PDF_WORKER_CLOSURE_UNCONFIRMED")
            else:
                worker.close()
    if error:
        raise error
    return result


def packaged_pdf_page_count(source: Path) -> int:
    return _request("count", source, timeout_seconds=60)["page_count"]


def packaged_pdf_render_page(source: Path, page_number: int, output: Path) -> dict:
    return _request("render", source, page_number=page_number, output=output, timeout_seconds=90)
