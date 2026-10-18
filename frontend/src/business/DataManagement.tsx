import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { SessionController, ViewState } from './adapter';
import type { ManagementOperation } from './types';
import { savedTime } from './quotes';

export interface ManagementConfirmation {operation:ManagementOperation;object_type:'chat'|'quote'|null;object_id:string|null;title:string;summary?:string;revision:number;scope:string}
export const keptRecords = '其他数据位置、原个人助手库、其他项目数据；程序、配置、凭据、固定资料、索引、模型文件；工程验证库、报告、截图、源码备份；导出、外部备份、上游服务记录；终端已有输出和外部重定向日志等副本。';
export const sourceStatusText = (status:string|undefined) => status === 'trash' ? '原聊天在最近删除' : status === 'permanently_deleted' ? '原聊天已永久删除' : status === 'not_found' ? '当前记录中未找到原聊天' : status === 'active' ? '原聊天仍保留' : '原聊天状态暂时无法核实';

export function ManagementConfirm({confirmation,state,onCancel,onConfirm}:{confirmation:ManagementConfirmation;state:ViewState;onCancel:()=>void;onConfirm:()=>void}) {
  const [submitting,setSubmitting]=useState(false);
  const dialog=useRef<HTMLElement>(null);
  useEffect(()=>{
    const prior=document.activeElement as HTMLElement|null;
    const controls=()=>[...dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"]') ?? []];
    controls()[0]?.focus();
    const key=(event:KeyboardEvent)=>{if (event.key === 'Escape') {event.preventDefault();onCancel();}if (event.key === 'Tab') {const buttons=controls(),first=buttons[0],last=buttons.at(-1);if (event.shiftKey && document.activeElement === first) {event.preventDefault();last?.focus();}else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first?.focus();}}};
    document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);prior?.focus();};
  },[onCancel]);
  const {operation,object_type,title}=confirmation;
  const isClear=operation === 'clear', permanent=operation === 'purge' || operation === 'empty_trash';
  const heading=isClear ? '清除当前应用记录' : operation === 'empty_trash' ? '清空最近删除' : permanent ? '永久删除' : object_type === 'chat' ? '删除聊天' : '删除草案';
  const button=isClear ? '永久清除并重新开始' : permanent ? operation === 'empty_trash' ? '清空最近删除' : '永久删除' : '移入最近删除';
  return createPortal(<div className="management-modal-backdrop" onClick={event=>{if (event.target === event.currentTarget && !submitting) onCancel();}}><section ref={dialog} role="dialog" aria-modal="true" aria-labelledby="management-confirm-title" className="management-modal">
    <h2 id="management-confirm-title">{heading}</h2>
    {isClear ? <><p>实际保存位置：</p><p className="data-path">{state.management?.database_path ?? '暂时无法核实保存位置'}</p><RecordCounts state={state}/><p>本次清除当前这份记录中的全部聊天及消息、全部已保存草案、最近删除、既有保存与删除核对状态、当前需求、评估和未提交方案，以及当前浏览器中属于这份记录的应用引用和页面缓存。</p><p>本次保留：{keptRecords}</p><p className="business-warning">将永久清除上述当前应用记录，并重新开始。应用内无法恢复。其他数据位置、工程记录、导出及备份不会清除。</p></> : operation === 'empty_trash' ? <><p>永久清除最近删除中的全部聊天和草案？</p><p>正常区中的聊天和草案保留。只保留核对历史事实和阻止旧操作重建所必需的最少状态，不保留需求、金额或正文。</p></> : <><p>{operation === 'trash' ? '将' : '永久删除'}“{title}”{operation === 'trash' ? '移入最近删除？' : '？'}</p>{confirmation.summary && <p className="business-note">{confirmation.summary}</p>}{operation === 'trash' ? object_type === 'chat' ? <p>消息仍保存在本机，可以恢复。这段聊天的当前选型及未保存方案将结束；已保存草案保留，其中可能包含相同的需求和自述。</p> : <p>草案可以恢复。聊天保留，其中已有的摘要、编号和引用内容不会一并删除。</p> : <p>正文和回收内容将移除，应用内无法恢复。只保留核对历史事实和阻止旧操作重建所必需的最少状态，不保留需求、金额或正文。</p>}</>}
    {(isClear || permanent) && <p className="business-note">“永久删除”表示应用内无法恢复，不承诺磁盘安全擦除，也不清除外部副本。</p>}
    <div className="management-confirm-buttons"><button disabled={submitting} onClick={onCancel}>取消</button><button className="danger-button" disabled={submitting || !state.management || state.managementBusy} onClick={()=>{setSubmitting(true);onConfirm();}}>{button}</button></div>
  </section></div>,document.body);
}

function RecordCounts({state}:{state:ViewState}) {
  const counts=state.management?.counts;
  return counts ? <div className="record-counts" aria-label="完整存档数量"><p>正常记录：{counts.conversations} 条聊天 · {counts.messages} 条消息 · {counts.quotes} 份草案</p><p>最近删除：{counts.trash_conversations} 条聊天 · {counts.trash_messages} 条消息 · {counts.trash_quotes} 份草案</p></div> : <p>完整存档数量暂时无法核实。</p>;
}

export function DataManagementPanel({state,controller,onClose,onConfirm,onOpenQuote}:{state:ViewState;controller:SessionController;onClose:()=>void;onConfirm:(confirmation:ManagementConfirmation)=>void;onOpenQuote:(id:string)=>void}) {
  const [copyMessage,setCopyMessage]=useState('');
  useEffect(()=>{void controller.refreshManagement().then(verified=>{if (verified) void controller.refreshTrash();});},[controller]);
  const confirm=(operation:ManagementOperation,object_type:'chat'|'quote'|null=null,object_id:string|null=null,title='')=>{
    if (!state.management) return;
    onConfirm({operation,object_type,object_id,title,revision:state.management.revision,scope:controller.managementScope()});
  };
  const clearConfirm=async()=>{if (await controller.refreshManagement()) {const current=controller.getSnapshot().management!;onConfirm({operation:'clear',object_type:null,object_id:null,title:'当前应用记录',revision:current.revision,scope:controller.managementScope()});}};
  const restoredObjectActive=(object_type:'chat'|'quote'|null,object_id:string|null)=>!!object_id && (object_type === 'chat' ? state.sessions.some(item=>item.id === object_id) : state.quoteRecords.some(item=>item.quote_id === object_id)) && !state.managementActions.some(action=>['unknown','submitting'].includes(action.status) && (!action.object_id || action.object_type === object_type && action.object_id === object_id));
  const renderedScope=state.management ? `${state.management.dataset_id}:${state.management.location_id}:${state.management.epoch}` : '';
  return <section className="restored-result data-management-panel" id="data-management-region" aria-label="数据管理">
    <div className="quote-panel-heading"><p>数据管理</p><button onClick={onClose}>收起</button></div>
    {state.managementLoading && <p className="business-note" role="status">正在核实本机保存位置与完整数量…</p>}
    {state.management ? <><h3>本机保存位置</h3><p className="data-path">{state.management.database_path}</p><button className="management-button" onClick={async()=>{try {await navigator.clipboard.writeText(state.management!.database_path);setCopyMessage('保存路径已复制');} catch {setCopyMessage('复制失败，请手动复制完整路径。');}}}>复制路径</button>{copyMessage && <p role="status">{copyMessage}</p>}<RecordCounts state={state}/></> : <p role="status" className="business-warning">{state.managementMessage || '暂时无法核实保存位置'}</p>}
    <button className="management-button" disabled={state.managementLoading} onClick={()=>void controller.refreshManagement().then(verified=>{if (verified) void controller.refreshTrash();})}>重新核实</button>
    <div className="data-lifecycle"><p>聊天、已保存草案和最近删除内容持久保留，退出和同位置重启后仍在。最近删除不会自动清除。</p><p>当前需求、评估和未提交方案主要在服务进程内；服务重启后需重新核对。关闭页面不等于停止服务。</p><p>刷新、切换、重开历史或恢复对象，都不会恢复旧保存许可、未提交方案或进行中的请求。恢复不会自动恢复另一类对象。</p><p>浏览器引用丢失不等于本机草案删除，完整保存记录仍从本机存档读取。改用另一数据位置不等于原位置已清理。</p><p>聊天与草案分别管理。删除一类对象会保留另一类对象及其中已出现的需求、自述、摘要、编号和引用文字；历史文字与保留的快照不会改写。</p></div>
    <h3>最近删除</h3><p className="business-note">内容仍保存在本机，不会自动清除。恢复保留原消息，或草案原编号、原保存时间和完整快照。</p>
    {state.trashMessage && <p role="status" className="business-warning">{state.trashMessage}</p>}
    {!state.trashMessage && !state.managementLoading && state.management && state.management.counts.trash_conversations + state.management.counts.trash_quotes === 0 && state.trash.length === 0 && <p className="business-note">最近删除为空。</p>}
    {state.trash.map(item=><article className="trash-item" key={`${item.object_type}:${item.object_id}`}><div><span className="object-kind">{item.object_type === 'chat' ? '聊天' : '草案'}</span><strong>{item.title || item.object_id}</strong></div>{item.object_type === 'quote' && <p className="quote-number">{item.object_id}</p>}<p className="business-note">移入时间：{savedTime(item.deleted_at)}{item.saved_at ? ` · 原保存时间：${savedTime(item.saved_at)}` : ''}</p><div className="saved-quote-actions"><button disabled={!state.management || state.managementLoading || state.managementBusy || !controller.canManageObject(item.object_type,item.object_id)} onClick={()=>{const current=controller.getSnapshot().management;if (current && renderedScope === controller.managementScope()) void controller.manage('restore',item.object_type,item.object_id,current.revision,renderedScope);}}>恢复</button><button disabled={!state.management || state.managementLoading || state.managementBusy || !controller.canManageObject(item.object_type,item.object_id)} onClick={()=>confirm('purge',item.object_type,item.object_id,item.title || item.object_id)}>永久删除</button></div></article>)}
    <button className="management-button" disabled={!state.management || state.managementBusy || !state.trash.length} onClick={()=>confirm('empty_trash')}>清空最近删除</button>
    {state.managementActions.filter(action=>action.status === 'unknown' || action.status === 'submitting' || action.operation === 'restore' && action.status === 'completed').map(action=><div className="management-result" key={action.action_ref} role="status"><p>{action.message}</p>{action.object_id && <p className="business-note">{action.object_type === 'chat' ? '聊天' : '草案'} · {state.sessions.find(item=>item.id === action.object_id)?.title ?? state.trash.find(item=>item.object_type === action.object_type && item.object_id === action.object_id)?.title ?? action.object_id}</p>}{action.status === 'unknown' && <button className="management-button" disabled={state.managementBusy || !state.management} onClick={()=>void controller.resolveManagement(action.action_ref)}>核对此次管理结果</button>}{action.status === 'completed' && action.operation === 'restore' && action.object_id && (restoredObjectActive(action.object_type,action.object_id) ? <button className="management-button" onClick={()=>{if (action.object_type === 'chat') {onClose();void controller.switchTo(action.object_id!);} else onOpenQuote(action.object_id!);}}>查看</button> : <p className="business-note">这是当时恢复完成的历史结果。该对象目前不在可打开的正常记录中，请核对最近删除或当前状态。</p>)}</div>)}
    <div className="clear-records-block"><h3>清除当前应用记录</h3><p>清除服务当前实际使用的这份记录，完成后进入空白新对话。确认前会列出实际位置、完整数量、清除范围和保留项。</p><button className="danger-button" disabled={!state.management || state.managementBusy || state.managementLoading} onClick={()=>void clearConfirm()}>清除当前应用记录</button><p className="business-note">本次保留：{keptRecords}</p></div>
  </section>;
}
