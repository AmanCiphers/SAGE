"""Mac desktop control.

Every action runs without a shell. The harness version built commands by string
interpolation (``f'open -a "{target}"'``) and passed them to ``shell=True``, so
a quote inside ``target`` closed the quoting and let the rest run as a command.
Since ``target`` is model-generated and web results reach the model, that made
injected page content able to steer arbitrary execution.

Arguments are passed as argv instead, and the few values that must be embedded
in an AppleScript literal are escaped for AppleScript, not for a shell.
"""

import os
import re
import subprocess
import tempfile
import time

BROWSERS = ("Safari", "Chrome", "Edge", "Firefox")

_ACTIONS = (
    "open_app",
    "open_url",
    "volume",
    "mute",
    "unmute",
    "notify",
    "speak",
    "sleep",
    "lock",
    "screenshot",
    "type_text",
    "press_key",
    "read_window",
    "frontmost_app",
)

# Keys worth sending by name. Anything else would have to be typed literally,
# and a wrong name silently does nothing, so the list stays explicit.
_KEYS = {
    "return": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "escape": 53,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "cmd+a": 0,
    "cmd+v": 9,
    "cmd+c": 8,
    "cmd+w": 13,
    "cmd+q": 12,
    "cmd+t": 17,
    "cmd+n": 45,
}

AUDIT_PATH = os.environ.get("SAGE_AUDIT_LOG", os.path.join(tempfile.gettempdir(), "sage_tool_audit.log"))

# Terminal scrollback can be enormous, and the whole thing lands in the model's
# context.
MAX_OUTPUT_CHARS = 4000


def audit(entry):
    """Append one line per tool call. Never raises into the caller's path."""
    try:
        with open(AUDIT_PATH, "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {entry}\n")
    except OSError:
        pass


def _osa(script):
    return ["osascript", "-e", script]


def _osa_string(value):
    """Escape a value for embedding inside an AppleScript double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _plain(value, field, limit=2048):
    """Reject values that would be read as an option by the target program."""
    value = str(value or "").strip()

    if not value:
        raise ValueError(f"{field} is required")

    if value.startswith("-"):
        raise ValueError(f"{field} may not start with '-'")

    if len(value) > limit:
        raise ValueError(f"{field} is too long")

    return value


def _command_for(action, target, browser):
    if action == "open_app":
        app = _plain(target, "target", 128)

        if not re.fullmatch(r"[A-Za-z0-9 ._]+", app):
            raise ValueError(f"unsupported app name: {app!r}")

        return ["open", "-a", app]

    if action == "open_url":
        url = _plain(target, "target")

        if browser:
            if browser not in BROWSERS:
                raise ValueError(f"browser must be one of {', '.join(BROWSERS)}")

            return ["open", "-a", browser, url]

        return ["open", url]

    if action == "volume":
        # Reject bools (True is an int) and fractions, so a model sending
        # volume=true or 3.7 gets told rather than silently rounded.
        if isinstance(target, bool):
            raise ValueError("volume must be an integer 0-100")

        if isinstance(target, float):
            if not target.is_integer():
                raise ValueError("volume must be a whole number 0-100")
            target = int(target)

        try:
            level = int(str(target).strip())
        except (TypeError, ValueError):
            raise ValueError("volume must be an integer 0-100")

        if not 0 <= level <= 100:
            raise ValueError("volume must be between 0 and 100")

        return _osa(f"set volume output volume {level}")

    if action == "mute":
        return _osa('set volume output muted true')

    if action == "unmute":
        return _osa('set volume output muted false')

    if action == "notify":
        return _osa(
            f'display notification "{_osa_string(str(target or ""))}" with title "Sage"'
        )

    if action == "speak":
        return _osa(f'say "{_osa_string(str(target or ""))}"')

    if action == "sleep":
        return ["pmset", "sleepnow"]

    if action == "lock":
        return _osa('tell application "System Events" to sleep')

    if action == "screenshot":
        path = _plain(target, "target") if target else os.path.join(tempfile.gettempdir(), "sage_screenshot.png")
        return ["screencapture", "-x", path]

    if action == "frontmost_app":
        return _osa(
            'tell application "System Events" to return name of first application '
            "process whose frontmost is true"
        )

    if action == "type_text":
        # Keystroke simulation, so this needs Accessibility permission for the
        # terminal process. The alternative -- `do script` in Terminal -- avoids
        # that but only works for Terminal, and cannot type into anything else.
        text = str(target or "")
        if not text:
            raise ValueError("target is required: the text to type")
        if len(text) > 4096:
            raise ValueError("target is too long")

        script = f'tell application "System Events" to keystroke "{_osa_string(text)}"'
        return _osa(script)

    if action == "press_key":
        key = str(target or "").strip().lower()
        if key not in _KEYS:
            raise ValueError(f"key must be one of {', '.join(sorted(_KEYS))}")

        return _osa(f'tell application "System Events" to key code {_KEYS[key]}')

    if action == "read_window":
        # Read what the frontmost window is showing, so a typed command's output
        # can be checked rather than assumed. Terminal is scripted through its
        # own dictionary because System Events exposes only a window reference,
        # not the text inside it.
        app = str(target or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._]*", app):
            raise ValueError(f"unsupported app name: {app!r}")

        if app and app.lower() != "terminal":
            raise ValueError(
                f"read_window can only read Terminal, not {app!r}; "
                "use bash or a screenshot for other apps"
            )

        return _osa(
            'tell application "Terminal" to return contents of selected tab of front window'
        )

    raise ValueError(f"unknown action '{action}'")


def pc_control(action, target=None, browser=None):
    """Run one Mac control action and report exactly what happened."""
    if action not in _ACTIONS:
        return {"error": f"unknown action '{action}'; expected one of {', '.join(_ACTIONS)}"}

    try:
        command = _command_for(action, target, browser)
    except ValueError as error:
        audit(f"pc_control REJECTED action={action} target={target!r} reason={error}")
        return {"error": str(error)}

    audit(f"pc_control {action} target={target!r} browser={browser!r}")

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"action": action, "ok": False, "error": "timed out after 60s"}

    # AppleScript answers on stdout. Without this, read_window and speak
    # returned an empty result and the caller could not tell success from a
    # silent no-op.
    output = (proc.stdout or "").strip()

    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"

    stderr = proc.stderr.strip()[:500]

    # osascript reports a missing Accessibility grant as a generic failure. Left
    # raw, the model retries the same call and then tells the user the action is
    # impossible, which is the part that is actually fixable.
    if "not allowed to send keystrokes" in stderr or "(-1002)" in stderr or "1002" in stderr:
        return {
            "action": action,
            "ok": False,
            "exit_code": proc.returncode,
            "output": output,
            "error": (
                "macOS has not granted this process Accessibility permission, so "
                "keystrokes cannot be sent. Grant it in System Settings > Privacy & "
                "Security > Accessibility for the app running SAGE (Terminal or your "
                "terminal emulator). To run a command, use the bash tool instead -- it "
                "needs no permission."
            ),
        }

    return {
        "action": action,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": output,
        "stderr": stderr,
    }
