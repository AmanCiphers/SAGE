"""Vision analysis via the NVIDIA NIM endpoint.

The harness version imported ``chat_vision`` from its own ``llm`` module, which
SAGE cannot use. This goes through SAGE's own LLM client with a vision model.
"""

import base64
import mimetypes
import os

from sage.core.llm import LLMClient

VISION_MODEL = os.environ.get("NVIDIA_VISION_MODEL", "meta/llama-3.2-11b-vision-instruct")

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def describe_image(path, prompt="Describe this image in detail.", llm=None):
    """Describe an image file, returning the model's text."""
    path = str(path or "").strip()

    if not path:
        return {"error": "path is required"}

    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    size = os.path.getsize(path)

    if size > MAX_IMAGE_BYTES:
        return {"error": f"image too large ({size} bytes, limit {MAX_IMAGE_BYTES})"}

    mime = mimetypes.guess_type(path)[0] or "image/png"

    if not mime.startswith("image/"):
        return {"error": f"not an image file: {mime}"}

    encoded = base64.b64encode(open(path, "rb").read()).decode()

    client = llm or LLMClient()

    message = client.complete(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }
        ],
        model=VISION_MODEL,
        system=None,
    )

    return {"path": path, "model": VISION_MODEL, "analysis": message.get("content")}
