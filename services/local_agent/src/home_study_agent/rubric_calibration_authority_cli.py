"""Local operator import of externally issued calibration metadata, not scores."""
import argparse
import getpass
import ipaddress
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .rubric_calibration_authority import verify
from .rubric_calibration_evidence import digest


def local_base(value):
    parsed = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:
        loopback = parsed.hostname == 'localhost'
    if (parsed.scheme != 'http' or not loopback or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('RUBRIC_CALIBRATION_LOCAL_ENDPOINT_REQUIRED')
    return value.rstrip('/')


def receipt_file(path):
    with Path(path).open('rb') as handle:
        raw = handle.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('RUBRIC_CALIBRATION_ENVELOPE_TOO_LARGE')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'envelope', 'signature'}:
        raise ValueError('RUBRIC_CALIBRATION_ENVELOPE_FIELDS')
    envelope = value['envelope']
    if not isinstance(envelope, dict) or not isinstance(envelope.get('child_id'), str):
        raise ValueError('RUBRIC_CALIBRATION_AUTHORITY_SCOPE_MISMATCH')
    return value


def main(argv=None, *, client=None, prompt_pin=getpass.getpass):
    parser = argparse.ArgumentParser(description='校验或导入已签名的校准元数据；不生成签名，不授予掌握资格。')
    parser.add_argument('operation', choices=['check', 'import', 'request-spec'])
    parser.add_argument('--file', help='外部签发的 JSON 包，仅含 envelope 和 signature')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--child')
    parser.add_argument('--turn')
    parser.add_argument('--out', help='request-spec 的本地输出文件；不会覆盖已有文件')
    args = parser.parse_args(argv)
    try:
        base = local_base(args.base_url)
        if args.operation in {'check', 'import'}:
            if not args.file:
                raise ValueError('RUBRIC_CALIBRATION_RECEIPT_FILE_REQUIRED')
            packet = receipt_file(args.file)
            child = packet['envelope']['child_id']
            verify(child, packet['envelope'], packet['signature'])
            if args.operation == 'check':
                print('签名、范围与有效期校验通过；这不代表评分已获得校准资格。')
                return 0
        else:
            if not args.child or not args.turn or not args.out or Path(args.out).exists():
                raise ValueError('RUBRIC_CALIBRATION_SPEC_DESTINATION_REQUIRED')
            child = args.child
        owned_client = client is None
        connection = client or httpx.Client(base_url=base, timeout=30, trust_env=False, follow_redirects=False)
        try:
            unlocked = connection.post('/v1/parent/unlock', json={'pin': prompt_pin('家长 PIN：')})
            if unlocked.status_code != 200:
                raise ValueError('RUBRIC_CALIBRATION_PARENT_AUTH_REQUIRED')
            headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
            root = '/v1/parent/children/' + quote(child, safe='') + '/rubric-calibration'
            if args.operation == 'import':
                response = connection.post(root + '/authority/import', headers=headers, json={
                    **packet, 'client_action_id': 'authority-import:' + digest(packet)})
            else:
                response = connection.get(root + '/evaluations/' + quote(args.turn, safe='') + '/request-spec', headers=headers)
            if response.status_code != 200:
                raise ValueError('RUBRIC_CALIBRATION_HOST_REJECTED')
            result = response.json()
            if args.operation == 'request-spec':
                with Path(args.out).open('x', encoding='utf-8') as output:
                    json.dump(result, output, ensure_ascii=False, indent=2)
                print('当前请求规格已写入本地文件；未调用模型，未授予校准资格。')
            else:
                print('证明已保存。当前状态：' + str(result['status']) + '；尚不代表评分或掌握资格。')
            return 0
        finally:
            if owned_client:
                connection.close()
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError) as error:
        code = str(error)
        print(code if code.startswith('RUBRIC_CALIBRATION_') and len(code) < 120 else 'RUBRIC_CALIBRATION_LOCAL_OPERATION_FAILED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
