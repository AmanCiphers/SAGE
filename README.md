# SAGE

A small assistant that does the work itself and only escalates when a request
genuinely needs an agent.

Every request goes through one pipeline:

```
analyze → route → execute → verify
```

The analyzer classifies the request. The router decides between SAGE and
HERMES. The chosen handler runs it. The verifier decides whether what came
back is actually an answer.

## Why two handlers

SAGE has its own tool loop and ten tools, so a single concrete step — run a
command, open an app, search the web, look at a screenshot, set a reminder —
never leaves the process.

HERMES is used for the work that is genuinely multi-step: planning an
underspecified goal, iterating until something works, driving a browser, or
open-ended research.

The analyzer proposes the route and a deterministic check has the last word
(`sage/core/routing.py`). It escalates on wording that implies planning,
iteration, or synthesis. It also *demotes* the narrow case where the analyzer
escalated something it had itself scoped to a single tool call, because a
false positive there routes a one-line command through a heavyweight agent.

A conversation is pinned to one HERMES session, so follow-up turns keep their
transcript instead of starting over.

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in NVIDIA_API_KEY
```

CLI:

```bash
.venv/bin/python -m sage "what am I running on"
```

Web console:

```bash
cd web && npm install && npm run build && cd ..
SAGE_API=http://127.0.0.1:8000 .venv/bin/python -m uvicorn sage.web.api:app --port 8000
```

`SAGE_API` is read at build time by `web/next.config.mjs`, so rebuild the
front end after changing it.

### Two-part answers

Every finished turn produces two things: the real response, and a short
conversational line meant to be heard.

| Surface | Real response | Spoken line |
| --- | --- | --- |
| `POST /chat` | `response` | `summary` |
| `POST /chat/stream` | `delta` frames, then `done` | a `say` frame after `done` |
| `POST /chat/approve` | `response` | `summary` |

`summary` is always present, empty when there is nothing to say. A parked
approval or a failed turn never gets one, because "that is done, sir" over
work that never ran is a lie.

The line is built for speech rather than for reading, which drives most of
`sage/core/spoken.py`: markdown, emoji, bullets, code, paths and URLs are all
stripped after generation, so a model that ignores the instruction still
produces something speakable, and it is capped at 32 words. When the answer is
too dense to speak, it points at the screen instead of reciting it. A single
verdict or one key number is still worth speaking, so those come through.

This is the field a voice mode will read. Nothing is wired to a speech engine
yet, and generating the line is best-effort: a failure leaves the real answer
untouched.

### Models

| Step | Default | Why |
| --- | --- | --- |
| Routing analyzer | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | Emits one small JSON object per turn |
| Tool loop, HERMES | `nvidia/nemotron-3-ultra-550b-a55b` | Does the actual work |

`routing.py` overrides the analyzer's delegate flag in both directions with
regex, so the analyzer proposes a route rather than deciding it. That is what
makes the cheap model safe: a wrong proposal is corrected deterministically,
and a provider failure degrades to direct handling instead of failing the
turn. Override with `SAGE_ANALYZER_MODEL`.

Note that most models in the NVIDIA model list have no deployment behind them
and return 404 on `/chat/completions`. Only three of 28 probed were live.

Tests:

```bash
.venv/bin/python -m pytest
```

The suite is fully mocked — no NVIDIA, Tavily, or HERMES calls.

## Configuration

| Variable | Required | Effect |
| --- | --- | --- |
| `NVIDIA_API_KEY` | yes | Model access for the analyzer, tool loop, and vision |
| `TAVILY_API_KEY` | no | Search provider. Without it, search falls back to DuckDuckGo |
| `SAGE_TAVILY_DEPTH` | no | `advanced` returns page text inline. Default `basic` |
| `SAGE_ALLOW_LOCAL_TOOLS` | no | `bash` and `pc_control` are on by default. Set `0` to disable them on the web surface |
| `SAGE_AUDIT_LOG` | no | Where `pc_control` writes its audit trail |
| `SAGE_KEEPALIVE` | no | Seconds between SSE keepalive comments |

`.env` is loaded on import and is git-ignored.


## Tools

`bash`, `web_search`, `fetch_url`, `pc_control`, `describe_image`, `chat_history`,
`create_task`, `task_status`, `create_reminder`, `start_loop`, `cancel_loop`.

`bash` and `pc_control` are arbitrary execution and full desktop control, so
they are on by default everywhere, so a request can actually be carried out;
set `SAGE_ALLOW_LOCAL_TOOLS=0` to lock them down, and while they are off a web
request is never delegated to HERMES either. `pc_control` builds argv lists and escapes AppleScript literals;
it never interpolates a target into a shell string. It can `open_app`,
`type_text`, `press_key`, `read_window` (Terminal scrollback) and report the
`frontmost_app`, so an app can be driven and its output checked. Keystroke
actions need macOS Accessibility permission for whatever app runs SAGE; without
it you get an error saying so rather than a silent no-op. `read_window` is
Terminal-only, because System Events exposes no way to read another app's
contents. For running a command, `bash` is the right tool — it needs no
permission and returns the output directly.

## Approvals

Two gates, because there are two places a destructive command can be issued.

**SAGE's own `bash`.** The model asking for a tool is not the user agreeing to
it, so a destructive command — recursive delete, `find -delete`, `mkfs`, a raw
device write, `shutdown`, `kill -1`, a permission or ownership change on `/` —
is refused before the shell runs and the turn is parked for a yes/no. Approval
covers the one command that was shown: a widened command (`rm -rf a && rm -rf
b`) is parked again, and the model cannot retry its way past the gate, because
the loop stops at the refusal.

**HERMES.** HERMES will not silently run a dangerous command, but only when it
is in a context that can ask. Outside an interactive CLI, a gateway session, or
an `HERMES_EXEC_ASK` process, it *auto-allows* — which is exactly what a
subprocess launched with pipes is. SAGE sets `HERMES_EXEC_ASK=1` on every
HERMES turn so the gate is live, and pins a narrow toolset
(`terminal,file,search,web,vision,skills,todo,memory`) instead of the
`hermes-cli` composite: `execute_code` runs arbitrary Python that never passes
the terminal's command checks, and `delegate_task` reaches a sub-agent that
does not reliably keep the ask-mode context.

When something needs consent, SAGE parks the turn and asks:

- CLI: prompts for yes/no, then replays that turn.
- Web: emits an `approval` frame; the console shows an allow-once / decline
  prompt and answers via `POST /chat/approve`.

A yes replays with process-scoped `--yolo` (HERMES) or re-runs the single
approved command (SAGE's bash). Neither overrides HERMES hard-deny rules or
protected-file writes — those stay blocked either way. Parked approvals are
single-use and expire after 15 minutes.

## HTTP API

| Route | Purpose |
| --- | --- |
| `GET /` | The console |
| `GET /info` | Model, transport, and the tool table for this surface |
| `POST /chat` | Non-streaming reply |
| `POST /chat/stream` | SSE: `stage`, `delta`, `tool`, `approval`, then `done` |
| `POST /chat/approve` | Answer a parked approval (HERMES or SAGE's bash) |
| `GET /notifications` | Unread notifications (fired reminders), then marks them read |

## Learning from past runs

Each turn records its handler, action, latency, and whether the verifier
accepted the result in `route_outcomes`. The analyzer reads the recent tally
and is told which handler has actually been answering, so a handler that keeps
returning refusals or provider errors becomes visible on the next turn.

## Verification

Every answer goes through `Verifier` before it is reported as done. It rejects
tracebacks, provider error payloads, and bare refusals. It also rejects a reply
that claims to have performed an action when no tool ran to perform it —
"Terminal opened." with nothing behind it is a fabricated result, which is
worse than an honest refusal because the user walks away believing something
happened on their machine. A turn rejected for that reason gets one retry with
the gap named, so the model can actually make the call before the turn fails.

The check is skipped for HERMES turns: HERMES runs its own tools internally and
only its final text is visible, so an empty tool set would say nothing about
what it did.

## Layout

```
sage/core/     analyzer, routing, decision, orchestrator, verifier, HERMES bridge
sage/tools/    registry, tool loop, and the individual tools
sage/tasks.py  task store, background workers, reminders, loops, scheduler
sage/web/      FastAPI app
web/           Next.js console
tests/         pytest suite
```

## Security notes

- The web surface has **no authentication**. Bind it to loopback unless you add
  auth in front of it.
- All web requests share one SQLite conversation, so there is no per-user
  isolation.
- `bash` and `pc_control` are **on by default**, so a web surface started
  without extra configuration is an unauthenticated remote shell. Bind it to
  loopback, or put auth in front of it, unless you mean to expose it.
- Set `SAGE_ALLOW_LOCAL_TOOLS=0` to make a surface execute nothing:
  `bash` and `pc_control` are dropped from the tool table *and* delegation to
  HERMES is refused, because HERMES has its own shell. Gating only the tool
  table would still hand out shell access through the agent.
- Destructive commands need approval on both execution paths regardless of this
  setting, and the web console asks before running one.
