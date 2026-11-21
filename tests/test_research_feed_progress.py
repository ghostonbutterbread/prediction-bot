import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bot.collector_replay_index import build_collector_replay_index, update_collector_replay_index
from tests.test_collector_replay_inputs import _legacy_collector_row

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/research_feed_progress.py'
EMPTY = hashlib.sha256(b'').hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ResearchFeedProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.derived = self.root / 'collector/data/derived_reports'
        self.feed = self.derived / 'feed'
        self.ack = self.derived / 'ack'
        self.feed.mkdir(parents=True)
        self.ack.mkdir()
        self.checkpoint = self.derived / 'checkpoint.json'
        self.env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', HOME=str(self.root), TMPDIR=str(self.root),
                        PREDICTION_BOT_COLLECTOR_ROOT=str(self.root/'collector'))

        self.comparison_stores = {}

    def default_inputs(self):
        source = self.root / 'fixture-source'; source.mkdir()
        raw, index, manifest = source/'raw.jsonl', source/'index.jsonl', source/'manifest.json'
        row = _legacy_collector_row(); row['run_id'] = 'fixture'
        raw.write_text(json.dumps(row) + '\n')
        build_collector_replay_index(raw, index, manifest)
        out = self.derived/'fixture-feed'; out.mkdir()
        cmd = [sys.executable, str(ROOT/'scripts/fresh_research_feed.py'), '--index', str(index), '--manifest', str(manifest),
               '--output-root', str(out), '--protected-root', str(source), '--max-index-bytes', '2000000',
               '--max-inspected-rows', '1', '--max-raw-row-bytes', '200000', '--max-wall-seconds', '30',
               '--max-memory-bytes', '536870912']
        result = subprocess.run(cmd, cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (Path(json.loads(result.stdout)['directory'])/'inputs.jsonl').read_bytes()

    def page(self, *, previous=None, resume=None, start=100, end=0, complete=True, checkpoint=True, data=None):
        if data is None:
            if not hasattr(self, '_inputs'):
                self._inputs = self.default_inputs()
            data = self._inputs
        manifest = b'{"manifest":"synthetic"}\n'
        receipt = {'schema_name':'fresh_research_feed', 'schema_version':1, 'status':'initial' if previous is None else ('new_indexed_data' if complete else 'truncated'),
                   'complete':complete, 'checkpoint_eligible':checkpoint,'manifest_sha256':digest(manifest),'index_sha256':'b'*64,
                   'index_path':'/synthetic/index', 'archive_identity':{'device':1,'inode':2},'committed_index_bytes':start if previous is None else 100,
                   'page_start_index_bytes':start,'next_index_bytes':end,
                   'previous_receipt_sha256':digest(previous.read_bytes()) if previous else None,
                   'resume_receipt_sha256':digest(resume.read_bytes()) if resume else None,
                   'inputs_sha256':digest(data),'outcomes_sha256':EMPTY, 'accepted_rows':int(bool(data))}
        raw = encoded(receipt)
        directory = self.feed / digest(raw)
        directory.mkdir()
        for name, content in [('receipt.json',raw),('manifest.json',manifest),('inputs.jsonl',data),('outcomes.jsonl',b'')]:
            (directory/name).write_bytes(content)
        return directory/'receipt.json'

    def comparison(self, page):
        repo = self.root/'fixture-repo'
        env = dict(self.env, PYTHONPATH=str(repo), GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test',
                   GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        if not repo.exists():
            repo.mkdir()
            shutil.copytree(ROOT/'bot', repo/'bot', ignore=shutil.ignore_patterns('__pycache__'))
            (repo/'scripts').mkdir()
            shutil.copy2(ROOT/'scripts/auto_research.py', repo/'scripts/auto_research.py')
            for command in (['git','init','-q'], ['git','add','bot','scripts'], ['git','commit','-qm','fixture']):
                subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)
        stores = [self.derived/(page.parent.name+'-baseline'), self.derived/(page.parent.name+'-candidate')]
        ids = []
        for store in stores:
            cmd = [sys.executable, str(repo/'scripts/auto_research.py'), 'run', '--store', str(store),
                   '--inputs', str(page.parent/'inputs.jsonl'), '--outcomes', str(page.parent/'outcomes.jsonl'),
                   '--idea', 'fixture', '--max-input-bytes', '65536', '--max-outcome-bytes', '1']
            run = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            ids.append(json.loads(run.stdout)['experiment_id'])
        cmd = [sys.executable, str(ROOT/'scripts/auto_research.py'), 'compare', '--store', str(stores[0]),
               '--candidate-store', str(stores[1]), *ids]
        run = subprocess.run(cmd, cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        file = self.derived / (page.parent.name + '-compare.json')
        file.write_text(run.stdout)
        self.comparison_stores[file] = stores
        return file

    def call(self, *args):
        return subprocess.run([sys.executable,str(SCRIPT),*map(str,args)], cwd=ROOT, env=self.env, capture_output=True,text=True)

    def do_ack(self, page, comparison=None):
        args=['ack','--page',page,'--ack-dir',self.ack]
        if comparison:
            stores = self.comparison_stores[comparison]
            args += ['--comparison',comparison,'--baseline-store',stores[0],'--candidate-store',stores[1]]
        return self.call(*args)

    def do_finish(self,last,previous=None,previous_checkpoint=None):
        args=['finish','--last-page',last,'--ack-dir',self.ack,'--output',self.checkpoint]
        if previous: args += ['--previous',previous]
        if previous_checkpoint: args += ['--previous-checkpoint',previous_checkpoint]
        return self.call(*args)

    def test_all_pages_are_acknowledged_before_checkpoint(self):
        previous=self.page(start=20,end=0)
        self.assertEqual(self.do_ack(previous,self.comparison(previous)).returncode,0)
        self.assertEqual(self.do_finish(previous).returncode,0)
        prior_proof=self.checkpoint
        self.checkpoint=self.derived/'next-checkpoint.json'
        first=self.page(previous=previous,start=100,end=60,complete=False,checkpoint=False)
        second=self.page(previous=previous,resume=first,start=60,end=20,complete=True)
        comp1, comp2 = self.comparison(first), self.comparison(second)
        self.assertEqual(self.do_ack(first,comp1).returncode,0)
        missing=self.do_finish(second,previous,prior_proof)
        self.assertNotEqual(missing.returncode,0)
        self.assertIn('unacknowledged',missing.stderr)
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(self.do_ack(second,comp2).returncode,0)
        finish=self.do_finish(second,previous,prior_proof)
        self.assertEqual(finish.returncode,0,finish.stderr)
        result=json.loads(self.checkpoint.read_text())
        self.assertEqual(result['last_page_sha256'],digest(second.read_bytes()))
        self.assertEqual(result['page_count'],2)
        self.assertEqual(result['coverage'],'complete_delta')

    def test_same_experiment_id_cannot_acknowledge_a_page(self):
        page = self.page()
        comparison = self.comparison(page)
        value = json.loads(comparison.read_bytes())
        value['runs'][1]['experiment_id'] = value['runs'][0]['experiment_id']
        comparison.write_bytes(encoded(value))
        result = self.do_ack(page, comparison)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(list(self.ack.iterdir()))

    def test_comparison_must_match_page_and_empty_outcomes(self):
        page=self.page()
        comp=self.comparison(page)
        value=json.loads(comp.read_bytes()); value['input_sha256']='a'*64
        comp.write_bytes(encoded(value))
        failed=self.do_ack(page,comp)
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('comparison',failed.stderr)
        self.assertEqual(list(self.ack.iterdir()),[])

    def test_ack_retry_and_existing_malformed_output_are_safe(self):
        from unittest.mock import patch
        from scripts import research_feed_progress as progress
        page = self.page(data=b'')
        with patch.object(progress, 'sync_directory', side_effect=OSError('injected directory failure')):
            with self.assertRaisesRegex(OSError, 'may exist'):
                progress.ack(page, self.ack, None, self.derived, 1024)
        result = progress.ack(page, self.ack, None, self.derived, 1024)
        self.assertTrue(result['reused'])
        target = Path(result['ack_path'])
        target.write_bytes(b'{malformed')
        with self.assertRaisesRegex(ValueError, 'existing output'):
            progress.ack(page, self.ack, None, self.derived, 1024)
        self.assertEqual(target.read_bytes(), b'{malformed')

    def test_checkpoint_cannot_occupy_future_acknowledgment_path(self):
        from scripts import research_feed_progress as progress
        page = self.page(data=b'')
        progress.ack(page, self.ack, None, self.derived, 1024)
        other = self.ack / ('f' * 64 + '.json')
        with self.assertRaisesRegex(ValueError, 'acknowledgment'):
            progress.finish(page, None, None, self.ack, other, self.derived, 1024)
        self.assertFalse(other.exists())

    def test_checkpoint_directory_sync_failure_can_be_retried_idempotently(self):
        from unittest.mock import patch
        from scripts import research_feed_progress as progress
        import stat
        page = self.page(data=b'')
        progress.ack(page, self.ack, None, self.derived, 1024)
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('injected directory fsync failure')
            real_fsync(fd)
        with patch.object(progress.os, 'fsync', side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, 'directory fsync failure'):
                progress.finish(page, None, None, self.ack, self.checkpoint, self.derived, 1024)
            with self.assertRaisesRegex(OSError, 'directory fsync failure'):
                progress.finish(page, None, None, self.ack, self.checkpoint, self.derived, 1024)
        self.assertTrue(self.checkpoint.exists())
        saved = self.checkpoint.read_bytes()
        progress.finish(page, None, None, self.ack, self.checkpoint, self.derived, 1024)
        self.assertEqual(self.checkpoint.read_bytes(), saved)
        self.checkpoint.write_bytes(b'{malformed')
        with self.assertRaisesRegex(ValueError, 'existing output'):
            progress.finish(page, None, None, self.ack, self.checkpoint, self.derived, 1024)
        self.assertEqual(self.checkpoint.read_bytes(), b'{malformed')

    def test_existing_zero_ack_can_be_retried_without_replacement(self):
        from scripts import research_feed_progress as progress
        page = self.page(data=b'')
        page_hash = digest(page.read_bytes())
        old = {'schema_name':'research_feed_page_ack', 'schema_version':1, 'page_sha256':page_hash,
               'status':'no_usable_inputs', 'comparison_path':None, 'comparison_sha256':None}
        target = self.ack / (page_hash + '.json')
        target.write_text(json.dumps(old, sort_keys=True, indent=2) + '\n')
        before = target.read_bytes()
        self.assertTrue(progress.ack(page, self.ack, None, self.derived, 1024)['reused'])
        self.assertEqual(target.read_bytes(), before)
        progress.finish(page, None, None, self.ack, self.checkpoint, self.derived, 1024)

    def test_zero_accepted_page_may_be_acknowledged_without_experiment(self):
        page=self.page(data=b'')
        run=self.do_ack(page)
        self.assertEqual(run.returncode,0,run.stderr)
        finish=self.do_finish(page)
        self.assertEqual(finish.returncode,0,finish.stderr)
        result=json.loads(self.checkpoint.read_text())
        self.assertEqual(result['page_count'],1)
        self.assertEqual(result['accepted_rows'],0)

    def test_unacknowledged_previous_checkpoint_cannot_be_trusted(self):
        previous=self.page(start=20,end=0)
        page=self.page(previous=previous,start=100,end=20,complete=True)
        self.assertEqual(self.do_ack(page,self.comparison(page)).returncode,0)
        result=self.do_finish(page,previous)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('previous checkpoint proof',result.stderr)
        self.assertFalse(self.checkpoint.exists())

    def test_invalid_page_extent_is_not_acknowledged(self):
        page=self.page(start=20,end=30,complete=True)
        failed=self.do_ack(page,self.comparison(page))
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('page extent',failed.stderr)
        self.assertEqual(list(self.ack.iterdir()),[])

    def test_real_synthetic_feed_pages_can_be_acknowledged_in_order(self):
        source=self.root/'source'; source.mkdir()
        raw,index,manifest = (source/'raw.jsonl', source/'index.jsonl', source/'manifest.json')
        row=_legacy_collector_row(); row['run_id']='first'
        raw.write_text(json.dumps(row)+'\n')
        build_collector_replay_index(raw,index,manifest)
        feed_cmd=[sys.executable,str(ROOT/'scripts/fresh_research_feed.py'),'--index',str(index),
            '--manifest',str(manifest),'--output-root',str(self.feed),'--protected-root',str(source),
            '--max-index-bytes','2000000','--max-inspected-rows','1','--max-raw-row-bytes','200000',
            '--max-wall-seconds','30','--max-memory-bytes','536870912']
        def invoke_feed(extra):
            proc=subprocess.run(feed_cmd+list(map(str,extra)),cwd=ROOT,env=self.env,capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            return Path(json.loads(proc.stdout)['directory'])/'receipt.json'
        previous=invoke_feed([])
        self.assertEqual(self.do_ack(previous,self.comparison(previous)).returncode,0)
        self.assertEqual(self.do_finish(previous).returncode,0)
        prior_proof=self.checkpoint
        self.checkpoint=self.derived/'next-checkpoint.json'
        with raw.open('a') as f:
            for name in ('second','third'):
                row=_legacy_collector_row(); row['run_id']=name
                f.write(json.dumps(row)+'\n')
        update_collector_replay_index(raw,index,manifest)
        first=invoke_feed(['--previous',previous])
        second=invoke_feed(['--previous',previous,'--resume',first])
        self.assertEqual(self.do_ack(first,self.comparison(first)).returncode,0)
        self.assertEqual(self.do_ack(second,self.comparison(second)).returncode,0)
        result=self.do_finish(second,previous,prior_proof)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(self.checkpoint.read_text())['page_count'],2)

    def test_tampered_ack_or_page_cannot_advance_checkpoint(self):
        page=self.page()
        comp=self.comparison(page)
        self.assertEqual(self.do_ack(page,comp).returncode,0)
        comp.write_bytes(comp.read_bytes()+b' ')
        failed=self.do_finish(page)
        self.assertNotEqual(failed.returncode,0)
        self.assertFalse(self.checkpoint.exists())


if __name__ == '__main__':
    unittest.main()
