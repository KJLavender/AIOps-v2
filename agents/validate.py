"""Validate agent: checks applied fixes and records what worked.

Permissions: read Deployments / ReplicaSets / pods in the watched namespaces;
update Remediations/Incidents; create Lessons. Validations run in parallel, so
a slow rollout never stalls detection or other repairs (the v1 agent did all
of this in one sequential loop).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from aiops import crd
from aiops.config import Config
from aiops.kube import KubeClient
from aiops.metrics import METRICS
from aiops.validation import Validator

log = logging.getLogger("agents.validate")


def lesson_name(spec: dict) -> str:
    t = spec["target"]
    return "lesson-" + crd.short_hash(f"{t['namespace']}/{t['deployment']}/{spec['symptom']}")


class ValidateAgent:
    def __init__(self, config: Config, kube: KubeClient, store: crd.Store) -> None:
        self.config = config
        self.store = store
        self.validator = Validator(kube, config)
        self.pool = ThreadPoolExecutor(max_workers=config.validate_workers)
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        for p in ("validated", "failed"):
            for src in ("rule", "kb", "llm"):
                METRICS.inc("aiops_remediations_total", 0, phase=p, source=src)

    def step(self) -> None:
        for rem in self.store.list("remediations"):
            name = rem["metadata"]["name"]
            if crd.phase(rem) != crd.APPLIED:
                continue
            with self._lock:
                if name in self._busy:
                    continue
                self._busy.add(name)
            self.pool.submit(self._run, rem)

    def _run(self, rem: dict) -> None:
        try:
            self.check(rem)
        except Exception:
            log.exception("validation crashed for %s", rem["metadata"]["name"])
        finally:
            with self._lock:
                self._busy.discard(rem["metadata"]["name"])

    def check(self, rem: dict) -> bool:
        spec = rem["spec"]
        target = SimpleNamespace(target_namespace=spec["target"]["namespace"],
                                 target_name=spec["target"]["deployment"])
        result = self.validator.validate(target)
        inc = self.store.get("incidents", rem["metadata"]["name"])
        if result.success:
            self.store.set_status(rem, crd.VALIDATED, result.detail)
            if inc:
                self.store.set_status(inc, crd.RESOLVED, f"fixed: {spec['summary'][:200]}")
            self.learn(spec)
            METRICS.inc("aiops_remediations_total", phase="validated", source=spec.get("source", ""))
            log.info("validated %s (%s)", rem["metadata"]["name"], result.detail)
            return True
        # Hand back to the repair agent, which owns rollbacks.
        self.store.set_status(rem, crd.FAILED, result.detail)
        METRICS.inc("aiops_remediations_total", phase="failed", source=spec.get("source", ""))
        log.info("validation failed for %s: %s", rem["metadata"]["name"], result.detail)
        return False

    def learn(self, spec: dict) -> None:
        """One Lesson per workload + symptom, replaced by the latest verified fix."""
        self.store.apply("Lesson", lesson_name(spec), {
            "symptom": spec["symptom"],
            "target": {"namespace": spec["target"]["namespace"], "deployment": spec["target"]["deployment"]},
            "action": spec["action"],
            "params": spec.get("params") or {},
            "rootCause": spec.get("rootCause", ""),
            "solution": spec.get("summary", ""),
            "confidence": spec.get("confidence", 0.9),
            "learnedFrom": spec.get("source", ""),
            "verifiedAt": crd.now(),
        }, labels={f"{crd.GROUP}/namespace": spec["target"]["namespace"]})
