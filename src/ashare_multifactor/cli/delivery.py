from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ashare_multifactor.audit.delivery import (
    DEFAULT_DELIVERY_MANIFEST,
    DEFAULT_KEY_RESULTS,
    run_delivery_checks,
    write_key_results,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check the v1.0 delivery for links, numbers and hashes"
    )
    parser.add_argument("command", choices=("check", "record-results"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=DEFAULT_DELIVERY_MANIFEST)
    parser.add_argument("--results", type=Path, default=DEFAULT_KEY_RESULTS)
    parser.add_argument(
        "--verify-sources",
        action="store_true",
        help="also re-read the recorded numbers from the local releases",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--generated-on")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "record-results":
        output = args.results if args.results.is_absolute() else root / args.results
        if args.json_out is not None:
            output = args.json_out if args.json_out.is_absolute() else root / args.json_out
        written = write_key_results(root, output, generated_on=args.generated_on)
        print(f"recorded documented results in {written}")
        return
    report = run_delivery_checks(
        root,
        manifest_path=args.manifest,
        results_path=args.results,
        verify_sources=args.verify_sources,
    )
    print(report.render())
    if args.json_out is not None:
        target = args.json_out if args.json_out.is_absolute() else root / args.json_out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if not report.ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
