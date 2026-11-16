import sqlite3

import pytest

from home_study_agent.daily_observation_projection import project_daily_observation


def _open(path):
    c = sqlite3.connect(path)
    c.executescript('''CREATE TABLE child_profile(child_id TEXT PRIMARY KEY);
      CREATE TABLE daily_session(session_id TEXT PRIMARY KEY, child_id TEXT);
      CREATE TABLE document_monitor_event(event_id TEXT,session_id TEXT,child_id TEXT,event_type TEXT,observed_at TEXT);
      CREATE TABLE child_state_event(state_event_id TEXT,session_id TEXT,child_id TEXT,state TEXT,observed_at TEXT);
      INSERT INTO child_profile VALUES('a'),('b');
      INSERT INTO daily_session VALUES('sa','a'),('sb','b');''')
    c.commit()
    return c


def test_samples_are_child_and_day_scoped_not_interpolated_after_restart(tmp_path):
    path = tmp_path / 'synthetic.sqlite'
    c = _open(path)
    c.executemany('INSERT INTO document_monitor_event VALUES(?,?,?,?,?)', [
        ('d1','sa','a','stable','2026-11-03T16:00:00Z'),
        ('d2','sa','a','stable','2026-11-04T15:59:59+00:00'),
        ('wrongchild','sb','a','stable','2026-11-04T02:00:00Z'),
        ('otherchild','sb','b','stable','2026-11-04T02:00:00Z'),
        ('nextday','sa','a','stable','2026-11-04T16:00:00Z'),
        ('naive','sa','a','stable','2026-11-04T02:00:00'),
    ])
    c.executemany('INSERT INTO child_state_event VALUES(?,?,?,?,?)', [
        ('c1','sa','a','writing_like','2026-11-04T04:00:00+00:00'),
        ('c2','sb','b','writing_like','2026-11-04T04:00:00+00:00'),
        ('c3','sa','a','PRIVATE_TEXT','2026-11-04T04:10:00+00:00'),
    ])
    c.commit()
    c.execute('BEGIN')
    first = project_daily_observation(c, 'a', '2026-11-04')
    c.rollback(); c.close()
    c = sqlite3.connect(path); c.execute('BEGIN')
    second = project_daily_observation(c, 'a', '2026-11-04')
    assert first == second
    assert first['sources']['document']['sample_count'] == 2
    assert first['sources']['camera']['states'] == {'other': 1, 'writing_like': 1}
    assert 'PRIVATE_TEXT' not in str(first)
    assert first['camera_observed_seconds'] is None
    assert first['page_visible_seconds'] is None
    assert first['question_visible_seconds'] is None
    assert first['actual_learning_seconds'] is None
    assert first['activity_inference'] is None
    c.rollback()
    c.execute("DELETE FROM document_monitor_event WHERE event_id='d1'"); c.commit(); c.execute('BEGIN')
    assert project_daily_observation(c, 'a', '2026-11-04')['sources']['document']['sample_count'] == 1


def test_missing_and_invalid_input_are_explicit(tmp_path):
    c = _open(tmp_path / 'synthetic.sqlite')
    with pytest.raises(ValueError, match='TRANSACTION_REQUIRED'):
        project_daily_observation(c, 'a', '2026-11-04')
    c.execute('DROP TABLE document_monitor_event'); c.commit(); c.execute('BEGIN')
    assert project_daily_observation(c, 'a', '2026-11-04')['sources']['document']['status'] == 'unavailable'
    with pytest.raises(ValueError, match='CHILD_UNKNOWN'):
        project_daily_observation(c, 'z', '2026-11-04')
    with pytest.raises(ValueError, match='DATE_INVALID'):
        project_daily_observation(c, 'a', '2026-11-4')


def test_sample_truncation_is_declared(tmp_path):
    c = _open(tmp_path / 'synthetic.sqlite')
    c.executemany('INSERT INTO document_monitor_event VALUES(?,?,?,?,?)', [
        (str(n),'sa','a','stable','2026-11-04T02:00:00Z') for n in range(101)
    ])
    c.commit(); c.execute('BEGIN')
    result = project_daily_observation(c, 'a', '2026-11-04')['sources']['document']
    assert result['sample_count'] == 101 and len(result['samples']) == 100 and result['truncated']


def test_camera_summary_uses_actual_states_and_excludes_shadow_or_low_confidence(tmp_path):
    c = _open(tmp_path / 'synthetic.sqlite')
    c.execute('ALTER TABLE child_state_event ADD COLUMN confidence REAL')
    c.execute('ALTER TABLE child_state_event ADD COLUMN shadow INTEGER')
    c.executemany('INSERT INTO child_state_event VALUES(?,?,?,?,?,?,?)',[
        ('face','sa','a','present','2026-11-04T04:00:00Z',.7,0),
        ('absent','sa','a','away','2026-11-04T04:01:00Z',.62,0),
        ('motion','sa','a','writing_like','2026-11-04T04:02:00Z',.52,0),
        ('shadow','sa','a','writing_like','2026-11-04T04:03:00Z',.8,1),
        ('uncertain','sa','a','paused_like','2026-11-04T04:04:00Z',.1,0),
    ])
    c.commit(); c.execute('BEGIN')
    result=project_daily_observation(c,'a','2026-11-04')['sources']['camera']
    assert result['states']['present']==1 and result['states']['away']==1
    assert result['eligible_states']=={'away':1,'present':1,'writing_like':1}


def test_email_labels_camera_samples_as_uncertain_not_activity_duration():
    from home_study_agent.daily_report_visual_submission import _render
    body={'generated_at':'2026-11-04T12:00:00Z', 'projection':{
        'visual':{'pages':[]},'timing':{'pages':[]},
        'samples':{'sources':{'document':{'sample_count':0},'camera':{
            'sample_count':3,'eligible_states':{'present':1,'writing_like':1,'away':1}}}}},
        'details':{'pages':[],'questions':[]}}
    _,text,_=_render('2026-11-04',body,'full')
    assert '画面出现变化，原因不确定：1 次' in text
    assert '未检测到正面人脸，也可能是遮挡：1 次' in text
    assert '这些样本不能判断是否做题、背书或持续专注' in text


def test_corrected_or_retracted_camera_samples_disappear_after_restart(tmp_path):
    path=tmp_path/'synthetic.sqlite'; c=_open(path)
    c.execute('ALTER TABLE child_state_event ADD COLUMN confidence REAL')
    c.execute('ALTER TABLE child_state_event ADD COLUMN shadow INTEGER')
    c.execute('CREATE TABLE observation_correction(child_id TEXT,state_event_id TEXT)')
    c.execute('CREATE TABLE behavior_retraction(child_id TEXT,event_id TEXT)')
    c.executemany('INSERT INTO child_state_event VALUES(?,?,?,?,?,?,?)',[
        ('corrected','sa','a','writing_like','2026-11-04T04:00:00Z',.7,0),
        ('retracted','sa','a','away','2026-11-04T04:01:00Z',.7,0),
    ])
    c.commit(); c.execute('BEGIN')
    before=project_daily_observation(c,'a','2026-11-04',include_all_source_ids=True)['sources']['camera']
    c.rollback()
    c.execute("INSERT INTO observation_correction VALUES('a','corrected')")
    c.execute("INSERT INTO behavior_retraction VALUES('a','observation:retracted')")
    c.commit(); c.close()
    c=sqlite3.connect(path); c.execute('BEGIN')
    after=project_daily_observation(c,'a','2026-11-04',include_all_source_ids=True)['sources']['camera']
    assert after['eligible_states']=={} and after['sample_count']==0 and after['source_ids']==[]
    assert after['withdrawn_count']==2 and after['source_fingerprint']!=before['source_fingerprint']
