"""Search provider chain.

Tavily is preferred when a key is configured because it does not rate-limit
the way the scraped DuckDuckGo endpoint does. DuckDuckGo stays as a fallback
so the system still works with no key at all.
"""

from sage.tools import tavily
from sage.tools.web import web_search

# Returned alongside results so callers can tell where an answer came from.
PROVIDER = "tavily"


def _try_tavily(query, num_results, errors):
    try:
        found = tavily.search(query, num_results=num_results)
    except tavily.TavilyUnavailable as error:
        errors.append(f"tavily: {error}")
        return None

    if not found["results"]:
        errors.append("tavily: returned no results")
        return None

    return found


def _try_duckduckgo(query, num_results, errors):
    try:
        return web_search(query, num_results=num_results)
    except Exception as error:
        errors.append(f"duckduckgo: {error}")
        return None


def search(query, num_results=5, include_raw=False):
    """Search the web, trying each provider in turn.

    Returns ``{"query", "provider", "results", "attempts"}``. Each result has
    ``title``, ``url``, ``snippet`` and, when available, ``text``.

    Raises RuntimeError only when every provider has failed.
    """
    errors = []
    found = _try_tavily(query, num_results, errors) if tavily.available() else None

    if found is None:
        if not tavily.available():
            errors.append("tavily: no API key configured")

        found = _try_duckduckgo(query, num_results, errors)

    if found is None:
        raise RuntimeError("all search providers failed: " + "; ".join(errors))

    results = []

    for result in found["results"]:
        normalized = {
            "title": result["title"],
            "url": result["url"],
            "snippet": result["snippet"],
        }

        if include_raw and result.get("text"):
            normalized["text"] = result["text"]

        results.append(normalized)

    if not results:
        raise RuntimeError(
            f"{found.get('provider', PROVIDER)} returned no usable results: "
            + "; ".join(errors)
        )

    return {
        "query": query,
        "provider": found.get("provider", "duckduckgo"),
        "results": results,
        "attempts": errors,
    }
