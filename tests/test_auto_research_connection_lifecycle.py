import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import auto_research


class AutoResearchConnectionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'derived' / 'research'
        self.store.mkdir(parents=True)
        with closing(auto_research.database(self.store)) as db, db:
            db.execute('INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('existing', 'now', 'idea', 'commit', 'tree', 'input', 'outcome', '{}', '{}', ''))
        self.opened = []
        self.fault = None
        self.real_connect = sqlite3.connect

    def connect(self, *args, **kwargs):
        owner = self
        class ControlledConnection(sqlite3.Connection):
            def execute(self, sql, *parameters):
                result = super().execute(sql, *parameters)
                if owner.fault and sql.startswith(owner.fault):
                    raise RuntimeError('injected SQL failure')
                return result

            def executescript(self, sql):
                if owner.fault == 'schema':
                    raise RuntimeError('injected schema failure')
                return super().executescript(sql)
        kwargs['factory'] = ControlledConnection
        db = self.real_connect(*args, **kwargs)
        self.opened.append(db)
        return db

    def invoke(self, args):
        with patch('bot.collector_paths.derived_reports_root', return_value=self.store.parent), \
             patch.object(auto_research.sqlite3, 'connect', side_effect=self.connect), \
             patch.object(sys, 'argv', ['auto_research', *args]), redirect_stdout(StringIO()):
            return auto_research.main()

    def assert_closed(self):
        self.assertTrue(self.opened)
        for db in self.opened:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                db.execute('SELECT 1')
        self.opened.clear()

    def queue_rows(self):
        with closing(self.real_connect(self.store / 'experiments.sqlite3')) as db:
            return db.execute('SELECT experiment_id FROM candidate_queue').fetchall()

    def test_queue_closes_and_preserves_commit_and_rollback(self):
        args = ['queue', '--store', str(self.store), '--experiment', 'existing', '--note', 'review']
        self.assertEqual(self.invoke(args), 0)
        self.assert_closed()
        self.assertEqual(self.queue_rows(), [('existing',)])
        with closing(self.real_connect(self.store / 'experiments.sqlite3')) as db, db:
            db.execute('INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('second', 'now', 'idea', 'commit', 'tree', 'input', 'outcome', '{}', '{}', ''))
        self.fault = 'INSERT INTO candidate_queue'
        args[args.index('existing')] = 'second'
        with self.assertRaisesRegex(RuntimeError, 'injected SQL failure'):
            self.invoke(args)
        self.assert_closed()
        self.assertEqual(self.queue_rows(), [('existing',)])

    def test_database_initialization_failure_closes_connection(self):
        self.fault = 'schema'
        with patch.object(auto_research.sqlite3, 'connect', side_effect=self.connect):
            with self.assertRaisesRegex(RuntimeError, 'injected schema failure'):
                auto_research.database(self.store)
        self.assert_closed()

    def test_compare_closes_connections_on_query_success_and_failure(self):
        # A successful query followed by unknown-experiment validation still
        # exercises both read transactions without fabricating result rows.
        args = ['compare', '--store', str(self.store), 'absent-one', 'absent-two']
        with self.assertRaisesRegex(ValueError, 'unknown experiment'):
            self.invoke(args)
        self.assertEqual(len(self.opened), 2)
        self.assert_closed()
        self.fault = 'SELECT git_commit'
        with self.assertRaisesRegex(RuntimeError, 'injected SQL failure'):
            self.invoke(args)
        self.assertEqual(len(self.opened), 1)
        self.assert_closed()

    def test_run_closes_and_preserves_commit_and_rollback(self):
        inputs, outcomes = self.root / 'inputs', self.root / 'outcomes'
        inputs.write_text('')
        outcomes.write_text('')
        args = ['run', '--store', str(self.store), '--inputs', str(inputs), '--outcomes', str(outcomes),
                '--idea', 'synthetic', '--max-input-bytes', '10', '--max-outcome-bytes', '10']

        def git(*args):
            if args[:2] == ('rev-parse', '--show-toplevel'):
                return str(auto_research.ROOT)
            if args[0] == 'status':
                return ''
            return 'synthetic-commit'

        def replay(**kwargs):
            kwargs['output_dir'].mkdir(parents=True)
            return SimpleNamespace(metadata={'decision_stats': {}, 'resolution_summary': {}})

        with patch.object(auto_research, 'git', side_effect=git), \
             patch('bot.weather.collector_source_router_replay.run_collector_source_router_replay', side_effect=replay):
            self.assertEqual(self.invoke(args), 0)
            self.assert_closed()
            with closing(self.real_connect(self.store / 'experiments.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM experiments').fetchone()[0], 2)
            self.fault = 'INSERT INTO experiments'
            with self.assertRaisesRegex(RuntimeError, 'injected SQL failure'):
                self.invoke(args)
            self.assert_closed()
            with closing(self.real_connect(self.store / 'experiments.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM experiments').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
