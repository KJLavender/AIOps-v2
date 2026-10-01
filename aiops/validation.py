"""Validates a fix: pods must become Running & Ready and stay stable."""
from __future__ import annotations

import logging
import time

from .config import Config
from .kube import KubeClient, deployment_selector, pod_is_ready
from .models import Diagnosis, ValidationResult

log = logging.getLogger("aiops.validation")


class Validator:
    def __init__(self, kube: KubeClient, config: Config) -> None:
        self.kube = kube
        self.config = config

    def validate(self, diagnosis: Diagnosis) -> ValidationResult:
        namespace = diagnosis.target_namespace
        deployment = diagnosis.target_name
        if not (namespace and deployment):
            return ValidationResult(False, "no deployment target to validate")

        rc, out, err = self.kube.rollout_status(
            namespace, deployment, self.config.validation_timeout_seconds
        )
        if rc != 0:
            return ValidationResult(False, f"rollout did not complete: {(err or out).strip()}")

        selector = self._selector(namespace, deployment)
        # Give the freshly rolled-out pods time to actually reach Ready.
        if not self._wait_ready(namespace, selector):
            return ValidationResult(False, "pods not Ready after rollout")

        # Stability window: pods must stay Ready (success = Running & Ready, held).
        deadline = time.time() + self.config.validation_stability_seconds
        while time.time() < deadline:
            if not self._pods_ready(namespace, selector):
                return ValidationResult(False, "pods became NotReady during stability window")
            time.sleep(self.config.validation_poll_seconds)

        return ValidationResult(True, "Running & Ready & stable")

    def _wait_ready(self, namespace: str, selector: str) -> bool:
        deadline = time.time() + self.config.validation_timeout_seconds
        while True:
            if self._pods_ready(namespace, selector):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(self.config.validation_poll_seconds)

    def _selector(self, namespace: str, deployment: str) -> str:
        try:
            dep = self.kube.get("deployment", deployment, namespace)
        except Exception:
            return f"app={deployment}"
        return deployment_selector(dep) or f"app={deployment}"

    def _pods_ready(self, namespace: str, selector: str) -> bool:
        # Ignore old pods that are already terminating during the rollout.
        pods = [
            p
            for p in self.kube.list_pods(namespace=namespace, selector=selector)
            if not p.get("metadata", {}).get("deletionTimestamp")
        ]
        if not pods:
            return False
        return all(pod_is_ready(p) for p in pods)
