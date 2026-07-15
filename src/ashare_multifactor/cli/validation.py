from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.validation.pipeline import (
    execute_full_validation_run,
    execute_validation_reproducibility,
    run_validation_release,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "reproduce", "stage", "publish"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.command == "run":
        result = execute_full_validation_run(args.root, run_id=args.run_id)
    elif args.command == "reproduce":
        result = execute_validation_reproducibility(
            args.root,
            run_id_prefix=args.run_id,
        )
    else:
        result = run_validation_release(
            args.root,
            publish=args.command == "publish",
            run_id=args.run_id,
        )
    print(result)


if __name__ == "__main__":
    main()
