# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public conversation tools with backend-bound author and team/session."""
from typing import Any

from openjiuwen.agent_teams.group_chat.handler import prepare_message
from openjiuwen.agent_teams.schema.meeting import MeetingRoute
from openjiuwen.agent_teams.tools.tool_base import TeamTool
from openjiuwen.core.common.logging import team_logger
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.harness.tools.base_tool import ToolOutput


class GroupSendMessageTool(TeamTool):
    def __init__(self, backend, t):
        super().__init__(ToolCard(id="team.group_send_message", name="group_send_message",
                                 description=t("group_send_message")))
        self.backend = backend
        self.t = t
        self.meeting_audio = True
        self.card.input_params = {"type": "object", "properties": {
            "content": {"type": "string"}, "client_message_id": {"type": "string"},
            "mentions": {"type": "array", "items": {"type": "string"}},
        }, "required": ["content", "client_message_id"], "additionalProperties": False}

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> ToolOutput:
        try:
            if getattr(self.backend, "conversation_mode", "team") != "group_chat":
                raise ValueError("group_send_message requires a group_chat runtime")
            if set(inputs) - {"content", "client_message_id", "mentions", "speech_text"}:
                raise ValueError("Only declared group-message arguments are supported")
            meeting = self.backend.meeting if self.backend.meeting_enabled else None
            if meeting is not None:
                speech_text = inputs.get("speech_text")
                if not isinstance(speech_text, str) or not speech_text.strip():
                    raise ValueError("'speech_text' is required while meeting is enabled")
                meta, _ = await prepare_message(
                    await self.backend.group_conversation(), self.backend.message_manager,
                    self.backend.member_name, inputs["content"], client_message_id=inputs["client_message_id"],
                    mentions=inputs.get("mentions", ()),
                )
                request = await meeting.enqueue_message(
                    self.backend.member_name, inputs["content"], speech_text=speech_text.strip(),
                    route=MeetingRoute(), mentions=tuple(meta["mentions"]), request_id=inputs["client_message_id"],
                )
                return ToolOutput(success=True, data={"queued": True, "request_id": request.request_id})
            result = await self.backend.append_group_message(
                self.backend.member_name, inputs["content"], client_message_id=inputs["client_message_id"],
                mentions=inputs.get("mentions", ()),
            )
            return ToolOutput(success=True, data=result.model_dump(mode="json"))
        except Exception as exc:
            team_logger.error("Could not post group message: {}", exc)
            return ToolOutput(success=False, error=str(exc))


def group_chat_prompt(spec) -> str:
    from openjiuwen.agent_teams.tools.locales import make_translator
    from openjiuwen.harness.prompts import resolve_language

    return "\n\n" + make_translator(resolve_language(spec.language))("group_role")
