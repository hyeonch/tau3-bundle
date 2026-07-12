"""Preflight the τ³ input bundle and record its frozen input hashes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from src.assessment.validate_tau3_assessments import read_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a τ³ run manifest")
    parser.add_argument("--cases", type=Path, default=Path("results/tau3/annotation_cases.jsonl"))
    parser.add_argument("--private-key", type=Path, default=Path("results/tau3/policy_packet/annotation_packet_private_key.csv"))
    parser.add_argument("--manifest", type=Path, default=Path("results/tau3/assessment/routing_manifest.json"))
    parser.add_argument("--prompt", type=Path, default=Path("results/tau3/assessment/judge_prompt.md"))
    parser.add_argument("--rubric", type=Path, default=Path("results/tau3/assessment/judge_rubric.md"))
    parser.add_argument("--stability", type=Path, default=Path("results/tau3/assessment/stability_subset.json"))
    parser.add_argument("--output", type=Path, default=Path("results/tau3/assessment/run_manifest.json"))
    args = parser.parse_args()
    cases = read_jsonl(args.cases)
    case_ids = {row["selection_id"] for row in cases}
    with args.private_key.open(encoding="utf-8", newline="") as handle:
        key_rows = list(csv.DictReader(handle))
    key_ids = {row["selection_id"] for row in key_rows}
    if len(cases) != len(case_ids) or len(key_rows) != len(key_ids) or case_ids != key_ids:
        raise ValueError("blind cases and private key must have identical unique selection_ids")
    stability = json.loads(args.stability.read_text(encoding="utf-8"))
    stability_ids = {item for cluster in stability["task_clusters"] for item in cluster["run_selection_ids"]}
    if len(stability_ids) != 96 or not stability_ids <= case_ids:
        raise ValueError("frozen stability subset must contain 96 selected blind cases")
    payload = {
        "schema_version": "tau3-run-manifest-v0.1.0",
        "counts": {"blind_cases": len(cases), "stability_cases": len(stability_ids)},
        "inputs": {path.name: {"path": path.as_posix(), "sha256": sha256(path)} for path in (args.cases, args.private_key, args.manifest, args.prompt, args.rubric, args.stability)},
        "outputs": {
            "internal": ["judge_outputs.jsonl", "judge_provenance.jsonl", "automated_assessments.jsonl"],
            "main_text_summary": ["main_text_summary.json"],
            "detailed_results": ["automated assessments", "provenance"],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[preflight:ready] cases={len(cases)} stability_cases={len(stability_ids)} manifest={args.output}")


def sha256(path: Path) -> str:
    if not path.is_dir():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(path.glob("*.jsonl")):
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    main()
