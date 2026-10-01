"""Core data models shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Symptom(str, Enum):
    """Pod/container states the agent watches (matches kubectl reason strings)."""

    CRASH_LOOP_BACKOFF = "CrashLoopBackOff"
    IMAGE_PULL_BACKOFF = "ImagePullBackOff"
    ERR_IMAGE_PULL = "ErrImagePull"
    OOM_KILLED = "OOMKilled"
    CONTAINER_CREATING = "ContainerCreating"
    CREATE_CONTAINER_CONFIG_ERROR = "CreateContainerConfigError"
    NOT_READY = "NotReady"
    PENDING = "Pending"
    FAILED = "Failed"
    UNKNOWN = "Unknown"

    @classmethod
    def from_reason(cls, reason: str) -> "Symptom":
        try:
            return cls(reason)
        except ValueError:
            return cls.UNKNOWN


# Symptoms considered actionable anomalies (drives the collector).
WATCHED_SYMPTOMS: frozenset[Symptom] = frozenset(
    {
        Symptom.CRASH_LOOP_BACKOFF,
        Symptom.IMAGE_PULL_BACKOFF,
        Symptom.ERR_IMAGE_PULL,
        Symptom.OOM_KILLED,
        Symptom.CONTAINER_CREATING,
        Symptom.CREATE_CONTAINER_CONFIG_ERROR,
        Symptom.NOT_READY,
        Symptom.PENDING,
        Symptom.FAILED,
    }
)


class DiagnosisSource(str, Enum):
    RULE = "rule"
    KB = "kb"
    LLM = "llm"


@dataclass
class PodIssue:
    """A detected anomaly on a single pod/container."""

    namespace: str
    pod: str
    container: Optional[str]
    symptom: Symptom
    message: str = ""
    phase: str = ""
    restart_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.pod}/{self.symptom.value}"


@dataclass
class Diagnosis:
    """Result of analysis: what is wrong and (optionally) how to auto-fix it."""

    source: DiagnosisSource
    symptom: Symptom
    root_cause: str
    summary: str
    actions: list[str] = field(default_factory=list)
    confidence: float = 1.0

    # Structured, safe-to-apply remediation (only these are auto-applied).
    patch: Optional[dict[str, Any]] = None
    target_kind: Optional[str] = None
    target_name: Optional[str] = None
    target_namespace: Optional[str] = None
    auto_fixable: bool = False

    # When True the pipeline should try KB/LLM instead of stopping at the rule.
    forward_to_kb: bool = False
    # False when the rule already pinpointed the cause: consult the KB only,
    # so a vague LLM answer never overrides a precise rule diagnosis.
    consult_llm: bool = True

    # LLM suggestion from the bounded catalog (actions.py); the pipeline
    # validates it and only then turns it into `patch`.
    proposed_action: Optional[str] = None
    action_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RemediationResult:
    attempted: bool
    success: bool
    detail: str = ""
    patch: Optional[dict[str, Any]] = None


@dataclass
class ValidationResult:
    success: bool
    detail: str = ""


@dataclass
class KBEntry:
    """A verified knowledge-base record (structured, never free-form Q&A)."""

    symptom: str
    root_cause: str
    solution: str
    problem: str = ""
    verified: bool = False
    confidence: float = 1.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    cluster: str = ""
    # Optional structured patch so a KB hit can auto-remediate like a rule.
    patch: Optional[dict[str, Any]] = None
    target_kind: Optional[str] = None
    # Workload the patch was verified on. Workload-specific patches (image, env)
    # only auto-apply to this Deployment; None = legacy / generic entry.
    target_name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symptom": self.symptom,
            "problem": self.problem,
            "root_cause": self.root_cause,
            "solution": self.solution,
            "verified": self.verified,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "cluster": self.cluster,
            "patch": self.patch,
            "target_kind": self.target_kind,
            "target_name": self.target_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KBEntry":
        return cls(
            symptom=data.get("symptom", ""),
            root_cause=data.get("root_cause", ""),
            solution=data.get("solution", ""),
            problem=data.get("problem", ""),
            verified=bool(data.get("verified", False)),
            confidence=float(data.get("confidence", 1.0)),
            timestamp=data.get("timestamp", ""),
            cluster=data.get("cluster", ""),
            patch=data.get("patch"),
            target_kind=data.get("target_kind"),
            target_name=data.get("target_name"),
        )
