"""Explicit synthetic native proof for selected original local restore requests.

No OS/native close credit. The optional operation is only for a negative request
whose body intentionally omits operation; its exact body still receives the proof.
"""
from synthetic_native_restore_request import KEY

def signed_local_restore_post(client, *, json, headers=None, operation=None, proof_out=None):
    from test_native_privacy_permit_http import certificate, signed_header
    path='/v1/parent/backups/restore'
    original_operation=json['operation'] if operation is None else operation
    proof=certificate(path,json,original_operation,kind='restore',child_id=None,scope=None)
    if proof_out is not None:proof_out.append(proof)
    return client.post(path,headers={**(headers or {}),**signed_header(proof)},json=json)


from contextlib import contextmanager
import sqlite3

@contextmanager
def observe_original_publication(monkeypatch,database,read_snapshot,published):
    from home_study_agent import restore_publication as publication
    original=publication.publish_prepared_database
    def inspect(owner):
        if owner.path!=database.path:return original(owner)
        journal=publication._decode(publication._read(owner.parent,publication._names(owner.path)[1]))
        prepared=owner.path.parent/journal['binding']['prepared_name']
        with publication._cold(prepared,owner=owner)as c:
            c.row_factory=sqlite3.Row
            before=read_snapshot(c)
        result=original(owner)
        with database.connect()as c:after=read_snapshot(c)
        assert before==after
        published.append(after)
        return result
    with monkeypatch.context()as local:
        local.setattr(publication,'publish_prepared_database',inspect)
        yield
