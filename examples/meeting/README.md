# Meeting wrapper integration

The order is **queue content + summary -> acquire ownership -> send full text through
existing Team messaging -> play summary -> release**. Admission creates no normal
message. Agents keep fetching their own inboxes. Human input sends immediately. History is the existing public team/session group
conversation, shared across meetings; each meeting keeps its own queue and lease.
No meeting tag is added to messages, and group_chat/handler.py is unchanged.

```python
from openjiuwen.agent_teams import MeetingCoordinator, MeetingScope, create_meeting_tools

meeting = MeetingCoordinator(
    existing_database,
    MeetingScope(session_id="discussion-1", team_name="research", meeting_id="meeting-1"),
    message_manager=existing_message_manager,
    conversation=existing_group_conversation,
)
await meeting.start()
member = meeting.for_member(trusted_member_name)
posted = await member.post_message(
    complete_agent_text,
    speech_text=agent_spoken_summary,
    mentions=[recipient_member_name],
)
# Full text and summary are in the speaking queue, not the message table.
assert posted.message.message_id is None
assert posted.speech_request.source_message_id is None
assert posted.speech_request.content == complete_agent_text
assert not posted.published

tools = await create_meeting_tools(member, language="en")

speech = await meeting.grant_next()
if speech is not None:
    # Existing group-message sending ran after the lease was committed.
    # Agents can now fetch the message normally; queue content has become a source link.
    if speech.message_error or speech.delivery_error:
        # Keep the same token; recover or explicitly release it before advancing.
        speech = await meeting.retry_publication(speech.lease_token)
    if speech is not None and not speech.message_error and not speech.delivery_error:
        full_text = speech.content
        source_id = speech.source_message_id
        # Host synthesizes and plays speech.speech_text.
        # It must discard late synthesis results after token revocation.

# Only after actual playback completion:
await meeting.complete_playback(finished_lease_token)
```

Reads and queue inspection never send. Each post gets an internally generated request
UUID; identical posts create separate utterances. The source_message_id foreign key is
empty until sending creates a normal message, then the queued full text is cleared.
Sending uses existing group history, notification, and Team inbox acknowledgement.
Notification failures keep normal Team polling behavior. History or link-recording
failure can happen after storage; the current lease is returned with an error, and
recovery uses the same existing group submission identity rather than creating another
message. Sending and recording its ID are separate operations.

Human interruption: call interrupt_speech, stop playback/discard synthesis for the
revoked token, call post_human_message(content, mentions=...), update addressed-member
priority, then acknowledge resume_after_interruption. Leader interruption targets its
observed token. Cancellation/close before grant never inserts a normal message.
Already sent text remains after interruption; a revoked grant returns None.

Existing-source request_speech remains available for trusted compatibility callers.
It cannot delay a message already sent. Populated older queue schemas are preserved
and rejected with an explicit error; use a new session or release the old one. Empty
older queues are rebuilt automatically.

Automatic TeamSpec/spawn and send-path installation, same-model/context summaries,
physical playback ownership, and crash/disconnect recovery remain later work. The
existing sync_history cost issue remains recorded for review after commit.
