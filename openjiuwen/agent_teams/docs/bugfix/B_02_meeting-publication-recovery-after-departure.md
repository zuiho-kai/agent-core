# Recover meeting publication after member departure

| Item | Value |
|---|---|
| Date | 2026-10-21 |
| Scope | MeetingCoordinator publication recovery |
| Refs | #1951 |
| Spec | S_30_meeting-speaking-contract.md, section 13 |

## Failure and cause

Normal sending can commit text before history projection or speech-queue link
recording fails. The queue then retains content with no source reference.
Previously, retry_publication called normal group posting again. That path checks
current author/recipient membership before looking up an existing submission.
If either member departed after the initial send, recovery failed even though
the source message remained stored. The current lease required explicit release.

The original error result also discarded a message ID already returned by sending
when the subsequent queue-link update failed.

## Fix

Keep the fix inside MeetingCoordinator. Current-token recovery derives the same
existing team/session/request submission identity used by group posting and reads
that message. Validate its public conversation, author, content, and mentions,
repair history and broadcast notification using existing helpers, then record its
queue link. A missing source falls back to normal sending with normal membership
checks. Preserve an ID already returned by sending in a link-failure result.

Group-chat implementation, normal message storage, queue schema, grant ordering,
and playback acknowledgement remain unchanged. A retained source ID in an error
result does not mean the queue-link transaction committed; playback still waits
for error-free recovery. No process-local recovery cache is introduced.

## Validation

Regression cases cover sender/recipient departure after history or link-recording
failure, one persisted source, transcript repair, retained lease, queue-content
cleanup, stale retry rejection, and normal rejection of unsent content involving
departed members. The final coordinator-only implementation passes 89 publication/
coordinator cases. The queue/database suite's 41 cases and group suites' 42 cases
also passed during this fix; group_chat was then restored exactly to HEAD.
Ruff checks, formatting checks, and git diff --check pass for the final change.
