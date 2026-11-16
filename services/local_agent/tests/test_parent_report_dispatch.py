"""Actual pending-report worker with isolated provider retries and parent changes."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_report_delivery import _pending_report_job


@pytest.mark.parametrize('change',['none','privacy','privacy_cycle','source','before_persist','source_before_persist'])
def test_report_retries_and_commit_require_current_parent_and_facts(tmp_path,monkeypatch,change):
    db=Database(tmp_path/'report.db')
    db.initialize()
    _pending_report_job(db)
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        data_encryption_key=b'R'*32,model_provider='deepseek',model_api_key='fixture-only',
        model_base_url='https://report.invalid')
    posts=[]
    privacy=PrivacyLifecycleService(db,tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    async def transport(request):
        posts.append(json.loads(request.content))
        if len(posts)==1 and change not in {'before_persist','source_before_persist'}:
            if change in {'privacy','privacy_cycle'}:
                privacy.update_preferences({'cloud_model_text_allowed':False})
                if change=='privacy_cycle':
                    privacy.update_preferences({'cloud_model_text_allowed':True})
            elif change=='source':
                with db.connect() as c:
                    c.execute("UPDATE daily_reflection SET learned_text='尚需核对这次回顾' WHERE session_id='pending-report-session'")
                    c.commit()
            return httpx.Response(503)
        content={'scenario':'核对今天留下的一小步。','script':['先谢谢你记录下来。','我们一起核对下一步。'],
            'action':{'what':'核对第一步','steps':['孩子选择一处','核对后停止']},
            'avoid':[{'do_not_say':'快一点。','say_instead':'准备好再开始。'}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(content,ensure_ascii=False)}}]})
    gateway=ModelGateway(settings,transport=httpx.MockTransport(transport))
    if change in {'before_persist','source_before_persist'}:
        original=StudyRepository.save_daily_report
        def revoke_before_save(self,*args,**kwargs):
            if change=='before_persist':
                privacy.update_preferences({'cloud_model_text_allowed':False})
            else:
                with db.connect() as c:
                    c.execute("UPDATE daily_reflection SET learned_text='保存前已修订的学习回顾' WHERE session_id='pending-report-session'")
                    c.commit()
            return original(self,*args,**kwargs)
        monkeypatch.setattr(StudyRepository,'save_daily_report',revoke_before_save)
    with TestClient(create_app(settings,model_gateway=gateway)):
        pass
    with db.connect() as c:
        reports=c.execute('SELECT source FROM daily_report').fetchall()
        job=dict(c.execute("SELECT * FROM report_job WHERE job_id='pending-report-job'").fetchone())
        calls=c.execute("SELECT status,error_code FROM model_call WHERE purpose='parent_report_coaching'").fetchall()
        assert len(calls)==1
        if change=='none':
            assert len(posts)==2 and len(reports)==1 and reports[0][0]=='cloud_parent_coaching_v1'
            assert job['status']=='completed'
        else:
            assert len(posts)==1
            assert not reports and job['status']=='pending'
    if change!='none':
        # Same durable report job recovers without sending another cloud request.
        privacy.update_preferences({'cloud_model_text_allowed':False})
        with TestClient(create_app(settings,model_gateway=type('LocalOnly',(),{'configured':False})())):
            pass
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM daily_report').fetchone()[0]==1
            assert c.execute("SELECT status FROM report_job WHERE job_id='pending-report-job'").fetchone()[0]=='completed'
            assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0]==0
        assert len(posts)==1
