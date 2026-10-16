# Meeting queue database

| Item | Value |
|---|---|
| Date | 2026-10-16 |
| Scope | Team database meeting queue foundation |
| Refs | #1951 |
| Spec | S_30_meeting-speaking-contract.md |

## Decisions

Persist ownership and FIFO order in the shared team database so independent member
processes have one authority. Lock the meeting row before every mutation, including
enqueue and routing changes. SQLite acquires its write lock through the initial UPDATE;
server databases lock the row. Keep transactions short and return immediately.

Use explicit session/team/meeting keys and durable terminal requests for callback isolation
and deduplication. Rank against the latest trusted routing metadata at grant time. Keep the
interruption barrier persisted so an acknowledgement for an old turn cannot reopen a new one.

Follow the existing per-session task/message table pattern: `TeamMeetingBase` and
`TeamSpeechRequestBase` define fields, and `_get_meeting_model()` /
`_get_speech_request_model()` resolve cached concrete classes. The engine imports the
factories, not concrete meeting classes. Session identity lives in the table suffix;
explicit scope IDs select the same factory models without rebinding context variables.
Session bind creates the tables; explicit meeting creation also ensures its two tables.
Session release drops them through the existing dynamic-table cleanup paths, and team
deletion uses the existing `team_info` cascade convention.

## Rejected alternatives

- An asyncio lock alone cannot serialize different member processes.
- Timestamp ordering can tie; per-meeting sequence numbers preserve committed admission order.
- Caller-supplied numeric priority would let agents promote themselves.
- Immediate grant after revocation could overlap old audio with the successor.
- Storing full outgoing text here would introduce another message store.
- Fixed meeting tables with a session column diverged from the existing session-table
  convention. The unreleased prototype was replaced with base models and dynamic factories.

## Validation

The 20 meeting database cases and 153 existing database/concurrency cases pass together
(173 passed) against real SQLite, including independent engines and spawned processes.
They cover priority/FIFO, token isolation, cancellation, interruption, close, persistence,
session/team cleanup, additive schema creation, contextual/explicit model factories,
session bind/drop/recreate, and concurrent initialization of a new session's meeting.
Tests use a short temporary path outside the repository to accommodate the existing
worktree cleanup case and Windows path limits. Ruff check and format pass for new Python files.
The existing engine has an unrelated ASYNC240 finding on `Path.expanduser()` with the
installed Ruff version. PostgreSQL/MySQL integration was not exercised locally.

## Follow-up

Integrate the outgoing delivery gate, pending receipts, bus notifications, summary generation,
application playback callbacks, and explicit fault-recovery handoff. This change does not enable
meeting behavior on the agent runtime.
