"""Evaluate: score an LLM-proposed fix before it is applied.

Two layers, after Future AGI's "heuristic + LLM-as-judge" evals:
1. Evidence check (deterministic): the cluster data must contain the signal the
   action is meant for (a readiness probe failure for a probe-path change, an
   OOM kill for a memory raise, ...). No signal -> rejected without an LLM call.
2. LLM judge: a second, independent call grades the proposal against the
   cluster evidence only - no web results, so a poisoned page can't vouch for
   itself. Every score must reach the threshold.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .models import Diagnosis, PodIssue

log = logging.getLogger("aiops.judge")

_EVIDENCE: dict[str, re.Pattern] = {
    "set_readiness_probe_path": re.compile(r"Readiness probe failed|Unhealthy", re.IGNORECASE),
    "rollout_restart": re.compile(r"Readiness probe failed|Unhealthy|not Ready", re.IGNORECASE),
    "lower_requests": re.compile(r"Insufficient (cpu|memory)|FailedScheduling", re.IGNORECASE),
    "set_memory_limit": re.compile(r"OOMKilled|out of memory", re.IGNORECASE),
}

_PROMPT = """You are a strict reviewer of automated Kubernetes fixes. Grade the
proposed fix using ONLY the cluster evidence below. Respond with ONLY JSON:
{{"grounded": 0.0, "fits": 0.0, "safe": 0.0, "reason": "..."}}
- grounded: the stated root cause is directly supported by the evidence (1.0)
  or invented / contradicted (0.0)
- fits: the action plausibly fixes that root cause (1.0) or not (0.0)
- safe: the change is limited and reversible and touches nothing else (1.0)

Symptom: {symptom}
Proposed root cause: {root_cause}
Proposed action: {action} {params}
Concrete change: {change}

--- Pod events (oldest first) ---
The LAST lines describe the current state. A brief failure while the container
was starting (e.g. "connection refused" in the first seconds) is normal and
does not contradict a later, different failure.
{events}

--- Container logs (tail) ---
{logs}

--- Container spec ---
{spec}
"""


@dataclass
class Verdict:
    passed: bool
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict:
        return {"passed": self.passed, "scores": self.scores, "reason": self.reason}


class Judge:
    def __init__(self, config: Config) -> None:
        self.config = config

    def evidence_supports(self, action: str, evidence: str) -> bool:
        pattern = _EVIDENCE.get(action)
        return bool(pattern and pattern.search(evidence or ""))

    def review(self, issue: PodIssue, diagnosis: Diagnosis, change: str,
               events: str, logs: str, spec: str) -> Verdict:
        action = diagnosis.proposed_action or ""
        evidence = f"{issue.message}\n{events}\n{logs}"
        if not self.evidence_supports(action, evidence):
            return Verdict(False, reason=f"no evidence in events/logs for {action}")
        raw = self._ask(_PROMPT.format(
            symptom=issue.symptom.value, root_cause=diagnosis.root_cause, action=action,
            params=json.dumps(diagnosis.action_params, ensure_ascii=False), change=change,
            events=_tail(events, 2000), logs=_tail(logs, 1500), spec=spec[:2000]))
        if raw is None:
            return Verdict(False, reason="judge unavailable")
        scores = {}
        for key in ("grounded", "fits", "safe"):
            try:
                value = float(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            scores[key] = value / 100 if value > 1 else value
        passed = min(scores.values()) >= self.config.judge_min_score
        return Verdict(passed, scores, str(raw.get("reason", ""))[:300])

    def _ask(self, prompt: str) -> Optional[dict]:
        payload = json.dumps({
            "model": self.config.judge_model or self.config.ollama_model,
            "prompt": prompt,
            "stream": False,
            # Qwen3-family models "think" first unless told not to (slow, and noise
            # before the JSON); ignored by models without a thinking mode.
            "think": False,
            "format": "json",
            "options": {"num_ctx": self.config.llm_num_ctx, "temperature": 0},
        }).encode("utf-8")
        req = urllib.request.Request(self.config.ollama_endpoint.rstrip("/") + "/api/generate",
                                     data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.config.llm_timeout_seconds) as resp:
                return json.loads(json.loads(resp.read().decode("utf-8")).get("response", "{}"))
        except (OSError, ValueError) as exc:
            log.warning("judge call failed: %s", exc)
            return None


def _tail(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[-limit:]
