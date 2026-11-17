"""Verify the bundled backports files from the locked setuptools wheel.

The hashes below are derived from setuptools 80.9.0's PyPI wheel with SHA-256
062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922.
The vendored backports.tarfile license is MIT and is included in the bundle.
"""
from __future__ import annotations
import hashlib
from importlib import metadata
from pathlib import Path
import sys
import sysconfig

EXPECTED = {
    'backports/__init__.py': '88e10cc2794e4567b374ef3edafc4120f491dfb0fb2468e5b99f1fe79bf3c65b',
    'backports/tarfile/__init__.py': '3f07f6a9421f0744a89493c229cc77bf3dd412efda89db38838b007f1cbde2a8',
    'backports/tarfile/__main__.py': '630da8193d5a7ebcf6781b24cdd3d82fc45e07fde5880a6684590dd846c399ce',
    'backports/tarfile/compat/__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    'backports/tarfile/compat/py38.py': '898932b7f82f5a32f31944c90fd4ee4df30c8ce93e7abb17666465bd060ddaa1',
    'backports.tarfile-1.2.0.dist-info/LICENSE': '86da0f01aeae46348a3c3d465195dc1ceccde79f79e87769a64b8da04b2a4741',
}

def validate_vendor(root: Path, expected: dict[str, str] = EXPECTED) -> None:
    for name, digest in expected.items():
        target = root.joinpath(*name.split('/'))
        for path in (target, *target.parents):
            if path == root.parent:
                break
            stat = path.lstat()
            if path.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                raise ValueError('BACKPORTS_VENDOR_LINK_REJECTED')
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('BACKPORTS_VENDOR_HASH_MISMATCH')

def main() -> int:
    try:
        if metadata.version('setuptools') != '80.9.0':
            raise ValueError('SETUPTOOLS_VERSION_MISMATCH')
        root = Path(sysconfig.get_path('purelib')) / 'setuptools' / '_vendor'
        validate_vendor(root)
    except Exception:
        print('WINDOWS_BACKPORTS_VENDOR_CHECK_FAILED', file=sys.stderr)
        return 1
    print('WINDOWS_BACKPORTS_VENDOR_CHECK_PASSED')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
