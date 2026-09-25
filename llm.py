import json
import os
import sys
from pathlib import Path

import requests

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
FAST_MODEL = os.environ.get("NVIDIA_FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
VISION_MODEL = os.environ.get("NVIDIA_VISION_MODEL", "meta/llama-3.2-11b-vision-instruct")


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def chat(messages, model=DEFAULT_MODEL, tools=None, stream=False, on_token=None):
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise SystemExit("NVIDIA_API_KEY not set. Add it to .env and load it (e.g. `set -a; source .env; set +a`).")

    payload = {"model": model, "messages": messages, "stream": stream}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    resp = requests.post(
        f"{NIM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=180,
    )
    if resp.status_code == 410:
        raise SystemExit(
            "410 Gone: your NVIDIA account is missing the 'Public API Endpoints' "
            "permission. Enable it in your build.nvidia.com org settings (or request "
            "it via NVIDIA support), or point NIM_BASE_URL at a self-hosted NIM."
        )
    resp.raise_for_status()

    if not stream:
        return resp.json()["choices"][0]["message"]

    content, tool_calls, order = [], {}, []
    for line in resp.iter_lines():
        if not line or line == b"data: [DONE]":
            continue
        try:
            chunk = json.loads(line[6:])
        except (json.JSONDecodeError, IndexError):
            continue
        if not chunk.get("choices"):
            continue
        delta = chunk["choices"][0].get("delta", {})
        if delta.get("content"):
            content.append(delta["content"])
            if on_token:
                on_token(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc["index"]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                order.append(idx)
            if tc.get("id"):
                tool_calls[idx]["id"] = tc["id"]
            if tc["function"].get("name"):
                tool_calls[idx]["function"]["name"] += tc["function"]["name"]
            if tc["function"].get("arguments"):
                tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]

    msg = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tool_calls[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_calls[i]["function"]["name"],
                    "arguments": tool_calls[i]["function"]["arguments"],
                },
            }
            for i in order
        ]
    return msg


def chat_vision(image_path, prompt="Describe this image in detail."):
    import base64
    import mimetypes

    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }
    ]
    return chat(messages, model=VISION_MODEL)["content"]


def main():
    from agent import repl, run_agent

    if len(sys.argv) > 1:
        run_agent(" ".join(sys.argv[1:]))
    else:
        repl("Prompt: ")


if __name__ == "__main__":
    main()