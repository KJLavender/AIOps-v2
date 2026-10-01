"""Bounded catalog of fixes - the only changes the platform can make.

Nobody hands the repair agent a patch. A Remediation names an action plus a
few parameters; the repair agent rebuilds the patch here from the *live*
Deployment, checking every parameter against hard limits and operator
allowlists. An LLM (fed with untrusted web results) may pick from a narrower
list than the deterministic rules, and web content that tries to steer the
model ("set image to ...", "add a privileged sidecar") has no action to map
onto.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config
from .kube import container_from_deployment
from .models import Symptom
from .patches import env_patch, image_patch, memory_limit_patch
from .util import image_repository, parse_memory_to_bytes

# Only actions that can plausibly fix the symptom. A deterministic crash loop is
# absent on purpose: kubelet already restarts it, and a new image or env value
# can't be guessed safely.
ALLOWED_BY_SYMPTOM: dict[Symptom, tuple[str, ...]] = {
    Symptom.NOT_READY: ("set_readiness_probe_path", "rollout_restart"),
    Symptom.PENDING: ("lower_requests",),
    Symptom.OOM_KILLED: ("set_memory_limit",),
}

# Deterministic sources (rules, verified lessons) may also swap an image or set
# an env var - but only to operator-approved values, re-checked at repair time.
RULE_ALLOWED_BY_SYMPTOM: dict[Symptom, tuple[str, ...]] = {
    **ALLOWED_BY_SYMPTOM,
    Symptom.IMAGE_PULL_BACKOFF: ("set_image",),
    Symptom.ERR_IMAGE_PULL: ("set_image",),
    Symptom.CRASH_LOOP_BACKOFF: ("set_env",),
}

_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,100}$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")


class UnsafeAction(ValueError):
    pass


@dataclass
class ActionPlan:
    action: str
    patch: dict[str, Any]
    summary: str


def allowed_for(symptom: Symptom, source: str = "llm") -> tuple[str, ...]:
    table = ALLOWED_BY_SYMPTOM if source == "llm" else RULE_ALLOWED_BY_SYMPTOM
    return table.get(symptom, ())


def build_plan(
    action: str,
    params: dict[str, Any],
    symptom: Symptom,
    deployment: Optional[dict[str, Any]],
    container_name: Optional[str],
    config: Config,
    source: str = "llm",
) -> ActionPlan:
    if action not in allowed_for(symptom, source):
        raise UnsafeAction(f"action '{action}' is not allowed for {symptom.value} ({source})")
    if not deployment:
        raise UnsafeAction("no owning Deployment to patch")
    container = container_from_deployment(deployment, container_name)
    if not container:
        raise UnsafeAction("container not found in Deployment")
    name = container["name"]
    params = params or {}

    if action == "rollout_restart":
        stamp = datetime.now(timezone.utc).isoformat()
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": stamp}}}}}
        return ActionPlan(action, patch, "Restart the Deployment's pods")

    if action == "set_readiness_probe_path":
        probe = (container.get("readinessProbe") or {}).get("httpGet")
        if not probe:
            raise UnsafeAction("container has no HTTP readiness probe")
        path = str(params.get("path") or "")
        if not _PATH_RE.match(path):
            raise UnsafeAction(f"invalid probe path {path!r}")
        if path == probe.get("path"):
            raise UnsafeAction("probe already uses that path")
        patch = _container_patch(name, {"readinessProbe": {"httpGet": {"path": path}}})
        return ActionPlan(action, patch,
                          f"Change {name} readiness probe path {probe.get('path')} -> {path}")

    if action == "set_memory_limit":
        memory = str(params.get("memory") or "")
        try:
            new = parse_memory_to_bytes(memory)
        except (ValueError, TypeError):
            raise UnsafeAction(f"invalid memory {memory!r}") from None
        current = (container.get("resources") or {}).get("limits", {}).get("memory")
        if current and new <= parse_memory_to_bytes(current):
            raise UnsafeAction(f"{memory} is not above the current limit {current}")
        if new > parse_memory_to_bytes(config.max_memory_limit):
            raise UnsafeAction(f"{memory} exceeds the cap {config.max_memory_limit}")
        return ActionPlan(action, memory_limit_patch(name, memory),
                          f"Raise {name} memory limit to {memory}")

    if action == "lower_requests":
        current = (container.get("resources") or {}).get("requests") or {}
        new: dict[str, str] = {}
        for key, parse in (("memory", parse_memory_to_bytes), ("cpu", _parse_cpu)):
            if params.get(key) in (None, ""):
                continue
            value = str(params[key])
            try:
                wanted = parse(value)
            except (ValueError, TypeError):
                raise UnsafeAction(f"invalid {key} {value!r}") from None
            if key not in current:
                raise UnsafeAction(f"no {key} request to lower")
            if not 0 < wanted < parse(str(current[key])):
                raise UnsafeAction(f"{key} {value} is not lower than {current[key]}")
            new[key] = value
        if not new:
            raise UnsafeAction("no request to lower")
        patch = _container_patch(name, {"resources": {"requests": new}})
        return ActionPlan(action, patch, f"Lower {name} requests to {new}")

    if action == "set_image":
        image = str(params.get("image") or "")
        current = container.get("image", "")
        approved = config.image_fallbacks.get(image_repository(current))
        if not approved or image != approved:
            raise UnsafeAction(f"{image!r} is not the approved fallback for {current!r}")
        if image == current:
            raise UnsafeAction("container already runs that image")
        return ActionPlan(action, image_patch(name, image), f"Switch {name} image {current} -> {image}")

    if action == "set_env":
        var, value = str(params.get("name") or ""), params.get("value")
        if var not in config.env_defaults or value != config.env_defaults[var]:
            raise UnsafeAction(f"{var}={value!r} is not an approved env default")
        return ActionPlan(action, env_patch(name, var, str(value)), f"Set {name} env {var}={value}")

    raise UnsafeAction(f"unknown action '{action}'")


def _parse_cpu(value: str) -> float:
    m = _CPU_RE.match(value.strip())
    if not m:
        raise ValueError(value)
    return float(m.group(1)) / (1000 if m.group(2) else 1)


def _container_patch(name: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {"containers": [{"name": name, **body}]}}}}
