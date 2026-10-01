"""LLM analyzer interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..models import Diagnosis, PodIssue


@dataclass
class LLMInput:
    """Context fed to the model (per spec: logs, events, describe, YAML, spec)."""

    logs: str = ""
    previous_logs: str = ""
    events: str = ""
    describe: str = ""
    deployment_yaml: str = ""
    container_spec: str = ""
    web_results: str = ""  # untrusted: search hits for the error
    allowed_actions: tuple[str, ...] = ()  # catalog the model may pick from


class LLMAnalyzer(ABC):
    @abstractmethod
    def analyze(self, issue: PodIssue, data: LLMInput) -> Optional[Diagnosis]:
        """Return a Diagnosis (source=LLM) or None if unavailable/uncertain."""


class NullAnalyzer(LLMAnalyzer):
    """Phase 1 default: LLM is off, so this always returns None."""

    def analyze(self, issue: PodIssue, data: LLMInput) -> Optional[Diagnosis]:
        return None
