"""Assemble deterministic and judge outputs into validated final τ³ assessments."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from src.assessment.run_tau3_judge import atomic_write_jsonl
from src.assessment.validate_tau3_assessments import (
    build_summary,
    canonical_sha256,
    index_unique,
    manifest_index,
    read_jsonl,
    sha256_file,
    validate_automated_assessment,
    validate_judge_output,
)
from src.assessment.verifiers import airline, banking_knowledge, retail, telecom
from src.assessment.verifiers.evidence import decision, unknown_decision


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path("results/tau3/annotation_cases.jsonl")
DEFAULT_MANIFEST = Path("results/tau3/assessment/routing_manifest.json")
DEFAULT_JUDGE_OUTPUTS = Path("results/tau3/assessment/judge_outputs.jsonl")
DEFAULT_PROVENANCE = Path("results/tau3/assessment/judge_provenance.jsonl")
DEFAULT_OUTPUT = Path("results/tau3/assessment/automated_assessments.jsonl")
DEFAULT_ASSESSMENT_SCHEMA = Path("schemas/tau3-automated-assessment.schema.json")
DEFAULT_JUDGE_SCHEMA = Path("schemas/tau3-judge-output.schema.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble validated τ³ hybrid automated assessments")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--judge-outputs", type=Path, default=DEFAULT_JUDGE_OUTPUTS)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--assessment-schema", type=Path, default=DEFAULT_ASSESSMENT_SCHEMA)
    parser.add_argument("--judge-schema", type=Path, default=DEFAULT_JUDGE_SCHEMA)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    cases = read_jsonl(args.cases)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    routes = manifest_index(manifest)
    judge_outputs = index_unique(read_jsonl(args.judge_outputs), "selection_id", "judge output")
    provenance = index_unique(read_jsonl(args.provenance), "selection_id", "judge provenance")
    case_ids = {case["selection_id"] for case in cases}
    if args.require_complete and (set(judge_outputs) != case_ids or set(provenance) != case_ids):
        raise ValueError("complete judge outputs and provenance are required for every blind case")

    assessments = []
    for ordinal, case in enumerate(cases, start=1):
        selection_id = case["selection_id"]
        output = judge_outputs.get(selection_id)
        row = provenance.get(selection_id)
        if output is None or row is None:
            raise ValueError(f"missing judge output or provenance for {selection_id}")
        if row["status"] != "valid":
            raise ValueError(f"judge provenance is not valid for {selection_id}")
        validate_judge_output(output, case, manifest, args.judge_schema)
        assessment = assemble_case(case, manifest, output, row, sha256_file(args.manifest))
        validate_automated_assessment(assessment, case, manifest, args.assessment_schema, sha256_file(args.manifest))
        assessments.append(assessment)
        atomic_write_jsonl(args.output, assessments)
        print(f"[assemble:case_persisted] ordinal={ordinal} selection_id={selection_id}")
    print(f"[assemble:finished] assessments={len(assessments)} output={args.output}")


def assemble_case(
    case: dict[str, Any], manifest: dict[str, Any], judge_output: dict[str, Any],
    provenance: dict[str, Any], manifest_sha256: str,
) -> dict[str, Any]:
    routes = manifest_index(manifest)
    judge = index_unique(judge_output["decisions"], "clause_id", "judge decision")
    deterministic = deterministic_results(case, judge, routes)
    clauses = []
    for policy_clause in case["policy"]["clauses"]:
        clause_id = policy_clause["id"]
        route = routes[clause_id]
        source = deterministic.get(clause_id, {})
        app = final_applicability(route, source.get("applicability"), judge.get(clause_id))
        outcome = final_outcome(route, app, source.get("outcome"), judge.get(clause_id))
        clauses.append({
            "clause_id": clause_id,
            "clause_type": policy_clause["type"],
            "assessment_route": route["assessment_route"],
            "applicability_mode": route["applicability"]["mode"],
            "outcome_mode": route["outcome"]["mode"],
            "applicability": app,
            "outcome": outcome,
            "margin": margin_for(app, outcome),
        })
    return {
        "schema_version": "0.1.0",
        "protocol_version": "tau3-hybrid-assessment-v0.1.0",
        "routing_manifest_version": manifest["routing_version"],
        "routing_manifest_sha256": manifest_sha256,
        "selection_id": case["selection_id"],
        "blind_run_id": case["blind_run_id"],
        "policy_snapshot_id": case["policy"]["snapshot_id"],
        "policy_snapshot_version": case["policy"]["snapshot_version"],
        "case_sha256": canonical_sha256(case),
        "execution": {
            "deterministic_bundle_version": "tau3-deterministic-v0.1.0",
            "judge": judge_execution(provenance),
            "retry_count": max(0, provenance["attempts"] - 1),
        },
        "clauses": clauses,
        "summary": build_summary(clauses),
        "validation": {
            "case_linkage_valid": True,
            "route_manifest_linkage_valid": True,
            "citation_steps_valid": True,
        },
    }


def deterministic_results(
    case: dict[str, Any], judge: dict[str, dict[str, Any]], routes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    domain = case["policy"]["domain"]
    assessors: dict[str, Callable[[dict[str, Any], dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]] = {
        "airline": airline.assess_case,
        "retail": retail.assess_case,
        "telecom": telecom.assess_case,
        "banking_knowledge": banking_knowledge.assess_case,
    }
    if domain not in assessors:
        raise ValueError(f"unsupported τ³ domain: {domain}")
    results = assessors[domain](case, routes)
    if domain == "banking_knowledge":
        results.update(banking_knowledge.resolve_hybrid_outcomes(case, judge, routes))
    return results


def final_applicability(
    route: dict[str, Any], deterministic: dict[str, Any] | None,
    judge: dict[str, Any] | None,
) -> dict[str, Any]:
    mode = route["applicability"]["mode"]
    if mode == "deterministic":
        if deterministic is None:
            raise ValueError(f"missing deterministic applicability for {route['clause_id']}")
        return deterministic
    if mode == "llm_judge":
        if judge is None or judge["applicability"] is None:
            raise ValueError(f"missing judge applicability for {route['clause_id']}")
        return inferred(judge["applicability"], route["applicability"]["procedure_id"])
    return unknown_decision(route["applicability"]["procedure_id"], "no frozen procedure is available")


def final_outcome(
    route: dict[str, Any], app: dict[str, Any], deterministic: dict[str, Any] | None,
    judge: dict[str, Any] | None,
) -> dict[str, Any]:
    procedure_id = route["outcome"]["procedure_id"]
    if app["value"] in {"not_applicable", "not_triggered"}:
        return decision("not_applicable_outcome", app["provenance"], procedure_id, [], app["reason"])
    if app["value"] == "unknown":
        return unknown_decision(procedure_id, "applicability is unresolved")
    mode = route["outcome"]["mode"]
    if mode == "deterministic":
        if deterministic is None:
            raise ValueError(f"missing deterministic outcome for {route['clause_id']}")
        return deterministic
    if mode == "llm_judge":
        if judge is None or judge["outcome"] is None:
            raise ValueError(f"missing judge outcome for {route['clause_id']}")
        return inferred(judge["outcome"], procedure_id)
    return unknown_decision(procedure_id, "no frozen procedure is available")


def inferred(value: dict[str, Any], procedure_id: str) -> dict[str, Any]:
    provenance = "missing" if value["value"] == "unknown" else "inferred"
    return decision(value["value"], provenance, procedure_id, value["evidence"], value["reason"])


def margin_for(app: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any] | None:
    if app["value"] in {"not_applicable", "not_triggered"}:
        return None
    values = {
        "pass": (1.0, 1.0, outcome["provenance"]),
        "fail": (-1.0, -1.0, outcome["provenance"]),
        "unknown": (-1.0, 1.0, "missing"),
    }
    lower, upper, status = values[outcome["value"]]
    return {"lower": lower, "upper": upper, "evidence_status": status}


def judge_execution(provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": provenance["provider"],
        "model": provenance["model"],
        "model_snapshot": None,
        "prompt_sha256": provenance["prompt_sha256"],
        "temperature": None,
        "reasoning_effort": None,
        "response_id": provenance["response_id"],
        "repetition_index": provenance["repetition_index"],
        "generated_at": provenance["finished_at"],
        "usage": provenance["usage"],
    }


if __name__ == "__main__":
    main()
