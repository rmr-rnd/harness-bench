"""Tavily web search tool."""
from __future__ import annotations

import httpx

TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for up-to-date information. Use when you need to find facts, current events, or verify information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                }
            },
            "required": ["query"],
        },
    },
}


def search(query: str, api_key: str, max_results: int = 5) -> str:
    """Call Tavily search API, return formatted results as plain text."""
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    parts = []
    if data.get("answer"):
        parts.append(f"Summary: {data['answer']}\n")
    for r in data.get("results", []):
        parts.append(f"[{r['title']}]({r['url']})\n{r.get('content', '')}\n")
    return "\n".join(parts) if parts else "No results found."
