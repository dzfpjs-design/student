"""Loopback synthetic-only existing host for the paired-to-formal React test."""
import argparse,json,os,socket,sys
from pathlib import Path
import uvicorn
sys.path.insert(0,str(Path(__file__).absolute().parents[1]))

class NoModel:
    configured=False
    async def complete(self,request):raise AssertionError('synthetic fixture has no model')

def main():
    p=argparse.ArgumentParser();p.add_argument('--data-root',required=True);args=p.parse_args();root=Path(args.data_root).resolve()
    if not root.is_dir()or not root.name.startswith('hsc-paper-formal-test-'):raise ValueError('explicit synthetic directory required')
    import_root = root / 'import-only'
    os.environ['HSC_DATA_DIR'] = str(import_root)
    os.environ['HSC_DATABASE_PATH'] = str(import_root / 'home-study.db')
    from test_paper_objective_bridge import fixture
    from home_study_agent.app import create_app, app as imported_app
    from home_study_agent.config import Settings
    from home_study_agent.lazy_application import LazyApplication
    from home_study_agent import money_egress
    # Import stays inert: never construct an unused default App/DB/owner.
    assert type(imported_app) is LazyApplication
    assert vars(imported_app)['_factory'] is create_app
    assert vars(imported_app)['_application'] is None and vars(imported_app)['_initialization_error'] is None
    assert money_egress._owner is None
    db,_,_,_=fixture(root)
    assert vars(imported_app)['_application'] is None and money_egress._owner is None
    with db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='公开合成孩子',curriculum_region='public' WHERE child_id='child-primary-4'");c.commit()
    app=create_app(Settings(data_dir=root,database_path=db.path,data_encryption_key=b'0'*32,
        session_token='public-paper-formal-fixture',maintenance_enabled=False),model_gateway=NoModel())
    assert vars(imported_app)['_application'] is None
    assert app.state.money_owner is money_egress._owner and money_egress._owner is not None
    assert type(app.state.model_gateway) is NoModel and app.state.model_gateway.configured is False
    sock=socket.socket();sock.bind(('127.0.0.1',0));print(json.dumps({'port':sock.getsockname()[1]}),flush=True)
    uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[sock])
if __name__=='__main__':main()
