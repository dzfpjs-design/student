"""Explicit visual-report submission; never scheduled or invoked by this module.

The database effect guard covers the claim, network operation and durable result.
No SQLite transaction is held during network I/O. A processing row left by a
crash is deliberately indistinguishable from an uncertain provider receipt.
"""
from datetime import datetime, timezone
from html import escape
import re

from .daily_report_delivery_state import assert_ledger_key, identity_key, schema_valid as ledger_schema_valid
from .daily_report_email_verification import verified
from .daily_report_frozen_dependencies import valid
from .daily_report_frozen_snapshot import _consent_generation, _decrypt, _key, schema_valid as snapshot_schema_valid
from .daily_report_observation import _authority, schema_valid as source_schema_valid
from .family_clock import family_time
from .notification_channels import ChannelSubmissionCancelled, SmtpChannel, submission_guard

_ORIGINAL_SMTP_SEND = SmtpChannel.send


def _utc_now():
    return datetime.now(timezone.utc)


def _check(c, service, child, day, recipient, snapshot, *, processing=None, expected_config=None):
    """Check current authority and exact encrypted source in one transaction."""
    key = _key(service.encryption_key)
    if not ledger_schema_valid(c) or not snapshot_schema_valid(c) or not source_schema_valid(c):
        raise ValueError('DELIVERY_STORAGE_UNCONFIRMED')
    barrier = c.execute('SELECT blocked FROM daily_observation_delivery_barrier WHERE singleton_id=1').fetchone()
    if not barrier or barrier['blocked'] != 0:
        raise ValueError('RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED')
    assert_ledger_key(c, key)
    address = _authority(c, child, day, recipient, key, processing_identity=processing)
    token = identity_key(key, child, day, address)
    if processing is not None and token != processing:
        raise ValueError('DELIVERY_IDENTITY_CHANGED')
    if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child,)).fetchone():
        raise ValueError('DELETION_PENDING')
    row = c.execute('''SELECT f.* FROM daily_observation_report_snapshot f
        JOIN daily_observation_report_outbox o ON o.source_id=f.source_id AND o.child_id=f.child_id
          AND o.day=f.day AND o.recipient_id=f.recipient_id
        WHERE f.snapshot_id=? AND f.child_id=? AND f.day=? AND f.recipient_id=?
          AND o.address_identity=? AND o.status='prepared' AND f.status='frozen_local' ''',
        (snapshot, child, day, recipient, address)).fetchone()
    if not row:
        raise ValueError('FROZEN_SNAPSHOT_NOT_FOUND')
    body = _decrypt(row, key)
    if body.get('address_identity') != address or body.get('consent_generation') != _consent_generation(c,child,recipient):
        raise ValueError('FROZEN_RECIPIENT_CHANGED')
    if not valid(c,child,body.get('dependencies')):
        raise ValueError('FROZEN_SOURCE_CHANGED')
    pref = c.execute('SELECT * FROM report_delivery_preference WHERE child_id=?', (child,)).fetchone()
    start, end, redaction = (pref['quiet_start'],pref['quiet_end'],pref['redaction_level']) if pref else ('21:00','07:00','standard')
    if (redaction not in ('standard','minimal') or not isinstance(start,str) or not isinstance(end,str)
        or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',start)
        or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',end)
        or pref and pref['timezone'] != 'Asia/Shanghai'):
        raise ValueError('DISCLOSURE_PREFERENCE_INVALID')
    instant = _utc_now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('INSTANT_INVALID')
    current = family_time(instant).strftime('%H:%M')
    if start <= current < end if start < end else current >= start or current < end:
        raise ValueError('QUIET_HOURS')
    config = service._config('email')
    if config.get('allow_external_delivery') is not True:
        raise ValueError('EMAIL_EXTERNAL_DELIVERY_NOT_AUTHORIZED')
    if expected_config is not None and config != expected_config:
        raise ValueError('EMAIL_CONFIGURATION_CHANGED')
    test = c.execute("SELECT status FROM report_delivery_channel_test WHERE child_id=? AND channel='email'", (child,)).fetchone()
    if not test or test['status'] != 'passed':
        raise ValueError('EMAIL_CHANNEL_TEST_REQUIRED')
    if not verified(c,child,recipient,key,config.get('password') or ''):
        raise ValueError('RECIPIENT_EMAIL_TEST_REQUIRED')
    ledger = c.execute('SELECT child_id,snapshot_id,status FROM daily_observation_delivery_ledger WHERE identity=?', (token,)).fetchone()
    expected_status = 'processing' if processing is not None else 'ready'
    if not ledger or ledger['child_id'] != child or ledger['snapshot_id'] != snapshot or ledger['status'] != expected_status:
        raise ValueError('DELIVERY_PLAN_NOT_CURRENT')
    return token, address, config, redaction, body


def _render(day, body, redaction):
    lines = [f'学习观察日报 · {day}', f"记录生成时间：{body['generated_at']}",
             '以下内容来自设备采样和文字识别，可能不完整或有误。']
    if redaction == 'minimal':
        lines += ['按照家长设置，此邮件不包含学习内容细节。请在本机家长中心查看。']
    else:
        projection = body['projection']
        visual, timing = projection['visual'], projection['timing']
        by_revision = {x['revision_id']:x['estimated_visible_ms'] for x in timing.get('pages', [])}
        details = {x['artifact_id']:x for x in body['details']['pages']}
        lines += ['', '高拍仪识别的页面：']
        if not visual['pages']:
            lines += ['当天没有可用的页面识别记录。']
        for index, page in enumerate(visual['pages'], 1):
            observations = page['observations']
            text = details[observations[0]['artifact_id']]
            visible = sum(by_revision.get(o['revision_id'],0) for o in observations)
            needs_review = any(o['review_required'] for o in observations)
            source_note = '云端页面识别候选，尚未核对' if page.get('source') == 'cloud_page' else ('识别置信度较低，请核对' if needs_review else '识别内容仍可能有误')
            lines += [f"页面 {index}（{source_note}；采样可见约 {visible / 1000:.0f} 秒）：",
                      text['text'] + ('\n（识别内容过长，已截断）' if text['text_truncated'] else '')]
        questions = body['details']['questions']
        if questions:
            lines += ['', '识别到的题目（需核对）：']
            lines += [f"- {q['text']}" + ('（已截断）' if q['text_truncated'] else '') for q in questions]
        task_time = projection.get('task_time')
        if task_time is not None:
            lines += ['', '已完成任务的记录计时：']
            if not task_time['tasks']:
                lines += ['当天没有可归属到当天的已完成任务计时。']
            for task in task_time['tasks']:
                declared = (f"；填写 {task['reported_minutes']} 分钟" if task['reported_minutes'] is not None else '')
                lines += [f"- {task['subject']}：{task['title']}；计时 {task['recorded_active_seconds'] / 60:.1f} 分钟{declared}"]
            if task_time['tasks']:
                lines += [f"已记录任务计时合计：{task_time['recorded_active_seconds'] / 60:.1f} 分钟。"]
            if task_time['omitted_count']:
                lines += [f"另有 {task_time['omitted_count']} 项时间跨日或记录不完整，未计入。"]
            lines += ['任务计时和填写时长不等于实际专注或学习时长。']
        samples = projection['samples'].get('sources', {})
        for label, key in (('高拍仪状态采样', 'document'), ('观察摄像头状态采样', 'camera')):
            source = samples.get(key, {})
            lines += [f"{label}：{source.get('sample_count', 0)} 次；采样不能代表连续行为。"]
        camera = samples.get('camera', {}).get('eligible_states', {})
        if camera:
            labels = (('present','检测到正面人脸'), ('writing_like','画面出现变化，原因不确定'),
                      ('paused_like','画面变化较少，原因不确定'), ('away','未检测到正面人脸，也可能是遮挡'))
            lines += ['', '观察摄像头的离散样本（仅供核对）：']
            lines += [f"- {label}：{camera[state]} 次" for state,label in labels if camera.get(state,0)]
            lines += ['这些样本不能判断是否做题、背书或持续专注。']
        lines += ['', '采样可见时间不等于学习、专注、做题或背书时长；本次无法可靠推断实际学习时长。']
    text = '\n'.join(lines)
    return f'学习观察日报｜{day}', text, '<html><body><pre style="white-space:pre-wrap">' + escape(text) + '</pre></body></html>'


def _finish(service, token, child, snapshot, state):
    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        updated = c.execute('''UPDATE daily_observation_delivery_ledger SET status=?,updated_at=?
            WHERE identity=? AND child_id=? AND snapshot_id=? AND status='processing' ''',
            (state, _utc_now().isoformat(), token, child, snapshot)).rowcount
        if updated != 1:
            c.rollback()
            raise ValueError('DELIVERY_CLAIM_LOST')
        c.commit()


def submit(service, child_id, day, recipient_id, snapshot_id, *, admission_check=None):
    """Submit exactly one pre-approved local plan; caller explicitly invokes it."""
    with service.database.external_effect_guard():
        with service.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if admission_check is not None:
                    admission_check()
                token, address, config, redaction, body = _check(c,service,child_id,day,recipient_id,snapshot_id)
                subject, text, html = _render(day,body,redaction)
                changed = c.execute("UPDATE daily_observation_delivery_ledger SET status='processing',updated_at=? WHERE identity=? AND status='ready' AND snapshot_id=?",
                                    (_utc_now().isoformat(),token,snapshot_id)).rowcount
                if changed != 1:
                    raise ValueError('DELIVERY_CLAIM_LOST')
                c.commit()
            except BaseException:
                c.rollback()
                raise
        checked = False
        rejected = None
        def before_data():
            nonlocal checked, rejected
            checked = True
            try:
                if admission_check is not None:
                    admission_check()
                with service.database.connect() as check:
                    check.execute('BEGIN')
                    try:
                        _, current_address, _, current_redaction, _ = _check(
                            check,service,child_id,day,recipient_id,snapshot_id,
                            processing=token,expected_config=config)
                        if current_address != address or current_redaction != redaction:
                            raise ValueError('DISCLOSURE_CHANGED')
                    finally:
                        check.rollback()
            except Exception:
                rejected = 'visual_report_authority_or_source_changed'
                return rejected
            return None
        adapter = service.adapters['email']
        # Only the built-in transport's inspected control flow proves that this
        # exact cancellation propagates directly before DATA. A replaceable
        # adapter may swallow the check, submit anyway and spoof the exception.
        trusted_cancellation = (type(adapter) is SmtpChannel
                                and getattr(adapter.send, '__func__', None) is _ORIGINAL_SMTP_SEND)
        try:
            with submission_guard(before_data):
                adapter.send(address,subject,text,html,config)
        except ChannelSubmissionCancelled as error:
            if trusted_cancellation and rejected and error.reason == rejected:
                _finish(service,token,child_id,snapshot_id,'ready')
                return {'status':'ready','reason':'submission_cancelled_before_data'}
            try:
                _finish(service,token,child_id,snapshot_id,'unknown')
            except Exception:
                pass
            return {'status':'unknown'}
        except Exception:
            # Includes ambiguous protocol failures and unexpected adapter errors.
            # If persistence itself fails, the existing processing row blocks retry.
            try:
                _finish(service,token,child_id,snapshot_id,'unknown')
            except Exception:
                pass
            return {'status':'unknown'}
        except BaseException:
            try:
                _finish(service,token,child_id,snapshot_id,'unknown')
            except Exception:
                pass
            raise
        if not checked or rejected:
            try:
                _finish(service,token,child_id,snapshot_id,'unknown')
            except Exception:
                pass
            return {'status':'unknown'}
        try:
            _finish(service,token,child_id,snapshot_id,'sent')
        except Exception:
            return {'status':'unknown'}
        return {'status':'sent'}
