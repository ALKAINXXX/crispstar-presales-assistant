"""Class-level ownership regressions, including captured real review shapes."""
import asyncio

import pytest

from app.schemas.chat import ChatRequest
from app.tools.business import BusinessToolRegistry, ConsultationBindingRejected
from test_consultation_scope_convergence import apply, item
from test_consultation_task_ownership import selection_review
from test_presales_orchestrator import call, completion, engine, review, script


@pytest.mark.parametrize('question', [
    'Basic、Team、Business中推荐哪个最低档套餐？',
    '请从Basic、Team、Business中选一个最低满足档。',
    'Basic、Team、Business哪个适合这个需求？',
    'Basic、Team、Business有能满足要求的吗？',
])
def test_reviewed_selection_scope_does_not_require_one_asking_phrase(question):
    message = '我们必须支持产品API，' + question
    inquiry, groups, target = selection_review(message, feature='api_access',
        requirement_quote='我们必须支持产品API', consultation_quote=question,
        scope_basis={'kind': 'named', 'span': {'quote': 'Basic、Team、Business'}})
    state = apply(message, [inquiry], intent='selection', groups=groups, target=target)
    assert state.consultation_complete and len(state.task_answer_questions) == 1
    assert not state.turn_consultations
    assert state.match().selected_plan_id == 'team'
    registry = BusinessToolRegistry(state.settings, state, message, quotes=object())
    assert 'search_knowledge' not in {s['function']['name'] for s in registry.available_schemas()}


def test_task_scope_does_not_erase_a_prior_subset_restriction():
    message = '必须支持审计日志。只考虑Basic和Team。请选最低满足档。'
    inquiry, groups, target = selection_review(message, consultation_quote='请选最低满足档。', all_plans=True)
    inquiry['plan_ids'] = []
    state = apply(message, [inquiry], intent='selection', groups=groups, target=target)
    assert not state.task_answer_questions and not state.consultation_complete


def test_task_evidence_pointer_does_not_truncate_the_question_scope():
    message = '必须支持审计日志。Basic、Team、Business推荐哪档？'
    inquiry, groups, target = selection_review(message,
        consultation_quote='Basic、Team、Business推荐哪档？')
    target['span'] = {'quote': '必须支持审计日志'}
    state = apply(message, [inquiry], intent='selection', groups=groups, target=target)
    assert state.consultation_complete and len(state.task_answer_questions) == 1


def test_selection_can_reference_its_actual_accepted_requirement():
    message = '必须支持审计日志，哪个套餐能满足？'
    inquiry, groups, target = selection_review(message, consultation_quote='哪个套餐能满足？',
        reference_span={'quote': '必须支持审计日志'}, all_plans=True,
        scope_basis={'kind': 'all_plans', 'span': {'quote': '哪个套餐能满足？'}})
    inquiry['plan_ids'] = []
    state = apply(message, [inquiry], intent='selection', groups=groups, target=target)
    assert state.consultation_complete and state.match().selected_plan_id == 'business'


def test_selection_reference_cannot_borrow_a_different_question():
    message = '必须支持审计日志，哪个套餐能满足？另外Team支持API吗？'
    inquiry, groups, target = selection_review(message, consultation_quote='哪个套餐能满足？',
        reference_span={'quote': 'Team支持API吗？'})
    state = apply(message, [inquiry], intent='selection', groups=groups, target=target)
    assert not state.task_answer_questions and not state.consultation_complete


@pytest.mark.parametrize('instruction', [
    '请引用资料里的原文。', '请提供对应出处。', '也请注明资料来源。', '并附上相应原句。',
])
@pytest.mark.parametrize('reversed_items', [False, True])
def test_sources_instruction_shares_the_real_question_identity(instruction, reversed_items):
    question = 'Business支持审计日志吗？'
    message = question + instruction
    core = item('support', question, ('business',), aspect='support', feature='audit_logs')
    constraint = item('sources', instruction, ('business',), aspect='detail', feature='audit_logs',
        reference_span={'quote': question})
    items = [constraint, core] if reversed_items else [core, constraint]
    state = apply(message, items)
    assert state.consultation_complete and len(state.consultation_questions) == 1
    assert [c.item_id for c in state.turn_consultations] == ['support']
    registry = BusinessToolRegistry(state.settings, state, message, quotes=object())
    mapping = registry._consultation_unit_mapping()
    assert mapping['sources'] == mapping['support']
    assert registry._consultation_binding(['sources']) == ('sources',)


@pytest.mark.parametrize('extra', [
    '请引用API限流的原文。', '请提供配置步骤的出处。', '请给出CSV格式的资料。',
    '请提供Team支持API的原文。',
])
def test_a_real_extra_detail_cannot_be_swallowed_as_a_source_instruction(extra):
    question = 'Business支持审计日志吗？'
    state = apply(question + extra, [
        item('support', question, ('business',), aspect='support', feature='audit_logs'),
        item('extra', extra, ('business',), aspect='detail', feature='audit_logs', reference_span={'quote': question}),
    ])
    assert not state.consultation_complete
    assert not any(f.get('purpose') == 'answer_requirement' for f in state.consultation_feedback)


def test_source_instruction_does_not_borrow_another_plan():
    message = 'Business支持审计日志吗？请引用原文。'
    state = apply(message, [item('support', 'Business支持审计日志吗？', ('business',),
        aspect='support', feature='audit_logs'),
        item('extra', '请引用原文。', ('team',), aspect='detail', feature='audit_logs')])
    assert not state.consultation_complete


def test_search_schema_contains_only_real_independent_query_identities():
    message = '必须支持审计日志，哪个套餐能满足？另外Team支持API吗？'
    inquiry, groups, target = selection_review(message, consultation_quote='哪个套餐能满足？')
    independent = item('api', 'Team支持API吗？', ('team',), aspect='support', feature='api_access')
    state = apply(message, [inquiry, independent], intent='selection', groups=groups, target=target)
    registry = BusinessToolRegistry(state.settings, state, message, quotes=object())
    search = next(s for s in registry.available_schemas() if s['function']['name'] == 'search_knowledge')
    allowed = search['function']['parameters']['properties']['consultation_item_ids']['items']['enum']
    assert 'api' in allowed and state.task_answer_questions[0]['unit_id'] not in allowed
    with pytest.raises(ConsultationBindingRejected):
        registry._consultation_binding([state.task_answer_questions[0]['unit_id']])


def test_source_instruction_still_requires_real_search_and_explanation(engine, monkeypatch):
    message = 'Business支持审计日志吗？请引用资料里的原文。'
    queries = [item('support', 'Business支持审计日志吗？', ('business',), aspect='support', feature='audit_logs'),
        item('sources', '请引用资料里的原文。', ('business',), aspect='detail', feature='audit_logs',
             reference_span={'quote': 'Business支持审计日志吗？'})]
    script(monkeypatch, [completion(call('update_requirements', {'intent': 'knowledge'}, 'candidate')),
                        completion(), completion()], review(queries))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == 'incomplete'
    assert response.business.consultation_coverage.items[0].status == 'incomplete'
    assert not response.business.proposal and engine.quotes.list_quotes() == []
