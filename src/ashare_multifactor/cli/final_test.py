from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ashare_multifactor.final_test.pipeline import resume_final_test_release
from ashare_multifactor.final_test.preparation import prepare_final_test


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--approval-key-file", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or resume the authorized one-shot final test"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="authorize and prepare symbol scope")
    _add_common_arguments(prepare)
    prepare.add_argument("--opening-token", type=Path, required=True)

    resume = commands.add_parser("resume", help="verify evidence and publish")
    _add_common_arguments(resume)
    resume.add_argument("--security-event-coverage", type=Path, required=True)
    resume.add_argument("--corporate-action-coverage", type=Path, required=True)
    resume.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    approval_key = args.approval_key_file.read_bytes()
    if args.command == "prepare":
        result = prepare_final_test(
            code_root=args.root,
            data_root=args.data_root,
            opening_token_path=args.opening_token,
            approval_key=approval_key,
            attempt_id=args.attempt_id,
        )
        print(result.root)
        return

    result = resume_final_test_release(
        code_root=args.root,
        data_root=args.data_root,
        approval_key=approval_key,
        attempt_id=args.attempt_id,
        security_event_coverage_path=args.security_event_coverage,
        corporate_action_coverage_root=args.corporate_action_coverage,
        run_id=args.run_id,
    )
    print(result.release.root if result.release is not None else result.attempt_root)


if __name__ == "__main__":
    main()
