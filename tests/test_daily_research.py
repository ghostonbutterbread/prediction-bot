import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/daily_research.py'
GUARD = SCRIPT.parent / 'daily_research_guard.sh'

class DailyResearchTest(unittest.TestCase):
    def test_guard_locks_job_and_reconciles_only_owned_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; work.mkdir()
            fake = root / 'docker'
            fake.write_text('#!/bin/sh\ncase "$1 $2" in\n  "container ls") if test -e "$FAKE_OWNED"; then printf "%s\\n" "$FAKE_NAME"; fi;;\n  "inspect --format") printf "%s\\n" "$FAKE_NAME";;\n  "rm -f") test "$3" = "$FAKE_NAME" || exit 9; rm "$FAKE_OWNED";;\n  *) exit 8;;\nesac\n')
            fake.chmod(0o755)
            name = 'daily-research-' + hashlib.sha256(str(work.resolve()).encode()).hexdigest()[:20]
            marker = root / 'container'; marker.write_text('owned')
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_NAME=name, FAKE_OWNED=str(marker))
            child = root / 'child.py'
            child.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[1]); p.write_text("ready")\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.02)\n')
            ready = root / 'ready'; release = root / 'release'
            command = ['bash', str(GUARD), str(work), '10', sys.executable, str(child), str(ready), str(release)]
            process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import time
                for _ in range(200):
                    if ready.exists() or process.poll() is not None: break
                    time.sleep(.01)
                self.assertTrue(ready.exists(), process.communicate(timeout=1) if process.poll() is not None else 'guard did not start')
                self.assertFalse(marker.exists(), 'stale owned container must be removed first')
                second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn('lock', second.stderr.lower())
                marker.write_text('owned')
                release.write_text('go')
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertFalse(marker.exists(), 'exit cleanup must remove owned container')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_completion_requires_bound_receipts_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'inputs'; source.write_text('{"x":1}\n')
            outcome = root / 'outcomes'; outcome.write_text('later outcome')
            catalog = root / 'catalog'; catalog.write_text(json.dumps([{'inputs': str(source), 'outcomes': str(outcome)}]))
            limits = ['--max-input-bytes', '1000', '--max-outcome-bytes', '1000']
            command = [sys.executable, str(SCRIPT), 'select', '--catalog', str(catalog), '--state', str(root / 'state'), *limits]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            selected = json.loads(result.stdout); directory = Path(selected['directory'])
            done = [sys.executable, str(SCRIPT), 'complete', '--directory', str(directory), *limits]
            missing = subprocess.run(done, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse((directory / 'complete.json').exists())
            receipts = [{'experiment_id': 'a', 'git_commit': 'base'}, {'experiment_id': 'b', 'git_commit': 'candidate'}]
            for name, receipt in zip(('baseline.json', 'candidate.json'), receipts):
                (directory / name).write_text(json.dumps(receipt))
            comparison = {'runs': receipts, 'selected_rows_matched': True, 'diagnostic_eligible': False,
                          'input_sha256': selected['input_sha256'], 'outcome_sha256': hashlib.sha256(b'').hexdigest()}
            (directory / 'comparison.json').write_text(json.dumps(comparison))
            comparison['outcome_sha256'] = selected['outcome_sha256']
            (directory / 'comparison.json').write_text(json.dumps(comparison))
            bad = subprocess.run(done, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertFalse((directory / 'complete.json').exists())
            comparison['outcome_sha256'] = hashlib.sha256(b'').hexdigest()
            (directory / 'comparison.json').write_text(json.dumps(comparison))
            self.assertEqual(subprocess.run(done, capture_output=True, text=True).returncode, 0)
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['status'], 'no_new_data')
            source.write_text('{"x":2}\n')
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['status'], 'selected')
            source.write_text('{"x":1}\n')
            (directory / 'candidate.json').write_text('{}')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('hash mismatch', result.stderr)

    def test_guard_refuses_mismatched_container_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; work.mkdir()
            name = 'daily-research-' + hashlib.sha256(str(work.resolve()).encode()).hexdigest()[:20]
            fake = root / 'docker'
            fake.write_text('#!/bin/sh\ncase "$1 $2" in\n "container ls") printf "%s\\n" "$FAKE_NAME";;\n "inspect --format") printf "someone-else\\n";;\n *) exit 42;;\nesac\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_NAME=name)
            marker = root / 'child-ran'
            result = subprocess.run(['bash', str(GUARD), str(work), '5', sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(marker)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('owner label mismatch', result.stderr)
            self.assertFalse(marker.exists())

    def test_guard_interrupt_cleans_owned_container(self):
        import signal
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; work.mkdir()
            name = 'daily-research-' + hashlib.sha256(str(work.resolve()).encode()).hexdigest()[:20]
            marker = root / 'container'; ready = root / 'ready'
            fake = root / 'docker'
            fake.write_text('#!/bin/sh\ncase "$1 $2" in\n "container ls") if test -e "$FAKE_OWNED"; then printf "%s\\n" "$FAKE_NAME"; fi;;\n "inspect --format") printf "%s\\n" "$FAKE_NAME";;\n "rm -f") rm "$FAKE_OWNED";;\n *) exit 8;;\nesac\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_NAME=name, FAKE_OWNED=str(marker))
            child = root / 'child.py'
            child.write_text('import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text("ready")\npathlib.Path(sys.argv[2]).write_text("owned")\ntime.sleep(30)\n')
            process = subprocess.Popen(['bash', str(GUARD), str(work), '40', sys.executable, str(child), str(ready), str(marker)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(200):
                    if marker.exists() or process.poll() is not None: break
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse(marker.exists())
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_catalog_and_aggregate_budgets_fail_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); data = root / 'data'; data.write_text('1234')
            outcome = root / 'outcome'; outcome.write_text('5678')
            catalog = root / 'catalog'; catalog.write_text(json.dumps([{'inputs': str(data), 'outcomes': str(outcome)}]))
            command = [sys.executable, str(SCRIPT), 'select', '--catalog', str(catalog), '--state', str(root / 'state'), '--max-input-bytes', '20', '--max-outcome-bytes', '20']
            for options in (['--max-catalog-bytes', '1'], ['--max-total-bytes', '7']):
                result = subprocess.run(command + options, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'state').exists())

    def test_select_incrementally_and_resume_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'inputs.jsonl'; data.write_text('{"input":1}\n')
            outcomes = root / 'outcomes.jsonl'; outcomes.write_text('')
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps([{'inputs': str(data), 'outcomes': str(outcomes)}]))
            state = root / 'state'
            command = [sys.executable, str(SCRIPT), 'select', '--catalog', str(catalog), '--state', str(state), '--max-input-bytes', '1000', '--max-outcome-bytes', '1000']
            def select():
                p = subprocess.run(command, text=True, capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                return json.loads(p.stdout)
            first = select()
            self.assertEqual(first['status'], 'selected')
            run = Path(first['directory'])
            self.assertEqual((run / 'inputs.jsonl').read_bytes(), data.read_bytes())
            self.assertEqual(select()['status'], 'in_progress')
            self.assertEqual((run / 'inputs.jsonl').read_bytes(), data.read_bytes())
            (run / 'complete.json').write_text('{}')
            corrupt = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(corrupt.returncode, 0)
            self.assertIn('completion', corrupt.stderr.lower())
            (run / 'complete.json').unlink()
            (run / 'inputs.jsonl').write_text('tampered')
            corrupt = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(corrupt.returncode, 0)
            self.assertIn('hash', corrupt.stderr.lower())

    def test_missing_and_oversize_inputs_are_errors_not_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.write_text('1234')
            catalog = root / 'catalog'; catalog.write_text(json.dumps([{'inputs': str(source), 'outcomes': str(root / 'missing')}]))
            cmd = [sys.executable, str(SCRIPT), 'select', '--catalog', str(catalog), '--state', str(root / 'state'), '--max-input-bytes', '3', '--max-outcome-bytes', '3']
            p = subprocess.run(cmd, text=True, capture_output=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('error', p.stderr.lower())
            self.assertFalse((root / 'state').exists())

if __name__ == '__main__': unittest.main()
