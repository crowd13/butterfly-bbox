"""Count whole assignments, including partial pages; currency uses Decimal."""

from decimal import Decimal
from math import ceil


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Money must be finite and nonnegative")
    return result


def cost(price, count, commission):
    return (money(price) * count * (1 + money(commission))).quantize(Decimal("0.01"))


def plan(config, count=None):
    count = config["dataset_size"] if count is None else count
    pilot_count = min(config["pilot_size"], count)
    pilot_pages = ceil(pilot_count / config["real_per_page"])
    remaining_pages = ceil((count - pilot_count) / config["real_per_page"])
    main_pages = pilot_pages + remaining_pages
    repeat_pages = ceil(config["repeat_limit"] / config["real_per_page"])
    commission = config["commission"]
    exam = cost(config["exam_price"], config["exam_attempts"], commission)
    main = cost(config["page_price"], main_pages, commission)
    repeat = cost(config["page_price"], repeat_pages, commission)
    total = exam + main + repeat
    protected = money(config["review_reserved"]) + money(config["free_reserve"])
    if total + protected > money(config["total_budget"]):
        raise ValueError("Plan exceeds bbox allocation; preserve review and free reserves")
    return {
        "pilot_pages": pilot_pages,
        "remaining_pages": remaining_pages,
        "main_pages": main_pages,
        "repeat_pages": repeat_pages,
        "exam": str(exam),
        "main": str(main),
        "repeat": str(repeat),
        "bbox_limit": str(total),
        "review_reserved": config["review_reserved"],
        "free_reserve": config["free_reserve"],
    }


def committed(batches, config):
    return sum(
        (cost(b["price"], len(b["suites"]) * b["overlap"], config["commission"]) for b in batches),
        Decimal(0),
    )


def check_commitment(batches, config):
    allocation = (
        money(config["total_budget"])
        - money(config["review_reserved"])
        - money(config["free_reserve"])
    )
    if committed(batches, config) > allocation:
        raise ValueError("Whole-page commitments exceed bbox allocation")
