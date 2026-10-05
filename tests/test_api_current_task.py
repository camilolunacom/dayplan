"""Current-task JSON contract through the real API and SQLite store."""
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from fastapi.testclient import TestClient
from dayplan import api, store
from dayplan.db import connect
from dayplan.config import Config
from dayplan.providers.base import RemoteTask


def remote(external_id, title=None, **extra):
    return RemoteTask(source="ticktick", external_id=external_id,
                      title=title or f"Task {external_id}", **extra)


class CurrentTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dayplan-current-task-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.cfg = Config(
            db_path=directory / "dayplan.sqlite", ticktick_token="test-token",
            ticktick_token_source="TICKTICK_TOKEN", asana_token=None,
            asana_workspaces=(), asana_projects=(), asana_sections=(),
            asana_only_mine=True, asana_include_subtasks=True,
            ticktick_due_within_days=None, ticktick_include_undated=True,
            jira_base_url=None, jira_site_url=None, jira_email=None,
            jira_api_token=None, jira_jql="", sync_interval_minutes=0,
            toggl_project_map=directory / "toggl-projects.json",
        )
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

    def sync_with(self, tasks):
        with patch("dayplan.store.fetch_all", return_value=({"ticktick": tasks}, {})):
            return store.sync(self.cfg, ["ticktick"], trigger="cli")

    def test_returns_only_first_manual_task_metadata_as_json_without_cache(self):
        self.sync_with([
            remote('A', title='Second'),
            remote('B', title='Diseñar café ☕', project='Private project',
                   url='https://example.com/private', notes='Private notes',
                   tags=['private'], raw={'private': 'provider data'}),
        ])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:B', 'ticktick:A'])
        response = self.client.get('/api/current-task')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'application/json')
        self.assertEqual(response.json(), {
            'schema_version': 1,
            'task': {
                'id': 'ticktick:B',
                'title': 'Diseñar café ☕',
                'project': 'Private project',
                'toggl_project': None,
                'toggl_project_id': None,
            },
        })
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_reordering_changes_current_task_without_mutating_on_get(self):
        self.sync_with([remote('A'), remote('B')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A', 'ticktick:B'])
        before = self.client.get('/api/state').json()
        self.assertEqual(self.client.get('/api/current-task').json()['task']['id'], 'ticktick:A')
        self.assertEqual(self.client.get('/api/state').json(), before)
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:B', 'ticktick:A'])
        self.assertEqual(self.client.get('/api/current-task').json()['task']['id'], 'ticktick:B')

    def test_closed_task_is_skipped_and_acknowledged_tail_matches_ui(self):
        self.sync_with([remote('A'), remote('B')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A'])
            store.acknowledge(conn, 'ticktick:B')
            conn.execute("UPDATE tasks SET closed = 1 WHERE id = ?", ('ticktick:A',))
            conn.commit()
        self.assertEqual(self.client.get('/api/current-task').json()['task']['id'], 'ticktick:B')

    def test_write_methods_are_not_allowed(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.assertEqual(self.client.request(method, '/api/current-task').status_code, 405)

    def test_unmapped_task_has_null_project_fields(self):
        self.sync_with([remote('A')])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A'])
        self.assertEqual(self.client.get('/api/current-task').json(), {
            'schema_version': 1,
            'task': {
                'id': 'ticktick:A',
                'title': 'Task A',
                'project': None,
                'toggl_project': None,
                'toggl_project_id': None,
            },
        })

    def test_mapped_task_preserves_configured_name_and_id_without_guessing(self):
        self.cfg.toggl_project_map.write_text(json.dumps({
            'ticktick': [{
                'title_contains': 'Mapped task',
                'toggl_project': 'Exact Toggl project',
                'toggl_project_id': 123456,
            }],
        }), encoding='utf-8')
        self.sync_with([
            remote('A', title='Mapped task', project='Different source project'),
            remote('B', title='Task without mapping', project='Exact Toggl project'),
        ])
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:A', 'ticktick:B'])
        self.assertEqual(self.client.get('/api/current-task').json()['task'], {
            'id': 'ticktick:A',
            'title': 'Mapped task',
            'project': 'Different source project',
            'toggl_project': 'Exact Toggl project',
            'toggl_project_id': 123456,
        })
        with closing(connect(self.cfg.db_path)) as conn:
            store.set_order(conn, ['ticktick:B', 'ticktick:A'])
        task = self.client.get('/api/current-task').json()['task']
        self.assertEqual(task['project'], 'Exact Toggl project')
        self.assertIsNone(task['toggl_project'])
        self.assertIsNone(task['toggl_project_id'])

    def test_empty_plan_returns_204_even_when_new_tasks_exist(self):
        self.sync_with([remote('NEW')])
        response = self.client.get('/api/current-task')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertEqual(response.headers['cache-control'], 'no-store')
