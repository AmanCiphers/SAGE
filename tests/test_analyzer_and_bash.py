"""Analyzer JSON handling, domain detection, and shell execution."""

import json
import os

import pytest

from sage.core.job_analyzer import Analyzer
from sage.tools.bash import (DESTRUCTIVE_PATTERNS, MAX_BYTES,
                              destructive_reason, run_bash)


class TestJsonExtraction:
    def parse(self, raw):
        return Analyzer._coerce(Analyzer._loads_object(raw))

    def test_plain_object(self):
        data = self.parse('{"intent":"a","action":"b","delegate":false,"tools":[]}')
        assert data["intent"] == "a"

    def test_fenced_json(self):
        raw = '```json\n{"intent":"a","action":"b","delegate":false,"tools":[]}\n```'
        assert self.parse(raw)["action"] == "b"

    def test_prose_around_json(self):
        raw = 'Sure! Here you go:\n{"intent":"a","action":"b","delegate":false,"tools":[]}\nHope that helps.'
        assert self.parse(raw)["intent"] == "a"

    @pytest.mark.parametrize("raw", ["[1, 2, 3]", "just prose", "", "no braces here"])
    def test_unusable_payloads_rejected(self, raw):
        # Whatever the model returns, the caller only ever sees ValueError.
        with pytest.raises(ValueError):
            Analyzer._loads_object(raw)

    def test_empty_object_is_valid_but_incomplete(self):
        # {} parses fine; it is _coerce that insists on the required keys.
        assert Analyzer._loads_object("{}") == {}
        with pytest.raises(ValueError, match="missing keys"):
            Analyzer._coerce({})

    def test_malformed_json_reports_where_it_broke(self):
        with pytest.raises(ValueError, match="did not parse at column"):
            Analyzer._loads_object('here you go: {"intent": "a", "action:} done')

    def test_no_object_rejected(self):
        with pytest.raises(ValueError, match="no JSON object"):
            Analyzer._loads_object("I cannot help with that")

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="expected text"):
            Analyzer._loads_object({"already": "parsed"})

    @pytest.mark.parametrize(
        "raw,expected",
        [("true", True), ("True", True), ("yes", True), ("1", True), ("false", False), ("no", False)],
    )
    def test_delegate_coercion(self, raw, expected):
        body = json.dumps({"intent": "a", "action": "b", "delegate": raw, "tools": []})
        assert self.parse(body)["delegate"] is expected

    def test_target_defaults_from_delegate(self):
        body = json.dumps({"intent": "a", "action": "b", "delegate": True, "tools": []})
        assert self.parse(body)["target"] == "hermes"

    def test_unknown_tools_are_dropped(self):
        body = json.dumps(
            {"intent": "a", "action": "b", "delegate": False, "tools": ["web_search", "rm_rf"]}
        )
        # Only implemented tools survive, so the model cannot request a tool
        # that does not exist and get a confusing runtime error.
        assert self.parse(body)["tools"] == ["web_search"]

    def test_tools_string_becomes_list(self):
        body = json.dumps({"intent": "a", "action": "b", "delegate": False, "tools": "web_search"})
        assert self.parse(body)["tools"] == ["web_search"]

    def test_missing_keys_rejected(self):
        with pytest.raises(ValueError, match="missing keys"):
            self.parse('{"intent":"a"}')

    def test_non_list_tools_rejected(self):
        body = json.dumps({"intent": "a", "action": "b", "delegate": False, "tools": {"a": 1}})
        with pytest.raises(ValueError, match="must be a list"):
            self.parse(body)

    def test_fails_closed_on_garbage(self, monkeypatch):
        # A malformed analysis must never trigger a subprocess or a network
        # call, so the fallback has to be the least-privileged path.
        class BadLLM:
            def chat(self, *a, **k):
                return "not json at all"

        analysis = Analyzer(llm=BadLLM()).analyze("please delete everything")

        assert analysis.delegate is False
        assert analysis.tools == []


class TestBash:
    def test_runs_and_reports_cwd(self, tmp_path):
        result = run_bash("pwd", workdir=str(tmp_path))
        assert result["exit_code"] == 0
        # macOS resolves /var to /private/var, so compare the resolved path.
        assert result["output"].strip() == os.path.realpath(tmp_path)
        assert result["cwd"] == str(tmp_path)
        assert result["timed_out"] is False

    def test_workdir_is_per_call_not_import_time(self, tmp_path, monkeypatch):
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()

        monkeypatch.chdir(first)
        assert run_bash("pwd")["cwd"].endswith("a")

        monkeypatch.chdir(second)
        # The harness snapshotted cwd at import, so this stayed in the first
        # directory for the life of the process.
        assert run_bash("pwd")["cwd"].endswith("b")

    def test_invalid_workdir_reports_error(self):
        assert "not a directory" in run_bash("pwd", workdir="/no/such/place")["error"]

    def test_empty_command(self):
        assert "command is required" in run_bash("  ")["error"]

    def test_timeout(self):
        result = run_bash("sleep 5", timeout=300)
        assert result["timed_out"] is True
        assert result["exit_code"] == -1

    def test_timeout_is_capped(self, tmp_path):
        result = run_bash("echo hi", workdir=str(tmp_path), timeout=10**9)
        assert result["exit_code"] == 0

    def test_stderr_is_captured(self):
        result = run_bash("echo oops 1>&2")
        assert "oops" in result["output"]

    def test_nonzero_exit_reported(self):
        assert run_bash("exit 3")["exit_code"] == 3

    def test_large_output_truncated_to_file(self, tmp_path):
        result = run_bash(
            f"for i in $(seq 1 20000); do echo line-$i; done", workdir=str(tmp_path)
        )

        assert result["truncated"] is True
        assert result["output_file"]
        assert "truncated" in result["output"]

        with open(result["output_file"]) as handle:
            assert handle.read().count("line-") > 1000

    def test_small_output_not_truncated(self):
        assert run_bash("echo small")["truncated"] is False


class TestDestructiveGate:
    """SAGE's own bash must not run destructive commands unreviewed.

    A one-shot "delete that directory" is an ordinary request, and the model
    reaching for the bash tool is not the user agreeing to it.
    """

    def test_recursive_delete_is_not_run(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("important")

        result = run_bash(f"rm -rf {victim}", workdir=str(tmp_path))

        assert result["approval_required"] is True
        assert "recursive delete" in result["reason"]
        # The whole point: nothing was removed.
        assert (victim / "keep.txt").exists()

    def test_system_and_disk_commands_are_not_run(self):
        for command in ("dd if=/dev/zero of=/dev/sda", "shutdown -h now", "mkfs.ext4 /dev/sda"):
            result = run_bash(command)
            assert result["approval_required"] is True, command

    def test_find_delete_is_not_run(self, tmp_path):
        (tmp_path / "a.log").write_text("x")
        result = run_bash(f"find {tmp_path} -name '*.log' -delete", workdir=str(tmp_path))
        assert result["approval_required"] is True
        assert (tmp_path / "a.log").exists()

    def test_ordinary_commands_still_run(self):
        assert run_bash("echo hello")["exit_code"] == 0
        assert run_bash("rm -f missing-file")["exit_code"] == 0

    def test_approved_command_runs(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()

        command = f"rm -rf {victim}"
        result = run_bash(command, workdir=str(tmp_path), approved_command=command)

        assert "approval_required" not in result
        assert not victim.exists()

    def test_approval_covers_one_command_only(self, tmp_path):
        approved = "rm -rf /tmp/sage-approved"
        widened = f"{approved} && rm -rf {tmp_path}"

        result = run_bash(widened, workdir=str(tmp_path), approved_command=approved)

        assert result["approval_required"] is True


@pytest.mark.parametrize("command,reason", [
    # recursive delete, however the flags are spelled or split
    ("rm -rf /tmp/x", "recursive delete"),
    ("rm -fr /tmp/x", "recursive delete"),
    ("rm -r -f /tmp/x", "recursive delete"),
    ("rm --recursive /tmp/x", "recursive delete"),
    ("git rm -r cached/", "recursive delete"),
    # bulk delete reached through find
    ("find /tmp -name '*.log' -delete", "bulk delete via find -delete"),
    ("find /tmp -name '*.log' -exec rm {} ;", "bulk delete via find -exec rm"),
    # machine-level damage
    ("dd if=/dev/zero of=/dev/disk0", "raw device write"),
    ("echo x > /dev/sda", "raw device write"),
    ("mkfs.ext4 /dev/sdb1", "filesystem format"),
    ("shutdown -h now", "power state change"),
    ("reboot", "power state change"),
    ("kill -1 -1", "kill every process"),
    (":(){ :|:& };:", "fork bomb"),
    # root with a mode specifier between the flags and the target
    ("chmod -R 777 /", "permission change on /"),
    ("chown -R root:root /", "ownership change on /"),
    ("truncate -s 0 /var/log/x", "truncate to zero bytes"),
])
def test_destructive_commands_are_classified(command, reason):
    assert destructive_reason(command) == reason


@pytest.mark.parametrize("command", [
    # the gate is a heuristic, and the bias is to ask rather than assume
    "rm /tmp/x", "rm -f /tmp/x", "rm -i /tmp/x",
    "ls -la /tmp", "chmod +x script.sh", "chmod 777 /etc/hosts",
    "chown me file.txt", "find /tmp -name '*.log' | head",
    "truncate -s 5M /tmp/big", "echo done", "echo x > /tmp/out.txt",
    "git status", "npm test",
])
def test_ordinary_commands_are_left_alone(command):
    assert destructive_reason(command) is None


def test_every_pattern_is_reachable():
    """Each entry needs a case above, or it is dead weight nobody notices."""
    covered = {
        "recursive delete", "bulk delete via find -delete",
        "bulk delete via find -exec rm", "raw device write",
        "filesystem format", "power state change", "kill every process",
        "permission change on /", "ownership change on /",
        "truncate to zero bytes", "fork bomb",
    }
    assert {reason for _, reason in DESTRUCTIVE_PATTERNS} <= covered
