"""Translate Multi-SWE-bench records into the SWE-bench harness schema.

Multi-SWE-bench names the same things differently (`fix_patch` for `patch`,
`base.sha` for `base_commit`, `org`/`repo` split). Converting here keeps the
evaluation harness itself untouched and identical to the frozen version.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--candidates", required=True, type=Path)
parser.add_argument("--shards", required=True, type=Path)
parser.add_argument("--limit", type=int, default=10)
parser.add_argument("--out-dataset", required=True, type=Path)
parser.add_argument("--out-selected", required=True, type=Path)
options = parser.parse_args()

applicable = [c for c in json.loads(options.candidates.read_text()) if c["applicable"]]
wanted = {c["instance_id"] for c in applicable}

rows = []
for shard in sorted(options.shards.glob("*.jsonl")):
    for line in shard.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a shard truncated by an interrupted download
        if record.get("instance_id") not in wanted:
            continue
        rows.append({
            "instance_id": record["instance_id"],
            "repo": f"{record['org']}/{record['repo']}",
            "base_commit": (record.get("base") or {}).get("sha"),
            "patch": record.get("fix_patch") or "",
            "test_patch": record.get("test_patch") or "",
            "difficulty": "n/a",
        })

rows.sort(key=lambda r: r["instance_id"])
selected = [r["instance_id"] for r in rows][: options.limit]
options.out_dataset.write_text(json.dumps(rows, indent=2), encoding="utf-8")
options.out_selected.write_text(json.dumps(selected, indent=2), encoding="utf-8")
print(f"applicable={len(applicable)} converted={len(rows)} executing={len(selected)}")
for iid in selected:
    print("  ", iid)
