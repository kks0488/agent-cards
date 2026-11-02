#!/usr/bin/env python3
"""Cooperative task claims, checkpoints, and evidence on Vikunja's v1 API."""

import argparse
from datetime import date, datetime, time, timezone
import html
from html.parser import HTMLParser
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PREFIX = 'agent-cards:v1 '
CHECKPOINT = re.compile(r'^(?:확정|confirmed)\s*[:：]', re.IGNORECASE)
EVIDENCE = re.compile(
    r'https?://[^\s<>]+|(?<!\w)#\d+\b'
    r'|(?:\b(?:issue|ticket|pr|card)\s+\d+\b)'
    r'|(?<!\w)(?:[a-zA-Z]:[\\/]|\.{0,2}/|~/)[\w.-]+(?:[/\\][\w.-]+)*'
    r'|\b[\w.-]+\.(?:md|py|sh|json|csv|tsv|txt|log|png|jpg|pdf|xlsx|html|js|ts|zip)\b'
    r'|\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b', re.IGNORECASE)


class CardError(Exception):
    """An actionable CLI error."""


class Conflict(CardError):
    """The caller must stop working on this card."""


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('br', 'p', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(value or '')
    return ''.join(parser.parts).strip()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat((value or '').replace('Z', '+00:00'))
        if parsed.year <= 1 or parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def agent_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise argparse.ArgumentTypeError('Use 1-64 letters, digits, dots, underscores or hyphens.')
    return value


def nonempty(value):
    if not value.strip():
        raise argparse.ArgumentTypeError('Text must not be empty.')
    return value.strip()


def due_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        day = date.fromisoformat(value)
        if day.year <= 1:
            raise ValueError
        return datetime.combine(day, time(23, 59, 59), timezone.utc).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError('--due must be a valid YYYY-MM-DD date.') from None


def positive_id(value):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('Use a positive card ID.') from None


def stale_hours(value):
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('--stale-hours must be a positive finite number.') from None


class Client:
    def __init__(self):
        base = os.environ.get('VIKUNJA_URL', '').strip().rstrip('/')
        self.token = os.environ.get('VIKUNJA_TOKEN', '').strip()
        try:
            address = urllib.parse.urlsplit(base)
            address.port  # Reject malformed ports before making a request.
        except ValueError:
            raise CardError('Set VIKUNJA_URL to a valid server URL.') from None
        if (address.scheme not in ('http', 'https') or not address.hostname
                or address.username or address.password or address.query or address.fragment):
            raise CardError('Set VIKUNJA_URL to your server URL or its /api/v1 URL.')
        if base.endswith('/api/v2'):
            raise CardError('This client uses /api/v1; set VIKUNJA_URL accordingly.')
        self.base = base if base.endswith('/api/v1') else base + '/api/v1'
        if not self.token:
            raise CardError('Set VIKUNJA_TOKEN to a Vikunja API token.')

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        # Do not retry writes: a timed-out comment may already have been committed.
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return (json.loads(raw) if raw else None), response.headers
        except urllib.error.HTTPError as error:
            # Server error bodies can contain private task data. Keep errors short.
            raise CardError(f'Vikunja returned HTTP {error.code} for {method} {path.split("?")[0]}.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CardError('Cannot reach Vikunja. A write may have succeeded; inspect the card before retrying.') from None
        except (ValueError, UnicodeError):
            raise CardError('Vikunja returned invalid JSON.') from None

    def api(self, method, path, body=None):
        return self.request(method, path, body)[0]

    def pages(self, path):
        page = 1
        while True:
            separator = '&' if '?' in path else '?'
            rows, headers = self.request('GET', f'{path}{separator}page={page}&per_page=50')
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise CardError('Expected a list from Vikunja.')
            yield from rows
            total = headers.get('x-pagination-total-pages')
            try:
                last_page = int(total) if total is not None else page
            except (TypeError, ValueError):
                raise CardError('Vikunja returned invalid pagination metadata.') from None
            if not rows or page >= last_page:
                # Non-paginated endpoints (including v1 comments) return all rows.
                return
            page += 1

    def project(self, value):
        if not value:
            raise CardError('Set AGENT_CARDS_PROJECT or use --project with a project ID or title.')
        if str(value).isdigit():
            if int(value) <= 0:
                raise CardError('Project ID must be positive.')
            return int(value)
        matches = [p for p in self.pages('/projects') if p['title'] == value]
        if len(matches) != 1:
            raise CardError('Project title must match exactly one project; use a numeric ID instead.')
        return matches[0]['id']

    def comment(self, card_id, event, text):
        wire = text + '\n' + PREFIX + json.dumps(event, ensure_ascii=False, separators=(',', ':'))
        result = self.api('PUT', f'/tasks/{card_id}/comments',
                          {'comment': html.escape(wire).replace('\n', '<br>')})
        if not isinstance(result, dict) or not isinstance(result.get('id'), int):
            raise CardError('Vikunja did not return a comment ID; inspect the card before retrying.')
        return result['id']

    def state(self, card_id):
        return claim_info(self.pages(f'/tasks/{card_id}/comments'))

    def update_task(self, card_id, **changes):
        # v1 partial POSTs can clear description and due_date. Always round-trip.
        task = self.api('GET', f'/tasks/{card_id}')
        task.update(changes)
        return self.api('POST', f'/tasks/{card_id}', task)

    def add_label(self, card_id, name):
        matches = [label for label in self.pages('/labels') if label['title'] == name]
        label = matches[0] if matches else self.api('PUT', '/labels', {'title': name})
        self.api('PUT', f'/tasks/{card_id}/labels', {'label_id': label['id']})


def claim_info(comments):
    """Replay immutable comment IDs; failed attempts never acquire ownership.

    A replacement claim compares BOTH the previous comment ID and owner. An
    intervening heartbeat, claim, or manual comment makes that attempt invalid.
    This combines the original release-and-claim idea into one append.
    """
    state = {'owner': None, 'claim_id': None, 'last_activity': None,
             'checkpoint': '', 'last_id': None, 'accepted': set()}
    for comment in sorted(comments, key=lambda c: c['id']):
        text = plain(comment.get('comment'))
        event = None
        for line in text.splitlines():
            if line.startswith(PREFIX):
                try:
                    candidate = json.loads(line[len(PREFIX):])
                    if isinstance(candidate, dict):
                        event = candidate
                except ValueError:
                    pass
        if event:
            kind, actor = event.get('kind'), event.get('agent')
            valid_actor = isinstance(actor, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', actor)
            if (valid_actor and kind == 'claim' and 'previous' in event and 'owner' in event
                    and event['previous'] == state['last_id'] and event['owner'] == state['owner']):
                state.update(owner=actor, claim_id=comment['id'],
                             last_activity=timestamp(comment.get('created')))
                state['accepted'].add(comment['id'])
            elif (valid_actor and kind in ('note', 'block', 'result')
                  and event.get('claim_id') == state['claim_id']
                  and (state['owner'] is not None or event.get('force') is True)
                  and (actor == state['owner'] or event.get('force') is True)):
                state['accepted'].add(comment['id'])
                if actor == state['owner']:
                    state['last_activity'] = timestamp(comment.get('created'))
                if kind == 'note':
                    for line in str(event.get('text', '')).splitlines():
                        if CHECKPOINT.match(line.strip()):
                            state['checkpoint'] = line.strip()
        state['last_id'] = comment['id']
    return state


def open_task(client, card_id):
    task = client.api('GET', f'/tasks/{card_id}')
    if task.get('done'):
        raise Conflict(f'#{card_id} is already closed. Stop working on it.')
    return task


def take(client, args):
    open_task(client, args.id)
    state = client.state(args.id)
    if state['owner'] and state['owner'] != args.agent and not args.force:
        last = state['last_activity']
        age = (datetime.now(timezone.utc) - last).total_seconds() if last else None
        if age is None or age < args.stale_hours * 3600:
            raise Conflict(f'#{args.id} is held by {state["owner"]}. Stop; use --force only for an intentional handoff.')
    event = {'kind': 'claim', 'agent': args.agent,
             'previous': state['last_id'], 'owner': state['owner']}
    cid = client.comment(args.id, event, f'Taken: {args.agent}')
    current = client.state(args.id)
    if cid not in current['accepted'] or current['owner'] != args.agent:
        raise Conflict(f'#{args.id} claim lost. Stop; current owner: {current["owner"] or "none"}.')
    open_task(client, args.id)
    print(f'#{args.id} taken by {args.agent}')
    print(current['checkpoint'] or 'No checkpoint yet.')


def write_progress(client, args):
    if args.command == 'done' and not EVIDENCE.search(args.text):
        raise CardError('Result needs evidence: a file path, URL, #number, ticket number or commit ID.')
    open_task(client, args.id)
    state = client.state(args.id)
    if (state['owner'] is None or state['owner'] != args.agent) and not args.force:
        raise Conflict(f'#{args.id} requires its current owner ({state["owner"] or "unclaimed"}). Run take first.')
    kind = {'done': 'result', 'note': 'note', 'block': 'block'}[args.command]
    event = {'kind': kind, 'agent': args.agent, 'claim_id': state['claim_id'],
             'text': args.text, 'force': args.force}
    heading = {'done': 'Result', 'note': 'Note', 'block': 'Blocked'}[args.command]
    cid = client.comment(args.id, event, f'{heading} by {args.agent}: {args.text}')
    current = client.state(args.id)
    if cid not in current['accepted'] or current['claim_id'] != state['claim_id']:
        raise Conflict(f'#{args.id} ownership changed. Stop and inspect the card.')
    if args.command == 'done':
        client.update_task(args.id, done=True)
        print(f'#{args.id} done; result recorded.')
    elif args.command == 'block':
        print(f'#{args.id} blocked; reason recorded; card remains open.')
    else:
        print(f'#{args.id} note recorded.')


def listing(client, args):
    project = os.environ.get('AGENT_CARDS_PROJECT', '').strip()
    pid = client.project(project) if project else None
    rows = []
    for task in client.pages('/tasks?filter=' + urllib.parse.quote('done = false')):
        if task.get('done') or (pid is not None and task.get('project_id') != pid):
            continue
        labels = [label['title'] for label in task.get('labels') or []]
        # Leave unrelated personal tasks out of this cooperative tool's output.
        state = client.state(task['id'])
        assigned = [label[6:] for label in labels if label.startswith('agent:')]
        if not assigned and not state['owner']:
            continue
        agent = args.agent
        if agent and (state['owner'] != agent if state['owner'] else agent not in assigned):
            continue
        if args.command == 'resume' and state['owner'] != agent:
            continue
        due = timestamp(task.get('due_date'))
        overdue = bool(due and due < datetime.now(timezone.utc))
        if getattr(args, 'overdue', False) and not overdue:
            continue
        rows.append({'id': task['id'], 'title': task['title'],
                     'due': due.isoformat() if due else None,
                     'owner': state['owner'], 'assigned': assigned,
                     'checkpoint': state['checkpoint'], 'overdue': overdue})
    rows.sort(key=lambda row: (row['due'] is None, row['due'] or '', row['id']))
    if getattr(args, 'json', False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f'#{row["id"]} | {row["title"]} | due: {row["due"] or "none"} | owner: {row["owner"] or "unclaimed"}')
            print('  ' + (row['checkpoint'] or 'No checkpoint yet.'))
        if not rows:
            print('No open cards.')


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='Create a card with a required deadline.')
    add.add_argument('agent', type=agent_name)
    add.add_argument('title', type=nonempty)
    add.add_argument('--due', required=True, type=due_date, help='YYYY-MM-DD, end of day UTC')
    add.add_argument('--project', help='Exact project title or numeric ID (default: AGENT_CARDS_PROJECT)')
    add.add_argument('--note', default='', help='Initial card description')
    for command in ('take', 'note', 'done', 'block'):
        action = sub.add_parser(command)
        action.add_argument('id', type=positive_id)
        if command != 'take':
            action.add_argument('text', type=nonempty)
        action.add_argument('--as', dest='agent', required=True, type=agent_name)
        action.add_argument('--force', action='store_true', help='Intentionally override another owner')
        if command == 'take':
            action.add_argument('--stale-hours', type=stale_hours, default=6.0,
                                help='Allow takeover after this many idle hours (default: 6)')
    show = sub.add_parser('list', help='List open agent cards.')
    show.add_argument('--agent', type=agent_name)
    show.add_argument('--overdue', action='store_true')
    show.add_argument('--json', action='store_true')
    resume = sub.add_parser('resume', help='Resume open cards held by this agent.')
    resume.add_argument('--as', dest='agent', required=True, type=agent_name)
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    try:
        client = Client()
        if args.command == 'add':
            pid = client.project(args.project or os.environ.get('AGENT_CARDS_PROJECT', '').strip())
            task = client.api('PUT', f'/projects/{pid}/tasks',
                              {'title': args.title, 'description': html.escape(args.note).replace('\n', '<br>'),
                               'due_date': args.due})
            try:
                client.add_label(task['id'], 'agent:' + args.agent)
            except CardError:
                raise CardError(f'#{task["id"]} was created but its agent label failed. Inspect it before creating another card.') from None
            print(f'#{task["id"]} added for {args.agent}; due {args.due[:10]}.')
        elif args.command == 'take':
            take(client, args)
        elif args.command in ('note', 'done', 'block'):
            write_progress(client, args)
        else:
            listing(client, args)
        return 0
    except Conflict as error:
        print(str(error), file=sys.stderr)
        return 3
    except CardError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
