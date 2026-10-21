# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit member toolset; install in place of unrestricted message tools."""

from dataclasses import asdict
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from openjiuwen.agent_teams.meeting.member import MeetingMemberClient
from openjiuwen.agent_teams.tools.locales import Translator, make_translator
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.common.logging import team_logger
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput


class _MeetingTool(TeamTool):
    def __init__(self, client: MeetingMemberClient, t: Translator, name: str, schema: dict[str, Any]) -> None:
        super().__init__(ToolCard(id=f"team.{name}", name=name, description=t(name), input_params=schema))
        self._client = client

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> ToolOutput:
        try:
            schema = self.card.input_params
            if not isinstance(inputs, dict) or set(inputs) - set(schema["properties"]):
                raise ValueError("Only the declared meeting tool arguments are supported")
            if set(schema.get("required", ())) - set(inputs):
                raise ValueError("Required meeting tool arguments are missing")
            return ToolOutput(success=True, data=await self._run(inputs))
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        except (RuntimeError, OSError, SQLAlchemyError) as exc:
            team_logger.warning("Meeting tool %s failed: %s", self.card.name, exc)
            return ToolOutput(success=False, error="meeting_operation_failed")

    async def _run(self, inputs: dict[str, Any]) -> Any:
        raise NotImplementedError


class MeetingPostMessageTool(_MeetingTool):
    def __init__(self, client: MeetingMemberClient, t: Translator) -> None:
        super().__init__(
            client,
            t,
            "meeting_post_message",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "speech_text": {"type": "string", "minLength": 1},
                    "mentions": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                },
                "required": ["content", "speech_text"],
                "additionalProperties": False,
            },
        )

    async def _run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return asdict(await self._client.post_message(**inputs))


class MeetingReadHistoryTool(_MeetingTool):
    def __init__(self, client: MeetingMemberClient, t: Translator) -> None:
        super().__init__(
            client,
            t,
            "meeting_read_history",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "additionalProperties": False,
            },
        )

    async def _run(self, inputs: dict[str, Any]) -> list[dict[str, Any]]:
        return [asdict(item) for item in await self._client.get_history(**inputs)]


class MeetingGetSpeakerTool(_MeetingTool):
    def __init__(self, client: MeetingMemberClient, t: Translator) -> None:
        super().__init__(
            client,
            t,
            "meeting_get_speaker",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def _run(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        request = await self._client.get_speaker()
        if request is None:
            return None
        payload = asdict(request)
        source = payload.pop("message")
        return {"request": payload, "source": source}


class MeetingInterruptSpeechTool(_MeetingTool):
    def __init__(self, client: MeetingMemberClient, t: Translator) -> None:
        super().__init__(
            client,
            t,
            "meeting_interrupt_speech",
            {
                "type": "object",
                "properties": {"expected_token": {"type": "string", "minLength": 1}},
                "required": ["expected_token"],
                "additionalProperties": False,
            },
        )

    async def _run(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(inputs["expected_token"], str):
            raise ValueError("expected_token must be a string")
        interruption = await self._client.interrupt_speech(**inputs)
        return asdict(interruption) if interruption is not None else None


async def create_meeting_tools(
    client: MeetingMemberClient,
    *,
    language: str = "en",
) -> dict[str, TeamTool]:
    """Build scoped publication, history, speaker reads, and Leader interruption.

    Install these instead of send_message/group_send_message in a meeting runtime.
    TeamSpec assembly, external MCP wiring, and final-reply interception are separate.
    """
    state = await client.get_state()
    if state is None:
        raise ValueError("Meeting must exist before constructing member tools")
    t = make_translator(language)
    tools = [MeetingPostMessageTool(client, t), MeetingReadHistoryTool(client, t), MeetingGetSpeakerTool(client, t)]
    if client.member_name == state.leader_member_name:
        tools.append(MeetingInterruptSpeechTool(client, t))
    return {tool.card.name: tool for tool in tools}
