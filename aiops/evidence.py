"""Evidence: what the monitor captures so nobody else needs cluster access.

The diagnose agent reads untrusted web pages and talks to an LLM, so it gets
no Kubernetes permissions at all. Everything it may look at - log tails,
events, the container spec - is copied into the Incident by the monitor,
with secrets redacted (log lines can carry tokens, and anything handed to the
diagnose agent may end up in a web-search query).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .config import Config
from .kube import KubeClient, container_from_deployment
from .models import PodIssue

_REDACTIONS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
                r"client[_-]?secret|auth)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted-jwt>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s:/@]+:[^\s@/]+@"), r"\1<redacted>@"),  # user:pass@host
]

_SPEC_FIELDS = ("name", "image", "resources", "readinessProbe", "livenessProbe", "startupProbe", "ports")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text or "")
    return text


def _tail(text: str, lines: int, chars: int = 6000) -> str:
    text = "\n".join((text or "").splitlines()[-lines:])
    return text[-chars:]


def container_summary(container: Optional[dict]) -> dict:
    """The container spec minus env values (names only, plus whether one is set)."""
    if not container:
        return {}
    summary = {k: container[k] for k in _SPEC_FIELDS if k in container}
    summary["env"] = [{"name": e.get("name"), "set": bool(e.get("value") or e.get("valueFrom"))}
                      for e in container.get("env", [])]
    return summary


def collect(kube: KubeClient, issue: PodIssue, deployment_name: Optional[str],
            deployment: Optional[dict], config: Config) -> dict[str, Any]:
    needs_logs = issue.symptom.value in {"CrashLoopBackOff", "Failed", "NotReady"}
    logs = previous = events = ""
    if needs_logs:
        logs = kube.pod_logs(issue.namespace, issue.pod, issue.container)
        previous = kube.pod_logs(issue.namespace, issue.pod, issue.container, previous=True)
    events = kube.pod_events(issue.namespace, issue.pod)
    container = container_from_deployment(deployment, issue.container) if deployment else None
    return {
        "logs": redact(_tail(logs, config.evidence_log_lines)),
        "previousLogs": redact(_tail(previous, config.evidence_log_lines)),
        "events": redact(_tail(events, 30)),
        "container": container_summary(container),
        "podIP": (issue.raw or {}).get("status", {}).get("podIP", ""),
    }


class EvidenceKube:
    """Read-only stand-in for KubeClient, answering from an Incident's evidence."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.evidence = spec.get("evidence") or {}

    def get_owner_deployment(self, namespace, pod):
        name = (self.spec.get("target") or {}).get("deployment")
        container = dict(self.evidence.get("container") or {})
        if not name or not container:
            return None, None
        # Rules check "is this env var already set?" - give them names, never values.
        container["env"] = [{"name": e["name"], "value": "<set>"} if e.get("set") else {"name": e["name"]}
                            for e in container.get("env", [])]
        return name, {"spec": {"template": {"spec": {"containers": [container]}}}}

    def pod_logs(self, namespace, pod, container=None, previous=False):
        return self.evidence.get("previousLogs" if previous else "logs", "")

    def pod_events(self, namespace, pod):
        return self.evidence.get("events", "")

    def describe_pod(self, namespace, pod):
        return ""
