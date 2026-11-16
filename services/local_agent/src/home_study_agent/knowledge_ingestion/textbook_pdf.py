"""Durable local OCR preparation for image-only textbook PDFs."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from ..local_ocr.service import RapidOcrEngine


_BOOK_PATTERN = re.compile(
    r"^(?P<grade>四年级|五年级|六年级|七年级|八年级|九年级)"
    r"(?P<volume>上册|下册)_(?P<subject>.+)_(?P<edition>.+)\.pdf$"
)
_GRADE_MAP = {"七年级": "初一", "八年级": "初二", "九年级": "初三"}
_STEM_SUBJECTS = {"数学", "物理", "化学", "科学"}
_FORMULA_MARKERS = frozenset("=+-×÷*/^√≤≥≠≈∑∫π")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TextbookBook:
    book_id: str
    relative_pdf_path: str
    title: str
    stage: str
    grade: str
    subject: str
    semester: str
    volume: str
    edition: str
    fingerprint: str


def parse_textbook_path(pdf_root: Path, path: Path) -> TextbookBook:
    relative = path.relative_to(pdf_root).as_posix()
    match = _BOOK_PATTERN.fullmatch(path.name)
    if not match:
        raise ValueError(f"unsupported textbook filename: {path.name}")
    raw_grade = match.group("grade")
    subject = "生物" if match.group("subject") == "生物学" else match.group("subject")
    volume = match.group("volume")
    stat = path.stat()
    return TextbookBook(
        book_id=f"njtb-{hashlib.sha256(relative.encode()).hexdigest()[:16]}",
        relative_pdf_path=relative,
        title=path.stem,
        stage="初中" if raw_grade in _GRADE_MAP else "小学",
        grade=_GRADE_MAP.get(raw_grade, raw_grade),
        subject=subject,
        semester="上学期" if volume == "上册" else "下学期",
        volume=volume,
        edition=match.group("edition"),
        fingerprint=f"size:{stat.st_size}:mtime_ns:{stat.st_mtime_ns}",
    )


class TextbookPdfOcrPipeline:
    """Checkpointed page OCR. PDF pixels and OCR stay on the local machine."""

    def __init__(
        self,
        pdf_root: Path,
        output_root: Path,
        metadata_db_path: Path,
        *,
        page_counter: Callable[[Path], int] | None = None,
        renderer: Callable[[Path, int], np.ndarray] | None = None,
        ocr_engine: Any | None = None,
    ):
        self.pdf_root = Path(pdf_root).resolve()
        self.output_root = Path(output_root).resolve()
        self.metadata_db_path = Path(metadata_db_path).resolve()
        self.page_counter = page_counter or self._pdf_page_count
        self.renderer = renderer or self._render_page
        self.ocr_engine = ocr_engine
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.metadata_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def prepare(self) -> dict:
        paths = sorted(self.pdf_root.rglob("*.pdf"), key=lambda item: item.as_posix())
        books = [parse_textbook_path(self.pdf_root, path) for path in paths]
        seen = {book.book_id for book in books}
        total_pages = changed_books = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE textbook_book SET status='missing',updated_at=?", (_now(),))
            for path, book in zip(paths, books):
                page_count = self.page_counter(path)
                if page_count < 1:
                    raise ValueError(f"textbook has no pages: {path}")
                prior = connection.execute(
                    "SELECT fingerprint,page_count FROM textbook_book WHERE book_id=?", (book.book_id,),
                ).fetchone()
                changed = bool(prior and (prior["fingerprint"] != book.fingerprint or prior["page_count"] != page_count))
                changed_books += int(changed)
                connection.execute(
                    """INSERT INTO textbook_book(book_id,relative_pdf_path,title,stage,grade,subject,semester,volume,
                       edition,fingerprint,page_count,status,completed_pages,failed_pages,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,0,?,?)
                       ON CONFLICT(book_id) DO UPDATE SET relative_pdf_path=excluded.relative_pdf_path,title=excluded.title,
                       stage=excluded.stage,grade=excluded.grade,subject=excluded.subject,semester=excluded.semester,
                       volume=excluded.volume,edition=excluded.edition,fingerprint=excluded.fingerprint,
                       page_count=excluded.page_count,status=CASE WHEN textbook_book.status='completed' AND
                       textbook_book.fingerprint=excluded.fingerprint AND textbook_book.page_count=excluded.page_count
                       THEN 'completed' ELSE 'pending' END,updated_at=excluded.updated_at""",
                    (book.book_id, book.relative_pdf_path, book.title, book.stage, book.grade, book.subject,
                     book.semester, book.volume, book.edition, book.fingerprint, page_count, _now(), _now()),
                )
                if changed:
                    connection.execute(
                        """UPDATE textbook_page SET status='pending',attempt_count=0,ocr_text_relative_path=NULL,
                           ocr_engine=NULL,ocr_confidence=NULL,line_count=0,layout_json='[]',manual_review_required=0,
                           formula_review_required=0,error_code=NULL,error_detail=NULL,completed_at=NULL,updated_at=?
                           WHERE book_id=?""", (_now(), book.book_id),
                    )
                for page_number in range(1, page_count + 1):
                    page_id = f"{book.book_id}-p{page_number:04d}"
                    connection.execute(
                        """INSERT OR IGNORE INTO textbook_page(page_id,book_id,page_number,status,attempt_count,
                           line_count,layout_json,manual_review_required,formula_review_required,created_at,updated_at)
                           VALUES(?,?,?,'pending',0,0,'[]',0,0,?,?)""",
                        (page_id, book.book_id, page_number, _now(), _now()),
                    )
                connection.execute(
                    "DELETE FROM textbook_page WHERE book_id=? AND page_number>?", (book.book_id, page_count),
                )
                total_pages += page_count
            if seen:
                placeholders = ",".join("?" for _ in seen)
                connection.execute(
                    f"UPDATE textbook_book SET status='missing',updated_at=? WHERE book_id NOT IN ({placeholders})",
                    (_now(), *sorted(seen)),
                )
            connection.commit()
        return {"books": len(books), "pages": total_pages, "changed_books": changed_books}

    def run(self, *, max_pages: int | None = None) -> dict:
        engine = self._healthy_engine()
        with self._connect() as connection:
            connection.execute(
                """UPDATE textbook_page SET status='pending',updated_at=?
                   WHERE status='processing' OR (status='failed' AND attempt_count<3)""", (_now(),),
            )
            connection.commit()
        processed = 0
        while max_pages is None or processed < max_pages:
            page = self._claim_page()
            if page is None:
                break
            try:
                self._process_page(page, engine)
            except Exception as error:
                self._fail_page(page["page_id"], error)
            self._refresh_book(page["book_id"])
            processed += 1
        return {**self.status(), "processed_this_run": processed}

    def run_pages(self, page_ids: list[str]) -> dict:
        engine = self._healthy_engine()
        processed = 0
        for page_id in dict.fromkeys(page_ids):
            with self._connect() as connection:
                page = connection.execute(
                    """SELECT p.*,b.relative_pdf_path,b.subject FROM textbook_page p
                       JOIN textbook_book b ON b.book_id=p.book_id WHERE p.page_id=?""", (page_id,),
                ).fetchone()
                if not page:
                    raise ValueError(f"unknown textbook page: {page_id}")
                if page["status"] in {"completed", "skipped_blank"}:
                    continue
                connection.execute(
                    """UPDATE textbook_page SET status='processing',attempt_count=attempt_count+1,
                       updated_at=? WHERE page_id=?""", (_now(), page_id),
                )
                connection.commit()
            try:
                self._process_page(page, engine)
            except Exception as error:
                self._fail_page(page_id, error)
            self._refresh_book(page["book_id"])
            processed += 1
        return {**self.status(), "processed_this_run": processed}

    def status(self) -> dict:
        with self._connect() as connection:
            pages = connection.execute(
                """SELECT COUNT(*) total,SUM(status='completed') completed,SUM(status='skipped_blank') skipped_blank,
                   SUM(status='skipped_nontext') skipped_nontext,
                   SUM(status='failed') failed,SUM(status='pending') pending,SUM(status='processing') processing
                   FROM textbook_page"""
            ).fetchone()
            books = connection.execute(
                "SELECT COUNT(*) total,SUM(status='completed') completed,SUM(status='missing') missing FROM textbook_book"
            ).fetchone()
        return {
            "books": {key: books[key] or 0 for key in books.keys()},
            "pages": {key: pages[key] or 0 for key in pages.keys()},
        }

    def _claim_page(self) -> sqlite3.Row | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT p.*,b.relative_pdf_path,b.subject FROM textbook_page p
                   JOIN textbook_book b ON b.book_id=p.book_id
                   WHERE (p.status='pending' OR (p.status='failed' AND p.attempt_count<3))
                     AND b.status!='missing'
                   ORDER BY CASE WHEN p.status='failed' THEN 0 ELSE 1 END,
                     b.relative_pdf_path,p.page_number LIMIT 1"""
            ).fetchone()
            if row:
                connection.execute(
                    """UPDATE textbook_page SET status='processing',attempt_count=attempt_count+1,
                       updated_at=? WHERE page_id=?""", (_now(), row["page_id"]),
                )
            connection.commit()
        return row

    def _healthy_engine(self) -> Any:
        engine = self.ocr_engine or RapidOcrEngine()
        health = engine.health() if hasattr(engine, "health") else {"ready": True}
        if not health.get("ready"):
            raise RuntimeError(f"local OCR unavailable: {health.get('reason', 'unknown')}")
        return engine

    def _process_page(self, page: sqlite3.Row, engine: Any) -> None:
        pdf_path = self.pdf_root / page["relative_pdf_path"]
        image = self.renderer(pdf_path, int(page["page_number"]))
        if image is None or min(image.shape[:2]) < 80:
            raise ValueError("rendered_page_invalid")
        lines = engine.recognize(image, "cpu")
        lines = sorted(lines, key=self._line_position)
        text_lines = [str(item.get("text") or "").strip() for item in lines if str(item.get("text") or "").strip()]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        ink_ratio = float(np.mean(gray < 245))
        if not text_lines:
            # Scanned books often use a solid colored flyleaf. It has a high
            # non-white ratio but essentially no contrast, and is still a true
            # blank page rather than failed OCR evidence.
            if ink_ratio < .01 or float(np.std(gray)) < 2.0:
                self._complete_blank(page["page_id"])
                return
            if int(page["attempt_count"] or 0) >= 2:
                self._complete_nontext(page["page_id"], ink_ratio=ink_ratio)
                return
            raise ValueError("ocr_empty_on_nonblank_page")
        confidence = sum(float(item.get("confidence") or 0) for item in lines) / max(1, len(lines))
        relative_text = Path("pages") / f"{page['page_id']}.txt"
        output = self.output_root / relative_text
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(text_lines), encoding="utf-8")
        formula_review = page["subject"] in _STEM_SUBJECTS and any(
            marker in "".join(text_lines) for marker in _FORMULA_MARKERS
        )
        layout = [{"text": item.get("text"), "confidence": item.get("confidence"), "box": item.get("box")} for item in lines]
        with self._connect() as connection:
            connection.execute(
                """UPDATE textbook_page SET status='completed',ocr_text_relative_path=?,ocr_engine=?,
                   ocr_confidence=?,line_count=?,layout_json=?,manual_review_required=?,formula_review_required=?,
                   error_code=NULL,error_detail=NULL,completed_at=?,updated_at=? WHERE page_id=?""",
                (relative_text.as_posix(), getattr(engine, "name", "local-ocr"), round(confidence, 4), len(text_lines),
                 json.dumps(layout, ensure_ascii=False), int(confidence < .78), int(formula_review),
                 _now(), _now(), page["page_id"]),
            )
            connection.commit()

    def _complete_blank(self, page_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE textbook_page SET status='skipped_blank',line_count=0,layout_json='[]',
                   error_code=NULL,error_detail=NULL,completed_at=?,updated_at=? WHERE page_id=?""",
                (_now(), _now(), page_id),
            )
            connection.commit()

    def _complete_nontext(self, page_id: str, *, ink_ratio: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE textbook_page SET status='skipped_nontext',line_count=0,layout_json=?,
                   manual_review_required=1,error_code=NULL,error_detail=NULL,completed_at=?,updated_at=?
                   WHERE page_id=?""",
                (json.dumps({"classification": "nontext_page", "ink_ratio": round(ink_ratio, 6)}),
                 _now(), _now(), page_id),
            )
            connection.commit()

    def _fail_page(self, page_id: str, error: Exception) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE textbook_page SET status='failed',error_code=?,error_detail=?,completed_at=?,updated_at=?
                   WHERE page_id=?""", (type(error).__name__, str(error)[:500], _now(), _now(), page_id),
            )
            connection.commit()

    def _refresh_book(self, book_id: str) -> None:
        with self._connect() as connection:
            counts = connection.execute(
                """SELECT COUNT(*) total,SUM(status IN ('completed','skipped_blank','skipped_nontext')) completed,
                   SUM(status='failed') failed,SUM(status IN ('pending','processing')) remaining
                   FROM textbook_page WHERE book_id=?""", (book_id,),
            ).fetchone()
            status = "completed" if not counts["remaining"] and not counts["failed"] else "failed" if not counts["remaining"] else "processing"
            connection.execute(
                """UPDATE textbook_book SET status=?,completed_pages=?,failed_pages=?,updated_at=? WHERE book_id=?""",
                (status, counts["completed"] or 0, counts["failed"] or 0, _now(), book_id),
            )
            connection.commit()

    @staticmethod
    def _line_position(item: dict) -> tuple[int, int]:
        box = item.get("box") or []
        if not box:
            return 0, 0
        points = np.asarray(box)
        return int(points[:, 1].min()), int(points[:, 0].min())

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        binary = shutil.which("pdfinfo")
        if not binary:
            from ..pdf_renderer import packaged_pdf_page_count
            return packaged_pdf_page_count(path)
        result = subprocess.run([binary, str(path)], capture_output=True, text=True, timeout=60, check=True)
        match = re.search(r"^Pages:\s+(\d+)$", result.stdout, re.MULTILINE)
        if not match:
            raise ValueError(f"pdf page count unavailable: {path}")
        return int(match.group(1))

    @staticmethod
    def _render_page(path: Path, page_number: int) -> np.ndarray:
        binary = shutil.which("pdftoppm")
        with tempfile.TemporaryDirectory(prefix="hsc-textbook-page-") as directory:
            prefix = Path(directory) / "page"
            if binary:
                subprocess.run(
                    [binary, "-f", str(page_number), "-l", str(page_number), "-singlefile", "-png", "-r", "150", str(path), str(prefix)],
                    capture_output=True, timeout=90, check=True,
                )
            else:
                from ..pdf_renderer import packaged_pdf_render_page
                packaged_pdf_render_page(path, page_number, prefix.with_suffix(".png"))
            image = cv2.imread(str(prefix.with_suffix(".png")), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("pdf_page_render_failed")
        return image

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
              PRAGMA journal_mode=WAL;
              PRAGMA foreign_keys=ON;
              CREATE TABLE IF NOT EXISTS textbook_book(
                book_id TEXT PRIMARY KEY,relative_pdf_path TEXT NOT NULL UNIQUE,title TEXT NOT NULL,stage TEXT NOT NULL,
                grade TEXT NOT NULL,subject TEXT NOT NULL,semester TEXT NOT NULL,volume TEXT NOT NULL,edition TEXT NOT NULL,
                fingerprint TEXT NOT NULL,page_count INTEGER NOT NULL,status TEXT NOT NULL,completed_pages INTEGER NOT NULL DEFAULT 0,
                failed_pages INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS textbook_page(
                page_id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES textbook_book(book_id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,status TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,
                ocr_text_relative_path TEXT,ocr_engine TEXT,ocr_confidence REAL,line_count INTEGER NOT NULL DEFAULT 0,
                layout_json TEXT NOT NULL DEFAULT '[]',manual_review_required INTEGER NOT NULL DEFAULT 0,
                formula_review_required INTEGER NOT NULL DEFAULT 0,error_code TEXT,error_detail TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,UNIQUE(book_id,page_number)
              );
              CREATE INDEX IF NOT EXISTS idx_textbook_page_status ON textbook_page(status,book_id,page_number);
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.metadata_db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection
