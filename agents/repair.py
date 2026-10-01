"""Repair agent: the only agent that changes workloads.

Permissions: get/patch Deployments, list ReplicaSets (rollout undo) and pods
(pre-check) in the watched namespaces; update Remediations/Incidents. Network
policy gives it no route to the internet or to the LLM.

It never applies a patch someone else wrote. For each proposal it re-reads the
live Deployment, rebuilds the change from (action, params) through the
catalog - re-checking limits and operator allowlists - pre-checks a new probe
path on the running pod, and only then patches. Failed validations come back
here for `kubectl rollout undo`.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Optional

from aiops import actions, crd
from aiops.config import Config
from aiops.kube import KubeClient, KubectlError, container_from_deployment, deployment_selector
from aiops.metrics import METRICS
from aiops.models import Symptom

log = logging.getLogger("agents.repair")


def http_status(url: str) -> Optional[int]:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None


class Repairer:
    def __init__(self, config: Config, kube: KubeClient, store: crd.Store) -> None:
        self.config = config
        self.kube = kube
        self.store = store
        self.http_status = http_status
        for p in ("applied", "rejected", "rolled_back"):  # start at 0 so increase() sees the first
            for src in ("rule", "kb", "llm"):
                METRICS.inc("aiops_remediations_total", 0, phase=p, source=src)

    def step(self) -> None:
        remediations = self.store.list("remediations")
        for rem in remediations:
            if crd.phase(rem) == crd.PROPOSED and rem["spec"].get("autoApply"):
                self.apply(rem, remediations)
            elif crd.phase(rem) == crd.FAILED:
                self.rollback(rem)

    # --- apply ---------------------------------------------------------------
    def plan(self, spec: dict, deployment: dict) -> actions.ActionPlan:
        """Rebuild the patch from the proposal against the live Deployment (raises UnsafeAction)."""
        source = "llm" if spec.get("source") == "llm" else "rule"
        return actions.build_plan(spec["action"], spec.get("params") or {}, Symptom.from_reason(spec["symptom"]),
                                  deployment, spec["target"].get("container") or None, self.config, source)

    def precheck(self, spec: dict, plan: actions.ActionPlan, deployment: dict) -> Optional[str]:
        """A new readiness path must answer on a running pod before we switch to it."""
        if plan.action != "set_readiness_probe_path" or not self.config.action_precheck:
            return None
        container = container_from_deployment(deployment, spec["target"].get("container") or None) or {}
        probe = (container.get("readinessProbe") or {}).get("httpGet") or {}
        port = probe.get("port")
        if isinstance(port, str):
            port = next((p.get("containerPort") for p in container.get("ports", []) if p.get("name") == port), None)
        path = spec["params"].get("path", "")
        pods = self.kube.list_pods(namespace=spec["target"]["namespace"],
                                   selector=deployment_selector(deployment))
        ips = [p.get("status", {}).get("podIP") for p in pods
               if p.get("status", {}).get("phase") == "Running" and p.get("status", {}).get("podIP")]
        if not (ips and port):
            return "no running pod to verify the new path on"
        status = self.http_status(f"{(probe.get('scheme') or 'HTTP').lower()}://{ips[0]}:{port}{path}")
        if status is None or status >= 400:
            return f"new path {path} answered {status or 'nothing'} on the pod"
        return None

    def apply(self, rem: dict, all_remediations: list[dict]) -> None:
        spec = rem["spec"]
        target = spec["target"]
        rem = self.store.set_status(rem, crd.APPLYING, "re-checking the proposal against the live cluster")
        reason = self._refuse(spec, rem, all_remediations)
        deployment = plan = None
        if reason is None:
            try:
                deployment = self.kube.get("deployment", target["deployment"], target["namespace"])
                plan = self.plan(spec, deployment)
                reason = self.precheck(spec, plan, deployment)
            except actions.UnsafeAction as exc:
                reason = str(exc)
            except KubectlError as exc:
                reason = f"cannot read the Deployment: {exc}"
        if reason is None and self.config.dry_run:
            reason = "dry-run: not applied"
        if reason is not None:
            self._reject(rem, reason)
            return
        revision = deployment["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "")
        rc, out, err = self.kube.patch("deployment", target["deployment"], target["namespace"], plan.patch)
        if rc != 0:
            self._reject(rem, f"patch failed: {(err or out).strip()}")
            return
        self.store.set_status(rem, crd.APPLIED, plan.summary, revisionBefore=revision, appliedPatch=plan.patch)
        self._incident(rem, crd.REMEDIATING, f"applied: {plan.summary}")
        METRICS.inc("aiops_remediations_total", phase="applied", source=spec.get("source", ""))
        log.info("applied %s: %s", rem["metadata"]["name"], plan.summary)

    def _refuse(self, spec: dict, rem: dict, all_remediations: list[dict]) -> Optional[str]:
        if not self.config.auto_fix:
            return "auto-fix is disabled"
        if not spec.get("action") or not spec["target"].get("deployment"):
            return "no action or no Deployment to change"
        for other in all_remediations:  # same fix already rolled back once: never again
            if other["metadata"]["name"] == rem["metadata"]["name"] or crd.phase(other) != crd.ROLLED_BACK:
                continue
            o = other["spec"]
            if (o["target"] == spec["target"] and o.get("action") == spec["action"]
                    and (o.get("params") or {}) == (spec.get("params") or {})):
                return "this exact fix was rolled back before"
        return None

    def _reject(self, rem: dict, reason: str) -> None:
        self.store.set_status(rem, crd.REJECTED, reason)
        self._incident(rem, crd.UNRESOLVED, f"fix refused: {reason}")
        METRICS.inc("aiops_remediations_total", phase="rejected", source=rem["spec"].get("source", ""))
        log.info("refused %s: %s", rem["metadata"]["name"], reason)

    # --- rollback ------------------------------------------------------------
    def rollback(self, rem: dict) -> None:
        target = rem["spec"]["target"]
        rc, out, err = self.kube.rollout_undo(target["namespace"], target["deployment"])
        detail = (out if rc == 0 else err or out).strip()
        self.store.set_status(rem, crd.ROLLED_BACK if rc == 0 else crd.REJECTED,
                              f"rollback: {detail}"[:300])
        self._incident(rem, crd.UNRESOLVED, f"fix failed validation and was rolled back ({detail[:80]})")
        METRICS.inc("aiops_remediations_total", phase="rolled_back", source=rem["spec"].get("source", ""))
        log.info("rolled back %s: %s", rem["metadata"]["name"], detail)

    def _incident(self, rem: dict, new_phase: str, message: str) -> None:
        inc = self.store.get("incidents", rem["metadata"]["name"])
        if inc:
            self.store.set_status(inc, new_phase, message)
