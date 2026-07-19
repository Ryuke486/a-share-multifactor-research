# Stage 9 Official Evidence Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive the current non-authoritative prepared attempt, add a scope-bound official-evidence collector, and re-seal Stage 7/8 without weakening any final-test gate.

**Architecture:** The collector is split into a transport client, immutable CNInfo query-package publisher, and official-document catalog/download/review workspace. Existing coverage validators remain the sole authority for execution inputs; the new code only produces their required source artifacts and refuses to infer economic facts from titles or incomplete documents.

**Tech Stack:** Python 3.14, standard-library `urllib`, Polars, PyYAML, pytest, Ruff, existing descriptor-bound filesystem helpers.

## Global Constraints

- Final-test period is exactly `2022-01-01` through `2025-12-31`; no 2026 data may be read.
- Supported markets are exactly `sh` and `sz`; every external symbol must be validated with `market_for_symbol`.
- During implementation and re-seal, use only fixtures and at most a 2021-or-earlier connectivity probe; do not acquire 2022–2025 evidence.
- Current attempt `stage9-final-20260719-ce7c729` is a non-authoritative infrastructure failure: preserve its token, ledger, panel, scope, state and data claim in an incident archive; never delete or overwrite them.
- Query coverage proves only that the official endpoint was queried. Nonzero company actions and security events still require official source bytes, evidence IDs and existing field-level coverage validation.
- Use a single-thread default with a fixed minimum request interval. No credential, cookie, token payload, approval key or authorization header may be persisted.
- Raw network data, `processed/`, `artifacts/`, temporary worktrees and keys stay outside Git. Stage only named source/test/document files.
- Every behavior change follows RED → GREEN → focused tests → full tests/Ruff. Do not run `resume` or calculate any final-test strategy output in this plan.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/ashare_multifactor/final_test/official_query_client.py` | Bounded CNInfo POST transport with retry/rate-limit behavior and no persistence. |
| `src/ashare_multifactor/final_test/official_query_collector.py` | Thin preparation-scope and authorization orchestration API. |
| `src/ashare_multifactor/final_test/official_query_collection_root.py` | Attempt-bound evidence-root identity, lock and descriptor safety. |
| `src/ashare_multifactor/final_test/official_query_packages.py` | Immutable package staging, pagination, recovery and exact index construction. |
| `src/ashare_multifactor/final_test/official_announcement_catalog.py` | Converts completed packages into a hash-bound announcement catalog and review tasks without asserting event facts. |
| `src/ashare_multifactor/final_test/official_document_fetcher.py` | Downloads approved official URLs into immutable evidence cache files and verifies byte identity. |
| `src/ashare_multifactor/final_test/official_evidence_workspace.py` | Coordinates public evidence roots, candidate snapshots, review input validation and ready-blocking manifests. |
| `src/ashare_multifactor/cli/final_test.py` | Adds the narrow `collect-queries` and `collect-documents` subcommands. |
| `tests/test_final_test_official_query_client.py` | Transport retry, timeout and redaction tests. |
| `tests/test_final_test_official_query_collector.py` | Scope, pagination, restart, package/index and path-safety tests. |
| `tests/test_final_test_official_evidence_workspace.py` | Catalog, download, review-queue and nonzero-event blocking tests. |
| `tests/test_final_test_cli.py` | End-to-end synthetic CLI coverage without token consumption or research execution. |
| `docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md` | Incident facts and task-state update after archive/reseal evidence exists. |

---

### Task 1: Close and archive the current prepared attempt

**Files:**
- Modify: `docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md`
- Test: `tests/test_final_test_incident_archive.py`
- Runtime output only: `processed/final_test_incidents/stage9-final-20260719-ce7c729/`

**Interfaces:**
- Consumes: `append_attempt_outcome(attempts, attempt_id, status="failed", authoritative=False, reason="official evidence collector unavailable before evidence acquisition")` and `archive_failed_attempt(processed_root, attempt_id=attempt_id)`.
- Produces: a no-replace incident directory with `incident_manifest.json`; subsequent collector work starts only after `processed/final_test` is absent.

- [x] **Step 1: Run the existing prepared-attempt archive regression**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest \
  tests/test_final_test_incident_archive.py::test_failed_attempt_archive_preserves_verified_two_phase_preparation \
  tests/test_final_test_incident_archive.py -q
```

Expected: PASS, including existing source-swap, partial-manifest and preparation-identity tests.

- [x] **Step 2: Verify and archive the real attempt exactly once**

Run a short Python invocation that imports only `append_attempt_outcome` and `archive_failed_attempt`, first verifies the state is `awaiting_official_evidence`, then records this exact reason:

```python
reason = "official evidence collector unavailable before evidence acquisition"
append_attempt_outcome(
    attempts,
    attempt_id="stage9-final-20260719-ce7c729",
    status="failed",
    authoritative=False,
    reason=reason,
)
archive_failed_attempt(processed, attempt_id="stage9-final-20260719-ce7c729")
```

Verify that the returned incident manifest hashes every archived file, `CURRENT.json` is absent, and no new token or attempt record was created. Record only the manifest SHA-256 and facts in the Stage 9 plan.

- [x] **Step 3: Commit the incident disclosure**

```bash
git add docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md
git commit -m "docs(stage9): archive collector-blocked attempt"
```

### Task 2: Add a bounded, injectable official-query transport

**Files:**
- Create: `src/ashare_multifactor/final_test/official_query_client.py`
- Create: `tests/test_final_test_official_query_client.py`

**Interfaces:**
- Consumes: endpoint/form from `OfficialQueryScope`; standard-library network primitives only.
- Produces: `OfficialQueryTransport.fetch(endpoint, form, timeout_seconds) -> bytes` and `fetch_with_retry(transport, endpoint, form, policy, sleep) -> bytes`.
- Used by: Task 3 collector; tests pass a deterministic fake transport.

- [x] **Step 1: Write failing retry/redaction tests**

```python
def test_client_retries_transient_status_then_returns_raw_bytes() -> None:
    transport = ScriptedTransport([HttpFailure(429), b'{"totalpages":0,"totalAnnouncement":0,"announcements":null}'])
    assert fetch_with_retry(transport, endpoint=OFFICIAL_QUERY_ENDPOINT, form=_form(), policy=_policy()) == transport.responses[-1]
    assert transport.calls == 2

def test_client_never_returns_or_persists_cookie_or_authorization_headers() -> None:
    request = build_request(_form())
    assert "Cookie" not in request.headers
    assert "Authorization" not in request.headers
```

- [x] **Step 2: Run the test and observe RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_query_client.py -q`

Expected: FAIL with `ModuleNotFoundError` or missing `fetch_with_retry`.

- [x] **Step 3: Implement the smallest transport boundary**

```python
class OfficialQueryTransport(Protocol):
    def fetch(
        self,
        endpoint: str,
        form: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> bytes:
        raise NotImplementedError

@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 20.0
    minimum_interval_seconds: float = 1.0

def fetch_with_retry(
    transport: OfficialQueryTransport,
    *,
    endpoint: str,
    form: Mapping[str, str],
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    for attempt in range(policy.attempts):
        try:
            response = transport.fetch(
                endpoint,
                form,
                timeout_seconds=policy.timeout_seconds,
            )
        except TransientOfficialQueryError:
            if attempt + 1 == policy.attempts:
                raise
            sleep(policy.minimum_interval_seconds * (2**attempt))
            continue
        if not response:
            raise ValueError("official query response is empty")
        return response
    raise RuntimeError("official query retry loop terminated unexpectedly")
```

Use `urllib.request.Request` with `application/x-www-form-urlencoded`, an explicit public User-Agent, and no cookies/authentication headers. Validate nonempty bytes before returning.

- [x] **Step 4: Add boundary cases and run focused tests**

Add tests for timeout exhaustion, non-retryable 4xx, malformed response bytes remaining unparsed, and deterministic backoff calls.

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_query_client.py -q`

Expected: PASS.

- [x] **Step 5: Commit Task 2**

```bash
git add src/ashare_multifactor/final_test/official_query_client.py tests/test_final_test_official_query_client.py
git commit -m "feat(stage9): add bounded official query transport"
```

### Task 3: Publish recoverable, scope-bound CNInfo query packages

**Files:**
- Create: `src/ashare_multifactor/final_test/official_query_collector.py`
- Modify: `src/ashare_multifactor/final_test/official_query_index.py`
- Create: `tests/test_final_test_official_query_collector.py`

**Interfaces:**
- Consumes: `FinalTestPreparation`, `FinalTestAuthorization`, `OfficialQueryTransport`, `official_query_scope()`, and existing `validate_official_query_package/index`.
- Produces:

```python
@dataclass(frozen=True)
class QueryCollectionResult:
    root: Path
    completed_scopes: int
    total_scopes: int
    index_path: Path | None

def collect_official_query_coverage(
    *,
    preparation: FinalTestPreparation,
    authorization: FinalTestAuthorization,
    contract: FinalActionSourceContract,
    output_root: Path,
    transport: OfficialQueryTransport,
    policy: RetryPolicy,
    max_scopes: int | None = None,
) -> QueryCollectionResult:
    raise NotImplementedError
```

`output_root` is exactly the attempt-specific evidence root
`processed/final_test_evidence/<attempt_id>`; the package/index directory is
its `official_query_coverage/` child.  Before any request, the collector
publishes or verifies a canonical `official_query_collection.json` binding the
attempt, approval, Git, Stage-8 identity, preparation-manifest hash and symbol
scope hash.  The index itself remains under the coverage child so existing
validators retain their canonical package layout.

- [x] **Step 1: Write failing collector tests**

```python
def test_collector_derives_exact_two_category_scope_from_verified_preparation(prepared: Prepared) -> None:
    result = collect_official_query_coverage(**prepared.args, transport=zero_result_transport())
    assert result.total_scopes == prepared.symbol_count * 2
    assert result.completed_scopes == result.total_scopes
    assert validate_official_query_coverage_index(result.index_path, expected_scopes=prepared.expected_scopes)

def test_collector_partial_run_has_no_index_and_resume_finishes_without_rewriting_packages(prepared: Prepared) -> None:
    first = collect_official_query_coverage(**prepared.args, transport=zero_result_transport(), max_scopes=1)
    package = _only_package(first.root).read_bytes()
    second = collect_official_query_coverage(**prepared.args, transport=zero_result_transport())
    assert first.index_path is None
    assert second.index_path is not None
    assert _only_package(second.root).read_bytes() == package

def test_collector_rejects_scope_or_authorization_drift_before_network(prepared: Prepared) -> None:
    with pytest.raises(ValueError, match="preparation|authorization|scope"):
        collect_official_query_coverage(**prepared.tampered_args, transport=FailIfCalled())
```

- [x] **Step 2: Run and observe RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_query_collector.py -q`

Expected: FAIL because the collector module is absent.

- [x] **Step 3: Implement immutable package construction and staging**

```python
def _expected_scopes(
    preparation: FinalTestPreparation,
    contract: FinalActionSourceContract,
) -> tuple[OfficialQueryScope, ...]:
    symbols = load_bound_symbol_scope(preparation)
    return tuple(
        official_query_scope(contract, symbol=symbol, category=category)
        for category in ("corporate_actions", "security_events")
        for symbol in symbols
    )

def _publish_package(scope: OfficialQueryScope, *, root: Path, transport: OfficialQueryTransport, policy: RetryPolicy) -> VerifiedOfficialQueryPackage:
    raise NotImplementedError
```

Store package metadata only through `canonical_json_bytes`, raw response bytes unchanged, and use the existing canonical path shape. Add one no-replace atomic index publisher after every expected scope validates. Do not edit the existing package validator to accept partial output.

- [x] **Step 4: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_query_coverage.py tests/test_final_test_official_query_index.py tests/test_final_test_official_query_collector.py -q
../../.venv/bin/python -m ruff check src/ashare_multifactor/final_test/official_query_client.py src/ashare_multifactor/final_test/official_query_collector.py tests/test_final_test_official_query_collector.py
```

Expected: all pass.

- [x] **Step 5: Commit Task 3**

```bash
git add src/ashare_multifactor/final_test/official_query_collector.py src/ashare_multifactor/final_test/official_query_index.py tests/test_final_test_official_query_collector.py
git commit -m "feat(stage9): collect immutable official query coverage"
```

### Task 4: Expose collection through the final-test CLI without changing authorization state

**Files:**
- Modify: `src/ashare_multifactor/cli/final_test.py`
- Create: `tests/test_final_test_cli.py`

**Interfaces:**
- Consumes: `load_registered_authorization`, `verify_preparation`, and `collect_official_query_coverage`.
- Produces:

```text
python -m ashare_multifactor.cli.final_test collect-queries \
  --root <code-root> --data-root <data-root> --approval-key-file <key-file> \
  --attempt-id <attempt-id> --output-root <attempt-specific-evidence-root> \
  [--max-scopes <positive-int>]
```

- [ ] **Step 1: Write failing CLI tests**

```python
def test_collect_queries_uses_registered_attempt_and_never_consumes_a_second_token(prepared_cli: PreparedCli) -> None:
    before = _registry_bytes(prepared_cli.final_root / "attempts")
    main(["collect-queries", *prepared_cli.argv])
    assert _registry_bytes(prepared_cli.final_root / "attempts") == before
    assert (prepared_cli.output_root / "official_query_coverage.json").is_file()

def test_collect_queries_rejects_a_foreign_output_root_or_non_waiting_attempt(prepared_cli: PreparedCli) -> None:
    with pytest.raises(ValueError, match="attempt|output|awaiting"):
        main(["collect-queries", *prepared_cli.foreign_output_argv])
```

- [ ] **Step 2: Run and observe RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_cli.py -q`

Expected: FAIL because `collect-queries` is not a recognized command.

- [ ] **Step 3: Add the narrow subcommand**

```python
collect = commands.add_parser("collect-queries", help="collect official query coverage")
_add_common_arguments(collect)
collect.add_argument("--output-root", type=Path, required=True)
collect.add_argument("--max-scopes", type=_positive_int)

if args.command == "collect-queries":
    authorization = load_registered_authorization(
        code_root=args.root,
        data_root=args.data_root,
        attempt_id=args.attempt_id,
        approval_key=approval_key,
    )
    preparation = verify_preparation(
        args.data_root / "processed/final_test",
        attempt_id=args.attempt_id,
        authorization=authorization,
        expected_state="awaiting_official_evidence",
    )
    result = collect_official_query_coverage(
        preparation=preparation,
        authorization=authorization,
        output_root=args.output_root,
        transport=UrllibOfficialQueryTransport(),
        policy=RetryPolicy(),
        max_scopes=args.max_scopes,
    )
    print(result.index_path or result.root)
    return
```

Require output root to be exactly `data_root / "processed/final_test_evidence" / attempt_id`; reject symlinks and paths outside that root. Keep the HTTP transport construction in the collector module so CLI stays thin.

- [ ] **Step 4: Run focused CLI and collector tests**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_cli.py tests/test_final_test_official_query_collector.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/ashare_multifactor/cli/final_test.py tests/test_final_test_cli.py
git commit -m "feat(stage9): add official query collection CLI"
```

### Task 5: Build the official-document catalog and fail-closed review workspace

**Files:**
- Create: `src/ashare_multifactor/final_test/official_announcement_catalog.py`
- Create: `src/ashare_multifactor/final_test/official_document_fetcher.py`
- Create: `src/ashare_multifactor/final_test/official_evidence_workspace.py`
- Create: `tests/test_final_test_official_evidence_workspace.py`
- Modify: `src/ashare_multifactor/cli/final_test.py`

**Interfaces:**
- Consumes: a complete `official_query_coverage.json`, the immutable preparation, and allowed URL rules from `action_source_contract.py`.
- Produces:

```python
@dataclass(frozen=True)
class EvidenceWorkspace:
    root: Path
    catalog_path: Path
    review_queue_path: Path
    ready: bool

def build_announcement_catalog(
    index_path: Path,
    *,
    preparation: FinalTestPreparation,
    destination: Path,
) -> Path:
    raise NotImplementedError

def fetch_official_documents(
    catalog_path: Path,
    *,
    destination: Path,
    transport: OfficialDocumentTransport,
    max_documents: int | None = None,
) -> EvidenceWorkspace:
    raise NotImplementedError

def validate_review_submission(
    workspace: EvidenceWorkspace,
    submission: Path,
) -> None:
    raise NotImplementedError
```

- [ ] **Step 1: Write failing catalog/download/review tests**

```python
def test_catalog_preserves_query_provenance_but_does_not_create_event_facts(complete_index: Path, prepared: Prepared) -> None:
    catalog = build_announcement_catalog(complete_index, preparation=prepared.value, destination=prepared.evidence_root)
    row = pl.read_parquet(catalog).row(0, named=True)
    assert {"announcement_id", "source_response", "source_url", "symbol", "market"} <= row.keys()
    assert "effective_date" not in row

def test_unreviewed_or_ambiguous_document_blocks_ready_coverage(workspace: EvidenceWorkspace) -> None:
    with pytest.raises(ValueError, match="review|ready|official"):
        validate_review_submission(workspace, workspace.review_queue_path)

def test_document_fetcher_rejects_non_whitelisted_urls_and_never_overwrites_cached_bytes(workspace: EvidenceWorkspace) -> None:
    with pytest.raises(ValueError, match="URL|source"):
        fetch_official_documents(
            workspace.catalog_path,
            destination=workspace.root / "documents",
            transport=FakeDocuments({"https://example.invalid/doc.pdf": b"bad"}),
        )
```

- [ ] **Step 2: Run and observe RED**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_evidence_workspace.py -q`

Expected: FAIL because the catalog/workspace modules do not exist.

- [ ] **Step 3: Implement catalog and immutable document cache**

```python
def build_announcement_catalog(index_path: Path, *, preparation: FinalTestPreparation, destination: Path) -> Path:
    packages = validate_official_query_coverage_index(index_path, expected_scopes=_expected_scopes(preparation))
    # Read only validated raw pages. Preserve announcement ID, title, timestamp,
    # attachment URL and package/page provenance. Reject missing or non-whitelisted URLs.
    # Write a deterministic Parquet catalog plus a manifest with file hashes.

def fetch_official_documents(
    catalog_path: Path,
    *,
    destination: Path,
    transport: OfficialDocumentTransport,
    max_documents: int | None = None,
) -> EvidenceWorkspace:
    catalog = _load_verified_catalog(catalog_path)
    selected = catalog.head(max_documents) if max_documents is not None else catalog
    for row in selected.iter_rows(named=True):
        assert_allowed_evidence_url(str(row["source_url"]), contract)
        _publish_document_no_replace(destination, row, transport.fetch(str(row["source_url"])))
    return _publish_review_workspace(destination, catalog)
```

For company-action candidates, reuse the existing BaoStock field normalizer only to create a candidate snapshot and review tasks. Do not publish `corporate_action_coverage/coverage.json` or `security_event_coverage.json` with `status: ready` from unreviewed data. The only way to create a ready manifest is a fully hashed, schema-valid reviewer submission that the existing validators accept.

- [ ] **Step 4: Add CLI command and test no state transition**

```text
python -m ashare_multifactor.cli.final_test collect-documents \
  --root <code-root> --data-root <data-root> --approval-key-file <key-file> \
  --attempt-id <attempt-id> --output-root <attempt-specific-evidence-root> \
  [--max-documents <positive-int>]
```

The command must reconstruct the registered authorization and verify the prepare state, but must leave the append-only state files byte-identical.

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_final_test_official_evidence_workspace.py tests/test_final_test_cli.py tests/test_corporate_action_coverage.py tests/test_final_test_resume.py -q
../../.venv/bin/python -m ruff check src/ashare_multifactor/final_test/official_announcement_catalog.py src/ashare_multifactor/final_test/official_document_fetcher.py src/ashare_multifactor/final_test/official_evidence_workspace.py tests/test_final_test_official_evidence_workspace.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/ashare_multifactor/final_test/official_announcement_catalog.py src/ashare_multifactor/final_test/official_document_fetcher.py src/ashare_multifactor/final_test/official_evidence_workspace.py src/ashare_multifactor/cli/final_test.py tests/test_final_test_official_evidence_workspace.py tests/test_final_test_cli.py
git commit -m "feat(stage9): stage official evidence for review"
```

### Task 6: Verify, review, re-run Stage 7/8, and publish a closed new seal

**Files:**
- Modify only if facts change: `docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md`
- Runtime output only: Stage7/Stage8 successor release directories and their manifests.

**Interfaces:**
- Consumes: clean implementation commit, frozen research/robustness protocol, existing Stage7 and Stage8 full-run/publish commands.
- Produces: a new Stage8 successor whose sealed protocol binds the exact clean Git commit/tree and still has `opening_token_status: closed`.

- [ ] **Step 1: Run all code verification before any re-seal**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest -q
../../.venv/bin/python -m ruff check src tests
git diff --check
git status --short
```

Expected: all tests pass, Ruff has no findings, no tracked uncommitted implementation changes.

- [ ] **Step 2: Perform independent security review against the spec**

Check each of these directly in code/tests: no final-period acquisition during implementation; package/index exactness; no secret persistence; descriptor/symlink safety; zero-event semantics; current attempt archive immutability; no event fact inferred from an announcement title; CLI does not consume/refresh a token.

- [ ] **Step 3: Reproduce and publish Stage7 successor**

Run the repository’s established Stage7 dual-run command twice with the clean commit, compare the core-file hashes, require `release_eligible=true`, publish a successor, and verify: prior Stage7 release manifest unchanged; successor only includes `sh`/`sz`; predecessor lineage is explicit.

- [ ] **Step 4: Rebind, reproduce and publish Stage8 successor**

Update only the frozen Stage7 identity in `configs/robustness_protocol.yaml` and its focused fixtures. Run focused tests, commit the binding, run the established Stage8 dual-run command twice, require equal core hashes and `release_eligible=true`, then publish the Stage8 successor.

- [ ] **Step 5: Verify the new seal and closure boundary**

Verify the new Stage8 manifest, lineage, protocol SHA, Git commit/tree, supported markets, source-contract hash and audit attachment hashes. Also verify:

```text
processed/final_test/CURRENT.json       absent
processed/final_test/attempts           absent
new opening ledger                      absent
archived current attempt                present and hash-valid
```

Do not create a new token or new final-test attempt in this task.

- [ ] **Step 6: Update factual status and commit**

Update only factual Stage9 status, including the archived incident hash and the new closed Stage8 identity. Then:

```bash
git add configs/robustness_protocol.yaml tests/test_final_test_gate.py tests/test_sealed_test_protocol.py tests/test_stage8_successor_seal.py docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md
git commit -m "chore(stage8): reseal official evidence collector"
```

Do not mark Stage9 complete. Request a new, explicit user authorization for the newly published seal before any `prepare` can run.

## Plan Self-Review

- **Spec coverage:** Tasks 1–6 respectively cover archival, query transport, scope/package/index recovery, CLI lifecycle binding, raw official document collection and manual-fact blocking, then full verification/reseal.
- **No event-fact shortcut:** Every task preserves existing official-action and security-event validators; no task permits query counts or title matching to produce execution inputs.
- **Placeholder scan:** All paths, commands, interfaces and expected failure/pass conditions are explicit. Runtime successor IDs are intentionally generated by verified publish commands rather than predeclared.
- **Type consistency:** Task 2 provides `OfficialQueryTransport`/`RetryPolicy`, Task 3 consumes them and emits `QueryCollectionResult`, Task 4 reconstructs authorization/preparation before calling Task 3, and Task 5 consumes only the completed index from Task 3.
