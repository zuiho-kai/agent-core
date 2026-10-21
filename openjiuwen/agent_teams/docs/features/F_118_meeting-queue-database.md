# Meeting queue and coordination wrapper

| Item | Value |
|---|---|
| Date | 2026-10-16 |
| Updated | 2026-10-20 |
| Scope | Shared meeting queue, speaking leases, and explicit wrapper over existing Team messaging |
| Refs | #1951; JiuwenSwarm #8260 |
| Spec | [S_30_meeting-speaking-contract.md](../specs/S_30_meeting-speaking-contract.md), sections 11–14 |

## Background

Meetings need a shared speaking order without replacing Team communication. Agent
processes may run independently, so the database owns queue order and speaking
leases. The originating agent supplies full content and a spoken summary together.
The wrapper delays normal sending until that utterance receives speaking ownership;
recipients continue fetching and acknowledging messages through existing Team paths.
This document combines the database foundation and its coordinator wrapper.

## Current design

The lifecycle is queue -> acquire speaking ownership -> send full text normally ->
play summary -> release. Human input bypasses the queue. Interruption retains text
already sent. Physical synthesis/playback and stopping audio belong to the host.

### Shared queue and ownership

- Persist ownership, FIFO sequence, and interruption barriers in the shared database.
  Every mutation updates the meeting row before reading it. SQLite acquires its write
  lock through that UPDATE; server databases lock the row. The process-local asyncio
  lock alone cannot serialize independent processes.
- Use explicit session/team/meeting keys. Latest trusted routing determines priority:
  Leader, then addressed members, then others; ties retain committed FIFO order.
  Ordinary priority changes never revoke an owner, and agents cannot supply numeric
  priorities to promote themselves.
- Grant one opaque lease at a time. Completion releases only the current token;
  stale callbacks cannot release a successor. Cancellation affects only queued work.
  Interruption revokes ownership and persists a barrier until the matching stop/input
  acknowledgement. Close permanently revokes the owner and cancels pending requests.
- Keep transactions short. No transaction waits for sending, history IO, models,
  summary generation, another speaker, or playback. Reads never send or acknowledge
  agent input, and no background scheduler or second Team runtime is created.

### Pending content and normal sending

Pending requests store full content, speech_text, and mentions. Admission inserts
only a request: no normal message or hidden message draft exists yet. The wrapper
generates a request UUID internally; member tools accept content, summary, and
mentions without caller-managed IDs. Repeated content is a separate utterance.
There are no admission retry lookups or payload comparisons; database primary and
unique constraints reject duplicate IDs/references.

The playback worker calls grant_next. After committing ownership, the coordinator
invokes the existing group post_message path, which creates the normal message,
projects history, and publishes the Team notification. The request UUID supplies
that existing path's internal submission identity. Existing message ID generation,
normal message schema, transport, recipient polling, and read acknowledgements stay
unchanged. History and public source lookup use the existing team/session group
conversation, shared by meeting instances in that conversation. Meeting scope
isolates speech requests and leases only; no meeting tag or changes to the group
handler are required. Private and non-group messages remain outside this history.

source_message_id is nullable while queued. After normal sending returns a message
ID, record_publication writes that reference and clears the queued full-text copy.
The per-session foreign key prevents dangling non-null references and restricts
source deletion. A unique team/meeting/source constraint prevents two requests in
one meeting from referencing the same sent message; pending null references coexist.
MeetingDao writes only queue/meeting rows. MessageDao and TeamDatabase need no new
methods, cross-DAO transactions, or broadcast promotion APIs.

Results bundle full text and summary. Before sending, content comes from the queue
and the source ID/message are absent; afterward it comes from the referenced message.
The host starts audio after sending and retains the lease until playback completes.
The existing-source request_speech path remains for trusted compatibility callers;
the wrapper validates team/author using ordinary retrieval. That path cannot delay
text already sent, and the queue DAO trusts the caller's source association.

### Sending failures and interruption

Sending and recording its result are separate operations. If message storage succeeds
but history projection or link recording fails, the normal message remains published
and queued content is retained. The wrapper returns the granted token with an error,
so the host can recover or explicitly release it. Current-token recovery uses existing
group submission behavior to find the same message. A source hydration failure also
preserves the token. Playback must wait for a result without delivery/source errors.
Ordinary Team notification failures remain logged with inbox polling as fallback;
notification is not an acknowledgement of recipient delivery.

Interruption or close can overlap sending. The wrapper records a message already sent,
then rechecks the lease before returning playable work. A revoked/closed turn returns
None; late synthesis results must be discarded by the host. Cancellation or close
before grant never creates a normal message. Completion never resends text or
implicitly grants the next turn.

### Session schema and lifecycle

Follow existing per-session task/message model conventions: TeamMeetingBase and
TeamSpeechRequestBase declare fields, and cached factories resolve concrete classes.
The engine imports factories, not concrete meeting classes. Session identity lives
in the table suffix; explicit scopes resolve their models without rebinding context.

Session binding creates the tables; explicit meeting creation also ensures the normal
message parent table. Session release drops referencing requests before messages.
Whole-team deletion follows the existing team_info cascade convention. Terminal
requests retain their identities and message references for callback isolation.

The schema helper ignores absent/current tables and rebuilds empty legacy queues
with content, mentions, and nullable source linkage, logging the migration at INFO.
Populated older schemas cannot be safely backfilled: log the table/reason at ERROR,
raise AGENT_TEAM_STATE_INVALID, and preserve every row. Use a new session or explicitly
release the old one; this change never invents source links or discards existing data.

## Review decisions and rejected alternatives

Review also removed meeting-specific history tagging. The wrapper reads the
existing team/session conversation; speech queues and leases remain meeting scoped.
group_chat/handler.py is restored unchanged. Sharing public group history avoids
adding message-association tables or special handling for human input.

The initial foundation required a source message before enqueueing and returned
identical repeated request IDs. A later draft implementation stored hidden normal
messages and promoted them after a grant using cross-DAO transactions. Review on
2026-10-20 replaced those approaches with pending queue content and normal sending
after grant. Hidden message states, promotion helpers, transaction-aware MessageDao
additions, caller-managed meeting client IDs, and posting retry comparisons were removed.

- Sending before grant exposes text before speaking ownership. Withholding only the
  notification still leaks ordinary deliverable rows through recipient polling.
- Delaying text until playback completion violates the agreed grant -> text -> audio
  ordering. Audio and full text instead originate from one claimed queue entry.
- Holding a transaction during history, bus IO, or playback stalls queue coordination.
- Keeping two permanent full-text stores creates conflicting sources of truth. The
  queue retains pending content only until the normal message reference is recorded.
- Adding spoken-summary fields to normal messages changes the wrong storage domain.
- Timestamp ordering can tie; committed per-meeting sequences determine queue FIFO.
- Reopening immediately after revocation can overlap old audio and its successor;
  persisted interruption acknowledgements protect that handoff.
- Rechecking internally established source ownership on every grant repeats boundary
  validation. Database foreign keys protect persisted links without explicit source locks.
- Fixed meeting tables with a session column diverge from the existing dynamic-table
  convention. The unreleased prototype was replaced with base models and factories.

## Validation

The queue-content redesign passed 229 distinct targeted cases: 123 meeting coordinator/
queue/publication, 64 ordinary message-manager/database-concurrency, and 42 group cases.
After restoring the group handler unchanged, 125 relevant cases pass: 83 wrapper/
coordinator and 42 group-conversation/integration cases. These verify shared history,
meeting-specific queues/leases, private-message exclusion, and team/session boundaries.
The earlier redesign coverage also includes no messages at admission, queue-only inspection,
internal UUIDs, normal sending at grant, content-to-reference handoff, nullable foreign
keys, priority/FIFO, token isolation, cancellation/close, interruption during sending,
mailbox consumption, sending/history/recording failure recovery, independent engines
and spawned processes, session/team cleanup, and legacy schema preservation.

Earlier foundation validation exercised 38 meeting database and 153 existing database/
concurrency cases together (191 passed), plus 42 group cases (233 total). Those are
historical results for the earlier design, not an additional current test count.

Windows checks use short temporary paths, extended workspace paths where needed,
and Python UTF-8 mode. Ruff and formatting checks pass for changed meeting code;
the engine check excludes its pre-existing ASYNC240 finding at Path.expanduser.
Whitespace checks pass. PostgreSQL/MySQL integration was not exercised locally.

## Follow-up

Automatic TeamSpec/spawn and outgoing-send installation, same-model/context summary
hooks, host stop notifications, playback ownership, and crash/disconnect recovery
remain integration work. This explicit wrapper does not enable runtime meeting mode.
Existing populated older queues require a new session or explicit release.

### Deferred: transcript synchronization cost

Decision recorded on 2026-10-20: retain the current `sync_history()` behavior for
this change. Review and fix the performance issue after the current changes are
committed; do not fold that optimization into the coordinator ordering correction.

Evidence from the current implementation:

- `group_chat/handler.py::sync_history` loads all team broadcasts for the session
  and converts group-chat rows into transcript records.
- `group_chat/conversation.py::GroupConversationLog.sync` holds a cross-process
  file lock, reads/parses the existing transcript, merges and sorts records, then
  rewrites the entire `history.jsonl` file. It does not append incrementally.
- `meeting/coordinator.py::_publish_granted` invokes the existing group send path, which awaits this work before publishing
  the broadcast notification and returning the speech for playback.
- `group_chat/handler.py::context_for` calls synchronization again when preparing
  recipient input, so one message can cause repeated full-history work across
  recipients. `asyncio.to_thread` avoids blocking the event-loop thread but does
  not remove the latency from callers awaiting completion.
- Ordinary Team messaging does not require this shared transcript file; this is
  a group-chat projection concern, separate from speaking ownership.

Impact: database reads, parsing, sorting, file writes, and lock contention grow
with transcript size. Repeated full rewrites yield roughly quadratic cumulative
data processing for similarly sized messages over a growing conversation. This
is a code-based cost analysis, not a measured latency benchmark.

Follow-up work: benchmark increasing transcript sizes and recipient counts;
evaluate bounded/indexed context reads and incremental or on-demand transcript
projection; decide whether transcript materialization should remain on the
grant-to-playback critical path. Merely moving the same synchronization into
another helper will not address its cost.

Acceptance: preserve source IDs, transcript access, mention routing, read
watermarks, retry/concurrency safety, and the required grant -> text -> audio ->
release ordering. Queued/cancelled content must never reach ordinary inboxes. Measure the
improvement rather than assuming background execution eliminates the cost.
