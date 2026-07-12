"""Shared evidence primitives for tau3 deterministic clause verifiers.

These helpers only read data that is already present in a blind annotation
case (`results/tau3/annotation_cases.jsonl`): the trajectory event list and
the policy snapshot. No model identity, native reward, or raw benchmark
metadata is available to or used by this module.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable


def canonical_text(value: Any) -> str:
    """Match `validate_tau3_assessments.canonical_text` exactly.

    Evidence citations are checked by the validator as a plain substring of
    this canonical serialization, so every quote this module builds must be
    derived from (or found within) the same string.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_quote(event: dict[str, Any], needle: str) -> str:
    """Return a citation-safe quote for `needle` against `event`.

    A JSON string value nested inside a `content` field (tool results are
    serialized JSON stored as a string) is escaped an extra time when the
    whole event is canonicalized. This tries the raw needle first and falls
    back to its escaped form so callers do not need to reason about nesting
    depth themselves.
    """
    canon = canonical_text(event)
    if needle in canon:
        return needle
    escaped = needle.replace("\\", "\\\\").replace('"', '\\"')
    if escaped in canon:
        return escaped
    raise ValueError(f"citation needle not found in event {event.get('step')}: {needle!r}")


def evidence_item(event: dict[str, Any], needle: str) -> dict[str, Any]:
    return {
        "source": "trajectory_step",
        "step": event["step"],
        "quote": find_quote(event, needle),
    }


def field_needle(key: str, value: Any) -> str:
    """Tight-separator `"key":value` needle matching nested-dict canonicalization.

    Safe for scalar and nested (list/dict) values alike: `sort_keys` and the
    tight separators reproduce exactly how the value is rendered when it sits
    inside a larger canonically-serialized structure (native JSON fields such
    as an assistant event's `tool_calls`, not a JSON-text-in-a-string field).
    """
    return f"{json.dumps(key, ensure_ascii=False)}:" + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_field_fragment(raw_content: str, key: str, value: Any) -> str:
    """Locate the literal `"key": value`-style fragment inside a raw JSON-text
    `content` string, preserving whatever spacing the original serializer used.
    """
    pattern = re.escape(json.dumps(key, ensure_ascii=False)) + r"\s*:\s*" + re.escape(
        json.dumps(value, ensure_ascii=False)
    )
    match = re.search(pattern, raw_content)
    if match is None:
        raise ValueError(f"field {key}={value!r} not found in content")
    return match.group(0)


@dataclass(frozen=True)
class ToolAction:
    call_step: int
    call_event: dict[str, Any]
    name: str
    arguments: dict[str, Any]
    result_step: int
    result_event: dict[str, Any]
    success: bool
    result_value: Any


@dataclass(frozen=True)
class PairingFailure:
    call_step: int
    name: str


def extract_tool_actions(
    trajectory: list[dict[str, Any]], caller_roles: Iterable[str] = ("assistant",)
) -> tuple[list[ToolAction], list[PairingFailure]]:
    """Pair tool calls from selected caller roles with their result event.

    A call's `tool_calls[i]` must line up with the i-th tool event
    immediately following the assistant turn (before the next non-tool
    event) and must share the same tool name. Any turn that fails this
    check is reported as a pairing failure and excluded from `actions`;
    callers must treat clauses depending on it as unknown rather than
    guessing at the mismatched pairing.
    """
    actions: list[ToolAction] = []
    failures: list[PairingFailure] = []
    allowed_roles = set(caller_roles)
    for index, event in enumerate(trajectory):
        calls = event.get("tool_calls") or []
        if event.get("role") not in allowed_roles or not calls:
            continue
        results = []
        for candidate in trajectory[index + 1 :]:
            if candidate.get("role") == "tool":
                results.append(candidate)
            else:
                break
        if len(results) < len(calls) or any(
            result["tool_name"] != call["name"] for result, call in zip(results, calls)
        ):
            for call in calls:
                failures.append(PairingFailure(call_step=event["step"], name=call["name"]))
            continue
        for call, result in zip(calls, results):
            success = not bool(result.get("tool_error"))
            result_value = None
            if success:
                try:
                    result_value = json.loads(result["content"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    result_value = None
            actions.append(
                ToolAction(
                    call_step=event["step"],
                    call_event=event,
                    name=call["name"],
                    arguments=call["arguments"],
                    result_step=result["step"],
                    result_event=result,
                    success=success,
                    result_value=result_value,
                )
            )
    return actions, failures


def actions_named(actions: Iterable[ToolAction], *names: str) -> list[ToolAction]:
    return [action for action in actions if action.name in names]


def successful(actions: Iterable[ToolAction]) -> list[ToolAction]:
    return [action for action in actions if action.success]


PAYMENT_SOURCE_PATTERN = re.compile(r"^(gift_card|credit_card|certificate|paypal)_")


def classify_payment_source(payment_id: str) -> str | None:
    match = PAYMENT_SOURCE_PATTERN.match(payment_id)
    return match.group(1) if match else None


def reservation_snapshots(
    actions: list[ToolAction],
) -> dict[str, list[tuple[int, ToolAction]]]:
    """Chronological Reservation-object observations, keyed by reservation_id.

    Sources: successful `get_reservation_details` reads and successful write
    calls whose return value is the updated Reservation (book/cancel/update_*).
    """
    snapshots: dict[str, list[tuple[int, ToolAction]]] = {}
    for action in actions:
        if not action.success or not isinstance(action.result_value, dict):
            continue
        if action.name not in {
            "get_reservation_details",
            "book_reservation",
            "cancel_reservation",
            "update_reservation_baggages",
            "update_reservation_flights",
            "update_reservation_passengers",
        }:
            continue
        reservation_id = action.result_value.get("reservation_id")
        if reservation_id is None:
            continue
        snapshots.setdefault(reservation_id, []).append((action.result_step, action))
    for entries in snapshots.values():
        entries.sort(key=lambda item: item[0])
    return snapshots


def user_snapshots(actions: list[ToolAction]) -> dict[str, list[tuple[int, ToolAction]]]:
    snapshots: dict[str, list[tuple[int, ToolAction]]] = {}
    for action in actions:
        if action.name != "get_user_details" or not action.success:
            continue
        if not isinstance(action.result_value, dict):
            continue
        user_id = action.result_value.get("user_id")
        if user_id is None:
            continue
        snapshots.setdefault(user_id, []).append((action.result_step, action))
    for entries in snapshots.values():
        entries.sort(key=lambda item: item[0])
    return snapshots


def prior_snapshot(
    snapshots: dict[str, list[tuple[int, ToolAction]]], entity_id: str, before_step: int
) -> ToolAction | None:
    entries = snapshots.get(entity_id, [])
    prior = None
    for step, action in entries:
        if step < before_step:
            prior = action
        else:
            break
    return prior


def decision(
    value: str,
    provenance: str,
    procedure_id: str,
    evidence: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "provenance": provenance,
        "procedure_id": procedure_id,
        "evidence": evidence,
        "reason": reason,
    }


def unknown_decision(procedure_id: str, reason: str) -> dict[str, Any]:
    return decision("unknown", "missing", procedure_id, [], reason)


# ---------------------------------------------------------------------------
# Domain-agnostic clause-shaped helpers.
#
# tau2 domains share a handful of policy patterns verbatim (an always-applicable
# single-tool-turn invariant, a triggered transfer-message obligation). These
# helpers implement that shared logic once so each domain verifier only wires
# up its own guarded-action predicates.
# ---------------------------------------------------------------------------

TRANSFER_MESSAGE = "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON."

ActionCheck = Callable[[ToolAction], tuple[str, list[dict[str, Any]], str]]


def procedures(routing_entry: dict[str, Any]) -> tuple[str, str]:
    return routing_entry["applicability"]["procedure_id"], routing_entry["outcome"]["procedure_id"]


def constant_applicable(routing_entry: dict[str, Any]) -> dict[str, Any]:
    app_proc, _ = procedures(routing_entry)
    return decision(
        "applicable",
        "verified",
        app_proc,
        [],
        "policy snapshot marks the clause as always applicable",
    )


def absent_result(
    policy_state: str, app_proc: str, out_proc: str, reason: str
) -> dict[str, dict[str, Any]]:
    absent_value = "not_applicable" if policy_state == "conditional" else "not_triggered"
    app = decision(absent_value, "verified", app_proc, [], reason)
    out = decision("not_applicable_outcome", "verified", out_proc, [], reason)
    return {"applicability": app, "outcome": out}


def aggregate(
    verdicts: list[tuple[str, list[dict[str, Any]], str]]
) -> tuple[str, list[dict[str, Any]], str]:
    fails = [(evidence, reason) for value, evidence, reason in verdicts if value == "fail"]
    if fails:
        evidence = [item for ev, _ in fails for item in ev]
        reasons = "; ".join(reason for _, reason in fails)
        return "fail", evidence, reasons
    unknowns = [(evidence, reason) for value, evidence, reason in verdicts if value == "unknown"]
    if unknowns:
        reasons = "; ".join(reason for _, reason in unknowns)
        return "unknown", [], reasons
    evidence = [item for value, ev, _ in verdicts for item in ev]
    reasons = "; ".join(reason for _, _, reason in verdicts)
    return "pass", evidence, reasons


def evaluate_guarded(
    clause: dict[str, Any],
    routing_entry: dict[str, Any],
    actions: list[ToolAction],
    per_action: ActionCheck,
    absent_reason: str,
    trigger_reason: str,
) -> dict[str, Any]:
    app_proc, out_proc = procedures(routing_entry)
    policy_state = clause["applicability"]
    if not actions:
        return absent_result(policy_state, app_proc, out_proc, absent_reason)
    app_evidence = [evidence_item(actions[0].call_event, field_needle("name", actions[0].name))]
    app = decision("applicable", "verified", app_proc, app_evidence, trigger_reason)
    value, ev, reason = aggregate([per_action(action) for action in actions])
    provenance = "missing" if value == "unknown" else "verified"
    out = decision(value, provenance, out_proc, ev, reason or trigger_reason)
    return {"applicability": app, "outcome": out}


def single_tool_turn_result(trajectory: list[dict[str, Any]], routing_entry: dict[str, Any]) -> dict[str, Any]:
    """Shared `invariant.single_tool_turn` predicate: always applicable; fails
    if any assistant turn issues more than one tool call, or a tool call
    together with a non-empty message."""
    _, out_proc = procedures(routing_entry)
    app = constant_applicable(routing_entry)
    for event in trajectory:
        if event.get("role") != "assistant":
            continue
        calls = event.get("tool_calls") or []
        content = event.get("content")
        if len(calls) > 1 or (calls and content):
            ev = [evidence_item(event, field_needle("role", "assistant"))]
            out = decision(
                "fail",
                "verified",
                out_proc,
                ev,
                "an assistant turn combined multiple tool calls or a tool call with a message",
            )
            return {"applicability": app, "outcome": out}
    out = decision(
        "pass",
        "verified",
        out_proc,
        [evidence_item(trajectory[0], field_needle("step", trajectory[0]["step"]))],
        "no assistant turn combined multiple tool calls or a tool call with a message",
    )
    return {"applicability": app, "outcome": out}


def transfer_sequence_result(
    actions: list[ToolAction],
    trajectory: list[dict[str, Any]],
    policy_state: str,
    routing_entry: dict[str, Any],
) -> dict[str, Any]:
    """Shared `obligation.transfer_sequence` predicate: triggered by a
    successful `transfer_to_human_agents` call; passes only if the first
    following assistant message with content exactly matches the required
    transfer text."""
    app_proc, out_proc = procedures(routing_entry)
    successful_transfers = successful(actions_named(actions, "transfer_to_human_agents"))
    if not successful_transfers:
        return absent_result(
            policy_state, app_proc, out_proc, "no successful transfer_to_human_agents call was observed"
        )
    app = decision(
        "applicable",
        "verified",
        app_proc,
        [evidence_item(successful_transfers[0].call_event, field_needle("name", "transfer_to_human_agents"))],
        "a successful transfer_to_human_agents call was observed",
    )

    def check(transfer: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        following = None
        for event in trajectory:
            if event["step"] > transfer.result_step and event.get("role") == "assistant" and event.get("content"):
                following = event
                break
        if following is None:
            return "fail", [], "no assistant message followed the transfer"
        if following["content"].strip() == TRANSFER_MESSAGE:
            return "pass", [evidence_item(following, TRANSFER_MESSAGE)], "the required transfer message followed the transfer call"
        return (
            "fail",
            [evidence_item(following, field_needle("role", "assistant"))],
            "the message following the transfer did not match the required transfer text",
        )

    value, ev, reason = aggregate([check(t) for t in successful_transfers])
    out = decision(value, "verified", out_proc, ev, reason)
    return {"applicability": app, "outcome": out}
