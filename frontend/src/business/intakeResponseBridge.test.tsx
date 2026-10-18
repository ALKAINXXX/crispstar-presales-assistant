// An external fixture comes from actual controlled or live Orchestrator /
// SQLite output. Production Thread/Markdown/components exercise the bridge;
// this remains distinct from actual browser and model acceptance.
import { readFileSync } from 'node:fs';
import { render } from '@testing-library/react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { expect, it } from 'vitest';
import { Thread, CurrentTaskContext } from '@/components/assistant-ui/elements/thread.aui';
import { convertMessage, money } from './adapter';
import type { ChatResponse, CurrentTask, ViewMessage } from './types';

function RealThread({messages,current}:{messages:ViewMessage[];current:CurrentTask|null}) {
  const runtime=useExternalStoreRuntime({messages,convertMessage,onNew:async()=>{}});
  return <AssistantRuntimeProvider runtime={runtime}><CurrentTaskContext.Provider value={current}><Thread autoFocus={false}/></CurrentTaskContext.Provider></AssistantRuntimeProvider>;
}

it.skipIf(!process.env.INTAKE_RESPONSE_FIXTURE)('intake response, group feedback and stored history cross the production bridge',()=>{
  const payload:{users:string[];responses:ChatResponse[];history:{id?:string;role:'user'|'assistant';content:string}[];hidden?:string}=JSON.parse(readFileSync(process.env.INTAKE_RESPONSE_FIXTURE!,'utf8'));
  expect(payload.responses.length).toBeGreaterThan(0);
  const messages:ViewMessage[]=[];
  const view=render(<RealThread messages={[]} current={null}/>);
  let feedbackCount=0;
  for (const [index,response] of payload.responses.entries()) {
    expect(response.business?.presentation).toBeDefined();
    const answer=response.business!.presentation!.answer_markdown;
    // Formal storage also carries the existing text projection of comparison
    // and sources, so restored history preserves their business facts.
    expect(response.message.startsWith(answer)).toBe(true);
    if (response.business!.pricing) {
      const price=response.business!.pricing;
      for (const fact of [price.plan_name,`${price.paid_seats} 个付费席位`,`${price.months} 个月`,`${money(price.base_price_minor)} 元`,price.formula,'本项目不计算税、折扣、实施费等']) {
        expect(answer).toContain(fact);
        expect(response.message).toContain(fact);
      }
      const notice=response.business!.proposal?.notice;
      if (notice) {
        expect(answer).toContain(notice);
        expect(response.message).toContain(notice);
      }
    }
    const stored=payload.history.find(item=>String(item.id)===String(response.message_id) && item.role==='assistant');
    expect(stored?.content).toBe(response.message);
    messages.push({id:`user-${index}`,role:'user',content:payload.users[index],createdAt:new Date()},{id:String(response.message_id ?? `assistant-${index}`),role:'assistant',content:response.message,response,createdAt:new Date()});
    view.rerender(<RealThread messages={[...messages]} current={response.business?.current_task ?? null}/>);
    const answers=[...view.container.querySelectorAll('[data-section="answer"]')];
    expect(answers.at(-1)?.textContent?.replace(/\s/g,'')).toBe(response.business!.presentation!.answer_markdown.replace(/\s/g,''));
    const serviceFeedback=response.business!.presentation!.feedback ?? [];
    feedbackCount+=serviceFeedback.length;
    const reply=[...view.container.querySelectorAll('[data-role="assistant"]')].at(-1)!;
    const shown=reply.querySelector('[data-section="operation-feedback"]');
    if (serviceFeedback.length) expect(shown?.hasAttribute('open')).toBe(false);
    for (const item of serviceFeedback) {
      expect(shown?.textContent).toContain(item.message);
      expect(shown?.querySelector(`[data-group-id="${item.group_id}"]`)?.getAttribute('data-feedback-status')).toBe(item.status);
      // The full accepted receipt and quoted input stay reviewable here;
      // the formal body answers the task instead of repeating those receipts.
      if (item.status === 'accepted') expect(response.message).not.toContain(item.message);
    }
    expect(response.message).not.toMatch(/^\s*(已接受|已接收)[：:]/);
    if (response.business!.intent === 'unit_price') {
      expect(reply.querySelector('[data-section="proposal"],[data-section="current-task"],[data-section="match"]')).toBeNull();
      if (response.business!.unit_price) {
        expect(reply.querySelector('[data-section="catalog"]')?.textContent).toContain('单价查询不会自动改变当前选型');
        expect(answer).toContain(`${money(response.business!.unit_price.unit_price_minor)} 元／席／月`);
      }
    }
    if (payload.hidden) expect(view.container.textContent).not.toContain(payload.hidden);
  }
  expect(feedbackCount).toBeGreaterThan(0);
  // A page refresh creates a fresh runtime. Live structured projections and
  // their stable message cache are not carried into restored SQLite history.
  view.unmount();
  const historyView=render(<RealThread messages={payload.history.map((item,index)=>({id:String(item.id ?? `historical-${index}`),role:item.role,content:item.content,historical:true,createdAt:new Date()}))} current={null}/>);
  expect(historyView.container.querySelector('.business-evidence')).toBeNull();
  expect(historyView.container.querySelector('[data-save-available="true"]')).toBeNull();
  const restored=[...historyView.container.querySelectorAll('[data-role="assistant"] [data-section="answer"]')];
  for (const [index,item] of payload.history.filter(item=>item.role==='assistant').entries()) {
    expect(restored[index].textContent?.replace(/\s/g,'')).toBe(item.content.replace(/^\s*-\s+/gm,'').replace(/\s/g,''));
  }
});
