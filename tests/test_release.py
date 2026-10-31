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
            # RFC 1952 XFL=2 records maximum compression; do not inherit a
            # changed Python default (3.15 changed gzip's default to level 6).
            self.assertEqual(archive.read_bytes()[8], 2)
            first = hashlib.sha256(archive.read_bytes()).digest()
            report.chmod(0o644)
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(first, hashlib.sha256(archive.read_bytes()).digest())
            with tarfile.open(archive) as tf:
                self.assertEqual(tf.getmember('selfsni/docs/report.json').mode, 0o644)
                self.assertEqual(tf.getmember('selfsni/install.sh').mode, 0o755)

    def test_release_assets_pin_payload_version_and_current_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tools').mkdir()
            for name in ('release.py', 'package.py'):
                shutil.copyfile('tools/' + name, root / 'tools' / name)
            shutil.copyfile('install.sh', root / 'install.sh')
            (root / 'versions.lock.json').write_text('{}\n')
            (root / 'VERSION').write_text('1.2.3\n')
            (root / 'CHANGELOG.md').write_text(
                '# Changes\n\n## v1.2.3 - Preview\n\nCurrent changes.\n\n'
                '## v1.2.2\n\nHistorical E2E success.\n')
            command = [sys.executable, str(root / 'tools/release.py'), '--tag', 'v1.2.3']
            subprocess.run(command, check=True, capture_output=True)
            dist = root / 'dist'
            for line in (dist / 'SHA256SUMS').read_text().splitlines():
                checksum, name = line.split()
                self.assertEqual(checksum, hashlib.sha256((dist / name).read_bytes()).hexdigest())
            bootstrap = (dist / 'install.sh').read_text()
            self.assertIn('/releases/download/v1.2.3/selfsni-1.2.3.tar.gz', bootstrap)
            self.assertIn(hashlib.sha256((dist / 'selfsni-1.2.3.tar.gz').read_bytes()).hexdigest(), bootstrap)
            notes = (dist / 'release-notes.md').read_text()
            self.assertIn('Current changes.', notes)
            self.assertNotIn('Historical E2E success.', notes)
            self.assertIn('NOT TESTED', notes)
            first = (dist / 'SHA256SUMS').read_bytes()
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(first, (dist / 'SHA256SUMS').read_bytes())
            with tarfile.open(dist / 'selfsni-1.2.3.tar.gz') as tf:
                self.assertEqual(tf.extractfile('selfsni/VERSION').read(), b'1.2.3\n')
                self.assertTrue(all(name.startswith('selfsni/') for name in tf.getnames()))
            mismatch = subprocess.run(command[:-1] + ['v1.2.4'], capture_output=True)
            self.assertNotEqual(mismatch.returncode, 0)
