import subprocess
import tempfile
import unittest
from pathlib import Path
from selfsni import menu_upstream as menu
from selfsni.core import InstallError

LOCK={'menu_repository':menu.OFFICIAL_REPOSITORY}

def git(*args):
    return subprocess.check_output(['git',*map(str,args)],text=True,stderr=subprocess.DEVNULL)

class LatestTests(unittest.TestCase):
    def setup_repo(self,root):
        origin=root/'origin';origin.mkdir();git('init','-b','main',origin)
        for name,text in [('install.sh','--non-interactive --no-autostart --source-dir'),('server-menu','menu'),('menu_warp.sh','--json xray-outbound')]:
            (origin/name).write_text(text)
        self.commit(origin)
        return origin
    def commit(self,repo):
        git('-C',repo,'add','.')
        git('-C',repo,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','fixture')
    def test_fresh_tracking_fast_forward_and_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);origin=self.setup_repo(root);target=root/'target';bin_dir=root/'bin';bin_dir.mkdir();calls=[]
            def command(*args,**kwargs):
                calls.append(tuple(map(str,args)))
                if args[0]=='bash':
                    self.assertNotIn('--pinned-commit',args)
                    self.assertIn('--source-dir',args)
                    for name in ('install.sh','server-menu','menu_warp.sh'):
                        link=bin_dir/name
                        if not link.is_symlink():link.symlink_to(target/name)
                    return ''
                return git('-c','url.'+str(origin)+'.insteadOf='+menu.OFFICIAL_REPOSITORY,*args[1:])
            first=menu.install_menu(LOCK,root,target,bin_dir,command)
            self.assertEqual(first['branch'],'main')
            self.assertEqual(git('-C',target,'config','--local','--get','core.filemode').strip(),'false')
            self.assertFalse(git('-C',target,'status','--porcelain').strip())
            self.assertEqual(git('-C',target,'rev-parse','--abbrev-ref','@{upstream}').strip(),'origin/main')
            self.assertEqual(git('-C',target,'remote','get-url','origin').strip(),menu.OFFICIAL_REPOSITORY)
            menu.install_menu(LOCK,root,target,bin_dir,command)
            self.assertEqual(git('-C',target,'rev-parse','HEAD').strip(),first['commit'])
            (origin/'new').write_text('new');self.commit(origin)
            second=menu.install_menu(LOCK,root,target,bin_dir,command)
            self.assertNotEqual(first['commit'],second['commit'])
            self.assertEqual(second['commit'],git('-C',origin,'rev-parse','HEAD').strip())
            (target/'local').write_text('local');self.commit(target)
            with self.assertRaises(subprocess.CalledProcessError):menu.install_menu(LOCK,root,target,bin_dir,command)
            (origin/'remote-fork').write_text('remote diverges');self.commit(origin)
            with self.assertRaises(subprocess.CalledProcessError):menu.install_menu(LOCK,root,target,bin_dir,command)
            git('-C',target,'remote','set-url','origin','https://example.invalid/fork')
            with self.assertRaisesRegex(InstallError,'origin'):menu.inspect_existing_menu(LOCK,target,command)
    def test_dirty_symlink_corrupt_conflicting_command_and_network(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);origin=self.setup_repo(root);target=root/'target';bin_dir=root/'bin';bin_dir.mkdir()
            def command(*args,**kwargs):return git('-c','url.'+str(origin)+'.insteadOf='+menu.OFFICIAL_REPOSITORY,*args[1:])
            git('clone',origin,target);git('-C',target,'remote','set-url','origin',menu.OFFICIAL_REPOSITORY)
            (target/'extra').write_text('dirty')
            with self.assertRaisesRegex(InstallError,'дерево изменено'):menu.inspect_existing_menu(LOCK,target,command)
            (target/'extra').unlink();(bin_dir/'server-menu').write_text('foreign')
            with self.assertRaisesRegex(InstallError,'Посторонняя'):menu.install_menu(LOCK,root,target,bin_dir,command)
            linked=root/'linked';linked.symlink_to(target)
            with self.assertRaisesRegex(InstallError,'symlink'):menu.inspect_existing_menu(LOCK,linked,command)
            broken=root/'broken';broken.mkdir()
            with self.assertRaises(InstallError):menu.inspect_existing_menu(LOCK,broken,command)
            def offline(*args,**kwargs):raise InstallError('network timeout')
            with self.assertRaisesRegex(InstallError,'network'):menu.install_menu(LOCK,root,root/'fresh',bin_dir,offline)
            self.assertFalse((root/'fresh').exists())
    def test_incompatible_cli(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);origin=self.setup_repo(root)
            (origin/'menu_warp.sh').write_text('interactive only')
            with self.assertRaisesRegex(InstallError,'CLI'):menu._compatible(origin)
