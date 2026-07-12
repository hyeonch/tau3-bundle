"""Deterministic verifier for the frozen tau3 telecom predicates.

The contracts implemented here are frozen in
``results/tau3/assessment/verifier_specs/telecom.md``.  In particular, task
generator fault tokens are never used as run-time evidence: every triggered
obligation below starts from an agent-visible tool observation.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Callable, Iterable

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
    single_tool_turn_result,
    successful,
    transfer_sequence_result,
)


ALWAYS_APPLICABLE_CLAUSE_IDS = {
    "telecom.goal.task_outcome",
    "telecom.invariant.grounded_response",
    "telecom.invariant.single_tool_turn",
}

DETERMINISTIC_OUTCOME_CLAUSE_IDS = {
    "telecom.invariant.no_resume_expired_contract",
    "telecom.invariant.one_awaiting_payment",
    "telecom.invariant.overdue_payment_only",
    "telecom.invariant.refuel_max_2gb",
    "telecom.invariant.resume_after_all_overdue_paid",
    "telecom.invariant.single_tool_turn",
    "telecom.obligation.confirm_bill_amount",
    "telecom.obligation.disable_airplane_mode",
    "telecom.obligation.disable_data_saver",
    "telecom.obligation.disable_wifi_calling_for_mms",
    "telecom.obligation.enable_account_roaming",
    "telecom.obligation.enable_device_roaming",
    "telecom.obligation.enable_mobile_data",
    "telecom.obligation.escalate_locked_sim",
    "telecom.obligation.grant_mms_permissions",
    "telecom.obligation.refuel_exhausted_data",
    "telecom.obligation.reseat_missing_sim",
    "telecom.obligation.reset_apn_then_reboot",
    "telecom.obligation.restore_service_before_data",
    "telecom.obligation.transfer_sequence",
    "telecom.obligation.upgrade_network_mode",
}

TELECOM_CLAUSE_IDS = ALWAYS_APPLICABLE_CLAUSE_IDS | DETERMINISTIC_OUTCOME_CLAUSE_IDS
DATE_CUTOFF = "2025-02-25"


def _text(action: ToolAction) -> str:
    return str(action.result_event.get("content") or "")


def _success_literal(action: ToolAction, prefix: str) -> bool:
    return action.success and _text(action).split("\n", 1)[0] == prefix


def _call_evidence(action: ToolAction) -> dict[str, Any]:
    return evidence_item(action.call_event, field_needle("name", action.name))


def _result_evidence(action: ToolAction, needle: str) -> dict[str, Any]:
    # Canonical event JSON escapes embedded newlines.  Keep citations tight
    # and stable by falling back to the first output line for multi-line
    # device diagnostics.
    try:
        return evidence_item(action.result_event, needle)
    except ValueError:
        first = needle.splitlines()[0]
        return evidence_item(action.result_event, first)


def _json_result(action: ToolAction) -> dict[str, Any] | list[Any] | None:
    if isinstance(action.result_value, (dict, list)):
        return action.result_value
    try:
        return json.loads(_text(action))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _network_fields(action: ToolAction) -> dict[str, str] | None:
    if action.name != "check_network_status" or not action.success:
        return None
    fields: dict[str, str] = {}
    for line in _text(action).splitlines():
        if ": " not in line:
            return None
        key, value = line.split(": ", 1)
        fields[key] = value
    required = {"Airplane Mode", "SIM Card Status", "Cellular Connection", "Cellular Signal", "Cellular Network Type", "Mobile Data Enabled", "Data Roaming Enabled"}
    return fields if required <= set(fields) else None


def _status_bar_text(action: ToolAction) -> str | None:
    text = _text(action)
    marker = "Status Bar: "
    if marker not in text:
        return None
    return text.split(marker, 1)[1].splitlines()[0]


def _device_positive(action: ToolAction, kind: str) -> bool:
    """Known positive state literals from a diagnostic or a write result."""
    text = _text(action)
    fields = _network_fields(action)
    if kind == "airplane_on":
        return (fields or {}).get("Airplane Mode") == "ON" or "✈️ Airplane Mode" in ( _status_bar_text(action) or "")
    if kind == "data_off":
        # The status-bar renderer also says "Data Disabled" when there is no
        # cellular connection even if the data switch itself is ON.  Only the
        # structured network diagnostic or the toggle's explicit first line
        # identifies the switch state.
        return (fields or {}).get("Mobile Data Enabled") == "No" or _text(action).split("\n", 1)[0] == "Mobile Data is now OFF."
    if kind == "roaming_off":
        return (fields or {}).get("Data Roaming Enabled") == "No"
    if kind == "data_saver_on":
        return "Data Saver mode is ON" in text or "🔽 Data Saver" in (_status_bar_text(action) or "")
    if kind == "wifi_calling_on":
        return "Wi-Fi Calling is currently turned ON." in text
    if kind == "no_service":
        return (fields or {}).get("Cellular Connection") == "no_service" or "📵 No Signal" in (_status_bar_text(action) or "")
    if kind == "service_restored":
        if fields is not None:
            return fields.get("Cellular Connection") == "connected" and fields.get("Cellular Signal") != "none"
        bar = _status_bar_text(action) or ""
        return any(icon in bar for icon in ("📶¹", "📶²", "📶³", "📶⁴"))
    if kind == "sim_missing":
        return "No SIM card detected in the phone." in text or (fields or {}).get("SIM Card Status") == "missing"
    if kind == "sim_locked":
        return "SIM card is locked with a PIN code." in text or "SIM card is locked with a PUK code." in text or (fields or {}).get("SIM Card Status") in {"locked_pin", "locked_puk"}
    if kind == "old_network":
        return "Network Mode Preference: 2g_only" in text or "Network Mode Preference: 3g_only" in text or (fields or {}).get("Cellular Network Type") in {"2G", "3G"}
    raise ValueError(f"unknown device state {kind}")


class Context:
    def __init__(self, case: dict[str, Any]):
        self.trajectory = case["trajectory"]
        self.actions, self.pairing_failures = extract_tool_actions(
            self.trajectory, caller_roles=("assistant", "user")
        )

    def named(self, *names: str) -> list[ToolAction]:
        return actions_named(self.actions, *names)


def assess_case(case: dict[str, Any], routing_by_clause: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if case["policy"].get("domain") != "telecom":
        raise ValueError("assess_case called with a non-telecom case")
    context = Context(case)
    results: dict[str, dict[str, Any]] = {}
    for clause in case["policy"]["clauses"]:
        clause_id = clause["id"]
        if clause_id in TELECOM_CLAUSE_IDS:
            results[clause_id] = CLAUSE_ASSESSORS[clause_id](clause, routing_by_clause[clause_id], context)
    return results


def _hybrid_always(clause, routing_entry, context: Context) -> dict[str, Any]:
    return {"applicability": constant_applicable(routing_entry), "outcome": None}


def _triggered(
    clause: dict[str, Any], routing_entry: dict[str, Any], triggers: list[ToolAction],
    check: Callable[[ToolAction], tuple[str, list[dict[str, Any]], str]], absent_reason: str,
) -> dict[str, Any]:
    app_proc, out_proc = procedures(routing_entry)
    if not triggers:
        return absent_result(clause["applicability"], app_proc, out_proc, absent_reason)
    app = decision("applicable", "verified", app_proc, [_result_evidence(triggers[0], _text(triggers[0]))], "agent-visible trigger evidence was observed")
    value, evidence, reason = aggregate([check(trigger) for trigger in triggers])
    return {"applicability": app, "outcome": decision(value, "missing" if value == "unknown" else "verified", out_proc, evidence, reason)}


def _conditional_actions(clause, routing_entry, actions: list[ToolAction], check, absent_reason: str) -> dict[str, Any]:
    app_proc, out_proc = procedures(routing_entry)
    if not actions:
        return absent_result(clause["applicability"], app_proc, out_proc, absent_reason)
    app = decision("applicable", "verified", app_proc, [_call_evidence(actions[0])], "a successful guarded action was observed")
    value, evidence, reason = aggregate([check(action) for action in actions])
    return {"applicability": app, "outcome": decision(value, "missing" if value == "unknown" else "verified", out_proc, evidence, reason)}


def _bill_observations(context: Context, before: int) -> dict[str, tuple[int, dict[str, Any], ToolAction]]:
    observed: dict[str, tuple[int, dict[str, Any], ToolAction]] = {}
    for action in context.actions:
        if not action.success or action.result_step >= before:
            continue
        value = _json_result(action)
        records: Iterable[dict[str, Any]] = []
        if action.name == "get_details_by_id" and isinstance(value, dict) and "bill_id" in value:
            records = [value]
        elif action.name == "get_bills_for_customer" and isinstance(value, list):
            records = [row for row in value if isinstance(row, dict)]
        for row in records:
            bill_id = row.get("bill_id")
            if bill_id:
                observed[bill_id] = (action.result_step, row, action)
    return observed


def _overdue_payment_only(clause, routing_entry, context: Context):
    actions = successful(context.named("send_payment_request"))
    def check(action):
        bill_id = action.arguments.get("bill_id")
        prior = _bill_observations(context, action.call_step).get(bill_id)
        if prior is None:
            return "unknown", [], "the requested bill had no prior structured status observation"
        _, bill, source = prior
        evidence = [_call_evidence(action), _result_evidence(source, f'"status": "{bill.get("status")}"')]
        if bill.get("status") == "Overdue":
            return "pass", evidence, "the target bill was observed overdue before its payment request"
        return "fail", evidence, "a payment request was sent for a bill not observed overdue"
    return _conditional_actions(clause, routing_entry, actions, check, "no successful payment request was observed")


def _confirm_bill_amount(clause, routing_entry, context):
    triggers = successful(context.named("send_payment_request"))
    def check(action):
        prior = _bill_observations(context, action.call_step).get(action.arguments.get("bill_id"))
        if prior is None or "total_due" not in prior[1]:
            return "fail", [_call_evidence(action)], "no amount-due observation preceded the payment request"
        _, bill, source = prior
        return "pass", [_result_evidence(source, f'"total_due": {json.dumps(bill["total_due"])}'), _call_evidence(action)], "the bill amount was observed before the payment request"
    return _triggered(clause, routing_entry, triggers, check, "no successful payment request was observed")


def _one_awaiting_payment(clause, routing_entry, context):
    actions = successful(context.named("send_payment_request"))
    def check(action):
        return "pass", [_result_evidence(action, "Payment request sent to the customer")], "the environment permits a successful request only when no other bill is awaiting payment"
    return _conditional_actions(clause, routing_entry, actions, check, "no successful payment request was observed")


def _no_resume_expired_contract(clause, routing_entry, context):
    actions = successful(context.named("resume_line"))
    def check(action):
        value = _json_result(action)
        line = value.get("line") if isinstance(value, dict) else None
        date = line.get("contract_end_date") if isinstance(line, dict) else None
        if not isinstance(date, str):
            return "unknown", [], "resume result did not contain a parseable contract end date"
        ev = [_result_evidence(action, f'"contract_end_date": "{date}"')]
        return ("pass" if date >= DATE_CUTOFF else "fail", ev, f"contract end date {date} was compared with frozen date {DATE_CUTOFF}")
    return _conditional_actions(clause, routing_entry, actions, check, "no successful resume_line call was observed")


def _resume_after_paid(clause, routing_entry, context):
    actions = successful(context.named("resume_line"))
    def check(action):
        customer_id = action.arguments.get("customer_id")
        history = _bill_observations(context, action.call_step)
        overdue = {bill_id for _, bill, _ in history.values() if bill.get("customer_id") == customer_id and bill.get("status") == "Overdue"}
        # Include prior Overdue states even if a later pre-resume observation turned them Awaiting Payment.
        for candidate in context.actions:
            if candidate.result_step >= action.call_step or not candidate.success:
                continue
            value = _json_result(candidate)
            rows = [value] if isinstance(value, dict) and "bill_id" in value else value if isinstance(value, list) else []
            for bill in rows:
                if isinstance(bill, dict) and bill.get("customer_id") == customer_id and bill.get("status") == "Overdue":
                    overdue.add(bill["bill_id"])
        if not overdue:
            return "unknown", [], "no pre-resume overdue bill cohort was observed"
        final = _bill_observations(context, action.call_step)
        missing = [bill_id for bill_id in overdue if bill_id not in final]
        if missing:
            return "unknown", [], "an overdue bill had no later status observation before resume"
        non_paid = [bill_id for bill_id in overdue if final[bill_id][1].get("status") != "Paid"]
        ev = [_call_evidence(action)] + [_result_evidence(final[b][2], f'"status": "{final[b][1].get("status")}"') for b in overdue]
        if non_paid:
            return "fail", ev, "an historically overdue bill was not observed Paid before resume"
        return "pass", ev, "every observed overdue bill was later observed Paid before resume"
    return _conditional_actions(clause, routing_entry, actions, check, "no successful resume_line call was observed")


def _refuel_max(clause, routing_entry, context):
    actions = successful(context.named("refuel_data"))
    def check(action):
        amount = action.arguments.get("gb_amount")
        if not isinstance(amount, (int, float)):
            return "unknown", [], "refuel amount was not numeric"
        ev = [_call_evidence(action)]
        return ("pass" if 0 < amount <= 2 else "fail", ev, f"refuel amount was {amount} GB")
    return _conditional_actions(clause, routing_entry, actions, check, "no successful refuel_data call was observed")


def _refuel_exhausted(clause, routing_entry, context):
    triggers: list[ToolAction] = []
    for action in successful(context.named("get_data_usage")):
        value = _json_result(action)
        try:
            exhausted = float(value["data_used_gb"]) > float(value["data_limit_gb"]) + float(value.get("data_refueling_gb", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if exhausted:
            triggers.append(action)
    def check(trigger):
        value = _json_result(trigger) or {}
        line_id = value.get("line_id")
        customer_id = trigger.arguments.get("customer_id")
        later = [a for a in successful(context.named("refuel_data")) if a.call_step > trigger.result_step and a.arguments.get("line_id") == line_id and a.arguments.get("customer_id") == customer_id]
        if later:
            return "pass", [_result_evidence(trigger, '"data_used_gb"'), _call_evidence(later[0])], "exhausted line was successfully refueled"
        return "fail", [_result_evidence(trigger, '"data_used_gb"')], "no successful refuel followed the exhausted-data observation"
    return _triggered(clause, routing_entry, triggers, check, "no exhausted-data observation was parsed")


def _toggle_obligation(clause, routing_entry, context, trigger_kind: str, tool: str, success_prefix: str):
    triggers = [a for a in context.actions if a.success and _device_positive(a, trigger_kind)]
    # A corrective-direction result itself proves the prior state, even without a diagnostic.
    triggers += [a for a in context.actions if _success_literal(a, success_prefix) and a not in triggers]
    triggers.sort(key=lambda a: a.result_step)
    def check(trigger):
        later = [a for a in context.named(tool) if a.result_step >= trigger.result_step and _success_literal(a, success_prefix)]
        if later:
            return "pass", [_result_evidence(trigger, _text(trigger)), _result_evidence(later[0], success_prefix)], "the observed device condition was corrected"
        return "fail", [_result_evidence(trigger, _text(trigger))], "the observed device condition had no matching corrective result"
    return _triggered(clause, routing_entry, triggers, check, "no agent-visible trigger state was observed")


def _enable_account_roaming(clause, routing_entry, context):
    triggers: list[ToolAction] = []
    for action in successful(context.named("enable_roaming")):
        if _success_literal(action, "Roaming enabled successfully"):
            triggers.append(action)
    # A direct line observation can also trigger only when it matches an agent lookup phone.
    phones = {a.arguments.get("phone_number") for a in successful(context.named("get_customer_by_phone"))}
    for action in successful(context.named("get_details_by_id")):
        value = _json_result(action)
        if isinstance(value, dict) and value.get("phone_number") in phones and value.get("roaming_enabled") is False:
            triggers.append(action)
    triggers = sorted({action.result_step: action for action in triggers}.values(), key=lambda a: a.result_step)
    def check(trigger):
        if trigger.name == "enable_roaming":
            return "pass", [_result_evidence(trigger, "Roaming enabled successfully")], "successful enable_roaming proves the account roaming trigger and correction"
        line = (_json_result(trigger) or {}).get("line_id")
        later = [a for a in successful(context.named("enable_roaming")) if a.call_step > trigger.result_step and a.arguments.get("line_id") == line and _success_literal(a, "Roaming enabled successfully")]
        if later:
            return "pass", [_result_evidence(trigger, '"roaming_enabled": false'), _result_evidence(later[0], "Roaming enabled successfully")], "target-line roaming was enabled"
        return "fail", [_result_evidence(trigger, '"roaming_enabled": false')], "target-line roaming was not enabled"
    return _triggered(clause, routing_entry, triggers, check, "no target-line roaming-off evidence was observed")


def _reseat_missing_sim(clause, routing_entry, context):
    triggers = [a for a in context.actions if a.success and _device_positive(a, "sim_missing")]
    def check(trigger):
        later = [a for a in context.named("reseat_sim_card") if a.result_step > trigger.result_step and _success_literal(a, "SIM card re-seated successfully.")]
        if later:
            return "pass", [_result_evidence(trigger, _text(trigger)), _result_evidence(later[0], "SIM card re-seated successfully.")], "missing SIM was followed by successful reseat"
        return "fail", [_result_evidence(trigger, _text(trigger))], "missing SIM was not followed by successful reseat"
    return _triggered(clause, routing_entry, triggers, check, "no missing-SIM diagnostic was observed")


def _locked_sim_transfer(clause, routing_entry, context):
    triggers = [a for a in context.actions if a.success and _device_positive(a, "sim_locked")]
    def check(trigger):
        later = [a for a in successful(context.named("transfer_to_human_agents")) if a.call_step > trigger.result_step]
        if later:
            return "pass", [_result_evidence(trigger, _text(trigger)), _call_evidence(later[0])], "locked SIM was escalated through a successful transfer"
        return "fail", [_result_evidence(trigger, _text(trigger))], "locked SIM was not escalated"
    return _triggered(clause, routing_entry, triggers, check, "no locked-SIM diagnostic was observed")


def _reset_apn(clause, routing_entry, context):
    triggers = [a for a in successful(context.named("check_apn_settings")) if "MMSC URL (for picture messages): Not Set" in _text(a) or "APN Name: Incorrect" in _text(a)]
    def check(trigger):
        resets = [a for a in context.named("reset_apn_settings") if a.result_step > trigger.result_step and a.success and "APN settings will reset at reboot." in _text(a)]
        reboot = next((a for a in context.named("reboot_device") if resets and a.result_step > resets[0].result_step and a.success and "Restarting network services..." in _text(a)), None)
        if resets and reboot:
            return "pass", [_result_evidence(trigger, "MMSC URL (for picture messages): Not Set"), _result_evidence(resets[0], "APN settings will reset at reboot."), _result_evidence(reboot, "Restarting network services...")], "APN reset was followed by reboot"
        return "fail", [_result_evidence(trigger, _text(trigger))], "bad APN was not reset and rebooted in order"
    return _triggered(clause, routing_entry, triggers, check, "no bad-APN diagnostic was observed")


def _grant_permissions(clause, routing_entry, context):
    triggers: list[tuple[ToolAction, set[str]]] = []
    for action in successful(context.named("check_app_permissions")):
        if action.arguments.get("app_name") != "messaging":
            continue
        text = _text(action)
        if "not found" in text:
            continue
        match = re.search(r"has permission for: (.+)\.", text)
        if not match:
            continue
        granted = {part.strip() for part in match.group(1).split(",")}
        missing = {p for p in {"storage", "sms"} if p not in granted}
        if missing:
            triggers.append((action, missing))
    app_proc, out_proc = procedures(routing_entry)
    if not triggers:
        return absent_result(clause["applicability"], app_proc, out_proc, "no parseable missing messaging permission was observed")
    app = decision("applicable", "verified", app_proc, [_result_evidence(triggers[0][0], _text(triggers[0][0]))], "messaging permission trigger was observed")
    verdicts = []
    for trigger, missing in triggers:
        grants = {a.arguments.get("permission") for a in context.named("grant_app_permission") if a.result_step > trigger.result_step and a.arguments.get("app_name") == "messaging" and _success_literal(a, f"Success. Permission '{a.arguments.get('permission')}' granted to app 'messaging'.")}
        if missing <= grants:
            evidence = [_result_evidence(trigger, _text(trigger))] + [_result_evidence(a, "Success. Permission") for a in context.named("grant_app_permission") if a.result_step > trigger.result_step and a.arguments.get("permission") in missing]
            verdicts.append(("pass", evidence, "every missing messaging permission was granted"))
        else:
            verdicts.append(("fail", [_result_evidence(trigger, _text(trigger))], "one or more missing messaging permissions were not granted"))
    value, evidence, reason = aggregate(verdicts)
    return {"applicability": app, "outcome": decision(value, "verified", out_proc, evidence, reason)}


def _restore_service_before_data(clause, routing_entry, context):
    triggers = [a for a in context.actions if a.success and _device_positive(a, "no_service")]
    guarded = {"toggle_data", "toggle_roaming", "enable_roaming", "refuel_data", "toggle_data_saver_mode", "disconnect_vpn", "set_network_mode_preference", "toggle_wifi_calling", "grant_app_permission"}
    def check(trigger):
        restored = False
        for action in context.actions:
            if action.result_step <= trigger.result_step:
                continue
            if action.success and _device_positive(action, "service_restored"):
                restored = True
            if action.name in guarded and action.success and not restored:
                return "fail", [_result_evidence(trigger, _text(trigger)), _call_evidence(action)], "a downstream data/MMS write occurred before restored service was observed"
        return "pass", [_result_evidence(trigger, _text(trigger))], "no downstream data/MMS write preceded restored service"
    return _triggered(clause, routing_entry, triggers, check, "no no-service observation was parsed")


def _upgrade_network_mode(clause, routing_entry, context):
    triggers = [a for a in context.actions if a.success and _device_positive(a, "old_network")]
    def check(trigger):
        later = [a for a in context.named("set_network_mode_preference") if a.result_step > trigger.result_step and a.success]
        if not later:
            return "fail", [_result_evidence(trigger, _text(trigger))], "old network preference had no later upgrade"
        first = later[0]
        mode = first.arguments.get("mode")
        ev = [_result_evidence(trigger, _text(trigger)), _call_evidence(first)]
        return ("pass" if mode in {"4g_5g_preferred", "4g_only"} else "fail", ev, f"network preference was set to {mode!r}")
    return _triggered(clause, routing_entry, triggers, check, "no 2G/3G preference was observed")


CLAUSE_ASSESSORS = {
    "telecom.goal.task_outcome": _hybrid_always,
    "telecom.invariant.grounded_response": _hybrid_always,
    "telecom.invariant.single_tool_turn": lambda c, r, x: single_tool_turn_result(x.trajectory, r),
    "telecom.invariant.overdue_payment_only": _overdue_payment_only,
    "telecom.obligation.confirm_bill_amount": _confirm_bill_amount,
    "telecom.invariant.one_awaiting_payment": _one_awaiting_payment,
    "telecom.invariant.no_resume_expired_contract": _no_resume_expired_contract,
    "telecom.invariant.resume_after_all_overdue_paid": _resume_after_paid,
    "telecom.invariant.refuel_max_2gb": _refuel_max,
    "telecom.obligation.refuel_exhausted_data": _refuel_exhausted,
    "telecom.obligation.disable_airplane_mode": lambda c, r, x: _toggle_obligation(c, r, x, "airplane_on", "toggle_airplane_mode", "Airplane Mode is now OFF."),
    "telecom.obligation.enable_mobile_data": lambda c, r, x: _toggle_obligation(c, r, x, "data_off", "toggle_data", "Mobile Data is now ON."),
    "telecom.obligation.enable_device_roaming": lambda c, r, x: _toggle_obligation(c, r, x, "roaming_off", "toggle_roaming", "Data Roaming is now ON."),
    "telecom.obligation.disable_data_saver": lambda c, r, x: _toggle_obligation(c, r, x, "data_saver_on", "toggle_data_saver_mode", "Data Saver Mode is now OFF."),
    "telecom.obligation.disable_wifi_calling_for_mms": lambda c, r, x: _toggle_obligation(c, r, x, "wifi_calling_on", "toggle_wifi_calling", "Wi-Fi Calling is now OFF."),
    "telecom.obligation.enable_account_roaming": _enable_account_roaming,
    "telecom.obligation.reseat_missing_sim": _reseat_missing_sim,
    "telecom.obligation.escalate_locked_sim": _locked_sim_transfer,
    "telecom.obligation.reset_apn_then_reboot": _reset_apn,
    "telecom.obligation.grant_mms_permissions": _grant_permissions,
    "telecom.obligation.restore_service_before_data": _restore_service_before_data,
    "telecom.obligation.upgrade_network_mode": _upgrade_network_mode,
    "telecom.obligation.transfer_sequence": lambda c, r, x: transfer_sequence_result(x.actions, x.trajectory, c["applicability"], r),
}
