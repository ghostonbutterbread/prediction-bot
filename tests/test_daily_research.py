import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/daily_research.py'

class DailyResearchTest(unittest.TestCase):
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
            self.assertEqual(select()['status'], 'no_new_data')
            data.write_text('{"input":2}\n')
            self.assertEqual(select()['status'], 'selected')

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
