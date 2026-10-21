# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application boundary tests against the real meeting and normal-message stores."""

import asyncio
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from openjiuwen.agent_teams import MeetingCoordinator, MeetingScope, SpeechRequestStatus
from openjiuwen.agent_teams.context import get_session_id, reset_session_id, set_session_id
from openjiuwen.agent_teams.meeting import MeetingState, SpeechRequest, SpeechSource
from openjiuwen.agent_teams.paths import reset_task_openjiuwen_home, set_task_openjiuwen_home
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.models import _get_message_model

pytestmark = [pytest.mark.asyncio, pytest.mark.level1]

SCOPE = MeetingScope("session", "team", "meeting")


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    home_token = set_task_openjiuwen_home(tmp_path)
    database = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "meeting.db")))
    await database.initialize()
    try:
        for team in ("team", "other-team"):
            await database.team.create_team(team, team, "captain")
            for name, role in (("captain", "leader"), ("a", "teammate"), ("b", "teammate"), ("human", "human_agent")):
                await database.member.create_member(
                    member_name=name, team_name=team, display_name=name, agent_card="{}", status="ready", role=role
                )
        yield database
    finally:
        await database.close()
        reset_task_openjiuwen_home(home_token)


async def _source(
    db: TeamDatabase,
    message_id: str = "source",
    *,
    member: str = "a",
    scope: MeetingScope = SCOPE,
    content: str = "Complete text",
    broadcast: bool = False,
    inline: bool = True,
) -> str:
    await db.meeting.create_meeting(scope)
    token = set_session_id(scope.session_id)
    try:
        await db.create_cur_session_tables()
        assert await db.message.create_message(
            message_id,
            scope.team_name,
            member,
            content,
            to_member_name=None if broadcast else "b",
            broadcast=broadcast,
            inline_content=inline,
        )
    finally:
        reset_session_id(token)
    return message_id


async def _request(coordinator: MeetingCoordinator, source: str = "source", *, member: str = "a") -> SpeechRequest:
    return await coordinator.request_speech(
        member, request_id=f"speech-{source}", source_message_id=source, speech_text=f"Summary of {source}"
    )


async def test_constructor_has_no_side_effects_and_start_is_explicit():
    database = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    coordinator = MeetingCoordinator(database, SCOPE)
    assert database.engine is None
    try:
        assert await coordinator.get_state() is None
        with pytest.raises(ValueError, match="Team does not exist"):
            await coordinator.start()
    finally:
        await database.close()


async def test_start_attach_and_close_preserve_database_and_terminal_state(db: TeamDatabase):
    coordinator = MeetingCoordinator(db, SCOPE)
    state = await coordinator.start()
    assert isinstance(state, MeetingState)
    assert state.scope == SCOPE
    assert state.leader_member_name == "captain"
    assert state == await MeetingCoordinator(db, SCOPE).start()
    with pytest.raises(FrozenInstanceError):
        state.closed = True
    assert await coordinator.close()
    assert not await coordinator.close()
    assert (await coordinator.start()).closed
    assert db.engine is not None
    assert await db.team.get_team("team") is not None


async def test_admission_is_detached_rejects_duplicates_and_never_auto_grants(db: TeamDatabase):
    await _source(db)
    coordinator = MeetingCoordinator(db, SCOPE)
    request = await _request(coordinator)
    assert isinstance(request, SpeechRequest)
    assert request.status == SpeechRequestStatus.QUEUED
    assert request.scope == SCOPE
    with pytest.raises(IntegrityError):
        await _request(coordinator)
    assert await coordinator.get_request(request.request_id) == request
    assert await coordinator.get_request("missing") is None
    assert await coordinator.get_speaker() is None
    assert (await coordinator.get_state()).owner_request_id is None
    assert asdict(request)["source_message_id"] == "source"
    with pytest.raises(FrozenInstanceError):
        request.speech_text = "changed"
    with pytest.raises(IntegrityError):
        await coordinator.request_speech(
            "a", request_id=request.request_id, source_message_id="source", speech_text="changed"
        )
    assert (await coordinator.get_request(request.request_id)).speech_text == request.speech_text


@pytest.mark.parametrize("invalid", ["missing", "wrong-team", "wrong-author", "wrong-session", "human"])
async def test_admission_rejects_invalid_source_associations(db: TeamDatabase, invalid: str):
    coordinator = MeetingCoordinator(db, SCOPE)
    await coordinator.start()
    member = "a"
    if invalid == "wrong-team":
        await _source(db, scope=MeetingScope(SCOPE.session_id, "other-team", "meeting"))
    elif invalid == "wrong-session":
        await _source(db, scope=MeetingScope("other-session", "team", "meeting"))
    elif invalid == "wrong-author":
        await _source(db, member="b")
    elif invalid == "human":
        member = "human"
        await _source(db, member=member)
    with pytest.raises(ValueError):
        await _request(coordinator, member=member)
    assert await coordinator.list_pending() == ()
    assert (await db.meeting.get_meeting(SCOPE)).next_sequence == 0


async def test_source_hydration_failure_does_not_hide_admitted_request(db: TeamDatabase, monkeypatch):
    await _source(db)
    hydrate = AsyncMock(side_effect=OSError("Source content temporarily unavailable"))
    coordinator = MeetingCoordinator(db, SCOPE)
    monkeypatch.setattr(coordinator, "get_source_message", hydrate)
    request = await _request(coordinator)
    assert request.source_message_id == "source"
    assert request.message is None and request.message_error == "source_message_unavailable"
    assert (await db.meeting.get_request(SCOPE, request.request_id)).status == SpeechRequestStatus.QUEUED


async def test_explicit_session_and_full_text_hydration_restore_context(db: TeamDatabase):
    await _source(db, content="Text in the meeting session", inline=False)
    await _source(db, scope=MeetingScope("other-session", "team", "other-meeting"), content="Other text")
    coordinator = MeetingCoordinator(db, SCOPE)
    token = set_session_id("other-session")
    try:
        await _request(coordinator)
        assert get_session_id() == "other-session"
        source = await coordinator.get_source_message("source")
        assert isinstance(source, SpeechSource)
        assert source.content == "Text in the meeting session"
        assert source.scope == SCOPE
        assert get_session_id() == "other-session"
        with pytest.raises(FrozenInstanceError):
            source.content = "replacement"
        async with db._sessions.read() as session:
            stored = await session.get(_get_message_model(SCOPE.session_id), "source")
            assert stored.content == "#file#"
    finally:
        reset_session_id(token)


@pytest.mark.parametrize("operation", ["request", "source"])
async def test_context_restored_when_source_query_fails(db: TeamDatabase, monkeypatch, operation: str):
    await _source(db)
    method = "get_message"
    monkeypatch.setattr(db.message, method, AsyncMock(side_effect=RuntimeError("lookup failed")))
    coordinator = MeetingCoordinator(db, SCOPE)
    token = set_session_id("unrelated")
    try:
        with pytest.raises(RuntimeError, match="lookup failed"):
            if operation == "request":
                await _request(coordinator)
            else:
                await coordinator.get_source_message("source")
        assert get_session_id() == "unrelated"
    finally:
        reset_session_id(token)


async def test_source_lookup_rejects_another_team_and_returns_none_for_missing(db: TeamDatabase):
    coordinator = MeetingCoordinator(db, SCOPE)
    await coordinator.start()
    assert await coordinator.get_source_message("missing") is None
    await _source(db, scope=MeetingScope(SCOPE.session_id, "other-team", "meeting"))
    with pytest.raises(ValueError, match="another team"):
        await coordinator.get_source_message("source")


@pytest.mark.parametrize("broadcast", [False, True])
async def test_a2a_consumption_and_audio_completion_are_independent(db: TeamDatabase, broadcast: bool):
    await _source(db, broadcast=broadcast)
    coordinator = MeetingCoordinator(db, SCOPE)
    await _request(coordinator)
    token = set_session_id(SCOPE.session_id)
    try:
        assert not (await db.message.get_message("source")).is_read
        assert await db.message.get_broadcast_read_at("team", "b") == 0
        assert (await coordinator.get_source_message("source")).content == "Complete text"
        lease = await coordinator.grant_next()
        assert await coordinator.complete_playback(lease.lease_token)
        assert not (await db.message.get_message("source")).is_read
        assert await db.message.get_broadcast_read_at("team", "b") == 0
        assert await db.message.mark_message_read("source", "b")
        assert (await coordinator.get_source_message("source")).content == "Complete text"
        assert (await coordinator.get_request(lease.request_id)).status == SpeechRequestStatus.COMPLETED
    finally:
        reset_session_id(token)


@pytest.mark.parametrize("consume_first", [False, True])
async def test_a2a_consumption_does_not_remove_queued_or_future_audio(db: TeamDatabase, consume_first: bool):
    await _source(db)
    coordinator = MeetingCoordinator(db, SCOPE)
    if not consume_first:
        await _request(coordinator)
    token = set_session_id(SCOPE.session_id)
    try:
        assert await db.message.mark_message_read("source", "b")
    finally:
        reset_session_id(token)
    request = await _request(coordinator) if consume_first else await coordinator.get_request("speech-source")
    assert request.status == SpeechRequestStatus.QUEUED
    assert (await coordinator.get_source_message("source")).content == "Complete text"


async def test_priority_grants_cancellation_and_stale_completion(db: TeamDatabase):
    coordinator = MeetingCoordinator(db, SCOPE)
    for source, member in (("ordinary", "b"), ("addressed", "a"), ("leader", "captain")):
        await _source(db, source, member=member)
        await _request(coordinator, source, member=member)
    assert await coordinator.set_addressed_members(["a"])
    assert [r.member_name for r in await coordinator.list_pending()] == ["captain", "a", "b"]
    assert (await coordinator.get_state()).addressed_members == ("a",)
    first = await coordinator.grant_next()
    assert first == await coordinator.get_speaker()
    assert await coordinator.grant_next() is None
    assert not await coordinator.cancel_request(first.request_id)
    assert await coordinator.cancel_request("speech-ordinary")
    assert not await coordinator.cancel_request("speech-ordinary")
    assert await coordinator.complete_playback(first.lease_token)
    assert await coordinator.get_speaker() is None
    assert (await coordinator.list_pending())[0].member_name == "a"
    second = await coordinator.grant_next()
    assert not await coordinator.complete_playback(first.lease_token)
    assert await coordinator.get_speaker() == second


async def test_leader_authorization_barrier_and_stale_target(db: TeamDatabase):
    await _source(db)
    await _source(db, "next")
    coordinator = MeetingCoordinator(db, SCOPE)
    await _request(coordinator)
    await _request(coordinator, "next")
    first = await coordinator.grant_next()
    with pytest.raises(ValueError, match="Only the meeting Leader"):
        await coordinator.interrupt_from_leader("a", expected_token=first.lease_token)
    with pytest.raises(ValueError, match="observed lease token"):
        await coordinator.interrupt_from_leader("captain", expected_token=" ")
    interruption = await coordinator.interrupt_from_leader("captain", expected_token=first.lease_token)
    assert interruption.lease_token == first.lease_token
    assert await coordinator.get_speaker() is None
    assert await coordinator.grant_next() is None
    assert not await coordinator.resume_after_interruption("stale")
    assert await coordinator.resume_after_interruption(interruption.interruption_id)
    assert await coordinator.get_speaker() is None
    second = await coordinator.grant_next()
    assert await coordinator.interrupt_from_leader("captain", expected_token=first.lease_token) is None
    assert not await coordinator.resume_after_interruption(interruption.interruption_id)
    assert await coordinator.get_speaker() == second


async def test_host_can_hold_idle_barrier_then_close_retains_text(db: TeamDatabase):
    coordinator = MeetingCoordinator(db, SCOPE)
    await coordinator.start()
    interruption = await coordinator.interrupt_speech()
    assert interruption.lease_token is None
    await _source(db)
    request = await _request(coordinator)
    assert await coordinator.grant_next() is None
    assert await coordinator.close()
    assert (await coordinator.get_request(request.request_id)).status == SpeechRequestStatus.CANCELLED
    assert not await coordinator.resume_after_interruption(interruption.interruption_id)
    assert await coordinator.grant_next() is None
    assert (await coordinator.get_source_message("source")).content == "Complete text"
    with pytest.raises(ValueError, match="closed"):
        await _request(coordinator)


async def test_independent_wrappers_share_existing_queue_and_one_grant(db: TeamDatabase):
    await _source(db)
    first, second = MeetingCoordinator(db, SCOPE), MeetingCoordinator(db, SCOPE)
    requests = await asyncio.gather(_request(first), _request(second), return_exceptions=True)
    assert sum(isinstance(request, IntegrityError) for request in requests) == 1
    grants = await asyncio.gather(first.grant_next(), second.grant_next())
    assert sum(grant is not None for grant in grants) == 1
    assert await first.get_speaker() == await second.get_speaker()


async def test_late_callbacks_after_session_cleanup_are_harmless(db: TeamDatabase):
    await _source(db)
    coordinator = MeetingCoordinator(db, SCOPE)
    await _request(coordinator)
    lease = await coordinator.grant_next()
    await db.drop_session_tables_by_id(SCOPE.session_id)
    assert await coordinator.get_state() is None
    assert await coordinator.get_speaker() is None
    assert await coordinator.get_request(lease.request_id) is None
    assert not await coordinator.complete_playback(lease.lease_token)
    assert await coordinator.interrupt_speech(expected_token=lease.lease_token) is None
    assert not await coordinator.resume_after_interruption("old-interruption")


@pytest.mark.parametrize(
    "other_scope",
    [
        MeetingScope("other-session", "team", "meeting"),
        MeetingScope("session", "other-team", "meeting"),
        MeetingScope("session", "team", "other-meeting"),
    ],
)
async def test_callbacks_and_requests_remain_isolated_by_scope(db: TeamDatabase, other_scope: MeetingScope):
    await _source(db)
    await _source(db, "other-source", scope=other_scope)
    first, other = MeetingCoordinator(db, SCOPE), MeetingCoordinator(db, other_scope)
    await _request(first)
    await other.request_speech("a", request_id="speech-source", source_message_id="other-source", speech_text="Other")
    lease = await first.grant_next()
    other_lease = await other.grant_next()
    assert not await other.complete_playback(lease.lease_token)
    assert await other.get_speaker() == other_lease
    assert (await first.get_source_message("source")).content == "Complete text"
    assert await first.close()
    assert await other.get_speaker() == other_lease
    assert await other.complete_playback(other_lease.lease_token)
