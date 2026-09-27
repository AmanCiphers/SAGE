"""HERMES bridge: command construction, approval detection, session pinning.

The subprocess is faked throughout. These pin the two things that silently
cost correctness if they drift -- a subprocess launched with pipes must not be
allowed to run dangerous commands unattended, and a conversation must not
silently lose its transcript between turns.
"""

import pytest

from sage.core.approvals import ApprovalRegistry, PendingApproval
from sage.core.hermes import (
    TOOLSETS,
    ApprovalRequired,
    Hermes,
    extract_approval,
    is_approval_required,
)

APPROVAL_TEXT = (
    "I can't run that yet.\n\n"
    "This action is potentially dangerous (delete in root path). "
    "Asking the user for approval.\n\n"
    "**Target:**\n```\nrm -rf /tmp/thing\n```"
)


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestCommand:
    def test_oneshot_when_no_session(self):
        command = Hermes(binary="/bin/hermes").build_command("do a thing")
        assert command == [
            "/bin/hermes", "--toolsets", TOOLSETS, "-z", "do a thing",
        ]

    def test_resume_when_pinned(self):
        command = Hermes(binary="/bin/hermes").build_command(
            "next", session_id="20260927_190911_7412a0"
        )
        assert "--resume" in command
        assert "20260927_190911_7412a0" in command

    def test_prompt_is_always_the_z_value(self):
        # hermes' argparse takes the token after -z as the prompt and rejects a
        # bare positional, so the prompt has to ride on -z even when resuming.
        # A resumed turn built any other way fails with a usage error.
        command = Hermes(binary="/bin/hermes").build_command(
            "next", session_id="20260927_190911_7412a0"
        )
        assert command[command.index("-z") + 1] == "next"
        assert command[-1] == "next"

    def test_toolset_excludes_unguarded_execution_paths(self):
        # execute_code and delegate_task are the ways a destructive command
        # slips past hermes' own approval gate: code runs without the terminal's
        # command checks, and a delegated sub-agent loses the ask-mode context.
        toolsets = Hermes(binary="/bin/hermes").build_command("x")
        names = toolsets[toolsets.index("--toolsets") + 1].split(",")
        assert "execute_code" not in names
        assert "delegate_task" not in names
        # and the tools that make it an agent are still there
        assert "terminal" in names
        assert "file" in names

    def test_resume_does_not_restore_cwd(self):
        # Otherwise a resumed session drags SAGE into its recorded directory.
        command = Hermes(binary="/bin/hermes").build_command("x", session_id="abc")
        assert "--no-restore-cwd" in command

    def test_yolo_is_process_scoped_flag(self):
        command = Hermes(binary="/bin/hermes").build_command("x", yolo=True)
        assert "--yolo" in command

    def test_model(self):
        command = Hermes(binary="/bin/hermes").build_command("x", model="gpt")
        assert command[command.index("-m") + 1] == "gpt"


class TestApprovalDetection:
    def test_detects_the_hermes_marker(self):
        assert is_approval_required(APPROVAL_TEXT)

    def test_ordinary_output_is_not_an_approval(self):
        for text in ["all done", "the directory does not exist", "I ran ls", ""]:
            assert not is_approval_required(text), text

    def test_extracts_target_and_reason(self):
        target, description = extract_approval(APPROVAL_TEXT)
        assert target == "rm -rf /tmp/thing"
        assert description == "delete in root path"

    def test_extract_tolerates_missing_pieces(self):
        assert extract_approval("just text") == ("", "")

    def test_raises_before_returning_when_not_yolo(self, monkeypatch):
        hermes = Hermes(binary="/bin/hermes")
        monkeypatch.setattr("sage.core.hermes.subprocess.run", lambda *a, **k: FakeCompleted(APPROVAL_TEXT))

        with pytest.raises(ApprovalRequired) as caught:
            hermes.run("rm -rf /tmp/thing")

        assert caught.value.target == "rm -rf /tmp/thing"

    def test_yolo_turn_returns_text_instead_of_raising(self, monkeypatch):
        hermes = Hermes(binary="/bin/hermes")
        monkeypatch.setattr(
            "sage.core.hermes.subprocess.run", lambda *a, **k: FakeCompleted("deleted it")
        )

        assert hermes.run("rm -rf /tmp/thing", yolo=True) == "deleted it"

    def test_asks_for_approval_env_is_always_set(self, monkeypatch):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return FakeCompleted("fine")

        monkeypatch.setattr("sage.core.hermes.subprocess.run", fake_run)

        Hermes(binary="/bin/hermes").run("hello")

        # Without this HERMES silently allows dangerous commands in any
        # non-interactive context, which is exactly what a subprocess is.
        assert captured["env"]["HERMES_EXEC_ASK"] == "1"

    def test_timeout_becomes_its_own_error(self, monkeypatch):
        import subprocess as sp

        def timeout(*a, **k):
            raise sp.TimeoutExpired(cmd="hermes", timeout=1)

        monkeypatch.setattr("sage.core.hermes.subprocess.run", timeout)

        from sage.core.hermes import HermesTimeout

        with pytest.raises(HermesTimeout):
            Hermes(binary="/bin/hermes", timeout=1).run("slow")

    def test_nonzero_exit_includes_stderr(self, monkeypatch):
        monkeypatch.setattr(
            "sage.core.hermes.subprocess.run",
            lambda *a, **k: FakeCompleted(stderr="boom", returncode=2),
        )

        with pytest.raises(RuntimeError, match="boom"):
            Hermes(binary="/bin/hermes").run("x")


class TestSessionList:
    def test_parses_ids_from_the_table(self, monkeypatch):
        table = (
            "Title                        Workspace          Last Active   ID\n"
            "─" * 60 + "\n"
            "Remove sage-probe-dir and    aman               just now      20260927_190911_7412a0\n"
            "The user wants me to run a   sage               2h ago        20260927_162323_8848cf\n"
        )
        monkeypatch.setattr(
            "sage.core.hermes.subprocess.run", lambda *a, **k: FakeCompleted(table)
        )

        sessions = Hermes(binary="/bin/hermes").list_sessions()

        assert sessions[0][0] == "20260927_190911_7412a0"
        assert sessions[1][0] == "20260927_162323_8848cf"

    def test_ignores_non_session_lines(self, monkeypatch):
        table = "Title Workspace Last Active ID\nnothing useful here\n"
        monkeypatch.setattr(
            "sage.core.hermes.subprocess.run", lambda *a, **k: FakeCompleted(table)
        )

        assert Hermes(binary="/bin/hermes").list_sessions() == []

    def test_listing_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            "sage.core.hermes.subprocess.run",
            lambda *a, **k: FakeCompleted("error: no sessions", returncode=1),
        )

        assert Hermes(binary="/bin/hermes").latest_session_id() is None

    def test_workspace_filter_is_passed(self, monkeypatch):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return FakeCompleted("")

        monkeypatch.setattr("sage.core.hermes.subprocess.run", fake_run)

        Hermes(binary="/bin/hermes", workspace="sage").list_sessions()

        # Without this SAGE could pin a session created outside its own repo.
        assert "--workspace" in captured["command"]
        assert "sage" in captured["command"]


class TestApprovalRegistry:
    def test_add_and_resolve_once(self):
        registry = ApprovalRegistry()
        pending = registry.add(PendingApproval(task="rm -rf /tmp/x"))

        assert registry.resolve(pending.id) is pending
        assert registry.resolve(pending.id) is None

    def test_unknown_id(self):
        assert ApprovalRegistry().resolve("nope") is None

    def test_expired_entries_are_pruned(self):
        registry = ApprovalRegistry()
        pending = registry.add(PendingApproval(task="x"))
        pending.created_at -= 10_000

        assert registry.get(pending.id) is None
