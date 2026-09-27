import json
import os
import queue
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sage.core.database import Database
from sage.core.job import JobStatus
from sage.core.llm import DEFAULT_MODEL
from sage.core.orchestrator import Orchestrator
from sage.tools import registry

# The pipeline reports progress with print(). Without this, stdout is block
# buffered and the log trail arrives in bursts that misrepresent where a
# request actually is.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

# The pipeline is silent for seconds at a time (analyzer call, web fetch,
# model time-to-first-token). Proxies and browsers can reset an idle
# response, so emit an SSE comment whenever nothing else is flowing.
KEEPALIVE_SECONDS = float(os.environ.get("SAGE_KEEPALIVE", "10"))

_SENTINEL = object()

app = FastAPI(title="SAGE")

app.mount("/static", StaticFiles(directory="sage/web/static"), name="static")

db = Database()
db.initialize()

conversation_id = db.get_or_create_primary_conversation()

# bash and pc_control are arbitrary execution and full desktop control. They
# stay off on the web surface unless explicitly enabled, so an exposed port is
# not an unauthenticated remote shell.
LOCAL_TOOLS = os.environ.get("SAGE_ALLOW_LOCAL_TOOLS", "").lower() in ("1", "true", "yes")
registry.set_local_tools_allowed(LOCAL_TOOLS)

orchestrator = Orchestrator(surface="web", local_tools=LOCAL_TOOLS)


class ChatRequest(BaseModel):
    message: str


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _turn(request):
    """Persist the user turn and return prior history for the model.

    The handler appends the new user message itself, so the history passed to
    it must exclude it or the turn gets duplicated.
    """
    history = db.get_messages(conversation_id)
    db.add_message(conversation_id, "user", request.message)

    return history


def _save_assistant(text):
    if text:
        db.add_message(conversation_id, "assistant", text)


@app.get("/")
def home():
    return FileResponse("sage/web/static/index.html")


@app.get("/info")
def info():
    return {
        "model": DEFAULT_MODEL,
        "transport": "nim · https",
        "runtime": "fastapi · uvicorn",
        "store": "sqlite",
        "endpoint": "POST /chat/stream",
        "pipeline": "analyze → route → execute → verify",
        "local_tools": registry.local_tools_allowed(),
        "tools": [s["function"]["name"] for s in registry.specs_for("web")],
    }


@app.post("/chat")
def chat(request: ChatRequest):
    """Non-streaming reply. Kept for the legacy static client."""
    history = _turn(request)
    job = orchestrator.run(request.message, conversation=history, surface="web")

    _save_assistant(job.result)

    payload = {"response": job.result or "", "status": job.status.value}

    if job.status is not JobStatus.COMPLETED:
        payload["error"] = job.error

    return payload


def _pump(orchestrator, message, history, out):
    """Drive the pipeline on a worker thread.

    The SSE loop has to stay responsive while the pipeline is blocked on a
    network call, so the generator is consumed here and handed to the
    response loop through a queue.
    """
    try:
        for event in orchestrator.run_stream(message, history, surface="web"):
            out.put(event)
    except BaseException as error:
        out.put({"error": f"{type(error).__name__}: {error}"})
    finally:
        out.put(_SENTINEL)


@app.post("/chat/stream")
def chat_stream(request: ChatRequest):
    """Server-sent events over the full agent pipeline.

    Emits ``stage`` and ``delta`` frames as work happens, keepalive comments
    during silent gaps, and always terminates with a ``done`` frame so the
    client never sees a truncated stream.
    """
    history = _turn(request)

    def events():
        out = queue.Queue()

        threading.Thread(
            target=_pump,
            args=(orchestrator, request.message, history, out),
            daemon=True,
        ).start()

        final = None
        failure = None

        while True:
            try:
                event = out.get(timeout=KEEPALIVE_SECONDS)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue

            if event is _SENTINEL:
                break

            if "job" in event:
                final = event
                continue

            if event.get("error"):
                failure = event["error"]

            yield _sse(event)

        if final is not None:
            try:
                _save_assistant(final.get("response"))
            except Exception as error:
                failure = f"could not persist reply: {type(error).__name__}: {error}"
        elif failure is None:
            failure = "pipeline ended without a result"

        done = {
            "done": True,
            "response": (final or {}).get("response") or "",
            "status": (final or {}).get("status") or JobStatus.FAILED.value,
        }

        if failure:
            done["error"] = failure

        yield _sse(done)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
