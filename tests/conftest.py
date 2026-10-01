import copy
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiops import crd  # noqa: E402
from aiops.config import Config  # noqa: E402


class FakeStore:
    """In-memory stand-in for crd.Store (same method surface)."""

    def __init__(self):
        self.objs = {"incidents": {}, "remediations": {}, "lessons": {}}
        self._seq = itertools.count(1)

    def list(self, plural, selector=None):
        return [copy.deepcopy(o) for o in self.objs[plural].values()]

    def get(self, plural, name):
        obj = self.objs[plural].get(name)
        return copy.deepcopy(obj) if obj else None

    def create(self, kind, spec, *, name=None, generate_name=None, labels=None, owner=None, status=None):
        plural = kind.lower() + "s"
        name = name or f"{generate_name or kind.lower() + '-'}{next(self._seq)}"
        obj = {"kind": kind, "metadata": {"name": name, "uid": f"uid-{name}", "labels": labels or {},
                                          "creationTimestamp": crd.now()},
               "spec": copy.deepcopy(spec), "status": {}}
        self.objs[plural][name] = obj
        if status:
            status = dict(status)
            return self.set_status(obj, status.pop("phase"), status.pop("message", ""), **status)
        return copy.deepcopy(obj)

    def apply(self, kind, name, spec, labels=None):
        plural = kind.lower() + "s"
        self.objs[plural][name] = {"kind": kind, "metadata": {"name": name, "labels": labels or {},
                                                              "creationTimestamp": crd.now()},
                                   "spec": copy.deepcopy(spec)}

    def set_status(self, obj, new_phase, message="", **fields):
        plural = obj["kind"].lower() + "s"
        stored = self.objs[plural][obj["metadata"]["name"]]
        status = dict(stored.get("status") or {})
        history = list(status.get("history") or []) + [{"time": crd.now(), "phase": new_phase, "message": message}]
        status.update(fields, phase=new_phase, message=message, updatedAt=crd.now(), history=history)
        stored["status"] = status
        return copy.deepcopy(stored)

    def delete(self, plural, name):
        self.objs[plural].pop(name, None)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def config():
    return Config(ollama_endpoint="", web_search_enabled=False, llm_enabled=False,
                  image_fallbacks={"nginx": "nginx:1.27-alpine"},
                  env_defaults={"DB_HOST": "db.local"}, validation_stability_seconds=0,
                  validation_poll_seconds=0)
