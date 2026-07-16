from __future__ import annotations

import argparse
from pathlib import Path

from ashare_multifactor.final_test.pipeline import run_final_test_release


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authorized one-shot final test")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--opening-token", type=Path, required=True)
    parser.add_argument("--approval-key-file", type=Path, required=True)
    parser.add_argument("--attempt-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    result = run_final_test_release(
        code_root=args.root,
        data_root=args.root,
        opening_token_path=args.opening_token,
        approval_key=args.approval_key_file.read_bytes(),
        attempt_id=args.attempt_id,
        run_id=args.run_id,
    )
    print(result.release.root if result.release is not None else result.attempt_root)


if __name__ == "__main__":
    main()
