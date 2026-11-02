# Working rules for an agent

Use these instructions in Claude Code, Codex CLI, or another coding agent. Replace `agent-07` with a unique name for each active worker. The coordinator supplies the server settings and card IDs.

1. Start or restart every session with `python3 agent_cards.py resume --as agent-07`. Read each card's latest `Confirmed:` or `확정:` line. Continue after that step.
2. Before doing work, run `python3 agent_cards.py take <id> --as agent-07`. Work only after exit code 0. If another owner holds it, stop. Do not use `--force` unless the coordinator explicitly directs a handoff.
3. After each completed step, run `python3 agent_cards.py note <id> "Confirmed: what is finished, where the evidence is, and what comes next" --as agent-07`. Use one line for the checkpoint. Keep progress updates less than six hours apart, or more often if a shorter idle interval was agreed.
4. If blocked, first leave a checkpoint, then run `python3 agent_cards.py block <id> "Reason, attempted solutions, and the input needed" --as agent-07`. Report the blocker to the coordinator. The card stays open.
5. Finish with `python3 agent_cards.py done <id> "Concrete result and evidence such as docs/guide.md, a URL, or #42" --as agent-07`. Verify the command succeeded. Never claim completion without checking the actual evidence.
6. Treat any nonzero exit as a reason to stop and inspect the card. After a connection failure, check whether your comment or completion was saved before retrying. Do not edit the structured claim records or keep working after a handoff.

Example checkpoint:

```sh
python3 agent_cards.py note 42 "Confirmed: Added tests/test_parser.py; parser tests pass; next run the full suite" --as agent-07
```

Coordinators must give every new card a deadline. Assignment labels show the original intended agent; comments determine the current owner.
