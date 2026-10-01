"""The Rule Engine: first matching rule that returns a diagnosis wins."""
from __future__ import annotations

import logging
from typing import Optional

from ..models import Diagnosis, PodIssue
from .base import Rule, RuleContext
from .builtin import (
    ConfigRefRule,
    CrashLoopBackOffRule,
    ImagePullRule,
    MissingEnvRule,
    NotReadyRule,
    OOMKilledRule,
    PendingRule,
)

log = logging.getLogger("aiops.rules.engine")


def default_rules() -> list[Rule]:
    # Order matters: MissingEnv must run before the generic CrashLoopBackOff rule.
    return [
        OOMKilledRule(),
        ImagePullRule(),
        MissingEnvRule(),
        CrashLoopBackOffRule(),
        ConfigRefRule(),
        PendingRule(),
        NotReadyRule(),
    ]


class RuleEngine:
    def __init__(self, rules: Optional[list[Rule]] = None) -> None:
        self.rules = rules if rules is not None else default_rules()

    def diagnose(self, issue: PodIssue, ctx: RuleContext) -> Optional[Diagnosis]:
        for rule in self.rules:
            if not rule.matches(issue):
                continue
            # A rule may decline (return None) so a more generic one can answer.
            diagnosis = rule.diagnose(issue, ctx)
            if diagnosis is not None:
                log.info("rule '%s' matched %s", rule.name, issue.key)
                return diagnosis
        return None
