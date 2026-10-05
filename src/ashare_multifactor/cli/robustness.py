from __future__ import annotations

import argparse
import json
from pathlib import Path

from ashare_multifactor.robustness.pipeline import (
    execute_robustness_reproducibility,
    execute_robustness_run,
    publish_robustness_release,
)


def main() -> None:
    # The final-test evidence rehearsal and the evidence-workflow successor
    # publication were deferred v2.0 operations; they are archived with the
    # final-test code under the Git tag archive/stage9-final-test.
    parser = argparse.ArgumentParser(description="Run sealed Stage-8 robustness analysis")
    parser.add_argument("command", choices=("run", "reproduce", "publish"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.command == "run":
        result = execute_robustness_run(args.root, run_id=args.run_id)
        print(result)
    elif args.command == "reproduce":
        result = execute_robustness_reproducibility(
            args.root, run_id_prefix=args.run_id
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if not args.run_id:
            parser.error("publish requires --run-id")
        result = publish_robustness_release(args.root, run_id=args.run_id)
        print(result.root)


if __name__ == "__main__":
    main()
