"""Load the one fixed product catalog and reject inconsistent business facts."""

from functools import lru_cache
from pathlib import Path

from app.schemas.catalog import Catalog


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "catalog.json"


def validate_catalog(catalog: Catalog) -> Catalog:
    """Validate finite catalog relationships without repairing or reordering data."""
    if not isinstance(catalog, Catalog):
        raise ValueError("Catalog validation requires a Catalog object")
    catalog = Catalog.model_validate(catalog)
    if not 2 <= len(catalog.plans) <= 3:
        raise ValueError("Catalog must contain 2 to 3 ordered plans")
    if not 6 <= len(catalog.features) <= 10:
        raise ValueError("Catalog must contain 6 to 10 features")
    if len(catalog.conditions) != 1:
        raise ValueError("Catalog must contain exactly one feature condition")

    identities = [catalog.product.product_id]
    identities.extend(feature.feature_id for feature in catalog.features)
    identities.extend(condition.condition_id for condition in catalog.conditions)
    identities.extend(plan.plan_id for plan in catalog.plans)
    if any(not identity.strip() or identity != identity.strip() for identity in identities):
        raise ValueError("Catalog identities must be nonblank and have no surrounding whitespace")
    if len(identities) != len(set(identities)):
        raise ValueError("Product, plan, feature and condition identities must be unique")

    feature_ids = {feature.feature_id for feature in catalog.features}
    condition = catalog.conditions[0]
    if condition.feature_id not in feature_ids:
        raise ValueError("Feature condition references an unknown feature")
    if condition.feature_id != "saml_sso":
        raise ValueError("The only feature condition must apply to saml_sso")

    ranks = [plan.rank for plan in catalog.plans]
    if ranks != list(range(1, len(catalog.plans) + 1)):
        raise ValueError("Plans must appear in continuous rank order starting at 1")

    previous_price = 0
    previous_supported: set[str] | None = None
    condition_used = False
    for plan in catalog.plans:
        if plan.unit_price_minor <= previous_price:
            raise ValueError("Plan prices must be positive and strictly increasing")
        previous_price = plan.unit_price_minor

        plan_feature_ids = [entry.feature_id for entry in plan.features]
        if len(plan_feature_ids) != len(set(plan_feature_ids)):
            raise ValueError(f"Plan {plan.plan_id} contains duplicate feature records")
        if set(plan_feature_ids) != feature_ids:
            raise ValueError(f"Plan {plan.plan_id} must cover every catalog feature exactly once")

        supported = {entry.feature_id for entry in plan.features if entry.supported}
        if previous_supported is not None and not previous_supported < supported:
            raise ValueError("Each higher plan must retain lower-plan capabilities and add a capability")
        previous_supported = supported

        for entry in plan.features:
            if entry.condition_id is not None:
                if entry.condition_id != condition.condition_id:
                    raise ValueError(f"Plan {plan.plan_id} references an unknown condition")
                if entry.feature_id != condition.feature_id:
                    raise ValueError(f"Plan {plan.plan_id} applies the condition to the wrong feature")
                if not entry.supported:
                    raise ValueError("Unsupported features must not carry a condition")
                condition_used = True
            if entry.supported and entry.feature_id == condition.feature_id:
                if entry.condition_id != condition.condition_id:
                    raise ValueError("Supported SAML SSO must reference its required condition")

    if not condition_used:
        raise ValueError("The catalog condition must be used by a supported feature")
    return catalog


def load_catalog(path: Path | str) -> Catalog:
    """Read UTF-8 JSON, apply strict schema checks, then validate relationships."""
    catalog_path = Path(path)
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read catalog file {catalog_path}: {exc}") from exc
    try:
        catalog = Catalog.model_validate_json(raw)
        return validate_catalog(catalog)
    except ValueError as exc:
        raise ValueError(f"Invalid catalog file {catalog_path}: {exc}") from exc


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    """Load and retain the validated, immutable catalog for this process."""
    return load_catalog(DEFAULT_CATALOG_PATH)
