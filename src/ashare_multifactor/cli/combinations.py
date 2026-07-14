from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.research.combination_pipeline import run_combination_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stage-five factor combinations")
    parser.add_argument("--config", type=Path, default=Path("configs/research_protocol.yaml"))
    parser.add_argument("--upstream-root", type=Path)
    args = parser.parse_args()
    run_combination_pipeline(args.config, upstream_root=args.upstream_root)


if __name__ == "__main__":
    main()
