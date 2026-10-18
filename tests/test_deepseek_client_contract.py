"""Request/transport checks; no provider calls or business-default synthesis."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from app.config import Settings
from app.llm.deepseek_client import DeepSeekClient, DeepSeekToolOutputInvalid
from app.llm.tool_protocol import ModelRequestInvalid
from app.llm.deepseek_tool_contract import ToolContract
from app.tools.business import BusinessToolRegistry
from test_orchestrator import engine, scripted, completion, tool, error_codes, assert_stored_exactly
from app.schemas.chat import ChatRequest


def tools():
    return BusinessToolRegistry.__new__(BusinessToolRegistry).schemas()


def answer(arguments, finish='tool_calls'):
    return ChatCompletion(id='controlled', created=0, object='chat.completion', model='deepseek-flash',
        choices=[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':None,
            'tool_calls':[{'id':'actual-id','type':'function','function':{
                'name':'update_requirements','arguments':arguments}}]}}])


def fake_sdk(monkeypatch, response):
    class SDK:
        requests, clients, options = [], [], []
        def __init__(self, **kwargs):
            self.init = kwargs
            type(self).clients.append(self)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        async def create(self, **kwargs):
            type(self).requests.append(deepcopy(kwargs))
            return response
        def with_options(self, **kwargs):
            type(self).options.append(kwargs)
            return self
        async def close(self):
            pass
    monkeypatch.setattr('app.llm.deepseek_client.AsyncOpenAI', SDK)
    return SDK


def settings(**kwargs):
    return Settings(_env_file=None,deepseek_api_key='controlled', **kwargs)


@pytest.mark.parametrize('mode', ['standard', 'deepseek_strict_beta'])
def test_isolated_dotenv_selects_normal_client_mode(monkeypatch,tmp_path,mode):
    monkeypatch.delenv('DEEPSEEK_TOOL_OUTPUT_MODE',raising=False)
    dotenv=tmp_path/'controlled.env'
    dotenv.write_text(f'DEEPSEEK_TOOL_OUTPUT_MODE={mode}\nGROQ_MODEL=deepseek-flash\n',encoding='utf-8')
    configured=Settings(_env_file=dotenv,deepseek_api_key='controlled')
    sdk=fake_sdk(monkeypatch,answer('{}'))
    client=DeepSeekClient(configured)
    assert configured.deepseek_tool_output_mode==client.tool_output_mode==mode
    assert configured.deepseek_model=='deepseek-flash' and configured.deepseek_api_key=='controlled'
    expected='https://api.deepseek.com/beta' if mode=='deepseek_strict_beta' else 'https://api.deepseek.com'
    assert sdk.clients[0].init['base_url']==expected
    assert not sdk.requests  # Merely loading configuration cannot dispatch.


def test_explicit_environment_can_manually_override_dotenv_mode(monkeypatch,tmp_path):
    dotenv=tmp_path/'controlled.env'
    dotenv.write_text('DEEPSEEK_TOOL_OUTPUT_MODE=deepseek_strict_beta\n',encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_TOOL_OUTPUT_MODE','standard')
    configured=Settings(_env_file=dotenv,deepseek_api_key='controlled')
    sdk=fake_sdk(monkeypatch,answer('{}'))
    client=DeepSeekClient(configured)
    assert configured.deepseek_tool_output_mode==client.tool_output_mode=='standard'
    assert sdk.clients[0].init['base_url']=='https://api.deepseek.com'
    assert not sdk.requests


@pytest.mark.parametrize('via_environment',[False,True])
def test_illegal_configured_mode_is_rejected_before_any_client(monkeypatch,tmp_path,via_environment):
    monkeypatch.delenv('DEEPSEEK_TOOL_OUTPUT_MODE',raising=False)
    dotenv=tmp_path/'controlled.env'
    dotenv.write_text('DEEPSEEK_TOOL_OUTPUT_MODE=invalid_mode\n',encoding='utf-8')
    sdk=fake_sdk(monkeypatch,answer('{}'))
    if via_environment:
        monkeypatch.setenv('DEEPSEEK_TOOL_OUTPUT_MODE','invalid_mode')
    with pytest.raises(ValidationError,match='deepseek_tool_output_mode'):
        Settings(_env_file=None if via_environment else dotenv,deepseek_api_key='controlled')
    with pytest.raises(ValueError,match='Unknown tool output mode'):
        DeepSeekClient(settings(deepseek_tool_output_mode='standard'),tool_output_mode='invalid_mode')
    assert not sdk.clients and not sdk.requests


def test_explicit_manual_standard_override_preserves_receipt_without_fallback(monkeypatch):
    configured=settings(deepseek_tool_output_mode='deepseek_strict_beta')
    original_tools=tools()
    raw='{"intent":"knowledge","consultations":[]}'
    receipt=answer(raw)
    sdk=fake_sdk(monkeypatch,receipt)
    client=DeepSeekClient(configured,tool_output_mode='standard')
    result=asyncio.run(client.chat(messages=[{'role':'user','content':'SSO是什么？'}],tools=original_tools))
    assert configured.deepseek_tool_output_mode=='deepseek_strict_beta' and client.tool_output_mode=='standard'
    assert sdk.clients[0].init['base_url']=='https://api.deepseek.com'
    assert result is receipt and result.choices[0].message.tool_calls[0].function.arguments==raw
    assert sdk.requests[0]['tools']==original_tools
    assert len(sdk.clients)==len(sdk.requests)==1 and not sdk.options


def test_normal_configured_client_runs_H2_and_one_review_with_real_business_chain(monkeypatch,tmp_path):
    from app.agents import orchestrator as module
    from app.agents.orchestrator import Orchestrator
    from app.db.session import init_saas_db
    from test_deepseek_strict_orchestrator import H2, strict_sdk
    from test_consultation_anchor_orchestrator import knowledge_provider, assert_knowledge_complete
    from test_consultation_scope_convergence import item
    from test_presales_orchestrator import same_history

    monkeypatch.delenv('DEEPSEEK_TOOL_OUTPUT_MODE',raising=False)
    dotenv=tmp_path/'controlled.env'
    dotenv.write_text('DEEPSEEK_TOOL_OUTPUT_MODE=deepseek_strict_beta\n',encoding='utf-8')
    configured=Settings(_env_file=dotenv,deepseek_api_key='controlled',
        saas_database_url=f"sqlite:///{tmp_path/'configured-private.sqlite3'}")
    init_saas_db(configured)
    runtime=Orchestrator(configured)
    units=[item('rule','企业版Business的SAML SSO需要什么前提',('business',),aspect='prerequisite',feature='saml_sso'),
        item('meaning','这项功能是什么意思',('business',),aspect='meaning',feature='saml_sso')]
    provider=knowledge_provider(monkeypatch,units)
    sdk=strict_sdk(monkeypatch,runtime,provider)
    # Keep only the controlled SDK; use the unmodified production constructor
    # so both main and SemanticVerifier requests read the configured mode.
    monkeypatch.setattr(module,'DeepSeekClient',DeepSeekClient)
    result=asyncio.run(runtime.chat(ChatRequest(message=H2)))
    assert_knowledge_complete(result,[('prerequisite',('business',),'saml_sso'),('meaning',('business',),'saml_sso')])
    assert sdk.review_count==result.business.semantic_review['dispatched']==1
    reviews=[r for r in sdk.requests if r['tool_choice']=={'type':'function','function':{'name':'adjudicate_turn'}}]
    mains=[r for r in sdk.requests if r not in reviews]
    assert len(reviews)==1 and mains
    assert all(r['model']=='deepseek-flash' and all(t['function']['strict'] is True for t in r['tools'])
        for r in sdk.requests)
    assert reviews[0]['temperature']==0 and len(reviews[0]['tools'])==1
    # Main requests now disclose explanation only after owning a real search
    # receipt. Preserve the single review and exact original four dispatches.
    assert [len(r['tools']) for r in mains] == [6, 6, 7, 7]
    assert all('explain_knowledge' not in {t['function']['name'] for t in r['tools']}
        for r in mains[:2])
    assert all('explain_knowledge' in {t['function']['name'] for t in r['tools']}
        for r in mains[2:])
    assert not result.business.requirements.items and not result.business.requirements.condition_assertions
    assert result.business.pricing is None and result.business.proposal is None
    same_history(runtime,result,H2)
    assert runtime.quotes.list_quotes()==[]


def test_standard_request_and_sdk_arguments_are_unchanged(monkeypatch):
    original_tools=tools()
    messages=[{'role':'user','content':'Team单价？'}]
    raw='{"intent":"unit_price","consultations":[]}'
    result=answer(raw)
    sdk=fake_sdk(monkeypatch,result)
    client=DeepSeekClient(settings())
    returned=asyncio.run(client.chat(messages=messages,tools=original_tools,max_completion_tokens=123,
        temperature=.7,tool_choice={'type':'function','function':{'name':'update_requirements'}},max_retries=0))
    payload=sdk.requests[0]
    assert sdk.clients[0].init['base_url']=='https://api.deepseek.com'
    assert returned is result and returned.choices[0].message.tool_calls[0].function.arguments==raw
    assert payload['tools']==original_tools and payload['messages']==messages
    assert payload['max_tokens']==123 and payload['temperature']==.7
    assert payload['extra_body']=={'thinking':{'type':'disabled'}}
    assert sdk.options==[{'max_retries':0,'timeout':45.0}]


def test_opt_in_projects_all_tools_and_preserves_explicit_values_and_sdk_receipt(monkeypatch):
    source=tools()
    value={'intent':'knowledge','selected_plan_id':None,'consultations':[{
        'item_id':'meaning','aspect':'meaning','topic':'SSO含义','span':{'quote':'SSO是什么','start':None},
        'plan_ids':[],'all_plans':False,'feature_id':None,'depends_on':[]}]}
    raw=ToolContract(source).encode_arguments('update_requirements', value)
    sdk_result=answer(raw)
    sdk=fake_sdk(monkeypatch,sdk_result)
    original=deepcopy(source)
    returned=asyncio.run(DeepSeekClient(settings(),tool_output_mode='deepseek_strict_beta').chat(
        messages=[{'role':'user','content':'SSO是什么'}],tools=source,max_completion_tokens=2048))
    assert sdk.clients[0].init['base_url']=='https://api.deepseek.com/beta'
    assert len(sdk.requests[0]['tools'])==7 and all(t['function']['strict'] is True for t in sdk.requests[0]['tools'])
    assert sdk.requests[0]['max_tokens']==2048
    assert json.loads(returned.choices[0].message.tool_calls[0].function.arguments)==value
    assert sdk_result.choices[0].message.tool_calls[0].function.arguments==raw
    assert returned.choices[0].message.tool_calls[0].id=='actual-id' and source==original


def test_previous_actual_call_is_encoded_for_wire_without_mutating_business_history(monkeypatch):
    source=tools(); contract=ToolContract(source)
    value={'intent':'knowledge','consultations':[]}
    raw=json.dumps(value)
    messages=[{'role':'user','content':'咨询'}, {'role':'assistant','content':None,'tool_calls':[{
        'id':'previous-id','type':'function','function':{'name':'update_requirements','arguments':raw}}]},
        {'role':'tool','tool_call_id':'previous-id','content':'{"ok":true}'}]
    original=deepcopy(messages)
    sdk=fake_sdk(monkeypatch,answer(contract.encode_arguments('update_requirements',value)))
    asyncio.run(DeepSeekClient(settings(),tool_output_mode='deepseek_strict_beta').chat(messages=messages,tools=source))
    encoded=sdk.requests[0]['messages'][1]['tool_calls'][0]
    assert encoded['id']=='previous-id'
    assert json.loads(contract.decode_arguments('update_requirements',encoded['function']['arguments']))==value
    assert messages==original and 'state' not in raw


@pytest.mark.parametrize('raw',['{"intent":', '{"unknown":true}'])
def test_illegal_wire_is_rejected_without_repair_or_retry(monkeypatch,raw):
    sdk=fake_sdk(monkeypatch,answer(raw))
    with pytest.raises(DeepSeekToolOutputInvalid):
        asyncio.run(DeepSeekClient(settings(),tool_output_mode='deepseek_strict_beta').chat(
            messages=[{'role':'user','content':'咨询'}],tools=tools()))
    assert len(sdk.requests)==1


def test_length_keeps_actual_stop_reason_and_unrepaired_arguments(monkeypatch):
    sdk=fake_sdk(monkeypatch,answer('{"intent":','length'))
    returned=asyncio.run(DeepSeekClient(settings(),tool_output_mode='deepseek_strict_beta').chat(
        messages=[{'role':'user','content':'咨询'}],tools=tools()))
    assert returned.choices[0].finish_reason=='length'
    assert returned.choices[0].message.tool_calls[0].function.arguments=='{"intent":'
    assert len(sdk.requests)==1


def test_actual_projected_request_still_obeys_context_budget_before_dispatch(monkeypatch):
    sdk=fake_sdk(monkeypatch,answer('{}'))
    with pytest.raises(ModelRequestInvalid,match='strict_wire_context_budget'):
        asyncio.run(DeepSeekClient(settings(deepseek_max_context_tokens=50),tool_output_mode='deepseek_strict_beta').chat(
            messages=[{'role':'user','content':'咨询'}],tools=tools()))
    assert not sdk.requests


@pytest.mark.parametrize('raw',['{"intent":"knowledge"}', '{"intent":'])
def test_truncated_candidate_never_reaches_review_even_if_json_is_valid(engine,monkeypatch,raw):
    provider=scripted(monkeypatch,[completion(tool('update_requirements',raw),finish_reason='length')])
    result=asyncio.run(engine.chat(ChatRequest(message='SSO是什么？')))
    assert result.status=='incomplete' and error_codes(result)=={'model_output_exhausted'}
    assert result.business.semantic_review['error_code']=='candidate_truncated'
    assert not provider.review_calls and not result.tool_results
    assert not result.business.requirements.items
    assert_stored_exactly(engine,result,'SSO是什么？')


def test_transport_shape_failure_has_candidate_stage_and_no_review(engine,monkeypatch):
    provider=scripted(monkeypatch,[DeepSeekToolOutputInvalid('strict_tool_output_invalid')])
    result=asyncio.run(engine.chat(ChatRequest(message='SSO是什么？')))
    assert result.status=='incomplete' and error_codes(result)=={'semantic_review_candidate_invalid'}
    assert result.business.semantic_review['dispatched']==0 and not provider.review_calls
    assert '尚未进行独立核对与业务执行' in result.message
    assert_stored_exactly(engine,result,'SSO是什么？')


def test_upstream_bad_request_keeps_actual_review_dispatch(engine,monkeypatch):
    provider=scripted(monkeypatch,[completion(tool('update_requirements',{'intent':'knowledge'}))],
        review_outputs=[ModelRequestInvalid('upstream_bad_request')])
    result=asyncio.run(engine.chat(ChatRequest(message='SSO是什么？')))
    assert result.status=='incomplete' and error_codes(result)=={'semantic_review_unavailable'}
    assert len(provider.review_calls)==1 and result.business.semantic_review['dispatched']==1
    assert 'request_rejection' not in result.business.semantic_review
    assert not result.business.requirements.items and result.business.proposal is None
    assert_stored_exactly(engine,result,'SSO是什么？')
