# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application boundary over the shared meeting queue; no mailbox consumption."""

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.handler import group_metadata
from openjiuwen.agent_teams.schema.events import EventMessage, MeetingQueueChangedEvent, TeamTopic
from openjiuwen.agent_teams.schema.meeting import (
    MeetingRoute,
    MeetingScope,
    MeetingState,
    MeetingStatus,
    SpeechInterruption,
    SpeechRequest,
    SpeechRequestStatus,
    SpeechSource,
)
from openjiuwen.agent_teams.schema.status import MEMBER_DEPARTED_STATUSES
from openjiuwen.agent_teams.tools.database import TeamDatabase
from openjiuwen.agent_teams.tools.database.meeting_dao import MeetingDao
from openjiuwen.agent_teams.tools.models import TeamMeetingBase, TeamSpeechRequestBase
from openjiuwen.core.common.logging import team_logger

if TYPE_CHECKING:
    from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
    from openjiuwen.agent_teams.meeting.member import MeetingMemberClient
    from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager


class MeetingCoordinator:
    """Expose queue ownership and explicit message references over an existing Team database.

    Host code must derive member names from trusted runtime identity. Do not expose
    this object or its host-only methods directly as agent/browser tools. The caller
    owns the database, publication dependencies, grant scheduling, and audio stop/input
    acknowledgement. This wrapper never starts a Team or acknowledges its mailbox.
    """

    def __init__(
        self,
        database: TeamDatabase,
        scope: MeetingScope,
        *,
        message_manager: "TeamMessageManager | None" = None,
        conversation: "GroupConversationLog | None" = None,
        on_state_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if message_manager is not None and (
            message_manager.db is not database or message_manager.team_name != scope.team_name
        ):
            raise ValueError("Message manager must belong to the coordinator's database and team")
        if conversation is not None and (
            conversation.team_name != scope.team_name or conversation.session_id != scope.session_id
        ):
            raise ValueError("Conversation must belong to the coordinator's team and session")
        self._database = database
        self._scope = scope
        self._message_manager = message_manager
        self._conversation = conversation
        self._on_state_change = on_state_change

    @property
    def queue_topic(self) -> str:
        """Transport topic for host consumers; subscribe before the initial drain."""
        return TeamTopic.MEETING_QUEUE.build(self.scope.session_id, self.scope.team_name)

    async def _notify_queue_changed(self) -> None:
        messager = self._message_manager.messager if self._message_manager is not None else None
        if messager is None:
            return
        try:
            await messager.publish(
                topic_id=self.queue_topic,
                message=EventMessage.from_event(MeetingQueueChangedEvent(
                    team_name=self.scope.team_name, meeting_id=self.scope.meeting_id,
                )),
            )
        except Exception as exc:
            # Admission/release already committed. Do not turn a lost wake into
            # a failed send receipt that makes the caller enqueue another copy.
            team_logger.warning("Meeting queue wake failed for %s: %s", self.scope.meeting_id, exc)

    def for_member(self, member_name: str) -> "MeetingMemberClient":
        """Bind trusted host identity to member inspection and interruption."""
        from openjiuwen.agent_teams.meeting.member import MeetingMemberClient

        if not isinstance(member_name, str) or not member_name.strip():
            raise ValueError("Member name must be nonempty")
        return MeetingMemberClient(self, member_name)

    async def _require_member(self, member_name: str) -> None:
        await self._dao()
        identity = await self._database.member.get_member_role_status(member_name, self.scope.team_name)
        if (
            identity is None
            or identity[0] not in {"leader", "teammate", "bridge_agent", "external_cli"}
            or identity[1] in MEMBER_DEPARTED_STATUSES
        ):
            raise ValueError("Meeting member must be a registered agent which has not departed")

    @staticmethod
    def _route_from_row(row) -> MeetingRoute:
        if group_metadata(row):
            return MeetingRoute()
        if row.broadcast:
            return MeetingRoute(kind="broadcast", protocol=row.protocol)
        return MeetingRoute(kind="direct", recipients=(row.to_member_name,), protocol=row.protocol)

    async def _read_message(self, message_id: str):
        await self._dao()
        token = set_session_id(self.scope.session_id)
        try:
            return await self._database.message.get_message(message_id)
        finally:
            reset_session_id(token)

    async def enqueue_message(
        self, member_name: str, content: str, *, speech_text: str,
        route: MeetingRoute, mentions: tuple[str, ...] = (), request_id: str | None = None,
    ) -> SpeechRequest:
        """Queue content explicitly selected by the host for meeting publication.

        Missing or closed meetings reject retries; disabled meetings admit work.
        This trusted primitive does not replace the tool boundary's recipient
        and dispatch policies. Audio eligibility is decided when dequeuing.
        """
        for label, value in (("Member name", member_name), ("Content", content), ("Speech text", speech_text)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a nonempty string")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise ValueError("Request ID must be a nonempty string")
        if not isinstance(route, MeetingRoute) or route.kind not in {"direct", "multicast", "broadcast", "group_chat"}:
            raise ValueError("A supported meeting route is required")
        if not isinstance(route.protocol, str) or not route.protocol.strip():
            raise ValueError("Route protocol must be a nonempty string")
        if any(not isinstance(name, str) or not name.strip() for name in route.recipients):
            raise ValueError("Route recipients must be nonempty member names")
        if len(set(route.recipients)) != len(route.recipients):
            raise ValueError("Route recipients must be distinct")
        if ((route.kind == "direct" and len(route.recipients) != 1)
                or (route.kind == "multicast" and not route.recipients)
                or (route.kind in {"broadcast", "group_chat"} and route.recipients)):
            raise ValueError("Route recipients do not match the route kind")
        if not isinstance(mentions, tuple) or any(not isinstance(name, str) or not name.strip() for name in mentions):
            raise ValueError("Mentions must be a tuple of nonempty member names")
        if len(set(mentions)) != len(mentions) or (mentions and route.kind != "group_chat"):
            raise ValueError("Only group routes support distinct mentions")
        state = await self.get_state()
        if state is None or state.closed:
            raise ValueError("Meeting must exist and be open to enqueue a message")
        await self._require_member(member_name)

        def matching(existing: SpeechRequest) -> SpeechRequest:
            if (
                existing.member_name, existing.content, existing.speech_text, existing.route, existing.mentions,
            ) != (member_name, content, speech_text, route, mentions):
                raise ValueError("client_message_id was already used for different meeting content")
            return existing

        if request_id is not None:
            existing = await self.get_request(request_id)
            if existing is not None:
                result = matching(existing)
                await self._notify_queue_changed()
                return result
        dao = await self._dao()
        try:
            row = await dao.enqueue(
                self.scope, request_id or uuid4().hex, member_name, content=content,
                speech_text=speech_text, mentions=mentions, route=route,
            )
        except IntegrityError:
            # Another process may win the same client_message_id after our read.
            existing = await self.get_request(request_id) if request_id is not None else None
            if existing is None:
                raise
            state = await self.get_state()
            if state is None or state.closed:
                raise ValueError("Meeting must exist and be open to enqueue a message") from None
            result = matching(existing)
            await self._notify_queue_changed()
            return result
        await self._notify_queue_changed()
        return await self._request(row)

    @property
    def scope(self) -> MeetingScope:
        """The explicit session/team/meeting identity shared by every operation."""
        return self._scope

    async def _dao(self) -> MeetingDao:
        await self._database.initialize()
        return self._database.meeting

    def _state(self, row: TeamMeetingBase) -> MeetingState:
        return MeetingState(
            scope=self.scope,
            leader_member_name=row.leader_member_name,
            addressed_members=tuple(json.loads(row.addressed_members)),
            closed=row.closed,
            owner_request_id=row.owner_request_id,
            lease_token=row.lease_token,
            interruption_id=row.interruption_id,
            revision=row.revision,
            status=MeetingStatus(row.status),
        )

    async def _request(self, row: TeamSpeechRequestBase) -> SpeechRequest:
        dao = await self._dao()
        ids = await dao.get_publication_ids(self.scope, row.request_id)
        ids = ids or ((row.source_message_id,) if row.source_message_id else ())
        messages = []
        message_error = None
        for message_id in ids:
            try:
                source = await self.get_source_message(message_id)
                if source is None:
                    message_error = "source_message_unavailable"
                else:
                    messages.append(source)
            except (ValueError, RuntimeError, OSError, SQLAlchemyError):
                message_error = "source_message_unavailable"
        if row.routing:
            route = MeetingRoute(**json.loads(row.routing))
        elif row.source_message_id:
            source_row = await self._read_message(row.source_message_id)
            route = self._route_from_row(source_row) if source_row is not None else MeetingRoute()
        else:
            route = MeetingRoute()
        message = messages[0] if messages else None
        return SpeechRequest(
            scope=self.scope, request_id=row.request_id, member_name=row.member_name,
            source_message_id=row.source_message_id, speech_text=row.speech_text, sequence=row.sequence,
            status=SpeechRequestStatus(row.status), lease_token=row.lease_token,
            created_at=row.created_at, granted_at=row.granted_at,
            content=message.content if message is not None else row.content,
            mentions=tuple(json.loads(row.mentions)), message=message, message_error=message_error,
            route=route, source_message_ids=ids, messages=tuple(messages),
        )

    async def start(self) -> MeetingState:
        """Create once or inspect the existing instance, including a closed one."""
        dao = await self._dao()
        state = self._state(await dao.create_meeting(self.scope))
        token = set_session_id(self.scope.session_id)
        try:
            # Queue-only admission does not create normal inbox/read tables.
            await self._database.create_cur_session_tables()
        finally:
            reset_session_id(token)
        try:
            if self._on_state_change is not None:
                await self._on_state_change()
        finally:
            await self._notify_queue_changed()
        return state

    async def get_state(self) -> MeetingState | None:
        """Observe persisted state without claiming or replaying any audio."""
        dao = await self._dao()
        row = await dao.get_meeting(self.scope)
        return self._state(row) if row is not None else None

    async def request_speech(
        self,
        member_name: str,
        *,
        request_id: str,
        source_message_id: str,
        speech_text: str,
    ) -> SpeechRequest:
        """Validate a committed source's team/author and enqueue its supplied summary.

        ``member_name`` comes from trusted host context. Request IDs must be unique.
        Admission does not publish text, mark it read, or grant audio.
        """
        dao = await self._dao()
        token = set_session_id(self.scope.session_id)
        try:
            source = await self._database.message.get_message(source_message_id)
        finally:
            reset_session_id(token)
        if source is None:
            raise ValueError("Source message does not exist in this session")
        if (source.team_name, source.from_member_name) != (self.scope.team_name, member_name):
            raise ValueError("Source message must belong to this team and the requesting member")
        row = await dao.enqueue(
            self.scope, request_id, member_name, source_message_id=source_message_id,
            speech_text=speech_text, route=self._route_from_row(source),
        )
        await self._notify_queue_changed()
        return await self._request(row)

    async def get_source_message(self, source_message_id: str) -> SpeechSource | None:
        """Read full text by ID, even after A2A consumption; never advance read state."""
        await self._dao()
        token = set_session_id(self.scope.session_id)
        try:
            row = await self._database.message.get_message(source_message_id)
        finally:
            reset_session_id(token)
        if row is None:
            return None
        if row.team_name != self.scope.team_name:
            raise ValueError("Source message belongs to another team")
        return SpeechSource(
            scope=self.scope,
            message_id=row.message_id,
            member_name=row.from_member_name,
            content=row.content,
            to_member_name=row.to_member_name,
            broadcast=row.broadcast,
            protocol=row.protocol,
            timestamp=row.timestamp,
        )

    async def get_request(self, request_id: str) -> SpeechRequest | None:
        """Inspect a queued, granted, or terminal audio request."""
        dao = await self._dao()
        row = await dao.get_request(self.scope, request_id)
        return await self._request(row) if row is not None else None

    async def list_pending(self) -> tuple[SpeechRequest, ...]:
        """Return current priority/FIFO order without claiming any request."""
        dao = await self._dao()
        return tuple([await self._request(row) for row in await dao.list_pending(self.scope)])

    async def set_addressed_members(self, member_names: Iterable[str]) -> bool:
        """Replace the trusted human-routing result; never preempt a current lease."""
        dao = await self._dao()
        changed = await dao.set_addressed_members(self.scope, member_names)
        if changed:
            await self._notify_queue_changed()
        return changed

    async def grant_next(self, *, playback_ready: bool = True) -> SpeechRequest | None:
        """Claim one request; the host publishes its text and handles playback."""
        if not isinstance(playback_ready, bool):
            raise ValueError("Playback readiness must be a boolean")
        dao = await self._dao()
        row = await dao.grant_next(self.scope, playback_ready=playback_ready)
        return await self._request(row) if row is not None else None

    async def record_publication(self, request_id: str, source_message_ids: tuple[str, ...]) -> SpeechRequest:
        """Validate and link sent messages, including sends completed after revocation."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("Request ID must be a nonempty string")
        if (not isinstance(source_message_ids, tuple) or not source_message_ids
                or any(not isinstance(value, str) or not value.strip() for value in source_message_ids)):
            raise ValueError("Publication requires a nonempty tuple of message IDs")
        if len(set(source_message_ids)) != len(source_message_ids):
            raise ValueError("Publication requires distinct message IDs")
        request = await self.get_request(request_id)
        if request is None:
            raise ValueError("Meeting request does not exist")
        if request.source_message_ids:
            if source_message_ids != request.source_message_ids:
                raise ValueError("Request already has different publication IDs")
        elif request.granted_at is None:
            raise ValueError("Publication requires a previously claimed request")
        recipients = request.route.recipients if request.route.kind in {"direct", "multicast"} else (None,)
        if len(source_message_ids) != len(recipients):
            raise ValueError("Publication requires one message ID per route recipient")
        for message_id, recipient in zip(source_message_ids, recipients):
            source = await self._read_message(message_id)
            if source is None or (source.team_name, source.from_member_name) != (
                self.scope.team_name, request.member_name,
            ):
                raise ValueError("Publication source must belong to the requesting member and session")
            # Preserve existing-source admission and publication retries.
            if request.source_message_ids:
                continue
            if source.content != request.content:
                raise ValueError("Publication source must match the queued content")
            if (source.to_member_name, source.broadcast, source.protocol) != (
                recipient, request.route.kind in {"broadcast", "group_chat"}, request.route.protocol,
            ):
                raise ValueError("Publication source must match the queued route and protocol")
            metadata = group_metadata(source)
            if request.route.kind == "group_chat":
                if not metadata or tuple(metadata.get("mentions", ())) != request.mentions:
                    raise ValueError("Publication source must match the queued group mentions")
            elif metadata:
                raise ValueError("Publication source must match the queued route kind")
        dao = await self._dao()
        await dao.record_publication(self.scope, request_id, source_message_ids)
        updated = await self.get_request(request_id)
        if updated is None:
            raise ValueError("Meeting request no longer exists")
        return updated

    async def finish_publication(self, lease_token: str, *, playback_ready: bool = True) -> SpeechRequest | None:
        """Finish recorded text publication; playback acknowledgement stays separate."""
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("Lease token must be a nonempty string")
        if not isinstance(playback_ready, bool):
            raise ValueError("Playback readiness must be a boolean")
        dao = await self._dao()
        row = await dao.finish_publication(self.scope, lease_token, playback_ready=playback_ready)
        if row is None:
            return None
        result = await self._request(row)
        if result.status == SpeechRequestStatus.TEXT_ONLY:
            await self._notify_queue_changed()
        return result

    async def get_speaker(self) -> SpeechRequest | None:
        """Observe the current audio/publication claim, including while disabled."""
        dao = await self._dao()
        meeting = await dao.get_meeting(self.scope)
        if meeting is None or meeting.closed or meeting.owner_request_id is None:
            return None
        row = await dao.get_request(self.scope, meeting.owner_request_id)
        if (row is None or row.status not in (SpeechRequestStatus.GRANTED, SpeechRequestStatus.PUBLISHING)
                or row.lease_token != meeting.lease_token):
            return None
        return await self._request(row)

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel queued text before sending; leave already sent messages intact."""
        dao = await self._dao()
        cancelled = await dao.cancel_request(self.scope, request_id)
        if cancelled:
            await self._notify_queue_changed()
        return cancelled

    async def complete_playback(self, lease_token: str) -> bool:
        """Acknowledge finished playback for the matching lease; never auto-grant."""
        dao = await self._dao()
        completed = await dao.complete_playback(self.scope, lease_token)
        if completed:
            await self._notify_queue_changed()
        return completed

    async def interrupt_speech(self, *, expected_token: str | None = None) -> SpeechInterruption | None:
        """Trusted host interruption; caller must stop audio before acknowledging."""
        dao = await self._dao()
        return await dao.interrupt_speech(self.scope, expected_token=expected_token)

    async def interrupt_from_leader(self, member_name: str, *, expected_token: str) -> SpeechInterruption | None:
        """Authorize a trusted Leader identity and interrupt only its observed lease."""
        if not expected_token.strip():
            raise ValueError("Leader interruption requires an observed lease token")
        dao = await self._dao()
        meeting = await dao.get_meeting(self.scope)
        if meeting is None:
            return None
        if member_name != meeting.leader_member_name:
            raise ValueError("Only the meeting Leader may interrupt through the agent boundary")
        return await dao.interrupt_speech(self.scope, expected_token=expected_token)

    async def resume_after_interruption(self, interruption_id: str) -> bool:
        """Host confirms audio stopped and, for human interruption, input admitted."""
        dao = await self._dao()
        resumed = await dao.resume_after_interruption(self.scope, interruption_id)
        if resumed:
            await self._notify_queue_changed()
        return resumed

    async def disable(self) -> bool:
        """Disable future audio; the consumer drains text and current playback may finish."""
        dao = await self._dao()
        disabled = await dao.disable(self.scope)
        if disabled:
            try:
                if self._on_state_change is not None:
                    await self._on_state_change()
            finally:
                await self._notify_queue_changed()
        return disabled

    async def close(self) -> bool:
        """Permanently close audio coordination; caller stops audio and owns the DB."""
        dao = await self._dao()
        closed = await dao.close_meeting(self.scope)
        if closed:
            try:
                if self._on_state_change is not None:
                    await self._on_state_change()
            finally:
                await self._notify_queue_changed()
        return closed
