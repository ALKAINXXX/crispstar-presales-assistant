"""Controlled model wire values, real parser/domain/retrieval/storage chain."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

from app.agents import orchestrator as module
from app.llm.deepseek_client import DeepSeekClient
from app.llm.deepseek_tool_contract import ToolContract
from app.schemas.chat import ChatRequest
from app.schemas.semantic import SemanticReview
from app.tools.business import BusinessToolRegistry
from test_presales_orchestrator import engine, same_history, group, call, completion
from test_consultation_scope_convergence import item
from test_consultation_anchor_orchestrator import knowledge_provider, assert_knowledge_complete


H2 = '企业版Business的SAML SSO需要什么前提，以及这项功能是什么意思？'


def source_contract():
    tools = BusinessToolRegistry.__new__(BusinessToolRegistry).schemas()
    review = SemanticReview.model_json_schema()
    review['required'] = list(dict.fromkeys((*review.get('required',()),
        'consultations','consultation_coverage_complete')))
    return ToolContract([*tools,{'type':'function','function':{
        'name':'adjudicate_turn','parameters':review}}])


def strict_sdk(monkeypatch, engine, provider, *, first_override=None, extra_business=False, bad_wire=False,
               bad_review_wire=False):
    contract = source_contract()
    legacy_provider = provider(engine.settings)
    class SDK:
        requests, review_count = [], 0
        def __init__(self, **kwargs):
            assert kwargs['base_url']=='https://api.deepseek.com/beta'
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            assert kwargs=={'max_retries':0,'timeout':45.0}
            return self
        async def create(self, **payload):
            type(self).requests.append(deepcopy(payload))
            assert all(t['function']['strict'] for t in payload['tools'])
            messages=deepcopy(payload['messages'])
            assert messages.pop()['role']=='system'  # Transport-only guidance.
            for m in messages:
                for c in m.get('tool_calls',()):
                    f=c['function']; f['arguments']=contract.decode_arguments(f['name'],f['arguments'])
            review=payload['tool_choice']=={'type':'function','function':{'name':'adjudicate_turn'}}
            if review:
                type(self).review_count+=1
            if extra_business and not review and len(provider.sent)==3:
                provider.sent.append({'messages':messages})
                result=completion(call('match_requirements',{},'actual-match'),call('calculate_price',{
                    'plan_id':'team','paid_seats':50,'months':12},'actual-price'))
            else:
                result=await legacy_provider.chat(messages=messages,tool_choice=payload['tool_choice'],
                    max_retries=0 if review else None)
            calls=[]
            for c in result.choices[0].message.tool_calls or ():
                raw=c.function.arguments
                if not review and len(provider.sent)==1 and c.function.name=='update_requirements' and first_override is not None:
                    raw=json.dumps(first_override,ensure_ascii=False)
                wire=contract.encode_arguments(c.function.name,raw)
                if bad_wire and c.function.name=='update_requirements':
                    value=json.loads(wire); value['unknown_business_fact']=True
                    wire=json.dumps(value)
                if bad_review_wire and c.function.name=='adjudicate_turn':
                    value=json.loads(wire); del value['consultation_coverage_complete']
                    wire=json.dumps(value)
                calls.append({'id':c.id,'type':'function','function':{'name':c.function.name,'arguments':wire}})
            return ChatCompletion(id='controlled-wire',object='chat.completion',created=0,model='deepseek-flash',
                choices=[{'index':0,'finish_reason':'tool_calls' if calls else 'stop',
                    'message':{'role':'assistant','content':result.choices[0].message.content,'tool_calls':calls}}])
        async def close(self):
            pass
    monkeypatch.setattr('app.llm.deepseek_client.AsyncOpenAI',SDK)
    monkeypatch.setattr(module,'DeepSeekClient',lambda settings:DeepSeekClient(settings,tool_output_mode='deepseek_strict_beta'))
    return SDK


@pytest.mark.parametrize('correct_first',[True,False])
def test_H2_strict_wire_runs_original_single_review_and_real_native_knowledge(engine,monkeypatch,correct_first):
    units=[item('rule','企业版Business的SAML SSO需要什么前提',('business',),aspect='prerequisite',feature='saml_sso'),
        item('meaning','这项功能是什么意思',('business',),aspect='meaning',feature='saml_sso')]
    provider=knowledge_provider(monkeypatch,units)
    sdk=strict_sdk(monkeypatch,engine,provider,first_override=None if correct_first else {'intent':'general','consultations':[]})
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert_knowledge_complete(result,[('prerequisite',('business',),'saml_sso'),('meaning',('business',),'saml_sso')])
    assert result.message.index('使用前提') < result.message.index('SAML SSO 表示')
    assert '兼容的身份提供方' in result.message
    assert sdk.review_count==1 and result.business.semantic_review['dispatched']==1
    assert not result.business.requirements.items and not result.business.requirements.condition_assertions
    assert result.business.pricing is None and result.business.proposal is None
    assert len([r for r in result.tool_results if r['name']=='explain_knowledge'])==2
    same_history(engine,result,H2)
    assert engine.quotes.list_quotes()==[]


def test_format_does_not_supply_omitted_meaning_or_mask_missing_coverage(engine,monkeypatch):
    units=[item('rule','企业版Business的SAML SSO需要什么前提',('business',),aspect='prerequisite',feature='saml_sso')]
    provider=knowledge_provider(monkeypatch,units)
    sdk=strict_sdk(monkeypatch,engine,provider)
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert result.status=='incomplete' and not result.business.consultation_coverage.scope_complete
    rule,meaning=result.business.consultation_coverage.items
    assert rule.stage=='answered' and rule.retrieval_ids and rule.prerequisites
    assert meaning.stage=='extraction_incomplete' and not meaning.retrieval_ids
    assert sdk.review_count==1 and not result.business.proposal
    same_history(engine,result,H2)


def test_valid_wire_wrong_relation_does_not_become_accepted_fact(engine,monkeypatch):
    units=[item('wrong',H2,('team',),aspect='prerequisite',feature='saml_sso')]
    provider=knowledge_provider(monkeypatch,units)
    sdk=strict_sdk(monkeypatch,engine,provider)
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert result.status=='incomplete' and not result.business.consultation_coverage.scope_complete
    assert all(i.stage=='extraction_incomplete' for i in result.business.consultation_coverage.items)
    assert not result.business.knowledge_answers and not result.business.requirements.condition_assertions
    assert sdk.review_count==1
    same_history(engine,result,H2)


def test_unknown_wire_field_is_not_dropped_to_manufacture_legal_candidate(engine,monkeypatch):
    provider=knowledge_provider(monkeypatch,[])
    sdk=strict_sdk(monkeypatch,engine,provider,bad_wire=True)
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert result.status=='incomplete' and result.business.semantic_review['dispatched']==0
    assert not sdk.review_count and not result.tool_results
    assert not result.business.requirements.items
    assert '尚未进行独立核对与业务执行' in result.message
    same_history(engine,result,H2)


def test_legal_wire_cannot_bypass_original_candidate_string_length(engine,monkeypatch):
    units=[item('meaning',H2,('business',),aspect='meaning',feature='saml_sso')]
    provider=knowledge_provider(monkeypatch,units)
    invalid=deepcopy(units); invalid[0]['topic']=''
    sdk=strict_sdk(monkeypatch,engine,provider,first_override={'intent':'knowledge','consultations':invalid})
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert result.status=='incomplete' and result.business.semantic_review['dispatched']==0
    assert not sdk.review_count
    assert result.business.errors[0]['code']=='semantic_review_candidate_invalid'
    assert len(result.tool_results)==1 and not result.tool_results[0]['result']['ok']
    assert not result.business.requirements.items
    same_history(engine,result,H2)


def test_missing_review_contract_is_rejected_without_filling_true(engine,monkeypatch):
    provider=knowledge_provider(monkeypatch,[])
    sdk=strict_sdk(monkeypatch,engine,provider,bad_review_wire=True)
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert result.status=='incomplete' and sdk.review_count==1
    assert result.business.errors[0]['code']=='semantic_review_invalid'
    assert result.business.semantic_review['dispatched']==1
    assert not result.business.knowledge_answers and not result.business.proposal
    same_history(engine,result,H2)


@pytest.mark.parametrize('uncertain',[False,True])
def test_real_task_modification_and_independent_question_keep_business_gates(engine,monkeypatch,uncertain):
    from test_proposals import seed
    sid,runtime,old=asyncio.run(seed(engine))
    before=runtime.state.current_task(recheck=False)
    quote='改成约70席' if uncertain else '改成50席'
    message=quote+'并重算；另外，Business的SSO是什么意思？'
    units=[item('meaning','Business的SSO是什么意思',('business',),aspect='meaning',feature='saml_sso')]
    groups=[group('seats',quote,70 if uncertain else 50,
        verdict='reject' if uncertain else 'accept',reason='uncertain' if uncertain else 'confirmed',
        certainty='approximate' if uncertain else 'certain')]
    provider=knowledge_provider(monkeypatch,units,review_intent='selection',
        reviewed_target={'kind':'selection','relation':'continue','task_id':before['requirements']['requirement_id']},
        reviewed_groups=groups)
    first={'intent':'selection','parameters':[{'name':'paid_seats','value':70 if uncertain else 50,'user_quote':quote}],
        'consultations':units}
    sdk=strict_sdk(monkeypatch,engine,provider,first_override=first,extra_business=True)
    result=asyncio.run(engine.chat(ChatRequest(session_id=sid,message=message)))
    assert sdk.review_count==1
    after=runtime.state.current_task(recheck=False)
    assert after['requirements']['requirement_id']==before['requirements']['requirement_id']
    assert after['requirements']['items']==before['requirements']['items']
    assert not after['requirements']['condition_assertions']
    assert result.business.consultation_coverage.items[0].stage=='answered'
    if uncertain:
        assert result.status=='needs_input' and result.business.pricing is None
        assert any(f['issue']=='uncertain' for f in result.business.operation_feedback)
        assert after['pricing']==before['pricing']
        assert result.business.proposal is None
    else:
        assert result.status=='completed' and result.business.pricing.base_price_minor==4800000
        assert after['pricing']['paid_seats']==50 and after['pricing']['months']==12
    same_history(engine,result,message)
    assert engine.quotes.list_quotes()==[]
    assert asyncio.run(engine.submit_quote(sid,old.action_ref)).status=='not_saved'


@pytest.mark.parametrize('reason',['strict_wire_context_budget','strict_tool_contract_invalid'])
def test_local_review_wire_rejection_is_not_a_provider_dispatch(engine,monkeypatch,reason):
    provider=knowledge_provider(monkeypatch,[])
    sdk=strict_sdk(monkeypatch,engine,provider)
    if reason=='strict_wire_context_budget':
        def count(serialized):
            try:
                payload=json.loads(serialized)
            except (TypeError,ValueError):
                return 1
            if isinstance(payload,dict) and any(
                t.get('function',{}).get('strict') and t['function']['name']=='adjudicate_turn'
                for t in payload.get('tools',())):
                return engine.settings.deepseek_max_context_tokens
            return 1
        monkeypatch.setattr('app.llm.token_counter.count_text_tokens',count)
    else:
        original_schema=SemanticReview.model_json_schema
        def unsupported_schema(*args,**kwargs):
            schema=original_schema(*args,**kwargs)
            schema['properties']['intent']['pattern']='.*'
            return schema
        monkeypatch.setattr(SemanticReview,'model_json_schema',unsupported_schema)
    result=asyncio.run(engine.chat(ChatRequest(message=H2)))
    assert len(sdk.requests)==1 and sdk.review_count==0
    assert result.status=='incomplete'
    assert result.business.semantic_review['dispatched']==0
    assert result.business.semantic_review['request_rejection']==reason
    assert result.business.errors[0]['code']=='semantic_review_request_invalid'
    assert '尚未派发独立核对' in result.message
    assert not result.business.requirements.items and not result.business.knowledge_answers
    assert not result.business.proposal
    same_history(engine,result,H2)
