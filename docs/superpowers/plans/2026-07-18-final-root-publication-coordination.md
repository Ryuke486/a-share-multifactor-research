# Final-root Publication Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the claimed final-test root the only writable namespace through outcome, and prevent resolvers from observing unverified CURRENT or data-claim transitions.

**Architecture:** Extend the existing `FinalRootBinding` into every execution/recovery writer by constructing child transactions from duplicated caller-held descriptors before any mkdir/open. Add descriptor-relative shared/exclusive coordination locks: writers hold exclusive locks through installation and post-install verification; readers hold shared locks through complete parse and verification.

**Tech Stack:** Python 3.14, POSIX directory FDs, `fcntl.flock`, pytest, ruff.

## Global Constraints

- Do not read real 2022–2025 final-test data, execute prepare/resume, access approval keys, or write `Data/`/`processed/`.
- Preserve sealed hashes, research rules, market scope, and fail-closed crash recovery.
- Every production change follows a witnessed red test, focused green test, full pytest, ruff, and `git diff --check`.

---

### Task 1: Root-bound execution and recovery writers

**Files:**
- Modify: `src/ashare_multifactor/final_test/final_root_binding.py`
- Modify: `src/ashare_multifactor/final_test/secure_attempt_staging.py`
- Modify: `src/ashare_multifactor/final_test/execution_sources.py`
- Modify: `src/ashare_multifactor/final_test/interrupted_recovery.py`
- Modify: `src/ashare_multifactor/final_test/publication_transaction.py`
- Modify: `src/ashare_multifactor/final_test/pipeline.py`
- Test: `tests/test_final_test_pipeline.py`

**Interfaces:**
- Consumes: `FinalRootBinding.final_fd`, `.attempts_fd`, and recorded root identities.
- Produces: `FinalPublicationTransaction.from_binding(...)` and root-bound optional parameters for attempt staging, execution inputs, and interrupted recovery.

- [x] Write parameterized failing tests that replace `final_test`, `processed`, or `attempts` immediately before each writer/recovery branch and assert the replacement tree gains no file, CURRENT, or success outcome.
- [x] Run each test alone and confirm the old path-reopen implementation writes or creates in the replacement tree.
- [x] Add descriptor-duplication/from-binding constructors and route every listed pipeline call through them before its first filesystem mutation.
- [x] Run pipeline/recovery focused tests and confirm all replacement tests pass.

### Task 2: Coordinated CURRENT and claim publication

**Files:**
- Modify: `src/ashare_multifactor/audit/publication.py`
- Modify: `src/ashare_multifactor/final_test/data_publication.py`
- Modify: `src/ashare_multifactor/final_test/data_extension.py`
- Test: `tests/test_audit_publication.py`
- Test: `tests/test_final_test_data.py`

**Interfaces:**
- Produces: root-relative shared/exclusive publication locks held across complete write+post-verify and read+parse+verify operations.

- [x] Write deterministic threaded failing tests that pause a writer after temporary-name replacement but before post-install verification, start a resolver, and assert it cannot return forged CURRENT/claim data.
- [x] Run each test alone and confirm the existing resolver returns during the paused writer window.
- [x] Implement reusable descriptor-relative flock contexts; take exclusive locks in CURRENT/claim writers and shared locks in every corresponding resolver/read path.
- [x] Preserve recovery behavior by keeping locks outside immutable transition validation and releasing them on every exception.
- [x] Run publication/data focused tests and confirm the concurrency tests pass without deadlock.

### Task 3: Final verification and audit record

**Files:**
- Modify: `.superpowers/sdd/execution-binding-remediation-report.md` (ignored local audit record)

- [x] Run all focused final-test publication, preparation, data, recovery, and pipeline tests.
- [x] Run full pytest, full ruff, and `git diff --check`.
- [x] Append exact test evidence and the local commit to the remediation report.
- [x] Commit only the reviewed remediation files and confirm tracked worktree cleanliness.

### Task 4: Bound resolver closure after independent final review

**Files:**
- Modify: `src/ashare_multifactor/audit/publication.py`
- Modify: `src/ashare_multifactor/final_test/data_inventory.py`
- Modify: `src/ashare_multifactor/final_test/data_publication.py`
- Modify: `src/ashare_multifactor/final_test/pipeline.py`
- Modify: `src/ashare_multifactor/final_test/secure_attempt_staging.py`
- Test: `tests/test_final_test_data.py`
- Test: `tests/test_final_test_pipeline.py`

**Interfaces:**
- Produces: `opened_verified_current_at(root, root_parent_fd, root_fd)` and `resolve_final_test_data_panel_at(final_root, final_fd)`.
- Consumes: the existing `FinalRootBinding.final_fd`, `.attempts_fd`, `open_existing_attempt_directories(..., root_binding=...)`, and `resolve_bound_execution_inputs_at(...)`.

- [x] Add normal/prepared/orphan/CURRENT recovery tests that replace canonical `final_test` with B, remove B lock files, and record both A/B inventories.
- [x] Confirm the old Path resolvers create a lock or other entry in B and never silently write B-derived failure/success into A.
- [x] Route every post-claim CURRENT, data-panel, attempt-directory, and execution-input read through caller-held descriptors.
- [x] Run the four-mode focused tests, all final-test recovery/publication tests, full pytest, ruff, and diff checks.
- [x] Append exact evidence to the ignored remediation report and create one local commit without resealing.

### Task 5: Close post-snapshot ABA read paths

**Files:**
- Modify: `src/ashare_multifactor/final_test/backtest.py`
- Modify: `src/ashare_multifactor/final_test/data_extension.py`
- Modify: `src/ashare_multifactor/final_test/pipeline.py`
- Modify: `src/ashare_multifactor/final_test/release_outputs.py`
- Modify: `src/ashare_multifactor/final_test/signals.py`
- Test: `tests/test_final_test_pipeline.py`

**Interfaces:**
- Consumes: `BoundExecutionInputs`, `FrozenPanelSnapshot`, `FinalRootBinding.attempts_fd`, and held attempt staging descriptors.
- Produces: bound-only optional parameters on signal/backtest/report helpers and byte-derived lineage records.

- [x] Add a normal-flow ABA test that swaps A to B strictly after the panel snapshot, mutates B execution inputs, staged files, and registry outcomes, restores A before package freeze/publication, and rejects any B marker in A datasets, artifacts, lineage, or report.
- [x] Add prepared/orphan/CURRENT ABA recovery coverage around the bound input/package read window and assert B remains unchanged after the deliberate fixture mutation.
- [x] Run the new tests against `bc144a9` and confirm the normal test fails because B-derived bytes enter A.
- [x] Pass the already-bound execution inputs and registry FD through signals/backtest/report; construct lineage identities from immutable bytes and held descriptors rather than lexical paths.
- [x] Audit every normal/recovery read after `FinalRootBinding.open` and retain lexical paths only as labels or for unrelated frozen upstream releases.
- [x] Run focused tests, all final-test publication/recovery tests, full pytest, ruff, and `git diff --check`; append evidence and create one local commit without resealing.

### Task 6: Anchor the execution-source generation chain

**Files:**
- Modify: `src/ashare_multifactor/final_test/coverage_snapshot.py`
- Modify: `src/ashare_multifactor/final_test/execution_sources.py`
- Modify: `src/ashare_multifactor/final_test/pipeline.py`
- Test: `tests/test_final_test_recovery_hardening.py`

**Interfaces:**
- Consumes: `FinalRootBinding.final_fd`, the attempt-bound coverage snapshot bytes, and `resolve_final_test_data_panel_at(...)`.
- Produces: descriptor-anchored source reuse, coverage freeze, and execution-input publication without reopening canonical `final_root`.

**Binding-context audit:**
- `snapshot_execution_coverages`: lexical destination existence/symlink checks and post-publication `_verify_snapshot(Path)`.
- `freeze_bound_coverage_snapshot`: lexical snapshot resolve/open.
- `build_final_execution_inputs`: lexical source/attempt root resolve and existence checks.
- `generate_final_execution_sources`: lexical data-panel resolver despite a supplied final FD.
- `_verify_reusable_source_root`: lexical manifest/frame/tree reads plus a second lexical data-panel resolver.
- `_publish_execution_sources_at` and `_publish_execution_inputs_at`: already descriptor-relative; retain them as the only bound write path.

- [x] Add a real `build_final_execution_inputs` ABA test that swaps A to a self-consistent B after the initial binding assertion, removes B's data-claim lock, injects B data/source markers, restores A only after the real FD publication, and proves the old chain touches B and imports B records into A.
- [x] Make coverage snapshot existing/new verification and freeze descriptor-relative when a final FD is supplied; carry immutable snapshot bytes into later consumers.
- [x] Split the bound execution-source path from the public unbound compatibility path; derive the complete expected source tree from immutable coverage bytes and validate/reuse it through the held final FD.
- [x] Resolve panel identity, source existence, attempt-input existence, coverage snapshot bytes, reusable source bytes, and final input publication only through held descriptors.
- [x] Search the complete bound executing path again for lexical final-root reads/writes and retain paths only as labels or for external frozen code/coverage releases.
- [x] Run focused tests, final-test recovery/publication suites, full pytest, ruff, and `git diff --check`; update evidence and amend the single local commit without resealing.
