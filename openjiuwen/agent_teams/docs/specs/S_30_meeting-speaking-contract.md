# Meeting Speaking Contract — Draft v0.1

## Status and scope

| Item | Value |
|---|---|
| Status | Database foundation implemented; runtime/audio integration remains proposed |
| Date | 2026-10-16 |
| Scope | PR2: meeting coordination in `agent-core` |
| Requirements | [agent-core #1951](https://github.com/openJiuwen-ai/agent-core/issues/1951), [JiuwenSwarm #8260](https://github.com/openJiuwen-ai/jiuwenswarm/issues/8260) |
| Foundation | [agent-core #1905](https://github.com/openJiuwen-ai/agent-core/pull/1905) |

**Required** denotes behavior specified by the issues. **Proposed** denotes a design choice to review together. Names and signatures below are illustrative, not existing APIs.

The initial scope is one human meeting one Team in a group conversation. Audio capture, VAD, ASR, meaningful-interruption classification, recipient routing, TTS, and playback belong to JiuwenSwarm. PR2 provides speaking ownership, outgoing-message coordination, interruption APIs, and spoken-summary generation.

## 1. Ownership and boundaries

| Responsibility | Owner |
|---|---|
| Determine human intent, recipient, and whether speech is a meaningful interruption | JiuwenSwarm |
| Order agents waiting to speak; grant and revoke ownership | `agent-core` |
| Publish the original complete text through existing delivery paths | `agent-core` |
| Generate a short spoken version with the originating agent's model and context | `agent-core` |
| Choose the configured voice, synthesize audio, play and stop audio | JiuwenSwarm |
| Report playback completion, failure, or cancellation | JiuwenSwarm |
| Decide whether a sub-agent should be interrupted | Leader, through its meeting tool |

**Required:** reuse existing Team messaging and group-chat mechanisms. Meeting coordination must not create a second message transport. #1905's incoming mailbox hook does not by itself gate outgoing publication; the integration must cover that boundary explicitly.

**Required:** meetings are opt-in. Disabled meetings preserve current tool availability, message delivery, output streaming, and model-call behavior.

## 2. Speaking ownership

**Required**

1. There is at most one speaking owner per meeting.
2. Human-facing replies, agent-to-agent messages, and broadcasts must acquire ownership before their complete text is published.
3. Normal release happens after text publication and audio playback have completed.
4. Waiting to speak must not prevent an agent from continuing independent thinking or tool work.
5. Humans bypass the agent queue.
6. Interrupting speech preserves text that has already been published.
7. Speech interruption does not itself cancel tools, discard plans, or invoke #1905's execution interruption.

**Proposed**

- Scope ownership by `(session_id, team_name, meeting_id)`. A new meeting instance gets a new `meeting_id`, even in an existing conversation.
- Initially, one queued speech request corresponds to one finalized outgoing message and its spoken version. Multicast is one logical request, with several recipients.
- Every request has a stable `request_id`; every grant has an opaque `lease_token`. Neither is just a member name.
- Playback callbacks must match the meeting instance and lease token. Duplicate and stale callbacks are harmless and cannot release a successor.
- Internal lifecycle events, approvals, and system notifications bypass speaking coordination. Their existing delivery must continue while an agent is speaking.
- Partial model output must not escape as the visible reply before ownership. The SDK must gate the human-facing output path as well as messaging tools.

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
QUEUED -> GRANTED -> TEXT_PUBLISHED -> SPEECH_READY -> PLAYING -> COMPLETED
```

- **QUEUED:** the SDK accepted the outgoing operation; it has not sent the text yet.
- **GRANTED:** this request holds speaking ownership.
- **TEXT_PUBLISHED:** the existing delivery path committed/published the full text.
- **SPEECH_READY:** the SDK supplies the original text, spoken version, author, message identity, and lease token to the application.
- **PLAYING:** the application is synthesizing/playing the spoken version under that token.
- **COMPLETED:** the application acknowledges that playback has finished; the SDK releases ownership.

**Proposed:** sending while queued returns a clearly marked pending receipt, rather than claiming the message was delivered. The actual send outcome becomes available through the existing background-operation/completion mechanism. Exact tool-result compatibility needs review before implementation.

The Leader must be able to inspect the current speaker's full text while its audio plays. Otherwise its interruption tool cannot make the judgment required by the RFC.

Text publication and summary generation are ordered: publish the full text first, then generate the spoken version. Summary generation must not modify the text message or the agent's conversation history.

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

**Required**

- Use the originating agent's same model and context for one additional request.
- Produce a short conversational summary: conclusion first; do not read code, tables, or links aloud.
- Deliver the full text and spoken version to the application; the application performs TTS.

**Proposed**

- Preserve the source language and material conclusions. Add no new decisions or tool actions.
- Use a copied context snapshot associated with the outgoing message and the same effective model configuration. Do not recursively run the agent's task loop or append this auxiliary request to its history.
- Complete tool-call/result pairs must be maintained in the summary request; do not invent tool results to make an incomplete context valid. The precise snapshot boundary is an open decision below.
- Put a finite timeout on summary generation. On failure, retain the full text, report that speech could not be generated, and release ownership. Do not silently read the entire original message or switch models.
- External runtimes without access to the required model/context need an explicit adapter contract; their support is not assumed.

## 7. Illustrative SDK operations and events

These describe semantics rather than final Python names or signatures.

| Operation | Contract |
|---|---|
| `request_speech(...) -> request_id` | Register one outgoing operation and return promptly; derive author and priority from trusted runtime state. |
| `wait_for_grant(request_id) -> lease` | Internal/background wait; cancelling the wait cancels the corresponding queued request. |
| `cancel_request(request_id)` | Cancel an unsent operation; never retract a published message. |
| `get_speaker() -> lease or None` | Return current ownership and the published message identity, when available. |
| `complete_playback(lease_token, outcome)` | Release only the matching current lease; duplicate/stale completion is a no-op. |
| `interrupt_speech(reason, expected_token?) -> interruption_id` | Trusted application/authorized Leader revokes ownership and starts the proposed interruption barrier. |
| `resume_after_interruption(interruption_id)` | Application confirms old audio is stopped and, for human interruptions, human input has been admitted. |
| `close_meeting(meeting_id)` | Revoke ownership, cancel pending sends/summary work, stop audio through the application, and reject late callbacks. |

Proposed application notifications:

- `speech_ready`: meeting/request/message identities, lease token, member, recipients/message kind, full text, and spoken summary.
- `speech_interrupted`: revoked token, interruption identity, reason, and initiating actor.
- `speech_failed`: request/token, failure phase, whether text was already published, and actionable error information.

Exact event names and transport mapping should reuse the existing event infrastructure. Events notify consumers; authoritative ownership must not depend on a subscriber receiving every event.

## 8. Failure, shutdown, and process coordination

**Proposed**

- Queue and ownership changes must be atomic across supported member processes. A process-local lock is insufficient.
- Shared team-database state is the chosen authority for the database foundation (§11). Existing message-bus notifications remain a runtime integration step.
- Never hold a database transaction while waiting for a model, a tool, another speaker, or audio playback.
- A failed text publication releases ownership and reports failure. A summary/TTS failure retains published text and also releases through the failure lifecycle.
- Releasing after synthesis/playback failure requires the application to confirm any active audio has stopped.
- A disconnected application or dead owner must not leave an invisible permanent lock. Fault-recovery timeouts initiate stop/revocation; they must not grant a successor while old playback could still continue.
- Fault-recovery deadlines are not a normal maximum speech duration. Automatic time-based speech interruption is deferred by the RFC.
- Closing a meeting cancels pending outgoing operations and auxiliary summary requests, while preserving published text and unrelated agent work.
- Meeting state must remain isolated across teams, sessions, and meeting restarts. Recovery of a persisted conversation must not replay old audio automatically.

## 9. Acceptance scenarios

1. Two independent member processes cannot own the same meeting simultaneously.
2. Leader, addressed-member, and ordinary-member queue priority works; ties preserve arrival order.
3. Normal priority never preempts the current speaker.
4. Waiting to speak does not block unrelated agent work.
5. Replies, A2A messages, broadcasts, and multicast all pass through the speaking gate; internal control traffic does not.
6. Text is published once through its existing path before its spoken version is offered for playback.
7. Playback completion releases the matching lease; an old callback cannot release the next turn.
8. Human interruption stops speech, preserves text, admits the human message first, and then reopens the queue.
9. Only the Leader can invoke the agent-facing interruption tool; a stale target cannot interrupt a successor.
10. Late summary/synthesis results after interruption are discarded.
11. Queue cancellation, summary failure, TTS failure, disconnect, and meeting shutdown leave no permanent ownership or unhandled background tasks.
12. Disabled meetings preserve current behavior, toolsets, and model-call counts.

## 10. Decisions to settle together before implementation

| Decision | Proposed starting point | Alternative / unresolved detail |
|---|---|---|
| Ownership unit | One finalized outgoing message plus its spoken version | Let an agent retain the floor for several messages; requires an explicit end-of-speech contract. |
| Meaning of addressed priority | Latest admitted human routing result; update pending priorities | Keep priority fixed at queue admission, or bind it to the originating human turn. |
| Cross-process authority | Shared team database plus existing bus notifications | Leader-owned coordinator; requires request/grant protocol and failover design. |
| Interruption handoff | Revoke immediately, reopen after stop/input acknowledgement | Release and grant immediately; application must independently prevent overlapping playback. |
| Queued tool behavior | Explicit pending receipt, eventual real send result | Block the tool call until publication; simpler, but may stall agent work. |
| Summary snapshot | Immutable context associated with finalized outgoing message | Decide the exact valid boundary for message-tool calls and parallel tools. |
| Initial runtime support | Native agents first; explicit capability check | Include external CLI members only after defining their same-model/context adapter. |
| Recovery policy and limits | Explicit failure/stop handoff, no silent audio replay | Define concrete timeouts, pending-operation recovery, and playback-owner authority. |

The first implementation step is the shared queue database, explicitly requested on 2026-10-16.
The remaining runtime, delivery, summary, and application contracts remain proposals.

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
table drop/cleanup. Explicit meeting creation can initialize its session's two tables
without changing the caller's bound session. Team deletion cascades through the existing
`team_info` foreign-key convention.

All mutations first update the meeting row, acquiring a database write/row lock before
reading queue state. This serializes independent engines and member processes, beyond the
existing process-local `DbSessions` lock. No transaction spans publication, models, or playback.
An increasing per-meeting sequence defines FIFO, independent of clock resolution.

`create_meeting` snapshots the team's leader identity; `enqueue` accepts only registered
non-human members and has no priority argument. `set_addressed_members` replaces the trusted
routing result. `list_pending` and `grant_next` rank pending requests using the latest result,
then sequence. Priority changes never revoke an owner. Reusing a request ID returns the
existing request for that author, including terminal requests, without re-enqueueing it.

`grant_next` returns a request with a fresh opaque lease token, or `None` when owned,
interrupted, closed, or empty. `complete_playback` releases only a matching current token.
`cancel_request` only cancels queued requests. Late playback and interruption acknowledgements
after session cleanup are harmless no-ops. `interrupt_speech` revokes the current request
and persists an interruption ID; no successor can be granted until
`resume_after_interruption` acknowledges that exact ID. `close_meeting` cancels pending
requests and revokes ownership permanently. Terminal requests remain as deduplication records.

This layer stores coordination metadata only. Full text, delivery, speech generation,
audio stop acknowledgement, leader-tool authorization, recovery deadlines, and bus
notifications belong to the forthcoming coordinator. Persisted leases are never automatically
replayed or expired by this DAO. Session release and runtime cleanup remove the scoped rows.
