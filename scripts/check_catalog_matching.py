"""Offline, read-only examples of the actual catalog and matcher.

Run from the repository root: python -m scripts.check_catalog_matching
"""

import sys

from app.schemas.matching import ConditionAssertion, HardRequirement, RequirementSet
from app.services.catalog import get_catalog
from app.services.matching import match_requirements


def _requirement(feature_id: str, statement: str) -> HardRequirement:
    return HardRequirement(
        requirement_id=feature_id,
        feature_id=feature_id,
        statement=statement,
        user_quote=statement,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    catalog = get_catalog()
    names = {plan.plan_id: plan.name for plan in catalog.plans}
    feature_names = {feature.feature_id: feature.name for feature in catalog.features}
    feature_names["scim"] = "SCIM 自动开通"
    print(f"产品：{catalog.product.name}；资料版本：{catalog.version}")
    print(f"目录校验通过：{len(catalog.plans)} 档套餐、{len(catalog.features)} 项能力、{len(catalog.conditions)} 类前提")
    for plan in catalog.plans:
        yuan, fen = divmod(plan.unit_price_minor, catalog.minor_units_per_major)
        print(f"  {plan.name}：{yuan}.{fen:02d} 元／席／月")

    basic_items = (
        _requirement("project_collaboration", "需要任务与项目协作"),
        _requirement("data_export", "必须能导出数据"),
    )
    cases = (
        ("C01 基础协作与导出", RequirementSet(requirement_id="C01", items=basic_items),
         "matched", "basic", ("basic", "team", "business")),
        ("C02 加入 API", RequirementSet(
            requirement_id="C02", items=(*basic_items, _requirement("api_access", "必须有 API"))
         ), "matched", "team", ("team", "business")),
        ("C04 私有化部署", RequirementSet(
            requirement_id="C04", items=(_requirement("private_deployment", "必须私有化部署"),)
         ), "blocked", None, ()),
        ("C05 目录外 SCIM", RequirementSet(
            requirement_id="C05", items=(_requirement("scim", "必须支持 SCIM 自动开通"),)
         ), "blocked", None, ()),
        ("C07 SSO 缺真实条件证据", RequirementSet(
            requirement_id="C07", items=(_requirement("saml_sso", "必须有 SAML SSO"),),
            condition_assertions=(ConditionAssertion(
                condition_id="saml_sso_compatible_idp", value="yes",
                user_quote="我们已经具备兼容的身份提供方",
            ),),
         ), "blocked", None, ()),
    )
    status_names = {
        "supported": "支持", "unsupported": "不支持",
        "unknown": "未知", "pending_condition": "前提或依据待确认",
    }
    reason_names = {
        "catalog_unsupported": "目录明确不支持",
        "unmodeled_requirement": "当前未建模，不能判断",
        "condition_evidence_missing": "缺少有效条件证据",
    }
    for title, requirements, expected_status, expected_lowest, expected_eligible in cases:
        # Keep these read-only pre-retrieval examples. The normal entry and
        # actual evidence are independently exercised by scripts.check_retrieval.
        result = match_requirements(requirements, retrieval_ids=())
        if (result.status, result.lowest_plan_id, result.eligible_plan_ids) != (
            expected_status, expected_lowest, expected_eligible
        ):
            raise RuntimeError(f"{title} 的实际匹配结果不符合预期")
        if result.evidence_origin != "none" or result.test_evidence:
            raise RuntimeError("检索前自检不得采用测试证据")
        lowest = names[result.lowest_plan_id] if result.lowest_plan_id else "无完整满足档"
        print(f"\n{title}：{lowest}")
        for plan in result.plans:
            details = []
            for item in plan.items:
                reasons = "、".join(reason_names.get(code, code) for code in item.reason_codes)
                label = feature_names.get(item.feature_id, item.feature_id)
                details.append(f"{label}={status_names[item.status]}" + (f"（{reasons}）" if reasons else ""))
            print(f"  {names[plan.plan_id]}：{'；'.join(details)}")
    print("\n本次本地匹配自检通过。")
    print("本命令使用固定规范化需求；未验证真实 SSO 检索、聊天业务接入或计价。")


if __name__ == "__main__":
    main()
