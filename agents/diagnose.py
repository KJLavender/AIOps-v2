"""Diagnose agent: turns an Incident into a Remediation proposal.

Permissions: read/update Incidents, create Remediations, read Lessons - and
nothing in the watched namespaces. It is the only agent that reads untrusted
web pages and talks to the LLM, so it is the one that holds no power: its
output is a *proposal* (action + parameters) that the repair agent re-checks
against the live cluster and its own allowlists before anything changes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from aiops import actions, crd, guard
from aiops.config import Config
from aiops.evidence import EvidenceKube
from aiops.judge import Judge
from aiops.kube import container_from_deployment
from aiops.llm import LLMAnalyzer
from aiops.llm.base import LLMInput
from aiops.metrics import METRICS
from aiops.models import Diagnosis, DiagnosisSource, PodIssue, Symptom
from aiops.rules import RuleEngine
from aiops.rules.base import RuleContext
from aiops.websearch import WebSearch, generalize

log = logging.getLogger("agents.diagnose")


def search_query(issue: PodIssue, ctx: RuleContext) -> str:
    """Symptom + the most specific error line, minus cluster noise."""
    detail = ""
    for source in (ctx.events, ctx.previous_logs, ctx.logs):
        lines = [l for l in (source or "").splitlines() if l.strip()]
        hits = [l for l in lines if re.search(r"fail|error|exception|refused|denied", l, re.I)]
        if hits:
            detail = hits[-1]
            break
    detail = re.sub(r"^\S+\s+(Warning|Normal)\s+\S+\s+\S+\s+", "", (detail or issue.message).strip())
    return generalize(f"kubernetes {issue.symptom.value} {detail}")[:200]


def same_fix(rem: dict, target: dict, action: Optional[str], params: dict) -> bool:
    spec = rem.get("spec") or {}
    return (spec.get("target", {}).get("namespace") == target.get("namespace")
            and spec.get("target", {}).get("deployment") == target.get("deployment")
            and spec.get("action") == action and (spec.get("params") or {}) == (params or {}))


class Decider:
    """Incident spec -> Remediation spec. Pure apart from the LLM / search calls."""

    def __init__(self, config: Config, llm: LLMAnalyzer, websearch: WebSearch, judge: Judge) -> None:
        self.config = config
        self.llm = llm
        self.websearch = websearch
        self.judge = judge
        self.rules = RuleEngine()

    def decide(self, spec: dict, lessons: list[dict], history: list[dict]) -> tuple[dict, dict]:
        target = spec["target"]
        issue = PodIssue(target["namespace"], target["pod"], target.get("container") or None,
                         Symptom.from_reason(spec["symptom"]), spec.get("message", ""),
                         restart_count=spec.get("restartCount", 0),
                         raw={"status": {"podIP": (spec.get("evidence") or {}).get("podIP", "")}})
        ekube = EvidenceKube(spec)
        ctx = RuleContext(kube=ekube, config=self.config)
        ctx.logs = ekube.pod_logs("", "")
        ctx.previous_logs = ekube.pod_logs("", "", previous=True)
        ctx.events = ekube.pod_events("", "")
        trace: dict[str, Any] = {}

        diag = self.rules.diagnose(issue, ctx)
        if diag is None or (not diag.auto_fixable and diag.forward_to_kb):
            lesson = self._lesson(spec, lessons)
            if lesson is not None:
                diag = lesson
            elif self.config.llm_enabled and (diag is None or diag.consult_llm):
                # Only reached when the rule was generic (or absent), so the
                # LLM's more specific answer wins even as a recommendation.
                llm_diag = self._ask_llm(issue, ctx, ekube, trace)
                if llm_diag is not None:
                    diag = llm_diag

        source = diag.source.value if diag else "none"
        auto = bool(diag and diag.auto_fixable and diag.proposed_action)
        action = diag.proposed_action if auto else None
        params = diag.action_params if auto else {}
        if auto and any(crd.phase(r) in (crd.ROLLED_BACK, crd.FAILED) and same_fix(r, target, action, params)
                        for r in history):
            auto = False
            trace["blocked"] = "this exact fix failed validation before"
        rem_spec = {
            "incident": spec.get("key", ""),
            "symptom": spec["symptom"],
            "target": {"namespace": target["namespace"], "deployment": target.get("deployment", ""),
                       "container": target.get("container", "")},
            "source": source,
            "rootCause": diag.root_cause if diag else "no rule, lesson or LLM answer",
            "summary": diag.summary if diag else "needs human review",
            "action": action,
            "params": params or {},
            "confidence": round(diag.confidence, 2) if diag else 0.0,
            "autoApply": auto,
            "evaluation": {k: trace[k] for k in ("webQuery", "guardFlags", "judge", "blocked") if k in trace},
        }
        return rem_spec, trace

    def _lesson(self, spec: dict, lessons: list[dict]) -> Optional[Diagnosis]:
        """A fix verified before on this exact workload and symptom."""
        target = spec["target"]
        for lesson in lessons:
            ls = lesson.get("spec") or {}
            if (ls.get("symptom") == spec["symptom"]
                    and ls.get("target", {}).get("namespace") == target["namespace"]
                    and ls.get("target", {}).get("deployment") == target.get("deployment")):
                return Diagnosis(DiagnosisSource.KB, Symptom.from_reason(spec["symptom"]),
                                 ls.get("rootCause", ""), ls.get("solution", ""),
                                 confidence=float(ls.get("confidence", 0.9)), auto_fixable=True,
                                 proposed_action=ls.get("action"), action_params=ls.get("params") or {})
        return None

    def _ask_llm(self, issue: PodIssue, ctx: RuleContext, ekube: EvidenceKube,
                 trace: dict) -> Optional[Diagnosis]:
        dep_name, dep = ekube.get_owner_deployment("", "")
        web = ""
        if self.config.web_search_enabled:
            query = search_query(issue, ctx)
            screened = guard.scan(self.websearch.search(query))
            web = screened.text
            trace.update(webQuery=query, guardFlags=screened.flags)
            if screened.flags:
                METRICS.inc("aiops_guard_blocked_total", amount=len(screened.flags))
        started = time.time()
        diag = self.llm.analyze(issue, LLMInput(
            logs=ctx.logs, previous_logs=ctx.previous_logs, events=ctx.events,
            deployment_yaml=json.dumps(dep.get("spec", {}), ensure_ascii=False) if dep else "",
            web_results=web,
            allowed_actions=actions.allowed_for(issue.symptom) if self.config.llm_auto_fix else ()))
        trace["llm"] = None if diag is None else {
            "rootCause": diag.root_cause, "action": diag.proposed_action, "params": diag.action_params,
            "confidence": diag.confidence, "seconds": round(time.time() - started, 1)}
        if diag is None or not diag.proposed_action or not self.config.llm_auto_fix:
            return diag
        if diag.confidence < self.config.llm_auto_fix_min_confidence:
            return diag
        try:  # early sanity check against the captured spec; repair re-checks live
            plan = actions.build_plan(diag.proposed_action, diag.action_params, issue.symptom,
                                      dep, issue.container, self.config, source="llm")
        except actions.UnsafeAction as exc:
            trace["rejected"] = str(exc)
            return diag
        if self.config.judge_enabled:
            container = container_from_deployment(dep, issue.container) or {}
            verdict = self.judge.review(issue, diag, plan.summary, events=ctx.events,
                                        logs=ctx.previous_logs or ctx.logs,
                                        spec=json.dumps(container, ensure_ascii=False))
            trace["judge"] = verdict.to_dict()
            METRICS.inc("aiops_judge_total", verdict="passed" if verdict.passed else "rejected")
            if not verdict.passed:
                return diag
        diag.auto_fixable = True
        diag.summary = f"{plan.summary} (LLM{' + web' if web else ''}: {diag.summary})"
        return diag


class DiagnoseAgent:
    def __init__(self, config: Config, store: crd.Store, decider: Decider) -> None:
        self.config = config
        self.store = store
        self.decider = decider
        self.pool = ThreadPoolExecutor(max_workers=config.diagnose_workers)
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        for verdict in ("passed", "rejected"):
            METRICS.inc("aiops_judge_total", 0, verdict=verdict)
        METRICS.inc("aiops_guard_blocked_total", 0)
        for src in ("rule", "kb", "llm", "none"):
            for auto in ("true", "false"):
                METRICS.inc("aiops_remediations_proposed_total", 0, source=src, auto=auto)

    def step(self) -> None:
        for inc in self.store.list("incidents"):
            name = inc["metadata"]["name"]
            # "" = created but its status write hasn't landed yet: still ours.
            if crd.phase(inc) not in (crd.OPEN, ""):
                continue
            with self._lock:
                if name in self._busy:
                    continue
                self._busy.add(name)
            self.pool.submit(self._run, inc)

    def _run(self, inc: dict) -> None:
        name = inc["metadata"]["name"]
        try:
            self.handle(inc)
        except Exception:
            log.exception("diagnosis failed for %s", name)
            METRICS.inc("aiops_agent_loop_errors_total", agent="diagnose")
        finally:
            with self._lock:
                self._busy.discard(name)

    def handle(self, inc: dict) -> dict:
        inc = self.store.set_status(inc, crd.DIAGNOSING, "diagnosing")
        rem_spec, trace = self.decider.decide(inc["spec"], self.store.list("lessons"),
                                              self.store.list("remediations"))
        auto = rem_spec["autoApply"]
        rem = self.store.create(
            "Remediation", rem_spec, name=inc["metadata"]["name"], owner=inc,
            labels={f"{crd.GROUP}/namespace": rem_spec["target"]["namespace"],
                    f"{crd.GROUP}/source": rem_spec["source"]},
            status={"phase": crd.PROPOSED if auto else crd.RECOMMENDED,
                    "message": rem_spec["summary"][:300]})
        self.store.set_status(inc, crd.PROPOSED if auto else crd.UNRESOLVED,
                              ("fix proposed: " if auto else "recommendation: ") + rem_spec["summary"][:200],
                              remediation=rem["metadata"]["name"])
        METRICS.inc("aiops_remediations_proposed_total", source=rem_spec["source"],
                    auto="true" if auto else "false")
        if trace:
            log.info("decision %s", json.dumps({"incident": inc["metadata"]["name"], **trace,
                                                "autoApply": auto}, ensure_ascii=False, default=str))
        log.info("%s -> %s (%s, auto=%s)", inc["metadata"]["name"], rem_spec["action"] or "recommendation",
                 rem_spec["source"], auto)
        return rem
