import { createContext, type ReactNode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { SessionController, ViewState } from './business/adapter';
import type { CurrentTask } from './business/types';

vi.mock('@assistant-ui/react',()=>({AssistantRuntimeProvider:({children}:{children:ReactNode})=><>{children}</>,useExternalStoreRuntime:()=>({})}));
vi.mock('@/components/assistant-ui/elements/thread.aui',()=>({
  Thread:()=> <div data-slot="aui_thread-viewport"><div id="result-current-turn" data-current="true" tabIndex={-1}>聊天内当前结果</div></div>,
  CurrentTaskContext:createContext(null),CurrentProposalContext:createContext(null),SaveActionsContext:createContext(null),
}));
vi.mock('@/components/assistant-ui/elements/threadlist-sidebar.aui',()=>({ThreadListSidebar:()=>null}));
vi.mock('@/components/ui/sidebar',()=>({SidebarProvider:({children}:{children:ReactNode})=><>{children}</>,SidebarInset:({children}:{children:ReactNode})=><main>{children}</main>,SidebarTrigger:()=>null}));
vi.mock('@/business/BusinessResponse',()=>({CurrentResult:()=> <p>服务端当前选型</p>}));
vi.mock('@/business/QuotePanels',()=>({QuotePanels:({mode,onClose,onOpenQuote}:{mode:string;onClose:()=>void;onOpenQuote:(id:string)=>void})=><section id="quote-panel-region" aria-label={mode === 'lookup' ? '按编号查草案' : '保存记录'}><button onClick={onClose}>收起</button>{mode === 'records' ? <button onClick={()=>onOpenQuote('Q-0123456789ABCDEF0123456789ABCDEF')}>查看草案</button> : <input aria-label="草案编号" defaultValue=""/>}</section>}));

const task:CurrentTask = {kind:'selection',status:'current',last_result:'updated',result_turn_id:'current-turn',requirements:{requirement_id:'req',revision:1,items:[],parameters:[],condition_assertions:[],selected_plan_id:null},match:null,pricing:null,pending:[],questions:[]};
function harness(currentTask:CurrentTask|null = task) {
  let state:ViewState = {activeKey:'same',sessionId:'same',messages:[],currentTask,currentProposal:null,catalog:null,restored:false,loading:false,busy:false,notice:'',sessions:[],sessionsLoading:false,health:'服务已连接',chatAvailability:{available:true,message:''},saveRecordsLoading:false,quoteLookup:{loading:false,quote:null,message:''},quoteRecords:[],quoteRecordsMessage:'',management:null,managementLoading:false,managementMessage:'',managementActions:[],managementBusy:false,trash:[],trashMessage:'',saveActions:[{session_id:'same',proposal_id:'old',action_ref:'existing-action-012345678901234567890123456789',started_at:'2026-10-05T01:00:00Z',status:'unknown',quote:null,message:'待核对',code:'restored_reference',restored:true}]};
  const listeners = new Set<()=>void>();
  const controller = {getSnapshot:()=>state,subscribe:(listener:()=>void)=>{listeners.add(listener);return ()=>{listeners.delete(listener);};},initialize:vi.fn(async()=>{}),startSync:()=>()=>{},leaveView:vi.fn(async()=>{}),resumeView:vi.fn(),send:vi.fn(),newChat:vi.fn(),switchTo:vi.fn(),saveProposal:vi.fn(),lookupQuote:vi.fn(async()=>{}),clearQuoteLookup:vi.fn()} as unknown as SessionController;
  return {controller,change(values:Partial<ViewState>){act(()=>{state={...state,...values};listeners.forEach(listener=>listener());});}};
}
const currentButton = ()=>screen.getByRole('button',{name:'查看当前结果'});
const lookupButton = ()=>screen.getByRole('button',{name:'按编号查草案'});
const recordsButton = ()=>screen.getByRole('button',{name:/^保存记录/});
const selected = (button:HTMLElement)=>button.getAttribute('aria-pressed') === 'true';
afterEach(()=>vi.unstubAllGlobals());

describe('business area navigation',()=>{
  it('selects only the opened area and clears selection when toggled or closed',()=>{
    const app=harness();render(<App controller={app.controller}/>);
    expect([currentButton(),lookupButton(),recordsButton()].map(selected)).toEqual([false,false,false]);
    fireEvent.click(lookupButton());
    expect([currentButton(),lookupButton(),recordsButton()].map(selected)).toEqual([false,true,false]);
    expect(lookupButton().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('region',{name:'按编号查草案'})).toBeTruthy();
    fireEvent.click(recordsButton());
    expect([currentButton(),lookupButton(),recordsButton()].map(selected)).toEqual([false,false,true]);
    expect(screen.queryByRole('region',{name:'按编号查草案'})).toBeNull();
    fireEvent.click(recordsButton());
    expect([currentButton(),lookupButton(),recordsButton()].map(selected)).toEqual([false,false,false]);
    fireEvent.click(lookupButton());fireEvent.click(screen.getByRole('button',{name:'收起'}));
    expect(selected(lookupButton())).toBe(false);
  });
  it('expands current results in the same region even when the chat result is present',()=>{
    const app=harness();render(<App controller={app.controller}/>);
    fireEvent.click(currentButton());expect(selected(currentButton())).toBe(true);
    expect(currentButton().getAttribute('aria-controls')).toBe('restored-current-region');
    expect(currentButton().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('region',{name:'当前结果'})).toBeTruthy();
    expect(screen.getByText('服务端当前选型')).toBeTruthy();
    fireEvent.click(lookupButton());expect(selected(currentButton())).toBe(false);
    fireEvent.click(currentButton());fireEvent.click(currentButton());
    expect(selected(currentButton())).toBe(false);
    expect(screen.queryByRole('region',{name:'按编号查草案'})).toBeNull();
  });
  it('handles restored current result expansion and its close button',()=>{
    const app=harness({...task,result_turn_id:'not-in-chat'});render(<App controller={app.controller}/>);
    fireEvent.click(currentButton());expect(selected(currentButton())).toBe(true);
    expect(currentButton().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('服务端当前选型')).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'收起'}));expect(selected(currentButton())).toBe(false);
    expect(screen.queryByText('服务端当前选型')).toBeNull();
  });
  it('opens a record with one read while leaving the manual input blank; reopening starts blank',()=>{
    const app=harness();render(<App controller={app.controller}/>);
    fireEvent.click(recordsButton());fireEvent.click(screen.getByRole('button',{name:'查看草案'}));
    expect([currentButton(),lookupButton(),recordsButton()].map(selected)).toEqual([false,true,false]);
    expect((screen.getByRole('textbox',{name:'草案编号'}) as HTMLInputElement).value).toBe('');
    expect(app.controller.lookupQuote).toHaveBeenCalledExactlyOnceWith('Q-0123456789ABCDEF0123456789ABCDEF');
    fireEvent.change(screen.getByRole('textbox',{name:'草案编号'}),{target:{value:'manual draft'}});
    fireEvent.click(lookupButton());fireEvent.click(lookupButton());
    expect((screen.getByRole('textbox',{name:'草案编号'}) as HTMLInputElement).value).toBe('');
    expect(app.controller.clearQuoteLookup).toHaveBeenCalled();
    expect(app.controller.lookupQuote).toHaveBeenCalledTimes(1);
  });
  it('clears selection on chat switch and when the current result is replaced',()=>{
    const app=harness();render(<App controller={app.controller}/>);
    fireEvent.click(currentButton());app.change({currentTask:{...task,result_turn_id:'new-turn'}});
    expect(selected(currentButton())).toBe(false);
    fireEvent.click(recordsButton());app.change({activeKey:'other',sessionId:'other',currentTask:null});
    expect(selected(recordsButton())).toBe(false);expect(selected(lookupButton())).toBe(false);
    expect(screen.getByRole('button',{name:'尚无当前选型'}).getAttribute('disabled')).not.toBeNull();
  });
  it('keeps the current region open independently of chat scrolling and saves no object',()=>{
    const observer = vi.fn();
    vi.stubGlobal('IntersectionObserver',observer);
    const app=harness();render(<App controller={app.controller}/>);fireEvent.click(currentButton());
    fireEvent.scroll(document.querySelector('[data-slot="aui_thread-viewport"]')!);
    expect(selected(currentButton())).toBe(true);
    expect(screen.getByRole('region',{name:'当前结果'})).toBeTruthy();
    expect(observer).not.toHaveBeenCalled();
    expect(app.controller.send).not.toHaveBeenCalled();
    expect(app.controller.saveProposal).not.toHaveBeenCalled();
  });
  it('a failed record read cannot reopen records over a selected lookup area',()=>{
    const app=harness();render(<App controller={app.controller}/>);
    fireEvent.click(recordsButton());
    app.change({saveActions:[{...app.controller.getSnapshot().saveActions[0],status:'checking'}]});
    fireEvent.click(lookupButton());
    app.change({saveActions:[{...app.controller.getSnapshot().saveActions[0],status:'unknown',restored:false,code:'action_in_flight'}]});
    expect(selected(lookupButton())).toBe(true);expect(selected(recordsButton())).toBe(false);
  });
});
