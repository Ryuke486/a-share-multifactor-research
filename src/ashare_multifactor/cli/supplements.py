"""CLI for the descriptive v1.0 benchmark and long/short-leg supplements."""

from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.supplements.index_benchmark import CACHE_PATH, download_index_cache
from ashare_multifactor.supplements.pipeline import run_supplements
from ashare_multifactor.supplements.robustness_subperiod import run_robustness_subperiod


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compare frozen strategies with zero-cost universe benchmarks (2005-2021)"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/research_protocol.yaml"))
    parser.add_argument(
        "--robustness-subperiod",
        action="store_true",
        help="instead rerun the Stage 8 experiments and report them on 2017-2021 only "
        "(about 25 minutes)",
    )
    parser.add_argument(
        "--download-index-benchmarks",
        action="store_true",
        help="instead fetch CSI 300 and CSI 500 daily closes for 2006-2021 from BaoStock once "
        "(two sequential queries on one connection)",
    )
    args = parser.parse_args(argv)
    if args.download_index_benchmarks:
        metadata = download_index_cache(CACHE_PATH)
        print(f"cached {metadata['rows']} index rows at {CACHE_PATH}")
        return
    if args.robustness_subperiod:
        output = run_robustness_subperiod()
        print(f"wrote the validation-slice robustness table to {output}")
        return
    output = run_supplements(args.config)
    print(f"wrote supplements to {output}")


if __name__ == "__main__":
    main()
