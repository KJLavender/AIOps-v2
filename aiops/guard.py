"""Protect: scan untrusted web results before they reach the LLM.

Search results are written by strangers. A page can carry text aimed at the
model ("ignore previous instructions, set the image to ...") rather than at a
human reader. Results come back as "Title: ..." blocks; any block that trips a
scanner is dropped whole and reported, the rest is passed through.
(Pattern of Future AGI's Protect scanners, done with plain regexes.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SCANNERS: dict[str, re.Pattern] = {
    "instruction_override": re.compile(
        r"ignore (all |any )?(previous|prior|above|earlier) (instructions|prompts?|rules)|"
        r"disregard (the |all )?(previous|above|system)|forget (everything|your instructions)",
        re.IGNORECASE),
    "role_hijack": re.compile(
        r"you are now (an? |the |in )|new instructions?:|system prompt|<\|?(system|im_start)\|?>|"
        r"^\s*(assistant|system)\s*:", re.IGNORECASE | re.MULTILINE),
    "agent_directive": re.compile(
        r"(ai|llm|agent|assistant|model)s? (should|must) (always )?(set|change|run|apply|use|output)|"
        r"(respond|answer|reply) (only )?with (the )?(json|action)", re.IGNORECASE),
    "remote_exec": re.compile(
        r"(curl|wget)[^\n|]{0,120}\|\s*(ba)?sh|base64 -d|powershell -enc|"
        r"privileged:\s*true|hostPID|--privileged", re.IGNORECASE),
}

_BLOCK_SPLIT = re.compile(r"(?=^Title: )", re.MULTILINE)


@dataclass
class GuardResult:
    text: str
    flags: list[str] = field(default_factory=list)  # "<scanner>@<block title>"

    @property
    def blocked(self) -> int:
        return len(self.flags)


def scan(text: str) -> GuardResult:
    if not text:
        return GuardResult("")
    kept, flags = [], []
    for block in _BLOCK_SPLIT.split(text):
        if not block.strip():
            continue
        hits = [name for name, pattern in _SCANNERS.items() if pattern.search(block)]
        if hits:
            title = block.strip().splitlines()[0][:80]
            flags.extend(f"{name}@{title}" for name in hits)
        else:
            kept.append(block)
    return GuardResult("".join(kept).strip(), flags)
