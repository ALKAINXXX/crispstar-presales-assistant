// Controlled fetch timing only. The production SessionController and its
// ExternalStore message conversion are used; this is not browser evidence.
import { describe, expect, it, vi } from 'vitest';
import { convertMessage, money, SessionController } from './adapter';
import type { ChatResponse, CurrentTask } from './types';
import { testDataManagement } from './testData';

const reply = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const flush = async () => { for (let n = 0; n < 40; n++) await Promise.resolve(); };
function harness(deferLists = false, deferInvalidations = false, savedSession?: string, deferHealth = false) {
  const values = new Map<string, string>(savedSession ? [['assistant.sessionId',savedSession]] : []);
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  const requests: { url: string; options?: RequestInit; resolve: (response: Response) => void; reject: (error: Error) => void }[] = [];
  const fetcher = ((url: RequestInfo | URL, options?: RequestInit) => {
    const address = String(url);
    if (address === '/data-management') return Promise.resolve(reply(testDataManagement));
    if (address === '/sessions' && !deferLists) return Promise.resolve(reply({ sessions: savedSession ? [{id:savedSession,title:'已保存聊天',updated_at:''}] : [] }));
    if (address === '/health' && !deferHealth) return Promise.resolve(reply({ status: 'ok' }));
    return new Promise<Response>((resolve, reject) => { requests.push({ url: address, options, resolve, reject }); if (address === '/proposal/view/invalidate' && !deferInvalidations) resolve(reply({invalidated:true})); });
  }) as typeof fetch;
  const controller = new SessionController(fetcher, storage);
  return { controller, storage, requests, request: (url: string, index = -1) => requests.filter(request => request.url === url).at(index)! };
}
async function select(app: ReturnType<typeof harness>, id: string, data: object = {}) {
  const loading = app.controller.switchTo(id);
  await flush();
  app.request(`/sessions/${id}`).resolve(reply({ messages: [], ...data }));
  await loading;
}
const task = (overrides: Partial<CurrentTask> = {}): CurrentTask => ({ kind: 'selection', status: 'current', last_result: 'updated', result_turn_id: 'selection-turn', result_message_id: 'row-2', requirements: { requirement_id: 'requirement', revision: 2, items: [], selected_plan_id: 'team', parameters: [{ name: 'paid_seats', value: 80, user_quote: '80席' }], condition_assertions: [] }, match: null, pricing: null, pending: [], questions: [], ...overrides });

describe('same-session order and cross-session stale response protection', () => {
  it('initialize verifies actual scope before requesting normal sessions without model calls', async () => {
    const app = harness(true);
    const initialization=app.controller.initialize();await flush();
    expect(app.requests.map(request => request.url)).toEqual(['/sessions']);
    app.request('/sessions').resolve(reply({ sessions: [] }));
    await initialization;
    expect(app.controller.getSnapshot().health).toBe('服务已连接');
  });

  it('new chat ignores an old known-session response while its own request completes', async () => {
    const app = harness(); await select(app, 'old');
    const old = app.controller.send('旧提问');
    await app.controller.newChat();
    const current = app.controller.send('新提问');
    expect(app.requests.filter(request => request.url === '/chat')).toHaveLength(2);
    expect(JSON.parse(app.request('/chat', 1).options!.body as string).session_id).toBeNull();
    app.request('/chat', 0).resolve(reply({ session_id: 'old', message: '旧回答' })); await old;
    expect(app.controller.getSnapshot().messages.map(message => message.content)).toEqual(['新提问']);
    expect(app.storage.getItem('assistant.sessionId')).toBeNull();
    expect(app.controller.getSnapshot().busy).toBe(true);
    app.request('/chat', 1).resolve(reply({ session_id: 'new', message: '新回答' })); await current;
    expect(app.storage.getItem('assistant.sessionId')).toBe('new');
    expect(app.controller.getSnapshot().messages.map(message => message.content)).toEqual(['新提问', '新回答']);
  });

  it('late history cannot replace the newer selected session', async () => {
    const app = harness(); const old = app.controller.switchTo('old'); await flush(); const current = app.controller.switchTo('current'); await flush();
    app.request('/sessions/current').resolve(reply({ messages: [{ role: 'assistant', content: '当前历史' }] })); await current;
    app.request('/sessions/old').resolve(reply({ messages: [{ role: 'assistant', content: '旧历史' }] })); await old;
    expect(app.controller.getSnapshot().sessionId).toBe('current');
    expect(app.controller.getSnapshot().messages[0].content).toBe('当前历史');
  });

  it('new chat ignores a late history failure', async () => {
    const app = harness(); const old = app.controller.switchTo('old'); await flush(); await app.controller.newChat();
    app.request('/sessions/old').reject(new Error('迟到读取错误')); await old;
    expect(app.controller.getSnapshot().notice).toBe('');
    expect(app.controller.getSnapshot().sessionId).toBeNull();
    expect(app.controller.getSnapshot().busy).toBe(false);
  });

  it.each(['response', 'failure'])('stale session list %s cannot erase a newer successful list', async outcome => {
    const app = harness(true); const old = app.controller.refreshSessions(); const newer = app.controller.refreshSessions();
    app.request('/sessions', 1).resolve(reply({ sessions: [{ id: 'kept', title: '新列表', updated_at: '' }] })); await newer;
    if (outcome === 'response') app.request('/sessions', 0).resolve(reply({ sessions: [{ id: 'stale', title: '旧列表', updated_at: '' }] }));
    else app.request('/sessions', 0).reject(new Error('旧列表故障'));
    await old; expect(app.controller.getSnapshot().sessions.map(session => session.id)).toEqual(['kept']);
  });

  it('same-session busy prevents duplicate send and late completion reloads server text', async () => {
    const app = harness(); await select(app, 'busy'); const running = app.controller.send('处理中');
    await app.controller.newChat(); await select(app, 'busy');
    expect(app.controller.getSnapshot().busy).toBe(true);
    await app.controller.send('重复提问'); expect(app.requests.filter(request => request.url === '/chat')).toHaveLength(1);
    app.request('/chat').resolve(reply({ session_id: 'busy', message: '完成回答', business: { turn_id: 'late' } })); await running;
    await flush();
    app.request('/sessions/busy').resolve(reply({ messages: [{ id: 'real-row', role: 'assistant', content: '真实已存文本' }] })); await flush();
    expect(app.controller.getSnapshot().busy).toBe(false);
    expect(app.controller.getSnapshot().messages[0]).toMatchObject({ id: 'real-row', content: '真实已存文本', historical: true });
    expect(app.controller.getSnapshot().messages[0].response).toBeUndefined();
  });

  it('unknown originating session prevents overlap when server creates its id before replying', async () => {
    const app = harness(); const running = app.controller.send('首次提问'); await app.controller.newChat(); await select(app, 'created');
    expect(app.controller.getSnapshot().busy).toBe(true);
    await app.controller.send('不能重复'); expect(app.requests.filter(request => request.url === '/chat')).toHaveLength(1);
    app.request('/chat').resolve(reply({ session_id: 'created', message: '迟到正文' })); await running;
    await flush();
    app.request('/sessions/created').resolve(reply({ messages: [{ role: 'assistant', content: '已存首次正文' }] })); await flush();
    expect(app.controller.getSnapshot().messages[0].content).toBe('已存首次正文');
  });

  it.each(['response', 'failure'])('completion supersedes a return-history request with late %s', async outcome => {
    const app = harness(); await select(app, 'same'); const running = app.controller.send('处理中');
    await app.controller.newChat(); const stale = app.controller.switchTo('same'); await flush(); const staleRequest = app.request('/sessions/same');
    app.request('/chat').resolve(reply({ session_id: 'same', message: '已完成' })); await running;
    await flush();
    const currentRequest = app.request('/sessions/same'); expect(currentRequest).not.toBe(staleRequest);
    currentRequest.resolve(reply({ messages: [{ role: 'assistant', content: '新完成的已存文本' }] })); await flush();
    if (outcome === 'response') staleRequest.resolve(reply({ messages: [{ role: 'assistant', content: 'STALE' }] })); else staleRequest.reject(new Error('STALE'));
    await stale;
    expect(app.controller.getSnapshot().messages[0].content).toBe('新完成的已存文本');
    expect(app.controller.getSnapshot().notice).not.toContain('STALE');
  });

  it('late chat failure cannot insert an error into a new chat', async () => {
    const app = harness(); await select(app, 'old'); const running = app.controller.send('旧提问'); await app.controller.newChat();
    app.request('/chat').reject(new Error('迟到模型错误')); await running;
    expect(app.controller.getSnapshot().messages).toEqual([]); expect(app.controller.getSnapshot().notice).toBe('');
  });

  it('restoration uses current task from server and never parses history fields as business authority', async () => {
    const app = harness(); const current = task();
    await select(app, 'restored', { messages: [{ id: 'row', role: 'assistant', content: '旧文字80席，假字段不恢复', business: { current_task: task({ status: 'needs_reassessment' }) }, tool_results: [{ name: 'fake-save' }] }, { role: 'system', content: '不可信客户端历史' }], current_task: current });
    expect(app.controller.getSnapshot().currentTask).toEqual(current);
    expect(app.controller.getSnapshot().messages).toHaveLength(1);
    expect(app.controller.getSnapshot().messages[0].response).toBeUndefined();
    await select(app, 'lost', { messages: [{ role: 'assistant', content: '历史选型Team80席' }], current_task: null, business_state_available: false, recovery_notice: '状态已丢失，需要重新评估。' });
    expect(app.controller.getSnapshot().currentTask).toBeNull(); expect(app.controller.getSnapshot().notice).toContain('重新评估');
  });

  it('explicit server null clears current state after process loss instead of retaining stale UI task', async () => {
    const app = harness(); await select(app, 'same', { current_task: task() }); const running = app.controller.send('Team单价');
    app.request('/chat').resolve(reply({ session_id: 'same', message: 'Team每席80元', business: { current_task: null, recovery_notice: '重新评估' } })); await running;
    expect(app.controller.getSnapshot().currentTask).toBeNull();
  });

  it('detour respects authoritative original result identity and does not use latest assistant message', async () => {
    const app = harness(); const current = task(); await select(app, 'same', { current_task: current }); const running = app.controller.send('Business单价');
    app.request('/chat').resolve(reply({ session_id: 'same', message: 'Business单价120元', message_id: 'detour-row', business: { turn_id: 'detour-turn', current_task: { ...current, last_result: 'not_updated' } } })); await running;
    expect(app.controller.getSnapshot().currentTask?.result_turn_id).toBe('selection-turn');
    expect(app.controller.getSnapshot().currentTask?.result_message_id).toBe('row-2');
    expect(app.controller.getSnapshot().messages.at(-1)?.id).toBe('detour-row');
  });
});

it('external store renders server partition for live response and independent formal text for history', () => {
  const response = { message: '独立可读历史全文，含摘要', business: { presentation: { answer_markdown: '简洁当轮结论' } } } as ChatResponse;
  expect(convertMessage({ id: 'row', role: 'assistant', content: response.message, response, createdAt: new Date() }).content).toEqual([{ type: 'text', text: '简洁当轮结论' }]);
  expect(convertMessage({ id: 'row', role: 'assistant', content: response.message, historical: true, createdAt: new Date() }).content).toEqual([{ type: 'text', text: response.message }]);
});

it('money only formats authoritative integer minor units without using seats or recomputing prices', () => {
  expect(money(1_234_567)).toBe('12,345.67'); expect(money(8_081)).toBe('80.81'); expect(money(7_680_000)).toBe('76,800.00');
  for (const invalid of ['7680000', NaN, Infinity, 2.5, -1, Number.MAX_SAFE_INTEGER + 1, null]) expect(money(invalid)).toBe('金额暂无法展示');
});

it('default fetch adapter invokes browser fetch without binding SessionController as its receiver', async () => {
  const previous = globalThis.fetch;
  const browserFetch = vi.fn(function (this: unknown, address: RequestInfo | URL) {
    // Native Window.fetch throws Illegal invocation if class-method syntax
    // supplies the controller as receiver. Either Window or undefined works.
    expect(this === globalThis || this === undefined).toBe(true);
    return Promise.resolve(reply(String(address) === '/data-management' ? testDataManagement : String(address) === '/sessions' ? { sessions: [] } : { status: 'ok' }));
  });
  globalThis.fetch = browserFetch as typeof fetch;
  try {
    const app = new SessionController(undefined, { getItem: () => null, setItem() {}, removeItem() {} });
    await app.initialize(); await flush();
    expect(browserFetch.mock.calls.map(call => String(call[0]))).toEqual(['/health', '/data-management', '/sessions']);
    expect(app.getSnapshot().health).toBe('服务已连接');
  } finally { globalThis.fetch = previous; }
});

it('live server recovery partition appears once in answer while session-read recovery stays visible', async () => {
  const app = harness();
  const recovery = '服务端没有恢复业务需求，请重新说明当前要求。';
  await select(app, 'lost', { messages: [{ id: 'old-row', role: 'assistant', content: '已存旧结论' }], current_task: null, recovery_notice: recovery });
  expect(app.controller.getSnapshot().notice).toBe(recovery);
  const running = app.controller.send('继续原选型并计算基价');
  app.request('/chat').resolve(reply({ session_id: 'lost', message: `${recovery}\n请重新说明必须满足的需求。`, message_id: 'new-row', business: { current_task: null, recovery_notice: recovery, presentation: { answer_markdown: `${recovery}\n请重新说明必须满足的需求。` } } }));
  await running;
  expect(app.controller.getSnapshot().notice).toBe('');
  expect(convertMessage(app.controller.getSnapshot().messages.at(-1)!).content).toEqual([{ type: 'text', text: `${recovery}\n请重新说明必须满足的需求。` }]);
  expect(app.controller.getSnapshot().currentTask).toBeNull();
});

it('next message removes current Proposal immediately while retaining the authoritative selection', async () => {
  const app = harness(); await select(app,'same');
  const first = app.controller.send('准备80席12个月方案');
  const request = JSON.parse(app.request('/chat').options!.body as string);
  const current = task();
  const proposal = {proposal_id:'server-snapshot',session_id:'same',view_id:request.view_id,turn_id:'selection-turn',action_ref:'PRIVATE_ACTION'};
  app.request('/chat').resolve(reply({session_id:'same',message:'方案已准备，尚未保存',business:{turn_id:'selection-turn',current_task:current,proposal}})); await first;
  expect(app.controller.getSnapshot().currentProposal?.proposal_id).toBe('server-snapshot');
  const detour = app.controller.send('Business单价是多少？');
  expect(app.controller.getSnapshot().currentProposal).toBeNull();
  expect(app.controller.getSnapshot().currentTask).toEqual(current);
  expect(JSON.parse(app.request('/chat').options!.body as string)).toEqual({message:'Business单价是多少？',session_id:'same',view_id:request.view_id});
  app.request('/chat').resolve(reply({session_id:'same',message:'每席120元',business:{current_task:{...current,last_result:'not_updated'},proposal:null}})); await detour;
  expect(app.controller.getSnapshot().currentProposal).toBeNull();
  expect(app.controller.getSnapshot().currentTask?.result_turn_id).toBe(current.result_turn_id);
});

it('navigation waits for view invalidation, rotates its nonce and never restores a history action', async () => {
  const app = harness(false,true);
  const loading = app.controller.switchTo('old'); await flush();
  expect(app.requests.some(request=>request.url === '/sessions/old')).toBe(false);
  expect(app.controller.getSnapshot().busy).toBe(true);
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await flush();
  app.request('/sessions/old').resolve(reply({messages:[],current_task:task(),proposal:{proposal_id:'IGNORE_HISTORY_ACTION',action_ref:'IGNORE'}})); await loading;
  expect(app.controller.getSnapshot().currentProposal).toBeNull();
  const running = app.controller.send('继续'); const oldView = JSON.parse(app.request('/chat').options!.body as string).view_id;
  app.request('/chat').resolve(reply({session_id:'old',message:'继续回答'})); await running;
  const newChat = app.controller.newChat(); await flush();
  await app.controller.send('许可关闭前不能发送');
  expect(app.requests.filter(request=>request.url === '/chat')).toHaveLength(1);
  expect(JSON.parse(app.request('/proposal/view/invalidate').options!.body as string)).toEqual({view_id:oldView,event:'new_chat'});
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await newChat;
  const newMessage = app.controller.send('新会话');
  expect(JSON.parse(app.request('/chat').options!.body as string).view_id).not.toBe(oldView);
  app.request('/chat').resolve(reply({session_id:'new',message:'新回答'})); await newMessage;
});

it('failed lifecycle invalidation gates sending until a normal navigation retries the pending old view', async () => {
  const app = harness(false,true);
  const leaving = app.controller.newChat(); await flush();
  const failed = app.request('/proposal/view/invalidate'); const failedView = JSON.parse(failed.options!.body as string).view_id;
  failed.reject(new Error('失效接口不可达')); await leaving;
  expect(app.controller.getSnapshot().busy).toBe(true); expect(app.controller.getSnapshot().notice).toContain('已暂停发送');
  await app.controller.send('不能绕过'); expect(app.requests.some(request=>request.url === '/chat')).toBe(false);
  const retry = app.controller.newChat(); await flush();
  expect(JSON.parse(app.request('/proposal/view/invalidate').options!.body as string).view_id).toBe(failedView);
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await flush();
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await retry;
  expect(app.controller.getSnapshot().busy).toBe(false);
});

it.each(['known','unknown'])('new chat explains %s-session background work while permit invalidation waits and ignores its late Proposal', async origin => {
  const app = harness(false,true);
  if (origin === 'known') {
    const loading = app.controller.switchTo('old'); await flush();
    app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await flush();
    app.request('/sessions/old').resolve(reply({messages:[],current_task:task()})); await loading;
  }
  const running = app.controller.send('后台仍在核对');
  const originalView = JSON.parse(app.request('/chat').options!.body as string).view_id;
  const leaving = app.controller.newChat(); await flush();
  expect(app.controller.getSnapshot()).toMatchObject({busy:true,sessionId:null,currentTask:null,currentProposal:null,messages:[],notice:'上一轮仍可能在后台处理；切换页面不表示工作已结束，正在核对旧许可失效状态。'});
  await app.controller.send('旧许可未核实前不能发送');
  expect(app.requests.filter(request=>request.url === '/chat')).toHaveLength(1);
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await leaving;
  expect(app.controller.getSnapshot().notice).toBe('');
  app.request('/chat').resolve(reply({session_id:origin === 'known' ? 'old' : 'created-late',message:'迟到业务正文',business:{turn_id:'late-turn',current_task:task(),proposal:{proposal_id:'NEVER_RESTORED',session_id:origin === 'known' ? 'old' : 'created-late',turn_id:'late-turn',view_id:originalView,save_available:true}}}));
  await flush();
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await running;
  expect(app.controller.getSnapshot()).toMatchObject({busy:false,sessionId:null,currentTask:null,currentProposal:null,messages:[],notice:''});
  expect(app.storage.getItem('assistant.sessionId')).toBeNull();
});

it('idle new chat has no background warning while its ordinary invalidation completes', async () => {
  const app = harness(false,true);
  const leaving = app.controller.newChat(); await flush();
  expect(app.controller.getSnapshot().notice).toBe('');
  expect(app.controller.getSnapshot().busy).toBe(true);
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await leaving;
  expect(app.controller.getSnapshot()).toMatchObject({notice:'',busy:false});
});

it('failed invalidation keeps the sending pause instead of clearing the background waiting notice', async () => {
  const app = harness(false,true);
  const running = app.controller.send('后台请求');
  const leaving = app.controller.newChat(); await flush();
  expect(app.controller.getSnapshot().notice).toContain('后台处理');
  app.request('/proposal/view/invalidate').reject(new Error('失效核对故障')); await leaving;
  expect(app.controller.getSnapshot().notice).toContain('已暂停发送');
  expect(app.controller.getSnapshot().busy).toBe(true);
  await app.controller.send('不能解除顺序保护');
  expect(app.requests.filter(request=>request.url === '/chat')).toHaveLength(1);
  app.request('/chat').reject(new Error('迟到后台故障')); await running;
  expect(app.controller.getSnapshot().notice).toContain('已暂停发送');
  expect(app.controller.getSnapshot().messages).toEqual([]);
});

it('completed new-chat invalidation cannot erase the newer selected-page notice', async () => {
  const app = harness(false,true);
  const running = app.controller.send('未知会话后台请求');
  const leaving = app.controller.newChat(); await flush();
  const oldInvalidation = app.request('/proposal/view/invalidate');
  const selected = app.controller.switchTo('different'); await flush();
  expect(app.controller.getSnapshot().notice).toBe('正在读取聊天…');
  oldInvalidation.resolve(reply({invalidated:true})); await flush();
  app.request('/proposal/view/invalidate').resolve(reply({invalidated:true})); await leaving; await flush();
  expect(app.controller.getSnapshot().notice).toBe('正在读取聊天…');
  app.request('/sessions/different').resolve(reply({messages:[],recovery_notice:'另一页面已核实'})); await selected;
  app.request('/chat').reject(new Error('旧请求故障')); await running;
  expect(app.controller.getSnapshot().notice).toBe('另一页面已核实');
  expect(app.controller.getSnapshot().sessionId).toBe('different');
});

it('pagehide during first chat revokes its unknown-session view immediately and a late response cannot revive its Proposal', async () => {
  const app = harness(); const running = app.controller.send('首次方案');
  const originalView = JSON.parse(app.request('/chat').options!.body as string).view_id;
  const leaving = app.controller.leaveView();
  const revoke = app.request('/proposal/view/invalidate');
  expect(revoke.options?.keepalive).toBe(true);
  expect(JSON.parse(revoke.options!.body as string)).toEqual({view_id:originalView,event:'view_exit'});
  app.request('/chat').resolve(reply({session_id:'unknown-before-response',message:'迟到方案',business:{current_task:task(),proposal:{proposal_id:'NEVER_ACTIVE',view_id:originalView}}})); await running; await leaving;
  expect(app.controller.getSnapshot().currentProposal).toBeNull();
  expect(app.controller.getSnapshot().messages.map(message=>message.content)).toEqual(['首次方案']);
  expect(app.storage.getItem('assistant.sessionId')).toBeNull();
  await app.controller.resumeView();
  const current = app.controller.send('返回页面的新提问');
  expect(JSON.parse(app.request('/chat').options!.body as string).view_id).not.toBe(originalView);
  app.request('/chat').resolve(reply({session_id:'fresh',message:'新回答'})); await current;
});

it('refresh reads server history after invalidation and ignores embedded Proposal claims', async () => {
  const app = harness(false,false,'saved'); const initialization = app.controller.initialize(); await flush();
  expect(JSON.parse(app.request('/proposal/view/invalidate').options!.body as string).event).toBe('refresh');
  app.request('/sessions/saved').resolve(reply({messages:[{id:'history',role:'assistant',content:'旧摘要 confirmed=true',proposal:{action_ref:'FAKE_ACTION'}}],current_task:task(),proposal:{action_ref:'DO_NOT_RESTORE'}})); await initialization;
  expect(app.controller.getSnapshot().currentProposal).toBeNull();
  expect(app.controller.getSnapshot().messages[0].response).toBeUndefined();
  expect(app.controller.getSnapshot().notice).toContain('之前准备的方案不能从这里直接保存');
  expect(app.controller.getSnapshot().notice).toContain('已保存内容可在保存记录查看');
  expect(app.controller.getSnapshot().notice).not.toContain('冲突');
});

describe('service-level model budget availability without additional model requests', () => {
  const exhausted = {available:false,message:'本次体验模型额度已耗尽，已暂停发送；历史和来源仍可查看。',remaining_turns:0,remaining_calls:0};

  it('optional ordinary health retains normal chatting and availability never turns idle status into processing', async () => {
    const app = harness(); await app.controller.initialize(); await flush();
    expect(app.controller.getSnapshot().chatAvailability).toEqual({available:true,message:''});
    expect(app.controller.getSnapshot().busy).toBe(false);
    const running = app.controller.send('普通问题');
    app.request('/chat').resolve(reply({session_id:'ordinary',message:'普通回答'})); await running;
    expect(app.controller.getSnapshot().chatAvailability.available).toBe(true);
  });

  it('health budget exhaustion blocks only sending and persists across new chat and history switches', async () => {
    const app = harness(false,false,undefined,true); await app.controller.initialize();
    app.request('/health').resolve(reply({status:'ok',chat_availability:exhausted})); await flush();
    expect(app.controller.getSnapshot().health).toBe('服务已连接');
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    expect(app.controller.getSnapshot().busy).toBe(false);
    await app.controller.send('不得调用模型'); await app.controller.newChat(); await app.controller.send('新聊天也不得调用');
    expect(app.requests.some(request=>request.url === '/chat')).toBe(false);
    await select(app,'history',{messages:[{id:'actual-row',role:'assistant',content:'已存方案与原文'}],current_task:task()});
    expect(app.controller.getSnapshot().messages[0].content).toBe('已存方案与原文');
    expect(app.controller.getSnapshot().currentTask).toEqual(task());
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    expect(app.controller.getSnapshot().busy).toBe(false);
  });

  it('typed budget error preserves the real response and selection, immediately pauses sending, and rechecks health exactly once', async () => {
    const app = harness(false,false,undefined,true); await select(app,'same',{current_task:task()});
    const response = {session_id:'same',message:'本轮未继续调用模型，原选型保留。',message_id:'real-budget-row',status:'failed',business:{turn_id:'budget-turn',current_task:task(),proposal:null,errors:[{code:'model_budget_exhausted',message:exhausted.message}]}};
    const running = app.controller.send('达到边界的问题');
    app.request('/chat').resolve(reply(response)); await flush();
    expect(app.controller.getSnapshot().chatAvailability.available).toBe(false);
    expect(app.controller.getSnapshot().chatAvailability.message).toBe(exhausted.message);
    expect(app.controller.getSnapshot().messages.at(-1)?.response).toEqual(response);
    expect(app.controller.getSnapshot().currentTask).toEqual(task());
    expect(app.controller.getSnapshot().health).toBe('服务已连接');
    await app.controller.send('不能因按钮或欢迎提示再调用');
    expect(app.requests.filter(request=>request.url === '/chat')).toHaveLength(1);
    expect(app.requests.filter(request=>request.url === '/health')).toHaveLength(1);
    app.request('/health').resolve(reply({status:'ok',chat_availability:exhausted})); await running;
    await flush();
    expect(app.controller.getSnapshot().busy).toBe(false);
    expect(app.requests.filter(request=>request.url === '/health')).toHaveLength(1);
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
  });

  it('completion keeps sending gated until zero-model health marks the just-exhausted budget', async () => {
    const app = harness(false,false,undefined,true); const running = app.controller.send('最后一个合法回合');
    app.request('/chat').resolve(reply({session_id:'last',message:'实际完成结果',business:{current_task:task()}})); await flush();
    expect(app.controller.getSnapshot().messages.at(-1)?.content).toBe('实际完成结果');
    expect(app.controller.getSnapshot().busy).toBe(true);
    await app.controller.send('health尚未结束不能加回合');
    expect(app.requests.filter(request=>request.url === '/chat')).toHaveLength(1);
    app.request('/health').resolve(reply({status:'ok',chat_availability:exhausted})); await running;
    expect(app.controller.getSnapshot().busy).toBe(false);
    expect(app.controller.getSnapshot().chatAvailability.available).toBe(false);
  });

  it('late budget response cannot change another session but can refresh global availability through health', async () => {
    const app = harness(false,false,undefined,true); await select(app,'old');
    const running = app.controller.send('旧会话问题'); await app.controller.newChat();
    app.request('/chat').resolve(reply({session_id:'old',message:'LATE_BODY_NEVER_INSERT',business:{current_task:task(),errors:[{code:'model_budget_exhausted',message:'LATE_ERROR_NEVER_INSERT'}]}})); await flush();
    expect(app.controller.getSnapshot().messages).toEqual([]); expect(app.controller.getSnapshot().currentTask).toBeNull();
    expect(app.controller.getSnapshot().chatAvailability.available).toBe(true);
    expect(app.controller.getSnapshot().notice).not.toContain('LATE');
    app.request('/health').resolve(reply({status:'ok',chat_availability:exhausted})); await running;
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    expect(app.controller.getSnapshot().messages).toEqual([]);
    expect(app.storage.getItem('assistant.sessionId')).toBeNull();
  });

  it('older health replies cannot erase a newer exhausted decision, and missing/failed health does not disguise the budget as unreachable', async () => {
    const app = harness(false,false,undefined,true);
    const old = app.controller.refreshHealth(); const current = app.controller.refreshHealth();
    app.request('/health',1).resolve(reply({status:'ok',chat_availability:exhausted})); await current;
    app.request('/health',0).resolve(reply({status:'ok',chat_availability:{available:true,message:'',remaining_turns:1}})); await old;
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    const ordinary = app.controller.refreshHealth(); app.request('/health').resolve(reply({status:'ok'})); await ordinary;
    expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    const failed = app.controller.refreshHealth(); app.request('/health').reject(new Error('连接检查失败')); await failed;
    expect(app.controller.getSnapshot().health).toBe('服务已连接'); expect(app.controller.getSnapshot().chatAvailability).toEqual(exhausted);
    expect(app.controller.getSnapshot().chatAvailability.message).not.toContain('稍后重试');
  });
});
