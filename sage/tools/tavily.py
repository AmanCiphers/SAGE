import os

import requests

SEARCH_URL = "https://api.tavily.com/search"

# "basic" stays inside the free tier. The advanced depth costs several times
# as many credits per request, which burns the 1,000/month allowance fast.
SEARCH_DEPTH = os.environ.get("SAGE_TAVILY_DEPTH", "basic")

TIMEOUT = 30


class TavilyUnavailable(RuntimeError):
    """No key configured, or the service could not be used."""


def key():
    return os.environ.get("TAVILY_API_KEY")


def available():
    return bool(key())


def search(query, num_results=5, include_raw=False):
    """Search via Tavily, returning the same shape as ``web.web_search``.

    ``include_raw`` attaches the fetched page text to each result, which makes
    a separate ``fetch_url`` call unnecessary.
    """
    api_key = key()

    if not api_key:
        raise TavilyUnavailable("TAVILY_API_KEY is not set")

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": SEARCH_DEPTH,
        "max_results": num_results,
        "include_answer": False,
        # Returned page text saves a separate fetch_url call downstream.
        "include_raw_content": bool(include_raw),
    }

    try:
        response = requests.post(SEARCH_URL, json=payload, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise TavilyUnavailable(f"request failed: {error}") from error

    if response.status_code in (401, 403):
        raise TavilyUnavailable(f"key rejected (HTTP {response.status_code})")

    if response.status_code == 429:
        raise TavilyUnavailable("monthly free credits exhausted (HTTP 429)")

    if response.status_code >= 400:
        raise TavilyUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")

    results = []

    for item in response.json().get("results") or []:
        result = {
            "title": (item.get("title") or "").strip(),
            "url": (item.get("url") or "").strip(),
            "snippet": (item.get("content") or "").strip(),
        }

        if include_raw:
            raw = (item.get("raw_content") or "").strip()
            result["text"] = raw

        results.append(result)

    return {
        "query": query,
        "provider": "tavily",
        "results": results,
    }
