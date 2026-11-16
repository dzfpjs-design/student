"""Local page-level evidence projection for scanner/OCR inputs.

OCR text is ingested through the same checkpointed corpus job as video
transcripts.  The scan remains at its original local location; this module saves
only a relative locator plus a lightweight fingerprint unless a caller explicitly
requests image hashing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..database import Database
from ..knowledge_graph.schema import ensure_schema
from ..repository import utc_now
from ..retrieval.hybrid import HashingVectorizer
from .local_corpora import CorpusSpec, LocalCorpusIngestor


@dataclass(frozen=True)
class DocumentCorpusSpec:
    corpus_id: str
    title: str
    document_kind: str
    ocr_text_root: Path
    image_root: Path | None
    source_ref: str
    license_boundary: str
    ocr_engine: str
    ocr_confidence: float | None = None
    metadata_db_path: Path | None = None
    version_label: str = "local-document-v1"
    pdf_root: Path | None = None

    def corpus_spec(self) -> CorpusSpec:
        return CorpusSpec(
            corpus_id=self.corpus_id, title=self.title, source_kind="local_scanned_document",
            transcript_root=self.ocr_text_root, video_root=None, source_ref=self.source_ref,
            license_boundary=self.license_boundary, metadata_db_path=self.metadata_db_path,
            version_label=self.version_label,
            pdf_root=self.pdf_root, image_root=self.image_root,
        )


class LocalDocumentEvidenceIngestor:
    """Resumable, CPU-only scanner evidence importer; never uploads a page."""

    _VALID_KINDS = {"textbook", "school_official_practice", "purchased_practice_book", "other_user_document"}
    _IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".heic")

    def __init__(self, database: Database, vectorizer: HashingVectorizer | None = None):
        self.database = database
        self.corpus = LocalCorpusIngestor(database, vectorizer=vectorizer)

    def start(self, spec: DocumentCorpusSpec, *, idempotency_key: str) -> str:
        self._validate(spec)
        return self.corpus.start(spec.corpus_spec(), idempotency_key=idempotency_key)

    def run(self, spec: DocumentCorpusSpec, job_id: str, *, max_resources: int | None = None,
            throttle_ms: int = 50, hash_images: bool = False) -> dict:
        self._validate(spec)
        result = self.corpus.run(spec.corpus_spec(), job_id, max_resources=max_resources, throttle_ms=throttle_ms)
        projected = self._project_pages(spec, hash_images=hash_images)
        return {**result, "document_pages": projected, "image_hashing": bool(hash_images)}

    def _project_pages(self, spec: DocumentCorpusSpec, *, hash_images: bool) -> dict:
        text_paths = sorted(spec.ocr_text_root.rglob("*.txt"), key=lambda path: path.as_posix())
        page_numbers = {path.stem: index for index, path in enumerate(text_paths, start=1)}
        metadata = LocalCorpusIngestor._metadata(spec.metadata_db_path, "local_scanned_document")
        images = self._images(spec.image_root)
        now = utc_now(); linked = image_linked = 0
        with self.database.connect() as connection:
            ensure_schema(connection, now); connection.commit(); connection.execute("BEGIN IMMEDIATE")
            resources = connection.execute("SELECT resource_id,external_id FROM kg_resource WHERE corpus_id=?", (spec.corpus_id,)).fetchall()
            for resource in resources:
                external_id = str(resource["external_id"])
                page_metadata = metadata.get(external_id, {})
                image = images.get(external_id)
                relative = self._relative_image(spec.image_root, image)
                image_hash = self._sha256(image) if image and hash_images else None
                fingerprint = self._fingerprint(image)
                connection.execute("""INSERT INTO kg_document_page(
                    page_id,resource_id,document_kind,page_number,image_relative_path,image_sha256,image_fingerprint,
                    ocr_engine,ocr_confidence,source_ref,license_boundary,version_label,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET
                    document_kind=excluded.document_kind,page_number=excluded.page_number,
                    image_relative_path=excluded.image_relative_path,image_sha256=COALESCE(excluded.image_sha256,kg_document_page.image_sha256),
                    image_fingerprint=excluded.image_fingerprint,ocr_engine=excluded.ocr_engine,
                    ocr_confidence=excluded.ocr_confidence,source_ref=excluded.source_ref,
                    license_boundary=excluded.license_boundary,version_label=excluded.version_label,updated_at=excluded.updated_at""",
                    (str(uuid4()),resource["resource_id"],spec.document_kind,
                     int(page_metadata.get("page_number") or page_numbers.get(external_id, 1)),relative,image_hash,fingerprint,
                     spec.ocr_engine,page_metadata.get("ocr_confidence",spec.ocr_confidence),page_metadata.get("source_ref") or spec.source_ref,
                     spec.license_boundary,spec.version_label,now,now))
                linked += 1; image_linked += int(image is not None)
            connection.commit()
        return {"linked": linked, "with_local_image": image_linked, "without_image": linked - image_linked}

    def _validate(self, spec: DocumentCorpusSpec) -> None:
        if spec.document_kind not in self._VALID_KINDS:
            raise ValueError("invalid document_kind")
        if not spec.ocr_text_root.is_dir():
            raise ValueError(f"missing OCR text directory: {spec.ocr_text_root}")
        if spec.image_root is not None and not spec.image_root.is_dir():
            raise ValueError(f"missing image directory: {spec.image_root}")
        if spec.ocr_confidence is not None and not 0 <= spec.ocr_confidence <= 1:
            raise ValueError("ocr_confidence must be between 0 and 1")

    def _images(self, root: Path | None) -> dict[str, Path]:
        if root is None:
            return {}
        return {path.stem: path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in self._IMAGE_SUFFIXES}

    @staticmethod
    def _relative_image(root: Path | None, image: Path | None) -> str | None:
        return image.relative_to(root).as_posix() if root and image else None

    @staticmethod
    def _fingerprint(path: Path | None) -> str | None:
        if path is None:
            return None
        stat = path.stat()
        return f"size:{stat.st_size}:mtime_ns:{stat.st_mtime_ns}"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()
