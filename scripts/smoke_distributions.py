"""Install built wheels/sdists into fresh temporary environments and run away from source."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def run(command, cwd, env):
    p=subprocess.run(command,cwd=cwd,env=env,text=True,capture_output=True,timeout=120)
    if p.returncode:
        raise RuntimeError(p.stdout+'\n'+p.stderr)
    return p.stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    artifacts=sorted([*args.directory.glob('*.whl'),*args.directory.glob('*.tar.gz')])
    if len(artifacts)!=2: raise SystemExit('Expected exactly one wheel and one sdist')
    root=Path(__file__).resolve().parents[1]
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME','HOME_AI_HA_TOKEN')}
    env['PYTHONDONTWRITEBYTECODE']='1'
    with tempfile.TemporaryDirectory(prefix='home-ai-install-') as folder:
        for idx,artifact in enumerate(artifacts):
            work=Path(folder)/str(idx); work.mkdir()
            venv.EnvBuilder(with_pip=True).create(work/'venv')
            python=work/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            if artifact.name.endswith('.tar.gz'):
                run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(root/'requirements-build.txt')],work,env)
            run([str(python),'-m','pip','install','--disable-pip-version-check','--no-deps','--no-build-isolation',str(artifact.resolve())],work,env)
            # This guard is installed inside the isolated environment; any demo socket connection fails.
            guard = "import socket,runpy; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Network forbidden')); runpy.run_module('home_ai',run_name='__main__')"
            rows=[json.loads(row) for row in run([str(python),'-I','-c',guard,'demo'],work,env).splitlines()]
            assert len(rows)==3 and all(row['metrics']['executed_actions']==0 for row in rows)
            assert json.loads(run([str(python),'-I','-m','home_ai','doctor','--json'],work,env))['config_valid']
            assert json.loads(run([str(python),'-I','-m','home_ai','validate-config'],work,env))['status']=='ok'
            # Verify the installed console entry point as well as python -m.
            cli=python.parent/('home-ai.exe' if os.name=='nt' else 'home-ai')
            console=run([str(cli),'demo'],work,env)
            assert len(console.splitlines())==3
            print(json.dumps({'artifact':artifact.name,'fresh_install':'PASS','outside_repo_demo':'PASS','console_demo':'PASS'}),flush=True)
    return 0

if __name__=='__main__': raise SystemExit(main())
