#!/usr/bin/env python3
"""Agent-proposed city-local weather sources: gate, go live, watch (paper/observer collection only).

A proposal is a branch in the source agent's clone that adds or patches city-local forecast sources
(registry entries and page parsers). `onboard` checks the branch's scope, registry fields, the per-city
source cap, tests and a live fetch probe, then asks an independent reviewer. On PASS it fast-forwards the
collector's checkout (beta), pushes, restarts the collector and verifies it; a collector that does not stay
up is rolled back. New sources are watched for `watch_days`; `watch` writes the morning/night integrity
report the source agent reads. See docs/source-onboarding.md.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = 'docs/weather/city_registry_starter.json'
CATALOG = 'bot/weather/source_catalog.yaml'
SOURCE_ID = re.compile(r'^src_local_[a-z0-9_]{3,60}$')
# The engine records one NWS station observation per city besides the catalog and local sources.
STATION_OBSERVATIONS_PER_CITY = 1
PROTECTED_SOURCE_FIELDS = ('verified', 'status', 'trust_score', 'metrics', 'last_reviewed', 'role', 'type', 'city_id')
SOURCES_PATH = ('decision_artifact', 'source_context', 'data', 'weather_source_snapshot', 'sources')
CITY_PATH = ('decision_artifact', 'source_context', 'data', 'weather_source_snapshot', 'station_resolution', 'city_id')
TRADED_WINDOW_HOURS = 48
IDENTITY = ['-c', 'user.name=Hermes Source Onboarding', '-c', 'user.email=hermes-agent@localhost.invalid']
DEFAULTS = {
    'schema': 1, 'remote': 'origin', 'branch': 'beta', 'max_sources_per_city': 10, 'watch_days': 14,
    'allowed_paths': [REGISTRY, 'bot/feeds/weather_local*.py', 'tests/test_weather_local*.py'],
    'test_modules': ['tests.test_weather_local_other', 'tests.test_weather_local_registry',
                     'tests.test_weather_local_fox', 'tests.test_weather_local_tegna', 'tests.test_weather_broad_collection'],
    'probe': True, 'restart_settle_seconds': 90, 'review_timeout_seconds': 1800, 'test_timeout_seconds': 900,
    'review_command': ['claude', '-p', '{prompt}', '--allowedTools',
                       'Bash(git diff:*) Bash(git log:*) Bash(git show:*) Read Grep Glob', '--output-format', 'text'],
}


class Rejected(Exception):
    """The proposal cannot go live as is; recorded for the agent, not an infrastructure failure."""


class Deferred(Exception):
    """A precondition outside the proposal is not met (busy checkout, network, reviewer); retried next run."""


class NoVerdict(Deferred):
    """The reviewer answered without a verdict line; retried, then rejected after MAX_NO_VERDICT attempts."""


MAX_NO_VERDICT = 3


class OnboardingError(Exception):
    """Infrastructure failure needing the operator (e.g. a rollback that did not complete)."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec='seconds')


def parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: float = 120,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def git(*args: str, cwd: Path, timeout: float = 120) -> str:
    try:
        return run(['git', *IDENTITY, *args], cwd=cwd, timeout=timeout).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise Deferred(f'git {args[0]} failed in {cwd}: {(exc.stderr or exc.stdout).strip()[-300:]}') from exc
    except subprocess.TimeoutExpired as exc:
        raise Deferred(f'git {args[0]} timed out after {timeout}s in {cwd}') from exc


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


class Work:
    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / 'config.json'
        self.proposals = root / 'proposals'
        self.watches_path = root / 'watches.json'
        self.reports = root / 'reports'
        self.storage_history = root / 'storage-history.jsonl'
        self.tmp = root / 'tmp'
        self.agent_repo = root / 'agent-repo'

    def config(self) -> dict:
        if not self.config_path.exists():
            raise SystemExit(f'not initialized: {self.config_path} missing (run init)')
        return {**DEFAULTS, **json.loads(self.config_path.read_text())}

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / '.lock', 'w') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Deferred('another onboarding run holds the lock') from None
            yield

    @contextlib.contextmanager
    def updating_watches(self):
        """Short read-modify-write of watches.json, shared by onboard and watch."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / '.watches.lock', 'w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            watches = self.watches()
            yield watches
            write_json(self.watches_path, watches)

    def proposal_records(self) -> list[dict]:
        return sorted((json.loads(p.read_text()) for p in self.proposals.glob('*/proposal.json')),
                      key=lambda r: r['proposed_at'])

    def save_proposal(self, record: dict) -> None:
        write_json(self.proposals / record['id'] / 'proposal.json', record)

    def watches(self) -> list[dict]:
        return read_json(self.watches_path, [])

    def onboarded_ids(self) -> set[str]:
        return {w['source_id'] for w in self.watches()}


# ----- mechanical checks -------------------------------------------------------------------------

def changed_paths(tree: Path, base: str, merge: str) -> list[str]:
    """Every path the merge touches (renames split into delete + add); only regular files may be added or modified."""
    raw = git('diff', '--no-renames', '--raw', '-z', base, merge, cwd=tree).split('\0')
    paths, problems = [], []
    for meta, path in zip(raw[0::2], raw[1::2]):
        if not meta:
            continue
        _, new_mode, _, _, status = meta.lstrip(':').split(' ')
        paths.append(path)
        if status == 'D':
            problems.append(f'{path} deleted')
        elif new_mode not in ('100644', '100755'):
            problems.append(f'{path} has mode {new_mode} (symlinks and submodules are not allowed)')
    if problems:
        raise Rejected('; '.join(problems))
    return paths


def check_scope(changed: list[str], allowed: list[str]) -> None:
    outside = [path for path in changed if not any(fnmatch.fnmatchcase(path, glob) for glob in allowed)]
    if outside:
        raise Rejected(f'changes outside the allowed source paths: {", ".join(sorted(outside))}')
    if not changed:
        raise Rejected('the branch changes nothing')


LOCAL_MAPPINGS = ('OTHER_LOCAL_ADAPTERS', 'OTHER_LOCAL_URLS', 'OTHER_LOCAL_UNAVAILABLE_REASONS')
MAPPINGS_MODULE = 'bot/feeds/weather_local_other.py'


def local_mappings(text: str) -> dict[str, dict[str, str]]:
    """Literal source_id -> value mappings that steer existing sources (compared, never executed)."""
    found = {}
    for node in ast.parse(text).body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else getattr(node, 'target', None)
        if isinstance(target, ast.Name) and target.id in LOCAL_MAPPINGS and isinstance(node.value, ast.Dict):
            found[target.id] = {ast.literal_eval(k): ast.unparse(v) for k, v in zip(node.value.keys, node.value.values)}
    return found


def check_mappings(old_text: str, new_text: str, allowed_ids: set[str]) -> None:
    old, new = local_mappings(old_text), local_mappings(new_text)
    uses = Counter(node.id for node in ast.walk(ast.parse(new_text)) if isinstance(node, ast.Name) and node.id in LOCAL_MAPPINGS)
    if set(old) != set(new) or any(uses[name] != 1 for name in new):
        raise Rejected(f'{MAPPINGS_MODULE} must define each of {", ".join(LOCAL_MAPPINGS)} once, as a literal dict '
                       'that is not modified elsewhere in the module')
    for name in LOCAL_MAPPINGS:
        changed = {k for k in old[name].keys() | new[name].keys() if old[name].get(k) != new[name].get(k)}
        if changed - allowed_ids:
            raise Rejected(f'{name} may only change for added or onboarded sources: {sorted(changed - allowed_ids)}')
        if name == 'OTHER_LOCAL_URLS':
            for source_id in changed & new[name].keys():
                if not str(ast.literal_eval(new[name][source_id])).startswith('https://'):
                    raise Rejected(f'{name}[{source_id!r}] must be an https URL')


def check_registry(old: dict, new: dict, onboarded: set[str],
                   declared: set[str] | frozenset[str] = frozenset()) -> tuple[set[str], set[str]]:
    """Return (added, patched) local source ids; only additions and patches of onboarded sources pass.

    A parser-only fix leaves the registry unchanged; it names the onboarded sources it fixes (`declared`)."""
    if old.get('cities') != new.get('cities') or old.get('version') != new.get('version'):
        raise Rejected('the registry cities and version may not change')
    old_sources = {s['source_id']: s for s in old['sources']}
    new_sources = {s.get('source_id'): s for s in new.get('sources', [])}
    if len(new_sources) != len(new.get('sources', [])):
        raise Rejected('duplicate source_id in the registry')
    removed = old_sources.keys() - new_sources.keys()
    if removed:
        raise Rejected(f'sources may not be removed: {sorted(removed)}')
    patched = {i for i in old_sources.keys() & new_sources.keys() if old_sources[i] != new_sources[i]}
    foreign = patched - onboarded
    if foreign:
        raise Rejected(f'only sources onboarded by this pipeline may be patched: {sorted(foreign)}')
    added = set(new_sources.keys() - old_sources.keys())
    cities = {c['city_id'] for c in new['cities']}
    for source_id in sorted(added | patched):
        source = new_sources[source_id]
        problems = []
        if not SOURCE_ID.match(str(source_id)):
            problems.append('source_id must match src_local_<name>')
        if source.get('city_id') not in cities:
            problems.append('unknown city_id')
        if source.get('role') != 'forecast' or source.get('type') != 'local_met':
            problems.append('role must be forecast and type local_met')
        if not str(source.get('url', '')).startswith('https://'):
            problems.append('url must be https')
        if not isinstance(source.get('name'), str) or not source['name'].strip():
            problems.append('name is required')
        if source_id in added:
            if source.get('status') != 'watch_only' or source.get('trust_score') != 0 or source.get('verified') is not False:
                problems.append('a new source starts watch_only, trust_score 0, verified false')
        elif any(source.get(k) != old_sources[source_id].get(k) for k in PROTECTED_SOURCE_FIELDS):
            problems.append(f'a patch may not change {", ".join(PROTECTED_SOURCE_FIELDS)}')
        if problems:
            raise Rejected(f'{source_id}: ' + '; '.join(problems))
    if set(declared) - onboarded:
        raise Rejected(f'--source names sources this pipeline did not onboard: {sorted(set(declared) - onboarded)}')
    patched |= set(declared)
    if not added and not patched:
        raise Rejected('the registry is unchanged: name the onboarded source(s) a parser fix is for with --source')
    return added, patched


def city_source_counts(registry: dict, catalog: list[dict]) -> dict[str, int]:
    """Sources recorded per city: every catalog entry, the city's local forecasts and its station observation."""
    local = Counter(s['city_id'] for s in registry['sources'] if s['role'] == 'forecast' and s['type'] != 'official')
    return {c['city_id']: len(catalog) + local[c['city_id']] + STATION_OBSERVATIONS_PER_CITY for c in registry['cities']}


def check_cap(old: dict, new: dict, catalog: list[dict], cap: int) -> dict[str, int]:
    before, after = city_source_counts(old, catalog), city_source_counts(new, catalog)
    over = {city: count for city, count in after.items() if count > before.get(city, 0) and count > cap}
    if over:
        raise Rejected(f'over the cap of {cap} sources per city: {over}')
    return {city: count for city, count in after.items() if count != before.get(city)}


def load_catalog(text: str) -> list[dict]:
    import yaml

    return yaml.safe_load(text)['sources']


# ----- gate steps --------------------------------------------------------------------------------

def scratch_env(python_path: str) -> dict:
    home = Path(tempfile.mkdtemp(prefix='source-onboarding-'))
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home), 'TMPDIR': str(home),
            'PREDICTION_BOT_COLLECTOR_ROOT': str(home / 'collector'), 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPATH': python_path}


def run_tests(cfg: dict, tree: Path, changed: list[str], out: Path) -> str:
    modules = list(cfg['test_modules'])
    modules += [p[:-3].replace('/', '.') for p in changed if fnmatch.fnmatchcase(p, 'tests/test_*.py')
                and (tree / p).exists() and p[:-3].replace('/', '.') not in modules]
    env = scratch_env(f'{tree}:{tree}/tests')
    try:
        result = run([cfg['python'], '-W', 'ignore', '-m', 'unittest', *modules], cwd=tree, env=env,
                     timeout=cfg['test_timeout_seconds'], check=False)
    except subprocess.TimeoutExpired:
        raise Rejected('tests timed out') from None
    finally:
        shutil.rmtree(env['HOME'], ignore_errors=True)
    out.write_text(result.stdout + result.stderr)
    tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or ['']
    if result.returncode:
        raise Rejected(f'tests failed ({tail[0]}); see {out}')
    return f'{len(modules)} module(s): {tail[0]}'


def run_probe(cfg: dict, tree: Path, source_ids: set[str], out: Path) -> dict:
    """Fetch each source live with the proposal's own code; each must yield a plausible dated forecast."""
    if not cfg['probe']:
        return {'skipped': True}
    env = scratch_env(str(tree))
    try:
        result = run([cfg['python'], str(Path(__file__).resolve()), 'probe', '--root', str(tree), '--out', str(out),
                      '--scratch', env['HOME'], *sorted(source_ids)], cwd=tree, env=env, timeout=600, check=False)
    except subprocess.TimeoutExpired:
        raise Rejected('the live fetch probe timed out') from None
    finally:
        shutil.rmtree(env['HOME'], ignore_errors=True)
    probe = read_json(out)
    if result.returncode or probe is None:
        raise Rejected(f'the live fetch probe crashed: {(result.stderr or result.stdout).strip()[-400:]}')
    failing = sorted(sid for sid, entry in probe.items() if not entry['passed'])
    if failing:
        raise Rejected('no plausible dated forecast from: ' + '; '.join(
            f'{sid} ({", ".join(sorted(set(probe[sid]["reasons"])))})' for sid in failing))
    return probe


REVIEW_PROMPT = """You are an independent, read-only reviewer gating an AUTOMATIC deployment into a running, paper-only
weather data collector. Do NOT edit, commit, merge, push or delete anything; no services, network or /mnt data.

`git diff {base} {merge}` (merge commit already prepared in this worktree) adds or patches city-local weather
forecast sources: registry entries in docs/weather/city_registry_starter.json, page parsers in
bot/feeds/weather_local*.py and tests. If you PASS, it is merged into the collector's checkout and the collector is
restarted automatically. Proposal note: {note}

Mechanical checks already passed: changed paths, registry fields, per-city source cap, tests ({tests}) and a live
fetch probe that ran the parser on today's real page: {probe}

Read the existing contract first: LocalForecastFeed.get_forecast in bot/feeds/weather_pro.py and existing parsers in
bot/feeds/weather_local*.py. BLOCK if the change could crash, hang or slow the collector (unbounded loops or
backtracking regexes over page text, sleeps, threads, import-time side effects), does anything besides parsing the
page text it is given (network, files, subprocess, environment, eval/exec, new third-party imports), maps the wrong
date, period, timezone, city or unit, could report a stale or undated value as today's forecast, reads outcome or
settlement data, weakens existing sources or checks, or breaks the parser contract. Untested edge cases that would
only make the new source unavailable are notes, not blockers.

First line exactly `VERDICT: PASS` or `VERDICT: BLOCK`, then numbered findings (file:line, minimal fix), up to 3 notes.
Under 400 words."""


def run_review(cfg: dict, tree: Path, base: str, merge: str, proposal: dict, tests: str, probe: dict,
               out: Path) -> str:
    probe_text = json.dumps({sid: {k: v for k, v in entry.items() if k != 'attempts'} for sid, entry in probe.items()}
                            if not probe.get('skipped') else probe, sort_keys=True)[:3000]
    prompt = REVIEW_PROMPT.format(base=base, merge=merge, note=proposal.get('note', '')[:500], tests=tests,
                                  probe=probe_text)
    command = [prompt if part == '{prompt}' else part for part in cfg['review_command']]
    try:
        result = run(command, cwd=tree, timeout=cfg['review_timeout_seconds'], check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Deferred(f'reviewer unavailable: {exc}') from exc
    out.write_text(result.stdout + result.stderr)
    first = next((line.strip().strip('*`# ') for line in result.stdout.splitlines() if line.strip()), '')
    verdict = re.fullmatch(r'VERDICT:\s*\**\s*(PASS|BLOCK)\**', first)
    if result.returncode:
        raise Deferred(f'reviewer failed (exit {result.returncode}); see {out}')
    if not verdict:
        raise NoVerdict(f'reviewer gave no verdict line; see {out}')
    if verdict.group(1) == 'BLOCK':
        raise Rejected(f'independent review BLOCK; findings in {out}')
    return str(out)


# ----- go live -----------------------------------------------------------------------------------

def unit_state(unit: str) -> dict:
    result = run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState,SubState,MainPID,NRestarts'], timeout=30)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def restart_collector(cfg: dict) -> tuple[bool, dict]:
    """Restart the unit; healthy means the same main process is still running after the settle time."""
    unit = cfg['collector_unit']
    try:
        run(['systemctl', '--user', 'restart', unit], timeout=120)
        time.sleep(min(5, cfg['restart_settle_seconds']))
        started = unit_state(unit)
        time.sleep(max(0, cfg['restart_settle_seconds'] - 5))
        settled = unit_state(unit)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        return False, {'error': f'{type(exc).__name__}: {exc}'}
    ok = (settled.get('ActiveState') == 'active' and settled.get('SubState') == 'running'
          and settled.get('MainPID') not in (None, '0') and settled.get('MainPID') == started.get('MainPID')
          and settled.get('NRestarts') == started.get('NRestarts'))
    return ok, {'started': started, 'settled': settled}


def checkout_ready(cfg: dict, beta: Path) -> str:
    """The collector checkout must be clean, on the branch and equal to the remote branch; returns its HEAD."""
    remote, branch = cfg['remote'], cfg['branch']
    if git('status', '--porcelain', '--untracked-files=no', cwd=beta):
        raise Deferred(f'{beta} has uncommitted changes')
    if git('rev-parse', '--abbrev-ref', 'HEAD', cwd=beta) != branch:
        raise Deferred(f'{beta} is not on {branch}')
    head = git('rev-parse', 'HEAD', cwd=beta)
    if head != git('rev-parse', f'{remote}/{branch}', cwd=beta):
        raise Deferred(f'{beta} differs from {remote}/{branch}')
    return head


def go_live(work: Work, cfg: dict, beta: Path, base: str, merge: str, proposal: dict) -> dict:
    remote, branch = cfg['remote'], cfg['branch']
    git('fetch', '-q', remote, branch, cwd=beta, timeout=300)
    if checkout_ready(cfg, beta) != base:  # re-checked: tests and review can take a while
        raise Deferred(f'{branch} moved during review; the proposal will be rebuilt on the new tip')
    git('merge', '--ff-only', '-q', merge, cwd=beta)
    try:
        git('push', '-q', remote, f'{merge}:refs/heads/{branch}', cwd=beta, timeout=300)
    except Deferred:
        git('reset', '-q', '--keep', base, cwd=beta)  # drops only our fast-forward; refuses to touch local edits
        raise
    ok, state = restart_collector(cfg)
    if ok:
        return {'merge': merge, 'restart': state}
    # The collector did not stay up on the new code: revert, restart (restore collection first), then push.
    try:
        git('revert', '-m', '1', '--no-edit', merge, cwd=beta)
        restored, restored_state = restart_collector(cfg)
    except Deferred as exc:
        raise OnboardingError(f'{proposal["id"]}: collector failed after go-live and the revert did not '
                              f'complete ({exc}); check {cfg["collector_unit"]} and {beta}') from exc
    revert = git('rev-parse', 'HEAD', cwd=beta)
    if not restored:
        raise OnboardingError(f'{proposal["id"]}: collector is not running even after rollback ({restored_state}); '
                              f'the revert {revert[:12]} is committed in {beta} but not pushed ({branch} on '
                              f'{remote} still has the merge)')
    try:
        git('push', '-q', remote, f'{revert}:refs/heads/{branch}', cwd=beta, timeout=300)
    except Deferred as exc:
        raise OnboardingError(f'{proposal["id"]}: collector restored on revert {revert[:12]}, but pushing the '
                              f'revert failed ({exc}); push {branch} from {beta}') from exc
    raise Rejected(f'collector did not stay up after restart ({state}); merge reverted as {revert[:12]} and the '
                   f'collector restored. Rebuild the change on a fresh branch from origin/beta (its commits are now '
                   f'reverted history)')


def process(work: Work, cfg: dict, proposal: dict) -> dict:
    beta = Path(cfg['beta_checkout'])
    remote, branch = cfg['remote'], cfg['branch']
    git('fetch', '-q', remote, branch, cwd=beta, timeout=300)
    base = checkout_ready(cfg, beta)
    ref = f'refs/source-proposals/{proposal["id"]}'
    folder = work.proposals / proposal['id']
    tree = work.tmp / proposal['id']
    try:
        try:
            git('fetch', '-q', str(work.agent_repo), f'+refs/heads/{proposal["branch"]}:{ref}', cwd=beta)
        except Deferred as exc:
            raise Rejected(f'branch {proposal["branch"]} could not be fetched from the agent clone ({exc})') from exc
        if git('rev-parse', ref, cwd=beta) != proposal['commit']:
            raise Rejected('the branch moved after it was proposed; propose it again')
        shutil.rmtree(tree, ignore_errors=True)
        git('worktree', 'add', '-q', '--detach', str(tree), base, cwd=beta)
        try:
            git('merge', '--no-ff', '--no-edit', '-q', '-m',
                f'[verified] Onboard local weather source(s): {proposal["note"][:120]}', ref, cwd=tree)
        except Deferred:
            raise Rejected(f'does not merge cleanly onto {branch} {base[:12]}') from None
        merge = git('rev-parse', 'HEAD', cwd=tree)
        try:
            changed = changed_paths(tree, base, merge)
            check_scope(changed, cfg['allowed_paths'])
            old_registry = json.loads(git('show', f'{base}:{REGISTRY}', cwd=tree))
            new_registry = json.loads((tree / REGISTRY).read_text())
            added, patched = check_registry(old_registry, new_registry, work.onboarded_ids(),
                                            set(proposal.get('sources') or ()))
            check_mappings(git('show', f'{base}:{MAPPINGS_MODULE}', cwd=tree), (tree / MAPPINGS_MODULE).read_text(),
                           added | patched)
            counts = check_cap(old_registry, new_registry, load_catalog((tree / CATALOG).read_text()),
                               cfg['max_sources_per_city'])
            added_cities = {s['city_id'] for s in new_registry['sources'] if s['source_id'] in added}
        except (ValueError, KeyError, TypeError, AttributeError, OSError, SyntaxError) as exc:
            raise Rejected(f'malformed change: {type(exc).__name__}: {exc}') from exc
        untraded = sorted(added_cities - traded_cities(cfg)) if added_cities else []
        if untraded:
            raise Rejected(f'no market for {", ".join(untraded)} in the last {TRADED_WINDOW_HOURS}h of collected rows: '
                           'local sources are fetched only for cities with listed markets')
        tests = run_tests(cfg, tree, changed, folder / 'tests.log')
        probe = run_probe(cfg, tree, added | patched, folder / 'probe.json')
        review = run_review(cfg, tree, base, merge, proposal, tests, probe, folder / 'review.md')
        live = go_live(work, cfg, beta, base, merge, proposal)
    finally:
        with contextlib.suppress(Deferred):
            git('worktree', 'remove', '--force', str(tree), cwd=beta)
        with contextlib.suppress(Deferred):
            git('update-ref', '-d', ref, cwd=beta)
    by_id = {s['source_id']: s for s in new_registry['sources']}
    with work.updating_watches() as watches:
        for source_id in sorted(added):
            live_at = now()
            watches.append({'source_id': source_id, 'city_id': by_id[source_id]['city_id'], 'url': by_id[source_id]['url'],
                            'proposal': proposal['id'], 'live_at': iso(live_at),
                            'until': iso(live_at + timedelta(days=cfg['watch_days'])), 'patches': []})
        for watch in watches:
            if watch['source_id'] in patched:
                watch['patches'].append({'proposal': proposal['id'], 'at': iso(now()), 'merge': live['merge']})
    return {'added': sorted(added), 'patched': sorted(patched), 'tests': tests, 'review': review,
            'city_counts': counts, **live}


# ----- probe (runs with the proposal's code on PYTHONPATH) ---------------------------------------

def cmd_probe(args) -> int:
    sys.path.insert(0, args.root)
    from zoneinfo import ZoneInfo

    from bot.feeds.weather_capture_store import WeatherCaptureStore
    from bot.feeds.weather_pro import LocalForecastFeed, NWSFeed, ProWeatherEngine
    from bot.weather.registry import WeatherRegistry

    registry = WeatherRegistry.from_file(Path(args.root) / REGISTRY)
    store = WeatherCaptureStore(storage_root=args.scratch)
    nws = NWSFeed(capture_store=store)
    feed = LocalForecastFeed(nws.http, capture_store=store)
    results = {}
    for source_id in args.sources:
        source = registry._sources_by_id.get(source_id)
        entry = {'passed': False, 'reasons': [], 'attempts': []}
        results[source_id] = entry
        if source is None:
            entry['reasons'].append('not_in_registry')
            continue
        city = registry.get_city(source['city_id'])
        candidate = next((s for s in registry.collection_sources(city['city']) if s['source_id'] == source_id), None)
        if candidate is None:
            entry['reasons'].append('not_a_collection_source')
            continue
        today = datetime.now(ZoneInfo(city['timezone'])).date()
        for target in (today, today + timedelta(days=1)):
            snapshot = feed.get_forecast(candidate, target_date=target.isoformat(), measurement_kind='high')
            plausible = ProWeatherEngine._snapshot_is_plausible(None, snapshot, 'high')
            entry['attempts'].append({'target_date': target.isoformat(), 'scoreable': snapshot.scoreable_forecast,
                                      'plausible': plausible, 'high_f': snapshot.high_temp_f,
                                      'reason': snapshot.availability_reason})
            entry['reasons'].append(snapshot.availability_reason or ('ok' if plausible else 'implausible'))
            entry['passed'] = entry['passed'] or bool(plausible)
        entry['highs_f'] = {a['target_date']: a['high_f'] for a in entry['attempts']}
    nws.close()
    write_json(Path(args.out), results)
    return 0


# ----- watch -------------------------------------------------------------------------------------

def recent_index_rows(index: Path, since: datetime, max_bytes: int = 64 << 20) -> list[dict]:
    rows, read = [], 0
    with index.open('rb') as handle:
        end = handle.seek(0, os.SEEK_END)
        carry = b''
        while end > 0 and read < max_bytes:
            start = max(0, end - (1 << 20))
            handle.seek(start)
            chunk = handle.read(end - start) + carry
            read += end - start
            lines = chunk.split(b'\n')
            carry, lines = (lines[0], lines[1:]) if start > 0 else (b'', lines)
            stop = False
            for line in reversed(lines):
                if not line.strip():
                    continue
                row = json.loads(line)
                observed = parse_time(row.get('observed_at'))
                if observed is not None and observed < since:
                    stop = True
                    break
                rows.append(row)
            if stop:
                break
            end = start
    return rows[::-1]


def dig(value, path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def row_sources(snapshot_path: Path, row: dict) -> tuple[dict, list[dict]]:
    with snapshot_path.open('rb') as handle:
        handle.seek(row['byte_offset'])
        raw = json.loads(handle.read(row['byte_length']))
    value = dig(raw, SOURCES_PATH)
    return raw, value if isinstance(value, list) else []


def sample_rows(cfg: dict, since: datetime, sample: int) -> tuple[int, list[tuple[dict, list[dict]]]]:
    """Collected rows observed since `since`, spread over the whole window: (rows in the window, [(raw, sources)])."""
    index_rows = recent_index_rows(Path(cfg['replay_index']), since)
    if not index_rows:
        return 0, []
    count, n = len(index_rows), min(sample, len(index_rows))
    picks = sorted({round(i * (count - 1) / max(1, n - 1)) for i in range(n)})  # first to last, evenly spaced
    snapshots = Path(cfg['market_snapshots'])
    return count, [row_sources(snapshots, index_rows[i]) for i in picks]


def row_cities(rows: list[tuple[dict, list[dict]]]) -> set[str]:
    return {city for city in (dig(raw, CITY_PATH) for raw, _ in rows) if isinstance(city, str)}


def market_cities(cfg: dict, since: datetime) -> set[str]:
    """Cities of every market collected since `since` (one row per market, so none is missed by sampling)."""
    latest = {row.get('market_id'): row for row in recent_index_rows(Path(cfg['replay_index']), since)}
    snapshots = Path(cfg['market_snapshots'])
    return row_cities([row_sources(snapshots, row) for row in latest.values()])


def traded_cities(cfg: dict, hours: int = TRADED_WINDOW_HOURS) -> set[str]:
    """Cities with collected markets recently (the engine fetches local sources only for these)."""
    try:
        cities = market_cities(cfg, now() - timedelta(hours=hours))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Deferred(f'cannot read collected rows to find traded cities: {type(exc).__name__}: {exc}') from exc
    if not cities:
        raise Deferred(f'no collected rows in the last {hours}h to find traded cities')
    return cities


def source_metrics(rows: list[tuple[dict, list[dict]]], source_id: str) -> dict:
    present = scoreable = clock_bad = date_bad = 0
    reasons, diffs, sizes, highs = Counter(), [], [], []
    for raw, sources in rows:
        by_id = {s.get('source_id'): s for s in sources}
        source = by_id.get(source_id)
        if source is None:
            continue
        present += 1
        sizes.append(len(json.dumps(source, separators=(',', ':'))))
        if not source.get('scoreable_forecast'):
            reasons[source.get('availability_reason') or (source.get('source_metadata') or {}).get('availability_reason')
                    or source.get('forecast_availability') or 'unavailable'] += 1
            continue
        scoreable += 1
        high = source.get('forecast_high')
        if isinstance(high, (int, float)):
            highs.append(high)
            others = [s.get('forecast_high') for sid, s in by_id.items() if sid != source_id and s.get('scoreable_forecast')
                      and isinstance(s.get('forecast_high'), (int, float))]
            if others:
                diffs.append(abs(high - statistics.median(others)))
        observed = parse_time(raw.get('observed_at'))
        fetched = parse_time(source.get('source_fetched_at') or source.get('fetched_at'))
        as_of = parse_time(source.get('source_as_of') or source.get('as_of'))
        if observed and ((fetched and fetched > observed + timedelta(minutes=5)) or (as_of and as_of > observed + timedelta(minutes=5))):
            clock_bad += 1
        # A daytime high's forecast period must start on the market date (in the source's own timezone).
        market_date, period_start = source.get('market_target_date'), source.get('forecast_start')
        if (source.get('forecast_measurement_kind') == 'high' and market_date and isinstance(period_start, str)
                and period_start[:10] != market_date):
            date_bad += 1
    issues = []
    if not present:
        issues.append('missing from collected rows')
    elif scoreable / present < 0.5:
        issues.append(f'only {scoreable}/{present} rows scoreable')
    if diffs and statistics.median(diffs) > 6:
        issues.append(f'median gap to other sources {statistics.median(diffs):.1f}F')
    if clock_bad:
        issues.append(f'{clock_bad} rows with a source clock after the decision time')
    if date_bad:
        issues.append(f'{date_bad} rows whose forecast period starts on another date than the market')
    return {'rows': present, 'scoreable': scoreable, 'unavailable_reasons': dict(reasons.most_common(4)),
            'median_gap_f': round(statistics.median(diffs), 2) if diffs else None,
            'high_range_f': [min(highs), max(highs)] if highs else None,
            'bytes_per_row': round(statistics.mean(sizes)) if sizes else None, 'issues': issues}


def storage_snapshot(cfg: dict) -> dict:
    snapshots = Path(cfg['market_snapshots'])
    accounting = read_json(Path(cfg['capture_root']) / 'accounting.json', {})
    return {'at': iso(now()), 'market_snapshots_bytes': snapshots.stat().st_size,
            'capture_bytes': accounting.get('bytes'), 'disk_free_bytes': shutil.disk_usage(snapshots.parent).free}


def storage_growth(history: list[dict]) -> dict:
    latest = history[-1]
    older = [h for h in history if parse_time(h['at']) <= parse_time(latest['at']) - timedelta(hours=20)]
    if not older:
        return {}
    base = older[-1]
    days = (parse_time(latest['at']) - parse_time(base['at'])).total_seconds() / 86400
    per_day = {key: round((latest[key] - base[key]) / days) for key in ('market_snapshots_bytes', 'capture_bytes')
               if isinstance(latest.get(key), int) and isinstance(base.get(key), int)}
    return {'since': base['at'], 'bytes_per_day': per_day}


def cmd_watch(args) -> int:
    work = Work(Path(args.work))
    cfg = work.config()
    moment = now()
    watches = work.watches()
    active = [w for w in watches if parse_time(w['until']) > moment and not w.get('completed_at')]
    since = moment - timedelta(hours=args.hours)
    index_count, sampled = sample_rows(cfg, since, args.sample)
    window_cities = market_cities(cfg, since) if sampled else set()
    report = {'generated_at': iso(moment), 'window_hours': args.hours, 'index_rows': index_count,
              'sampled_rows': len(sampled), 'sources': {}}
    for watch in active:
        live_at = parse_time(watch['live_at'])
        rows = [item for item in sampled if (parse_time(item[0].get('observed_at')) or moment) >= live_at]
        metrics = source_metrics(rows, watch['source_id'])
        if not rows and moment - live_at < timedelta(hours=1):
            metrics['issues'] = []  # first collection pass may not have run yet
        elif not metrics['rows'] and sampled and watch['city_id'] not in window_cities:
            # Not a source defect: the engine fetches local sources only for cities with listed markets.
            metrics['issues'] = []
            metrics['note'] = f'idle: no {watch["city_id"]} markets collected in the window'
        report['sources'][watch['source_id']] = {**metrics, 'city_id': watch['city_id'], 'live_at': watch['live_at'],
                                                 'until': watch['until'], 'patches': len(watch['patches'])}
    if any(not w.get('completed_at') and parse_time(w['until']) <= moment for w in watches):
        with work.updating_watches() as current:  # re-read: onboard may have added a watch meanwhile
            for watch in current:
                if not watch.get('completed_at') and parse_time(watch['until']) <= moment:
                    watch['completed_at'] = iso(moment)
                    report.setdefault('completed', []).append(watch['source_id'])
    with work.storage_history.open('a') as stream:
        stream.write(json.dumps(storage_snapshot(cfg)) + '\n')
    history = [json.loads(line) for line in work.storage_history.read_text().splitlines() if line.strip()]
    report['storage'] = {'latest': history[-1], **storage_growth(history)}
    stamp = moment.strftime('%Y%m%dT%H%M%SZ')
    write_json(work.reports / f'watch-{stamp}.json', report)
    lines = [f'Source watch {stamp} — {len(active)} source(s) under watch, {len(sampled)} rows sampled from '
             f'the last {args.hours}h']
    for source_id, entry in report['sources'].items():
        flag = '; '.join(entry['issues']) or entry.get('note') or 'ok'
        lines.append(f'- {source_id} ({entry["city_id"]}, live {entry["live_at"][:10]}, until {entry["until"][:10]}): '
                     f'{entry["scoreable"]}/{entry["rows"]} scoreable, gap {entry["median_gap_f"]}F, '
                     f'{entry["bytes_per_row"]} B/row — {flag}')
    for source_id in report.get('completed', []):
        lines.append(f'- {source_id}: two-week watch completed')
    growth = report['storage'].get('bytes_per_day', {})
    lines.append(f'- Storage: snapshots {history[-1]["market_snapshots_bytes"] / 1e9:.1f} GB'
                 + (f' (+{growth["market_snapshots_bytes"] / 1e9:.2f} GB/day)' if 'market_snapshots_bytes' in growth else '')
                 + f', captures {(history[-1]["capture_bytes"] or 0) / 1e9:.1f} GB, disk free '
                 f'{history[-1]["disk_free_bytes"] / 1e9:.0f} GB')
    (work.reports / f'watch-{stamp}.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    return 0


# ----- commands ----------------------------------------------------------------------------------

def cmd_init(args) -> int:
    work = Work(Path(args.work))
    if work.config_path.exists():
        raise SystemExit(f'already initialized: {work.config_path}')
    beta = Path(args.beta).resolve()
    cfg = {'schema': 1, 'beta_checkout': str(beta), 'python': args.python, 'collector_unit': args.unit,
           'replay_index': args.replay_index, 'market_snapshots': args.market_snapshots,
           'capture_root': args.capture_root, 'max_sources_per_city': args.max_sources_per_city,
           'watch_days': args.watch_days}
    work.root.mkdir(parents=True, exist_ok=True)
    run(['git', 'clone', '-q', '--branch', DEFAULTS['branch'], str(beta), str(work.agent_repo)], timeout=600)
    git('config', 'user.name', 'Source Agent', cwd=work.agent_repo)
    git('config', 'user.email', 'source-agent@localhost.invalid', cwd=work.agent_repo)
    write_json(work.config_path, cfg)
    print(json.dumps({'status': 'initialized', **cfg}))
    return 0


def cmd_status(args) -> int:
    work = Work(Path(args.work))
    cfg = work.config()
    beta = Path(cfg['beta_checkout'])
    registry = json.loads((beta / REGISTRY).read_text())
    counts = city_source_counts(registry, load_catalog((beta / CATALOG).read_text()))
    free = {city: cfg['max_sources_per_city'] - count for city, count in counts.items()}
    try:
        traded = traded_cities(cfg)
    except Deferred as exc:
        traded = set(free)
        print(f'(traded cities unknown: {exc})')
    print(f'Cap: {cfg["max_sources_per_city"]} sources per city; free slots in traded cities (markets collected in '
          f'the last {TRADED_WINDOW_HOURS}h): ' + ', '.join(f'{city} {slots}' for city, slots in sorted(free.items())
                                                            if city in traded))
    idle = sorted(set(free) - traded)
    if idle:
        print(f'Not traded now (new sources there are rejected): {", ".join(idle)}')
    for record in work.proposal_records()[-10:]:
        print(f'- proposal {record["id"]} [{record["status"]}] {record["branch"]} — {record.get("reason") or record["note"][:80]}')
    for record in work.proposal_records():
        if record['status'] == 'failed':
            print(f'BLOCKED: onboarding is paused until the operator repairs and acknowledges {record["id"]}')
    for watch in work.watches():
        state = 'completed' if watch.get('completed_at') else f'watch until {watch["until"][:10]}'
        print(f'- source {watch["source_id"]} ({watch["city_id"]}) live {watch["live_at"][:10]}, {state}, '
              f'{len(watch["patches"])} patch(es)')
    return 0


def cmd_propose(args) -> int:
    work = Work(Path(args.work))
    work.config()
    commit = git('rev-parse', '--verify', f'refs/heads/{args.branch}^{{commit}}', cwd=work.agent_repo)
    if any(r['status'] == 'pending' and r['branch'] == args.branch for r in work.proposal_records()):
        raise SystemExit(f'{args.branch} already has a pending proposal')
    record = {'id': f'{now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}', 'branch': args.branch, 'commit': commit,
              'note': args.note, 'sources': sorted(set(args.source or ())), 'proposed_at': now().isoformat(),
              'status': 'pending', 'events': []}
    work.save_proposal(record)
    print(json.dumps({'status': 'proposed', 'id': record['id'], 'commit': commit}))
    return 0


def cmd_acknowledge(args) -> int:
    work = Work(Path(args.work))
    record = next((r for r in work.proposal_records() if r['id'] == args.id and r['status'] == 'failed'), None)
    if record is None:
        raise SystemExit(f'no failed proposal {args.id}')
    record.update(status='failed_acknowledged')
    record['events'].append({'at': iso(now()), 'event': 'acknowledged', 'reason': args.note})
    work.save_proposal(record)
    print(json.dumps({'status': 'acknowledged', 'id': args.id}))
    return 0


def cmd_onboard(args) -> int:
    work = Work(Path(args.work))
    cfg = work.config()
    lines = []
    if any(r['status'] == 'failed' for r in work.proposal_records()):
        return 0  # an incomplete rollback was reported; nothing goes live until the operator acknowledges it
    try:
        with work.lock():
            for record in [r for r in work.proposal_records() if r['status'] == 'pending']:
                previous = record.get('reason')
                try:
                    result = process(work, cfg, record)
                    record.update(status='live', reason=None, result=result)
                    lines.append(f'LIVE {record["id"]}: added {result["added"] or "-"}, patched {result["patched"] or "-"}; '
                                 f'merge {result["merge"][:12]}; collector restarted and running')
                except Rejected as exc:
                    record.update(status='rejected', reason=str(exc))
                    lines.append(f'REJECTED {record["id"]} ({record["branch"]}): {exc}')
                except NoVerdict as exc:
                    attempts = 1 + sum(e['event'] == 'no_verdict' for e in record['events'])
                    record['events'].append({'at': iso(now()), 'event': 'no_verdict', 'reason': str(exc)})
                    if attempts < MAX_NO_VERDICT:
                        record['reason'] = f'deferred: {exc}'
                        work.save_proposal(record)
                        break
                    record.update(status='rejected', reason=f'{exc} ({attempts} attempts)')
                    lines.append(f'REJECTED {record["id"]} ({record["branch"]}): {record["reason"]}')
                except Deferred as exc:
                    record['reason'] = f'deferred: {exc}'
                    if record['reason'] != previous:  # report a new reason once, not every run
                        lines.append(f'DEFERRED {record["id"]}: {exc}')
                    record['events'].append({'at': iso(now()), 'event': 'deferred', 'reason': str(exc)})
                    work.save_proposal(record)
                    break  # the precondition is shared; retry next run
                except OnboardingError as exc:
                    # Never retried automatically: the operator must look at the collector and checkout first.
                    record.update(status='failed', reason=str(exc))
                    record['events'].append({'at': iso(now()), 'event': 'failed', 'reason': str(exc)})
                    work.save_proposal(record)
                    raise
                record['events'].append({'at': iso(now()), 'event': record['status'], 'reason': record.get('reason')})
                work.save_proposal(record)
    except Deferred as exc:
        lines.append(f'DEFERRED: {exc}')
    except OnboardingError as exc:
        print('[CRON_FAILURE]\n' + '\n'.join([*lines, str(exc)]))
        return 1
    if lines:
        print('Source onboarding\n' + '\n'.join(f'• {line}' for line in lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init')
    p.add_argument('--work', required=True)
    p.add_argument('--beta', required=True, help='collector checkout (its PROJECT_ROOT)')
    p.add_argument('--python', required=True)
    p.add_argument('--unit', required=True, help='collector systemd --user unit')
    p.add_argument('--replay-index', required=True)
    p.add_argument('--market-snapshots', required=True)
    p.add_argument('--capture-root', required=True)
    p.add_argument('--max-sources-per-city', type=int, default=DEFAULTS['max_sources_per_city'])
    p.add_argument('--watch-days', type=int, default=DEFAULTS['watch_days'])
    p.set_defaults(func=cmd_init)
    for name, func in (('status', cmd_status), ('onboard', cmd_onboard)):
        p = sub.add_parser(name)
        p.add_argument('--work', required=True)
        p.set_defaults(func=func)
    p = sub.add_parser('propose')
    p.add_argument('--work', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--note', required=True, help='what the change adds or fixes and why')
    p.add_argument('--source', action='append', help='onboarded source a parser-only fix is for (repeatable)')
    p.set_defaults(func=cmd_propose)
    p = sub.add_parser('acknowledge', help='operator: clear a failed rollback after repairing it')
    p.add_argument('--work', required=True)
    p.add_argument('--id', required=True)
    p.add_argument('--note', required=True)
    p.set_defaults(func=cmd_acknowledge)
    p = sub.add_parser('watch')
    p.add_argument('--work', required=True)
    p.add_argument('--hours', type=int, default=12)
    p.add_argument('--sample', type=int, default=400)
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser('probe', help=argparse.SUPPRESS)
    p.add_argument('--root', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--scratch', required=True)
    p.add_argument('sources', nargs='+')
    p.set_defaults(func=cmd_probe)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
