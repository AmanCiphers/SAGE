"""Command construction and injection defences for pc_control.

The harness version interpolated targets into shell strings, so a quote in a
model-supplied target escaped the quoting. These tests pin the shell-free
behaviour so that regression is loud.
"""

import pytest

from sage.tools import mac


class TestInjection:
    @pytest.mark.parametrize(
        "target",
        [
            'Safari" ; touch /tmp/pwned ; echo "',
            'Safari"; rm -rf ~; echo "',
            "$(touch /tmp/pwned)",
            "`touch /tmp/pwned`",
            "Safari | tee /tmp/pwned",
            "Safari\ntouch /tmp/pwned",
            "Safari && touch /tmp/pwned",
        ],
    )
    def test_shell_metacharacters_cannot_escape(self, target):
        with pytest.raises(ValueError):
            mac._command_for("open_app", target, None)

    def test_leading_dash_is_rejected(self):
        # Otherwise a target could be read as a flag by the target program.
        with pytest.raises(ValueError):
            mac._command_for("open_app", "-R", None)

    def test_osa_values_are_escaped(self):
        command = mac._command_for("notify", 'hi "there"', None)
        script = command[-1]

        assert script.startswith("display notification ")
        # The inner quotes are escaped, so the literal ends where we put it.
        assert '\\"there\\"' in script
        assert script.count('"') % 2 == 0

    def test_backslash_is_escaped_before_quotes(self):
        # Order matters: escaping quotes first would double-escape the
        # backslashes it introduces.
        assert mac._osa_string('a\\"b') == 'a\\\\\\"b'

    def test_no_action_builds_a_shell_string(self):
        for action, target in [
            ("open_app", "Safari"),
            ("open_url", "https://example.com"),
            ("volume", 30),
            ("speak", "hello"),
            ("notify", "hi"),
            ("screenshot", None),
        ]:
            command = mac._command_for(action, target, None)
            assert isinstance(command, list)
            assert all(isinstance(part, str) for part in command)


class TestValidation:
    def test_volume_bounds(self):
        assert mac._command_for("volume", 0, None)
        assert mac._command_for("volume", 100, None)

        for bad in (-1, 101, "loud", None, 3.7):
            with pytest.raises(ValueError):
                mac._command_for("volume", bad, None)

    def test_browser_enum(self):
        assert mac._command_for("open_url", "https://x.com", "Safari")

        with pytest.raises(ValueError):
            mac._command_for("open_url", "https://x.com", "Netscape")

    def test_real_app_names_accepted(self):
        for app in ("Safari", "Google Chrome", "Visual Studio Code", "Terminal"):
            assert mac._command_for("open_app", app, None) == ["open", "-a", app]

    def test_empty_target_rejected(self):
        with pytest.raises(ValueError):
            mac._command_for("open_app", "", None)

    def test_unknown_action_raises_when_building(self):
        with pytest.raises(ValueError):
            mac._command_for("teleport", None, None)


class TestPublicApi:
    def test_unknown_action_returns_error_not_exception(self):
        result = mac.pc_control("teleport")
        assert "error" in result
        assert "unknown action" in result["error"]

    def test_rejected_action_is_audited_and_not_run(self, tmp_path, monkeypatch):
        log = tmp_path / "audit.log"
        monkeypatch.setattr(mac, "AUDIT_PATH", str(log))

        result = mac.pc_control("open_app", 'Safari"; touch /tmp/x; "')

        assert "error" in result
        assert "REJECTED" in log.read_text()

    def test_audit_never_raises(self, monkeypatch):
        monkeypatch.setattr(mac, "AUDIT_PATH", "/nonexistent-dir/audit.log")
        mac.audit("this should not raise")
