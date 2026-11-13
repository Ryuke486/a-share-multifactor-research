from __future__ import annotations

import numpy as np
import pytest

from ashare_multifactor.supplements.bootstrap import (
    block_bootstrap_intervals,
    circular_block_indices,
)


def _returns(seed: int, size: int = 1260, mean: float = 0.0004) -> np.ndarray:
    return np.random.default_rng(seed).normal(mean, 0.012, size)


def test_blocks_are_contiguous_on_the_circle_and_cover_exactly_the_sample_length() -> None:
    indices = circular_block_indices(10, block=4, rng=np.random.default_rng(1))

    assert indices.shape == (10,)
    assert indices.min() >= 0 and indices.max() < 10
    for start in (0, 4):
        chunk = indices[start : start + 4]
        assert np.array_equal(chunk, (chunk[0] + np.arange(4)) % 10)
    assert indices[9] == (indices[8] + 1) % 10


def test_intervals_are_reproducible_and_bracket_the_point_estimates() -> None:
    strategy, benchmark = _returns(1), _returns(2)

    first = block_bootstrap_intervals(
        strategy, benchmark, years=5.0, block=21, replications=500, seed=11
    )
    second = block_bootstrap_intervals(
        strategy, benchmark, years=5.0, block=21, replications=500, seed=11
    )
    other_seed = block_bootstrap_intervals(
        strategy, benchmark, years=5.0, block=21, replications=500, seed=12
    )

    assert first == second
    assert first != other_seed
    growth = float(np.prod(1.0 + strategy))
    relative = growth / float(np.prod(1.0 + benchmark))
    assert first["annual_return"] == pytest.approx(growth ** (1 / 5.0) - 1.0)
    assert first["annual_relative_return"] == pytest.approx(relative ** (1 / 5.0) - 1.0)
    for name in ("annual_return", "annual_relative_return", "sharpe"):
        assert first[f"{name}_low"] < first[name] < first[f"{name}_high"]
    assert 0.0 <= first["relative_non_positive_share"] <= 1.0


def test_pairs_are_resampled_together_so_an_identical_benchmark_has_no_spread() -> None:
    strategy = _returns(3)

    result = block_bootstrap_intervals(
        strategy, strategy.copy(), years=5.0, block=21, replications=200, seed=5
    )

    assert result["annual_relative_return_low"] == pytest.approx(0.0, abs=1e-12)
    assert result["annual_relative_return_high"] == pytest.approx(0.0, abs=1e-12)
    assert result["relative_non_positive_share"] == 1.0


def test_a_clearly_positive_edge_excludes_zero_and_a_noisy_one_does_not() -> None:
    benchmark = _returns(4, size=2520)
    edge = benchmark + 0.0008
    noise = benchmark + np.random.default_rng(9).normal(0.0, 0.01, benchmark.size)

    strong = block_bootstrap_intervals(
        edge, benchmark, years=10.0, block=21, replications=400, seed=3
    )
    weak = block_bootstrap_intervals(
        noise, benchmark, years=10.0, block=21, replications=400, seed=3
    )

    assert strong["annual_relative_return_low"] > 0.0
    assert strong["relative_non_positive_share"] == 0.0
    assert weak["annual_relative_return_low"] < 0.0 < weak["annual_relative_return_high"]


def test_invalid_inputs_are_rejected() -> None:
    returns = _returns(1, size=50)

    with pytest.raises(ValueError, match="same length"):
        block_bootstrap_intervals(
            returns, returns[:-1], years=1.0, block=5, replications=10, seed=1
        )
    with pytest.raises(ValueError, match="block"):
        block_bootstrap_intervals(returns, returns, years=1.0, block=0, replications=10, seed=1)
    with pytest.raises(ValueError, match="finite"):
        bad = returns.copy()
        bad[3] = np.nan
        block_bootstrap_intervals(bad, returns, years=1.0, block=5, replications=10, seed=1)
    with pytest.raises(ValueError, match="-100%"):
        lost = returns.copy()
        lost[3] = -1.0
        block_bootstrap_intervals(lost, returns, years=1.0, block=5, replications=10, seed=1)
