"""Finite current-turn inquiry coverage from accepted scope and real receipts.

No catalog display context, old task result or model final prose enters this
collector. It never performs a query, retries a tool, or creates business state.
"""

import json
import re

from app.schemas.catalog import Catalog
from app.schemas.chat import (ConsultationCoverage, ConsultationPrerequisite, ConsultationResult,
                              ConsultationSupport, UnitPriceObservation)
from app.schemas.pricing import PricingResult, UnitPriceResult
from app.schemas.retrieval import RetrievalEvidence


def _validated(value, schema):
    if isinstance(value, schema):
        return schema.model_validate(value)
    return schema.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


class TurnConsultationResults:
    """Keep every checked result association; conflicts have no winning value."""

    def __init__(self):
        self._prices: dict[str, UnitPriceObservation] = {}
        self.catalogs: list[tuple[Catalog, str]] = []
        self.conflicts: list[dict] = []
        self._conflicted_plans: set[str] = set()
        self._catalog_identity: tuple[str, str, str, str, int] | None = None
        self._identity_conflict = False

    def _identity(self, price, tool_call_id):
        identity = (price.product_id, price.catalog_version, price.currency,
                    price.billing_unit, price.minor_units_per_major)
        if self._catalog_identity is None:
            self._catalog_identity = identity
        elif identity != self._catalog_identity:
            self._identity_conflict = True
            self._conflict("catalog_identity_conflict", price.plan_id, tool_call_id,
                           price.model_dump(mode="json"))

    def _conflict(self, code, plan_id, tool_call_id, candidate):
        self._conflicted_plans.add(plan_id)
        if not any(c["code"] == code and c["plan_id"] == plan_id
                   and c["tool_call_id"] == tool_call_id for c in self.conflicts):
            self.conflicts.append({"code": code, "plan_id": plan_id,
                "tool_call_id": tool_call_id, "candidate": candidate})

    def add_unit(self, value, tool_call_id: str, *, query_kind="single_plan"):
        price = _validated(value, UnitPriceResult)
        self._identity(price, tool_call_id)
        previous = self._prices.get(price.plan_id)
        if previous is None:
            self._prices[price.plan_id] = UnitPriceObservation(price=price,
                tool_call_ids=[tool_call_id], query_kinds=[query_kind])
        elif previous.price != price:
            self._conflict("unit_price_conflict", price.plan_id, tool_call_id,
                           price.model_dump(mode="json"))
        else:
            if tool_call_id not in previous.tool_call_ids:
                previous.tool_call_ids.append(tool_call_id)
            if query_kind not in previous.query_kinds:
                previous.query_kinds.append(query_kind)
        return price

    def add_catalog(self, value, tool_call_id: str):
        catalog = _validated(value, Catalog)
        for previous, _ in self.catalogs:
            if previous != catalog:
                self._identity_conflict = True
                self._conflict("catalog_result_conflict", "*", tool_call_id,
                    {"product_id": catalog.product.product_id, "catalog_version": catalog.version})
        self.catalogs.append((catalog, tool_call_id))
        for plan in catalog.plans:
            self.add_unit(UnitPriceResult(product_id=catalog.product.product_id,
                plan_id=plan.plan_id, plan_name=plan.name, catalog_version=catalog.version,
                currency=catalog.currency, billing_unit=catalog.billing_unit,
                minor_units_per_major=catalog.minor_units_per_major,
                unit_price_minor=plan.unit_price_minor), tool_call_id, query_kind="full_catalog")
        return catalog

    @property
    def observations(self):
        return list(self._prices.values())

    @property
    def unit_prices(self):
        """All valid actual rates; raw fallback, not a reviewed inquiry scope."""
        return [entry.price for entry in self.observations if self.price_valid(entry.price.plan_id)]

    def scoped_unit_prices(self, items):
        """Formal prices for accepted unit-price questions, in their order.

        ``items`` is the service-accepted turn scope, never candidate prose.
        Full-catalog observations remain intact for real receipt disclosure.
        A support-only scope consequently has no formal price collection.
        Callers without a reviewed scope must explicitly use the raw fallback
        and preserve its unfinished/legacy status instead of guessing a scope.
        """
        prices, included = [], set()
        for item in items:
            if (_item_value(item, "aspect") != "unit_price"
                    or _item_value(item, "status") in {"needs_input", "missing", "ambiguous", "blocked", "empty_scope"}
                    or _item_value(item, "stage") in {"extraction_incomplete", "ambiguous_scope", "dependency_unresolved", "empty_scope"}):
                continue
            for identity in _item_value(item, "plan_ids", ()):
                if identity not in included and self.price_valid(identity):
                    prices.append(self._prices[identity].price)
                    included.add(identity)
        return prices

    def price_valid(self, plan_id):
        return (not self._identity_conflict and plan_id not in self._conflicted_plans
                and plan_id in self._prices)

    def coverage(self, items, knowledge_answers=(), evidence=(), records=(), feedback=(),
                 *, scope_complete=True, pricing=None, questions=None):
        return build_consultation_coverage(items, self, knowledge_answers, evidence,
            records, feedback, scope_complete=scope_complete, pricing=pricing,
            questions=questions)


def _item_value(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _bindings(value):
    identities = value.get("consultation_item_ids", ())
    return tuple(identities) if isinstance(identities, (list, tuple)) else ()


def _item_bindings(item):
    """Only identities certified for this answer unit can supply its receipt."""
    item_id = _item_value(item, "item_id")
    return {item_id, *_item_value(item, "source_item_ids", ())}


def _answer_bound(item, answer):
    units = answer.get("consultation_unit_ids")
    if units is not None:
        # An explicit subset must not expand through its shared source item.
        return _item_value(item, "item_id") in units
    return bool(_item_bindings(item) & set(_bindings(answer)))


def _record_bound(item, record, single_item_id=None):
    return bool(_item_bindings(item) & set(_record_bindings(record, single_item_id)))


def _record_data(record):
    result = record.get("result") or {}
    return result.get("data", {}) if isinstance(result, dict) and result.get("ok") else {}


def _record_arguments(record):
    arguments = record.get("accepted_arguments") or record.get("display_arguments") or record.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
    if not isinstance(arguments, dict):
        return {}
    return arguments


def _record_bindings(record, single_item_id=None):
    arguments = _record_arguments(record)
    explicit = _bindings(arguments)
    return explicit or ((single_item_id,) if single_item_id else ())


def _accepted_knowledge(item, answers, evidence):
    """Recheck actual retrieval/continuous quote associations, never keywords."""
    bound = [answer for answer in answers if _answer_bound(item, answer)]
    checked = []
    for answer in bound:
        observation = evidence.get(answer.get("retrieval_id"))
        if observation is None or observation.status == "unavailable":
            continue
        originals = {hit.source.source_id: hit.source for hit in observation.hits
                     if hit.relevance == "related"}
        citations = answer.get("citations", ())
        if any(c.get("source_id") not in originals or not c.get("quote")
               or c["quote"] not in originals[c["source_id"]].raw_text for c in citations):
            continue
        if answer.get("answerability") == "supported" and not citations:
            continue
        if answer.get("answerability") not in {"supported", "insufficient"}:
            continue
        checked.append(answer)
    return checked, bool(bound)


def _knowledge_stage(item, answers, observations, records, catalogs, single_item_id=None):
    answer_set, had_candidate = _accepted_knowledge(item, answers, observations)
    bound_ids = {data.get("retrieval_id") for record in records
                 if record.get("name") == "search_knowledge"
                 for data in [_record_data(record)] if _record_bound(item, record, single_item_id)}
    bound_ids.update(answer.get("retrieval_id") for answer in answers if _answer_bound(item, answer))
    searches = [observations[identity] for identity in bound_ids if identity in observations]
    if answer_set:
        if len({answer["answerability"] for answer in answer_set}) != 1:
            # Neither candidate wins merely because it arrived last.
            return "incomplete", "explanation_rejected", answer_set, searches
        return "completed", ("evidence_insufficient" if all(
            a["answerability"] == "insufficient" for a in answer_set) else "answered"), answer_set, searches
    if had_candidate:
        return "incomplete", "explanation_rejected", [], searches
    rejected = any(record.get("name") == "explain_knowledge"
        and not (record.get("result") or {}).get("ok")
        and _record_bound(item, record, single_item_id)
        for record in records)
    if rejected:
        return "incomplete", "explanation_rejected", [], searches
    if any(search.status == "retrieved" for search in searches):
        return "incomplete", "explanation_pending", [], searches
    if any(search.status == "unavailable" for search in searches):
        return "incomplete", "query_failed", [], searches
    if searches:
        return "incomplete", "evidence_insufficient", [], searches
    return "incomplete", "not_queried", [], searches


def _unsupported_sso_quote(quote, plan, feature_name):
    """Recognize a complete explicit negative relation in an accepted original.

    The actual catalog already determines support. This finite check prevents
    an unrelated/conditional quote from being used as the explanation for that
    fact: the full original sentence must identify the feature and the plan
    must occur in the negative clause's subject, not elsewhere in the sentence.
    It is not a keyword-based rule for general knowledge answerability.
    """
    aliases = {plan.plan_id, plan.plan_id.capitalize(), plan.name}
    aliases.update(part for part in plan.name.split() if part)
    for sentence in re.findall(r"[^。！？\n]+[。！？]", quote):
        if feature_name not in sentence:
            continue
        for clause in re.findall(r"(?:^|[；，])\s*([^；，。！？]+?)\s*不支持(?:该能力|\s*" + re.escape(feature_name) + r")[。！？]", sentence):
            subjects = {part.strip() for part in re.split(r"[、和与及/]+", clause)}
            if aliases & subjects:
                return True
    return False


def _sso_prerequisites(entry, results, accepted, observations):
    """Bind complete adopted SSO rules to true catalog applicability."""
    from app.services.catalog import get_catalog
    catalog = get_catalog()
    if results._identity_conflict or any(actual != catalog for actual, _ in results.catalogs):
        entry.status, entry.stage = "incomplete", "result_conflict"
        return
    if entry.stage == "explanation_rejected":
        # Conflicting verdicts or rejected associations provide no winner.
        return
    feature = next((feature for feature in catalog.features if feature.feature_id == entry.feature_id), None)
    if feature is None:
        entry.status, entry.stage = "incomplete", "result_conflict"
        return
    plans = {plan.plan_id: plan for plan in catalog.plans}
    conditions = {condition.condition_id: condition for condition in catalog.conditions
                  if condition.feature_id == feature.feature_id}
    # A generic rule question retains its unspecified inquiry scope. The
    # formal rule can name only plans that actually carry this condition.
    identities = entry.plan_ids or [plan.plan_id for plan in catalog.plans
        if any(relation.feature_id == feature.feature_id and relation.supported
               and relation.condition_id in conditions for relation in plan.features)]
    supported_answers = [answer for answer in accepted if answer["answerability"] == "supported"]
    call_ids = list(dict.fromkeys(identity for _, identity in results.catalogs))
    missing_rules, invalid_scope = [], False
    for identity in identities:
        plan = plans.get(identity)
        relation = next((relation for relation in plan.features if relation.feature_id == feature.feature_id), None) if plan else None
        if relation is None:
            entry.missing_plan_ids.append(identity)
            invalid_scope = True
            continue
        if not relation.supported:
            # A Business-only condition citation cannot explain why Basic or
            # Team does not offer SSO. Adopt the explicit original relation.
            adopted = [(answer, citation) for answer in supported_answers for citation in answer["citations"]
                if _unsupported_sso_quote(citation["quote"], plan, feature.name)
                and any(hit.source.source_id == citation["source_id"] and hit.source.dataset_version == catalog.version
                        for hit in observations[answer["retrieval_id"]].hits)]
            if adopted:
                entry.support.append(ConsultationSupport(product_id=catalog.product.product_id,
                    catalog_version=catalog.version, plan_id=plan.plan_id, plan_name=plan.name,
                    feature_id=feature.feature_id, feature_name=feature.name,
                    supported=False, tool_call_ids=call_ids,
                    retrieval_ids=list(dict.fromkeys(answer["retrieval_id"] for answer, _ in adopted)),
                    source_ids=list(dict.fromkeys(citation["source_id"] for _, citation in adopted))))
            else:
                missing_rules.append(identity)
            continue
        condition = conditions.get(relation.condition_id)
        adopted = next(((answer, citation) for answer in supported_answers
                        for citation in answer["citations"] if condition and condition.statement in citation["quote"]), None)
        if condition is None or adopted is None:
            missing_rules.append(identity)
            continue
        answer, citation = adopted
        observation = observations[answer["retrieval_id"]]
        original = next(hit.source for hit in observation.hits if hit.source.source_id == citation["source_id"])
        if original.dataset_version != catalog.version:
            missing_rules.append(identity)
            continue
        entry.prerequisites.append(ConsultationPrerequisite(product_id=catalog.product.product_id,
            catalog_version=catalog.version, plan_id=plan.plan_id, plan_name=plan.name,
            feature_id=feature.feature_id, feature_name=feature.name,
            condition_id=condition.condition_id, statement=condition.statement,
            quote=citation["quote"], retrieval_id=answer["retrieval_id"], source_id=citation["source_id"],
            tool_call_ids=call_ids))
    entry.missing_plan_ids.extend(missing_rules)
    if invalid_scope:
        entry.status, entry.stage = "incomplete", "result_conflict"
    elif missing_rules or not identities:
        entry.status = "incomplete"
        if accepted:
            entry.stage = "explanation_rejected"
    elif entry.stage == "answered":
        entry.status = "completed"


def build_consultation_coverage(items, results, knowledge_answers=(), evidence=(),
                                records=(), feedback=(), *, scope_complete=True,
                                pricing: PricingResult | None = None, questions=None):
    # None is the genuine legacy collector contract. An explicitly empty
    # question list is a reviewed current turn with no inquiries, never a
    # request to reconstruct questions from tool returns or candidate items.
    if questions is not None:
        items = [dict(question, item_id=question["unit_id"])
                 for question in sorted(questions, key=lambda value: value["order"])]
    observations = {e.retrieval_id: e for e in evidence}
    knowledge_items = [item for item in items if _item_value(item, "aspect")
                       in {"support", "meaning", "prerequisite", "detail"}
                       and _item_value(item, "status") not in {"missing", "ambiguous", "blocked", "empty_scope"}]
    # Registry inference is over original valid identities, not the number of
    # answer units (one source item can legitimately cover several units).
    original_ids = list(dict.fromkeys(identity for item in knowledge_items
        for identity in (_item_value(item, "source_item_ids", ())
                         or (_item_value(item, "item_id"),))))
    single_item_id = original_ids[0] if len(original_ids) == 1 else None
    coverage = []
    for item in items:
        item_id, aspect = _item_value(item, "item_id"), _item_value(item, "aspect")
        plan_ids = list(dict.fromkeys(_item_value(item, "plan_ids", ())))
        topic = _item_value(item, "topic", "本轮咨询")
        entry = ConsultationResult(item_id=item_id, aspect=aspect, plan_ids=plan_ids,
            question_id=_item_value(item, "question_id"),
            source_item_ids=list(_item_value(item, "source_item_ids", ())),
            order=_item_value(item, "order"),
            feature_id=_item_value(item, "feature_id"), topic=topic,
            status="incomplete", stage="not_queried")
        accepted_status = _item_value(item, "status")
        if accepted_status in {"empty_scope", "missing", "ambiguous", "blocked"}:
            entry.status, entry.stage = {
                "empty_scope": ("completed", "empty_scope"),
                "missing": ("incomplete", "extraction_incomplete"),
                "ambiguous": ("needs_input", "ambiguous_scope"),
                "blocked": ("incomplete", "dependency_unresolved"),
            }[accepted_status]
            if accepted_status != "empty_scope":
                entry.missing_plan_ids = list(plan_ids)
            coverage.append(entry)
            continue
        if aspect == "unit_price":
            for identity in plan_ids:
                if results.price_valid(identity):
                    observation = results._prices[identity]
                    entry.unit_prices.append(observation.price)
                    entry.tool_call_ids.extend(observation.tool_call_ids)
                else:
                    entry.missing_plan_ids.append(identity)
            if not entry.missing_plan_ids and plan_ids:
                entry.status, entry.stage = "completed", "answered"
            elif results._identity_conflict or set(plan_ids) & results._conflicted_plans:
                entry.stage = "result_conflict"
            elif any(record.get("name") == "query_catalog"
                     and not (record.get("result") or {}).get("ok")
                     and ((record.get("result") or {}).get("error") or {}).get("code")
                         not in {"dependency_not_executed", "semantic_review_required"}
                     and _record_arguments(record).get("plan_id") in {None, *entry.missing_plan_ids}
                     for record in records):
                entry.stage = "query_failed"
        elif aspect == "pricing":
            if pricing and (not plan_ids or pricing.plan_id in plan_ids):
                entry.status, entry.stage = "completed", "answered"
            else:
                entry.stage = "dependency_unresolved"
        else:
            state, stage, accepted, searches = _knowledge_stage(
                item, knowledge_answers, observations, records, results.catalogs, single_item_id)
            entry.status, entry.stage = state, stage
            entry.retrieval_ids = list(dict.fromkeys(
                [search.retrieval_id for search in searches] + [a["retrieval_id"] for a in accepted]))
            entry.source_ids = list(dict.fromkeys(c["source_id"] for a in accepted for c in a.get("citations", ())))
            if stage == "evidence_insufficient" and accepted:
                entry.limitation = "本次资料未提供足以说明“" + topic + "”的依据；不能据此认定产品不支持。"
            if aspect == "prerequisite" and entry.feature_id == "saml_sso":
                _sso_prerequisites(entry, results, accepted, observations)
                if entry.stage == "explanation_rejected":
                    entry.limitation = None
            if aspect == "support":
                feature_id = entry.feature_id
                actual = results.catalogs[0][0] if results.catalogs and not results._identity_conflict else None
                plans = {plan.plan_id: plan for plan in actual.plans} if actual else {}
                features = {feature.feature_id: feature.name for feature in actual.features} if actual else {}
                for identity in plan_ids:
                    plan = plans.get(identity)
                    relation = next((f for f in plan.features if f.feature_id == feature_id), None) if plan else None
                    if relation and feature_id in features:
                        entry.support.append(ConsultationSupport(product_id=actual.product.product_id,
                            catalog_version=actual.version, plan_id=plan.plan_id, plan_name=plan.name,
                            feature_id=feature_id, feature_name=features[feature_id], supported=relation.supported,
                            condition_id=relation.condition_id,
                            tool_call_ids=list(dict.fromkeys(call_id for _, call_id in results.catalogs))))
                    else:
                        entry.missing_plan_ids.append(identity)
                if entry.missing_plan_ids or not plan_ids:
                    entry.status = "incomplete"
                    entry.stage = "result_conflict" if results._identity_conflict else "not_queried"
            for record in records:
                data = _record_data(record)
                if (_record_bound(item, record, single_item_id)
                        if record.get("name") == "search_knowledge" else _answer_bound(item, data)):
                    identity = record.get("tool_call_id") or record.get("id")
                    if identity:
                        entry.tool_call_ids.append(identity)
        entry.tool_call_ids = list(dict.fromkeys(entry.tool_call_ids))
        coverage.append(entry)
    known_items = {identity for item in coverage
                   for identity in (item.item_id, *item.source_item_ids)}
    # Current contracts already include every unanswered original question.
    # A rejected candidate identity is an acceptance receipt, not another
    # question. Only genuine legacy collectors need feedback projection.
    for verdict in feedback if questions is None else ():
        if verdict.get("status") not in {"rejected", "blocked", "missing", "ambiguous"} or verdict.get("item_id") in known_items:
            continue
        aspect = verdict.get("aspect")
        if aspect not in {"unit_price", "support", "meaning", "prerequisite", "detail", "pricing"}:
            aspect = "detail"
        topic = verdict.get("topic") or "本轮相关咨询"
        ambiguous = verdict.get("status") == "ambiguous" or verdict.get("issue") in {"ambiguous_scope", "reference_ambiguous", "scope_ambiguous"}
        stage = ("ambiguous_scope" if ambiguous else "dependency_unresolved"
                 if verdict.get("status") == "blocked" else "extraction_incomplete")
        coverage.append(ConsultationResult(item_id=verdict["item_id"], aspect=aspect,
            plan_ids=list(verdict.get("plan_ids", ())), topic=topic,
            status="needs_input" if ambiguous else "incomplete", stage=stage))
        known_items.add(verdict["item_id"])
    status = ("needs_input" if any(item.status == "needs_input" for item in coverage) else
              "incomplete" if not scope_complete or any(item.status != "completed" for item in coverage) else "completed")
    return ConsultationCoverage(scope_complete=scope_complete, status=status,
                                items=coverage, conflicts=list(results.conflicts))
