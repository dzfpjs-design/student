"""Explicit public test issuer at selected legitimate restore sites only."""
KEY='a'*64

def signed_restore_post(client,*,headers,json):
    from test_native_privacy_permit_http import certificate,signed_header
    path='/v1/parent/backups/restore'
    proof=certificate(path,json,json['operation'],kind='restore',child_id=None,scope=None)
    return client.post(path,headers={**headers,**signed_header(proof)},json=json)
