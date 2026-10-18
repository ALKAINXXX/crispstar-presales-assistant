"""Checker guard tests only; synthetic DOM text is not browser evidence."""

import json

import pytest
from types import SimpleNamespace

from app.schemas.chat import AnswerPresentation, BusinessResponse, ChatResponse, CurrentTask
from app.schemas.matching import DeclaredParameter, HardRequirement, RequirementSet
from app.services.catalog import get_catalog
from app.services.pricing import calculate_subscription_price, get_unit_price
from app.services.matching import match_requirements
from scripts.check_ui_evidence import latest_report, validate_report_identity, validate_view
from scripts import check_ui_evidence


def rate_view():
    catalog = get_catalog()
    rate = get_unit_price("team")
    response = ChatResponse(message="受控单价文本", model="fixture-only", session_id="fixture-session",
        status="completed", business=BusinessResponse(turn_id="fixture-turn", intent="unit_price",
            requirements=RequirementSet(requirement_id="fixture-requirements"), catalog_context=catalog, unit_price=rate))
    status = "本轮执行完成（completed）目录与单价（unit_price）fixture-session fixture-turn"
    amount = f"{rate.plan_name}（team）单价80.00 元／席／月 CNY {catalog.version} 仅有单价，未计算总价"
    tools = "本轮没有工具调用记录。"
    sections = [{"section": name, "visible": value, "allText": value} for name, value in (
        ("status", status), ("answer", response.message), ("pricing", amount), ("tools", tools),
    )]
    observation = {"label": "synthetic-checker-unit-rate", "visible": "\n".join(s["visible"] for s in sections),
        "allText": "".join(s["allText"] for s in sections), "sections": sections}
    return response, observation, catalog


def test_checker_rejects_negative_model_report_as_normal_evidence():
    with pytest.raises(AssertionError, match="不是正常真实模型"):
        validate_report_identity({"kind": "controlled_model_fault_real_services_isolated_db", "cases": [{}]})


def test_latest_report_filters_empty_and_negative_runs(tmp_path):
    expected = tmp_path / "browser-formal.json"
    expected.write_text(json.dumps({"kind": "real_browser_real_model_formal_index_isolated_db", "cases": [{}]}), encoding="utf-8")
    (tmp_path / "browser-empty.json").write_text(json.dumps({"kind": "real_browser_real_model_formal_index_isolated_db", "cases": []}), encoding="utf-8")
    (tmp_path / "browser-negative.json").write_text(json.dumps({"kind": "controlled_model_fault_real_services_isolated_db", "cases": [{}]}), encoding="utf-8")
    assert latest_report(tmp_path) == expected


def test_checker_detects_displayed_unit_price_change():
    response, observation, catalog = rate_view()
    assert "completed" in validate_view(response, observation, catalog, {})
    for field in ("visible", "allText"):
        observation[field] = observation[field].replace("80.00", "1.00")
    for section in observation["sections"]:
        for field in ("visible", "allText"):
            section[field] = section[field].replace("80.00", "1.00")
    with pytest.raises(AssertionError, match="受控单价"):
        validate_view(response, observation, catalog, {})


def test_checker_detects_old_turn_identity_reused_for_new_response():
    response, observation, catalog = rate_view()
    newer = response.model_copy(update={"business": response.business.model_copy(update={"turn_id": "different-current-turn"})})
    with pytest.raises(AssertionError, match="本轮 ID"):
        validate_view(newer, observation, catalog, {})


def test_checker_requires_same_formal_answer_default_visible():
    response, observation, catalog = rate_view()
    answer = next(section for section in observation["sections"] if section["section"] == "answer")
    answer["visible"] = "本轮执行完成。"
    observation["visible"] = "\n".join(section["visible"] for section in observation["sections"])
    with pytest.raises(AssertionError, match="正式回答没有默认显示"):
        validate_view(response, observation, catalog, {})


def test_checker_allows_version_metadata_folded_without_hiding_price():
    response, observation, catalog = rate_view()
    price = next(section for section in observation["sections"] if section["section"] == "pricing")
    price["visible"] = price["visible"].replace(catalog.version, "")
    observation["visible"] = "\n".join(section["visible"] for section in observation["sections"])
    assert "completed" in validate_view(response, observation, catalog, {})


def test_checker_keeps_original_task_available_during_unit_price_detour():
    response, observation, catalog = rate_view()
    requirements = RequirementSet(requirement_id="original-selection", revision=2, items=(
        HardRequirement(requirement_id="original-api", statement="必须支持 产品 API",
                        user_quote="产品API必须有", feature_id="api_access"),), parameters=(
        DeclaredParameter(name="paid_seats", value=80, user_quote="80席"),
        DeclaredParameter(name="months", value=12, user_quote="12个月"),))
    match = match_requirements(requirements, retrieval_ids=())
    price = calculate_subscription_price("team", 80, 12)
    response.business.current_task = CurrentTask(status="current", last_result="not_updated",
        requirements=requirements, match=match, pricing=price)
    current = {"section": "current-task", "visible": "必须支持 产品 API 80 席 12 个月 团队版 Team 76,800.00 元 本轮没有更新",
               "allText": "必须支持 产品 API 80 席 12 个月 团队版 Team 76,800.00 元 本轮没有更新 original-selection 修订 2"}
    observation["sections"].append(current)
    observation["visible"] = "\n".join(section["visible"] for section in observation["sections"])
    observation["allText"] = "\n".join(section["allText"] for section in observation["sections"])
    assert "completed" in validate_view(response, observation, catalog, {})
    current["visible"] = current["visible"].replace("76,800.00", "115,200.00")
    current["allText"] = current["allText"].replace("76,800.00", "115,200.00")
    observation["visible"] = "\n".join(section["visible"] for section in observation["sections"])
    observation["allText"] = "\n".join(section["allText"] for section in observation["sections"])
    with pytest.raises(AssertionError, match="插问金额替换了原任务金额"):
        validate_view(response, observation, catalog, {})


def test_checker_rejects_formula_whitespace_rewriting():
    response, observation, catalog = rate_view()
    calculated = calculate_subscription_price("team", 80, 12)
    response.business.intent = "price"
    response.business.pricing = calculated
    response.business.unit_price = None
    status = observation["sections"][0]
    status["visible"] = status["allText"] = status["visible"].replace("目录与单价（unit_price）", "只读计价（price）")
    demands = {"section": "requirements", "visible": "fixture-requirements 修订 1", "allText": "fixture-requirements 修订 1"}
    observation["sections"].insert(1, demands)
    price = next(s for s in observation["sections"] if s["section"] == "pricing")
    # Correct numeric result, wrong exact formula: whitespace normalization
    # must not hide an alteration of the returned authoritative formula.
    price["visible"] = price["allText"] = (
        f"{calculated.plan_name}（team）单价80.00 元／席／月 CNY {catalog.version} "
        "实际采用付费席位80 席实际采用订阅月份12 月76,800.00 元 " + calculated.formula.replace(" × ", "×")
    )
    observation["visible"] = "\n".join(s["visible"] for s in observation["sections"])
    observation["allText"] = "".join(s["allText"] for s in observation["sections"])
    with pytest.raises(AssertionError, match="原算式未逐字"):
        validate_view(response, observation, catalog, {})


def test_checker_runtime_code_digest_is_optional_but_rejects_changed_runtime(tmp_path, monkeypatch):
    """Isolated file metadata checks, without using synthetic browser evidence."""
    import hashlib

    ui_root = tmp_path / "app/ui"
    (ui_root / "assets").mkdir(parents=True)
    (ui_root / "index.html").write_text("test UI", encoding="utf-8")
    runtime = tmp_path / "app/services/state.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("VERSION = 1\n", encoding="utf-8")
    index_path = tmp_path / "formal-index.json"
    index_path.write_text("isolated index metadata", encoding="utf-8")
    monkeypatch.setattr(check_ui_evidence, "ROOT", tmp_path)
    monkeypatch.setattr(check_ui_evidence, "DEFAULT_INDEX_PATH", index_path)
    index = SimpleNamespace(inputs=SimpleNamespace(embedding="isolated embedding profile"))
    monkeypatch.setattr(check_ui_evidence, "_read_index", lambda _: index)
    monkeypatch.setattr(check_ui_evidence, "prepare_corpus", lambda: SimpleNamespace(inputs=lambda _: index.inputs))
    monkeypatch.setattr(check_ui_evidence, "_check_inputs", lambda observed, expected: None)
    monkeypatch.setattr(check_ui_evidence, "model_digest", lambda _: "isolated-inputs-digest")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    report = {"kind": check_ui_evidence.NORMAL_KIND, "cases": [{"model_dialogue_inputs": []}],
        "catalog_version": get_catalog().version, "formal_index_sha256": digest(index_path),
        "formal_inputs_digest": "isolated-inputs-digest", "formal_index_unchanged": True,
        "ui_sha256": {"index.html": digest(ui_root / "index.html")}, "turn_count": 1, "model_call_count": 0}
    assert validate_report_identity(report)[1] is index  # Legacy reports stay readable.
    report["runtime_code_sha256"] = {"app/services/state.py": digest(runtime)}
    assert validate_report_identity(report)[1] is index
    runtime.write_text("VERSION = 2\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="当前业务代码"):
        validate_report_identity(report)


def projected_rate_view():
    response, _, catalog = rate_view()
    answer = f"{response.business.unit_price.plan_name} 单价 80.00 元／席／月，按付费席位和订阅月数计算订阅基价。"
    response.message = answer
    response.business.presentation = AnswerPresentation(answer_markdown=answer)
    diagnostics = f"fixture-turn fixture-session completed unit_price {catalog.version}"
    parts = [{"section": name, "intent": response.business.intent if name == "tools" else None,
              "visible": text if name == "answer" else "查看技术记录", "allText": text}
             for name, text in (("answer", answer), ("tools", diagnostics))]
    observation = {"uiContract": check_ui_evidence.REACT_UI_CONTRACT, "label": "controlled-react-rate",
        "turnId": response.business.turn_id, "sessionId": response.session_id,
        "visible": " ".join(part["visible"] for part in parts), "allText": " ".join(part["allText"] for part in parts),
        "sections": parts, "matrixCells": [], "claimEvidence": [], "currentResult": None,
        "sources": [], "toolRecords": []}
    return response, observation, catalog


def test_projected_checker_accepts_plain_answer_and_collapsed_diagnostics_without_old_headings():
    response, observation, catalog = projected_rate_view()
    assert "新界面同源展示" in validate_view(response, observation, catalog, {})
    assert "本轮执行完成" not in observation["visible"]


def unassessed_selection_view():
    response, observation, catalog = projected_rate_view()
    business = response.business
    business.intent = "selection"
    business.unit_price = None
    business.requirements = RequirementSet(requirement_id="new-empty-requirements")
    business.match = match_requirements(business.requirements, retrieval_ids=())
    business.presentation = AnswerPresentation(
        answer_markdown="还不能推荐套餐：尚未明确必须满足的功能。", show_comparison=True, show_current_result=True)
    response.message = business.presentation.answer_markdown
    business.current_task = CurrentTask(status="current", requirements=business.requirements,
        match=business.match, result_turn_id=business.turn_id, result_message_id="actual-row")
    observation["sections"][0]["visible"] = observation["sections"][0]["allText"] = response.message
    observation["sections"][1]["intent"] = business.intent
    observation["sections"][1]["allText"] = f"{business.turn_id} {response.session_id} completed selection"
    observation["currentResult"] = {"count": 1, "resultTurnId": business.turn_id, "resultMessageId": "actual-row",
        "requirementId": business.requirements.requirement_id, "revision": 1, "status": "current",
        "visible": "当前选型结果 尚未评估（需求未明确）"}
    observation["visible"] = response.message + " 当前选型结果 尚未评估（需求未明确） 查看技术记录"
    observation["allText"] = observation["visible"] + " " + observation["sections"][1]["allText"]
    return response, observation, catalog


def test_projected_checker_keeps_empty_comparison_unassessed_without_rendering_an_empty_table():
    response, observation, catalog = unassessed_selection_view()
    assert response.business.match.status == "comparison_only"
    assert "映射通过" in validate_view(response, observation, catalog, {})
    observation["currentResult"]["visible"] = "当前选型结果 选择暂无"
    with pytest.raises(AssertionError, match="需求未明确的真实评估状态"):
        validate_view(response, observation, catalog, {})


@pytest.mark.parametrize("fault, reason", [("empty-table", "旧选型表"), ("rows-text", "空比较表"),
                                         ("satisfied", "已满足或无满足"), ("no-satisfying", "已满足或无满足")])
def test_projected_checker_rejects_false_empty_evaluation_ui(fault, reason):
    response, observation, catalog = unassessed_selection_view()
    if fault == "empty-table":
        observation["sections"].append({"section": "match", "visible": "No rows", "allText": "No rows"})
        observation["visible"] += " No rows"
        observation["allText"] += " No rows"
    elif fault == "rows-text":
        observation["visible"] += " No rows"
    elif fault == "satisfied":
        observation["visible"] += " 三个套餐已满足所有要求"
    else:
        observation["visible"] += " 尚无满足全部要求的套餐"
    with pytest.raises(AssertionError, match=reason):
        validate_view(response, observation, catalog, {})


@pytest.mark.parametrize("fault, reason", [("answer", "正式回答展示分区"), ("price", "受控单价"), ("turn", "本轮 ID")])
def test_projected_checker_rejects_changed_answer_amount_or_stale_identity(fault, reason):
    response, observation, catalog = projected_rate_view()
    if fault == "answer":
        observation["sections"][0]["visible"] = "本轮执行完成"
        observation["visible"] = "本轮执行完成 查看技术记录"
    elif fault == "price":
        # Same presentation text with an inconsistent authoritative API price.
        response.business.unit_price = response.business.unit_price.model_copy(update={"unit_price_minor": 100})
    else:
        observation["turnId"] = "stale-turn"
    with pytest.raises(AssertionError, match=reason):
        validate_view(response, observation, catalog, {})


def test_projected_checker_does_not_accept_latest_message_as_original_current_result():
    response, observation, catalog = projected_rate_view()
    requirements = RequirementSet(requirement_id="original", revision=3,
        parameters=(DeclaredParameter(name="paid_seats", value=80, user_quote="80席"),))
    response.business.current_task = CurrentTask(status="current", last_result="not_updated", requirements=requirements,
        result_turn_id="original-turn", result_message_id="stored-row-2")
    observation["currentResult"] = {"count": 1, "resultTurnId": "original-turn", "resultMessageId": "stored-row-2",
        "requirementId": "original", "revision": 3, "status": "current", "visible": "80 席"}
    assert "completed" in validate_view(response, observation, catalog, {})
    observation["currentResult"]["resultTurnId"] = response.business.turn_id
    with pytest.raises(AssertionError, match="当前结果的 DOM 身份"):
        validate_view(response, observation, catalog, {})


def test_projected_checker_rejects_unadopted_hit_presented_as_answer_source():
    response, observation, catalog = projected_rate_view()
    observation["sources"] = [{"sourceId": "other-hit", "adopted": True, "rawText": "真实但未采用原文", "allText": ""}]
    with pytest.raises(AssertionError, match="来源入口混入未采用片段"):
        validate_view(response, observation, catalog, {})


def test_final_observation_sidecar_can_bind_later_ui_without_mutating_old_model_report(tmp_path, monkeypatch):
    import hashlib
    from copy import deepcopy

    ui_root = tmp_path / "app/ui"
    (ui_root / "assets").mkdir(parents=True)
    (ui_root / "index.html").write_text("old UI", encoding="utf-8")
    runtime = tmp_path / "app/services/state.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("VERSION = 1\n", encoding="utf-8")
    index_path = tmp_path / "formal-index.json"
    index_path.write_text("isolated index metadata", encoding="utf-8")
    monkeypatch.setattr(check_ui_evidence, "ROOT", tmp_path)
    monkeypatch.setattr(check_ui_evidence, "DEFAULT_INDEX_PATH", index_path)
    index = SimpleNamespace(inputs=SimpleNamespace(embedding="isolated embedding profile"))
    monkeypatch.setattr(check_ui_evidence, "_read_index", lambda _: index)
    monkeypatch.setattr(check_ui_evidence, "prepare_corpus", lambda: SimpleNamespace(inputs=lambda _: index.inputs))
    monkeypatch.setattr(check_ui_evidence, "_check_inputs", lambda observed, expected: None)
    monkeypatch.setattr(check_ui_evidence, "model_digest", lambda _: "isolated-inputs-digest")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    report = {"kind": check_ui_evidence.NORMAL_KIND, "run_id": "real-owned-run", "cases": [{"model_dialogue_inputs": []}],
        "catalog_version": get_catalog().version, "formal_index_sha256": digest(index_path),
        "formal_inputs_digest": "isolated-inputs-digest", "formal_index_unchanged": True,
        "ui_sha256": check_ui_evidence.ui_hashes(tmp_path), "runtime_code_sha256": check_ui_evidence.runtime_hashes(tmp_path),
        "turn_count": 1, "model_call_count": 0}
    before = deepcopy(report)
    (ui_root / "index.html").write_text("final UI built and actually reloaded", encoding="utf-8")
    sidecar = {"uiContract": check_ui_evidence.REACT_UI_CONTRACT, "reportRunId": "real-owned-run",
        "capturedAt": "2026-10-04T12:00:00+08:00", "browserReloadedAfterBuild": True,
        "ui_sha256": check_ui_evidence.ui_hashes(tmp_path), "runtime_code_sha256": check_ui_evidence.runtime_hashes(tmp_path), "observations": []}
    assert validate_report_identity(report, sidecar)[1] is index
    assert report == before
    with pytest.raises(AssertionError, match="UI 源码"):
        validate_report_identity(report)
    with pytest.raises(AssertionError, match="实际构建"):
        validate_report_identity(report, {**sidecar, "ui_sha256": report["ui_sha256"]})
    runtime.write_text("VERSION = 2\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="业务代码改变"):
        validate_report_identity(report, sidecar)


def test_recorded_history_row_ids_locate_response_without_entering_model_dialogue():
    response, _, catalog = projected_rate_view()
    response.message_id = "4"
    previous = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧正式回答"}]
    user = "Team单价"
    case = {"http_status": 200, "user": user, "response": response.model_dump(mode="json"),
        "history": [{**message, "id": str(index + 1)} for index, message in enumerate(previous)] +
                   [{"role": "user", "content": user, "id": "3"}, {"role": "assistant", "content": response.message, "id": "4"}],
        "model_dialogue_inputs": [previous + [{"role": "user", "content": user}]],
        "text_matches_stored": True, "next_model_uses_stored_history": True, "request_session_id": response.session_id}
    index = SimpleNamespace(inputs=SimpleNamespace(sources=[]))
    assert check_ui_evidence.validate_case(case, catalog, index)[0].message_id == "4"
    case["history"][-1]["id"] = "wrong-row"
    with pytest.raises(AssertionError, match="原回复定位 ID"):
        check_ui_evidence.validate_case(case, catalog, index)
    case["history"][-1]["id"] = "4"
    case["next_model_uses_stored_history"] = False
    with pytest.raises(AssertionError, match="实际模型输入"):
        check_ui_evidence.validate_case(case, catalog, index)


def test_retrospective_message_capture_checks_current_against_real_completed_timeline():
    response, observation, catalog = projected_rate_view()
    original = CurrentTask(status="current", requirements=RequirementSet(requirement_id="task", revision=1),
        result_turn_id="original-result", result_message_id="original-row")
    newer = CurrentTask(status="needs_reassessment", requirements=RequirementSet(requirement_id="task", revision=2),
        result_turn_id="new-result", result_message_id="new-row")
    response.business.current_task = original
    response.business.presentation.show_current_result = True
    latest_response = response.model_copy(deep=True)
    latest_response.business.current_task = newer
    report = {"cases": [
        {"finished_at": "2026-10-04T08:00:00+00:00", "response": response.model_dump(mode="json")},
        {"finished_at": "2026-10-04T08:02:00+00:00", "response": latest_response.model_dump(mode="json")}]}
    observation.update(captureMode="retrospective-live-message", capturedAt="2026-10-04T08:03:00Z",
        messageResult={"current": False, "resultTurnId": "original-result", "resultMessageId": "original-row",
                       "requirementId": "task", "revision": 1, "status": "current", "visible": "历史选型结果"},
        currentResult={"count": 1, "resultTurnId": "new-result", "resultMessageId": "new-row",
                       "requirementId": "task", "revision": 2, "status": "needs_reassessment", "visible": "当前选型结果，需要重新评估"})
    expected = check_ui_evidence.current_at_capture(report, observation)
    assert expected == newer
    assert "历史卡片补采" in validate_view(response, observation, catalog, {}, current_expected=expected, current_from_record=True)
    observation["messageResult"]["current"] = True
    with pytest.raises(AssertionError, match="冒充了当前结果"):
        validate_view(response, observation, catalog, {}, current_expected=expected, current_from_record=True)
    observation["messageResult"]["current"] = False
    observation["currentResult"]["resultTurnId"] = "original-result"
    with pytest.raises(AssertionError, match="当前结果的 DOM 身份"):
        validate_view(response, observation, catalog, {}, current_expected=expected, current_from_record=True)


def test_retrospective_capture_cannot_select_a_future_or_different_session_result():
    response, observation, _ = projected_rate_view()
    original = CurrentTask(status="current", requirements=RequirementSet(requirement_id="original"), result_turn_id="old")
    response.business.current_task = original
    future = response.model_copy(deep=True)
    future.business.current_task = original.model_copy(update={"result_turn_id": "future"})
    foreign = response.model_copy(deep=True)
    foreign.session_id = "other-session"
    foreign.business.current_task = original.model_copy(update={"result_turn_id": "foreign"})
    report = {"cases": [
        {"finished_at": "2026-10-04T08:00:00+00:00", "response": response.model_dump(mode="json")},
        {"finished_at": "2026-10-04T08:01:00+00:00", "response": foreign.model_dump(mode="json")},
        {"finished_at": "2026-10-04T08:04:00+00:00", "response": future.model_dump(mode="json")}]}
    observation["capturedAt"] = "2026-10-04T08:02:00Z"
    assert check_ui_evidence.current_at_capture(report, observation).result_turn_id == "old"
    observation["capturedAt"] = "2026-10-04T07:59:00Z"
    with pytest.raises(AssertionError, match="之前没有"):
        check_ui_evidence.current_at_capture(report, observation)
