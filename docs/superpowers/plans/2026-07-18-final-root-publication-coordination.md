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
