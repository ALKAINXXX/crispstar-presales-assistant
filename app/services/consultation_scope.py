"""One source-owned, finite consultation scope for a single user turn.

The inventory is built before looking at submitted items. It describes questions,
not product answers or purchase facts. Items are then mapped to those source
relations; missing relations remain visible and are never silently repaired.
"""

import re
from dataclasses import dataclass, field

from app.schemas.business import SourceSpan
from app.services.catalog import get_catalog


PLAN_ALIASES = {
    "basic": ("Basic", "基础套餐", "基础版", "基础档"),
    "team": ("Team", "团队套餐", "团队版", "团队档"),
    "business": ("Business", "商业套餐", "商业版", "商务套餐", "企业套餐", "企业版", "企业档"),
}
FEATURE_ALIASES = {
    "api_access": ("API", "程序接口", "编程接口"),
    "webhook_notifications": ("Webhook", "事件通知", "事件回调"),
    "data_export": ("导出",), "audit_logs": ("审计",),
    "saml_sso": ("SSO", "SAML", "统一登录", "单点登录"),
    "project_collaboration": ("项目协作", "任务协作", "协作"),
    "basic_roles": ("角色", "成员权限"), "private_deployment": ("私有化",),
}
ALL = re.compile(r"(?:全部|所有)(?:当前)?(?:的)?(?:套餐|版本)|每(?:一)?(?:个|种)(?:当前)?(?:的)?套餐|各(?:个)?套餐|各档|三档|全目录")
PLAN_REFERENCE = re.compile(r"它们|它俩|这(?:两|几|些)(?:个|种)?(?:套餐|档)|上述(?:套餐|各档)|这些版本|另一个(?:套餐|版本|档)?|(?:其余|剩余)(?:当前)?(?:的)?套餐")
FEATURE_REFERENCE = re.compile(r"这(?:一|两|几|些)?(?:个|种)?(?:项|能力|功能)|该(?:项)?(?:功能|能力)|上述能力")
UNIT_PRICE = re.compile(r"单价|每席|月费|订阅价格|订阅费用")
FEE_TOPIC = re.compile(r"基价|费用|价格|收费|计费|单价|月费|金额|总价|总额|钱|税|折扣|实施费|服务费")
FEE_PROPERTY = re.compile(r"包含|包括|涵盖|含税|含不含|含哪些|(?:不|是否)?含(?:费用|税|折扣|实施|服务)|口径|构成|组成|费用项|哪些费用|(?:收费|计费)(?:方式|规则|周期|项目)|(?:如何|怎么)收费|按什么计费")
FEE_MEMBERSHIP = re.compile(r"(?:算|计)(?:在(?:里面|内|其中)|进去|入)|纳入")
AMOUNT_QUESTION = re.compile(r"多少钱|(?:总价|总额|金额|合计|总共|一共)[^？?。]*?多少")
TOTAL_AMOUNT = re.compile(r"总价|总额|总费用|合计|总共|一共|金额|算钱")
PREREQUISITE_ASK = re.compile(r"(?:需要|必须|要)(?:先|预先)?(?:具备|准备|满足)(?:什么|哪些)|(?:使用|启用|开通|接入)(?:之前|前).*?(?:需要|必须|要).*?(?:具备|准备|满足|有)")
SUPPORT = re.compile(r"支持|兼容|是否(?:有|能)|能否|可否|具备|有没有")
PREREQUISITE = re.compile(r"前提|条件|身份提供方")
MEANING = re.compile(r"含义|什么意思|是什么|什么是|指什么|怎么理解")
DETAIL = re.compile(r"限流|格式|容量|留存|地域|品牌|配置|规则|如何|怎么|多少|哪些|什么")
QUESTION = re.compile(r"[？?吗呢]|多少|哪些|是否|能否|可否|有没有|什么|如何|怎么|请.*(?:告诉|说明|列出|查询|查)|(?:问|查|比较).*(?:单价|支持|前提|含义)")
REQUEST_VERB = r"(?:告诉我|说明(?!书)|解释(?!器)|列出|查询|说(?!明书|法|辞|过|了|到|起)(?:说|一下|一说)?)"
REQUEST = re.compile(r"^\s*(?:(?:请|先|再|并且|并|也|还|然后|接着|同时|分别|最后)\s*)*" + REQUEST_VERB)
EXCLUSION_PREFIX = re.compile(r"(?:除了|除去|排除|不含|不包括|不包含|不问|不查|不用问|不用查)\s*")
EXCLUSION_SUFFIX = re.compile(r"\s*(?:除外|之外|以外|不用查|不用问|不查|不问)")
BOUNDARY = re.compile(r"[。！？!?；;\n]|另外|此外|另问|顺便问")


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    identity: str


def _tokens(aliases, text):
    found = []
    for identity, labels in aliases.items():
        for label in labels:
            expression = re.escape(label)
            if re.search(r"[A-Za-z]", label):
                expression = r"(?<![A-Za-z])" + expression + r"(?![A-Za-z])"
            found.extend(Mention(match.start(), match.end(), identity)
                for match in re.finditer(expression, text, re.I))
    # Longer aliases own their characters. Chinese/English concatenation is
    # allowed; two spellings of one identity deduplicate only at set projection.
    result = []
    for token in sorted(found, key=lambda token: (token.start, -token.end)):
        if not any(token.start < other.end and other.start < token.end for other in result):
            result.append(token)
    return tuple(result)


def _ordered(tokens):
    return tuple(dict.fromkeys(token.identity for token in tokens))


def _source(message, start, end):
    return {"quote": message[start:end], "start": start, "end": end}


def _overlap(left, right):
    return left["start"] < right["end"] and right["start"] < left["end"]


@dataclass
class Segment:
    start: int
    end: int
    text: str
    block: int
    plans: tuple = ()
    features: tuple = ()
    exclusions: tuple = ()
    excluded: tuple = ()
    positive: tuple = ()
    aspects: tuple = ()
    basis: tuple = ()
    scope: tuple = ()
    evidence: list = field(default_factory=list)
    ambiguous: bool = False
    universal: bool = False


def _listed(tokens, text, start):
    first = next((token for token in tokens if token.start == start), None)
    if first is None:
        return (), start
    result, end = [first], first.end
    for token in tokens:
        if token.start < end:
            continue
        gap = text[end:token.start]
        if (not gap.strip() and token.identity == result[-1].identity) or re.fullmatch(
                r"\s*(?:以及|、|,|，|和|及|与|/)\s*", gap):
            result.append(token)
            end = token.end
        else:
            break
    return tuple(result), end


def _exclusions(text, tokens):
    ranges = []
    for qualifier in EXCLUSION_PREFIX.finditer(text):
        names, end = _listed(tokens, text, qualifier.end())
        if names:
            ranges.append((qualifier.start(), end, _ordered(names)))
    for token in tokens:
        names, end = _listed(tokens, text, token.start)
        suffix = EXCLUSION_SUFFIX.match(text, end)
        if suffix:
            ranges.append((token.start, suffix.end(), _ordered(names)))
    return tuple(dict.fromkeys(ranges))


@dataclass(frozen=True)
class InquiryAnchor:
    """The source asking predicate and its object, independently of roles."""
    segment_index: int
    start: int
    end: int
    aspect: str
    object_text: str


@dataclass
class TopicFrame:
    """A local source relationship; neither a question nor an answer."""
    segment_index: int
    group: int
    block: int
    plans: tuple
    features: tuple
    relations: tuple
    role_spans: dict
    source: dict
    universal: bool = False
    query: bool = False
    coordinated: bool = False


def _inquiry_kind(text, *, allow_entity_fallback=True):
    # Classify the object of a real asking predicate, rather than every noun
    # occurring in the surrounding text. Modifiers such as 支持SSO的前提
    # belong to that object and do not create another support question.
    changed_amount = re.search(r"(?:改成|改为|修改|调整|把.*改).*(?:算|计算).*(?:费用|价格|金额|总价|钱)", text)
    if re.search(r"(?:示例|引用|原话是|有人说)[：:]*[“\"]", text) and not re.search(r"请|解释|回答", text):
        return None
    if re.search(r"^\s*(?:本次|这次|请)?(?:不问|不用问|不要问)", text):
        return None
    if re.search(r"不确定|不知道|未确认|尚未确认", text) and not re.search(
            r"[？?吗呢]|多少|哪些|什么|如何|怎么|请|想问|问一下", text):
        return None
    # Coordinated commands can omit 请 and question punctuation. The source
    # still owns an asking predicate, rather than becoming mere background.
    asking = bool(QUESTION.search(text) or REQUEST.search(text))
    # A billing property asks for documentation, not a rate or a computed
    # total. Apply the distinction to the asking object before interpreting
    # nearby price nouns or purchase quantities. A plan-list exclusion such
    # as 不包括Basic limits the queried objects, not the contents of a fee.
    fee_object = text
    for start, end, _ in _exclusions(text, _tokens(PLAN_ALIASES, text)):
        fee_object = fee_object[:start] + " " * (end - start) + fee_object[end:]
    membership_question = FEE_MEMBERSHIP.search(fee_object) and not AMOUNT_QUESTION.search(fee_object)
    if asking and FEE_TOPIC.search(fee_object) and (FEE_PROPERTY.search(fee_object) or membership_question):
        return "detail"
    numeric = re.search(r"\d+\s*(?:席|个?月|年)|改成|改为|席位.*改", text)
    if changed_amount or (asking and FEE_TOPIC.search(text)
            and not re.search(r"单价|每席", text)
            and (numeric or TOTAL_AMOUNT.search(text) or re.search(r"计算|算一下|算下|算出", text))):
        return "pricing"
    if UNIT_PRICE.search(text):
        if re.search(r"不查|不用查", text) and not re.search(r"多少|[？?]|请|列出|每席|月费|各套餐|所有|全部|只问", text):
            return None
        if not asking and re.search(r"\d+\s*(?:元|块)", text):
            return None
        return "unit_price"
    if changed_amount or (re.search(r"多少钱|金额|总价|算钱|费用|价格", text) and asking):
        return "pricing" if numeric else "unit_price"
    if not asking:
        return None
    # Definition predicates ask for a concept, even when that concept occurs
    # in a prerequisite (for example an identity provider). A plain copula
    # instead asks for the named property: 前提是什么 / 返回格式是什么.
    if re.search(r"是什么意思|什么意思|指什么|(?:如何|怎么)理解|什么是", text):
        return "meaning"
    if re.search(r"限流|格式|容量|留存|地域|品牌|配置|规则|重试|每分钟|调用.*次", text):
        return "detail"
    if PREREQUISITE_ASK.search(text) or re.search(r"前提|条件", text) or (PREREQUISITE.search(text)
            and re.search(r"需要|必须|具备|满足", text)):
        return "prerequisite"
    if SUPPORT.search(text):
        argument = re.search(r"(?:支持|兼容)\s*([^吗呢？?]+)", text)
        # In an explicit inquiry, API的支持情况 names the capability before
        # the predicate. 情况 is not a new unsupported detail or capability.
        # Keep unknown objects (API支持JSON) and real details on their old path.
        # The same source-owned exclusions already masked above qualify the
        # plan scope; they are not part of the support question's object.
        if argument and re.fullmatch(r"的?情况(?:如何|怎样)?", fee_object[argument.start(1):].strip(" ，,")) \
                and (_tokens(FEATURE_ALIASES, text[:argument.start()])
                    or FEATURE_REFERENCE.search(text[:argument.start()])):
            return "support"
        if argument and not _tokens(FEATURE_ALIASES, argument.group(1)) \
                and not FEATURE_REFERENCE.search(argument.group(1)) \
                and argument.group(1).strip() not in {"", "它", "它们", "这个", "那个"}:
            return "detail"
        return "support"
    if MEANING.search(text):
        return "meaning"
    if DETAIL.search(text):
        return "detail"
    # Retain an explicit bounded asking fragment even when its object cannot
    # yet be assigned. Its unresolved reason is added during role binding.
    if allow_entity_fallback and (_tokens(PLAN_ALIASES, text) or _tokens(FEATURE_ALIASES, text) or FEATURE_REFERENCE.search(text)):
        return "detail"
    return None


def _question_object(text, aspect):
    value = text
    for token in reversed((*_tokens(PLAN_ALIASES, value),)):
        value = value[:token.start] + " " * (token.end - token.start) + value[token.end:]
    for token in reversed(_tokens(FEATURE_ALIASES, value)):
        value = value[:token.start] + " " * (token.end - token.start) + value[token.end:]
    value = re.sub(r"(?:这|该)(?:项|个)?(?:功能|能力)|它们|它|方面|产品|套餐|版本", "", value)
    value = re.sub(r"是多少|是什么|有什么|什么|哪些|多少|是否|能否|可否|怎么|如何|请|问一下|告诉我|说明|列出|需要具备|需要|使用|支持|分别|都|均|[的有是吗呢及和与、，,。！？!?；;\s]", "", value)
    return value if aspect == "detail" else ""


def _predicate_cuts(text):
    """Locate repeated asking predicates even without a punctuation boundary.

    A terminal predicate asks about its preceding object; an opening predicate
    asks about its following object. A closing 吗/呢 belongs to that same
    predicate, rather than creating another inquiry.
    """
    heads = list(re.finditer(r"需要(?:具备|满足)?(?:什么|哪些)|有(?:什么|哪些)|(?:是否|能否|可否)(?:支持|兼容)|是什么|什么意思|指什么|是多少|多少|[吗呢]", text))
    cuts = []
    for previous, current in zip(heads, heads[1:]):
        previous_open = bool(re.match(r"需要|有|是否|能否|可否", previous.group()))
        current_open = bool(re.match(r"需要|有|是否|能否|可否", current.group()))
        between = text[previous.end():current.start()]
        if current.group() in {"吗", "呢"}:
            continue
        if current_open:
            cuts.append(current.start())
        elif not previous_open and between.strip(" ，,以及并且还有和与"):
            cuts.append(previous.end())
    # A second command also opens its own question when punctuation and 请
    # are omitted (单价并说明费用构成). Require a preceding inquiry so a
    # single modified command is not split into an invented earlier question.
    for continuation in re.finditer(r"(?:并且|并|以及|还|再|然后|接着|同时|最后)(?:请)?" + REQUEST_VERB, text):
        if _inquiry_kind(text[:continuation.start()]):
            cuts.append(continuation.start())
    return cuts


def _segments(message):
    """Candidate clauses, not an assertion that punctuation defines questions."""
    result, start, block, group = [], 0, 0, 0
    quoted = [(match.start(), match.end()) for match in re.finditer(r'“[^”]*”|"[^"\n]*"', message)]
    for boundary in (*BOUNDARY.finditer(message), None):
        end = boundary.start() if boundary else len(message)
        text = message[start:end]
        cuts = [0]
        for separator in re.finditer(r"[，,]|以及|并且|还有", text):
            # Name lists and qualifiers remain one source expression. A new
            # asking predicate or explicit coordination component is a cut,
            # whether or not it repeats a plan identity.
            tail = text[separator.end():]
            next_clause = re.split(r"[，,]", tail, maxsplit=1)[0]
            paired_component = bool(_tokens(PLAN_ALIASES, next_clause) and _tokens(FEATURE_ALIASES, next_clause)
                and re.search(r"方面.*(?:问|查|看)", next_clause))
            if _inquiry_kind(next_clause) or paired_component:
                cuts.append(separator.end())
        cuts.append(len(text))
        # Punctuation only proposes intervals. Repeated asking predicates
        # inside one interval identify the actual additional inquiry anchors.
        cuts = sorted(set(cuts + [begin + position for begin, finish in zip(cuts, cuts[1:])
            for position in _predicate_cuts(text[begin:finish])]))
        for begin, finish in zip(cuts, cuts[1:]):
            value = text[begin:finish].rstrip("，,")
            if not value.strip():
                continue
            left, right = start + begin, start + begin + len(value)
            plans = _tokens(PLAN_ALIASES, value)
            features = _tokens(FEATURE_ALIASES, value)
            exclusions = _exclusions(value, plans)
            excluded = tuple(dict.fromkeys(identity for _, _, names in exclusions for identity in names))
            positive = _ordered(tuple(token for token in plans if not any(
                a <= token.start and token.end <= b for a, b, _ in exclusions)))
            kind = _inquiry_kind(value)
            if any(left >= a and right <= b for a, b in quoted):
                kind = None
            segment = Segment(left, right, value, block, plans, features, exclusions,
                excluded, positive, (kind,) if kind else ())
            segment.group = group
            result.append(segment)
        if boundary:
            if boundary.group() in {"另外", "此外", "另问", "顺便问"}:
                block += 1
            start = boundary.end()
            group += 1
    return result


def _frame(segment, index, message):
    """Extract finite local roles in either orientation before expansion."""
    catalog_plans = tuple(plan.plan_id for plan in get_catalog().plans)
    universal = bool(ALL.search(segment.text))
    plans = catalog_plans if universal else segment.positive
    feature_tokens = segment.features
    marker = SUPPORT.search(segment.text)
    if segment.aspects == ("support",) and marker:
        explicit = tuple(token for token in feature_tokens if token.start >= marker.end())
        if explicit:
            feature_tokens = explicit
    features = _ordered(feature_tokens)
    source = _source(message, segment.start, segment.end)
    positive_tokens = tuple(token for token in segment.plans if not any(
        a <= token.start and token.end <= b for a, b, _ in segment.exclusions))
    events = sorted([("plan", token) for token in positive_tokens]
        + [("feature", token) for token in feature_tokens], key=lambda event: event[1].start)
    relations, role_spans = [], {}
    # Ownership edges establish each component's direction locally. One role
    # list remains common; repeated ownership components are split *before*
    # expanding their lists. Mixed directions cannot turn into a whole-clause
    # Cartesian product.
    owners = []
    for event_index, ((kind, token), (other_kind, other)) in enumerate(zip(events, events[1:])):
        gap = segment.text[token.end:other.start].strip()
        ownership = r"的|(?:是否|能否|可否)?(?:支持|兼容)" if kind == "plan" \
            else r"(?:方面)?(?:问|查|看)"
        if kind != other_kind and re.fullmatch(ownership, gap):
            begin = token.start
            for earlier_kind, earlier in reversed(events[:event_index]):
                if earlier_kind != kind or earlier.identity != token.identity \
                        or segment.text[earlier.end:begin].strip():
                    break
                begin = earlier.start
            owners.append(begin)
    if len(owners) > 1:
        starts = [events[0][1].start, *owners[1:]]
        for begin, finish in zip(starts, [*starts[1:], len(segment.text)]):
            local_plans = _ordered(tuple(token for kind, token in events
                if kind == "plan" and begin <= token.start < finish))
            local_features = _ordered(tuple(token for kind, token in events
                if kind == "feature" and begin <= token.start < finish))
            for plan in local_plans:
                for feature in local_features:
                    pair = (plan, feature)
                    if pair not in relations:
                        relations.append(pair)
                    role_spans.setdefault(pair, []).append(_source(message, segment.start + begin,
                        segment.start + finish))
    else:
        relations = [(plan, feature) for plan in (plans or (None,)) for feature in (features or (None,))]
        role_spans = {pair: [source] for pair in relations}
    coordinated = bool((plans or features) and not re.search(r"了解|正在|我们|需要|必须|公司|不问|不查|不用|记录|引用|原话", segment.text)
        and (not segment.aspects or re.match(r"\s*(?:[A-Za-z]|基础|团队|企业|商业|商务)", segment.text)))
    return TopicFrame(index, segment.group, segment.block, plans, features, tuple(relations),
        role_spans, source, universal, bool(segment.aspects), coordinated)


def _combine_frames(frames, segment, index, message):
    relations = tuple(dict.fromkeys(pair for frame in frames for pair in frame.relations))
    spans = {}
    for frame in frames:
        for pair, sources in frame.role_spans.items():
            spans.setdefault(pair, []).extend(source for source in sources if source not in spans.get(pair, ()))
    return TopicFrame(index, segment.group, segment.block,
        tuple(dict.fromkeys(plan for plan, _ in relations if plan)),
        tuple(dict.fromkeys(feature for _, feature in relations if feature)), relations, spans,
        _source(message, segment.start, segment.end), all(frame.universal for frame in frames), True, False)


def _bind_roles(anchor, segment, current, previous, message):
    """The same source-role inheritance for every supported inquiry aspect."""
    plan_reference = PLAN_REFERENCE.search(segment.text)
    feature_reference = FEATURE_REFERENCE.search(segment.text) or re.search(r"这(?:项|个)功能|该功能|它的|它是什么|它需要", segment.text)
    explicit_reference = bool(plan_reference or feature_reference)
    remaining_reference = bool(plan_reference and re.search(r"另一个|其余|剩余", plan_reference.group()))
    available = [frame for frame in previous if frame.block == segment.block
        or (explicit_reference and not remaining_reference)]
    coordinated = []
    for frame in reversed(available):
        if frame.group == segment.group and frame.coordinated and not frame.query:
            coordinated.append(frame)
        elif frame.group == segment.group:
            break
    coordinated.reverse()
    members = coordinated + ([current] if current.plans or current.features else [])
    complete_members = all(all(plan and (feature or anchor.aspect in {"unit_price", "pricing"})
        for plan, feature in frame.relations) for frame in members)
    if coordinated and complete_members and (not current.plans and not current.features or current.coordinated) \
            and (re.search(r"分别|都|均", segment.text) or current.coordinated):
        return _combine_frames(members, segment, anchor.segment_index, message), None, False
    # Finite remainder is a role operation, not a new whole-message entity set.
    if plan_reference and not current.plans and re.search(r"另一个|其余|剩余", plan_reference.group()):
        finite = [frame for frame in available if not frame.query and len(frame.plans) > 1]
        bases = {frame.plans for frame in finite}
        if len(bases) > 1:
            return current, "competing_plan_frames", True
        if len(bases) == 1:
            base = finite[-1]
            used = next((frame.plans for frame in reversed(available) if frame.query), ()) \
                if "另一个" in plan_reference.group() else ()
            plans = tuple(plan for plan in base.plans if plan not in used)
            if "另一个" in plan_reference.group() and len(plans) != 1:
                return current, "competing_plan_frames", True
            current.plans = plans
            current.universal = base.universal
            current.role_spans = {(plan, None): [base.source] for plan in plans}
        elif segment.excluded or any(_exclusions(frame.source["quote"], _tokens(PLAN_ALIASES, frame.source["quote"])) for frame in available):
            current.plans = tuple(plan.plan_id for plan in get_catalog().plans)
            current.universal = True
        else:
            return current, "plan_reference_unresolved", False
        current.relations = tuple((plan, feature) for plan in current.plans
            for feature in (current.features or (None,)))
    want_feature = anchor.aspect not in {"unit_price", "pricing"} and not current.features
    want_plan = not current.plans and anchor.aspect != "pricing"
    compatible = available
    if current.features:
        compatible = [frame for frame in compatible if not frame.features or set(current.features) <= set(frame.features)]
    if current.plans and want_feature:
        matched = [frame for frame in compatible if set(current.plans) <= set(frame.plans)]
        if matched:
            compatible = matched
    local = [frame for frame in compatible if frame.group == segment.group and not frame.query
        and ((want_feature and frame.features) or (want_plan and frame.plans))]
    if local:
        compatible = local
    candidates = [frame for frame in compatible if (want_feature and frame.features) or (want_plan and frame.plans)]
    if want_feature and any(frame.features for frame in candidates):
        candidates = [frame for frame in candidates if frame.features]
    # A later explicit compatible relationship refines an earlier frame with
    # a missing role. Unknown roles are not competing positive identities.
    candidates = [frame for frame in candidates if not any(
        other.segment_index > frame.segment_index
        and any(plan is None or feature is None for plan, feature in frame.relations)
        and all(any((plan is None or plan == p) and (feature is None or feature == f)
            for p, f in other.relations) for plan, feature in frame.relations)
        for other in candidates)]
    if candidates:
        # A coordinated list is one frame. Distinct actual local relations are
        # competing interpretations, rather than a nearest-entity guess.
        projections = {tuple((None if current.plans else plan, None if current.features else feature)
            for plan, feature in frame.relations) for frame in candidates}
        feature_choices = {feature for frame in candidates for feature in frame.features}
        singular_feature = bool(re.search(r"这(?:项|个)功能|该功能|它的|它是什么|它需要", segment.text))
        if len(projections) > 1 or (singular_feature and len(feature_choices) > 1):
            # An explicitly named concept definition is legitimately generic
            # when only its optional plan role competes. Support/prerequisite
            # questions and actual references still require their relationship.
            if anchor.aspect == "meaning" and current.features and not current.plans and not explicit_reference:
                return current, None, False
            return current, "competing_topic_frames", True
        inherited = candidates[-1]
        plans = current.plans or inherited.plans
        features = current.features or inherited.features
        # If one explicit role changes, preserve the inherited other role;
        # fully inherited roles retain their actual pairs rather than crossing.
        if not current.plans and not current.features:
            relations = inherited.relations
            role_spans = {pair: list(inherited.role_spans.get(pair, (inherited.source,))) for pair in relations}
        else:
            relations = tuple((plan, feature) for plan in (plans or (None,)) for feature in (features or (None,)))
            role_spans = {pair: [current.source, inherited.source] for pair in relations}
        current.plans, current.features, current.relations = plans, features, relations
        current.role_spans = role_spans
        current.universal = current.universal or (not _tokens(PLAN_ALIASES, segment.text) and inherited.universal)
    elif not current.relations or any((plan and plan not in current.plans) or
            (feature and feature not in current.features) for plan, feature in current.relations):
        current.relations = tuple((plan, feature) for plan in (current.plans or (None,))
            for feature in (current.features or (None,)))
    if feature_reference and not current.features:
        return current, "feature_reference_unresolved", False
    if plan_reference and not current.plans:
        return current, "plan_reference_unresolved", False
    if anchor.aspect == "support" and not current.features:
        return current, "feature_role_unresolved", False
    if anchor.aspect in {"unit_price", "support"} and not current.plans:
        return current, "plan_role_unresolved", False
    return current, None, False


def _inventory(message):
    segments = _segments(message)
    questions, previous = [], []
    for index, segment in enumerate(segments):
        # A repeated aspect without a new object can limit the preceding
        # inquiry. It does not ask for a second copy of the same answer.
        head, *tail = re.split(r"[，,]", segment.text, maxsplit=1)
        limiter = re.fullmatch(r"\s*(?:这里|本次|这次|本轮|此次)?(?:只|仅)(?:是)?(?:查询|问|查)(?:一下)?"
            r"(?:单价|月费|每席每月(?:的)?(?:单价|价格))\s*", head)
        retained_task = not tail or re.fullmatch(r"\s*(?:请)?(?:不要|不用|无需|先别|别|不)"
            r"(?:修改|改动|更改|改变|调整|变更|改).*(?:方案|选型|任务)\s*", tail[0])
        if limiter and retained_task and questions and questions[-1]["aspect"] == "unit_price" \
                and previous and previous[-1].block == segment.block:
            continue
        frame = _frame(segment, index, message)
        if not segment.aspects:
            if frame.plans or frame.features:
                # Exclusion-only and quoted/background fragments do not ask,
                # and excluded names never become positive antecedent roles.
                if not (segment.excluded and not segment.positive and not frame.universal) \
                        and not re.search(r"原话是|引用|这里只记录|不问", segment.text):
                    frame.plans = tuple(plan for plan in frame.plans if plan not in segment.excluded)
                    frame.relations = tuple(pair for pair in frame.relations if pair[0] not in segment.excluded)
                    previous.append(frame)
            if segment.excluded and not frame.plans and not frame.features:
                # An exclusion establishes a finite catalog remainder,
                # while carrying no positive plan or capability role.
                frame.relations = ()
                previous.append(frame)
            continue
        aspect = segment.aspects[0]
        anchor = InquiryAnchor(index, segment.start, segment.end, aspect, _question_object(segment.text, aspect))
        frame, unresolved, ambiguous = _bind_roles(anchor, segment, frame, previous, message)
        plans = frame.plans
        features = frame.features if aspect not in {"unit_price", "pricing"} else ()
        relations = frame.relations if aspect not in {"unit_price", "pricing"} else tuple((plan, None) for plan in (plans or (None,)))
        if not relations:
            relations = ((None, None),)
        # Each aspect applies to its actual relationships; expansion never
        # reconstructs a Cartesian set from entities elsewhere in the turn.
        feature_order = features or (None,)
        for feature in feature_order:
            basis = tuple(dict.fromkeys(plan for plan, value in relations if value == feature and plan))
            remaining = tuple(plan for plan in basis if plan not in segment.excluded)
            for plan in (remaining or (None,)):
                pair = (plan, feature)
                role_sources = frame.role_spans.get(pair, ())
                if plan is None and basis:
                    role_sources = tuple(source for (p, f), sources in frame.role_spans.items()
                        if f == feature for source in sources)
                source = _source(message, anchor.start, anchor.end)
                evidence = [source]
                for role_source in role_sources:
                    if role_source not in evidence:
                        evidence.append(role_source)
                question_id = f"q{anchor.start}:{aspect}"
                inherited = not segment.positive and not ALL.search(segment.text) and bool(plans)
                questions.append({"question_id": question_id,
                    "inquiry_origin": "entity_fallback" if aspect == "detail"
                        and _inquiry_kind(segment.text, allow_entity_fallback=False) is None else "explicit",
                    "unit_id": f"{question_id}:{plan or '-'}:{feature or '-'}", "order": len(questions),
                    "aspect": aspect, "plan_ids": [plan] if plan else [], "feature_id": feature,
                    "topic": segment.text, "spans": evidence, "anchor_span": source,
                    "role_spans": list(role_sources), "question_object": anchor.object_text,
                    "source_item_ids": [], "status": "ambiguous" if ambiguous else "missing",
                    "issue": "ambiguous" if ambiguous else "extraction_failed",
                    "unresolved_reason": unresolved, "scope_empty": bool(basis and not remaining),
                    "scope_unestablished": bool(unresolved),
                    "scope_basis_kind": "reference" if inherited else "all_plans" if ALL.search(segment.text) else "named",
                    "scope_basis_ids": list(basis), "scope_exclusion_ids": list(segment.excluded),
                    "all_plans": frame.universal and not segment.excluded, "segment_index": index})
        # The source frame, rather than accepted model items, supports later
        # compatible aspects. Business state and tool results are not involved.
        segment.basis = frame.plans
        segment.scope = tuple(plan for plan in frame.plans if plan not in segment.excluded)
        # Later questions inherit the user's remaining objects, not the
        # pre-exclusion catalog basis. Keep that original basis on this
        # question for validation of the complete set expression above.
        if segment.excluded:
            frame.plans = segment.scope
            frame.relations = tuple(pair for pair in frame.relations if pair[0] not in segment.excluded)
            frame.universal = False
        previous.append(frame)
    # Preserve the existing finite exclusion impact boundary, now applying it
    # to the actual inquiry anchors rather than a model's submitted fragments.
    for index, control in enumerate(segments):
        if not control.excluded or control.aspects or ALL.search(control.text):
            continue
        candidates = [other for other, value in enumerate(segments) if value.aspects and value.block == control.block]
        if re.search(r"价格|单价|订阅费用", control.text):
            candidates = [other for other in candidates if "unit_price" in segments[other].aspects]
        else:
            before = [other for other in candidates if other < index]
            after = [other for other in candidates if other > index]
            candidates = ([max(before)] if before else []) + ([min(after)] if after else [])
        for owner in candidates:
            affected = [question for question in questions if question["segment_index"] == owner]
            if re.search(r"价格|单价|订阅费用", control.text):
                affected = [question for question in affected if question["aspect"] == "unit_price"]
            remaining = [question for question in affected if not set(question["plan_ids"]) & set(control.excluded)]
            if len(remaining) == len(affected):
                continue
            if len(candidates) > 1:
                for question in affected:
                    question.update(status="ambiguous", issue="ambiguous", unresolved_reason="competing_exclusion_owners")
            else:
                source = _source(message, control.start, control.end)
                for question in affected:
                    question["spans"].append(source)
                    question["all_plans"] = False
                    question["scope_exclusion_ids"] = list(dict.fromkeys((*question["scope_exclusion_ids"], *control.excluded)))
                obligations = {}
                for question in affected:
                    obligations.setdefault((question["question_id"], question["feature_id"]), []).append(question)
                for units in obligations.values():
                    live = [question for question in units if question in remaining]
                    removed = [question for question in units if question not in live]
                    if not live:
                        first = units[0]
                        first.update(plan_ids=[], scope_empty=True,
                            unit_id=f"{first['question_id']}:-:{first['feature_id'] or '-'}")
                        removed = units[1:]
                    questions = [question for question in questions if question not in removed]
    def answer_order(question):
        segment = segments[question["segment_index"]]
        plan = question["plan_ids"][0] if question["plan_ids"] else None
        plans = segment.scope or segment.basis or tuple(question["scope_basis_ids"])
        return (segment.start, plans.index(plan) if plan in plans else -1, question["order"])
    questions.sort(key=answer_order)
    for order, question in enumerate(questions):
        question["order"] = order
    return questions


def selection_task_question(item, message, locate_span, questions, *, requirement_spans=()):
    """Recognize only a catalog-wide selection question, never an answer.

The caller must separately prove the same feature was accepted by this turn's
selection operations. Independent inquiries retain their normal scope checks.
"""
    plans = {plan.plan_id for plan in get_catalog().plans}
    if item.aspect != "support" or item.scope_exclusions \
            or not (set(item.plan_ids) == plans or (item.all_plans and not item.plan_ids)):
        return None
    try:
        source = locate_span(item.span, message)
        basis = locate_span(item.scope_basis.span, message) if item.scope_basis else None
        reference = locate_span(item.reference_span, message) if item.reference_span else None
        facts = [locate_span(span, message) for span in requirement_spans]
    except ValueError:
        return None
    # A source span is an evidence pointer, not the complete extent of a task.
    # Bind a generic recommendation to accepted same-feature facts in its
    # original block. An explicit independent-question boundary cuts that link.
    boundaries = list(re.finditer(r"另外|此外|另问|顺便问", message))
    def block(span):
        start = max((m.end() for m in boundaries if m.end() <= span["start"]), default=0)
        end = min((m.start() for m in boundaries if m.start() >= span["start"]), default=len(message))
        return (start, end) if span["end"] <= end else None
    owned_block = block(source)
    if owned_block is None or not any(block(fact) == owned_block for fact in facts):
        return None
    if reference and (block(reference) != owned_block
            or not any(_overlap(reference, fact) for fact in facts)
            or any(_overlap(reference, q["anchor_span"])
                   and q.get("inquiry_origin") != "entity_fallback" for q in questions)):
        return None
    task = {"start": owned_block[0], "end": owned_block[1]}
    overlapping = [question for question in questions if _overlap(source, question["anchor_span"])]
    if any(question.get("inquiry_origin") != "entity_fallback"
           or question["feature_id"] not in {None, item.feature_id} for question in overlapping):
        return None
    # A long source must not consume another explanation or an unknown detail.
    if re.search(r"原文|依据|文档|资料|解释|说明|为什么|如何|怎么|限流|格式|容量|留存", source["quote"]):
        return None
    clauses = list(re.finditer(r"[^。！？!?；;\n]+", message))
    owners = [clause for clause in clauses
        if source["start"] < clause.end() and clause.start() < source["end"]]
    if not owners:
        return None
    owner = owners[-1]
    if re.search(r"原文|依据|文档|资料|解释|说明|为什么|如何|怎么|限流|格式|容量|留存", owner.group()):
        return None
    named = set(_ordered(_tokens(PLAN_ALIASES, owner.group())))
    if named and named != plans:
        return None
    # A prior restriction within the same reviewed task must not disappear
    # because the asking clause itself omits the names. Independent questions
    # cannot contribute their own plan names to this task's restriction.
    task_clauses = [clause.group() for clause in clauses
        if task["start"] <= clause.start() and clause.end() <= task["end"]
        and not any(q.get("inquiry_origin") != "entity_fallback"
                    and clause.start() < q["anchor_span"]["end"]
                    and q["anchor_span"]["start"] < clause.end() for q in questions)]
    task_names = set(_ordered(_tokens(PLAN_ALIASES, "。".join(task_clauses))))
    if task_names and task_names != plans:
        return None
    basis_end = basis["start"] + len(basis["quote"].rstrip("。！？!?；; \t\r\n")) if basis else None
    if basis and (not owner.start() <= basis["start"] < basis_end <= owner.end()
            or item.scope_basis.kind == "reference"
            or (item.scope_basis.kind == "named" and set(_ordered(_tokens(PLAN_ALIASES, basis["quote"]))) != plans)
            or (item.scope_basis.kind == "all_plans" and not task["start"] <= basis["start"] < basis_end <= task["end"])):
        return None
    return {"question_id": "selection:" + item.item_id, "unit_id": "selection:" + item.item_id, "aspect": "support",
        "owned_unit_ids": [question["unit_id"] for question in overlapping],
        "feature_id": item.feature_id, "topic": item.topic, "spans": [source],
        "plan_ids": [plan.plan_id for plan in get_catalog().plans], "source_item_ids": [item.item_id],
        "status": "business_owned", "issue": "confirmed"}


def _mapped_question_candidates(item, span, questions):
    # Source pointers may address the actual asking clause or its specifically
    # bound relationship component. An unrelated component in a long source
    # is not an identity permission.
    def owns_question(question):
        if _overlap(span, question["anchor_span"]):
            return True
        # A local topic component may substantiate its asking predicate.
        # An earlier independent question can supply inherited roles, but a
        # pointer to that question cannot also answer a later inquiry.
        return any(_overlap(span, source) for source in question["role_spans"]) and not any(
            other["question_id"] != question["question_id"]
            and _overlap(span, other["anchor_span"]) for other in questions)

    candidates = [question for question in questions if question["aspect"] == item.aspect
        and (question["feature_id"] == item.feature_id or question["unresolved_reason"])
        and owns_question(question)]
    anchors = {question["question_id"] for question in candidates}
    if item.aspect == "detail" and len(anchors) > 1:
        # These are original inquiry objects, not model-generated identities.
        # A long quote can substantiate a merge only when the reviewed topic
        # actually names both distinct objects. It cannot broadcast one detail.
        selected = [question for question in candidates if question["question_object"]
            and question["question_object"] in _question_object(item.topic, "detail")]
        candidates = selected
    return candidates


def _is_source_instruction(text):
    verbs = r"引用|注明|标注|列出|展示|提供|给出|附上|贴出|附带"
    sources = r"原文|原句|资料|文档|来源|出处|依据|链接"
    grounded_answer = re.search(
        r"(?:根据|依据|基于).*?(?:原文|原句|资料|文档|来源|出处|链接).*?(?:说明|解释|回答)", text)
    if not grounded_answer and (not re.search(verbs, text) or not re.search(sources, text)):
        return False
    residue = re.sub(verbs + "|" + sources +
        r"|根据|基于|解释|请|也|并|再|同时|另外|还|把|将|的|里|中|相关|相应|对应|上述|本次|回答|答案|结论|说明|一并|一起|出来|一下", "", text)
    return not residue.strip(" ，,。！？!?；;：:\t\r\n")


def _source_requirement_owner(item, span, reference, questions, message):
    """Attach a source-only answer instruction to an existing RAG question.

    The closed vocabulary excludes new product facts and details. A unique,
    matching source question is required; no plan or capability is invented.
    """
    if item.aspect != "detail" or item.scope_exclusions:
        return []
    if not _is_source_instruction(span["quote"]):
        return []
    candidates = [q for q in questions if q["aspect"] in {"support", "meaning", "prerequisite", "detail"}
        and q["status"] == "accepted" and q["source_item_ids"]
        and (item.feature_id is None or item.feature_id == q["feature_id"])
        and (not item.plan_ids or set(item.plan_ids).intersection(q["plan_ids"]))
        and q["anchor_span"]["end"] <= span["start"]]
    if reference:
        candidates = [q for q in candidates if _overlap(reference, q["anchor_span"])]
    else:
        candidates = [q for q in candidates if not message[q["anchor_span"]["end"]:span["start"]].strip(
            " ，,。！？!?；;\t\r\n")]
    if set(item.plan_ids) - {plan for q in candidates for plan in q["plan_ids"]}:
        # A source-only instruction cannot introduce its own product scope.
        # The reviewer may copy the full catalog from an earlier selection;
        # bind that generic metadata only to the unique source-owned question.
        # A conflicting specific subset still cannot borrow another answer.
        if set(item.plan_ids) != {plan.plan_id for plan in get_catalog().plans}:
            return []
    return candidates if len({q["question_id"] for q in candidates}) == 1 else []

def accept_consultation_scope(review, message, locate_span):
    """Return accepted legacy identities, feedback, and the complete inventory."""
    questions = _inventory(message)
    valid, feedback = [], []
    known_plans = {plan.plan_id for plan in get_catalog().plans}
    known_features = {feature.feature_id for feature in get_catalog().features}
    message_plans = set(_ordered(_tokens(PLAN_ALIASES, message)))
    # A response constraint may be listed before its question by the model.
    # Establish real questions first; source order still belongs to inventory.
    for item in sorted(review.consultations, key=lambda value: _is_source_instruction(value.span.quote)):
        span, issue, mapped, answer_requirement = None, None, [], False
        try:
            span = locate_span(item.span, message)
            reference = locate_span(item.reference_span, message) if item.reference_span else None
            basis = locate_span(item.scope_basis.span, message) if item.scope_basis else None
            exclusion_sources = [locate_span(source, message) for source in item.scope_exclusions]
            if any(identity not in known_plans for identity in item.plan_ids) or (item.feature_id
                    and item.feature_id not in known_features):
                raise ValueError("unknown_identity")
            if any(identity not in message_plans for identity in item.plan_ids) and not item.all_plans:
                # Catalog-wide expressions legitimately name no individual
                # objects; their source is checked below before this identity.
                if not any(question["all_plans"] or question["scope_empty"] or len(question["plan_ids"])
                        and question["plan_ids"][0] == identity for identity in item.plan_ids for question in questions):
                    raise ValueError("unknown_identity")
            candidates = _mapped_question_candidates(item, span, questions)
            if not candidates:
                mapped = _source_requirement_owner(item, span, reference, questions, message)
                if mapped:
                    answer_requirement = True
                    continue
                raise ValueError("reference_missing")
            # Short source pointers may not move a relation to a different
            # question. Full-message pointers can map a legal same-aspect group.
            if reference and not any(_overlap(reference, source)
                    for question in candidates for source in question["spans"]):
                raise ValueError("reference_missing")
            if any(question["status"] == "ambiguous" for question in candidates):
                raise ValueError("ambiguous")
            if any(question["scope_unestablished"] for question in candidates):
                raise ValueError("extraction_failed")
            if basis:
                # In a complete expression this flag may label its universal
                # basis. It cannot override the explicit exclusion expression.
                if item.all_plans and item.scope_basis.kind != "all_plans":
                    raise ValueError("reference_missing")
                proposed = tuple(dict.fromkeys(item.plan_ids))
            elif item.all_plans:
                if not all(question["all_plans"] for question in candidates):
                    raise ValueError("reference_missing")
                proposed = tuple(plan.plan_id for plan in get_catalog().plans)
            else:
                proposed = tuple(dict.fromkeys(item.plan_ids))
            allowed = {identity for question in candidates for identity in question["plan_ids"]}
            if not set(proposed) <= allowed:
                raise ValueError("unknown_identity" if set(proposed) - message_plans
                    and not ALL.search(message) else "reference_missing")
            if basis:
                # The wire may submit a complete set expression instead of
                # enumerating its result. Verify that expression first; empty
                # plan_ids is not itself a claim that the result is empty.
                if any(item.scope_basis.kind != question["scope_basis_kind"] and not (
                        item.scope_basis.kind == "named" and question["scope_basis_kind"] == "reference"
                        and any(_overlap(basis, source) for source in question["role_spans"]))
                        for question in candidates):
                    raise ValueError("reference_missing")
                if item.scope_basis.kind == "all_plans" and not ALL.search(basis["quote"]):
                    raise ValueError("reference_missing")
                if item.scope_basis.kind == "named" and not _tokens(PLAN_ALIASES, basis["quote"]):
                    raise ValueError("reference_missing")
                if not any(_overlap(basis, source) for question in candidates for source in question["spans"]):
                    raise ValueError("reference_missing")
                basis_tokens = _tokens(PLAN_ALIASES, basis["quote"])
                basis_excluded = {identity for _, _, identities in _exclusions(basis["quote"], basis_tokens)
                    for identity in identities}
                basis_ids = tuple(plan.plan_id for plan in get_catalog().plans) if ALL.search(basis["quote"]) \
                    else _ordered(basis_tokens)
                if item.scope_basis.kind == "reference":
                    basis_ids = tuple(identity for identity in basis_ids if identity not in basis_excluded)
                    if not any(_overlap(basis, source) for question in candidates for source in question["spans"][1:]):
                        raise ValueError("reference_missing")
                if any(set(basis_ids) != set(question["scope_basis_ids"]) for question in candidates):
                    raise ValueError("reference_missing")
                exclusions = {identity for source in exclusion_sources
                    for _, _, identities in _exclusions(source["quote"], _tokens(PLAN_ALIASES, source["quote"]))
                    for identity in identities}
                if any(not any(_overlap(source, evidence)
                        for question in candidates for evidence in question["spans"]) for source in exclusion_sources):
                    raise ValueError("reference_missing")
                if any(exclusions != set(question["scope_exclusion_ids"]) for question in candidates):
                    raise ValueError("reference_missing")
                computed = {identity for identity in basis_ids if identity not in exclusions}
                if computed != allowed or not set(proposed) <= computed:
                    raise ValueError("reference_missing")
                if not proposed:
                    proposed = tuple(dict.fromkeys(identity for question in candidates for identity in question["plan_ids"]))
            elif exclusion_sources:
                # These fields form one complete expression, not an ignored
                # second declaration beside otherwise valid explicit IDs.
                raise ValueError("reference_missing")
            if not proposed and item.aspect in {"unit_price", "support"} and (
                    not basis or not exclusion_sources or not all(question["scope_empty"] for question in candidates)):
                raise ValueError("extraction_failed")
            if item.aspect == "pricing" and not item.depends_on and any(group.verdict == "accept"
                    and any(operation.field in {"paid_seats", "months"}
                        and operation.action in {"set", "clear", "replace"} for operation in group.operations)
                    for group in review.groups):
                raise ValueError("dependency_unresolved")
            mapped = [question for question in candidates if not question["plan_ids"]
                or question["plan_ids"][0] in proposed]
            if not mapped:
                raise ValueError("reference_missing")
            ordered_plans = tuple(dict.fromkeys(identity for question in mapped for identity in question["plan_ids"]))
            accepted = item.model_copy(update={"plan_ids": ordered_plans, "span": SourceSpan(**span),
                "all_plans": item.all_plans and set(ordered_plans) == known_plans})
            valid.append(accepted)
            for question in mapped:
                question["source_item_ids"].append(item.item_id)
                question.update(status="empty_scope" if question["scope_empty"] else "accepted", issue="confirmed")
        except ValueError as exc:
            issue = str(exc) if str(exc) in {"reference_missing", "unknown_identity",
                "dependency_unresolved", "extraction_failed", "ambiguous"} else "extraction_failed"
        finally:
            feedback.append({"item_id": item.item_id, "status": "rejected" if issue else "verified",
            "issue": issue or "confirmed", "spans": [span] if span else [],
            "depends_on": list(item.depends_on), "topic": item.topic,
            "plan_ids": list(dict.fromkeys(plan for question in mapped for plan in question["plan_ids"]))
                if answer_requirement else list(item.plan_ids), "aspect": item.aspect,
            "question_ids": list(dict.fromkeys(question["question_id"] for question in mapped)),
            "unit_ids": [question["unit_id"] for question in mapped],
            **({"purpose": "answer_requirement"} if answer_requirement else {})})
    for question in questions:
        if not question["source_item_ids"]:
            feedback.append({"item_id": question["unit_id"], "status": question["status"],
                "issue": question["issue"], "spans": question["spans"], "depends_on": [],
                "topic": question["topic"], "plan_ids": question["plan_ids"], "aspect": question["aspect"],
                "question_ids": [question["question_id"]], "unit_ids": [question["unit_id"]]})
        question.pop("segment_index")
    valid.sort(key=lambda item: min(question["order"] for question in questions
        if item.item_id in question["source_item_ids"]))
    return tuple(valid), feedback, tuple(questions)
