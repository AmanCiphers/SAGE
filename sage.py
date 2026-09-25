import faulthandler
import os

faulthandler.enable()

from agent import MANAGER, STORE, set_worker_hook, run_agent
from channels import build_channels, notify, start_channels
from tasks import Scheduler

CHANNELS = build_channels()
start_channels(CHANNELS)


def on_worker_done(task, status):
    notify(CHANNELS, f"[worker] '{task['title']}' -> {status}")


def on_fire(reminder):
    if (reminder["context"] or "").startswith("LOOP:"):
        MANAGER.start(reminder["context"][5:], on_done=on_worker_done)
    else:
        notify(CHANNELS, f"[reminder] {reminder['title']}")


set_worker_hook(on_worker_done)
scheduler = Scheduler(STORE, on_fire=on_fire)
scheduler.start()


def route_message(text):
    run_agent(text, out=lambda reply: notify(CHANNELS, reply))


if os.environ.get("SAGE_VOICE"):
    from agent import run_voice_agent
    from voice import parse_speak_detail, prewarm, speak, start_voice

    print(f"[voice] STT engine: {prewarm()}")

    def voice_route(text):
        print(f"\n[voice] {text}")
        try:
            reply = run_voice_agent(text, out=lambda r: print(r))
            spoken, detail = parse_speak_detail(reply)
        except Exception as e:
            spoken, detail = f"[error] {e}", ""
        if spoken:
            speak(spoken)
        if detail and detail != spoken:
            print(f"[note] {detail}")

    start_voice(voice_route)
    print("[voice] enabled")

for c in CHANNELS:
    print(f"[channel] {type(c).__name__}")

print("Sage online, sir. Ctrl-C to dismiss me.\n")
while True:
    try:
        goal = input("> ").strip()
    except (KeyboardInterrupt, EOFError):
        break
    if not goal or goal.lower() in ("exit", "quit"):
        continue
    try:
        route_message(goal)
    except Exception as e:
        print(f"[error] {e}")

print("Sage going to sleep. State persists in sage.db.")