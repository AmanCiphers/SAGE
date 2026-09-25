import os
import subprocess
import tempfile
from pathlib import Path

import requests

SESSION = {"cwd": os.getcwd(), "env": dict(os.environ)}

MAX_LINES = 2000
MAX_BYTES = 51200

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a command in the user's shell. Use this to explore files, run tests, "
                "or make changes. Explain what the command does and why before running it. "
                "Prefer the workdir parameter to change directories instead of `cd X && cmd`. "
                "Output is truncated; if truncated, the full output is written to the returned "
                "output_file path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Directory to run the command in. Defaults to the current working directory.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Kill the command after this many milliseconds. Default 120000.",
                    },
                },
                "required": ["command"],
            },
        },
    }
]


WEB_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo) and return titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its text content (HTML stripped). Use for reading pages the user mentions.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

PC_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "pc_control",
            "description": "Control the user's Mac. Actions: open_app (target=app name), open_url (target=url, browser=optional Safari/Chrome/Edge/Firefox, defaults to system default), volume (target=0-100), mute, unmute, notify (target=message), speak (target=text), sleep, lock, screenshot (target=optional output path).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "target": {"type": "string"},
                    "browser": {
                        "type": "string",
                        "enum": ["Safari", "Chrome", "Edge", "Firefox"],
                    },
                },
                "required": ["action"],
            },
        },
    }
]

VISION_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": "Analyze an image file with a vision model. Use after screenshot to see the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    }
]

LOOP_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "start_loop",
            "description": "Run a goal repeatedly in the background every N seconds (monitoring loops).",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "every_seconds": {"type": "integer"},
                },
                "required": ["goal"],
            },
        },
    }
]

TASK_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Assign a task to a background worker so Sage can keep talking to the user. Returns a task_id for later progress queries.",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": "Report progress of background tasks. Omit task_id to list all tasks.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Schedule a reminder or event. due_in_seconds is how far in the future (e.g. 3600 = in an hour). repeat_interval makes it recur (86400 = daily, 604800 = weekly); set 0 for one-off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due_in_seconds": {"type": "integer"},
                    "repeat_interval": {"type": "integer"},
                },
                "required": ["title", "due_in_seconds"],
            },
        },
    },
]


def run_bash(command, workdir=None, timeout=120000):
    cwd = workdir or SESSION["cwd"]
    timeout_s = timeout / 1000
    got_timeout = False

    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/zsh",
            cwd=cwd,
            env=SESSION["env"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = proc.stdout
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        got_timeout = True
        output = (e.stdout or "") + f"\n[timed out after {timeout_s}s]"
        exit_code = -1

    lines = output.splitlines()
    trunc = len(lines) > MAX_LINES or len(output) > MAX_BYTES
    output_file = None
    if trunc:
        output_file = str(Path(tempfile.gettempdir()) / "opencode" / f"bash_{abs(hash(command))}.txt")
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(output)
        kept = "\n".join(lines[:MAX_LINES])
        output = f"{kept}\n... [output truncated: {len(lines)} lines / {len(output)} bytes] full output: {output_file}"

    return {
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code,
        "timed_out": got_timeout,
        "output": output,
        "truncated": trunc,
        "output_file": output_file,
    }


def web_search(query, num_results=5):
    import html as html_mod
    import re

    resp = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query, "kl": "us-en"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()

    def clean(s):
        return html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()

    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text)
    results = []
    for i, (url, title) in enumerate(links[:num_results]):
        results.append(
            {
                "title": clean(title),
                "url": url,
                "snippet": clean(snips[i]) if i < len(snips) else "",
            }
        )
    return {"query": query, "results": results}


def fetch_url(url):
    import html as html_mod
    import re

    resp = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
    )
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", resp.text or "", flags=re.S | re.I)
    text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))).strip()
    if len(text) > 12000:
        text = text[:12000] + "\n...[truncated]"
    return {"url": url, "status": resp.status_code, "content_type": resp.headers.get("content-type"), "text": text}


def pc_control(action, target=None, browser=None):
    if action == "open_app" and target and (
        "://" in target or target.endswith((".com", ".net", ".org", ".tv"))
    ):
        action = "open_url"
    if action == "open_url":
        cmd = f'open -a "{browser}" "{target}"' if browser else f'open "{target}"'
    elif action == "open_app":
        cmd = f'open -a "{target}"'
    else:
        scripts = {
            "volume": f'osascript -e "set volume output volume {target}"',
            "mute": 'osascript -e "set volume output muted true"',
            "unmute": 'osascript -e "set volume output muted false"',
            "notify": f'osascript -e \'display notification "{target}" with title "Sage"\'',
            "speak": f'say "{target}"',
            "sleep": "pmset sleepnow",
            "lock": "osascript -e 'tell application \"System Events\" to sleep'",
            "screenshot": f'screencapture -x "{target or "/tmp/sage_screenshot.png"}"',
        }
        cmd = scripts.get(action)
    if not cmd:
        return {"error": f"unknown action '{action}'"}
    proc = subprocess.run(cmd, shell=True, executable="/bin/zsh", capture_output=True, text=True, timeout=60)
    return {"action": action, "ran": cmd, "exit_code": proc.returncode, "stderr": proc.stderr}


def describe_image(path, prompt="Describe this image in detail."):
    from llm import chat_vision

    if not os.path.exists(path):
        return {"error": f"{path} not found"}
    return {"image": path, "analysis": chat_vision(path, prompt)}