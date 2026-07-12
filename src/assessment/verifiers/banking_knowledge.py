"""Deterministic portions of the tau3 banking-knowledge assessment.

The two reverse-hybrid clauses deliberately resolve only after the semantic
judge supplies applicability evidence identifying ordering target steps.  See
``results/tau3/assessment/verifier_specs/banking_knowledge.md``.
"""
from __future__ import annotations

import re
from typing import Any

from src.assessment.verifiers.evidence import (
    ToolAction,
    absent_result,
    actions_named,
    aggregate,
    constant_applicable,
    decision,
    evidence_item,
    extract_tool_actions,
    field_needle,
    procedures,
)


PURE_CASE_CLAUSE_IDS = {
    "banking.invariant.kb_grounded_response",
    "banking.obligation.unlock_before_agent_call",
}
HYBRID_OUTCOME_CLAUSE_IDS = {
    "banking.obligation.current_time_tool",
    "banking.obligation.log_verification",
}
BANKING_CLAUSE_IDS = PURE_CASE_CLAUSE_IDS | HYBRID_OUTCOME_CLAUSE_IDS
CURRENT_TIME_PATTERN = re.compile(r"^The current time is \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]{2,5}\.$")


def _text(action: ToolAction) -> str:
    return str(action.result_event.get("content") or "")


def _call_evidence(action: ToolAction) -> dict[str, Any]:
    return evidence_item(action.call_event, field_needle("name", action.name))


def _result_evidence(action: ToolAction, needle: str) -> dict[str, Any]:
    try:
        return evidence_item(action.result_event, needle)
    except ValueError:
        return evidence_item(action.result_event, needle.splitlines()[0])


class Context:
    def __init__(self, case: dict[str, Any]):
        self.trajectory = case["trajectory"]
        self.actions, self.pairing_failures = extract_tool_actions(self.trajectory)


def assess_case(case: dict[str, Any], routing_by_clause: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve banking decisions that require only the blind case.

    Reverse-hybrid outcomes are intentionally omitted.  Call
    :func:`resolve_hybrid_outcomes` after merging judge applicability output.
    """
    if case["policy"].get("domain") != "banking_knowledge":
        raise ValueError("assess_case called with a non-banking_knowledge case")
    context = Context(case)
    results: dict[str, dict[str, Any]] = {}
    for clause in case["policy"]["clauses"]:
        clause_id = clause["id"]
        if clause_id == "banking.invariant.kb_grounded_response":
            results[clause_id] = {"applicability": constant_applicable(routing_by_clause[clause_id]), "outcome": None}
        elif clause_id == "banking.obligation.unlock_before_agent_call":
            results[clause_id] = _unlock_before_call(clause, routing_by_clause[clause_id], context)
    return results


def _successful_unlock(action: ToolAction) -> bool:
    name = action.arguments.get("agent_tool_name")
    return isinstance(name, str) and action.success and _text(action).startswith(f"Tool unlocked: {name}\n")


def _unlock_before_call(clause: dict[str, Any], routing_entry: dict[str, Any], context: Context) -> dict[str, Any]:
    app_proc, out_proc = procedures(routing_entry)
    calls = actions_named(context.actions, "call_discoverable_agent_tool")
    relevant_failures = [failure for failure in context.pairing_failures if failure.name in {"call_discoverable_agent_tool", "unlock_discoverable_agent_tool"}]
    if relevant_failures:
        app = decision("unknown", "missing", app_proc, [], "relevant unlock/call tool pairing failed")
        return {"applicability": app, "outcome": decision("unknown", "missing", out_proc, [], "relevant unlock/call tool pairing failed")}
    if not calls:
        return absent_result(clause["applicability"], app_proc, out_proc, "no call_discoverable_agent_tool attempt was observed")
    names = [action.arguments.get("agent_tool_name") for action in calls]
    if any(not isinstance(name, str) or not name for name in names):
        app = decision("unknown", "missing", app_proc, [_call_evidence(calls[0])], "protected call lacked a parseable agent_tool_name")
        return {"applicability": app, "outcome": decision("unknown", "missing", out_proc, [], "protected call lacked a parseable agent_tool_name")}
    app = decision("applicable", "verified", app_proc, [_call_evidence(calls[0])], "a discoverable agent-tool call attempt was observed")
    unlocks = [action for action in actions_named(context.actions, "unlock_discoverable_agent_tool") if _successful_unlock(action)]

    def check(call: ToolAction):
        name = call.arguments["agent_tool_name"]
        covered = [unlock for unlock in unlocks if unlock.arguments["agent_tool_name"] == name and unlock.result_step < call.call_step]
        if covered:
            return "pass", [_call_evidence(call), _result_evidence(covered[-1], f"Tool unlocked: {name}")], f"{name} was unlocked before it was called"
        return "fail", [_call_evidence(call)], f"{name} was called without a prior successful exact-name unlock"

    value, evidence, reason = aggregate([check(call) for call in calls])
    return {"applicability": app, "outcome": decision(value, "verified", out_proc, evidence, reason)}


def resolve_hybrid_outcomes(
    case: dict[str, Any], judge_decisions: dict[str, dict[str, Any]], routing_by_clause: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Combine judge applicability with deterministic banking outcomes.

    ``judge_decisions`` maps clause ID to the corresponding structured judge
    decision (`applicability` plus an unused/null `outcome`).  The resolver
    trusts neither prose nor task-context evidence for ordering: an applicable
    decision must supply one or more trajectory-step citations, per the frozen
    prompt contract.  Returned applicability decisions use the final
    automated-assessment shape and inferred provenance.
    """
    if case["policy"].get("domain") != "banking_knowledge":
        raise ValueError("resolve_hybrid_outcomes called with a non-banking_knowledge case")
    context = Context(case)
    supplied = {clause["id"] for clause in case["policy"]["clauses"]}
    results: dict[str, dict[str, Any]] = {}
    for clause_id in sorted(HYBRID_OUTCOME_CLAUSE_IDS & supplied):
        judge = judge_decisions.get(clause_id)
        if judge is None or judge.get("applicability") is None:
            raise ValueError(f"missing judge applicability for {clause_id}")
        routing = routing_by_clause[clause_id]
        app_proc, out_proc = procedures(routing)
        source_app = judge["applicability"]
        app_value = source_app.get("value")
        app_evidence = list(source_app.get("evidence") or [])
        app_reason = str(source_app.get("reason") or "judge applicability decision")
        if app_value not in {"applicable", "not_applicable", "not_triggered", "unknown"}:
            raise ValueError(f"invalid judge applicability value for {clause_id}")
        app_provenance = "missing" if app_value == "unknown" else "inferred"
        app = decision(app_value, app_provenance, app_proc, app_evidence, app_reason)
        if app_value in {"not_applicable", "not_triggered"}:
            results[clause_id] = {"applicability": app, "outcome": decision("not_applicable_outcome", app_provenance, out_proc, [], app_reason)}
            continue
        if app_value == "unknown":
            results[clause_id] = {"applicability": app, "outcome": decision("unknown", "missing", out_proc, [], app_reason)}
            continue
        target_steps = _trajectory_target_steps(app_evidence)
        if not target_steps:
            results[clause_id] = {"applicability": app, "outcome": decision("unknown", "missing", out_proc, [], "applicable judge decision omitted trajectory ordering target steps")}
            continue
        if clause_id == "banking.obligation.current_time_tool":
            outcome = _current_time_outcome(context, out_proc, target_steps)
        else:
            outcome = _verification_log_outcome(context, out_proc, target_steps)
        results[clause_id] = {"applicability": app, "outcome": outcome}
    return results


def _trajectory_target_steps(evidence: list[dict[str, Any]]) -> list[int]:
    steps = []
    for item in evidence:
        if item.get("source") != "trajectory_step" or not isinstance(item.get("step"), int):
            return []
        steps.append(item["step"])
    return sorted(set(steps))


def _current_time_outcome(context: Context, procedure_id: str, target_steps: list[int]) -> dict[str, Any]:
    observations = [action for action in actions_named(context.actions, "get_current_time") if action.success and CURRENT_TIME_PATTERN.fullmatch(_text(action))]
    verdicts = []
    for step in target_steps:
        prior = [action for action in observations if action.result_step < step]
        if prior:
            verdicts.append(("pass", [_result_evidence(prior[-1], _text(prior[-1]))], f"current time was observed before target step {step}"))
        else:
            verdicts.append(("fail", [_target_evidence(context, step)], f"no valid current-time result preceded target step {step}"))
    value, evidence, reason = aggregate(verdicts)
    return decision(value, "verified", procedure_id, evidence, reason)


def _verification_log_outcome(context: Context, procedure_id: str, target_steps: list[int]) -> dict[str, Any]:
    logs = [action for action in actions_named(context.actions, "log_verification") if action.success and _text(action).startswith("Verification logged successfully.\n")]
    verdicts = []
    for step in target_steps:
        later = [action for action in logs if action.call_step > step]
        if later:
            verdicts.append(("pass", [_result_evidence(later[0], "Verification logged successfully.")], f"verification completion at step {step} was followed by a successful log"))
        else:
            verdicts.append(("fail", [_target_evidence(context, step)], f"verification completion at step {step} had no later successful log"))
    value, evidence, reason = aggregate(verdicts)
    return decision(value, "verified", procedure_id, evidence, reason)


def _target_evidence(context: Context, step: int) -> dict[str, Any]:
    event = next((event for event in context.trajectory if event.get("step") == step), None)
    if event is None:
        raise ValueError(f"judge cited missing trajectory step {step}")
    return evidence_item(event, field_needle("step", step))
