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
