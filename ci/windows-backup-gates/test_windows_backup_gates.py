"""Windows runner-only synthetic checks; not a production backup/repair tool."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
import uuid

if os.name == 'nt':
    import winreg
    import ctypes
    from ctypes import wintypes

RUNNER = os.name == 'nt' and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_OS') == 'Windows'


def powershell(script, env):
    local = os.environ.copy()
    local.update(env)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            env=local, capture_output=True, text=True, timeout=20, check=True)
    return result.stdout.strip()


def sddl(path):
    # This deliberately excludes SACL. Reading Owner/Group/DACL is not a full ACL backup.
    result = powershell("$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:HSC_SYNTHETIC_TARGET; "
                        "$s=[System.Security.AccessControl.AccessControlSections]; "
                        "$a.GetSecurityDescriptorSddlForm($s::Owner -bor $s::Group -bor $s::Access)",
                        {'HSC_SYNTHETIC_TARGET': str(path)})
    if not result or len(result) > 16000:
        raise RuntimeError('SYNTHETIC_SDDL_UNAVAILABLE')
    return result


def reject_reparse(path):
    """Reject any reparse ancestor and every reparse object in this fixture tree."""
    root = Path(path).absolute()
    reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', None)
    if reparse is None:
        raise RuntimeError('SYNTHETIC_REPARSE_FLAG_UNAVAILABLE')
    for ancestor in (root, *root.parents):
        info = ancestor.lstat()
        if info.st_file_attributes & reparse:
            raise RuntimeError('SYNTHETIC_REPARSE_REJECTED')
    def fail_walk(error):
        raise error
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
        for name in dirs + files:
            info = (Path(parent) / name).lstat()
            if info.st_file_attributes & reparse:
                raise RuntimeError('SYNTHETIC_REPARSE_REJECTED')
            if name in files and info.st_nlink != 1:
                raise RuntimeError('SYNTHETIC_HARDLINK_REJECTED')


def create_unique_reg_key(relative):
    # RegCreateKeyEx exposes creation disposition so existing keys are not adopted.
    create = ctypes.WinDLL('advapi32', use_last_error=True).RegCreateKeyExW
    create.argtypes = [wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR,
                       wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       ctypes.POINTER(wintypes.HKEY), ctypes.POINTER(wintypes.DWORD)]
    create.restype = wintypes.LONG
    result = wintypes.HKEY()
    disposition = wintypes.DWORD()
    # Predefined HKEY values are sign-extended on 64-bit Windows.
    hkcu = ctypes.c_int32(int(winreg.HKEY_CURRENT_USER)).value
    status = create(wintypes.HKEY(hkcu), relative, 0, None, 0,
                    winreg.KEY_READ | winreg.KEY_WRITE, None,
                    ctypes.byref(result), ctypes.byref(disposition))
    if status:
        raise OSError(status, 'SYNTHETIC_REG_CREATE_FAILED')
    close = ctypes.WinDLL('advapi32').RegCloseKey
    close.argtypes, close.restype = [wintypes.HKEY], wintypes.LONG
    close_status = close(result)
    if close_status:
        raise OSError(close_status, 'SYNTHETIC_REG_CLOSE_FAILED')
    if disposition.value != 1:  # REG_CREATED_NEW_KEY
        raise RuntimeError('SYNTHETIC_REG_ALREADY_EXISTS')


def reg_read_tree(relative):
    values, subtrees = {}, {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative, 0, winreg.KEY_READ) as key:
        subkey_count, value_count, _ = winreg.QueryInfoKey(key)
        for i in range(value_count):
            name, value, typ = winreg.EnumValue(key, i)
            if isinstance(value, bytes):
                value = {'hex': value.hex()}
            if typ not in (winreg.REG_SZ, winreg.REG_DWORD, winreg.REG_BINARY):
                raise RuntimeError('SYNTHETIC_REG_TYPE_UNEXPECTED')
            values[name] = {'type': typ, 'value': value}
        for i in range(subkey_count):
            child = winreg.EnumKey(key, i)
            if child != 'child':
                raise RuntimeError('SYNTHETIC_REG_SUBKEY_UNEXPECTED')
            subtrees[child] = reg_read_tree(relative + '\\' + child)
    return {'values': values, 'subkeys': subtrees}


def reg_restore_tree(relative, tree):
    if set(tree) != {'values', 'subkeys'} or set(tree['subkeys']) - {'child'}:
        raise RuntimeError('SYNTHETIC_REG_SNAPSHOT_INVALID')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, relative, 0, winreg.KEY_WRITE) as key:
        for name, entry in tree['values'].items():
            typ, value = entry['type'], entry['value']
            if typ == winreg.REG_BINARY:
                value = bytes.fromhex(value['hex'])
            if typ not in (winreg.REG_SZ, winreg.REG_DWORD, winreg.REG_BINARY):
                raise RuntimeError('SYNTHETIC_REG_TYPE_UNEXPECTED')
            winreg.SetValueEx(key, name, 0, typ, value)
    for child, subtree in tree['subkeys'].items():
        reg_restore_tree(relative + '\\' + child, subtree)


@unittest.skipUnless(RUNNER, 'Only a disposable GitHub-hosted Windows runner is supported')
class WindowsSyntheticBackupTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get('RUNNER_TEMP'):
            self.fail('RUNNER_TEMP_REQUIRED')
        self.token = uuid.uuid4().hex
        self.tmp = tempfile.TemporaryDirectory(prefix='hsc-synthetic-' + self.token + '-', dir=os.environ['RUNNER_TEMP'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        reject_reparse(self.root)
        self.keys = ['Software\\hsc-synthetic-' + self.token + '-' + name for name in ('source', 'restored')]
        self.owned_keys = []
        self.addCleanup(self.delete_registry)
        for key in self.keys:
            create_unique_reg_key(key)
            self.owned_keys.append(key)

    def delete_registry(self):
        for path in self.owned_keys:
            for target in (path + '\\child', path):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, target)
                except FileNotFoundError:
                    pass

    def test_registry_snapshot_independent_restore_and_acl_read(self):
        source, restored = self.keys
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, source, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, 'name', 0, winreg.REG_SZ, 'synthetic only')
            winreg.SetValueEx(key, 'version', 0, winreg.REG_DWORD, 1)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, source + '\\child', 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, 'sample', 0, winreg.REG_BINARY, b'fixture')
        before = reg_read_tree(source)
        source_sddl = sddl('Registry::HKEY_CURRENT_USER\\' + source)
        backup = self.root / 'registry-snapshot.json'
        backup.write_text(json.dumps({'tree': before, 'readable_sddl': source_sddl}, sort_keys=True), encoding='utf-8')
        disk_snapshot = json.loads(backup.read_text(encoding='utf-8'))
        reg_restore_tree(restored, disk_snapshot['tree'])
        self.assertEqual(reg_read_tree(source), before)
        self.assertEqual(reg_read_tree(restored), before)
        self.assertEqual(disk_snapshot['readable_sddl'], sddl('Registry::HKEY_CURRENT_USER\\' + source))
        self.assertEqual(source_sddl, sddl('Registry::HKEY_CURRENT_USER\\' + restored))
        # The comparison is only for readable Owner/Group/DACL, never SACL.

    def test_sqlite_live_wal_snapshot_and_stopped_write_raw_restore(self):
        db = self.root / 'synthetic.db'
        online = self.root / 'online-snapshot.db'
        raw = self.root / 'raw-restore'
        raw.mkdir()
        source = sqlite3.connect(str(db))
        self.addCleanup(source.close)
        self.assertEqual(source.execute('pragma journal_mode=wal').fetchone()[0].lower(), 'wal')
        source.execute('pragma wal_autocheckpoint=0')
        source.execute('create table marker (value text)')
        source.execute("insert into marker values ('committed in WAL')")
        source.commit()
        wal = Path(str(db) + '-wal')
        shm = Path(str(db) + '-shm')
        self.assertTrue(wal.is_file() and wal.stat().st_size > 0 and shm.is_file())
        with sqlite3.connect(str(online)) as target:
            source.backup(target)
        with sqlite3.connect(str(online)) as restored:
            self.assertEqual(restored.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('select value from marker').fetchone()[0], 'committed in WAL')
        # This sample has one owner; holding a write transaction freezes that owner.
        # This does not prove exclusion or cross-file consistency in the real app.
        source.execute('begin immediate')
        before = {}
        for path in (db, wal, shm):
            before[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            shutil.copyfile(path, raw / path.name)
        for path in (db, wal, shm):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before[path.name])
            self.assertEqual(hashlib.sha256((raw / path.name).read_bytes()).hexdigest(), before[path.name])
        source.rollback()
        with sqlite3.connect(str(raw / db.name)) as restored:
            self.assertEqual(restored.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('select value from marker').fetchone()[0], 'committed in WAL')

    def test_filesystem_acl_and_reparse_junction_fail_closed(self):
        original = self.root / 'original'
        original.mkdir()
        (original / 'sample.txt').write_text('synthetic only', encoding='utf-8')
        restored = self.root / 'restored'
        shutil.copytree(original, restored)
        reject_reparse(original)
        reject_reparse(restored)
        self.assertEqual(sddl(original), sddl(restored))
        self.assertEqual(sddl(original / 'sample.txt'), sddl(restored / 'sample.txt'))
        hardlink = original / 'hardlink.txt'
        try:
            os.link(original / 'sample.txt', hardlink)
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_HARDLINK_REJECTED'):
                reject_reparse(original)
        finally:
            if hardlink.exists():
                hardlink.unlink()
        junction = original / 'junction'
        try:
            powershell("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:HSC_SYNTHETIC_LINK "
                       "-Target $env:HSC_SYNTHETIC_DEST | Out-Null",
                       {'HSC_SYNTHETIC_LINK': str(junction), 'HSC_SYNTHETIC_DEST': str(restored)})
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_REPARSE_REJECTED'):
                reject_reparse(original)
        finally:
            if os.path.lexists(junction):
                attributes = junction.lstat().st_file_attributes
                if not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise RuntimeError('SYNTHETIC_JUNCTION_IDENTITY_CHANGED')
                os.rmdir(junction)


if __name__ == '__main__':
    unittest.main(verbosity=2)
