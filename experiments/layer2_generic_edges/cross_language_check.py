#!/usr/bin/env python3
"""Cross-language generalization check: run the SAME two generic tree-sitter
extractors (built and validated only against Python/Kokoro-FastAPI) against
one TypeScript, one Java, one Go, and one Rust repository already available
locally. Reports edge counts and a random-ish sample for manual sanity
inspection -- "useful signal" vs. "hairball" is a judgment call made by
reading the sample, not a number.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from treesitter_extractors import extract_all, known_symbol_names

TARGETS = {
    "typescript": {
        "root": Path("/Users/ksnaik/sample_repos/domain-driven-hexagon"),
        "files": [
            "src/app.module.ts",
            "src/libs/guard.ts",
            "src/configs/app.routes.ts",
            "src/modules/user/commands/create-user/create-user.http.controller.ts",
            "src/modules/user/database/user.repository.ts",
        ],
    },
    "java": {
        "root": Path("/Users/ksnaik/sample_repos/spring-boot-demo"),
        "files": [
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/handler/DelayQueueHandler.java",
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/config/RabbitMqConfig.java",
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/handler/QueueThreeHandler.java",
        ],
    },
    "go": {
        "root": Path("/Users/ksnaik/sample_repos/simplebank"),
        "files": ["token/paseto_maker.go", "token/jwt_maker.go", "token/payload.go", "main.go"],
    },
    "rust": {
        "root": Path("/Users/ksnaik/sample_repos/Rocket"),
        "files": [
            "contrib/dyn_templates/src/fairing.rs",
            "contrib/dyn_templates/src/metadata.rs",
            "contrib/dyn_templates/src/template.rs",
        ],
    },
}


def main() -> int:
    random.seed(7)
    all_results: dict[str, list[dict]] = {}
    for lang, cfg in TARGETS.items():
        sources: dict[str, str] = {}
        for rel in cfg["files"]:
            path = cfg["root"] / rel
            if not path.is_file():
                print(f"  (skip, not found: {rel})")
                continue
            sources[rel] = path.read_text(encoding="utf-8", errors="replace")

        known_names = known_symbol_names(sources, language=lang)

        edges: list[dict] = []
        for rel, source in sources.items():
            try:
                edges.extend(extract_all(source, file=rel, language=lang, known_names=known_names))
            except Exception as exc:  # tree-sitter parse or walk failure
                print(f"  (skip {rel}: {exc})")
        all_results[lang] = edges
        print(f"\n=== {lang}: {len(sources)} files scanned, {len(known_names)} known symbol names, {len(edges)} edges (filtered) ===")
        sample = random.sample(edges, min(12, len(edges)))
        for e in sample:
            print(f"  [{e['kind']}] {e['user_symbol']} -> {e['used_symbol']}  ({e['file']}:{e['line']})")

    out_path = Path(__file__).parent / "cross_language_edges.json"
    out_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
