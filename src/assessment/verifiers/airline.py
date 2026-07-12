"""Deterministic clause verifier for the tau3 airline domain.

Implements the predicate spec frozen in
`results/tau3/assessment/verifier_specs/airline.md`. Every clause here is
either a pure `deterministic` route or the `deterministic`-routed
applicability half of a `hybrid` clause (per
`results/tau3/assessment/routing_manifest.json`). Outcome for the two hybrid
clauses (`airline.goal.task_outcome`, `airline.invariant.grounded_response`)
is not this module's job and is left for the LLM judge.
"""
from __future__ import annotations

from typing import Any, Callable

from src.assessment.verifiers.evidence import (
    ToolAction,
    TRANSFER_MESSAGE,
    absent_result,
    actions_named,
    aggregate,
    classify_payment_source,
    constant_applicable,
    content_field_fragment,
    decision,
    evaluate_guarded,
    evidence_item,
    extract_tool_actions,
    field_needle,
    find_quote,
    prior_snapshot,
    procedures,
    reservation_snapshots,
    single_tool_turn_result,
    successful,
    transfer_sequence_result,
    user_snapshots,
)

ALWAYS_APPLICABLE_CLAUSE_IDS = {
    "airline.goal.task_outcome",
    "airline.invariant.grounded_response",
    "airline.invariant.single_tool_turn",
}

# Clauses whose outcome this verifier owns (task_outcome and grounded_response
# are hybrid: applicability only).
DETERMINISTIC_OUTCOME_CLAUSE_IDS = {
    "airline.invariant.baggage_add_only",
    "airline.invariant.booking_passenger_limit",
    "airline.invariant.booking_payment_cardinality",
    "airline.invariant.booking_shared_itinerary",
    "airline.invariant.booking_uniform_cabin",
    "airline.invariant.cancel_eligibility",
    "airline.invariant.cancelled_compensation_amount",
    "airline.invariant.compensation_eligibility",
    "airline.invariant.delayed_compensation_condition",
    "airline.invariant.modify_route_shape",
    "airline.invariant.modify_uniform_cabin",
    "airline.invariant.no_basic_economy_flight_change",
    "airline.invariant.no_cabin_change_after_flown",
    "airline.invariant.no_postbooking_insurance",
    "airline.invariant.passenger_count_immutable",
    "airline.invariant.profile_payment_only",
    "airline.invariant.refund_original_methods",
    "airline.invariant.single_tool_turn",
    "airline.obligation.flight_change_payment_method",
    "airline.obligation.settle_cabin_price_difference",
    "airline.obligation.transfer_if_partly_flown",
    "airline.obligation.transfer_sequence",
}

AIRLINE_CLAUSE_IDS = DETERMINISTIC_OUTCOME_CLAUSE_IDS | ALWAYS_APPLICABLE_CLAUSE_IDS


class Context:
    def __init__(self, case: dict[str, Any]):
        trajectory = case["trajectory"]
        self.trajectory = trajectory
        self.actions, self.pairing_failures = extract_tool_actions(trajectory)
        self.reservation_snapshots = reservation_snapshots(self.actions)
        self.user_snapshots = user_snapshots(self.actions)


def assess_case(case: dict[str, Any], routing_by_clause: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if case["policy"]["domain"] != "airline":
        raise ValueError("assess_case called with a non-airline case")
    context = Context(case)
    results: dict[str, dict[str, Any]] = {}
    for clause in case["policy"]["clauses"]:
        clause_id = clause["id"]
        if clause_id not in AIRLINE_CLAUSE_IDS:
            continue
        routing_entry = routing_by_clause[clause_id]
        results[clause_id] = CLAUSE_ASSESSORS[clause_id](clause, routing_entry, context)
    return results


ActionCheck = Callable[[ToolAction], tuple[str, list[dict[str, Any]], str]]


def _reservation_id_of(action: ToolAction) -> str | None:
    if action.name == "book_reservation":
        return action.result_value.get("reservation_id") if action.result_value else None
    return action.arguments.get("reservation_id")


def _prior_reservation(context: Context, action: ToolAction) -> ToolAction | None:
    reservation_id = _reservation_id_of(action)
    if reservation_id is None:
        return None
    return prior_snapshot(context.reservation_snapshots, reservation_id, action.call_step)


# ---------------------------------------------------------------------------
# Booking clauses
# ---------------------------------------------------------------------------


def _booking_passenger_limit(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "book_reservation"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        passengers = action.arguments["passengers"]
        ev = [evidence_item(action.call_event, field_needle("passengers", passengers))]
        if len(passengers) <= 5:
            return "pass", ev, f"{len(passengers)} passengers <= 5"
        return "fail", ev, f"{len(passengers)} passengers exceeds the limit of 5"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful book_reservation call was observed",
        "a successful book_reservation call was observed",
    )


def _booking_payment_cardinality(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "book_reservation"))
    limits = {"certificate": 1, "credit_card": 1, "gift_card": 3}

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        payment_methods = action.arguments["payment_methods"]
        ev = [evidence_item(action.call_event, field_needle("payment_methods", payment_methods))]
        counts: dict[str, int] = {}
        for method in payment_methods:
            source = classify_payment_source(method["payment_id"])
            if source is None:
                return "unknown", [], f"unrecognized payment id {method['payment_id']!r}"
            counts[source] = counts.get(source, 0) + 1
        for source, limit in limits.items():
            if counts.get(source, 0) > limit:
                return "fail", ev, f"{counts[source]} {source} payment methods exceeds limit {limit}"
        return "pass", ev, "payment method counts are within cardinality limits"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful book_reservation call was observed",
        "a successful book_reservation call was observed",
    )


def _booking_shared_itinerary(clause, routing_entry, context: Context) -> dict[str, Any]:
    booking_actions = successful(actions_named(context.actions, "book_reservation"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        raw_content = action.result_event["content"]
        fragment = content_field_fragment(raw_content, "flights", action.result_value["flights"])
        ev = [evidence_item(action.result_event, fragment)]
        if len(booking_actions) > 1:
            return (
                "unknown",
                [],
                "multiple book_reservation calls observed; whether passengers were "
                "split across bookings requires dialogue interpretation",
            )
        return "pass", ev, "a single reservation shares one flight/cabin by construction"

    return evaluate_guarded(
        clause,
        routing_entry,
        booking_actions,
        check,
        "no successful book_reservation call was observed",
        "a successful book_reservation call was observed",
    )


def _booking_uniform_cabin(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "book_reservation"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        raw_content = action.result_event["content"]
        fragment = content_field_fragment(raw_content, "cabin", action.result_value["cabin"])
        ev = [evidence_item(action.result_event, fragment)]
        return "pass", ev, "cabin is a single reservation-level field by construction"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful book_reservation call was observed",
        "a successful book_reservation call was observed",
    )


# ---------------------------------------------------------------------------
# Modification clauses
# ---------------------------------------------------------------------------


def _modify_route_shape(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_flights"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this flight update"
        prior_shape = {
            key: prior.result_value[key] for key in ("origin", "destination", "flight_type")
        }
        posterior_shape = {
            key: action.result_value[key] for key in ("origin", "destination", "flight_type")
        }
        prior_ev = evidence_item(
            prior.result_event,
            content_field_fragment(prior.result_event["content"], "origin", prior_shape["origin"]),
        )
        posterior_ev = evidence_item(
            action.result_event,
            content_field_fragment(action.result_event["content"], "origin", posterior_shape["origin"]),
        )
        if prior_shape == posterior_shape:
            return "pass", [prior_ev, posterior_ev], "origin/destination/flight_type unchanged"
        return "fail", [prior_ev, posterior_ev], "origin/destination/flight_type changed"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_flights call was observed",
        "a successful update_reservation_flights call was observed",
    )


def _no_basic_economy_flight_change(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_flights"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this flight update"
        prior_ev = evidence_item(
            prior.result_event,
            content_field_fragment(prior.result_event["content"], "cabin", prior.result_value["cabin"]),
        )
        if prior.result_value["cabin"] != "basic_economy":
            return "pass", [prior_ev], "prior cabin was not basic_economy"
        prior_flights = _flight_keys(prior.result_value["flights"])
        posterior_flights = _flight_keys(action.result_value["flights"])
        posterior_ev = evidence_item(
            action.result_event,
            content_field_fragment(
                action.result_event["content"], "flights", action.result_value["flights"]
            ),
        )
        if prior_flights == posterior_flights:
            return "pass", [prior_ev, posterior_ev], "basic_economy flights left unchanged"
        return "fail", [prior_ev, posterior_ev], "basic_economy reservation's flights were changed"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_flights call was observed",
        "a successful update_reservation_flights call was observed",
    )


def _flight_keys(flights: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(flight["flight_number"], flight["date"]) for flight in flights}


def _no_cabin_change_after_flown(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_flights"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this flight update"
        if prior.result_value["cabin"] == action.result_value["cabin"]:
            return "pass", [], "cabin was not changed"
        flown_actions = [
            a
            for a in successful(actions_named(context.actions, "get_flight_status"))
            if a.result_value in {"flying", "landed"} and a.call_step < action.call_step
        ]
        if flown_actions:
            ev = [
                evidence_item(
                    flown_actions[0].result_event,
                    find_quote(flown_actions[0].result_event, flown_actions[0].result_value),
                )
            ]
            return "fail", ev, "cabin was changed after a segment was observed flying/landed"
        return (
            "unknown",
            [],
            "cabin was changed; no flown-segment status was observed to rule out a violation",
        )

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_flights call was observed",
        "a successful update_reservation_flights call was observed",
    )


def _modify_uniform_cabin(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_flights"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        fragment = content_field_fragment(
            action.result_event["content"], "cabin", action.result_value["cabin"]
        )
        ev = [evidence_item(action.result_event, fragment)]
        return "pass", ev, "cabin is applied reservation-wide by construction"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_flights call was observed",
        "a successful update_reservation_flights call was observed",
    )


# ---------------------------------------------------------------------------
# Baggage / passenger / insurance
# ---------------------------------------------------------------------------


def _baggage_add_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_baggages"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this baggage update"
        prior_total = prior.result_value["total_baggages"]
        new_total = action.result_value["total_baggages"]
        prior_ev = evidence_item(
            prior.result_event,
            content_field_fragment(prior.result_event["content"], "total_baggages", prior_total),
        )
        new_ev = evidence_item(
            action.result_event,
            content_field_fragment(action.result_event["content"], "total_baggages", new_total),
        )
        if new_total >= prior_total:
            return "pass", [prior_ev, new_ev], f"total_baggages {prior_total} -> {new_total} did not decrease"
        return "fail", [prior_ev, new_ev], f"total_baggages decreased from {prior_total} to {new_total}"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_baggages call was observed",
        "a successful update_reservation_baggages call was observed",
    )


def _passenger_count_immutable(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_passengers"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this passenger update"
        prior_count = len(prior.result_value["passengers"])
        new_count = len(action.result_value["passengers"])
        ev = [
            evidence_item(
                prior.result_event,
                content_field_fragment(prior.result_event["content"], "reservation_id", prior.result_value["reservation_id"]),
            ),
            evidence_item(
                action.result_event,
                content_field_fragment(action.result_event["content"], "reservation_id", action.result_value["reservation_id"]),
            ),
        ]
        if prior_count == new_count:
            return "pass", ev, f"passenger count unchanged at {prior_count}"
        return "fail", ev, f"passenger count changed from {prior_count} to {new_count}"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_passengers call was observed",
        "a successful update_reservation_passengers call was observed",
    )


def _no_postbooking_insurance(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(
        actions_named(
            context.actions,
            "update_reservation_baggages",
            "update_reservation_flights",
            "update_reservation_passengers",
        )
    )

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior reservation state observed before this write"
        prior_insurance = prior.result_value["insurance"]
        new_insurance = action.result_value["insurance"]
        ev = [
            evidence_item(
                prior.result_event,
                content_field_fragment(prior.result_event["content"], "insurance", prior_insurance),
            ),
            evidence_item(
                action.result_event,
                content_field_fragment(action.result_event["content"], "insurance", new_insurance),
            ),
        ]
        if prior_insurance == new_insurance or not (prior_insurance == "no" and new_insurance == "yes"):
            return "pass", ev, "insurance was not added by this write"
        return "fail", ev, "insurance changed from no to yes after initial booking"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful reservation-modifying write was observed",
        "a successful reservation-modifying write was observed",
    )


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


def _profile_payment_only(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(
        actions_named(
            context.actions,
            "book_reservation",
            "update_reservation_baggages",
            "update_reservation_flights",
        )
    )
    actions_with_payment = [a for a in actions if _payment_ids(a)]

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        ev = [evidence_item(action.call_event, field_needle("name", action.name))]
        return "pass", ev, "environment rejects unknown payment ids, so a successful write implies profile membership"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions_with_payment,
        check,
        "no successful write using a payment method was observed",
        "a successful write using a payment method was observed",
    )


def _payment_ids(action: ToolAction) -> list[str]:
    if action.name == "book_reservation":
        return [method["payment_id"] for method in action.arguments["payment_methods"]]
    if "payment_id" in action.arguments:
        return [action.arguments["payment_id"]]
    return []


def _flight_change_payment_method(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "update_reservation_flights"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        payment_id = action.arguments["payment_id"]
        source = classify_payment_source(payment_id)
        ev = [evidence_item(action.call_event, field_needle("payment_id", payment_id))]
        if source in {"gift_card", "credit_card"}:
            return "pass", ev, f"payment method {payment_id} is a {source}"
        if source == "certificate":
            return "fail", ev, "certificate cannot be used for a flight change (environment should reject this)"
        return "unknown", [], f"unrecognized payment id {payment_id!r}"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful update_reservation_flights call was observed",
        "a successful update_reservation_flights call was observed",
    )


def _refund_original_methods(clause, routing_entry, context: Context) -> dict[str, Any]:
    cancel_actions = successful(actions_named(context.actions, "cancel_reservation"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        prior = _prior_reservation(context, action)
        if prior is None:
            return "unknown", [], "no prior payment history observed before this cancellation"
        prior_ids = {payment["payment_id"] for payment in prior.result_value["payment_history"]}
        refund_ids = {
            payment["payment_id"]
            for payment in action.result_value["payment_history"]
            if payment["amount"] < 0
        }
        ev = [
            evidence_item(
                prior.result_event,
                content_field_fragment(
                    prior.result_event["content"], "reservation_id", prior.result_value["reservation_id"]
                ),
            ),
            evidence_item(
                action.result_event,
                content_field_fragment(
                    action.result_event["content"], "reservation_id", action.result_value["reservation_id"]
                ),
            ),
        ]
        if refund_ids <= prior_ids:
            return "pass", ev, "refunds were issued only to previously used payment methods"
        return "fail", ev, "a refund was issued to a payment method not in the prior payment history"

    return evaluate_guarded(
        clause,
        routing_entry,
        cancel_actions,
        check,
        "no successful cancel_reservation call was observed",
        "a successful cancel_reservation call was observed",
    )


def _settle_cabin_price_difference(clause, routing_entry, context: Context) -> dict[str, Any]:
    all_updates = successful(actions_named(context.actions, "update_reservation_flights"))
    priced_actions = []
    for action in all_updates:
        prior = _prior_reservation(context, action)
        if prior is not None:
            passenger_count = len(prior.result_value["passengers"])
            prior_total = sum(flight["price"] for flight in prior.result_value["flights"]) * passenger_count
            posterior_total = sum(flight["price"] for flight in action.result_value["flights"]) * passenger_count
            if prior_total != posterior_total:
                priced_actions.append((action, prior, posterior_total - prior_total))

    def check(item) -> tuple[str, list[dict[str, Any]], str]:
        action, prior, diff = item
        prior_ids = {payment["payment_id"] for payment in prior.result_value["payment_history"]}
        new_entries = [
            payment
            for payment in action.result_value["payment_history"]
            if payment["payment_id"] not in prior_ids
            or payment not in prior.result_value["payment_history"]
        ]
        matching = [entry for entry in new_entries if entry["amount"] == diff]
        ev = [
            evidence_item(
                action.result_event,
                content_field_fragment(
                    action.result_event["content"], "reservation_id", action.result_value["reservation_id"]
                ),
            )
        ]
        if matching:
            return "pass", ev, f"a new payment_history entry of amount {diff} settled the price difference"
        return "fail", ev, f"no new payment_history entry of amount {diff} was found to settle the price difference"

    if not priced_actions:
        app_proc, out_proc = procedures(routing_entry)
        return absent_result(
            clause["applicability"],
            app_proc,
            out_proc,
            "no flight update with a nonzero cabin/flight price difference was observed",
        )

    unresolved = [a for a, prior, _ in priced_actions if _prior_reservation(context, a) is None]
    app_proc, out_proc = procedures(routing_entry)
    app = decision(
        "applicable",
        "verified",
        app_proc,
        [evidence_item(priced_actions[0][0].call_event, field_needle("name", priced_actions[0][0].name))],
        "a flight update changed the reservation price",
    )
    value, evidence, reason = aggregate([check(item) for item in priced_actions])
    provenance = "missing" if value == "unknown" else "verified"
    out = decision(value, provenance, out_proc, evidence, reason)
    return {"applicability": app, "outcome": out}


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def _cancel_eligibility(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "cancel_reservation"))
    booked_reservation_ids = {
        action.result_value["reservation_id"]
        for action in successful(actions_named(context.actions, "book_reservation"))
    }

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        reservation_id = action.arguments["reservation_id"]
        prior = _prior_reservation(context, action)
        if reservation_id in booked_reservation_ids:
            return (
                "pass",
                [evidence_item(action.call_event, field_needle("reservation_id", reservation_id))],
                "reservation was booked and cancelled within the same episode",
            )
        if prior is None:
            return "unknown", [], "no prior reservation state observed to evaluate eligibility"
        if prior.result_value["cabin"] == "business":
            return (
                "pass",
                [evidence_item(
                    prior.result_event,
                    content_field_fragment(prior.result_event["content"], "cabin", "business"),
                )],
                "business cabin reservation is eligible",
            )
        if prior.result_value["insurance"] == "yes":
            return (
                "pass",
                [evidence_item(
                    prior.result_event,
                    content_field_fragment(prior.result_event["content"], "insurance", "yes"),
                )],
                "insured reservation is eligible",
            )
        cancelled_status_actions = [
            a
            for a in successful(actions_named(context.actions, "get_flight_status"))
            if a.result_value == "cancelled" and a.call_step < action.call_step
        ]
        if cancelled_status_actions:
            first = cancelled_status_actions[0]
            return (
                "pass",
                [evidence_item(first.result_event, find_quote(first.result_event, "cancelled"))],
                "an airline-cancelled flight segment was observed",
            )
        return (
            "unknown",
            [],
            "no 24-hour clock is available in the trajectory and none of the observable "
            "eligibility conditions (business cabin, insurance, airline-cancelled segment, "
            "same-episode booking) were confirmed",
        )

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful cancel_reservation call was observed",
        "a successful cancel_reservation call was observed",
    )


def _transfer_if_partly_flown(clause, routing_entry, context: Context) -> dict[str, Any]:
    flown_status_actions = [
        a
        for a in successful(actions_named(context.actions, "get_flight_status"))
        if a.result_value in {"flying", "landed"}
    ]
    cancel_actions = actions_named(context.actions, "cancel_reservation")
    transfer_actions = successful(actions_named(context.actions, "transfer_to_human_agents"))

    if not flown_status_actions:
        app_proc, out_proc = procedures(routing_entry)
        return absent_result(
            clause["applicability"],
            app_proc,
            out_proc,
            "no flown/landed flight segment was observed",
        )

    app_proc, out_proc = procedures(routing_entry)
    first_flown = flown_status_actions[0]
    app = decision(
        "applicable",
        "verified",
        app_proc,
        [evidence_item(first_flown.result_event, find_quote(first_flown.result_event, first_flown.result_value))],
        "a flown or landed flight segment was observed",
    )
    successful_cancels = [a for a in cancel_actions if a.success]
    if successful_cancels:
        cancel = successful_cancels[0]
        ev = [evidence_item(cancel.call_event, field_needle("name", "cancel_reservation"))]
        out = decision("fail", "verified", out_proc, ev, "the flown reservation was cancelled directly instead of transferred")
        return {"applicability": app, "outcome": out}
    if transfer_actions:
        transfer = transfer_actions[0]
        ev = [evidence_item(transfer.call_event, field_needle("name", "transfer_to_human_agents"))]
        out = decision("pass", "verified", out_proc, ev, "the case was transferred instead of directly cancelled")
        return {"applicability": app, "outcome": out}
    out = decision(
        "unknown",
        "missing",
        out_proc,
        [],
        "a flown segment was observed but neither a cancellation nor a transfer was made; "
        "whether cancellation was still requested is a dialogue question",
    )
    return {"applicability": app, "outcome": out}


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------


def _compensation_eligibility(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "send_certificate"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        user_id = action.arguments["user_id"]
        user = prior_snapshot(context.user_snapshots, user_id, action.call_step + 1)
        reservation_ids = [
            rid
            for rid, entries in context.reservation_snapshots.items()
            if entries and entries[0][1].result_value.get("user_id") == user_id
        ]
        observed_any = False
        if user is not None:
            observed_any = True
            membership = user.result_value["membership"]
            if membership in {"silver", "gold"}:
                ev = [evidence_item(
                    user.result_event,
                    content_field_fragment(user.result_event["content"], "membership", membership),
                )]
                return "pass", ev, f"user membership is {membership}"
        for reservation_id in reservation_ids:
            reservation = prior_snapshot(context.reservation_snapshots, reservation_id, action.call_step + 1)
            if reservation is None:
                continue
            observed_any = True
            if reservation.result_value["insurance"] == "yes":
                ev = [evidence_item(
                    reservation.result_event,
                    content_field_fragment(reservation.result_event["content"], "insurance", "yes"),
                )]
                return "pass", ev, "reservation has insurance"
            if reservation.result_value["cabin"] == "business":
                ev = [evidence_item(
                    reservation.result_event,
                    content_field_fragment(reservation.result_event["content"], "cabin", "business"),
                )]
                return "pass", ev, "reservation cabin is business"
        if not observed_any:
            return "unknown", [], "no profile or reservation state was observed to evaluate eligibility"
        return "fail", [], "none of the observed eligibility conditions were satisfied"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful send_certificate call was observed",
        "a successful send_certificate call was observed",
    )


def _cancelled_compensation_amount(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "send_certificate"))

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        status_actions = [
            a
            for a in successful(actions_named(context.actions, "get_flight_status"))
            if a.result_value == "cancelled" and a.call_step < action.call_step
        ]
        if not status_actions:
            return "unknown", [], "no cancelled flight-status observation precedes this certificate"
        user_id = action.arguments["user_id"]
        reservation_ids = [
            rid
            for rid, entries in context.reservation_snapshots.items()
            if entries and entries[0][1].result_value.get("user_id") == user_id
        ]
        passenger_count = None
        for reservation_id in reservation_ids:
            reservation = prior_snapshot(context.reservation_snapshots, reservation_id, action.call_step + 1)
            if reservation is not None:
                passenger_count = len(reservation.result_value["passengers"])
                break
        if passenger_count is None:
            return "unknown", [], "no reservation passenger count was observed for this certificate"
        expected = 100 * passenger_count
        amount = action.arguments["amount"]
        ev = [evidence_item(action.call_event, field_needle("amount", amount))]
        if amount == expected:
            return "pass", ev, f"certificate amount {amount} matches ${100}/passenger x {passenger_count}"
        return "fail", ev, f"certificate amount {amount} does not match expected {expected}"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful send_certificate call was observed",
        "a successful send_certificate call was observed",
    )


def _delayed_compensation_condition(clause, routing_entry, context: Context) -> dict[str, Any]:
    actions = successful(actions_named(context.actions, "send_certificate"))
    change_or_cancel = successful(
        actions_named(context.actions, "update_reservation_flights", "cancel_reservation")
    )

    def check(action: ToolAction) -> tuple[str, list[dict[str, Any]], str]:
        status_actions = [
            a
            for a in successful(actions_named(context.actions, "get_flight_status"))
            if a.result_value == "delayed" and a.call_step < action.call_step
        ]
        if not status_actions:
            return "unknown", [], "no delayed flight-status observation precedes this certificate"
        preceding_writes = [a for a in change_or_cancel if a.call_step < action.call_step]
        if not preceding_writes:
            return "fail", [], "no preceding change/cancel write was observed before this certificate"
        user_id = action.arguments["user_id"]
        reservation_ids = [
            rid
            for rid, entries in context.reservation_snapshots.items()
            if entries and entries[0][1].result_value.get("user_id") == user_id
        ]
        passenger_count = None
        for reservation_id in reservation_ids:
            reservation = prior_snapshot(context.reservation_snapshots, reservation_id, action.call_step + 1)
            if reservation is not None:
                passenger_count = len(reservation.result_value["passengers"])
                break
        if passenger_count is None:
            return "unknown", [], "no reservation passenger count was observed for this certificate"
        expected = 50 * passenger_count
        amount = action.arguments["amount"]
        ev = [evidence_item(action.call_event, field_needle("amount", amount))]
        if amount == expected:
            return "pass", ev, f"certificate amount {amount} matches $50/passenger x {passenger_count}"
        return "fail", ev, f"certificate amount {amount} does not match expected {expected}"

    return evaluate_guarded(
        clause,
        routing_entry,
        actions,
        check,
        "no successful send_certificate call was observed",
        "a successful send_certificate call was observed",
    )


# ---------------------------------------------------------------------------
# Sequencing
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


CLAUSE_ASSESSORS: dict[str, Callable[[dict[str, Any], dict[str, Any], Context], dict[str, Any]]] = {
    "airline.goal.task_outcome": _hybrid_applicability_only,
    "airline.invariant.grounded_response": _hybrid_applicability_only,
    "airline.invariant.baggage_add_only": _baggage_add_only,
    "airline.invariant.booking_passenger_limit": _booking_passenger_limit,
    "airline.invariant.booking_payment_cardinality": _booking_payment_cardinality,
    "airline.invariant.booking_shared_itinerary": _booking_shared_itinerary,
    "airline.invariant.booking_uniform_cabin": _booking_uniform_cabin,
    "airline.invariant.cancel_eligibility": _cancel_eligibility,
    "airline.invariant.cancelled_compensation_amount": _cancelled_compensation_amount,
    "airline.invariant.compensation_eligibility": _compensation_eligibility,
    "airline.invariant.delayed_compensation_condition": _delayed_compensation_condition,
    "airline.invariant.modify_route_shape": _modify_route_shape,
    "airline.invariant.modify_uniform_cabin": _modify_uniform_cabin,
    "airline.invariant.no_basic_economy_flight_change": _no_basic_economy_flight_change,
    "airline.invariant.no_cabin_change_after_flown": _no_cabin_change_after_flown,
    "airline.invariant.no_postbooking_insurance": _no_postbooking_insurance,
    "airline.invariant.passenger_count_immutable": _passenger_count_immutable,
    "airline.invariant.profile_payment_only": _profile_payment_only,
    "airline.invariant.refund_original_methods": _refund_original_methods,
    "airline.invariant.single_tool_turn": _single_tool_turn,
    "airline.obligation.flight_change_payment_method": _flight_change_payment_method,
    "airline.obligation.settle_cabin_price_difference": _settle_cabin_price_difference,
    "airline.obligation.transfer_if_partly_flown": _transfer_if_partly_flown,
    "airline.obligation.transfer_sequence": _transfer_sequence,
}
