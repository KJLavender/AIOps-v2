"""Scans the cluster and turns abnormal pods into PodIssue objects."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config
from .kube import KubeClient
from .models import WATCHED_SYMPTOMS, PodIssue, Symptom

log = logging.getLogger("aiops.collector")

_WAITING_SYMPTOMS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "ContainerCreating",
    "CreateContainerConfigError",
}


class Collector:
    def __init__(self, kube: KubeClient, config: Config) -> None:
        self.kube = kube
        self.config = config

    def list_watched_pods(self) -> list[dict[str, Any]]:
        namespaces = [None] if self.config.all_namespaces else self.config.namespaces
        pods: list[dict[str, Any]] = []
        for ns in namespaces:
            pods.extend(self.kube.list_pods(namespace=ns))
        return pods

    def scan(self) -> list[PodIssue]:
        issues: list[PodIssue] = []
        for pod in self.list_watched_pods():
            issue = self.analyze(pod)
            if issue and issue.symptom in WATCHED_SYMPTOMS:
                issues.append(issue)
        return issues

    def _past_grace(self, meta: dict[str, Any]) -> bool:
        """New pods are briefly Pending/ContainerCreating; don't flag them yet."""
        return _older_than(meta.get("creationTimestamp"), self.config.pending_grace_seconds)

    def analyze(self, pod: dict[str, Any]) -> Optional[PodIssue]:
        meta = pod.get("metadata", {})
        status = pod.get("status", {})
        namespace = meta.get("namespace", "default")
        name = meta.get("name", "")
        phase = status.get("phase", "")

        container_statuses = status.get("containerStatuses", []) + status.get(
            "initContainerStatuses", []
        )

        for cs in container_statuses:
            cname = cs.get("name")
            restarts = cs.get("restartCount", 0)

            # OOMKilled shows up in the *last* terminated state most often.
            last_term = cs.get("lastState", {}).get("terminated", {})
            if last_term.get("reason") == "OOMKilled":
                return PodIssue(
                    namespace, name, cname, Symptom.OOM_KILLED,
                    last_term.get("message", "Container OOMKilled"),
                    phase, restarts, pod,
                )
            term = cs.get("state", {}).get("terminated", {})
            if term.get("reason") == "OOMKilled":
                return PodIssue(
                    namespace, name, cname, Symptom.OOM_KILLED,
                    term.get("message", "Container OOMKilled"),
                    phase, restarts, pod,
                )

            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")
            if reason == "ContainerCreating" and not self._past_grace(meta):
                continue  # every new pod passes through this briefly
            if reason in _WAITING_SYMPTOMS:
                return PodIssue(
                    namespace, name, cname, Symptom.from_reason(reason),
                    waiting.get("message", reason), phase, restarts, pod,
                )

            # Between restarts a crashing container sits in terminated "Error"
            # (not "waiting: CrashLoopBackOff"); catch that too.
            if (
                term.get("exitCode", 0) not in (0, None)
                and term.get("reason") != "OOMKilled"
                and restarts >= 1
            ):
                return PodIssue(
                    namespace, name, cname, Symptom.CRASH_LOOP_BACKOFF,
                    f"{term.get('reason', 'Error')}: exit {term.get('exitCode')}",
                    phase, restarts, pod,
                )

        # Running but never becomes Ready (e.g. failing readiness probe).
        for cs in status.get("containerStatuses", []):
            started = cs.get("state", {}).get("running", {}).get("startedAt")
            if (
                phase == "Running"
                and started
                and not cs.get("ready", True)
                and _age_seconds(started) >= self.config.not_ready_grace_seconds
            ):
                return PodIssue(
                    namespace, name, cs.get("name"), Symptom.NOT_READY,
                    "container running but not Ready", phase,
                    cs.get("restartCount", 0), pod,
                )

        if phase == "Pending" and self._past_grace(meta):
            return PodIssue(
                namespace, name, None, Symptom.PENDING,
                _pending_message(status), phase, 0, pod,
            )
        if phase == "Failed":
            return PodIssue(
                namespace, name, None, Symptom.FAILED,
                status.get("message", "Pod failed"), phase, 0, pod,
            )
        return None


def _older_than(timestamp: Optional[str], seconds: int) -> bool:
    return not timestamp or _age_seconds(timestamp) >= seconds  # unknown age: treat as old


def _age_seconds(timestamp: str) -> float:
    try:
        started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - started).total_seconds()


def _pending_message(status: dict[str, Any]) -> str:
    for cond in status.get("conditions", []):
        if cond.get("type") == "PodScheduled" and cond.get("status") != "True":
            return cond.get("message", "Pod pending / unschedulable")
    return "Pod pending"
