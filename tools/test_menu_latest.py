#!/usr/bin/env python3
"""Actual official installer and menu item 9 in isolated temporary paths."""
import os
import sys
import shutil
from pathlib import Path
import subprocess
import tempfile
from selfsni.menu_upstream import OFFICIAL_REPOSITORY


def main():
    with tempfile.TemporaryDirectory(prefix='bob-menu-live-') as d:
        root=Path(d);origin=root/'origin';target=root/'target'
        def git(*args,env=None):return subprocess.check_output(['git',*map(str,args)],text=True,env=env,stderr=subprocess.DEVNULL).strip()
        git('clone','--branch','main','--single-branch',OFFICIAL_REPOSITORY,origin)
        revision=git('-C',origin,'rev-parse','HEAD')
        for test in ('test_warp_endpoints.sh','test_warp_cron.sh'):
            subprocess.run(['bash',str(origin/'tests'/test)],check=True,stdout=subprocess.DEVNULL)
        shim=root/'shim';shim.mkdir()
        real_git=shutil.which('git')
        wrapper=shim/'git'
        wrapper.write_text('#!'+sys.executable+'\nimport os,sys\na=sys.argv[1:]\nreal='+repr(real_git)+'\n'+
                          'if "fetch" in a: a=["-c",'+repr('url.'+str(origin)+'.insteadOf='+OFFICIAL_REPOSITORY)+']+a\n'+
                          'os.execv(real,[real]+a)\n')
        wrapper.chmod(0o755)
        env=dict(os.environ,PATH=str(shim)+os.pathsep+os.environ['PATH'],
                 VPS_MENU_TARGET_DIR=str(target),VPS_MENU_BIN_DIR=str(root/'bin'),VPS_MENU_BASHRC=str(root/'bashrc'),
                 VPS_MENU_SKIP_DEPS='1',VPS_MENU_ALLOW_NON_ROOT_TEST='1')
        git('clone','--branch','main','--single-branch',origin,target)
        git('-C',target,'remote','set-url','origin',OFFICIAL_REPOSITORY)
        (root/'bashrc').write_text('# unchanged\n')
        subprocess.run(['bash',str(target/'install.sh'),'--non-interactive','--no-autostart','--source-dir',str(target)],env=env,check=True,stdout=subprocess.DEVNULL)
        assert git('-C',target,'branch','--show-current')=='main'
        assert git('-C',target,'rev-parse','--abbrev-ref','@{upstream}')=='origin/main'
        assert not git('-c','core.filemode=false','-C',target,'status','--porcelain')
        script=(target/'server-menu').read_text()
        state_function=script[script.index('function menu_git_state'):script.index('function check_for_updates')]
        def state():return subprocess.check_output(['bash','-c',state_function+'\nmenu_git_state'],env=env,text=True).strip()
        assert state()=='current'
        (origin/'bob-test-update.txt').write_text('isolated fast-forward fixture\n')
        git('-C',origin,'add','.')
        git('-C',origin,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','isolated updater fixture')
        assert state()=='behind'
        # Execute the author's exact item-9 update condition, including installer.
        start=script.index('if [ "$state" = behind ] &&')
        condition=script[start+3:script.index('; then',start)]
        subprocess.run(['bash','-ec','state=behind\n'+condition],env=env,check=True,stdout=subprocess.DEVNULL)
        assert state()=='current' and git('-C',target,'rev-parse','HEAD')==git('-C',origin,'rev-parse','HEAD')
        assert (root/'bashrc').read_text()=='# unchanged\n'
        assert (root/'bin/server-menu').is_symlink() and (root/'bin/menu_warp.sh').is_symlink()
        print('PASS official main revision '+revision+': main tracking origin/main; official installer; item 9 current/behind/fast-forward/current; bashrc unchanged')

if __name__=='__main__':main()
