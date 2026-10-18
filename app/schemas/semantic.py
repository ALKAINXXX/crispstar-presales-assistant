"""Bounded language review candidates; these carry no product or save authority.

Offsets use Python's zero-based Unicode character indexes. A unique continuous
quote may be located by the server when a model supplies no usable offsets.
Repeated quotes require the exact position; equality alone proves no meaning.
"""

from typing import Literal

from pydantic import Field, PrivateAttr, StrictFloat, StrictInt, field_validator, model_validator

from app.schemas.business import ConsultationItem, RequirementCandidate, SourceSpan
from app.schemas.catalog import ReadOnlyModel


class SemanticTarget(ReadOnlyModel):
    kind: Literal["selection", "price", "none"] = Field(default="none", description="需要修改或执行的服务端任务对象；知识/单价插问无修改时为none。不能仅凭intent猜归属。")
    relation: Literal["continue", "return", "new", "detour", "independent"] = Field(default="detour", description="同一意图也可能是新任务。new/independent不继承旧参数；return指原选型；detour只询问。")
    task_id: str | None = None
    copy_from: Literal["selection", "price"] | None = None
    reuse_fields: tuple[Literal["paid_seats", "months", "conditions", "requirements"], ...] = ()
    span: SourceSpan | None = Field(default=None, description="本轮明确新任务或授权复用的连续原话。已有任务时new/independent必须给出；首次空任务可省略。")


class SemanticOperation(ReadOnlyModel):
    action: Literal["set", "clear", "add", "remove", "clarify", "replace", "select_plan",
                    "cancel_selection", "request_evaluation", "request_pricing", "request_proposal", "cancel_pricing", "note"] = Field(
        description="select_plan只用于用户主动选择原话中出现的目录套餐/同义身份，不可把最低满足档写成basic；最低档应cancel_selection或省略，由真实匹配决定。request_proposal仅准备可点击对象，不构成保存授权。replace需求必须同组保留旧项直到新项可靠；replace数量字段且无需求对象时表示采用已核定终值，和set经过相同闸门。")
    field: Literal["paid_seats", "months", "saml_sso_compatible_idp"] | None = None
    value: StrictInt | StrictFloat | Literal["yes", "no", "unknown", "basic", "team", "business"] | None = Field(
        default=None, description="Wire可描述真实非法小数供本组拒绝；业务数量仍必须整数。用户真实50.5不能取整为50；首候选50.0而原话整数50可由本次核对规范为整数50。")
    requirement_id: str | None = None
    requirement: RequirementCandidate | None = None
    span: SourceSpan
    speech_act: Literal["assertion", "instruction", "question", "hypothesis", "quoted", "background"] = "assertion"
    certainty: Literal["certain", "uncertain", "approximate", "planned", "conditional"] = "certain"
    time_scope: Literal["current", "future", "unspecified"] = "current"


ReviewReason = Literal["confirmed", "not_actual_purchase", "uncertain", "approximate", "ambiguous", "conditional",
    "future", "quoted", "question", "negative", "conflict", "candidate_disagreement",
    "target_unknown", "reference_missing", "invalid_type", "invalid_range", "unknown_identity",
    "dependency_unresolved", "extraction_failed", "detail_uncovered"]


class SemanticGroup(ReadOnlyModel):
    group_id: str = Field(min_length=1, max_length=80)
    depends_on: tuple[str, ...] = Field(default=(), max_length=12)
    verdict: Literal["accept", "reject", "unresolved"]
    reason: ReviewReason = "confirmed"
    operations: tuple[SemanticOperation, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_rejected_hypothesis_reason(cls, value, info):
        # A diagnostic synonym can explain a rejection, but cannot authorize
        # facts. Accept groups and other unknown reasons remain strict.
        if value == "hypothesis" and info.data.get("verdict") in {"reject", "unresolved"}:
            return "conditional"
        return value


class SemanticReview(ReadOnlyModel):
    # Production parser metadata, never a model-supplied business verdict.
    _consultation_contract_missing: bool = PrivateAttr(default=False)
    intent: Literal["general", "knowledge", "unit_price", "price", "selection", "quote_query", "unsupported_action"]
    target: SemanticTarget = Field(default_factory=SemanticTarget)
    coverage_complete: bool = Field(description="独立阅读完整本轮原话后，明确修改、硬要求及其细节已全部覆盖；否则为false。")
    groups: tuple[SemanticGroup, ...] = Field(default=(), max_length=24)
    consultations: tuple[ConsultationItem, ...] = Field(default=(), max_length=24)
    consultation_coverage_complete: bool = Field(default=True,
        description="独立阅读完整原话后，全部咨询对象、实际套餐能力关系及方面已保留。与修改/硬细节coverage_complete分别判断，不表示工具执行完成。")
    quote_id: str | None = Field(default=None, max_length=120)
    quote_ids: tuple[str, ...] = Field(default=(), max_length=4,
        description="仅逐项列出本轮原话明确要求读取的草案编号；不猜最近对象。")

    @property
    def consultation_contract_missing(self):
        return self._consultation_contract_missing

    @model_validator(mode="after")
    def unique_groups(self):
        identities = tuple(group.group_id for group in self.groups)
        if len(set(identities)) != len(identities):
            raise ValueError("Review groups need unique identities")
        if any(dep not in identities or dep == group.group_id
               for group in self.groups for dep in group.depends_on):
            raise ValueError("Review dependencies must reference other groups")
        if len({item.item_id for item in self.consultations}) != len(self.consultations):
            raise ValueError("Consultation items need unique identities")
        if any(dep not in identities for item in self.consultations for dep in item.depends_on):
            raise ValueError("Consultation dependencies must reference review groups")
        return self
