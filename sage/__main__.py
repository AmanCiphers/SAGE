import argparse
import sys

from sage.core.approvals import registry as approval_registry
from sage.core.database import Database
from sage.core.job import JobStatus
from sage.core.orchestrator import Orchestrator
from sage.tools import registry

DEFAULT_PROMPT = "Who are you and what can you do?"


def _ask_yes_no(question):
    while True:
        answer = input(f"{question} [y/N] ").strip().lower()

        if answer in ("y", "yes"):
            return True

        if answer in ("", "n", "no"):
            return False


def _resolve_approval(orchestrator, job):
    """Prompt for a parked approval.

    Returns the finished Job when the user allowed it and the turn was
    replayed, or None when they declined.
    """
    pending = job.approval
    source = "HERMES" if not pending.approved_command else "SAGE"

    print()
    print(f"[{source}] needs your approval to continue:")
    print(f"  reason: {pending.description or 'dangerous action'}")

    if pending.target:
        print(f"  target: {pending.target}")

    if not _ask_yes_no("Allow it?"):
        print(f"[SAGE] Declined. {source} did not run that action.")
        return None

    if pending.approved_command:
        print("[SAGE] Approved. Re-running that one command...")
    else:
        print("[SAGE] Approved. Re-running with approvals bypassed for this turn...")

    return orchestrator.run(
        pending.task,
        surface="cli",
        yolo=True,
        approved_command=pending.approved_command,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m sage",
        description="Run a request through the SAGE agent pipeline.",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="what to ask SAGE. Quote it, or just type it unquoted.",
    )

    args = parser.parse_args(argv)
    prompt = " ".join(args.prompt).strip() or DEFAULT_PROMPT

    # A local CLI invocation is the user at their own machine, so the local
    # tools are on. The gate stays shut on the web surface.
    registry.set_local_tools_allowed(True)

    store = Database()
    store.initialize()

    orchestrator = Orchestrator(
        surface="cli",
        local_tools=True,
        store=store,
        conversation_id=store.get_or_create_primary_conversation(),
    )

    job = orchestrator.run(prompt, surface="cli")

    if job is not None and job.status is JobStatus.NEEDS_APPROVAL:
        replayed = _resolve_approval(orchestrator, job)

        if replayed is not None:
            job = replayed
        else:
            return 0

    print()

    if job.status is JobStatus.COMPLETED:
        print(f"[SAGE] {job.result}")
    else:
        print(f"[SAGE] Request failed: {job.error}", file=sys.stderr)

    return 0 if job.status is JobStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
