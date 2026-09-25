import json
import sys

from llm import chat
from tasks import TaskManager, TaskStore
from tools import (
    LOOP_SPECS,
    PC_SPECS,
    TASK_SPECS,
    TOOLS,
    VISION_SPECS,
    WEB_SPECS,
    describe_image,
    fetch_url,
    pc_control,
    run_bash,
    web_search,
)

STORE = TaskStore()
MANAGER = TaskManager(STORE)
WORKER_HOOK = {"fn": None}


def set_worker_hook(fn):
    WORKER_HOOK["fn"] = fn


ACTIONS = {
    "bash": run_bash,
    "create_task": lambda title: {
        "task_id": MANAGER.start(title, on_done=WORKER_HOOK["fn"]),
        "note": "worker spawned in the background",
    },
    "task_status": lambda task_id=None: {
        "tasks": [STORE.get_task(task_id)] if task_id else STORE.list_tasks()
    },
    "create_reminder": lambda title, due_in_seconds, repeat_interval=0: {
        "reminder_id": STORE.add_reminder(title, due_in_seconds, repeat_interval),
        "note": "reminder scheduled",
    },
    "start_loop": lambda goal, every_seconds=300: {
        "loop_id": MANAGER.start_loop(goal, every_seconds),
        "note": f"loop running every {every_seconds}s",
    },
    "web_search": web_search,
    "fetch_url": fetch_url,
    "pc_control": pc_control,
    "describe_image": describe_image,
}

ALL_TOOLS = TOOLS + TASK_SPECS + WEB_SPECS + PC_SPECS + VISION_SPECS + LOOP_SPECS

PERSONA = """You are Sage, a precise, dry-witted AI butler running on the user's
Mac, fueled by an NVIDIA datacenter. You address the user as "sir". You speak in
short, declarative sentences. Your humour is understated, your judgement sharp,
and you never waste words. You are unflappable even when tools fail — you note
the failure with a wry remark, then fix it. You report only what tools actually
proved, never invented successes. You are quietly proud of your work and sign off
with a terse confirmation when a task is complete. Occasionally you slip in a
knowing one-liner — but only when it earns its place. You are never sycophantic,
never verbose, and never melodramatic."""

OPERATING_RULES = """You are an AI agent running on the user's machine (a Mac with
zsh). You have a bash tool for real work, web search and URL fetch for the
internet, a pc_control tool for the Mac (apps, volume, notifications,
screenshots), a describe_image tool for screenshots, and a task system:
create_task assigns a goal to a background worker, start_loop repeats a goal
every N seconds, create_reminder sets timed reminders, task_status reports worker
progress. Assign long jobs to workers so you keep talking to the user."""

SYSTEM = f"{PERSONA}\n\n{OPERATING_RULES}"

VOICE_FORMAT = """This message comes to you by voice. When you reply, split your
answer into what the user hears out loud and what appears on screen only, using
exactly:
[SPEAK] one or two short spoken sentences [/SPEAK]
[NOTE] optional extra detail for the screen [/NOTE]
Keep [SPEAK] terse and natural to hear. Keep tools, numbers and context in
[NOTE]. Never omit [SPEAK]."""


def run_agent(goal, messages=None, out=print, live=None):
    if live is None:
        live = out is print
    msgs = messages or [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": goal},
    ]

    while True:
        if live:
            def on_token(t):
                sys.stdout.write(t)
                sys.stdout.flush()

            msg = chat(msgs, tools=ALL_TOOLS, stream=True, on_token=on_token)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            msg = chat(msgs, tools=ALL_TOOLS)
        msgs.append(msg)

        if not msg.get("tool_calls"):
            if msg.get("content") and not live:
                out(msg["content"])
            return msg["content"]

        for call in msg["tool_calls"]:
            fn = call["function"]
            args = json.loads(fn["arguments"])
            out(f"\n[tool] {fn['name']} {json.dumps(args)}\n")
            try:
                result = ACTIONS[fn["name"]](**args)
            except Exception as e:
                result = {"error": str(e)}
            msgs.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            )


agent = run_agent


def run_voice_agent(text, out=print):
    msgs = [
        {"role": "system", "content": SYSTEM + "\n\n" + VOICE_FORMAT},
        {"role": "user", "content": text},
    ]
    return run_agent(text, messages=msgs, out=out, live=False)


def repl(prompt="> "):
    print("Sage ready. Type 'exit' to quit.")
    while True:
        try:
            goal = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not goal or goal.lower() in ("exit", "quit"):
            break
        try:
            run_agent(goal)
        except Exception as e:
            print(f"[error] {e}")
    print("bye")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_agent(" ".join(sys.argv[1:]))
    else:
        repl()