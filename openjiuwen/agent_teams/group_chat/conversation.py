# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public chat history in a shared workspace or an opt-in Team/session archive."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Literal

from openjiuwen.agent_teams.paths import (
    group_conversation_dir,
    group_conversation_registry_dir,
    group_conversation_session_dir,
    team_home,
    team_workspace_dir,
)
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from openjiuwen.agent_teams.skill.file_lock import cross_process_file_lock
from openjiuwen.agent_teams.team_workspace.frontmatter import atomic_write


class GroupConversationLog:
    """File projection of public messages; all member progress lives in the DB.

    Methods are synchronous; async callers use ``asyncio.to_thread``. A small
    registration file under the default team home locates custom workspaces
    when the runtime is offline. Existing sessions cannot switch workspace.
    """

    def __init__(
        self,
        team_name: str,
        session_id: str,
        *,
        workspace_path: str | Path | None = None,
        history_location: Literal["workspace", "session"] | None = None,
    ) -> None:
        for label, value in (("team_name", team_name), ("session_id", session_id)):
            if not isinstance(value, str) or not value.strip() or len(value) > 255:
                raise ValueError(f"{label} must be a nonempty string of at most 255 characters")
        self.team_name = team_name
        self.session_id = session_id
        self._workspace_path = Path(workspace_path).expanduser().resolve() if workspace_path is not None else None
        self._registry_dir = group_conversation_registry_dir(team_name)
        self._registration = self._registry_dir / (hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")
        if history_location not in (None, "workspace", "session"):
            raise ValueError("Conversation history location must be workspace or session")
        registration = self._registration_data()
        stored = registration.get("history_location", "workspace") if registration else "workspace"
        if stored not in ("workspace", "session"):
            raise ValueError("Invalid registered conversation history location")
        if stored == "session" and history_location == "workspace":
            raise ValueError("A session-owned conversation cannot switch back to workspace storage")
        self.history_location = history_location or stored
        # Resolve now to reject invalid identities and conflicting roots early.
        _ = self.path

    def _registration_data(self) -> dict | None:
        if self._registration.resolve() != self._registration:
            raise ValueError("Conversation registration must not follow symlinks")
        try:
            registration = json.loads(self._registration.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(registration, dict) or (registration.get("team_name"), registration.get("session_id")) != (
            self.team_name,
            self.session_id,
        ):
            raise ValueError("Conversation workspace registration has a different scope")
        return registration

    def _workspace(self) -> Path:
        registration = self._registration_data()
        if registration is None:
            return self._workspace_path or team_workspace_dir(self.team_name).resolve()
        if self.history_location == "workspace" and registration.get("history_location") == "session":
            raise ValueError("Conversation history location changed; reopen the archive")
        root = Path(registration["workspace_path"]).resolve()
        if self._workspace_path is not None and self._workspace_path != root:
            raise ValueError("An existing conversation session cannot switch workspace")
        return root

    @property
    def path(self) -> Path:
        """Absolute history directory, also usable without a live runtime."""
        workspace = self._workspace()
        if self.history_location == "workspace":
            return group_conversation_dir(self.team_name, self.session_id, workspace_path=workspace)
        path = group_conversation_session_dir(self.team_name, self.session_id)
        scope_file = path / "scope.json"
        if scope_file.resolve() != scope_file:
            raise ValueError("Conversation scope file must not follow symlinks")
        try:
            scope = json.loads(scope_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return path
        if not isinstance(scope, dict) or (scope.get("team_name"), scope.get("session_id")) != (
            self.team_name,
            self.session_id,
        ):
            raise ValueError("Conversation directory belongs to a different scope")
        return path

    def _workspace_archive(self) -> Path:
        path = group_conversation_dir(self.team_name, self.session_id, workspace_path=self._workspace())
        value = str(path)
        if sys.platform == "win32" and not value.startswith("\\\\?\\"):
            path = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
        if path.resolve() != path:
            raise ValueError("Conversation workspace archive must not follow symlinks")
        return path

    @property
    def history_path(self) -> Path:
        """Absolute path to the session history, one JSON message per line."""
        target = self.path / "history.jsonl"
        if target.resolve() != target:
            raise ValueError("Conversation history file must not follow symlinks or escape its directory")
        return target

    def sync(self, messages: list[ConversationMessage]) -> None:
        """Refresh the file from committed DB rows without dropping concurrent appends."""
        if any((m.team_name, m.session_id) != (self.team_name, self.session_id) for m in messages):
            raise ValueError("Conversation message belongs to a different scope")
        with cross_process_file_lock(self._registry_dir):
            records = {m.message_id: m for m in self._read_messages()}
            records.update((m.message_id, m) for m in messages)
            ordered = sorted(records.values(), key=lambda m: (m.timestamp, m.message_id))
            registration = dict(
                team_name=self.team_name, session_id=self.session_id, workspace_path=str(self._workspace())
            )
            # Atomic-write sidecars must obey the same path checks as payloads.
            targets = [self._registration, self.history_path]
            if self.history_location == "session":
                targets.append(self.path / "scope.json")
            if any(
                target.with_suffix(target.suffix + ".tmp").resolve() != target.with_suffix(target.suffix + ".tmp")
                for target in targets
            ):
                raise ValueError("Conversation temporary files must not follow symlinks")
            if self.history_location == "session":
                registration["history_location"] = "session"
                atomic_write(
                    self.path / "scope.json",
                    json.dumps(
                        {
                            "team_name": self.team_name,
                            "session_id": self.session_id,
                        },
                        ensure_ascii=False,
                    ),
                )
            atomic_write(self._registration, json.dumps(registration, ensure_ascii=False))
            atomic_write(
                self.history_path,
                "".join(json.dumps(m.model_dump(), ensure_ascii=False, allow_nan=False) + "\n" for m in ordered),
            )

    def _read_messages(self) -> list[ConversationMessage]:
        try:
            text = self.history_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            source = self._workspace_archive() if self.history_location == "session" else self.path
            history = source / "history.jsonl"
            if history.resolve() != history:
                raise ValueError("Conversation history file must not follow symlinks")
            try:
                text = history.read_text(encoding="utf-8")
            except FileNotFoundError:
                if any(source.glob("[0-9]*_*.json")):
                    raise ValueError(
                        "Legacy per-message history must be converted to history.jsonl before use"
                    ) from None
                return []
        records = [json.loads(line) for line in text.split("\n") if line.strip()]
        messages = []
        for record in records:
            message = ConversationMessage.model_validate(record)
            if (message.team_name, message.session_id) != (self.team_name, self.session_id):
                raise ValueError("Conversation history contains a message from a different scope")
            messages.append(message)
        return messages

    def _delete_session(self) -> None:
        path = self.path
        old = self._workspace_archive() if self.history_location == "session" else None
        if path.exists():
            shutil.rmtree(path)
        if old is not None and old.exists():
            shutil.rmtree(old)
        self._registration.unlink(missing_ok=True)

    @classmethod
    def delete_registered(cls, team_name: str, session_id: str | None = None) -> None:
        """Delete only registered group history; leave ordinary team sessions alone."""
        if not (team_home(team_name) / "conversation-workspaces").exists():
            return
        registry_dir = group_conversation_registry_dir(team_name)
        with cross_process_file_lock(registry_dir):
            for path in registry_dir.glob("*.json"):
                registration = json.loads(path.read_text(encoding="utf-8"))
                if registration["team_name"] != team_name:
                    raise ValueError("Conversation workspace registration belongs to another team")
                if session_id is not None and registration["session_id"] != session_id:
                    continue
                log = cls(team_name, registration["session_id"])
                if log._registration != path:
                    raise ValueError("Conversation workspace registration has an invalid filename")
                log._delete_session()
