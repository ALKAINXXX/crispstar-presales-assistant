import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { Thread, CurrentTaskContext, CurrentProposalContext, SaveActionsContext } from '@/components/assistant-ui/elements/thread.aui';
import { ThreadListSidebar } from '@/components/assistant-ui/elements/threadlist-sidebar.aui';
import { SidebarInset, SidebarProvider, SidebarTrigger } from '@/components/ui/sidebar';
import { CurrentResult } from '@/business/BusinessResponse';
import { QuotePanels } from '@/business/QuotePanels';
import { DataManagementPanel, ManagementConfirm, type ManagementConfirmation } from '@/business/DataManagement';
import { SessionController, UI_CONTRACT, convertMessage } from '@/business/adapter';

type BusinessArea = 'current'|'lookup'|'records'|'management'|null;

export function App({controller}: {controller:SessionController}) {
  const state = useSyncExternalStore(controller.subscribe,controller.getSnapshot);
  const [activeArea,setActiveArea] = useState<BusinessArea>(null);
  const [confirmation,setConfirmation] = useState<ManagementConfirmation|null>(null);
  const previousSaveStates = useRef(new Map<string,string>());
  useEffect(()=>{
    void controller.initialize();
    const stopSync=controller.startSync();
    const leave = () => { void controller.leaveView(); };
    const resume = (event:PageTransitionEvent) => { if (event.persisted) void controller.resumeView(); };
    window.addEventListener('pagehide',leave);
    window.addEventListener('pageshow',resume);
    return () => {stopSync();window.removeEventListener('pagehide',leave); window.removeEventListener('pageshow',resume); };
  },[controller]);
  useEffect(()=>{setActiveArea(null);},[state.activeKey]);
  useEffect(()=>{
    setActiveArea(area=>area === 'current' ? null : area);
  },[state.currentTask?.result_turn_id]);
  const unknownSaves = state.saveActions.filter(action=>action.status === 'unknown').length;
  useEffect(()=>{
    // Keep the existing attention path for a newly lost save receipt, without
    // reopening records after a refresh or each failed read-only retry.
    const action = state.saveActions.find(action=>action.status === 'unknown'
      && action.session_id === state.sessionId && !action.restored
      && previousSaveStates.current.get(`${action.session_id}:${action.action_ref}`) === 'submitting');
    previousSaveStates.current = new Map(state.saveActions.map(action=>[`${action.session_id}:${action.action_ref}`,action.status]));
    if (action) {
      setActiveArea('records');
    }
  },[state.saveActions,state.sessionId]);
  const welcome = useMemo(()=>({Welcome:()=> <Welcome controller={controller}/>}),[controller]);
  const runtime = useExternalStoreRuntime({messages:state.messages,convertMessage,isRunning:state.busy && !state.loading && !!state.management && !state.managementBusy,isLoading:state.loading,isSendDisabled:state.busy || !state.management || !state.chatAvailability.available,onNew:controller.send,adapters:{threadList:{threadId:state.sessionId ?? state.activeKey,isLoading:state.sessionsLoading,threads:state.sessions.map(session=>({id:session.id,remoteId:session.id,title:session.title,status:'regular' as const})),onSwitchToNewThread:controller.newChat,onSwitchToThread:controller.switchTo}}});
  const closeArea = () => {controller.clearQuoteLookup();setActiveArea(null);};
  const togglePanel = (area:'lookup'|'records'|'management') => {
    // Each opening starts a manual query with no remembered number or late
    // result from an earlier read. Copying a record never enters this path.
    controller.clearQuoteLookup();
    setActiveArea(value=>value === area ? null : area);
  };
  const toggleCurrent = () => {
    if (activeArea === 'current') {closeArea();return;}
    if (!state.currentTask) return;
    controller.clearQuoteLookup();
    setActiveArea('current');
  };
  const openQuote = (id:string) => {
    setActiveArea('lookup');
    void controller.lookupQuote(id);
  };
  const deleteChat=(id:string,title:string)=>{
    const scope=controller.managementScope();
    void controller.refreshManagement().then(verified=>{const current=controller.getSnapshot().management;if (verified && current && scope === controller.managementScope()) setConfirmation({operation:'trash',object_type:'chat',object_id:id,title,revision:current.revision,scope});});
  };
  const unresolvedManagement=state.managementActions.filter(action=>action.status === 'unknown').length;
  const lastManagement=state.managementActions.at(-1);
  const feedbackClass=lastManagement?.message === state.notice ? lastManagement.status === 'completed' ? 'management-feedback' : 'management-attention' : '';
  return <AssistantRuntimeProvider runtime={runtime}><CurrentTaskContext.Provider value={state.currentTask}><CurrentProposalContext.Provider value={state.currentProposal}><SaveActionsContext.Provider value={{actions:state.saveActions,onSave:id=>void controller.saveProposal(id),disabled:state.busy || !state.management}}><SidebarProvider className="h-dvh min-h-0 overflow-hidden"><div className="app-layout flex h-dvh w-full" data-ui-contract={UI_CONTRACT}>
    <ThreadListSidebar onDelete={deleteChat} canDelete={id=>controller.canManageObject('chat',id)} onDataManagement={()=>togglePanel('management')} dataActive={activeArea === 'management'}/><SidebarInset className="min-h-0 min-w-0 overflow-hidden"><header className="chat-header flex shrink-0 items-center gap-3 border-b px-4 py-3"><SidebarTrigger aria-label="切换会话侧栏"/><div className="min-w-0 flex-1"><h1>售前问答</h1><p>{state.health}</p></div><div className="header-business-actions" role="group" aria-label="业务区域">
      <button className="current-result-button disabled:opacity-50" disabled={!state.currentTask} aria-pressed={activeArea === 'current'} aria-expanded={activeArea === 'current'} aria-controls="restored-current-region" onClick={toggleCurrent}>{state.currentTask ? '查看当前结果' : '尚无当前选型'}</button>
      <button aria-pressed={activeArea === 'lookup'} aria-expanded={activeArea === 'lookup'} aria-controls={activeArea === 'lookup' ? 'quote-panel-region' : undefined} onClick={()=>togglePanel('lookup')}>按编号查草案</button>
      <button aria-pressed={activeArea === 'records'} aria-expanded={activeArea === 'records'} aria-controls={activeArea === 'records' ? 'quote-panel-region' : undefined} onClick={()=>togglePanel('records')}>保存记录{unknownSaves ? `（${unknownSaves} 待核对）` : ''}</button>
    </div></header>
    {!state.chatAvailability.available && <div className="view-notice chat-availability-notice" role="status">{state.chatAvailability.message}</div>}
    {!state.management && state.managementMessage && <div className="view-notice management-attention" role="status">{state.managementMessage}<button onClick={()=>void controller.refreshManagement()}>重试核实</button></div>}
    {state.notice && <div className={`view-notice ${feedbackClass}`} role="status">{state.notice}</div>}
    {unresolvedManagement > 0 && <div className="view-notice management-attention" role="status">{unresolvedManagement} 项管理结果待核对。<button onClick={()=>togglePanel('management')}>核对原操作</button></div>}
    {(activeArea === 'lookup' || activeArea === 'records') && <QuotePanels key={activeArea} mode={activeArea} state={state} controller={controller} onClose={closeArea} onOpenQuote={openQuote} onConfirm={setConfirmation}/>}
    {activeArea === 'management' && <DataManagementPanel state={state} controller={controller} onClose={closeArea} onConfirm={setConfirmation} onOpenQuote={openQuote}/>}
    {activeArea === 'current' && state.currentTask && <section id="restored-current-region" aria-label="当前结果" className="restored-result"><div className="flex items-center justify-between gap-3"><p>当前选型</p><button className="text-sm" onClick={closeArea}>收起</button></div><CurrentResult task={state.currentTask} catalog={state.catalog} restored anchor="restored-current-result"/></section>}
    <div className="min-h-0 flex-1 overflow-hidden"><Thread components={welcome}/></div>
  </SidebarInset></div>{confirmation && <ManagementConfirm confirmation={confirmation} state={state} onCancel={()=>setConfirmation(null)} onConfirm={()=>{const selected=confirmation;setConfirmation(null);void controller.manage(selected.operation,selected.object_type,selected.object_id,selected.revision,selected.scope);}}/>}</SidebarProvider></SaveActionsContext.Provider></CurrentProposalContext.Provider></CurrentTaskContext.Provider></AssistantRuntimeProvider>;
}
function Welcome({controller}:{controller:SessionController}) {
  const state = useSyncExternalStore(controller.subscribe,controller.getSnapshot);
  return <div className="thread-welcome"><p className="text-2xl font-semibold">今天想了解什么？</p><p>可以直接问一个问题，也可以逐步说明必须满足的需求。</p><div className="welcome-prompts">{['启用 SSO 前要准备什么身份系统？','Team 每席每月多少钱？','必须支持任务与项目协作；必须支持数据导出。请选择最低满足档。'].map(text=><button key={text} disabled={state.busy || !state.management || !state.chatAvailability.available} onClick={()=>void controller.send(text)}>{text}</button>)}</div></div>;
}
