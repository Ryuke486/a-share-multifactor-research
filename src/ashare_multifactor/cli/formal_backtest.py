from __future__ import annotations

import argparse
import json
from pathlib import Path

from ashare_multifactor.research.formal_backtest_pipeline import (
    audit_formal_backtest,
    run_formal_pipeline,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("audit", "all"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.command == "audit":
        print(json.dumps(audit_formal_backtest(args.root), ensure_ascii=False, indent=2))
        return
    result = run_formal_pipeline(args.root, publish=args.publish)
    print(result)


if __name__ == "__main__":
    main()

