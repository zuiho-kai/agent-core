# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-scoped public publication, retries, and dedicated meeting tool capabilities."""

import asyncio
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams import MeetingCoordinator, MeetingScope, SpeechRequestStatus, create_meeting_tools
from openjiuwen.agent_teams.context import get_session_id, reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.group_chat.handler import post_message
from openjiuwen.agent_teams.group_chat.message_handler import GroupMessageHandler
from openjiuwen.agent_teams.paths import reset_task_openjiuwen_home, set_task_openjiuwen_home
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.agent_teams.tools.team import TeamBackend

pytestmark = [pytest.mark.asyncio, pytest.mark.level1]
SCOPE = MeetingScope("session", "team", "meeting")


@pytest_asyncio.fixture
async def runtime(tmp_path):
    # Extend only workspace paths; SQLite URLs interpret the prefix's '?' as a query.
    workspace = Path(f"\\\\?\\{tmp_path}") if sys.platform == "win32" else tmp_path
    home = set_task_openjiuwen_home(workspace)
    db = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "meeting.db")))
    await db.initialize()
    try:
        await db.team.create_team("team", "Team", "captain")
        for name, role in (("captain", "leader"), ("a", "teammate"), ("b", "teammate"), ("human", "human_agent")):
            await db.member.create_member(name, "team", name, "{}", "ready", role=role)
        manager = TeamMessageManager("team", "captain", db, AsyncMock())
        conversation = await asyncio.to_thread(GroupConversationLog, "team", "session")
        coordinator = MeetingCoordinator(db, SCOPE, message_manager=manager, conversation=conversation)
        await coordinator.start()
        yield SimpleNamespace(db=db, manager=manager, conversation=conversation, meeting=coordinator)
    finally:
        await db.close()
        reset_task_openjiuwen_home(home)


async def _post(client, label="utterance", **overrides):
    arguments = dict(
        content="Complete explanation",
        speech_text="Spoken summary",
        mentions=["b"],
    )
    arguments.update(overrides)
    return await client.post_message(**arguments)


async def _rows(runtime):
    token = set_session_id("session")
    try:
        return await runtime.db.message.get_team_messages("team")
    finally:
        reset_session_id(token)


async def test_queue_is_invisible_until_grant_publishes_text_before_playback(runtime):
    member = runtime.meeting.for_member("a")
    result = await _post(member)
    request = result.speech_request
    assert not result.published and result.message.message_id is None
    assert request.source_message_id is None and request.message is None and request.message_error is None
    assert request.content == result.message.content and request.speech_text == "Spoken summary"
    assert request.mentions == ("b",) and request.status == SpeechRequestStatus.QUEUED
    assert await _rows(runtime) == []
    assert await member.get_history() == () and await member.get_speaker() is None
    runtime.manager.messager.publish.assert_not_awaited()
    assert not runtime.conversation.history_path.exists()
    token = set_session_id("session")
    try:
        assert await runtime.db.message.get_messages("team", "b") == []
        assert await runtime.db.message.get_broadcast_messages("team", "b") == []
        assert not await runtime.db.message.has_unread_messages("team")
    finally:
        reset_session_id(token)
    # Read-only queue inspection must not create or send a message.
    assert (await runtime.meeting.get_request(request.request_id)).content == result.message.content
    assert (await runtime.meeting.list_pending())[0].content == result.message.content
    assert await _rows(runtime) == []
    lease = await runtime.meeting.grant_next()
    assert lease.message.content == result.message.content and lease.content == result.message.content
    assert lease.source_message_id == lease.message.message_id
    assert lease.message.broadcast and lease.message_error is None and lease.delivery_error is None
    assert (await member.get_history())[0].message_id == lease.source_message_id
    stored = await runtime.db.meeting.get_request(SCOPE, lease.request_id)
    assert stored.content is None and stored.source_message_id == lease.source_message_id
    rows = await _rows(runtime)
    assert len(rows) == 1 and rows[0].content == result.message.content and rows[0].broadcast
    event = runtime.manager.messager.publish.call_args.kwargs["message"]
    assert event.get_payload().message_id == lease.source_message_id
    assert await runtime.meeting.grant_next() is None
    with pytest.raises(FrozenInstanceError):
        lease.message.content = "overwrite"


async def test_meetings_keep_separate_queues_and_leases_with_shared_conversation_history(runtime):
    first = await _post(runtime.meeting.for_member("a"))
    second = await _post(runtime.meeting.for_member("b"))
    other = MeetingCoordinator(
        runtime.db,
        MeetingScope("session", "team", "other"),
        message_manager=runtime.manager,
        conversation=runtime.conversation,
    )
    await other.start()
    third = await _post(other.for_member("a"))
    assert len({r.speech_request.request_id for r in (first, second, third)}) == 3
    assert len(await runtime.meeting.list_pending()) == 2
    assert len(await other.list_pending()) == 1
    assert await _rows(runtime) == []
    expected = []
    for _ in range(2):
        lease = await runtime.meeting.grant_next()
        expected.append(lease.source_message_id)
        await runtime.meeting.complete_playback(lease.lease_token)
    other_lease = await other.grant_next()
    expected.append(other_lease.source_message_id)
    assert {m.message_id for m in await runtime.meeting.for_member("a").get_history()} == set(expected)
    assert {m.message_id for m in await other.for_member("a").get_history()} == set(expected)
    source = await runtime.meeting.for_member("a").get_source_message(other_lease.source_message_id)
    assert source.message_id == other_lease.source_message_id
    assert await runtime.meeting.get_speaker() is None
    assert (await other.get_speaker()).lease_token == other_lease.lease_token
    assert not await runtime.meeting.complete_playback(other_lease.lease_token)


async def test_member_reads_share_group_conversation_but_exclude_private_and_other_team_messages(runtime):
    member = runtime.meeting.for_member("a")
    await _post(member)
    lease = await runtime.meeting.grant_next()
    published = await member.get_source_message(lease.source_message_id)
    ordinary = await post_message(
        runtime.conversation, runtime.manager, "a", "Other discussion", client_message_id="ordinary"
    )
    token = set_session_id("session")
    try:
        await runtime.db.message.create_message(
            "private", "team", "b", "Private", to_member_name="a", inline_content=True
        )
        await runtime.db.message.create_message(
            "ordinary-broadcast", "team", "b", "Non-group broadcast", broadcast=True, inline_content=True
        )
        await runtime.db.team.create_team("other-team", "Other team", "captain")
        await runtime.db.message.create_message(
            "other-team-group",
            "other-team",
            "b",
            "Other conversation",
            broadcast=True,
            meta={"type": "group_chat", "mentions": []},
            inline_content=True,
        )
        assert await runtime.db.message.mark_message_read(lease.source_message_id, "b")
        before = await runtime.db.message.get_broadcast_read_at("team", "b")
    finally:
        reset_session_id(token)
    shared = await member.get_source_message(ordinary.message.message_id)
    assert shared.content == "Other discussion"
    assert {m.message_id for m in await member.get_history()} == {published.message_id, shared.message_id}
    assert await member.get_source_message("missing") is None
    for message_id in ("private", "ordinary-broadcast", "other-team-group"):
        with pytest.raises(ValueError, match="this public conversation"):
            await member.get_source_message(message_id)
    token = set_session_id("session")
    try:
        assert await runtime.db.message.get_broadcast_read_at("team", "b") == before
        assert not (await runtime.db.message.get_message("private")).is_read
    finally:
        reset_session_id(token)


async def test_new_post_after_playback_creates_new_request(runtime):
    member = runtime.meeting.for_member("a")
    first = await _post(member)
    lease = await runtime.meeting.grant_next()
    assert await runtime.meeting.complete_playback(lease.lease_token)
    second = await _post(member)
    assert first.speech_request.request_id != second.speech_request.request_id
    assert second.message.message_id is None and not second.published
    assert len(await runtime.meeting.list_pending()) == 1 and len(await _rows(runtime)) == 1


@pytest.mark.parametrize("override", [{}, dict(content="changed"), dict(speech_text="changed"), dict(mentions=["a"])])
@pytest.mark.parametrize("phase", ["queued", "granted", "completed"])
async def test_repeated_content_is_a_separate_post(runtime, override, phase):
    member = runtime.meeting.for_member("a")
    first = await _post(member)
    if phase != "queued":
        lease = await runtime.meeting.grant_next()
        if phase == "completed":
            await runtime.meeting.complete_playback(lease.lease_token)
    original = await runtime.meeting.get_request(first.speech_request.request_id)
    second = await _post(member, **override)
    assert second.speech_request.request_id != original.request_id
    assert await runtime.meeting.get_request(original.request_id) == original
    assert second.speech_request.source_message_id is None
    assert len(await _rows(runtime)) == (0 if phase == "queued" else 1)


async def test_admission_failure_never_publishes_text(runtime, monkeypatch):
    member = runtime.meeting.for_member("a")
    original = runtime.db.meeting.enqueue
    monkeypatch.setattr(runtime.db.meeting, "enqueue", AsyncMock(side_effect=SQLAlchemyError("transient")))
    with pytest.raises(SQLAlchemyError):
        await _post(member)
    assert await _rows(runtime) == []
    assert await member.get_history() == ()
    runtime.manager.messager.publish.assert_not_awaited()
    monkeypatch.setattr(runtime.db.meeting, "enqueue", original)
    repaired = await _post(member)
    assert not repaired.duplicate and not repaired.published
    assert repaired.speech_request.content == repaired.message.content


async def test_close_during_grant_dispatch_preserves_published_text_but_returns_no_playable_lease(runtime, monkeypatch):
    original = runtime.manager.publish_broadcast

    async def publish_then_close(*args, **kwargs):
        await original(*args, **kwargs)
        await runtime.meeting.close()

    monkeypatch.setattr(runtime.manager, "publish_broadcast", publish_then_close)
    posted = await _post(runtime.meeting.for_member("a"))
    assert await runtime.meeting.grant_next() is None
    history = await runtime.meeting.for_member("a").get_history()
    assert (
        history[0].message_id == (await runtime.meeting.get_request(posted.speech_request.request_id)).source_message_id
    )
    request = await runtime.meeting.get_request(posted.speech_request.request_id)
    assert request.status == SpeechRequestStatus.INTERRUPTED
    assert await runtime.meeting.get_speaker() is None


async def test_human_publication_uses_shared_transcript_without_audio(runtime):
    result = await runtime.meeting.post_human_message("Human instruction", mentions=["a"])
    assert result.message.member_name == "user"
    assert result.notified_members == ("a",)
    assert result.speech_request is None and result.audio_error is None
    assert await runtime.meeting.list_pending() == ()
    assert await runtime.meeting.for_member("a").get_history() == (result.message,)


@pytest.mark.parametrize("name", ["unknown", "human", "user"])
async def test_member_capabilities_reject_non_agent_identity(runtime, name):
    client = runtime.meeting.for_member(name)
    with pytest.raises(ValueError, match="registered agent"):
        await _post(client)
    with pytest.raises(ValueError, match="registered agent"):
        await client.get_history()
    assert await _rows(runtime) == []


async def test_departed_member_loses_publication_and_read_capabilities(runtime):
    member = runtime.meeting.for_member("a")
    await _post(member)
    assert await runtime.db.member.update_member_status("a", "team", "shutdown_requested")
    with pytest.raises(ValueError, match="registered agent"):
        await _post(member, "another")
    with pytest.raises(ValueError, match="registered agent"):
        await member.get_history()


@pytest.mark.parametrize(
    "override",
    [
        dict(content=" "),
        dict(speech_text=" "),
        dict(mentions=[""]),
        dict(mentions=["unknown"]),
        dict(mentions="b"),
    ],
)
async def test_invalid_post_is_rejected_before_persistence(runtime, override):
    with pytest.raises(ValueError):
        await _post(runtime.meeting.for_member("a"), **override)
    assert await _rows(runtime) == []
    assert await runtime.meeting.list_pending() == ()


async def test_closed_meeting_rejects_new_publication_but_retains_history(runtime):
    member = runtime.meeting.for_member("a")
    await _post(member)
    lease = await runtime.meeting.grant_next()
    assert await runtime.meeting.close()
    with pytest.raises(ValueError, match="open"):
        await _post(member, "new")
    assert (await member.get_history())[0].message_id == lease.source_message_id


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
async def test_history_limit_is_validated(runtime, limit):
    with pytest.raises(ValueError, match="History limit"):
        await runtime.meeting.for_member("a").get_history(limit=limit)


async def test_context_is_restored_after_publication_and_history(runtime):
    token = set_session_id("unrelated")
    try:
        member = runtime.meeting.for_member("a")
        await _post(member)
        assert get_session_id() == "unrelated"
        assert await member.get_history(limit=1) == ()
        lease = await runtime.meeting.grant_next()
        assert get_session_id() == "unrelated"
        published = await member.get_source_message(lease.source_message_id)
        assert await member.get_history(limit=1) == (published,)
        assert published.content == lease.message.content
        assert get_session_id() == "unrelated"
    finally:
        reset_session_id(token)


@pytest.mark.parametrize("language", ["en", "cn"])
async def test_dedicated_tools_expose_only_bound_member_capabilities(runtime, language):
    member = runtime.meeting.for_member("a")
    tools = await create_meeting_tools(member, language=language)
    assert set(tools) == {"meeting_post_message", "meeting_read_history", "meeting_get_speaker"}
    for operation in ("grant_next", "complete_playback", "close", "post_human_message", "request_speech"):
        assert not hasattr(member, operation)
    inputs = dict(content="Complete explanation", speech_text="Spoken summary")
    for override in ("sender", "member_name", "team_name", "session_id", "meeting_id", "to", "client_message_id"):
        result = await tools["meeting_post_message"].invoke({**inputs, override: "spoof"})
        assert not result.success
    assert await _rows(runtime) == []
    result = await tools["meeting_post_message"].invoke(inputs)
    assert result.success
    assert result.data["message"]["member_name"] == "a"
    history = await tools["meeting_read_history"].invoke({})
    assert history.success and history.data == []
    assert not result.data["message"]["published"]
    await runtime.meeting.grant_next()
    history = await tools["meeting_read_history"].invoke({})
    assert history.success and len(history.data) == 1
    assert not (await tools["meeting_read_history"].invoke({"limit": True})).success


async def test_leader_tool_is_gated_and_runtime_authorization_is_rechecked(runtime):
    member = runtime.meeting.for_member("a")
    await _post(member)
    lease = await runtime.meeting.grant_next()
    with pytest.raises(ValueError, match="Only the meeting Leader"):
        await member.interrupt_speech(expected_token=lease.lease_token)
    leader = runtime.meeting.for_member("captain")
    tools = await create_meeting_tools(leader)
    assert "meeting_interrupt_speech" in tools
    result = await tools["meeting_interrupt_speech"].invoke({"expected_token": lease.lease_token})
    assert result.success and result.data["lease_token"] == lease.lease_token
    assert await runtime.db.member.update_member_status("captain", "team", "shutdown_requested")
    assert not (await tools["meeting_interrupt_speech"].invoke({"expected_token": lease.lease_token})).success


@pytest.mark.parametrize("language", ["en", "cn"])
async def test_speaker_tool_exposes_full_source_and_token_without_acknowledging_delivery(runtime, language):
    leader_tools = await create_meeting_tools(runtime.meeting.for_member("captain"), language=language)
    tool = leader_tools["meeting_get_speaker"]
    idle = await tool.invoke({})
    assert idle.success and idle.data is None
    assert not (await tool.invoke({"meeting_id": "other"})).success
    posted = await _post(runtime.meeting.for_member("a"), content="Full explanation " * 200)
    lease = await runtime.meeting.grant_next()
    observed = await tool.invoke({})
    assert observed.success
    assert observed.data["request"]["lease_token"] == lease.lease_token
    assert observed.data["source"]["content"] == posted.message.content
    assert observed.data["source"]["message_id"] == lease.source_message_id
    token = set_session_id("session")
    try:
        assert await runtime.db.message.get_broadcast_read_at("team", "captain") == 0
    finally:
        reset_session_id(token)
    interrupted = await leader_tools["meeting_interrupt_speech"].invoke(
        {"expected_token": observed.data["request"]["lease_token"]}
    )
    assert interrupted.success and interrupted.data["lease_token"] == lease.lease_token
    assert (await tool.invoke({})).data is None


async def test_speaker_tool_observation_cannot_interrupt_a_successor(runtime):
    tools = await create_meeting_tools(runtime.meeting.for_member("captain"))
    await _post(runtime.meeting.for_member("a"))
    first = await runtime.meeting.grant_next()
    observed = await tools["meeting_get_speaker"].invoke({})
    await _post(runtime.meeting.for_member("b"))
    assert await runtime.meeting.complete_playback(first.lease_token)
    successor = await runtime.meeting.grant_next()
    result = await tools["meeting_interrupt_speech"].invoke({"expected_token": observed.data["request"]["lease_token"]})
    assert result.success and result.data is None
    assert await runtime.meeting.get_speaker() == successor


async def test_speaker_tool_rechecks_member_and_source_scope(runtime):
    tools = await create_meeting_tools(runtime.meeting.for_member("a"))
    token = set_session_id("session")
    try:
        await runtime.db.message.create_message(
            "private-source", "team", "b", "Private", to_member_name="captain", inline_content=True
        )
    finally:
        reset_session_id(token)
    await runtime.meeting.request_speech(
        "b", request_id="private", source_message_id="private-source", speech_text="Private summary"
    )
    await runtime.meeting.grant_next()
    rejected = await tools["meeting_get_speaker"].invoke({})
    assert not rejected.success
    assert await runtime.db.member.update_member_status("a", "team", "shutdown_requested")
    rejected = await tools["meeting_get_speaker"].invoke({})
    assert not rejected.success and "registered agent" in rejected.error


async def test_ordinary_group_message_identity_is_unchanged(runtime):
    expected = str(uuid5(NAMESPACE_URL, json.dumps(["team", "session", "normal"], ensure_ascii=False)))
    result = await post_message(runtime.conversation, runtime.manager, "a", "Normal", client_message_id="normal")
    assert result.message.message_id == expected
    normal_metadata = json.loads((await _rows(runtime))[0].meta)
    assert "meeting_id" not in normal_metadata
    assert (await runtime.meeting.for_member("a").get_history())[0].message_id == result.message.message_id
    await _post(runtime.meeting.for_member("a"))
    lease = await runtime.meeting.grant_next()
    metadata = next(json.loads(row.meta) for row in await _rows(runtime) if row.message_id == lease.source_message_id)
    assert set(metadata) == set(normal_metadata)


async def test_publication_requires_matching_existing_dependencies(runtime):
    mismatched = TeamMessageManager("other-team", "a", runtime.db, AsyncMock())
    with pytest.raises(ValueError, match="Message manager"):
        MeetingCoordinator(runtime.db, SCOPE, message_manager=mismatched)
    with pytest.raises(ValueError, match="Conversation"):
        MeetingCoordinator(
            runtime.db, MeetingScope("other-session", "team", "meeting"), conversation=runtime.conversation
        )
    unconfigured = MeetingCoordinator(runtime.db, SCOPE)
    with pytest.raises(ValueError, match="requires the existing"):
        await _post(unconfigured.for_member("a"))


async def test_request_insert_failure_rolls_back_source_and_sequence(runtime):
    def reject_request_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO team_speech_request_"):
            raise SQLAlchemyError("request storage failed after draft flush")

    engine = runtime.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", reject_request_insert)
    try:
        with pytest.raises(SQLAlchemyError):
            await _post(runtime.meeting.for_member("a"))
    finally:
        event.remove(engine, "before_cursor_execute", reject_request_insert)
    assert await _rows(runtime) == []
    assert await runtime.meeting.list_pending() == ()
    assert (await runtime.db.meeting.get_meeting(SCOPE)).next_sequence == 0
    runtime.manager.messager.publish.assert_not_awaited()


async def test_normal_send_failure_keeps_content_and_lease(runtime, monkeypatch):
    await _post(runtime.meeting.for_member("a"))
    original = runtime.db.message.create_message
    monkeypatch.setattr(runtime.db.message, "create_message", AsyncMock(return_value=False))
    lease = await runtime.meeting.grant_next()
    assert lease.delivery_error and lease.content == "Complete explanation" and lease.source_message_id is None
    assert await _rows(runtime) == []
    assert (await runtime.meeting.get_state()).lease_token == lease.lease_token
    monkeypatch.setattr(runtime.db.message, "create_message", original)
    sent = await runtime.meeting.retry_publication(lease.lease_token)
    assert sent.source_message_id and sent.delivery_error is None and sent.lease_token == lease.lease_token


async def test_recording_failure_after_send_keeps_reference_recoverable(runtime, monkeypatch):
    await _post(runtime.meeting.for_member("a"))
    original = runtime.db.meeting.record_publication
    monkeypatch.setattr(
        runtime.db.meeting, "record_publication", AsyncMock(side_effect=SQLAlchemyError("record failed"))
    )
    lease = await runtime.meeting.grant_next()
    assert lease.delivery_error and lease.source_message_id is not None
    assert len(await _rows(runtime)) == 1
    queued = await runtime.db.meeting.get_request(SCOPE, lease.request_id)
    assert queued.content == "Complete explanation"
    monkeypatch.setattr(runtime.db.meeting, "record_publication", original)
    sent = await runtime.meeting.retry_publication(lease.lease_token)
    assert sent.source_message_id and sent.delivery_error is None
    assert len(await _rows(runtime)) == 1
    assert (await runtime.db.meeting.get_request(SCOPE, lease.request_id)).content is None


@pytest.mark.parametrize("departed", ["a", "b"])
@pytest.mark.parametrize("failure_phase", ["history", "recording"])
async def test_publication_recovery_survives_member_departure(runtime, monkeypatch, departed, failure_phase):
    await _post(runtime.meeting.for_member("a"))
    if failure_phase == "history":
        owner, method = runtime.conversation, "sync"
        failure = Mock(side_effect=OSError("history unavailable"))
    else:
        owner, method = runtime.db.meeting, "record_publication"
        failure = AsyncMock(side_effect=SQLAlchemyError("record failed"))
    original = getattr(owner, method)
    monkeypatch.setattr(owner, method, failure)
    lease = await runtime.meeting.grant_next()
    assert lease.delivery_error and lease.lease_token
    rows = await _rows(runtime)
    assert len(rows) == 1
    assert lease.source_message_id == (rows[0].message_id if failure_phase == "recording" else None)
    monkeypatch.setattr(owner, method, original)
    assert await runtime.db.member.update_member_status(departed, "team", "shutdown_requested")
    # Recover the committed row, never insert another message after departure.
    monkeypatch.setattr(
        runtime.db.message, "create_message", AsyncMock(side_effect=AssertionError("must reuse source"))
    )
    recovered = await runtime.meeting.retry_publication(lease.lease_token)
    assert recovered.delivery_error is None and recovered.message_error is None
    assert recovered.lease_token == lease.lease_token
    assert recovered.source_message_id == rows[0].message_id
    assert recovered.content == "Complete explanation"
    queued = await runtime.db.meeting.get_request(SCOPE, lease.request_id)
    assert queued.source_message_id == rows[0].message_id and queued.content is None
    assert len(await _rows(runtime)) == 1
    records = [json.loads(line) for line in runtime.conversation.history_path.read_text(encoding="utf-8").splitlines()]
    assert [record["message_id"] for record in records] == [rows[0].message_id]
    assert await runtime.meeting.retry_publication("stale") is None
    assert await runtime.meeting.complete_playback(recovered.lease_token)


@pytest.mark.parametrize("departed", ["a", "b"])
async def test_unsent_publication_still_rejects_departed_members(runtime, departed):
    await _post(runtime.meeting.for_member("a"))
    assert await runtime.db.member.update_member_status(departed, "team", "shutdown_requested")
    lease = await runtime.meeting.grant_next()
    assert lease.delivery_error and lease.source_message_id is None
    assert await _rows(runtime) == []
    recovered = await runtime.meeting.retry_publication(lease.lease_token)
    assert recovered.delivery_error and recovered.source_message_id is None
    assert await _rows(runtime) == []


@pytest.mark.parametrize("termination", ["cancel", "close"])
async def test_terminated_queued_content_is_never_sent(runtime, termination):
    posted = await _post(runtime.meeting.for_member("a"))
    if termination == "cancel":
        assert await runtime.meeting.cancel_request(posted.speech_request.request_id)
    else:
        assert await runtime.meeting.close()
    assert await runtime.meeting.grant_next() is None
    assert await runtime.meeting.for_member("b").get_history() == ()
    assert await _rows(runtime) == []
    runtime.manager.messager.publish.assert_not_awaited()


async def test_interruption_preserves_only_sent_text_and_blocks_successor(runtime):
    await _post(runtime.meeting.for_member("a"))
    second = await _post(runtime.meeting.for_member("b"))
    lease = await runtime.meeting.grant_next()
    interruption = await runtime.meeting.interrupt_speech(expected_token=lease.lease_token)
    assert await runtime.meeting.grant_next() is None
    assert not await runtime.meeting.complete_playback(lease.lease_token)
    assert [m.message_id for m in await runtime.meeting.for_member("a").get_history()] == [lease.source_message_id]
    assert await runtime.meeting.resume_after_interruption(interruption.interruption_id)
    successor = await runtime.meeting.grant_next()
    assert successor.request_id == second.speech_request.request_id
    count = runtime.manager.messager.publish.await_count
    assert await runtime.meeting.complete_playback(successor.lease_token)
    assert runtime.manager.messager.publish.await_count == count


async def test_priority_selects_which_text_is_sent_first(runtime):
    ordinary = await _post(runtime.meeting.for_member("a"))
    addressed = await _post(runtime.meeting.for_member("b"))
    leader = await _post(runtime.meeting.for_member("captain"))
    await runtime.meeting.set_addressed_members(["b"])
    expected = [r.speech_request.request_id for r in (leader, addressed, ordinary)]
    sent_ids = []
    for request_id in expected:
        lease = await runtime.meeting.grant_next()
        assert lease.request_id == request_id
        sent_ids.append(lease.source_message_id)
        assert await runtime.meeting.grant_next() is None
        assert await runtime.meeting.complete_playback(lease.lease_token)
    assert [m.message_id for m in await runtime.meeting.for_member("a").get_history()] == sent_ids


async def test_message_timestamp_is_assigned_by_normal_sending_at_grant(runtime, monkeypatch):
    monkeypatch.setattr("openjiuwen.agent_teams.tools.database.meeting_dao.get_current_time", lambda: 10)
    clock = iter([100, 101])
    monkeypatch.setattr("openjiuwen.agent_teams.tools.database.message_dao.get_current_time", lambda: next(clock))
    posted = await _post(runtime.meeting.for_member("a"))
    assert posted.message.timestamp == 10 and await _rows(runtime) == []
    human = await runtime.meeting.post_human_message("Correction", mentions=["b"])
    token = set_session_id("session")
    try:
        await runtime.db.message.mark_message_read(human.message.message_id, "b")
    finally:
        reset_session_id(token)
    lease = await runtime.meeting.grant_next()
    assert lease.message.timestamp == 101 > human.message.timestamp
    token = set_session_id("session")
    try:
        unread = await runtime.db.message.get_broadcast_messages("team", "b", unread_only=True)
        assert [m.message_id for m in unread] == [lease.source_message_id]
    finally:
        reset_session_id(token)


async def test_missing_send_dependencies_retains_claim_without_creating_message(runtime):
    await _post(runtime.meeting.for_member("a"))
    unconfigured = MeetingCoordinator(runtime.db, SCOPE)
    lease = await unconfigured.grant_next()
    assert lease.delivery_error and lease.content == "Complete explanation"
    assert (await runtime.meeting.get_state()).lease_token == lease.lease_token
    assert await _rows(runtime) == []
    sent = await runtime.meeting.retry_publication(lease.lease_token)
    assert sent.message.content == "Complete explanation" and sent.delivery_error is None


async def test_history_failure_after_storage_preserves_same_lease_and_single_message(runtime, monkeypatch):
    await _post(runtime.meeting.for_member("a"))
    sync = runtime.conversation.sync
    monkeypatch.setattr(
        runtime.conversation, "sync", lambda messages: (_ for _ in ()).throw(OSError("history unavailable"))
    )
    lease = await runtime.meeting.grant_next()
    assert lease.delivery_error and lease.content == "Complete explanation"
    assert len(await _rows(runtime)) == 1
    assert await runtime.meeting.grant_next() is None
    monkeypatch.setattr(runtime.conversation, "sync", sync)
    recovered = await runtime.meeting.retry_publication(lease.lease_token)
    assert recovered.lease_token == lease.lease_token and recovered.delivery_error is None
    assert len(await _rows(runtime)) == 1
    assert await runtime.meeting.retry_publication("stale") is None


async def test_granted_source_failure_retains_token_and_can_be_retried(runtime, monkeypatch):
    await _post(runtime.meeting.for_member("a"))
    original = runtime.meeting.get_source_message
    monkeypatch.setattr(runtime.meeting, "get_source_message", AsyncMock(side_effect=OSError("source unavailable")))
    lease = await runtime.meeting.grant_next()
    assert lease.lease_token and lease.message is None and lease.message_error
    assert lease.source_message_id and lease.delivery_error is None
    assert (await runtime.meeting.get_state()).lease_token == lease.lease_token
    monkeypatch.setattr(runtime.meeting, "get_source_message", original)
    recovered = await runtime.meeting.retry_publication(lease.lease_token)
    assert recovered.message is not None and recovered.message_error is None and recovered.delivery_error is None


async def test_independent_engines_claim_and_send_only_one_queued_message(runtime):
    other_db = TeamDatabase(runtime.db.config)
    await other_db.initialize()
    other_manager = TeamMessageManager("team", "captain", other_db, AsyncMock())
    other = MeetingCoordinator(other_db, SCOPE, message_manager=other_manager, conversation=runtime.conversation)
    try:
        posts = await asyncio.gather(_post(runtime.meeting.for_member("a")), _post(other.for_member("a")))
        assert posts[0].speech_request.request_id != posts[1].speech_request.request_id
        assert await _rows(runtime) == []
        grants = await asyncio.gather(runtime.meeting.grant_next(), other.grant_next())
        assert sum(grant is not None for grant in grants) == 1
        assert runtime.manager.messager.publish.await_count + other_manager.messager.publish.await_count == 1
        assert len(await _rows(runtime)) == 1
        assert len(await runtime.meeting.list_pending()) == 1
    finally:
        await other_db.close()


async def test_existing_mailbox_delivers_full_text_after_grant_before_completion_once(runtime):
    backend = TeamBackend("team", "b", False, runtime.db, runtime.manager.messager)
    backend.group_chat_spec = SimpleNamespace(language="en", workspace=None)
    backend.bind_group_session("session")
    host = SimpleNamespace(
        deliver_input=AsyncMock(),
        has_pending_interrupt=lambda: False,
        auto_start_member=AsyncMock(),
    )
    blueprint = SimpleNamespace(member_name="b", role=TeamRole.TEAMMATE, team_spec=backend.group_chat_spec)
    infra = SimpleNamespace(team_backend=backend, message_manager=backend.message_manager)
    handler = GroupMessageHandler(host, blueprint, infra, AsyncMock())
    token = set_session_id("session")
    try:
        posted = await _post(runtime.meeting.for_member("a"))
        await handler.on_poll_mailbox(None)
        host.deliver_input.assert_not_awaited()
        assert await runtime.db.message.get_broadcast_read_at("team", "b") == 0
        lease = await runtime.meeting.grant_next()
        await handler.on_poll_mailbox(None)
        host.deliver_input.assert_awaited_once()
        delivered = host.deliver_input.call_args.args[0]
        assert posted.message.content in delivered and lease.source_message_id in delivered
        assert (await runtime.meeting.get_speaker()).lease_token == lease.lease_token
        assert await runtime.meeting.complete_playback(lease.lease_token)
        await handler.on_poll_mailbox(None)
        host.deliver_input.assert_awaited_once()
    finally:
        reset_session_id(token)


async def test_notification_failure_keeps_normal_team_polling_behavior(runtime):
    await _post(runtime.meeting.for_member("a"))
    runtime.manager.messager.publish.side_effect = ConnectionError("bus unavailable")
    lease = await runtime.meeting.grant_next()
    assert lease.source_message_id and lease.delivery_error is None
    token = set_session_id("session")
    try:
        unread = await runtime.db.message.get_broadcast_messages("team", "b", unread_only=True)
        assert [row.message_id for row in unread] == [lease.source_message_id]
    finally:
        reset_session_id(token)


async def test_shared_group_history_and_source_lookup_remain_session_scoped(runtime):
    other_conversation = await asyncio.to_thread(GroupConversationLog, "team", "other-session")
    other = await post_message(
        other_conversation, runtime.manager, "a", "Other session text", client_message_id="other-session-message"
    )
    member = runtime.meeting.for_member("a")
    assert await member.get_history() == ()
    assert await member.get_source_message(other.message.message_id) is None
    human = await runtime.meeting.post_human_message("Current session human input", mentions=["a"])
    assert (await member.get_history())[0].message_id == human.message.message_id
