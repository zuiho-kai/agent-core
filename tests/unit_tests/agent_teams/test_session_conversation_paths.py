# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Meeting opt-in archives reuse Team/session storage; ordinary groups stay unchanged."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openjiuwen.agent_teams import paths
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.group_chat.conversation import GroupConversationLog
from openjiuwen.agent_teams.group_chat.handler import context_for, deliver_group_message
from openjiuwen.agent_teams.interaction.payload import GroupChatMessage
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from openjiuwen.agent_teams.team_workspace.dir_links import create_dir_link
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.models import PermissionLevel

TEAM = "jiuwen_team_web_1a176ef8719_dfce93b6a059"
SESSION = "web_1a176ef8719_dfce93b6a059"


@pytest.fixture
def home(tmp_path_factory):
    # Deliberately ordinary paths: older tests may use an extended-path base to
    # exercise the unchanged legacy layout on Windows.
    root = Path(str(tmp_path_factory.getbasetemp()).removeprefix("\\\\?\\")) / "session" / uuid4().hex[:8]
    root.mkdir(parents=True)
    token = paths.set_task_openjiuwen_home(root)
    try:
        yield root
    finally:
        paths.reset_task_openjiuwen_home(token)


def message(log, identifier="first", content="Start meeting"):
    return ConversationMessage(
        message_id=identifier,
        team_name=log.team_name,
        session_id=log.session_id,
        client_message_id=identifier,
        sender="user",
        sender_name="user",
        content=content,
        timestamp=1 if identifier == "first" else 2,
    )


def test_default_workspace_layout_is_unchanged(home):
    root = home / "shared"
    team_key = TEAM + "-" + hashlib.sha256(TEAM.encode()).hexdigest()
    session_key = SESSION + "-" + hashlib.sha256(SESSION.encode()).hexdigest()
    log = GroupConversationLog(TEAM, SESSION, workspace_path=root)
    assert log.history_location == "workspace"
    assert log.history_path == root / "conversations" / team_key / session_key / "history.jsonl"
    assert TeamAgentSpec(agents={"leader": DeepAgentSpec()}).conversation_history_location is None


def test_session_archive_writes_plain_windows_paths_and_reopens_without_opt_in(home):
    log = GroupConversationLog(TEAM, SESSION, history_location="session")
    assert log.path == paths.team_session_dir(TEAM, SESSION) / "conversation"
    assert len(str(log.history_path.with_suffix(".jsonl.tmp"))) < 260
    assert not str(log.history_path).startswith("\\\\?\\")
    log.sync([message(log)])
    log.sync([message(log)])
    reopened = GroupConversationLog(TEAM, SESSION)
    assert reopened.history_location == "session"
    assert reopened._read_messages()[0].content == "Start meeting"
    assert len(reopened._read_messages()) == 1
    assert json.loads(log._registration.read_text(encoding="utf-8"))["history_location"] == "session"
    with pytest.raises(ValueError, match="switch back"):
        GroupConversationLog(TEAM, SESSION, history_location="workspace")


@pytest.mark.asyncio
async def test_first_group_delivery_uses_spec_opt_in_before_archiving(home):
    token = set_session_id(SESSION)
    db = TeamDatabase(DatabaseConfig(connection_string=":memory:"))
    try:
        await db.initialize()
        await db.team.create_team(TEAM, "Meeting", "leader")
        await db.member.create_member("alice", TEAM, "Alice", "{}", "ready", role="teammate")
        backend = TeamBackend(TEAM, "leader", True, db, SimpleNamespace(publish=AsyncMock()))
        backend.group_chat_spec = TeamAgentSpec(
            team_name=TEAM,
            agents={"leader": DeepAgentSpec()},
            conversation_history_location="session",
        )
        backend.bind_group_session(SESSION)
        payload = GroupChatMessage("Start meeting", "start", mentions=["alice"])
        result = await deliver_group_message(backend, payload)
        assert result.ok, result.reason
        archive = await backend.group_conversation()
        assert archive.history_location == "session"
        assert archive.history_path.read_text(encoding="utf-8")
        retry = await deliver_group_message(backend, payload)
        assert retry.ok and retry.data["duplicate"]
        await db.message.create_message(
            "private", TEAM, "user", "Private discussion", to_member_name="alice", inline_content=True
        )
        metadata = dict(type="group_chat", client_message_id="foreign", sender_name="user", mentions=[], attachments=[])
        await db.message.create_message(
            "foreign-team", "other-team", "user", "Other team", broadcast=True, inline_content=True, meta=metadata
        )
        other_session = set_session_id("other-session")
        try:
            await db.create_cur_session_tables()
            await db.message.create_message(
                "foreign-session", TEAM, "user", "Other session", broadcast=True, inline_content=True, meta=metadata
            )
        finally:
            reset_session_id(other_session)
        # Framework excerpts still use the existing DB contract, even when a
        # member's file-tool policy denies reading the full external archive.
        policy = PermissionEngine(
            {"enabled": True, "external_directory": {"*": "deny"}}, workspace_root=paths.team_workspace_dir(TEAM)
        )
        level, _ = policy.evaluate_global_policy_directly("read_file", {"file_path": str(archive.history_path)})
        assert level == PermissionLevel.DENY
        trigger = await db.message.get_message(result.data["message"]["message_id"])
        context = await context_for(backend, "alice", trigger)
        assert "Start meeting" in context and str(archive.history_path) in context
        assert all(text not in context for text in ("Private discussion", "Other team", "Other session"))
    finally:
        await db.close()
        reset_session_id(token)


def test_session_archive_migrates_old_workspace_history_and_cleans_selected_scope(home):
    root = home / "shared"
    log = GroupConversationLog(TEAM, SESSION, workspace_path=root, history_location="session")
    old = log._workspace_archive() / "history.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text(message(log).model_dump_json() + "\n", encoding="utf-8")
    log._registry_dir.mkdir(parents=True, exist_ok=True)
    log._registration.write_text(
        json.dumps(
            {
                "team_name": TEAM,
                "session_id": SESSION,
                "workspace_path": str(root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    assert log._read_messages()[0].content == "Start meeting"
    log.sync([message(log, "second", "Follow-up")])
    assert [row.content for row in log._read_messages()] == ["Start meeting", "Follow-up"]
    assert old.exists()
    other = GroupConversationLog(TEAM, "other", history_location="session")
    other.sync([message(other, content="Keep this")])
    GroupConversationLog.delete_registered(TEAM, SESSION)
    assert not log.history_path.exists() and not old.exists()
    assert other._read_messages()[0].content == "Keep this"


def test_sanitized_session_aliases_cannot_mix_history(home):
    first = GroupConversationLog("team", "session/a", history_location="session")
    second = GroupConversationLog("team", "session_a", history_location="session")
    first.sync([])
    with pytest.raises(ValueError, match="different scope"):
        second.sync([message(second)])


def test_spec_serializes_opt_in_and_rejects_unknown_location():
    spec = TeamAgentSpec(agents={"leader": DeepAgentSpec()}, conversation_history_location="session")
    assert TeamAgentSpec.model_validate_json(spec.model_dump_json()).conversation_history_location == "session"
    with pytest.raises(ValueError):
        TeamAgentSpec(agents={"leader": DeepAgentSpec()}, conversation_history_location="unknown")


def test_existing_workspace_writer_cannot_overwrite_session_registration(home):
    old = GroupConversationLog("team", "session")
    upgraded = GroupConversationLog("team", "session", history_location="session")
    upgraded.sync([message(upgraded)])
    with pytest.raises(ValueError, match="reopen"):
        old.sync([])
    assert GroupConversationLog("team", "session").history_location == "session"


@pytest.mark.parametrize("action", ["ask", "deny"])
def test_session_storage_does_not_grant_file_permissions(home, action):
    workspace = home / "shared"
    engine = PermissionEngine(
        {"enabled": True, "external_directory": {"*": action}},
        workspace_root=workspace,
    )
    log = GroupConversationLog("team", "session", workspace_path=workspace, history_location="session")
    log.sync([message(log)])
    permission, rule = engine.evaluate_global_policy_directly("read_file", {"file_path": str(log.history_path)})
    assert permission == PermissionLevel(action)
    assert "external_directory" in rule
    assert engine.trusted_dirs == []


def test_concurrent_session_writers_keep_all_messages(home):
    logs = [GroupConversationLog("team", "session", history_location="session") for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(copy_context().run, log.sync, [message(log, str(i))]) for i, log in enumerate(logs)]
        for future in futures:
            future.result()
    assert {row.message_id for row in logs[0]._read_messages()} == {"0", "1", "2", "3"}


def test_session_reopen_rejects_workspace_changes_and_foreign_records(home):
    log = GroupConversationLog("team", "session", workspace_path=home / "shared", history_location="session")
    log.sync([])
    with pytest.raises(ValueError, match="switch workspace"):
        GroupConversationLog("team", "session", workspace_path=home / "other")
    foreign = message(log).model_copy(update={"session_id": "other"})
    with pytest.raises(ValueError, match="different scope"):
        log.sync([foreign])
    log.history_path.write_text(foreign.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different scope"):
        log.sync([])


def test_failed_session_projection_reopens_from_original_history(home, monkeypatch):
    from openjiuwen.agent_teams.group_chat import conversation

    log = GroupConversationLog("team", "session", history_location="session")
    old = log._workspace_archive() / "history.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text(message(log).model_dump_json() + "\n", encoding="utf-8")
    write = conversation.atomic_write

    def fail_history(path, text):
        if path.name == "history.jsonl":
            raise OSError("projection failed")
        write(path, text)

    monkeypatch.setattr(conversation, "atomic_write", fail_history)
    with pytest.raises(OSError, match="projection failed"):
        log.sync([])
    reopened = GroupConversationLog("team", "session")
    assert reopened.history_location == "session"
    assert reopened._read_messages()[0].content == "Start meeting"
    monkeypatch.setattr(conversation, "atomic_write", write)
    reopened.sync([message(reopened, "second")])
    assert len(reopened._read_messages()) == 2


@pytest.mark.parametrize("name", ["scope.json", "history.jsonl", "scope.json.tmp", "history.jsonl.tmp"])
def test_session_files_cannot_redirect_reads_or_writes(home, name):
    log = GroupConversationLog("team", "session", history_location="session")
    log.sync([])
    target = home / "outside.txt"
    target.write_text("preserve", encoding="utf-8")
    link = log.path / name
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    with pytest.raises(ValueError, match="symlinks"):
        log.sync([message(log)])
    assert target.read_text(encoding="utf-8") == "preserve"


def test_session_directory_cannot_redirect_storage_or_cleanup(home):
    log = GroupConversationLog("team", "session", history_location="session")
    archive = log.path
    archive.parent.mkdir(parents=True)
    outside = home / "outside"
    outside.mkdir()
    sentinel = outside / "preserve.jsonl"
    sentinel.write_text("preserve", encoding="utf-8")
    create_dir_link(outside, archive)
    with pytest.raises(ValueError, match="symlinks"):
        log.sync([])
    with pytest.raises(ValueError, match="symlinks"):
        log._delete_session()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
