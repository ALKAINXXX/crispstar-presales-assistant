import { useState } from 'react';
import type { ReactNode } from 'react';
import { DataTable, type DataTableRow } from '@/components/assistant-ui/elements/data-table';
import { Sources } from '@/components/assistant-ui/elements/sources';
import { SpecSheet } from '@/components/assistant-ui/elements/spec-sheet';
import { money } from './adapter';
import { savedTime } from './quotes';
import type { Business, Catalog, ChatResponse, CurrentTask, Evidence, Match, MatchItem, Pricing, Proposal, Quote, QuoteFacts, SaveAction, SourceRecord } from './types';

export const statusNames = {supported:'支持',unsupported:'不支持',unknown:'未知',pending_condition:'前提待确认'};
const reasons: Record<string,string> = {catalog_unsupported:'目录明确不支持',unsupported_feature:'目录明确不支持',unmodeled_requirement:'现有资料无法判断此要求',ambiguous_requirement:'需求含义还需澄清',condition_assertion_missing:'尚未说明是否具备前提',condition_unknown:'尚不确定是否具备前提',condition_not_met:'你已说明不具备前提',condition_evidence_missing:'缺少可采用的前提资料',condition_evidence_conflict:'相关前提资料存在冲突',condition_evidence_version_mismatch:'前提资料版本不一致',condition_evidence_association_mismatch:'资料未支持此条件',evidence_not_in_chain:'缺少本次评估的有效依据',retrieval_timeout:'资料查询超时',retrieval_failed:'资料查询未完成',condition_evidence_invalid:'前提资料无法采用'};
const pendingNames: Record<string,string> = {paid_seats:'付费席位',months:'订阅月份',requirements:'必须满足的需求',selected_plan_id:'选择套餐',plan:'选择套餐',semantic_review:'原话语义核对',review:'原话语义核对'};
const planName = (id: string | null | undefined, catalog: Catalog | null) => id ? catalog?.plans.find(plan=>plan.plan_id === id)?.name ?? id : '尚无满足全部要求的套餐';

export function collectSources(business: Business) {
  const observations = [...business.retrieval_evidence ?? [], ...business.match?.retrieval_evidence ?? []];
  const sources = new Map<string,SourceRecord>();
  for (const observation of observations) for (const hit of observation.hits ?? []) sources.set(hit.source.source_id, hit.source);
  return sources;
}
function SourceDisclosure({sources}: {sources: SourceRecord[]}) {
  const [open,setOpen] = useState(false);
  if (!sources.length) return null;
  return <Sources locale="zh-CN" open={open} onOpenChange={setOpen} sources={sources.map(source=>({
    sourceId:source.source_id,title:`${source.document_title} · ${source.section_title}`,snippet:source.raw_text,
    location:`资料版本 ${source.dataset_version}`,
    identity:[`${source.relative_path}：${source.line_start}–${source.line_end} 行`, `来源 ${source.source_id}`, source.document_sha256 ? `文档摘要 ${source.document_sha256}` : '', source.text_sha256 ? `原文摘要 ${source.text_sha256}` : ''].filter(Boolean).join('\n'),
  }))}/>;
}

function StatusCell({item,catalog,sources}: {item: MatchItem | undefined; catalog:Catalog|null; sources:Map<string,SourceRecord>}) {
  if (!item) return <span className="match-status unknown">未知 · 响应缺少对应判断</span>;
  const condition = catalog?.conditions.find(condition=>condition.condition_id === item.condition_id);
  const ids = item.condition_evidence_status === 'valid' ? item.evidence_source_ids ?? [] : [];
  return <div className="match-cell" data-status={item.status} data-requirement-id={item.requirement_id}>
    <span className={`match-status ${item.status}`}>{statusNames[item.status]}</span>
    {(item.reason_codes ?? []).length > 0 && <p className="match-reason">{item.reason_codes.map(code=>reasons[code] ?? '此判断仍有未解决限制').join('；')}</p>}
    {condition && <p className="match-condition">{condition.statement}</p>}
    {ids.length > 0 && <div data-evidence-for={item.requirement_id} data-source-ids={ids.join(' ')}><SourceDisclosure sources={ids.map(id=>sources.get(id)).filter((source):source is SourceRecord=>!!source)}/></div>}
  </div>;
}

export function Comparison({match,catalog,sources = new Map()}: {match:Match;catalog:Catalog|null;sources?:Map<string,SourceRecord>}) {
  if (match.requirements.items.length === 0) return null;
  const rows:DataTableRow[] = match.requirements.items.map(item=>({requirement_id:item.requirement_id,statement:item.statement}));
  const role = (id:string) => [id === match.lowest_plan_id ? '最低满足档' : '', match.eligible_plan_ids.includes(id) && id !== match.lowest_plan_id ? '其他满足档' : '',id === match.selected_plan_id ? (match.selection_reason === 'user_selected' ? '你主动选择' : '当前选择') : ''].filter(Boolean).join(' · ');
  const columns = [{key:'statement',label:'本次需求',priority:'primary' as const,sortable:false,width:'27%'}, ...match.plans.map(plan=>({key:plan.plan_id,label:`${planName(plan.plan_id,catalog)}${role(plan.plan_id) ? ` · ${role(plan.plan_id)}` : ''}`,sortable:false,renderCell:(row:DataTableRow):ReactNode=><div data-plan-id={plan.plan_id}><StatusCell item={plan.items.find(item=>item.requirement_id === row.requirement_id)} catalog={catalog} sources={sources}/></div>}))];
  return <section data-section="match" data-ui-part="comparison" data-lowest-plan-id={match.lowest_plan_id ?? ''} data-selected-plan-id={match.selected_plan_id ?? ''} data-eligible-plan-ids={match.eligible_plan_ids.join(' ')} data-selection-reason={match.selection_reason} className="business-block comparison-block">
    <h3>围绕你的需求比较</h3>
    <DataTable columns={columns} rows={rows} rowKey="requirement_id" caption="本次需求的套餐比较" locale="zh-CN" sort={null}/>
    {catalog && <details className="catalog-basis" data-section="catalog" data-catalog-version={catalog.version}><summary>查看目录依据</summary><p className="business-note">以下是当前需求对应的产品目录。是否满足你的要求，还需结合上方比较中的前提与限制。目录版本：{catalog.version}。</p>{match.requirements.items.map(requirement=>{
      const feature = catalog.features.find(feature=>feature.feature_id === requirement.feature_id);
      return <div className="catalog-feature" key={requirement.requirement_id} data-requirement-id={requirement.requirement_id}><p>{requirement.statement}</p>{feature ? <ul>{match.plans.map(plan=>{
        const authoritative = catalog.plans.find(entry=>entry.plan_id === plan.plan_id)?.features?.find(entry=>entry.feature_id === requirement.feature_id);
        const condition = catalog.conditions.find(entry=>entry.condition_id === authoritative?.condition_id);
        return <li key={plan.plan_id} data-plan-id={plan.plan_id}>{planName(plan.plan_id,catalog)}：{authoritative ? authoritative.supported ? '目录支持' : '目录不支持' : '目录未提供对应能力'}{condition ? `。使用前提：${condition.statement}` : ''}</li>;
      })}</ul> : <p className="business-note">该要求尚无对应的目录能力，不能补充支持承诺。</p>}</div>;
    })}</details>}
    {match.selection_reason === 'user_selected' && <p className="business-note">选择更高档不会改变最低满足结论。</p>}
    {match.selection_reason === 'selected_plan_not_satisfying' && <p className="business-warning">你指定的 {planName(match.requirements.selected_plan_id,catalog)} 未满足全部要求，请查看表中限制。</p>}
  </section>;
}

export function CurrentResult({task,catalog,current = true,restored = false,anchor,unitPrice,showQuestions = true}: {task:CurrentTask;catalog:Catalog|null;current?:boolean;restored?:boolean;anchor?:string;unitPrice?:Partial<Pricing>|null;showQuestions?:boolean}) {
  const parameters = new Map(task.requirements.parameters.map(parameter=>[parameter.name,parameter.value]));
  const sources = new Map((task.match?.retrieval_evidence ?? []).flatMap(evidence=>evidence.hits.map(hit=>[hit.source.source_id,hit.source] as const)));
  const needsAssessment = task.status === 'needs_reassessment' || !task.match;
  const price = task.status === 'needs_reassessment' ? null : task.pricing;
  const selectedPrice = task.status === 'needs_reassessment' ? null : price?.unit_price_minor ?? unitPrice?.unit_price_minor;
  const pendingLabel = (name:string) => pendingNames[name] ?? (name.startsWith('condition:') ? catalog?.conditions.find(condition=>condition.condition_id === name.slice(10))?.statement ?? '功能使用前提尚未明确' : name.startsWith('clarify:') ? `需求含义需澄清：${task.requirements.items.find(item=>item.requirement_id === name.slice(8))?.statement ?? '尚未明确的需求'}` : '需要补充信息');
  const selectionIssue = !needsAssessment && task.match?.selection_reason === 'selected_plan_not_satisfying' ? '你指定的套餐未满足全部需求，尚未采用' : !needsAssessment && task.match?.selection_reason === 'unknown_selected_plan' ? '你指定的套餐无法识别，尚未采用' : null;
  const assessedPlanId = task.match?.selected_plan_id ?? (task.match?.selection_reason === 'selected_plan_not_satisfying' ? task.requirements.selected_plan_id : null);
  const unresolvedPlans = task.status === 'needs_reassessment' ? [] : task.match?.plans.filter(plan=>!assessedPlanId || plan.plan_id === assessedPlanId) ?? [];
  const blockers = task.requirements.items.flatMap(requirement=>{
    const unresolved = unresolvedPlans.flatMap(plan=>{
      const item = plan.items.find(item=>item.requirement_id === requirement.requirement_id);
      return !item || item.status !== 'supported' ? [{plan,item}] : [];
    });
    return unresolved.length ? [{requirement,unresolved}] : [];
  });
  const rows = [
    {label:'选择',value:task.requirements.items.length === 0 || task.match?.status === 'comparison_only' ? '尚未评估（需求未明确）' : needsAssessment ? '评估尚未完成' : selectionIssue ?? planName(task.match ? task.match.selected_plan_id : null,catalog),emphasis:true},
    ...(task.requirements.selected_plan_id && task.requirements.selected_plan_id !== task.match?.selected_plan_id ? [{label:'你的指定',value:`${planName(task.requirements.selected_plan_id,catalog)}（尚未采用）`}] : []),
    {label:'付费席位',value:parameters.has('paid_seats') ? `${parameters.get('paid_seats')} 席` : task.pending.includes('paid_seats') ? '待补充' : '未指定'},
    {label:'订阅月份',value:parameters.has('months') ? `${parameters.get('months')} 个月` : task.pending.includes('months') ? '待补充' : '未指定'},
    ...(selectedPrice != null ? [{label:'目录单价',value:`${money(selectedPrice)} 元／席／月`}] : []),
    ...(price ? [{label:'订阅基价',value:`${money(price.base_price_minor)} 元`,emphasis:true}] : [{label:'订阅基价',value:task.status === 'needs_reassessment' ? '待重新评估和计算' : '尚未计算'}]),
    {label:'金额口径',value:'仅订阅基价，不包含税、折扣、实施费等'},
    ...(!task.pending.length && !blockers.length && !selectionIssue ? [{label:'待解决',value:needsAssessment ? '需完成需求评估' : '无'}] : []),
  ];
  return <section id={anchor} tabIndex={-1} data-section="current-task" data-ui-part="spec" data-current={String(current)} data-result-turn-id={task.result_turn_id ?? ''} data-result-message-id={task.result_message_id ?? ''} data-requirement-id={task.requirements.requirement_id} data-revision={task.requirements.revision} data-status={task.status} className={`business-block result-block ${current ? 'is-current' : 'is-history'}`}>
    <SpecSheet title={current ? '当前选型结果' : '历史选型结果'} subtitle={restored ? '服务端仍保留的当前状态' : undefined} rows={rows} visibleCount={rows.length} className="business-spec"/>
    <div className="result-requirements"><h4>已接受需求</h4>{task.requirements.items.length ? <ul>{task.requirements.items.map(requirement=><li key={requirement.requirement_id}>{requirement.statement}</li>)}</ul> : <p className="business-note">尚未明确必须满足的需求。</p>}</div>
    {task.status === 'needs_reassessment' && <p className="business-warning">需求已更新，评估尚未完成；之前的选择和金额不能作为当前结果。请继续完成评估。</p>}
    {(task.pending.length > 0 || blockers.length > 0 || selectionIssue) && <div className="result-blockers" data-section="unresolved"><h4>全部未解决项</h4><ul>{selectionIssue && <li>{selectionIssue}</li>}{task.pending.map(name=><li key={name} data-pending={name}>{pendingLabel(name)}</li>)}{blockers.map(({requirement,unresolved})=><li key={requirement.requirement_id} data-unresolved-requirement-id={requirement.requirement_id}><p>{requirement.statement}</p>{unresolved.map(({plan,item})=><div key={plan.plan_id} data-unresolved-plan-id={plan.plan_id}><span>{planName(plan.plan_id,catalog)}：</span><StatusCell item={item} catalog={catalog} sources={sources}/></div>)}</li>)}</ul></div>}
    {showQuestions && task.questions.length > 0 && <ul className="business-questions">{task.questions.map(question=><li key={question}>{question}</li>)}</ul>}
    {(price || task.requirements.items.length > 0 || task.requirements.parameters.length > 0 || task.requirements.condition_assertions.length > 0) && <details className="result-details"><summary>查看需求原话与详细算式</summary>{price && <p>算式：{price.formula}</p>}<ul>{task.requirements.items.map(requirement=><li key={requirement.requirement_id}>{requirement.statement}：{requirement.user_quote}</li>)}{task.requirements.parameters.map(parameter=><li key={parameter.name}>{pendingNames[parameter.name] ?? parameter.name}：{parameter.user_quote}</li>)}{task.requirements.condition_assertions.map(condition=><li key={condition.condition_id}>前提自述：{condition.user_quote}</li>)}</ul></details>}
    {restored && task.match && task.match.requirements.items.length > 0 && <details className="result-details"><summary>查看逐档比较与目录依据</summary><Comparison match={task.match} catalog={catalog} sources={sources}/></details>}
  </section>;
}

function SafeDiagnostics({response}: {response:ChatResponse}) {
  const business = response.business;
  const observations = [...business?.retrieval_evidence ?? [],...business?.match?.retrieval_evidence ?? []];
  const distinct = new Map(observations.map(evidence=>[evidence.retrieval_id,evidence]));
  return <details data-section="tools" data-ui-part="diagnostics" data-intent={business?.intent} className="diagnostics">
    <summary>查看技术记录</summary>
    <p>执行与检索记录用于进一步核查；其他返回片段未自动成为回答依据。</p>
    <p>会话 {response.session_id} · 回复 {business?.turn_id ?? '未提供'} · 状态 {response.status}</p>
    {business?.semantic_review && <details data-section="semantic-review"><summary>本轮原话语义核对</summary><pre>{JSON.stringify({status:business.semantic_review.status,dispatched:business.semantic_review.dispatched,elapsed_ms:business.semantic_review.elapsed_ms,usage:business.semantic_review.usage,error_code:business.semantic_review.error_code},null,2)}</pre></details>}
    {business?.errors.length ? <pre>{JSON.stringify(business.errors,null,2)}</pre> : null}
    {[...distinct.values()].map((evidence:Evidence)=><details key={evidence.retrieval_id} data-adopted="false"><summary>其他返回资料与检索记录</summary><p>查询：{evidence.query} · {evidence.status} · 相似度仅用于检索排序</p><SourceDisclosure sources={evidence.hits.map(hit=>hit.source)}/><pre>{JSON.stringify({...evidence,hits:evidence.hits.map(hit=>({...hit,source:{source_id:hit.source.source_id}}))},null,2)}</pre></details>)}
    {(response.tool_results ?? []).map((record,index)=><details key={index} data-tool-call-id={String(record.tool_call_id ?? '')}><summary>{String(record.display_name ?? record.name ?? '工具记录')}</summary><p>参数：{String(record.display_arguments_status ?? '未提供')}</p><pre className="tool-arguments">{JSON.stringify(record.display_arguments ?? null,null,2)}</pre><pre className="tool-result">{JSON.stringify(record.display_result ?? null,null,2)}</pre></details>)}
  </details>;
}

export function ProposalSummary({proposal,currentResult,currentProposal,replacedByCurrentProposal = false,anchor,savedQuote,saveAction,onSave,saveDisabled = false}: {proposal:Proposal|QuoteFacts['snapshot'];currentResult:boolean;currentProposal:boolean;replacedByCurrentProposal?:boolean;anchor?:string;savedQuote?:Quote|QuoteFacts;saveAction?:SaveAction;onSave?:(proposalId:string)=>void;saveDisabled?:boolean}) {
  const selected = proposal.plans.find(plan=>plan.plan_id === proposal.selected_plan_id);
  const sources = new Map(proposal.adopted_sources.map(evidence=>[evidence.source.source_id,evidence.source]));
  const price = proposal.pricing;
  const rows = [
    {label:'最低满足档',value:proposal.lowest_plan_name},
    {label:'本次选择',value:proposal.selected_plan_name,emphasis:true},
    {label:'选择依据',value:proposal.selection_reason === 'user_selected' ? '采用你主动指定、且完整满足需求的套餐；最低满足档如上' : '采用完整满足本次需求的最低档'},
    {label:'付费席位',value:`${price.paid_seats} 席`},
    {label:'订阅期限',value:`${price.months} 个月`},
    {label:'实际单价',value:`${money(price.unit_price_minor)} 元／席／月`},
    {label:'币种',value:`人民币（${price.currency}）`},
    {label:'算式',value:price.formula},
    {label:'订阅基价',value:`${money(price.base_price_minor)} 元`,emphasis:true},
  ];
  const full = 'proposal_id' in proposal ? proposal : null;
  const savedRemoved=saveAction?.status === 'saved' && !saveAction.quote && (saveAction.current_status === 'trash' || saveAction.current_status === 'permanently_deleted');
  const available = !!full && currentProposal && full.save_available && !!onSave && !saveAction;
  const previousPlanNotice = `${replacedByCurrentProposal ? '当前已有新准备的方案。' : ''}这是之前准备的方案，现在不能从这里直接保存。已保存内容可在保存记录查看；需要再次保存时，请重新准备方案。`;
  return <section id={anchor} tabIndex={-1} className={`business-block proposal-block result-block ${currentResult ? 'is-current' : 'is-history'}`} data-section={savedQuote ? 'saved-quote' : 'proposal'} data-ui-part={savedQuote ? 'saved-snapshot' : 'approval'} data-current={String(currentResult)} data-proposal-current={String(currentProposal)} data-proposal-id={full?.proposal_id} data-quote-id={savedQuote?.quote_id} data-requirement-id={proposal.requirements.requirement_id} data-revision={proposal.requirements.revision}>
    <div className="proposal-heading"><h3>{savedQuote ? '已保存草案 · 保存时快照' : currentProposal ? '保存前核对 · 当前选型' : currentResult ? '方案摘要 · 当前选型' : '方案摘要 · 历史选型'}</h3><span className="proposal-state">{savedQuote ? '草案已保存' : savedRemoved ? `当时保存成功 · ${saveAction.current_status === 'trash' ? '目前在最近删除' : '目前已永久删除'}` : saveAction ? saveAction.status === 'not_saved' ? '明确未保存' : saveAction.status === 'submitting' ? '保存提交中' : saveAction.status === 'checking' ? '正在读取保存结果' : '保存结果待核对' : currentProposal ? '本次待确认方案' : replacedByCurrentProposal ? '已有新方案 · 此处只读' : '只读查看'}</span></div>
    {savedRemoved && <p className="business-note">{saveAction.message} 此处保留的是聊天中已经出现的历史方案文字，不能用旧保存动作重新创建草案。</p>}
    {savedQuote && <div className="quote-receipt" role="status"><p>真实编号：<span className="quote-number">{savedQuote.quote_id}</span></p><p>保存时间：{savedTime(savedQuote.saved_at)}</p><p className="business-note">以下是保存时的历史事实，不会随当前选型或资料变化。</p></div>}
    <p className="proposal-scope">保存对象是以下需求、套餐、条件、金额与采用依据形成的内部草案快照。</p>
    <div className="proposal-requirements"><h4>本次必须满足</h4><ul>{proposal.requirements.items.map(requirement=>{
      const conclusion = selected?.items.find(item=>item.requirement_id === requirement.requirement_id);
      return <li key={requirement.requirement_id}><span>{requirement.statement}</span><span className={`match-status ${conclusion?.status ?? 'unknown'}`}>{conclusion ? statusNames[conclusion.status] : '未知'}</span></li>;
    })}</ul></div>
    <SpecSheet title="套餐与金额" rows={rows} visibleCount={rows.length} className="business-spec"/>
    {proposal.conditions.length > 0 && <div className="proposal-conditions"><h4>决定性使用条件</h4>{proposal.conditions.map(condition=><div className="proposal-condition" key={condition.condition_id} data-condition-id={condition.condition_id}>
      <p>{condition.statement}</p><p className="proposal-user-assertion">你的自述：{condition.user_quote}</p><SourceDisclosure sources={condition.source_ids.map(id=>sources.get(id)).filter((source):source is SourceRecord=>!!source)}/>
    </div>)}</div>}
    <p className="business-note">仅计算订阅基价；不包含税、折扣、实施费等。保存为内部草案不会发送给客户或构成审批。</p>
    {sources.size > 0 && <div className="proposal-sources" data-adopted="true" data-source-ids={[...sources.keys()].join(' ')}><SourceDisclosure sources={[...sources.values()]}/></div>}
    <details className="catalog-basis proposal-details"><summary>查看需求原话、版本与核查标识</summary>
      <ul>{proposal.requirements.items.map(requirement=><li key={requirement.requirement_id}>{requirement.statement}：{requirement.user_quote}</li>)}{proposal.requirements.parameters.map(parameter=><li key={parameter.name}>{pendingNames[parameter.name] ?? parameter.name}：{parameter.user_quote}</li>)}</ul>
      {(proposal.requirements.context_quotes ?? []).length > 0 && <div><p>其他已采用的用户自述与选择原话：</p><ul>{proposal.requirements.context_quotes!.map((quote,index)=><li key={index}>{quote}</li>)}</ul></div>}
      <p>{full ? `方案 ${full.proposal_id} · ` : ''}需求修订 {proposal.requirements.revision} · 目录版本 {proposal.catalog_version}</p>
      <p>方案形成时间 {proposal.created_at}{full && !savedQuote ? ` · 会话 ${full.session_id} · 交互版本 ${full.interaction_revision}` : ''}</p>
      {proposal.adopted_sources.map(evidence=><p key={`${evidence.retrieval_id}:${evidence.source.source_id}`}>采用检索 {evidence.retrieval_id} · 链路 {evidence.chain_id} · 索引版本 {evidence.index_build_id} · 资料版本 {evidence.dataset_version}</p>)}
    </details>
    {!savedQuote && <div className="proposal-approval" data-save-available={String(available && !saveDisabled)}><p>{saveAction ? saveAction.message : currentProposal ? full?.save_available ? '点击即确认眼前方案并保存为内部草案。' : full?.notice : previousPlanNotice}</p>{!saveAction && <button disabled={!available || saveDisabled} type="button" onClick={()=>full && onSave?.(full.proposal_id)}>确认并保存草案{full && !full.save_available && currentProposal ? '（尚未开放）' : ''}</button>}</div>}
  </section>;
}

export function BusinessResponseView({response,currentTask,currentProposal = null,saveActions = [],onSave,saveDisabled = false}: {response:ChatResponse;currentTask:CurrentTask|null;currentProposal?:Proposal|null;saveActions?:SaveAction[];onSave?:(proposalId:string)=>void;saveDisabled?:boolean}) {
  const business = response.business;
  if (!business?.presentation) return <SafeDiagnostics response={response}/>;
  const catalog = business.catalog_context ?? business.catalog;
  const sourceMap = collectSources(business);
  const used = business.presentation.used_source_ids.map(id=>sourceMap.get(id)).filter((source):source is SourceRecord=>!!source);
  const own = business.current_task;
  const isCurrent = !!own && !!currentTask && (currentTask.result_turn_id === business.turn_id || (currentTask.result_message_id != null && response.message_id != null && String(currentTask.result_message_id) === String(response.message_id)));
  const proposal = business.proposal;
  const isCurrentSelection = !!own && !!currentTask && own.requirements.requirement_id === currentTask.requirements.requirement_id && own.requirements.revision === currentTask.requirements.revision && own.result_turn_id === currentTask.result_turn_id;
  const isCurrentProposal = !!proposal && currentProposal?.proposal_id === proposal.proposal_id && currentProposal.session_id === response.session_id;
  const replacedByCurrentProposal = !!proposal && !!currentProposal && currentProposal.session_id === response.session_id && currentProposal.proposal_id !== proposal.proposal_id;
  const saveAction = proposal ? saveActions.find(action=>action.proposal_id === proposal.proposal_id && action.session_id === response.session_id) : undefined;
  const savedQuote = saveAction?.status === 'saved' ? saveAction.quote ?? undefined : undefined;
  return <div className="business-evidence">
    {!!business.presentation.feedback?.length && <details className="catalog-basis" data-section="operation-feedback" data-ui-part="feedback"><summary>查看本轮接收情况</summary><ul>{business.presentation.feedback.map((item,index)=><li key={`${item.group_id}:${index}`} data-group-id={item.group_id} data-feedback-status={item.status}><p className={item.status === 'accepted' ? 'business-note' : 'business-warning'}>{item.message}</p></li>)}</ul></details>}
    {!proposal && used.length > 0 && <section data-section="sources" data-ui-part="sources" data-adopted="true" className="answer-sources" data-source-ids={business.presentation.used_source_ids.join(' ')}><SourceDisclosure sources={used}/></section>}
    {business.presentation.show_comparison && business.match && <Comparison match={business.match} catalog={catalog} sources={sourceMap}/>}
    {proposal && <ProposalSummary proposal={savedQuote?.snapshot ?? proposal} currentResult={savedQuote ? false : isCurrentSelection} currentProposal={isCurrentProposal} replacedByCurrentProposal={replacedByCurrentProposal} anchor={`result-${business.turn_id}`} saveAction={saveAction} savedQuote={savedQuote} onSave={onSave} saveDisabled={saveDisabled}/>}
    {(business.queried_quotes ?? []).map(quote=><details key={quote.quote_id} className="catalog-basis queried-quote"><summary>查看本条回复当时查回的保存内容与原文</summary><p className="business-note">这是本条回复当时查回的保存快照。当前草案状态以保存记录或编号查询的实际结果为准。</p><ProposalSummary proposal={quote.snapshot} currentResult={false} currentProposal={false} savedQuote={quote}/></details>)}
    {business.intent === 'unit_price' && business.unit_price && <details className="catalog-basis" data-section="catalog" data-catalog-version={business.unit_price.catalog_version}><summary>查看单价依据</summary><p>{business.unit_price.plan_name} · {money(business.unit_price.unit_price_minor)} 元／席／月</p><p className="business-note">来自服务端产品目录，版本 {business.unit_price.catalog_version}。单价查询不会自动改变当前选型。</p></details>}
    {!proposal && business.presentation.show_current_result && own && <CurrentResult task={own} catalog={catalog} current={isCurrent} anchor={`result-${business.turn_id}`} unitPrice={business.unit_price} showQuestions={false}/>}
    <SafeDiagnostics response={response}/>
  </div>;
}
