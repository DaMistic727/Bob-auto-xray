"""Exercise standalone bootstrap without installing anything or using a network.
The fake curl transports a local fixture; SHA256 and archive validation are real.
"""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

class BootstrapTests(unittest.TestCase):
    def exercise(self, correct=True, unsafe=False):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            bootstrap=root/'install.sh'
            shutil.copyfile('install.sh',bootstrap)
            archive=root/'fixture.tar.gz'
            with tarfile.open(archive,'w:gz') as tf:
                script=b'#!/usr/bin/env bash\nprintf "BOOTSTRAP_OK\\n"\n'
                info=tarfile.TarInfo('selfsni/install.sh');info.size=len(script)
                tf.addfile(info,io.BytesIO(script))
                if unsafe:
                    info=tarfile.TarInfo('selfsni/escape');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
                    tf.addfile(info)
            fakebin=root/'bin';fakebin.mkdir()
            curl=fakebin/'curl'
            curl.write_text('#!/usr/bin/env python3\nimport os,shutil,sys\nshutil.copyfile(os.environ["BOOTSTRAP_FIXTURE"],sys.argv[sys.argv.index("-o")+1])\n')
            curl.chmod(0o755)
            checksum=hashlib.sha256(archive.read_bytes()).hexdigest() if correct else '0'*64
            env=dict(os.environ,PATH=str(fakebin)+':'+os.environ['PATH'],BOOTSTRAP_FIXTURE=str(archive),
                     SELFSNI_RELEASE_URL='https://release.invalid/v1/fixture.tar.gz',SELFSNI_RELEASE_SHA256=checksum)
            return subprocess.run(['bash',str(bootstrap)],env=env,capture_output=True,text=True,timeout=30)

    def test_verified_release_executes(self):
        r=self.exercise();self.assertEqual(r.returncode,0,r.stderr);self.assertIn('BOOTSTRAP_OK',r.stdout)
        self.assertNotIn('% Total', r.stderr)
        self.assertIn('SHA256 подтверждён', r.stderr)

    def test_tampered_release_never_executes(self):
        r=self.exercise(correct=False);self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)

    def test_symlink_archive_never_executes(self):
        r=self.exercise(unsafe=True);self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)
