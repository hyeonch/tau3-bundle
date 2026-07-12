# τ³ Runbook

Bundle root를 현재 작업 디렉터리로 둔 뒤, input hash를 고정하고 judge → assembly → summary 순서로 실행한다.

```bash
python -m src.assessment.prepare_tau3_run \
  --cases results/tau3/annotation_cases \
  --output results/tau3/assessment/run_manifest.json

python -m src.assessment.run_tau3_judge \
  --model gpt-5.4 --provider openai --max-retries 2 --resume \
  --cases results/tau3/annotation_cases \
  --output results/tau3/assessment/judge_outputs.jsonl \
  --provenance-output results/tau3/assessment/judge_provenance.jsonl \
  --event-log /tmp/tau3_judge_events.jsonl \
  --invalid-output-log /tmp/tau3_invalid_outputs.jsonl

python -m src.assessment.assemble_tau3_assessments --require-complete \
  --cases results/tau3/annotation_cases \
  --judge-outputs results/tau3/assessment/judge_outputs.jsonl \
  --provenance results/tau3/assessment/judge_provenance.jsonl \
  --output results/tau3/assessment/automated_assessments.jsonl

python -m src.assessment.export_tau3_maintext_summary \
  --cases results/tau3/annotation_cases \
  --assessments results/tau3/assessment/automated_assessments.jsonl \
  --provenance results/tau3/assessment/judge_provenance.jsonl \
  --output results/tau3/assessment/main_text_summary.json
```

`main_text_summary.json`은 trajectory, task context, citation quote, selection ID 없이 본문 표·수치에 필요한 집계만 담는다. 상세 assessment와 provenance는 appendix 재현성 자료로 사용한다.

동일한 judge 명령에 항상 `--resume`을 붙인다. 각 case가 끝날 때마다 output과 provenance를 atomic rename으로 저장하므로, 중단 뒤 같은 명령을 다시 실행하면 유효 output은 건너뛰고 미완료 case만 실행한다. 이전 failed case의 provenance는 재시도 결과로 교체되며 중복 row를 만들지 않는다.

`annotation_cases`는 여러 JSONL shard를 담는 디렉터리여도 된다. Stability는 frozen `stability_subset.json`의 96건을 independent repetition index 0·1·2로 각각 실행·assemble한 후, 세 assessment path를 `export_tau3_maintext_summary --stability-assessments`로 전달한다.
