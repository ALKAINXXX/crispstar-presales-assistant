// Production controller and panels with a controlled HTTP transport. SQLite,
// restart and real endpoint effects are independently covered by pytest.
import { act, fireEvent, render } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { SessionController } from './adapter';
import { DataManagementPanel, ManagementConfirm, type ManagementConfirmation } from './DataManagement';
import { QuotePanels } from './QuotePanels';
import { SAVE_REFERENCES_KEY } from './quotes';
import { testDataManagement } from './testData';
import type { DataManagement, ManagementAction, Proposal, Quote, QuoteSummary, Session, TrashItem } from './types';

const qid='Q-0123456789ABCDEF0123456789ABCDEF',actionRef='SAVE_ACTION_012345678901234567890123456789';
const reply=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const flush=async()=>{for(let n=0;n<60;n++) await Promise.resolve();};
function proposal(view_id='view'):Proposal {
  return {proposal_id:'proposal',session_id:'chat-a',view_id,turn_id:'turn',interaction_revision:1,created_at:'2026-10-05T01:00:00Z',catalog_version:'v1',requirements:{requirement_id:'r',revision:1,items:[],parameters:[],condition_assertions:[],selected_plan_id:null},plans:[],eligible_plan_ids:['team'],lowest_plan_id:'team',lowest_plan_name:'Team',selected_plan_id:'team',selected_plan_name:'Team',selection_reason:'lowest_satisfying',pricing:{plan_id:'team',plan_name:'Team',unit_price_minor:8000,paid_seats:10,months:12,base_price_minor:960000,formula:'80 × 10 × 12',catalog_version:'v1',currency:'CNY',billing_unit:'seat_month',minor_units_per_major:100},conditions:[],adopted_sources:[],action_ref:actionRef,status:'pending_confirmation',save_available:true,notice:'尚未保存'};
}
const quote=():Quote=>({quote_id:qid,saved_at:'2026-10-05T01:02:03Z',snapshot_version:1,snapshot:proposal(),source_conversation_status:'active'});
const summary=():QuoteSummary=>({quote_id:qid,saved_at:'2026-10-05T01:02:03Z',selected_plan_name:'Team',paid_seats:10,months:12,base_price_minor:960000,source_session_id:'chat-a',source_conversation_status:'active'});
const sessions=():Session[]=>[{id:'chat-a',title:'聊天 A',updated_at:'',version:1,last_message_id:2},{id:'chat-b',title:'聊天 B',updated_at:'',version:1,last_message_id:2}];
type Intercept=(url:string,options?:RequestInit)=>Promise<Response>|undefined;
function harness(values=new Map<string,string>()) {
  let info:DataManagement=structuredClone(testDataManagement),normalSessions=sessions(),quotes=[summary()],trash:TrashItem[]=[],intercept:Intercept=()=>undefined;
  const actions=new Map<string,ManagementAction>(),calls:{url:string;options?:RequestInit}[]=[];
  const storage={getItem:(key:string)=>values.get(key) ?? null,setItem:(key:string,value:string)=>{values.set(key,value);},removeItem:(key:string)=>{values.delete(key);}};
  let savedReply:unknown={status:'saved',code:'saved',message:'草案已保存',quote:quote()},chatReply:(body:Record<string,unknown>)=>unknown=body=>({session_id:body.session_id ?? 'chat-a',session_version:1,message:'正式回答',message_id:2,business:null});
  const fetcher=((input:RequestInfo|URL,options?:RequestInit)=>{
    const url=String(input);calls.push({url,options});const held=intercept(url,options);if (held) return held;
    if (url === '/data-management') return Promise.resolve(reply(info));
    if (url === '/data-management/trash') return Promise.resolve(reply({items:trash,...info}));
    if (url === '/sessions') return Promise.resolve(reply({sessions:normalSessions}));
    if (url === '/quotes') return Promise.resolve(reply({quotes}));
    if (url === '/health') return Promise.resolve(reply({status:'ok',chat_availability:{available:true,message:''}}));
    if (url === '/proposal/view/invalidate') return Promise.resolve(reply({invalidated:true}));
    if (url === '/chat') return Promise.resolve(reply(chatReply(JSON.parse(options!.body as string))));
    if (url.includes('/quote-actions/resolve') || url.endsWith('/quotes')) return Promise.resolve(reply(savedReply));
    if (url.startsWith('/sessions/')) return Promise.resolve(reply({session_version:normalSessions.find(value=>url.endsWith(value.id))?.version,messages:[{id:2,role:'assistant',content:'原聊天中已经出现的草案编号和摘要'}]}));
    if (url.startsWith('/quotes/')) return Promise.resolve(quotes.some(value=>value.quote_id === qid) ? reply({...quote(),source_conversation_status:quotes[0].source_conversation_status}) : reply({detail:{code:'quote_in_trash',message:'当时保存成功；目前在最近删除。'}},410));
    if (url.startsWith('/data-management/actions/')) return Promise.resolve(reply(actions.get(url.split('/').at(-1)!)));
    if (url === '/data-management/actions') {
      const body=JSON.parse(options!.body as string),receipt:ManagementAction={...body,status:'completed',message:body.operation === 'clear' ? '当前应用记录已清除，可以重新开始。其他数据位置、工程记录和外部副本未处理。' : '已核实完成',dataset_id:info.dataset_id,epoch:info.epoch,revision:++info.revision,completed_count:1,remaining_count:0};
      if (body.operation === 'trash' && body.object_type === 'chat') {const chat=normalSessions.find(value=>value.id === body.object_id)!;normalSessions=normalSessions.filter(value=>value !== chat);trash.push({object_type:'chat',object_id:chat.id,title:chat.title,deleted_at:'2026-10-05T02:00:00Z'});quotes=quotes.map(value=>({...value,source_conversation_status:value.source_session_id === chat.id ? 'trash' : value.source_conversation_status}));}
      if (body.operation === 'trash' && body.object_type === 'quote') {quotes=[];trash.push({object_type:'quote',object_id:qid,title:'Team',saved_at:quote().saved_at,deleted_at:'2026-10-05T02:00:00Z'});savedReply={status:'saved',code:'quote_in_trash',message:'当时保存成功；目前在最近删除。',quote:null,quote_id:qid,current_status:'trash'};}
      if (body.operation === 'restore') {trash=trash.filter(item=>item.object_id !== body.object_id);if (body.object_type === 'quote') quotes=[summary()];else normalSessions.push({...sessions()[0],version:3});}
      if (body.operation === 'clear') {normalSessions=[];quotes=[];trash=[];info={...info,epoch:'epoch-after-clear',counts:structuredClone(testDataManagement.counts),last_clear:{action_ref:body.action_ref,completed_at:'2026-10-05T02:00:00Z'}};receipt.epoch=info.epoch;}
      actions.set(body.action_ref,receipt);return Promise.resolve(reply(receipt));
    }
    return Promise.reject(new Error(`unexpected ${url}`));
  }) as typeof fetch;
  const controller=new SessionController(fetcher,storage);
  return {controller,calls,values,storage,actions,get info(){return info;},set info(value:DataManagement){info=value;},set sessions(value:Session[]){normalSessions=value;},set quotes(value:QuoteSummary[]){quotes=value;},set trash(value:TrashItem[]){trash=value;},set intercept(value:Intercept){intercept=value;},set savedReply(value:unknown){savedReply=value;},set chatReply(value:(body:Record<string,unknown>)=>unknown){chatReply=value;}};
}
function LiveManagement({app,onConfirm=()=>{}}:{app:ReturnType<typeof harness>;onConfirm?:(value:ManagementConfirmation)=>void}) {
  const state=useSyncExternalStore(app.controller.subscribe,app.controller.getSnapshot);
  return <DataManagementPanel state={state} controller={app.controller} onClose={()=>{}} onConfirm={onConfirm} onOpenQuote={()=>{}}/>;
}
async function prepare(app:ReturnType<typeof harness>) {
  app.chatReply=body=>({session_id:'chat-a',session_version:1,message_id:2,message:'方案摘要留在聊天',business:{turn_id:'turn',current_task:null,proposal:proposal(String(body.view_id))}});
  await app.controller.send('准备方案');return app.controller.getSnapshot().currentProposal!;
}

describe('actual scope, complete directory and historical boundaries',()=>{
  it('verifies actual path and scopes browser references by actual location as well as identity and epoch',async()=>{
    const app=harness(new Map([['assistant.sessionId','missing'],[SAVE_REFERENCES_KEY,JSON.stringify([{session_id:'chat-a',proposal_id:'proposal',action_ref:actionRef,started_at:'2026-10-05T01:00:00Z'}])]]));
    await app.controller.initialize();expect(app.controller.getSnapshot().sessionId).toBeNull();
    expect(app.controller.getSnapshot().saveActions[0].status).toBe('unknown');
    expect(app.values.get(SAVE_REFERENCES_KEY)).toBeUndefined();
    expect([...app.values.keys()].some(key=>key.includes('test-location.test-epoch.quoteActions'))).toBe(true);
    await app.controller.resolveSaveAction('proposal','chat-a');
    const resolving=app.calls.find(call=>call.url.endsWith('/resolve'))!;
    expect(resolving.options?.headers).toMatchObject({'X-Data-Epoch':'test-epoch','X-Data-Location':'test-location'});
    expect(app.calls.some(call=>call.url === '/chat' || call.url === '/sessions/chat-a/quotes')).toBe(false);
  });
  it('scope read failure states uncertainty, prevents writes, and retry resumes after a real verification',async()=>{
    const app=harness();app.intercept=url=>url === '/data-management' ? Promise.reject(new Error('offline')) : undefined;
    await app.controller.initialize();await app.controller.send('不能发送');
    expect(app.controller.getSnapshot().managementMessage).toContain('暂时无法核实保存位置');
    expect(app.calls.some(call=>call.url === '/chat')).toBe(false);
    app.intercept=()=>undefined;await app.controller.refreshManagement();await app.controller.send('现在可发送');
    expect(app.calls.filter(call=>call.url === '/chat')).toHaveLength(1);
  });
  it('complete saved directory survives absent browser references and marks a deleted source without an open link',async()=>{
    const app=harness();app.quotes=[{...summary(),source_conversation_status:'trash'}];await app.controller.initialize();await app.controller.refreshSaveRecords();
    const view=render(<QuotePanels mode="records" state={app.controller.getSnapshot()} controller={app.controller} onClose={()=>{}} onOpenQuote={()=>{}}/>);
    expect(view.getByText(qid)).toBeTruthy();expect(view.getByText('原聊天在最近删除')).toBeTruthy();
    expect(view.queryByRole('button',{name:'打开原聊天'})).toBeNull();expect(app.controller.getSnapshot().saveActions).toEqual([]);
    expect(app.calls.some(call=>call.url === '/chat')).toBe(false);
  });
  it('deleting another chat stays put; deleting current chat enters blank while preserving saved drafts and references',async()=>{
    const app=harness();await app.controller.initialize();await prepare(app);await app.controller.saveProposal('proposal');
    await app.controller.manage('trash','chat','chat-b',app.info.revision);
    expect(app.controller.getSnapshot().sessionId).toBe('chat-a');expect(app.controller.getSnapshot().messages).toHaveLength(2);
    await app.controller.manage('trash','chat','chat-a',app.info.revision);
    const state=app.controller.getSnapshot();expect(state.sessionId).toBeNull();expect(state.messages).toEqual([]);expect(state.currentProposal).toBeNull();
    expect(state.quoteRecords[0].quote_id).toBe(qid);expect(state.quoteRecords[0].source_conversation_status).toBe('trash');expect(state.saveActions).toHaveLength(1);
  });
  it('deleting a quote closes direct details, preserves chat text and separates past saving from present trash',async()=>{
    const app=harness();await app.controller.initialize();await prepare(app);await app.controller.saveProposal('proposal');await app.controller.lookupQuote(qid);
    const text=app.controller.getSnapshot().messages.map(value=>value.content);
    await app.controller.manage('trash','quote',qid,app.info.revision);
    expect(app.controller.getSnapshot().sessionId).toBe('chat-a');expect(app.controller.getSnapshot().messages.map(value=>value.content)).toEqual(text);
    expect(app.controller.getSnapshot().quoteLookup.quote).toBeNull();expect(app.controller.getSnapshot().quoteRecords).toEqual([]);
    const action=app.controller.getSnapshot().saveActions[0];expect(action.status).toBe('saved');expect(action.current_status).toBe('trash');expect(action.quote).toBeNull();
    await app.controller.lookupQuote(qid);expect(app.controller.getSnapshot().quoteLookup.message).toContain('当时保存成功');
    await app.controller.saveProposal('proposal');expect(app.calls.filter(value=>value.url === '/sessions/chat-a/quotes')).toHaveLength(1);
  });
  it('unknown related saves are checked by the original action before deletion, and uncertainty prevents the mutation',async()=>{
    const app=harness();await app.controller.initialize();await prepare(app);app.savedReply={status:'unknown',code:'read_failed',message:'待核对',quote:null};
    await app.controller.saveProposal('proposal');await app.controller.manage('trash','chat','chat-a',app.info.revision);
    expect(app.calls.some(value=>value.url === '/data-management/actions')).toBe(false);expect(app.calls.filter(value=>value.url.endsWith('/resolve'))).toHaveLength(1);
    expect(app.controller.getSnapshot().notice).toContain('相关保存结果仍待核对');
    await app.controller.manage('trash','chat','chat-b',app.info.revision);expect(app.calls.filter(value=>value.url === '/data-management/actions')).toHaveLength(1);
  });
  it('restoration keeps the selected chat and offers a separate View action without reviving a Proposal',async()=>{
    const app=harness();app.trash=[{object_type:'quote',object_id:qid,title:'Team',saved_at:quote().saved_at,deleted_at:'2026-10-05T02:00:00Z'}];app.quotes=[];
    await app.controller.initialize();await app.controller.switchTo('chat-b');await app.controller.refreshTrash();
    await app.controller.manage('restore','quote',qid,app.info.revision);
    expect(app.controller.getSnapshot().sessionId).toBe('chat-b');expect(app.controller.getSnapshot().currentProposal).toBeNull();
    expect(app.controller.getSnapshot().quoteRecords[0].saved_at).toBe(quote().saved_at);
    const view=render(<LiveManagement app={app}/>);expect(view.getByRole('button',{name:'查看'})).toBeTruthy();
  });
  it('failed position verification retains readable trash but disables restoration and permanent deletion safely',async()=>{
    const app=harness();app.trash=[{object_type:'quote',object_id:qid,title:'Team',deleted_at:'2026-10-05T02:00:00Z'}];app.quotes=[];
    await app.controller.initialize();await app.controller.refreshTrash();const view=render(<LiveManagement app={app}/>);await act(async()=>{await flush();});
    app.intercept=url=>url === '/data-management' ? Promise.reject(new Error('read failed')) : undefined;
    await act(async()=>{await app.controller.refreshManagement();});
    expect(app.controller.getSnapshot().management).toBeNull();expect(view.getByText(qid)).toBeTruthy();
    const restore=view.getByRole('button',{name:'恢复'}),purge=view.getByRole('button',{name:'永久删除'});
    expect(restore.hasAttribute('disabled')).toBe(true);expect(purge.hasAttribute('disabled')).toBe(true);
    fireEvent.click(restore);fireEvent.click(purge);expect(app.calls.some(value=>value.url === '/data-management/actions')).toBe(false);
  });
});

describe('clearing, pending receipts and changes from other pages',()=>{
  it('full clearing removes current scoped references and opens blank, while keeping other position references',async()=>{
    const other='assistant.dataset.other.other-location.old-epoch.quoteActions',app=harness(new Map([[other,'other reference']]));
    await app.controller.initialize();await prepare(app);await app.controller.saveProposal('proposal');
    const oldKey=[...app.values.keys()].find(key=>key.includes('test-location.test-epoch.quoteActions'))!;
    await app.controller.manage('clear',null,null,app.info.revision,app.controller.managementScope());
    expect(app.values.get(oldKey)).toBeUndefined();expect(app.values.get(other)).toBe('other reference');
    const state=app.controller.getSnapshot();expect(state.sessionId).toBeNull();expect(state.messages).toEqual([]);expect(state.saveActions).toEqual([]);expect(state.trash).toEqual([]);
    expect(state.management?.epoch).toBe('epoch-after-clear');expect(state.notice).toContain('其他数据位置、工程记录和外部副本未处理');
  });
  it('lost clearing receipt remains pending, blocks writes and resolves the original ref with GET without resubmission',async()=>{
    const app=harness();await app.controller.initialize();
    app.intercept=(url,options)=>url === '/data-management/actions' && options?.method === 'POST' ? Promise.reject(new Error('lost')) : undefined;
    await app.controller.manage('clear',null,null,app.info.revision);
    const pending=app.controller.getSnapshot().managementActions[0];expect(pending.status).toBe('unknown');expect(app.controller.getSnapshot().busy).toBe(true);
    await app.controller.send('不可继续');expect(app.calls.some(value=>value.url === '/chat')).toBe(false);
    app.actions.set(pending.action_ref,{...pending,status:'not_completed',message:'核对完成：本次未执行。',completed_count:0,remaining_count:1});
    app.intercept=()=>undefined;await app.controller.resolveManagement(pending.action_ref);
    expect(app.calls.filter(value=>value.url === '/data-management/actions')).toHaveLength(1);
    expect(app.calls.some(value=>value.url === `/data-management/actions/${pending.action_ref}`)).toBe(true);
    expect(app.controller.getSnapshot().managementActions[0].status).toBe('not_completed');expect(app.controller.getSnapshot().busy).toBe(false);
  });
  it('a poll started before clear cannot reinstall its old epoch after the completed clear receipt',async()=>{
    const app=harness();await app.controller.initialize();let finish:(value:Response)=>void=()=>{};
    const before=structuredClone(app.info);app.intercept=url=>url === '/data-management' ? new Promise<Response>(resolve=>{finish=resolve;}) : undefined;
    const oldPoll=app.controller.refreshManagement(),clearing=app.controller.manage('clear',null,null,app.info.revision);await flush();
    expect(app.controller.getSnapshot().busy).toBe(true);app.intercept=()=>undefined;finish(reply(before));await oldPoll;await clearing;
    expect(app.controller.getSnapshot().management?.epoch).toBe('epoch-after-clear');
    await app.controller.send('清理后新提问');const sending=app.calls.filter(value=>value.url === '/chat').at(-1)!;
    expect(sending.options?.headers).toMatchObject({'X-Data-Epoch':'epoch-after-clear'});
  });
  it('a mismatched management receipt stays pending and does not claim object deletion',async()=>{
    const app=harness();await app.controller.initialize();await app.controller.switchTo('chat-a');
    app.intercept=(url,options)=>url === '/data-management/actions' ? Promise.resolve(reply({...JSON.parse(options!.body as string),status:'completed',message:'不可采信成功',dataset_id:app.info.dataset_id,epoch:app.info.epoch,revision:2,object_id:'another-chat'})) : undefined;
    await app.controller.manage('trash','chat','chat-a',app.info.revision);
    expect(app.controller.getSnapshot().sessionId).toBe('chat-a');expect(app.controller.getSnapshot().managementActions[0].status).toBe('unknown');expect(app.controller.getSnapshot().notice).not.toContain('不可采信成功');
  });
  it('a finite timeout never turns a late clearing receipt into success',async()=>{
    vi.useFakeTimers();try {
      const app=harness();await app.controller.initialize();let finish:(value:Response)=>void=()=>{};
      app.intercept=url=>url === '/data-management/actions' ? new Promise<Response>(resolve=>{finish=resolve;}) : undefined;
      const managing=app.controller.manage('clear',null,null,app.info.revision);await vi.advanceTimersByTimeAsync(15_000);await managing;
      const pending=app.controller.getSnapshot().managementActions[0];finish(reply({...pending,status:'completed',message:'迟到成功'}));await flush();
      expect(app.controller.getSnapshot().managementActions[0].status).toBe('unknown');expect(app.controller.getSnapshot().notice).not.toContain('迟到成功');
    } finally {vi.useRealTimers();}
  });
  it('ordinary revision changes preserve this page’s valid Proposal and direct quote detail',async()=>{
    const app=harness();await app.controller.initialize();await prepare(app);await app.controller.lookupQuote(qid);
    const before=app.controller.getSnapshot();app.info={...app.info,revision:app.info.revision+1};await app.controller.refreshManagement();
    expect(app.controller.getSnapshot().currentProposal).toEqual(before.currentProposal);expect(app.controller.getSnapshot().quoteLookup.quote?.snapshot).toEqual(before.quoteLookup.quote?.snapshot);
  });
  it('another page’s new persisted reply reopens historical text and removes current saving permission',async()=>{
    const app=harness();await app.controller.initialize();await prepare(app);
    app.sessions=[{...sessions()[0],last_message_id:3},sessions()[1]];app.info={...app.info,revision:app.info.revision+1};await app.controller.refreshManagement();
    expect(app.controller.getSnapshot().currentProposal).toBeNull();expect(app.controller.getSnapshot().messages.every(value=>value.historical)).toBe(true);
  });
  it('same-identity copied database uses distinct location scope and ignores an old directory response',async()=>{
    const app=harness();await app.controller.initialize();let finish:(value:Response)=>void=()=>{};
    app.intercept=url=>url === '/quotes' ? new Promise<Response>(resolve=>{finish=resolve;}) : undefined;
    const reading=app.controller.refreshQuoteDirectory();app.intercept=()=>undefined;app.info={...app.info,location_id:'new-location',database_path:'D:/another/saas.sqlite3'};app.quotes=[];
    await app.controller.refreshManagement();finish(reply({quotes:[summary()]}));await reading;
    expect(app.controller.getSnapshot().management?.database_path).toBe('D:/another/saas.sqlite3');expect(app.controller.getSnapshot().quoteRecords).toEqual([]);
  });
  it('an old location’s late clearing result cannot erase the current location or its references',async()=>{
    const app=harness();await app.controller.initialize();let finish:(value:Response)=>void=()=>{};
    app.intercept=url=>url === '/data-management/actions' ? new Promise<Response>(resolve=>{finish=resolve;}) : undefined;
    const clearing=app.controller.manage('clear',null,null,app.info.revision),old=app.controller.getSnapshot().managementActions[0];
    app.intercept=()=>undefined;app.info={...app.info,location_id:'new-location',database_path:'D:/another/saas.sqlite3'};await app.controller.refreshManagement();await app.controller.switchTo('chat-b');
    finish(reply({...old,status:'completed',message:'旧位置清理成功'}));await clearing;
    expect(app.controller.getSnapshot().sessionId).toBe('chat-b');expect(app.controller.getSnapshot().notice).not.toContain('旧位置清理成功');
    expect(app.controller.getSnapshot().managementBusy).toBe(false);
  });
  it('data-management confirmation shows full counts and exclusions with one irreversible confirm',async()=>{
    const app=harness();app.info={...app.info,counts:{conversations:4,messages:20,quotes:3,trash_conversations:2,trash_messages:8,trash_quotes:1}};await app.controller.initialize();
    const confirm=vi.fn(),view=render(<LiveManagement app={app} onConfirm={confirm}/>);
    await act(async()=>{await flush();});
    await act(async()=>{fireEvent.click(view.getByRole('button',{name:'清除当前应用记录'}));await flush();});
    const selected=confirm.mock.calls[0][0] as ManagementConfirmation;
    const modal=render(<ManagementConfirm confirmation={selected} state={app.controller.getSnapshot()} onCancel={()=>{}} onConfirm={()=>{}}/>);
    expect(modal.getByRole('dialog').textContent).toContain('D:/isolated/saas.sqlite3');expect(modal.getByRole('dialog').textContent).toContain('4 条聊天 · 20 条消息 · 3 份草案');
    expect(modal.getByRole('dialog').textContent).toContain('原个人助手库');expect(modal.getByRole('dialog').textContent).toContain('终端已有输出和外部重定向日志');expect(modal.getByRole('dialog').textContent).toContain('不承诺磁盘安全擦除');
    expect(modal.getAllByRole('button',{name:'永久清除并重新开始'})).toHaveLength(1);
  });
});
