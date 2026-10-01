"""Small shared helpers (Kubernetes quantity parsing, tokenizing)."""
from __future__ import annotations

import re

# Binary suffixes must be checked before decimal ones ("Mi" before "M").
_SUFFIX_TO_BYTES: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "Pi": 1024 ** 5,
    "K": 1000,
    "M": 1000 ** 2,
    "G": 1000 ** 3,
    "T": 1000 ** 4,
    "P": 1000 ** 5,
}


def parse_memory_to_bytes(quantity: str) -> int:
    """Parse a Kubernetes memory quantity (e.g. '64Mi', '1Gi', '512M') to bytes."""
    q = quantity.strip()
    for suffix, mult in sorted(_SUFFIX_TO_BYTES.items(), key=lambda kv: -len(kv[0])):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * mult)
    return int(float(q))


def format_memory_mib(num_bytes: int) -> str:
    """Format a byte count back to a clean Mi/Gi quantity."""
    mib = num_bytes / (1024 ** 2)
    if mib >= 1024 and mib % 1024 == 0:
        return f"{int(mib // 1024)}Gi"
    return f"{max(1, int(round(mib)))}Mi"


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> set[str]:
    """Lowercase word tokens, used by the Phase 1 keyword Knowledge Base search."""
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def image_repository(image: str) -> str:
    """Strip tag/digest: 'nginx:1.27' -> 'nginx', 'reg:5000/app:v1' -> 'reg:5000/app'."""
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name
