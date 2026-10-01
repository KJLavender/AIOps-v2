"""Thin wrapper around the `kubectl` binary (matches the spec's commands)."""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

from .config import Config

log = logging.getLogger("aiops.kube")


class KubectlError(RuntimeError):
    pass


class KubeClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    # --- low level ---------------------------------------------------------
    def run(
        self, args: list[str], check: bool = False, timeout: int = 120,
        input: Optional[str] = None,
    ) -> tuple[int, str, str]:
        cmd = [self.config.kubectl_bin, *args]
        log.debug("exec: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, input=input
            )
        except FileNotFoundError as exc:
            raise KubectlError(
                f"'{self.config.kubectl_bin}' not found. Install kubectl or set "
                "AIOPS_KUBECTL_BIN."
            ) from exc
        if check and proc.returncode != 0:
            raise KubectlError(proc.stderr.strip() or proc.stdout.strip())
        return proc.returncode, proc.stdout, proc.stderr

    def run_json(self, args: list[str]) -> dict[str, Any]:
        rc, out, err = self.run(args)
        if rc != 0:
            raise KubectlError(err.strip() or out.strip())
        return json.loads(out) if out.strip() else {}

    # --- reads -------------------------------------------------------------
    def list_pods(
        self, namespace: Optional[str] = None, selector: Optional[str] = None
    ) -> list[dict[str, Any]]:
        args = ["get", "pods", "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        elif self.config.all_namespaces:
            args += ["-A"]
        if selector:
            args += ["-l", selector]
        return self.run_json(args).get("items", [])

    def get(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        return self.run_json(["get", kind, name, "-n", namespace, "-o", "json"])

    def describe_pod(self, namespace: str, pod: str) -> str:
        _, out, _ = self.run(["describe", "pod", pod, "-n", namespace])
        return out

    def pod_logs(
        self,
        namespace: str,
        pod: str,
        container: Optional[str] = None,
        previous: bool = False,
    ) -> str:
        args = ["logs", pod, "-n", namespace, f"--tail={self.config.log_tail_lines}"]
        if container:
            args += ["-c", container]
        if previous:
            args += ["--previous"]
        _, out, err = self.run(args)
        return out or err

    def pod_events(self, namespace: str, pod: str) -> str:
        _, out, _ = self.run(
            [
                "get",
                "events",
                "-n",
                namespace,
                "--field-selector",
                f"involvedObject.name={pod}",
                "--sort-by=.lastTimestamp",
            ]
        )
        return out

    def get_owner_deployment(
        self, namespace: str, pod: str
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """Resolve Pod -> ReplicaSet -> Deployment via ownerReferences."""
        try:
            pod_obj = self.get("pod", pod, namespace)
        except KubectlError:
            return None, None
        rs_ref = _find_owner(pod_obj, "ReplicaSet")
        if not rs_ref:
            return None, None
        try:
            rs_obj = self.get("replicaset", rs_ref, namespace)
        except KubectlError:
            return None, None
        dep_ref = _find_owner(rs_obj, "Deployment")
        if not dep_ref:
            return None, None
        try:
            return dep_ref, self.get("deployment", dep_ref, namespace)
        except KubectlError:
            return dep_ref, None

    # --- writes ------------------------------------------------------------
    def patch(
        self,
        kind: str,
        name: str,
        namespace: str,
        patch: dict[str, Any],
        patch_type: str = "strategic",
        dry_run: bool = False,
    ) -> tuple[int, str, str]:
        args = [
            "patch",
            kind,
            name,
            "-n",
            namespace,
            "--type",
            patch_type,
            "-p",
            json.dumps(patch),
        ]
        if dry_run:
            args += ["--dry-run=server"]
        return self.run(args)

    def rollout_undo(self, namespace: str, deployment: str) -> tuple[int, str, str]:
        return self.run(["rollout", "undo", f"deployment/{deployment}", "-n", namespace])

    def rollout_status(
        self, namespace: str, deployment: str, timeout_seconds: int
    ) -> tuple[int, str, str]:
        return self.run(
            [
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                namespace,
                f"--timeout={timeout_seconds}s",
            ],
            # Leave kubectl room to report its own timeout; killing it first
            # raised TimeoutExpired and skipped the rollback path.
            timeout=timeout_seconds + 30,
        )


def _find_owner(obj: dict[str, Any], kind: str) -> Optional[str]:
    for owner in obj.get("metadata", {}).get("ownerReferences", []):
        if owner.get("kind") == kind:
            return owner.get("name")
    return None


def deployment_selector(deployment: dict[str, Any]) -> Optional[str]:
    """Build an `-l k=v,k2=v2` selector string from a Deployment spec."""
    labels = (
        deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    )
    if not labels:
        return None
    return ",".join(f"{k}={v}" for k, v in labels.items())


def container_from_deployment(
    deployment: dict[str, Any], container_name: Optional[str]
) -> Optional[dict[str, Any]]:
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if container_name:
        for c in containers:
            if c.get("name") == container_name:
                return c
    return containers[0] if containers else None


def pod_is_ready(pod: dict[str, Any]) -> bool:
    if pod.get("status", {}).get("phase") != "Running":
        return False
    for cond in pod.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False
