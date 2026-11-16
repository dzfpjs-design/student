"""Real local IO and original authority guards; no simulated Windows credit."""
import hashlib,os,sqlite3
from pathlib import Path
import pytest
from home_study_agent.knowledge_ingestion import local_corpora as module
from home_study_agent.knowledge_ingestion.local_corpora import LocalCorpusIngestor,CorpusSpec


def test_logical_object_returns_and_drive_anchor_keeps_original_io(tmp_path,monkeypatch):
    leaf=tmp_path/'原材料 % # &.pdf';leaf.write_bytes(b'public original')
    calls=[];original=module.io_path
    def observed(path):
        assert path.parent!=path
        calls.append(path);return original(path)
    monkeypatch.setattr(module,'io_path',observed)
    assert LocalCorpusIngestor._registered_path(leaf,directory=False)is leaf
    assert LocalCorpusIngestor._registered_path(tmp_path,directory=True)is tmp_path
    assert calls and all(p.is_absolute()and not str(p).startswith('\\\\?\\')for p in calls)
    assert Path(leaf.anchor)not in calls


@pytest.mark.parametrize('name',['relative','bad:name','..','CON','trailing.','trailing '])
def test_invalid_literal_rejects_before_os_adapter(tmp_path,monkeypatch,name):
    called=[];monkeypatch.setattr(module,'io_path',lambda p:called.append(p))
    value=Path(name)if name=='relative'else tmp_path/name
    with pytest.raises(ValueError,match='^REGISTERED_ASSET_ROOT_REQUIRED$'):
        LocalCorpusIngestor._registered_path(value,directory=False)
    assert called==[]


def test_original_alias_and_multiple_links_still_reject_without_bytes_change(tmp_path):
    leaf=tmp_path/'original.pdf';leaf.write_bytes(b'public original');before=leaf.read_bytes()
    alias=tmp_path/'alias.pdf';alias.symlink_to(leaf)
    with pytest.raises(ValueError,match='^REGISTERED_ASSET_ROOT_REQUIRED$'):LocalCorpusIngestor._registered_path(alias,directory=False)
    alias.unlink();linked=tmp_path/'linked.pdf';os.link(leaf,linked)
    try:
        with pytest.raises(ValueError,match='^REGISTERED_ASSET_ROOT_REQUIRED$'):LocalCorpusIngestor._registered_path(leaf,directory=False)
        assert leaf.read_bytes()==before
    finally:linked.unlink()


def test_actual_metadata_register_and_normal_hash_stream_paths_stay_usable(tmp_path,monkeypatch):
    from test_registered_corpus_roots import document
    database,spec,pdf=document(tmp_path);seen=[];original=module.io_path
    monkeypatch.setattr(module,'io_path',lambda path:(seen.append(path),original(path))[1])
    metadata=spec.metadata_db_path;before=hashlib.sha256(metadata.read_bytes()).hexdigest()
    assert len(LocalCorpusIngestor._metadata(metadata,'local_scanned_document'))==1
    LocalCorpusIngestor(database).register(spec.corpus_spec())
    assert hashlib.sha256(metadata.read_bytes()).hexdigest()==before
    text=next(spec.ocr_text_root.rglob('*.txt'));raw=text.read_bytes()
    digest,segments=LocalCorpusIngestor._stream_hash_and_segments(text)
    assert digest==hashlib.sha256(raw).hexdigest()and segments
    assert LocalCorpusIngestor._sha256(pdf)==hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert LocalCorpusIngestor._fingerprint(pdf)==f'size:{pdf.stat().st_size}:mtime_ns:{pdf.stat().st_mtime_ns}'
    assert metadata in seen and pdf in seen and text in seen


def test_native_glob_keeps_logical_paths_and_original_hidden_video_filter(tmp_path):
    root=tmp_path/'videos';(root/'.hidden').mkdir(parents=True);(root/'nested').mkdir()
    visible=root/'nested'/'one.mp4';visible.write_bytes(b'public video');(root/'.hidden'/'secret.mp4').write_bytes(b'public hidden')
    spec=CorpusSpec('c','public','bilibili',tmp_path,root,'public','local')
    assert LocalCorpusIngestor._video_files(spec)=={'one':visible}


def test_original_relative_plain_cli_domain_does_not_gain_resolution_or_absolute_adapter(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);text=Path('texts');text.mkdir();(text/'one.txt').write_text('public relative source',encoding='utf-8')
    video=Path('videos');video.mkdir();(video/'one.mp4').write_bytes(b'public relative video')
    metadata=Path('metadata # %.db')
    with sqlite3.connect(metadata)as c:
        c.executescript('''CREATE TABLE selected_v2(bvid,grade,subject,semester);
          CREATE TABLE selected(bvid,grade,subject);CREATE TABLE selected_aoshu(bvid,grade,subject,semester);
          CREATE TABLE details(bvid,title,author,desc);
          INSERT INTO selected_v2 VALUES('one','4','math','1');INSERT INTO details VALUES('one','public','public','public');''')
    def forbid_absolute(path):raise AssertionError('RELATIVE_PATH_REACHED_ABSOLUTE_IO_ADAPTER')
    monkeypatch.setattr(module,'io_path',forbid_absolute);monkeypatch.setattr(module,'io_glob',lambda *a:forbid_absolute(a[0]))
    assert set(LocalCorpusIngestor._metadata(metadata,'bilibili'))=={'one'}
    assert LocalCorpusIngestor._sha256(video/'one.mp4')==hashlib.sha256((video/'one.mp4').read_bytes()).hexdigest()
    spec=CorpusSpec('relative','public','bilibili',text,video,'public','local',metadata)
    assert LocalCorpusIngestor._video_files(spec)=={'one':video/'one.mp4'}
    assert list(module._corpus_glob(text,'*.txt'))==[text/'one.txt']
    assert str(text)=='texts'and not text.is_absolute()


def test_real_upsert_does_not_silently_drop_video_hash_or_fingerprint_at_raw_blind_gate(tmp_path,monkeypatch):
    from home_study_agent.database import Database
    text=tmp_path/'text';text.mkdir();(text/'one.txt').write_text('public source segment',encoding='utf-8')
    videos=tmp_path/'video';videos.mkdir();video=videos/'one.mp4';video.write_bytes(b'public original video')
    class RawBlindPath(type(video)):
        def is_file(self):return False
    blind=RawBlindPath(str(video));seen=[];original=module.io_path
    def adapter(path):
        seen.append(path)
        return original(Path(str(path)))
    monkeypatch.setattr(module,'io_path',adapter)
    monkeypatch.setattr(LocalCorpusIngestor,'_video_files',staticmethod(lambda spec:{'one':blind}))
    database=Database(tmp_path/'db.sqlite');database.initialize();ingestor=LocalCorpusIngestor(database)
    spec=CorpusSpec('c','public','bilibili',text,videos,'public','local')
    job=ingestor.start(spec,idempotency_key='public-video-gate',hash_videos=True)
    assert ingestor.run(spec,job,throttle_ms=0)['status']=='completed'
    with database.connect()as c:
        digest,fp=c.execute('SELECT video_sha256,video_fingerprint FROM kg_resource').fetchone()
        assert digest==hashlib.sha256(video.read_bytes()).hexdigest()
        assert fp==f'size:{video.stat().st_size}:mtime_ns:{video.stat().st_mtime_ns}'
    assert sum(path==blind for path in seen)>=4
