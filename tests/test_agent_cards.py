"""Exercise the real CLI against an isolated, deliberately strict HTTP server."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / 'agent_cards.py'


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}
        self.comments = {}
        self.labels = {}
        self.next_comment = 1
        self.next_label = 1
        self.requests = []
        self.updates = []
        self.projects = [{'id': 1, 'title': 'Demo'}, {'id': 2, 'title': 'Other'}]
        self.claim_barrier = None
        self.claim_reads = 0
        self.before_claim = None
        self.before_progress = None
        self.fail_labels = False
        self.fail_updates = False

    def comment(self, tid, text, created=None):
        row = {'id': self.next_comment, 'comment': text,
               'created': created or datetime.now(timezone.utc).isoformat()}
        self.next_comment += 1
        self.comments.setdefault(tid, []).append(row)
        return deepcopy(row)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        store = self.server.store
        split = urlsplit(self.path)
        parts = split.path.removeprefix('/api/v1').strip('/').split('/')
        query = parse_qs(split.query)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or 'null')
        barrier = None
        headers = {}
        with store.lock:
            store.requests.append((self.command, split.path, deepcopy(body)))
            if self.headers.get('Authorization') != 'Bearer test-token':
                status, data = 401, {'message': 'Unauthorized'}
            elif parts == ['projects'] and self.command == 'GET':
                status, data = 200, store.projects
            elif parts == ['labels']:
                if self.command == 'GET':
                    status, data = 200, list(store.labels.values())
                elif store.fail_labels:
                    status, data = 403, {}
                else:
                    row = {'id': store.next_label, 'title': body['title']}
                    store.next_label += 1
                    store.labels[row['id']] = row
                    status, data = 201, row
            elif len(parts) == 3 and parts[0] == 'projects' and parts[2] == 'tasks':
                tid = max(store.tasks, default=0) + 1
                row = dict(body, id=tid, project_id=int(parts[1]), done=False, labels=[], priority=2)
                store.tasks[tid] = row
                store.comments[tid] = []
                status, data = 201, row
            elif parts == ['tasks']:
                status, data = 200, [t for t in store.tasks.values() if not t['done']]
            elif len(parts) >= 2 and parts[0] == 'tasks' and parts[1].isdigit():
                tid = int(parts[1])
                task = store.tasks.get(tid)
                if task is None:
                    status, data = 404, {}
                elif len(parts) == 2:
                    if self.command == 'POST' and store.fail_updates:
                        status, data = 500, {}
                    elif self.command == 'POST':
                        # Model v1's destructive partial update behavior.
                        store.updates.append(deepcopy(body))
                        updated = {'description': '', 'due_date': '', 'labels': [], 'priority': 0}
                        updated.update(body)
                        store.tasks[tid] = updated
                        task = updated
                        status, data = 200, task
                    else:
                        status, data = 200, task
                elif parts[2] == 'comments':
                    if self.command == 'GET':
                        status, data = 200, deepcopy(store.comments[tid])
                        if store.claim_barrier is not None and store.claim_reads < 2:
                            store.claim_reads += 1
                            barrier = store.claim_barrier
                    else:
                        protocol_text = body['comment'].replace('&quot;', '"')
                        if store.before_claim and '"kind":"claim"' in protocol_text:
                            hook, store.before_claim = store.before_claim, None
                            hook(store, tid)
                        if store.before_progress and any('"kind":"' + kind + '"' in protocol_text
                                                         for kind in ('note', 'block', 'result')):
                            hook, store.before_progress = store.before_progress, None
                            hook(store, tid)
                        status, data = 201, store.comment(tid, body['comment'])
                elif parts[2] == 'labels':
                    label = store.labels[body['label_id']]
                    if label not in task['labels']:
                        task['labels'].append(label)
                    status, data = 200, label
                else:
                    status, data = 404, {}
            else:
                status, data = 404, {}
            data = deepcopy(data)
        # Hold both initial snapshots until two independent CLI processes read.
        if barrier:
            barrier.wait(timeout=10)
        if isinstance(data, list):
            # Comments are unpaginated in v1. Others deliberately span pages.
            if not (len(parts) > 2 and parts[2] == 'comments'):
                size = 2
                page = int(query.get('page', ['1'])[0])
                headers['x-pagination-total-pages'] = str((len(data) + size - 1) // size)
                data = data[(page - 1) * size:page * size]
            elif len(data) > 1:
                data.reverse()  # Replay must use IDs, never response order.
        encoded = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)


class AgentCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.store = Store()
        self.env = dict(os.environ, VIKUNJA_URL=f'http://127.0.0.1:{self.server.server_port}',
                        VIKUNJA_TOKEN='test-token', AGENT_CARDS_PROJECT='Demo',
                        PYTHONDONTWRITEBYTECODE='1')

    @property
    def store(self):
        return self.server.store

    def cli(self, *args, code=0, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                env=env or self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def add(self, agent='agent-07', title='Write guide', **kwargs):
        tid = max(self.store.tasks, default=0) + 1
        self.cli('add', agent, title, '--due', kwargs.pop('due', '2030-12-20'),
                 '--note', 'Keep <this> description\nand its second line.',
                 *sum(([key, value] for key, value in kwargs.items()), []))
        return tid

    def take(self, tid=1, agent='agent-07'):
        return self.cli('take', tid, '--as', agent)

    def note(self, text='확정: Draft saved in docs/guide.md', agent='agent-07', tid=1):
        return self.cli('note', tid, text, '--as', agent)

    def test_add_requires_valid_due_before_network(self):
        self.cli('add', 'agent-07', 'A task', code=2)
        for due in ('2030-02-30', '2030-1-01', '0001-01-01'):
            self.cli('add', 'agent-07', 'A task', '--due', due, code=2)
        self.assertEqual(self.store.requests, [])

    def test_add_uses_default_project_label_note_and_utc_deadline(self):
        tid = self.add()
        task = self.store.tasks[tid]
        self.assertEqual(task['project_id'], 1)
        self.assertEqual(task['labels'][0]['title'], 'agent:agent-07')
        self.assertEqual(task['due_date'], '2030-12-20T23:59:59+00:00')
        self.assertIn('&lt;this&gt;', task['description'])
        self.assertIn('<br>', task['description'])

    def test_project_override_id_and_paginated_title_resolution(self):
        self.store.projects.append({'id': 3, 'title': 'Third'})
        self.add(**{'--project': 'Third'})
        self.assertEqual(self.store.tasks[1]['project_id'], 3)
        self.add(**{'--project': '2'})
        self.assertEqual(self.store.tasks[2]['project_id'], 2)
        self.store.projects.append({'id': 4, 'title': 'Demo'})
        self.cli('add', 'agent-07', 'Task', '--due', '2030-01-01', code=1)

    def test_two_processes_take_only_one_succeeds(self):
        self.add()
        self.store.claim_barrier = threading.Barrier(2)
        def run(agent):
            return subprocess.run([sys.executable, str(SCRIPT), 'take', '1', '--as', agent],
                                  env=self.env, text=True, capture_output=True, timeout=15)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ['agent-07', 'agent-08']))
        self.assertEqual(sorted(result.returncode for result in results), [0, 3],
                         [(r.stdout, r.stderr) for r in results])
        winner = 'agent-07' if results[0].returncode == 0 else 'agent-08'
        loser = 'agent-08' if winner == 'agent-07' else 'agent-07'
        self.note(agent=winner)
        self.cli('note', 1, 'Should fail', '--as', loser, code=3)
        self.assertEqual(len(self.store.comments[1]), 3)

    def test_owner_can_refresh_and_other_agent_is_rejected(self):
        self.add()
        self.take()
        self.take()
        self.cli('take', 1, '--as', 'agent-08', code=3)
        self.assertEqual(len(self.store.comments[1]), 2)

    def test_note_requires_owner_and_resumes_last_checkpoint_line(self):
        self.add()
        self.cli('note', 1, '확정: No owner', '--as', 'agent-07', code=3)
        self.take()
        self.note('확정: First step\nExtra details\n확정: Second step at docs/guide.md')
        self.note('Ordinary progress message')
        result = self.cli('resume', '--as', 'agent-07')
        self.assertIn('확정: Second step at docs/guide.md', result.stdout)
        self.assertNotIn('확정: First step', result.stdout)
        self.assertEqual(self.cli('resume', '--as', 'agent-08').stdout.strip(), 'No open cards.')
        self.note('Confirmed: Reviewed guide.md')
        self.assertIn('Confirmed: Reviewed guide.md', self.cli('resume', '--as', 'agent-07').stdout)

    def test_force_take_transfers_ownership_and_keeps_checkpoint(self):
        self.add()
        self.take()
        self.note()
        output = self.cli('take', 1, '--as', 'agent-08', '--force').stdout
        self.assertIn('확정: Draft saved', output)
        for command, text in [('note', 'Progress'), ('done', 'Saved docs/guide.md'), ('block', 'Need input')]:
            self.cli(command, 1, text, '--as', 'agent-07', code=3)
        self.note(agent='agent-08')
        self.assertEqual(self.cli('resume', '--as', 'agent-07').stdout.strip(), 'No open cards.')

    def test_stale_take_default_six_hours_and_custom_window(self):
        self.add()
        self.take()
        self.store.comments[1][0]['created'] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        self.cli('take', 1, '--as', 'agent-08', code=3)
        self.cli('take', 1, '--as', 'agent-08', '--stale-hours', '4')
        self.store.comments[1][-1]['created'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.take(agent='agent-09')
        self.note(agent='agent-09')

    def test_owner_note_refreshes_idle_time_but_losing_claim_does_not(self):
        self.add()
        self.take()
        self.store.comments[1][0]['created'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.note()
        self.cli('take', 1, '--as', 'agent-08', code=3)

    def test_stale_handoff_fails_if_owner_returns_before_claim_write(self):
        self.add()
        self.take()
        self.store.comments[1][0]['created'] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        def heartbeat(store, tid):
            event = {'kind': 'note', 'agent': 'agent-07', 'claim_id': 1,
                     'text': 'Confirmed: Still working', 'force': False}
            store.comment(tid, 'agent-cards:v1 ' + json.dumps(event))
        self.store.before_claim = heartbeat
        self.cli('take', 1, '--as', 'agent-08', code=3)
        self.assertIn('agent-07', self.cli('list').stdout)

    def test_unknown_claim_time_fails_closed(self):
        self.add()
        self.take()
        self.store.comments[1][0]['created'] = 'not-a-timestamp'
        self.cli('take', 1, '--as', 'agent-08', code=3)
        self.cli('take', 1, '--as', 'agent-08', '--force')

    def test_done_rejects_blank_and_unsubstantiated_results(self):
        self.add()
        self.take()
        self.cli('done', 1, '  ', '--as', 'agent-07', code=2)
        self.cli('done', 1, 'Finished successfully', '--as', 'agent-07', code=1)
        self.assertFalse(self.store.tasks[1]['done'])
        self.assertEqual(len(self.store.comments[1]), 1)

    def test_done_records_evidence_and_full_update_preserves_fields(self):
        self.add()
        before = deepcopy(self.store.tasks[1])
        self.take()
        self.cli('done', 1, 'Saved docs/guide.md; reviewed #42', '--as', 'agent-07')
        after = self.store.tasks[1]
        self.assertTrue(after['done'])
        for key in before:
            if key != 'done':
                self.assertEqual(after[key], before[key], key)
        self.assertIn('Result by agent-07', self.store.comments[1][-1]['comment'])
        self.assertEqual(self.store.updates[0]['description'], before['description'])
        self.assertEqual(self.store.updates[0]['due_date'], before['due_date'])
        self.cli('take', 1, '--as', 'agent-07', code=3)
        self.assertEqual(self.cli('resume', '--as', 'agent-07').stdout.strip(), 'No open cards.')

    def test_block_keeps_card_open_and_records_reason(self):
        self.add()
        self.take()
        self.cli('block', 1, 'Waiting for test fixture #42', '--as', 'agent-07')
        self.assertFalse(self.store.tasks[1]['done'])
        self.assertIn('Blocked by agent-07', self.store.comments[1][-1]['comment'])
        self.assertIn('#1', self.cli('resume', '--as', 'agent-07').stdout)

    def test_force_note_does_not_transfer_ownership(self):
        self.add()
        self.take()
        self.cli('note', 1, 'Confirmed: Reviewed guide.md', '--as', 'agent-08', '--force')
        rows = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual(rows[0]['owner'], 'agent-07')
        self.assertEqual(rows[0]['checkpoint'], 'Confirmed: Reviewed guide.md')
        self.cli('done', 1, 'Reviewed https://example.org/review/42', '--as', 'agent-08', '--force')
        self.assertTrue(self.store.tasks[1]['done'])

    def test_force_is_explicit_exception_on_an_unclaimed_card(self):
        self.add()
        self.cli('note', 1, 'Confirmed: Reviewed guide.md', '--as', 'agent-08', '--force')
        rows = json.loads(self.cli('list', '--json').stdout)
        self.assertIsNone(rows[0]['owner'])
        self.assertEqual(rows[0]['checkpoint'], 'Confirmed: Reviewed guide.md')
        self.cli('done', 1, 'Still no evidence', '--as', 'agent-08', '--force', code=1)
        self.cli('done', 1, 'Reviewed guide.md', '--as', 'agent-08', '--force')
        self.assertTrue(self.store.tasks[1]['done'])

    def test_handoff_during_progress_rejects_old_owner_and_does_not_close(self):
        self.add()
        self.take()
        def handoff(store, tid):
            event = {'kind': 'claim', 'agent': 'agent-08', 'previous': store.comments[tid][-1]['id'],
                     'owner': 'agent-07'}
            store.comment(tid, 'agent-cards:v1 ' + json.dumps(event))
        self.store.before_progress = handoff
        self.cli('done', 1, 'Saved guide.md', '--as', 'agent-07', code=3)
        self.assertFalse(self.store.tasks[1]['done'])
        self.note(agent='agent-08')

    def test_failed_close_leaves_result_and_reports_failure(self):
        self.add()
        self.take()
        self.store.fail_updates = True
        result = self.cli('done', 1, 'Saved guide.md', '--as', 'agent-07', code=1)
        self.assertIn('HTTP 500', result.stderr)
        self.assertFalse(self.store.tasks[1]['done'])
        self.assertIn('Result by agent-07', self.store.comments[1][-1]['comment'])

    def test_done_accepts_common_evidence_formats(self):
        for reference in ('./output', '/tmp/report', 'https://example.org/review/42', '#42',
                          'ticket 42', 'abc1234', 'report.txt', r'C:\output\report'):
            tid = self.add()
            self.take(tid=tid)
            self.cli('done', tid, 'Completed and checked: ' + reference, '--as', 'agent-07')
            self.assertTrue(self.store.tasks[tid]['done'])

    def test_list_filter_overdue_json_pagination_and_scope(self):
        self.add(due='2000-01-01')
        self.take()
        self.note()
        self.add(agent='agent-08', title='Future guide')
        self.add(agent='agent-09', title='Other project', **{'--project': 'Other'})
        self.add(agent='agent-10', title='Unclaimed guide')
        rows = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual([r['id'] for r in rows], [1, 2, 4])
        self.assertEqual(rows[0]['owner'], 'agent-07')
        self.assertEqual(rows[0]['checkpoint'], '확정: Draft saved in docs/guide.md')
        self.assertTrue(rows[0]['overdue'])
        self.assertEqual(len(json.loads(self.cli('list', '--overdue', '--json').stdout)), 1)
        self.assertEqual(len(json.loads(self.cli('list', '--agent', 'agent-08', '--json').stdout)), 1)
        self.assertEqual(json.loads(self.cli('list', '--agent', 'agent-10', '--json').stdout)[0]['owner'], None)
        env = dict(self.env)
        env.pop('AGENT_CARDS_PROJECT')
        self.assertEqual(len(json.loads(self.cli('list', '--json', env=env).stdout)), 4)
        self.cli('add', 'agent-07', 'No project', '--due', '2030-01-01', env=env, code=1)
        self.cli('add', 'agent-07', 'Bad project', '--due', '2030-01-01', '--project', '0', code=1)

    def test_agent_filter_follows_current_owner_after_transfer(self):
        self.add()
        self.take()
        self.cli('take', 1, '--as', 'agent-08', '--force')
        self.assertEqual(json.loads(self.cli('list', '--agent', 'agent-07', '--json').stdout), [])
        self.assertEqual(len(json.loads(self.cli('list', '--agent', 'agent-08', '--json').stdout)), 1)

    def test_html_and_non_protocol_comments_do_not_change_owner_or_checkpoint(self):
        self.add()
        self.take()
        self.note('확정: Compared <draft> & final in guide.md')
        self.store.comment(1, '<p>확정: Unattributed manual text</p>')
        self.store.comment(1, 'agent-cards:v1 {broken JSON')
        self.store.comment(1, 'agent-cards:v1 []')
        rows = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual(rows[0]['owner'], 'agent-07')
        self.assertEqual(rows[0]['checkpoint'], '확정: Compared <draft> & final in guide.md')

    def test_all_comment_history_is_read_beyond_fifty_comments(self):
        self.add()
        self.take()
        for number in range(60):
            self.store.comment(1, f'Manual detail {number}')
        self.note('확정: Latest checkpoint in guide.md')
        self.assertIn('Latest checkpoint', self.cli('resume', '--as', 'agent-07').stdout)

    def test_input_validation_and_missing_config(self):
        self.cli('take', 0, '--as', 'agent-07', code=2)
        self.cli('take', 1, '--as', 'bad name', code=2)
        self.cli('take', 1, code=2)
        for window in ('0', '-1', 'nan', 'inf'):
            self.cli('take', 1, '--as', 'agent-07', '--stale-hours', window, code=2)
        env = dict(self.env, VIKUNJA_TOKEN='')
        self.cli('list', env=env, code=1)
        for url in ('', 'file:///tmp/server', 'http://user:password@example.org', 'https://example.org/api/v2'):
            self.cli('list', env=dict(self.env, VIKUNJA_URL=url), code=1)
        self.assertEqual(self.store.requests, [])

    def test_api_suffix_errors_and_partial_creation_message(self):
        self.add()
        self.cli('list', env=dict(self.env, VIKUNJA_URL=self.env['VIKUNJA_URL'] + '/api/v1'))
        self.cli('take', 999, '--as', 'agent-07', code=1)
        self.store.fail_labels = True
        result = self.cli('add', 'agent-08', 'Second task', '--due', '2030-01-01', code=1)
        self.assertIn('#2 was created', result.stderr)
        self.assertEqual(len(self.store.tasks), 2)


if __name__ == '__main__':
    unittest.main()
