"""Exact CNY results shared by tools, UI and Quote snapshots."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from app.schemas.catalog import ReadOnlyModel


# Fixed demo input limits, not product capacity or sales policy.
PaidSeats = Annotated[StrictInt, Field(ge=1, le=500)]
Months = Annotated[StrictInt, Field(ge=1, le=12)]
PlanIdentity = Annotated[str, Field(min_length=1)]


def format_cny_minor(amount_minor: int) -> str:
    """Format integer fen for display without converting money to float."""
    if type(amount_minor) is not int or amount_minor < 0:
        raise ValueError("CNY display requires nonnegative integer minor units")
    yuan, fen = divmod(amount_minor, 100)
    return f"{yuan:,}.{fen:02d}"


def subscription_formula(unit_price_minor: int, paid_seats: int, months: int) -> str:
    """Display the adopted operands and exact subscription base price."""
    return (
        f"{format_cny_minor(unit_price_minor)} 元／席／月 × {paid_seats} 席 × "
        f"{months} 月 = {format_cny_minor(unit_price_minor * paid_seats * months)} 元"
        "（订阅基价）"
    )


class CatalogPrice(ReadOnlyModel):
    product_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_name: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    currency: Literal["CNY"]
    billing_unit: Literal["seat_month"]
    minor_units_per_major: Literal[100]
    unit_price_minor: StrictInt = Field(gt=0)
    scope: Literal["read_only_pricing"] = "read_only_pricing"
    fit_status: Literal["not_assessed"] = "not_assessed"


class UnitPriceResult(CatalogPrice):
    """Known catalog rate only; no assumed seats, duration or total."""

    status: Literal["unit_price_only"] = "unit_price_only"


class PricingResult(CatalogPrice):
    """Computed base price, without matching approval or save permission.

    JSON keeps integer fen. Future SQLite snapshots must keep the same integer
    fields and unit metadata; deserialization checks arithmetic consistency,
    but does not authorize client-supplied results as server-owned facts.
    """

    status: Literal["calculated"] = "calculated"
    paid_seats: PaidSeats
    months: Months
    base_price_minor: StrictInt = Field(gt=0)
    formula: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_calculation(self):
        if self.base_price_minor != self.unit_price_minor * self.paid_seats * self.months:
            raise ValueError("Subscription base price does not match its adopted operands")
        if self.formula != subscription_formula(self.unit_price_minor, self.paid_seats, self.months):
            raise ValueError("Subscription formula does not match its adopted operands")
        return self
