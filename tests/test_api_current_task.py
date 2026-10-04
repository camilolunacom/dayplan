"""Current-task display contract through the real API and SQLite store."""
import unittest
from contextlib import closing

from fastapi.testclient import TestClient
from dayplan import api, store
from dayplan.db import connect
from _support import TempDbCase, remote


class CurrentTaskTests(TempDbCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = api.create_app()

        def override_conn():
            conn = connect(self.cfg.db_path)
            try:
                yield conn
            finally:
                conn.close()

        self.app.dependency_overrides[api.get_conn] = override_conn
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_returns_only_first_manual_task_title_as_utf8_without_cache(self):
        self.sync_with([remote('A', title='Second'), remote('B', title='Diseñar café ☕', project='Private project', url='https://example.com/private')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:B', 'ticktick:A'])
        response = self.client.get('/api/current-task')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, 'Diseñar café ☕'.encode('utf-8'))
        self.assertEqual(response.headers['content-type'], 'text/plain; charset=utf-8')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_reordering_changes_current_task_without_mutating_on_get(self):
        self.sync_with([remote('A'), remote('B')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A', 'ticktick:B'])
        before = self.client.get('/api/state').json()
        self.assertEqual(self.client.get('/api/current-task').text, 'Task A')
        self.assertEqual(self.client.get('/api/state').json(), before)
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:B', 'ticktick:A'])
        self.assertEqual(self.client.get('/api/current-task').text, 'Task B')

    def test_closed_task_is_skipped_and_acknowledged_tail_matches_ui(self):
        self.sync_with([remote('A'), remote('B')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A'])
            store.acknowledge(conn, 'ticktick:B')
            conn.execute("UPDATE tasks SET closed = 1 WHERE id = ?", ('ticktick:A',))
            conn.commit()
        self.assertEqual(self.client.get('/api/current-task').text, 'Task B')

    def test_post_is_not_allowed(self):
        self.assertEqual(self.client.post('/api/current-task').status_code, 405)

    def test_empty_plan_returns_204_even_when_new_tasks_exist(self):
        self.sync_with([remote('NEW')])
        response = self.client.get('/api/current-task')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertEqual(response.headers['cache-control'], 'no-store')
