# τ³ Hybrid Automated Assessment

이 디렉터리는 960-run automated assessment의 clause/evidence routing 계약을 보관한다.
현재 routing/schema, deterministic verifier와 LLM judge protocol을 고정했고 primary judge는 아직 실행하지 않았다.

## 현재 산출물

| 파일 | 역할 |
|---|---|
| `routing_manifest.json` | 114개 clause ID의 applicability/outcome route와 procedure ID를 고정한 source of truth |
| `routing_manifest.md` | route 수와 핵심 경계 요약 |
| `stability_subset.json` | judge 3-repeat용 10 task clusters / 96 runs 고정 subset |
| `audit_core.json` | 20-case author audit core의 blind selection ID 목록 |
| `stability_audit.md` | 두 표본의 quota 규칙과 blinding 경계 요약 |
| `judge_prompt.md` | blind input, target separation, citation과 reverse-hybrid 계약을 고정한 judge prompt source |
| `judge_rubric.md` | LLM target 55개를 정확히 한 번씩 정의한 clause-level 판정 rubric |
| `schemas/tau3-judge-provenance.schema.json` | judge request/response hash/usage를 기록하는 provenance 계약 |
| `schemas/tau3-routing-manifest.schema.json` | routing manifest 검증 계약 |
| `schemas/tau3-judge-output.schema.json` | LLM이 맡은 target만 반환하도록 제한한 structured output 계약 |
| `schemas/tau3-automated-assessment.schema.json` | deterministic/LLM 결과와 provenance를 합친 최종 run-level 계약 |

고정 route는 deterministic 59개, hybrid 12개, LLM judge 43개다. `unknown`은 정적 clause 유형이
아니다. required evidence 부재, deterministic parse 실패, judge schema/citation 실패가 발생한 run에서
강제로 적용되는 fallback이다.

## 경계

- routing 생성에는 trajectory outcome, native reward와 benchmark review를 사용하지 않았다.
- deterministic route는 구조화된 state/tool evidence로 술어가 닫히는 clause만 허용한다.
- applicability와 outcome의 담당 route를 분리했다. 혼합형에서 LLM이 맡지 않은 target은 `null`이어야 한다.
- deterministic parse 실패를 LLM 판정으로 우회하지 않는다.
- pass/fail outcome에는 blind case에 실제 존재하는 step/quote evidence가 필요하다.
- final assessment는 blind case hash와 routing manifest hash를 함께 보존한다.

stability subset과 audit core는 primary judge 실행 전에 고정했다
(`primary_judge_executed_before_freeze: false`). 두 파일 모두 task selection, private key,
routing manifest의 SHA-256과 seed `20260711`을 기록한다. `audit_core.json`에는 blind
`selection_id`/`blind_run_id`만 있다. per-case reward/model/family와 cell quota 표는 audit 1차
판정 전 unblinding을 막기 위해 제외했고, quota 준수는 코드와 테스트로만 검증한다.

## 재생성과 검증

```bash
python -m src.assessment.build_tau3_routing_manifest
python -m src.assessment.select_tau3_stability_audit
python -m src.assessment.validate_tau3_assessments \
  --judge-outputs path/to/judge-outputs.jsonl
python -m src.assessment.validate_tau3_assessments \
  --assessments path/to/automated-assessments.jsonl
python -m src.assessment.run_tau3_judge \
  --model your-judge-model \
  --output results/tau3/assessment/judge_outputs.jsonl \
  --provenance-output results/tau3/assessment/judge_provenance.jsonl
```

두 validation 명령은 `results/tau3/annotation_cases.jsonl`이 local에 생성돼 있어야 한다.
judge runner는 `.env` 또는 환경변수의 `OPENAI_API_KEY`를 사용하며, 결과가 유효 judge output인지
별도로 validation command로 확인한다. 실행 중에는 stdout과 `judge_events.jsonl`에 `case_composed`,
`attempt_started`, `attempt_valid`/`attempt_invalid_output`, `case_persisted`가 즉시 기록된다.
각 case가 끝날 때마다 output/provenance JSONL을 temp-file→atomic rename으로 저장한다. 중단 뒤에는 같은
명령에 `--resume`을 붙이면 이미 유효하게 저장된 case를 건너뛰고 나머지만 실행한다.

## 다음 작업

최소 LLM judge runner와 provenance logger를 구현했다 (`src/assessment/run_tau3_judge.py`). OpenAI-compatible
adapter 하나와 canonical composition, schema/citation validation, judge-output/provenance
JSONL만 포함한다. 다음 단계는 960-run primary와 96-run 3-repeat stability 실행이다.
