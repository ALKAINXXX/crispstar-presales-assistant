// Component checks are controlled UI contracts, not real browser acceptance.
import { render } from '@testing-library/react';
import { expect, it } from 'vitest';
import { BusinessResponseView } from './BusinessResponse';
import { convertMessage } from './adapter';
import type { ChatResponse } from './types';

function response():ChatResponse {
  const accepted='已接受：API与12个月已确认。',rejected='未更新：付费席位（原话：“公司大约80人”）。公司人数不能作为实际购买席位。';
  const answer='还缺实际购买席位，暂不能计算订阅总价；公司人数不能作为购买量。API需求和12个月期限保留，最低满足档为 Team。请明确购买多少席。';
  return {message:answer,model:'controlled-component',session_id:'synthetic-session',status:'needs_input',tool_results:[],business:{turn_id:'synthetic-turn',intent:'selection',requirements:{requirement_id:'synthetic-requirements',revision:1,items:[],selected_plan_id:null,parameters:[],condition_assertions:[]},catalog:null,catalog_context:null,match:null,pricing:null,unit_price:null,current_task:null,retrieval_evidence:[],knowledge_answers:[],changes:[],questions:['明确购买多少席？'],pending:['paid_seats'],errors:[],semantic_review:{status:'verified',dispatched:1,elapsed_ms:123,usage:{prompt_tokens:50,completion_tokens:20}},operation_feedback:[{group_id:'accepted',status:'accepted',message:'API与12个月已确认'},{group_id:'rejected',status:'rejected',message:'公司人数不能作为实际购买席位'}],presentation:{answer_markdown:answer,show_comparison:false,show_current_result:false,used_source_ids:[],feedback:[{group_id:'accepted',status:'accepted',message:accepted},{group_id:'rejected',status:'rejected',message:rejected}]}}};
}

it('keeps complete feedback in the existing closed disclosure while using the exact conclusion-first server answer',()=>{
  const value=response();
  const view=render(<BusinessResponseView response={value} currentTask={null}/>);
  const section=view.container.querySelector('[data-section="operation-feedback"]')!;
  for (const item of value.business!.presentation!.feedback!) {
    expect(section.textContent).toContain(item.message);
    expect(value.message).not.toContain(item.message);
    expect(section.querySelector(`[data-group-id="${item.group_id}"]`)?.getAttribute('data-feedback-status')).toBe(item.status);
  }
  expect(section.hasAttribute('open')).toBe(false);
  expect(section.querySelector('summary')?.textContent).toBe('查看本轮接收情况');
  expect(value.message).toContain('公司人数不能作为购买量');
  expect(value.message).toContain('API需求和12个月期限保留');
  expect(view.container.querySelector('[data-section="proposal"],[data-section="current-task"]')).toBeNull();
  expect(view.container.querySelector('[data-section="semantic-review"]')?.textContent).toContain('"dispatched": 1');
  const converted=convertMessage({id:'synthetic-message',role:'assistant',content:value.message,response:value,createdAt:new Date()});
  expect(converted.content).toEqual([{type:'text',text:value.business!.presentation!.answer_markdown}]);
});

it('does not strip nonaccepted impacts or system failures from the server answer',()=>{
  const value=response();
  const body='本次未能更新为50席；仍保留80席、12个月和原API需求。重新评估发生系统故障，旧金额不能作为本轮新结果，请重试本次修改。';
  value.message=body;
  value.business!.presentation!.answer_markdown=body;
  value.business!.presentation!.feedback!.push({group_id:'blocked',status:'blocked',message:'未更新：付费席位（原话：“改为50席”）。本组尚未完成，原80席保留。',previous_value:80,retained:true});
  value.business!.errors=[{code:'matching_failed',message:'重新评估发生系统故障'}];
  const converted=convertMessage({id:'blocked-message',role:'assistant',content:value.message,response:value,createdAt:new Date()});
  expect(converted.content).toEqual([{type:'text',text:body}]);
  const view=render(<BusinessResponseView response={value} currentTask={null}/>);
  const feedback=view.container.querySelector('[data-section="operation-feedback"]')!;
  expect(feedback.querySelector('[data-group-id="blocked"]')?.textContent).toContain('原80席保留');
  expect(view.container.querySelector('[data-section="tools"]')?.textContent).toContain('matching_failed');
  expect(view.container.querySelector('[data-save-available="true"]')).toBeNull();
});

it('legacy history without feedback shows ordinary text and has no invented review evidence',()=>{
  const legacy='已接受：旧历史保留原文字。旧金额仅为当时记录。';
  const converted=convertMessage({id:'legacy-message',role:'assistant',content:legacy,historical:true,createdAt:new Date()});
  expect(converted.content).toEqual([{type:'text',text:legacy}]);
  const value=response();delete value.business!.presentation!.feedback;delete value.business!.operation_feedback;delete value.business!.semantic_review;
  const view=render(<BusinessResponseView response={value} currentTask={null}/>);
  expect(view.container.querySelector('[data-section="operation-feedback"],[data-section="semantic-review"]')).toBeNull();
});
