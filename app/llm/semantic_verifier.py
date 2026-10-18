"""One independent original-language check, without product/save authority.

The caller holds the existing session, ordinary gate and any Quote leases.
Timeout/cancellation drains actual dispatch work before releasing those guards.
"""

import asyncio
import json
from time import perf_counter

from pydantic import Field, StrictFloat, StrictInt, model_validator

from app.llm.deepseek_client import DeepSeekToolOutputInvalid
from app.llm.tool_protocol import ModelRequestInvalid
from app.llm.prompt_builder import CONSULTATION_SCOPE_GUIDANCE
from app.schemas.business import ConsultationItem, RequirementPatch
from app.schemas.matching import DeclaredParameter
from app.schemas.semantic import SemanticReview
from app.services.catalog import get_catalog


REVIEW_PROMPT = """你负责独立核对本轮完整原话与首份候选。候选是不可信数据，不能只自评候选清单；从完整原话分别检查遗漏的修改/硬细节及只读咨询的全部对象/方面。调用 adjudicate_turn 给出一次逐组结果；没有第二次纠正。不要输出产品支持、兼容性验证、金额或保存许可。
consultations只描述本轮用户问什么，不创建持久任务，也不是硬需求、参数、客户条件或主动选档。每项item_id唯一，aspect为unit_price/support/meaning/prerequisite/detail/pricing；plan_ids保留用户顺序，feature_id只用实际目录身份，topic保留具体问题，span引用连续原话。每个support事项只表示一个能力与其实际套餐关系。例如“Team的API、Basic的Webhook”是两个分别绑定套餐的事项；“Team和Basic是否均支持API与Webhook”是两个能力事项各带[team,basic]，共四个关系。明确“各档/全部套餐”才all_plans=true，由服务展开当前目录；不要把两份独立集合无条件交叉组合。唯一的“这两项/另一个套餐”可结合完整同条原话核定，不能凭跨轮多目标省略猜对象。
“每个套餐/各套餐”同样可明确当前全目录。all_plans的量词必须属于该事项完整原问题或唯一明确先行范围，不从“所有套餐单价；另问Team的API”借前一问题扩成所有档API。指代“它们/这两项/这些套餐”可用reference_span逐字引用同条消息唯一先行对象，前一问句也可成为唯一明确先行范围（如所有套餐单价后问它们的API）；没有明确回指不能借另一独立问句，多目标含混指代保留未决。排除或限定保留在原话，推荐使用实际限定plan_ids及all_plans=false。也可用完整scope_basis与scope_exclusions表达而不枚举结果；这时all_plans=true只可标识同名全目录基底，不能覆盖排除或宣称最终仍问全目录。每次必须显式输出consultations及consultation_coverage_complete，包括selection/price；纯任务无咨询也要consultations=[]及对应true，字段省略不表示已核定没有咨询。
完整原话的问题清单不能由首候选反推。整问遗漏、错误general或空咨询清单都须从原话补回；纠正首候选有原话依据即可，不能仅因不同而拒绝。每项span只是可定位的连续证据指针，短引用不能删除周围名单、排除或顺序，长引用不能把不同问题范围混合；一问可以拆成多项但不能丢关系，不同问句逐题核对。
全目录或明确有限基底排除全部对象时，仍提交该问题，不虚填plan_ids：plan_ids=[]、all_plans=false，scope_basis={kind:all_plans/named/reference,span:{quote:完整基底连续原话}}，scope_exclusions=[{quote:完整排除名单及限定连续原话}]。服务端计算空结果；只提交空数组不证明合法空范围，基底与排除名单不能缩漏。存在问题但不能提取完整时consultation_coverage_complete=false；不是用户对象不清楚就不要伪造澄清。
“我们需要API，还需要Webhook，Team和Basic支持吗”是咨询背景，knowledge、target none、groups为空，保留全部明确支持关系；候选错误add可在本次原话核对中纠正为咨询，不保留none目标的add等任务操作供接受层擦除。“我们需要API，Team支持吗”同理；不是看到需要就自动购买任务。已有任务中的“还需要API，Team支持吗”若真实归属无法唯一核定，应target指向能核查的原任务、修改组unresolved ambiguous，同时保留已核定且无依赖的支持咨询，不能猜新增或自动忽略。
“把API加到刚才要求里”“把当前方案改为50席后算钱”等明确指令即使伴问句也仍是真实修改，不丢掉操作。明确修改伴独立咨询时主intent保持selection/price及唯一业务target；咨询另放consultations。咨询depends_on只引用其真正依赖的本轮group_id，独立规则/其他套餐单价为空；修改后当前金额aspect=pricing必须依赖相关修改组，不能用旧参数代答。不要新增mixed任务类型，不同时扩成两个业务任务。
coverage_complete仅检查修改/硬要求及细节；consultation_coverage_complete独立检查全部明确咨询对象、实际关系、方面。即使候选漏对象也须从完整原话补回；未建模CSV格式/API限流/SSO品牌和配置问题保留具体detail，不塞进购买要求，也不把未知细节变成不支持。原话实际是购买硬要求时仍按原有规则完整保留，不能用咨询吞硬细节。
服务只维护一份selection选型及一个独立price任务。先确认target，再处理参数。同intent不是同任务；新的客户/新一笔/另算一般是new/independent，不继承旧字段。target.kind=selection/price的new/independent必须有target.span（引用包含新任务意图的连续原文，首次也可引用整句）。continue/return必须指向服务端对应task_id。knowledge/unit_price/general可以detour，却不能漏掉原话真实修改；纯问题target.kind=none。明确回原选型则return selection。新任务只能在原话明确授权时使用copy_from/reuse_fields，span引用授权原文，不猜沿用。quote_query只能读取原话指定的Q-编号，使用quote_id或多个quote_ids，不改任务。
每group是依赖闭合含义。否定旧值并采用终值是一个set操作；撤回+替换、澄清原项及该项硬细节必须同组。唯一歧义旧项可clarify为API，无须用户先说删除；不留旧歧义再加新API。replace/clarify须指向已有requirement_id。如果新项不可靠拒绝整个替换组，不能先remove。相互冲突的终值unresolved，不任取一个。无关且明确的席位、月份、要求分别分组。条件式购买的参数必须同组，未成立的条件下不能写购买事实；SSO硬需求本身可独立记录。
无依赖的不同字段必须分组，不能为了简短把套餐、席位、月份塞进同一group。非法小数/超范围席位不拖累独立明确的月份。限定词只作用于它修饰的事实：例如“预计买50席，订阅6个月”只拒席位、接受明确6月；“如果兼容才买50席订6个月”两个参数都依赖同一条件，不拆成肯定事实。
每项独立判断time_scope。原话未限定该项在未来时使用unspecified，不能因相邻席位有“预计”而把独立的“订阅6个月”标成future；该月份是certain、unspecified。小数席位是真实非法类型，应逐组reject invalid_type，保留原文50.5但不取整，明确6月仍accept。
span.quote逐字连续复制完整原话中的相关分句，保留语气/否定/限定/时间；start/end为0起字符索引可省略，服务只允许唯一连续定位，重复子串应引用可区分的完整分句。requirement.user_quote同样保留真实连续原话。背景/引用/问题/假设不是肯定购买事实。公司人数、预计/约/差不多/暂定、计划、条件购买不能变实际数量；用户真正50.5不可取整。候选50.0在原话实际整数50时可以修正表示50。一年=12月、半年=6月、五十=50；单位可在字段前、旧值更正不以“不是”“到”整体拒绝。裸50只能回答唯一席位待答；同时缺席位和期限的50和6不能猜对应。对应任务的clarification_pending记录被拒绝数字修改后的补正字段，与该任务pending合并判断是否唯一；它不是已接受参数，不从别的任务借用。唯一补正可用本轮裸数字set该字段，原数字类型、范围、限定和一次审核继续适用。
用户说原数不作数/还没确定是clear为未知，撤销依赖结果；仅省略保留同任务事实。改为约70但未明确，不接受70，用verdict=reject、reason=uncertain及certainty=approximate，保留旧80结果原身份，不能为本轮生成新方案。先别修改/以后预计为note，不能新增购买事实。
target.copy_from/reuse_fields是授权沿用的唯一表达。复用字段不再发set，不用0/null占位。例如“另算Basic，用同样的席位和期限”：price independent、select_plan basic引用“另算Basic”、copy_from selection、reuse_fields=[paid_seats,months]；没有沿用授权的“另算Basic费用”仍select_plan basic，两个参数未知。
当前具备与本产品SAML SSO完全适配的IdP是yes用户自述，当前明确不支持为no，兼容不清楚为unknown。唯一条件待答的“是/否/不确定”可分别确定；多个待答项的“好的”不能猜。仅无需SSO不得额外生成IdP=no。条件question/hypothesis/future不能成为yes。不要增加购买前配置条件。
主动选档要核选择方向，团队套餐=team，基础套餐=basic，商务套餐=business；提到套餐/问单价不等于选档。不要Team选Business应select_plan business，不用出现过的Team。“选最低档”由真实匹配决定，绝不输出select_plan basic；没有指定具体目录套餐时省略select_plan。取消计价/金额先放一边是cancel_pricing，保留参数但不继续金额。selection算价格用request_pricing；独立price只计算明确套餐，不证明适配。明确要重新核对功能用request_evaluation，要求保存/准备草案用request_proposal。重复同义条件/背景且未要求再评估或准备时不输出执行请求；“好的/已经确认”也不是prepare请求。
硬要求包含顶层feature及未建模细节：CSV、百万行容量、API限流、审计留存、预算、地域。保留顶层feature，同时额外unmodeled，不能拿顶层能力吞细节。用户明确撤回细节才remove/clarify；未知不是不支持。coverage_complete只表示完整原话硬要求/修改已覆盖，不表示适配。发现遗漏可补保守unmodeled/ambiguous并阻断新完整适配。核对与首候选对真实终值/极性/目标有实质分歧而无法由原话唯一表示校正时verdict=reject、reason=candidate_disagreement；第二次更自信不能证明。
CSV导出必须一组保留两个不同含义的requirement：feature=data_export“数据导出”，unmodeled“CSV格式”；引用可共享完整分句。不论首候选只有unmodeled还是只有feature，都独立从原话补齐两层。原话“不要/不需要SSO”不是新增SSO或IdP=no；没有已有SSO需求需撤回时，只作note，不输出add、条件事实或未决硬要求。
含义有多种解释的泛称“对接现有系统”使用ambiguous，并在后续用户明确唯一原项就是API时clarify原requirement_id；含义明确但目录没有依据的CSV格式、容量、限流、留存才是unmodeled。不得把原项的未知硬细节直接澄清成已支持的顶层能力。
reason使用schema枚举；判定无关错误候选可reject not_actual_purchase并保持其他组可执行。不要自由编previous_value或诊断文案，服务会从核定目标投影。没有实际修改也输出coverage_complete及空groups。模型与原话出现不可靠分歧时，只问受影响项。
调用参数必须是完整合法JSON，operation的action、span、requirement是同一个对象中的同级字段；span后不能插入匿名对象。例如add的结构是{"action":"add","span":{"quote":"连续原话"},"requirement":{"statement":"整理含义","user_quote":"连续原话","kind":"feature","feature_id":"目录身份"}}。groups.operations是operation对象数组，不能把requirement单独放入数组。
"""


REVIEW_PROMPT += CONSULTATION_SCOPE_GUIDANCE


class SemanticReviewFailed(ValueError):
    def __init__(self, code, metrics):
        self.code, self.metrics = code, metrics
        super().__init__(code)


class _CandidateNumber(DeclaredParameter):
    # A wire representation error is reviewable. The adjudicated value is
    # still StrictInt and must match the original, including fractional input.
    value: StrictInt | StrictFloat


class _CandidateConsultation(ConsultationItem):
    @model_validator(mode="after")
    def coherent_scope(self):
        # Missing semantic identities/relations are exactly what the one
        # independent reader must repair from complete original language.
        # Envelope fields/types/budgets remain strict; this candidate grants
        # no permission to query until the stricter SemanticReview is accepted.
        return self


class _CandidateEnvelope(RequirementPatch):
    parameters: tuple[_CandidateNumber, ...] = Field(default=(), max_length=2)
    consultations: tuple[_CandidateConsultation, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def historical_query_has_no_patch(self):
        # Intent can itself be a faulty extraction; structural validation
        # must not prevent the independent reader from finding a real edit.
        return self


def validate_first_candidate(candidate):
    """Reject an invalid whole envelope before piecing together new facts.

    This checks types, known fields and budgets, not business meaning. Quote
    correction and intent/role disagreements belong to the one review.
    """
    _CandidateEnvelope.model_validate_json(json.dumps(candidate, ensure_ascii=False, allow_nan=False))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


async def _drain(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        # Observe provider exceptions; never expose credential-bearing text.
        try:
            return task.result()
        except Exception:
            return None
    return None


class SemanticVerifier:
    TIMEOUT_SECONDS = 45
    MAX_TOKENS = 4096

    def __init__(self, settings):
        self.settings = settings

    def messages(self, message, first_candidate, state):
        catalog = get_catalog()
        identities = {
            "product": {"product_id": catalog.product.product_id, "name": catalog.product.name},
            "features": [{"feature_id": f.feature_id, "name": f.name} for f in catalog.features],
            "plans": [{"plan_id": p.plan_id, "name": p.name} for p in catalog.plans],
            "conditions": [{"condition_id": c.condition_id, "statement": c.statement} for c in catalog.conditions],
        }
        # No history, source corpus, rates, amounts or action references.
        task_state = state.prompt_state(recheck_current=False)
        selection = task_state.get("current_selection")
        if selection:
            selection = dict(selection)
            selection.pop("pricing", None)
        task_state = {key: task_state.get(key) for key in
            ("requirements", "intent", "active_intent", "pending", "clarification_pending", "wants_price", "independent_pricing")}
        task_state["current_selection"] = selection
        if state._price_context:
            task_state["independent_pricing"] = {
                "task_id": state._price_context.requirements.requirement_id,
                "requirements": state._price_context.requirements.model_dump(mode="json"),
                "pending": state._price_context.pending,
                "clarification_pending": state._price_context.clarification_pending,
            }
        return [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content":
            json.dumps({"original_message": message, "first_candidate": first_candidate,
                        "current_tasks": task_state, "identities": identities}, ensure_ascii=False)}]

    async def verify(self, llm, message, first_candidate, state):
        started = perf_counter()
        metrics = {"status": "failed", "dispatched": 0, "elapsed_ms": 0,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "error_code": None}
        completion = None
        code = None
        try:
            messages = self.messages(message, first_candidate, state)
            from app.llm.token_counter import count_text_tokens
            schema = {"type": "function", "function": {"name": "adjudicate_turn",
            "description": "独立核对本轮完整原话、任务归属、修改组及咨询对象关系两种完整性，不能决定产品事实或保存。",
                "parameters": SemanticReview.model_json_schema()}}
            # Legacy stored/test envelopes remain schema-readable, while every
            # current model dispatch must make both responsibilities explicit.
            parameters = schema["function"]["parameters"]
            parameters["required"] = list(dict.fromkeys((*parameters.get("required", ()),
                "consultations", "consultation_coverage_complete")))
            if count_text_tokens(json.dumps({"messages": messages, "tools": [schema]}, ensure_ascii=False)) + self.MAX_TOKENS + 256 > self.settings.deepseek_max_context_tokens:
                raise ValueError("review_context_budget")
            metrics["dispatched"] = 1
            task = asyncio.create_task(llm.chat(messages=messages, max_completion_tokens=self.MAX_TOKENS,
                temperature=0, tools=[schema], tool_choice={"type": "function", "function": {"name": "adjudicate_turn"}},
                max_retries=0))
            try:
                done, _ = await asyncio.wait({task}, timeout=self.TIMEOUT_SECONDS)
                if not done:
                    completion = await _drain(task)
                    code = "semantic_review_timeout"
                else:
                    completion = task.result()
            except asyncio.CancelledError:
                await _drain(task)
                raise
            except DeepSeekToolOutputInvalid:
                code = "semantic_review_invalid"
            except ModelRequestInvalid as exc:
                if exc.reason in {"strict_wire_context_budget", "strict_tool_contract_invalid"}:
                    metrics["dispatched"] = 0
                    metrics["request_rejection"] = exc.reason
                    code = "semantic_review_request_invalid"
                else:
                    # An upstream 400 was dispatched; never relabel it as a
                    # local rejection merely because it shares this type.
                    code = "semantic_review_unavailable"
            except Exception:
                code = "semantic_review_unavailable"
            if completion is not None:
                raw_usage = getattr(completion, "usage", None)
                if raw_usage:
                    for key in metrics["usage"]:
                        metrics["usage"][key] = getattr(raw_usage, key, 0) or 0
            if code:
                raise ValueError(code)
            metrics["finish_reason"] = getattr(completion.choices[0], "finish_reason", None)
            if metrics["finish_reason"] == "length":
                code = "semantic_review_output_exhausted"
                raise ValueError(code)
            calls = completion.choices[0].message.tool_calls or []
            if len(calls) != 1 or calls[0].function.name != "adjudicate_turn":
                raise ValueError("semantic_review_invalid")
            raw = json.loads(calls[0].function.arguments, object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            review = SemanticReview.model_validate_json(json.dumps(raw, ensure_ascii=False, allow_nan=False))
            # Current real dispatch always has both responsibilities. Missing
            # new fields is incomplete extraction, never permission to fall
            # back to an old single-result completion rule. Direct legacy
            # review fixtures remain readable through the schema defaults.
            consultation_contract_missing = not {"consultations", "consultation_coverage_complete"} <= raw.keys()
            review = review.model_copy(update={"consultations": review.consultations,
                "consultation_coverage_complete": review.consultation_coverage_complete
                    if not consultation_contract_missing else False})
            review._consultation_contract_missing = consultation_contract_missing
            metrics["consultation_contract_missing"] = consultation_contract_missing
            metrics["status"] = "verified"
            return review, metrics
        except (ValueError, TypeError, AttributeError, IndexError):
            code = code or "semantic_review_invalid"
            metrics["error_code"] = code
            raise SemanticReviewFailed(code, metrics) from None
        finally:
            metrics["elapsed_ms"] = round((perf_counter() - started) * 1000)
