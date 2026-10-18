// pytest supplies actual controlled Orchestrator / SQLite output. Only the
// model is scripted for poison/error injection; the real Thread/runtime,
// server partitions, domain results and history are exercised together.
import { readFileSync } from 'node:fs';
import { fireEvent, render } from '@testing-library/react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { expect, it } from 'vitest';
import { Thread, CurrentTaskContext } from '@/components/assistant-ui/elements/thread.aui';
import { convertMessage } from './adapter';
import type { ChatResponse, CurrentTask, ViewMessage } from './types';

// Strip only the controlled formatter's supported presentation markers from
// the expected server text. Amounts, restrictions, citations and save wording
// remain exact; the actual rendered answer is only whitespace-normalized.
function visibleControlledMarkdown(text: string) {
  return text.replace(/^[ \t]*>[ \t]?/gm, '').replace(/^[ \t]*-[ \t]+/gm, '').replace(/\*\*([^*]+)\*\*/g, '$1').replace(/\s/g, '');
}

function RealThread({ messages, current, sendDisabled = false }: { messages: ViewMessage[]; current: CurrentTask | null; sendDisabled?:boolean }) {
  const runtime = useExternalStoreRuntime({ messages, convertMessage, isSendDisabled:sendDisabled, onNew: async () => {} });
  return <AssistantRuntimeProvider runtime={runtime}><CurrentTaskContext.Provider value={current}><Thread autoFocus={false} /></CurrentTaskContext.Provider></AssistantRuntimeProvider>;
}

it.skipIf(!process.env.RESPONSE_BRIDGE_FIXTURE)('blocked model body stays blocked through actual response, production Thread and historical restore', () => {
  const payload: { hidden: string; users: string[]; responses: ChatResponse[]; history: { role: 'user' | 'assistant'; content: string }[] } = JSON.parse(readFileSync(process.env.RESPONSE_BRIDGE_FIXTURE!, 'utf8'));
  const messages: ViewMessage[] = [];
  const view = render(<RealThread messages={[]} current={null} />);
  for (const [index, response] of payload.responses.entries()) {
    messages.push({ id: `user-${index}`, role: 'user', content: payload.users[index], createdAt: new Date() }, { id: String(response.message_id ?? `assistant-${index}`), role: 'assistant', content: response.message, response, createdAt: new Date() });
    view.rerender(<RealThread messages={[...messages]} current={response.business?.current_task ?? null} />);
    expect(view.container.textContent).not.toContain(payload.hidden);
    const answers = [...view.container.querySelectorAll('[data-section="answer"]')];
    expect(answers.at(-1)?.textContent?.replace(/\s/g, '')).toBe(visibleControlledMarkdown(response.business?.presentation?.answer_markdown ?? response.message));
    if (index === 0) {
      expect(view.container.textContent).toContain('76,800.00');
      expect(view.container.textContent).toContain(response.business!.pricing!.formula);
      expect(view.container.textContent).toContain('团队版 Team');
    }
  }
  view.rerender(<RealThread messages={payload.history.map((message, index) => ({ ...message, id: `stored-${index}`, historical: true, createdAt: new Date() }))} current={null} />);
  expect(view.container.textContent).not.toContain(payload.hidden);
  expect(view.container.querySelector('.business-evidence')).toBeNull();
  expect(view.container.querySelectorAll('.history-label')).toHaveLength(payload.history.filter(message => message.role === 'assistant').length);
  // The real Markdown component renders the formatter's list, emphasis and
  // quotation markers as nodes. Every factual character must still match the
  // corresponding stored row after removing only those layout markers.
  const restoredAnswers = [...view.container.querySelectorAll('[data-role="assistant"] [data-section="answer"]')];
  const storedAnswers = payload.history.filter(message => message.role === 'assistant');
  for (const [index, message] of storedAnswers.entries()) {
    expect(restoredAnswers[index].textContent?.replace(/\s/g, '')).toBe(visibleControlledMarkdown(message.content));
  }
  const restoredUsers = [...view.container.querySelectorAll('[data-role="user"]')];
  for (const [index, message] of payload.history.filter(message => message.role === 'user').entries()) {
    expect(restoredUsers[index].textContent?.replace(/\s/g, '')).toBe(message.content.replace(/\s/g, ''));
  }
  expect(view.container.querySelector('script, img, button[onclick], a[href^="javascript:"]')).toBeNull();
});

it('controlled Markdown normalization preserves exact amount, unresolved condition and quoted save status in the real Thread', () => {
  const answer = '订阅基价为 **76,800.00 元**。\n\n- SSO 前提尚未确认。\n\n> [1] 原文：尚未保存，不能视为适配通过。';
  const view = render(<RealThread messages={[{id:'controlled-markdown',role:'assistant',content:answer,historical:true,createdAt:new Date()}]} current={null}/>);
  const visible = view.container.querySelector('[data-section="answer"]')!.textContent!.replace(/\s/g, '');
  expect(visible).toBe(visibleControlledMarkdown(answer));
  expect(visible).toContain('76,800.00元');
  expect(visible).toContain('SSO前提尚未确认');
  expect(visible).toContain('[1]原文：尚未保存，不能视为适配通过');
  expect(visible).not.toBe(visibleControlledMarkdown(answer.replace('76,800.00', '1.00')));
  expect(visible).not.toBe(visibleControlledMarkdown(answer.replace('尚未保存', '已保存')));
});

it('Markdown model and source-like HTML stays inert, and unsupported operations are absent from official Thread', () => {
  const dangerous = '<button onclick="steal()">确认并保存</button><script>steal()</script><img src=x onerror=steal()> [恶意链接](javascript:steal())';
  const view = render(<RealThread messages={[{ id: 'historical-html', role: 'assistant', content: dangerous, historical: true, createdAt: new Date() }]} current={null} />);
  expect(view.container.querySelector('script, img, button[onclick], a[href^="javascript:"]')).toBeNull();
  for (const label of ['编辑', '重生成', '分支', '删除', '取消执行', '确认并保存']) expect([...view.container.querySelectorAll('button')].some(button => button.textContent === label || button.getAttribute('aria-label') === label)).toBe(false);
});

it('service availability disables the production send control without pretending the Thread is running or blocking reading', () => {
  const view = render(<RealThread messages={[{id:'saved',role:'assistant',content:'可继续读取的实际历史',historical:true,createdAt:new Date()}]} current={null} sendDisabled/>);
  fireEvent.change(view.getByLabelText('聊天输入'),{target:{value:'额度耗尽时不得发送'}});
  expect(view.getByLabelText('发送').hasAttribute('disabled')).toBe(true);
  expect(view.getByLabelText('发送').textContent).toContain('发送');
  expect(view.container.textContent).not.toContain('处理中');
  expect(view.container.textContent).toContain('可继续读取的实际历史');
  expect(view.getByLabelText('聊天输入').hasAttribute('disabled')).toBe(false);
});
