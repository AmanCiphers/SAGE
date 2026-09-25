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
| `voice.py` | Optional voice: wake word (custom-trained `sage` centroid), recording (`sounddevice`), STT via isolated subprocess, prompt scenarios, TTS (home server, macOS `say` fallback) |
| `stt_worker.py` | Speech-to-text in a subprocess so native whisper crashes can never take down the server |
| `record_wake_samples.py` | Record `sage` wake-word audio into `wake_data/sage/` |
| `train_wake.py` | Train the `sage` centroid match model into `wake_models/sage.json` |
| `render_prompts.py` | Render the 10 scenario prompt WAVs through the TTS server (or `--local` via macOS `say`) |
| `scenarios/` | `scenarios.json` + WAV audio for ack/no-command/stall/timeout/thinking/tts-down/disabled/permission/task-done/reminder |
| `sage.py` | Full harness entry point: scheduler + channels + voice + REPL |
| `server.py` | FastAPI control API for the web UI (`/chat`, `/tasks`, `/reminders`, `/status`, `/voice/*`) |
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
   `sounddevice numpy faster-whisper openai-whisper`.
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
   # SAGE_WAKE_WORD=sage             # wake word (custom model matches this)
   # SAGE_WAKE_THRESHOLD=0.90        # override wake match threshold at runtime
   # SAGE_WHISPER_MODEL=small        # STT model (small/base/tiny fallback chain)
   # CHATTERBOX_URL=http://192.168.1.44:8000/tts  # home TTS: POST {"text": ...} -> WAV bytes
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
# with SAGE_VOICE=1: say "sage", then a command        # voice
```

## Custom wake word ("sage")

The wake word is **not** the prebuilt `jarvis` model — it's a centroid match over
a temporal log-mel representation, trained on your own recordings:

```bash
python3 record_wake_samples.py          # record ~15 clean "sage" takes -> wake_data/sage/
python3 train_wake.py --data wake_data/sage --threshold 0.90 --out wake_models/sage.json
```

How detection works:

1. Listen continuously; a voice **burst** is opened when audio RMS exceeds a low
   threshold and closed after ~0.3s of silence (soft consonants are kept).
2. A burst only counts if it lasts 0.25–1.3s (a word, not a sentence).
3. The burst is compared to the trained centroid; a match above `threshold`
   fires the wake word (silent re-arming for the two-frame debounce).

This rejects normal chatter (no burst end), long sentences (too long), and
vowel-like filler ("um", ~0.39 vs 0.90+ for real "sage" on the temporal feature).
Tuning: raise/lower `--threshold`, or override at runtime with
`SAGE_WAKE_THRESHOLD=0.95 python3 server.py`. Every match prints
`[voice] wake match 'sage' score=... (threshold ...)`.

## Notes / limitations

- Free NIM endpoints have rate limits — each prompt is 1–2 API calls, and each
  worker step consumes one.
- WhatsApp is send-only (the agent can notify you; you can't reply back through
  it without a Meta webhook).
- The wake word is a custom-trained `sage` centroid model — see "Custom wake
  word" above. It fires on a 0.25–1.3s voice burst whose temporal log-mel
  feature clears the threshold; general speech, fillers, and long sentences
  are rejected.
- Voice TTS prefers the home server (`POST /tts` with `{"text": ...}`
  returning WAV bytes, e.g. `http://192.168.1.44:8000/tts`). If it's
  unreachable the voice falls back to macOS `say` and plays the `tts_down`
  prompt. The 10 scenario phrases (ack, no-command, stall, timeout, thinking,
  tts-down, disabled, permission, task-done, reminder) are rendered from the
  same server via `render_prompts.py`.
- STT runs in an isolated subprocess (`stt_worker.py`) because Apple's Python
  segfaults on native whisper inference in threads; the server supervises and
  auto-restarts the worker. Transfer with a WAV path on stdin / JSON on stdout.
- PC/voice features need macOS; some actions require accessibility permissions.
- State persists in `sage.db` (SQLite) across restarts.