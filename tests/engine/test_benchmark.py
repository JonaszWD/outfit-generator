import time
from collections.abc import Callable

import pytest

from app.engine.config import EngineConfig
from app.engine.generator import closet_stats, count_outfits, generate_outfits
from app.engine.types import Context, ItemView
from tests.engine.fixtures import big_closet, neutral_closet

CONFIG = EngineConfig()
BUDGET_SECONDS = 1.0


def timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


@pytest.mark.slow
@pytest.mark.parametrize(
    ("label", "closet"),
    [("varied", big_closet(150)), ("worst case: all neutral and compatible", neutral_closet(150))],
)
def test_150_items_are_counted_in_under_a_second(label: str, closet: list[ItemView]) -> None:
    assert len(closet) == 150
    result, count_seconds = timed(lambda: count_outfits(closet, CONFIG))
    stats, stats_seconds = timed(lambda: closet_stats(closet, CONFIG))
    _, generate_seconds = timed(lambda: generate_outfits(closet, Context(), CONFIG, 3, rng_seed=1))
    print(
        f"\n[{label}] 150 items: {result.count:,} outfits | count {count_seconds:.3f}s | "
        f"stats {stats_seconds:.3f}s | generate {generate_seconds:.3f}s"
    )
    assert stats.total == result.count
    assert count_seconds < BUDGET_SECONDS
    assert generate_seconds < BUDGET_SECONDS
