# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value objects for persisted meeting speaking coordination."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True, slots=True)
class MeetingRoute:
    """Original Team publication destination, retained until dequeue."""

    kind: Literal["direct", "multicast", "broadcast", "group_chat"] = "group_chat"
    recipients: tuple[str, ...] = ()
    protocol: str = "plain"

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipients", tuple(self.recipients))


@dataclass(frozen=True, slots=True)
class MeetingScope:
    """Explicit meeting identity, independent of the current session context."""

    session_id: str
    team_name: str
    meeting_id: str

    def __post_init__(self) -> None:
        if not all((self.session_id.strip(), self.team_name.strip(), self.meeting_id.strip())):
            raise ValueError("Meeting scope identifiers must be non-empty")


class MeetingStatus(StrEnum):
    """Team capability lifecycle; disabling preserves the meeting identity."""

    ACTIVE = "active"
    DISABLED = "disabled"
    CLOSED = "closed"


class SpeechRequestStatus(StrEnum):
    """Database coordination states; publication/audio phases belong to the coordinator."""

    QUEUED = "queued"
    GRANTED = "granted"
    PUBLISHING = "publishing"
    TEXT_ONLY = "text_only"
    FLUSHING = "flushing"
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
    status: MeetingStatus | None = None

    def __post_init__(self) -> None:
        # Keep the existing constructor/closed observation compatible. The
        # explicit lifecycle is authoritative when supplied by the coordinator.
        status = MeetingStatus(self.status) if self.status is not None else (
            MeetingStatus.CLOSED if self.closed else MeetingStatus.ACTIVE
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "closed", status == MeetingStatus.CLOSED)

    @property
    def enabled(self) -> bool:
        return self.status == MeetingStatus.ACTIVE

    @property
    def flushing(self) -> bool:
        """Compatibility observation for legacy disable-time flushes."""
        return False


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
    route: MeetingRoute = MeetingRoute()
    source_message_ids: tuple[str, ...] = ()
    messages: tuple["SpeechSource", ...] = ()


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
