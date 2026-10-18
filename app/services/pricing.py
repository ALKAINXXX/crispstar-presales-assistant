"""Deterministic, read-only pricing from the validated Catalog.

No model, Match, network, database, arbitrary price or confirmation input.
Missing/invalid arguments raise ValidationError before catalog access; an
unknown plan raises ValueError. Catalog load failures propagate as failures,
never as fallback prices. Successful calculation says nothing about fit.
"""

from pydantic import ConfigDict, validate_call

from app.schemas.pricing import (
    Months,
    PaidSeats,
    PlanIdentity,
    PricingResult,
    UnitPriceResult,
    subscription_formula,
)
from app.services import catalog as catalog_service


@validate_call(config=ConfigDict(strict=True))
def get_unit_price(plan_id: PlanIdentity) -> UnitPriceResult:
    """Query a catalog rate even when seats, months or requirements are absent."""
    catalog = catalog_service.get_catalog()
    plan = next((plan for plan in catalog.plans if plan.plan_id == plan_id), None)
    if plan is None:
        raise ValueError(f"Unknown catalog plan: {plan_id!r}")
    return UnitPriceResult(
        product_id=catalog.product.product_id,
        plan_id=plan.plan_id,
        plan_name=plan.name,
        catalog_version=catalog.version,
        currency=catalog.currency,
        billing_unit=catalog.billing_unit,
        minor_units_per_major=catalog.minor_units_per_major,
        unit_price_minor=plan.unit_price_minor,
    )


@validate_call(config=ConfigDict(strict=True))
def calculate_subscription_price(
    plan_id: PlanIdentity, paid_seats: PaidSeats, months: Months
) -> PricingResult:
    """Require explicit legal inputs; do not infer, default, truncate or clamp."""
    price = get_unit_price(plan_id)
    return PricingResult(
        **price.model_dump(exclude={"status"}),
        paid_seats=paid_seats,
        months=months,
        base_price_minor=price.unit_price_minor * paid_seats * months,
        formula=subscription_formula(price.unit_price_minor, paid_seats, months),
    )
