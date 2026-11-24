#!/usr/bin/env python3
"""Agentless auto-learning cycle for collector-first weather research.

One ``run`` takes an outcome-free snapshot of the newest collector rows, replays
the champion and every active candidate commit in isolation, compares them with
trusted code, binds later finalized outcomes to earlier sealed pairs, scores the
pairs and writes a report for the research agent. It never trades, touches
accounts or services, changes the collector, or promotes a candidate; promotion
is an operator decision (``set-champion``).

Commands: ``init`` (create a work root), ``run`` (one cycle), ``register`` (the
research agent queues a committed candidate), ``adopt-pair`` (queue an existing
sealed comparison for outcome binding), ``set-champion`` (operator only) and
``status``. See docs/auto-learning-loop.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True

CONFIG_SCHEMA = 'auto-learning-config-v1'
DOCKER_LABEL = 'org.prediction-bot.auto-learning'
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
STORE_RELATIVE = Path('collector/data/derived_reports/research')
REQUIRED_CONFIG = ('collector_root', 'trusted_checkout', 'python', 'executor', 'index', 'manifest',
                   'protected_roots', 'resolutions', 'page_rows', 'max_active_candidates')
DEFAULTS = {
    'image': None,
    'container_python': '/opt/hermes/.venv/bin/python3',
    'min_new_rows': None,  # defaults to page_rows
    'min_free_bytes': 50 * 1024 ** 3,
    'replay_timeout_seconds': 600,
    'trusted_timeout_seconds': 900,
    'feed_wall_seconds': 600,
    'feed_memory_bytes': 2 * 1024 ** 3,
    'max_index_bytes': 8 * 1024 ** 3,
    'max_raw_row_bytes': 8 * 1024 ** 2,
    'max_input_bytes': 256 * 1024 ** 2,
    'max_resolution_bytes': 512 * 1024 ** 2,
    'max_decision_bytes': 256 * 1024 ** 2,
    'conclude_min_resolved_changes': 30,
    'candidate_max_age_days': 7,
    'pending_max_age_days': 7,
    'max_candidate_failures': 2,
    'notify': 'events',
}
# Knowledge clocks (when something was known/fetched) that must not be later than the decision
# observation: these names plus any key ending in ``_at``/``_as_of``/``_timestamp``. Forecast-target
# keys (``target_date``, ``forecast_date``, ``market_target_date``) are not clocks and are ignored.
KNOWLEDGE_CLOCK_KEYS = {'as_of', 'source_timestamp', 'timestamp'}
CLOCK_SUFFIXES = ('_at', '_as_of', '_timestamp')
DECISION_IDENTITY_KEYS = {'decision_key', 'observed_at'}


class CycleError(Exception):
    """An actionable failure: the message says what failed and what to reconcile."""


class LedgerUnstable(CycleError):
    """The live resolution ledger was being rewritten; retry next cycle."""


# --------------------------------------------------------------------------- utilities

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or 'T' not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def age_days(value) -> float:
    parsed = parse_time(value)
    if parsed is None:
        raise CycleError(f'invalid stored timestamp {value!r}; reconcile the record')
    return (utcnow() - parsed).total_seconds() / 86400


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha(value) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in '0123456789abcdef' for c in value)


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n'


def write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(dumps(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_jsonl(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path, limit: int = 16 * 1024 * 1024):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CycleError(f'{path} exceeds {limit} bytes')
    return json.loads(data)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise CycleError(f'{path}:{number} is not a JSON object')
                rows.append(row)
    return rows


def tail(text: str, size: int = 1500) -> str:
    text = (text or '').strip()
    return text[-size:]


def run_command(command: list[str], *, name: str, env: dict | None = None, cwd: Path | str | None = None,
                timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run([str(part) for part in command], env=env, cwd=cwd, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CycleError(f'{name} exceeded {timeout}s; partial outputs retained for inspection') from exc
    if result.returncode != 0:
        raise CycleError(f'{name} failed (exit {result.returncode}): {tail(result.stderr) or tail(result.stdout)}')
    return result


def git(*args: str, cwd: Path) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    return run_command(['git', *args], name=f'git {args[0]}', env=env, cwd=cwd).stdout.strip()


def low_priority_prefix() -> list[str]:
    prefix = []
    if shutil.which('nice'):
        prefix += ['nice', '-n', '10']
    if shutil.which('ionice'):
        prefix += ['ionice', '-c', '3']
    return prefix


# --------------------------------------------------------------------------- work root

class Work:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.config = self.root / 'config.json'
        self.champion = self.root / 'champion.json'
        self.champion_history = self.root / 'champion-history.jsonl'
        self.lock = self.root / '.cycle.lock'
        self.feed_state = self.root / 'state' / 'feed.json'
        self.feeds = self.root / 'feeds'
        self.code = self.root / 'code'
        self.cycles = self.root / 'cycles'
        self.pending = self.root / 'pending'
        self.candidates = self.root / 'candidates'
        self.agent_repo = self.root / 'agent-repo'
        self.reports = self.root / 'reports'
        self.scoreboard = self.root / 'scoreboard.json'
        self.report = self.root / 'report.md'


def load_config(work: Work) -> dict:
    if not work.config.is_file():
        raise CycleError(f'missing {work.config}; run init first')
    cfg = {**DEFAULTS, **read_json(work.config)}
    if cfg.get('schema') != CONFIG_SCHEMA:
        raise CycleError(f'{work.config} schema must be {CONFIG_SCHEMA}')
    missing = [key for key in REQUIRED_CONFIG if cfg.get(key) in (None, '', [])]
    if missing:
        raise CycleError(f'config missing required keys: {", ".join(missing)}')
    if cfg['executor'] not in ('docker', 'local'):
        raise CycleError('config executor must be docker or local')
    if cfg['executor'] == 'docker' and not cfg.get('image'):
        raise CycleError('docker executor requires a pinned image id')
    if cfg['min_new_rows'] is None:
        cfg['min_new_rows'] = cfg['page_rows']
    for key in ('page_rows', 'max_active_candidates', 'min_new_rows'):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or cfg[key] < 1:
            raise CycleError(f'config {key} must be a positive integer')
    derived = (Path(cfg['collector_root']) / 'data' / 'derived_reports').resolve()
    if work.root == derived or not work.root.is_relative_to(derived):
        raise CycleError(f'work root must be beneath {derived}')
    return cfg


@contextmanager
def work_lock(work: Work, *, wait_seconds: int = 0):
    work.lock.touch(exist_ok=True)
    with work.lock.open('a') as handle:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CycleError('another auto-learning cycle or registration holds the work lock; '
                                     'if no cycle is running, inspect the last cycles/*/cycle.json')
                time.sleep(5)
        yield


def trusted_env(cfg: dict, scratch: Path) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    return {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': str(scratch),
            'TMPDIR': str(scratch), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPATH': str(cfg['trusted_checkout']), 'GIT_CONFIG_NOSYSTEM': '1',
            'PREDICTION_BOT_COLLECTOR_ROOT': str(cfg['collector_root'])}


def trusted(cfg: dict, scratch: Path, script: str, *args, name: str | None = None) -> subprocess.CompletedProcess:
    checkout = Path(cfg['trusted_checkout'])
    command = [*low_priority_prefix(), cfg['python'], checkout / 'scripts' / script, *args]
    return run_command(command, name=name or script, env=trusted_env(cfg, scratch), cwd=checkout,
                       timeout=cfg['trusted_timeout_seconds'])


def verify_trusted_checkout(cfg: dict) -> dict:
    checkout = Path(cfg['trusted_checkout'])
    if git('status', '--porcelain', '--untracked-files=all', cwd=checkout):
        raise CycleError(f'trusted checkout {checkout} is dirty; it must be a clean reviewed commit')
    return {'path': str(checkout), 'commit': git('rev-parse', 'HEAD', cwd=checkout)}


# --------------------------------------------------------------------------- candidates

def candidate_records(work: Work) -> list[dict]:
    records = []
    if not work.candidates.is_dir():
        return records
    for directory in sorted(work.candidates.iterdir()):
        path = directory / 'candidate.json'
        if path.is_file():
            record = read_json(path)
            events = read_jsonl(directory / 'events.jsonl')
            record['status'] = events[-1]['status'] if events else 'active'
            record['events'] = events
            records.append(record)
    return sorted(records, key=lambda r: r['registered_at'])


def active_candidates(work: Work, cfg: dict) -> list[dict]:
    return [r for r in candidate_records(work) if r['status'] == 'active'][:cfg['max_active_candidates']]


def candidate_event(work: Work, candidate_id: str, status: str, **details) -> None:
    append_jsonl(work.candidates / candidate_id / 'events.jsonl', {'at': iso(), 'status': status, **details})


def ensure_code(work: Work, source_repos: list[Path], commit: str) -> Path:
    """Return an immutable clean shallow checkout of one commit, cached by commit."""
    if not is_sha(commit):
        raise CycleError(f'invalid commit id: {commit!r}')
    destination = work.code / commit
    if destination.exists():
        if git('rev-parse', 'HEAD', cwd=destination) != commit or \
                git('status', '--porcelain', '--untracked-files=all', cwd=destination):
            raise CycleError(f'cached checkout {destination} is not clean at {commit}; inspect and remove it')
        return destination
    work.code.mkdir(parents=True, exist_ok=True)
    errors = []
    for source in source_repos:
        temporary = work.code / f'.tmp-{commit}-{uuid.uuid4().hex[:8]}'
        try:
            git('init', '-q', str(temporary), cwd=work.code)
            git('fetch', '-q', '--depth', '1', str(source), commit, cwd=temporary)
            git('checkout', '-q', '--detach', 'FETCH_HEAD', cwd=temporary)
            if git('rev-parse', 'HEAD', cwd=temporary) != commit:
                raise CycleError(f'fetched checkout does not match {commit}')
            if not (temporary / 'scripts' / 'auto_research.py').is_file():
                raise CycleError(f'commit {commit} lacks scripts/auto_research.py')
            os.rename(temporary, destination)
            return destination
        except CycleError as exc:
            errors.append(f'{source}: {exc}')
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    raise CycleError(f'could not materialize commit {commit}: ' + '; '.join(errors))


# --------------------------------------------------------------------------- snapshot

def is_clock_key(key: str) -> bool:
    return key in KNOWLEDGE_CLOCK_KEYS or key.endswith(CLOCK_SUFFIXES)


def iter_knowledge_clocks(value, path: str):
    """Yield (path, datetime or None) for every clock-named scalar; None means unreadable."""
    if isinstance(value, dict):
        for key, item in value.items():
            child = f'{path}.{key}'
            if isinstance(key, str) and is_clock_key(key) and item is not None and not isinstance(item, (dict, list)):
                yield child, parse_time(item)
            else:
                yield from iter_knowledge_clocks(item, child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_knowledge_clocks(item, path + '[]')


def audit_inputs(path: Path, clean_output: Path | None = None) -> dict:
    """Summarize an outcome-free page; rows whose source clocks postdate the decision are excluded.

    With ``clean_output`` the rows that pass are written there byte-for-byte.
    """
    markets, observed, violations, examples, clean = set(), [], 0, [], []
    lines = [line for line in path.read_bytes().splitlines(keepends=True) if line.strip()]
    rows = [json.loads(line) for line in lines]
    for line, row in zip(lines, rows):
        markets.add(row.get('market_id'))
        decision_time = parse_time(row.get('observed_at'))
        if decision_time is not None:
            observed.append(decision_time)
        scanned = {k: v for k, v in row.items() if k not in DECISION_IDENTITY_KEYS}
        # Fail closed: an unreadable clock counts as a violation, like a later one.
        late = sorted({p for p, t in iter_knowledge_clocks(scanned, 'row')
                       if decision_time is not None and (t is None or t > decision_time)})
        if late or decision_time is None:
            violations += 1
            if len(examples) < 5:
                examples.append({'market_id': row.get('market_id'), 'observed_at': row.get('observed_at'),
                                 'late_clock_paths': late[:4] or ['missing observed_at']})
        else:
            clean.append(line)
    if clean_output is not None:
        with clean_output.open('xb') as stream:
            stream.write(b''.join(clean))
    return {'rows': len(rows), 'clean_rows': len(clean), 'markets': len(markets - {None}),
            'oldest_observed_at': iso(min(observed)) if observed else None,
            'newest_observed_at': iso(max(observed)) if observed else None,
            'source_clock_violation_rows': violations, 'source_clock_examples': examples}


def take_snapshot(cfg: dict, work: Work, cycle: Path) -> dict:
    manifest = read_json(Path(cfg['manifest']), 1024 * 1024)
    indexed_rows = manifest.get('indexed_rows')
    if not isinstance(indexed_rows, int):
        raise CycleError(f'collector manifest {cfg["manifest"]} lacks indexed_rows')
    state = read_json(work.feed_state) if work.feed_state.exists() else None
    if state is not None:
        new_rows = indexed_rows - state['indexed_rows_at_snapshot']
        if new_rows < cfg['min_new_rows']:
            return {'status': 'waiting_for_new_rows', 'new_rows': new_rows, 'min_new_rows': cfg['min_new_rows']}
    args = ['--index', cfg['index'], '--manifest', cfg['manifest'], '--output-root', work.feeds]
    for root in cfg['protected_roots']:
        args += ['--protected-root', root]
    args += ['--max-index-bytes', cfg['max_index_bytes'], '--max-inspected-rows', cfg['page_rows'],
             '--max-raw-row-bytes', cfg['max_raw_row_bytes'], '--max-wall-seconds', cfg['feed_wall_seconds'],
             '--max-memory-bytes', cfg['feed_memory_bytes']]
    result = trusted(cfg, cycle / 'scratch', 'fresh_research_feed.py', *args, name='fresh feed snapshot')
    page = Path(json.loads(result.stdout)['directory'])
    receipt = read_json(page / 'receipt.json')
    numbers = [item['row_number'] for item in receipt['row_provenance']]
    if not numbers:
        raise CycleError('fresh feed returned no rows; inspect the collector index')
    if state is not None and min(numbers) <= state['last_max_row_number']:
        return {'status': 'waiting_for_new_rows', 'reason': 'snapshot would overlap the previous snapshot',
                'page': str(page)}
    snapshot = {'status': 'snapshot', 'page': str(page), 'receipt_sha256': sha256_file(page / 'receipt.json'),
                'inputs': str(page / 'inputs.jsonl'), 'inputs_sha256': receipt['inputs_sha256'],
                'inspected_rows': receipt['inspected_rows'], 'accepted_rows': receipt['accepted_rows'],
                'rejections': receipt['rejections'], 'min_row_number': min(numbers),
                'max_row_number': max(numbers), 'indexed_rows_at_snapshot': indexed_rows,
                'committed_index_bytes': receipt['committed_index_bytes']}
    snapshot['replay_inputs'] = snapshot['inputs']
    snapshot['replay_rows'] = receipt['accepted_rows']
    if receipt['accepted_rows']:
        snapshot['audit'] = audit_inputs(page / 'inputs.jsonl')
        if snapshot['audit']['source_clock_violation_rows']:
            # Fail closed: replay only rows whose recorded source clocks precede the decision.
            clean = cycle / 'replay-inputs.jsonl'
            audit_inputs(page / 'inputs.jsonl', clean)
            snapshot['replay_inputs'] = str(clean)
            snapshot['replay_rows'] = snapshot['audit']['clean_rows']
    return snapshot


# --------------------------------------------------------------------------- replay and compare

def remove_stale_containers(cfg: dict) -> list[str]:
    if cfg['executor'] != 'docker':
        return []
    listed = run_command(['docker', 'ps', '-aq', '--filter', f'label={DOCKER_LABEL}=1'], name='docker ps', timeout=60)
    stale = listed.stdout.split()
    for container in stale:
        run_command(['docker', 'rm', '-f', container], name='docker rm stale', timeout=60)
    return stale


def replay(cfg: dict, cycle: Path, cycle_id: str, label: str, commit: str, code: Path, inputs: Path,
           empty_outcomes: Path) -> dict:
    run_dir = cycle / 'runs' / label
    runtime = run_dir / 'runtime'
    (runtime / STORE_RELATIVE).parent.mkdir(parents=True, exist_ok=False)
    options = ['--idea', f'auto-learning {label} @ {commit[:12]}', '--max-input-bytes', str(cfg['max_input_bytes']),
               '--max-outcome-bytes', '1']
    container = None
    if cfg['executor'] == 'docker':
        container = f'auto-learning-{cycle_id}-{label}'
        command = ['timeout', '--signal=TERM', '--kill-after=10s', str(cfg['replay_timeout_seconds']),
                   'docker', 'run', '--rm', '--name', container, '--label', f'{DOCKER_LABEL}=1',
                   '--label', f'{DOCKER_LABEL}.cycle={cycle_id}', '--log-driver', 'none', '--network', 'none',
                   '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--cpus', '2', '--memory', '2g',
                   '--pids-limit', '256', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--user', f'{os.getuid()}:{os.getgid()}', '--entrypoint', cfg['container_python'],
                   '-e', 'HOME=/tmp', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                   '-e', 'PREDICTION_BOT_COLLECTOR_ROOT=/work/runtime/collector',
                   '--mount', f'type=bind,src={code},dst=/work/code,readonly',
                   '--mount', f'type=bind,src={inputs},dst=/work/inputs.jsonl,readonly',
                   '--mount', f'type=bind,src={empty_outcomes},dst=/work/outcomes.jsonl,readonly',
                   '--mount', f'type=bind,src={runtime},dst=/work/runtime',
                   '--workdir', '/work/code', cfg['image'], 'scripts/auto_research.py', 'run',
                   '--store', f'/work/runtime/{STORE_RELATIVE}', '--inputs', '/work/inputs.jsonl',
                   '--outcomes', '/work/outcomes.jsonl', *options]
        env = None
    else:
        scratch = run_dir / 'scratch'
        scratch.mkdir()
        command = [cfg['python'], code / 'scripts' / 'auto_research.py', 'run', '--store', runtime / STORE_RELATIVE,
                   '--inputs', inputs, '--outcomes', empty_outcomes, *options]
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(scratch), 'TMPDIR': str(scratch),
               'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(code),
               'GIT_CONFIG_NOSYSTEM': '1', 'PREDICTION_BOT_COLLECTOR_ROOT': str(runtime / 'collector')}
    started = time.monotonic()
    try:
        result = subprocess.run([str(p) for p in command], env=env, cwd=code, capture_output=True, text=True,
                                timeout=cfg['replay_timeout_seconds'] + 60)
    except subprocess.TimeoutExpired as exc:
        raise CycleError(f'replay {label} exceeded its time limit') from exc
    finally:
        if container:
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True, text=True, timeout=60)
    (run_dir / 'stderr.log').write_text(result.stderr[-200_000:], encoding='utf-8')
    if result.returncode != 0:
        raise CycleError(f'replay {label} at {commit[:12]} failed (exit {result.returncode}): {tail(result.stderr)}')
    try:  # candidate-controlled output: any malformed receipt is that candidate's failure
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict) or not isinstance(receipt.get('experiment_id'), str):
            raise ValueError('receipt lacks experiment_id')
    except ValueError as exc:
        raise CycleError(f'replay {label} printed an invalid receipt: {exc}; stdout {tail(result.stdout, 300)!r}') from exc
    if receipt.get('git_commit') != commit:
        raise CycleError(f'replay {label} reported commit {receipt.get("git_commit")}, expected {commit}')
    write_json_new(run_dir / 'receipt.json', receipt)
    return {'label': label, 'commit': commit, 'experiment_id': receipt['experiment_id'],
            'store': str(runtime / STORE_RELATIVE), 'seconds': round(time.monotonic() - started, 1)}


def sealed_control(store: Path, experiment_id: str) -> dict:
    rows = read_jsonl(store / 'runs' / experiment_id / 'sealed_control_decisions.jsonl')
    return {json.dumps([row['decision_key'], row['canonical_input_sha256']], sort_keys=True): row for row in rows}


def decision_diff(baseline: dict, candidate: dict) -> dict:
    base, cand = sealed_control(Path(baseline['store']), baseline['experiment_id']), \
        sealed_control(Path(candidate['store']), candidate['experiment_id'])
    if set(base) != set(cand):
        raise CycleError('decision identities differ between champion and candidate stores')
    transitions, examples = Counter(), []
    for key in sorted(base):
        a, b = base[key], cand[key]
        if (a['action'], a.get('side')) != (b['action'], b.get('side')):
            transitions[f"{a['action']}->{b['action']}"] += 1
            if len(examples) < 12:
                examples.append({'market_id': a['market_id'], 'champion': a['action'], 'candidate': b['action'],
                                 'champion_price': a.get('entry_price'), 'candidate_price': b.get('entry_price'),
                                 'probability': b.get('probability'), 'candidate_skip_reason': b.get('skip_reason')})
    actions = lambda rows: dict(Counter(row['action'] for row in rows.values()))
    return {'decisions': len(base), 'changed': sum(transitions.values()), 'transitions': dict(transitions),
            'champion_actions': actions(base), 'candidate_actions': actions(cand), 'examples': examples}


def compare(cfg: dict, cycle: Path, baseline: dict, candidate: dict) -> tuple[Path, dict]:
    result = trusted(cfg, cycle / 'scratch', 'auto_research.py', 'compare', '--store', baseline['store'],
                     '--candidate-store', candidate['store'], baseline['experiment_id'], candidate['experiment_id'],
                     name=f'trusted compare {candidate["label"]}')
    path = cycle / 'comparisons' / f'{candidate["label"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(result.stdout)
    comparison = json.loads(result.stdout)
    if comparison.get('selected_rows_matched') is not True:
        raise CycleError(f'trusted comparison for {candidate["label"]} did not match selected rows')
    return path, comparison


# --------------------------------------------------------------------------- outcomes

def pair_records(work: Work) -> list[dict]:
    records = []
    if not work.pending.is_dir():
        return records
    for directory in sorted(work.pending.iterdir()):
        path = directory / 'pair.json'
        if path.is_file():
            record = read_json(path)
            events = read_jsonl(directory / 'events.jsonl')
            record['events'] = events
            record['status'] = events[-1]['status'] if events else 'open'
            records.append(record)
    return records


def create_pair(work: Work, *, pair_id: str, label: str, candidate_id: str | None, comparison_path: Path,
                comparison: dict, inputs: Path, baseline: dict, candidate: dict, extra: dict) -> dict:
    for existing in pair_records(work):  # one pair per version and snapshot, even after a crash/retry
        if (existing.get('candidate_id'), existing['label'], existing['inputs_sha256']) == \
                (candidate_id, label, comparison['input_sha256']):
            raise CycleError(f'pair for {label} on inputs {comparison["input_sha256"][:12]} already exists '
                             f'({existing["pair_id"]})')
    record = {'schema': 'auto-learning-pending-pair-v1', 'pair_id': pair_id, 'label': label,
              'candidate_id': candidate_id, 'created_at': iso(),
              'comparison_path': str(comparison_path), 'comparison_sha256': sha256_file(comparison_path),
              'inputs_path': str(inputs), 'inputs_sha256': comparison['input_sha256'],
              'matched_decisions': comparison['matched_decisions'],
              'baseline': {k: baseline[k] for k in ('commit', 'experiment_id', 'store')},
              'candidate': {k: candidate[k] for k in ('commit', 'experiment_id', 'store')}, **extra}
    write_json_new(work.pending / pair_id / 'pair.json', record)
    append_jsonl(work.pending / pair_id / 'events.jsonl', {'at': iso(), 'status': 'open', 'bound': 0,
                                                            'total': record['matched_decisions']})
    return record


def filter_resolutions(cfg: dict, market_ids: set[str], destination: Path) -> dict:
    """Copy only relevant complete rows from the live resolution ledger into a pinned file."""
    source = Path(cfg['resolutions'])
    # The resolver rewrites this ledger in place; only use a read that saw a stable file.
    for _attempt in range(3):
        before = source.stat()
        if before.st_size > cfg['max_resolution_bytes']:
            raise CycleError(f'resolution ledger {source} exceeds max_resolution_bytes')
        with source.open('rb') as stream:
            data = stream.read(before.st_size + 1)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino) \
                and len(data) == before.st_size:
            break
        time.sleep(5)
    else:
        raise LedgerUnstable(f'resolution ledger {source} changed during three reads; outcomes deferred')
    data = data[:data.rfind(b'\n') + 1]
    kept, present, malformed = [], set(), 0
    for line in data.splitlines(keepends=True):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        ids = {row.get(key) for key in ('market_id', 'requested_market_id', 'returned_market_id')
               if isinstance(row, dict) and isinstance(row.get(key), str)}
        if ids & market_ids:
            kept.append(line)
            present |= ids & market_ids
    payload = b''.join(kept)
    with destination.open('xb') as stream:
        stream.write(payload)
    return {'source': str(source), 'source_bytes_read': len(data), 'source_prefix_sha256': sha256_bytes(data),
            'path': str(destination), 'sha256': sha256_bytes(payload), 'bytes': len(payload), 'rows': len(kept),
            'malformed_source_rows': malformed, 'markets_present': present}


def market_ids_of(inputs: Path, cache: dict) -> set[str]:
    if inputs not in cache:
        cache[inputs] = {row['market_id'] for row in read_jsonl(inputs) if isinstance(row.get('market_id'), str)}
    return cache[inputs]


def process_outcomes(cfg: dict, work: Work, cycle: Path, cycle_id: str) -> dict:
    open_pairs = [p for p in pair_records(work) if p['status'] == 'open']
    summary = {'open_pairs': len(open_pairs), 'attempted': 0, 'newly_bound': 0, 'finalized': [], 'expired': [],
               'errors': []}
    if not open_pairs:
        return summary
    cache: dict = {}
    usable = []
    for pair in open_pairs:
        try:
            market_ids_of(Path(pair['inputs_path']), cache)
            usable.append(pair)
        except (OSError, ValueError, KeyError) as exc:
            summary['errors'].append({'pair_id': pair['pair_id'], 'error': f'inputs unreadable: {exc}'})
            if age_days(pair['created_at']) >= cfg['pending_max_age_days']:
                last = pair['events'][-1]
                append_jsonl(work.pending / pair['pair_id'] / 'events.jsonl', {
                    **{k: v for k, v in last.items() if k != 'at'}, 'at': iso(), 'status': 'expired',
                    'cycle_id': cycle_id, 'error': f'inputs unreadable: {exc}'[:1000]})
                summary['expired'].append(pair['pair_id'])
    open_pairs = usable
    needed = set().union(set(), *(market_ids_of(Path(p['inputs_path']), cache) for p in open_pairs))
    try:
        pinned = filter_resolutions(cfg, needed, cycle / 'resolutions.filtered.jsonl')
    except LedgerUnstable as exc:
        summary['deferred'] = str(exc)
        return summary
    present = pinned.pop('markets_present')
    summary['resolutions'] = pinned
    for pair in open_pairs:
        directory = work.pending / pair['pair_id']
        last = pair['events'][-1]
        markets = market_ids_of(Path(pair['inputs_path']), cache)
        resolved_markets = len(markets & present)
        pair_age = age_days(pair['created_at'])
        if resolved_markets and resolved_markets != last.get('resolved_markets'):
            summary['attempted'] += 1
            try:
                output = directory / 'bindings' / cycle_id
                output.parent.mkdir(parents=True, exist_ok=True)
                bound = json.loads(trusted(
                    cfg, cycle / 'scratch', 'prepare_research_outcomes.py', '--comparison', pair['comparison_path'],
                    '--inputs', pair['inputs_path'], '--resolutions', pinned['path'],
                    '--resolution-sha256', pinned['sha256'], '--max-input-bytes', cfg['max_input_bytes'],
                    '--max-resolution-bytes', cfg['max_resolution_bytes'], '--output', output,
                    name=f'bind outcomes {pair["pair_id"]}').stdout)
                outcomes = output / 'binding' / 'finalized_replay_outcomes.jsonl'
                feedback = directory / 'feedback' / f'{cycle_id}.json'
                feedback.parent.mkdir(parents=True, exist_ok=True)
                trusted(cfg, cycle / 'scratch', 'research_feedback.py', '--store', pair['baseline']['store'],
                        '--candidate-store', pair['candidate']['store'], '--baseline',
                        pair['baseline']['experiment_id'], '--candidate', pair['candidate']['experiment_id'],
                        '--outcomes', outcomes, '--outcome-sha256', bound['outcomes_sha256'],
                        '--max-outcome-bytes', cfg['max_resolution_bytes'],
                        '--max-decision-bytes', cfg['max_decision_bytes'], '--output', feedback,
                        name=f'score {pair["pair_id"]}')
                counts = bound['binding_counts']
                status = 'final' if counts.get('bound') == pair['matched_decisions'] else 'open'
                if status == 'open' and pair_age >= cfg['pending_max_age_days']:
                    status = 'expired'
                append_jsonl(directory / 'events.jsonl', {
                    'at': iso(), 'status': status, 'cycle_id': cycle_id, 'bound': counts.get('bound', 0),
                    'total': pair['matched_decisions'], 'resolved_markets': resolved_markets,
                    'binding': str(output), 'feedback': str(feedback)})
                summary['newly_bound'] += max(0, counts.get('bound', 0) - last.get('bound', 0))
                if status == 'final':
                    summary['finalized'].append(pair['pair_id'])
                elif status == 'expired':
                    summary['expired'].append(pair['pair_id'])
            except (CycleError, OSError, ValueError, KeyError, TypeError) as exc:
                summary['errors'].append({'pair_id': pair['pair_id'], 'error': f'{type(exc).__name__}: {exc}'})
                if pair_age >= cfg['pending_max_age_days']:
                    append_jsonl(directory / 'events.jsonl', {**{k: v for k, v in last.items() if k != 'at'},
                                                             'at': iso(), 'status': 'expired', 'cycle_id': cycle_id,
                                                             'error': str(exc)[:1000]})
                    summary['expired'].append(pair['pair_id'])
        elif pair_age >= cfg['pending_max_age_days']:
            append_jsonl(directory / 'events.jsonl', {**{k: v for k, v in last.items() if k != 'at'},
                                                     'at': iso(), 'status': 'expired', 'cycle_id': cycle_id})
            summary['expired'].append(pair['pair_id'])
    return summary


# --------------------------------------------------------------------------- scoring

def run_metrics(run: dict) -> dict:
    """Directional counts plus the price-proxy P&L (recorded ask, else recorded market reference)."""
    proxy = run.get('hypothetical_quote_proxy') or {}
    correct, incorrect = run.get('directionally_correct_rows', 0), run.get('directionally_incorrect_rows', 0)
    totals = [proxy.get(f'hypothetical_{kind}_pnl_total_usd') for kind in ('ask_quote', 'reference_price')]
    complete = bool(proxy) and all(proxy.get(f'hypothetical_{kind}_pnl_total_status') in ('complete', 'empty')
                                   for kind in ('ask_quote', 'reference_price'))
    return {'resolved_rows': run.get('resolved_rows', 0), 'resolved_buys': run.get('resolved_buy_rows', 0),
            'correct': correct, 'incorrect': incorrect,
            'quote_pnl_usd': round(sum(t or 0.0 for t in totals), 6) if complete else None,
            'ask_quote_decisions': proxy.get('hypothetical_ask_quote_pnl_decisions', 0),
            'reference_price_decisions': proxy.get('hypothetical_reference_price_pnl_decisions', 0)}


def add_metrics(total: dict, part: dict) -> None:
    for key, value in part.items():
        if key == 'quote_pnl_usd':
            if value is None:
                total['quote_pnl_complete'] = False
            else:
                total['quote_pnl_usd'] = round(total.get('quote_pnl_usd', 0.0) + value, 6)
        else:
            total[key] = total.get(key, 0) + value


def build_scoreboard(work: Work, cfg: dict) -> dict:
    candidates = {c['candidate_id']: c for c in candidate_records(work)}
    groups: dict[str, dict] = {}
    for pair in pair_records(work):
        key = pair.get('candidate_id') or ('champion-replicate' if pair['label'] == 'replicate' else pair['label'])
        group = groups.setdefault(key, {'pairs': 0, 'final_pairs': 0, 'decisions': 0, 'changed_decisions': 0,
                                        'resolved_changed': 0, 'champion': {}, 'candidate': {},
                                        'source_clock_violation_rows': 0, 'transitions': Counter()})
        group['pairs'] += 1
        group['final_pairs'] += pair['status'] in ('final', 'expired')
        group['decisions'] += pair['matched_decisions']
        group['changed_decisions'] += pair.get('diff', {}).get('changed', 0)
        group['source_clock_violation_rows'] += pair.get('source_clock_violation_rows', 0)
        group['transitions'].update(pair.get('diff', {}).get('transitions', {}))
        feedback_path = next((e['feedback'] for e in reversed(pair['events']) if e.get('feedback')), None)
        if feedback_path:
            feedback = read_json(Path(feedback_path))
            champion, candidate = feedback['runs']
            add_metrics(group['champion'], run_metrics(champion))
            add_metrics(group['candidate'], run_metrics(candidate))
            group['resolved_changed'] += sum(item['count'] for item in feedback['changed_resolved_decisions'])
    for candidate_id in candidates:  # candidates with no pair yet (new or broken) still appear
        groups.setdefault(candidate_id, {'pairs': 0, 'final_pairs': 0, 'decisions': 0, 'changed_decisions': 0,
                                         'resolved_changed': 0, 'champion': {}, 'candidate': {},
                                         'source_clock_violation_rows': 0, 'transitions': Counter()})
    board = {'schema': 'auto-learning-scoreboard-v1', 'generated_at': iso(),
             'champion': read_json(work.champion), 'groups': {}}
    for key, group in groups.items():
        group['transitions'] = dict(group['transitions'])
        a, b = group['champion'], group['candidate']
        if a and b:
            net = lambda m: m.get('correct', 0) - m.get('incorrect', 0)
            group['delta'] = {'net_correct': net(b) - net(a)}
            if a.get('quote_pnl_complete', True) and b.get('quote_pnl_complete', True):
                group['delta']['quote_pnl_usd'] = round(b.get('quote_pnl_usd', 0.0) - a.get('quote_pnl_usd', 0.0), 6)
        if key in candidates:
            record = candidates[key]
            last = record['events'][-1] if record['events'] else {}
            group.update({'commit': record['commit'], 'hypothesis': record['hypothesis'], 'status': record['status'],
                          'registered_at': record['registered_at'], 'verdict': last.get('verdict'),
                          'verdict_reason': last.get('reason'),
                          'needs_operator_review': bool(last.get('needs_operator_review'))})
        board['groups'][key] = group
    return board


def evaluate_candidates(work: Work, cfg: dict, board: dict) -> list[dict]:
    """Conclude active candidates once evidence or age limits are reached. Never promotes."""
    concluded = []
    for record in candidate_records(work):
        if record['status'] != 'active':
            continue
        group = board['groups'].get(record['candidate_id'], {})
        candidate_age = age_days(record['registered_at'])
        failures = sum(1 for e in record['events'][-cfg['max_candidate_failures']:] if e.get('status') == 'active'
                       and e.get('replay_failure'))
        verdict = reason = None
        delta = group.get('delta', {})
        better = delta.get('quote_pnl_usd', 0) > 0 and delta.get('net_correct', 0) >= 0
        if failures >= cfg['max_candidate_failures']:
            verdict, reason = 'broken', f'{failures} consecutive replay failures'
        elif group.get('pairs', 0) >= 2 and group.get('changed_decisions', 0) == 0:
            verdict, reason = 'no_effect', 'no decision changed across two snapshots'
        elif group.get('resolved_changed', 0) >= cfg['conclude_min_resolved_changes']:
            verdict, reason = ('better' if better else 'not_better'), 'reached resolved changed-decision target'
        elif candidate_age >= cfg['candidate_max_age_days']:
            verdict = 'insufficient_evidence' if group.get('resolved_changed', 0) < 5 else (
                'better' if better else 'not_better')
            reason = f'reached {cfg["candidate_max_age_days"]}-day testing window'
        if verdict:
            details = {'verdict': verdict, 'reason': reason, 'delta': delta,
                       'resolved_changed': group.get('resolved_changed', 0),
                       'needs_operator_review': verdict == 'better'}
            candidate_event(work, record['candidate_id'], 'concluded', **details)
            concluded.append({'candidate_id': record['candidate_id'], **details})
    return concluded


def fmt_money(value) -> str:
    return 'n/a' if value is None else f'{value:+.2f}'


def render_report(work: Work, cfg: dict, board: dict, cycle_summary: dict) -> str:
    champion = board['champion']
    lines = ['# Auto-learning report', '', f'- Generated: {board["generated_at"]}',
             f'- Champion: `{champion["commit"]}` ({champion.get("note", "")})',
             f'- Last cycle: `{cycle_summary.get("cycle_id")}` — {cycle_summary.get("status")}',
             f'- Candidate slots: {len(active_candidates(work, cfg))}/{cfg["max_active_candidates"]} active',
             '', 'Scores are diagnostic: outcomes are bound after decisions are sealed. P&L is a price proxy: $1 '
             'per BUY at the recorded ask quote, or the recorded market price when no ask was recorded; binary '
             'payout, zero fees, not fills. Source availability is audited per snapshot but not proven. '
             'Nothing here promotes automatically.', '']
    snapshot = cycle_summary.get('snapshot') or {}
    if snapshot.get('status') == 'snapshot':
        audit = snapshot.get('audit', {})
        lines += ['## Latest snapshot', '', f'- Rows: {snapshot.get("accepted_rows")} accepted / '
                  f'{snapshot.get("inspected_rows")} inspected, {audit.get("markets")} markets',
                  f'- Observed: {audit.get("oldest_observed_at")} → {audit.get("newest_observed_at")}',
                  f'- Source-clock violations: {audit.get("source_clock_violation_rows")}', '']
    lines += ['## Candidates', '']
    rows = sorted(board['groups'].items(), key=lambda kv: kv[1].get('registered_at') or '')
    if not rows:
        lines.append('_No scored pairs yet._')
    for key, group in rows:
        a, b, delta = group.get('champion', {}), group.get('candidate', {}), group.get('delta', {})
        title = key if key != 'champion-replicate' else 'champion (self-replicate)'
        lines += [f'### {title}', '']
        if group.get('hypothesis'):
            lines += [f'- Hypothesis: {group["hypothesis"]}', f'- Commit: `{group["commit"]}`',
                      f'- Status: {group.get("status")}' + (f' — verdict **{group["verdict"]}** ({group.get("verdict_reason")})'
                                                            if group.get('verdict') else '')
                      + (' — **needs operator review for promotion**' if group.get('needs_operator_review') else '')]
        lines += [f'- Pairs: {group["pairs"]} ({group["final_pairs"]} final), decisions {group["decisions"]}, '
                  f'changed {group["changed_decisions"]} {group.get("transitions") or ""}',
                  f'- Resolved changed decisions: {group["resolved_changed"]}']
        if a:
            lines.append(f'- Champion on these rows: {a.get("resolved_buys", 0)} resolved buys, '
                         f'{a.get("correct", 0)} correct / {a.get("incorrect", 0)} incorrect, '
                         f'proxy P&L {fmt_money(a.get("quote_pnl_usd"))} ({a.get("ask_quote_decisions", 0)} ask, '
                         f'{a.get("reference_price_decisions", 0)} reference-price)')
        if b and key != 'champion-replicate':
            lines.append(f'- Candidate: {b.get("resolved_buys", 0)} resolved buys, {b.get("correct", 0)} correct / '
                         f'{b.get("incorrect", 0)} incorrect, proxy P&L {fmt_money(b.get("quote_pnl_usd"))}')
            lines.append(f'- Delta (candidate − champion): net correct {delta.get("net_correct", "n/a")}, '
                         f'proxy P&L {fmt_money(delta.get("quote_pnl_usd"))}')
        lines.append('')
    recent = cycle_summary.get('diffs') or {}
    if recent:
        lines += ['## Latest changed-decision examples', '']
        for label, diff in recent.items():
            for example in diff.get('examples', [])[:6]:
                lines.append(f'- {label}: `{example["market_id"]}` {example["champion"]} → {example["candidate"]} '
                             f'(p={example.get("probability")}, price={example.get("candidate_price") or example.get("champion_price")}, '
                             f'skip={example.get("candidate_skip_reason")})')
        lines.append('')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- commands

def cmd_run(args) -> int:
    work = Work(args.work)
    cfg = load_config(work)
    cycle_id = f'{utcnow():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}'
    cycle = work.cycles / cycle_id
    summary: dict = {'cycle_id': cycle_id, 'started_at': iso(), 'status': 'running'}
    with work_lock(work):
        cycle.mkdir(parents=True)
        journal = cycle / 'cycle.json'
        write_json_atomic(journal, summary)
        try:
            summary['trusted'] = verify_trusted_checkout(cfg)
            free = shutil.disk_usage(work.root).free
            if free < cfg['min_free_bytes']:
                raise CycleError(f'free space {free} bytes below min_free_bytes {cfg["min_free_bytes"]}; '
                                 'no cycle work started')
            summary['stale_containers_removed'] = remove_stale_containers(cfg)
            summary['outcomes'] = process_outcomes(cfg, work, cycle, cycle_id)
            snapshot = {'status': 'skipped'} if args.skip_snapshot else take_snapshot(cfg, work, cycle)
            summary['snapshot'] = snapshot
            if snapshot['status'] == 'snapshot':
                # Consume the snapshot before replays: a failed or interrupted cycle never re-replays
                # the same rows (no duplicate pairs); it loses that snapshot instead, loudly.
                write_json_atomic(work.feed_state, {
                    'updated_at': iso(), 'cycle_id': cycle_id, 'page': snapshot['page'],
                    'indexed_rows_at_snapshot': snapshot['indexed_rows_at_snapshot'],
                    'last_max_row_number': snapshot['max_row_number']})
            summary['diffs'], summary['candidate_failures'] = {}, []
            if snapshot['status'] == 'snapshot' and snapshot.get('replay_rows', 0) > 0:
                inputs = Path(snapshot['replay_inputs'])
                empty = cycle / 'empty-outcomes.jsonl'
                empty.write_bytes(b'')
                sources = [work.agent_repo, Path(cfg['trusted_checkout'])]
                champion_commit = read_json(work.champion)['commit']
                baseline = replay(cfg, cycle, cycle_id, 'champion', champion_commit,
                                  ensure_code(work, sources, champion_commit), inputs, empty)
                versions = [(c['candidate_id'], c['candidate_id'], c['commit']) for c in active_candidates(work, cfg)]
                if not versions:
                    versions = [('replicate', None, champion_commit)]
                runs = {'champion': baseline}
                for label, candidate_id, commit in versions:
                    try:
                        candidate = replay(cfg, cycle, cycle_id, label, commit, ensure_code(work, sources, commit),
                                           inputs, empty)
                        comparison_path, comparison = compare(cfg, cycle, baseline, candidate)
                        if comparison['matched_decisions'] != snapshot['replay_rows']:
                            raise CycleError(f'{label} compared {comparison["matched_decisions"]} of '
                                             f'{snapshot["replay_rows"]} replay rows')
                        diff = decision_diff(baseline, candidate)
                        summary['diffs'][label] = diff
                        runs[label] = candidate
                        create_pair(work, pair_id=f'{cycle_id}--{label}', label=label, candidate_id=candidate_id,
                                    comparison_path=comparison_path, comparison=comparison, inputs=inputs,
                                    baseline=baseline, candidate=candidate,
                                    extra={'cycle_id': cycle_id, 'diff': {k: diff[k] for k in ('changed', 'transitions')},
                                           'snapshot_page': snapshot['page'],
                                           'newest_observed_at': snapshot['audit']['newest_observed_at'],
                                           'source_clock_violation_rows': snapshot['audit']['source_clock_violation_rows']})
                        if candidate_id:
                            candidate_event(work, candidate_id, 'active', cycle_id=cycle_id, changed=diff['changed'])
                    except (CycleError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                        summary['candidate_failures'].append({'label': label, 'error': f'{type(exc).__name__}: {exc}'})
                        if candidate_id:
                            candidate_event(work, candidate_id, 'active', cycle_id=cycle_id, replay_failure=str(exc)[:2000])
                summary['runs'] = runs
            board = build_scoreboard(work, cfg)
            summary['concluded'] = evaluate_candidates(work, cfg, board)
            if summary['concluded']:
                board = build_scoreboard(work, cfg)
            write_json_atomic(work.scoreboard, board)
            summary['status'] = 'complete_with_warnings' if (summary['candidate_failures'] or summary['outcomes']['errors']
                                                              or summary['outcomes'].get('deferred')) else 'complete'
            report = render_report(work, cfg, board, summary)
            write_text_atomic(work.report, report)
            write_text_atomic(work.reports / f'{cycle_id}.md', report)
        except Exception as exc:  # journal every failure before reporting it
            summary.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished_at=iso())
            write_json_atomic(journal, summary)
            print(f'[CRON_FAILURE]\nAuto-learning cycle {cycle_id} failed: {exc}\nJournal: {journal}')
            return 1
        summary['finished_at'] = iso()
        write_json_atomic(journal, summary)
    message = cycle_message(summary, work)
    if args.verbose or cfg['notify'] == 'always' or notable(summary):
        print(message)
    return 0


def notable(summary: dict) -> bool:
    return bool(summary.get('concluded') or summary.get('candidate_failures') or summary['outcomes'].get('errors')
                or summary['outcomes'].get('deferred')
                or (summary.get('snapshot', {}).get('audit') or {}).get('source_clock_violation_rows'))


def cycle_message(summary: dict, work: Work) -> str:
    snapshot = summary.get('snapshot', {})
    lines = [f'Auto-learning cycle {summary["cycle_id"]} — {summary["status"]}']
    if snapshot.get('status') == 'snapshot':
        audit = snapshot.get('audit') or {}
        lines.append(f'• Snapshot: {snapshot["accepted_rows"]} rows, {audit.get("markets")} markets, '
                     f'source-clock violations {audit.get("source_clock_violation_rows")} (excluded)')
    else:
        lines.append(f'• Snapshot: {snapshot.get("status")}')
    for label, diff in (summary.get('diffs') or {}).items():
        lines.append(f'• {label}: {diff["changed"]}/{diff["decisions"]} decisions changed {diff["transitions"] or ""}')
    outcomes = summary.get('outcomes', {})
    lines.append(f'• Outcomes: {outcomes.get("newly_bound", 0)} newly bound decisions, '
                 f'{len(outcomes.get("finalized", []))} pairs final, {outcomes.get("open_pairs", 0)} were open')
    for item in summary.get('concluded', []):
        lines.append(f'• Concluded {item["candidate_id"]}: {item["verdict"]} ({item["reason"]}); delta {item["delta"]}')
    for item in summary.get('candidate_failures', []):
        lines.append(f'• Candidate failure {item["label"]}: {item["error"][:300]}')
    for item in outcomes.get('errors', []):
        lines.append(f'• Outcome error {item["pair_id"]}: {item["error"][:300]}')
    if outcomes.get('deferred'):
        lines.append(f'• Outcomes deferred: {outcomes["deferred"]}')
    lines.append(f'Report: {work.report}')
    return '\n'.join(lines)


def cmd_init(args) -> int:
    work = Work(args.work)
    if work.config.exists():
        raise CycleError(f'{work.config} already exists')
    trusted_checkout = Path(args.trusted).resolve()
    champion = args.champion or git('rev-parse', 'HEAD', cwd=trusted_checkout)
    cfg = {'schema': CONFIG_SCHEMA, 'collector_root': str(Path(args.collector_root).resolve()),
           'trusted_checkout': str(trusted_checkout), 'python': args.python, 'executor': args.executor,
           'image': args.image, 'index': str(Path(args.index).resolve()), 'manifest': str(Path(args.manifest).resolve()),
           'protected_roots': [str(Path(p).resolve()) for p in args.protected_root],
           'resolutions': str(Path(args.resolutions).resolve()), 'page_rows': args.page_rows,
           'max_active_candidates': args.max_active}
    derived = Path(cfg['collector_root']) / 'data' / 'derived_reports'
    if work.root == derived or not work.root.is_relative_to(derived):
        raise CycleError(f'work root must be beneath {derived}')
    work.root.mkdir(parents=True, exist_ok=True)
    write_json_new(work.config, cfg)
    load_config(work)  # validate values before creating anything else
    for directory in (work.feeds, work.code, work.cycles, work.pending, work.candidates, work.reports):
        directory.mkdir(exist_ok=True)
    if not work.agent_repo.exists():
        git('clone', '-q', '--no-local', str(trusted_checkout), str(work.agent_repo), cwd=work.root)
    git('branch', '-f', 'champion', champion, cwd=work.agent_repo)
    write_json_new(work.champion, {'commit': git('rev-parse', champion, cwd=work.agent_repo), 'set_at': iso(),
                                   'note': args.note})
    append_jsonl(work.champion_history, read_json(work.champion))
    print(json.dumps({'status': 'initialized', 'work': str(work.root), 'champion': read_json(work.champion)}))
    return 0


def cmd_register(args) -> int:
    work = Work(args.work)
    cfg = load_config(work)
    if not args.hypothesis.strip():
        raise CycleError('a hypothesis is required')
    with work_lock(work, wait_seconds=args.wait_seconds):
        commit = git('rev-parse', '--verify', f'{args.commit}^{{commit}}', cwd=work.agent_repo)
        champion = read_json(work.champion)['commit']
        if commit == champion:
            raise CycleError('candidate commit equals the champion commit')
        records = candidate_records(work)
        if any(r['commit'] == commit for r in records):
            raise CycleError(f'commit {commit} is already registered')
        active = [r for r in records if r['status'] == 'active']
        if len(active) >= cfg['max_active_candidates']:
            raise CycleError(f'no free candidate slot ({len(active)}/{cfg["max_active_candidates"]} active)')
        git('cat-file', '-e', f'{commit}:scripts/auto_research.py', cwd=work.agent_repo)
        changed = git('diff', '--name-only', champion, commit, cwd=work.agent_repo).splitlines()
        candidate_id = f'cand-{utcnow():%Y%m%d}-{uuid.uuid4().hex[:6]}'
        record = {'schema': 'auto-learning-candidate-v1', 'candidate_id': candidate_id, 'commit': commit,
                  'base_champion': champion, 'branch': args.branch, 'hypothesis': args.hypothesis.strip(),
                  'registered_at': iso(), 'changed_paths': changed, 'registered_by': args.registered_by}
        write_json_new(work.candidates / candidate_id / 'candidate.json', record)
        candidate_event(work, candidate_id, 'active', note='registered')
    print(json.dumps({'status': 'registered', 'candidate_id': candidate_id, 'commit': commit,
                      'changed_paths': changed}))
    return 0


def cmd_adopt_pair(args) -> int:
    """Queue an existing sealed comparison (empty original outcomes) for later outcome binding."""
    work = Work(args.work)
    load_config(work)
    with work_lock(work, wait_seconds=args.wait_seconds):
        comparison_path = Path(args.comparison).resolve(strict=True)
        comparison = read_json(comparison_path)
        if comparison.get('selected_rows_matched') is not True or comparison.get('outcome_sha256') != EMPTY_SHA256:
            raise CycleError('comparison must be a matched sealed comparison with empty original outcomes')
        inputs = Path(args.inputs).resolve(strict=True)
        if sha256_file(inputs) != comparison.get('input_sha256'):
            raise CycleError('inputs do not match the comparison input hash')
        runs = comparison['runs']
        stores = [Path(args.baseline_store).resolve(strict=True), Path(args.candidate_store).resolve(strict=True)]
        sides = [{'commit': run['git_commit'], 'experiment_id': run['experiment_id'], 'store': str(store)}
                 for run, store in zip(runs, stores)]
        pair_id = f'adopted-{utcnow():%Y%m%dT%H%M%SZ}-{args.label}'
        record = create_pair(work, pair_id=pair_id, label=args.label, candidate_id=args.candidate_id,
                             comparison_path=comparison_path, comparison=comparison, inputs=inputs,
                             baseline=sides[0], candidate=sides[1],
                             extra={'adopted': True, 'note': args.note, 'diff': decision_diff(
                                 {'store': sides[0]['store'], 'experiment_id': sides[0]['experiment_id']},
                                 {'store': sides[1]['store'], 'experiment_id': sides[1]['experiment_id']}),
                                 'source_clock_violation_rows': audit_inputs(inputs)['source_clock_violation_rows']})
    print(json.dumps({'status': 'adopted', 'pair_id': record['pair_id']}))
    return 0


def cmd_set_champion(args) -> int:
    work = Work(args.work)
    load_config(work)
    with work_lock(work, wait_seconds=args.wait_seconds):
        commit = git('rev-parse', '--verify', f'{args.commit}^{{commit}}', cwd=work.agent_repo)
        record = {'commit': commit, 'set_at': iso(), 'note': args.note}
        write_json_atomic(work.champion, record)
        append_jsonl(work.champion_history, record)
        git('update-ref', 'refs/heads/champion', commit, cwd=work.agent_repo)
        # Scores are only comparable against one champion: retire candidates measured against the old one.
        for candidate in candidate_records(work):
            if candidate['status'] == 'active':
                candidate_event(work, candidate['candidate_id'], 'concluded', verdict='superseded',
                                reason=f'champion changed to {commit[:12]}; re-register against the new champion',
                                delta={}, resolved_changed=0, needs_operator_review=False)
    print(json.dumps({'status': 'champion_set', **record}))
    return 0


def cmd_status(args) -> int:
    work = Work(args.work)
    cfg = load_config(work)
    board = read_json(work.scoreboard) if work.scoreboard.exists() else None
    print(json.dumps({'champion': read_json(work.champion),
                      'active_candidates': [{k: c[k] for k in ('candidate_id', 'commit', 'hypothesis', 'registered_at')}
                                            for c in active_candidates(work, cfg)],
                      'free_slots': cfg['max_active_candidates'] - len(active_candidates(work, cfg)),
                      'report': str(work.report), 'scoreboard_generated_at': board and board['generated_at']},
                     indent=2))
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f'[CRON_FAILURE] auto_learning_cycle: {message}')
        raise SystemExit(2)


def main() -> int:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--work', required=True)
    init.add_argument('--collector-root', required=True)
    init.add_argument('--trusted', required=True, help='clean reviewed checkout used for trusted steps')
    init.add_argument('--champion', help='initial champion commit (default: trusted HEAD)')
    init.add_argument('--python', required=True, help='host interpreter with project dependencies')
    init.add_argument('--executor', choices=('docker', 'local'), default='docker')
    init.add_argument('--image', help='pinned docker image id for candidate replays')
    init.add_argument('--index', required=True)
    init.add_argument('--manifest', required=True)
    init.add_argument('--protected-root', action='append', required=True)
    init.add_argument('--resolutions', required=True, help='finalized resolution ledger (read only)')
    init.add_argument('--page-rows', type=int, default=400)
    init.add_argument('--max-active', type=int, default=3)
    init.add_argument('--note', default='initial champion')
    run = sub.add_parser('run')
    run.add_argument('--work', required=True)
    run.add_argument('--skip-snapshot', action='store_true', help='only bind outcomes and rescore')
    run.add_argument('--verbose', action='store_true', help='always print the cycle summary')
    register = sub.add_parser('register')
    register.add_argument('--work', required=True)
    register.add_argument('--commit', required=True, help='commit in <work>/agent-repo')
    register.add_argument('--hypothesis', required=True)
    register.add_argument('--branch')
    register.add_argument('--registered-by', default='research-agent')
    register.add_argument('--wait-seconds', type=int, default=1800)
    adopt = sub.add_parser('adopt-pair')
    adopt.add_argument('--work', required=True)
    adopt.add_argument('--comparison', required=True)
    adopt.add_argument('--inputs', required=True)
    adopt.add_argument('--baseline-store', required=True)
    adopt.add_argument('--candidate-store', required=True)
    adopt.add_argument('--label', required=True)
    adopt.add_argument('--candidate-id')
    adopt.add_argument('--note', default='')
    adopt.add_argument('--wait-seconds', type=int, default=60)
    champion = sub.add_parser('set-champion', help='operator only: change the baseline commit')
    champion.add_argument('--work', required=True)
    champion.add_argument('--commit', required=True)
    champion.add_argument('--note', required=True)
    champion.add_argument('--wait-seconds', type=int, default=60)
    status = sub.add_parser('status')
    status.add_argument('--work', required=True)
    args = parser.parse_args()
    handlers = {'init': cmd_init, 'run': cmd_run, 'register': cmd_register, 'adopt-pair': cmd_adopt_pair,
                'set-champion': cmd_set_champion, 'status': cmd_status}
    try:
        return handlers[args.command](args)
    except (CycleError, OSError, ValueError, KeyError) as exc:
        print(f'[CRON_FAILURE]\nauto_learning_cycle {args.command}: {type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
