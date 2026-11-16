"""Offline maintainer workflow. No key creation, dynamic trust roots or automatic renewal."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE = HERE / 'publisher.swift'
SIGNER = HERE / 'publisher-macos-arm64'
CONTRACT = REPO / 'services/local_agent/src/home_study_agent/pet_audio_catalog.py'
SOURCE_SHA = '31bd53d4e73848a736da1e21615fd0bf57e3d26ac537f03281eb9a890bfcb013'
SIGNER_SHA = 'bfb94d7c6dd8f3fcf4f26fe48c7dec4066d25c874b50754a451ab9d51075c8eb'
CONTRACT_SHA = '5a9a300d3c450309539e144a1178dccfeb27cb780153862b0fee6838cc011c9a'
KEY_ID = 'hsc-maintainer-20261024-v1'

class Rejected(ValueError):
    pass

def need(value, code):
    if not value:
        raise Rejected(code)

def absolute(value):
    p = Path(value)
    need('..' not in p.parts, 'PUBLIC_PATH_ALIAS_REJECTED')
    return p if p.is_absolute() else Path.cwd() / p

def parents(p):
    for parent in p.parents:
        s = parent.lstat()
        need(stat.S_ISDIR(s.st_mode) and not stat.S_ISLNK(s.st_mode), 'PUBLIC_PATH_ALIAS_REJECTED')

def read(p, cap):
    parents(p)
    before = p.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= cap, 'PUBLIC_INPUT_REJECTED')
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        held = os.fstat(fd)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(cap + 1)
        after = p.lstat()
        end = os.fstat(fd)
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
        need(stamp(before) == stamp(held) == stamp(after) == stamp(end) and len(data) == before.st_size, 'PUBLIC_INPUT_CHANGED')
        return data, stamp(before)
    finally:
        os.close(fd)

def vacant(output):
    parents(output)
    need(not os.path.lexists(output), 'OUTPUT_ALREADY_EXISTS')

def contract():
    for p, cap, digest in [(SOURCE, 16384, SOURCE_SHA), (SIGNER, 200000, SIGNER_SHA), (CONTRACT, 32768, CONTRACT_SHA)]:
        need(hashlib.sha256(read(p, cap)[0]).hexdigest() == digest, 'REVIEWED_TOOL_OR_CONTRACT_CHANGED')
    spec = importlib.util.spec_from_file_location('_reviewed_pet_contract', CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')

def prevalidate(module, payload):
    # Only the format-checking envelope uses an ephemeral in-memory key. It is never runtime authority.
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')
    envelope = canonical({'schema': 'pet_audio_catalog_signed_v1', 'key_id': 'ephemeral-format-check-only',
        'payload_base64': base64.b64encode(payload).decode('ascii'),
        'signature_base64': base64.b64encode(key.sign(payload)).decode('ascii')})
    return module.verify_signed_catalog(envelope, _trusted_keys={'ephemeral-format-check-only': {'issuer': 'FORMAT CHECK ONLY', 'publicKeyPem': public}})

def atomic_publish(output, data):
    temporary = None
    temporary_id = None
    def remove_owned():
        held = temporary.lstat()
        need((held.st_dev, held.st_ino) == temporary_id and stat.S_ISREG(held.st_mode), 'OWNED_TEMP_CHANGED')
        temporary.unlink()
    try:
        fd, name = tempfile.mkstemp(prefix='.pet-catalog-', suffix='.tmp', dir=output.parent)
        temporary = Path(name)
        held = os.fstat(fd)
        temporary_id = (held.st_dev, held.st_ino)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory link publishes complete bytes atomically and cannot replace an existing name.
        os.link(temporary, output, follow_symlinks=False)
        remove_owned()
        temporary = None
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            remove_owned()

def publish(payload_file, output_file, sign=False):
    payload_file, output_file = absolute(payload_file), absolute(output_file)
    vacant(output_file)
    payload, stamp = read(payload_file, 12000)
    module = contract()
    prevalidate(module, payload)  # Full dates, schema, sources, fixed protocol/model and integer-ceiling price contract.
    result = {'schema': 1, 'mode': 'preflight', 'contract_valid': True, 'private_key_accessed': False,
        'payload_sha256': hashlib.sha256(payload).hexdigest(), 'network_used': False}
    if not sign:
        return result
    need(sys.platform == 'darwin' and os.uname().machine == 'arm64', 'REVIEWED_MACOS_ARM64_REQUIRED')
    # Neither a caller-controlled command nor provider/root configuration is forwarded.
    child_env = {k: os.environ[k] for k in ('HOME', 'TMPDIR', 'USER', 'LOGNAME') if k in os.environ}
    child_env.update({'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    try:
        process = subprocess.run([str(SIGNER), 'sign-reviewed-stdin'], input=payload, capture_output=True, env=child_env, timeout=20)
    except subprocess.TimeoutExpired:
        raise Rejected('SIGNER_RESULT_UNKNOWN_NO_RETRY') from None
    need(process.returncode == 0 and not process.stderr and 0 < len(process.stdout) <= 16384, 'REVIEWED_SIGNER_FAILED')
    # Deliberately no trusted-keys argument: actual output must pass the real compiled production root.
    checked = module.verify_signed_catalog(process.stdout)
    envelope = json.loads(process.stdout)
    need(envelope['key_id'] == KEY_ID and base64.b64decode(envelope['payload_base64']) == payload, 'SIGNED_PAYLOAD_CHANGED')
    need(read(payload_file, 12000) == (payload, stamp), 'PUBLIC_INPUT_CHANGED')
    atomic_publish(output_file, process.stdout)
    result.update({'mode': 'signed', 'private_key_accessed': True, 'signed_catalog_sha256': checked['sha256']})
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True, help='Reviewed canonical public JSON; dates are never rewritten.')
    parser.add_argument('--output', required=True, help='New public signed-package path; no overwrite.')
    parser.add_argument('--sign', action='store_true', help='Explicitly access the existing dedicated Keychain signer once.')
    args = parser.parse_args()
    try:
        print(json.dumps(publish(args.payload, args.output, args.sign), sort_keys=True))
    except Exception as error:
        print(error.args[0] if isinstance(error, Rejected) else 'MAINTAINER_OPERATION_REJECTED', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
