"""Build reviewed source archives and distributions from one Git commit.
Requires requirements-build.txt in an isolated environment. Never invokes Docker.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from importlib.metadata import version
from pathlib import Path
from check_public import scan_artifact


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--out", type=Path, required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    commit=subprocess.check_output(['git','rev-parse','--verify','--end-of-options',args.ref+'^{commit}'],cwd=root).decode().strip()
    output=args.out.resolve()
    if output.exists() and any(output.iterdir()): raise SystemExit('Output directory must be empty; preserve old release artifacts')
    archive=subprocess.check_output(['git','archive','--format=zip',commit],cwd=root)
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='home-ai-release-') as folder:
        source=Path(folder)/'source'; source.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            for name in z.namelist():
                if not (source/name).resolve().is_relative_to(source.resolve()): raise SystemExit('Unsafe source archive')
            z.extractall(source)
        metadata=tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'))
        package_version=metadata['project']['version']
        result=subprocess.run([sys.executable,'-m','build','--no-isolation','--outdir',str(output)],cwd=source,capture_output=True,text=True)
        if result.returncode:
            print(result.stdout); print(result.stderr); return result.returncode
    prefix='home-ai-'+package_version+'/'
    zip_path=output/('home-ai-'+package_version+'-source.zip')
    subprocess.run(['git','archive','--format=zip','--prefix='+prefix,'--output='+str(zip_path),commit],cwd=root,check=True)
    artifacts=sorted(p for p in output.iterdir() if p.is_file())
    for artifact in artifacts:
        failures=scan_artifact(artifact)
        if failures: raise SystemExit('Artifact scan failed: '+artifact.name+'; inspect with check_public.py')
    manifest={'commit':commit,'package_version':package_version,'archive_prefix':prefix,
              'python':sys.version.split()[0],'build_tools':{p:version(p) for p in ('build','setuptools','wheel','packaging','pyproject_hooks')},
              'artifacts':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts},
              'reproducibility':'Functional installation and source content; byte-identical wheels not claimed'}
    (output/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
