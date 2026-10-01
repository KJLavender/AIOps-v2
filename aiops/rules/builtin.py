"""Built-in rules for the known symptoms in the spec."""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..kube import container_from_deployment
from ..models import Diagnosis, DiagnosisSource, PodIssue, Symptom
from ..patches import env_patch, image_patch, memory_limit_patch
from ..util import format_memory_mib, image_repository, parse_memory_to_bytes
from .base import Rule, RuleContext

log = logging.getLogger("aiops.rules")

# Registry answers meaning "this tag/repo does not exist" (vs. auth/network).
_IMAGE_NOT_FOUND_RE = re.compile(r"not found|manifest unknown", re.IGNORECASE)

# Log lines naming a missing environment variable. Upper-case names only, to
# keep ordinary words out.
_ENV_VAR = r"""['"]?([A-Z][A-Z0-9_]*)['"]?"""
_MISSING_ENV_RES = [
    re.compile(_ENV_VAR + r" (?:is )?(?:not set|unset|missing|undefined|required)"),
    re.compile(r"(?:environment variable|env var)s?[:\s]+" + _ENV_VAR),
    re.compile(r"KeyError: " + _ENV_VAR),
]

_MISSING_REF_RE = re.compile(r'(configmap|secret) "([^"]+)" not found', re.IGNORECASE)


class OOMKilledRule(Rule):
    """Rule 1: OOMKilled -> raise the memory limit and let the Deployment restart."""

    symptom = Symptom.OOM_KILLED
    name = "oom-killed"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        dep_name, dep_obj = ctx.owner_deployment(issue)
        new_memory = self._compute_memory(issue, ctx, dep_obj)

        actions = ["Increase Memory Limit", "Restart Deployment"]
        if not dep_name or not dep_obj:
            # No owning Deployment (bare pod) -> we can only advise.
            return Diagnosis(
                source=DiagnosisSource.RULE,
                symptom=self.symptom,
                root_cause="memory limit too low (container OOMKilled)",
                summary="Pod OOMKilled but has no owning Deployment to patch.",
                actions=actions,
                auto_fixable=False,
            )

        container = container_from_deployment(dep_obj, issue.container)
        cname = container.get("name") if container else issue.container
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=self.symptom,
            root_cause="memory limit too low (container OOMKilled)",
            summary=f"Raise memory limit to {new_memory} on {dep_name}/{cname}.",
            actions=actions,
            patch=memory_limit_patch(cname, new_memory),
            proposed_action="set_memory_limit",
            action_params={"memory": new_memory},
            target_kind="deployment",
            target_name=dep_name,
            target_namespace=issue.namespace,
            auto_fixable=True,
            confidence=0.9,
        )

    def _compute_memory(self, issue: PodIssue, ctx: RuleContext, dep_obj) -> str:
        default = ctx.config.default_memory_limit
        if not dep_obj:
            return default
        container = container_from_deployment(dep_obj, issue.container)
        current = (
            (container or {})
            .get("resources", {})
            .get("limits", {})
            .get("memory")
        )
        if not current:
            return default
        try:
            scaled = int(parse_memory_to_bytes(current) * ctx.config.memory_scale_factor)
            default_bytes = parse_memory_to_bytes(default)
            return format_memory_mib(max(scaled, default_bytes))
        except (ValueError, TypeError):
            return default


class ImagePullRule(Rule):
    """Rule 2: ImagePullBackOff / ErrImagePull -> check tag & registry access.

    We never guess a 'correct' image. Only when the registry says the tag does
    not exist AND the operator approved a fallback for that repository
    (AIOPS_IMAGE_FALLBACKS) is the image patched automatically.
    """

    symptom = Symptom.IMAGE_PULL_BACKOFF
    name = "image-pull"

    def matches(self, issue: PodIssue) -> bool:
        return issue.symptom in (Symptom.IMAGE_PULL_BACKOFF, Symptom.ERR_IMAGE_PULL)

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        fix = self._fallback_fix(issue, ctx)
        if fix is not None:
            return fix
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=issue.symptom,
            root_cause="image cannot be pulled (bad tag or registry access)",
            summary=f"Verify image tag and registry access: {issue.message}",
            actions=["Check Image Tag", "Check Registry Access"],
            auto_fixable=False,
            forward_to_kb=True,  # a KB entry may know the correct image
        )

    def _fallback_fix(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        if not _IMAGE_NOT_FOUND_RE.search(issue.message):
            return None
        dep_name, dep_obj = ctx.owner_deployment(issue)
        if not dep_name or not dep_obj:
            return None
        container = container_from_deployment(dep_obj, issue.container)
        image = (container or {}).get("image", "")
        fallback = ctx.config.image_fallbacks.get(image_repository(image)) if image else None
        if not fallback or fallback == image:
            return None
        cname = container.get("name")
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=issue.symptom,
            root_cause=f"image tag does not exist: {image}",
            summary=f"Switch {dep_name}/{cname} from {image} to approved fallback {fallback}.",
            actions=["Replace Image With Approved Fallback", "Restart Deployment"],
            patch=image_patch(cname, fallback),
            proposed_action="set_image",
            action_params={"image": fallback},
            target_kind="deployment",
            target_name=dep_name,
            target_namespace=issue.namespace,
            auto_fixable=True,
            confidence=0.9,
        )


class MissingEnvRule(Rule):
    """Rule 3: CrashLoopBackOff whose logs name a missing environment variable.

    Auto-fixes only when the variable has an operator-approved value
    (AIOPS_ENV_DEFAULTS); otherwise returns a precise recommendation. Returns
    None when the logs don't mention a missing variable, so the generic
    CrashLoopBackOff rule takes over.
    """

    symptom = Symptom.CRASH_LOOP_BACKOFF
    name = "missing-env"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        var = find_missing_env_var(f"{ctx.logs}\n{ctx.previous_logs}")
        if not var:
            return None
        dep_name, dep_obj = ctx.owner_deployment(issue)
        container = container_from_deployment(dep_obj, issue.container) if dep_obj else None
        cname = (container or {}).get("name") or issue.container
        if any(e.get("name") == var and e.get("value") for e in (container or {}).get("env", [])):
            return None  # already set: the crash is about something else

        diag = Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=issue.symptom,
            root_cause=f"required environment variable {var} is not set",
            summary=f"Set env var {var} on {dep_name or issue.pod}/{cname}.",
            actions=[f"Set {var}", "Restart Deployment"],
            confidence=0.9,
            forward_to_kb=True,  # a verified KB entry may know the value
            consult_llm=False,  # the LLM can't know the value either
        )
        value = ctx.config.env_defaults.get(var)
        if value is not None and dep_name and dep_obj and cname:
            diag.summary = f"Set env var {var}={value} (approved default) on {dep_name}/{cname}."
            diag.patch = env_patch(cname, var, value)
            diag.proposed_action = "set_env"
            diag.action_params = {"name": var, "value": value}
            diag.target_kind = "deployment"
            diag.target_name = dep_name
            diag.target_namespace = issue.namespace
            diag.auto_fixable = True
            diag.forward_to_kb = False
        return diag


def find_missing_env_var(text: str) -> Optional[str]:
    for pattern in _MISSING_ENV_RES:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


class CrashLoopBackOffRule(Rule):
    """Rule 4: CrashLoopBackOff -> collect logs/events and hand off to KB/LLM."""

    symptom = Symptom.CRASH_LOOP_BACKOFF
    name = "crash-loop"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=self.symptom,
            root_cause="application crashes on start (see logs)",
            summary="Collected logs & events; forwarding to Knowledge Base / LLM.",
            actions=["Collect Logs", "Collect Events", "Forward to Knowledge Base"],
            auto_fixable=False,
            forward_to_kb=True,
        )


class ConfigRefRule(Rule):
    """Rule 5: CreateContainerConfigError -> a referenced ConfigMap/Secret is missing.

    Advisory only: inventing config data would be unsafe.
    """

    symptom = Symptom.CREATE_CONTAINER_CONFIG_ERROR
    name = "config-ref"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        match = _MISSING_REF_RE.search(issue.message)
        if match:
            kind, ref = match.group(1).lower(), match.group(2)
            root_cause = f"{kind} '{ref}' referenced by the pod does not exist"
            summary = (
                f"Create {kind} '{ref}' in namespace {issue.namespace}, "
                "or fix the reference in the Deployment."
            )
        else:
            root_cause = "container config cannot be built (bad env/volume reference)"
            summary = f"Check ConfigMap/Secret references: {issue.message}"
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=issue.symptom,
            root_cause=root_cause,
            summary=summary,
            actions=["Check ConfigMap/Secret References"],
            confidence=0.9,
            forward_to_kb=True,
            consult_llm=False,
        )


class PendingRule(Rule):
    """Rule 6: Pending -> explain why the scheduler can't place the pod."""

    symptom = Symptom.PENDING
    name = "pending"

    _REASONS = [
        (re.compile(r"Insufficient (cpu|memory)", re.IGNORECASE),
         "resource requests exceed free node capacity ({0})",
         "Lower the Deployment's {0} requests or add node capacity."),
        (re.compile(r"didn't match (?:Pod's )?node (?:affinity|selector)", re.IGNORECASE),
         "no node matches the nodeSelector / affinity",
         "Fix nodeSelector/affinity or label a node accordingly."),
        (re.compile(r"untolerated taint", re.IGNORECASE),
         "all nodes carry taints the pod does not tolerate",
         "Add a toleration or remove the taint."),
        (re.compile(r"unbound .*PersistentVolumeClaim", re.IGNORECASE),
         "PersistentVolumeClaim is not bound",
         "Check the PVC / StorageClass."),
    ]

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        for pattern, cause, fix in self._REASONS:
            match = pattern.search(issue.message)
            if match:
                arg = match.group(1).lower() if match.groups() else ""
                return Diagnosis(
                    source=DiagnosisSource.RULE,
                    symptom=self.symptom,
                    root_cause=cause.format(arg),
                    summary=fix.format(arg),
                    actions=["Review Scheduling Constraints"],
                    confidence=0.9,
                    forward_to_kb=True,
                    consult_llm=False,
                )
        return None  # unrecognised -> KB / LLM


class NotReadyRule(Rule):
    """Rule 7: Running but NotReady -> probe failing; hand logs/events to KB/LLM."""

    symptom = Symptom.NOT_READY
    name = "not-ready"

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        # Events are oldest-first; the latest failure is the current one (the
        # first is often a "connection refused" from while the app was starting).
        probes = re.findall(r"Readiness probe failed:[^\n]*", ctx.events)
        detail = probes[-1].strip() if probes else "readiness probe not passing"
        return Diagnosis(
            source=DiagnosisSource.RULE,
            symptom=self.symptom,
            root_cause=f"container never becomes Ready ({detail})",
            summary="Check the readiness probe path/port against what the app serves.",
            actions=["Check Readiness Probe", "Collect Logs", "Collect Events"],
            confidence=0.8,
            forward_to_kb=True,
        )
