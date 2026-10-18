"""Model candidates are requests, never authoritative business results."""

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.schemas.catalog import ReadOnlyModel
from app.schemas.matching import ConditionAssertion, DeclaredParameter


class SourceSpan(ReadOnlyModel):
    quote: str = Field(min_length=1, max_length=4000)
    start: StrictInt | None = Field(default=None, ge=0)
    end: StrictInt | None = Field(default=None, ge=0)


class ConsultationScopeBasis(ReadOnlyModel):
    """A source pointer for a finite scope, never a claimed empty result."""
    kind: Literal["all_plans", "named", "reference"]
    span: SourceSpan


class ConsultationItem(ReadOnlyModel):
    """This turn's question scope; never purchase facts or product authority.

    One item describes one aspect/capability and its actual plan relations.
    Different capabilities with different plans must remain separate items.
    """
    item_id: str = Field(min_length=1, max_length=80)
    aspect: Literal["unit_price", "support", "meaning", "prerequisite", "detail", "pricing"] = Field(
        description="unit_price询问每席每月费率；pricing询问由席位与期限计算的数值总价；费用构成、税/折扣/实施费是否包含、计费口径属于detail，须检索资料解释，不是单价或数值计价。")
    plan_ids: tuple[str, ...] = Field(default=(), max_length=12,
        description="用户明确询问的套餐，保留顺序。每个事项只含一个能力/方面，不将不同对象无条件交叉组合。")
    all_plans: bool = Field(default=False,
        description="无完整范围表达时，true表示本事项最终询问全目录，不能借另一问题的量词。完整scope_basis为all_plans并有排除出处时，true也可仅表示全目录基底；服务严格核验表达后投影实际剩余plan_ids，不以此标志覆盖排除。")
    feature_id: str | None = Field(default=None,
        description="目录内能力身份；未建模问题只能保留topic，不编目录能力。")
    topic: str = Field(min_length=1, max_length=500,
        description="用户实际询问的方面或具体细节，不是产品支持结论。")
    span: SourceSpan
    reference_span: SourceSpan | None = Field(default=None,
        description="本事项有这两项/它们/这些套餐等指代时，给出同条消息中唯一对应的连续先行原话。只能核定该指代，不能借另一独立问题扩大范围。")
    scope_basis: ConsultationScopeBasis | None = Field(default=None,
        description="有限范围基底的连续原话：all_plans全目录、named明确列表、reference同条消息唯一先行范围。完整基底及排除表达可以替代枚举plan_ids；服务端严格校验后计算非空或空集合，plan_ids=[]本身不声明空结果。")
    scope_exclusions: tuple[SourceSpan, ...] = Field(default=(), max_length=4,
        description="关联本问题的明确排除连续原话。合法空范围必须保留排除出处，不用虚填套餐或all_plans代替空结果。")
    depends_on: tuple[str, ...] = Field(default=(), max_length=12,
        description="该答复所依赖的本轮实际修改group_id；独立咨询为空，修改后金额必须关联修改。")

    @model_validator(mode="after")
    def coherent_scope(self):
        if self.aspect == "support" and self.feature_id is None:
            raise ValueError("A catalog support relation needs a feature identity")
        if self.aspect in {"unit_price", "support"} and not (self.plan_ids or self.all_plans
                or (self.scope_basis is not None and self.scope_exclusions)):
            raise ValueError("A price/support question needs its actual plan scope")
        return self


class RequirementCandidate(ReadOnlyModel):
    statement: str = Field(min_length=1, max_length=500)
    user_quote: str = Field(min_length=1, max_length=2000,
        description="逐字复制本轮用户消息的连续原文，禁止改写或拼接。并列需求可共享完整分句；statement的整理不适用于user_quote。")
    kind: Literal["feature", "unmodeled", "ambiguous"] = "feature"
    feature_id: str | None = None


class RemovalCandidate(ReadOnlyModel):
    requirement_id: str = Field(min_length=1)
    user_quote: str = Field(min_length=1, max_length=2000)


class RequirementPatch(ReadOnlyModel):
    intent: Literal["general", "knowledge", "unit_price", "price", "selection", "quote_query", "unsupported_action"]
    add: tuple[RequirementCandidate, ...] = Field(default=(), max_length=12)
    remove: tuple[RemovalCandidate, ...] = Field(default=(), max_length=12)
    parameters: tuple[DeclaredParameter, ...] = Field(default=(), max_length=2,
        description="只提交本轮明确的付费席位或期限并引用对应数量原话。未提及的字段省略，订阅基价请求不代表12个月。独立计价不能偷用原选型参数。")
    conditions: tuple[ConditionAssertion, ...] = Field(default=(), max_length=1)
    selected_plan_id: str | None = None
    selection_quote: str | None = None
    pricing_quote: str | None = None
    cancel_pricing_quote: str | None = None
    context_quotes: tuple[str, ...] = Field(default=(), max_length=6)
    consultations: tuple[ConsultationItem, ...] = Field(default=(), max_length=24,
        description="本轮只读咨询范围；与购买修改分开，不写硬需求、参数、条件或主动选档。")

    @model_validator(mode="after")
    def historical_query_has_no_patch(self):
        if self.intent == "quote_query" and any((self.add, self.remove, self.parameters,
                self.conditions, self.selected_plan_id, self.selection_quote,
                self.pricing_quote, self.cancel_pricing_quote, self.context_quotes, self.consultations)):
            raise ValueError("Historical Quote lookup cannot change current requirements")
        return self


# A finite service-owned vocabulary, never exception text or rejected values.
INPUT_REJECTIONS = {
    "reference_missing": ("候选原话必须来自本轮实际用户输入；本轮候选未更新。", "请只重述需要修改的一项；其他已接受信息保留。"),
    "cancellation_unconfirmed": ("取消计价须有明确本轮原话，不能同时提出计价。", "请明确是否取消计价；已接受数量和需求保留。"),
    "removal_unconfirmed": ("删除必须指向已有需求，并有用户明确修改的原话。", "请明确说出需要取消的那项要求。"),
    "removal_target_mismatch": ("删除原话未指向该硬需求，不能删除其他需求。", "请明确说出需要取消的那项要求。"),
    "unknown_feature": ("目录外能力须保留为未建模的需求，不能当成目录已支持的能力。", "请重述具体要求；资料未知不会被当作已支持。"),
    "requirement_kind_mismatch": ("未知或歧义需求不能同时借用目录能力身份。", "请重述具体要求；其他已接受需求保留。"),
    "parameter_not_explicit": ("数量或期限缺少明确原话；人数、计划和歧义不能补成计价参数。", "请明确实际购买席位或订阅月数；可以只修正这一项。"),
    "condition_unknown_identity": ("该前提不属于当前产品的受控适用条件。", "请说明目前是否具备兼容 SAML SSO 的身份提供方。"),
    "condition_not_current": ("泛泛肯定或未来计划不能补足当前前提。", "请明确目前具备、不具备或不确定；未来准备不会记为已具备。"),
    "condition_unclear": ("前提仍有歧义或未知，不能变成明确肯否。", "可以回答目前不确定；不会据此宣称适配通过。"),
    "condition_undirected": ("前提答复未指向明确的待答条件。", "请明确目前是否具备兼容 SAML SSO 的身份提供方。"),
    "condition_not_compatible": ("有身份系统尚不等于明确具备兼容的身份提供方。", "请说明该身份提供方目前是否兼容；也可以回答不确定。"),
    "condition_contradiction": ("前提候选与明确肯否原话不相容。", "请明确目前具备、不具备或不确定；原自述仍保留。"),
    "selection_not_explicit": ("指定套餐必须关联有效身份与本轮明确原话。", "请只说明要选择哪个套餐，或取消指定、按最低满足档选型。"),
    "state_budget": ("工作状态超过本地小型需求预算。", "请缩小本次范围或新建会话；原任务未被覆盖。"),
    "price_parameters_mismatch": ("计价参数与已接受的席位或月份不一致，未计算新金额。", "请只修正需要改变的参数，再重新计算。"),
    "price_not_requested": ("本轮仅选型，未要求计价。", "需要金额时请明确要求计算订阅基价。"),
    "price_match_not_current": ("选型计价须先取得当前需求的完整满足档。", "请先核对当前需求及阻塞原因，再继续选型。"),
    "price_evidence_invalid": ("选型所依附的证据已失效，须重新匹配后计价。", "请重新核对当前需求；失效证据不会用于新金额。"),
    "price_plan_not_selected": ("独立计价须有用户明确指定的套餐。", "请明确本次独立计算的套餐；不会改变原选型。"),
    "price_plan_not_unique": ("计价工具的套餐候选未指向本轮唯一明确套餐。", "请明确本次独立计算哪个套餐。"),
    "invalid_field": ("本轮输入的一项类型、范围或字段无效，尚未更新。", "请只修正提示的字段；其他已接受信息保留。"),
    "review_not_completed": ("本轮原话核对未完成；未提交新的业务事实。", "可以稍后重试本轮输入；旧有效结果保持原身份。"),
    "unreviewed_mutation": ("后续工具实参未通过本轮原话核对；未更新业务事实。", "请在新的聊天中明确需要修改的一项。"),
    "review_dependency": ("本轮相关修改尚未确定；未执行依赖它的新结果。", "请先确认提示的未决项；旧有效结果保持原身份。"),
}


class RejectedInputDetails(ReadOnlyModel):
    issue: Literal[
        "reference_missing", "cancellation_unconfirmed", "removal_unconfirmed", "removal_target_mismatch",
        "unknown_feature", "requirement_kind_mismatch", "parameter_not_explicit", "condition_unknown_identity",
        "condition_not_current", "condition_unclear", "condition_undirected", "condition_not_compatible",
        "condition_contradiction", "selection_not_explicit", "state_budget", "price_parameters_mismatch",
        "price_not_requested", "price_match_not_current", "price_evidence_invalid", "price_plan_not_selected",
        "price_plan_not_unique", "invalid_field",
        "review_not_completed", "unreviewed_mutation", "review_dependency",
    ]
    field: Literal["candidate", "requirements", "paid_seats", "months", "saml_sso_compatible_idp", "selected_plan_id", "pricing"]
    previous_value: StrictInt | Literal["yes", "no", "unknown", "basic", "team", "business"] | None = None
    update_status: Literal["unchanged"] = "unchanged"
    scope: Literal["candidate_patch", "pricing"] = "candidate_patch"
