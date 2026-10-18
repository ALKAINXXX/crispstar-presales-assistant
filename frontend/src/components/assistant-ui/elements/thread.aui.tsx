// Adapted from assistant-ui MIT default Thread at 3542d602272a62eddeb8989befc910841c267022.
// Root/Viewport/Messages/Composer/ScrollToBottom remain official primitives.
import { type ComponentType, type FC, createContext, useContext } from 'react';
import { ActionBarPrimitive, AuiIf, ComposerPrimitive, MessagePrimitive, ThreadPrimitive, useAuiState } from '@assistant-ui/react';
import { MarkdownTextPrimitive } from '@assistant-ui/react-markdown';
import '@assistant-ui/react-markdown/styles/dot.css';
import remarkGfm from 'remark-gfm';
import { ArrowDownIcon, ArrowUpIcon, CopyIcon, CheckIcon } from 'lucide-react';
import { BusinessResponseView } from '@/business/BusinessResponse';
import type { ChatResponse, CurrentTask, Proposal, SaveAction } from '@/business/types';
import { cn } from '@/lib/utils';
import { safeHref } from '../utils/href';

export const CurrentTaskContext = createContext<CurrentTask|null>(null);
export const CurrentProposalContext = createContext<Proposal|null>(null);
export const SaveActionsContext = createContext<{actions:SaveAction[];onSave:(proposalId:string)=>void;disabled:boolean}|null>(null);
export type ThreadProps = {components?:{Welcome?:ComponentType;AssistantMessage?:ComponentType};autoFocus?:boolean};
const ComponentsContext = createContext<NonNullable<ThreadProps['components']>>({});
export const Thread:FC<ThreadProps> = ({components={},autoFocus=true}) => {
  const isEmpty = useAuiState(s=>s.thread.messages.length === 0 && !s.thread.isLoading);
  const viewportIdentity = useAuiState(s=>s.thread.messages[0]?.id ?? 'empty-thread');
  // Live first turns must keep the answer beginning visible. Empty history
  // also enables the official initialization reset before a history load.
  const initializeFromHistory = useAuiState(s=>s.thread.messages.every(message=>message.metadata.custom?.historical === true));
  const Welcome = components.Welcome ?? ThreadWelcome;
  return <ComponentsContext.Provider value={components}><ThreadPrimitive.Root className="aui-root aui-thread-root bg-background @container flex h-full flex-col" style={{['--thread-max-width' as string]:'66rem'}}>
    <ThreadPrimitive.Viewport key={viewportIdentity} turnAnchor="top" scrollToBottomOnThreadSwitch={false} scrollToBottomOnInitialize={initializeFromHistory} data-slot="aui_thread-viewport" className="aui-thread-viewport relative flex flex-1 flex-col overflow-x-hidden overflow-y-auto scroll-smooth">
      <div className={cn('thread-inner mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-6 pt-6',isEmpty && 'justify-center')}>
        <AuiIf condition={s=>s.thread.messages.length === 0 && !s.thread.isLoading}><Welcome/></AuiIf>
        <AuiIf condition={s=>s.thread.isLoading}><p role="status" className="text-muted-foreground p-4">正在读取聊天…</p></AuiIf>
        <div data-slot="aui_message-group" className="message-group mb-8 flex flex-col gap-y-8 empty:hidden"><ThreadPrimitive.Messages>{()=> <ThreadMessage/>}</ThreadPrimitive.Messages></div>
        <ThreadPrimitive.ViewportFooter className="aui-thread-viewport-footer bg-background sticky bottom-0 mt-auto flex flex-col gap-3 pb-5 pt-3">
          <ThreadPrimitive.ScrollToBottom asChild><button className="scroll-bottom absolute -top-10 self-center rounded-full border bg-white px-3 py-2 text-xs shadow-sm disabled:invisible" aria-label="回到底部"><ArrowDownIcon size={15}/>回到底部</button></ThreadPrimitive.ScrollToBottom>
          <Composer autoFocus={autoFocus}/>
          <p className="composer-note">合成产品资料 · 订阅基价供参考 · 点击确认后保存为内部草案</p>
        </ThreadPrimitive.ViewportFooter>
      </div>
    </ThreadPrimitive.Viewport>
  </ThreadPrimitive.Root></ComponentsContext.Provider>;
};
const ThreadMessage:FC = () => {
  const role = useAuiState(s=>s.message.role);
  const Custom = useContext(ComponentsContext).AssistantMessage ?? AssistantMessage;
  return role === 'user' ? <UserMessage/> : <Custom/>;
};
const ThreadWelcome = () => <div className="thread-welcome"><p className="text-2xl font-semibold">今天想了解什么？</p><p>可以询问产品前提、套餐价格，或说明必须满足的需求。</p></div>;
const Composer:FC<{autoFocus:boolean}> = ({autoFocus}) => {
  const running = useAuiState(s=>s.thread.isRunning);
  return <ComposerPrimitive.Root className="aui-composer-root flex w-full flex-col rounded-2xl border bg-muted/30 p-2 focus-within:border-foreground/35">
    <ComposerPrimitive.Input placeholder="说说你的问题或需求…" className="aui-composer-input max-h-48 min-h-12 w-full resize-none bg-transparent px-3 py-2 text-base leading-6 outline-none" rows={1} autoFocus={autoFocus} enterKeyHint="send" aria-label="聊天输入" maxLength={20000}/>
    <div className="flex justify-end"><ComposerPrimitive.Send asChild disabled={running}><button className="composer-send flex items-center gap-1 rounded-xl bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-45" aria-label="发送">{running ? '处理中' : '发送'}<ArrowUpIcon size={16}/></button></ComposerPrimitive.Send></div>
  </ComposerPrimitive.Root>;
};
const UserMessage = () => <MessagePrimitive.Root data-slot="aui_user-message-root" className="user-message flex justify-end px-1" data-role="user"><div className="max-w-[85%] rounded-2xl bg-muted px-4 py-3 whitespace-pre-wrap break-words"><MessagePrimitive.Parts/></div></MessagePrimitive.Root>;
const MarkdownText = () => <MarkdownTextPrimitive remarkPlugins={[remarkGfm]} className="aui-md" components={{a:({href,children})=>{const safe = safeHref(href);return safe ? <a href={safe} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;},img:({alt})=><span>{alt}</span>}}/>;
const AssistantMessage = () => {
  const messageId = useAuiState(s=>s.message.id);
  const metadata = useAuiState(s=>s.message.metadata.custom);
  const response = metadata?.response as ChatResponse|undefined;
  const currentTask = useContext(CurrentTaskContext);
  const currentProposal = useContext(CurrentProposalContext);
  const saving = useContext(SaveActionsContext);
  const hasLaterSave = response?.business?.proposal && saving?.actions.some(action=>action.proposal_id === response.business!.proposal!.proposal_id && action.session_id === response.session_id);
  return <MessagePrimitive.Root id={`message-${messageId}`} tabIndex={-1} data-message-id={messageId} data-slot="aui_assistant-message-root" data-role="assistant" data-card-session-id={response?.session_id} data-card-turn-id={response?.business?.turn_id} className="assistant-message min-w-0 px-1">
    {metadata?.historical === true && <div className="history-label">已存聊天 · 当时文字；后续保存结果见“保存记录”</div>}
    {hasLaterSave && <div className="history-label">以下为保存前生成的回答，后续保存结果见下方状态及“保存记录”。</div>}
    <div data-section="answer" data-ui-part="answer"><MessagePrimitive.Parts components={{Text:MarkdownText}}/></div>
    {response && <BusinessResponseView response={response} currentTask={currentTask} currentProposal={currentProposal} saveActions={saving?.actions} onSave={saving?.onSave} saveDisabled={saving?.disabled}/>}
    <ActionBarPrimitive.Root hideWhenRunning autohide="always" className="message-actions mt-2 flex"><ActionBarPrimitive.Copy asChild><button className="copy-message" aria-label="复制回答"><AuiIf condition={s=>s.message.isCopied}><CheckIcon size={14}/></AuiIf><AuiIf condition={s=>!s.message.isCopied}><CopyIcon size={14}/></AuiIf>复制</button></ActionBarPrimitive.Copy></ActionBarPrimitive.Root>
  </MessagePrimitive.Root>;
};
