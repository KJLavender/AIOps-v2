"""Human approval for fixes the LLM proposed (tiered automation).

Rules and verified Lessons are applied automatically. A fix from a source in
AIOPS_APPROVAL_SOURCES (default: llm) passes every check as before, then waits
in phase AwaitingApproval. diagnose sends an ntfy notification with
"approve" / "reject" buttons; each button POSTs to the monitor console with a
one-time token. Only the token's SHA-256 is stored on the Remediation.

Approving only moves the phase to Proposed. repair still rebuilds the patch
from the live Deployment and re-runs every check, so a human "yes" never
skips a safety gate. Unanswered requests are rejected after
AIOPS_APPROVAL_TTL seconds.

    python -m agents.approval <remediation> approve|reject   # without a phone
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiops import crd
from aiops.config import Config
from aiops.metrics import METRICS

log = logging.getLogger("agents.approval")


def needs_approval(config: Config, source: str) -> bool:
    return source in config.approval_sources


def new_request(config: Config) -> tuple[str, dict]:
    """A fresh token and the record stored on the Remediation (hash only)."""
    token = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(seconds=config.approval_ttl_seconds)
    return token, {"tokenHash": _hash(token),
                   "expiresAt": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expired(rem: dict) -> bool:
    expires = ((rem.get("spec") or {}).get("approval") or {}).get("expiresAt")
    return expires is None or crd.age_seconds(expires) > 0


def notify(config: Config, rem: dict, token: str) -> bool:
    """Push the request with approve / reject buttons. False if nothing was sent."""
    if not config.approval_ntfy_url:
        return False
    spec, name = rem["spec"], rem["metadata"]["name"]
    base, _, topic = config.approval_ntfy_url.rstrip("/").rpartition("/")
    target = f"{spec['target']['namespace']}/{spec['target']['deployment']}"
    lines = [f"{target} · {spec['symptom']}", spec.get("summary", "")[:300],
             f"confidence {spec.get('confidence')}"]
    judge = (spec.get("evaluation") or {}).get("judge") or {}
    if judge.get("scores"):
        lines.append("judge " + ", ".join(f"{k} {v}" for k, v in judge["scores"].items()))
    lines.append(f"Expires {spec['approval']['expiresAt']}. CLI: python -m agents.approval {name} approve")
    body = {"topic": topic, "title": f"🔧 Approve fix for {target}?", "message": "\n".join(lines),
            "tags": ["wrench"], "priority": 4}
    if config.console_url:
        url = f"{config.console_url}/api/approval/{name}?token={token}"
        body["actions"] = [
            {"action": "http", "label": "Approve", "url": url + "&decision=approve", "method": "POST", "clear": True},
            {"action": "http", "label": "Reject", "url": url + "&decision=reject", "method": "POST", "clear": True},
        ]
    req = urllib.request.Request(base + "/", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except OSError as exc:
        log.warning("approval notification for %s failed: %s", name, exc)
        return False


def decide(store: crd.Store, name: str, approve: bool, token: Optional[str] = None,
           who: str = "ntfy") -> tuple[int, str]:
    """Answer a request -> (HTTP status, message). token=None is the operator CLI path."""
    rem = store.get("remediations", name)
    if rem is None:
        return 404, "no such remediation"
    if crd.phase(rem) != crd.AWAITING_APPROVAL:
        return 409, f"remediation is {crd.phase(rem)}, not waiting for approval"
    if token is not None:
        stored = ((rem.get("spec") or {}).get("approval") or {}).get("tokenHash", "")
        if not stored or not hmac.compare_digest(stored, _hash(token)):
            return 403, "wrong token"
    if expired(rem):
        reject(store, rem, "approval expired")
        return 410, "approval expired"
    if approve:
        store.set_status(rem, crd.PROPOSED, f"approved by a human ({who})", approvedBy=who, approvedAt=crd.now())
        METRICS.inc("aiops_approvals_total", decision="approved")
        log.info("approved %s (%s)", name, who)
        return 200, "approved: repair re-checks and applies it within a poll interval"
    reject(store, rem, f"rejected by a human ({who})")
    METRICS.inc("aiops_approvals_total", decision="rejected")
    return 200, "rejected"


def reject(store: crd.Store, rem: dict, reason: str) -> None:
    store.set_status(rem, crd.REJECTED, reason)
    inc = store.get("incidents", rem["metadata"]["name"])
    if inc:
        store.set_status(inc, crd.UNRESOLVED, f"fix not applied: {reason}")
    log.info("%s: %s", rem["metadata"]["name"], reason)


def expire_stale(store: crd.Store, remediations: list[dict]) -> int:
    count = 0
    for rem in remediations:
        if crd.phase(rem) == crd.AWAITING_APPROVAL and expired(rem):
            reject(store, rem, "approval expired")
            METRICS.inc("aiops_approvals_total", decision="expired")
            count += 1
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("approve", "reject"):
        print("usage: python -m agents.approval <remediation> approve|reject", file=sys.stderr)
        return 2
    from aiops.kube import KubeClient
    config = Config.from_env()
    store = crd.Store(KubeClient(config), config.system_namespace)
    code, message = decide(store, argv[0], argv[1] == "approve", token=None, who="cli")
    print(message)
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
