"""PDFium runs in a disposable spawn child, never in the service thread."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from threading import Event, Thread

MAX_PDF_BYTES = 512 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_PNG_BYTES = 18 * 1024 * 1024


def _watch_parent(connection, stopped: Event) -> None:
    # The original duplex pipe, rather than a reopened PID, proves the parent
    # is still present even while PDFium is inside a native render call.
    while not stopped.wait(.2):
        try:
            if connection.poll(0):
                connection.recv_bytes(8)
                os._exit(71)
        except (EOFError, OSError):
            os._exit(71)


def pdf_worker(connection, operation: str, source: str, page_number, output) -> None:
    stopped = Event()
    watcher = Thread(target=_watch_parent, args=(connection, stopped), daemon=True)
    watcher.start()
    resources = []
    result = {"ok": False, "code": "PDF_RENDER_FAILED"}
    try:
        import pypdfium2 as pdfium

        source_path = Path(source)
        if not source_path.is_file() or not 0 < source_path.stat().st_size <= MAX_PDF_BYTES:
            raise ValueError("PDF_SOURCE_SIZE_INVALID")
        document = pdfium.PdfDocument(str(source_path))
        resources.append(document)
        count = len(document)
        if not 0 < count <= 100_000:
            raise ValueError("PDF_PAGE_COUNT_INVALID")
        if operation == "count":
            result = {"ok": True, "page_count": count}
        elif operation == "render":
            if type(page_number) is not int or not 1 <= page_number <= count:
                raise ValueError("PDF_PAGE_OUT_OF_RANGE")
            page = document[page_number - 1]
            resources.append(page)
            width, height = page.get_size()
            scale = 150 / 72
            if (not all(math.isfinite(v) and v > 0 for v in (width, height))
                    or math.ceil(width * scale) * math.ceil(height * scale) > MAX_PIXELS):
                raise ValueError("PDF_PAGE_IMAGE_TOO_LARGE")
            bitmap = page.render(scale=scale, force_bitmap_format=pdfium.raw.FPDFBitmap_BGR)
            resources.append(bitmap)
            import cv2

            image = bitmap.to_numpy()
            if image.ndim != 3 or image.shape[2] != 3 or image.shape[0] * image.shape[1] > MAX_PIXELS:
                raise ValueError("PDF_PAGE_IMAGE_INVALID")
            ok, encoded = cv2.imencode(".png", image)
            if not ok or not 0 < encoded.nbytes <= MAX_PNG_BYTES:
                raise ValueError("PDF_PAGE_IMAGE_TOO_LARGE")
            with Path(output).open("xb") as handle:
                handle.write(encoded.tobytes())
            result = {"ok": True, "page_count": count, "png_bytes": int(encoded.nbytes),
                      "width": int(image.shape[1]), "height": int(image.shape[0])}
        else:
            raise ValueError("PDF_OPERATION_INVALID")
    except ImportError:
        result = {"ok": False, "code": "PDF_RENDERER_UNAVAILABLE"}
    except ValueError as error:
        allowed = {"PDF_SOURCE_SIZE_INVALID", "PDF_PAGE_COUNT_INVALID", "PDF_PAGE_OUT_OF_RANGE",
                   "PDF_PAGE_IMAGE_TOO_LARGE", "PDF_PAGE_IMAGE_INVALID", "PDF_OPERATION_INVALID"}
        result = {"ok": False, "code": str(error) if str(error) in allowed else "PDF_RENDER_FAILED"}
    except Exception:
        # File paths, document content and native exception text never leave
        # this child through an error response.
        result = {"ok": False, "code": "PDF_RENDER_FAILED"}
    finally:
        failed_close = False
        for resource in reversed(resources):
            try:
                resource.close()
            except Exception:
                failed_close = True
        if failed_close:
            result = {"ok": False, "code": "PDF_RESOURCE_CLOSURE_FAILED"}
        stopped.set()
        watcher.join(.5)
        try:
            connection.send_bytes(json.dumps(result, separators=(",", ":")).encode("ascii"))
        except (OSError, EOFError):
            pass
        finally:
            connection.close()
