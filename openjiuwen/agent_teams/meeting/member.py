# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-bound speaker inspection and interruption, separate from host playback."""

from dataclasses import replace
from typing import TYPE_CHECKING

from openjiuwen.agent_teams.schema.meeting import (
    MeetingScope,
    MeetingState,
    SpeechInterruption,
    SpeechRequest,
)

if TYPE_CHECKING:
    from openjiuwen.agent_teams.meeting.coordinator import MeetingCoordinator


class MeetingMemberClient:
    """Created by a trusted host via coordinator.for_member, never from agent identity arguments."""

    def __init__(self, coordinator: "MeetingCoordinator", member_name: str) -> None:
        self._coordinator = coordinator
        self._member_name = member_name

    @property
    def scope(self) -> MeetingScope:
        return self._coordinator.scope

    @property
    def member_name(self) -> str:
        return self._member_name

    async def get_state(self) -> MeetingState | None:
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator.get_state()

    async def get_speaker(self) -> SpeechRequest | None:
        await self._coordinator._require_member(self.member_name)
        request = await self._coordinator.get_speaker()
        if request is None:
            return None
        return replace(
            request, content=None, speech_text="", message=None, messages=(),
            source_message_id=None, source_message_ids=(),
        )

    async def interrupt_speech(self, *, expected_token: str) -> SpeechInterruption | None:
        """Leader-only operation, checked at execution time against the persisted meeting."""
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator.interrupt_from_leader(self.member_name, expected_token=expected_token)
