# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trusted application boundary over the persisted audio queue."""

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from openjiuwen.agent_teams.schema.meeting import (
    MeetingScope,
    MeetingState,
    MeetingStatus,
    SpeechInterruption,
    SpeechRequest,
    SpeechRequestStatus,
)
from openjiuwen.agent_teams.schema.status import MEMBER_DEPARTED_STATUSES
from openjiuwen.agent_teams.tools.database import TeamDatabase
from openjiuwen.agent_teams.tools.database.meeting_dao import MeetingDao
from openjiuwen.agent_teams.tools.models import TeamMeetingBase, TeamSpeechRequestBase

if TYPE_CHECKING:
    from openjiuwen.agent_teams.meeting.member import MeetingMemberClient


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


class MeetingCoordinator:
    """Host-owned admission, observation, and playback controls for one meeting.

    The host derives member identity from its trusted runtime and gives agents
    only a client created by ``for_member``. This object never sends text,
    consumes mailboxes, starts agent execution, or automatically plays audio.
    The caller owns the database, scheduling, and audio-stop acknowledgement.
    """

    def __init__(self, database: TeamDatabase, scope: MeetingScope) -> None:
        self._database = database
        self._scope = scope

    @property
    def scope(self) -> MeetingScope:
        """Explicit session/team/meeting identity; independent of ambient context."""
        return self._scope

    def for_member(self, member_name: str) -> "MeetingMemberClient":
        """Bind a trusted runtime identity; membership is rechecked on every call."""
        from openjiuwen.agent_teams.meeting.member import MeetingMemberClient

        _require_text(member_name, "Member name")
        return MeetingMemberClient(self, member_name)

    async def _dao(self) -> MeetingDao:
        await self._database.initialize()
        return self._database.meeting

    async def _require_member(self, member_name: str) -> str:
        _require_text(member_name, "Member name")
        await self._dao()
        identity = await self._database.member.get_member_role_status(member_name, self.scope.team_name)
        if (identity is None or identity[0] not in {"leader", "teammate", "bridge_agent", "external_cli"}
                or identity[1] in MEMBER_DEPARTED_STATUSES):
            raise ValueError("Meeting member must be a registered agent which has not departed")
        return identity[0]

    def _state(self, row: TeamMeetingBase) -> MeetingState:
        return MeetingState(
            scope=self.scope,
            leader_member_name=row.leader_member_name,
            addressed_members=tuple(json.loads(row.addressed_members)),
            owner_request_id=row.owner_request_id,
            lease_token=row.lease_token,
            interruption_id=row.interruption_id,
            revision=row.revision,
            status=MeetingStatus(row.status),
        )

    def _request(self, row: TeamSpeechRequestBase) -> SpeechRequest:
        return SpeechRequest(
            scope=self.scope, request_id=row.request_id, member_name=row.member_name,
            source_message_id=row.source_message_id, speech_text=row.speech_text, sequence=row.sequence,
            status=SpeechRequestStatus(row.status), lease_token=row.lease_token,
            created_at=row.created_at, granted_at=row.granted_at,
        )

    async def start(self) -> MeetingState:
        """Create once or inspect the existing meeting, including terminal state."""
        dao = await self._dao()
        return self._state(await dao.create_meeting(self.scope))

    async def enable(self) -> MeetingState:
        """Create or resume this exact meeting ID, preserving any interruption barrier."""
        dao = await self._dao()
        return self._state(await dao.enable_team_meeting(
            self.scope.session_id, self.scope.team_name, meeting_id=self.scope.meeting_id,
        ))

    async def get_state(self) -> MeetingState | None:
        """Observe persisted state without claiming audio or creating a meeting."""
        dao = await self._dao()
        row = await dao.get_meeting(self.scope)
        return self._state(row) if row is not None else None

    async def request_speech(
        self, member_name: str, *, speech_text: str, source_message_id: str | None = None,
        request_id: str | None = None,
    ) -> SpeechRequest:
        """Admit host-selected speech, optionally referencing a normal Team message.

        The host supplies trusted speaker identity. References must belong to
        that speaker in this Team/session. Matching request-ID retries return
        the stored request; no admission automatically grants or delivers text.
        """
        _require_text(speech_text, "Speech text")
        if request_id is not None:
            _require_text(request_id, "Request ID")
        if source_message_id is not None:
            _require_text(source_message_id, "Source message ID")
        await self._require_member(member_name)
        dao = await self._dao()
        state = await dao.get_meeting(self.scope)
        if state is None:
            raise ValueError("Meeting must exist to request speech")
        if source_message_id is not None:
            identity = await self._database.message.get_message_identity(
                source_message_id, session_id=self.scope.session_id,
            )
            if identity != (self.scope.team_name, member_name):
                raise ValueError("Source message must belong to the requesting member and Team/session")
        return self._request(await dao.enqueue(
            self.scope, request_id if request_id is not None else uuid4().hex, member_name,
            speech_text=speech_text, source_message_id=source_message_id,
        ))

    async def get_request(self, request_id: str) -> SpeechRequest | None:
        """Inspect a queued, granted, or terminal request without reading its source text."""
        _require_text(request_id, "Request ID")
        dao = await self._dao()
        row = await dao.get_request(self.scope, request_id)
        return self._request(row) if row is not None else None

    async def list_pending(self) -> tuple[SpeechRequest, ...]:
        """Inspect the current priority/FIFO order without claiming playback."""
        dao = await self._dao()
        return tuple(self._request(row) for row in await dao.list_pending(self.scope))

    async def get_speaker(self) -> SpeechRequest | None:
        """Inspect the current audio owner, including while disabled."""
        dao = await self._dao()
        meeting = await dao.get_meeting(self.scope)
        if meeting is None or meeting.closed or meeting.owner_request_id is None:
            return None
        row = await dao.get_request(self.scope, meeting.owner_request_id)
        if (row is None or row.status != SpeechRequestStatus.GRANTED or row.lease_token is None
                or row.lease_token != meeting.lease_token):
            return None
        return self._request(row)

    async def set_addressed_members(self, member_names: Iterable[str]) -> bool:
        """Replace trusted host priority metadata without preempting current audio."""
        if isinstance(member_names, str):
            raise ValueError("Addressed members must be an iterable of member names")
        names = tuple(member_names)
        for name in names:
            _require_text(name, "Addressed member name")
        dao = await self._dao()
        return await dao.set_addressed_members(self.scope, names)

    async def grant_next(self) -> SpeechRequest | None:
        """Claim one utterance only when the host is ready to play audio."""
        dao = await self._dao()
        row = await dao.grant_next(self.scope)
        return self._request(row) if row is not None else None

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel queued speech while leaving the normal Team message intact."""
        _require_text(request_id, "Request ID")
        dao = await self._dao()
        return await dao.cancel_request(self.scope, request_id)

    async def release(self, lease_token: str) -> bool:
        """Acknowledge completed playback for the matching lease; never auto-grant."""
        dao = await self._dao()
        return await dao.release(self.scope, lease_token)

    async def interrupt_speech(self, *, expected_token: str | None = None) -> SpeechInterruption | None:
        """Trusted host interruption; stop audio before acknowledging the returned barrier."""
        dao = await self._dao()
        return await dao.interrupt_speech(self.scope, expected_token=expected_token)

    async def interrupt_from_leader(self, member_name: str, *, expected_token: str) -> SpeechInterruption | None:
        """Authorize a live Leader and interrupt only the observed lease."""
        _require_text(expected_token, "Observed lease token")
        role = await self._require_member(member_name)
        state = await self.get_state()
        if state is None:
            return None
        if role != "leader" or member_name != state.leader_member_name:
            raise ValueError("Only the meeting Leader may interrupt through the agent boundary")
        dao = await self._dao()
        return await dao.interrupt_speech(self.scope, expected_token=expected_token)

    async def resume_after_interruption(self, interruption_id: str) -> bool:
        """Host confirms stopped audio and, for human interruption, admitted input."""
        dao = await self._dao()
        return await dao.resume_after_interruption(self.scope, interruption_id)

    async def force_release(
        self, *, expected_token: str | None = None, expected_interruption_id: str | None = None,
    ) -> bool:
        """Recover only the exact observed lease/barrier; the host must stop abandoned audio."""
        dao = await self._dao()
        return await dao.force_release(
            self.scope, expected_token=expected_token, expected_interruption_id=expected_interruption_id,
        )

    async def disable(self) -> bool:
        """Disable new speech, retaining the current lease, queue, and interruption barrier."""
        dao = await self._dao()
        return await dao.disable(self.scope)

    async def close(self) -> bool:
        """Permanently close audio coordination; the host stops audio and keeps the database."""
        dao = await self._dao()
        return await dao.close_meeting(self.scope)
