"""Offline examples of real pricing services, without chat or database access.

Run from the repository root: python -m scripts.check_pricing
"""

import sys

from pydantic import ValidationError

from app.schemas.pricing import PricingResult, format_cny_minor
from app.services.pricing import calculate_subscription_price, get_unit_price


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    unit = get_unit_price("team")
    if unit.unit_price_minor != 8000 or unit.status != "unit_price_only":
        raise RuntimeError("Team 单价与当前冻结目录不一致")
    print(f"Team 单价：{format_cny_minor(unit.unit_price_minor)} 元／席／月；没有假设总价")
    print(f"币种：{unit.currency}；100 分 = 1 元；资料版本：{unit.catalog_version}")

    for plan_id, seats, months, expected_minor in (
        ("business", 80, 12, 11_520_000),
        ("business", 50, 12, 7_200_000),
        ("basic", 1, 1, 4_000),
        ("team", 500, 12, 48_000_000),
    ):
        result = calculate_subscription_price(plan_id, seats, months)
        if result.base_price_minor != expected_minor:
            raise RuntimeError("实际订阅基价不符合冻结目录的复算结果")
        if PricingResult.model_validate_json(result.model_dump_json()) != result:
            raise RuntimeError("金额或单位在 JSON 往返中发生变化")
        print(f"{result.plan_name}：{result.formula}")

    print("\n非法输入与缺参：")
    for label, arguments in (
        ("零席位", {"plan_id": "business", "paid_seats": 0, "months": 12}),
        ("布尔席位", {"plan_id": "business", "paid_seats": True, "months": 12}),
        ("小数席位", {"plan_id": "business", "paid_seats": 80.5, "months": 12}),
        ("501 席", {"plan_id": "business", "paid_seats": 501, "months": 12}),
        ("13 月", {"plan_id": "business", "paid_seats": 80, "months": 13}),
        ("缺月份", {"plan_id": "business", "paid_seats": 80}),
        ("未知套餐", {"plan_id": "unknown", "paid_seats": 80, "months": 12}),
        ("外部单价", {"plan_id": "business", "paid_seats": 80, "months": 12,
                    "unit_price_minor": 1}),
    ):
        try:
            calculate_subscription_price(**arguments)
        except (ValidationError, ValueError):
            print(f"  {label}：明确拒绝，未输出总价")
        else:
            raise RuntimeError(f"{label} 被错误接受")

    print("\nJSON 往返通过；计算结果始终为 read_only_pricing / not_assessed。")
    print("金额由真实目录和代码产生；未评估适配，不产生确认或保存许可。")
    print("本次计价服务自检通过。")


if __name__ == "__main__":
    main()
