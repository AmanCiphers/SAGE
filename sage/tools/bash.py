"""Shell execution.

The harness version captured ``cwd`` and ``env`` at import time, which is wrong
for a long-running server: the working directory is wherever the process
happened to start, and the environment snapshot predates anything loaded later.
Both are resolved per call here.
"""

import os
import subprocess
import tempfile
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 51200
DEFAULT_TIMEOUT_MS = 120000
MAX_TIMEOUT_MS = 600000


def _resolve_workdir(workdir):
    if not workdir:
        return os.getcwd()

    path = Path(workdir).expanduser().resolve()

    if not path.is_dir():
        raise ValueError(f"workdir is not a directory: {workdir}")

    return str(path)


def run_bash(command, workdir=None, timeout=DEFAULT_TIMEOUT_MS):
    """Run a shell command and return its output, truncating oversized results."""
    command = str(command or "").strip()

    if not command:
        return {"error": "command is required"}

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
