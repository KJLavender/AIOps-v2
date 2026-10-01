"""Builders for kubectl strategic-merge patches on a Deployment's pod template."""
from __future__ import annotations

from typing import Any


def _container_patch(container_name: str, container_body: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": container_name, **container_body}]
                }
            }
        }
    }


def memory_limit_patch(container_name: str, memory: str) -> dict[str, Any]:
    return _container_patch(
        container_name,
        {"resources": {"limits": {"memory": memory}, "requests": {"memory": memory}}},
    )


def image_patch(container_name: str, image: str) -> dict[str, Any]:
    return _container_patch(container_name, {"image": image})


def env_patch(container_name: str, name: str, value: str) -> dict[str, Any]:
    # Strategic merge keys env entries by name, so this upserts DB_HOST etc.
    return _container_patch(container_name, {"env": [{"name": name, "value": value}]})
