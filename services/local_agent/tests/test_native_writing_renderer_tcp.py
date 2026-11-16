"""Real renderer handlers/API + separate backend processes, synthetic peripherals."""
import json
import os
from pathlib import Path
import subprocess

from test_native_writing_receipt_http import _process_client
from test_native_writing_shell_http import scheduled, draft, KEY


def test_renderer_capture_loss_restart_semantic_query_and_retained_submit_actual_tcp(tmp_path):
    root=Path(__file__).resolve().parents[3]
    with scheduled(tmp_path/'data') as case:
        body=draft(case)
        journal=tmp_path/'browser-storage.json'
        pids=[]
        for phase in ('capture','recover_submit'):
            with _process_client(case,expected_losses=1 if phase=='capture' else 0,semantic=True,
                                 session_token='public-native-writing-tcp-session') as (client,pid):
                pids.append(pid)
                config=tmp_path/(phase+'-input.json')
                config.write_text(json.dumps(dict(base_url=str(client.base_url).rstrip('/'),body=body,key=KEY,
                    phase=phase,journal=str(journal),result=str(tmp_path/(phase+'-result.json')),
                    session_token='public-native-writing-tcp-session',runtime_epoch=len(pids))))
                log=tmp_path/(phase+'-renderer.log')
                with log.open('w') as output:
                    result=subprocess.run([str(root/'node_modules/.bin/vitest'),'run',
                        'src/renderer/src/NativeWritingRealTcp.test.tsx'],cwd=root,
                        env={**os.environ,'HSC_NATIVE_TCP_CASE':str(config)},stdout=output,stderr=subprocess.STDOUT,timeout=90)
                assert result.returncode==0,log.read_text()[-5000:]
        assert len(set(pids))==2
        with case['db'].connect() as c:
            original=c.execute("SELECT * FROM capture_artifact WHERE artifact_type='writing_original'").fetchall()
            assert len(original)==1
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(body['prompt_id'],)).fetchone()
            assert row['status']=='answered'
            attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
            assert attempt['submitted_answer']==body['text']
            assert attempt['submitted_answer'] not in {'继续提交本题','刚才作文交上了吗'}
        (tmp_path/'renderer-tcp-result.json').write_text(json.dumps(dict(pids=pids,owned_processes_remaining=0,
            artifact_id=original[0]['artifact_id'],attempt_id=row['attempt_id'],originals=1,exact_original_text=True,
            control_utterance_not_answer=True,synthetic_model_posts=case['synthetic_model_posts'],external_paid_posts=0,
            boundary='Real SessionWorkspace/nativeWritingFlow/localApi/main capture signer/router/SQLite with two backend and two renderer test processes. Synthetic microphone text and speech; jsdom storage transported exactly across renderer processes. Not Electron or Windows.'),indent=2))
