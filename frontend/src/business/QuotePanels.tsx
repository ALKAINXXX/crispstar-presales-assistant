import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ProposalSummary } from './BusinessResponse';
import { money, type SessionController, type ViewState } from './adapter';
import { savedTime } from './quotes';
import { sourceStatusText, type ManagementConfirmation } from './DataManagement';

export function QuotePanels({mode,state,controller,onClose,onOpenQuote,onConfirm}: {mode:'lookup'|'records';state:ViewState;controller:SessionController;onClose:()=>void;onOpenQuote?:(quoteId:string)=>void;onConfirm?:(confirmation:ManagementConfirmation)=>void}) {
  const [quoteId,setQuoteId] = useState('');
  const [copyNotice,setCopyNotice] = useState<{kind:'success'|'error';message:string}|null>(null);
  const copySequence = useRef(0);
  const copyTimer = useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
  useEffect(()=>{if (mode === 'lookup') setQuoteId('');},[mode]);
  useEffect(()=>{if (mode === 'records') void controller.refreshSaveRecords();},[mode,controller]);
  useEffect(()=>()=>{copySequence.current++;clearTimeout(copyTimer.current);},[]);
  const copyQuoteId = async (id:string) => {
    const sequence = ++copySequence.current;
    clearTimeout(copyTimer.current);
    setCopyNotice(null);
    const showNotice = (kind:'success'|'error',message:string) => {
      if (sequence !== copySequence.current) return;
      setCopyNotice({kind,message});
      copyTimer.current = setTimeout(()=>{if (sequence === copySequence.current) setCopyNotice(null);},kind === 'success' ? 2_500 : 5_000);
    };
    try { await navigator.clipboard.writeText(id);showNotice('success','✓ 草案编号已复制'); }
    catch { showNotice('error','复制失败，请手动复制完整编号。'); }
  };
  const deleteQuote=async(id:string,summary:string)=>{
    const scope=controller.managementScope();
    if (!await controller.refreshManagement()) return;
    const current=controller.getSnapshot().management;
    if (current && scope === controller.managementScope()) onConfirm?.({operation:'trash',object_type:'quote',object_id:id,title:id,summary,revision:current.revision,scope});
  };
  const quotePending=(id:string)=>state.managementActions.some(action=>action.object_type === 'quote' && action.object_id === id && ['submitting','unknown'].includes(action.status));
  return <><section id="quote-panel-region" className="restored-result quote-panel" aria-label={mode === 'lookup' ? '按编号查草案' : '保存记录'}>
    <div className="quote-panel-heading"><p>{mode === 'lookup' ? '按编号查草案' : '保存记录'}</p><button onClick={onClose}>收起</button></div>
    {mode === 'lookup' ? <>
      <p className="business-note">直接读取保存时的内容，模型暂不可用时也可查询。</p>
      <form className="quote-lookup-form" onSubmit={event=>{event.preventDefault();void controller.lookupQuote(quoteId);}}><label htmlFor="quote-id-input">草案编号</label><input id="quote-id-input" value={quoteId} onChange={event=>{setQuoteId(event.target.value);controller.clearQuoteLookup();}} placeholder="Q-…" autoComplete="off"/><button disabled={state.quoteLookup.loading} type="submit">{state.quoteLookup.loading ? '读取中…' : '查询'}</button></form>
      {state.quoteLookup.message && <p role="status" className="business-warning">{state.quoteLookup.message}</p>}
      {state.quoteLookup.quote && <><p className="business-note">{sourceStatusText(state.quoteLookup.quote.source_conversation_status)}。聊天与草案独立管理。</p>{state.quoteLookup.quote.source_conversation_status === 'active' && <button className="management-button" onClick={()=>void controller.switchTo(state.quoteLookup.quote!.snapshot.session_id)}>打开原聊天</button>}<ProposalSummary proposal={state.quoteLookup.quote.snapshot} currentResult={false} currentProposal={false} savedQuote={state.quoteLookup.quote}/></>}
    </> : <>
      <p className="business-note">从本机存档读取完整草案目录，不依赖浏览器保存动作引用，也不需要模型在线。删除草案会保留聊天中的摘要、编号和引用文字。</p>
      {state.saveRecordsLoading && <p role="status" className="business-note">正在读取保存记录…</p>}
      {state.quoteRecordsMessage && <p role="status" className="business-warning">{state.quoteRecordsMessage}<button className="management-button" onClick={()=>void controller.refreshSaveRecords()}>重试读取</button></p>}
      {!state.saveRecordsLoading && !state.quoteRecordsMessage && state.quoteRecords.length === 0 && <p className="business-note">当前正常区没有已保存草案；最近删除可在数据管理查看。</p>}
      {state.quoteRecords.map(quote=><article className="save-event quote-record" key={quote.quote_id} data-quote-id={quote.quote_id}><div className="save-event-heading"><h3>{quotePending(quote.quote_id) ? '草案管理结果待核对' : '已保存草案'}</h3><details className="object-menu"><summary aria-label={`草案 ${quote.quote_id} 的菜单`}>⋯</summary><div><button disabled={!controller.canManageObject('quote',quote.quote_id)} onClick={event=>{event.currentTarget.closest('details')?.removeAttribute('open');void deleteQuote(quote.quote_id,`${quote.selected_plan_name} · ${quote.paid_seats} 席 · ${quote.months} 个月`);}}>删除草案</button></div></details></div><p className="quote-number">{quote.quote_id}</p><p className="saved-quote-summary">{quote.selected_plan_name} · {quote.paid_seats} 席 · {quote.months} 个月 · 订阅基价 {money(quote.base_price_minor)} 元</p><p className="business-note">保存时间：{savedTime(quote.saved_at)}</p><p className="business-note">{quotePending(quote.quote_id) ? '以上是此前读取的存档摘要，目前管理结果待核对。请先在数据管理核对原操作。' : sourceStatusText(quote.source_conversation_status)}</p><div className="saved-quote-actions"><button onClick={()=>void copyQuoteId(quote.quote_id)}>复制编号</button>{onOpenQuote && <button disabled={quotePending(quote.quote_id)} onClick={()=>onOpenQuote(quote.quote_id)}>查看草案</button>}{quote.source_conversation_status === 'active' && <button disabled={quotePending(quote.quote_id)} onClick={()=>void controller.switchTo(quote.source_session_id)}>打开原聊天</button>}</div></article>)}
      {state.saveActions.length > 0 && <h3 className="save-check-heading">保存动作核对</h3>}
      <p className="business-note">核对已经发起的原保存动作，不会再次创建草案。历史上的保存成功和目前删除状态分别表达。</p>
      {[...state.saveActions].reverse().filter(action=>action.status !== 'saved' || !state.quoteRecords.some(quote=>quote.quote_id === (action.quote?.quote_id ?? action.quote_id))).map(action=><article key={`${action.session_id}:${action.proposal_id}`} className="save-event" data-business-event="quote-save" data-save-status={action.status}>
        <div className="save-event-heading"><h3>{action.status === 'saved' ? action.current_status === 'trash' || action.current_status === 'permanently_deleted' ? '当时保存成功' : '草案已保存' : action.status === 'not_saved' ? '当前记录未找到该保存动作' : action.status === 'submitting' ? '正在保存草案' : action.status === 'checking' ? '正在核对保存结果' : '保存结果待核对'}</h3>{action.session_id !== state.sessionId && state.sessions.some(session=>session.id === action.session_id) && <button onClick={()=>void controller.switchTo(action.session_id)}>打开原聊天</button>}</div>
        <p className="business-note">{action.session_id === state.sessionId ? '本聊天' : '原聊天'}的保存操作 · 发起于 {savedTime(action.started_at)}</p>
        <p role="status">{action.message}</p>
        {action.status === 'unknown' && <button className="resolve-save-button" onClick={()=>void controller.resolveSaveAction(action.proposal_id,action.session_id)}>核对保存结果</button>}
        {action.status === 'not_saved' && <p className="business-note">此结果只核对当前这份记录，不能证明历史上从未保存。继续保存时，请在聊天中重新准备并核对方案，再点击新的保存按钮。</p>}
        {action.status === 'saved' && !action.quote && <><p className="quote-number">{action.quote_id}</p><p className="business-note">{action.current_status === 'trash' ? '目前在最近删除，可在数据管理恢复。' : '目前已永久删除，无法读取。'}旧保存动作不会重新创建草案。</p></>}
        {action.status === 'saved' && action.quote && <><p className="quote-number">{action.quote.quote_id}</p><p className="saved-quote-summary">{action.quote.snapshot.selected_plan_name} · {action.quote.snapshot.pricing.paid_seats} 席 · {action.quote.snapshot.pricing.months} 个月 · 订阅基价 {money(action.quote.snapshot.pricing.base_price_minor)} 元</p><p className="business-note">保存时间：{savedTime(action.quote.saved_at)}</p><div className="saved-quote-actions"><button onClick={()=>void copyQuoteId(action.quote!.quote_id)}>复制编号</button>{onOpenQuote && <button onClick={()=>onOpenQuote(action.quote!.quote_id)}>查看草案</button>}</div><details><summary>展开保存内容</summary><ProposalSummary proposal={action.quote.snapshot} currentResult={false} currentProposal={false} savedQuote={action.quote}/></details></>}
      </article>)}
    </>}
  </section>{copyNotice && createPortal(<div className={`quote-copy-notice ${copyNotice.kind}`} role={copyNotice.kind === 'success' ? 'status' : 'alert'} aria-atomic="true">{copyNotice.message}</div>,document.body)}</>;
}
