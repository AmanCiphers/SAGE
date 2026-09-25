import faulthandler
import os
import threading
import time
from typing import Optional

faulthandler.enable()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import voice
from agent import MANAGER, STORE, run_agent, run_voice_agent, set_worker_hook
from channels import build_channels, notify, start_channels
from tasks import Scheduler

app = FastAPI(title="Sage Control API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CHANNELS = build_channels()
start_channels(CHANNELS)
STARTED = time.time()

LLM_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")


def on_worker_done(task, status):
    notify(CHANNELS, f"[worker] '{task['title']}' -> {status}")
    if status == "done":
        voice.SCENES.play("task_done")


def on_fire(reminder):
    if (reminder["context"] or "").startswith("LOOP:"):
        MANAGER.start(reminder["context"][5:], on_done=on_worker_done)
    else:
        notify(CHANNELS, f"[reminder] {reminder['title']}")
        voice.SCENES.play("reminder")


set_worker_hook(on_worker_done)
Scheduler(STORE, on_fire=on_fire).start()


class VoiceToggleBody(BaseModel):
    on: Optional[bool] = None


voice_loop = None
voice_thread = None
voice_events = []
voice_seq = 0
voice_lock = threading.Lock()


def _voice_sink(kind, text=""):
    global voice_seq
    with voice_lock:
        voice_seq += 1
        voice_events.append({"id": voice_seq, "kind": kind, "text": text})
        del voice_events[:-200]


def on_voice_command(text):
    try:
        reply = run_voice_agent(text, out=lambda _r: None)
        spoken, detail = voice.parse_speak_detail(reply)
        if spoken:
            _voice_sink("speaking", spoken)
            voice.speak(spoken)
        if detail:
            _voice_sink("detail", detail)
    except Exception as e:
        _voice_sink("tool", f"[voice] {e}")


@app.post("/voice/toggle")
def voice_toggle(body: VoiceToggleBody):
    global voice_loop
    if voice_loop and voice_loop.is_alive():
        body.on = body.on if body.on is not None else False
        if not body.on:
            voice_loop.stop()
            _voice_sink("state", "off")
            return {"state": "off"}
    elif body.on is None or body.on:
        voice_loop = voice.start_voice(on_voice_command, sink=_voice_sink)
        if voice_loop:
            _voice_sink("state", "starting")
        return {"state": voice_loop.state if voice_loop else "error"}
    return {"state": voice_loop.state if voice_loop else "off"}


@app.get("/voice/state")
def voice_state():
    with voice_lock:
        thr = (
            voice_loop.spot.threshold
            if voice_loop and voice_loop.spot and voice_loop.spot.ok
            else None
        )
        return {
            "state": voice_loop.state if voice_loop else "off",
            "model": voice.WAKE_MODEL,
            "tts_url": voice.CHATTERBOX_URL,
            "wake_threshold": thr,
        }


@app.get("/voice/events")
def voice_events_list(after: int = 0):
    with voice_lock:
        return {
            "events": [
                e for e in voice_events
                if e["id"] > after and e["kind"] in ("state", "heard", "speaking", "detail", "tool", "scene")
            ][-50:]
        }


class SceneBody(BaseModel):
    key: str
    force: bool = False


@app.get("/voice/scenarios")
def voice_scenarios():
    return {"scenarios": voice.SCENES.list()}


@app.post("/voice/scenario")
def voice_scenario(body: SceneBody):
    if body.key not in voice.SCENES.config:
        return {"played": False, "error": f"unknown scenario '{body.key}'"}
    played = voice.SCENES.play(body.key, force=body.force)
    _voice_sink("scene", f"played '{body.key}'" if played else f"suppressed '{body.key}' (cooldown)")
    return {"played": played, "key": body.key}


class ChatBody(BaseModel):
    message: str


class ReminderBody(BaseModel):
    title: str
    due_in_seconds: int
    repeat_interval: int = 0


@app.post("/chat")
def chat(body: ChatBody):
    events = []
    reply = run_agent(body.message, out=events.append)
    return {"reply": reply, "events": events}


@app.get("/tasks")
def tasks():
    return STORE.list_tasks()


@app.get("/reminders")
def reminders():
    return STORE.list_reminders()


@app.post("/reminders")
def add_reminder(body: ReminderBody):
    rid = STORE.add_reminder(body.title, body.due_in_seconds, body.repeat_interval)
    return {"reminder_id": rid}


@app.get("/status")
def status():
    return {
        "uptime_s": int(time.time() - STARTED),
        "channels": [type(c).__name__ for c in CHANNELS],
        "model": LLM_MODEL,
    }


if __name__ == "__main__":
    print(f"[startup] STT engine: {voice.prewarm()}")
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)