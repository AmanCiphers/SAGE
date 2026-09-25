# Sage — AI Harness

A JARVIS-style personal AI assistant for a hackathon, powered by the NVIDIA NIM
API (OpenAI-compatible). It runs on your Mac, talks over the terminal (and
optionally Telegram/voice), assigns background workers, schedules reminders,
searches the web, controls the PC, and analyzes images.

## Requirements status

| # | Requirement | Status | Where |
|---|-------------|--------|-------|
| 1 | Natural voice + "Sage" wake word | Optional, guarded | `voice.py` |
| 2 | Background 24/7, minimal resources | Done | `sage.py` (daemon threads, polling) |
| 3 | Task manager + workers (talk while they run) | Done | `tasks.py` |
| 4 | Loops (recurring monitoring) | Done | `tasks.py` `start_loop` |
| 5 | Telegram + WhatsApp integration | Done | `channels.py` |
| 6 | Reminders / scheduling (CRON-style) | Done | `tasks.py` (recurring intervals) |
| 7 | Internet / browser | Done | `tools.py` (`web_search`, `fetch_url`) |
| 8 | Control your PC | Done | `tools.py` (`pc_control`, AppleScript) |
| 9 | Use the terminal | Done | `tools.py` (`bash`) |
| 10 | Image analysis | Done | `llm.py` `chat_vision` |

## Files

| File | Purpose |
|------|---------|
| `llm.py` | NVIDIA NIM client: `chat()` (streaming + tool calls supported), `chat_vision()`, env loading |
| `tools.py` | Tool schemas + executors: `bash`, `web_search`, `fetch_url`, `pc_control`, `describe_image`; constants for task/loop/reminder tools |
| `tasks.py` | SQLite store (`sage.db`), `TaskManager` (background worker threads), `Scheduler` (fires reminders/loops every 5s) |
| `agent.py` | The agent core: `run_agent()` loop, tool registry (`ACTIONS`), persistent `repl()` |
| `channels.py` | Output channels: console, Telegram (bi-directional), WhatsApp (send-only) |
| `voice.py` | Optional voice: wake word (`openwakeword`), recording (`sounddevice`), transcription (`whisper`), TTS (macOS `say`) |
| `sage.py` | Full harness entry point: scheduler + channels + voice + REPL |
| `server.py` | FastAPI control API for the web UI (`/chat`, `/tasks`, `/reminders`, `/status`) |
| `ui/` | Next.js + Tailwind web dashboard (monochrome terminal UI) |
| `.env` | Secrets (see Setup) |

## How the agent works

```
user goal → run_agent loop:
  send messages + tool schemas to NIM → model replies with either
    (a) text  → print/notify, done
    (b) tool_calls → execute each tool locally, send results back as
        "tool" role messages, loop again
```

The model never touches your machine directly — the harness executes its calls.
Tool results (exit codes, output, errors) are fed back so the model self-corrects.

## Setup

1. `pip install requests` (mandatory). Optional: `python-telegram-bot`,
   `openwakeword sounddevice numpy openai-whisper`.
2. Create `.env` with:
   ```
   NVIDIA_API_KEY=your_key
   # Optional:
   # NVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b   # faster default model
   # NVIDIA_FAST_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
   # NVIDIA_VISION_MODEL=meta/llama-3.2-11b-vision-instruct
   # TELEGRAM_BOT_TOKEN=...          # chat with Sage from your phone
   # WHATSAPP_TOKEN=... / WHATSAPP_PHONE_ID=... / WHATSAPP_TO=...
   # SAGE_VOICE=1                    # enable wake-word voice loop
   # SAGE_WAKE_WORD=jarvis
   # SAGE_WHISPER_MODEL=tiny
   # CHATTERBOX_URL=http://127.0.0.1:4123/v1/audio/speech  # TTS via Resemble Chatterbox
   # CHATTERBOX_VOICE=alloy          # optional voice name
   ```
3. The `.env` file is parsed automatically at import time — no sourcing needed.

## Usage

- **Full harness (scheduler + Telegram + voice + REPL):** `python3 sage.py`
- **Terminal agent, persistent REPL:** `python3 llm.py` (type `exit` to quit)
- **One-shot:** `python3 llm.py "your question"`
- **Agent only:** `python3 agent.py`
- **Web UI (control panel):**
  ```
  pip install fastapi uvicorn
  python3 -m uvicorn server:app --port 8000 &
  cd ui && npm install && npm run dev   # then open http://localhost:3000
  ```

## Agent tools

Called automatically by the model via its reasoning:

- `bash(command, workdir, timeout)` — run shell commands (opencode-style:
  truncation of large output with full log saved to a temp file)
- `create_task(title)` — spawn a background worker
- `task_status(task_id)` — progress of tasks
- `create_reminder(title, due_in_seconds, repeat_interval)` — timed reminders
- `start_loop(goal, every_seconds)` — recurring background goal
- `web_search(query)` — DuckDuckGo
- `fetch_url(url)` — read a page (HTML stripped)
- `pc_control(action, target, browser)` — apps, URLs (browser-aware), volume,
  notifications, speech, sleep, lock, screenshots
- `describe_image(path, prompt)` — vision-model analysis (e.g. after screenshot)

## Testing checklist

```
python3 llm.py "hi"                                   # auth + streaming
what's the date and current directory                  # bash
search the web for "nvidia nim"                        # web_search
fetch https://build.nvidia.com and summarize            # fetch_url
show a notification on my mac saying test              # pc_control/notify
take a screenshot and describe what you see            # screenshot + vision
hire a worker to run: sleep 3 && echo done            # worker
what's the status of my tasks                          # task_status
remind me in 20 seconds to drink water                # reminder (watch console)
# with TELEGRAM_BOT_TOKEN set: message the bot "hey"   # Telegram
# with SAGE_VOICE=1: say "jarvis", then a command      # voice
```

## Notes / limitations

- Free NIM endpoints have rate limits — each prompt is 1–2 API calls, and each
  worker step consumes one.
- WhatsApp is send-only (the agent can notify you; you can't reply back through
  it without a Meta webhook).
- The default wake word is the prebuilt `jarvis` model; a custom "Sage" wake
  word requires training an `openwakeword` model (or Porcupine).
- Voice TTS prefers a **Resemble Chatterbox** server (OpenAI-compatible
  `POST /v1/audio/speech`, e.g. `chatterbox-tts-api` on port 4123). If it's not
  reachable it falls back to macOS `say`. Set `CHATTERBOX_URL`/`CHATTERBOX_VOICE`
  to point at your server and voice.
- PC/voice features need macOS; some actions require accessibility permissions.
- State persists in `sage.db` (SQLite) across restarts.