import type { AppendMessage, ThreadMessageLike } from '@assistant-ui/react';
import type { Catalog, ChatResponse, CurrentTask, DataManagement, ManagementAction, ManagementOperation, Proposal, Quote, QuoteActionResult, QuoteSummary, SaveAction, Session, SessionRead, TrashItem, ViewMessage } from './types';
import { isQuote, persistSaveReferences, readQuoteAction, restoreSaveReferences, SAVE_REFERENCES_KEY } from './quotes';

export const UI_CONTRACT = 'assistant-ui-external-store-v1';
export const convertMessage = (message: ViewMessage): ThreadMessageLike => ({
  id: message.id, role: message.role, createdAt: message.createdAt,
  content: [{ type: 'text', text: message.response?.business?.presentation?.answer_markdown ?? message.content }],
  metadata: { custom: { response: message.response, historical: !!message.historical } },
});

export function money(value: unknown): string {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return '金额暂无法展示';
  const amount = BigInt(value);
  return `${(amount / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${(amount % 100n).toString().padStart(2, '0')}`;
}

export interface ChatAvailability {available:boolean;message:string;remaining_turns?:number;remaining_calls?:number}
export interface ViewState { activeKey: string; sessionId: string | null; messages: ViewMessage[]; currentTask: CurrentTask | null; currentProposal: Proposal | null; catalog: Catalog | null; restored: boolean; loading: boolean; busy: boolean; notice: string; sessions: Session[]; sessionsLoading: boolean; health: string; chatAvailability:ChatAvailability; saveActions:SaveAction[];saveRecordsLoading:boolean;quoteLookup:{loading:boolean;quote:Quote|null;message:string};quoteRecords:QuoteSummary[];quoteRecordsMessage:string;management:DataManagement|null;managementLoading:boolean;managementMessage:string;managementActions:ManagementAction[];managementBusy:boolean;trash:TrashItem[];trashMessage:string }
type ViewEvent = 'refresh'|'switch'|'history_reenter'|'new_chat'|'view_exit';
type Flight = {requestSession: string|null; responseSession?: string};
export class SessionController {
  private state: ViewState;
  private listeners = new Set<() => void>();
  private flights = new Map<string, Flight>();
  private epoch = 0;
  private sessionEpoch = 0;
  private healthEpoch = 0;
  private viewId = crypto.randomUUID();
  private viewActive = true;
  private pendingInvalidations = new Map<string,ViewEvent>();
  private invalidationBarrier: Promise<boolean> = Promise.resolve(true);
  private lookupEpoch = 0;
  private saveRecordsFlight:Promise<void>|null = null;
  private saveActionChecks = new Map<string,Promise<void>>();
  private quoteDirectoryEpoch = 0;
  private trashReadEpoch = 0;
  private managementFlight:Promise<boolean>|null = null;
  private managementReadEpoch = 0;
  private scopeRequired = false;
  private sessionVersion:number|undefined;
  private saveReferencesKey = SAVE_REFERENCES_KEY;
  private sessionKey = 'assistant.sessionId';
  private managementKey = '';
  private syncCleanup:(()=>void)|null = null;
  private verifiedScope:DataManagement|null = null;
  private scopeFingerprint = () => this.verifiedScope ? `${this.verifiedScope.dataset_id}:${this.verifiedScope.location_id}:${this.verifiedScope.epoch}` : '';
  constructor(private fetcher: typeof fetch = (...args) => fetch(...args), private storage: Pick<Storage,'getItem'|'setItem'|'removeItem'> = localStorage) {
    const id = storage.getItem('assistant.sessionId');
    this.state = {activeKey: id ?? 'new:0', sessionId: id, messages: [], currentTask: null, currentProposal:null, catalog: null, restored: false, loading: false, busy: false, notice: '', sessions: [], sessionsLoading: true, health: '正在连接服务',chatAvailability:{available:true,message:''},saveActions:restoreSaveReferences(storage),saveRecordsLoading:false,quoteLookup:{loading:false,quote:null,message:''},quoteRecords:[],quoteRecordsMessage:'',management:null,managementLoading:false,managementMessage:'',managementActions:[],managementBusy:false,trash:[],trashMessage:''};
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private patch(values: Partial<ViewState>) {
    this.state = {...this.state, ...values};
    const unresolvedManagement=this.state.managementActions.some(action=>(action.status === 'submitting' || action.status === 'unknown' || this.state.managementBusy && action === this.state.managementActions.at(-1)) && (action.operation === 'clear' || action.object_type === 'chat' && action.object_id === this.state.sessionId));
    this.state.busy = !this.viewActive || (this.scopeRequired && !this.state.management) || !!this.state.management?.clearing || unresolvedManagement || this.state.loading || this.pendingInvalidations.size > 0 || this.flights.has(this.state.activeKey) || [...this.flights.values()].some(f => !f.requestSession || f.requestSession === this.state.sessionId || f.responseSession === this.state.sessionId) || this.state.saveActions.some(action=>action.session_id === this.state.sessionId && action.status === 'submitting');
    this.listeners.forEach(listener => listener());
  }
  private headers(session = false) {
    return {'Content-Type':'application/json',...(this.state.management ? {'X-Data-Epoch':this.state.management.epoch,'X-Data-Location':this.state.management.location_id} : {}),...(session && this.sessionVersion !== undefined ? {'X-Session-Version':String(this.sessionVersion)} : {})};
  }
  private resetLocal(notice:string,clearRecords = false) {
    this.epoch++;this.lookupEpoch++;this.sessionEpoch++;this.viewId=crypto.randomUUID();this.sessionVersion=undefined;
    this.pendingInvalidations.clear();this.storage.removeItem(this.sessionKey);
    if (clearRecords) {this.flights.clear();this.saveRecordsFlight=null;this.saveActionChecks.clear();this.quoteDirectoryEpoch++;this.trashReadEpoch++;}
    this.patch({activeKey:`new:${this.epoch}`,sessionId:null,messages:[],currentTask:null,currentProposal:null,catalog:null,restored:false,loading:false,notice,quoteLookup:{loading:false,quote:null,message:''},...(clearRecords ? {saveActions:[],quoteRecords:[],sessions:[],trash:[],managementBusy:false,saveRecordsLoading:false} : {})});
  }
  refreshManagement = ():Promise<boolean> => {
    if (this.managementFlight) return this.managementFlight;
    this.patch({managementLoading:true});
    const readEpoch=++this.managementReadEpoch;
    const reading = (async()=>{
      const abort=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;
      try {
        const response=await Promise.race([this.fetcher('/data-management',{signal:abort.signal}),new Promise<Response>((_resolve,reject)=>{timer=setTimeout(()=>{abort.abort();reject(new Error('保存位置读取超时。'));},15_000);})]);
        const data:DataManagement = await this.read(response,'保存位置读取');
        if (readEpoch !== this.managementReadEpoch) return false;
        if (!data.dataset_id || !data.location_id || !data.epoch || !Number.isSafeInteger(data.revision) || typeof data.database_path !== 'string' || !data.database_path.trim() || !data.counts || !['conversations','messages','quotes','trash_conversations','trash_messages','trash_quotes'].every(key=>Number.isSafeInteger(data.counts[key as keyof DataManagement['counts']]) && data.counts[key as keyof DataManagement['counts']] >= 0)) throw new Error('保存位置或完整数量返回信息不完整。');
        const prior=this.verifiedScope, changed=!!prior && (prior.dataset_id !== data.dataset_id || prior.epoch !== data.epoch || prior.location_id !== data.location_id);
        const revisionChanged=!!prior && prior.revision !== data.revision;
        const first=!prior;
        if (changed) {
          if (prior.dataset_id === data.dataset_id && prior.location_id === data.location_id) this.storage.removeItem(this.saveReferencesKey);
          this.resetLocal(prior.dataset_id === data.dataset_id && prior.location_id === data.location_id ? '当前应用记录已清除，可以重新开始。其他数据位置、工程记录和外部副本未处理。' : '服务已改用另一数据位置；原位置未因此清理。',true);
        }
        if (first || changed) {
          const location=`assistant.dataset.${encodeURIComponent(data.dataset_id)}.${encodeURIComponent(data.location_id)}`,prefix=`${location}.${encodeURIComponent(data.epoch)}`;
          this.saveReferencesKey=`${prefix}.quoteActions`;this.sessionKey=`${prefix}.sessionId`;this.managementKey=`${location}.managementActions`;
          const previousEpoch=this.storage.getItem(`${location}.epoch`);
          if (previousEpoch && previousEpoch !== data.epoch) {
            this.storage.removeItem(`${location}.${encodeURIComponent(previousEpoch)}.quoteActions`);this.storage.removeItem(`${location}.${encodeURIComponent(previousEpoch)}.sessionId`);
            let pending:ManagementAction[]=[];
            try {const saved=JSON.parse(this.storage.getItem(this.managementKey) ?? '[]');if (Array.isArray(saved)) pending=saved.filter(value=>value?.operation === 'clear' && value.action_ref === data.last_clear?.action_ref);} catch { /* No readable pending clear. */ }
            this.storage.setItem(this.managementKey,JSON.stringify(pending));
          }
          this.storage.setItem(`${location}.epoch`,data.epoch);
          // Legacy references can only be read for verification once. New
          // writes are scoped to the actual database identity and generation.
          const migrated=this.storage.getItem('assistant.w11.referencesMigrated');
          if (!migrated && first) {
            persistSaveReferences(this.storage,this.state.saveActions,this.saveReferencesKey);
            const legacy=this.storage.getItem('assistant.sessionId');if (legacy) this.storage.setItem(this.sessionKey,legacy);
            this.storage.removeItem(SAVE_REFERENCES_KEY);this.storage.removeItem('assistant.sessionId');this.storage.setItem('assistant.w11.referencesMigrated','true');
          }
          const id=this.storage.getItem(this.sessionKey);
          let actions:ManagementAction[]=[];
          try {const saved=JSON.parse(this.storage.getItem(this.managementKey) ?? '[]');if (Array.isArray(saved)) actions=saved.filter(value=>value && typeof value.action_ref === 'string').map(value=>({...value,status:'unknown',message:'管理操作结果待核对，请核对原操作。'}));} catch { /* No readable references. */ }
          this.patch({saveActions:restoreSaveReferences(this.storage,this.saveReferencesKey),managementActions:changed && prior?.dataset_id === data.dataset_id && prior.location_id === data.location_id ? this.state.managementActions.filter(action=>action.operation === 'clear' && action.action_ref === data.last_clear?.action_ref) : actions,...(first ? {sessionId:id,activeKey:id ?? `new:${this.epoch}`} : {})});
        }
        this.verifiedScope=data;this.patch({management:data,managementMessage:''});
        if (changed || revisionChanged) {
          const sessionsVerified=await this.refreshSessions();
          if (sessionsVerified && this.state.sessionId) {
            const session=this.state.sessions.find(value=>value.id === this.state.sessionId);
            if (!session) this.resetLocal('原聊天已不在正常记录中。已保存草案保留，可在保存记录查看。');
            else {
              const busy=[...this.flights.values()].some(flight=>!flight.requestSession || flight.requestSession === session.id || flight.responseSession === session.id);
              const persisted=this.state.messages.at(-1)?.id;
              if (!busy && !this.state.loading && ((this.sessionVersion !== undefined && session.version !== this.sessionVersion) || (session.last_message_id !== undefined && String(session.last_message_id ?? '') !== String(persisted ?? '')))) await this.switchTo(session.id);
            }
          }
          await this.refreshQuoteDirectory();void this.refreshTrash();
          const quote=this.state.quoteLookup.quote;
          if (quote) {
            const existing=this.state.quoteRecords.find(value=>value.quote_id === quote.quote_id);
            if (existing) this.patch({quoteLookup:{...this.state.quoteLookup,quote:{...quote,source_conversation_status:existing.source_conversation_status}}});
            else if (!this.state.quoteRecordsMessage) void this.lookupQuote(quote.quote_id);
          }
          for (const action of this.state.saveActions) {
            const id=action.quote?.quote_id ?? action.quote_id;
            if (action.status === 'saved' && id && !this.state.quoteRecords.some(value=>value.quote_id === id) && !this.state.quoteRecordsMessage) void this.checkSaveAction(action);
            else if (action.status === 'saved' && action.quote && id) {
              const existing=this.state.quoteRecords.find(value=>value.quote_id === id);
              if (existing) this.updateSaveAction(action,{quote:{...action.quote,source_conversation_status:existing.source_conversation_status}});
            }
          }
        }
        return true;
      } catch(error) {
        if (readEpoch !== this.managementReadEpoch) return false;
        this.patch({management:null,managementMessage:`暂时无法核实保存位置。${error instanceof Error ? error.message : ''}`});return false;
      } finally {clearTimeout(timer);this.patch({managementLoading:false});}
    })().finally(()=>{if (this.managementFlight === reading) this.managementFlight=null;});
    this.managementFlight=reading;return reading;
  };
  startSync = () => {
    if (this.syncCleanup) return this.syncCleanup;
    const sync=()=>{if (this.viewActive) void this.refreshManagement();};
    const storage=(event:StorageEvent)=>{
      if (event.key === this.saveReferencesKey) {
        const existing=new Set(this.state.saveActions.map(action=>`${action.session_id}:${action.action_ref}`));
        const restored=restoreSaveReferences(this.storage,this.saveReferencesKey).filter(action=>!existing.has(`${action.session_id}:${action.action_ref}`));
        if (restored.length) this.patch({saveActions:[...this.state.saveActions,...restored]});
      }
      if (event.key === this.managementKey) {
        try {const refs=JSON.parse(this.storage.getItem(this.managementKey) ?? '[]');if (Array.isArray(refs)) {const existing=new Set(this.state.managementActions.map(action=>action.action_ref));this.patch({managementActions:[...this.state.managementActions,...refs.filter(value=>value && typeof value.action_ref === 'string' && !existing.has(value.action_ref)).map(value=>({...value,status:'unknown' as const,message:'另一已打开页面的管理结果待核对，请核对原操作。'}))]});}} catch { /* Unreadable references do not assert a result. */ }
      }
      if (event.key?.startsWith('assistant.dataset.')) sync();
    };
    window.addEventListener('focus',sync);window.addEventListener('storage',storage);
    const timer=setInterval(sync,3_000);
    this.syncCleanup=()=>{window.removeEventListener('focus',sync);window.removeEventListener('storage',storage);clearInterval(timer);this.syncCleanup=null;};
    return this.syncCleanup;
  };
  refreshTrash = async () => {
    if (!this.state.management) return;
    const scope=this.scopeFingerprint(),epoch=++this.trashReadEpoch;
    try {const data=await this.read(await this.fetcher('/data-management/trash',{headers:this.headers()}),'最近删除读取');if (!Array.isArray(data.items)) throw new Error('最近删除返回结果不完整，暂时无法核实。');if (scope === this.scopeFingerprint() && epoch === this.trashReadEpoch) this.patch({trash:data.items,trashMessage:''});}
    catch(error) {if (scope === this.scopeFingerprint() && epoch === this.trashReadEpoch) this.patch({trashMessage:error instanceof Error ? error.message : '最近删除暂时无法读取。'});}
  };
  refreshQuoteDirectory = async () => {
    const scope=this.scopeFingerprint(),epoch=++this.quoteDirectoryEpoch;
    try {const data=await this.read(await this.fetcher('/quotes',{headers:this.headers()}),'完整草案目录读取');if (!Array.isArray(data.quotes)) throw new Error('完整草案目录返回结果不完整，暂时无法核实。');if (scope === this.scopeFingerprint() && epoch === this.quoteDirectoryEpoch) this.patch({quoteRecords:data.quotes,quoteRecordsMessage:''});}
    catch(error) {if (scope === this.scopeFingerprint() && epoch === this.quoteDirectoryEpoch) this.patch({quoteRecords:[],quoteRecordsMessage:error instanceof Error ? error.message : '完整草案目录暂时无法读取。'});}
  };
  canManageObject = (kind:'chat'|'quote',id:string) => {
    if (!this.state.management || this.state.managementBusy) return false;
    const session=kind === 'chat' ? id : this.state.quoteRecords.find(quote=>quote.quote_id === id)?.source_session_id ?? this.state.trash.find(item=>item.object_type === 'quote' && item.object_id === id)?.source_session_id;
    return !this.state.managementActions.some(action=>action.status === 'unknown' && (action.object_id === id || !action.object_id)) && ![...this.flights.values()].some(flight=>!flight.requestSession || flight.requestSession === session || flight.responseSession === session) && !this.state.saveActions.some(action=>action.session_id === session && ['submitting','checking'].includes(action.status));
  };
  private persistManagementActions() {
    if (!this.managementKey) return;
    const merged=new Map<string,ManagementAction>();
    try {const refs=JSON.parse(this.storage.getItem(this.managementKey) ?? '[]');if (Array.isArray(refs)) for (const value of refs) if (value && typeof value.action_ref === 'string') merged.set(value.action_ref,value);} catch { /* No readable references. */ }
    for (const action of this.state.managementActions) if (action.status === 'submitting' || action.status === 'unknown') merged.set(action.action_ref,action);else merged.delete(action.action_ref);
    this.storage.setItem(this.managementKey,JSON.stringify([...merged.values()].map(({action_ref,operation,object_type,object_id,expected_revision})=>({action_ref,operation,object_type,object_id,expected_revision}))));
  }
  private applyManagementReceipt(action:ManagementAction,value:unknown):ManagementAction {
    const result=value as ManagementAction;
    if (!result || result.action_ref !== action.action_ref || result.operation !== action.operation || result.object_type !== action.object_type || result.object_id !== action.object_id || !['completed','not_completed','unknown'].includes(result.status) || typeof result.message !== 'string') throw new Error('无法核实管理操作回执。');
    if (result.status === 'completed' && (result.dataset_id !== this.verifiedScope?.dataset_id || !result.epoch || !Number.isSafeInteger(result.revision))) throw new Error('无法核实管理结果所属的记录。');
    const message=result.status === 'completed' && action.operation === 'trash' ? action.object_type === 'chat' ? '聊天已移入最近删除，可恢复。已保存草案保留，其中可能仍有相同的需求和自述。' : '草案已移入最近删除，可恢复。聊天及其中已有的摘要、编号和引用文字保留。' : result.message;
    return {...action,...result,message:result.completed_count != null && result.remaining_count != null && result.remaining_count > 0 ? `${message} 已完成 ${result.completed_count} 项，剩余 ${result.remaining_count} 项。` : message};
  }
  manage = async (operation:ManagementOperation,object_type:'chat'|'quote'|null,object_id:string|null,expected_revision:number,expected_scope?:string) => {
    if (!this.state.management || this.state.managementBusy) return;
    const confirmedScope=this.managementScope();
    if (expected_scope && expected_scope !== this.managementScope()) {this.patch({notice:'当前保存位置或记录已变化，本次未执行。请重新核实并确认。'});return;}
    const all=operation === 'clear' || operation === 'empty_trash';
    const source=object_type === 'chat' ? object_id : this.state.quoteRecords.find(quote=>quote.quote_id === object_id)?.source_session_id ?? this.state.trash.find(item=>item.object_type === 'quote' && item.object_id === object_id)?.source_session_id;
    if ((all && (this.flights.size > 0 || this.state.managementActions.some(action=>action.status === 'unknown') || this.state.saveActions.some(action=>['submitting','checking'].includes(action.status)))) || (!all && object_type && object_id && !this.canManageObject(object_type,object_id))) {this.patch({notice:'相关操作仍在进行或结果待核对，请等实际操作结束并核对原结果后再管理数据。关闭页面或取消浏览器请求不表示数据库工作已结束。'});return;}
    const pending=this.state.saveActions.filter(action=>action.status === 'unknown' && (all || action.session_id === source));
    if (pending.length) {await Promise.all(pending.map(action=>this.checkSaveAction(action)));if (this.state.saveActions.some(action=>action.status === 'unknown' && (all || action.session_id === source))) {this.patch({notice:'相关保存结果仍待核对，请先在保存记录核对原动作，再执行删除或清理。'});return;}}
    if (!this.state.management || confirmedScope !== this.managementScope()) {this.patch({notice:'核对期间保存位置或记录已变化，本次未执行。请重新核实并确认。'});return;}
    const action:ManagementAction={action_ref:crypto.randomUUID().replaceAll('-',''),operation,object_type,object_id,expected_revision,status:'submitting',message:'正在处理并核实实际结果…'};
    this.patch({managementActions:[...this.state.managementActions,action],managementBusy:true});
    try {this.persistManagementActions();} catch {this.patch({managementBusy:false,managementActions:this.state.managementActions.filter(value=>value !== action),notice:'浏览器未能保留原管理动作，因此没有提交。'});return;}
    await this.requestManagement(action,false);
  };
  managementScope = () => this.state.management ? `${this.state.management.dataset_id}:${this.state.management.location_id}:${this.state.management.epoch}` : '';
  resolveManagement = async (ref:string) => {
    const action=this.state.managementActions.find(value=>value.action_ref === ref);
    if (action?.status === 'unknown' && !this.state.managementBusy) {this.patch({managementBusy:true});await this.requestManagement(action,true);}
  };
  private async requestManagement(action:ManagementAction,resolve:boolean) {
    const originalScope=this.scopeFingerprint(),originalLocation=this.verifiedScope?.location_id,originalDataset=this.verifiedScope?.dataset_id;
    let settled:ManagementAction;
    const abort=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;
    try {
      const response=await Promise.race([this.fetcher(resolve ? `/data-management/actions/${encodeURIComponent(action.action_ref)}` : '/data-management/actions',resolve ? {headers:this.headers(),signal:abort.signal} : {method:'POST',headers:this.headers(),body:JSON.stringify({action_ref:action.action_ref,operation:action.operation,object_type:action.object_type,object_id:action.object_id,expected_revision:action.expected_revision}),signal:abort.signal}),new Promise<Response>((_resolve,reject)=>{timer=setTimeout(()=>{abort.abort();reject(new Error('结果读取超时'));},15_000);})]);
      settled=this.applyManagementReceipt(action,await response.json());
      if (settled.status === 'completed' && response.status !== 200) throw new Error('未取得正式完成回执。');
    } catch {settled={...action,status:'unknown',message:'未取得可核实的管理结果，结果待核对。请核对原操作，不要再次提交。'};}
    finally {clearTimeout(timer);}
    const sameClear=action.operation === 'clear' && this.verifiedScope?.location_id === originalLocation && this.verifiedScope?.dataset_id === originalDataset && this.verifiedScope?.last_clear?.action_ref === action.action_ref;
    if (originalScope !== this.scopeFingerprint() && !sameClear) return;
    this.patch({managementActions:this.state.managementActions.map(value=>value.action_ref === action.action_ref ? settled : value),managementBusy:settled.status === 'completed',notice:settled.message});
    if (settled.status === 'completed') {
      if (action.operation === 'clear') {this.managementReadEpoch++;this.storage.removeItem(this.saveReferencesKey);this.resetLocal(settled.message,true);this.patch({managementBusy:true});}
      else if (action.operation === 'trash' && action.object_type === 'chat' && this.state.sessionId === action.object_id) this.resetLocal(settled.message);
      else if ((action.operation === 'trash' || action.operation === 'purge') && action.object_type === 'quote' && this.state.quoteLookup.quote?.quote_id === action.object_id) this.clearQuoteLookup();
      if (this.managementFlight) await this.managementFlight;
      await this.refreshManagement();await Promise.all([this.refreshSessions(),this.refreshQuoteDirectory(),this.refreshTrash()]);
    }
    this.patch({managementBusy:false});
    try {this.persistManagementActions();this.storage.setItem('assistant.dataset.changed',crypto.randomUUID());} catch { /* Server receipt remains authoritative. */ }
  }
  private async read(response: Response, action: string) {
    let data;
    try { data = await response.json(); } catch { throw new Error(`${action}没有返回可读取的结果（HTTP ${response.status}）。`); }
    if (!response.ok) throw new Error(`${action}未完成：${typeof data.detail === 'string' ? data.detail : typeof data.detail?.message === 'string' ? data.detail.message : `HTTP ${response.status}`}`);
    return data;
  }
  private updateSaveAction(action:SaveAction,values:Partial<SaveAction>) {
    this.patch({saveActions:this.state.saveActions.map(existing=>existing.action_ref === action.action_ref && existing.session_id === action.session_id ? {...existing,...values} : existing)});
  }
  // A real click calls this method. The submitted facts stay entirely on the
  // server; an old card can only name its Proposal, never supply a new object.
  saveProposal = async (proposalId:string) => {
    const proposal = this.state.currentProposal;
    if (!proposal || proposal.proposal_id !== proposalId || !proposal.save_available || proposal.session_id !== this.state.sessionId || this.state.busy) return;
    if (this.state.saveActions.some(action=>action.action_ref === proposal.action_ref && action.session_id === proposal.session_id)) return;
    const action:SaveAction = {session_id:proposal.session_id,action_ref:proposal.action_ref,proposal_id:proposal.proposal_id,started_at:new Date().toISOString(),status:'submitting',code:'submitting',message:'正在保存你确认的具体方案…',quote:null};
    const saveActions = [...this.state.saveActions,action];
    try { persistSaveReferences(this.storage,saveActions,this.saveReferencesKey); }
    catch { this.patch({notice:'浏览器未能保留原保存动作，因此没有提交。请允许本站使用本地存储后再点击保存。'}); return; }
    this.patch({saveActions,currentProposal:null});
    await this.requestSaveAction(action,false);
  };
  resolveSaveAction = async (proposalId:string,sessionId:string) => {
    const action = this.state.saveActions.find(value=>value.proposal_id === proposalId && value.session_id === sessionId);
    // Only a record persisted before a submit may reach the resolving endpoint.
    if (!action || action.status !== 'unknown' || !this.viewActive) return;
    await this.checkSaveAction(action);
  };
  // One finite pass per opening, over operations that were actually initiated.
  // This reads real results; it cannot submit a Proposal or revive its permit.
  refreshSaveRecords = ():Promise<void> => {
    if (this.saveRecordsFlight) return this.saveRecordsFlight;
    if (!this.viewActive) return Promise.resolve();
    const actions = [...this.state.saveActions].reverse().filter(action=>action.status !== 'submitting' && action.status !== 'checking');
    this.patch({saveRecordsLoading:true});
    const scope=this.scopeFingerprint();
    let next = 0;
    const worker = async () => {
      while (next < actions.length && this.viewActive) {
        const action = actions[next++];
        await this.checkSaveAction(action);
      }
    };
    const reading = Promise.all([this.refreshQuoteDirectory(),...Array.from({length:Math.min(2,actions.length)},worker)]).then(()=>{}).finally(()=>{
      if (this.saveRecordsFlight === reading) {this.saveRecordsFlight = null;if (scope === this.scopeFingerprint()) this.patch({saveRecordsLoading:false});}
    });
    this.saveRecordsFlight = reading;
    return reading;
  };
  private checkSaveAction(action:SaveAction):Promise<void> {
    const key = `${action.session_id}:${action.action_ref}`;
    const running = this.saveActionChecks.get(key);
    if (running) return running;
    const current = this.state.saveActions.find(value=>value.session_id === action.session_id && value.action_ref === action.action_ref);
    if (!current || current.status === 'submitting' || current.status === 'checking' || !this.viewActive) return Promise.resolve();
    this.updateSaveAction(current,{status:'checking',quote:null,message:'正在核对原保存动作…'});
    const checking = this.requestSaveAction(current,true).finally(()=>{
      if (this.saveActionChecks.get(key) === checking) this.saveActionChecks.delete(key);
    });
    this.saveActionChecks.set(key,checking);
    return checking;
  }
  private async readSaveAction(action:SaveAction) {
    const abort = new AbortController();
    let timer:ReturnType<typeof setTimeout>|undefined;
    try {
      return await Promise.race([
        this.fetcher(`/sessions/${encodeURIComponent(action.session_id)}/quote-actions/resolve`,{method:'POST',headers:this.headers(),body:JSON.stringify({action_ref:action.action_ref}),signal:abort.signal}).then(response=>readQuoteAction(response,action)),
        new Promise<QuoteActionResult>((_resolve,reject)=>{timer = setTimeout(()=>{abort.abort();reject(new Error('保存记录读取超时'));},15_000);}),
      ]);
    } finally { clearTimeout(timer); }
  }
  private async requestSaveAction(action:SaveAction,resolve:boolean) {
    const scope=this.scopeFingerprint();
    try {
      const result = resolve ? await this.readSaveAction(action) : await readQuoteAction(await this.fetcher(`/sessions/${encodeURIComponent(action.session_id)}/quotes`,{method:'POST',headers:this.headers(),body:JSON.stringify({action_ref:action.action_ref})}),action);
      if (scope !== this.scopeFingerprint()) return;
      this.updateSaveAction(action,{...result,restored:false});
    } catch(error) {
      if (scope !== this.scopeFingerprint()) return;
      const timedOut = resolve && error instanceof Error && error.message === '保存记录读取超时';
      this.updateSaveAction(action,{status:'unknown',code:resolve ? timedOut ? 'result_read_timeout' : 'result_read_unavailable' : 'receipt_unavailable',message:resolve ? `${timedOut ? '读取保存记录超时' : '暂时无法读取并核实原保存动作结果'}，保存结果待核对。请重试核对；不要重新保存。` : '未取得可核实的保存回执，保存结果待核对。请核对原动作结果；不要重新保存。',quote:null});
    }
  }
  lookupQuote = async (quoteId:string) => {
    const id = quoteId.trim(),epoch = ++this.lookupEpoch;
    if (!/^Q-[0-9A-F]{32}$/.test(id)) { this.patch({quoteLookup:{loading:false,quote:null,message:'编号格式不正确，请复制完整的草案编号（Q- 开头）。'}}); return; }
    this.patch({quoteLookup:{loading:true,quote:null,message:'正在按编号读取保存内容…'}});
    try {
      const quote:unknown = await this.read(await this.fetcher(`/quotes/${encodeURIComponent(id)}`,{headers:this.headers(),cache:'no-store'}),'草案读取');
      if (!isQuote(quote) || quote.quote_id !== id) throw new Error('读取结果无法核实，请稍后再查。');
      if (epoch === this.lookupEpoch) this.patch({quoteLookup:{loading:false,quote,message:''}});
    } catch(error) { if (epoch === this.lookupEpoch) this.patch({quoteLookup:{loading:false,quote:null,message:error instanceof Error ? error.message : '草案读取未完成，请稍后再查。'}}); }
  };
  clearQuoteLookup = () => { this.lookupEpoch++;this.patch({quoteLookup:{loading:false,quote:null,message:''}}); };
  async initialize() {
    this.scopeRequired=true;this.patch({});
    void this.refreshHealth();
    if (!await this.refreshManagement()) return;
    if (!await this.refreshSessions()) return;
    if (this.state.sessionId && this.state.sessions.some(session=>session.id === this.state.sessionId)) await this.switchTo(this.state.sessionId,'refresh');
    else if (this.state.sessionId) this.resetLocal('原聊天不在当前正常记录中；可在数据管理核对最近删除。');
  }
  refreshHealth = async () => {
    const epoch = ++this.healthEpoch;
    try {
      const data = await this.read(await this.fetcher('/health'), '服务连接');
      if (epoch !== this.healthEpoch) return;
      const availability = data.chat_availability;
      const chatAvailability = availability && typeof availability.available === 'boolean'
        ? {available:availability.available,message:typeof availability.message === 'string' && availability.message.trim() ? availability.message : availability.available ? '' : '本次验证的模型额度已耗尽，已暂停发送；仍可查看历史、方案与来源。',
            ...(Number.isSafeInteger(availability.remaining_turns) && availability.remaining_turns >= 0 ? {remaining_turns:availability.remaining_turns} : {}),
            ...(Number.isSafeInteger(availability.remaining_calls) && availability.remaining_calls >= 0 ? {remaining_calls:availability.remaining_calls} : {})}
        : this.state.chatAvailability;
      this.patch({health:'服务已连接',chatAvailability});
    } catch {
      // An exhausted budget is a service-level availability state, not a
      // connection failure. Keep its explicit explanation until health supplies
      // a newer availability decision; navigation alone cannot clear it.
      if (epoch === this.healthEpoch && this.state.chatAvailability.available) this.patch({health:'服务暂不可达'});
    }
  };
  async refreshSessions() {
    const epoch = ++this.sessionEpoch;
    try { const data = await this.read(await this.fetcher('/sessions',{headers:this.headers()}), '会话列表');if (!Array.isArray(data.sessions)) throw new Error('会话列表返回结果不完整，暂时无法核实。');if (epoch === this.sessionEpoch) {this.patch({sessions:data.sessions,sessionsLoading:false});return true;}return false; }
    catch { if (epoch === this.sessionEpoch) this.patch({sessionsLoading:false,notice:'聊天列表暂时无法读取，不能核实是否为空。请稍后重试。'});return false; }
  }
  private invalidateView(viewId:string,event:ViewEvent,keepalive = false) {
    const scope=this.scopeFingerprint();
    this.pendingInvalidations.set(viewId,event);
    this.patch({});
    const next = this.invalidationBarrier.then(async () => {
      if (scope !== this.scopeFingerprint()) {this.pendingInvalidations.delete(viewId);this.patch({});return true;}
      try {
        for (const [pendingView,pendingEvent] of this.pendingInvalidations) {
          await this.read(await this.fetcher('/proposal/view/invalidate',{method:'POST',headers:this.headers(),body:JSON.stringify({view_id:pendingView,event:pendingEvent}),keepalive}), '页面确认许可失效');
          this.pendingInvalidations.delete(pendingView);
        }
        this.patch({});
        return true;
      } catch {
        if (scope !== this.scopeFingerprint()) return true;
        this.patch({currentProposal:null,notice:'页面确认许可失效请求未完成，已暂停发送。请重新进入会话或新建聊天以重试。'});
        return false;
      }
    });
    this.invalidationBarrier = next;
    return next;
  }
  // The view nonce closes even a first /chat whose server session ID has not
  // reached the browser. A late response cannot reactivate that page view.
  leaveView = async () => {
    const viewId = this.viewId;
    this.viewActive = false;
    this.epoch++;
    this.pendingInvalidations.set(viewId,'view_exit');
    this.patch({currentProposal:null});
    // Start fetch synchronously in pagehide, before the browser can discard the
    // page. Server-side ordering, rather than an unload-time UI wait, protects
    // a concurrent /chat. The nonce also handles an unknown new session ID.
    try {
      await this.read(await this.fetcher('/proposal/view/invalidate',{method:'POST',headers:this.headers(),body:JSON.stringify({view_id:viewId,event:'view_exit'}),keepalive:true}), '离开页面');
      this.pendingInvalidations.delete(viewId);
    } catch { /* Keep the failed revocation gated for the next activation. */ }
    this.patch({});
  };
  resumeView = () => { this.viewActive = true; return this.state.sessionId ? this.switchTo(this.state.sessionId,'history_reenter') : this.newChat(); };
  newChat = async () => {
    const previousView = this.viewId;
    const previousSession = this.state.sessionId;
    const hasBackgroundWork = [...this.flights.values()].some(flight=>!flight.requestSession || flight.requestSession === previousSession || flight.responseSession === previousSession);
    const waitingNotice = hasBackgroundWork ? '上一轮仍可能在后台处理；切换页面不表示工作已结束，正在核对旧许可失效状态。' : '';
    this.viewId = crypto.randomUUID();
    const epoch = ++this.epoch;
    this.sessionVersion=undefined;this.storage.removeItem(this.sessionKey);
    this.clearQuoteLookup();
    this.patch({activeKey:`new:${epoch}`,sessionId:null,messages:[],currentTask:null,currentProposal:null,catalog:null,restored:false,loading:false,notice:waitingNotice});
    const invalidated = await this.invalidateView(previousView,'new_chat');
    if (invalidated && waitingNotice && epoch === this.epoch && this.state.activeKey === `new:${epoch}` && this.state.sessionId === null && this.state.notice === waitingNotice) this.patch({notice:''});
  };
  switchTo = async (id: string,event:ViewEvent = 'history_reenter') => {
    const previousView = this.viewId;
    this.viewId = crypto.randomUUID();
    const epoch = ++this.epoch;
    this.sessionVersion=undefined;this.storage.setItem(this.sessionKey, id);
    const switching = this.state.sessionId != null && this.state.sessionId !== id;
    this.clearQuoteLookup();
    this.patch({activeKey:id,sessionId:id,messages:[],currentTask:null,currentProposal:null,catalog:null,restored:false,loading:true,notice:'正在读取聊天…'});
    try {
      if (!await this.invalidateView(previousView,switching ? 'switch' : event) || epoch !== this.epoch) return;
      const data: SessionRead = await this.read(await this.fetcher(`/sessions/${encodeURIComponent(id)}`,{headers:this.headers()}), '聊天读取');
      if (epoch !== this.epoch) return;
      this.sessionVersion=data.session_version;
      this.patch({messages:(data.messages ?? []).filter(message => message.role === 'user' || message.role === 'assistant').map((message,index)=>({id:String(message.id ?? `history:${id}:${index}`),role:message.role as 'user'|'assistant',content:message.content,historical:true,createdAt:new Date()})), currentTask:data.current_task ?? null,currentProposal:null,catalog:data.catalog_context ?? null,restored:!!data.current_task,notice:data.recovery_notice ?? (data.current_task ? '已恢复聊天文字。当前选型可从“查看当前结果”查看。之前准备的方案不能从这里直接保存；已保存内容可在保存记录查看，需要再次保存时请重新准备方案。' : '已恢复聊天文字，仅供回看。已保存内容可在保存记录查看；需要再次保存时，请重新准备方案。')});
    } catch(error) { if (epoch === this.epoch) this.patch({notice:error instanceof Error ? error.message : '聊天读取未完成。'}); }
    finally { if (epoch === this.epoch) this.patch({loading:false}); }
  };
  send = async (input: AppendMessage | string) => {
    const text = (typeof input === 'string' ? input : input.content.filter(part=>part.type === 'text').map(part=>part.type === 'text' ? part.text : '').join('\n')).trim();
    if (!text || this.state.busy || !this.state.chatAvailability.available) return;
    const key = this.state.activeKey, epoch = this.epoch, requestSession = this.state.sessionId, viewId = this.viewId,requestScope=this.scopeFingerprint();
    const flight: Flight = {requestSession};
    this.flights.set(key,flight);
    this.patch({messages:[...this.state.messages,{id:crypto.randomUUID(),role:'user',content:text,createdAt:new Date()}],currentProposal:null,notice:''});
    try {
      const data: ChatResponse = await this.read(await this.fetcher('/chat',{method:'POST',headers:this.headers(!!requestSession),body:JSON.stringify({message:text,session_id:requestSession,view_id:viewId})}), '发送');
      if (typeof data.message !== 'string' || !data.session_id) throw new Error('回复缺少正式答案或会话身份，请重新读取聊天核对。');
      if (requestSession && data.session_id !== requestSession) throw new Error('回复的会话身份与本次提问不一致，请重新读取聊天核对。');
      flight.responseSession = data.session_id;
      if (epoch !== this.epoch) {if (requestScope === this.scopeFingerprint()) await this.invalidateView(viewId,'view_exit');return;}
      const budgetError = data.business?.errors?.find(error=>error.code === 'model_budget_exhausted');
      if (budgetError) {
        this.healthEpoch++;
        this.patch({health:'服务已连接',chatAvailability:{available:false,message:typeof budgetError.message === 'string' && budgetError.message.trim() ? budgetError.message : '本次验证的模型额度已耗尽，已暂停发送；仍可查看历史、方案与来源。'}});
      }
      this.sessionVersion=data.session_version;this.storage.setItem(this.sessionKey,data.session_id);
      const recovery = data.business?.recovery_notice;
      const notice = recovery && !data.business?.presentation?.answer_markdown.includes(recovery) ? recovery : '';
      const candidate = data.business?.proposal;
      const currentProposal = candidate?.session_id === data.session_id && candidate.view_id === viewId && candidate.turn_id === data.business?.turn_id ? candidate : null;
      this.patch({activeKey:data.session_id,sessionId:data.session_id,messages:[...this.state.messages,{id:String(data.message_id ?? data.business?.turn_id ?? crypto.randomUUID()),role:'assistant',content:data.message,response:data,createdAt:new Date()}],currentTask:data.business ? data.business.current_task : null,currentProposal,catalog:data.business?.catalog_context ?? data.business?.catalog ?? this.state.catalog,notice});
    } catch(error) {
      if (epoch !== this.epoch) return;
      this.patch({messages:[...this.state.messages,{id:crypto.randomUUID(),role:'assistant',content:error instanceof Error ? error.message : '请求未完成。',createdAt:new Date()}],notice:'本次请求未完成，无法据此判断后台工作已经结束。没有自动重发；可重新读取此会话，确认服务端实际保存的回复后继续。'});
    } finally {
      // Keep the sending flight gated until this zero-model check resolves.
      // Switching and reading remain available while the check completes.
      await this.refreshHealth();
      if (this.flights.get(key) === flight) this.flights.delete(key);
      this.patch({});
      if (requestScope === this.scopeFingerprint() && epoch !== this.epoch && this.viewActive && this.state.sessionId && (this.state.sessionId === requestSession || this.state.sessionId === flight.responseSession)) void this.switchTo(this.state.sessionId);
      void this.refreshSessions();
    }
  };
}
