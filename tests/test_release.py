import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


class ReleaseTests(unittest.TestCase):
    def test_archive_is_independent_of_local_read_write_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'tools').mkdir()
            (root / 'docs').mkdir()
            shutil.copyfile('tools/package.py', root / 'tools/package.py')
            (root / 'VERSION').write_text('1.2.3\n')
            (root / 'install.sh').write_text('#!/bin/bash\nexit 0\n')
            (root / 'install.sh').chmod(0o755)
            (root / 'versions.lock.json').write_text('{}\n')
            report = root / 'docs/report.json'
            report.write_text('{"passed":true}\n')
            report.chmod(0o600)
            command = [sys.executable, str(root / 'tools/package.py')]
            subprocess.run(command, check=True, capture_output=True)
            archive = root / 'dist/selfsni-1.2.3.tar.gz'
            first = hashlib.sha256(archive.read_bytes()).digest()
            report.chmod(0o644)
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(first, hashlib.sha256(archive.read_bytes()).digest())
            with tarfile.open(archive) as tf:
                self.assertEqual(tf.getmember('selfsni/docs/report.json').mode, 0o644)
                self.assertEqual(tf.getmember('selfsni/install.sh').mode, 0o755)
