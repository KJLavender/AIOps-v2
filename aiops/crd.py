"""Incidents, Remediations and Lessons as Kubernetes custom resources.

The agents never call each other. They hand work over through these objects,
each moving its own phase forward:

    Incident     monitor creates  -> diagnose -> repair / validate close it
    Remediation  diagnose creates -> repair applies -> validate checks -> repair rolls back
    Lesson       validate upserts a verified fix -> diagnose reuses it

Every transition is appended to status.history, so `kubectl describe` shows
the whole story. The phase field is the whole coordination protocol: each
agent only picks up objects in the phases it owns.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .kube import KubeClient, KubectlError

log = logging.getLogger("aiops.crd")

GROUP = "aiops.homelab.dev"
VERSION = "v1alpha1"
API_VERSION = f"{GROUP}/{VERSION}"

# Incident phases
OPEN, DIAGNOSING, PROPOSED, REMEDIATING = "Open", "Diagnosing", "Proposed", "Remediating"
RESOLVED, UNRESOLVED = "Resolved", "Unresolved"
ACTIVE_INCIDENT = {OPEN, DIAGNOSING, PROPOSED, REMEDIATING}

# Remediation phases (PROPOSED shared)
RECOMMENDED, REJECTED, APPLYING, APPLIED = "Recommended", "Rejected", "Applying", "Applied"
VALIDATED, FAILED, ROLLED_BACK = "Validated", "Failed", "RolledBack"
FINISHED_REMEDIATION = {RECOMMENDED, REJECTED, VALIDATED, ROLLED_BACK}

_HISTORY_LIMIT = 20


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def age_seconds(timestamp: Optional[str]) -> float:
    if not timestamp:
        return float("inf")
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - then).total_seconds()


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def phase(obj: dict) -> str:
    return (obj.get("status") or {}).get("phase", "")


class Store:
    def __init__(self, kube: KubeClient, namespace: str) -> None:
        self.kube = kube
        self.namespace = namespace

    def list(self, plural: str, selector: Optional[str] = None) -> list[dict]:
        args = ["get", f"{plural}.{GROUP}", "-n", self.namespace, "-o", "json"]
        if selector:
            args += ["-l", selector]
        return self.kube.run_json(args).get("items", [])

    def get(self, plural: str, name: str) -> Optional[dict]:
        try:
            return self.kube.run_json(["get", f"{plural}.{GROUP}", name, "-n", self.namespace, "-o", "json"])
        except KubectlError:
            return None

    def create(self, kind: str, spec: dict, *, name: Optional[str] = None,
               generate_name: Optional[str] = None, labels: Optional[dict] = None,
               owner: Optional[dict] = None, status: Optional[dict] = None) -> dict:
        meta: dict[str, Any] = {"namespace": self.namespace, "labels": labels or {}}
        if name:
            meta["name"] = name
        else:
            meta["generateName"] = generate_name or kind.lower() + "-"
        if owner:
            meta["ownerReferences"] = [{
                "apiVersion": API_VERSION, "kind": owner["kind"], "name": owner["metadata"]["name"],
                "uid": owner["metadata"]["uid"], "controller": True, "blockOwnerDeletion": False,
            }]
        body = {"apiVersion": API_VERSION, "kind": kind, "metadata": meta, "spec": spec}
        rc, out, err = self.kube.run(["create", "-f", "-", "-o", "json"], input=json.dumps(body))
        if rc != 0:
            raise KubectlError(err.strip() or out.strip())
        obj = json.loads(out)
        if status:
            obj = self.set_status(obj, status.pop("phase"), status.pop("message", ""), **status)
        return obj

    def apply(self, kind: str, name: str, spec: dict, labels: Optional[dict] = None) -> None:
        """Create-or-replace (used for Lessons: one per workload + symptom)."""
        body = {"apiVersion": API_VERSION, "kind": kind,
                "metadata": {"name": name, "namespace": self.namespace, "labels": labels or {}},
                "spec": spec}
        rc, out, err = self.kube.run(["apply", "-f", "-"], input=json.dumps(body))
        if rc != 0:
            raise KubectlError(err.strip() or out.strip())

    def set_status(self, obj: dict, new_phase: str, message: str = "", **fields) -> dict:
        """Move obj to new_phase, keeping an audit trail in status.history."""
        status = dict(obj.get("status") or {})
        history = list(status.get("history") or [])
        history.append({"time": now(), "phase": new_phase, "message": message[:300]})
        status.update(fields, phase=new_phase, message=message[:500], updatedAt=now(),
                      history=history[-_HISTORY_LIMIT:])
        plural = obj["kind"].lower() + "s"
        rc, out, err = self.kube.run([
            "patch", f"{plural}.{GROUP}", obj["metadata"]["name"], "-n", self.namespace,
            "--subresource=status", "--type=merge", "-p", json.dumps({"status": status}),
            "-o", "json"])
        if rc != 0:
            raise KubectlError(err.strip() or out.strip())
        return json.loads(out)

    def delete(self, plural: str, name: str) -> None:
        self.kube.run(["delete", f"{plural}.{GROUP}", name, "-n", self.namespace, "--wait=false"])
