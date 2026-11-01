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
    def exercise(self, correct=True, unsafe=False, args=(), release_env=True):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            bootstrap=root/'install.sh'
            shutil.copyfile('install.sh',bootstrap)
            archive=root/'fixture.tar.gz'
            with tarfile.open(archive,'w:gz') as tf:
                script=b'#!/usr/bin/env bash\nprintf "BOOTSTRAP_OK %s\\n" "$*"\n'
                info=tarfile.TarInfo('selfsni/install.sh');info.size=len(script)
                tf.addfile(info,io.BytesIO(script))
                if unsafe:
                    info=tarfile.TarInfo('selfsni/escape');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd'
                    tf.addfile(info)
            fakebin=root/'bin';fakebin.mkdir()
            curl=fakebin/'curl'
            curl.write_text('#!/usr/bin/env python3\nimport os,shutil,sys\nopen(os.environ["BOOTSTRAP_FIXTURE"]+".fetched","w").close()\n'
                            'shutil.copyfile(os.environ["BOOTSTRAP_FIXTURE"],sys.argv[sys.argv.index("-o")+1])\n')
            curl.chmod(0o755)
            checksum=hashlib.sha256(archive.read_bytes()).hexdigest() if correct else '0'*64
            env={k:v for k,v in os.environ.items() if not k.startswith('SELFSNI_RELEASE_')}
            env.update(PATH=str(fakebin)+':'+os.environ['PATH'],BOOTSTRAP_FIXTURE=str(archive))
            if release_env:
                env.update(SELFSNI_RELEASE_URL='https://release.invalid/v1/fixture.tar.gz',SELFSNI_RELEASE_SHA256=checksum)
            result=subprocess.run(['bash',str(bootstrap),*args],env=env,capture_output=True,text=True,timeout=30)
            result.fetched=Path(str(archive)+'.fetched').exists()
            return result

    def test_verified_release_executes(self):
        r=self.exercise();self.assertEqual(r.returncode,0,r.stderr);self.assertIn('BOOTSTRAP_OK',r.stdout)
        self.assertNotIn('% Total', r.stderr)
        self.assertIn('SHA256 подтверждён', r.stderr)

    def test_tampered_release_never_executes(self):
        r=self.exercise(correct=False);self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)

    def test_symlink_archive_never_executes(self):
        r=self.exercise(unsafe=True);self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)

    def test_doctor_arguments_reach_only_the_verified_payload(self):
        r=self.exercise(args=('doctor','--json'));self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('BOOTSTRAP_OK doctor --json', r.stdout)
        r=self.exercise(correct=False,args=('doctor','--json'))
        self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)

    def test_repository_bootstrap_requires_release_url_and_sha256(self):
        # Only a release asset carries both values; without them nothing is downloaded.
        r=self.exercise(args=('doctor','--json'),release_env=False)
        self.assertNotEqual(r.returncode,0);self.assertNotIn('BOOTSTRAP_OK',r.stdout)
        self.assertFalse(r.fetched)
        self.assertIn('SELFSNI_RELEASE_URL', r.stderr)
