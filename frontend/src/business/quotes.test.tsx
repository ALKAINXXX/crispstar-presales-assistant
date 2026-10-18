// Controlled transport failures exercise the production browser controller.
// SQLite commits and real model calls are checked independently.
import { act, fireEvent, render } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { SessionController } from './adapter';
import { BusinessResponseView, ProposalSummary } from './BusinessResponse';
import { QuotePanels } from './QuotePanels';
import { isQuote, SAVE_REFERENCES_KEY } from './quotes';
import type { Business, ChatResponse, CurrentTask, Proposal, Quote, QuoteFacts } from './types';
import { testDataManagement } from './testData';

const actionRef = 'PRIVATE_ACTION_012345678901234567890123456789';
const quoteId = 'Q-0123456789ABCDEF0123456789ABCDEF';
const response = (value:unknown,status = 200) => new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
function snapshot(viewId = 'view'):Proposal {
  const requirements = {requirement_id:'requirement',revision:1,items:[{requirement_id:'audit',statement:'必须支持审计日志',user_quote:'审计日志必须支持',feature_id:'audit',kind:'feature'}],parameters:[{name:'paid_seats',value:80,user_quote:'80席'},{name:'months',value:12,user_quote:'12个月'}],condition_assertions:[],selected_plan_id:null};
  return {proposal_id:'proposal',session_id:'same',view_id:viewId,turn_id:'selection',interaction_revision:1,created_at:'2026-10-05T01:00:00Z',catalog_version:'original-v1',requirements,plans:[{plan_id:'business',items:[{requirement_id:'audit',status:'supported',reason_codes:[],condition_id:null,evidence_source_ids:[],condition_evidence_status:null,condition_assertion_status:null}],satisfies_all:true,blocking_reasons:[]}],eligible_plan_ids:['business'],lowest_plan_id:'business',lowest_plan_name:'商务版 Business',selected_plan_id:'business',selected_plan_name:'商务版 Business',selection_reason:'lowest_satisfying',pricing:{plan_id:'business',plan_name:'商务版 Business',unit_price_minor:12000,paid_seats:80,months:12,base_price_minor:11520000,formula:'120.00 × 80 × 12 = 115,200.00',catalog_version:'original-v1',currency:'CNY',billing_unit:'seat_month',minor_units_per_major:100},conditions:[],adopted_sources:[],action_ref:actionRef,status:'pending_confirmation',save_available:true,notice:'尚未保存'};
}
const quote = (proposal:Proposal):Quote => ({quote_id:quoteId,saved_at:'2026-10-05T01:02:03+00:00',snapshot_version:1,snapshot:structuredClone(proposal)});
function current(proposal:Proposal):CurrentTask {return {kind:'selection',status:'current',last_result:'updated',result_turn_id:'selection',result_message_id:'row',requirements:proposal.requirements,match:null,pricing:proposal.pricing,pending:[],questions:[]};}
function business(proposal:Proposal):Business {return {turn_id:'selection',intent:'selection',requirements:proposal.requirements,match:null,pricing:proposal.pricing,unit_price:null,current_task:current(proposal),catalog:null,catalog_context:null,retrieval_evidence:[],knowledge_answers:[],errors:[],changes:[],questions:[],pending:[],proposal,presentation:{answer_markdown:'方案已准备，尚未保存。',show_comparison:false,show_current_result:true,used_source_ids:[]}};}
function harness(values = new Map<string,string>()) {
  const storage = {getItem:(key:string)=>values.get(key) ?? null,setItem:(key:string,value:string)=>{values.set(key,value);},removeItem:(key:string)=>{values.delete(key);}};
  const calls:{url:string;options?:RequestInit;resolve:(value:Response)=>void;reject:(error:Error)=>void;referencesAtSend:string|null}[] = [];
  const fetcher = ((url:RequestInfo|URL,options?:RequestInit)=>{
    const address = String(url);
    if (address === '/data-management') return Promise.resolve(response(testDataManagement));
    if (address === '/quotes') return Promise.resolve(response({quotes:[]}));
    if (address === '/health') return Promise.resolve(response({status:'ok'}));
    if (address === '/sessions') return Promise.resolve(response({sessions:[]}));
    if (address === '/proposal/view/invalidate') return Promise.resolve(response({invalidated:true}));
    if (address.startsWith('/sessions/') && options?.method !== 'POST') return Promise.resolve(response({messages:[]}));
    return new Promise<Response>((resolve,reject)=>calls.push({url:address,options,resolve,reject,referencesAtSend:storage.getItem(SAVE_REFERENCES_KEY)}));
  }) as typeof fetch;
  const controller = new SessionController(fetcher,storage);
  return {controller,storage,values,calls,last:(url:string)=>calls.filter(call=>call.url === url).at(-1)!};
}
async function prepare(app:ReturnType<typeof harness>) {
  const sending = app.controller.send('准备具体方案');
  const viewId = JSON.parse(app.last('/chat').options!.body as string).view_id;
  const proposal = snapshot(viewId);
  app.last('/chat').resolve(response({session_id:'same',message:'方案已准备，尚未保存。',message_id:'row',business:business(proposal),tool_results:[]}));
  await sending;
  return proposal;
}
const flush = async () => { for(let n=0;n<40;n++) await Promise.resolve(); };
function fiftySeats():Proposal {
  const proposal = snapshot();
  proposal.proposal_id = 'proposal-50';proposal.turn_id = 'selection-50';proposal.action_ref = 'NEW_ACTION_01234567890123456789012345678901';
  proposal.requirements.revision = 2;proposal.requirements.parameters[0] = {name:'paid_seats',value:50,user_quote:'明确改为50席'};
  proposal.pricing.paid_seats = 50;proposal.pricing.base_price_minor = 7200000;proposal.pricing.formula = '120.00 × 50 × 12 = 72,000.00';
  return proposal;
}
function restored(proposals:Proposal[]) {
  return harness(new Map([[SAVE_REFERENCES_KEY,JSON.stringify(proposals.map(proposal=>({session_id:proposal.session_id,proposal_id:proposal.proposal_id,action_ref:proposal.action_ref,started_at:'2026-10-05T01:00:00Z'})))]]));
}
function savedResult(proposal:Proposal,index = 0) {
  const saved = quote(proposal);
  if(index) saved.quote_id = `Q-${index.toString(16).toUpperCase().padStart(32,'0')}`;
  return {status:'saved',code:'saved',message:'草案已保存',quote:saved};
}
function LiveRecords({app,onOpenQuote}: {app:ReturnType<typeof harness>;onOpenQuote?:(id:string)=>void}) {
  const state = useSyncExternalStore(app.controller.subscribe,app.controller.getSnapshot);
  return <QuotePanels mode="records" state={state} controller={app.controller} onClose={()=>{}} onOpenQuote={onOpenQuote}/>;
}
function LiveQuotePanel({app,mode}: {app:ReturnType<typeof harness>;mode:'lookup'|'records'}) {
  const state = useSyncExternalStore(app.controller.subscribe,app.controller.getSnapshot);
  return <QuotePanels mode={mode} state={state} controller={app.controller} onClose={()=>{}}/>;
}
async function savedRecordsView(clipboard:ReturnType<typeof vi.fn>) {
  const first=snapshot(),second=fiftySeats(),app=restored([first,second]);
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:clipboard}});
  const view=render(<LiveRecords app={app}/>);
  await act(async()=>{
    for(const call of app.calls) {
      const action=JSON.parse(call.options!.body as string).action_ref;
      call.resolve(response(savedResult(action === actionRef ? first : second,action === actionRef ? 0 : 1)));
    }
    await flush();
  });
  return {app,view};
}

describe('actual click, receipt and restoration boundaries',()=>{
  it('persists only original references before submitting and blocks duplicate clicks without extra model requests',async()=>{
    const app = harness(),proposal = await prepare(app),task = app.controller.getSnapshot().currentTask;
    const saving = app.controller.saveProposal('proposal');
    await app.controller.saveProposal('proposal');
    const submits = app.calls.filter(call=>call.url === '/sessions/same/quotes');
    expect(submits).toHaveLength(1);
    expect(JSON.parse(submits[0].options!.body as string)).toEqual({action_ref:actionRef});
    const refs = JSON.parse(submits[0].referencesAtSend!);
    expect(Object.keys(refs[0]).sort()).toEqual(['action_ref','proposal_id','session_id','started_at']);
    expect(submits[0].referencesAtSend).not.toContain('11520000');
    submits[0].resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(proposal)}));await saving;
    expect(app.controller.getSnapshot().saveActions[0].quote?.quote_id).toBe(quoteId);
    expect(app.controller.getSnapshot().currentProposal).toBeNull();
    expect(app.controller.getSnapshot().currentTask).toEqual(task);
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(1);
    expect(app.controller.getSnapshot().messages.map(message=>message.content).join()).not.toContain(actionRef);
  });
  it('never resolves a displayed but unclicked Proposal',async()=>{
    const app=harness();await prepare(app);
    await app.controller.resolveSaveAction('proposal','same');
    expect(app.calls.some(call=>call.url.endsWith('quote-actions/resolve'))).toBe(false);
    expect(app.controller.getSnapshot().currentProposal?.proposal_id).toBe('proposal');
    expect(app.storage.getItem(SAVE_REFERENCES_KEY)).toBeNull();
  });
  it('storage failure prevents submission instead of losing the recovery reference',async()=>{
    const app=harness();await prepare(app);
    app.storage.setItem=()=>{throw new Error('storage disabled');};
    await app.controller.saveProposal('proposal');
    expect(app.calls.some(call=>call.url.endsWith('/quotes'))).toBe(false);
    expect(app.controller.getSnapshot().notice).toContain('没有提交');
    expect(app.controller.getSnapshot().currentProposal).not.toBeNull();
  });
  it.each(['network','parse','damaged','wrong_action'])('uncertain %s receipt retains original action and never resubmits',async kind=>{
    const app=harness(),proposal=await prepare(app),saving=app.controller.saveProposal('proposal');
    const request=app.last('/sessions/same/quotes');
    if (kind === 'network') request.reject(new Error('lost after actual submit'));
    else if (kind === 'parse') request.resolve(new Response('broken',{status:200}));
    else {
      const saved=quote(proposal);
      if (kind === 'damaged') saved.snapshot.plans=[null] as unknown as Proposal['plans'];
      else saved.snapshot.action_ref='OTHER_ORIGINAL_ACTION_01234567890123456789';
      request.resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:saved}));
    }
    await saving;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    expect(app.controller.getSnapshot().saveActions[0].quote).toBeNull();
    await app.controller.saveProposal('proposal');
    expect(app.calls.filter(call=>call.url === '/sessions/same/quotes')).toHaveLength(1);
    const checking=app.controller.resolveSaveAction('proposal','same');
    expect(JSON.parse(app.last('/sessions/same/quote-actions/resolve').options!.body as string)).toEqual({action_ref:actionRef});
    app.last('/sessions/same/quote-actions/resolve').resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(proposal)}));await checking;
    expect(app.controller.getSnapshot().saveActions[0].quote?.quote_id).toBe(quoteId);
  });
  it('accepts legitimate HTTP 503 unknown and keeps read failure unknown until a successful original-action check',async()=>{
    const app=harness(),proposal=await prepare(app),saving=app.controller.saveProposal('proposal');
    app.last('/sessions/same/quotes').resolve(response({status:'unknown',code:'action_in_flight',message:'保存结果待核对',quote:null},503));await saving;
    expect(app.controller.getSnapshot().saveActions[0].code).toBe('action_in_flight');
    const checking=app.controller.resolveSaveAction('proposal','same');
    app.last('/sessions/same/quote-actions/resolve').resolve(response({status:'unknown',code:'read_failed',message:'读取暂不可用',quote:null},503));await checking;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    const retry=app.controller.resolveSaveAction('proposal','same');
    app.last('/sessions/same/quote-actions/resolve').resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(proposal)}));await retry;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('saved');
  });
  it('refresh restores only initiated references, never creates old permission or automatically closes an unsubmitted action',async()=>{
    const app=harness(),proposal=await prepare(app),saving=app.controller.saveProposal('proposal');
    app.last('/sessions/same/quotes').reject(new Error('lost'));await saving;
    const refreshed=harness(app.values);await refreshed.controller.initialize();
    expect(refreshed.controller.getSnapshot().currentProposal).toBeNull();
    expect(refreshed.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    expect(refreshed.calls).toEqual([]);
    const checking=refreshed.controller.resolveSaveAction('proposal','same');
    refreshed.last('/sessions/same/quote-actions/resolve').resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(proposal)}));await checking;
    expect(refreshed.controller.getSnapshot().saveActions[0].quote?.quote_id).toBe(quoteId);
    expect(refreshed.calls.some(call=>call.url.endsWith('/quotes'))).toBe(false);
  });
  it('definitive not_saved explains cause and consumes old operation without changing selection',async()=>{
    const app=harness();await prepare(app);const saving=app.controller.saveProposal('proposal');
    app.last('/sessions/same/quotes').resolve(response({status:'not_saved',code:'proposal_expired',message:'当前方案许可已失效，请重新准备',quote:null}));await saving;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('not_saved');
    expect(app.controller.getSnapshot().saveActions[0].message).toContain('重新准备');
    expect(app.controller.getSnapshot().currentTask?.pricing?.base_price_minor).toBe(11520000);
    await app.controller.resolveSaveAction('proposal','same');
    expect(app.calls.some(call=>call.url.endsWith('resolve'))).toBe(false);
  });
  it.each(['saved','unknown'])('late %s save result updates only original operation after another conversation becomes current',async status=>{
    const app=harness(),proposal=await prepare(app),saving=app.controller.saveProposal('proposal');
    await app.controller.newChat();
    const sending=app.controller.send('新会话问题');
    app.last('/chat').resolve(response({session_id:'other',message:'另一个会话回答',business:null}));await sending;
    const before=app.controller.getSnapshot();
    app.last('/sessions/same/quotes').resolve(response({status,code:status,message:status === 'saved' ? '草案已保存' : '保存结果待核对',quote:status === 'saved' ? quote(proposal) : null},status === 'saved' ? 200 : 503));await saving;
    const after=app.controller.getSnapshot();
    expect(after.sessionId).toBe('other');expect(after.messages).toEqual(before.messages);
    expect(after.currentTask).toEqual(before.currentTask);expect(after.currentProposal).toBeNull();
    expect(after.notice).toBe(before.notice);expect(after.saveActions[0].session_id).toBe('same');
  });
  it('sending a new message invalidates old submit, and natural-language confirmation never calls quote endpoints',async()=>{
    const app=harness();await prepare(app);const sending=app.controller.send('好的，保存这个');
    await app.controller.saveProposal('proposal');
    expect(app.calls.some(call=>call.url.includes('/quotes'))).toBe(false);
    app.last('/chat').resolve(response({session_id:'same',message:'请点击新方案保存按钮',business:null}));await sending;
  });
  it('same-session chat waits during submit and becomes usable after an uncertain receipt',async()=>{
    const app=harness();await prepare(app);const saving=app.controller.saveProposal('proposal');
    await app.controller.send('保存与消息交错');
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(1);
    expect(app.controller.getSnapshot().messages.some(message=>message.content === '保存与消息交错')).toBe(false);
    app.last('/sessions/same/quotes').reject(new Error('response unavailable'));await saving;
    const chatting=app.controller.send('插问单价');
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(2);
    app.last('/chat').resolve(response({session_id:'same',message:'单价回答',business:null}));await chatting;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
  });
  it('accepted 50-seat change rejects the old card and needs a new real click while saved 80-seat Quote remains unchanged',async()=>{
    const app=harness(),original=await prepare(app),first=app.controller.saveProposal('proposal');
    app.last('/sessions/same/quotes').resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(original)}));await first;
    const changing=app.controller.send('明确改为50席，其他不变');
    const modified=structuredClone(original);modified.proposal_id='proposal-50';modified.turn_id='selection-50';modified.action_ref='NEW_ACTION_01234567890123456789012345678901';
    modified.requirements.revision=2;modified.requirements.parameters[0]={name:'paid_seats',value:50,user_quote:'明确改为50席'};
    modified.pricing.paid_seats=50;modified.pricing.base_price_minor=7200000;modified.pricing.formula='120.00 × 50 × 12 = 72,000.00';
    const data=business(modified);data.turn_id='selection-50';data.current_task!.result_turn_id='selection-50';
    app.last('/chat').resolve(response({session_id:'same',message:'50席方案72,000.00',business:data}));await changing;
    await app.controller.saveProposal('proposal');
    expect(app.calls.filter(call=>call.url === '/sessions/same/quotes')).toHaveLength(1);
    expect(app.controller.getSnapshot().currentProposal?.pricing.base_price_minor).toBe(7200000);
    const second=app.controller.saveProposal('proposal-50');
    expect(JSON.parse(app.last('/sessions/same/quotes').options!.body as string)).toEqual({action_ref:modified.action_ref});
    const saved=quote(modified);saved.quote_id='Q-FEDCBA9876543210FEDCBA9876543210';
    app.last('/sessions/same/quotes').resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:saved}));await second;
    expect(app.controller.getSnapshot().saveActions.map(action=>action.quote?.snapshot.pricing.base_price_minor)).toEqual([11520000,7200000]);
  });
});

describe('finite automatic recovery of initiated save records',()=>{
  it('restores existing references and reads both saved snapshots without cached success, creation or model calls',async()=>{
    const first=snapshot(),second=fiftySeats(),app=restored([first,second]);
    expect(app.controller.getSnapshot().saveActions.every(action=>action.status === 'unknown' && action.quote === null)).toBe(true);
    expect(app.calls).toHaveLength(0);
    const reading=app.controller.refreshSaveRecords();
    expect(app.controller.refreshSaveRecords()).toBe(reading);
    expect(app.controller.getSnapshot().saveRecordsLoading).toBe(true);
    expect(app.calls).toHaveLength(2);
    for(const call of app.calls) {
      const action=JSON.parse(call.options!.body as string).action_ref;
      expect(call.url).toBe('/sessions/same/quote-actions/resolve');
      call.resolve(response(savedResult(action === actionRef ? first : second,action === actionRef ? 0 : 1)));
    }
    await reading;
    const state=app.controller.getSnapshot();
    expect(state.saveRecordsLoading).toBe(false);
    expect(state.saveActions.map(action=>action.quote?.snapshot.pricing.base_price_minor)).toEqual([11520000,7200000]);
    expect(state.saveActions.map(action=>action.quote?.snapshot.pricing.paid_seats)).toEqual([80,50]);
    expect(state.currentProposal).toBeNull();expect(state.messages).toEqual([]);
    expect(JSON.parse(app.storage.getItem(SAVE_REFERENCES_KEY)!)[0]).not.toHaveProperty('quote');
  });
  it('checks each initiated action once, with two automatic requests at most and no repeat from renders or retry clicks',async()=>{
    const proposals=Array.from({length:5},(_,n)=>({...snapshot(),proposal_id:`proposal-${n}`,action_ref:`REFERENCE_${n}_012345678901234567890123456789`}));
    const app=restored(proposals),reading=app.controller.refreshSaveRecords();
    expect(app.calls).toHaveLength(2);
    await app.controller.resolveSaveAction(proposals[4].proposal_id,'same');
    expect(app.calls).toHaveLength(2);
    expect(app.controller.refreshSaveRecords()).toBe(reading);
    for(let n=0;n<5;n++) {
      const call=app.calls[n];
      const proposal=proposals.find(value=>value.action_ref === JSON.parse(call.options!.body as string).action_ref)!;
      call.resolve(response(savedResult(proposal,n+1)));await flush();
      expect(app.calls.length).toBeLessThanOrEqual(Math.min(n+3,5));
    }
    await reading;
    expect(app.calls).toHaveLength(5);
    expect(new Set(app.calls.map(call=>JSON.parse(call.options!.body as string).action_ref)).size).toBe(5);
    expect(app.controller.getSnapshot().saveActions.every(action=>action.status === 'saved')).toBe(true);
  });
  it('skips a live submission and an unclicked Proposal, and automatic reads never block chat or a separate save',async()=>{
    const old={...snapshot(),proposal_id:'old-proposal',action_ref:'OLD_ACTION_01234567890123456789012345678901'},app=restored([old]);
    const proposal=await prepare(app),reading=app.controller.refreshSaveRecords();
    expect(app.controller.getSnapshot().busy).toBe(false);
    expect(app.controller.getSnapshot().currentProposal).toEqual(proposal);
    const saving=app.controller.saveProposal('proposal');
    expect(app.calls.filter(call=>call.url.endsWith('/quotes'))).toHaveLength(1);
    expect(app.calls.filter(call=>call.url.endsWith('/resolve'))).toHaveLength(1);
    await app.controller.resolveSaveAction('proposal','same');
    app.last('/sessions/same/quotes').resolve(response(savedResult(proposal)));await saving;
    expect(app.controller.getSnapshot().busy).toBe(false);
    const chatting=app.controller.send('读取记录期间正常插问');
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(2);
    app.last('/chat').resolve(response({session_id:'same',message:'正常回答',business:null}));await chatting;
    app.last('/sessions/same/quote-actions/resolve').resolve(response(savedResult(old,2)));await reading;
    expect(app.controller.getSnapshot().messages.at(-1)?.content).toBe('正常回答');
    expect(app.controller.getSnapshot().saveActions.map(action=>action.status)).toEqual(['saved','saved']);
  });
  it('opening records during a live submission cannot resolve or replace its result',async()=>{
    const app=harness(),proposal=await prepare(app),saving=app.controller.saveProposal('proposal');
    await app.controller.refreshSaveRecords();
    expect(app.calls.filter(call=>call.url.endsWith('/resolve'))).toHaveLength(0);
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('submitting');
    app.last('/sessions/same/quotes').resolve(response(savedResult(proposal)));await saving;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('saved');
  });
  it.each(['transport','read_failure','in_flight'])('automatic %s stays unknown and a manual retry reads the same action only',async kind=>{
    const proposal=snapshot(),app=restored([proposal]),reading=app.controller.refreshSaveRecords();
    const call=app.last('/sessions/same/quote-actions/resolve');
    if(kind === 'transport') call.reject(new Error('network unavailable'));
    else call.resolve(response({status:'unknown',code:kind,message:kind === 'in_flight' ? '仍在提交，保存结果待核对' : '数据库暂时无法读取，保存结果待核对',quote:null},503));
    await reading;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    expect(app.controller.getSnapshot().saveActions[0].message).toContain('待核对');
    const retry=app.controller.resolveSaveAction('proposal','same');
    app.last('/sessions/same/quote-actions/resolve').resolve(response(savedResult(proposal)));await retry;
    expect(app.controller.getSnapshot().saveActions[0].quote?.quote_id).toBe(quoteId);
    expect(app.calls.map(value=>value.url)).toEqual(['/sessions/same/quote-actions/resolve','/sessions/same/quote-actions/resolve']);
    expect(app.calls.every(value=>JSON.parse(value.options!.body as string).action_ref === actionRef)).toBe(true);
  });
  it('finite read timeout aborts the request, leaves unknown, and a late result cannot declare success',async()=>{
    vi.useFakeTimers();
    try {
      const proposal=snapshot(),app=restored([proposal]),reading=app.controller.refreshSaveRecords();
      const call=app.last('/sessions/same/quote-actions/resolve');
      await vi.advanceTimersByTimeAsync(15_000);await reading;
      expect(call.options!.signal?.aborted).toBe(true);
      expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
      expect(app.controller.getSnapshot().saveActions[0].message).toContain('超时');
      call.resolve(response(savedResult(proposal)));await flush();
      expect(app.controller.getSnapshot().saveActions[0].quote).toBeNull();
    } finally { vi.useRealTimers(); }
  });
  it('a failed reread clears a prior saved result instead of declaring cached facts authoritative',async()=>{
    const proposal=snapshot(),app=restored([proposal]),first=app.controller.refreshSaveRecords();
    app.last('/sessions/same/quote-actions/resolve').resolve(response(savedResult(proposal)));await first;
    const second=app.controller.refreshSaveRecords();
    expect(app.controller.getSnapshot().saveActions[0].quote).toBeNull();
    app.last('/sessions/same/quote-actions/resolve').reject(new Error('read failure'));await second;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    expect(app.controller.getSnapshot().saveActions[0].quote).toBeNull();
  });
  it('database-confirmed not_saved is shown accurately and never creates a replacement action',async()=>{
    const app=restored([snapshot()]),reading=app.controller.refreshSaveRecords();
    app.last('/sessions/same/quote-actions/resolve').resolve(response({status:'not_saved',code:'not_saved',message:'核对完成：原动作未保存',quote:null}));await reading;
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('not_saved');
    expect(app.controller.getSnapshot().saveActions).toHaveLength(1);
    expect(app.calls).toHaveLength(1);
  });
  it('records mount reads once, displays distinguishable summaries, and copies or opens each true quote ID',async()=>{
    const first=snapshot(),second=fiftySeats(),app=restored([first,second]);
    const open=vi.fn(),clipboard=vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:clipboard}});
    const view=render(<LiveRecords app={app} onOpenQuote={open}/>);
    expect(view.getByText('正在读取保存记录…')).toBeTruthy();
    expect(view.getAllByText('正在核对保存结果')).toHaveLength(2);
    await act(async()=>{
      for(const call of app.calls) {
        const action=JSON.parse(call.options!.body as string).action_ref;
        call.resolve(response(savedResult(action === actionRef ? first : second,action === actionRef ? 0 : 1)));
      }
      await flush();
    });
    expect(view.queryByText('正在读取保存记录…')).toBeNull();
    const summaries=[...view.container.querySelectorAll('.saved-quote-summary')].map(node=>node.textContent);
    expect(summaries).toEqual(['商务版 Business · 50 席 · 12 个月 · 订阅基价 72,000.00 元','商务版 Business · 80 席 · 12 个月 · 订阅基价 115,200.00 元']);
    expect([...view.container.querySelectorAll('.save-event > p')].filter(node=>node.textContent?.startsWith('保存时间：'))).toHaveLength(2);
    await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
    expect(clipboard).toHaveBeenCalledWith(savedResult(second,1).quote.quote_id);
    expect(view.getByText('✓ 草案编号已复制')).toBeTruthy();
    fireEvent.click(view.getAllByRole('button',{name:'查看草案'})[0]);
    expect(open).toHaveBeenCalledWith(savedResult(second,1).quote.quote_id);
    view.rerender(<LiveRecords app={app} onOpenQuote={open}/>);
    expect(app.calls).toHaveLength(2);
    expect(view.container.innerHTML).not.toContain(actionRef);
  });
  it('manual lookup opens empty, preserves typed input across read state, and clears only when reopened',async()=>{
    const app=harness(),view=render(<LiveQuotePanel mode="lookup" app={app}/>);
    const input=view.getByLabelText('草案编号') as HTMLInputElement;
    expect(input.value).toBe('');expect(app.calls).toHaveLength(0);
    fireEvent.change(input,{target:{value:quoteId}});
    fireEvent.click(view.getByRole('button',{name:'查询'}));
    expect(input.value).toBe(quoteId);
    expect(view.getByRole('button',{name:'读取中…'})).toBeTruthy();
    await act(async()=>{app.last(`/quotes/${quoteId}`).resolve(response(quote(snapshot())));await flush();});
    expect(input.value).toBe(quoteId);
    expect(app.controller.getSnapshot().quoteLookup.quote?.quote_id).toBe(quoteId);
    view.rerender(<LiveQuotePanel mode="lookup" app={app}/>);
    expect(input.value).toBe(quoteId);
    view.rerender(<LiveQuotePanel mode="records" app={app}/>);
    view.rerender(<LiveQuotePanel mode="lookup" app={app}/>);
    expect((view.getByLabelText('草案编号') as HTMLInputElement).value).toBe('');
    expect(app.calls).toHaveLength(1);
  });
  it('typing clears prior facts and invalidates a late response without changing the manual input or querying',async()=>{
    const app=harness(),view=render(<LiveQuotePanel mode="lookup" app={app}/>);
    const input=view.getByLabelText('草案编号') as HTMLInputElement;
    fireEvent.change(input,{target:{value:quoteId}});fireEvent.click(view.getByRole('button',{name:'查询'}));
    fireEvent.change(input,{target:{value:'Q-manual-edit'}});
    expect(app.controller.getSnapshot().quoteLookup).toEqual({loading:false,quote:null,message:''});
    expect(input.value).toBe('Q-manual-edit');
    await act(async()=>{app.last(`/quotes/${quoteId}`).resolve(response(quote(snapshot())));await flush();});
    expect(input.value).toBe('Q-manual-edit');
    expect(app.controller.getSnapshot().quoteLookup.quote).toBeNull();
    expect(view.container.querySelector('[data-section="saved-quote"]')).toBeNull();
    expect(app.calls).toHaveLength(1);
  });
  it('a read opened from records never fills the manual input and editing clears the displayed result',async()=>{
    const app=harness();
    const reading=app.controller.lookupQuote(quoteId);app.last(`/quotes/${quoteId}`).resolve(response(quote(snapshot())));await reading;
    const view=render(<LiveQuotePanel mode="lookup" app={app}/>);
    const input=view.getByLabelText('草案编号') as HTMLInputElement;
    expect(input.value).toBe('');expect(view.container.querySelector('[data-section="saved-quote"]')).not.toBeNull();
    fireEvent.change(input,{target:{value:'Q-user-input'}});
    expect(input.value).toBe('Q-user-input');expect(view.container.querySelector('[data-section="saved-quote"]')).toBeNull();
    expect(app.calls).toHaveLength(1);
  });
});

describe('copy-only feedback with a finite visible toast',()=>{
  it('shows success for 2.5 seconds and copying never queries, clears facts, or opens another area',async()=>{
    const clipboard=vi.fn().mockResolvedValue(undefined),{app,view}=await savedRecordsView(clipboard);
    const reading=app.controller.lookupQuote(quoteId);app.last(`/quotes/${quoteId}`).resolve(response(quote(snapshot())));await act(async()=>{await reading;});
    const before=app.controller.getSnapshot().quoteLookup,requests=app.calls.length;
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
      expect(clipboard).toHaveBeenCalledExactlyOnceWith(savedResult(fiftySeats(),1).quote.quote_id);
      expect(view.getByText('✓ 草案编号已复制').className).toContain('quote-copy-notice success');
      expect(app.calls).toHaveLength(requests);expect(app.controller.getSnapshot().quoteLookup).toEqual(before);
      await act(async()=>{await vi.advanceTimersByTimeAsync(2_499);});
      expect(view.getByText('✓ 草案编号已复制')).toBeTruthy();
      await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
      expect(view.queryByText('✓ 草案编号已复制')).toBeNull();
    } finally { vi.useRealTimers(); }
  });
  it('clipboard failure shows an explicit failure toast for five seconds without reading or altering a quote',async()=>{
    const clipboard=vi.fn().mockRejectedValue(new Error('clipboard denied')),{app,view}=await savedRecordsView(clipboard);
    const requests=app.calls.length;
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[1]);});
      expect(clipboard).toHaveBeenCalledExactlyOnceWith(quoteId);
      expect(view.getByRole('alert').textContent).toBe('复制失败，请手动复制完整编号。');
      expect(view.getByRole('alert').className).toContain('quote-copy-notice error');
      expect(view.queryByText('✓ 草案编号已复制')).toBeNull();expect(app.calls).toHaveLength(requests);
      await act(async()=>{await vi.advanceTimersByTimeAsync(4_999);});expect(view.getByRole('alert')).toBeTruthy();
      await act(async()=>{await vi.advanceTimersByTimeAsync(1);});expect(view.queryByRole('alert')).toBeNull();
    } finally { vi.useRealTimers(); }
  });
  it('a second copy restarts the timer and keeps the exact ID of each selected row',async()=>{
    const clipboard=vi.fn().mockResolvedValue(undefined),{view}=await savedRecordsView(clipboard);
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
      await act(async()=>{await vi.advanceTimersByTimeAsync(2_000);});
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[1]);});
      expect(clipboard.mock.calls.map(call=>call[0])).toEqual([savedResult(fiftySeats(),1).quote.quote_id,quoteId]);
      expect(vi.getTimerCount()).toBe(1);
      await act(async()=>{await vi.advanceTimersByTimeAsync(2_499);});expect(view.getByText('✓ 草案编号已复制')).toBeTruthy();
      await act(async()=>{await vi.advanceTimersByTimeAsync(1);});expect(view.queryByText('✓ 草案编号已复制')).toBeNull();
    } finally { vi.useRealTimers(); }
  });
  it.each(['success','failure'])('an older pending copy %s cannot overwrite a newer copy or extend its timer',async oldResult=>{
    let settle:(value?:unknown)=>void=()=>{};
    const clipboard=vi.fn().mockImplementationOnce(()=>new Promise((resolve,reject)=>{settle=oldResult === 'success' ? resolve : reject;})).mockResolvedValueOnce(undefined);
    const {view}=await savedRecordsView(clipboard);
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[1]);});
      await act(async()=>{await vi.advanceTimersByTimeAsync(2_000);settle(oldResult === 'failure' ? new Error('old failure') : undefined);await flush();});
      expect(view.getByText('✓ 草案编号已复制')).toBeTruthy();expect(view.queryByRole('alert')).toBeNull();
      expect(vi.getTimerCount()).toBe(1);
      await act(async()=>{await vi.advanceTimersByTimeAsync(500);});expect(view.queryByText('✓ 草案编号已复制')).toBeNull();
      expect(clipboard.mock.calls.map(call=>call[0])).toEqual([savedResult(fiftySeats(),1).quote.quote_id,quoteId]);
    } finally { vi.useRealTimers(); }
  });
  it('unmount cleans the timer of a visible toast',async()=>{
    const {view}=await savedRecordsView(vi.fn().mockResolvedValue(undefined));
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
      expect(vi.getTimerCount()).toBe(1);
      view.unmount();
      expect(vi.getTimerCount()).toBe(0);expect(document.querySelector('.quote-copy-notice')).toBeNull();
    } finally { vi.useRealTimers(); }
  });
  it('a pending copy cannot publish a toast after unmount',async()=>{
    let complete:()=>void=()=>{};
    const clipboard=vi.fn().mockImplementation(()=>new Promise<void>(resolve=>{complete=resolve;}));
    const {view}=await savedRecordsView(clipboard);
    vi.useFakeTimers();
    try {
      await act(async()=>{fireEvent.click(view.getAllByRole('button',{name:'复制编号'})[0]);});
      view.unmount();
      await act(async()=>{complete();await flush();});
      expect(vi.getTimerCount()).toBe(0);expect(document.querySelector('.quote-copy-notice')).toBeNull();
    } finally { vi.useRealTimers(); }
  });
});

describe('direct read and saved-snapshot presentation',()=>{
  it('restored quotes bypass a cached 410 and a later deletion bypasses an old readable snapshot',async()=>{
    const original=quote(snapshot()),calls:{url:string;options?:RequestInit}[]=[];
    let deleted=true,cached:Response|undefined;
    const fetcher=(async(url:RequestInfo|URL,options?:RequestInit)=>{
      const address=String(url);
      expect(address).toBe(`/quotes/${quoteId}`);
      if (options?.cache !== 'no-store' && cached) return cached.clone();
      calls.push({url:address,options});
      const actual=deleted ? response({detail:{code:'quote_in_trash',message:'当时保存成功；目前在最近删除。'}},410) : response(original);
      cached=actual.clone();return actual;
    }) as typeof fetch;
    const storage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
    const controller=new SessionController(fetcher,storage);
    await controller.lookupQuote(quoteId);
    expect(controller.getSnapshot().quoteLookup.quote).toBeNull();
    expect(controller.getSnapshot().quoteLookup.message).toContain('最近删除');
    deleted=false;
    await controller.lookupQuote(quoteId);
    expect(controller.getSnapshot().quoteLookup.quote).toEqual(original);
    expect(controller.getSnapshot().quoteLookup.message).toBe('');
    deleted=true;
    await controller.lookupQuote(quoteId);
    expect(controller.getSnapshot().quoteLookup.quote).toBeNull();
    expect(controller.getSnapshot().quoteLookup.message).toContain('最近删除');
    expect(calls).toHaveLength(3);
    expect(calls.every(call=>call.options?.cache === 'no-store')).toBe(true);
    expect(controller.getSnapshot().currentProposal).toBeNull();
    expect(controller.getSnapshot().currentTask).toBeNull();
    expect(controller.getSnapshot().saveActions).toEqual([]);
  });
  it('direct lookup works with model unavailable and never replaces current selection or Proposal',async()=>{
    const app=harness(),proposal=await prepare(app),before=app.controller.getSnapshot();
    const lookup=app.controller.lookupQuote(quoteId);
    app.last(`/quotes/${quoteId}`).resolve(response(quote(proposal)));await lookup;
    expect(app.controller.getSnapshot().quoteLookup.quote?.quote_id).toBe(quoteId);
    expect(app.controller.getSnapshot().currentTask).toEqual(before.currentTask);
    expect(app.controller.getSnapshot().currentProposal).toEqual(before.currentProposal);
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(1);
    expect(app.calls.some(call=>call.url.endsWith('resolve'))).toBe(false);
  });
  it('invalid and absent IDs cannot create records and clear stale lookup facts',async()=>{
    const app=harness();await app.controller.lookupQuote('bad');
    expect(app.calls).toHaveLength(0);expect(app.controller.getSnapshot().quoteLookup.message).toContain('格式不正确');
    const lookup=app.controller.lookupQuote(quoteId);app.last(`/quotes/${quoteId}`).resolve(response({detail:{code:'quote_not_found',message:'未找到该草案'}},404));await lookup;
    expect(app.controller.getSnapshot().quoteLookup.quote).toBeNull();expect(app.controller.getSnapshot().quoteLookup.message).toContain('未找到');
    expect(app.calls).toHaveLength(1);
  });
  it('late lookup cannot appear after a conversation switch',async()=>{
    const app=harness(),proposal=await prepare(app),lookup=app.controller.lookupQuote(quoteId);
    await app.controller.newChat();app.last(`/quotes/${quoteId}`).resolve(response(quote(proposal)));await lookup;
    expect(app.controller.getSnapshot().quoteLookup).toEqual({loading:false,quote:null,message:''});
  });
  it('read fault explains temporary inability to read without claiming missing or losing current selection',async()=>{
    const app=harness();await prepare(app);const lookup=app.controller.lookupQuote(quoteId);
    app.last(`/quotes/${quoteId}`).resolve(response({detail:{code:'quote_read_failed',message:'草案暂时无法读取，请稍后重试'}},503));await lookup;
    expect(app.controller.getSnapshot().quoteLookup.message).toContain('暂时无法读取');
    expect(app.controller.getSnapshot().quoteLookup.message).not.toContain('未保存');
    expect(app.controller.getSnapshot().currentProposal?.proposal_id).toBe('proposal');
  });
  it('legacy snapshot notice and save_available=false never override the saved outer receipt or render a button',()=>{
    const proposal=snapshot();proposal.save_available=false;proposal.notice='尚未保存，功能尚未开放';
    const saved=quote(proposal),view=render(<ProposalSummary proposal={saved.snapshot} currentResult={false} currentProposal={false} savedQuote={saved}/>);
    expect(view.container.textContent).toContain('草案已保存');expect(view.container.textContent).toContain(quoteId);expect(view.container.textContent).toContain('115,200.00');
    expect(view.container.textContent).not.toContain('尚未保存');expect(view.container.textContent).not.toContain('尚未开放');
    expect(view.container.querySelector('.proposal-approval')).toBeNull();expect(view.container.textContent).not.toContain(actionRef);
    expect(view.container.querySelector('[data-current="false"]')).not.toBeNull();
  });
  it('new live summary enables only its real click and later receipt changes the card to historical Quote facts',async()=>{
    const app=harness(),proposal=await prepare(app),state=app.controller.getSnapshot();
    const chat:ChatResponse={session_id:'same',message:'方案已准备，尚未保存',status:'completed',business:business(proposal),tool_results:[]};
    const view=render(<BusinessResponseView response={chat} currentTask={state.currentTask} currentProposal={proposal} onSave={id=>void app.controller.saveProposal(id)}/>);
    const button=view.getByRole('button',{name:'确认并保存草案'}) as HTMLButtonElement;expect(button.disabled).toBe(false);
    await act(async()=>fireEvent.click(button));
    const savingRequest=app.last('/sessions/same/quotes');
    await act(async()=>{savingRequest.resolve(response({status:'saved',code:'saved',message:'草案已保存',quote:quote(proposal)}));for(let i=0;i<10;i++)await Promise.resolve();});
    view.rerender(<BusinessResponseView response={chat} currentTask={state.currentTask} saveActions={app.controller.getSnapshot().saveActions}/>);
    expect(view.container.querySelector('[data-section="saved-quote"]')?.getAttribute('data-current')).toBe('false');
    expect(view.container.querySelector('.proposal-approval')).toBeNull();
  });
  it('model read-only facts display the same snapshot without operational identity or current permission',()=>{
    const proposal=snapshot();
    const {proposal_id:_p,session_id:_s,turn_id:_t,view_id:_v,interaction_revision:_i,action_ref:_a,status:_st,save_available:_sa,notice:_n,...facts}=proposal;
    const queried:QuoteFacts={quote_id:quoteId,saved_at:'2026-10-05T01:02:03Z',snapshot_version:1,snapshot:facts};
    const data=business(proposal);data.proposal=null;data.queried_quotes=[queried];data.presentation!.show_current_result=false;
    const view=render(<BusinessResponseView response={{session_id:'same',message:'查回历史草案',status:'completed',business:data,tool_results:[]}} currentTask={current(proposal)}/>);
    const saved=view.container.querySelector('[data-section="saved-quote"]')!;
    expect(saved.textContent).toContain('115,200.00');expect(saved.textContent).not.toContain(actionRef);expect(saved.textContent).not.toContain('尚未保存');
    expect(saved.getAttribute('data-current')).toBe('false');expect(saved.querySelector('.proposal-approval')).toBeNull();
  });
  it('restored pending action has a normal check button without credentials in text or DOM',async()=>{
    const app=harness();await prepare(app);const saving=app.controller.saveProposal('proposal');app.last('/sessions/same/quotes').reject(new Error('lost'));await saving;
    const view=render(<LiveRecords app={app}/>);
    await act(async()=>{app.last('/sessions/same/quote-actions/resolve').reject(new Error('read unavailable'));await flush();});
    expect(view.getByRole('button',{name:'核对保存结果'})).toBeTruthy();expect(view.container.innerHTML).not.toContain(actionRef);
    expect(view.container.querySelector('[data-business-event="quote-save"]')).not.toBeNull();
    expect(view.container.querySelector('[data-tool-call-id]')).toBeNull();
  });
  it.each(['plans','conditions','items'])('damaged nested %s snapshot is rejected before rendering',field=>{
    const saved=quote(snapshot());
    if(field === 'items') saved.snapshot.requirements.items=[null] as unknown as Proposal['requirements']['items'];
    else if (field === 'plans') saved.snapshot.plans=[null] as unknown as Proposal['plans'];
    else saved.snapshot.conditions=[null] as unknown as Proposal['conditions'];
    expect(isQuote(saved)).toBe(false);
  });
});
