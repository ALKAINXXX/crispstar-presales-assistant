"""Regressions for the captured mixed selection/RAG completion failure."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.agents import orchestrator as module
from app.config import Settings
from app.llm.tool_protocol import validate_tool_message_sequence
from app.schemas.chat import ChatRequest
from app.schemas.semantic import SemanticReview
from app.services.work_state import WorkState
from app.tools.business import BusinessToolRegistry
from controlled_review import is_review_request, review_completion
from test_presales_orchestrator import call, completion, engine, script


CAPTURED = json.loads((Path(__file__).parent / 'fixtures' /
    'confirmed_source_instruction_case.json').read_text(encoding='utf-8'))


def apply_case(case):
    state = WorkState(Settings(_env_file=None))
    state.begin_review_turn()
    state.apply_review(SemanticReview.model_validate_json(
        json.dumps(case['review'], ensure_ascii=False)), case['message'], case['candidate'])
    return state


def test_actual_captured_source_instruction_uses_only_the_real_api_question():
    state = apply_case(CAPTURED)
    assert state.consultation_complete
    assert len(state.task_answer_questions) == 1
    assert [c.item_id for c in state.turn_consultations] == ['c2']
    assert len(state.consultation_questions) == 1
    assert state.consultation_questions[0]['plan_ids'] == ['team']
    alias = next(entry for entry in state.consultation_feedback if entry['item_id'] == 'c3')
    assert alias['status'] == 'accepted' and alias['purpose'] == 'answer_requirement'
    assert alias['plan_ids'] == ['team']
    registry = BusinessToolRegistry(state.settings, state, CAPTURED['message'], quotes=object())
    mapping = registry._consultation_unit_mapping()
    assert mapping['c3'] == mapping['c2']
    assert len(mapping['c3']) == 1
    assert state.task_answer_questions[0]['unit_id'] not in mapping['c3']


@pytest.mark.parametrize('instruction', [
    '请依据资料说明。', '请根据文档解释。', '请基于相关资料回答。',
])
@pytest.mark.parametrize('catalog_metadata', [False, True])
@pytest.mark.parametrize('instruction_first', [False, True])
def test_source_wording_and_item_order_do_not_create_an_extra_question(
        instruction, catalog_metadata, instruction_first):
    case = deepcopy(CAPTURED)
    case['message'] = case['message'].replace('请依据资料说明。', instruction)
    source = case['review']['consultations'][-1]
    source['span']['quote'] = instruction
    if not catalog_metadata:
        source['scope_basis'] = None
        source['plan_ids'] = ['team']
    if instruction_first:
        case['review']['consultations'] = [source, *case['review']['consultations'][:-1]]
    state = apply_case(case)
    assert state.consultation_complete
    assert len(state.consultation_questions) == 1
    assert next(e for e in state.consultation_feedback if e['item_id'] == 'c3')['plan_ids'] == ['team']


@pytest.mark.parametrize('instruction', [
    '请依据资料说明API的限流是多少。',
    '请根据文档解释SSO的配置步骤。',
    '请依据资料说明，并比较Basic和Team。',
    '请依据资料说明哪些费用另计。',
])
def test_a_new_business_question_is_not_hidden_as_a_source_requirement(instruction):
    case = deepcopy(CAPTURED)
    case['message'] = case['message'].replace('请依据资料说明。', instruction)
    case['review']['consultations'][-1]['span']['quote'] = instruction
    state = apply_case(case)
    assert not state.consultation_complete
    assert not any(e.get('purpose') == 'answer_requirement' for e in state.consultation_feedback)


@pytest.mark.parametrize('changes', [
    {'plan_ids': ['basic']},
    {'plan_ids': ['business']},
    {'feature_id': 'private_deployment'},
    {'reference_span': {'quote': '我们必须把澄星协作私有化部署在自己的服务器上。'}},
    {'scope_exclusions': [{'quote': 'Basic'}]},
])
def test_conflicting_specific_metadata_cannot_borrow_the_api_answer(changes):
    case = deepcopy(CAPTURED)
    case['review']['consultations'][-1].update(changes)
    state = apply_case(case)
    assert not state.consultation_complete
    assert not any(e.get('purpose') == 'answer_requirement' for e in state.consultation_feedback)


def test_accepting_the_instruction_still_requires_actual_retrieval(engine, monkeypatch):
    script(monkeypatch, [
        completion(call('update_requirements', CAPTURED['candidate'], 'candidate')),
        completion(call('match_requirements', {}, 'match')),
        completion(), completion(),
    ], CAPTURED['review'])
    response = asyncio.run(engine.chat(ChatRequest(message=CAPTURED['message'])))
    assert response.status == 'incomplete'
    assert response.business.consultation_coverage.status == 'incomplete'
    assert any(item.status == 'incomplete' for item in response.business.consultation_coverage.items)
    assert not response.business.proposal
    assert engine.quotes.list_quotes() == []


def test_captured_review_completes_with_real_match_search_and_citation(engine, monkeypatch):
    """Only model responses are controlled; business and local RAG tools are real."""
    class Provider:
        main_calls = 0
        reviews = 0

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs['messages'])
            if is_review_request(kwargs):
                type(self).reviews += 1
                assert self.reviews == 1
                return review_completion(CAPTURED['review'])
            type(self).main_calls += 1
            if self.main_calls == 1:
                return completion(
                    call('update_requirements', CAPTURED['candidate'], 'candidate'),
                    call('match_requirements', {}, 'actual-match'),
                    call('query_catalog', {}, 'actual-catalog'))
            if self.main_calls == 2:
                return completion(call('search_knowledge', {
                    'query': 'Team 团队版 产品 API 支持',
                    'consultation_item_ids': ['c3']}, 'actual-search'))
            if self.main_calls == 3:
                receipt = next(json.loads(m['content']) for m in kwargs['messages']
                    if m['role'] == 'tool' and m.get('tool_call_id') == 'actual-search')
                assert receipt['ok'] and receipt['data']['status'] == 'retrieved'
                source = next(hit['source'] for hit in receipt['data']['hits']
                    if hit['relevance'] == 'related' and 'api_access' in hit['source']['feature_ids'])
                return completion(call('explain_knowledge', {
                    'retrieval_id': receipt['data']['retrieval_id'],
                    'consultation_item_ids': ['c3'], 'answerability': 'supported',
                    'citations': [{'source_id': source['source_id'], 'quote': source['raw_text']}]},
                    'actual-explanation'))
            assert self.main_calls == 4
            return completion()

    monkeypatch.setattr(module, 'DeepSeekClient', Provider)
    response = asyncio.run(engine.chat(ChatRequest(message=CAPTURED['message'])))
    assert response.status == 'completed', response.message
    assert response.business.match.blocking_reasons == ('catalog_unsupported',)
    coverage = response.business.consultation_coverage
    assert coverage.scope_complete and coverage.status == 'completed'
    assert len(coverage.items) == 1 and coverage.items[0].plan_ids == ['team']
    assert coverage.items[0].retrieval_ids and coverage.items[0].source_ids
    assert response.business.knowledge_answers[0]['citations']
    assert all(record['result']['ok'] for record in response.tool_results)
    search = next(record['result']['data'] for record in response.tool_results
        if record['name'] == 'search_knowledge')
    assert search['scope'] == 'formal' and search['index_build_id']
    assert not response.business.proposal and not engine.quotes.list_quotes()
