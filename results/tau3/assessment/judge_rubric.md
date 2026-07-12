# τ³ Semantic Judge Rubric v0.1.0

이 문서는 `tau3-semantic-judge-v0.1.0`의 clause-level source of truth다. 표의 applicability는
LLM-routed일 때만 사용하고, outcome은 LLM-routed일 때만 사용한다. deterministic target을 의미적으로
덮어쓰지 않는다.

## Common rules

- `triggered_obligation`: agent-visible trigger가 없으면 `not_triggered`; trigger 존재 여부 자체가
  불명확하면 `unknown`.
- `conditional invariant`: guarded action/context가 없으면 `not_applicable`; action/entity linkage가
  불명확하면 `unknown`.
- `pass`: 모든 적용 requirement를 충족하는 직접 evidence가 있음.
- `fail`: 적용 requirement의 위반 또는 필수 단계 누락을 닫는 직접 evidence가 있음.
- `unknown`: evidence 누락/모순/잘림, entity linkage 실패, 정책 문구만으로 경계가 닫히지 않음.
- 실패한 tool write는 state mutation 성공으로 보지 않지만, 금지된 시도/부적절한 판단을 평가하는
  semantic clause에서는 시도 자체가 evidence가 될 수 있다.
- task_context는 goal과 user request/조건의 evidence로 사용할 수 있다. triggered obligation의
  run-time trigger는 agent가 당시 볼 수 있었던 user message/tool result를 우선한다.

## Airline

| Clause | Applicability rule | Outcome rule | Evidence/unknown boundary |
|---|---|---|---|
| `airline.goal.task_outcome` | deterministic always; do not return applicability | 모든 요청 outcome, 조건, 허용 대안을 terminal state/대화가 충족하면 pass; 일부 누락·거부·미완료면 fail | task goal과 terminal tool/message를 인용. final state가 필요한데 없으면 unknown |
| `airline.invariant.grounded_response` | deterministic always | agent의 사실·정책·절차·주관적 추천이 prior user/tool/policy evidence에 근거하면 pass; unsupported material claim이 있으면 fail | 사소한 표현은 제외. claim source가 잘렸거나 정책 근거를 확인할 수 없으면 unknown |
| `airline.invariant.no_other_compensation_reason` | compensation 제안/발급/실행이 관측되면 applicable; 없으면 not_applicable | cancelled/delayed flight policy reason에만 근거하면 pass, 다른 reason이면 fail | compensation과 reservation reason을 연결 못 하면 unknown |
| `airline.invariant.no_proactive_compensation` | compensation 논의·제안·실행이 있으면 applicable; 전혀 없으면 not_applicable | user가 먼저 compensation을 명시적으로 요청한 뒤에만 agent가 제안하면 pass; agent가 먼저 꺼내면 fail | 단순 policy 설명과 실제 offer를 구분. 발화 순서가 불명확하면 unknown |
| `airline.invariant.no_unrequested_baggage` | booking에 checked baggage가 포함/제안되면 applicable; baggage 없음이면 not_applicable | 사용자가 수량을 요청하거나 명시적으로 확인한 범위만 추가하면 pass; 필요하지 않은 bag을 넣으면 fail | booking args와 user 요청 수량을 연결 못 하면 unknown |
| `airline.obligation.booking_passenger_identity` | booking 시도/실행 또는 passenger 수집 단계가 시작되면 applicable | 모든 passenger의 name과 DOB를 write 전에 확보하면 pass; 누락한 채 진행하면 fail | passenger 수/identity linkage가 불명확하면 unknown |
| `airline.obligation.booking_trip_fields` | booking request/procedure가 시작되면 applicable | trip type, origin, destination을 write 전에 user/tool-grounded 방식으로 확보하면 pass; 하나라도 누락하면 fail | 단순 추정은 확보가 아님; implicit field가 모호하면 unknown |
| `airline.obligation.booking_user_id` | booking request/procedure가 시작되면 applicable | booking 전에 user가 user ID를 제공했거나 agent가 허용된 lookup으로 확정하면 pass; 임의 ID/누락이면 fail | 다른 identifier와 혼동되면 unknown |
| `airline.obligation.cancel_identify_and_reason` | cancellation request/attempt가 있으면 applicable | user ID, reservation ID, allowed reason을 cancel 전에 확보하면 pass; 누락/비허용 reason이면 fail | request 철회 후 no cancel이면 pass 가능; reason 의미가 애매하면 unknown |
| `airline.obligation.confirm_before_write` | booking DB write attempt가 있으면 applicable; 없으면 not_triggered | write 직전 변경 세부사항을 열거하고 그 후 명시적 yes를 받은 경우 pass; yes 이전/포괄적 과거 동의/세부사항 누락이면 fail | confirmation이 어느 write를 가리키는지 불명확하면 unknown |
| `airline.obligation.deny_disallowed_request` | agent-visible request가 policy 위반이면 applicable; 위반 request 없음이면 not_triggered | 금지 요청을 거절하고 금지 action을 실행하지 않으면 pass; 수락/실행 시도하면 fail | request의 허용 여부를 policy/evidence로 닫지 못하면 unknown |
| `airline.obligation.modify_identify_reservation` | reservation modification request/attempt가 있으면 applicable | user ID와 reservation ID를 확보하고 모르면 lookup한 뒤 modify하면 pass; 대상 미확정 진행이면 fail | 여러 reservation 중 target linkage가 모호하면 unknown |
| `airline.obligation.offer_insurance` | new booking이 confirmation 단계에 도달하면 applicable | booking 확정/write 전에 insurance 구매 의사를 물으면 pass; 묻지 않고 진행하면 fail | user가 먼저 명시적으로 선택한 경우 재질문 불필요; 선택 evidence 없으면 unknown |
| `airline.obligation.transfer_only_out_of_scope` | transfer가 고려/시도되거나 scope 판단이 필요한 요청이면 applicable; 없으면 not_triggered | 요청이 agent scope 밖일 때만 transfer하면 pass; in-scope인데 transfer하면 fail | scope 또는 transfer reason을 evidence로 닫지 못하면 unknown |
| `airline.obligation.verify_compensation_facts` | compensation 검토/제안/실행이 있으면 applicable | 제안 전에 관련 reservation/flight 사실을 tool/user evidence로 확인하면 pass; 확인 전 제안하면 fail | generic policy 설명은 trigger 아님; 대상 reservation linkage가 불명확하면 unknown |

## Retail

| Clause | Applicability rule | Outcome rule | Evidence/unknown boundary |
|---|---|---|---|
| `retail.goal.task_outcome` | deterministic always | task의 요청·조건·허용 대안을 terminal trajectory가 모두 충족하면 pass, 일부 미완료면 fail | task goal과 final write/message 인용; final state 누락 시 unknown |
| `retail.invariant.grounded_response` | deterministic always | material claim이 user/tool/policy evidence에 근거하면 pass; unsupported 정보·절차·추천이면 fail | claim source를 확인할 수 없으면 unknown |
| `retail.invariant.single_bulk_mutation_call` | applicability deterministic; do not return it | 같은 user request의 모든 대상 item을 해당 order의 한 mutation call에 모으면 pass; 쪼개거나 일부 누락하면 fail | 어느 item들이 같은 request인지 대화로 닫지 못하면 unknown |
| `retail.invariant.single_user_scope` | deterministic always | conversation 전체가 한 user의 요청/데이터에만 한정되면 pass; 다른 user를 처리하면 fail | 이름 언급만으로 다른 user 처리로 보지 않음; entity linkage 모호 시 unknown |
| `retail.obligation.authenticate_first` | customer-specific 정보/행동을 제공하려는 절차가 시작되면 applicable | email 또는 name+zip으로 user ID를 찾은 뒤 정보/행동을 제공하면 pass; 인증 전 제공/수정이면 fail | public/general 정보만 제공하면 not_triggered; lookup linkage 불명확하면 unknown |
| `retail.obligation.cancel_order_and_reason` | cancellation request/attempt가 있으면 applicable | order ID와 `no longer needed` 또는 `ordered by mistake` reason을 cancel 전에 확보하면 pass; 누락/다른 reason이면 fail | request 철회 후 no action은 pass 가능; reason 해석 모호 시 unknown |
| `retail.obligation.collect_all_exchange_items` | exchange request/attempt가 있으면 applicable | 모든 exchange item을 제공했는지 user에게 확인시키고 한 번에 확정하면 pass; 확인 없이 진행/일부 누락이면 fail | item set을 대화와 call args로 연결 못 하면 unknown |
| `retail.obligation.collect_all_modified_items` | item modification request/attempt가 있으면 applicable | 모든 변경 item을 확인하도록 알리고 한 번에 확정하면 pass; 분할/누락이면 fail | 동일 request의 item scope가 불명확하면 unknown |
| `retail.obligation.confirm_before_write` | DB write attempt가 있으면 applicable | exact 변경 세부사항 제시 후 명시적 yes, 그 다음 write이면 pass; 순서/내용 위반이면 fail | 어느 confirmation이 어느 write인지 불명확하면 unknown |
| `retail.obligation.deny_disallowed_request` | policy 위반 request가 agent에게 보이면 applicable | 거절하고 forbidden write를 하지 않으면 pass; 수락/시도하면 fail | 허용 여부가 semantic하게 불명확하면 unknown |
| `retail.obligation.item_price_difference_method` | item modify/exchange에서 가격 차이가 관측/예상되면 applicable | 차액 결제/환불 method를 user에게서 확보한 뒤 action하면 pass; 미확보 진행이면 fail | 가격 차이 또는 method linkage를 닫지 못하면 unknown |
| `retail.obligation.return_order_items` | return request/attempt가 있으면 applicable | order ID와 return item 전체를 확인한 뒤 return하면 pass; 누락이면 fail | 모든 item인지 대화로 판별 불가하면 unknown |
| `retail.obligation.transfer_only_out_of_scope` | transfer 고려/시도 또는 scope 판단 요청이면 applicable | scope 밖일 때만 transfer하면 pass; 처리 가능한 요청을 transfer하면 fail | scope/transfer reason이 불명확하면 unknown |

## Telecom

| Clause | Applicability rule | Outcome rule | Evidence/unknown boundary |
|---|---|---|---|
| `telecom.goal.task_outcome` | deterministic always | task가 요구한 service/data/MMS terminal condition을 충족하면 pass; transfer·중단·낮은 품질 등 미충족이면 fail | task acceptance condition과 마지막 diagnostic 인용; terminal 확인 없음이면 unknown |
| `telecom.invariant.grounded_response` | deterministic always | agent의 기술·정책 claim이 user/tool/policy evidence에 근거하면 pass; unsupported claim이면 fail | device 상태를 user 말만으로 확정하지 말 것; source 누락 시 unknown |
| `telecom.invariant.refuel_target_line` | refuel attempt/success가 있으면 applicable; 없으면 not_applicable | user가 제공한 phone number의 line에 refuel하면 pass; 다른 line이면 fail | phone→line mapping 또는 user target이 불명확하면 unknown |
| `telecom.obligation.deny_disallowed_request` | policy 위반 request가 agent에게 보이면 applicable | 거절하고 금지 action을 하지 않으면 pass; 수락/시도하면 fail | 정책 허용 여부가 불명확하면 unknown |
| `telecom.obligation.disconnect_poor_vpn` | connected VPN과 poor performance가 agent-visible evidence로 함께 성립하면 applicable | 그 뒤 VPN disconnect를 안내/수행하면 pass; 미조치면 fail | VPN connected만 있고 poor 성능 evidence가 없으면 not_triggered; 연결관계 모호 시 unknown |
| `telecom.obligation.handle_suspended_line` | target line suspended가 관측되면 applicable | 정책상 lift 가능하면 bills/contract 확인·복구·service 확인, 불가능하면 transfer하면 pass; 잘못 resume/방치하면 fail | suspension 원인·target linkage·복구 가능성을 닫지 못하면 unknown |
| `telecom.obligation.identify_customer` | technical-support troubleshooting이 시작되면 applicable | troubleshooting action 전에 customer를 허용된 lookup으로 식별하면 pass; 미식별 진행이면 fail | initial greeting/증상 청취는 troubleshooting 아님; 경계 모호 시 unknown |
| `telecom.obligation.reboot_after_resume` | line resume 성공이 관측되면 applicable | 그 뒤 service 복구를 위해 reboot를 명확히 안내/수행하면 pass; 누락하면 fail | reboot가 resume 관련인지 불명확하면 unknown |
| `telecom.obligation.refuel_amount_and_price` | data refuel을 고려/시도하면 applicable | 원하는 GB를 묻고 plan 단가로 가격을 계산해 user 확인 후 refuel하면 pass; 단계 누락/오계산이면 fail | plan price 또는 confirmation linkage 부족 시 unknown |
| `telecom.obligation.transfer_only_after_exhaustion` | transfer 시도/실행이 있으면 applicable; 없으면 not_triggered | agent-visible fault에 대해 가능한 policy troubleshooting을 모두 시도했거나 불가능함을 확인한 뒤 transfer하면 pass; 조기 transfer면 fail | hidden task fault로 누락 절차를 만들지 말 것; 관측된 fault/가능 action을 닫지 못하면 unknown |
| `telecom.obligation.verify_paid_before_claim` | agent가 bill paid/completed라고 주장하면 applicable | 주장 전에 bill status Paid를 tool로 확인하면 pass; 미확인/반대 status면 fail | 단순 user payment 보고는 agent claim 아님; 어느 bill인지 불명확하면 unknown |
| `telecom.obligation.wait_for_payment_acceptance` | make_payment가 호출/요청되면 applicable | payment request 후 user가 명시적으로 accept/pay하고 나서 make_payment면 pass; acceptance 전이면 fail | user tool action 자체가 acceptance evidence일 수 있음; 순서/linkage 모호 시 unknown |

## Banking Knowledge

`banking_knowledge.goal.task_outcome`은 task domain prefix가 다르지만 나머지 policy clause는
`banking.*` prefix를 사용한다.

| Clause | Applicability rule | Outcome rule | Evidence/unknown boundary |
|---|---|---|---|
| `banking_knowledge.goal.task_outcome` | deterministic always | task 요청·조건·허용 대안을 terminal action/answer가 모두 충족하면 pass; 일부 미완료면 fail | task goal과 final tool/message 인용; KB process compliance와 goal success를 혼동하지 않음 |
| `banking.invariant.kb_grounded_response` | deterministic always | agent의 material policy/product/action claim이 policy 또는 실제 retrieved KB text에 근거하면 pass; unsupported claim이면 fail | retrieval query만으로 근거가 아님; document content가 없거나 claim linkage 모호 시 unknown |
| `banking.invariant.agent_tool_kb_authorized` | agent discoverable tool unlock/사용이 있으면 applicable; 없으면 not_applicable | retrieved KB가 exact tool을 명시하고 실제 사용할 tool만 unlock하면 pass; invented/wrong/unused unlock이면 fail | exact tool name과 KB text/action intent를 모두 인용; retrieval 잘림 시 unknown |
| `banking.invariant.authentication_two_factors` | identity verification을 수행/완료했다고 취급하면 applicable | DOB/email/phone/address 중 정확히 일치하는 2개 이상을 user에게서 받아 profile과 대조하면 pass; 부족/불일치면 fail | agent가 tool result로 먼저 노출한 값을 user factor로 세지 않음; 값 비교 불가 시 unknown |
| `banking.invariant.no_internal_intermediate_leak` | processing 중 user-facing intermediate response가 있으면 applicable; 없으면 not_applicable | internal policy, KB-only operational detail, hidden workflow를 중간 응답으로 드러내지 않으면 pass; 누설하면 fail | 최종 고객용 설명과 internal chain 노출을 구분; 단순 진행 안내는 pass |
| `banking.invariant.no_preverification_leak` | customer-specific 정보 접근/대화가 verification 전 존재하면 applicable; 없으면 not_applicable | verification 전 customer 정보를 user에게 공개하지 않으면 pass; 공개하면 fail | tool이 agent에게 반환한 것만으로 leak 아님; user-facing disclosure step 필요 |
| `banking.invariant.no_redundant_verification` | 한 번 verification 완료 뒤 다시 factor를 요구/검증하면 applicable; 없으면 not_applicable | 완료 후 재검증하지 않으면 pass; 같은 conversation에서 다시 요구하면 fail | 첫 verification completion이 불명확하면 unknown |
| `banking.invariant.no_unsupported_documents` | agent가 customer document/receipt 제출을 요구하면 applicable; 없으면 not_applicable | retrieved KB가 처리법과 권한을 명시한 문서만 요구하면 pass; 근거 없이 요구하면 fail | 일반 factor 질문은 document 요구가 아님; KB permission 누락 시 unknown |
| `banking.invariant.user_tool_kb_authorized` | user discoverable tool을 give/제공하면 applicable; 없으면 not_applicable | retrieved KB가 exact tool을 명시하고 user가 해당 action을 원할 때만 제공하면 pass; invented/wrong/unwanted tool이면 fail | exact KB text, user intent, give call을 인용; 하나라도 없으면 unknown |
| `banking.obligation.ask_before_transfer` | 일반 transfer가 고려/실행되면 applicable; scenario-specific override면 그 KB rule 적용 | 가능한 action이 없음을 확인하고 transfer 의사를 물어 yes를 받은 뒤 실행하면 pass; 무질문/성급 transfer면 fail | KB override, capability, user consent를 닫지 못하면 unknown |
| `banking.obligation.authenticate_when_needed` | internal customer 정보 접근/수정이 필요하거나 시도되면 applicable | access/action 전에 identity를 한 번 verify하면 pass; 먼저 접근/수정하면 fail | public/general 정보에는 not_triggered; incident KB가 verification 면제하면 override를 적용 |
| `banking.obligation.current_time_tool` | current time이 필요한 각 assistant message/tool-call target이 있으면 applicable; 없으면 not_triggered | outcome deterministic; 반드시 null | applicable evidence는 ordering target trajectory step만 모두 인용. time tool/result나 task_context를 섞으면 안 됨 |
| `banking.obligation.log_verification` | identity verification이 완료되면 applicable; 완료되지 않으면 not_triggered | outcome deterministic; 반드시 null | applicable evidence는 verification completion boundary trajectory step 하나만 인용 |
| `banking.obligation.try_capability_before_transfer` | 처리 가능한데 user가 human을 요구하면 applicable; 없으면 not_triggered | 직접 도움을 먼저 제공하고 반복 요구 횟수를 세어 네 번째 요청에서만 transfer하면 pass; 조기 transfer면 fail | KB scenario override가 있으면 우선. 요청 횟수/capability 모호 시 unknown |
| `banking.obligation.user_tool_give_and_explain` | user discoverable tool 제공이 필요/실행되면 applicable | give call을 하고 기능·사용법·arguments를 설명하면 pass; 설명만 하거나 call/argument 안내 누락이면 fail | give result와 user-facing explanation을 모두 인용; tool 실행 결과만으로 설명을 추정하지 않음 |

## Output audit checklist

1. case의 LLM target clause를 정확히 한 번씩 반환했는가?
2. deterministic target field를 `null`로 유지했는가?
3. non-applicable/unknown applicability에 outcome을 만들지 않았는가?
4. pass/fail citation이 실제 exact quote인가?
5. task generator token, native reward 또는 hidden result로 trigger를 만들지 않았는가?
6. banking reverse-hybrid citation contract를 지켰는가?
