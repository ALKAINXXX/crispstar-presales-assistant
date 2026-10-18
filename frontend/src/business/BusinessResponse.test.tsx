// Synthetic responses exercise production React components. They are not
// real-model, real-browser, or user experience acceptance evidence.
import { act, fireEvent, render } from '@testing-library/react';
import { expect, it } from 'vitest';
import { BusinessResponseView, Comparison, CurrentResult } from './BusinessResponse';
import type { Business, Catalog, ChatResponse, CurrentTask, Match, Proposal, Requirements, SourceRecord } from './types';

const catalog: Catalog = { version: 'fixture-v1', product: { name: '受控产品' }, plans: [{ plan_id: 'basic', name: '基础版 Basic', rank: 1, unit_price_minor: 4000 }, { plan_id: 'team', name: '团队版 Team', rank: 2, unit_price_minor: 8081 }, { plan_id: 'business', name: '商务版 Business', rank: 3, unit_price_minor: 12000 }], features: [], conditions: [{ condition_id: 'idp', name: '身份系统', statement: '须当前具备兼容的企业身份提供方' }] };
const requirements = (): Requirements => ({ requirement_id: 'fixture-requirements', revision: 2, selected_plan_id: null, items: [{ requirement_id: 'api', statement: '需要产品 API', kind: 'feature', feature_id: 'api', user_quote: '产品API必须有' }], parameters: [{ name: 'paid_seats', value: 3, user_quote: '3席' }, { name: 'months', value: 2, user_quote: '2个月' }], condition_assertions: [] });
const source = (id = 'source-used', raw = '须当前具备兼容的企业身份提供方。'): SourceRecord => ({ source_id: id, document_title: '受控产品原文', section_title: '统一身份登录', relative_path: 'docs/product.md', line_start: 12, line_end: 14, dataset_version: 'fixture-v1', raw_text: raw });
function base(): ChatResponse {
  const business: Business = { turn_id: 'fixture-turn', intent: 'selection', requirements: requirements(), catalog_context: catalog, catalog: null, match: null, pricing: null, unit_price: null, current_task: null, retrieval_evidence: [], knowledge_answers: [], errors: [], changes: [], pending: [], questions: [], presentation: { answer_markdown: '采用团队版 Team，决定性理由是产品 API。', show_comparison: false, show_current_result: false, used_source_ids: [] } };
  return { session_id: 'fixture-session', message_id: 'stored-row', status: 'completed', message: '独立可理解的正式历史文本', model: 'controlled-only', tool_results: [], business };
}
function match(req: Requirements = requirements()): Match {
  return { requirements: req, eligible_plan_ids: ['team', 'business'], lowest_plan_id: 'team', selected_plan_id: 'team', selection_reason: 'lowest_satisfying', status: 'matched', retrieval_evidence: [], plans: catalog.plans.map(plan => ({ plan_id: plan.plan_id, satisfies_all: plan.plan_id !== 'basic', blocking_reasons: [], items: req.items.map(item => ({ requirement_id: item.requirement_id, status: plan.plan_id === 'basic' ? 'unsupported' : 'supported', reason_codes: plan.plan_id === 'basic' ? ['catalog_unsupported'] : [], condition_id: null, condition_evidence_status: null, condition_assertion_status: null, evidence_source_ids: [] })) })) };
}
function task(overrides: Partial<CurrentTask> = {}): CurrentTask {
  return { kind: 'selection', status: 'current', last_result: 'updated', requirements: requirements(), match: match(), pricing: { plan_id: 'team', plan_name: '团队版 Team', unit_price_minor: 8081, paid_seats: 3, months: 2, base_price_minor: 1234567, catalog_version: 'fixture-v1', formula: '服务端算式 TOKEN × 3 席 × 2 月 = 返回金额（订阅基价）' }, result_turn_id: 'fixture-turn', result_message_id: 'stored-row', pending: [], questions: [], ...overrides };
}
function proposalResponse() {
  const req = requirements();
  req.items = [{requirement_id:'sso',statement:'必须支持 SSO',feature_id:'sso',kind:'feature',user_quote:'SSO必须支持'}, {requirement_id:'audit',statement:'必须支持审计日志',feature_id:'audit',kind:'feature',user_quote:'审计日志必须支持'}];
  req.parameters = [{name:'paid_seats',value:80,user_quote:'80个付费席位'}, {name:'months',value:12,user_quote:'12个月'}];
  req.condition_assertions = [{condition_id:'idp',value:'met',user_quote:'我们目前已具备兼容的企业身份提供方'}];
  const comparison = match(req); comparison.lowest_plan_id = 'business'; comparison.selected_plan_id = 'business'; comparison.eligible_plan_ids = ['business'];
  const adopted = source('adopted-condition');
  const proposal:Proposal = {proposal_id:'snapshot-1',session_id:'fixture-session',turn_id:'fixture-turn',interaction_revision:3,created_at:'2026-10-04T12:00:00Z',catalog_version:'fixture-v1',requirements:req,plans:comparison.plans,eligible_plan_ids:['business'],lowest_plan_id:'business',lowest_plan_name:'商务版 Business',selected_plan_id:'business',selected_plan_name:'商务版 Business',selection_reason:'lowest_satisfying',pricing:{plan_id:'business',plan_name:'商务版 Business',unit_price_minor:12000,paid_seats:80,months:12,base_price_minor:11520000,formula:'120.00 元／席／月 × 80 席 × 12 月 = 115,200.00 元（订阅基价）',catalog_version:'fixture-v1',currency:'CNY',billing_unit:'seat_month',minor_units_per_major:100},conditions:[{condition_id:'idp',name:'身份系统',statement:'须当前具备兼容的企业身份提供方',user_quote:'我们目前已具备兼容的企业身份提供方',source_ids:[adopted.source_id]}],adopted_sources:[{retrieval_id:'selection-retrieval',chain_id:'selection-chain',dataset_version:'fixture-v1',index_build_id:'fixture-index',index_inputs_sha256:'fixture-sha',source:adopted}],action_ref:'SECRET_UNGUESSABLE_ACTION',status:'pending_confirmation',save_available:false,notice:'方案已准备，尚未保存；确认并保存草案功能尚未开放。'};
  const current = task({requirements:req,match:comparison,pricing:proposal.pricing});
  const response = base(); response.business!.requirements = req; response.business!.match = comparison; response.business!.current_task = current; response.business!.proposal = proposal; response.business!.presentation!.show_current_result = true; response.business!.presentation!.show_comparison = true;
  // Unrelated retrieval and live price fields must not replace snapshot facts.
  response.business!.retrieval_evidence = [{retrieval_id:'detour',query:'插问',status:'retrieved',hits:[{source:source('detour-source','插问原文不属于此方案'),rank:1,score:1,relevance:'related'}]}];
  return {response,current,proposal};
}
async function expandButtons(container: HTMLElement) {
  for (const button of [...container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')]) await act(async () => { fireEvent.click(button); });
}

it('live inline result leaves its service questions in the formal answer while a standalone result retains them', () => {
  const question = '明确购买多少个付费席位？';
  const current = task({pending:['paid_seats'],questions:[question],pricing:null});
  const response = base();
  response.business!.current_task = current;
  response.business!.pending = ['paid_seats'];
  response.business!.questions = [question];
  response.business!.presentation!.show_current_result = true;
  response.business!.presentation!.answer_markdown = `Team支持API。\n\n请补充：${question}`;
  const inline = render(<BusinessResponseView response={response} currentTask={current}/>);
  expect(inline.container.querySelector('[data-section="current-task"]')).not.toBeNull();
  expect(inline.container.querySelector('.business-questions')).toBeNull();
  expect(response.business!.presentation!.answer_markdown).toContain(question);
  inline.unmount();
  const standalone = render(<CurrentResult task={current} catalog={catalog}/>);
  expect(standalone.container.querySelector('.business-questions')?.textContent).toBe(question);
});

it('SpecSheet formats authoritative returned amount and formula without recomputing', () => {
  const current = task(); const view = render(<CurrentResult task={current} catalog={catalog} unitPrice={{ unit_price_minor: 8081 }} />);
  expect(view.container.textContent).toContain('80.81 元／席／月');
  expect(view.container.textContent).toContain('12,345.67 元');
  expect(view.container.textContent).toContain(current.pricing!.formula);
  expect(view.container.textContent).not.toContain('484.86');
  expect(view.container.querySelector('[data-current="true"]')).not.toBeNull();
  expect(view.container.querySelector('button')).toBeNull();
});

it('DataTable preserves all four states, requirements as rows, catalog order and actual choice meanings', () => {
  const req = requirements(); req.items.push({ requirement_id: 'scim', statement: '必须支持 SCIM', feature_id: null, kind: 'unmodeled', user_quote: 'SCIM必须有' }, { requirement_id: 'sso', statement: '必须支持 SSO', feature_id: 'sso', kind: 'feature', user_quote: 'SSO必须有' });
  const comparison = match(req); comparison.selected_plan_id = 'business'; comparison.selection_reason = 'user_selected';
  for (const plan of comparison.plans) {
    plan.items[1].status = 'unknown'; plan.items[1].reason_codes = ['unmodeled_requirement'];
    plan.items[2].status = 'pending_condition'; plan.items[2].condition_id = 'idp'; plan.items[2].reason_codes = ['condition_assertion_missing'];
  }
  const view = render(<Comparison match={comparison} catalog={catalog} />);
  const section = view.container.querySelector('[data-section="match"]')!;
  for (const word of ['支持', '不支持', '未知', '前提待确认', '需要产品 API', '必须支持 SCIM', '必须支持 SSO', '须当前具备兼容的企业身份提供方', '最低满足档', '其他满足档', '你主动选择']) expect(section.textContent).toContain(word);
  expect([...section.querySelectorAll('thead th')].slice(1).map(th => th.textContent)).toEqual(['基础版 Basic', '团队版 Team · 最低满足档', '商务版 Business · 其他满足档 · 你主动选择']);
  expect(section.querySelector('thead button')).toBeNull();
  expect(section.getAttribute('data-selected-plan-id')).toBe('business');
});

it('actual sources preserve complete original and location, and unadopted retrieval is only diagnostics', async () => {
  const response = base(); const raw = '完整中文长原文。'.repeat(150); const used = source('source-used', raw); const other = source('source-other', '召回但未采用原文。');
  response.business!.intent = 'knowledge'; response.business!.retrieval_evidence = [{ retrieval_id: 'retrieval-used', query: 'SSO前提', status: 'retrieved', hits: [{ source: used, rank: 1, score: 0.6, relevance: 'related' }, { source: other, rank: 2, score: 0.5, relevance: 'related' }] }];
  response.business!.presentation!.used_source_ids = ['source-used'];
  const view = render(<BusinessResponseView response={response} currentTask={null} />);
  const adopted = view.container.querySelector('[data-section="sources"]')!;
  expect(adopted.getAttribute('data-adopted')).toBe('true');
  await expandButtons(view.container);
  const original = adopted.querySelector('.source-raw-text')!;
  expect(original.textContent).toBe(raw);
  expect(adopted.textContent).toContain('docs/product.md'); expect(adopted.textContent).toContain('12–14'); expect(adopted.textContent).toContain('fixture-v1');
  expect(adopted.querySelector('[data-source-id="source-other"]')).toBeNull();
  expect(adopted.querySelector('a[href]')).toBeNull();
  expect(view.container.querySelector('[data-section="match"]')).toBeNull();
});

it('comparison conditions have evidence bound to specific requirement rather than all recalled hits', async () => {
  const req = requirements(); const assessed = match(req);
  assessed.plans[1].items[0] = { ...assessed.plans[1].items[0], condition_id: 'idp', condition_evidence_status: 'valid', evidence_source_ids: ['condition-source'] };
  const used = source('condition-source');
  const view = render(<Comparison match={assessed} catalog={catalog} sources={new Map([['condition-source', used]])} />);
  const link = view.container.querySelector('[data-evidence-for="api"]')!;
  expect(link.getAttribute('data-source-ids')).toBe('condition-source'); await expandButtons(view.container);
  expect(link.querySelector('.source-raw-text')?.textContent).toBe(used.raw_text);
});

it('rejected inquiry does not create a fresh spec or comparison or replace the original result identity', () => {
  const current = task(); const response = base(); response.business!.turn_id = 'rejected-turn'; response.message_id = 'rejected-row'; response.business!.intent = 'selection'; response.business!.current_task = { ...current, last_result: 'not_updated' };
  response.business!.errors = [{ code: 'invalid_arguments', message: '约数席位未接受，仍保留此前3席。' }];
  const view = render(<BusinessResponseView response={response} currentTask={current} />);
  expect(view.container.querySelector('[data-section="current-task"]')).toBeNull(); expect(view.container.querySelector('[data-section="match"]')).toBeNull();
  expect(view.container.textContent).not.toContain('已保存');
  const old = render(<CurrentResult task={current} catalog={catalog} current={true} />);
  expect(old.container.querySelector('[data-current="true"]')?.getAttribute('data-result-turn-id')).toBe('fixture-turn');
});

it('accepted but incomplete modification drops old price and identifies need to assess again', () => {
  const modified = task({ status: 'needs_reassessment', match: null, pricing: null, result_turn_id: 'changed-turn', requirements: { ...requirements(), revision: 3, parameters: [{ name: 'paid_seats', value: 50, user_quote: '50席' }, { name: 'months', value: 2, user_quote: '2个月' }] } });
  const view = render(<CurrentResult task={modified} catalog={catalog} />);
  expect(view.container.textContent).toContain('50 席'); expect(view.container.textContent).toContain('2 个月'); expect(view.container.textContent).toContain('评估尚未完成');
  expect(view.container.textContent).not.toContain('12,345.67'); expect(view.container.textContent).not.toContain('服务端算式 TOKEN');
});

it('safe diagnostics render only sanitized tool fields, with real order and no synthetic save controls', () => {
  const response = base(); const injected = '<button onclick="steal()">确认并保存</button><script>steal()</script>';
  response.tool_results = [{ tool_call_id: 'call-one', display_name: '读取目录', arguments: '{"hidden":"HIDDEN_BAD_BODY"}', display_arguments_status: 'rejected', display_arguments: {}, result: { raw: 'HIDDEN_BAD_BODY' }, display_result: { ok: false, error: { code: 'invalid_arguments', message: injected } } }, { tool_call_id: 'call-two', display_name: '校验价格', display_arguments: { plan_id: 'team' }, display_result: { ok: true, data: { amount: 1234567 } } }];
  const view = render(<BusinessResponseView response={response} currentTask={null} />);
  const records = [...view.container.querySelectorAll('[data-tool-call-id]')];
  expect(records.map(record => record.getAttribute('data-tool-call-id'))).toEqual(['call-one', 'call-two']);
  expect(JSON.parse(records[0].querySelector('.tool-arguments')!.textContent!)).toEqual({});
  expect(view.container.textContent).not.toContain('HIDDEN_BAD_BODY');
  expect(JSON.parse(records[0].querySelector('.tool-result')!.textContent!).error.message).toBe(injected);
  expect(view.container.querySelector('script, button[onclick], a[href^="javascript:"]')).toBeNull();
  expect(view.container.querySelector('[data-section="tools"]')?.hasAttribute('open')).toBe(false);
});

it('live result becomes history according to server result identity rather than newest reply', () => {
  const response = base(); const old = task(); response.business!.current_task = old; response.business!.presentation!.show_current_result = true;
  const current = task({ result_turn_id: 'new-selection-turn', result_message_id: 'new-row' });
  const view = render(<BusinessResponseView response={response} currentTask={current} />);
  expect(view.container.querySelector('[data-current="true"]')).toBeNull(); expect(view.container.querySelector('[data-current="false"]')).not.toBeNull();
  expect(view.container.textContent).toContain('历史选型结果');
});

it('missing requirements after process-state loss show no empty comparison or false no-satisfying conclusion', () => {
  const empty = { ...requirements(), items: [], parameters: [], selected_plan_id: null };
  const unassessed = { ...match(empty), lowest_plan_id: null, selected_plan_id: null, eligible_plan_ids: [], status: 'comparison_only', selection_reason: 'empty_requirements' };
  const comparison = render(<Comparison match={unassessed} catalog={catalog} />);
  expect(comparison.container.querySelector('[data-slot="data-table"]')).toBeNull();
  expect(comparison.container.textContent).not.toContain('No rows');
  const result = render(<CurrentResult task={task({ requirements: empty, match: unassessed, pricing: null, pending: ['requirements'], questions: ['请重新说明必须满足的需求。'] })} catalog={catalog} />);
  expect(result.container.textContent).toContain('尚未评估（需求未明确）');
  expect(result.container.textContent).toContain('请重新说明必须满足的需求');
  expect(result.container.textContent).not.toContain('尚无满足全部要求的套餐');
});

it('real-shaped Proposal snapshot supplies the full review object and disabled approval without deriving facts from other response fields', () => {
  const {response,current,proposal} = proposalResponse();
  response.business!.pricing = {...proposal.pricing,base_price_minor:1,formula:'UNADOPTED_PRICE'};
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  const summary = view.container.querySelector('[data-section="proposal"]')!;
  for (const text of ['本次必须满足','必须支持 SSO','必须支持审计日志','最低满足档','本次选择','商务版 Business','80 席','12 个月','120.00 元／席／月','人民币（CNY）','115,200.00 元',proposal.pricing.formula,'须当前具备兼容的企业身份提供方','我们目前已具备兼容的企业身份提供方','内部草案','尚未保存','尚未开放']) expect(summary.textContent).toContain(text);
  expect(summary.getAttribute('data-current')).toBe('true'); expect(summary.getAttribute('data-proposal-current')).toBe('true');
  expect(summary.querySelectorAll('.proposal-requirements .match-status.supported')).toHaveLength(2);
  expect(summary.querySelector<HTMLButtonElement>('.proposal-approval button')?.disabled).toBe(true);
  expect(view.container.querySelector('[data-section="current-task"]')).toBeNull();
  expect(summary.textContent).not.toContain('UNADOPTED_PRICE'); expect(summary.textContent).not.toContain('SECRET_UNGUESSABLE_ACTION');
  expect(summary.querySelector('[data-source-id="detour-source"]')).toBeNull();
  expect(summary.textContent).not.toContain('已确认完成'); expect(summary.textContent).not.toContain('Quote');
});

it('Proposal shows a user-selected higher plan separately from the minimum and preserves only adopted original sources', async () => {
  const {response,current,proposal} = proposalResponse();
  proposal.lowest_plan_id = 'team'; proposal.lowest_plan_name = '团队版 Team'; proposal.selection_reason = 'user_selected';
  proposal.requirements.context_quotes = ['我们使用企业目录；我主动选择商务版 Business。'];
  const raw = '此方案实际采用的完整原文。'.repeat(100) + '<script>neverRun()</script>';
  proposal.adopted_sources[0].source.raw_text = raw;
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  const summary = view.container.querySelector('[data-section="proposal"]')!;
  expect(summary.textContent).toContain('团队版 Team'); expect(summary.textContent).toContain('商务版 Business'); expect(summary.textContent).toContain('你主动指定');
  expect(summary.textContent).toContain('我们使用企业目录；我主动选择商务版 Business。');
  await expandButtons(summary as HTMLElement);
  expect(summary.querySelector('.source-raw-text')?.textContent).toBe(raw);
  expect(summary.textContent).toContain('docs/product.md'); expect(summary.textContent).toContain('12–14'); expect(summary.textContent).toContain('fixture-v1');
  expect(summary.querySelector('script,button[onclick],a[href]')).toBeNull();
  expect(summary.textContent).not.toContain('插问原文不属于此方案');
});

it('unchanged current selection and expired old proposal action have distinct display identities', () => {
  const {response,current,proposal} = proposalResponse();
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  view.rerender(<BusinessResponseView response={response} currentTask={current} currentProposal={null}/>);
  const summary = view.container.querySelector('[data-section="proposal"]')!;
  expect(summary.getAttribute('data-current')).toBe('true'); expect(summary.getAttribute('data-proposal-current')).toBe('false');
  expect(summary.textContent).toContain('只读查看'); expect(summary.textContent).toContain('这是之前准备的方案，现在不能从这里直接保存');
  expect(summary.textContent).toContain('已保存内容可在保存记录查看');
  expect(summary.textContent).not.toContain('冲突'); expect(summary.textContent).not.toContain('已失效');
  const changed = task({result_turn_id:'new-result',requirements:{...current.requirements,revision:3}});
  view.rerender(<BusinessResponseView response={response} currentTask={changed} currentProposal={null}/>);
  expect(summary.getAttribute('data-current')).toBe('false'); expect(summary.textContent).toContain('历史选型');
  expect(summary.textContent).not.toContain('已有新方案');
});

it('only an actual different current Proposal labels a previous summary as replaced, including equal content', () => {
  const {response,current,proposal} = proposalResponse();
  const newer = {...proposal,proposal_id:'another-legal-action',action_ref:'OTHER_ACTION'};
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={newer}/>);
  const summary = view.container.querySelector('[data-section="proposal"]')!;
  expect(summary.textContent).toContain('已有新方案 · 此处只读');
  expect(summary.textContent).toContain('当前已有新准备的方案');
  expect(summary.textContent).not.toContain('冲突'); expect(summary.textContent).not.toContain('明确未保存');
  expect(summary.querySelector<HTMLButtonElement>('.proposal-approval button')?.disabled).toBe(true);
  view.rerender(<BusinessResponseView response={response} currentTask={null} currentProposal={null}/>);
  expect(summary.textContent).toContain('只读查看');
  expect(summary.textContent).not.toContain('已有新方案');
  expect(summary.textContent).not.toContain('明确未保存');
});

it('reassembly on a newer turn retains current selection identity without reviving an old action', () => {
  const {response,current,proposal} = proposalResponse();
  response.business!.turn_id = 'prepare-again'; proposal.turn_id = 'prepare-again'; proposal.proposal_id = 'snapshot-2';
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  const summary = view.container.querySelector('[data-section="proposal"]')!;
  expect(summary.getAttribute('data-current')).toBe('true'); expect(summary.getAttribute('data-proposal-current')).toBe('true');
  expect(summary.id).toBe('result-prepare-again');
});

it('unit price and independent pricing cannot render approval from completed status or model confirmation', () => {
  for (const intent of ['unit_price','price']) {
    const response = base(); response.business!.intent = intent; response.business!.pricing = task().pricing;
    response.message = 'confirmed=true 帮我保存';
    const view = render(<BusinessResponseView response={response} currentTask={null}/>);
    expect(view.container.querySelector('[data-section="proposal"],.proposal-approval')).toBeNull();
    view.unmount();
  }
});

it('a prepared save object leaves this turn’s key comparison visible before review without a duplicate CurrentResult', () => {
  const {response,current,proposal} = proposalResponse();
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  const comparison = view.container.querySelector('[data-section="match"]')!;
  const review = view.container.querySelector('[data-section="proposal"]')!;
  expect(comparison.closest('details')).toBeNull();
  expect(comparison.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(view.container.querySelector('[data-section="current-task"]')).toBeNull();
  for (const label of ['付费席位','订阅期限','实际单价','算式','订阅基价','决定性使用条件']) {
    const visibleText = [...review.querySelectorAll('span,h4,p')].find(node=>node.textContent === label);
    expect(visibleText).toBeDefined();
    expect(visibleText?.closest('details')).toBeNull();
  }
  expect(review.querySelector('.proposal-sources')?.closest('details')).toBeNull();
});

it('quantity-only presentation keeps the new complete review and omits the unchanged comparison', () => {
  const {response,current,proposal} = proposalResponse();
  response.business!.presentation!.show_comparison = false;
  const view = render(<BusinessResponseView response={response} currentTask={current} currentProposal={proposal}/>);
  expect(view.container.querySelector('[data-section="match"]')).toBeNull();
  expect(view.container.querySelector('[data-section="proposal"]')?.textContent).toContain(proposal.pricing.formula);
  expect(view.container.querySelector('[data-section="current-task"]')).toBeNull();
});

it('same-page CurrentResult shows every accepted requirement and unresolved judgment outside long-details disclosures', () => {
  const req = requirements();
  req.items = Array.from({length:10},(_,index)=>({requirement_id:`unknown-${index}`,statement:`未建模的长中文需求 ${index}`,kind:'unmodeled',feature_id:null,user_quote:`用户原话 ${index}`}));
  const assessed = match(req); assessed.lowest_plan_id = null; assessed.selected_plan_id = null; assessed.eligible_plan_ids = [];
  for (const plan of assessed.plans) for (const item of plan.items) { item.status = 'unknown'; item.reason_codes = ['unmodeled_requirement']; }
  const current = task({requirements:req,match:assessed,pricing:null,pending:['paid_seats','months','condition:idp']});
  const view = render(<CurrentResult task={current} catalog={catalog} restored/>);
  expect(view.container.querySelector('.result-requirements')?.closest('details')).toBeNull();
  expect(view.container.querySelectorAll('[data-unresolved-requirement-id]')).toHaveLength(10);
  for (const node of view.container.querySelectorAll('[data-unresolved-requirement-id],[data-pending]')) expect(node.closest('details')).toBeNull();
  expect(view.container.querySelector('[data-pending="condition:idp"]')?.textContent).toContain('须当前具备兼容的企业身份提供方');
  expect(view.container.querySelector('.business-spec')?.textContent).toContain('金额口径');
  expect(view.container.querySelector('.result-details')?.hasAttribute('open')).toBe(false);
  expect(view.container.querySelector('.result-details')?.textContent).toContain('用户原话 9');
  expect(view.container.querySelector('[data-section="match"]')?.closest('details')?.hasAttribute('open')).toBe(false);
  expect(view.container.querySelector('.proposal-approval')).toBeNull();
});

it('CurrentResult with an unfinished assessment cannot present a retained old amount or choice as current', () => {
  const current = task({status:'needs_reassessment'});
  const view = render(<CurrentResult task={current} catalog={catalog} unitPrice={{unit_price_minor:8081}}/>);
  expect(view.container.querySelector('.business-spec')?.textContent).toContain('评估尚未完成');
  expect(view.container.textContent).not.toContain('12,345.67');
  expect(view.container.textContent).not.toContain(current.pricing!.formula);
  expect(view.container.querySelector('.result-blockers')).toBeNull();
  expect(view.container.querySelector('.result-requirements')?.textContent).toContain('需要产品 API');
});

it('a refused specified plan remains an unresolved choice and does not inherit blockers from unrelated alternatives', () => {
  const req = requirements(); req.selected_plan_id = 'basic';
  const assessed = match(req); assessed.selected_plan_id = null; assessed.selection_reason = 'selected_plan_not_satisfying';
  const view = render(<CurrentResult task={task({requirements:req,match:assessed,pricing:null})} catalog={catalog}/>);
  expect(view.container.querySelector('.business-spec')?.textContent).toContain('你指定的套餐未满足全部需求，尚未采用');
  expect(view.container.querySelector('.result-blockers')?.textContent).toContain('目录明确不支持');
  expect([...view.container.querySelectorAll('[data-unresolved-plan-id]')].map(node=>node.getAttribute('data-unresolved-plan-id'))).toEqual(['basic']);
  expect(view.container.querySelector('.business-spec')?.textContent).not.toContain('待解决无');
});

it('one source disclosure exposes adopted original, section, version and location without an additional location fold', async () => {
  const response = base();
  response.business!.intent = 'knowledge';
  response.business!.presentation!.used_source_ids = ['adopted'];
  response.business!.retrieval_evidence = [{retrieval_id:'source-query',query:'前提',status:'retrieved',hits:[{source:source('adopted'),rank:1,score:1,relevance:'related'}]}];
  const view = render(<BusinessResponseView response={response} currentTask={null}/>);
  const adopted = view.container.querySelector('[data-section="sources"]') as HTMLElement;
  await expandButtons(adopted);
  expect(adopted.querySelector('details')).toBeNull();
  expect(adopted.querySelector('.source-identity')?.textContent).toContain('docs/product.md：12–14 行');
  expect(adopted.textContent).toContain('统一身份登录');
  expect(adopted.textContent).toContain('资料版本 fixture-v1');
  expect(adopted.textContent).not.toContain('undefined');
});
