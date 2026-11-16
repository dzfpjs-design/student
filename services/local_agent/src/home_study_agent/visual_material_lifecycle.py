"""Borrowed-transaction hooks for managed visual derivatives and audit scopes."""
import json
from uuid import uuid4

from .visual_material import _json, require
from .visual_material_host import ACTION, QUARANTINE
from .visual_retention import ACTION as RETENTION_ACTION, QUARANTINE as RETENTION_QUARANTINE

CONFIRMATION = 'visual_material_confirmation_v1'
ACTIONS = (ACTION, QUARANTINE, CONFIRMATION, RETENTION_ACTION, RETENTION_QUARANTINE)


def rows(c,child_id):
    return [dict(r) for r in c.execute('SELECT * FROM audit_event WHERE child_id=? AND action IN ('+','.join('?' for _ in ACTIONS)+') ORDER BY rowid',
        (child_id,*ACTIONS)).fetchall()]


def count(c,child_id):
    return c.execute('SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action IN ('+','.join('?' for _ in ACTIONS)+')',(child_id,*ACTIONS)).fetchone()[0]


def delete_scope(c,child_id,*,artifact_ids=None):
    """Return exact file paths to the existing deletion job; never unlink here."""
    require(c.in_transaction,'WRITE_TRANSACTION_REQUIRED')
    assets=c.execute("SELECT * FROM capture_artifact WHERE child_id=? AND artifact_type='document_visual_region'",(child_id,)).fetchall()
    selected=[]
    for asset in assets:
        source=_json(asset['details_json']).get('source_binding',{})
        require(isinstance(source,dict) and source.get('child_id')==child_id,'RECORD_INVALID')
        if artifact_ids is None or source.get('artifact_id') in artifact_ids or asset['artifact_id'] in artifact_ids:
            selected.append(asset)
    audits=rows(c,child_id)
    ids={r['audit_id'] for r in audits if artifact_ids is None or _json(r['summary_json']).get('artifact_id') in artifact_ids}
    ids.update(r['audit_id'] for r in audits if r['action']==QUARANTINE
        and _json(r['summary_json']).get('authority_audit_id') in ids)
    for audit_id in ids:
        c.execute('DELETE FROM audit_event WHERE audit_id=? AND child_id=?',(audit_id,child_id))
    for asset in selected:
        c.execute('DELETE FROM capture_artifact WHERE artifact_id=? AND child_id=?',(asset['artifact_id'],child_id))
    # Preserve independent original captures, erase their selected derivative
    # facts/permissions. A deleted source must not regain its old visual binding.
    originals=c.execute("SELECT artifact_id,details_json FROM capture_artifact WHERE child_id=? AND artifact_type='document_ocr'",(child_id,)).fetchall()
    for row in originals:
        if artifact_ids is not None and row['artifact_id'] not in artifact_ids:
            continue
        details=_json(row['details_json'])
        if 'visual_materials' in details:
            del details['visual_materials']
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=? AND child_id=?',
                (json.dumps(details,ensure_ascii=False),row['artifact_id'],child_id))
    return dict(visual_material_audits=len(ids),visual_material_assets=len(selected)), [r['local_path'] for r in selected if r['local_path']]


def _linked(value,source_ids,excerpt_ids):
    if isinstance(value,dict):
        return any((k=='source_id' and isinstance(v,str) and v in source_ids)
            or (k=='excerpt_id' and isinstance(v,str) and v in excerpt_ids)
            or _linked(v,source_ids,excerpt_ids) for k,v in value.items())
    if isinstance(value,list):
        return any(_linked(v,source_ids,excerpt_ids) for v in value)
    return False


def delete_sources(c,child_id,source_ids):
    require(c.in_transaction,'WRITE_TRANSACTION_REQUIRED')
    excerpts={r[0] for source in source_ids for r in c.execute(
        'SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(source,)).fetchall()}
    originals=c.execute("SELECT artifact_id,details_json FROM capture_artifact WHERE child_id=? AND artifact_type='document_ocr'",(child_id,)).fetchall()
    ids={r['artifact_id'] for r in originals if _linked(_json(r['details_json']),source_ids,excerpts)}
    return delete_scope(c,child_id,artifact_ids=ids)


def quarantine_after_restore(c,restore_id):
    require(c.in_transaction,'WRITE_TRANSACTION_REQUIRED')
    from .repository import utc_now
    existing={(r['child_id'],_json(r['summary_json']).get('authority_audit_id')) for r in c.execute(
        'SELECT child_id,summary_json FROM audit_event WHERE action=?',(QUARANTINE,)).fetchall()}
    changed=0
    policy_quarantines={(r['child_id'],_json(r['summary_json']).get('authority_audit_id')) for r in c.execute(
        'SELECT child_id,summary_json FROM audit_event WHERE action=?',(RETENTION_QUARANTINE,)).fetchall()}
    for row in c.execute('SELECT * FROM audit_event WHERE action=?',(RETENTION_ACTION,)).fetchall():
        data=_json(row['summary_json'])
        if data.get('policy',{}).get('status')!='enabled' or (row['child_id'],row['audit_id']) in policy_quarantines:
            continue
        c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
            ('visual-retention-quarantine:'+uuid4().hex,'system',RETENTION_QUARANTINE,row['child_id'],
             json.dumps(dict(authority_audit_id=row['audit_id'],restore_id=restore_id,reason='backup_restore')),utc_now()))
        changed+=1
    for row in c.execute('SELECT * FROM audit_event WHERE action=?',(ACTION,)).fetchall():
        data=_json(row['summary_json'])
        if data.get('request',{}).get('kind')!='storage' or (row['child_id'],row['audit_id']) in existing:
            continue
        c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
            ('visual-quarantine:'+uuid4().hex,'system',QUARANTINE,row['child_id'],
             json.dumps(dict(authority_audit_id=row['audit_id'],artifact_id=data['artifact_id'],
                 region_id=data['region_id'],restore_id=restore_id,reason='backup_restore')),
             utc_now()))
        changed+=1
    return changed
