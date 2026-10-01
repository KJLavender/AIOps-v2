"""Rule base class and the diagnosis context passed to rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import Config
from ..kube import KubeClient
from ..models import Diagnosis, PodIssue, Symptom


@dataclass
class RuleContext:
    """Everything a rule may need to diagnose an issue (collected lazily)."""

    kube: KubeClient
    config: Config
    logs: str = ""
    previous_logs: str = ""
    events: str = ""
    describe: str = ""
    _deployment_cache: dict = field(default_factory=dict)

    def owner_deployment(self, issue: PodIssue):
        if "resolved" not in self._deployment_cache:
            name, obj = self.kube.get_owner_deployment(issue.namespace, issue.pod)
            self._deployment_cache = {"resolved": True, "name": name, "obj": obj}
        return self._deployment_cache["name"], self._deployment_cache["obj"]


class Rule:
    """A single symptom handler. Subclasses set `symptom` and implement diagnose."""

    symptom: Symptom
    name: str = "rule"

    def matches(self, issue: PodIssue) -> bool:
        return issue.symptom == self.symptom

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        raise NotImplementedError
