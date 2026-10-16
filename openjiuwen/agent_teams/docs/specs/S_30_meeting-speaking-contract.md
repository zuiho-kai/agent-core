# Meeting Speaking Contract — Draft v0.2

## Status and scope

| Item | Value |
|---|---|
| Status | Database queue and strict message linkage implemented; wrapper/audio integration remains proposed |
| Date | 2026-10-16 |
| Scope | PR2: meeting coordination in `agent-core` |
| Requirements | [agent-core #1951](https://github.com/openJiuwen-ai/agent-core/issues/1951), [JiuwenSwarm #8260](https://github.com/openJiuwen-ai/jiuwenswarm/issues/8260) |
| Foundation | [agent-core #1905](https://github.com/openJiuwen-ai/agent-core/pull/1905) |

**Required** denotes behavior specified by the issues. **Proposed** denotes a design choice to review together. Names and signatures below are illustrative, not existing APIs.

The initial scope is one human meeting one Team in a group conversation. Audio capture, VAD, ASR, meaningful-interruption classification, TTS, and playback belong to JiuwenSwarm. Agents supply full text and a spoken summary together. PR2 provides audio ownership, interruption primitives, and a strict reference from each audio request to its normal message. Existing team routing and text delivery remain unchanged.

## 1. Ownership and boundaries

| Responsibility | Owner |
|---|---|
| Determine human intent, recipient, and whether speech is a meaningful interruption | JiuwenSwarm |
| Order agents waiting to speak; grant and revoke ownership | `agent-core` |
| Publish the original complete text through existing delivery paths | `agent-core` |
| Supply a short spoken version alongside the full message | Originating agent; generation integration remains proposed |
| Choose the configured voice, synthesize audio, play and stop audio | JiuwenSwarm |
| Report playback completion, failure, or cancellation | JiuwenSwarm |
| Decide whether a sub-agent should be interrupted | Leader, through its meeting tool |

**Required:** reuse existing Team routing, messaging, and group-chat mechanisms. Meeting coordination must not create a second text transport or change the normal message schema. The audio queue does not gate outgoing text publication.

**Required:** meetings are opt-in. Disabled meetings preserve current tool availability, message delivery, output streaming, and model-call behavior.

## 2. Speaking ownership

**Required**

1. There is at most one speaking owner per meeting.
2. Full messages use existing delivery paths immediately; their spoken versions must acquire ownership before playback.
3. Normal release happens after text publication and audio playback have completed.
4. Waiting to speak must not prevent an agent from continuing independent thinking or tool work.
5. Humans bypass the agent queue.
6. Interrupting speech preserves text that has already been published.
7. Speech interruption does not itself cancel tools, discard plans, or invoke #1905's execution interruption.

**Proposed**

- Scope ownership by `(session_id, team_name, meeting_id)`. A new meeting instance gets a new `meeting_id`, even in an existing conversation.
- One queued speech request references one persisted normal message and stores its spoken version. Multicast correlation beyond one persisted source row remains future wrapper work.
- Every request has a stable `request_id`; every grant has an opaque `lease_token`. Neither is just a member name.
- Playback callbacks must match the meeting instance and lease token. Duplicate and stale callbacks are harmless and cannot release a successor.
- Internal lifecycle events, approvals, and system notifications bypass speaking coordination. Their existing delivery must continue while an agent is speaking.
- Existing output streaming continues independently of audio ownership.

## 3. Queue policy

**Required priority:** `Leader > agents addressed by the human > other agents`. Requests at the same priority are served in arrival order.

**Proposed interpretation**

- Priority selects the next owner; it does not preempt the current owner.
- The SDK derives priority from trusted team identity and human routing metadata. Agents cannot promote themselves by supplying a priority parameter.
- "Addressed" means selected by the routing result for the latest admitted human utterance, including explicit name matches and provider-based routing.
- When a new human utterance is admitted, pending requests are ranked against that latest routing result; their arrival order remains unchanged. Current ownership is unaffected unless an explicit interruption occurs.
- A request can be cancelled while queued. Cancellation removes only that request.

Example: B is speaking. Requests arrive from C, addressed agent A, and the Leader. B finishes normally; the next order is Leader, A, C. Only an explicit human or Leader interruption can end B's speech early.

## 4. Message and audio lifecycle

The proposed normal lifecycle is:

```text
NORMAL_MESSAGE_PUBLISHED -> QUEUED -> GRANTED -> PLAYING -> COMPLETED
```

- **NORMAL_MESSAGE_PUBLISHED:** full text is committed and delivered through the normal communication path.
- **QUEUED:** the SDK accepted an audio request referencing that message, with its agent-produced `speech_text`.
- **GRANTED:** this request holds speaking ownership.
- **PLAYING:** the application is synthesizing/playing the spoken version under that token.
- **COMPLETED:** the application acknowledges that playback has finished; the SDK releases ownership.

Normal message receipts retain their existing delivery meaning. Audio admission is a separate result carrying its request ID and source message ID; a queued audio request does not mean the text is pending. The future wrapper must expose both results without acknowledging an agent's mailbox.

The Leader must be able to inspect the current speaker's full text while its audio plays. Otherwise its interruption tool cannot make the judgment required by the RFC.

The agent supplies full text and summary together. Persist the full text through normal storage, then admit the summary with its source reference. The request stores no full-text copy and does not alter the original message or conversation history.

## 5. Human and Leader interruptions

**Required**

- JiuwenSwarm decides whether human speech is a meaningful interruption, then invokes the SDK interruption API. An acknowledgement or cough alone must not trigger it.
- The Leader receives an enabled-meetings-only "interrupt speech" tool. Ordinary members do not receive it.
- Both interruption paths revoke the current speaking turn and notify JiuwenSwarm to stop its audio.
- Already published text remains available.

**Proposed race-handling contract**

1. Revoke the current lease immediately.
2. Enter an interruption barrier: no new agent is granted ownership yet.
3. Notify the application to stop audio for the revoked token and discard its pending synthesis.
4. For human interruptions, admit the human's message through existing routing before reopening the queue.
5. The application acknowledges audio stopped and input admission is complete; the SDK reopens the queue.

This barrier prevents the next agent from starting audio while old audio is still stopping or the human's correction is still being routed. It is a proposal beyond the issues' high-level "force release" wording.

A Leader tool call should target the observed lease token. If that speaker has already finished, the call must not interrupt an unrelated successor. The application must also reject late speech-ready events and synthesis results belonging to revoked tokens.

## 6. Spoken-summary contract

The originating agent supplies a short conversational `speech_text` alongside its full
message. The request DAO persists this supplied text and performs no model call.
JiuwenSwarm resolves the original by `source_message_id`, exposes both representations,
and performs TTS/playback. Agent-generation and wrapper integration remain proposed.

The summary should preserve the language and conclusions, without reading code, tables,
or links aloud. A failed summary must not retract a published message. The earlier
proposal for a separate agent-core same-model/context summarization call is superseded.

## 7. Illustrative SDK operations and events

These describe semantics rather than final Python names or signatures.

| Operation | Contract |
|---|---|
| `request_speech(...) -> request_id` | Register audio for a committed source message and supplied summary; derive priority from trusted runtime state. |
| `wait_for_grant(request_id) -> lease` | Internal/background wait; cancelling the wait cancels the corresponding queued request. |
| `cancel_request(request_id)` | Cancel queued audio; never retract a published message. |
| `get_speaker() -> lease or None` | Return current ownership and the published message identity, when available. |
| `complete_playback(lease_token, outcome)` | Release only the matching current lease; duplicate/stale completion is a no-op. |
| `interrupt_speech(reason, expected_token?) -> interruption_id` | Trusted application/authorized Leader revokes ownership and starts the proposed interruption barrier. |
| `resume_after_interruption(interruption_id)` | Application confirms old audio is stopped and, for human interruptions, human input has been admitted. |
| `close_meeting(meeting_id)` | Revoke ownership, cancel pending audio, stop playback through the application, and reject late callbacks. |

Proposed application notifications:

- `speech_ready`: meeting/request/source-message identities, lease token, member, and spoken summary; full text is resolved from normal storage.
- `speech_interrupted`: revoked token, interruption identity, reason, and initiating actor.
- `speech_failed`: request/token, failure phase, whether text was already published, and actionable error information.

Exact event names and transport mapping should reuse the existing event infrastructure. Events notify consumers; authoritative ownership must not depend on a subscriber receiving every event.

## 8. Failure, shutdown, and process coordination

**Proposed**

- Queue and ownership changes must be atomic across supported member processes. A process-local lock is insufficient.
- Shared team-database state is the chosen authority for the database foundation (§11). Existing message-bus notifications remain a runtime integration step.
- Never hold a database transaction while waiting for a model, a tool, another speaker, or audio playback.
- Failed text publication cannot admit audio without a committed source. A TTS failure retains published text and releases through the failure lifecycle.
- Releasing after synthesis/playback failure requires the application to confirm any active audio has stopped.
- A disconnected application or dead owner must not leave an invisible permanent lock. Fault-recovery timeouts initiate stop/revocation; they must not grant a successor while old playback could still continue.
- Fault-recovery deadlines are not a normal maximum speech duration. Automatic time-based speech interruption is deferred by the RFC.
- Closing a meeting cancels pending audio requests while preserving text delivery and unrelated agent work.
- Meeting state must remain isolated across teams, sessions, and meeting restarts. Recovery of a persisted conversation must not replay old audio automatically.

## 9. Acceptance scenarios

1. Two independent member processes cannot own the same meeting simultaneously.
2. Leader, addressed-member, and ordinary-member queue priority works; ties preserve arrival order.
3. Normal priority never preempts the current speaker.
4. Waiting to speak does not block unrelated agent work.
5. Normal routing, text delivery, and internal control traffic continue independently of the audio queue.
6. Each audio request references a committed source message and its immutable spoken summary; text is delivered through its existing path.
7. Playback completion releases the matching lease; an old callback cannot release the next turn.
8. Human interruption stops speech, preserves text, admits the human message first, and then reopens the queue.
9. Only the Leader can invoke the agent-facing interruption tool; a stale target cannot interrupt a successor.
10. Late synthesis results after interruption are discarded.
11. Queue cancellation, summary failure, TTS failure, disconnect, and meeting shutdown leave no permanent ownership or unhandled background tasks.
12. Disabled meetings preserve current behavior, toolsets, and model-call counts.

## 10. Decisions to settle together before implementation

| Decision | Proposed starting point | Alternative / unresolved detail |
|---|---|---|
| Ownership unit | One finalized outgoing message plus its spoken version | Let an agent retain the floor for several messages; requires an explicit end-of-speech contract. |
| Meaning of addressed priority | Latest admitted human routing result; update pending priorities | Keep priority fixed at queue admission, or bind it to the originating human turn. |
| Cross-process authority | Shared team database plus existing bus notifications | Leader-owned coordinator; requires request/grant protocol and failover design. |
| Interruption handoff | Revoke immediately, reopen after stop/input acknowledgement | Release and grant immediately; application must independently prevent overlapping playback. |
| Queued tool behavior | Normal text receipts remain unchanged; audio has a separate request result | Exact wrapper API remains proposed. |
| Summary generation | Agent supplies text and summary together; queue stores the summary | Agent-generation integration is separate from the database change. |
| Initial runtime support | Reuse existing team/group-chat paths | Finalized streamed replies need a persisted source identity before audio admission. |
| Recovery policy and limits | Explicit failure/stop handoff, no silent audio replay | Define concrete timeouts, pending-operation recovery, and playback-owner authority. |

The first implementation step is the shared queue database, explicitly requested on 2026-10-16.
The remaining wrapper, agent-generation, and application contracts remain proposals.

## 11. Database foundation

`TeamDatabase.meeting` exposes `MeetingDao`. The abstract SQLModel bases
`TeamMeetingBase` and `TeamSpeechRequestBase` follow the existing task/message convention.
`_get_meeting_model()` and `_get_speech_request_model()` resolve concrete per-session
tables `team_meeting_<session_suffix>` and `team_speech_request_<session_suffix>`.
The factories default to the bound session and also accept an explicit session ID for
meeting operations. Every operation takes an explicit
`MeetingScope(session_id, team_name, meeting_id)`; context switches cannot change its target.
Session identity is represented by the physical table, rather than a duplicated column.
Meeting row keys are `(team_name, meeting_id)`; request keys add `request_id`.
Meeting IDs cannot be reopened after close. A new meeting requires a new ID.

The tables participate in normal session schema creation, model caching, and session
table drop/cleanup. Explicit meeting creation ensures both meeting tables and their
normal-message parent table without changing the caller's bound session. Team deletion cascades through the existing
`team_info` foreign-key convention.

All mutations first update the meeting row, acquiring a database write/row lock before
reading queue state. This serializes independent engines and member processes, beyond the
existing process-local `DbSessions` lock. No transaction spans publication, models, or playback.
An increasing per-meeting sequence defines FIFO, independent of clock resolution.

`create_meeting` snapshots the team's leader identity; `enqueue` accepts only registered
non-human members and has no priority argument. `set_addressed_members` replaces the trusted
routing result. `list_pending` and `grant_next` rank pending requests using the latest result,
then sequence. Priority changes never revoke an owner. Reusing a request ID returns the
existing request for that author/source/summary, including terminal requests, without re-enqueueing it.

`grant_next` returns a request with a fresh opaque lease token, or `None` when owned,
interrupted, closed, or empty. `complete_playback` releases only a matching current token.
`cancel_request` only cancels queued requests. Late playback and interruption acknowledgements
after session cleanup are harmless no-ops. `interrupt_speech` revokes the current request
and persists an interruption ID; no successor can be granted until
`resume_after_interruption` acknowledges that exact ID. `close_meeting` cancels pending
requests and revokes ownership permanently. Terminal requests remain as deduplication records.

This layer stores coordination metadata, the source message reference, and its spoken
summary. Full text stays in normal message storage. Audio stop acknowledgement,
leader-tool authorization, recovery deadlines, and bus notifications remain integration
work. Persisted leases are never automatically replayed or expired by this DAO.
Session release and runtime cleanup remove the scoped rows.

## 12. Strict audio-to-message linkage

`enqueue(scope, request_id, member_name, *, source_message_id, speech_text)` requires
an already committed normal message in the scope's session and a nonblank summary.
Its admission transaction locks the meeting and inserts the reference and summary
together without querying the source message. A per-session foreign
key restricts deletion of referenced messages. One source message has at most one
request per meeting, enforced by a database unique constraint. A repeated request ID
must match author, source message ID, and exact summary; conflicts raise `ValueError`
without changing sequence, payload, or ownership. Terminal records retain these links.
Source-message team/author validation belongs to the forthcoming wrapper. Until then,
the trusted caller must supply the correct association. Missing/deleted source messages
fail through the foreign key with `IntegrityError`; the transaction rolls back.
The foreign key arbitrates source deletion against admission. Constraint enforcement can
still acquire internal locks; this does not promise deadlock-free arbitrary transactions.

Normal message storage and transport are unchanged. Consumers can combine the normal
message with the request using explicit scope and `source_message_id`; retrieving
audio work must not acknowledge an agent's mailbox. The queue stores no full-text copy.
Creating a normal message and admitting audio remain separate commits; applications
retry admission with stable IDs. This schema change does not implement a wrapper or
claim atomic message-plus-request publication.

Empty legacy request tables are rebuilt during schema setup, with an INFO migration
log. The ensure helper ignores absent/current tables and detects the prototype schema
by its missing source/summary columns. Constraints are declared by the model. A populated
legacy table logs its table name and reason at ERROR and raises
`AGENT_TEAM_STATE_INVALID`; it never invents source links or discards rows. Session
teardown drops referencing requests before messages.
