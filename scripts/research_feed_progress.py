#!/usr/bin/env python3
"""Acknowledge consumed feed pages before advancing a research checkpoint.

This is a cooperating-agent receipt check, not publisher authentication or an
atomic transaction with downstream experiments. Never changes the collector.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.collector_paths import derived_reports_root

EMPTY = hashlib.sha256(b'').hexdigest()
MAX_META = 8 * 1024 * 1024


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_bounded(path: Path, maximum: int) -> bytes:
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError(f'file exceeds byte limit: {path}')
    return raw


def within(path: Path, root: Path) -> Path:
    path = path.resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('paths must be separate objects under data/derived_reports')
    return path


def page_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('invalid page extent')
    return value


def load_page(path: Path, root: Path, max_input_bytes: int) -> tuple[dict, str]:
    path = within(path, root)
    if path.name != 'receipt.json':
        raise ValueError('page must name receipt.json')
    raw = read_bounded(path, MAX_META)
    page_hash = digest(raw)
    if path.parent.name != page_hash:
        raise ValueError('page receipt hash does not match directory')
    page = json.loads(raw)
    if not isinstance(page, dict) or page.get('schema_name') != 'fresh_research_feed':
        raise ValueError('invalid feed page receipt')
    accepted = page.get('accepted_rows')
    if type(accepted) is not int or accepted < 0:
        raise ValueError('invalid accepted row count')
    extent, start, end = (page_int(page.get(k)) for k in ('committed_index_bytes', 'page_start_index_bytes', 'next_index_bytes'))
    if not 0 <= end <= start <= extent:
        raise ValueError('invalid page extent')
    for name, key, cap in (('manifest.json','manifest_sha256',MAX_META),
                           ('inputs.jsonl','inputs_sha256',max_input_bytes),
                           ('outcomes.jsonl','outcomes_sha256',1)):
        content = read_bounded(path.parent / name, cap)
        if digest(content) != page.get(key):
            raise ValueError(f'page {name} hash mismatch')
        if name == 'inputs.jsonl' and sum(bool(line.strip()) for line in content.splitlines()) != accepted:
            raise ValueError('accepted row count does not match input')
        if name == 'outcomes.jsonl' and content != b'':
            raise ValueError('feed outcomes must be empty')
    return page, page_hash


def load_comparison(path: Path, root: Path, page: dict) -> str:
    raw = read_bounded(within(path, root), MAX_META)
    comp = json.loads(raw)
    if (not isinstance(comp, dict) or comp.get('selected_rows_matched') is not True or
            comp.get('matched_decisions') != page['accepted_rows'] or
            comp.get('input_sha256') != page['inputs_sha256'] or comp.get('outcome_sha256') != EMPTY):
        raise ValueError('comparison does not match feed page and empty outcomes')
    runs = comp.get('runs')
    if not isinstance(runs, list) or len(runs) != 2 or any(not isinstance(r, dict) or not isinstance(r.get('experiment_id'), str) or not r['experiment_id'] for r in runs):
        raise ValueError('comparison must identify both experiments')
    return digest(raw)


def publish(path: Path, value: dict) -> None:
    if not path.parent.is_dir():
        raise ValueError('output parent must exist')
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.progress-', dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def ack(page_path: Path, ack_dir: Path, comparison: Path | None, root: Path, limit: int) -> dict:
    page, page_hash = load_page(page_path, root, limit)
    ack_dir = within(ack_dir, root)
    if not ack_dir.is_dir():
        raise ValueError('ack directory must exist')
    if ack_dir == page_path.resolve().parent.parent or ack_dir.is_relative_to(page_path.resolve().parent.parent):
        raise ValueError('ack directory must be outside immutable feed pages')
    if page['accepted_rows']:
        if comparison is None:
            raise ValueError('comparison required for accepted input rows')
        comparison = within(comparison, root)
        comparison_hash = load_comparison(comparison, root, page)
        status = 'compared'
    else:
        if comparison is not None:
            raise ValueError('zero-accepted page must not assert a comparison')
        comparison_hash, status = None, 'no_usable_inputs'
    receipt = {'schema_name':'research_feed_page_ack', 'schema_version':1, 'page_sha256':page_hash,
               'status':status, 'comparison_path':str(comparison) if comparison else None,
               'comparison_sha256':comparison_hash}
    dest = ack_dir / (page_hash + '.json')
    try:
        publish(dest, receipt)
        reused = False
    except FileExistsError:
        if json.loads(read_bounded(dest, MAX_META)) != receipt:
            raise ValueError('existing acknowledgment differs')
        reused = True
    return {'status':status,'page_sha256':page_hash,'ack_path':str(dest),'reused':reused}


def finish(last_path: Path, previous_path: Path | None, previous_checkpoint: Path | None, ack_dir: Path, output: Path, root: Path, limit: int) -> dict:
    last_path = within(last_path, root)
    ack_dir, output = within(ack_dir, root), within(output, root)
    feed_root = last_path.parent.parent
    if output == feed_root or output.is_relative_to(feed_root) or ack_dir == feed_root or ack_dir.is_relative_to(feed_root):
        raise ValueError('checkpoint and acknowledgments must be outside immutable feed pages')
    if output.exists():
        raise ValueError('checkpoint output already exists')
    last, last_hash = load_page(last_path, root, limit)
    if last.get('checkpoint_eligible') is not True:
        raise ValueError('last page is not checkpoint eligible')
    if previous_path:
        previous, previous_hash = load_page(previous_path, root, limit)
        if previous_checkpoint is None:
            raise ValueError('previous checkpoint proof required')
        proof = json.loads(read_bounded(within(previous_checkpoint, root), MAX_META))
        if (not isinstance(proof, dict) or proof.get('schema_name') != 'research_feed_checkpoint'
                or proof.get('last_page_sha256') != previous_hash):
            raise ValueError('previous checkpoint proof does not match previous page')
        if last.get('complete') is not True:
            raise ValueError('delta is not complete')
        if any(previous.get(k) != last.get(k) for k in ('index_path', 'archive_identity')):
            raise ValueError('previous checkpoint source differs')
        boundary = previous.get('committed_index_bytes')
    else:
        if previous_checkpoint is not None:
            raise ValueError('previous checkpoint proof requires previous page')
        previous, previous_hash, boundary = None, None, 0
    pages, seen = [], set()
    path, page, page_hash = last_path, last, last_hash
    while True:
        if page_hash in seen:
            raise ValueError('page chain contains a cycle')
        seen.add(page_hash)
        if page.get('previous_receipt_sha256') != previous_hash:
            raise ValueError('page chain uses a different previous checkpoint')
        for key in ('manifest_sha256','index_sha256','index_path','archive_identity','committed_index_bytes'):
            if page.get(key) != last.get(key):
                raise ValueError('page chain changes pinned source')
        ack_path = ack_dir / (page_hash + '.json')
        if not ack_path.is_file():
            raise ValueError('unacknowledged feed page')
        receipt = json.loads(read_bounded(ack_path, MAX_META))
        if not isinstance(receipt, dict) or receipt.get('schema_name') != 'research_feed_page_ack' or receipt.get('page_sha256') != page_hash:
            raise ValueError('invalid page acknowledgment')
        if page['accepted_rows']:
            if receipt.get('status') != 'compared' or not isinstance(receipt.get('comparison_path'), str):
                raise ValueError('page comparison acknowledgment missing')
            if load_comparison(Path(receipt['comparison_path']), root, page) != receipt.get('comparison_sha256'):
                raise ValueError('page comparison changed after acknowledgment')
        elif receipt.get('status') != 'no_usable_inputs' or receipt.get('comparison_path') is not None:
            raise ValueError('zero-input acknowledgment invalid')
        pages.append((path, page, page_hash))
        parent_hash = page.get('resume_receipt_sha256')
        if parent_hash is None:
            break
        if not isinstance(parent_hash, str) or len(parent_hash) != 64 or any(c not in '0123456789abcdef' for c in parent_hash):
            raise ValueError('invalid parent page identity')
        parent_path = last_path.parent.parent / parent_hash / 'receipt.json'
        parent, actual = load_page(parent_path, root, limit)
        if actual != parent_hash or parent.get('next_index_bytes') != page.get('page_start_index_bytes'):
            raise ValueError('page chain has a gap or overlap')
        path, page, page_hash = parent_path, parent, actual
    if page.get('page_start_index_bytes') != last.get('committed_index_bytes'):
        raise ValueError('page chain does not start at pinned extent')
    if last.get('complete') is True and last.get('next_index_bytes') != boundary:
        raise ValueError('page chain does not reach previous checkpoint boundary')
    coverage = 'complete_delta' if previous else ('complete_bootstrap' if last.get('complete') is True else 'partial_bootstrap_newest_sample')
    result = {'schema_name':'research_feed_checkpoint', 'schema_version':1, 'last_page_path':str(last_path),
              'last_page_sha256':last_hash, 'previous_receipt_sha256':previous_hash,
              'manifest_sha256':last.get('manifest_sha256'), 'page_count':len(pages),
              'page_sha256_newest_to_oldest':[x[2] for x in reversed(pages)],
              'accepted_rows':sum(x[1]['accepted_rows'] for x in pages), 'coverage':coverage,
              'limitations':['acknowledgments verify declared receipts, not experiment producer authenticity',
                             'checkpoint does not prove publication continuity or independent events']}
    publish(output, result)
    return {'checkpoint_path':str(output),'last_page_path':str(last_path),'page_count':len(pages),'coverage':coverage}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('ack')
    a.add_argument('--page', type=Path, required=True)
    a.add_argument('--ack-dir', type=Path, required=True)
    a.add_argument('--comparison', type=Path)
    b = sub.add_parser('finish')
    b.add_argument('--last-page', type=Path, required=True)
    b.add_argument('--previous', type=Path)
    b.add_argument('--previous-checkpoint', type=Path)
    b.add_argument('--ack-dir', type=Path, required=True)
    b.add_argument('--output', type=Path, required=True)
    for p in (a,b):
        p.add_argument('--max-input-bytes', type=int, default=64*1024*1024)
    args=parser.parse_args()
    try:
        if args.max_input_bytes < 1:
            raise ValueError('input byte limit must be positive')
        root=derived_reports_root().resolve()
        result = ack(args.page,args.ack_dir,args.comparison,root,args.max_input_bytes) if args.command=='ack' else finish(args.last_page,args.previous,args.previous_checkpoint,args.ack_dir,args.output,root,args.max_input_bytes)
        print(json.dumps(result,sort_keys=True))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(f'research_feed_progress: {exc}',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
