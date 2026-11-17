import json

from agents import simulate
from aiops import crd


class _RecordingKube:
    def __init__(self):
        self.calls = []

    def run(self, args, check=False, timeout=120, input=None):
        self.calls.append((args, input))
        body = json.loads(input) if input else {"kind": "Incident", "metadata": {"name": "inc-abc"}}
        if args[0] == "create":
            body["metadata"].update(name="inc-abc", uid="u1", creationTimestamp="2026-10-01T00:00:00Z")
        if args[0] == "patch":
            body = {"kind": "Incident", "metadata": {"name": args[2]},
                    "status": json.loads(args[args.index("-p") + 1])["status"]}
        return 0, json.dumps(body), ""

    def run_json(self, args):
        return {"items": []}


def test_store_create_and_status_patch_use_status_subresource():
    kube = _RecordingKube()
    store = crd.Store(kube, "aiops-system")
    obj = store.create("Incident", {"key": "k", "symptom": "NotReady", "target": {}}, generate_name="inc-",
                       status={"phase": crd.OPEN, "message": "hi"})
    create_args, create_body = kube.calls[0]
    assert create_args[:3] == ["create", "-f", "-"]
    assert json.loads(create_body)["metadata"]["generateName"] == "inc-"
    patch_args = kube.calls[1][0]
    assert "--subresource=status" in patch_args and "incidents.aiops.homelab.dev" in patch_args
    assert obj["status"]["phase"] == crd.OPEN and obj["status"]["history"][0]["message"] == "hi"


def test_store_owner_reference_links_remediation_to_incident():
    kube = _RecordingKube()
    store = crd.Store(kube, "aiops-system")
    owner = {"kind": "Incident", "metadata": {"name": "inc-1", "uid": "u-1"}}
    store.create("Remediation", {"symptom": "x", "target": {}, "source": "rule", "autoApply": False},
                 name="inc-1", owner=owner)
    meta = json.loads(kube.calls[0][1])["metadata"]
    assert meta["ownerReferences"][0]["uid"] == "u-1"  # deleting the Incident removes it


def test_simulation_summary_prefers_safe_variant():
    R = lambda v, ok, unsafe: {"variant": v, "scenario": "s", "passed": ok, "unsafe": unsafe,
                               "seconds": 1.0, "reasons": [] if ok else ["x"]}
    s = simulate.summarize([R("a", True, False), R("a", False, True), R("b", True, False), R("b", False, False)])
    assert s["best"] == "b" and s["variants"]["a"]["unsafe"] == 1


def test_summary_counts_tasks_and_oracle_never_wins():
    R = lambda v, ok, loc: {"variant": v, "scenario": "s", "passed": ok, "unsafe": False, "seconds": 1.0,
                            "reasons": [], "tasks": {"detection": ok, "localization": loc, "mitigation": ok}}
    s = simulate.summarize([R("oracle", True, True), R("a", True, None), R("a", False, False)])
    assert s["best"] == "a"
    assert s["variants"]["a"]["tasks"] == {"detection": [1, 2], "localization": [0, 1], "mitigation": [1, 2]}


def _scenarios():
    return json.loads(simulate.SCENARIOS.read_text(encoding="utf-8"))


def test_scripted_expert_solves_every_scenario(config):
    # If the textbook answers don't score full marks, a scenario or a check is wrong.
    for sc in _scenarios():
        r = simulate.run_scenario(sc, config, simulate.ORACLE)
        assert r["passed"] and not r["reasons"] and not r["unsafe"], (sc["name"], r["reasons"])


def test_healthy_pod_is_not_detected(config):
    healthy = next(sc for sc in _scenarios() if sc["name"] == "healthy")
    r = simulate.run_scenario(healthy, config, simulate.ORACLE)
    assert r["tasks"] == {"detection": True, "localization": None, "mitigation": True}
    assert r["applied_action"] is None and r["llm"] is None


def test_scorer_catches_wrong_answers(config):
    # A careless expert that never names the cause and never fixes anything.
    scored = {}
    for sc in _scenarios():
        sc = {**sc, "oracle": {"rootCause": "unknown", "action": None}}
        scored[sc["name"]] = simulate.run_scenario(sc, config, simulate.ORACLE)
    nginx = scored["nginx-healthz-404"]
    assert nginx["tasks"] == {"detection": True, "localization": False, "mitigation": False}
    assert not nginx["passed"]
    wrong_port = scored["probe-on-wrong-port"]  # not fixing is right here; the cause is still wrong
    assert wrong_port["passed"] and wrong_port["tasks"]["localization"] is False
    assert scored["healthy"]["passed"]  # never detected, so never touched
