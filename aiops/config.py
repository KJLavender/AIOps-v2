"""Runtime configuration, overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _env_map(name: str) -> dict[str, str]:
    """Parse "k1=v1,k2=v2" into a dict (empty / malformed pairs are skipped)."""
    result: dict[str, str] = {}
    for pair in (os.getenv(name) or "").split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip() and value.strip():
            result[key.strip()] = value.strip()
    return result


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


@dataclass
class Config:
    # --- Discovery ---
    all_namespaces: bool = True
    namespaces: list[str] = field(default_factory=lambda: ["default"])
    poll_interval_seconds: int = 15
    kubectl_bin: str = "kubectl"

    # --- Remediation ---
    auto_fix: bool = True
    dry_run: bool = False
    default_memory_limit: str = "512Mi"
    memory_scale_factor: float = 2.0
    handle_cooldown_seconds: int = 600  # avoid re-fixing the same issue repeatedly
    # Operator-approved values. Only images / env vars listed here are auto-fixed;
    # anything else stays a recommendation.
    image_fallbacks: dict[str, str] = field(default_factory=dict)  # repo -> image
    env_defaults: dict[str, str] = field(default_factory=dict)  # VAR -> value
    not_ready_grace_seconds: int = 120  # Running but NotReady this long = issue
    pending_grace_seconds: int = 60  # Pending / ContainerCreating this long = issue

    # --- Validation ---
    validation_timeout_seconds: int = 300  # 5 minutes, per spec
    validation_poll_seconds: int = 15
    validation_stability_seconds: int = 60

    # --- Knowledge Base ---
    kb_path: str = "data/knowledge_base.jsonl"
    kb_score_threshold: float = 0.35
    cluster_name: str = "homelab"

    # --- LLM (disabled in Phase 1) ---
    llm_enabled: bool = False
    ollama_endpoint: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    llm_timeout_seconds: int = 60
    llm_num_ctx: int = 8192  # tokens; prompts with logs + web results run ~5k
    llm_min_confidence: float = 0.6  # drop LLM answers below this
    # Let the LLM pick a fix from the bounded catalog in actions.py.
    llm_auto_fix: bool = False
    llm_auto_fix_min_confidence: float = 0.7
    max_memory_limit: str = "2Gi"  # cap for LLM-proposed memory raises
    rollback_on_failure: bool = True  # `rollout undo` when validation fails
    action_precheck: bool = True  # probe a proposed readiness path on the pod first
    # Evaluate: a second LLM call grades each proposed fix against the cluster
    # evidence; every score must reach judge_min_score.
    judge_enabled: bool = True
    judge_min_score: float = 0.7
    judge_model: str = ""  # empty = same model as the analyzer
    prompt_variant: str = "default"  # Optimize: chosen by the simulation suite

    # --- Web search (Exa MCP, same backend as the agent-reach skill) ---
    web_search_enabled: bool = False
    exa_endpoint: str = "https://mcp.exa.ai/mcp"
    web_search_results: int = 3
    web_search_max_chars: int = 4000
    web_search_timeout_seconds: int = 30
    log_tail_lines: int = 200

    # --- v2 multi-agent ---
    system_namespace: str = "aiops-system"  # where Incidents/Remediations/Lessons live
    incident_cooldown_seconds: int = 600  # minimum gap before re-opening an issue
    unresolved_recheck_hours: int = 6  # unchanged + unresolved: look again this rarely
    retention_days: int = 7  # finished Incidents (and their Remediations) are pruned
    evidence_log_lines: int = 80  # log lines captured into an Incident
    diagnose_workers: int = 2
    validate_workers: int = 4
    agent_port: int = 8080  # /metrics, /healthz (and the console on the monitor)

    # --- Dashboard ---
    dashboard_host: str = "127.0.0.1"  # use 0.0.0.0 in-cluster
    dashboard_port: int = 8080

    @classmethod
    def from_env(cls) -> "Config":
        ns_env = os.getenv("AIOPS_NAMESPACES")
        return cls(
            all_namespaces=_env_bool("AIOPS_ALL_NAMESPACES", True),
            namespaces=[n.strip() for n in ns_env.split(",")] if ns_env else ["default"],
            poll_interval_seconds=_env_int("AIOPS_POLL_INTERVAL", 15),
            kubectl_bin=os.getenv("AIOPS_KUBECTL_BIN", "kubectl"),
            auto_fix=_env_bool("AIOPS_AUTO_FIX", True),
            dry_run=_env_bool("AIOPS_DRY_RUN", False),
            default_memory_limit=os.getenv("AIOPS_DEFAULT_MEMORY", "512Mi"),
            memory_scale_factor=_env_float("AIOPS_MEMORY_SCALE", 2.0),
            handle_cooldown_seconds=_env_int("AIOPS_COOLDOWN", 600),
            image_fallbacks=_env_map("AIOPS_IMAGE_FALLBACKS"),
            env_defaults=_env_map("AIOPS_ENV_DEFAULTS"),
            not_ready_grace_seconds=_env_int("AIOPS_NOT_READY_GRACE", 120),
            pending_grace_seconds=_env_int("AIOPS_PENDING_GRACE", 60),
            validation_timeout_seconds=_env_int("AIOPS_VALIDATION_TIMEOUT", 300),
            validation_poll_seconds=_env_int("AIOPS_VALIDATION_POLL", 15),
            validation_stability_seconds=_env_int("AIOPS_STABILITY", 60),
            kb_path=os.getenv("AIOPS_KB_PATH", "data/knowledge_base.jsonl"),
            kb_score_threshold=_env_float("AIOPS_KB_THRESHOLD", 0.35),
            cluster_name=os.getenv("AIOPS_CLUSTER", "homelab"),
            llm_enabled=_env_bool("AIOPS_LLM_ENABLED", False),
            ollama_endpoint=os.getenv("AIOPS_OLLAMA_ENDPOINT", "http://localhost:11434"),
            ollama_model=os.getenv("AIOPS_OLLAMA_MODEL", "llama3"),
            llm_timeout_seconds=_env_int("AIOPS_LLM_TIMEOUT", 60),
            llm_num_ctx=_env_int("AIOPS_LLM_NUM_CTX", 8192),
            llm_min_confidence=_env_float("AIOPS_LLM_MIN_CONFIDENCE", 0.6),
            llm_auto_fix=_env_bool("AIOPS_LLM_AUTO_FIX", False),
            llm_auto_fix_min_confidence=_env_float("AIOPS_LLM_AUTO_FIX_MIN_CONFIDENCE", 0.7),
            max_memory_limit=os.getenv("AIOPS_MAX_MEMORY", "2Gi"),
            rollback_on_failure=_env_bool("AIOPS_ROLLBACK", True),
            action_precheck=_env_bool("AIOPS_ACTION_PRECHECK", True),
            judge_enabled=_env_bool("AIOPS_JUDGE", True),
            judge_min_score=_env_float("AIOPS_JUDGE_MIN_SCORE", 0.7),
            judge_model=os.getenv("AIOPS_JUDGE_MODEL", ""),
            prompt_variant=os.getenv("AIOPS_PROMPT_VARIANT", "default"),
            web_search_enabled=_env_bool("AIOPS_WEB_SEARCH", False),
            exa_endpoint=os.getenv("AIOPS_EXA_ENDPOINT", "https://mcp.exa.ai/mcp"),
            web_search_results=_env_int("AIOPS_WEB_SEARCH_RESULTS", 3),
            web_search_max_chars=_env_int("AIOPS_WEB_SEARCH_MAX_CHARS", 4000),
            web_search_timeout_seconds=_env_int("AIOPS_WEB_SEARCH_TIMEOUT", 30),
            log_tail_lines=_env_int("AIOPS_LOG_TAIL", 200),
            system_namespace=os.getenv("AIOPS_SYSTEM_NAMESPACE", "aiops-system"),
            incident_cooldown_seconds=_env_int("AIOPS_INCIDENT_COOLDOWN", 600),
            unresolved_recheck_hours=_env_int("AIOPS_UNRESOLVED_RECHECK_HOURS", 6),
            retention_days=_env_int("AIOPS_RETENTION_DAYS", 7),
            evidence_log_lines=_env_int("AIOPS_EVIDENCE_LOG_LINES", 80),
            diagnose_workers=_env_int("AIOPS_DIAGNOSE_WORKERS", 2),
            validate_workers=_env_int("AIOPS_VALIDATE_WORKERS", 4),
            agent_port=_env_int("AIOPS_PORT", 8080),
            dashboard_host=os.getenv("AIOPS_DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_env_int("AIOPS_DASHBOARD_PORT", 8080),
        )
