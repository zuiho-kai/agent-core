# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Database authority for audio queue admission and priority ordering.

Mutations lock the meeting row before reading it; creation locks the owning
Team row. These database locks also serialize independent member processes.
Callers are trusted runtime code; agent tools must not expose this DAO directly.
"""

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy import case, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openjiuwen.agent_teams.schema.meeting import MeetingScope, MeetingStatus, SpeechRequestStatus
from openjiuwen.agent_teams.tools.database.engine import DbSessions, create_meeting_tables, get_current_time
from openjiuwen.agent_teams.tools.models import (
    Team,
    TeamMeetingBase,
    TeamMember,
    TeamSpeechRequestBase,
    _get_meeting_model,
    _get_speech_request_model,
)
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import raise_error

_MeetingRow = TypeVar("_MeetingRow", TeamMeetingBase, TeamSpeechRequestBase)


class _MeetingNotFoundError(ValueError):
    """An explicit scope no longer has persisted meeting state."""


def _scope_filter(model: type[_MeetingRow], scope: MeetingScope) -> tuple:
    return model.team_name == scope.team_name, model.meeting_id == scope.meeting_id


def _scope_values(scope: MeetingScope) -> dict[str, str]:
    return {"team_name": scope.team_name, "meeting_id": scope.meeting_id}


class MeetingDao:
    """Short atomic queue operations without model execution or audio playback."""

    def __init__(self, sessions: DbSessions) -> None:
        self._sessions = sessions

    @staticmethod
    async def _table_exists(session: AsyncSession, model: type[_MeetingRow]) -> bool:
        conn = await session.connection()
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(model.__tablename__))

    @asynccontextmanager
    async def _locked(self, scope: MeetingScope) -> AsyncIterator[tuple[AsyncSession, TeamMeetingBase]]:
        model = _get_meeting_model(scope.session_id)
        try:
            async with self._sessions.write() as session:
                # Write first to avoid upgrading a stale SQLite read snapshot.
                # A lock alone does not change the externally observed revision.
                result = await session.execute(
                    update(model).where(*_scope_filter(model, scope)).values(revision=model.revision)
                )
                if result.rowcount != 1:
                    raise _MeetingNotFoundError("Meeting does not exist")
                meeting = await session.get(model, _scope_values(scope))
                yield session, meeting
                await session.commit()
        except DBAPIError as exc:
            # Inspect after rollback: late calls after session release must not
            # recreate tables, and unrelated database failures must still surface.
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    raise _MeetingNotFoundError("Meeting does not exist") from exc
            raise

    async def _get_row(self, model: type[_MeetingRow], key: dict[str, str]) -> _MeetingRow | None:
        try:
            async with self._sessions.read() as session:
                return await session.get(model, key)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return None
            raise

    @staticmethod
    async def _find_team_meeting(
        session: AsyncSession, model: type[TeamMeetingBase], team_name: str, *, lock: bool = False,
    ) -> TeamMeetingBase | None:
        query = select(model).where(model.team_name == team_name, model.status != MeetingStatus.CLOSED).limit(2)
        if lock:
            query = query.with_for_update()
        rows = list(await session.scalars(query))
        if len(rows) > 1:
            raise_error(
                StatusCode.AGENT_TEAM_STATE_INVALID,
                reason="Team has multiple non-closed meetings; close the obsolete instances explicitly",
            )
        return rows[0] if rows else None

    async def create_meeting(self, scope: MeetingScope) -> TeamMeetingBase:
        """Create once, allowing one non-closed meeting per Team/session.

        Retrying an existing ID returns its persisted state without resuming a
        disabled or closed meeting. The leader identity comes from the Team.
        """
        await create_meeting_tables(self._sessions, scope.session_id)
        model = _get_meeting_model(scope.session_id)
        async with self._sessions.write() as session:
            locked = await session.execute(
                update(Team).where(Team.team_name == scope.team_name).values(team_name=Team.team_name)
            )
            if locked.rowcount != 1:
                raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Meeting requires an existing Team")
            current = await self._find_team_meeting(session, model, scope.team_name, lock=True)
            row = await session.get(model, _scope_values(scope), with_for_update=True)
            if row is None:
                if current is not None:
                    raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Team already has a non-closed meeting")
                leader = await session.scalar(
                    select(Team.leader_member_name).where(Team.team_name == scope.team_name)
                )
                row = model(**_scope_values(scope), leader_member_name=leader)
                session.add(row)
            await session.commit()
            return row

    async def get_meeting(self, scope: MeetingScope) -> TeamMeetingBase | None:
        """Inspect a meeting without acquiring ownership or creating tables."""
        model = _get_meeting_model(scope.session_id)
        return await self._get_row(model, _scope_values(scope))

    async def get_team_meeting(self, session_id: str, team_name: str) -> TeamMeetingBase | None:
        """Inspect the single active or disabled meeting; reject ambiguous rows."""
        model = _get_meeting_model(session_id)
        try:
            async with self._sessions.read() as session:
                return await self._find_team_meeting(session, model, team_name)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return None
            raise

    async def enqueue(
        self,
        scope: MeetingScope,
        request_id: str,
        member_name: str,
        *,
        speech_text: str,
        source_message_id: str | None = None,
    ) -> TeamSpeechRequestBase:
        """Admit an agent's utterance with an optional existing Team message ID.

        Identical request-ID retries return the stored request in any status;
        conflicting retries fail. Only active meetings admit new requests.
        Source identity validation belongs to the wrapper; database constraints
        reject invalid or duplicate non-null message references.
        """
        if not request_id.strip():
            raise ValueError("Request ID must be non-empty")
        if not speech_text.strip():
            raise ValueError("Speech text must be non-empty")
        if source_message_id is not None and not source_message_id.strip():
            raise ValueError("Source message ID must be non-empty")
        model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            key = {**_scope_values(scope), "request_id": request_id}
            existing = await session.get(model, key)
            if existing is not None:
                if (existing.member_name, existing.speech_text, existing.source_message_id) != (
                    member_name, speech_text, source_message_id,
                ):
                    raise ValueError("Request ID already belongs to a different speech request")
                return existing
            if meeting.status != MeetingStatus.ACTIVE:
                raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Meeting must be active to queue speech")
            role = await session.scalar(
                select(TeamMember.role).where(
                    TeamMember.team_name == scope.team_name, TeamMember.member_name == member_name,
                )
            )
            if role not in {"leader", "teammate", "bridge_agent", "external_cli"}:
                raise ValueError("Only registered agent members may enter the speaking queue")
            meeting.next_sequence += 1
            meeting.revision += 1
            request = model(
                **key,
                member_name=member_name,
                source_message_id=source_message_id,
                speech_text=speech_text,
                sequence=meeting.next_sequence,
                created_at=get_current_time(),
            )
            session.add(request)
            return request

    async def set_addressed_members(self, scope: MeetingScope, member_names: Iterable[str]) -> bool:
        """Replace trusted host metadata without changing existing FIFO positions."""
        addressed = json.dumps(sorted(set(member_names)))
        async with self._locked(scope) as (_, meeting):
            if meeting.status != MeetingStatus.ACTIVE:
                return False
            if meeting.addressed_members != addressed:
                meeting.addressed_members = addressed
                meeting.revision += 1
            return True

    @staticmethod
    def _pending_query(scope: MeetingScope, meeting: TeamMeetingBase):
        model = _get_speech_request_model(scope.session_id)
        priority = case(
            (model.member_name == meeting.leader_member_name, 0),
            (model.member_name.in_(json.loads(meeting.addressed_members)), 1),
            else_=2,
        )
        return (
            select(model)
            .where(*_scope_filter(model, scope), model.status == SpeechRequestStatus.QUEUED)
            .order_by(priority, model.sequence)
        )

    async def list_pending(self, scope: MeetingScope) -> list[TeamSpeechRequestBase]:
        """Inspect leader/addressed/other priority, then FIFO, in one transaction."""
        async with self._locked(scope) as (session, meeting):
            return list(await session.scalars(self._pending_query(scope, meeting)))

    async def get_request(self, scope: MeetingScope, request_id: str) -> TeamSpeechRequestBase | None:
        """Inspect a request in any status without claiming or changing it."""
        model = _get_speech_request_model(scope.session_id)
        return await self._get_row(model, {**_scope_values(scope), "request_id": request_id})
