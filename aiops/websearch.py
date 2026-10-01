"""Web search for unknown failures, via Exa's hosted MCP endpoint.

Same search backend the agent-reach skill uses (mcp.exa.ai, no API key). The
results are *untrusted reference material*: they only ever reach the LLM as
context, and whatever the LLM proposes must still pass the bounded action
catalog in actions.py before anything is applied.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

from .config import Config

log = logging.getLogger("aiops.websearch")

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HASH_RE = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]{5,10}){1,2}\b")  # pod-name suffixes
_UUID_RE = re.compile(r"\(?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)?")
_POD_REF_RE = re.compile(r"\s+in pod \S+")  # "... container app in pod web-x_ns(uid)"
_SPACE_RE = re.compile(r"\s+")


def generalize(text: str) -> str:
    """Strip cluster-specific noise (pod refs, UIDs, IPs, pod hashes) so the query matches public posts."""
    text = _POD_REF_RE.sub("", text or "")
    text = _UUID_RE.sub("", text)
    text = _IP_RE.sub("", text)
    text = _HASH_RE.sub("", text)
    return _SPACE_RE.sub(" ", text).strip()


class WebSearch:
    def __init__(self, config: Config) -> None:
        self.config = config

    def search(self, query: str) -> str:
        """Plain-text results (title/url/highlights), '' when disabled or failing."""
        if not self.config.web_search_enabled or not query.strip():
            return ""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {"query": query, "numResults": self.config.web_search_results},
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            self.config.exa_endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                # Exa's edge answers 403 to the default "Python-urllib" agent.
                "User-Agent": "aiops-agent/0.1 (+https://github.com/KJLavender/AIOps)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.web_search_timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except OSError as exc:
            log.warning("web search failed: %s", exc)
            return ""
        text = parse_mcp_response(body)
        limit = self.config.web_search_max_chars
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def parse_mcp_response(body: str) -> str:
    """Accepts a JSON-RPC body or an SSE stream (`data: {...}` lines)."""
    candidates = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    if not candidates:
        candidates = [body]
    texts: list[str] = []
    for raw in candidates:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        for item in (msg.get("result") or {}).get("content") or []:
            if item.get("type") == "text" and item.get("text"):
                texts.append(item["text"])
    return "\n\n".join(texts)
