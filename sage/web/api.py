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
from sage.core.handler import Handler
from sage.core.job import JobStatus
from sage.core.llm import DEFAULT_MODEL
from sage.core.orchestrator import Orchestrator
from sage.tasks import get_runtime
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

# bash and pc_control are arbitrary execution and full desktop control. They are
# on by default so a request like "open Terminal" can be carried out instead of
# being declined; set SAGE_ALLOW_LOCAL_TOOLS=0 to lock them down. Destructive
# commands still require approval either way.
LOCAL_TOOLS = os.environ.get("SAGE_ALLOW_LOCAL_TOOLS", "1").lower() not in ("0", "false", "no", "")
registry.set_local_tools_allowed(LOCAL_TOOLS)

def _notify(reminder):
    """Surface a fired reminder to the web client, which polls for these."""
    db.add_notification("reminder", reminder["title"])


# Built early so the scheduler can deliver reminders fired later, and so the
# tool table the handler builds is the same runtime the API reports on.
task_store, task_manager, _ = get_runtime(notify=_notify)

orchestrator = Orchestrator(
    surface="web", local_tools=LOCAL_TOOLS, store=db, conversation_id=conversation_id,
    handler=Handler(
        task_store=task_store, task_manager=task_manager, chat_store=db
    ),
)


class ChatRequest(BaseModel):
    message: str


class ApprovalRequest(BaseModel):
    id: str
    approved: bool


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


def _save_assistant(text, status=None):
    """Persist the reply, but only when the turn actually answered.

    A refusal stored as an assistant message conditions every later turn: the
    model reads "I can't run shell commands" as how it has been behaving, and
    carries it into the next, unrelated question. Only real answers belong in
    the history the model is shown.
    """
    if not text:
        return

    if status is not None and status != JobStatus.COMPLETED.value:
        return

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

    _save_assistant(job.result, job.status)

    payload = {"response": job.result or "", "status": job.status.value}

    if job.status is JobStatus.COMPLETED:
        return payload

    if job.status is JobStatus.NEEDS_APPROVAL:
        payload["error"] = job.error
        payload["approval"] = job.approval.as_payload()
        return payload

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
                _save_assistant(final.get("response"), final.get("status"))
            except Exception as error:
                failure = f"could not persist reply: {type(error).__name__}: {error}"
        elif failure is None:
            failure = "pipeline ended without a result"

        done = {
            "done": True,
            "response": (final or {}).get("response") or "",
            "status": (final or {}).get("status") or JobStatus.FAILED.value,
        }

        approval = (final or {}).get("approval")

        if approval:
            done["approval"] = approval

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


@app.post("/chat/approve")
def chat_approve(request: ApprovalRequest):
    """Answer a parked approval and replay the turn.

    A refusal ends it: the parked turn is dropped and nothing is executed. A yes
    replays the same task with process-scoped ``--yolo``, which still cannot
    override HERMES hard-deny rules. When the refusal came from SAGE's own bash
    tool, only the command the user actually saw is allowed through.
    """
    from sage.core.approvals import registry as approvals

    pending = approvals.resolve(request.id)

    if pending is None:
        return {
            "status": "failed",
            "error": "no pending approval with that id (it may have expired)",
        }

    if not request.approved:
        return {
            "status": "declined",
            "response": (
                "Declined. Nothing was run."
                if pending.approved_command
                else "Declined. HERMES was not allowed to run that action."
            ),
        }

    job = orchestrator.run(
        pending.task,
        conversation=db.get_messages(pending.conversation_id or conversation_id),
        surface="web",
        yolo=True,
        approved_command=pending.approved_command,
    )

    _save_assistant(job.result, job.status)

    return {
        "status": job.status.value,
        "response": job.result or "",
        "error": job.error,
    }


@app.get("/notifications")
def notifications():
    """Unread notifications, then marked read. Polled by the console."""
    pending = db.unread_notifications()
    db.mark_notifications_read()

    return {"notifications": pending}
