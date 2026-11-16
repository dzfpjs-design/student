"""Small contract fixtures; parser stubs count calls, never supply timing evidence."""
from pathlib import Path
from collections import OrderedDict
from threading import Lock
import hashlib
import sqlite3

import pytest

from home_study_agent import manual_standard_scope as original_manual


@pytest.fixture
def manual(monkeypatch):
    # Per-test metadata only; this cannot grant source or ownership authority.
    with getattr(original_manual, '_PDF_PAGE_COUNTS_LOCK', Lock()):
        monkeypatch.setattr(original_manual, '_PDF_PAGE_COUNTS', OrderedDict(), raising=False)
    return original_manual


def source(tmp_path, *, raw=b'%PDF-1.7\nsynthetic-count-contract\n', source_id='source-one'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'source.pdf'
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE curriculum_standard_source (source_id TEXT,source_key TEXT,source_type TEXT,jurisdiction TEXT,subject_title TEXT,title TEXT,version_label TEXT,official_url TEXT,pdf_filename TEXT,pdf_sha256 TEXT,page_count INTEGER,status TEXT,local_root_path TEXT)')
    c.execute('CREATE TABLE curriculum_manual_pdf_blob (pdf_sha256 TEXT,page_count INTEGER,content BLOB)')
    c.execute('INSERT INTO curriculum_standard_source VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (source_id, 'synthetic', 'national_standard', 'CN', '数学', 'Synthetic source', 'synthetic-v1',
               'https://www.moe.gov.cn/synthetic.pdf', path.name, digest, 2, 'downloaded', str(tmp_path)))
    c.execute('INSERT INTO curriculum_manual_pdf_blob VALUES(?,?,?)', (digest, 2, raw))
    return c, path, source_id


def observe_parser(monkeypatch, manual, count=2):
    calls = []
    def parser(path):
        calls.append(Path(path).read_bytes())
        return count
    monkeypatch.setattr(manual.TextbookPdfOcrPipeline, '_pdf_page_count', staticmethod(parser))
    monkeypatch.setattr(manual, 'pdf_renderer_available', lambda: True)
    return calls


def test_identical_verified_pdf_reuses_successful_parse(tmp_path, manual, monkeypatch):
    c, _, sid = source(tmp_path)
    calls = observe_parser(monkeypatch, manual)
    try:
        first = manual.registered_pdf(c, sid)
        second = manual.registered_pdf(c, sid)
        assert first == second
        assert len(calls) == 1
    finally:
        c.close()


@pytest.mark.parametrize('change,expected', [
    ('status', 'MANUAL_OFFICIAL_SOURCE_REQUIRED'),
    ('type', 'MANUAL_OFFICIAL_SOURCE_REQUIRED'),
    ('country', 'MANUAL_OFFICIAL_SOURCE_REQUIRED'),
    ('subject', 'MANUAL_OFFICIAL_SOURCE_REQUIRED'),
    ('url', 'MANUAL_OFFICIAL_SOURCE_REQUIRED'),
    ('filename', 'MANUAL_REGISTERED_PDF_REQUIRED'),
    ('source_sha', 'MANUAL_PDF_CHANGED'),
    ('file_bytes', 'MANUAL_PDF_CHANGED'),
    ('saved_bytes', 'MANUAL_SAVED_PDF_CHANGED'),
    ('saved_count', 'MANUAL_SAVED_PDF_CHANGED'),
    ('declared_count', 'MANUAL_PDF_PAGE_COUNT_CHANGED'),
    ('zero_count', 'MANUAL_PDF_PAGE_COUNT_CHANGED'),
    ('alias', 'MANUAL_SOURCE_ALIAS_REJECTED'),
    ('missing_without_saved', 'MANUAL_SOURCE_FILE_REQUIRED'),
    ('oversize', 'MANUAL_SOURCE_FILE_REQUIRED'),
    ('relative_path', 'MANUAL_SOURCE_PATH_REQUIRED'),
    ('renderer', 'MANUAL_PDF_RENDERER_UNAVAILABLE'),
])
def test_warm_cache_preserves_current_source_checks(tmp_path, manual, monkeypatch, change, expected):
    c, path, sid = source(tmp_path)
    calls = observe_parser(monkeypatch, manual)
    try:
        manual.registered_pdf(c, sid)
        if change in {'status', 'type', 'country', 'subject', 'url', 'filename', 'source_sha'}:
            field, value = {
                'status': ('status', 'withdrawn'), 'type': ('source_type', 'other'),
                'country': ('jurisdiction', 'other'), 'subject': ('subject_title', 'other'),
                'url': ('official_url', 'https://example.invalid/source.pdf'),
                'filename': ('pdf_filename', '../source.pdf'), 'source_sha': ('pdf_sha256', '0' * 64),
            }[change]
            c.execute('UPDATE curriculum_standard_source SET ' + field + '=?', (value,))
        elif change == 'file_bytes':
            path.write_bytes(b'changed-public-contract')
        elif change == 'saved_bytes':
            c.execute('UPDATE curriculum_manual_pdf_blob SET content=?', (b'changed-contract',))
        elif change == 'saved_count':
            c.execute('UPDATE curriculum_manual_pdf_blob SET page_count=3')
        elif change in {'declared_count', 'zero_count'}:
            value = 3 if change == 'declared_count' else 0
            c.execute('UPDATE curriculum_standard_source SET page_count=?', (value,))
            c.execute('UPDATE curriculum_manual_pdf_blob SET page_count=?', (value,))
        elif change == 'alias':
            held = path.with_name('held.pdf')
            path.rename(held)
            path.symlink_to(held)
        elif change == 'missing_without_saved':
            path.unlink()
            c.execute('DELETE FROM curriculum_manual_pdf_blob')
        elif change == 'oversize':
            with path.open('r+b') as file:
                file.truncate(80_000_001)
        elif change == 'relative_path':
            monkeypatch.chdir(path.parent)
            c.execute("UPDATE curriculum_standard_source SET local_root_path='.'")
        elif change == 'renderer':
            monkeypatch.setattr(manual, 'pdf_renderer_available', lambda: False)
        with pytest.raises(manual.ManualScopeError) as error:
            manual.registered_pdf(c, sid)
        assert error.value.code == expected
        assert len(calls) == 1
    finally:
        c.close()


def test_changed_valid_bytes_have_a_different_parse_key(tmp_path, manual, monkeypatch):
    c, path, sid = source(tmp_path)
    calls = observe_parser(monkeypatch, manual)
    try:
        manual.registered_pdf(c, sid)
        raw = b'%PDF-1.7\nother-synthetic-count-contract\n'
        digest = hashlib.sha256(raw).hexdigest()
        path.write_bytes(raw)
        c.execute('UPDATE curriculum_standard_source SET pdf_sha256=?', (digest,))
        c.execute('UPDATE curriculum_manual_pdf_blob SET pdf_sha256=?,content=?', (digest, raw))
        manual.registered_pdf(c, sid)
        assert len(calls) == 2 and calls[0] != calls[1]
    finally:
        c.close()


def test_missing_historical_path_still_uses_exact_saved_bytes(tmp_path, manual, monkeypatch):
    c, path, sid = source(tmp_path)
    calls = observe_parser(monkeypatch, manual)
    try:
        first = manual.registered_pdf(c, sid)
        path.unlink()
        assert manual.registered_pdf(c, sid) == first
        assert len(calls) == 1
    finally:
        c.close()


@pytest.mark.parametrize('failure', ['ordinary_exception', 'worker_closure', 'page_count_mismatch', 'zero_pages'])
def test_failed_parse_never_populates_cache(tmp_path, manual, monkeypatch, failure):
    from home_study_agent.pdf_renderer import PdfRenderError
    c, _, sid = source(tmp_path)
    calls = []
    def parser(path):
        calls.append(Path(path).read_bytes())
        if len(calls) == 1:
            if failure == 'ordinary_exception':
                raise OSError('synthetic-I/O-failure')
            if failure == 'worker_closure':
                raise PdfRenderError('PDF_WORKER_CLOSURE_UNCONFIRMED')
            return 3 if failure == 'page_count_mismatch' else 0
        return 2
    monkeypatch.setattr(manual.TextbookPdfOcrPipeline, '_pdf_page_count', staticmethod(parser))
    monkeypatch.setattr(manual, 'pdf_renderer_available', lambda: True)
    try:
        with pytest.raises(manual.ManualScopeError) as error:
            manual.registered_pdf(c, sid)
        assert error.value.code == ('MANUAL_PDF_METADATA_UNAVAILABLE' if failure in {'ordinary_exception', 'worker_closure'} else 'MANUAL_PDF_PAGE_COUNT_CHANGED')
        manual.registered_pdf(c, sid)
        manual.registered_pdf(c, sid)
        assert len(calls) == 2
    finally:
        c.close()


def test_cross_database_reuse_never_grants_a_source(tmp_path, manual, monkeypatch):
    c1, _, s1 = source(tmp_path / 'one', source_id='one')
    c2, _, s2 = source(tmp_path / 'two', source_id='two')
    calls = observe_parser(monkeypatch, manual)
    try:
        manual.registered_pdf(c1, s1)
        with pytest.raises(manual.ManualScopeError) as missing:
            manual.registered_pdf(c2, 'one')
        assert missing.value.code == 'MANUAL_SOURCE_NOT_FOUND'
        c2.execute("UPDATE curriculum_standard_source SET status='withdrawn'")
        with pytest.raises(manual.ManualScopeError) as withdrawn:
            manual.registered_pdf(c2, s2)
        assert withdrawn.value.code == 'MANUAL_OFFICIAL_SOURCE_REQUIRED'
        c2.execute("UPDATE curriculum_standard_source SET status='downloaded'")
        assert manual.registered_pdf(c2, s2)[0]['source_id'] == 'two'
        assert len(calls) == 1
    finally:
        c1.close()
        c2.close()


def test_snapshot_cleanup_failure_never_populates_cache(tmp_path, manual, monkeypatch):
    c, _, sid = source(tmp_path)
    calls = observe_parser(monkeypatch, manual)
    original_directory = manual.TemporaryDirectory
    exits = []
    class Directory:
        def __init__(self, *args, **kwargs):
            self.original = original_directory(*args, **kwargs)
        def __enter__(self):
            return self.original.__enter__()
        def __exit__(self, *args):
            self.original.__exit__(*args)
            exits.append(True)
            if len(exits) == 1:
                raise OSError('synthetic-original-cleanup-failure')
    monkeypatch.setattr(manual, 'TemporaryDirectory', Directory)
    try:
        with pytest.raises(manual.ManualScopeError) as error:
            manual.registered_pdf(c, sid)
        assert error.value.code == 'MANUAL_PDF_METADATA_UNAVAILABLE'
        manual.registered_pdf(c, sid)
        manual.registered_pdf(c, sid)
        assert len(calls) == len(exits) == 2
    finally:
        c.close()


def test_cache_is_bounded_metadata_and_evicts_oldest(tmp_path, manual, monkeypatch):
    calls = observe_parser(monkeypatch, manual)
    for i in range(33):
        c, _, sid = source(tmp_path / str(i), raw=b'%PDF-synthetic-' + str(i).encode())
        try:
            manual.registered_pdf(c, sid)
        finally:
            c.close()
    assert len(manual._PDF_PAGE_COUNTS) == 32
    assert all(type(k) is str and len(k) == 64 and type(v) is int and v == 2 for k, v in manual._PDF_PAGE_COUNTS.items())
    c, _, sid = source(tmp_path / 'oldest-again', raw=b'%PDF-synthetic-0')
    try:
        manual.registered_pdf(c, sid)
    finally:
        c.close()
    assert len(calls) == 34 and len(manual._PDF_PAGE_COUNTS) == 32


def test_no_cache_lock_held_while_original_parser_runs(tmp_path, manual, monkeypatch):
    c, _, sid = source(tmp_path)
    calls = []
    def parser(path):
        assert manual._PDF_PAGE_COUNTS_LOCK.acquire(False)
        manual._PDF_PAGE_COUNTS_LOCK.release()
        calls.append(Path(path).read_bytes())
        return 2
    monkeypatch.setattr(manual.TextbookPdfOcrPipeline, '_pdf_page_count', staticmethod(parser))
    monkeypatch.setattr(manual, 'pdf_renderer_available', lambda: True)
    try:
        manual.registered_pdf(c, sid)
        manual.registered_pdf(c, sid)
        assert len(calls) == 1
    finally:
        c.close()
