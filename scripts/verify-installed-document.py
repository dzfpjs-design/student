"""Read a camera frame from stdin; test the installed EXE in an isolated profile."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

spec = importlib.util.spec_from_file_location('daily_runtime', Path(__file__).with_name('verify-daily-learning-runtime.py'))
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)
root = Path(tempfile.mkdtemp(prefix='hsc-physical-document-'))
runtime = daily.Runtime(Path(sys.argv[1]), root)
report = {'status':'running', 'isolated_profile':True, 'real_child_evidence':False, 'cloud_calls':0}
try:
    image = sys.stdin.read(24_000_001).strip()
    assert 32 <= len(image) <= 24_000_000
    runtime.start()
    sid = runtime.api('/v1/sessions', {'child_id':'child-primary-4','client_action_id':'physical-document-session'}, expected=201)['session_id']
    result = runtime.api('/v1/sessions/'+sid+'/ocr', {'image_base64':image,'client_action_id':'physical-document-ocr'}, timeout=120)
    quality = result.get('details', {}).get('quality', {})
    report.update(status='ocr_executed', confidence=result.get('confidence'), text_chars=len(result.get('extracted_text','')), raw_saved=bool(result.get('local_path')), quality_passed=quality.get('passed'), quality_issues=quality.get('issues', []))
    assert not report['raw_saved']
    # No recognition text or source image is copied to the evidence report.
    report['artifact_created'] = bool(result.get('artifact_id'))
    runtime.stop()
    runtime.start()
    with runtime.opener.open(daily.Request(runtime.base+'/v1/health',headers={'X-HSC-Session':runtime.token})) as response:
        report['restart_health'] = response.status
except Exception as error:
    report.update(status='failed',error_type=type(error).__name__,error=str(error))
finally:
    runtime.stop()
    runtime.log.close()
    shutil.rmtree(root)
    report['test_profile_removed']=True
    Path(sys.argv[2]).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
if report['status']=='failed': raise SystemExit(1)
