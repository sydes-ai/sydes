"""Pins the prototype's core non-goal: `sydes.recovery` must never encode
framework-, library-, or language-specific rules. The agent's PROMPT may
name concepts like "decorators" or "dependency injection" as open examples
of what repository-local wiring can look like (see `agent.py`'s system
prompt docstring) -- what must never appear anywhere in this package is a
rule keyed to one specific framework's name or API.
"""

from __future__ import annotations

from pathlib import Path

import sydes.recovery

_BANNED_TERMS = (
    "nestjs", "nest.js",
    "querybus", "commandbus", "query bus", "command bus",
    "queryhandler", "@queryhandler", "commandhandler",
    "cqrs",
    "springframework", "spring boot", "springbootapplication",
    "@controller", "@service", "@component", "@autowired", "@requestmapping",
    "@getmapping", "@postmapping",
    "django", "flask", "fastapi", "express.js", "expressjs",
    "actix", "rocket::", "gin.engine", "gorilla/mux",
)


def test_no_framework_specific_terms_anywhere_in_recovery_source():
    package_dir = Path(sydes.recovery.__file__).parent
    offenders: list[str] = []
    for path in sorted(package_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for term in _BANNED_TERMS:
            if term in text:
                offenders.append(f"{path.name}: {term!r}")
    assert not offenders, f"framework-specific terms found in recovery module: {offenders}"
