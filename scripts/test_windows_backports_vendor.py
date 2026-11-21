import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path=Path(__file__).with_name('check_windows_backports_vendor.py')
spec=importlib.util.spec_from_file_location('vendor_check',path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class VendorCheckTests(unittest.TestCase):
    def test_windows_build_adds_only_reviewed_vendor_files_and_license(self):
        package = json.loads(path.parent.parent.joinpath('package.json').read_text(encoding='utf-8'))
        scripts = package['scripts']
        self.assertIn('check_windows_backports_vendor.py', scripts['prebuild:agent:win'])
        command = scripts['build:agent:win']
        for name in module.EXPECTED:
            self.assertIn('setuptools/_vendor/' + name, command)
        self.assertIn('licenses/backports.tarfile', command)
        self.assertEqual(command.count('setuptools/_vendor/backports'), len(module.EXPECTED))

    def test_expected_files_are_checked_and_tampering_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'backports'/'__init__.py';file.parent.mkdir()
            file.write_bytes(b'synthetic reviewed content')
            expected={'backports/__init__.py':hashlib.sha256(file.read_bytes()).hexdigest()}
            module.validate_vendor(root,expected)
            file.write_bytes(b'modified content')
            with self.assertRaises(ValueError):module.validate_vendor(root,expected)
            file.unlink()
            with self.assertRaises(FileNotFoundError):module.validate_vendor(root,expected)

if __name__ == '__main__':unittest.main()
