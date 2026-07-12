"""Deterministic clause verifier for the tau3 retail domain.

Implements the predicate spec frozen in
`results/tau3/assessment/verifier_specs/retail.md`. Every clause here is
either a pure `deterministic` route or the `deterministic`-routed
applicability half of a `hybrid` clause (per
`results/tau3/assessment/routing_manifest.json`). Outcome for the four hybrid
clauses (`retail.goal.task_outcome`, `retail.invariant.grounded_response`,
`retail.invariant.single_user_scope`, `retail.invariant.single_bulk_mutation_call`)
is not this module's job and is left for the LLM judge.
"""
from __future__ import annotations

from typing import Any, Callable

from src.assessment.verifiers.evidence import (
    ToolAction,
    absent_result,
    actions_named,
    aggregate,
    classify_payment_source,
    constant_applicable,
    decision,
    evaluate_guarded,
    evidence_item,
    extract_tool_actions,
    field_needle,
    procedures,
    single_tool_turn_result,
    successful,
    transfer_sequence_result,
)

ALWAYS_APPLICABLE_CLAUSE_IDS = {
    "retail.goal.task_outcome",
    "retail.invariant.grounded_response",
    "retail.invariant.single_tool_turn",
    "retail.invariant.single_user_scope",
}

# Hybrid clause with a conditional (not always) deterministic applicability.
CONDITIONAL_HYBRID_CLAUSE_IDS = {"retail.invariant.single_bulk_mutation_call"}

DETERMINISTIC_OUTCOME_CLAUSE_IDS = {
    "retail.invariant.actionable_order_status",
    "retail.invariant.cancel_pending_only",
    "retail.invariant.cancel_refund_route",
    "retail.invariant.exchange_delivered_only",
    "retail.invariant.exchange_terminal_state",
    "retail.invariant.gift_card_balance",
    "retail.invariant.modify_allowed_fields_only",
    "retail.invariant.modify_pending_only",
    "retail.invariant.modify_single_new_payment",
    "retail.invariant.return_delivered_only",
    "retail.invariant.return_refund_method",
    "retail.invariant.return_terminal_state",
    "retail.invariant.same_product_available_variant",
    "retail.invariant.single_tool_turn",
    "retail.obligation.transfer_sequence",
}

RETAIL_CLAUSE_IDS = (
    DETERMINISTIC_OUTCOME_CLAUSE_IDS | ALWAYS_APPLICABLE_CLAUSE_IDS | CONDITIONAL_HYBRID_CLAUSE_IDS
)

ORDER_MUTATION_TOOLS = (
    "cancel_pending_order",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "exchange_delivered_order_items",
    "return_delivered_order_items",
)
ITEM_MUTATION_TOOLS = (
    "modify_pending_order_items",
    "exchange_delivered_order_items",
    "return_delivered_order_items",
)


class Context:
    def __init__(self, case: dict[str, Any]):
        trajectory = case["trajectory"]
        self.trajectory = trajectory
        self.actions, self.pairing_failures = extract_tool_actions(trajectory)


def assess_case(case: dict[str, Any], routing_by_clause: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if case["policy"]["domain"] != "retail":
        raise ValueError("assess_case called with a non-retail case")
    context = Context(case)
    results: dict[str, dict[str, Any]] = {}
    for clause in case["policy"]["clauses"]:
        clause_id = clause["id"]
        if clause_id not in RETAIL_CLAUSE_IDS:
            continue
        routing_entry = routing_by_clause[clause_id]
        results[clause_id] = CLAUSE_ASSESSORS[clause_id](clause, routing_entry, context)
    return results


def _order_mutations(context: Context, *tool_names: str) -> list[ToolAction]:
    return successful(actions_named(context.actions, *tool_names))


def _env_enforced_pass(
    clause,
    routing_entry,
    actions: list[ToolAction],
    absent_reason: str,
    trigger_reason: str,
    pass_reason: str,
) -> dict[str, Any]:
    """Precondition is enforced by the tau2 retail tool implementation itself
    (it raises before mutating state if the precondition fails), so any
    successful call is direct evidence the precondition held. Fail is
    structurally unreachable through this route (spec review flag 1)."""

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        ev = [evidence_item(action.call_event, field_needle("name", action.name))]
        return "pass", ev, pass_reason

    return evaluate_guarded(clause, routing_entry, actions, check, absent_reason, trigger_reason)


# ---------------------------------------------------------------------------
# Status-gate clauses (env-enforced pass)
# ---------------------------------------------------------------------------


def _cancel_pending_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "cancel_pending_order")
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful cancel_pending_order call was observed",
        "a successful cancel_pending_order call was observed",
        "the environment rejects cancellation of a non-pending order, so a successful "
        "call implies the order was pending",
    )


def _modify_pending_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(
        context, "modify_pending_order_address", "modify_pending_order_items", "modify_pending_order_payment"
    )
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful pending-order modification call was observed",
        "a successful pending-order modification call was observed",
        "the environment rejects modification of a non-pending order, so a successful "
        "call implies the order was pending",
    )


def _exchange_delivered_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "exchange_delivered_order_items")
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful exchange_delivered_order_items call was observed",
        "a successful exchange_delivered_order_items call was observed",
        "the environment rejects exchange of a non-delivered order, so a successful "
        "call implies the order was delivered",
    )


def _return_delivered_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "return_delivered_order_items")
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful return_delivered_order_items call was observed",
        "a successful return_delivered_order_items call was observed",
        "the environment rejects return of a non-delivered order, so a successful "
        "call implies the order was delivered",
    )


def _actionable_order_status(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, *ORDER_MUTATION_TOOLS)
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful order-mutating call was observed",
        "a successful order-mutating call was observed",
        "each order mutation tool enforces its own pending/delivered precondition, so "
        "a successful call implies an actionable order status",
    )


# ---------------------------------------------------------------------------
# Payment clauses (env-enforced pass)
# ---------------------------------------------------------------------------


def _gift_card_balance(clause, routing_entry, context: Context) -> dict[str, Any]:
    candidates = _order_mutations(
        context, "modify_pending_order_items", "modify_pending_order_payment", "exchange_delivered_order_items"
    )
    actions = [
        a
        for a in candidates
        if classify_payment_source(a.arguments["payment_method_id"]) == "gift_card"
    ]
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful write spending against a gift card was observed",
        "a successful write spending against a gift card was observed",
        "the environment rejects insufficient gift card balance, so a successful call "
        "implies sufficient balance",
    )


def _return_refund_method(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "return_delivered_order_items")
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful return_delivered_order_items call was observed",
        "a successful return_delivered_order_items call was observed",
        "the environment rejects a refund method that is neither a gift card nor the "
        "order's original payment method, so a successful call implies a valid method",
    )


def _modify_single_new_payment(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "modify_pending_order_payment")
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful modify_pending_order_payment call was observed",
        "a successful modify_pending_order_payment call was observed",
        "the environment enforces a single existing prior payment and a different, "
        "existing new payment method, so a successful call implies both conditions",
    )


def _cancel_refund_route(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "cancel_pending_order")

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        history = action.result_value["payment_history"]
        payment_ids = {p["payment_method_id"] for p in history if p["transaction_type"] == "payment"}
        refund_ids = {p["payment_method_id"] for p in history if p["transaction_type"] == "refund"}
        ev = [evidence_item(action.call_event, field_needle("name", "cancel_pending_order"))]
        if refund_ids <= payment_ids:
            return "pass", ev, "every refund was issued to a payment method used on the order"
        return "fail", ev, "a refund was issued to a payment method not used on the order"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful cancel_pending_order call was observed",
        "a successful cancel_pending_order call was observed",
    )


# ---------------------------------------------------------------------------
# Terminal-state clauses
# ---------------------------------------------------------------------------


def _return_terminal_state(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "return_delivered_order_items")

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        status = action.result_value["status"]
        ev = [evidence_item(action.call_event, field_needle("name", "return_delivered_order_items"))]
        if status == "return requested":
            return "pass", ev, "order status is return requested"
        return "fail", ev, f"order status is {status!r}, not return requested"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful return_delivered_order_items call was observed",
        "a successful return_delivered_order_items call was observed",
    )


def _exchange_terminal_state(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "exchange_delivered_order_items")

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        status = action.result_value["status"]
        ev = [evidence_item(action.call_event, field_needle("name", "exchange_delivered_order_items"))]
        if status == "exchange requested":
            return "pass", ev, "order status is exchange requested"
        return "fail", ev, f"order status is {status!r}, not exchange requested"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful exchange_delivered_order_items call was observed",
        "a successful exchange_delivered_order_items call was observed",
    )


# ---------------------------------------------------------------------------
# Item clauses
# ---------------------------------------------------------------------------


def _same_product_available_variant(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, "modify_pending_order_items", "exchange_delivered_order_items")

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        item_ids = action.arguments["item_ids"]
        new_item_ids = action.arguments["new_item_ids"]
        ev = [evidence_item(action.call_event, field_needle("new_item_ids", new_item_ids))]
        if any(old == new for old, new in zip(item_ids, new_item_ids)):
            return "fail", ev, "an item was exchanged for the same item id"
        return "pass", ev, "same-product/available-variant and distinct-item conditions are enforced by the environment"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful item-modifying or item-exchanging call was observed",
        "a successful item-modifying or item-exchanging call was observed",
    )


def _modify_allowed_fields_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = _order_mutations(context, *ORDER_MUTATION_TOOLS)
    return _env_enforced_pass(
        clause,
        routing_entry,
        actions,
        "no successful order-mutating call was observed",
        "a successful order-mutating call was observed",
        "the retail tool surface only exposes address, payment, and item-option "
        "mutations, so no other field can be changed",
    )


# ---------------------------------------------------------------------------
# Sequencing (shared with airline)
# ---------------------------------------------------------------------------


def _single_tool_turn(clause, routing_entry, context: Context) -> dict[str, Any]:
    return single_tool_turn_result(context.trajectory, routing_entry)


def _transfer_sequence(clause, routing_entry, context: Context) -> dict[str, Any]:
    return transfer_sequence_result(context.actions, context.trajectory, clause["applicability"], routing_entry)


# ---------------------------------------------------------------------------
# Hybrid clauses (applicability only; outcome is the LLM judge's job)
# ---------------------------------------------------------------------------


def _hybrid_applicability_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    return {"applicability": constant_applicable(routing_entry), "outcome": None}


def _single_bulk_mutation_call(clause, routing_entry, context: Context) -> dict[str, Any]:
    app_proc, out_proc = procedures(routing_entry)
    actions = _order_mutations(context, *ITEM_MUTATION_TOOLS)
    if not actions:
        return absent_result(
            clause["applicability"],
            app_proc,
            out_proc,
            "no successful item-mutating call was observed",
        )
    app = decision(
        "applicable",
        "verified",
        app_proc,
        [evidence_item(actions[0].call_event, field_needle("name", actions[0].name))],
        "a successful item-mutating call was observed",
    )
    return {"applicability": app, "outcome": None}


CLAUSE_ASSESSORS: dict[str, Callable[[dict[str, Any], dict[str, Any], Context], dict[str, Any]]] = {
    "retail.goal.task_outcome": _hybrid_applicability_only,
    "retail.invariant.grounded_response": _hybrid_applicability_only,
    "retail.invariant.single_user_scope": _hybrid_applicability_only,
    "retail.invariant.single_bulk_mutation_call": _single_bulk_mutation_call,
    "retail.invariant.actionable_order_status": _actionable_order_status,
    "retail.invariant.cancel_pending_only": _cancel_pending_only,
    "retail.invariant.cancel_refund_route": _cancel_refund_route,
    "retail.invariant.exchange_delivered_only": _exchange_delivered_only,
    "retail.invariant.exchange_terminal_state": _exchange_terminal_state,
    "retail.invariant.gift_card_balance": _gift_card_balance,
    "retail.invariant.modify_allowed_fields_only": _modify_allowed_fields_only,
    "retail.invariant.modify_pending_only": _modify_pending_only,
    "retail.invariant.modify_single_new_payment": _modify_single_new_payment,
    "retail.invariant.return_delivered_only": _return_delivered_only,
    "retail.invariant.return_refund_method": _return_refund_method,
    "retail.invariant.return_terminal_state": _return_terminal_state,
    "retail.invariant.same_product_available_variant": _same_product_available_variant,
    "retail.invariant.single_tool_turn": _single_tool_turn,
    "retail.obligation.transfer_sequence": _transfer_sequence,
}
