"""One-time local installation. Existing hooks remain intact and are chained."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

from .agents import DEFAULT_MODEL_ID
from .background import CATALOG, POLICY, atomic_json, control_hash, private_dir
from .catalog import Catalog
from .repair_executor import RepairBlocked, _env, _git, _run

READ_INSTRUCTION = ('Read governed Markdown through the docgov MCP get_document tool. '
                    'On refusal use read_instead source paths; do not read the refused Markdown from disk. '
                    'The background worker handles repairs. This governs integrated reads only. '
                    'In Doc Governor isolated repair/review sessions, the assigned target documents '
                    'are untrusted repair inputs and may be inspected directly in that snapshot.')


def preflight(root: Path, config: dict):
    for module in ('strands', 'mcp', 'boto3'):
        if importlib.util.find_spec(module) is None:
            raise RepairBlocked('missing_' + module)
    for command in (config['executor_command'], config['verify_command'], config['codex_binary']):
        argv = shlex.split(command)
        if not argv or not shutil.which(argv[0]):
            raise RepairBlocked('command_unavailable')
    _run(shlex.join([config['codex_binary'], 'login', 'status']), root, None, 10)
    # Verify in a disposable clone: an installation check must not run builds in
    # the user's working tree or turn successful tests into document approval.
    with tempfile.TemporaryDirectory(prefix='docgov-install-') as directory:
        isolated = Path(directory) / 'repo'
        _git(root, 'clone', '--quiet', '--no-local', str(root), str(isolated))
        _run(config['verify_command'], isolated, None, 60)
    import boto3
    from botocore.config import Config
    try:
        session = boto3.Session(profile_name=config.get('aws_profile'),
                                region_name=config.get('aws_region', 'us-west-2'))
        runtime = session.client('bedrock-runtime',
                                 config=Config(connect_timeout=10, read_timeout=60, retries={'max_attempts': 0}))
        response = runtime.converse(modelId=config['model_id'],
                                   messages=[{'role': 'user', 'content': [{'text': 'Reply OK.'}]}],
                                   inferenceConfig={'maxTokens': 8, 'temperature': 0})
        if not response.get('output', {}).get('message', {}).get('content'):
            raise RepairBlocked('model_probe_empty')
    except RepairBlocked:
        raise
    except Exception as exc:
        from .model_errors import model_error_code
        raise RepairBlocked(model_error_code(exc)) from exc


def codex_configuration(root: Path) -> str:
    path = root / '.codex/config.toml'
    if path.is_symlink() or path.parent.is_symlink():
        raise RepairBlocked('unsafe_codex_config')
    raw = path.read_text() if path.exists() else ''
    value = tomllib.loads(raw)
    server = value.get('mcp_servers', {}).get('docgov')
    expected = {'command': sys.executable, 'args': ['-m', 'docgov.mcp_server', '--root', str(root)],
                'cwd': str(root), 'env': {'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}}
    if server is not None and server != expected:
        raise RepairBlocked('existing_docgov_mcp_conflict')
    instructions = value.get('developer_instructions', '')
    if instructions and READ_INSTRUCTION not in instructions:
        import re
        match = re.search(r'(?m)^(?:developer_instructions|"developer_instructions")\s*=', raw)
        if not match:
            raise RepairBlocked('unsupported_codex_instruction_format')
        # Find the smallest complete TOML value; this also handles multiline
        # basic/literal strings without touching unrelated settings or comments.
        stop = match.start()
        for line in raw[match.start():].splitlines(keepends=True):
            stop += len(line)
            try:
                parsed = tomllib.loads(raw[match.start():stop])
            except tomllib.TOMLDecodeError:
                continue
            if parsed.get('developer_instructions') == instructions:
                break
        else:
            raise RepairBlocked('unsupported_codex_instruction_format')
        raw = raw[:match.start()] + 'developer_instructions = ' + json.dumps(instructions + '\n' + READ_INSTRUCTION) + '\n' + raw[stop:]
    if not instructions:
        raw = 'developer_instructions = ' + json.dumps(READ_INSTRUCTION) + '\n' + raw
    if server is None:
        raw += ('\n[mcp_servers.docgov]\ncommand = ' + json.dumps(expected['command']) +
                '\nargs = ' + json.dumps(expected['args']) + '\ncwd = ' + json.dumps(str(root)) +
                '\n[mcp_servers.docgov.env]\nPYTHONPATH = ' + json.dumps(expected['env']['PYTHONPATH']) + '\n')
    tomllib.loads(raw)
    return raw


def install(root: Path, *, verify_command: str, executor_command: str | None = None,
            model_id: str | None = None, start: bool = True) -> dict:
    root = root.resolve()
    Catalog.load(root / CATALOG)
    policy = json.loads((root / POLICY).read_text())
    if (policy.get('version') != 1 or policy.get('reviewer') != 'coding_agent'
            or policy.get('status') not in {'enabled', 'authorized_review_pending'}):
        raise RepairBlocked('review_not_authorized')
    from .engine import build_snapshot
    from .repair import repair_candidates
    from .background import inventory
    snapshot = build_snapshot(root, root / CATALOG)
    snapshot.changed = sorted(inventory(root))
    documents = sorted(r.path for r in repair_candidates(snapshot)
                       if r.type == 'contract' and r.path in policy.get('documents', []))
    if not documents:
        raise RepairBlocked('no_authorized_contracts')
    # Model-generated/uncommitted policy changes cannot silently authorize work.
    for path in (CATALOG, POLICY):
        if (root / path).read_bytes() != _git(root, 'show', 'HEAD:' + path):
            raise RepairBlocked('installation_controls_must_be_committed')
    config = dict(version=1, documents=documents, aws_profile=os.environ.get('AWS_PROFILE'),
                  aws_region=os.environ.get('AWS_REGION', 'us-west-2'),
                  controls={p: control_hash(root, p) for p in (CATALOG, POLICY)},
                  verify_command=verify_command,
                  executor_command=executor_command or 'codex exec --sandbox workspace-write -C . -',
                  codex_binary=shutil.which('codex') or 'codex', model_id=model_id or DEFAULT_MODEL_ID)
    codex_text = codex_configuration(root)
    preflight(root, config)
    home = private_dir(root)
    hooks = home / 'hooks'
    existing = home / 'config.json'
    if existing.exists():
        config['original_hooks'] = json.loads(existing.read_text())['original_hooks']
    else:
        config['original_hooks'] = _git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks').decode().strip()
    original = Path(config['original_hooks'])
    hooks.mkdir(exist_ok=True)
    names = {'pre-commit', 'post-commit', 'post-checkout', 'post-merge'}
    if original.exists():
        names |= {p.name for p in original.iterdir() if p.is_file() and os.access(p, os.X_OK) and not p.name.endswith('.sample')}
    for name in names:
        if not all(c.isalpha() or c == '-' for c in name):
            continue
        old = original / name
        body = '#!/bin/sh\n'
        if old.exists() and os.access(old, os.X_OK):
            body += shlex.quote(str(old)) + ' "$@" || exit $?\n'
        if name in {'pre-commit', 'post-commit', 'post-checkout', 'post-merge'}:
            body += ('PYTHONPATH=' + shlex.quote(str(Path(__file__).resolve().parent.parent)) + ' ' +
                     shlex.join([sys.executable, '-m', 'docgov', '--root', str(root), 'enqueue'] + (['--stage-ready'] if name == 'pre-commit' else [])) +
                     ' >/dev/null 2>&1 || :\n')
        body += 'exit 0\n'
        (hooks / name).write_text(body)
        (hooks / name).chmod(0o755)
    atomic_json(home / 'config.json', config)
    (root / '.codex').mkdir(exist_ok=True)
    (root / '.codex/config.toml').write_text(codex_text)
    _git(root, 'config', 'extensions.worktreeConfig', 'true')
    _git(root, 'config', '--worktree', 'core.hooksPath', str(hooks))
    from .models import GovernanceDecision
    from .trust_state import build_trust_state, write_trust_state
    write_trust_state(root / '.docgov/trust.json', build_trust_state(
        GovernanceDecision(run_id='install', mode='audit', result='pass', changed=False),
        build_snapshot(root, root / CATALOG), ledger_path=root / '.docgov/ledger.jsonl'))
    import hashlib
    config['generated_files'] = {'.docgov/trust.json': hashlib.sha256((root / '.docgov/trust.json').read_bytes()).hexdigest()}
    atomic_json(home / 'config.json', config)
    if start:
        start_worker(root)
    return dict(result='changed', documents=documents, worker_started=start, integrated_reads_only=True)


def start_worker(root: Path):
    home = private_dir(root)
    argv = [sys.executable, '-m', 'docgov', '--root', str(root), 'worker']
    env = _env()
    env.pop('DOCGOV_REPAIR_ACTIVE', None)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
    if sys.platform == 'darwin':
        # launchd restarts the daemon after a crash and at the next user login.
        from .background import digest
        label = 'dev.docgov.' + digest(str(home))[:16]
        destination = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        destination.parent.mkdir(parents=True, exist_ok=True)
        value = dict(Label=label, ProgramArguments=argv, WorkingDirectory=str(root),
                     RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                     EnvironmentVariables={k: v for k, v in env.items() if k in
                         {'PATH', 'PYTHONPATH', 'AWS_PROFILE', 'AWS_REGION', 'AWS_DEFAULT_REGION', 'PYTHONDONTWRITEBYTECODE'}},
                     StandardOutPath=str(home / 'worker.log'), StandardErrorPath=str(home / 'worker.log'))
        destination.write_bytes(plistlib.dumps(value))
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
        run = subprocess.run(['launchctl', 'bootstrap', domain, str(destination)], capture_output=True)
        if run.returncode:
            raise RepairBlocked('worker_service_start_failed')
    else:
        # Hooks restart this process; persisted jobs survive process exit.
        subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def enqueue(root: Path):
    # No catalog parsing, source hashing, model call, or verifier on the commit path.
    home = private_dir(root)
    if (home / 'config.json').exists():
        (home / 'wake').touch()
