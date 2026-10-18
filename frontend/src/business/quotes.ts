import type { Quote, QuoteActionResult, SaveAction } from './types';

export const SAVE_REFERENCES_KEY = 'assistant.quoteActionReferences.v1';
const object = (value:unknown):value is Record<string,unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const text = (value:unknown):value is string => typeof value === 'string' && value.length > 0;
const minor = (value:unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const texts = (value:unknown):value is string[] => Array.isArray(value) && value.every(entry=>typeof entry === 'string');
const strings = (value:Record<string,unknown>,keys:string[]) => keys.every(key=>text(value[key]));
const matchItem = (value:unknown) => object(value) && text(value.requirement_id) && ['supported','unsupported','unknown','pending_condition'].includes(String(value.status)) && texts(value.reason_codes) && texts(value.evidence_source_ids);
export function isQuote(value:unknown):value is Quote {
  if (!object(value) || !/^Q-[0-9A-F]{32}$/.test(String(value.quote_id)) || !text(value.saved_at) || !Number.isFinite(Date.parse(value.saved_at)) || value.snapshot_version !== 1 || !object(value.snapshot)) return false;
  const snapshot = value.snapshot, pricing = snapshot.pricing, requirements = snapshot.requirements;
  return strings(snapshot,['proposal_id','session_id','action_ref','selected_plan_id','lowest_plan_id','selected_plan_name','lowest_plan_name','catalog_version','created_at'])
    && ['lowest_satisfying','user_selected'].includes(String(snapshot.selection_reason))
    && object(requirements) && text(requirements.requirement_id) && minor(requirements.revision)
    && Array.isArray(requirements.items) && requirements.items.every(item=>object(item) && strings(item,['requirement_id','statement','user_quote']))
    && Array.isArray(requirements.parameters) && requirements.parameters.every(parameter=>object(parameter) && strings(parameter,['name','user_quote']) && minor(parameter.value))
    && Array.isArray(requirements.condition_assertions) && requirements.condition_assertions.every(assertion=>object(assertion) && strings(assertion,['condition_id','value','user_quote']))
    && (requirements.context_quotes === undefined || texts(requirements.context_quotes))
    && Array.isArray(snapshot.plans) && snapshot.plans.every(plan=>object(plan) && text(plan.plan_id) && Array.isArray(plan.items) && plan.items.every(matchItem))
    && Array.isArray(snapshot.conditions) && snapshot.conditions.every(condition=>object(condition) && strings(condition,['condition_id','name','statement','user_quote']) && texts(condition.source_ids))
    && Array.isArray(snapshot.adopted_sources) && snapshot.adopted_sources.every(source=>object(source) && strings(source,['retrieval_id','chain_id','dataset_version','index_build_id','index_inputs_sha256']) && object(source.source) && strings(source.source,['source_id','document_title','section_title','relative_path','dataset_version','raw_text']) && minor(source.source.line_start) && minor(source.source.line_end))
    && object(pricing) && minor(pricing.unit_price_minor) && minor(pricing.base_price_minor) && minor(pricing.paid_seats) && minor(pricing.months) && text(pricing.formula) && pricing.currency === 'CNY';
}
export async function readQuoteAction(response:Response, action:SaveAction):Promise<QuoteActionResult> {
  const value:unknown = await response.json();
  if (!object(value) || !text(value.code) || !text(value.message)) throw new Error('无法核实保存回执');
  if (value.status === 'saved' && response.status === 200 && isQuote(value.quote)
    && value.quote.snapshot.session_id === action.session_id && value.quote.snapshot.action_ref === action.action_ref && value.quote.snapshot.proposal_id === action.proposal_id) return value as unknown as QuoteActionResult;
  if (value.status === 'saved' && response.status === 200 && value.quote === null && ['trash','permanently_deleted'].includes(String(value.current_status)) && /^Q-[0-9A-F]{32}$/.test(String(value.quote_id))) return value as unknown as QuoteActionResult;
  if (((value.status === 'not_saved' && response.status === 200) || (value.status === 'unknown' && response.status === 503)) && value.quote === null) return value as unknown as QuoteActionResult;
  throw new Error('无法核实保存回执');
}
export function restoreSaveReferences(storage:Pick<Storage,'getItem'>,key = SAVE_REFERENCES_KEY):SaveAction[] {
  try {
    const values:unknown = JSON.parse(storage.getItem(key) ?? '[]');
    if (!Array.isArray(values)) return [];
    const references = new Map<string,SaveAction>();
    for (const value of values) if (object(value) && text(value.session_id) && text(value.proposal_id) && text(value.started_at) && Number.isFinite(Date.parse(value.started_at)) && /^[A-Za-z0-9_-]{32,128}$/.test(String(value.action_ref))) {
      const action = {session_id:value.session_id,proposal_id:value.proposal_id,action_ref:String(value.action_ref),started_at:value.started_at,status:'unknown' as const,code:'restored_reference',message:'已恢复之前发起的保存动作，打开保存记录后将读取真实结果。旧方案的保存许可不会恢复。',quote:null,restored:true};
      references.set(`${action.session_id}:${action.action_ref}`,action);
    }
    return [...references.values()];
  } catch { return []; }
}
export function persistSaveReferences(storage:Pick<Storage,'getItem'|'setItem'>,actions:SaveAction[],key = SAVE_REFERENCES_KEY) {
  const merged=new Map(restoreSaveReferences(storage,key).map(action=>[`${action.session_id}:${action.action_ref}`,action]));
  for (const action of actions) merged.set(`${action.session_id}:${action.action_ref}`,action);
  storage.setItem(key,JSON.stringify([...merged.values()].map(({session_id,action_ref,proposal_id,started_at})=>({session_id,action_ref,proposal_id,started_at}))));
}
export function savedTime(value:string) {
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)) + '（北京时间）';
}
