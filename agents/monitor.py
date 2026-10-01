"""Monitor agent: notices failing pods and opens Incidents.

Permissions: read pods / logs / events / Deployments in the watched
namespaces; create Incidents. It never changes a workload.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiops import crd, evidence
from aiops.collector import Collector
from aiops.config import Config
from aiops.kube import KubeClient
from aiops.metrics import METRICS
from aiops.models import PodIssue

log = logging.getLogger("agents.monitor")


def issue_key(namespace: str, workload: str, symptom: str) -> str:
    return f"{namespace}/{workload}/{symptom}"


def fingerprint(issue: PodIssue, deployment: Optional[dict]) -> str:
    """Changes when someone changes the workload (Deployment generation), not on every restart."""
    generation = (deployment or {}).get("metadata", {}).get("generation", issue.pod)
    return crd.short_hash(f"{issue.namespace}/{generation}/{issue.symptom.value}")


class Monitor:
    def __init__(self, config: Config, kube: KubeClient, store: crd.Store) -> None:
        self.config = config
        self.kube = kube
        self.store = store
        self.collector = Collector(kube, config)

    def step(self) -> None:
        incidents = self.store.list("incidents")
        latest: dict[str, dict] = {}
        for inc in sorted(incidents, key=lambda i: i["metadata"]["creationTimestamp"]):
            latest[inc["spec"]["key"]] = inc
        open_count = sum(1 for i in incidents if crd.phase(i) in crd.ACTIVE_INCIDENT)
        METRICS.set("aiops_incidents_open", open_count)
        METRICS.set("aiops_lessons", len(self.store.list("lessons")))

        for issue in self.collector.scan():
            deployment_name, deployment = self.kube.get_owner_deployment(issue.namespace, issue.pod)
            workload = deployment_name or issue.pod
            key = issue_key(issue.namespace, workload, issue.symptom.value)
            fp = fingerprint(issue, deployment)
            if not self._should_open(latest.get(key), fp):
                continue
            self.open_incident(issue, key, deployment_name, deployment, fp)
        self.prune(incidents)

    def _should_open(self, last: Optional[dict], fp: str = "") -> bool:
        if last is None:
            return True
        if crd.phase(last) in crd.ACTIVE_INCIDENT or crd.phase(last) == "":
            return False  # someone is already on it (or about to be)
        updated = (last.get("status") or {}).get("updatedAt") or last["metadata"]["creationTimestamp"]
        age = crd.age_seconds(updated)
        if age < self.config.incident_cooldown_seconds:
            return False
        # Unresolved and nothing changed since (same Deployment generation):
        # the same diagnosis would come out, so only look again rarely.
        if crd.phase(last) == crd.UNRESOLVED and last["spec"].get("fingerprint") == fp:
            return age >= self.config.unresolved_recheck_hours * 3600
        return True

    def open_incident(self, issue: PodIssue, key: str, deployment_name: Optional[str],
                      deployment: Optional[dict], fp: str = "") -> dict:
        spec = {
            "key": key,
            "fingerprint": fp,
            "symptom": issue.symptom.value,
            "message": evidence.redact(issue.message)[:500],
            "restartCount": issue.restart_count,
            "target": {"namespace": issue.namespace, "pod": issue.pod,
                       "container": issue.container or "", "deployment": deployment_name or ""},
            "evidence": evidence.collect(self.kube, issue, deployment_name, deployment, self.config),
        }
        labels = {f"{crd.GROUP}/namespace": issue.namespace,
                  f"{crd.GROUP}/symptom": issue.symptom.value,
                  f"{crd.GROUP}/key": crd.short_hash(key)}
        inc = self.store.create("Incident", spec, generate_name="inc-", labels=labels,
                                status={"phase": crd.OPEN, "message": spec["message"][:200]})
        METRICS.inc("aiops_incidents_created_total", namespace=issue.namespace, symptom=issue.symptom.value)
        log.info("opened %s for %s (%s)", inc["metadata"]["name"], key, spec["message"][:80])
        return inc

    def prune(self, incidents: list[dict]) -> None:
        """Finished Incidents older than the retention go (Remediations follow via ownerRefs)."""
        limit = self.config.retention_days * 86400
        for inc in incidents:
            if crd.phase(inc) in crd.ACTIVE_INCIDENT:
                continue
            if crd.age_seconds((inc.get("status") or {}).get("updatedAt")) > limit:
                self.store.delete("incidents", inc["metadata"]["name"])
