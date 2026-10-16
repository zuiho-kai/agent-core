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
Session bind creates the tables; explicit meeting creation also ensures its two tables
and the normal-message parent table.
Session release drops them through the existing dynamic-table cleanup paths, and team
deletion uses the existing `team_info` cascade convention.

Keep the normal message schema, router, transport, and read acknowledgements unchanged.
Store the agent-produced spoken summary as `speech_text` on the speech request and
reference the original message using required `source_message_id`. The audio queue
does not gate normal text delivery or generate summaries.

Admission references an already committed message in the explicit session. The DAO
does not query or validate the source message; team/author association is the trusted
caller's responsibility until the wrapper implements that boundary. Missing or deleted
source references fail through the database foreign key when the request is committed.
A per-session foreign key prevents dangling references; a unique constraint on
team/meeting/source message prevents duplicate audio requests under different IDs.
An existing request ID is idempotent only for the same author, source, and summary,
including terminal requests. Never replace its payload during a retry.

Message deletion is restricted while requests reference it. Session cleanup drops
request tables before message tables; whole-team deletion retains its cascade behavior.
Empty prototype request tables are rebuilt with the new constraints. Nonempty prototype
tables cannot be safely backfilled without source identities and fail explicitly rather
than inventing links or discarding data.
The schema helper follows neighboring ensure conventions: ignore an absent table,
leave a valid schema untouched, and log successful migration with its table name.
Detect the prototype schema by its missing source/summary columns; constraints are
declared by the model and verified by integrity tests. If a populated legacy table
cannot be safely upgraded, log the table and reason at ERROR and raise
`AGENT_TEAM_STATE_INVALID`; preserve its rows and propagate the failure.

Normal-message creation and queue admission remain separate commits. This change
guarantees referential integrity and deduplication, not atomic message-plus-request
publication. Applications retry admission using a stable request ID after a transient
failure. No transaction spans bus publication, summary generation, or playback.

## Rejected alternatives

- An asyncio lock alone cannot serialize different member processes.
- Timestamp ordering can tie; per-meeting sequence numbers preserve committed admission order.
- Caller-supplied numeric priority would let agents promote themselves.
- Immediate grant after revocation could overlap old audio with the successor.
- Storing full outgoing text here would introduce another message store.
- Adding summary fields to normal message storage violates the requested boundary.
- Application source checks alone cannot prevent dangling links under concurrent deletion.
- Explicitly locking the source message couples queue admission to normal message updates;
  foreign-key enforcement already protects the persisted reference.
- Silently accepting changed source/summary content on retry makes concurrent producers disagree.
- Fixed meeting tables with a session column diverged from the existing session-table
  convention. The unreleased prototype was replaced with base models and dynamic factories.

## Validation

The 38 meeting database cases and 153 existing database/concurrency cases pass together
(191 passed) against real SQLite, including independent engines and spawned processes.
They cover priority/FIFO, token isolation, cancellation, interruption, close, persistence,
session/team cleanup, additive schema creation, contextual/explicit model factories,
session bind/drop/recreate, and concurrent initialization of a new session's meeting.
The linkage cases also cover missing/cross-session source rejection by the foreign key, immutable retries, unique
source constraints, foreign-key enforcement, admission/deletion races, independent-engine
payload conflicts, unchanged normal-message content/read state, and legacy-table handling.
Schema-helper cases verify absent/current tables, migration/error logging, typed failures,
and preservation of populated legacy tables.
The 42 existing group-conversation/integration/wakeup cases also pass (233 total).
Tests use a short temporary path outside the repository to accommodate the existing
worktree cleanup case and Windows path limits. Group-chat checks use extended Windows
paths and Python UTF-8 mode. Ruff check and format pass for the request DAO, models, and tests.
The existing engine has an unrelated ASYNC240 finding on `Path.expanduser()` with the
installed Ruff version. PostgreSQL/MySQL integration was not exercised locally.

## Follow-up

Integrate the thin meeting/team lifecycle wrapper, agent-produced summaries, source-message
correlation in application payloads, bus notifications, playback callbacks, and explicit
fault-recovery handoff. The wrapper must validate the source message's team/author and
bind it to the trusted caller identity. Text continues through normal communication paths independently of
audio ownership. This change does not enable meeting behavior on the agent runtime.
