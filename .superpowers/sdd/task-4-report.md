# Task 4 Report

## Status

DONE. The initial real-data blocker was resolved through an explicitly approved validation-policy change, and the audited smoke dataset was published successfully.

## Implemented before the blocker

- Added audited yearly Parquet construction consuming `ResearchConfig`.
- Reads and validates one daily pair at a time, flushes one year at a time, and sorts by `(date, symbol)`.
- Builds in a staging directory; Parquet and JSON files use temporary files before replacement.
- Writes stable machine-readable `manifest.json` and `quality_issues.json`.
- Restricts the API to `smoke_data`; CLI requires explicit `--mode smoke`.
- Added two-day, quality-error cleanup, and smoke-boundary tests.

## Test evidence

- RED: `test_builds_two_days_into_audited_year_partition` failed because `ashare_multifactor.data.build` did not exist.
- GREEN: `PYTHONPATH=src ../../.venv/bin/pytest tests/test_build.py -v` -> 3 passed.
- Ruff: build module and build tests passed.

## Real smoke evidence

- Requested range: 2012-01-01 through 2015-12-31.
- Discovered 970 paired trading days: 2012=243, 2013=238, 2014=245, 2015=244.
- Source CSV size: 1,379,069,081 bytes (1.284 GiB).
- Blocking issue: date `2015-09-14`, symbol `832317`, code `missing_price`, count `1`.
- Missing fields observed: `prev_close_raw` and `prev_close_adj`.
- Exception: `ValueError: data quality errors: missing_price:1`.
- No `processed/daily_panel` or staging directory remained after failure.

## Concern / decision needed

The approved validator classifies any missing adjusted key price as an error. Task 4 therefore cannot satisfy the real-build acceptance checklist without an explicit project-level decision about this source record. Per the plan, validation was not weakened and the row was not dropped or imputed.

## Approved validation-policy change and completion

The user explicitly approved separating previous-close availability from same-day OHLC validity.

- RED: both `prev_close_raw` and `prev_close_adj` null cases produced the old `missing_price` error.
- GREEN: they now produce stable `missing_prev_close` warnings and pass `raise_on_errors`.
- Same-day raw/adjusted OHLC null tests still produce blocking `missing_price` errors.
- Missing previous closes remain null; no imputation, row removal, or future `list_date` filter was introduced.

The real smoke build was rerun from scratch successfully:

- Manifest: 970 file pairs, 2,272,248 rows, 2012-01-04 through 2015-12-31, years 2012-2015.
- Partition rows: 2012=566,386; 2013=564,815; 2014=570,677; 2015=570,370.
- Output size: 168,820,027 bytes.
- Warning records: 770; `missing_prev_close`=2 records/2 rows, `missing_valuation`=768 records/4,905 affected-row counts.
- Previous-close warning dates: 2015-09-14 and 2015-12-15.
- No errors, staging directories, or `.tmp` files remained.
- `Data/` and `processed/` remained Git-ignored.

Verification after the policy change:

- Validation/build专项: 23 passed.
- Full suite: 42 passed.
- Ruff: all checks passed.
- `git diff --check`: passed.

## Transactional publish review fix

An Important review finding identified that deleting an existing target before publishing staging could lose the last successful dataset if `os.replace(staging, target)` failed.

- RED: a simulated staging publish failure removed the existing target marker.
- GREEN: publishing now atomically renames the existing target to a unique sibling backup, promotes staging, restores the backup on failure, and deletes the backup only after success.
- Tests cover both successful replacement and failed-publish rollback.
- A handled failure leaves the old target intact with no staging or backup residue.
- A backup is never pre-emptively deleted at startup; if rollback itself is interrupted, its unique sibling path remains explicitly recoverable.
- Build/validation专项 after the fix: 25 passed; full suite: 44 passed; Ruff and diff check passed.
- Existing real artifacts were checked read-only: all four partitions remain readable with 2,272,248 total manifest rows, and no staging or temporary paths exist.

## Final consistency cleanup

- Corrected the plan to record that symbol 832317 lacked both `prev_close_raw` and `prev_close_adj` on 2015-09-14.
- Before the ignore-rule change, `git check-ignore --no-index` showed that non-anchored `Data/` matched both the root raw-data path and `src/ashare_multifactor/data/build.py` on the case-insensitive filesystem.
- Anchored the rule as `/Data/`: root raw data remains ignored, while the source `data` package is no longer ignored.
- Post-change evidence: root `Data/每天一个文件/不复权` matched `/Data/`; `src/ashare_multifactor/data/build.py` returned not ignored.
- Final verification: full pytest 44 passed, Ruff passed, and `git diff --check` passed.
