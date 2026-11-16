from home_study_agent.local_supply import digest
from home_study_agent.task_start_binding import TaskStartBinding


def test_slow_asr_cannot_reinterpret_pre_offer_audio_as_a_fresh_yes():
    import sqlite3
    from home_study_agent.oral_daily_reminder import voice_after_offer
    with sqlite3.connect(':memory:') as c:
        c.execute('''CREATE TABLE voice_capture_binding(voice_input_id TEXT,child_id TEXT,session_id TEXT,
            status TEXT,created_at TEXT)''')
        c.executemany('INSERT INTO voice_capture_binding VALUES(?,?,?,?,?)',[
            ('old-audio','child','session','completed','2026-09-14T08:00:00+00:00'),
            ('new-audio','child','session','completed','2026-09-14T08:01:01+00:00'),
            ('unfinished','child','session','prepared','2026-09-14T08:01:01+00:00')])
        at='2026-09-14T08:01:00+00:00'
        assert not voice_after_offer(c,'child','session','old-audio',at)
        assert voice_after_offer(c,'child','session','new-audio',at)
        assert not voice_after_offer(c,'other-child','session','new-audio',at)
        assert not voice_after_offer(c,'child','other-session','new-audio',at)
        assert not voice_after_offer(c,'child','session','unfinished',at)
        assert not voice_after_offer(c,'child','session','unknown',at)


def test_heard_offer_selects_exact_version_without_discarding_other_candidates():
    first={'plan_item_id':'first','subject':'数学','task_type':'oral','source_type':'oral_daily_check'}
    offered={**first,'plan_item_id':'offered'}
    snapshot={'items':[first,offered],'daily_supply_offer':{'prompt_id':'heard','source_id':digest(offered)}}
    selected=TaskStartBinding.select_resume_target(snapshot,dict(subject=None,task_kind=None,relative_order=None))
    assert selected['status']=='ready' and selected['item']==offered
    assert selected['items']==[first,offered] and 'item' not in snapshot


def test_missing_version_or_model_invented_subject_cannot_select_another_task():
    offered={'plan_item_id':'offered','subject':'数学','task_type':'oral','source_type':'oral_daily_check'}
    other={**offered,'plan_item_id':'other','subject':'英语'}
    target={'items':[other,offered],'daily_supply_offer':{'prompt_id':'heard','source_id':digest(offered)}}
    wrong=TaskStartBinding.select_resume_target(target,dict(subject='english',task_kind=None,relative_order=None))
    assert wrong['status']=='ambiguous' and wrong['item'] is None
    missing=TaskStartBinding.select_resume_target({**target,'items':[other]},dict(subject=None,task_kind=None,relative_order=None))
    assert missing['status']=='ambiguous' and missing['item'] is None
