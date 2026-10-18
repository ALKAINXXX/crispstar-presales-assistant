"""Immutable product records; product facts live in catalog.json."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ReadOnlyModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        validate_default=True,
        revalidate_instances="always",
    )

    @model_validator(mode="after")
    def reject_blank_text(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{name} must not be blank")
        return self


class Product(ReadOnlyModel):
    product_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Feature(ReadOnlyModel):
    feature_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class FeatureCondition(ReadOnlyModel):
    condition_id: str = Field(min_length=1)
    feature_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class PlanFeature(ReadOnlyModel):
    feature_id: str = Field(min_length=1)
    supported: bool
    condition_id: str | None = None


class Plan(ReadOnlyModel):
    plan_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    rank: StrictInt = Field(ge=1)
    unit_price_minor: StrictInt = Field(gt=0)
    features: tuple[PlanFeature, ...]


class Catalog(ReadOnlyModel):
    version: str = Field(min_length=1)
    product: Product
    currency: Literal["CNY"] = "CNY"
    billing_unit: Literal["seat_month"] = "seat_month"
    minor_units_per_major: Literal[100] = 100
    features: tuple[Feature, ...]
    conditions: tuple[FeatureCondition, ...]
    plans: tuple[Plan, ...]
