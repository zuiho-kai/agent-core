# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared database authority for meeting queues and speaking leases.

Queue mutations lock the meeting row with an UPDATE before reading it.
Creation/activation lock the owning Team row before checking for a live meeting. Unlike
the local DbSessions lock, this also serializes independent member processes.
Callers are trusted runtime code; agent tools must not expose this DAO directly.
"""

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TypeVar
from uuid import uuid4

from sqlalchemy import case, inspect, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openjiuwen.agent_teams.schema.meeting import (
    MeetingRoute,
    MeetingScope,
    MeetingStatus,
    SpeechInterruption,
    SpeechRequestStatus,
)
from openjiuwen.agent_teams.tools.database.engine import (
    DbSessions,
    _ensure_meeting_lifecycle_schema,
    _ensure_speech_request_schema,
    create_meeting_tables,
    get_current_time,
)
from openjiuwen.agent_teams.tools.models import (
    Team,
    TeamMeetingBase,
    TeamMember,
    TeamSpeechPublicationBase,
    TeamSpeechRequestBase,
    _get_meeting_model,
    _get_speech_publication_model,
    _get_speech_request_model,
)
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import raise_error

_MeetingRow = TypeVar("_MeetingRow", TeamMeetingBase, TeamSpeechRequestBase)


class _MeetingNotFoundError(ValueError):
    """An explicit scope no longer has persisted coordination state."""


def _scope_filter(
    model: type[TeamMeetingBase] | type[TeamSpeechRequestBase] | type[TeamSpeechPublicationBase], scope: MeetingScope,
) -> tuple:
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
        self._lifecycle_schemas_ready: set[str] = set()

    async def _ensure_lifecycle_schema(self, session_id: str) -> None:
        """Upgrade existing queues on first access, including standalone callers.

        Cache schema readiness only, never active meeting state. Do not recreate
        missing tables: late receipts after session release must remain harmless.
        """
        model = _get_meeting_model(session_id)
        if model.__tablename__ in self._lifecycle_schemas_ready:
            return
        async with self._sessions.write() as session:
            if await self._table_exists(session, model):
                conn = await session.connection()
                await conn.run_sync(_ensure_meeting_lifecycle_schema, model.__table__)
                await conn.run_sync(_ensure_speech_request_schema, _get_speech_request_model(session_id).__table__)
                await conn.run_sync(_get_speech_publication_model(session_id).__table__.create, checkfirst=True)
            await session.commit()
        self._lifecycle_schemas_ready.add(model.__tablename__)

    @asynccontextmanager
    async def _locked(self, scope: MeetingScope) -> AsyncIterator[tuple[AsyncSession, TeamMeetingBase]]:
        meeting_model = _get_meeting_model(scope.session_id)
        await self._ensure_lifecycle_schema(scope.session_id)
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
    async def _table_exists(
        session: AsyncSession,
        model: type[TeamMeetingBase] | type[TeamSpeechRequestBase] | type[TeamSpeechPublicationBase],
    ) -> bool:
        conn = await session.connection()
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(model.__tablename__))

    async def _get_row(
        self, model: type[_MeetingRow], key: dict[str, str], *, session_id: str,
    ) -> _MeetingRow | None:
        """Read normally; inspect missing tables only for late reads after session release."""
        await self._ensure_lifecycle_schema(session_id)
        try:
            async with self._sessions.read() as session:
                return await session.get(model, key)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return None
            raise

    async def create_meeting(self, scope: MeetingScope) -> TeamMeetingBase:
        """Create once without resuming a disabled or closed instance.

        Standalone and Team activation share the same one-live-meeting rule.
        """
        return await self._create_or_enable(scope, enable=False, reuse_current=False)

    async def get_meeting(self, scope: MeetingScope) -> TeamMeetingBase | None:
        """Read ownership/barrier state without acquiring a write lock."""
        meeting_model = _get_meeting_model(scope.session_id)
        return await self._get_row(meeting_model, _scope_values(scope), session_id=scope.session_id)

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

    async def _create_or_enable(
        self, scope: MeetingScope, *, enable: bool, reuse_current: bool,
    ) -> TeamMeetingBase:
        await create_meeting_tables(self._sessions, scope.session_id)
        model = _get_meeting_model(scope.session_id)
        async with self._sessions.write() as session:
            # Every creation path takes the Team lock, including standalone start.
            # This serializes different requested IDs across member processes.
            locked = await session.execute(
                update(Team).where(Team.team_name == scope.team_name).values(team_name=Team.team_name)
            )
            if locked.rowcount != 1:
                raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Meeting requires an existing Team")
            current = await self._find_team_meeting(session, model, scope.team_name, lock=True)
            row = current if reuse_current and current is not None else await session.get(
                model, _scope_values(scope), with_for_update=True,
            )
            if row is None:
                if current is not None:
                    raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Team already has a non-closed meeting")
                leader = await session.scalar(select(Team.leader_member_name).where(Team.team_name == scope.team_name))
                row = model(**_scope_values(scope), leader_member_name=leader)
                session.add(row)
            elif enable:
                if row.status == MeetingStatus.CLOSED:
                    raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Closed meeting IDs cannot be re-enabled")
                if row.status == MeetingStatus.DISABLED:
                    if row.interruption_id is not None:
                        raise_error(
                            StatusCode.AGENT_TEAM_STATE_INVALID,
                            reason="Retry disabling the meeting to finish its pending text flush before enabling",
                        )
                    row.status = MeetingStatus.ACTIVE
                    row.revision += 1
            await session.commit()
            return row

    async def enable_team_meeting(
        self, session_id: str, team_name: str, *, meeting_id: str | None = None,
    ) -> TeamMeetingBase:
        """Create or resume the Team/session's non-closed meeting with the same ID."""
        scope = MeetingScope(session_id, team_name, meeting_id if meeting_id is not None else uuid4().hex)
        return await self._create_or_enable(scope, enable=True, reuse_current=meeting_id is None)

    async def get_team_meeting(self, session_id: str, team_name: str) -> TeamMeetingBase | None:
        """Read the single active or disabled meeting; never pick ambiguous rows."""
        model = _get_meeting_model(session_id)
        await self._ensure_lifecycle_schema(session_id)
        try:
            async with self._sessions.read() as session:
                return await self._find_team_meeting(session, model, team_name)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return None
            raise

    async def begin_disable(self, scope: MeetingScope) -> str | None:
        """Revoke audio and retain pending text behind a resumable flush barrier."""
        try:
            async with self._locked(scope) as (session, meeting):
                if meeting.status == MeetingStatus.CLOSED:
                    return None
                if meeting.status == MeetingStatus.DISABLED:
                    return meeting.interruption_id
                owner_id = meeting.owner_request_id
                await self._release(session, scope, meeting, SpeechRequestStatus.INTERRUPTED)
                model = _get_speech_request_model(scope.session_id)
                await session.execute(
                    update(model)
                    .where(
                        *_scope_filter(model, scope),
                        or_(model.status == SpeechRequestStatus.QUEUED, model.request_id == owner_id),
                    )
                    .values(status=SpeechRequestStatus.FLUSHING)
                )
                meeting.status = MeetingStatus.DISABLED
                meeting.interruption_id = uuid4().hex
                return meeting.interruption_id
        except _MeetingNotFoundError:
            return None

    async def next_flush_request(self, scope: MeetingScope, flush_token: str) -> TeamSpeechRequestBase | None:
        """Read the oldest pending text; publication happens outside the transaction."""
        model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            if meeting.status != MeetingStatus.DISABLED or meeting.interruption_id != flush_token:
                return None
            return await session.scalar(
                select(model)
                .where(*_scope_filter(model, scope), model.status == SpeechRequestStatus.FLUSHING)
                .order_by(model.sequence)
                .limit(1)
            )

    async def complete_flush_request(
        self, scope: MeetingScope, flush_token: str, request_id: str, message_id: str | tuple[str, ...],
    ) -> bool:
        """Record sent text and cancel its audio atomically for the current flush."""
        model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            if meeting.status != MeetingStatus.DISABLED or meeting.interruption_id != flush_token:
                return False
            row = await session.get(model, {**_scope_values(scope), "request_id": request_id})
            if row is None or row.status != SpeechRequestStatus.FLUSHING:
                return False
            await self._record_publications(session, scope, row, message_id)
            row.status = SpeechRequestStatus.INTERRUPTED if row.lease_token else SpeechRequestStatus.CANCELLED
            return True

    async def finish_disable(self, scope: MeetingScope, flush_token: str) -> bool:
        """Finish only after all text is recorded; failed flushes remain retryable."""
        model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, meeting):
            if meeting.status != MeetingStatus.DISABLED or meeting.interruption_id != flush_token:
                return False
            pending = await session.scalar(
                select(model.request_id)
                .where(*_scope_filter(model, scope), model.status == SpeechRequestStatus.FLUSHING)
                .limit(1)
            )
            if pending is not None:
                return False
            meeting.interruption_id = None
            return True

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
        route: MeetingRoute | None = None,
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
            if meeting.status != MeetingStatus.ACTIVE:
                raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Meeting must be active to queue speech")
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
                routing=json.dumps(asdict(route or MeetingRoute())),
                sequence=meeting.next_sequence,
                created_at=get_current_time(),
            )
            session.add(request)
            return request

    async def set_addressed_members(self, scope: MeetingScope, member_names: Iterable[str]) -> bool:
        """Replace trusted routing metadata; existing FIFO positions stay unchanged."""
        addressed = json.dumps(sorted(set(member_names)))
        async with self._locked(scope) as (_, meeting):
            if meeting.status != MeetingStatus.ACTIVE:
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
        return await self._get_row(
            request_model, {**_scope_values(scope), "request_id": request_id}, session_id=scope.session_id,
        )

    async def grant_next(self, scope: MeetingScope) -> TeamSpeechRequestBase | None:
        """Grant one lease; message sending runs afterward in the wrapper."""
        async with self._locked(scope) as (session, meeting):
            if meeting.status != MeetingStatus.ACTIVE or meeting.interruption_id or meeting.owner_request_id:
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

    async def get_publication_ids(self, scope: MeetingScope, request_id: str) -> tuple[str, ...]:
        await self._ensure_lifecycle_schema(scope.session_id)
        model = _get_speech_publication_model(scope.session_id)
        try:
            async with self._sessions.read() as session:
                ids = await session.scalars(
                    select(model.message_id)
                    .where(*_scope_filter(model, scope), model.request_id == request_id)
                    .order_by(model.position)
                )
                return tuple(ids)
        except DBAPIError:
            async with self._sessions.read() as session:
                if not await self._table_exists(session, model):
                    return ()
            raise

    async def record_publication(
        self, scope: MeetingScope, request_id: str, message_id: str | tuple[str, ...],
    ) -> None:
        """Record all normal message references, even after audio revocation."""
        model = _get_speech_request_model(scope.session_id)
        async with self._locked(scope) as (session, _):
            row = await session.get(model, {**_scope_values(scope), "request_id": request_id})
            if row is not None:
                await self._record_publications(session, scope, row, message_id)

    @staticmethod
    async def _record_publications(
        session: AsyncSession, scope: MeetingScope, row: TeamSpeechRequestBase, message_id: str | tuple[str, ...],
    ) -> None:
        ids = (message_id,) if isinstance(message_id, str) else tuple(message_id)
        if not ids or len(ids) != len(set(ids)) or any(not value for value in ids):
            raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Publication requires distinct message IDs")
        model = _get_speech_publication_model(scope.session_id)
        existing = tuple(await session.scalars(
            select(model.message_id)
            .where(*_scope_filter(model, scope), model.request_id == row.request_id)
            .order_by(model.position)
        ))
        if existing and existing != ids:
            raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Request already has different publication IDs")
        if row.source_message_id is not None and row.source_message_id != ids[0]:
            raise_error(StatusCode.AGENT_TEAM_STATE_INVALID, reason="Request source cannot change after publication")
        if not existing:
            for position, value in enumerate(ids):
                session.add(model(
                    **_scope_values(scope), request_id=row.request_id, message_id=value, position=position,
                ))
        row.source_message_id = ids[0]
        row.content = None

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
                if not lease_token or meeting.status != MeetingStatus.ACTIVE or meeting.lease_token != lease_token:
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
                if meeting.status != MeetingStatus.ACTIVE or meeting.interruption_id:
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
                if (
                    not interruption_id
                    or meeting.status != MeetingStatus.ACTIVE
                    or meeting.interruption_id != interruption_id
                ):
                    return False
                meeting.interruption_id = None
                return True
        except _MeetingNotFoundError:
            return False

    async def close_meeting(self, scope: MeetingScope) -> bool:
        """Permanently revoke ownership and cancel unsent operations; caller stops audio."""
        async with self._locked(scope) as (session, meeting):
            if meeting.status == MeetingStatus.CLOSED:
                return False
            await self._stop_speech(session, scope, meeting)
            meeting.status = MeetingStatus.CLOSED
            return True

    async def _stop_speech(self, session: AsyncSession, scope: MeetingScope, meeting: TeamMeetingBase) -> None:
        request_model = _get_speech_request_model(scope.session_id)
        await self._release(session, scope, meeting, SpeechRequestStatus.INTERRUPTED)
        await session.execute(
            update(request_model)
            .where(
                *_scope_filter(request_model, scope),
                request_model.status.in_((SpeechRequestStatus.QUEUED, SpeechRequestStatus.FLUSHING)),
            )
            .values(status=SpeechRequestStatus.CANCELLED)
        )
        meeting.interruption_id = None
