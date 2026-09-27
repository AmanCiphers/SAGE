import html
import re

import requests


def web_search(query, num_results=5):
    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={
            "q": query,
            "kl": "us-en"
        },
        headers={
            "User-Agent": "Mozilla/5.0"
        },
        timeout=30
    )

    response.raise_for_status()

    def clean(text):
        return html.unescape(
            re.sub(r"<[^>]+>", "", text)
        ).strip()

    links = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        response.text
    )

    snippets = re.findall(
        r'class="result__snippet">(.*?)</a>',
        response.text
    )

    results = []

    for i, (url, title) in enumerate(links[:num_results]):
        results.append({
            "title": clean(title),
            "url": url,
            "snippet": clean(snippets[i]) if i < len(snippets) else ""
        })

    return {
        "query": query,
        "results": results
    }


def fetch_url(url):
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
        timeout=30
    )

    response.raise_for_status()

    text = re.sub(
        r"<script.*?</script>|<style.*?</style>",
        "",
        response.text or "",
        flags=re.S | re.I
    )

    text = html.unescape(
        re.sub(
            r"\s+",
            " ",
            re.sub(r"<[^>]+>", " ", text)
        )
    ).strip()

    if len(text) > 12000:
        text = text[:12000] + "\n...[truncated]"

    return {
        "url": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "text": text
    }
