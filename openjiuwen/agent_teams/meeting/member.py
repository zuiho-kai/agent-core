# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-bound speech admission and observation, separate from host playback."""

from dataclasses import replace
from typing import TYPE_CHECKING

from openjiuwen.agent_teams.schema.meeting import MeetingScope, MeetingState, SpeechInterruption, SpeechRequest

if TYPE_CHECKING:
    from openjiuwen.agent_teams.meeting.coordinator import MeetingCoordinator


class MeetingMemberClient:
    """Created by a trusted host via ``for_member``; never bind identity from agent arguments."""

    def __init__(self, coordinator: "MeetingCoordinator", member_name: str) -> None:
        self._coordinator = coordinator
        self._member_name = member_name

    @property
    def scope(self) -> MeetingScope:
        return self._coordinator.scope

    @property
    def member_name(self) -> str:
        return self._member_name

    async def request_speech(
        self, *, speech_text: str, source_message_id: str | None = None, request_id: str | None = None,
    ) -> SpeechRequest:
        """Request speech only as this bound member, without sending or claiming audio."""
        return await self._coordinator.request_speech(
            self.member_name, speech_text=speech_text, source_message_id=source_message_id, request_id=request_id,
        )

    async def get_request(self, request_id: str) -> SpeechRequest | None:
        """Inspect only this member's own request."""
        await self._coordinator._require_member(self.member_name)
        request = await self._coordinator.get_request(request_id)
        return request if request is not None and request.member_name == self.member_name else None

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel only this member's queued speech."""
        request = await self.get_request(request_id)
        if request is None:
            return False
        return await self._coordinator.cancel_request(request_id)

    async def get_state(self) -> MeetingState | None:
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator.get_state()

    async def get_speaker(self) -> SpeechRequest | None:
        """Inspect ownership metadata without disclosing another member's text or message reference."""
        await self._coordinator._require_member(self.member_name)
        request = await self._coordinator.get_speaker()
        return replace(request, speech_text="", source_message_id=None) if request is not None else None

    async def interrupt_speech(self, *, expected_token: str) -> SpeechInterruption | None:
        """Leader-only interruption with execution-time authorization and an observed lease."""
        return await self._coordinator.interrupt_from_leader(self.member_name, expected_token=expected_token)
