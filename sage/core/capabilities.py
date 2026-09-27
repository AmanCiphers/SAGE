"""What SAGE and Hermes can actually do.

This is the single source of truth for routing. The analyzer prompt used to
carry a hand-written rule about when to delegate, which went stale the moment
either side gained a capability. It is generated from these lists instead.
"""

SAGE_CAPABILITIES = [
    ("bash", "run a single shell command, read or write files, run tests, inspect the repo"),
    ("web_search", "look something up on the web cheaply, in one call"),
    ("fetch_url", "read one specific page the user named"),
    ("pc_control", "open an app or URL, set volume, notify, speak, screenshot, lock"),
    ("describe_image", "look at a screenshot or image file"),
    ("create_task", "hand a long goal to a background worker"),
    ("task_status", "check on background work"),
    ("create_reminder", "schedule a reminder"),
    ("start_loop", "repeat a job on an interval"),
    ("cancel_loop", "stop a loop or reminder"),
]

# What still justifies paying for a heavyweight agent. Notice that ordinary
# machine-touching work is absent: SAGE has bash and desktop control now, so
# "run the tests" or "open Safari" are SAGE's to do.
# Verified against the `hermes-cli` toolset in
# ~/.hermes/hermes-agent/toolsets.py rather than assumed. The backing tools are
# named so a claim here can be re-checked if that toolset changes.
HERMES_CAPABILITIES = [
    ("planning", "work out a multi-step approach to an underspecified goal "
                 "(todo, clarify, memory)"),
    ("multi_step", "carry out several dependent steps without being told each one "
                   "(delegate_task, terminal, process, read_file, write_file, patch)"),
    ("judgment", "decide which commands or tools to use when the path is not obvious "
                 "(terminal, execute_code, search_files)"),
    ("autonomy", "keep working, diagnose failures, and iterate until something works "
                 "(terminal, process, session_search, skills)"),
    ("deep_research", "investigate an open question and synthesise a conclusion "
                      "(browser_*, web_search, web_extract, vision_analyze)"),
    ("computer_use", "drive the desktop through an interactive session "
                     "(computer_use, read_terminal, open_preview)"),
    ("scheduling", "run recurring or unattended jobs (cronjob)"),
]

SAGE_ONE_STEP = (
    "Prefer doing it yourself with a tool when the request is a single concrete step.\n"
    "If a tool above can do what was asked, call it. Do not describe manual steps for "
    "the user to do by hand, and do not say a capability is unavailable or disabled "
    "before you have tried it."
)


def _lines(capabilities):
    return "\n".join(f"- {name}: {description}" for name, description in capabilities)


def sage_prompt(surface="cli", local_tools=True):
    available = SAGE_CAPABILITIES

    if not local_tools:
        available = [c for c in SAGE_CAPABILITIES if c[0] not in ("bash", "pc_control")]

    return (
        f"You can handle these yourself:\n{_lines(available)}\n\n"
        f"{SAGE_ONE_STEP}"
    )


def hermes_prompt():
    return f"Escalate to Hermes for:\n{_lines(HERMES_CAPABILITIES)}"


def routing_rule(surface="cli", local_tools=True):
    """The delegate/description pair fed to the analyzer."""
    return (
        "true when the request needs planning, several dependent steps, judgement "
        "about which tools to use, iteration until it works, or open-ended research. "
        "false for a single concrete step you can finish with one tool call, and "
        "false for ordinary questions and explanations."
    )
