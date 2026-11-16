"""Unchanged original Parent-only route policy; no forged native authority."""
from contextlib import contextmanager
from pathlib import Path
import shutil
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from parent_owned_media_fixture import client,auth,seed_media,assert_consumers,identity,restore,backup

@contextmanager
def parent_only(root):
    settings=Settings(data_dir=root,database_path=root/'agent.db',data_encryption_key=b'o'*32,
        native_launch_attestation_key=None,session_token='public-parent-only-session',maintenance_enabled=False)
    with TestClient(create_app(settings),headers={'X-HSC-Session':'public-parent-only-session'})as c:yield c

def test_original_parent_only_owned_portable_restores_deleted_photo_and_other_child(tmp_path):
    root=tmp_path/'data';root.mkdir(mode=0o700)
    with client(root,root/'agent.db')as c:
        h=auth(c,True);items=[seed_media(c,child,n)for n,child in enumerate(('child-primary-4','child-junior-3'))]
        restore(c,h,backup(c,h))
        for item in items:assert_consumers(c,item)
        exported=c.post('/v1/parent/backups/portable',headers=h,json={'passphrase':'public-parent-only-passphrase'})
        assert exported.status_code==200,exported.text
        external=tmp_path/'public-parent-portable.hsc';shutil.copyfile(exported.json()['path'],external)
    with parent_only(root)as c:
        h=auth(c);db=c.app.state.privacy_lifecycle.database
        with db.connect()as d:
            assert d.execute('SELECT count(*)FROM controlled_launch_epoch').fetchone()[0]==0
            assert d.execute("SELECT count(*)FROM native_privacy_permit WHERE status='claimed'").fetchone()[0]==0
        removed=c.request('DELETE','/v1/parent/capture-artifacts/'+items[0]['frame']+'/raw-media',headers=h,
            json={'confirmation':'确认删除','client_action_id':'public-r5-original-photo-delete'})
        assert removed.status_code==200,removed.text
        assert_consumers(c,items[1]);operation=identity(c,h)
        result=c.post('/v1/parent/backups/portable/restore',headers=h,json={'path':str(external),
            'passphrase':'public-parent-only-passphrase','confirmation':'确认迁移恢复','operation':operation})
        assert result.status_code==200,result.text
    with parent_only(root)as fresh:
        for item in items:assert_consumers(fresh,item)
