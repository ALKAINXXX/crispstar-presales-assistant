// Fixed geometry isolates the real runtime's scroll triggers. Physical
// viewport and animation behavior are checked separately in the browser.
import { act, fireEvent, render } from '@testing-library/react';
import { AssistantRuntimeProvider, useAuiState, useExternalStoreRuntime } from '@assistant-ui/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { convertMessage } from '@/business/adapter';
import type { ViewMessage } from '@/business/types';
import { Thread } from './thread.aui';

const messages:ViewMessage[]=[
  {id:'first-user',role:'user',content:'需要 API，帮我算价。',createdAt:new Date()},
  {id:'first-answer',role:'assistant',content:'还缺实际购买席位，暂不能计算订阅总价。请明确购买多少席。',createdAt:new Date()},
];
function RuntimeThread({threadId,messages,isLoading=false,isRunning=false}:{threadId:string;messages:ViewMessage[];isLoading?:boolean;isRunning?:boolean}) {
  const runtime=useExternalStoreRuntime({messages,convertMessage,isLoading,isRunning,onNew:async()=>{},adapters:{threadList:{
    threadId,isLoading:false,threads:['server-first','server-second'].map(id=>({id,remoteId:id,title:id,status:'regular' as const})),
    onSwitchToNewThread:async()=>{},onSwitchToThread:async()=>{},
  }}});
  return <AssistantRuntimeProvider runtime={runtime}><SelectedThread/><Thread autoFocus={false}/></AssistantRuntimeProvider>;
}
const SelectedThread=()=> <output data-testid="selected-thread">{useAuiState(state=>state.threads.mainThreadId)}</output>;

let frames:Map<number,FrameRequestCallback>;
beforeEach(()=>{
  frames=new Map();let nextId=0;
  vi.stubGlobal('requestAnimationFrame',(callback:FrameRequestCallback)=>{frames.set(++nextId,callback);return nextId;});
  vi.stubGlobal('cancelAnimationFrame',(id:number)=>frames.delete(id));
});
afterEach(()=>vi.unstubAllGlobals());
async function settleFrames() {
  for (let pass=0;pass<5;pass++) await act(async()=>{
    await Promise.resolve();
    const pending=[...frames.values()];frames.clear();
    pending.forEach(callback=>callback(performance.now()));
  });
}
function measuredViewport(container:HTMLElement,height=1400) {
  const viewport=container.querySelector<HTMLElement>('[data-slot="aui_thread-viewport"]')!;
  Object.defineProperties(viewport,{scrollHeight:{configurable:true,value:height},clientHeight:{configurable:true,value:500}});
  const scroll=vi.spyOn(viewport,'scrollTo').mockImplementation((options:ScrollToOptions|number)=>{
    viewport.scrollTop=Math.max(0,Math.min(typeof options === 'number' ? options : Number(options.top ?? 0),viewport.scrollHeight-viewport.clientHeight));
    viewport.dispatchEvent(new Event('scroll'));
  });
  return {viewport,scroll};
}

it('keeps the first answer reading position when a temporary chat receives its server session identity',async()=>{
  const view=render(<RuntimeThread threadId="new:0" messages={messages}/>);
  const {viewport,scroll}=measuredViewport(view.container);
  await settleFrames();
  act(()=>{viewport.scrollTop=160;viewport.dispatchEvent(new Event('scroll'));});
  scroll.mockClear();
  view.rerender(<RuntimeThread threadId="server-first" messages={messages}/>);
  await settleFrames();
  expect(view.getByTestId('selected-thread').textContent).toBe('server-first');
  expect(viewport.scrollTop).toBe(160);
  expect(scroll).not.toHaveBeenCalled();
  expect(view.container.querySelector('[data-section="answer"]')?.textContent).toContain('还缺实际购买席位');
});

it('keeps the first live answer in view when a nonoverflowing first user message grows into a long response',async()=>{
  const view=render(<RuntimeThread threadId="new:0" messages={[]}/>);
  let {viewport,scroll}=measuredViewport(view.container,500);
  await settleFrames();
  view.rerender(<RuntimeThread threadId="new:0" messages={[messages[0]]} isRunning/>);
  ({viewport,scroll}=measuredViewport(view.container,500));
  await settleFrames();
  expect(viewport.scrollTop).toBe(0);
  scroll.mockClear();
  Object.defineProperty(viewport,'scrollHeight',{configurable:true,value:1400});
  view.rerender(<RuntimeThread threadId="server-first" messages={messages} isRunning/>);
  await settleFrames();
  view.rerender(<RuntimeThread threadId="server-first" messages={messages}/>);
  await settleFrames();
  expect(viewport.scrollTop).toBe(0);
  expect(scroll.mock.calls).not.toContainEqual([expect.objectContaining({top:1400})]);
});

it('does not carry a short restored history bottom-scroll intent into the first live turn of a new chat',async()=>{
  const history=messages.map(message=>({...message,historical:true}));
  const view=render(<RuntimeThread threadId="server-first" messages={history}/>);
  let {viewport}=measuredViewport(view.container,500);
  await settleFrames();
  view.rerender(<RuntimeThread threadId="new:1" messages={[]}/>);
  ({viewport}=measuredViewport(view.container,500));
  await settleFrames();
  view.rerender(<RuntimeThread threadId="new:1" messages={[messages[0]]} isRunning/>);
  ({viewport}=measuredViewport(view.container,500));
  await settleFrames();
  Object.defineProperty(viewport,'scrollHeight',{configurable:true,value:1400});
  view.rerender(<RuntimeThread threadId="server-second" messages={messages} isRunning/>);
  await settleFrames();
  view.rerender(<RuntimeThread threadId="server-second" messages={messages}/>);
  await settleFrames();
  expect(viewport.scrollTop).toBe(0);
});

it('still initializes loaded history at the end and retains the explicit scroll-to-bottom action',async()=>{
  const view=render(<RuntimeThread threadId="server-first" messages={messages}/>);
  let {viewport}=measuredViewport(view.container);
  await settleFrames();
  act(()=>{viewport.scrollTop=120;viewport.dispatchEvent(new Event('scroll'));});
  view.rerender(<RuntimeThread threadId="server-second" messages={[]} isLoading/>);
  await settleFrames();
  view.rerender(<RuntimeThread threadId="server-second" messages={messages.map(message=>({...message,id:`second-${message.id}`,historical:true}))}/>);
  ({viewport}=measuredViewport(view.container));
  await settleFrames();
  expect(viewport.scrollTop).toBe(900);
  act(()=>{viewport.scrollTop=120;viewport.dispatchEvent(new Event('scroll'));});
  const button=view.getByRole('button',{name:'回到底部'}) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  fireEvent.click(button);
  expect(viewport.scrollTop).toBe(900);
});
