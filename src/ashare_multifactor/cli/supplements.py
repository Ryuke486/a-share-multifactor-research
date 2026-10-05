"""CLI for the descriptive v1.0 benchmark and long/short-leg supplements."""

from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.supplements.pipeline import run_supplements


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compare frozen strategies with zero-cost universe benchmarks (2005-2021)"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/research_protocol.yaml"))
    args = parser.parse_args(argv)
    output = run_supplements(args.config)
    print(f"wrote supplements to {output}")


if __name__ == "__main__":
    main()
