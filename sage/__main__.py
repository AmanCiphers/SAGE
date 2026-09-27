import argparse
import sys

from sage.core.job import JobStatus
from sage.core.orchestrator import Orchestrator
from sage.tools import registry

DEFAULT_PROMPT = "Who are you and what can you do?"


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

    job = Orchestrator().run(prompt, surface="cli")

    print()

    if job.status is JobStatus.COMPLETED:
        print(f"[SAGE] {job.result}")
    else:
        print(f"[SAGE] Request failed: {job.error}", file=sys.stderr)

    return 0 if job.status is JobStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
