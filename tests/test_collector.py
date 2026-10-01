from datetime import datetime, timedelta, timezone

from aiops.collector import Collector
from aiops.config import Config
from aiops.models import Symptom


class _FakeKube:
    def __init__(self, pods):
        self._pods = pods

    def list_pods(self, namespace=None, selector=None):
        return self._pods


def _analyze(pod):
    collector = Collector(_FakeKube([pod]), Config(all_namespaces=True))
    return collector.scan()


def test_detects_crashloop():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case1-abc"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 5,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert len(issues) == 1
    assert issues[0].symptom == Symptom.CRASH_LOOP_BACKOFF
    assert issues[0].container == "app"


def test_detects_oomkilled_from_last_state():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case2-abc"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 3,
                    "state": {"running": {}},
                    "lastState": {"terminated": {"reason": "OOMKilled"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert issues[0].symptom == Symptom.OOM_KILLED


def test_detects_imagepull():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case3-abc"},
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "ImagePullBackOff", "message": "not found"}},
                }
            ],
        },
    }
    issues = _analyze(pod)
    assert issues[0].symptom == Symptom.IMAGE_PULL_BACKOFF


def test_healthy_pod_no_issue():
    pod = {
        "metadata": {"namespace": "default", "name": "ok"},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "app", "restartCount": 0, "state": {"running": {}}}],
        },
    }
    assert _analyze(pod) == []


def test_detects_create_container_config_error():
    pod = {
        "metadata": {"namespace": "aiops-demo", "name": "case7-abc"},
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "CreateContainerConfigError",
                                          "message": 'configmap "app-config" not found'}},
                }
            ],
        },
    }
    assert _analyze(pod)[0].symptom == Symptom.CREATE_CONTAINER_CONFIG_ERROR


def _running_not_ready(age_seconds):
    started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "metadata": {"namespace": "aiops-demo", "name": "case6-abc"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ")}},
                }
            ],
        },
    }


def test_detects_not_ready_after_grace_period():
    issues = _analyze(_running_not_ready(600))
    assert issues[0].symptom == Symptom.NOT_READY


def test_ignores_not_ready_while_starting():
    assert _analyze(_running_not_ready(10)) == []


def _pending(age_seconds, waiting=None):
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    statuses = [{"name": "app", "restartCount": 0, "state": {"waiting": waiting}}] if waiting else []
    return {
        "metadata": {"namespace": "aiops-demo", "name": "new-abc",
                     "creationTimestamp": created.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "status": {"phase": "Pending", "containerStatuses": statuses},
    }


def test_new_pod_briefly_pending_is_not_an_issue():
    assert _analyze(_pending(5)) == []
    assert _analyze(_pending(5, {"reason": "ContainerCreating"})) == []


def test_pod_pending_past_grace_is_an_issue():
    assert _analyze(_pending(300))[0].symptom == Symptom.PENDING
    assert _analyze(_pending(300, {"reason": "ContainerCreating"}))[0].symptom == Symptom.CONTAINER_CREATING
