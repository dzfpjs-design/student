"""Prepare an isolated planned check, never an answered or completed task."""
import json
import os
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'services/local_agent/src'), str(root/'services/local_agent/tests')]
path, receipt = (Path(v).resolve() for v in sys.argv[1:3])
temporary_root = Path(tempfile.gettempdir()).resolve()
temporary_roots = {temporary_root, Path('/private/var/folders'), Path('/private/tmp')}
if (not any(path.is_relative_to(root) for root in temporary_roots if root.exists()) or path.exists()
        or receipt.parent != path.parent):
    raise RuntimeError('Requires a new temporary database')
os.environ.update(HSC_DATA_DIR=str(path.parent), HSC_DATABASE_PATH=str(path), HSC_MAINTENANCE_ENABLED='0')


def local_only(event, args):
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1','::1','localhost'):
        raise RuntimeError('External networking forbidden')


sys.addaudithook(local_only)
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from fastapi.testclient import TestClient
from test_goal_program import _qualified_goal_fixture

child = 'child-junior-3'
db = Database(path)
with TestClient(create_app(Settings(data_dir=path.parent,database_path=path,maintenance_enabled=False))):
    pass
repo = StudyRepository(db)
repo.seed_family()
LearningStateService(db).seed_catalog()
GoalProgramService(db).seed_defaults()
item, answer = _qualified_goal_fixture(db)
with db.connect() as c:
    node = c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?',(item,)).fetchone()[0]
    c.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?",(node,))
    c.execute('DELETE FROM knowledge_edge WHERE target_node_id=?',(node,))
    c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?',(child,))
    c.commit()
sid = repo.start_session(child,'native-goal-session')['session_id']
with db.connect() as c:
    c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?",(sid,))
    c.commit()
repo.add_homework(sid,'数学','合成桌面验收作业',10,'written',None,None,None,None,None,'native-goal-homework')
workspace = repo.generate_plan(sid,'native-goal-plan')
goal = next(p for p in workspace['plan_items'] if p['source_type']=='junior_exam_goal')
for task in workspace['plan_items']:
    if task['plan_item_id'] != goal['plan_item_id']:
        repo.reschedule_plan_item(task['plan_item_id'],'Isolated goal check; defer other work','native-defer-'+task['plan_item_id'])
with db.connect() as c:
    c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    c.commit()
receipt.write_text(json.dumps(dict(child_id=child,session_id=sid,plan_item_id=goal['plan_item_id'],
    diagnostic_item_id=item,knowledge_node_id=node,synthetic=True,real_model_quality=False)),encoding='utf-8')
