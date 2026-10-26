# Meeting Speaking Contract — Draft v0.2

## Status and scope

| Item | Value |
|---|---|
| Status | Database queue, strict message linkage, and explicit meeting wrapper implemented; runtime/audio integration remains proposed |
| Date | 2026-10-21 |
| Scope | PR2: meeting coordination in `agent-core` |
| Requirements | [agent-core #1951](https://github.com/openJiuwen-ai/agent-core/issues/1951), [JiuwenSwarm #8260](https://github.com/openJiuwen-ai/jiuwenswarm/issues/8260) |
| Foundation | [agent-core #1905](https://github.com/openJiuwen-ai/agent-core/pull/1905) |

**Required** denotes behavior specified by the issues. **Proposed** denotes a design choice to review together. Sections 1–10 describe the design; sections 11–14 document the implemented database and wrapper APIs.

The initial scope is one human meeting one Team in a group conversation. Audio capture, VAD, ASR, meaningful-interruption classification, TTS, and playback belong to JiuwenSwarm. Agents supply full text and a spoken summary together. PR2 provides audio ownership, interruption primitives, queued content, and a foreign-key reference to the normal message after sending. Existing Team routing and transport are reused; meeting-enabled agent publication waits for a speaking grant.

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

**Required:** reuse existing Team routing, messaging, and group-chat mechanisms. Meeting coordination must not create a second text transport or change the normal message schema. Speaking ownership gates outgoing agent text publication and audio playback; human input bypasses the queue.

**Required:** meetings are opt-in. Disabled meetings preserve current tool availability, message delivery, output streaming, and model-call behavior.

## 2. Speaking ownership

**Required**

1. There is at most one speaking owner per meeting.
2. Agent replies, A2A messages, and broadcasts must acquire speaking ownership before text delivery. The holder sends full text first, then plays its spoken version.
3. Normal release happens after text publication and audio playback have completed.
4. Waiting to speak must not prevent an agent from continuing independent thinking or tool work.
5. Humans bypass the agent queue.
6. Interrupting speech preserves text that has already been published.
7. Speech interruption does not itself cancel tools, discard plans, or invoke #1905's execution interruption.

**Proposed**

- Scope ownership by `(session_id, team_name, meeting_id)`. A new meeting instance gets a new `meeting_id`, even in an existing conversation.
- One queued speech request stores full text and its spoken version; it references a normal message after sending. Multicast correlation beyond one persisted source row remains future wrapper work.
- Every request has a stable `request_id`; every grant has an opaque `lease_token`. Neither is just a member name.
- Playback callbacks must match the meeting instance and lease token. Duplicate and stale callbacks are harmless and cannot release a successor.
- Internal lifecycle events, approvals, and system notifications bypass speaking coordination. Their existing delivery must continue while an agent is speaking.
- Internal progress/control streaming may continue while waiting; final meeting utterances must use the ownership-gated send path. Automatic runtime installation remains pending.

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
QUEUED -> GRANTED_AND_TEXT_PUBLISHED -> PLAYING -> COMPLETED
```

- **QUEUED:** full text, spoken summary, and recipients are durably stored in the speech queue; no normal message exists yet.
- **GRANTED_AND_TEXT_PUBLISHED:** the coordinator commits the lease, sends queued text through the existing Team/group path, and records the returned message ID on the request.
- **PLAYING:** the application plays the spoken summary, with complete source text included in the returned request.
- **COMPLETED:** the application acknowledges playback and the matching lease is released. Completion does not send the text again.

Normal agent consumers cannot see queued content because it is not in the message table. Human inputs remain immediate. Cancellation or close before a grant never sends that content. Interruption after a grant preserves text already published, as required by RFC #8260/#1951.

The originating member currently supplies full text and spoken summary together. The RFC's same-model/context summary-generation hook and installation into every existing outgoing runtime path remain integration work; this wrapper does not claim to implement those steps.

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

The upstream RFC requires a hook that uses the same model and context for an additional short spoken-summary call after the holder sends its full text. The result should preserve language and conclusions without reading code, tables, or links aloud. JiuwenSwarm performs TTS/playback.

The current explicit member wrapper accepts supplied `speech_text` to exercise coordination without model integration. It does not implement or supersede the required generation hook. The DAO makes no model calls and stores the supplied summary with a source reference.

## 7. Illustrative SDK operations and events

These describe semantics rather than final Python names or signatures.

| Operation | Contract |
|---|---|
| `request_speech(...) -> request_id` | Low-level compatibility admission for an existing source. New member publication stages text until ownership. |
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
- Failed queue admission sends neither text nor audio; playback begins only after granted text publication succeeds. A TTS failure retains published text and releases through the failure lifecycle.
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
5. Human routing and internal control traffic continue while agent text publication waits for speaking ownership.
6. Each audio request contains queued full text and summary until sending creates its normal source message.
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
| Queued tool behavior | Member post queues both versions; grant uses normal sending before playback | Explicit wrapper implemented; installation into ordinary outgoing tools remains pending. |
| Summary generation | Required same-model/context hook; queue stores its summary | Current wrapper accepts a supplied summary; model-generation integration remains pending. |
| Initial runtime support | Reuse existing team/group-chat paths | Finalized streamed replies must enter the speaking queue before normal sending. |
| Recovery policy and limits | Explicit failure/stop handoff, no silent audio replay | Define concrete timeouts, pending-operation recovery, and playback-owner authority. |

The first implementation step is the shared queue database, explicitly requested on 2026-10-16.
The explicit wrapper is described in section 13. Agent-generation, runtime wiring, and
application playback contracts remain proposals.

## 11. Database foundation

TeamDatabase.meeting exposes MeetingDao over per-session TeamMeetingBase and
TeamSpeechRequestBase models. Every operation uses an explicit MeetingScope
(session_id, team_name, meeting_id). Meeting and request tables participate in
normal session creation/drop and team deletion cascades. Request keys are
(team_name, meeting_id, request_id).

Every mutation updates the meeting row before reading it, serializing independent
processes through the database lock. Priority is Leader, then addressed members,
then others, with FIFO within a tier. Priority never preempts the owner. One lease
is granted at a time; completion, cancellation, interruption barriers, and permanent
close retain their existing semantics. No transaction waits for sending or audio.

A request stores speech_text, optional queued content, and mentions. Its nullable
source_message_id references the normal message primary key after sending. The
foreign key restricts deletion of a referenced message; a unique key prevents two
requests in one meeting from referencing the same sent message. Multiple pending
requests may have null source references. Database constraints reject duplicate
request IDs; admission does not compare previous payloads or implement retries.

## 12. Pending content and normal Team sending

Member admission stores full text and summary only in the speech-request queue.
It does not insert, hide, update, or broadcast a normal message. The message table
has no draft state. Caller-supplied client_message_id is not part of the meeting API;
the wrapper generates a request UUID for each call. Identical content submitted twice
is two independent utterances, as in normal Team messaging.

The playback worker calls grant_next to claim one request. After the lease commits,
the coordinator invokes the existing group-chat post_message path with the queued
content, author, and mentions. That path performs ordinary message creation,
history projection, and Team broadcast notification. Agents continue fetching their
own inboxes and acknowledging input through existing handlers. Text is sent before
the host starts audio; audio playback is external to the wrapper.

The coordinator then records the returned message_id on the speech request and clears
its queued content. MeetingDao only updates its own request/meeting rows; it never
reads or writes message rows. MessageDao, TeamDatabase, and ordinary Team send methods
need no new transaction-aware methods or promotion APIs. SpeechRequest.content is
read from queued content before sending and from the referenced message afterward.

The existing group path retains its internal client ID and metadata contract. The
wrapper supplies its own request UUID internally, never requiring an agent/application
to manage those IDs. Group history is shared at the existing team/session conversation scope; no
meeting_id tag or changes to group_chat/handler.py are needed. Existing group history
synchronization is retained; its performance follow-up remains deferred. No separate message transport or distributor is added.

## 13. Host meeting coordinator

MeetingCoordinator(database, scope, message_manager=..., conversation=...) attaches
existing resources without starting another Team. start creates or observes the
meeting; it never reopens a closed ID. The host schedules grant_next, stops audio on
interruption, and acknowledges completion. Reads (get_request, list_pending,
get_speaker, source/history inspection) never send messages or acknowledge inboxes.

SpeechRequest includes the spoken summary and full content. source_message_id and
message are absent while queued; this is not a lookup error. After sending, the result
also includes the persisted SpeechSource. Source hydration failure returns message_error
without hiding the current token. A sending/history/link-recording failure returns
that token with delivery_error, leaves unsent queue content available, and blocks a
successor until the host recovers or releases the lease. The host must not start audio
while either error is present. retry_publication targets only the current token and
uses the existing group submission identity to recover committed text; it does not
implement posting deduplication. Recovery of committed text is handled inside the
coordinator and does not require its author or recipients to remain active. New
sends continue using the unchanged group path and its membership validation.

Sending and recording its result are deliberately separate normal operations. If sending
stores the message and a subsequent step fails, that message remains published and queue
content remains until its link is recorded. Existing group submission identity lets
current-token recovery find the same message. This is not a cross-DAO atomic transaction.
If sending returned an ID before queue-link recording failed, the error result
retains that known source_message_id; the persisted queue may still have no link.
Recovery repairs history/notification through existing helpers before recording
the source link and clearing queued content. It never inserts a second message
when the existing submission is found.
Bus errors retain normal Team semantics: they are logged, and inbox polling can still
consume the stored message; notification is not a recipient-delivery acknowledgement.

Interruption/close may overlap sending. Already sent text remains; the coordinator
records its link even after revocation, then rechecks the lease before returning
playable work. A revoked/closed turn returns None. complete_playback releases only
the matching lease and never sends again or automatically grants a successor.
Cancellation or close before grant never creates a normal message for that request.

request_speech remains a trusted compatibility path for an already existing source.
The coordinator validates its team/author through ordinary message retrieval. It
cannot retroactively delay text already sent. This path needs no sending dependencies.

Empty legacy queue tables are rebuilt with queued content, mentions, and nullable
source linkage. Populated older schemas fail with AGENT_TEAM_STATE_INVALID and are
preserved; use a new session or explicitly release the older session. No persisted
source links are invented and no existing data is silently discarded.

## 14. Member-bound publication

for_member binds trusted runtime identity. Member operations check registration,
agent role, and departure status. post_message(content, *, speech_text, mentions=())
validates text and recipients, generates a request ID, and queues both versions.
The receipt has message.message_id=None and published=False. Its speech request
carries the full content without requiring a separate fetch. IDs are generated
internally; tools accept no sender, scope, client ID, or delivery-control overrides.

Human post_human_message(content, *, mentions=()) invokes existing sending immediately
without entering the speech queue. Member history/source access exposes public group
messages in the bound team/session
conversation, including ordinary group posts and other meeting instances. Private
messages and other teams/sessions remain excluded. Meeting scope identifies the
queue and lease; it is not a message/history partition. Existing group fields such
as sender_name, attachments,
and internal client IDs remain confined to that existing integration.

create_meeting_tools provides posting, public history, current speaker inspection,
and Leader-only interruption. Runtime send-path installation, same-model/context
summary generation, playback ownership, host stop notifications, and crash/disconnect
reconciliation remain later integration work. This explicit wrapper does not claim
complete runtime installation.
