# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value objects for persisted meeting speaking coordination."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class MeetingScope:
    """Explicit meeting identity, independent of the current session context."""

    session_id: str
    team_name: str
    meeting_id: str

    def __post_init__(self) -> None:
        if not all((self.session_id.strip(), self.team_name.strip(), self.meeting_id.strip())):
            raise ValueError("Meeting scope identifiers must be non-empty")


class SpeechRequestStatus(StrEnum):
    """Database coordination states; publication/audio phases belong to the coordinator."""

    QUEUED = "queued"
    GRANTED = "granted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SpeechInterruption:
    """Persisted barrier identity and the revoked turn to stop in the application."""

    interruption_id: str
    request_id: str | None
    lease_token: str | None
