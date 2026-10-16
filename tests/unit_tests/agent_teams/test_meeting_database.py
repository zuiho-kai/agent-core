# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Meeting queue invariants against real SQLite, including independent processes."""

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect

from openjiuwen.agent_teams.context import get_session_id, reset_session_id, set_session_id
from openjiuwen.agent_teams.schema.meeting import MeetingScope, SpeechRequestStatus
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.models import (
    TeamMeetingBase,
    TeamSpeechRequestBase,
    _get_meeting_model,
    _get_speech_request_model,
    _sanitize_session_id_for_table,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.level1]

SCOPE = MeetingScope("session", "team", "meeting")


async def _seed_team(db: TeamDatabase, team_name: str = "team") -> None:
    await db.team.create_team(team_name, "Meeting Team", "captain")
    for name, role in (("captain", "leader"), ("a", "teammate"), ("b", "teammate"), ("human", "human_agent")):
        await db.member.create_member(
            member_name=name,
            team_name=team_name,
            display_name=name,
            agent_card="{}",
            status="ready",
            role=role,
        )


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    database = TeamDatabase(DatabaseConfig(connection_string=str(tmp_path / "meeting.db")))
    await database.initialize()
    try:
        await _seed_team(database)
        await database.meeting.create_meeting(SCOPE)
        yield database
    finally:
        await database.close()


async def test_priority_fifo_and_no_preemption(db: TeamDatabase) -> None:
    """Latest routing ranks pending requests without preempting the current lease."""
    dao = db.meeting
    await dao.enqueue(SCOPE, "current", "b")
    current = await dao.grant_next(SCOPE)
    await dao.enqueue(SCOPE, "b1", "b")
    await dao.enqueue(SCOPE, "a1", "a")
    await dao.enqueue(SCOPE, "b2", "b")
    await dao.enqueue(SCOPE, "leader", "captain")
    await dao.enqueue(SCOPE, "a2", "a")
    await dao.set_addressed_members(SCOPE, ["a"])
    assert await dao.grant_next(SCOPE) is None
    assert (await dao.get_meeting(SCOPE)).lease_token == current.lease_token
    assert [r.request_id for r in await dao.list_pending(SCOPE)] == ["leader", "a1", "a2", "b1", "b2"]
    assert await dao.complete_playback(SCOPE, current.lease_token)
    leader = await dao.grant_next(SCOPE)
    assert leader.request_id == "leader"
    await dao.set_addressed_members(SCOPE, ["b"])
    assert await dao.complete_playback(SCOPE, leader.lease_token)
    for expected in ("b1", "b2", "a1", "a2"):
        granted = await dao.grant_next(SCOPE)
        assert granted.request_id == expected
        assert await dao.complete_playback(SCOPE, granted.lease_token)
    assert await dao.grant_next(SCOPE) is None


async def test_deduplication_and_queued_only_cancellation(db: TeamDatabase) -> None:
    dao = db.meeting
    original = await dao.enqueue(SCOPE, "one", "a")
    duplicate = await dao.enqueue(SCOPE, "one", "a")
    assert original.sequence == duplicate.sequence
    with pytest.raises(ValueError, match="another member"):
        await dao.enqueue(SCOPE, "one", "b")
    assert await dao.cancel_request(SCOPE, "one")
    assert not await dao.cancel_request(SCOPE, "one")
    assert not await dao.cancel_request(SCOPE, "missing")
    assert (await dao.enqueue(SCOPE, "one", "a")).status == SpeechRequestStatus.CANCELLED
    await dao.enqueue(SCOPE, "two", "a")
    granted = await dao.grant_next(SCOPE)
    assert granted.request_id == "two"
    assert not await dao.cancel_request(SCOPE, "two")
    assert await dao.complete_playback(SCOPE, granted.lease_token)
    assert (await dao.enqueue(SCOPE, "two", "a")).status == SpeechRequestStatus.COMPLETED
    assert await dao.grant_next(SCOPE) is None


async def test_stale_callback_cannot_release_successor(db: TeamDatabase) -> None:
    dao = db.meeting
    await dao.enqueue(SCOPE, "first", "a")
    await dao.enqueue(SCOPE, "next", "a")
    first = await dao.grant_next(SCOPE)
    assert not await dao.complete_playback(SCOPE, "wrong")
    assert not await dao.complete_playback(SCOPE, "")
    assert await dao.complete_playback(SCOPE, first.lease_token)
    second = await dao.grant_next(SCOPE)
    assert first.lease_token != second.lease_token
    assert not await dao.complete_playback(SCOPE, first.lease_token)
    assert (await dao.get_meeting(SCOPE)).owner_request_id == "next"
    assert await dao.interrupt_speech(SCOPE, expected_token=first.lease_token) is None
    assert (await dao.get_meeting(SCOPE)).lease_token == second.lease_token


async def test_interruption_barrier_and_stale_acknowledgements(db: TeamDatabase) -> None:
    dao = db.meeting
    await dao.enqueue(SCOPE, "one", "a")
    await dao.enqueue(SCOPE, "two", "b")
    granted = await dao.grant_next(SCOPE)
    interrupted = await dao.interrupt_speech(SCOPE, expected_token=granted.lease_token)
    assert interrupted.request_id == "one"
    assert interrupted.lease_token == granted.lease_token
    assert (await dao.get_request(SCOPE, "one")).status == SpeechRequestStatus.INTERRUPTED
    assert not await dao.complete_playback(SCOPE, granted.lease_token)
    assert await dao.grant_next(SCOPE) is None
    assert await dao.interrupt_speech(SCOPE) is None
    assert not await dao.resume_after_interruption(SCOPE, "wrong")
    assert await dao.resume_after_interruption(SCOPE, interrupted.interruption_id)
    next_grant = await dao.grant_next(SCOPE)
    next_interrupt = await dao.interrupt_speech(SCOPE, expected_token=next_grant.lease_token)
    assert not await dao.resume_after_interruption(SCOPE, interrupted.interruption_id)
    assert await dao.grant_next(SCOPE) is None
    assert await dao.resume_after_interruption(SCOPE, next_interrupt.interruption_id)


async def test_human_admission_barrier_when_idle(db: TeamDatabase) -> None:
    interruption = await db.meeting.interrupt_speech(SCOPE)
    assert interruption.lease_token is None
    await db.meeting.enqueue(SCOPE, "pending", "a")
    assert await db.meeting.grant_next(SCOPE) is None
    assert await db.meeting.resume_after_interruption(SCOPE, interruption.interruption_id)
    assert (await db.meeting.grant_next(SCOPE)).request_id == "pending"


async def test_close_is_permanent_and_late_callbacks_are_harmless(db: TeamDatabase) -> None:
    dao = db.meeting
    await dao.enqueue(SCOPE, "one", "a")
    await dao.enqueue(SCOPE, "two", "b")
    granted = await dao.grant_next(SCOPE)
    assert await dao.close_meeting(SCOPE)
    assert not await dao.close_meeting(SCOPE)
    assert (await dao.get_request(SCOPE, "one")).status == SpeechRequestStatus.INTERRUPTED
    assert (await dao.get_request(SCOPE, "two")).status == SpeechRequestStatus.CANCELLED
    assert (await dao.create_meeting(SCOPE)).closed
    assert not await dao.complete_playback(SCOPE, granted.lease_token)
    assert not await dao.resume_after_interruption(SCOPE, "late")
    assert not await dao.set_addressed_members(SCOPE, ["a"])
    assert await dao.interrupt_speech(SCOPE) is None
    assert await dao.grant_next(SCOPE) is None
    with pytest.raises(ValueError, match="closed"):
        await dao.enqueue(SCOPE, "late", "a")


@pytest.mark.parametrize("member", ["human", "unknown", "user"])
async def test_only_registered_agents_can_queue(db: TeamDatabase, member: str) -> None:
    with pytest.raises(ValueError, match="registered agent"):
        await db.meeting.enqueue(SCOPE, "bad", member)
    assert await db.meeting.list_pending(SCOPE) == []


async def test_invalid_scope_and_missing_meeting(db: TeamDatabase) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MeetingScope("", "team", "meeting")
    with pytest.raises(ValueError, match="non-empty"):
        await db.meeting.enqueue(SCOPE, " ", "a")
    missing = MeetingScope("session", "team", "missing")
    with pytest.raises(ValueError, match="does not exist"):
        await db.meeting.grant_next(missing)
    with pytest.raises(ValueError, match="Team does not exist"):
        await db.meeting.create_meeting(MeetingScope("session", "unknown", "meeting"))
    assert await db.meeting.get_meeting(missing) is None


async def test_scope_isolation(db: TeamDatabase) -> None:
    await _seed_team(db, "other")
    scopes = [
        SCOPE,
        MeetingScope("other-session", "team", "meeting"),
        MeetingScope("session", "other", "meeting"),
        MeetingScope("session", "team", "new-meeting"),
    ]
    tokens = []
    for scope in scopes:
        await db.meeting.create_meeting(scope)
        await db.meeting.enqueue(scope, "same-id", "a")
        tokens.append((await db.meeting.grant_next(scope)).lease_token)
    assert len(set(tokens)) == len(scopes)
    for scope in scopes[1:]:
        assert not await db.meeting.complete_playback(scope, tokens[0])
        assert (await db.meeting.get_meeting(scope)).owner_request_id == "same-id"


async def test_model_factories_follow_session_convention() -> None:
    """Factories share cached classes with contextual resolution and partition sessions."""
    token = set_session_id("meeting-factory-session")
    try:
        meeting_model = _get_meeting_model()
        request_model = _get_speech_request_model()
        assert issubclass(meeting_model, TeamMeetingBase)
        assert issubclass(request_model, TeamSpeechRequestBase)
        assert meeting_model is _get_meeting_model("meeting-factory-session")
        assert request_model is _get_speech_request_model("meeting-factory-session")
        assert meeting_model is not _get_meeting_model("another-factory-session")
        assert request_model is not _get_speech_request_model("another-factory-session")
        suffix = _sanitize_session_id_for_table("meeting-factory-session")
        assert meeting_model.__tablename__ == f"team_meeting_{suffix}"
        assert request_model.__tablename__ == f"team_speech_request_{suffix}"
        assert "session_id" not in meeting_model.__table__.columns
        assert "session_id" not in request_model.__table__.columns
        assert {c.name for c in meeting_model.__table__.primary_key} == {"team_name", "meeting_id"}
        assert {c.name for c in request_model.__table__.primary_key} == {"team_name", "meeting_id", "request_id"}
        assert get_session_id() == "meeting-factory-session"
    finally:
        reset_session_id(token)


async def test_session_bind_creates_and_drops_meeting_tables(db: TeamDatabase) -> None:
    """Meeting tables join normal bind/drop, without touching another session."""
    token = set_session_id("meeting-bind-session")
    try:
        await db.create_cur_session_tables()
        meeting_model = _get_meeting_model()
        request_model = _get_speech_request_model()
        async with db.engine.begin() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert meeting_model.__tablename__ in names
        assert request_model.__tablename__ in names
        await db.drop_cur_session_tables()
        async with db.engine.begin() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert meeting_model.__tablename__ not in names
        assert request_model.__tablename__ not in names
        assert await db.meeting.get_meeting(SCOPE) is not None
        await db.create_cur_session_tables()
        assert _get_meeting_model() is meeting_model
        scope = MeetingScope("meeting-bind-session", "team", "meeting")
        await db.meeting.create_meeting(scope)
        await db.meeting.enqueue(scope, "bound", "a")
        assert (await db.meeting.grant_next(scope)).request_id == "bound"
        assert get_session_id() == "meeting-bind-session"
    finally:
        reset_session_id(token)


async def test_independent_engines_admit_once_and_grant_one_owner(db: TeamDatabase) -> None:
    other = TeamDatabase(db.config)
    await other.initialize()
    try:
        admissions = await asyncio.gather(
            db.meeting.enqueue(SCOPE, "duplicate", "a"),
            other.meeting.enqueue(SCOPE, "duplicate", "a"),
        )
        assert admissions[0].sequence == admissions[1].sequence
        await asyncio.gather(
            db.meeting.enqueue(SCOPE, "two", "b"),
            other.meeting.enqueue(SCOPE, "three", "a"),
        )
        pending = await db.meeting.list_pending(SCOPE)
        assert len(pending) == 3
        assert [r.sequence for r in pending] == [1, 2, 3]
        grants = await asyncio.gather(db.meeting.grant_next(SCOPE), other.meeting.grant_next(SCOPE))
        assert sum(grant is not None for grant in grants) == 1
        assert (await other.meeting.get_meeting(SCOPE)).owner_request_id == "duplicate"
    finally:
        await other.close()


async def test_reopen_preserves_ownership_without_replay(db: TeamDatabase) -> None:
    await db.meeting.enqueue(SCOPE, "one", "a")
    granted = await db.meeting.grant_next(SCOPE)
    await db.close()
    await db.initialize()
    assert (await db.meeting.create_meeting(SCOPE)).lease_token == granted.lease_token
    assert await db.meeting.grant_next(SCOPE) is None
    assert await db.meeting.complete_playback(SCOPE, granted.lease_token)


async def test_independent_engines_create_same_session_meeting(db: TeamDatabase) -> None:
    """Concurrent setup of a previously unseen session preserves one meeting instance."""
    other = TeamDatabase(db.config)
    await other.initialize()
    scope = MeetingScope("concurrent-meeting-setup", "team", "meeting")
    try:
        created = await asyncio.gather(db.meeting.create_meeting(scope), other.meeting.create_meeting(scope))
        assert all(meeting.meeting_id == "meeting" for meeting in created)
        await db.meeting.enqueue(scope, "one", "a")
        granted = await other.meeting.grant_next(scope)
        assert granted.request_id == "one"
        assert (await db.meeting.get_meeting(scope)).lease_token == granted.lease_token
    finally:
        await other.close()


async def test_session_release_and_team_deletion_cleanup(db: TeamDatabase) -> None:
    other_session = MeetingScope("other", "team", "meeting")
    await db.meeting.create_meeting(other_session)
    await db.meeting.enqueue(SCOPE, "one", "a")
    await db.meeting.enqueue(other_session, "two", "b")
    await db.drop_session_tables_by_id(SCOPE.session_id)
    assert await db.meeting.get_meeting(SCOPE) is None
    assert await db.meeting.get_request(SCOPE, "one") is None
    assert not await db.meeting.complete_playback(SCOPE, "late-token")
    assert not await db.meeting.resume_after_interruption(SCOPE, "late-interruption")
    assert await db.meeting.interrupt_speech(SCOPE, expected_token="late-token") is None
    assert await db.meeting.get_meeting(other_session) is not None
    await db.team.delete_team("team")
    assert await db.meeting.get_meeting(other_session) is None
    assert await db.meeting.get_request(other_session, "two") is None


async def test_global_cleanup_removes_coordination_state(db: TeamDatabase) -> None:
    await db.meeting.enqueue(SCOPE, "one", "a")
    await db.meeting.grant_next(SCOPE)
    dropped, cleared = await db.cleanup_all_runtime_state()
    assert _get_meeting_model(SCOPE.session_id).__tablename__ in dropped
    assert _get_speech_request_model(SCOPE.session_id).__tablename__ in dropped
    assert cleared == ["team_info", "team_member"]
    assert await db.meeting.get_meeting(SCOPE) is None
    assert await db.meeting.get_request(SCOPE, "one") is None


async def test_additive_schema_setup_preserves_existing_team(db: TeamDatabase) -> None:
    """Initializing a pre-meeting database creates the new tables without losing data."""
    async with db.engine.begin() as conn:
        await conn.run_sync(_get_speech_request_model(SCOPE.session_id).__table__.drop)
        await conn.run_sync(_get_meeting_model(SCOPE.session_id).__table__.drop)
    await db.close()
    await db.initialize()
    assert (await db.team.get_team("team")).leader_member_name == "captain"
    await db.meeting.create_meeting(SCOPE)
    assert (await db.meeting.enqueue(SCOPE, "new", "a")).sequence == 1
    async with db.engine.begin() as conn:
        names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    assert _get_meeting_model(SCOPE.session_id).__tablename__ in names
    assert _get_speech_request_model(SCOPE.session_id).__tablename__ in names
    assert "team_meeting" not in names
    assert "team_speech_request" not in names


def _process_grant(db_path: str, barrier) -> str | None:
    """Spawn-safe worker: each process has independent engines and asyncio locks."""

    async def run() -> str | None:
        database = TeamDatabase(DatabaseConfig(connection_string=db_path))
        await database.initialize()
        try:
            await asyncio.to_thread(barrier.wait, 20)
            granted = await database.meeting.grant_next(SCOPE)
            return granted.lease_token if granted is not None else None
        finally:
            await database.close()

    return asyncio.run(run())


async def test_independent_processes_cannot_both_own_meeting(db: TeamDatabase) -> None:
    await db.meeting.enqueue(SCOPE, "one", "a")
    await db.meeting.enqueue(SCOPE, "two", "b")
    context = multiprocessing.get_context("spawn")

    def contend() -> list[str | None]:
        with context.Manager() as manager:
            barrier = manager.Barrier(2)
            with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
                futures = [pool.submit(_process_grant, db.config.connection_string, barrier) for _ in range(2)]
                return [future.result(timeout=40) for future in futures]

    tokens = await asyncio.to_thread(contend)
    assert sum(token is not None for token in tokens) == 1
    meeting = await db.meeting.get_meeting(SCOPE)
    assert meeting.owner_request_id == "one"
    assert meeting.lease_token in tokens
