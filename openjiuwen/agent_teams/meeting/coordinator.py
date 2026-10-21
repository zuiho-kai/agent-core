# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application boundary over the shared meeting queue; no mailbox consumption."""

import json
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.handler import group_metadata, post_message, sync_history
from openjiuwen.agent_teams.schema.meeting import (
    MeetingMessage,
    MeetingPostResult,
    MeetingScope,
    MeetingState,
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
    """Attach speaking ownership and grant-gated publication to an existing Team database.

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

    def for_member(self, member_name: str) -> "MeetingMemberClient":
        """Bind trusted host identity to a restricted member publication interface."""
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

    def _message(self, row) -> MeetingMessage:
        meta = group_metadata(row)
        if row.team_name != self.scope.team_name or not row.broadcast or not meta:
            raise ValueError("Message does not belong to this public conversation")
        return MeetingMessage(
            scope=self.scope,
            message_id=row.message_id,
            member_name=row.from_member_name,
            content=row.content,
            mentions=tuple(meta["mentions"]),
            timestamp=row.timestamp,
            published=row.broadcast,
        )

    async def _get_conversation_message(self, message_id: str) -> MeetingMessage | None:
        await self._dao()
        token = set_session_id(self.scope.session_id)
        try:
            row = await self._database.message.get_message(message_id)
        finally:
            reset_session_id(token)
        return self._message(row) if row is not None else None

    async def _get_history(self, limit: int) -> tuple[MeetingMessage, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("History limit must be an integer from 1 to 100")
        await self._dao()
        token = set_session_id(self.scope.session_id)
        try:
            rows = await self._database.message.get_team_messages(self.scope.team_name, broadcast=True)
        finally:
            reset_session_id(token)
        rows = [row for row in rows if group_metadata(row)]
        rows.sort(key=lambda row: (row.timestamp, row.message_id))
        return tuple(self._message(row) for row in rows[-limit:])

    async def _post_message(
        self,
        member_name: str,
        content: str,
        *,
        speech_text: str | None,
        mentions: tuple[str, ...] | list[str],
    ) -> MeetingPostResult:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Meeting content must be nonblank text")
        if member_name != "user" and (not isinstance(speech_text, str) or not speech_text.strip()):
            raise ValueError("Meeting speech_text must be nonblank text")
        if not isinstance(mentions, (list, tuple)) or len(mentions) > 100:
            raise ValueError("mentions must be a list of at most 100 member names")
        if any(not isinstance(name, str) or not name.strip() or len(name) > 255 for name in mentions):
            raise ValueError("mentions must contain nonempty member names")
        if self._message_manager is None or self._conversation is None:
            raise ValueError("Meeting publication requires the existing message manager and conversation")
        state = await self.get_state()
        if state is None or state.closed:
            raise ValueError("Meeting must exist and be open for publication")
        mentions = tuple(dict.fromkeys(mentions))
        for name in mentions:
            if name == "user":
                continue
            identity = await self._database.member.get_member_role_status(name, self.scope.team_name)
            if identity is None or identity[1] in MEMBER_DEPARTED_STATUSES:
                raise ValueError(f"Unknown or departed group member: {name}")
        request_id = uuid4().hex
        if member_name == "user":
            posted = await self._send_message(member_name, content, mentions, request_id)
            message = await self._get_conversation_message(posted.message.message_id)
            if message is None:
                raise RuntimeError("Published human message is no longer available")
            return MeetingPostResult(
                message=message,
                duplicate=False,
                notified_members=tuple(posted.notified_members),
                speech_request=None,
            )
        dao = await self._dao()
        row = await dao.enqueue(
            self.scope, request_id, member_name, content=content, speech_text=speech_text, mentions=mentions
        )
        return MeetingPostResult(
            message=MeetingMessage(
                scope=self.scope,
                message_id=None,
                member_name=member_name,
                content=content,
                mentions=mentions,
                timestamp=row.created_at,
                published=False,
            ),
            duplicate=False,
            notified_members=(),
            speech_request=await self._request(row),
        )

    async def _send_message(self, member_name: str, content: str, mentions: tuple[str, ...], request_id: str):
        """Use existing group storage, history, transport, and recipient acknowledgements."""
        token = set_session_id(self.scope.session_id)
        try:
            return await post_message(
                self._conversation,
                self._message_manager,
                member_name,
                content,
                client_message_id=request_id,
                mentions=mentions,
            )
        finally:
            reset_session_id(token)

    async def post_human_message(
        self,
        content: str,
        *,
        mentions: tuple[str, ...] | list[str] = (),
    ) -> MeetingPostResult:
        """Send human text immediately through existing Team messaging."""
        return await self._post_message("user", content, speech_text=None, mentions=mentions)

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
        )

    async def _request(self, row: TeamSpeechRequestBase) -> SpeechRequest:
        message = None
        message_error = None
        if row.source_message_id is not None:
            try:
                message = await self.get_source_message(row.source_message_id)
                if message is None:
                    message_error = "source_message_unavailable"
            except (ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
                team_logger.warning("Meeting source lookup failed for %s: %s", row.source_message_id, exc)
                message_error = "source_message_unavailable"
        return SpeechRequest(
            scope=self.scope,
            request_id=row.request_id,
            member_name=row.member_name,
            source_message_id=row.source_message_id,
            speech_text=row.speech_text,
            sequence=row.sequence,
            status=SpeechRequestStatus(row.status),
            lease_token=row.lease_token,
            created_at=row.created_at,
            granted_at=row.granted_at,
            content=message.content if message is not None else row.content,
            mentions=tuple(json.loads(row.mentions)),
            message=message,
            message_error=message_error,
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
        return await self._request(
            await dao.enqueue(
                self.scope,
                request_id,
                member_name,
                source_message_id=source_message_id,
                speech_text=speech_text,
            )
        )

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
        return await dao.set_addressed_members(self.scope, member_names)

    async def grant_next(self) -> SpeechRequest | None:
        """Claim queued work, send full text normally, then return both versions for audio."""
        dao = await self._dao()
        row = await dao.grant_next(self.scope)
        if row is None:
            return None
        return await self._publish_granted(await self._request(row))

    async def _publish_granted(self, request: SpeechRequest, *, recover: bool = False) -> SpeechRequest | None:
        """Send only claimed queue content; ordinary reads never publish anything."""
        state = await self.get_state()
        if state is None or state.closed or state.lease_token != request.lease_token:
            return None
        if request.source_message_id is None:
            try:
                if self._message_manager is None or self._conversation is None:
                    raise ValueError("Sending queued speech requires message manager and conversation")
                source_id = None
                if recover:
                    # Use the existing group submission identity only on recovery;
                    # an already committed message may outlive its members.
                    identity = str(
                        uuid5(
                            NAMESPACE_URL,
                            json.dumps(
                                [self.scope.team_name, self.scope.session_id, request.request_id], ensure_ascii=False
                            ),
                        )
                    )
                    source = await self._get_conversation_message(identity)
                    if source is not None:
                        if (source.member_name, source.content, source.mentions) != (
                            request.member_name,
                            request.content,
                            request.mentions,
                        ):
                            raise ValueError("Stored publication does not match the queued utterance")
                        token = set_session_id(self.scope.session_id)
                        try:
                            await sync_history(self._conversation, self._message_manager)
                            await self._message_manager.publish_broadcast(identity, request.member_name)
                        finally:
                            reset_session_id(token)
                        source_id = source.message_id
                if source_id is None:
                    posted = await self._send_message(
                        request.member_name, request.content, request.mentions, request.request_id
                    )
                    source_id = posted.message.message_id
                # Preserve the known source in the error result if recording
                # its durable queue link fails after sending has succeeded.
                request = replace(request, source_message_id=source_id)
                dao = await self._dao()
                await dao.record_publication(self.scope, request.request_id, source_id)
                updated = await self.get_request(request.request_id)
                if updated is None:
                    return None
                request = updated
            except Exception as exc:
                team_logger.warning("Granted meeting publication failed for %s: %s", request.request_id, exc)
                request = replace(request, delivery_error="message_publication_failed")
        # Sending may overlap interruption/close. Already sent text remains;
        # a revoked turn must never be returned as ready for playback.
        state = await self.get_state()
        if state is None or state.closed or state.lease_token != request.lease_token:
            return None
        return request

    async def retry_publication(self, lease_token: str) -> SpeechRequest | None:
        """Retry delivery preparation for this current lease, retaining the same token."""
        request = await self.get_speaker()
        if request is None or request.lease_token != lease_token:
            return None
        return await self._publish_granted(request, recover=True)

    async def get_speaker(self) -> SpeechRequest | None:
        """Observe the current lease; a concurrent release can make it stale immediately."""
        dao = await self._dao()
        meeting = await dao.get_meeting(self.scope)
        if meeting is None or meeting.closed or meeting.owner_request_id is None:
            return None
        row = await dao.get_request(self.scope, meeting.owner_request_id)
        if row is None or row.status != SpeechRequestStatus.GRANTED or row.lease_token != meeting.lease_token:
            return None
        return await self._request(row)

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel queued text before sending; leave already sent messages intact."""
        dao = await self._dao()
        return await dao.cancel_request(self.scope, request_id)

    async def complete_playback(self, lease_token: str) -> bool:
        """Acknowledge finished playback for the matching lease; never auto-grant."""
        dao = await self._dao()
        return await dao.complete_playback(self.scope, lease_token)

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
        return await dao.resume_after_interruption(self.scope, interruption_id)

    async def close(self) -> bool:
        """Permanently close audio coordination; caller stops audio and owns the DB."""
        dao = await self._dao()
        return await dao.close_meeting(self.scope)
