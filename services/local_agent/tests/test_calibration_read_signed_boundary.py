"""Real signed authority cannot outlive a single validation's external trust/time."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from home_study_agent import rubric_calibration_authority as authority
from home_study_agent.rubric_calibration_read import ReadScope
from test_calibration_evaluator_host import signer
from test_rubric_calibration import flow, CHILD


def test_repeated_signature_is_verified_once_then_rechecked_at_finish(flow,tmp_path,monkeypatch):
    db = flow[0]
    sign, now = signer(tmp_path,monkeypatch,children=[CHILD])
    signed = sign('scorer_revision',dict(child_id=CHILD,receipt_id='synthetic-release',status='current',
        provider='synthetic',model='synthetic-model',model_revision='synthetic-v1',
        verification_method='signed_release_manifest',evidence_hash='a'*64,endpoint_scope_sha256='b'*64,
        verified_at=now.isoformat(),expires_at=(now+timedelta(days=1)).isoformat()))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        authority.import_receipt(c,child_id=CHILD,**signed)
        c.commit()
        c.execute('BEGIN')
        calls = []
        original = authority.verify
        def counted(*args,**kwargs):
            calls.append(True)
            return original(*args,**kwargs)
        monkeypatch.setattr(authority,'verify',counted)
        scope = ReadScope(c,CHILD,None)
        first = scope.resolve(c,child_id=CHILD,kind='scorer_revision',reference_id='synthetic-release')
        first['provider'] = 'mutated returned object'
        assert scope.resolve(c,child_id=CHILD,kind='scorer_revision',reference_id='synthetic-release')['provider']=='synthetic'
        scope.finish()
        assert len(calls)==2
        c.rollback()


def test_scope_reuses_only_signature_math_while_rechecking_live_policy(flow,tmp_path,monkeypatch):
    sign, now = signer(tmp_path,monkeypatch,children=[CHILD])
    signed = sign('scorer_revision',dict(child_id=CHILD,receipt_id='math-only',status='current',
        provider='synthetic',model='synthetic-model',model_revision='synthetic-v1',
        verification_method='signed_release_manifest',evidence_hash='a'*64,endpoint_scope_sha256='b'*64,
        verified_at=now.isoformat(),expires_at=(now+timedelta(days=1)).isoformat()))
    with flow[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        authority.import_receipt(c,child_id=CHILD,**signed)
        c.commit()
        c.execute('BEGIN')
        calls = []
        load_key = authority.serialization.load_pem_public_key
        def counted(*args, **kwargs):
            calls.append(True)
            return load_key(*args, **kwargs)
        monkeypatch.setattr(authority.serialization, 'load_pem_public_key', counted)
        scope = ReadScope(c,CHILD,None)
        assert scope.resolve(c,child_id=CHILD,kind='scorer_revision',reference_id='math-only')
        scope.finish()
        assert len(calls) == 1
        other = ReadScope(c,CHILD,None)
        assert other.resolve(c,child_id=CHILD,kind='scorer_revision',reference_id='math-only')
        other.finish()
        assert len(calls) == 2
        c.rollback()


@pytest.mark.parametrize('change',['trust_file','time_expiry'])
def test_signature_rechecked_at_single_validation_acceptance(flow,tmp_path,monkeypatch,change):
    db = flow[0]
    sign, now = signer(tmp_path,monkeypatch,children=[CHILD])
    signed = sign('scorer_revision',dict(child_id=CHILD,receipt_id='synthetic-release',status='current',
        provider='synthetic',model='synthetic-model',model_revision='synthetic-v1',
        verification_method='signed_release_manifest',evidence_hash='a'*64,endpoint_scope_sha256='b'*64,
        verified_at=now.isoformat(),expires_at=(now+timedelta(days=1)).isoformat()))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        authority.import_receipt(c,child_id=CHILD,**signed)
        c.commit()
        c.execute('BEGIN')
        scope = ReadScope(c,CHILD,None)
        proof = scope.resolve(c,child_id=CHILD,kind='scorer_revision',reference_id='synthetic-release')
        assert proof['signed_authority']['key_id']=='isolated-signer'
        changes = c.total_changes
        with pytest.raises(ValueError,match='READ_SCOPE_CHANGED'):
            scope.resolve(c,child_id='child-middle-3',kind='scorer_revision',reference_id='synthetic-release')
        if change=='trust_file':
            Path(os.environ['HSC_RUBRIC_CALIBRATION_TRUST_FILE']).write_text(json.dumps(
                {'version':'rubric_authority_trust_v1','issuers':[]}))
            reason='TRUST_CONFIGURATION_CHANGED'
        else:
            class Later(datetime):
                @classmethod
                def now(cls,tz=None):
                    return now+timedelta(days=1,microseconds=1)
            monkeypatch.setattr(authority,'datetime',Later)
            reason='AUTHORITY_EXPIRED'
        assert c.total_changes==changes
        with pytest.raises(ValueError,match=reason):
            scope.finish()
        assert c.in_transaction and c.total_changes==changes
        c.rollback()
