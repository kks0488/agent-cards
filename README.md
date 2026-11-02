# agent-cards

A small, standard-library Python CLI that gives AI agents exclusive cooperative task claims, deadlines, checkpoints, and evidence-backed completion on Vikunja.

[한국어](README.ko.md)

## Why this exists

When one person runs several agents at once, two can pick up the same task, a task can be created without a deadline, a session can drop halfway through, or an agent can report "done" without leaving a usable result. agent-cards records the current owner and progress in the task's comments so the next session knows where to pick up.

Vikunja stores the tasks, and this tool adds a small working agreement around them. It uses the v1 API and requires Python 3.9 or later, with no packages to install. See [Vikunja's API documentation](https://vikunja.io/docs/api-documentation/) for API availability and token setup.

## Setup in three steps

1. Download this directory and open a terminal in it. Check your Python version:

   ```sh
   python3 --version
   ```

2. On your Vikunja server, create a project and an API token under Settings > API Tokens. Give the token read access to projects, tasks, labels, and comments, plus permission to create tasks, labels, and comments, attach labels, and update tasks. Set these variables in your shell, replacing the placeholders with your own values:

   ```sh
   export VIKUNJA_URL='https://tasks.example.org'
   export VIKUNJA_TOKEN='<your-api-token>'
   export AGENT_CARDS_PROJECT='Demo'
   ```

   `VIKUNJA_URL` accepts the server's root URL or a URL ending in `/api/v1`. Use your own HTTPS server for remote access. `AGENT_CARDS_PROJECT` accepts a numeric project ID or an exact, unique project title.

3. Create a card and pass its ID to an agent. Give each working agent a distinct name:

   ```sh
   python3 agent_cards.py add agent-07 "Write the setup guide" --due 2030-12-20 --note "Save the guide in docs/setup.md"
   python3 agent_cards.py take 42 --as agent-07
   python3 agent_cards.py note 42 "Confirmed: Wrote docs/setup.md; next check the examples" --as agent-07
   python3 agent_cards.py done 42 "Wrote and checked docs/setup.md; review #81" --as agent-07
   ```

   The ID `42` is an example. Use the ID returned by `add`. Dates set the deadline to **23:59:59 UTC** on the specified day.

## Commands

Run commands as `python3 agent_cards.py <command>`. Pass `--help` to any command to see its arguments.

| Command | Purpose |
| --- | --- |
| `add <agent> "title" --due YYYY-MM-DD [--project P] [--note "text"]` | Create a card with an `agent:<name>` assignment label and a required deadline. `--note` sets the description. |
| `take <id> --as <agent> [--stale-hours 6] [--force]` | Claim an open card, refresh your own claim, or take over an idle claim. |
| `note <id> "text" --as <agent> [--force]` | Record progress as the current owner. Lines beginning with `Confirmed:` or `확정:` become resume checkpoints. |
| `done <id> "result and evidence" --as <agent> [--force]` | Record the result and close the card. Empty results and results without a reference are refused. |
| `block <id> "reason" --as <agent> [--force]` | Record why work stopped. The card stays open and keeps its current owner. |
| `list [--agent A] [--overdue] [--json]` | Show open agent cards with their deadlines, current owners, and latest checkpoint line. |
| `resume --as <agent>` | Show open cards currently held by that agent along with the latest checkpoint for each. |

`add` requires `--project` or `AGENT_CARDS_PROJECT`. When that environment variable is set, `list` and `resume` stay within that project; otherwise they scan all accessible projects. `list` includes cards that have an `agent:` label or a recognized claim. `--agent` filters by current owner and falls back to the assignment label for unclaimed cards. Taking a card does not change its original assignment label.

A fresh claim blocks other agents for six hours by default. Only accepted records from that owner reset the timer. After that interval passes, another agent can take over. Passing `--force` to `take` skips the waiting period. Passing `--force` to `note`, `block`, or `done` allows an intentional owner override without transferring the claim, though it does not bypass result validation. Regular writes require an active claim first. A blocked agent should leave a checkpoint before stopping, and active agents should record progress more often than the idle interval.

Completion evidence can be a file path, a URL, `#42`, `ticket 42`, or a commit ID. The tool checks that the text includes a reference-shaped value, but it cannot verify that a file exists, a URL works, or the work passed review. Keep the result descriptive and check the evidence yourself.

## Example terminal output

The following output uses fictional data:

```text
$ python3 agent_cards.py take 42 --as agent-07
#42 taken by agent-07
No checkpoint yet.
$ python3 agent_cards.py note 42 "Confirmed: Draft saved in docs/setup.md" --as agent-07
#42 note recorded.
$ python3 agent_cards.py resume --as agent-07
#42 | Write the setup guide | due: 2030-12-20T23:59:59+00:00 | owner: agent-07
  Confirmed: Draft saved in docs/setup.md
$ python3 agent_cards.py take 42 --as agent-08
#42 is held by agent-07. Stop; use --force only for an intentional handoff.
$ python3 agent_cards.py done 42 "Checked docs/setup.md; review #81" --as agent-07
#42 done; result recorded.
```

## Agent instructions

Give your Claude Code, Codex CLI, or other agent the short [working rules](examples/agent_rules.md). On a restarted session, the agent should begin with `resume` and continue from the last confirmed step. Before doing work, it should refresh its claim with `take`.

## How claims work

Each claim is stored as a single structured comment. The client replays comments by increasing comment ID and accepts a claim only when its expected previous comment ID and previous owner match the replayed state. Two agents reading the same state at the same time cannot both claim the card. Intervening progress from a returning owner also invalidates a stale handoff. Rejected claim attempts remain visible in the comment history for auditing, but they never become the owner or reset the owner's idle timer. After writing a comment, the client reads the comments again and reports a failure if its attempt was rejected.

Keep the structured `agent-cards:v1` comment records intact. Manual comments are allowed, but they do not refresh the owner or create checkpoints. Agent commands escape user text for Vikunja's HTML comment storage. Every task update first fetches the full task and sends it back with the changed field, preserving the description, deadline, and other fields that a partial v1 update might clear.

This tool coordinates cooperating agents; it is not a server-enforced lock or an authorization boundary. It relies on the server returning committed comments consistently in ID order. An explicit force handoff, a manual edit, or a takeover after an idle period can interrupt an agent, so the agent must stop whenever an ownership check fails. Writing a comment and closing a task are separate API calls, which means a network failure can leave a result comment on an open card. Inspect the task before retrying. Updating the full task also cannot prevent a concurrent manual edit from being overwritten. The isolated tests do not guarantee compatibility with a live server.

Exit codes are `0` for success, `1` for configuration, API, or evidence errors, `2` for invalid arguments, and `3` for ownership conflicts or closed cards. Any nonzero exit means the agent must inspect the problem before continuing.

## Tests

From this directory, run:

```sh
python3 -m unittest -v
```

The test suite starts a local fake Vikunja HTTP server and runs the real CLI in separate processes. It covers all commands, concurrent claims, idle and forced handoffs, checkpoints, permissions, deadlines, evidence checks, pagination, and preservation of task fields. No live server or credentials are needed.

## License

[MIT](LICENSE). Copyright (c) 2026 kks0488.
