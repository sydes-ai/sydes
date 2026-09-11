#!/usr/bin/env python3
"""Phase 6: cross-language member-access generalization check, mirroring
cross_language_check.py's structure. Reports parsed member accesses,
resolved vs unknown counts, and a sample for manual sanity inspection.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from member_access_treesitter import extract_member_access, known_type_names

TARGETS = {
    "typescript": {
        "root": Path("/Users/ksnaik/sample_repos/domain-driven-hexagon"),
        "files": [
            "src/app.module.ts",
            "src/libs/guard.ts",
            "src/modules/user/commands/create-user/create-user.http.controller.ts",
            "src/modules/user/database/user.repository.ts",
            "src/modules/user/domain/user.entity.ts",
        ],
    },
    "java": {
        "root": Path("/Users/ksnaik/sample_repos/spring-boot-demo"),
        "files": [
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/handler/DelayQueueHandler.java",
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/config/RabbitMqConfig.java",
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/handler/QueueThreeHandler.java",
            "demo-mq-rabbitmq/src/main/java/com/xkcoding/mq/rabbitmq/constants/RabbitConsts.java",
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
    random.seed(11)
    summary_rows = []
    all_results: dict[str, list[dict]] = {}

    for lang, cfg in TARGETS.items():
        sources: dict[str, str] = {}
        for rel in cfg["files"]:
            path = cfg["root"] / rel
            if path.is_file():
                sources[rel] = path.read_text(encoding="utf-8", errors="replace")

        known = known_type_names(sources, language=lang)

        entries: list[dict] = []
        for rel, source in sources.items():
            try:
                entries.extend(extract_member_access(source, file=rel, language=lang, known_names=known))
            except Exception as exc:
                print(f"  (skip {rel}: {exc})")

        all_results[lang] = entries
        resolved = [e for e in entries if e["status"] == "resolved"]
        unknown = [e for e in entries if e["status"] == "unknown"]
        print(f"\n=== {lang}: {len(sources)} files, {len(entries)} member accesses parsed, "
              f"{len(resolved)} resolved, {len(unknown)} unknown ===")
        sample = random.sample(resolved, min(8, len(resolved)))
        for e in sample:
            print(f"  RESOLVED [{e['function']}] {e['receiver']}.{e['member']} -> {e['resolved_type']}  ({e['file']}:{e['line']})")

        summary_rows.append({
            "language": lang, "files": len(sources), "parsed": len(entries),
            "resolved": len(resolved), "unknown": len(unknown),
        })

    out_path = Path(__file__).parent / "cross_language_member_access.json"
    out_path.write_text(json.dumps({"summary": summary_rows, "entries": all_results}, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
