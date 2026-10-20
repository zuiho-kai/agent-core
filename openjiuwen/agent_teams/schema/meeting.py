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


@dataclass(frozen=True, slots=True)
class MeetingState:
    """Detached observation of a meeting; reading it does not claim playback."""

    scope: MeetingScope
    leader_member_name: str
    addressed_members: tuple[str, ...]
    closed: bool
    owner_request_id: str | None
    lease_token: str | None
    interruption_id: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """Full queued content and summary, with a normal message reference after sending."""

    scope: MeetingScope
    request_id: str
    member_name: str
    source_message_id: str | None
    speech_text: str
    sequence: int
    status: SpeechRequestStatus
    lease_token: str | None
    created_at: int
    granted_at: int | None
    content: str | None = None
    mentions: tuple[str, ...] = ()
    message: "SpeechSource | None" = None
    message_error: str | None = None
    delivery_error: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechSource:
    """Read-only text projection; obtaining it never acknowledges delivery."""

    scope: MeetingScope
    message_id: str
    member_name: str
    content: str
    to_member_name: str | None
    broadcast: bool
    protocol: str
    timestamp: int


@dataclass(frozen=True, slots=True)
class MeetingMessage:
    """Queued utterance or public conversation message viewed through the bound scope."""

    scope: MeetingScope
    message_id: str | None
    member_name: str
    content: str
    mentions: tuple[str, ...]
    timestamp: int
    published: bool = True


@dataclass(frozen=True, slots=True)
class MeetingPostResult:
    """Queued agent utterance or immediately published human input."""

    message: MeetingMessage
    duplicate: bool
    notified_members: tuple[str, ...]
    speech_request: SpeechRequest | None
    # Admission failures raise without creating a normal message.
    audio_error: str | None = None

    @property
    def published(self) -> bool:
        return self.message.published
