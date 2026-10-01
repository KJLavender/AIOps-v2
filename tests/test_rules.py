from aiops.config import Config
from aiops.kube import container_from_deployment
from aiops.models import PodIssue, Symptom
from aiops.rules import RuleEngine
from aiops.rules.base import RuleContext
from aiops.rules.builtin import find_missing_env_var


class _FakeKube:
    def __init__(self, deployment=None, name="case2-oomkilled"):
        self._deployment = deployment
        self._name = name

    def get_owner_deployment(self, namespace, pod):
        if self._deployment is None:
            return None, None
        return self._name, self._deployment


def _deployment(memory="64Mi"):
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "resources": {"limits": {"memory": memory}},
                        }
                    ]
                }
            }
        }
    }


def _container_deployment(**container):
    return {"spec": {"template": {"spec": {"containers": [{"name": "app", **container}]}}}}


def _ctx(kube, config=None, **fields):
    return RuleContext(kube=kube, config=config or Config(), **fields)


def _patched_container(diag):
    return diag.patch["spec"]["template"]["spec"]["containers"][0]


def _limit_memory(diag):
    return diag.patch["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]


def test_oom_rule_applies_memory_floor():
    # 64Mi doubled is 128Mi, but the 512Mi default floor wins (matches the spec).
    kube = _FakeKube(_deployment("64Mi"))
    issue = PodIssue("aiops-demo", "case2-oomkilled-x", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is True
    assert diag.target_kind == "deployment"
    assert _limit_memory(diag) == "512Mi"


def test_oom_rule_scales_when_above_floor():
    # 512Mi doubled = 1Gi, above the floor, so scaling wins.
    kube = _FakeKube(_deployment("512Mi"))
    issue = PodIssue("aiops-demo", "big", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert _limit_memory(diag) == "1Gi"


def test_oom_rule_without_deployment_is_advisory():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "bare-pod", "app", Symptom.OOM_KILLED)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is False


def test_imagepull_rule_is_advisory_and_forwards():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "case3", "app", Symptom.IMAGE_PULL_BACKOFF, "not found")
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.auto_fixable is False
    assert diag.forward_to_kb is True


def test_crashloop_rule_forwards_to_kb():
    kube = _FakeKube(None)
    issue = PodIssue("aiops-demo", "case1", "app", Symptom.CRASH_LOOP_BACKOFF)
    diag = RuleEngine().diagnose(issue, _ctx(kube))
    assert diag.forward_to_kb is True
    assert diag.auto_fixable is False


def test_container_from_deployment_by_name():
    dep = _deployment()
    assert container_from_deployment(dep, "app")["name"] == "app"
    assert container_from_deployment(dep, None)["name"] == "app"


_NOT_FOUND = 'failed to resolve reference "docker.io/library/nginx:notfound": not found'


def test_imagepull_uses_approved_fallback_when_tag_missing():
    kube = _FakeKube(_container_deployment(image="nginx:notfound"), "case3-imagepull")
    config = Config(image_fallbacks={"nginx": "nginx:1.27-alpine"})
    issue = PodIssue("aiops-demo", "case3", "app", Symptom.ERR_IMAGE_PULL, _NOT_FOUND)
    diag = RuleEngine().diagnose(issue, _ctx(kube, config))
    assert diag.auto_fixable is True
    assert diag.target_name == "case3-imagepull"
    assert _patched_container(diag) == {"name": "app", "image": "nginx:1.27-alpine"}


def test_imagepull_without_fallback_stays_advisory():
    kube = _FakeKube(_container_deployment(image="myapp:bad"), "web")
    config = Config(image_fallbacks={"nginx": "nginx:1.27-alpine"})
    issue = PodIssue("ns", "web-x", "app", Symptom.IMAGE_PULL_BACKOFF, _NOT_FOUND)
    diag = RuleEngine().diagnose(issue, _ctx(kube, config))
    assert diag.auto_fixable is False
    assert diag.forward_to_kb is True


def test_imagepull_auth_error_never_swaps_image():
    kube = _FakeKube(_container_deployment(image="nginx:private"), "web")
    config = Config(image_fallbacks={"nginx": "nginx:1.27-alpine"})
    issue = PodIssue("ns", "web-x", "app", Symptom.ERR_IMAGE_PULL, "401 Unauthorized")
    diag = RuleEngine().diagnose(issue, _ctx(kube, config))
    assert diag.auto_fixable is False


def test_missing_env_with_approved_default_is_auto_fixed():
    kube = _FakeKube(_container_deployment(image="python:3.12-slim"), "case4-missing-env")
    config = Config(env_defaults={"DB_HOST": "db.local"})
    issue = PodIssue("aiops-demo", "case4", "app", Symptom.CRASH_LOOP_BACKOFF, "Error: exit 1")
    ctx = _ctx(kube, config, previous_logs="Exception: DB_HOST is not set\n")
    diag = RuleEngine().diagnose(issue, ctx)
    assert diag.auto_fixable is True
    assert diag.target_name == "case4-missing-env"
    assert _patched_container(diag)["env"] == [{"name": "DB_HOST", "value": "db.local"}]


def test_missing_env_without_default_is_precise_advisory():
    kube = _FakeKube(_container_deployment(), "case4-missing-env")
    issue = PodIssue("aiops-demo", "case4", "app", Symptom.CRASH_LOOP_BACKOFF)
    ctx = _ctx(kube, logs="KeyError: 'API_TOKEN'")
    diag = RuleEngine().diagnose(issue, ctx)
    assert diag.auto_fixable is False
    assert "API_TOKEN" in diag.root_cause
    assert diag.consult_llm is False


def test_crash_without_env_hint_falls_to_generic_rule():
    kube = _FakeKube(_container_deployment(), "case1-crashloop")
    issue = PodIssue("aiops-demo", "case1", "app", Symptom.CRASH_LOOP_BACKOFF)
    diag = RuleEngine().diagnose(issue, _ctx(kube, logs="+ exit 1"))
    assert diag.root_cause == "application crashes on start (see logs)"
    assert diag.consult_llm is True


def test_find_missing_env_var_patterns():
    assert find_missing_env_var("Exception: DB_HOST is not set") == "DB_HOST"
    assert find_missing_env_var("Missing required environment variable: REDIS_URL") == "REDIS_URL"
    assert find_missing_env_var("KeyError: 'SECRET_KEY'") == "SECRET_KEY"
    assert find_missing_env_var("connection refused") is None


def test_config_ref_rule_names_missing_configmap():
    issue = PodIssue(
        "aiops-demo", "case7", "app", Symptom.CREATE_CONTAINER_CONFIG_ERROR,
        'configmap "app-config" not found',
    )
    diag = RuleEngine().diagnose(issue, _ctx(_FakeKube(None)))
    assert "app-config" in diag.root_cause
    assert diag.auto_fixable is False


def test_pending_rule_explains_insufficient_memory():
    issue = PodIssue(
        "aiops-demo", "case5", None, Symptom.PENDING,
        "0/1 nodes are available: 1 Insufficient memory.",
    )
    diag = RuleEngine().diagnose(issue, _ctx(_FakeKube(None)))
    assert "memory" in diag.root_cause
    assert diag.auto_fixable is False


def test_pending_rule_declines_unknown_reason():
    issue = PodIssue("ns", "p", None, Symptom.PENDING, "Pod pending")
    assert RuleEngine().diagnose(issue, _ctx(_FakeKube(None))) is None


def test_not_ready_rule_reports_latest_probe_failure():
    events = (
        "10m Warning Unhealthy pod/x Readiness probe failed: dial tcp: connect: connection refused\n"
        "5s Warning Unhealthy pod/x Readiness probe failed: HTTP probe failed with statuscode: 404\n"
    )
    issue = PodIssue("aiops-demo", "case6", "app", Symptom.NOT_READY)
    diag = RuleEngine().diagnose(issue, _ctx(_FakeKube(None), events=events))
    assert "statuscode: 404" in diag.root_cause
