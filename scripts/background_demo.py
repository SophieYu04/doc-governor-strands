"""Live-model, isolated schema-change demonstration. Never runs against an app/backend."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from docgov.background import Worker, atomic_json, private_dir
from docgov.install import install
from docgov.mcp_server import DocumentSupply, build_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    if root.exists():
        parser.error('output must be a new directory')
    shutil.copytree(Path(__file__).resolve().parent.parent/'examples/background-schema',root)
    def git(*argv):
        return subprocess.check_output(['git',*argv],cwd=root,stderr=subprocess.PIPE).decode().strip()
    git('init','-q');git('config','user.name','Doc Governor isolated demo');git('config','user.email','demo@example.invalid')
    git('add','.');git('commit','-qm','Initial schema and owner-authorized contract')
    import shlex
    install(root,verify_command=shlex.join([sys.executable,'verify.py']),start=False)
    # Development action: change schema and commit. No documentation prompt.
    schema=root/'schema.sql'
    schema.write_text(schema.read_text().replace('display_name TEXT NOT NULL','display_name TEXT NOT NULL,\n    bio TEXT'))
    git('add','schema.sql');git('commit','-qm','Add optional profile bio field')
    started=time.monotonic()
    worker=Worker(root)
    try:
        while time.monotonic()-started<310:
            worker.run(once=True)
            jobs=worker.jobs()
            if jobs and all(job['state'] in {'complete','failed'} for job in jobs):
                break
            time.sleep(0.5)
        response=DocumentSupply(build_config(['--root',str(root)])).get_document('docs/SCHEMA.md')
        result=dict(live_model_requested=True,model_doubles_used=False,
                    elapsed_seconds=round(time.monotonic()-started,2),
                    jobs=[{k:job.get(k) for k in ('state','phase','attempts','elapsed','error_code')} for job in worker.jobs()],
                    mcp_code=response['code'],
                    new_field_documented=response['code']=='ok' and 'bio' in response['content'],
                    model_cost_usd=None,source_head=git('rev-parse','HEAD'),
                    staged_paths=git('diff','--cached','--name-only').splitlines())
        atomic_json(root.parent/(root.name+'-result.json'),result)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['new_field_documented'] else 2
    finally:
        worker.db.close()

if __name__=='__main__':raise SystemExit(main())
