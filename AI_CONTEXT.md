# τ³ Assessment: Context for Implementation Help

## What this run is for

This bundle evaluates 960 fixed agent trajectories with the SES τ³ policy framework. It produces two related run-level views:

- **Deterministic certificate:** only conclusions supported by deterministic trajectory predicates.
- **Hybrid assessment:** deterministic predicates plus cited semantic judgments from a fixed LLM judge.

The manuscript uses the hybrid pass/fail/unresolved rates, their comparison with deterministic certificates, native-reward cross-tabs, judge operating totals, and a 96-run stability analysis. The LLM judge is an assessment component, not ground truth or an independent human validation set.

## Fixed inputs and execution order

`results/tau3/annotation_cases/part-*.jsonl` contains the 960 blind cases as ordered JSONL shards. Each row supplies only the task context, policy snapshot, and trajectory needed for assessment. `annotation_packet_private_key.csv` maps blind IDs to aggregate reporting groups.

Run in this order:

1. `prepare_tau3_run` checks coverage and records input hashes.
2. `run_tau3_judge` creates one cited structured judge output and provenance row per case using `gpt-5.4`.
3. `assemble_tau3_assessments` combines deterministic domain verifiers and judge decisions into schema-validated final assessments.
4. `export_tau3_maintext_summary` creates the compact manuscript summary.

The exact commands are in `results/tau3/assessment/RUNBOOK.md`.

## Non-negotiable rules

- Do not use agent model identity, native reward, benchmark review, trial, seed, or provider metadata in judge prompts or clause decisions.
- A judge may decide only the LLM-routed target fields in `routing_manifest.json`. It must not overwrite deterministic targets.
- Every pass/fail decision requires exact, case-local evidence. A missing or ambiguous predicate is `unknown`, not pass.
- `task_context` citations may quote only `BLIND_CASE.task_context`. Conversation and tool evidence must use `trajectory_step` with the exact step number.
- Banking `current_time_tool` and `log_verification` are reverse-hybrid: the judge supplies applicability target steps; deterministic code resolves the ordering outcome.
- Keep `routing_manifest.json`, `judge_prompt.md`, `judge_rubric.md`, schemas, and verifier behavior fixed during a single reported run. Any intentional change requires a new protocol/input hash and a fresh full run.

## Interruption and recovery

Always run the judge with `--resume`. Each completed case atomically persists the output and provenance files. Re-running the same command skips cases with a valid output/provenance pair and reruns unfinished or failed cases. Do not hand-edit JSONL rows to bypass validation.

## What an AI may safely help change

- Fix a clear implementation bug that prevents schema validation, atomic persistence, or resume.
- Improve operational logging, error messages, dependency setup, or a deterministic verifier bug when supported by a reproducible failing case.
- Add tests that reproduce the failure before changing behavior.

Before changing prompt/rubric/schema/routing or the meaning of a policy clause, stop and explicitly decide whether to version the protocol and rerun all 960 cases. Do not patch individual outputs or replace selected cases after seeing results.

## Useful diagnostics to provide with a help request

- Exact command and full traceback/event lines.
- Python version and `jsonschema` version.
- Counts of output and provenance rows, not raw API keys.
- The relevant `selection_id`, response hash, prompt hash, and validator error.
- Whether the failure happened before any valid output, during a resumed run, or during assembly/export.

Do not include API keys. Keep evidence excerpts limited to the failing case when they are necessary to diagnose a citation or parser bug.
