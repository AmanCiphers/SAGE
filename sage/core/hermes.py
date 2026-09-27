"""Subprocess bridge to the HERMES CLI.

SAGE drives HERMES out-of-process, so two HERMES behaviours shape this module.

Sessions
    ``-z/--oneshot`` always starts a *new* session and prints no session id, so
    a conversation has to be pinned explicitly. ``--resume latest`` is not safe
    once more than one conversation exists, because it can attach the wrong
    transcript. Instead the id of the session HERMES just created is captured
    from ``hermes sessions list`` and passed back with ``--resume`` next time.

Approvals
    Outside an interactive CLI, gateway session, or ``HERMES_EXEC_ASK`` context
    HERMES *silently allows* dangerous commands rather than prompting. A
    subprocess launched with pipes is none of those, so without the env var
    below every approval is skipped. With it set, HERMES refuses the action,
    hands the request back to the model, and the process exits normally -- the
    request is visible only as text in stdout, because the pending-approval
    queue lives in the (now dead) child's memory.
"""

import os
import re
import shutil
import subprocess

DEFAULT_TIMEOUT = 900
WORKSPACE = "sage"

# Deliberately excludes execute_code, delegate_task, browser_exec, computer_use
# and cronjob from hermes' default composite. See build_command.
TOOLSETS = "terminal,file,search,web,vision,skills,todo,memory"

# Stable strings from hermes-agent tools/approval.py.
_APPROVAL_MARKERS = ("asking the user for approval", "potentially dangerous")
_TARGET_RE = re.compile(r"\*\*Target:?\*\*\s*```(?:[a-z]*\n)?(.*?)```", re.S | re.I)
_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{4,}$")


class ApprovalRequired(Exception):
    """HERMES refused an action and wants the user's consent before retrying."""

    def __init__(self, target="", description=""):
        self.target = target
        self.description = description
        super().__init__(description or "HERMES requested approval")


class HermesTimeout(Exception):
    """HERMES exceeded the wall-clock budget."""


def _hermes_binary():
    found = shutil.which("hermes")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/hermes")
    return fallback if os.path.exists(fallback) else "hermes"


def is_approval_required(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _APPROVAL_MARKERS)


def extract_approval(text):
    """Pull the blocked target and reason out of HERMES's approval message."""
    match = _TARGET_RE.search(text or "")
    target = match.group(1).strip() if match else ""

    description = ""
    reason = re.search(
        r"potentially dangerous\s*\(([^)]*)\)", text or "", re.I
    )
    if reason:
        description = reason.group(1).strip()

    return target, description


class Hermes:
    """One HERMES turn, optionally resuming a pinned session."""

    def __init__(self, binary=None, timeout=DEFAULT_TIMEOUT, workspace=WORKSPACE):
        self.binary = binary or _hermes_binary()
        self.timeout = timeout
        self.workspace = workspace

    def build_command(self, task, model=None, session_id=None, yolo=False):
        command = [self.binary]

        if model:
            command += ["-m", model]

        # A narrow toolset, not the hermes-cli composite. The composite adds
        # execute_code and delegate_task, and a sub-agent reached through
        # delegate_task does not reliably keep the ask-mode approval context, so
        # a destructive command can slip past the gate that hermes-cli itself
        # applies. Everything the agent needs to do real work is still here.
        command += ["--toolsets", TOOLSETS]

        if session_id:
            command += ["--resume", session_id]
            # Without this a resumed session cd's into its recorded directory,
            # which would drag SAGE out of the user's cwd.
            command.append("--no-restore-cwd")

        if yolo:
            command.append("--yolo")

        # The prompt is the value of -z and has to come last: hermes' argparse
        # takes the next token after -z as the prompt, and a bare positional is
        # rejected outright, resume or not.
        command += ["-z", task]

        return command

    def run(self, task, model=None, session_id=None, yolo=False):
        """Run one turn. Returns the reply text.

        Raises ApprovalRequired when HERMES wants consent, HermesTimeout when
        the budget is exhausted, and RuntimeError on a non-zero exit.
        """
        env = dict(os.environ)
        # Makes headless runs surface approvals instead of auto-allowing them.
        env["HERMES_EXEC_ASK"] = "1"

        command = self.build_command(task, model, session_id, yolo)

        print(f"[HERMES] {'resume' if session_id else 'new'} {self.workspace} :: {task[:80]}")

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise HermesTimeout(
                f"HERMES exceeded {self.timeout}s"
            ) from None

        if result.returncode != 0:
            raise RuntimeError(f"Hermes failed ({result.returncode}):\n{result.stderr.strip()}")

        text = result.stdout.strip()

        if not yolo and is_approval_required(text):
            target, description = extract_approval(text)
            raise ApprovalRequired(target, description)

        return text

    def list_sessions(self, limit=10):
        """Return [(session_id, title)] newest first for this workspace."""
        try:
            result = subprocess.run(
                [self.binary, "sessions", "list", "--workspace", self.workspace,
                 "--limit", str(limit)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []

        if result.returncode != 0:
            return []

        sessions = []

        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            candidate = parts[-1]
            if _SESSION_ID_RE.match(candidate):
                title = line[: line.rfind(candidate)].strip(" -")
                sessions.append((candidate, title))

        return sessions

    def latest_session_id(self):
        sessions = self.list_sessions(limit=1)
        return sessions[0][0] if sessions else None
