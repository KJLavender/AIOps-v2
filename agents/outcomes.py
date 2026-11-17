"""Outcomes: what each finished Remediation's scores were, and how it turned out.

The LLM's confidence and the judge's scores are self-reported and not
calibrated. To calibrate them (or just to know how often a 0.9 is right), the
scores have to be stored next to the real result. Incidents and Remediations
are pruned after AIOPS_RETENTION_DAYS, so the monitor copies each finished
Remediation into an Outcome that is kept for AIOPS_OUTCOME_RETENTION_DAYS.

    python -m agents.outcomes            # summary from the cluster
    python -m agents.outcomes --json     # the raw records
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from aiops import crd
from aiops.config import Config
from aiops.kube import KubectlError
from aiops.metrics import METRICS

log = logging.getLogger("agents.outcomes")

# Finished Remediation phase -> (result, was it applied, did it work)
_RESULTS = {
    crd.VALIDATED: ("validated", True, True),
    crd.ROLLED_BACK: ("rolled_back", True, False),
    crd.REJECTED: ("rejected", False, None),
    crd.RECOMMENDED: ("recommended", False, None),
}
BUCKETS = ((0.0, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


def outcome_spec(rem: dict) -> Optional[dict]:
    """The Outcome record for a finished Remediation, or None if it isn't finished."""
    result = _RESULTS.get(crd.phase(rem))
    if result is None:
        return None
    spec, status = rem["spec"], rem.get("status") or {}
    evaluation = spec.get("evaluation") or {}
    judge = evaluation.get("judge") or {}
    phases = [h.get("phase") for h in status.get("history") or []]
    if crd.APPLIED in phases and not result[1]:  # a rollback that itself failed ends Rejected
        result = (result[0], True, False)
    return {
        "remediation": rem["metadata"]["name"],
        "symptom": spec.get("symptom", ""),
        "target": spec.get("target", {}),
        "source": spec.get("source", ""),
        "action": spec.get("action"),
        "params": spec.get("params") or {},
        "confidence": spec.get("confidence"),
        "judgeScores": judge.get("scores") or {},
        "judgePassed": judge.get("passed"),
        "guardFlags": len(evaluation.get("guardFlags") or []),
        "approval": ("approved" if status.get("approvedBy") else
                     "asked" if crd.AWAITING_APPROVAL in phases else "not needed"),
        "result": result[0],
        "applied": result[1],
        "worked": result[2],
        "reason": status.get("message", "")[:300],
        "decidedAt": rem["metadata"].get("creationTimestamp"),
        "finishedAt": status.get("updatedAt"),
    }


def record(store: crd.Store, remediations: list[dict], known: set[str]) -> int:
    """Create an Outcome for each newly finished Remediation; returns how many."""
    added = 0
    for rem in remediations:
        name = rem["metadata"]["name"]
        if name in known:
            continue
        spec = outcome_spec(rem)
        if spec is None:
            continue
        try:
            store.create("Outcome", spec, name=name,
                         labels={f"{crd.GROUP}/source": spec["source"], f"{crd.GROUP}/result": spec["result"]})
        except KubectlError as exc:
            if "AlreadyExists" not in str(exc):
                raise
        known.add(name)
        added += 1
        METRICS.inc("aiops_outcomes_total", source=spec["source"], result=spec["result"])
    return added


def prune(store: crd.Store, outcomes: list[dict], retention_days: int) -> None:
    for out in outcomes:
        if crd.age_seconds(out["metadata"].get("creationTimestamp")) > retention_days * 86400:
            store.delete("outcomes", out["metadata"]["name"])


def summarize(records: list[dict]) -> dict:
    """Counts by source and result, plus how well the LLM's confidence predicted success."""
    by_source: dict[str, dict] = {}
    for r in records:
        s = by_source.setdefault(r.get("source") or "none", {"total": 0, "applied": 0, "worked": 0})
        s["total"] += 1
        s["applied"] += bool(r.get("applied"))
        s["worked"] += r.get("worked") is True
    applied_llm = [r for r in records if r.get("source") == "llm" and r.get("applied")
                   and isinstance(r.get("confidence"), (int, float))]
    buckets = []
    for lo, hi in BUCKETS:
        rows = [r for r in applied_llm if lo <= r["confidence"] < hi]
        buckets.append({"confidence": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(rows),
                        "worked": sum(r["worked"] is True for r in rows)})
    brier = (round(sum((r["confidence"] - (1.0 if r["worked"] else 0.0)) ** 2 for r in applied_llm)
                   / len(applied_llm), 3) if applied_llm else None)
    return {"total": len(records), "bySource": by_source, "llmApplied": len(applied_llm),
            "llmBuckets": buckets, "llmBrier": brier}


def main(argv: list[str]) -> int:
    from aiops.kube import KubeClient
    config = Config.from_env()
    store = crd.Store(KubeClient(config), config.system_namespace)
    records = [o["spec"] for o in store.list("outcomes")]
    if "--json" in argv:
        print(json.dumps(records, ensure_ascii=False, indent=1))
        return 0
    s = summarize(records)
    print(f"{s['total']} outcomes")
    for source, c in sorted(s["bySource"].items()):
        print(f"  {source:<6} total {c['total']:>3}  applied {c['applied']:>3}  worked {c['worked']:>3}")
    print(f"LLM fixes that were applied: {s['llmApplied']}  (Brier score {s['llmBrier']}, lower is better)")
    for b in s["llmBuckets"]:
        rate = f"{b['worked'] / b['n']:.0%}" if b["n"] else "-"
        print(f"  confidence {b['confidence']}: {b['n']:>3} applied, worked {rate}")
    if s["llmApplied"] < 30:
        print("Too few applied LLM fixes to calibrate yet; the table is for watching, not for tuning thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
