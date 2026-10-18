"""Business evidence: genuine cached CPU retrieval and explicit failure seams.

Normal observations use the production child process and the formal knowledge index.
Worker-return substitutions below prove validation/error propagation only; they
are never counted as successful Embedding or a semantic entailment evaluation.
All file mutations are confined to temporary copies.
"""

from hashlib import sha256
import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from app.schemas.matching import ConditionAssertion, HardRequirement, RequirementSet
from app.schemas.retrieval import RetrievalOptions
from app.services.catalog import get_catalog
from app.services.knowledge import DEFAULT_CORPUS_DIR, DEFAULT_INDEX_PATH, PROJECT_ROOT, KnowledgeError, canonical_json
from app.services.matching import match_requirements
from app.services.retrieval import KnowledgeService


CONDITION = "saml_sso_compatible_idp"
SSO_QUERIES = ("统一登录需要哪些前提", "接企业身份系统前要具备什么")


def requirement(feature="saml_sso", assertion=None):
    values = dict(requirement_id="retrieval-needs", items=(HardRequirement(
        requirement_id=f"need-{feature}", statement=f"必须具备 {feature}",
        user_quote=f"我们需要 {feature}", feature_id=feature),))
    if assertion is not None:
        quotes = {"yes": "我们现在已经具备兼容的身份提供方。",
                  "no": "我们现在没有兼容的身份提供方。",
                  "unknown": "以后准备，目前还不确定。"}
        values["condition_assertions"] = (ConditionAssertion(
            condition_id=CONDITION, value=assertion, user_quote=quotes[assertion]),)
    return RequirementSet(**values)


def business_item(result):
    return next(plan for plan in result.plans if plan.plan_id == "business").items[0]


@pytest.fixture(scope="module")
def real_observations():
    """Missing model/cache or a broken formal index fails; never skip reality."""
    service = KnowledgeService()
    synonyms = tuple(service.retrieve(query) for query in SSO_QUERIES)
    detail = service.retrieve("审计日志具体保存多少天？")
    unrelated = service.retrieve("澄星协作是否提供自动驾驶汽车道路测试服务？")
    low = service.retrieve(SSO_QUERIES[0], options=RetrievalOptions(min_score=1.0))
    empty = service.retrieve(SSO_QUERIES[0], options=RetrievalOptions(max_total_chars=100))
    api = service.retrieve("产品 API 与 Webhook 的集成用途分别是什么？")
    return {"service": service, "synonyms": synonyms, "detail": detail,
            "unrelated": unrelated, "low": low, "empty": empty, "api": api}


@pytest.mark.parametrize("position", [0, 1])
def test_real_chinese_synonyms_retrieve_complete_sso_premise(real_observations, position):
    result = real_observations["synonyms"][position]
    assert result.status == "retrieved", result.model_dump()
    assert result.query == SSO_QUERIES[position]
    assert result.error is None
    assert result.embedding.dimension == 512
    assert result.embedding.model == "BAAI/bge-small-zh-v1.5"
    assert result.embedding.execution_providers == ("CPUExecutionProvider",)
    condition_hits = [hit for hit in result.hits if CONDITION in hit.source.condition_ids]
    assert len(condition_hits) == 1
    source = condition_hits[0].source
    assert condition_hits[0].relevance == "related"
    assert get_catalog().conditions[0].statement in source.raw_text
    assert "未来计划准备不能当作当前具备" in source.raw_text
    assert "助手不连接客户身份系统验证真实环境" in source.raw_text
    assert source.plan_ids == ("business",)
    assert source.feature_ids == ("saml_sso",)


def test_real_returned_sources_are_original_whole_sections_with_bounded_text(real_observations):
    for result in real_observations["synonyms"]:
        assert 1 <= len(result.hits) <= result.options.top_k == 4
        assert sum(len(hit.source.raw_text) for hit in result.hits) <= 1400
        assert len({hit.source.source_id for hit in result.hits}) == len(result.hits)
        assert [hit.score for hit in result.hits] == sorted((hit.score for hit in result.hits), reverse=True)
        for hit in result.hits:
            source = hit.source
            lines = (DEFAULT_CORPUS_DIR / source.relative_path).read_text(encoding="utf-8").splitlines()
            assert source.raw_text == "\n".join(lines[source.line_start - 1:source.line_end])
            assert source.raw_text.startswith("## " + source.section_title + "\n")
            assert len(source.raw_text) <= 420
            assert source.dataset_version == result.dataset_version == get_catalog().version
            assert sha256(source.raw_text.encode("utf-8")).hexdigest() == source.text_sha256


def test_actual_low_scores_and_budget_empty_have_different_meanings(real_observations):
    low, empty = real_observations["low"], real_observations["empty"]
    assert low.status == "low_relevance"
    assert low.hits and all(hit.relevance == "low" for hit in low.hits)
    assert low.error is None
    assert empty.status == "empty"
    assert empty.hits == ()
    assert empty.error is None
    assert empty.excluded_source_ids
    assert "text_budget_excluded" in empty.reason_codes
    service = real_observations["service"]
    for evidence in (low, empty):
        result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                    retrieval_ids=(evidence.retrieval_id,))
        assert result.status == "blocked"
        item = business_item(result)
        assert item.status == "pending_condition"
        assert "condition_evidence_missing" in item.reason_codes
        assert "catalog_unsupported" not in item.reason_codes


def test_related_unanswered_detail_and_unrelated_query_never_claim_semantic_support(real_observations):
    detail, unrelated = real_observations["detail"], real_observations["unrelated"]
    assert detail.status == "retrieved"
    assert any("保存期限" in hit.source.raw_text or "留存" in hit.source.raw_text for hit in detail.hits)
    assert detail.semantic_support == unrelated.semantic_support == "not_assessed"
    assert unrelated.status in ("retrieved", "low_relevance")
    assert detail.error is unrelated.error is None
    # Membership proves a real Source exists, not a numeric retention answer.
    source_id = detail.hits[0].source.source_id
    returned = real_observations["service"].validate_citations(detail.retrieval_id, (source_id,))
    assert returned[0] == detail.hits[0].source
    assert "日志覆盖范围、保存期限、不可篡改保证及合规认证均未在本版资料中说明" in "\n".join(
        hit.source.raw_text for hit in detail.hits)


def test_finite_original_text_review_records_related_but_unanswered_detail(real_observations):
    """An explicit prose review is separate from vector relevance and lookup."""
    service = real_observations["service"]
    detail = real_observations["detail"]
    source_id = next(hit.source.source_id for hit in detail.hits if "保存期限" in hit.source.raw_text)
    reviewed = service.record_semantic_review(detail.retrieval_id, verdict="insufficient",
        conclusion="当前资料能否给出审计日志具体保存天数？", source_ids=(source_id,),
        rationale="实际原文明确说明保存期限未规定，不能从审计能力存在推导具体天数。",
        origin="server_review")
    assert reviewed.status == "retrieved"
    assert reviewed.semantic_support == "insufficient"
    assert reviewed.semantic_review.source_ids == (source_id,)
    assert reviewed.hits == detail.hits
    assert reviewed.query == detail.query
    assert service.evidence(detail.retrieval_id) == reviewed


def test_supported_free_prose_review_cannot_substitute_for_condition_or_unmodeled_requirement(real_observations):
    service = real_observations["service"]
    api = real_observations["api"]
    reviewed = service.record_semantic_review(api.retrieval_id, verdict="supported",
        conclusion="产品 API 与 Webhook 承担不同集成用途。",
        source_ids=tuple(hit.source.source_id for hit in api.hits),
        rationale="实际片段分别解释 API 与事件通知的用途，未形成 SSO 的条件来源。",
        origin="server_review")
    assert reviewed.semantic_support == "supported"
    sso = match_requirements(requirement(assertion="yes"), knowledge=service,
                             retrieval_ids=(api.retrieval_id,))
    assert sso.status == "blocked"
    assert "condition_evidence_missing" in business_item(sso).reason_codes
    request = RequirementSet(requirement_id="unmodeled-needs", items=(HardRequirement(
        requirement_id="scim", statement="必须支持 SCIM 自动开通", user_quote="必须自动开通账号", kind="unmodeled"),))
    scim = match_requirements(request, knowledge=service, retrieval_ids=(api.retrieval_id,))
    assert scim.status == "blocked"
    assert all(plan.items[0].status == "unknown" for plan in scim.plans)


def test_citations_only_accept_sources_returned_in_that_observation(real_observations):
    service = real_observations["service"]
    observation = real_observations["synonyms"][0]
    returned = tuple(hit.source.source_id for hit in observation.hits)
    assert tuple(source.source_id for source in service.validate_citations(observation.retrieval_id, returned)) == returned
    envelope = json.loads(DEFAULT_INDEX_PATH.read_text(encoding="utf-8"))
    outside = next(source["source_id"] for source in envelope["payload"]["inputs"]["sources"]
                   if source["source_id"] not in returned)
    with pytest.raises(KnowledgeError) as caught:
        service.validate_citations(observation.retrieval_id, (outside,))
    assert caught.value.code == "citation_not_returned"
    with pytest.raises(KnowledgeError) as caught:
        service.validate_citations(observation.retrieval_id, ("fabricated-source",))
    assert caught.value.code == "citation_not_returned"


@pytest.mark.parametrize("assertion,status,reason", [
    ("yes", "supported", None), ("unknown", "pending_condition", "condition_unknown"),
    ("no", "unsupported", "condition_not_met"),
])
def test_true_condition_evidence_and_explicit_user_facts_jointly_change_match(real_observations, assertion, status, reason):
    service = real_observations["service"]
    observation = real_observations["synonyms"][0]
    request = requirement(assertion=assertion)
    result = match_requirements(request, knowledge=service, retrieval_ids=(observation.retrieval_id,))
    item = business_item(result)
    assert item.status == status
    assert item.condition_evidence_status == "valid"
    assert item.condition_assertion_status == assertion
    assert item.condition_user_quote == request.condition_assertions[0].user_quote
    assert result.evidence_origin == "retrieval"
    assert result.retrieval_evidence == (observation,)
    assert item.evidence_source_ids
    source = service.validate_citations(observation.retrieval_id, item.evidence_source_ids)[0]
    assert CONDITION in source.condition_ids
    for plan in result.plans[:2]:
        assert plan.items[0].status == "unsupported"
        assert plan.items[0].reason_codes == ("catalog_unsupported",)
    if reason:
        assert result.status == "blocked"
        assert reason in item.reason_codes
    else:
        assert result.status == "matched"
        assert result.eligible_plan_ids == ("business",)
        assert result.lowest_plan_id == result.selected_plan_id == "business"


def test_explicit_absent_receipt_blocks_even_after_another_retrieval(real_observations):
    service = real_observations["service"]
    result = match_requirements(requirement(assertion="yes"), knowledge=service, retrieval_ids=())
    assert result.status == "blocked"
    assert result.retrieval_evidence == ()
    assert business_item(result).condition_evidence_status == "missing"
    assert "condition_evidence_missing" in business_item(result).reason_codes


def test_known_missing_user_premise_preserves_no_even_when_evidence_is_missing():
    result = match_requirements(requirement(assertion="no"), knowledge=KnowledgeService(), retrieval_ids=())
    item = business_item(result)
    assert item.status == "unsupported"
    assert item.condition_assertion_status == "no"
    assert "condition_not_met" in item.reason_codes
    assert "condition_evidence_missing" in item.reason_codes


def test_only_returned_sso_condition_not_a_related_api_receipt_can_pass(real_observations):
    observation = real_observations["api"]
    assert observation.status == "retrieved"
    assert not any(CONDITION in hit.source.condition_ids for hit in observation.hits)
    result = match_requirements(requirement(assertion="yes"), knowledge=real_observations["service"],
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert "condition_evidence_missing" in business_item(result).reason_codes


def test_new_business_chain_rejects_old_real_receipt_and_serialized_evidence(real_observations):
    observation = real_observations["synonyms"][0]
    service = KnowledgeService()
    with pytest.raises(KnowledgeError) as caught:
        service.evidence(observation.retrieval_id)
    assert caught.value.code == "evidence_not_in_chain"
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert "evidence_not_in_chain" in business_item(result).reason_codes
    with pytest.raises(TypeError):
        match_requirements(requirement(assertion="yes"), evidence=observation.model_dump(mode="json"))


def test_normal_matcher_entry_retrieves_real_condition_automatically():
    result = match_requirements(requirement(assertion="yes"))
    assert result.status == "matched"
    assert result.selected_plan_id == "business"
    assert result.evidence_origin == "retrieval"
    assert result.retrieval_evidence and result.retrieval_evidence[0].status == "retrieved"
    assert business_item(result).condition_evidence_status == "valid"


def test_shared_combined_user_quote_preserves_both_needs_and_embeds_accepted_sso_statement():
    shared_quote = "我们要统一登录和审计日志"
    request = RequirementSet(requirement_id="shared-original-input", items=(
        HardRequirement(requirement_id="sso", statement="必须统一登录", user_quote=shared_quote, feature_id="saml_sso"),
        HardRequirement(requirement_id="audit", statement="必须审计日志", user_quote=shared_quote, feature_id="audit_logs"),
    ), condition_assertions=(ConditionAssertion(condition_id=CONDITION, value="yes",
        user_quote="我们现在具备兼容的身份提供方。"),))
    result = match_requirements(request)
    assert result.status == "matched"
    assert result.lowest_plan_id == result.selected_plan_id == "business"
    assert len(result.retrieval_evidence) == 1
    assert result.retrieval_evidence[0].query == "必须统一登录"
    assert result.requirements.items[0].user_quote == result.requirements.items[1].user_quote == shared_quote
    business = next(plan for plan in result.plans if plan.plan_id == "business")
    assert tuple(item.status for item in business.items) == ("supported", "supported")
    assert business.items[0].condition_evidence_status == "valid"
    assert business.items[1].condition_id is None


def test_recognized_condition_conflict_keeps_returned_sources_and_only_blocks_sso():
    """This server-review injection proves propagation, not contradiction discovery."""
    service = KnowledgeService()
    observation = service.retrieve(SSO_QUERIES[0])
    source_ids = tuple(hit.source.source_id for hit in observation.hits
                       if "saml_sso" in hit.source.feature_ids)[:2]
    assert len(source_ids) == 2
    updated = service.record_condition_conflict(observation.retrieval_id, condition_id=CONDITION,
        source_ids=source_ids, reason="受控负面例：这两处相关条件解释已识别不一致。", origin="controlled_test")
    assert updated.conflicts[0].source_ids == source_ids
    assert updated.conflicts[0].origin == "controlled_test"
    sso = match_requirements(requirement(assertion="yes"), knowledge=service,
                             retrieval_ids=(observation.retrieval_id,))
    assert sso.status == "blocked"
    assert business_item(sso).condition_evidence_status == "conflict"
    assert set(source_ids) <= set(business_item(sso).evidence_source_ids)
    assert "condition_evidence_conflict" in business_item(sso).reason_codes
    # A clean re-retrieval and omitted conflict ID cannot hide a known conflict.
    clean = service.retrieve(SSO_QUERIES[0])
    assert clean.conflicts == ()
    repeated = match_requirements(requirement(assertion="yes"), knowledge=service,
        retrieval_ids=(clean.retrieval_id,))
    assert repeated.status == "blocked"
    assert business_item(repeated).condition_evidence_status == "conflict"
    assert updated.retrieval_id in {item.retrieval_id for item in repeated.retrieval_evidence}
    # Controlled ledger-eviction seam only: emulate this record leaving the
    # ordinary 16-observation window, without 17 needless native-model calls.
    service._issued.pop(updated.retrieval_id)
    assert service.evidence(updated.retrieval_id) == updated
    after_eviction = match_requirements(requirement(assertion="yes"), knowledge=service,
        retrieval_ids=(clean.retrieval_id,))
    assert after_eviction.status == "blocked"
    assert business_item(after_eviction).condition_evidence_status == "conflict"
    for feature, expected in (("api_access", "team"), ("audit_logs", "business")):
        unaffected = match_requirements(requirement(feature), knowledge=service,
                                        retrieval_ids=(observation.retrieval_id,))
        assert unaffected.status == "matched"
        assert unaffected.selected_plan_id == expected


def test_conflict_review_cannot_invent_a_source(real_observations):
    observation = real_observations["synonyms"][0]
    with pytest.raises(KnowledgeError) as caught:
        real_observations["service"].record_condition_conflict(observation.retrieval_id,
            condition_id=CONDITION, source_ids=("fabricated-source",), reason="无真实来源", origin="controlled_test")
    assert caught.value.code == "citation_not_returned"


def test_index_or_body_changes_after_real_receipt_stop_reuse(real_observations, tmp_path, monkeypatch):
    service = real_observations["service"]
    observation = real_observations["synonyms"][0]
    corpus = tmp_path / "knowledge"
    shutil.copytree(DEFAULT_CORPUS_DIR, corpus)
    path = corpus / "01_overview.md"
    path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr(service, "_corpus_dir", corpus)
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert "index_corpus_mismatch" in business_item(result).reason_codes
    assert business_item(result).condition_evidence_status == "unavailable"
    with pytest.raises(KnowledgeError) as caught:
        service.validate_citations(observation.retrieval_id, (observation.hits[0].source.source_id,))
    assert caught.value.code == "index_corpus_mismatch"


def test_custom_catalog_mismatch_cannot_accept_formal_receipt(real_observations):
    catalog = get_catalog()
    changed = catalog.model_copy(update={"version": "chengxing-demo-v2"})
    result = match_requirements(requirement(assertion="yes"), catalog=changed,
        knowledge=real_observations["service"], retrieval_ids=(real_observations["synonyms"][0].retrieval_id,))
    assert result.status == "blocked"
    assert "condition_evidence_version_mismatch" in business_item(result).reason_codes


def reseal_index(path, change):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    change(envelope["payload"])
    payload = envelope["payload"]
    payload["inputs_sha256"] = sha256(canonical_json(payload["inputs"]).encode("utf-8")).hexdigest()
    envelope["payload_sha256"] = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("mutation,expected", [
    ("catalog", "index_catalog_mismatch"), ("embedding", "index_embedding_mismatch"),
    ("body", "index_corpus_mismatch"),
])
def test_real_worker_rejects_changed_isolated_index_or_corpus(tmp_path, mutation, expected):
    corpus = tmp_path / "knowledge"
    shutil.copytree(DEFAULT_CORPUS_DIR, corpus)
    index_path = tmp_path / "index.json"
    shutil.copyfile(DEFAULT_INDEX_PATH, index_path)
    if mutation == "body":
        path = corpus / "01_overview.md"
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "catalog":
        reseal_index(index_path, lambda payload: payload["inputs"].__setitem__("catalog_sha256", "0" * 64))
    else:
        reseal_index(index_path, lambda payload: payload["inputs"]["embedding"].__setitem__("query_prefix", "changed representation"))
    service = KnowledgeService(corpus_dir=corpus, index_path=index_path)
    observation = service.retrieve(SSO_QUERIES[0])
    assert observation.status == "unavailable"
    assert observation.hits == ()
    assert observation.error.step == "index_validation"
    assert observation.error.code == expected
    assert expected in observation.reason_codes
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert expected in business_item(result).reason_codes
    assert "catalog_unsupported" not in business_item(result).reason_codes
    assert match_requirements(requirement("api_access"), knowledge=service,
        retrieval_ids=(observation.retrieval_id,)).selected_plan_id == "team"


def test_actual_killable_worker_deadline_is_not_retrieval_empty():
    service = KnowledgeService(timeout_seconds=0.001)
    observation = service.retrieve(SSO_QUERIES[0])
    assert observation.status == "unavailable"
    assert observation.hits == ()
    assert observation.error.code == "retrieval_timeout"
    assert "retrieval_timeout" in observation.reason_codes
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert "retrieval_timeout" in business_item(result).reason_codes
    assert business_item(result).condition_evidence_status == "unavailable"


def test_real_isolated_source_removal_stops_sso_without_polluting_formal_corpus(tmp_path, real_observations):
    """23 genuine document vectors + a genuine query; no old chain receipt."""
    import math

    from app.config import get_settings
    from app.llm.embedding import LocalEmbeddingClient
    from app.services.catalog import DEFAULT_CATALOG_PATH, load_catalog
    from app.services.knowledge import load_index
    from scripts.check_retrieval import NEGATIVE_VERSION, NegativeKnowledgeService, build_negative_index

    settings = get_settings()
    client = LocalEmbeddingClient(settings, offline=True)
    formal = load_index(client)
    original_index = DEFAULT_INDEX_PATH.read_bytes()
    original_files = {path: path.read_bytes() for path in DEFAULT_CORPUS_DIR.iterdir() if path.is_file()}
    corpus = tmp_path / "negative-knowledge"
    shutil.copytree(DEFAULT_CORPUS_DIR, corpus)
    catalog_path, index_path = tmp_path / "negative-catalog.json", tmp_path / "negative-index.json"
    catalog_data = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_data["version"] = NEGATIVE_VERSION
    catalog_path.write_text(json.dumps(catalog_data, ensure_ascii=False), encoding="utf-8")
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = NEGATIVE_VERSION
    removed = next(source for source in formal.inputs.sources if CONDITION in source.condition_ids)
    path = corpus / removed.relative_path
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[removed.line_start - 1:removed.line_end]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for doc in manifest["documents"]:
        doc["sections"] = [section for section in doc["sections"] if section["section_id"] != removed.section_id]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    negative = build_negative_index(client, corpus, catalog_path, index_path)
    assert negative.scope == "w05_negative_experiment"
    assert len(negative.inputs.sources) == len(negative.vectors) == 23
    assert not any(CONDITION in source.condition_ids or get_catalog().conditions[0].statement in source.raw_text
                   for source in negative.inputs.sources)
    service = NegativeKnowledgeService(client=client, catalog_path=catalog_path, settings=settings,
        corpus_dir=corpus, index_path=index_path)
    old_id = real_observations["synonyms"][0].retrieval_id
    with pytest.raises(KnowledgeError) as caught:
        service.evidence(old_id)
    assert caught.value.code == "evidence_not_in_chain"
    observation = service.retrieve(SSO_QUERIES[0])
    assert observation.status == "retrieved"
    assert observation.scope == "negative_experiment"
    assert observation.dataset_version == NEGATIVE_VERSION
    assert observation.hits and not any(CONDITION in hit.source.condition_ids for hit in observation.hits)
    vector = service.actual_query_vectors[observation.retrieval_id]
    assert len(vector) == 512 and math.isclose(math.sqrt(sum(value * value for value in vector)), 1, abs_tol=1e-5)
    request = requirement(assertion="yes")
    audit = HardRequirement(requirement_id="audit", statement="必须有审计日志", user_quote="我们也需要审计日志", feature_id="audit_logs")
    request = request.model_copy(update={"items": request.items + (audit,)})
    match = match_requirements(request, knowledge=service, catalog=load_catalog(catalog_path),
        retrieval_ids=(observation.retrieval_id,))
    assert match.status == "blocked"
    assert match.lowest_plan_id is None
    assert business_item(match).status == "pending_condition"
    assert business_item(match).condition_evidence_status == "missing"
    assert "condition_evidence_missing" in business_item(match).reason_codes
    business = next(plan for plan in match.plans if plan.plan_id == "business")
    assert business.items[1].status == "supported"
    with pytest.raises(KnowledgeError) as caught:
        load_index(client, index_path=index_path)
    assert caught.value.code == "index_scope_invalid"
    formal_service = KnowledgeService(index_path=index_path)
    assert formal_service.retrieve(SSO_QUERIES[0]).error.code == "index_scope_invalid"
    assert DEFAULT_INDEX_PATH.read_bytes() == original_index
    assert all(path.read_bytes() == raw for path, raw in original_files.items())
    assert load_index(client) == formal


def test_timeout_return_substitute_proves_error_propagation_only(real_observations, monkeypatch):
    """A failed new lookup cannot quietly reuse the chain's earlier real hit."""
    service = real_observations["service"]
    old_observation = real_observations["synonyms"][0]
    assert service.evidence(old_observation.retrieval_id).status == "retrieved"
    def deadline(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", deadline)
    observation = service.retrieve(SSO_QUERIES[0])
    assert observation.status == "unavailable"
    assert observation.error.code == "retrieval_timeout"
    assert observation.hits == ()
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
        retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert result.retrieval_evidence == (observation,)
    assert business_item(result).status == "pending_condition"
    assert business_item(result).condition_evidence_status == "unavailable"
    assert "retrieval_timeout" in business_item(result).reason_codes


@pytest.mark.parametrize("bad", ["identity", "rewritten_source", "relevance", "failed_with_hits"])
def test_bad_worker_return_cannot_be_issued_as_valid_condition(real_observations, monkeypatch, bad):
    """Only _execute is substituted; the payload starts from a genuine receipt."""
    service = KnowledgeService()
    original = real_observations["synonyms"][0]
    def malformed(request):
        copied = original.model_copy(update={"retrieval_id": request["retrieval_id"], "chain_id": request["chain_id"]})
        if bad == "identity":
            return copied.model_copy(update={"query": "another request"})
        if bad == "rewritten_source":
            source = copied.hits[0].source.model_copy(update={"raw_text": "伪造的产品承诺。"})
            hit = copied.hits[0].model_copy(update={"source": source})
            return copied.model_copy(update={"hits": (hit,) + copied.hits[1:]})
        if bad == "relevance":
            hit = copied.hits[0].model_copy(update={"score": -0.9, "relevance": "related"})
            return copied.model_copy(update={"hits": (hit,) + copied.hits[1:]})
        return copied.model_copy(update={"status": "unavailable"})
    monkeypatch.setattr(service, "_execute", malformed)
    observation = service.retrieve(original.query)
    assert observation.status == "unavailable"
    assert observation.hits == ()
    assert observation.error is not None
    result = match_requirements(requirement(assertion="yes"), knowledge=service,
                                retrieval_ids=(observation.retrieval_id,))
    assert result.status == "blocked"
    assert business_item(result).condition_evidence_status == ("invalid" if bad == "rewritten_source" else "unavailable")


def test_oversized_real_query_is_rejected_before_truncating_into_false_evidence():
    observation = KnowledgeService().retrieve("统一登录前提" * 150)
    assert observation.status == "unavailable"
    assert observation.error.step == "query_embedding"
    assert observation.error.code == "invalid_query"
    assert observation.hits == ()


@pytest.mark.parametrize("options", [
    {"top_k": 1}, {"top_k": 6}, {"min_score": float("nan")},
    {"max_total_chars": 50},
])
def test_bad_retrieval_options_do_not_reach_model(options):
    with pytest.raises(ValidationError):
        RetrievalOptions(**options)


@pytest.mark.parametrize("scope_kind", ["current", "legacy"])
def test_verification_report_guard_recognizes_own_report_scopes_without_rewriting(tmp_path, scope_kind):
    from app.config import Settings
    from scripts.check_retrieval import LEGACY_REPORT_SCOPE, REPORT_SCOPE, guard_report_target

    scope = REPORT_SCOPE if scope_kind == "current" else LEGACY_REPORT_SCOPE
    path = tmp_path / "check_retrieval.json"
    original = json.dumps({"scope": scope}).encode("utf-8")
    path.write_bytes(original)

    assert REPORT_SCOPE == "finite_retrieval_and_matching_checks"
    assert guard_report_target(path, Settings(_env_file=None)) == path.resolve()
    assert path.read_bytes() == original


@pytest.mark.parametrize("target", ["catalog", "index", "manifest", "another_artifact"])
def test_verification_cli_rejects_output_overwriting_business_inputs_or_other_artifacts(tmp_path, target):
    """Actual CLI guard exits before a model/experiment worker can start."""
    from app.services.catalog import DEFAULT_CATALOG_PATH

    protected = {"catalog": DEFAULT_CATALOG_PATH, "index": DEFAULT_INDEX_PATH,
                 "manifest": DEFAULT_CORPUS_DIR / "manifest.json"}
    if target == "another_artifact":
        path = tmp_path / "existing-unrelated.json"
        path.write_text('{"scope": "another_business_artifact", "must_survive": true}', encoding="utf-8")
    else:
        path = protected[target]
    original = path.read_bytes()
    completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.check_retrieval", "--offline",
        "--output", str(path)], cwd=PROJECT_ROOT, capture_output=True, encoding="utf-8", timeout=10)
    assert completed.returncode == 1
    assert "检索验证输出路径被拒绝" in completed.stderr
    assert "真实同义问题与条件原文" not in completed.stdout
    assert path.read_bytes() == original


def test_verification_timeout_cleanup_stops_only_its_private_worker_descendants(tmp_path):
    """Real owned process tree: the child must not finish after parent cleanup."""
    from scripts.check_retrieval import stop_worker_tree

    marker = tmp_path / "private-child-survived.txt"
    child_code = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print('private child started', flush=True); time.sleep(30)"
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, creationflags=flags, start_new_session=os.name != "nt")
    try:
        assert process.stdout.readline().strip() == "private child started"
        assert process.poll() is None
        stop_worker_tree(process)
        assert process.poll() is not None
        time.sleep(2.5)
        assert not marker.exists(), "The script terminated its parent but left its child running"
    finally:
        if process.poll() is None:
            stop_worker_tree(process)
        process.stdout.close()
        process.stderr.close()
