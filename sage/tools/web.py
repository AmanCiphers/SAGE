import html
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests

SEARCH_URL = "https://lite.duckduckgo.com/lite/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 30

MAX_FETCH_CHARS = 12000

# The provider rate-limits aggressively and answers a challenge page with
# HTTP 202, so back off and retry before giving up.
SEARCH_ATTEMPTS = 3
SEARCH_BACKOFF = (0.0, 2.0, 6.0)
SEARCH_MIN_INTERVAL = 1.5

_last_search = [0.0]

ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
SNIPPET = re.compile(r"<td class='result-snippet'>(.*?)</td>", re.I | re.S)
CHALLENGE = re.compile(r"anomaly|unusual traffic|are you a robot", re.I)
TAG = re.compile(r"<[^>]+>")
SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)

TEXTUAL = ("text/html", "text/plain", "application/xhtml", "application/json")


def _clean(text):
    return html.unescape(TAG.sub("", text or "")).strip()


def _unwrap(url):
    """DDG wraps results in a /l/?uddg=<encoded> redirect; recover the target."""
    if url.startswith("//"):
        url = "https:" + url

    parsed = urlparse(url)

    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")

        if target:
            return unquote(target[0])

    return url


def _iter_results(page):
    snippets = [_clean(s) for s in SNIPPET.findall(page)]

    for index, (attrs, text) in enumerate(ANCHOR.findall(page)):
        if "result-link" not in attrs:
            continue

        href = re.search(r'href="([^"]+)"', attrs, re.I)

        if not href:
            continue

        title = _clean(text)

        if not title:
            continue

        yield {
            "title": title,
            "url": _unwrap(html.unescape(href.group(1))),
            "snippet": snippets[index] if index < len(snippets) else "",
        }


def _throttle():
    gap = time.monotonic() - _last_search[0]

    if gap < SEARCH_MIN_INTERVAL:
        time.sleep(SEARCH_MIN_INTERVAL - gap)

    _last_search[0] = time.monotonic()


def _search_once(query):
    _throttle()

    response = requests.get(
        SEARCH_URL,
        params={"q": query},
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    if response.status_code == 202 or CHALLENGE.search(response.text):
        raise RuntimeError("anti-bot challenge")

    response.raise_for_status()

    results = []

    for result in _iter_results(response.text):
        results.append(result)

        if len(results) >= 5:
            break

    return results


def web_search(query, num_results=5):
    """Search the web.

    Retries on rate limiting, then raises rather than returning nothing, so
    callers never mistake a blocked request for an empty result set.
    """
    last = None

    for attempt in range(SEARCH_ATTEMPTS):
        try:
            results = _search_once(query)[:num_results]

            return {
                "query": query,
                "results": results,
            }
        except Exception as error:
            last = error
            print(f"[SEARCH] attempt {attempt + 1} failed: {error}")

            if attempt + 1 < SEARCH_ATTEMPTS:
                time.sleep(SEARCH_BACKOFF[min(attempt + 1, len(SEARCH_BACKOFF) - 1)])

    raise RuntimeError(f"search provider blocked {SEARCH_ATTEMPTS} attempts: {last}")


def fetch_url(url):
    """Fetch a page and return its readable text, truncated."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()

    if not any(kind in content_type for kind in TEXTUAL):
        raise RuntimeError(f"refusing to parse non-text content: {content_type}")

    text = SCRIPT_STYLE.sub("", response.text or "")
    text = html.unescape(TAG.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS] + "\n...[truncated]"

    return {
        "url": url,
        "status": response.status_code,
        "content_type": content_type,
        "text": text,
    }
