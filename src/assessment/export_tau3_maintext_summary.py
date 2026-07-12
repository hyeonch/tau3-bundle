"""Produce compact τ³ manuscript summary tables."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.assessment.validate_tau3_assessments import (
    manifest_index,
    read_jsonl,
    sha256_file,
    validate_automated_assessment,
)


DEFAULT_CASES = Path("results/tau3/annotation_cases.jsonl")
DEFAULT_MANIFEST = Path("results/tau3/assessment/routing_manifest.json")
DEFAULT_ASSESSMENTS = Path("results/tau3/assessment/automated_assessments.jsonl")
DEFAULT_PROVENANCE = Path("results/tau3/assessment/judge_provenance.jsonl")
DEFAULT_PRIVATE_KEY = Path("results/tau3/policy_packet/annotation_packet_private_key.csv")
DEFAULT_STABILITY = Path("results/tau3/assessment/stability_subset.json")
DEFAULT_OUTPUT = Path("results/tau3/assessment/main_text_summary.json")
DEFAULT_ASSESSMENT_SCHEMA = Path("schemas/tau3-automated-assessment.schema.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export τ³ manuscript summary without trajectories or citations")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--assessments", type=Path, default=DEFAULT_ASSESSMENTS)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--stability-subset", type=Path, default=DEFAULT_STABILITY)
    parser.add_argument("--stability-assessments", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--assessment-schema", type=Path, default=DEFAULT_ASSESSMENT_SCHEMA)
    args = parser.parse_args()

    cases = {row["selection_id"]: row for row in read_jsonl(args.cases)}
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    assessments = index_by_id(read_jsonl(args.assessments), "assessment")
    if set(assessments) != set(cases):
        raise ValueError("main-text export requires exactly one assessment for every blind case")
    manifest_sha256 = sha256_file(args.manifest)
    for selection_id, assessment in assessments.items():
        validate_automated_assessment(
            assessment, cases[selection_id], manifest, args.assessment_schema, manifest_sha256
        )
    key_rows = index_by_id(read_csv(args.private_key), "private-key row")
    if set(key_rows) != set(cases):
        raise ValueError("private key must cover exactly the assessed blind cases")
    provenance = index_by_id(read_jsonl(args.provenance), "provenance")
    if set(provenance) != set(cases):
        raise ValueError("provenance must cover exactly the assessed blind cases")

    output = {
        "schema_version": "tau3-main-text-summary-v0.1.0",
        "export_boundary": {
            "contains_raw_trajectory": False,
            "contains_task_context": False,
            "contains_citation_quotes": False,
            "contains_selection_ids": False,
        },
        "source": {
            "cases_sha256": sha256_file(args.cases),
            "manifest_sha256": manifest_sha256,
            "assessments_sha256": sha256_file(args.assessments),
            "provenance_sha256": sha256_file(args.provenance),
            "private_key_sha256": sha256_file(args.private_key),
        },
        "sample": {"runs": len(assessments), "groups": group_counts(key_rows.values())},
        "outcomes": summarize_outcomes(assessments, key_rows),
        "native_reward_cross_tab": reward_cross_tab(assessments, key_rows),
        "judge_operations": judge_operations(provenance.values()),
        "stability": summarize_stability(args.stability_subset, args.stability_assessments),
    }
    atomic_write_json(args.output, output)
    print(f"[export:finished] runs={len(assessments)} output={args.output}")


def summarize_outcomes(assessments: dict[str, dict[str, Any]], key_rows: dict[str, dict[str, str]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for selection_id, assessment in assessments.items():
        key = key_rows[selection_id]
        grouped[(key["study_group"], key["domain"], key["agent_model"])].append(assessment)
    return {
        "overall": outcome_counts(assessments.values()),
        "by_study_group_domain_model": [
            {
                "study_group": group[0], "domain": group[1], "agent_model": group[2],
                **outcome_counts(rows),
            }
            for group, rows in sorted(grouped.items())
        ],
    }


def outcome_counts(rows: Any) -> dict[str, Any]:
    rows = list(rows)
    deterministic = Counter(row["summary"]["deterministic_certificate"]["status"] for row in rows)
    hybrid = Counter(row["summary"]["hybrid_assessment"]["status"] for row in rows)
    return {
        "n": len(rows),
        "deterministic_certificate": dict(sorted(deterministic.items())),
        "hybrid_assessment": dict(sorted(hybrid.items())),
        "hybrid_pass_rate": rate_with_wilson(hybrid["hybrid_pass"], len(rows)),
        "hybrid_fail_rate": rate_with_wilson(hybrid["hybrid_fail"], len(rows)),
        "hybrid_unresolved_rate": rate_with_wilson(hybrid["unresolved"], len(rows)),
    }


def reward_cross_tab(assessments: dict[str, dict[str, Any]], key_rows: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    table: Counter[tuple[str, str]] = Counter()
    for selection_id, assessment in assessments.items():
        table[(key_rows[selection_id]["native_reward"], assessment["summary"]["hybrid_assessment"]["status"])] += 1
    return [
        {"native_reward": reward, "hybrid_status": status, "n": count}
        for (reward, status), count in sorted(table.items())
    ]


def judge_operations(rows: Any) -> dict[str, Any]:
    rows = list(rows)
    status = Counter(row["status"] for row in rows)
    models = Counter(row["model"] for row in rows)
    usage = {field: sum((row["usage"].get(field) or 0) for row in rows) for field in ("input_tokens", "output_tokens", "cost_usd")}
    return {"runs": len(rows), "status": dict(sorted(status.items())), "models": dict(sorted(models.items())), "usage": usage}


def summarize_stability(subset_path: Path, assessment_paths: list[Path]) -> dict[str, Any] | None:
    if not assessment_paths:
        return None
    subset = json.loads(subset_path.read_text(encoding="utf-8"))
    ids = {selection_id for cluster in subset["task_clusters"] for selection_id in cluster["run_selection_ids"]}
    repeats = [index_by_id(read_jsonl(path), f"stability assessment {path}") for path in assessment_paths]
    if any(set(repeat) != ids for repeat in repeats):
        raise ValueError("every stability assessment file must contain exactly the frozen 96-run subset")
    patterns = Counter(
        tuple(repeat[selection_id]["summary"]["hybrid_assessment"]["status"] for repeat in repeats)
        for selection_id in ids
    )
    return {
        "runs": len(ids), "repetitions": len(repeats),
        "exact_agreement_runs": sum(count for pattern, count in patterns.items() if len(set(pattern)) == 1),
        "status_patterns": [
            {"statuses": list(pattern), "n": count}
            for pattern, count in sorted(patterns.items())
        ],
    }


def rate_with_wilson(successes: int, total: int) -> dict[str, float | int]:
    if total == 0:
        return {"numerator": 0, "denominator": 0, "rate": 0.0, "ci95_lower": 0.0, "ci95_upper": 0.0}
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return {"numerator": successes, "denominator": total, "rate": rate, "ci95_lower": max(0.0, centre - radius), "ci95_upper": min(1.0, centre + radius)}


def group_counts(rows: Any) -> dict[str, dict[str, int]]:
    rows = list(rows)
    fields = ("study_group", "domain", "agent_model", "family")
    return {field: dict(sorted(Counter(row[field] for row in rows).items())) for field in fields}


def index_by_id(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed = {row["selection_id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"duplicate {label} selection_id")
    return indexed


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
