# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-bound publication capabilities, separate from host playback controls."""

from typing import TYPE_CHECKING

from openjiuwen.agent_teams.schema.meeting import (
    MeetingMessage,
    MeetingPostResult,
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

    async def post_message(
        self,
        content: str,
        *,
        speech_text: str,
        mentions: tuple[str, ...] | list[str] = (),
    ) -> MeetingPostResult:
        """Queue full text and summary; normal Team sending starts after a speaking grant."""
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator._post_message(
            self.member_name,
            content,
            speech_text=speech_text,
            mentions=mentions,
        )

    async def get_history(self, *, limit: int = 50) -> tuple[MeetingMessage, ...]:
        """Read the team's public group history in this session, without consuming input."""
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator._get_history(limit)

    async def get_source_message(self, source_message_id: str) -> MeetingMessage | None:
        """Read public group messages in this team/session; exclude private messages."""
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator._get_conversation_message(source_message_id)

    async def get_state(self) -> MeetingState | None:
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator.get_state()

    async def get_speaker(self) -> SpeechRequest | None:
        await self._coordinator._require_member(self.member_name)
        request = await self._coordinator.get_speaker()
        if request is not None and request.source_message_id is not None:
            # Host admission can reference arbitrary sources; member tools may
            # expose only sources belonging to this public conversation.
            await self._coordinator._get_conversation_message(request.source_message_id)
        return request

    async def interrupt_speech(self, *, expected_token: str) -> SpeechInterruption | None:
        """Leader-only operation, checked at execution time against the persisted meeting."""
        await self._coordinator._require_member(self.member_name)
        return await self._coordinator.interrupt_from_leader(self.member_name, expected_token=expected_token)
