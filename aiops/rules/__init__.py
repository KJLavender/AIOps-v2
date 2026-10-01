"""Rule Engine: handles known problems without spending LLM tokens."""
from .engine import RuleEngine, default_rules

__all__ = ["RuleEngine", "default_rules"]
