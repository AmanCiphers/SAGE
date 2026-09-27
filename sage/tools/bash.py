"""Shell execution.

The harness version captured ``cwd`` and ``env`` at import time, which is wrong
for a long-running server: the working directory is wherever the process
happened to start, and the environment snapshot predates anything loaded later.
Both are resolved per call here.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 51200
DEFAULT_TIMEOUT_MS = 120000
MAX_TIMEOUT_MS = 600000

# Commands that destroy data or the machine. These are refused before the shell
# runs and handed back as an approval request: a one-shot "delete that
# directory" is a normal thing for a user to ask, and the model picking the bash
# tool is not consent to run it. HERMES gates the same class of command on its
# side; this closes the same hole on ours.
DESTRUCTIVE_PATTERNS = (
    (r"\brm\s+(-[a-z]*[rR][a-z]*f|-[a-z]*f[a-z]*[rR])\b", "recursive delete"),
    (r"\brm\s+(-[a-z]*[rR])\b", "recursive delete"),
    # Long-form flags, and flags given as separate words: `rm --recursive`,
    # `rm -r -f`.
    (r"\brm\s+(--recursive|--force|--dir\b)", "recursive delete"),
    (r"\bfind\b[^|;]*\s-delete\b", "bulk delete via find -delete"),
    (r"\bfind\b[^|;]*\s-exec\s+rm\b", "bulk delete via find -exec rm"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^|;]*\bof=/dev/", "raw device write"),
    (r">\s*/dev/(sd|hd|nvme|disk)", "raw device write"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "power state change"),
    (r"\bkill\s+(-1|-KILL\s+1)\b", "kill every process"),
    # `chmod -R 777 /` -- the mode specifier sits between the flags and the
    # target, so match up to a bare `/` argument rather than just flags.
    (r"\bchmod\b[^|;&]*\s/(?:\s|$)", "permission change on /"),
    (r"\bchown\b[^|;&]*\s/(?:\s|$)", "ownership change on /"),
    # `truncate -s 0 file` empties a file. A plain `> file` redirect is left
    # alone: it is how most ordinary commands write their output.
    (r"\btruncate\b[^|;&]*\s-s\s*0\b", "truncate to zero bytes"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
)


def destructive_reason(command):
    """Why ``command`` is destructive, or ``None`` when it looks ordinary."""
    text = str(command or "")

    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return reason

    return None


def _destructively_approved(command, approved):
    """True when ``command`` is the one the user just approved.

    Approval is for a single command, not for the rest of the turn. The
    approved text is cut out and whatever is left is checked again, so
    ``rm -rf /tmp/x && ls`` runs once confirmed while ``rm -rf /tmp/x && rm -rf
    /home`` parks again: the second delete was never shown to anyone.
    """
    if not approved:
        return False

    approved = str(approved).strip()

    if not approved or approved not in str(command or ""):
        return False

    remainder = str(command).replace(approved, " ")

    return destructive_reason(remainder) is None


def _resolve_workdir(workdir):
    if not workdir:
        return os.getcwd()

    path = Path(workdir).expanduser().resolve()

    if not path.is_dir():
        raise ValueError(f"workdir is not a directory: {workdir}")

    return str(path)


def run_bash(command, workdir=None, timeout=DEFAULT_TIMEOUT_MS, approved_command=None):
    """Run a shell command and return its output, truncating oversized results.

    Destructive commands are not run. They come back as an approval request so
    the caller can ask the user; pass the approved text back as
    ``approved_command`` to run that one command.
    """
    command = str(command or "").strip()

    if not command:
        return {"error": "command is required"}

    reason = destructive_reason(command)

    if reason and not _destructively_approved(command, approved_command):
        return {
            "error": f"approval required: {reason} needs your confirmation",
            "approval_required": True,
            "reason": reason,
            "command": command,
        }

    try:
        cwd = _resolve_workdir(workdir)
    except (OSError, ValueError) as error:
        return {"error": str(error)}

    try:
        timeout_ms = min(int(timeout or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_TIMEOUT_MS

    timed_out = False

    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/zsh",
            cwd=cwd,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
        output = proc.stdout or ""
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        partial = error.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        output = f"{partial}\n[timed out after {timeout_ms}ms]"
        exit_code = -1

    lines = output.splitlines()
    truncated = len(lines) > MAX_LINES or len(output) > MAX_BYTES
    output_file = None

    if truncated:
        directory = Path(tempfile.gettempdir()) / "sage"
        directory.mkdir(parents=True, exist_ok=True)
        output_file = str(directory / f"bash_{abs(hash(command))}.txt")
        Path(output_file).write_text(output)
        output = (
            f"{chr(10).join(lines[:MAX_LINES])}\n"
            f"... [truncated: {len(lines)} lines / {len(output)} bytes] "
            f"full output: {output_file}"
        )

    return {
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
        "output": output,
        "output_file": output_file,
    }
