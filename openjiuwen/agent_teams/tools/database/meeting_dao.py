# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared database authority for meeting queues and speaking leases.

Every mutation locks the meeting row with an UPDATE before reading it. Unlike
the local DbSessions lock, this also serializes independent member processes.
Callers are trusted runtime code; agent tools must not expose this DAO directly.
"""

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import case, inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openjiuwen.agent_teams.schema.meeting import MeetingScope, SpeechInterruption, SpeechRequestStatus
from openjiuwen.agent_teams.tools.database.engine import DbSessions, create_meeting_tables, get_current_time
from openjiuwen.agent_teams.tools.models import (
    Team,
    TeamMeetingBase,
    TeamMember,
    TeamSpeechRequestBase,
    _get_meeting_model,
    _get_speech_request_model,
)

_MeetingRow = TypeVar("_MeetingRow", TeamMeetingBase, TeamSpeechRequestBase)


class _MeetingNotFoundError(ValueError):
    """An explicit scope no longer has persisted coordination state."""


def _scope_filter(model: type[TeamMeetingBase] | type[TeamSpeechRequestBase], scope: MeetingScope) -> tuple:
    return (
        model.team_name == scope.team_name,
        model.meeting_id == scope.meeting_id,
    )


def _scope_values(scope: MeetingScope) -> dict[str, str]:
    return {"team_name": scope.team_name, "meeting_id": scope.meeting_id}


class MeetingDao:
    """Short atomic queue operations; no waits for models, publication, or playback."""

    def __init__(self, sessions: DbSessions) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def _locked(self, scope: MeetingScope) -> AsyncIterator[tuple[AsyncSession, TeamMeetingBase]]:
        meeting_model = _get_meeting_model(scope.session_id)
        try:
            async with self._sessions.write() as session:
                # Write first: do not upgrade a stale SQLite read snapshot.
                result = await session.execute(
                    update(meeting_model)
                    .where(*_scope_filter(meeting_model, scope))
                    .values(revision=meeting_model.revision + 1)
                )
                if result.rowcount != 1:
                    raise _MeetingNotFoundError("Meeting does not exist")
                meeting = await session.get(meeting_model, _scope_values(scope))
                yield session, meeting
                await session.commit()
        except DBAPIError as exc:
            # A late callback can arrive after session release dropped its
            # tables. Inspect only after rollback, preserving the write-first
            # transaction above and surfacing unrelated database failures.
            async with self._sessions.read() as session:
                if not await self._table_exists(session, meeting_model):
                    raise _MeetingNotFoundError("Meeting does not exist") from exc
            raise

    @staticmethod
    async def _table_exists(session: AsyncSession, model: type[TeamMeetingBase] | type[TeamSpeechRequestBase]) -> bool:
        conn = await session.connection()
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(model.__tablename__))

    async def _get_row(self, model: type[_MeetingRow], key: dict[str, str]) -> _MeetingRow | None:
        """Read normally; inspect missing tables only for late reads after session release."""
        try:
            async with self._sessions.read() as session:
                return await session.get(model, key)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return None
            raise

    async def create_meeting(self, scope: MeetingScope) -> TeamMeetingBase:
        """Create an instance once; duplicate creation never resets persisted state."""
        await create_meeting_tables(self._sessions, scope.session_id)
        meeting_model = _get_meeting_model(scope.session_id)
        async with self._sessions.write() as session:
            existing = await session.get(meeting_model, _scope_values(scope))
            if existing is not None:
                return existing
            leader = await session.scalar(select(Team.leader_member_name).where(Team.team_name == scope.team_name))
            if leader is None:
                raise ValueError("Team does not exist")
            meeting = meeting_model(**_scope_values(scope), leader_member_name=leader)
            session.add(meeting)
            try:
                await session.commit()
            except IntegrityError:
                # Another process may have created this instance concurrently.
                await session.rollback()
                existing = await session.get(meeting_model, _scope_values(scope))
                if existing is None:
                    raise
                return existing
            return meeting

    async def get_meeting(self, scope: MeetingScope) -> TeamMeetingBase | None:
        """Read ownership/barrier state without acquiring a write lock."""
        meeting_model = _get_meeting_model(scope.session_id)
        return await self._get_row(meeting_model, _scope_values(scope))

    async def enqueue(
        self,
        scope: MeetingScope,
        request_id: str,
        member_name: str,
        *,
        speech_text: str,
        content: str | None = None,
        mentions: Iterable[str] = (),
        source_message_id: str | None = None,
    ) -> TeamSpeechRequestBase:
        """Queue full text and summary, or reference an already sent source.

        Source identity and recipient validation belong to the wrapper. Database
        constraints reject duplicate IDs and invalid non-null source references.
        """
        if not request_id.strip():
            raise ValueError("Request ID must be non-empty")
        if not speech_text.strip():
            raise ValueError("Speech text must be non-empty")
        if source_message_id is not None:
            if not source_message_id.strip():
                raise ValueError("Source message ID must be non-empty")
        elif content is None or not content.strip():
            raise ValueError("Queued content must be non-empty")
        request_model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            if meeting.closed:
                raise ValueError("Meeting is closed")
            key = {**_scope_values(scope), "request_id": request_id}
            role = await session.scalar(
                select(TeamMember.role).where(
                    TeamMember.team_name == scope.team_name, TeamMember.member_name == member_name
                )
            )
            if role not in {"leader", "teammate", "bridge_agent", "external_cli"}:
                raise ValueError("Only registered agent members may enter the speaking queue")
            meeting.next_sequence += 1
            request = request_model(
                **key,
                member_name=member_name,
                source_message_id=source_message_id,
                content=content,
                speech_text=speech_text,
                mentions=json.dumps(list(mentions)),
                sequence=meeting.next_sequence,
                created_at=get_current_time(),
            )
            session.add(request)
            return request

    async def set_addressed_members(self, scope: MeetingScope, member_names: Iterable[str]) -> bool:
        """Replace trusted routing metadata; existing FIFO positions stay unchanged."""
        addressed = json.dumps(sorted(set(member_names)))
        async with self._locked(scope) as (_, meeting):
            if meeting.closed:
                return False
            meeting.addressed_members = addressed
            return True

    @staticmethod
    def _pending_query(scope: MeetingScope, meeting: TeamMeetingBase):
        request_model = _get_speech_request_model(scope.session_id)
        priority = case(
            (request_model.member_name == meeting.leader_member_name, 0),
            (request_model.member_name.in_(json.loads(meeting.addressed_members)), 1),
            else_=2,
        )
        return (
            select(request_model)
            .where(*_scope_filter(request_model, scope), request_model.status == SpeechRequestStatus.QUEUED)
            .order_by(priority, request_model.sequence)
        )

    async def list_pending(self, scope: MeetingScope) -> list[TeamSpeechRequestBase]:
        """Inspect the current ranking in one coherent meeting transaction."""
        async with self._locked(scope) as (session, meeting):
            result = await session.scalars(self._pending_query(scope, meeting))
            return list(result)

    async def get_request(self, scope: MeetingScope, request_id: str) -> TeamSpeechRequestBase | None:
        """Read a queued, granted, or terminal request for inspection/completion."""
        request_model = _get_speech_request_model(scope.session_id)
        return await self._get_row(request_model, {**_scope_values(scope), "request_id": request_id})

    async def grant_next(self, scope: MeetingScope) -> TeamSpeechRequestBase | None:
        """Grant one lease; message sending runs afterward in the wrapper."""
        async with self._locked(scope) as (session, meeting):
            if meeting.closed or meeting.interruption_id or meeting.owner_request_id:
                return None
            request = await session.scalar(self._pending_query(scope, meeting).limit(1))
            if request is None:
                return None
            request.status = SpeechRequestStatus.GRANTED
            request.lease_token = uuid4().hex
            request.granted_at = get_current_time()
            meeting.owner_request_id = request.request_id
            meeting.lease_token = request.lease_token
            return request

    async def record_publication(self, scope: MeetingScope, request_id: str, message_id: str) -> None:
        """Replace queued full text with the message reference after normal sending.

        Sending may overlap an interruption. Record the resulting message even
        then; text already sent remains published while playback is revoked.
        """
        request_model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, _):
            await session.execute(
                update(request_model)
                .where(*_scope_filter(request_model, scope), request_model.request_id == request_id)
                .values(source_message_id=message_id, content=None)
            )

    async def cancel_request(self, scope: MeetingScope, request_id: str) -> bool:
        """Cancel only a queued operation; never retract a granted/published message."""
        request_model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, _):
            result = await session.execute(
                update(request_model)
                .where(
                    *_scope_filter(request_model, scope),
                    request_model.request_id == request_id,
                    request_model.status == SpeechRequestStatus.QUEUED,
                )
                .values(status=SpeechRequestStatus.CANCELLED)
            )
            return result.rowcount == 1

    @staticmethod
    async def _release(session: AsyncSession, scope: MeetingScope, meeting: TeamMeetingBase, status: str) -> None:
        request_model = _get_speech_request_model(scope.session_id)
        if meeting.owner_request_id is not None:
            await session.execute(
                update(request_model)
                .where(
                    *_scope_filter(request_model, scope),
                    request_model.request_id == meeting.owner_request_id,
                )
                .values(status=status)
            )
        meeting.owner_request_id = None
        meeting.lease_token = None

    async def complete_playback(self, scope: MeetingScope, lease_token: str) -> bool:
        """Release only the current lease after trusted publication/playback completion."""
        try:
            async with self._locked(scope) as (session, meeting):
                if not lease_token or meeting.closed or meeting.lease_token != lease_token:
                    return False
                await self._release(session, scope, meeting, SpeechRequestStatus.COMPLETED)
                return True
        except _MeetingNotFoundError:
            return False

    async def interrupt_speech(
        self, scope: MeetingScope, *, expected_token: str | None = None
    ) -> SpeechInterruption | None:
        """Revoke the observed turn and block grants until explicit acknowledgement.

        Trusted human admission can also open a barrier when nobody is speaking.
        A Leader caller must supply the observed token through its tool boundary.
        """
        try:
            async with self._locked(scope) as (session, meeting):
                if meeting.closed or meeting.interruption_id:
                    return None
                if expected_token is not None and (not expected_token or meeting.lease_token != expected_token):
                    return None
                interruption = SpeechInterruption(uuid4().hex, meeting.owner_request_id, meeting.lease_token)
                await self._release(session, scope, meeting, SpeechRequestStatus.INTERRUPTED)
                meeting.interruption_id = interruption.interruption_id
                return interruption
        except _MeetingNotFoundError:
            return None

    async def resume_after_interruption(self, scope: MeetingScope, interruption_id: str) -> bool:
        """Acknowledge audio stopped/input admitted for this exact barrier."""
        try:
            async with self._locked(scope) as (_, meeting):
                if not interruption_id or meeting.closed or meeting.interruption_id != interruption_id:
                    return False
                meeting.interruption_id = None
                return True
        except _MeetingNotFoundError:
            return False

    async def close_meeting(self, scope: MeetingScope) -> bool:
        """Permanently revoke ownership and cancel unsent operations; caller stops audio."""
        request_model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            if meeting.closed:
                return False
            await self._release(session, scope, meeting, SpeechRequestStatus.INTERRUPTED)
            await session.execute(
                update(request_model)
                .where(*_scope_filter(request_model, scope), request_model.status == SpeechRequestStatus.QUEUED)
                .values(status=SpeechRequestStatus.CANCELLED)
            )
            meeting.closed = True
            meeting.interruption_id = None
            return True
