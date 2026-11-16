"""Pixel preparation cannot hold the learner's database write lock."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from home_study_agent.document_vision import DiagramRegionGate
from home_study_agent.local_supply import LocalSupplyError, digest
from home_study_agent.visual_material import record_region
from test_document_vision import RegionEngine
from test_visual_material import visual_case, frame


def test_stop_commits_during_pixel_preparation_and_late_region_never_persists(tmp_path):
    db, monitor, raw, scope = visual_case(tmp_path)
    entered, release = Event(), Event()
    class PausedOcr(RegionEngine):
        def recognize(self, image, route):
            entered.set()
            assert release.wait(8)
            return super().recognize(image, route)
    with db.connect() as c:
        before = c.execute('SELECT details_json FROM capture_artifact').fetchone()[0]
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(DiagramRegionGate(PausedOcr()).prepare_region, raw,
            region_xyxy=[200,180,600,560], known_child_names=('学生',))
        assert entered.wait(5)
        stopping = workers.submit(monitor.stop,'study','stop-during-prepare')
        try:
            stopping.result(timeout=3)
        finally:
            release.set()
        prepared = pending.result(timeout=5)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(LocalSupplyError, match='SESSION_NOT_ACTIVE'):
            record_region(c,**scope,prepared=prepared,region_id='figure',expected_details_version=digest(json.loads(before)))
        assert c.execute('SELECT details_json FROM capture_artifact').fetchone()[0] == before
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == 0


def test_prepared_value_is_not_json_and_cannot_bind_a_different_frame(tmp_path):
    db, _, raw, scope = visual_case(tmp_path)
    prepared = DiagramRegionGate(RegionEngine()).prepare_region(frame(True),region_xyxy=[200,180,600,560])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        details = json.loads(c.execute('SELECT details_json FROM capture_artifact').fetchone()[0])
        args = dict(**scope,region_id='figure',expected_details_version=digest(details))
        with pytest.raises(LocalSupplyError,match='PREPARED_REQUIRED'):
            record_region(c,**args,prepared={'safe':True,'frame_sha256':details['monitor_frame_sha256']})
        with pytest.raises(LocalSupplyError,match='FRAME_CHANGED'):
            record_region(c,**args,prepared=prepared)
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 1


@pytest.mark.parametrize('change',['pause_resume','missing_generation','private_name'])
def test_finished_preparation_cannot_borrow_new_generation_or_privacy_scope(tmp_path,change):
    db, monitor, raw, scope = visual_case(tmp_path)
    prepared = DiagramRegionGate(RegionEngine()).prepare_region(raw,region_xyxy=[200,180,600,560],known_child_names=('学生',))
    if change=='pause_resume':
        monitor.pause('study','pause')
        monitor.resume('study','resume')
    with db.connect() as c:
        if change=='private_name': c.execute("UPDATE child_profile SET nickname='different-private-name'")
        if change=='missing_generation':
            details = json.loads(c.execute('SELECT details_json FROM capture_artifact').fetchone()[0])
            details.pop('monitor_generation')
            c.execute('UPDATE capture_artifact SET details_json=?',(json.dumps(details),))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT details_json FROM capture_artifact').fetchone()[0]
        with pytest.raises(LocalSupplyError,match='GENERATION_CHANGED|PRIVACY_SCOPE_CHANGED'):
            record_region(c,**scope,prepared=prepared,region_id='figure',expected_details_version=digest(json.loads(before)))
        assert c.execute('SELECT details_json FROM capture_artifact').fetchone()[0] == before
