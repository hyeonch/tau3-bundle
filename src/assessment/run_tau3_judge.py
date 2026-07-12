"""Minimal, versioned runner for the tau3 semantic judge.

It intentionally has one OpenAI-compatible chat-completions adapter.  It writes
valid judge decisions and a separate per-attempt provenance row; aggregation and
large-scale orchestration are handled by separate scripts.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from src.assessment.validate_tau3_assessments import (
    canonical_sha256,
    manifest_index,
    read_jsonl,
    validate_judge_output,
    validate_schema,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path("results/tau3/annotation_cases.jsonl")
DEFAULT_MANIFEST = Path("results/tau3/assessment/routing_manifest.json")
DEFAULT_PROMPT = Path("results/tau3/assessment/judge_prompt.md")
DEFAULT_RUBRIC = Path("results/tau3/assessment/judge_rubric.md")
DEFAULT_JUDGE_SCHEMA = Path("schemas/tau3-judge-output.schema.json")
DEFAULT_PROVENANCE_SCHEMA = Path("schemas/tau3-judge-provenance.schema.json")
DEFAULT_OUTPUT = Path("results/tau3/assessment/judge_outputs.jsonl")
DEFAULT_PROVENANCE_OUTPUT = Path("results/tau3/assessment/judge_provenance.jsonl")
DEFAULT_EVENT_LOG = Path("results/tau3/assessment/judge_events.jsonl")
DEFAULT_INVALID_OUTPUT_LOG = Path("results/tau3/assessment/judge_invalid_outputs.jsonl")
DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
PROTOCOL_VERSION = "tau3-semantic-judge-v0.5.0"


@dataclass(frozen=True)
class JudgeRequest:
    system_prompt: str
    user_prompt: str
    prompt_sha256: str


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None


class EventLogger:
    """Append-only operational log that never stores prompt or trajectory text."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        row = {"timestamp": utc_now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(canonical_json(row) + "\n")
        detail = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        print(f"[judge:{event}] {detail}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the minimal tau3 semantic judge")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    parser.add_argument("--judge-schema", type=Path, default=DEFAULT_JUDGE_SCHEMA)
    parser.add_argument("--provenance-schema", type=Path, default=DEFAULT_PROVENANCE_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--provenance-output", type=Path, default=DEFAULT_PROVENANCE_OUTPUT)
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--invalid-output-log", type=Path, default=DEFAULT_INVALID_OUTPUT_LOG,
                        help="local-only raw invalid judge responses for debugging; never publish")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--selection-file", type=Path, help="JSON artifact containing cases[].selection_id")
    parser.add_argument("--resume", action="store_true", help="preserve prior valid outputs and run only unfinished cases")
    parser.add_argument("--repetition-index", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--timeout-sec", type=float, default=240)
    parser.add_argument("--sleep-sec", type=float, default=0.2)
    args = parser.parse_args()
    if args.repetition_index < 0 or args.max_retries < 0:
        raise ValueError("repetition-index and max-retries must be non-negative")

    api_key = load_api_key(args.env)
    cases = read_jsonl(args.cases)
    if args.selection_file is not None:
        selected = json.loads(args.selection_file.read_text(encoding="utf-8"))
        selection_ids = [item["selection_id"] for item in selected["cases"]]
        by_id = {case["selection_id"]: case for case in cases}
        missing = [selection_id for selection_id in selection_ids if selection_id not in by_id]
        if missing:
            raise ValueError(f"selection file references missing cases: {missing}")
        cases = [by_id[selection_id] for selection_id in selection_ids]
    if args.limit is not None:
        cases = cases[: args.limit]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    prompt_source = args.prompt.read_text(encoding="utf-8")
    rubric_source = args.rubric.read_text(encoding="utf-8")
    judge_schema = json.loads(args.judge_schema.read_text(encoding="utf-8"))
    request_fn = openai_chat_request(
        args.endpoint, api_key, args.model, args.timeout_sec, structured_output_schema(judge_schema)
    )
    events = EventLogger(args.event_log)
    events.emit("run_started", model=args.model, provider=args.provider, case_count=len(cases),
                repetition_index=args.repetition_index, max_retries=args.max_retries)

    outputs = read_jsonl_if_exists(args.output) if args.resume else []
    prior_provenance = read_jsonl_if_exists(args.provenance_output) if args.resume else []
    output_by_id = {row["selection_id"]: row for row in outputs}
    if len(output_by_id) != len(outputs):
        raise ValueError("judge output file contains duplicate selection_id rows")
    provenance_by_id = {row["selection_id"]: row for row in prior_provenance}
    if len(provenance_by_id) != len(prior_provenance):
        raise ValueError("judge provenance file contains duplicate selection_id rows")
    # A crash can occur between the two atomic writes below.  Treat an output
    # without a matching valid provenance row as unfinished, remove that lone
    # output snapshot, and regenerate the pair on resume.
    incomplete_outputs = {
        selection_id for selection_id in output_by_id
        if provenance_by_id.get(selection_id, {}).get("status") != "valid"
    }
    if incomplete_outputs:
        outputs = [row for row in outputs if row["selection_id"] not in incomplete_outputs]
        output_by_id = {row["selection_id"]: row for row in outputs}
        atomic_write_jsonl(args.output, outputs)
    completed = set(output_by_id)
    events.emit("resume_state", enabled=args.resume, prior_valid_outputs=len(outputs),
                prior_provenance_rows=len(provenance_by_id), remaining_cases=sum(case["selection_id"] not in completed for case in cases))
    current_rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if case["selection_id"] in completed:
            events.emit("case_skipped_completed", ordinal=index + 1, selection_id=case["selection_id"])
            continue
        request = compose_request(case, manifest, prompt_source, rubric_source, judge_schema)
        events.emit("case_composed", ordinal=index + 1, total_cases=len(cases),
                    selection_id=case["selection_id"], blind_run_id=case["blind_run_id"],
                    prompt_sha256=request.prompt_sha256, prompt_characters=len(request.system_prompt) + len(request.user_prompt))
        output, row = run_one(
            case, manifest, request, request_fn, args.judge_schema, args.provider, args.model,
            args.endpoint, args.repetition_index, args.max_retries, event_fn=events.emit,
            invalid_output_fn=InvalidOutputLogger(args.invalid_output_log).append,
        )
        validate_schema(row, args.provenance_schema, "judge provenance")
        provenance_by_id[case["selection_id"]] = row
        current_rows.append(row)
        if output is not None:
            outputs.append(output)
        atomic_write_jsonl(args.output, outputs)
        atomic_write_jsonl(args.provenance_output, [
            provenance_by_id[item["selection_id"]]
            for item in cases if item["selection_id"] in provenance_by_id
        ])
        events.emit("case_persisted", ordinal=index + 1, selection_id=case["selection_id"],
                    status=row["status"], attempts=row["attempts"], valid_outputs=len(outputs),
                    provenance_rows=len(provenance_by_id))
        if index + 1 < len(cases):
            time.sleep(args.sleep_sec)

    failed = sum(row["status"] == "failed" for row in current_rows)
    events.emit("run_finished", valid_outputs=len(outputs), current_run_valid_outputs=sum(row["status"] == "valid" for row in current_rows),
                failed_cases=failed, total_cases=len(cases), prior_provenance_rows=len(prior_provenance))


def compose_request(
    case: dict[str, Any], manifest: dict[str, Any], prompt_source: str,
    rubric_source: str, judge_schema: dict[str, Any],
) -> JudgeRequest:
    system_prompt, user_template = extract_prompt_blocks(prompt_source)
    routes = manifest_index(manifest)
    targets = []
    for clause in case["policy"]["clauses"]:
        route = routes[clause["id"]]
        app_target = route["applicability"]["mode"] == "llm_judge"
        outcome_target = route["outcome"]["mode"] == "llm_judge"
        if app_target or outcome_target:
            targets.append({
                "clause_id": clause["id"], "clause_type": clause["type"],
                "policy_applicability": clause["applicability"], "description": clause["description"],
                "applicability_target": app_target, "outcome_target": outcome_target,
            })
    replacements = {
        "{{ROUTING_TARGETS_JSON}}": canonical_json(targets),
        "{{CASE_RELEVANT_RUBRIC_TEXT}}": select_rubric(rubric_source, {item["clause_id"] for item in targets}),
        "{{JUDGE_OUTPUT_SCHEMA_JSON}}": canonical_json(judge_schema),
        "{{BLIND_CASE_JSON}}": canonical_json(case),
    }
    user_prompt = user_template
    for token, replacement in replacements.items():
        if user_prompt.count(token) != 1:
            raise ValueError(f"judge prompt template must contain {token} exactly once")
        user_prompt = user_prompt.replace(token, replacement)
    digest = canonical_sha256({"system": system_prompt, "user": user_prompt})
    return JudgeRequest(system_prompt, user_prompt, digest)


def extract_prompt_blocks(source: str) -> tuple[str, str]:
    blocks = re.findall(r"```text\n(.*?)\n```", source, re.DOTALL)
    if len(blocks) != 2:
        raise ValueError("judge prompt source must contain exactly system and user text blocks")
    return blocks[0], blocks[1]


def select_rubric(source: str, clause_ids: set[str]) -> str:
    common_match = re.search(r"## Common rules\n(.*?)(?=\n## )", source, re.DOTALL)
    if common_match is None:
        raise ValueError("judge rubric has no Common rules section")
    rows = re.findall(r"^\| `([^`]+)` \|.*$", source, re.MULTILINE)
    found = {clause_id for clause_id in rows if clause_id in clause_ids}
    if found != clause_ids:
        raise ValueError(f"judge rubric missing case targets: {sorted(clause_ids - found)}")
    selected_rows = [line for line in source.splitlines() if re.match(r"^\| `([^`]+)` \|", line) and line.split("`")[1] in clause_ids]
    return "## Common rules\n" + common_match.group(1).strip() + "\n\n## Case clause rules\n" + "\n".join(selected_rows)


def run_one(
    case: dict[str, Any], manifest: dict[str, Any], request: JudgeRequest,
    request_fn: Callable[[JudgeRequest], ProviderResponse], judge_schema: Path, provider: str,
    model: str, endpoint: str, repetition_index: int, max_retries: int,
    event_fn: Callable[..., None] | None = None,
    invalid_output_fn: Callable[..., None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    started_at = utc_now()
    last_error: str | None = None
    response: ProviderResponse | None = None
    attempt_log: list[dict[str, Any]] = []
    active_request = request
    for attempt in range(1, max_retries + 2):
        emit_event(event_fn, "attempt_started", selection_id=case["selection_id"], attempt=attempt,
                   max_attempts=max_retries + 1)
        try:
            response = request_fn(active_request)
            output = parse_json_object(response.content)
            validate_judge_output(output, case, manifest, judge_schema)
            attempt_log.append(attempt_row(attempt, "valid", None, response))
            emit_event(event_fn, "attempt_valid", selection_id=case["selection_id"], attempt=attempt,
                       response_id=response.response_id, input_tokens=response.input_tokens,
                       output_tokens=response.output_tokens)
            return output, provenance_row(case, request, provider, model, endpoint, repetition_index, started_at, attempt_log, "valid", None, response)
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = f"{type(error).__name__}: {error}"
            attempt_log.append(attempt_row(attempt, "request_error", last_error, None))
            response = None
            emit_event(event_fn, "attempt_request_error", selection_id=case["selection_id"], attempt=attempt,
                       error=last_error)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"
            attempt_log.append(attempt_row(attempt, "invalid_output", last_error, response))
            emit_event(event_fn, "attempt_invalid_output", selection_id=case["selection_id"], attempt=attempt,
                       response_id=response.response_id if response else None, error=last_error)
            if response is not None and invalid_output_fn is not None:
                invalid_output_fn(case=case, attempt=attempt, request=request, response=response,
                                  validation_error=last_error)
            if attempt < max_retries + 1:
                active_request = targeted_repair_request(request, last_error)
    emit_event(event_fn, "case_failed", selection_id=case["selection_id"], attempts=len(attempt_log), error=last_error)
    return None, provenance_row(case, request, provider, model, endpoint, repetition_index, started_at, attempt_log, "failed", last_error, response)


def emit_event(event_fn: Callable[..., None] | None, event: str, **fields: Any) -> None:
    if event_fn is not None:
        event_fn(event, **fields)


def provenance_row(
    case: dict[str, Any], request: JudgeRequest, provider: str, model: str, endpoint: str,
    repetition_index: int, started_at: str, attempt_log: list[dict[str, Any]], status: str, failure: str | None,
    response: ProviderResponse | None,
) -> dict[str, Any]:
    return {
        "schema_version": "0.1.0", "selection_id": case["selection_id"], "blind_run_id": case["blind_run_id"],
        "judge_protocol_version": PROTOCOL_VERSION, "prompt_sha256": request.prompt_sha256,
        "provider": provider, "model": model, "endpoint": endpoint, "repetition_index": repetition_index,
        "started_at": started_at, "finished_at": utc_now(), "attempts": len(attempt_log), "attempt_log": attempt_log, "status": status,
        "failure": failure, "response_id": response.response_id if response else None,
        "response_sha256": canonical_sha256(response.content) if response else None,
        "usage": {"input_tokens": response.input_tokens if response else None, "output_tokens": response.output_tokens if response else None, "cost_usd": None},
    }


def attempt_row(attempt: int, status: str, failure: str | None, response: ProviderResponse | None) -> dict[str, Any]:
    return {
        "attempt": attempt, "status": status, "failure": failure,
        "response_id": response.response_id if response else None,
        "response_sha256": canonical_sha256(response.content) if response else None,
        "usage": {"input_tokens": response.input_tokens if response else None, "output_tokens": response.output_tokens if response else None, "cost_usd": None},
    }


def targeted_repair_request(base_request: JudgeRequest, validation_error: str) -> JudgeRequest:
    if "citation quote not found in task context" in validation_error:
        instruction = (
            "A prior response mislabeled evidence source. Recheck every evidence item: task_context is only "
            "the supplied BLIND_CASE.task_context object; any quote copied from a conversation or tool event "
            "must use source trajectory_step with its exact step number. Return a complete replacement JSON."
        )
    elif "citation quote not found at trajectory step" in validation_error:
        instruction = (
            "A prior citation was not an exact substring of its cited step. Remove any added quotation marks, "
            "markdown markers, or paraphrase and return a complete replacement JSON using the shortest exact quote."
        )
    else:
        instruction = "A prior response failed local validation. Return a complete replacement JSON with exact citations."
    user_prompt = base_request.user_prompt + "\n\nTARGETED VALIDATION RETRY:\n" + instruction
    return JudgeRequest(base_request.system_prompt, user_prompt, canonical_sha256({"system": base_request.system_prompt, "user": user_prompt}))


class InvalidOutputLogger:
    """Explicitly opt-in local debug capture for raw invalid judge JSON/text."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, *, case: dict[str, Any], attempt: int, request: JudgeRequest,
               response: ProviderResponse, validation_error: str) -> None:
        row = {
            "selection_id": case["selection_id"], "blind_run_id": case["blind_run_id"],
            "attempt": attempt, "prompt_sha256": request.prompt_sha256,
            "response_id": response.response_id, "response_sha256": canonical_sha256(response.content),
            "validation_error": validation_error, "raw_response": response.content,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(canonical_json(row) + "\n")


def openai_chat_request(
    endpoint: str, api_key: str, model: str, timeout_sec: float, judge_schema: dict[str, Any],
) -> Callable[[JudgeRequest], ProviderResponse]:
    def request_fn(request: JudgeRequest) -> ProviderResponse:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "tau3_judge_output",
                    "strict": True,
                    "schema": judge_schema,
                },
            },
        }
        raw = json.dumps(body).encode("utf-8")
        http_request = urllib.request.Request(endpoint, data=raw, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_sec) as http_response:
                data = json.loads(http_response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise urllib.error.URLError(f"HTTP {error.code}: {detail}") from error
        usage = data.get("usage") or {}
        return ProviderResponse(data["choices"][0]["message"]["content"], data.get("id"), usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return request_fn


def structured_output_schema(judge_schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten local nullable ``oneOf`` definitions for OpenAI strict output.

    The local JSON Schema intentionally uses ``oneOf`` to express nullable
    applicability/outcome objects. GPT-5.4 Structured Outputs rejects that
    keyword, but accepts a nullable object type.  Expand only internal refs,
    retain the same object properties, and leave the local schema as the
    authoritative validator after the response arrives.
    """
    definitions = judge_schema.get("$defs", {})

    def expand(node: Any) -> Any:
        if isinstance(node, list):
            return [expand(item) for item in node]
        if not isinstance(node, dict):
            return node
        if set(node) == {"$ref"} and node["$ref"].startswith("#/$defs/"):
            name = node["$ref"].split("/")[-1]
            return expand(copy.deepcopy(definitions[name]))
        if "oneOf" in node:
            options = [expand(option) for option in node["oneOf"]]
            null_options = [option for option in options if option == {"type": "null"}]
            non_null = [option for option in options if option != {"type": "null"}]
            if len(null_options) == 1 and len(non_null) == 1:
                result = non_null[0]
                current_type = result.get("type", "object")
                result["type"] = list(current_type) + ["null"] if isinstance(current_type, list) else [current_type, "null"]
                return result
            raise ValueError("strict output schema has an unsupported non-nullable oneOf")
        result = {key: expand(value) for key, value in node.items() if key not in {"$schema", "$id", "$defs"}}
        return result

    return expand(copy.deepcopy(judge_schema))


def parse_json_object(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("judge response is not a JSON object")
    return parsed


def load_api_key(env_path: Path) -> str:
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    raise SystemExit("OPENAI_API_KEY가 .env 또는 환경변수에 없다.")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Persist a complete case-level snapshot without exposing a truncated JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def read_jsonl_if_exists(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path) if path.exists() else []


if __name__ == "__main__":
    main()
