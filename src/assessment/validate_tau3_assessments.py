from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


DEFAULT_CASES = Path("results/tau3/annotation_cases.jsonl")
DEFAULT_MANIFEST = Path("results/tau3/assessment/routing_manifest.json")
DEFAULT_ASSESSMENT_SCHEMA = Path("schemas/tau3-automated-assessment.schema.json")
DEFAULT_JUDGE_SCHEMA = Path("schemas/tau3-judge-output.schema.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate tau3 automated assessments against cases and routing")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--assessments", type=Path)
    parser.add_argument("--judge-outputs", type=Path)
    parser.add_argument("--assessment-schema", type=Path, default=DEFAULT_ASSESSMENT_SCHEMA)
    parser.add_argument("--judge-schema", type=Path, default=DEFAULT_JUDGE_SCHEMA)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    if bool(args.assessments) == bool(args.judge_outputs):
        raise ValueError("provide exactly one of --assessments or --judge-outputs")

    cases = {case["selection_id"]: case for case in read_jsonl(args.cases)}
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_sha256 = sha256_file(args.manifest)
    rows_path = args.assessments or args.judge_outputs
    rows = read_jsonl(rows_path)
    seen: set[str] = set()
    for row in rows:
        selection_id = row.get("selection_id")
        if selection_id in seen:
            raise ValueError(f"duplicate output for {selection_id}")
        seen.add(selection_id)
        case = cases.get(selection_id)
        if case is None:
            raise ValueError(f"output references unknown case {selection_id}")
        if args.assessments:
            validate_automated_assessment(
                row, case, manifest, args.assessment_schema, manifest_sha256
            )
        else:
            validate_judge_output(row, case, manifest, args.judge_schema)

    if args.require_complete and set(cases) != seen:
        raise ValueError(f"expected {len(cases)} outputs, found {len(seen)}")
    print(f"validated {len(rows)} outputs against {len(cases)} blind cases")


def validate_judge_output(
    output: dict[str, Any],
    case: dict[str, Any],
    manifest: dict[str, Any],
    schema_path: Path,
) -> None:
    validate_schema(output, schema_path, "judge output")
    validate_case_linkage(output, case)
    routes = manifest_index(manifest)
    case_clauses = {clause["id"]: clause for clause in case["policy"]["clauses"]}
    required = {
        clause_id
        for clause_id in case_clauses
        if routes[clause_id]["applicability"]["mode"] == "llm_judge"
        or routes[clause_id]["outcome"]["mode"] == "llm_judge"
    }
    decisions = index_unique(output["decisions"], "clause_id", "judge decision")
    if set(decisions) != required:
        raise ValueError("judge output must contain every and only LLM-routed clause")

    for clause_id, decision in decisions.items():
        route = routes[clause_id]
        policy_clause = case_clauses[clause_id]
        app_required = route["applicability"]["mode"] == "llm_judge"
        outcome_required = route["outcome"]["mode"] == "llm_judge"
        if app_required != (decision["applicability"] is not None):
            raise ValueError(f"judge applicability target mismatch: {clause_id}")
        if not outcome_required and decision["outcome"] is not None:
            raise ValueError(f"judge must not overwrite deterministic outcome: {clause_id}")

        app = decision["applicability"]
        if app is not None:
            validate_policy_applicability(policy_clause, app["value"])
            validate_evidence_list(app["evidence"], case)
            if app["value"] == "applicable" and not app["evidence"]:
                raise ValueError(f"applicable judge decision lacks trigger evidence: {clause_id}")
            if clause_id in {
                "banking.obligation.current_time_tool",
                "banking.obligation.log_verification",
            } and app["value"] == "applicable":
                if any(item["source"] != "trajectory_step" for item in app["evidence"]):
                    raise ValueError(f"banking reverse-hybrid applicability needs trajectory target evidence: {clause_id}")
        if outcome_required:
            app_value = app["value"] if app is not None else "applicable"
            should_decide = app_value == "applicable"
            if should_decide != (decision["outcome"] is not None):
                raise ValueError(f"judge outcome target mismatch after applicability: {clause_id}")
        outcome = decision["outcome"]
        if outcome is not None:
            if outcome["value"] in {"pass", "fail"} and not outcome["evidence"]:
                raise ValueError(f"decided judge outcome lacks evidence: {clause_id}")
            validate_evidence_list(outcome["evidence"], case)


def validate_automated_assessment(
    assessment: dict[str, Any],
    case: dict[str, Any],
    manifest: dict[str, Any],
    schema_path: Path,
    manifest_sha256: str,
) -> None:
    validate_schema(assessment, schema_path, "automated assessment")
    validate_case_linkage(assessment, case)
    if assessment["routing_manifest_version"] != manifest["routing_version"]:
        raise ValueError("assessment routing version does not match manifest")
    if assessment["routing_manifest_sha256"] != manifest_sha256:
        raise ValueError("assessment routing hash does not match manifest file")
    if assessment["case_sha256"] != canonical_sha256(case):
        raise ValueError("assessment case hash does not match blind case")

    routes = manifest_index(manifest)
    case_clauses = {clause["id"]: clause for clause in case["policy"]["clauses"]}
    results = index_unique(assessment["clauses"], "clause_id", "assessment clause")
    if set(results) != set(case_clauses):
        raise ValueError("assessment must contain every and only supplied policy clause")

    for clause_id, result in results.items():
        route = routes[clause_id]
        policy_clause = case_clauses[clause_id]
        if result["clause_type"] != policy_clause["type"]:
            raise ValueError(f"clause type mismatch: {clause_id}")
        if result["assessment_route"] != route["assessment_route"]:
            raise ValueError(f"assessment route mismatch: {clause_id}")
        if result["applicability_mode"] != route["applicability"]["mode"]:
            raise ValueError(f"applicability route mismatch: {clause_id}")
        if result["outcome_mode"] != route["outcome"]["mode"]:
            raise ValueError(f"outcome route mismatch: {clause_id}")
        validate_final_decision(result, policy_clause, route, case)

    expected = build_summary(list(results.values()))
    if assessment["summary"] != expected:
        raise ValueError("assessment summary does not match clause results")


def validate_final_decision(
    result: dict[str, Any],
    policy_clause: dict[str, Any],
    route: dict[str, Any],
    case: dict[str, Any],
) -> None:
    app = result["applicability"]
    outcome = result["outcome"]
    if app["procedure_id"] != route["applicability"]["procedure_id"]:
        raise ValueError(f"applicability procedure mismatch: {result['clause_id']}")
    if outcome["procedure_id"] != route["outcome"]["procedure_id"]:
        raise ValueError(f"outcome procedure mismatch: {result['clause_id']}")
    validate_policy_applicability(policy_clause, app["value"])
    validate_provenance(app, route["applicability"]["mode"])
    validate_provenance(outcome, route["outcome"]["mode"])
    validate_evidence_list(app["evidence"], case)
    validate_evidence_list(outcome["evidence"], case)

    if app["value"] in {"not_applicable", "not_triggered"}:
        if outcome["value"] != "not_applicable_outcome" or result["margin"] is not None:
            raise ValueError(f"non-applicable clause has an outcome margin: {result['clause_id']}")
        if outcome["provenance"] != app["provenance"]:
            raise ValueError(f"non-applicable outcome loses applicability provenance: {result['clause_id']}")
        return
    if app["value"] == "unknown":
        if outcome["value"] != "unknown":
            raise ValueError(f"unknown applicability must keep unknown outcome: {result['clause_id']}")
    elif outcome["value"] not in {"pass", "fail", "unknown"}:
        raise ValueError(f"applicable clause has invalid outcome: {result['clause_id']}")

    expected_margin = {
        "pass": {"lower": 1.0, "upper": 1.0, "evidence_status": outcome["provenance"]},
        "fail": {"lower": -1.0, "upper": -1.0, "evidence_status": outcome["provenance"]},
        "unknown": {"lower": -1.0, "upper": 1.0, "evidence_status": "missing"},
    }[outcome["value"]]
    if result["margin"] != expected_margin:
        raise ValueError(f"margin does not match outcome: {result['clause_id']}")
    if outcome["value"] in {"pass", "fail"} and not outcome["evidence"]:
        raise ValueError(f"decided outcome lacks direct evidence: {result['clause_id']}")


def validate_provenance(decision: dict[str, Any], mode: str) -> None:
    value = decision["value"]
    if value in {"unknown", "not_applicable_outcome"}:
        expected = "missing" if value == "unknown" else decision["provenance"]
    elif mode == "deterministic":
        expected = "verified"
    elif mode == "llm_judge":
        expected = "inferred"
    else:
        expected = "missing"
    if decision["provenance"] != expected:
        raise ValueError(f"{mode} decision has invalid provenance {decision['provenance']}")


def validate_policy_applicability(policy_clause: dict[str, Any], value: str) -> None:
    if value not in {"applicable", "not_applicable", "not_triggered", "unknown"}:
        raise ValueError(f"invalid applicability value {value}: {policy_clause['id']}")
    policy_state = policy_clause["applicability"]
    if policy_state == "always" and value != "applicable":
        raise ValueError(f"always clause cannot be {value}: {policy_clause['id']}")
    if policy_state == "conditional" and value == "not_triggered":
        raise ValueError(f"conditional clause cannot be not_triggered: {policy_clause['id']}")
    if policy_state == "triggered" and value == "not_applicable":
        raise ValueError(f"triggered clause cannot be not_applicable: {policy_clause['id']}")


def validate_evidence_list(evidence: list[dict[str, Any]], case: dict[str, Any]) -> None:
    events = {event["step"]: event for event in case["trajectory"]}
    for item in evidence:
        quote = item["quote"]
        if item["source"] == "trajectory_step":
            if not isinstance(item["step"], int):
                raise ValueError("trajectory_step citation requires an integer step")
            event = events.get(item["step"])
            if event is None:
                raise ValueError(f"citation references missing trajectory step {item['step']}")
            if not quote_in_source(quote, event):
                raise ValueError(f"citation quote not found at trajectory step {item['step']}")
        else:
            if item["step"] is not None:
                raise ValueError("task_context citation requires a null step")
            if not quote_in_source(quote, case["task_context"]):
                raise ValueError("citation quote not found in task context")


def quote_in_source(quote: str, source: Any) -> bool:
    """Accept exact or display-normalized contiguous source citations.

    Tool result content can itself be JSON text. Re-serializing the outer event
    escapes that inner text, so a literal quote copied from visible tool output
    must be matched against string fields as well as canonical event JSON. If
    exact matching fails, display-only Markdown delimiters, quote glyphs, and
    JSON punctuation are ignored while preserving the ordered word/number token
    sequence. This accepts ``HAT266`` inside ``**HAT266**`` but not a paraphrase
    or a reordered/non-contiguous citation.
    """
    candidates = [canonical_text(source), *string_values(source)]
    if any(quote in text for text in candidates):
        return True
    quote_tokens = citation_tokens(quote)
    return bool(quote_tokens) and any(
        contiguous_tokens(quote_tokens, citation_tokens(text)) for text in candidates
    )


def citation_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def contiguous_tokens(needle: list[str], haystack: list[str]) -> bool:
    width = len(needle)
    return any(haystack[index:index + width] == needle for index in range(len(haystack) - width + 1))


def string_values(value: Any):
    if isinstance(value, str):
        yield value
        # Some trajectory content fields are JSON wrapper strings, e.g.
        # {"message": "line one\\nline two"}. The judge sees the decoded
        # message text, while the blind case retains the wrapper literal.
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if decoded != value:
            yield from string_values(decoded)
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    hybrid_intervals: list[tuple[str, float, float]] = []
    certificate_intervals: list[tuple[str, float, float]] = []
    not_applicable = 0
    unknown = 0
    deterministic = 0
    judge_dependent = 0

    for result in results:
        route = result["assessment_route"]
        deterministic += int(route == "deterministic")
        judge_dependent += int(route in {"hybrid", "llm_judge"})
        app_value = result["applicability"]["value"]
        if app_value in {"not_applicable", "not_triggered"}:
            not_applicable += 1
            if result["applicability"]["provenance"] != "verified":
                certificate_intervals.append((result["clause_id"], -1.0, 1.0))
            continue
        margin = result["margin"]
        if margin is None:
            raise ValueError(f"applicable clause lacks margin: {result['clause_id']}")
        if app_value == "unknown" or result["outcome"]["value"] == "unknown":
            unknown += 1
        hybrid_intervals.append((result["clause_id"], margin["lower"], margin["upper"]))
        if (
            result["applicability"]["provenance"] == "verified"
            and result["outcome"]["provenance"] == "verified"
        ):
            certificate_intervals.append((result["clause_id"], margin["lower"], margin["upper"]))
        else:
            certificate_intervals.append((result["clause_id"], -1.0, 1.0))

    certificate = summarize_intervals(certificate_intervals, "certified")
    hybrid = summarize_intervals(hybrid_intervals, "hybrid")
    return {
        "deterministic_certificate": certificate,
        "hybrid_assessment": hybrid,
        "coverage": {
            "total_clauses": len(results),
            "deterministic_clauses": deterministic,
            "judge_dependent_clauses": judge_dependent,
            "unknown_clauses": unknown,
            "not_applicable_clauses": not_applicable,
        },
    }


def summarize_intervals(intervals: list[tuple[str, float, float]], prefix: str) -> dict[str, Any]:
    if not intervals:
        return {
            "lower": -1.0,
            "upper": 1.0,
            "status": "unresolved",
            "bottleneck_clause_ids": [],
        }
    lower = min(item[1] for item in intervals)
    upper = min(item[2] for item in intervals)
    if lower >= 0:
        status = f"{prefix}_pass"
    elif upper < 0:
        status = f"{prefix}_fail"
    else:
        status = "unresolved"
    return {
        "lower": lower,
        "upper": upper,
        "status": status,
        "bottleneck_clause_ids": sorted(item[0] for item in intervals if item[1] == lower),
    }


def validate_case_linkage(output: dict[str, Any], case: dict[str, Any]) -> None:
    expected = {
        "selection_id": case["selection_id"],
        "blind_run_id": case["blind_run_id"],
    }
    if "policy_snapshot_id" in output:
        expected["policy_snapshot_id"] = case["policy"]["snapshot_id"]
    if "policy_snapshot_version" in output:
        expected["policy_snapshot_version"] = case["policy"]["snapshot_version"]
    for field, value in expected.items():
        if output[field] != value:
            raise ValueError(f"output {field} does not match blind case")


def validate_schema(value: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"invalid {label}: {errors[0].message}")


def manifest_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return index_unique(manifest["entries"], "clause_id", "routing entry")


def index_unique(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    indexed = {row[key]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"duplicate {label}")
    return indexed


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def canonical_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    if not path.is_dir():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(path.glob("*.jsonl")):
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    paths = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    if not paths:
        raise ValueError(f"no JSONL shards found at {path}")
    return [
        json.loads(line)
        for item in paths
        for line in item.read_text(encoding="utf-8").splitlines()
        if line
    ]


if __name__ == "__main__":
    main()
