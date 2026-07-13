"""CLI for the isolated factor-research pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.research.factor_pipeline import STAGES, run_factor_pipeline


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    args = parser.parse_args(argv)
    run_factor_pipeline(args.config, args.stage)


if __name__ == "__main__":
    main()
