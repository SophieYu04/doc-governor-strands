"""Durable per-document coordination; the MCP read path never imports this module.

Work stays in the worktree's private Git directory. Models run in isolated
repositories, and a completed repair is not publishable until independent review.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from .coding_review import POLICY, review_documents
from .engine import build_snapshot, dependency_evidence, dependency_fingerprint, has_matching_coding_review
from .repair import repair_candidates
from .repair_executor import RepairBlocked, _env, _git, _index_entries, _working_signature, repair_staged
from .trust_state import build_trust_state, write_trust_state

CATALOG = '.docgov/catalog.yaml'
TRANSIENT = {'command_timeout', 'model_timeout', 'model_throttled', 'index_locked', 'review_locked'}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def private_dir(root: Path) -> Path:
    path = Path(_git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'docgov').decode().strip())
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def control_hash(root: Path, path: str) -> str:
    if path == CATALOG:
        from .catalog import Catalog
        value = Catalog.load(root / path).to_dict()
        for record in value.get('documents', []):
            record.pop('status', None)
            record.pop('last_verified_at', None)
        return digest(value)
    return hashlib.sha256((root / path).read_bytes()).hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def inventory(root: Path) -> set[str]:
    paths = _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    return {p.decode() for p in paths.split(b'\0') if p and p != b'.docgov/coding-review.lock'}


class Worker:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.home = private_dir(self.root)
        self.config = json.loads((self.home / 'config.json').read_text())
        self.db = sqlite3.connect(self.home / 'jobs.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, target TEXT, state TEXT, payload TEXT)')
        self.db.commit()

    def save(self, job):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)',
                            (job['id'], job['target'], job['state'], json.dumps(job, sort_keys=True)))

    def jobs(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM jobs ORDER BY rowid')]

    def authorized(self):
        # Installation pins the owner-controlled policy. Generated prose or an
        # edited catalog cannot expand this worker's grant.
        for path, sha in self.config['controls'].items():
            if control_hash(self.root, path) != sha:
                raise RepairBlocked('installed_policy_changed')

    def discover(self):
        self.authorized()
        snapshot = build_snapshot(self.root, self.root / CATALOG)
        snapshot.changed = sorted(inventory(self.root))
        candidates = repair_candidates(snapshot)
        existing = {job['id'] for job in self.jobs()}
        for record in candidates:
            if record.path not in self.config['documents'] or record.type != 'contract':
                continue
            evidence = dependency_evidence(snapshot, record)
            if not evidence or has_matching_coding_review(snapshot, record):
                continue
            identity = digest([record.path, snapshot.files.get(record.path),
                               dependency_fingerprint(evidence), self.config['controls']])
            if identity in existing:
                continue
            self.save(dict(id=identity, target=record.path, state='pending', phase='repair',
                           sources=[e.path for e in evidence], attempts=0, elapsed=0,
                           ready_at=time.time() + 2, notified=False))

    def prepare(self, job):
        directory = self.home / 'work' / job['id']
        directory.mkdir(parents=True, exist_ok=True)
        isolated = directory / 'repo'
        if (directory / 'snapshot.json').exists():
            return directory
        live = build_snapshot(self.root, self.root / CATALOG)
        record = live.catalog.record_for(job['target'])
        identity = digest([record.path, live.files.get(record.path),
                           dependency_fingerprint(dependency_evidence(live, record)), self.config['controls']])
        if identity != job['id']:
            raise RepairBlocked('source_changed_during_repair')
        if isolated.exists():
            # An incomplete clone is private disposable state, never user work.
            import shutil
            shutil.rmtree(isolated)
        isolated.mkdir()
        paths = inventory(self.root)
        signature = _working_signature(self.root, paths)
        head = _git(self.root, 'rev-parse', 'HEAD').decode().strip()
        tree = _git(self.root, 'write-tree').decode().strip()
        _git(isolated, 'init', '-q')
        _git(isolated, '-c', 'protocol.file.allow=always', 'fetch', '--quiet', '--no-tags', str(self.root), head)
        _git(isolated, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
        for name in set(_index_entries(isolated)) | paths:
            source, target = self.root / name, isolated / name
            if name not in paths or not source.exists():
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                target.chmod(source.stat().st_mode & 0o777)
        _git(isolated, 'add', '-A')
        if (inventory(self.root) != paths or _working_signature(self.root, paths) != signature
                or _git(self.root, 'rev-parse', 'HEAD').decode().strip() != head
                or _git(self.root, 'write-tree').decode().strip() != tree):
            raise RepairBlocked('source_changed_during_repair')
        atomic_json(directory / 'snapshot.json', dict(signature=signature, head=head, tree=tree, index=_index_entries(self.root)))
        atomic_json(directory / 'job.json', dict(job=job, config=self.config))
        return directory

    def execute(self, directory: Path, phase: str, timeout: float):
        result_file = directory / (phase + '.json')
        if result_file.exists():
            return json.loads(result_file.read_text())
        env = _env()
        env.pop('DOCGOV_REPAIR_ACTIVE', None)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        if self.config.get('aws_profile'):
            env['AWS_PROFILE'] = self.config['aws_profile']
        env['AWS_REGION'] = env['AWS_DEFAULT_REGION'] = self.config.get('aws_region', 'us-west-2')
        env['DOCGOV_MODEL_TIMEOUT_SECONDS'] = '60'
        env['DOCGOV_NODE_TIMEOUT_SECONDS'] = '60'
        env['DOCGOV_GRAPH_TIMEOUT_SECONDS'] = '60'
        process = subprocess.Popen([sys.executable, '-m', 'docgov.background', str(directory), phase],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return dict(result='blocked', error_code='command_timeout')
        if not result_file.exists():
            return dict(result='blocked', error_code='phase_failed')
        return json.loads(result_file.read_text())

    def generated_base(self, saved: dict, paths: set[str]) -> bool:
        """Do not attribute a preexisting user draft to a generated repair.

        A base must be the index bytes, an installer-generated artifact, or an
        exact output of an earlier job whose base passed the same check.
        """
        owned = {path: {sha} for path, sha in self.config.get('generated_files', {}).items()}
        for previous in self.jobs():
            if previous['state'] == 'complete' and previous.get('staging_safe') is True:
                for path, sha in previous.get('outputs', {}).items():
                    owned.setdefault(path, set()).add(sha)
        for path in paths:
            before = saved['signature'].get(path)
            entry = saved['index'].get(path)
            if before is None:
                if entry is not None:
                    return False
                continue
            if before[1] in owned.get(path, set()):
                continue
            if entry is None:
                return False
            index_sha = hashlib.sha256(_git(self.root, 'cat-file', 'blob', entry[1])).hexdigest()
            if before[1] != index_sha:
                return False
        return True

    def publish(self, directory: Path, job):
        self.authorized()
        isolated = directory / 'repo'
        snapshot = json.loads((directory / 'snapshot.json').read_text())
        original = {p: tuple(value) if value else None for p, value in snapshot['signature'].items()}
        review = json.loads((directory / 'review.json').read_text())
        outputs = set(review['modified_paths']) | {job['target'], '.docgov/trust.json'}
        allowed = {job['target'], CATALOG, '.docgov/ledger.jsonl', '.docgov/trust.json'}
        if any(path not in allowed and not (path.startswith('.docgov/reviews/') and path.endswith('.json'))
               for path in outputs):
            raise RepairBlocked('unexpected_publication_scope')
        live = build_snapshot(isolated, isolated / CATALOG)
        record = live.catalog.record_for(job['target'])
        if record is None or not has_matching_coding_review(live, record):
            raise RepairBlocked('review_no_longer_matches')
        staging_safe = self.generated_base(snapshot, outputs)
        payload = {path: (isolated / path).read_bytes() for path in outputs}
        journal = directory / 'publication.json'
        if not journal.exists():
            atomic_json(journal, {path: hashlib.sha256(content).hexdigest() for path, content in payload.items()})
        paths = inventory(self.root)
        current = _working_signature(self.root, paths | set(original) | outputs)
        # A restart can encounter some/all already-published files. Accept only
        # the exact old or exact verified output; never overwrite a third value.
        for path in paths | set(original) | outputs:
            value = current.get(path)
            if value == original.get(path):
                continue
            if path in payload and value and value[1] == hashlib.sha256(payload[path]).hexdigest():
                continue
            raise RepairBlocked('source_changed_during_repair')
        if _git(self.root, 'rev-parse', 'HEAD').decode().strip() != snapshot['head']:
            raise RepairBlocked('source_changed_during_repair')
        index = Path(_git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
        lock = index.with_name(index.name + '.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RepairBlocked('index_locked') from exc
        os.close(fd)
        try:
            import shutil
            candidate_index = directory / 'publication-index'
            shutil.copyfile(index, candidate_index)
            if _git(self.root, 'write-tree', env=_env() | {'GIT_INDEX_FILE': str(candidate_index)}).decode().strip() != snapshot['tree']:
                raise RepairBlocked('source_changed_during_repair')
            for path, content in sorted(payload.items(), key=lambda item: item[0] == '.docgov/trust.json'):
                if _working_signature(self.root, {path}).get(path) != current.get(path):
                    raise RepairBlocked('source_changed_during_repair')
                destination = self.root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        finally:
            lock.unlink(missing_ok=True)
        # Never stage in the worker. The pre-commit integration may stage exact
        # outputs only after checking that staged sources match their evidence.
        job['outputs'] = {p: hashlib.sha256(b).hexdigest() for p, b in payload.items()}
        job['staging_safe'] = staging_safe
        job['state'] = 'complete'
        self.save(job)

    def tick(self):
        self.discover()
        for job in self.jobs():
            if job['state'] not in {'pending', 'running'} or job['ready_at'] > time.time():
                continue
            if job.get('running_since'):
                job['elapsed'] += max(0, time.time() - job.pop('running_since'))
            started = time.monotonic()
            job['running_since'] = time.time()
            job['state'] = 'running'
            self.save(job)
            try:
                directory = self.prepare(job)
                if job['elapsed'] >= 300:
                    raise RepairBlocked('document_timeout')
                if job['phase'] != 'publish':
                    result = self.execute(directory, job['phase'], min(60, 300 - job['elapsed']))
                    if result['result'] == 'blocked':
                        raise RepairBlocked(result.get('error_code', 'phase_failed'))
                    job['phase'] = 'review' if job['phase'] == 'repair' else 'publish'
                    job['attempts'] = 0
                    job.pop('error_code', None)
                if job['phase'] == 'publish':
                    self.publish(directory, job)
            except RepairBlocked as exc:
                code = str(exc)
                job['error_code'] = code
                if code == 'source_changed_during_repair':
                    job['state'] = 'superseded'
                    # The same content can recur after an index-only race. A
                    # superseded snapshot is removed from identity deduplication.
                    with self.db:
                        self.db.execute('DELETE FROM jobs WHERE id = ?', (job['id'],))
                    import shutil
                    shutil.rmtree(self.home / 'work' / job['id'], ignore_errors=True)
                    continue
                job['attempts'] += 1
                if code in TRANSIENT and job['attempts'] < 3 and job['elapsed'] < 300:
                    job['state'] = 'pending'
                    job['ready_at'] = time.time() + (5 if job['attempts'] == 1 else 20)
                    (self.home / 'work' / job['id'] / (job['phase'] + '.json')).unlink(missing_ok=True)
                else:
                    job['state'] = 'failed'
                    # A durable single notification record, with no source text.
                    if not job['notified']:
                        atomic_json(self.home / ('notice-' + job['id'] + '.json'),
                                    dict(document=job['target'], error_code=code))
                        job['notified'] = True
            finally:
                job['elapsed'] += time.monotonic() - started
                job.pop('running_since', None)
            self.save(job)

    def stage_ready(self):
        """Stage verified generated bytes only if the index has their exact sources."""
        import shutil
        import tempfile
        self.authorized()
        for job in self.jobs():
            if job['state'] != 'complete' or not job.get('outputs') or job.get('staging_safe') is not True:
                continue
            outputs = dict(job['outputs'])
            # The append-only ledger can refer to earlier completed reviews.
            # Include their exact owned receipt bytes, never older document text.
            for previous in self.jobs():
                if previous['state'] != 'complete' or previous.get('staging_safe') is not True:
                    continue
                for path, sha in previous.get('outputs', {}).items():
                    candidate = self.root / path
                    if (path.startswith('.docgov/reviews/') and candidate.is_file()
                            and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha):
                        outputs[path] = sha
            if any(not (self.root / p).is_file() or hashlib.sha256((self.root / p).read_bytes()).hexdigest() != sha
                   for p, sha in outputs.items()):
                continue
            saved = json.loads((self.home / 'work' / job['id'] / 'snapshot.json').read_text())
            entries = _index_entries(self.root)
            if any(entries.get(p) != (tuple(saved['index'][p]) if p in saved['index'] else None) for p in outputs):
                continue
            tree = _git(self.root, 'write-tree').decode().strip()
            staged = build_snapshot(self.root, self.root / CATALOG, source_ref=tree)
            live = build_snapshot(self.root, self.root / CATALOG)
            record = live.catalog.record_for(job['target'])
            staged_record = staged.catalog.record_for(job['target'])
            if (record is None or staged_record is None or not has_matching_coding_review(live, record)
                    or dependency_fingerprint(dependency_evidence(staged, staged_record)) !=
                       dependency_fingerprint(dependency_evidence(live, record))):
                continue
            # Unstaged policy edits cannot become trusted via a generated doc.
            if any(_git(self.root, 'show', tree + ':' + p) != (self.root / p).read_bytes()
                   for p in (CATALOG, POLICY) if p not in outputs):
                continue
            signature = _working_signature(self.root, inventory(self.root))
            index = Path(_git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
            lock = index.with_name(index.name + '.lock')
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return
            os.close(fd)
            try:
                with tempfile.TemporaryDirectory(dir=self.home) as temporary:
                    candidate = Path(temporary) / 'index'
                    shutil.copyfile(index, candidate)
                    env = _env() | {'GIT_INDEX_FILE': str(candidate)}
                    if _git(self.root, 'write-tree', env=env).decode().strip() != tree:
                        continue
                    for p in sorted(outputs):
                        blob = _git(self.root, 'hash-object', '-w', '--stdin', data=(self.root / p).read_bytes()).decode().strip()
                        _git(self.root, 'update-index', '--add', '--cacheinfo', entries.get(p, ('100644', ''))[0], blob, p, env=env)
                    if _working_signature(self.root, inventory(self.root)) != signature:
                        continue
                    shutil.copyfile(candidate, lock)
                    os.replace(lock, index)
            finally:
                lock.unlink(missing_ok=True)

    def run(self, once=False):
        with (self.home / 'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while True:
                self.tick()
                if once:
                    return
                time.sleep(0.5)


def _execute_phase(directory: Path, phase: str):
    value = json.loads((directory / 'job.json').read_text())
    job, config = value['job'], value['config']
    root = directory / 'repo'
    if phase == 'repair':
        result = repair_staged(root, enable_model=True, source_paths=job['sources'],
                               target_paths={job['target']}, model_id=config.get('model_id'),
                               executor_command=config['executor_command'],
                               verify_command=config['verify_command'], timeout=60)
    else:
        # Stage only inside the disposable snapshot, so independent review sees
        # a consistent full source tree even if the user partially staged files.
        _git(root, 'add', '-A')
        decision = review_documents(root, [job['target']], verify_command=config['verify_command'],
                                    timeout=60, codex_binary=config.get('codex_binary', 'codex'))
        result = decision.to_dict()
        result['error_code'] = decision.error
        if decision.result != 'blocked':
            snapshot = build_snapshot(root, root / CATALOG)
            original = json.loads((directory / 'snapshot.json').read_text())['signature']
            result['modified_paths'] = [name for name in inventory(root)
                if (name in {CATALOG, '.docgov/ledger.jsonl'} or name.startswith('.docgov/reviews/'))
                and (not original.get(name) or hashlib.sha256((root / name).read_bytes()).hexdigest() != original[name][1])]
            write_trust_state(root / '.docgov/trust.json',
                              build_trust_state(decision, snapshot, ledger_path=root / '.docgov/ledger.jsonl'))
    atomic_json(directory / (phase + '.json'), result)


def execute_phase(directory: Path, phase: str):
    with (directory / (phase + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (directory / (phase + '.json')).exists():
            _execute_phase(directory, phase)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('phase', choices=['repair', 'review'])
    args = parser.parse_args()
    execute_phase(args.directory, args.phase)
