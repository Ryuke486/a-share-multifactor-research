# 板块9两阶段一次性最终测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将最终测试改为同一attempt的`prepare → awaiting_official_evidence → resume`流程，使精确证券范围在官方证据采集前不可变发布，同时保持一次授权、一次测试期打开和一次权威release。

**Architecture:** 新增独立的准备模块和恢复授权模块；`registry.py`只保存append-only状态事件，`pipeline.py`只编排已准备attempt的正式执行。CLI显式区分`prepare`和`resume`，旧一体化入口失败关闭。

**Tech Stack:** Python 3.14、Polars、PyYAML、pytest、ruff、现有audit/publication/final_test接口。

## Global Constraints

- 研究市场仅为上交所和深交所，排除北交所。
- 2022-01-01至2025-12-31只能在用户批准、令牌验证和attempt登记后读取。
- 2026年及以后数据不得读取。
- 不改变因子、方向、候选、权重、组合、成本、参数、指标或样本划分。
- 不因最终结果修改模型或报告口径。
- 用户批准只消费一次；prepare、resume和最终release必须使用同一`attempt_id`。
- 官方证据缺失时保持`awaiting_official_evidence`，不得写失败结果或创建第二个attempt。
- 原始数据只读；生成物只写入`processed/final_test`或`artifacts/final_test`。
- 所有实现遵循测试先行：失败反例 → 最小实现 → 相关回归 → 独立审查。
- 代码变更后旧Stage8封印失效；最终执行前必须重新完成Stage7/8双run、successor发布和用户授权。

---

### Task 1: Append-only Attempt状态机

**Files:**
- Modify: `src/ashare_multifactor/final_test/registry.py`
- Test: `tests/test_final_test_registry.py`

**Interfaces:**
- Consumes: 现有不可变`<attempt_id>.json`和`<attempt_id>.outcome.json`。
- Produces: `AttemptStateEvent`、`append_attempt_state`状态追加接口、`resolve_attempt_state`状态解析接口。

- [x] **Step 1: 写合法状态链和并发领取失败测试**

```python
def test_attempt_state_events_are_append_only_and_resume_claim_is_exclusive(tmp_path: Path) -> None:
    registry = tmp_path / "attempts"
    _register(registry, "attempt-001")
    append_attempt_state(registry, attempt_id="attempt-001", state="preparing")
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="awaiting_official_evidence",
        identities={"prepare_manifest_sha256": "a" * 64},
    )
    append_attempt_state(
        registry,
        attempt_id="attempt-001",
        state="executing",
        identities={
            "prepare_manifest_sha256": "a" * 64,
            "security_event_coverage_sha256": "b" * 64,
            "corporate_action_coverage_sha256": "c" * 64,
        },
    )
    assert resolve_attempt_state(registry, "attempt-001")["state"] == "executing"
    with pytest.raises(ValueError, match="state transition"):
        append_attempt_state(
            registry,
            attempt_id="attempt-001",
            state="executing",
            identities={"prepare_manifest_sha256": "a" * 64},
        )
```

同时增加以下独立反例：跳过`preparing`、从`awaiting_official_evidence`退回`preparing`、终态后追加状态、缺少64位哈希、状态文件内容被修改。

- [x] **Step 2: 运行RED测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_registry.py`

Expected: FAIL，导入`append_attempt_state`或`resolve_attempt_state`失败。

- [x] **Step 3: 实现固定序号状态事件**

```python
_STATE_SEQUENCE = {
    "registered": 0,
    "preparing": 1,
    "awaiting_official_evidence": 2,
    "executing": 3,
}


def append_attempt_state(
    registry_root: Path,
    *,
    attempt_id: str,
    state: str,
    identities: dict[str, str] | None = None,
) -> dict[str, Any]:
    current = resolve_attempt_state(registry_root, attempt_id)
    expected = _STATE_SEQUENCE[current["state"]] + 1
    if _STATE_SEQUENCE.get(state) != expected:
        raise ValueError("invalid final-test state transition")
    payload = {
        "attempt_id": attempt_id,
        "sequence": expected,
        "state": state,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "identities": _validate_state_identities(state, identities or {}),
    }
    _write_exclusive(
        registry_root / f"{attempt_id}.state.{expected:02d}-{state}.json",
        _json_bytes(payload),
    )
    return payload
```

`resolve_attempt_state`必须从基础登记、固定序号事件和终态outcome重建状态；发现重复序号、缺号、非法文件名、身份字段不完整或终态冲突时拒绝。

- [x] **Step 4: 运行GREEN和既有registry/gate回归**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_registry.py tests/test_final_test_gate.py`

Expected: PASS。

- [x] **Step 5: 提交并进入任务审查**

```bash
git add src/ashare_multifactor/final_test/registry.py tests/test_final_test_registry.py
git commit -m "feat(final-test): add append-only attempt states"
```

审查重点：状态文件不可改写、`executing`排他、终态不可继续。

### Task 2: 不可变prepare清单和证券范围

**Files:**
- Create: `src/ashare_multifactor/final_test/preparation.py`
- Test: `tests/test_final_test_preparation.py`

**Interfaces:**
- Consumes: `FinalTestAuthorization`、已验证`ResearchConfig`和现有`build_final_test_daily_panel`接口。
- Produces: `FinalTestPreparation`、`prepare_final_test`准备接口、`verify_preparation`复核接口。

- [x] **Step 1: 写prepare先登记、后扫描且不进入研究链路的失败测试**

```python
def test_prepare_registers_before_scan_and_stops_before_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.authorize_final_test",
        lambda **kwargs: calls.append("registered") or _authorization(),
    )
    monkeypatch.setattr(
        "ashare_multifactor.final_test.preparation.build_final_test_daily_panel",
        lambda *args, **kwargs: calls.append("data_scan") or _build_panel(tmp_path),
    )
    result = prepare_final_test(
        code_root=tmp_path,
        data_root=tmp_path,
        opening_token_path=tmp_path / "token.json",
        approval_key=b"0123456789abcdef",
        attempt_id="attempt-001",
    )
    assert calls == ["registered", "data_scan"]
    assert result.state == "awaiting_official_evidence"
    assert not (tmp_path / "processed/final_test/attempt_runs").exists()
```

增加真实Polars夹具反例：证券升序去重、`4/8/92`代码拒绝、日期超出2022–2025拒绝、数据manifest漂移拒绝、prepare目录预存在拒绝。

- [x] **Step 2: 运行RED测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_preparation.py`

Expected: FAIL with `ModuleNotFoundError`。

- [x] **Step 3: 实现准备对象和确定性证券摘要**

```python
@dataclass(frozen=True)
class FinalTestPreparation:
    attempt_id: str
    state: str
    root: Path
    manifest_path: Path
    manifest_sha256: str
    symbol_scope_path: Path
    symbol_count: int
    symbols_sha256: str


def _symbols_digest(symbols: list[str]) -> str:
    return hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
```

`prepare_final_test`的最小顺序必须为：`authorize_final_test` → `append_attempt_state(preparing)` → 加载冻结配置 → 构建/恢复数据面板 → 验证面板 → 写`symbol_scope.parquet` → 写`prepare_manifest.json` → 复验 → `append_attempt_state(awaiting_official_evidence)`。

准备目录固定为`processed/final_test/preparations/<attempt_id>`。manifest字段必须与规格一致，文件通过临时目录和`os.replace`一次发布；不导入signals、backtest、metrics或report模块。

- [x] **Step 4: 实现prepare恢复限制**

```python
def verify_preparation(
    final_root: Path,
    *,
    attempt_id: str,
    authorization: FinalTestAuthorization,
) -> FinalTestPreparation:
    """Verify exact files and identities; never rebuild or replace symbol scope."""
```

若状态已是`awaiting_official_evidence`，只允许验证并返回同一准备清单；若状态为`preparing`，只允许沿现有data claim恢复，禁止重新发现一套不同输入。

- [x] **Step 5: 运行GREEN和数据模块回归**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_preparation.py tests/test_final_test_data.py`

Expected: PASS。

- [x] **Step 6: 提交并进入任务审查**

```bash
git add src/ashare_multifactor/final_test/preparation.py tests/test_final_test_preparation.py
git commit -m "feat(final-test): publish immutable preparation scope"
```

审查重点：首个最终期扫描前已登记、prepare不计算研究结果、证券集合不可漂移。

### Task 3: 同attempt授权恢复和官方覆盖预检

**Files:**
- Create: `src/ashare_multifactor/final_test/resume.py`
- Modify: `src/ashare_multifactor/final_test/execution_sources.py`
- Test: `tests/test_final_test_resume.py`

**Interfaces:**
- Consumes: attempt登记、token snapshot、opening ledger、`FinalTestPreparation`和两类coverage。
- Produces: `load_registered_authorization`授权恢复接口、`preflight_resume`恢复预检接口。

- [x] **Step 1: 写resume不二次消费令牌及漂移失败测试**

```python
def test_resume_reconstructs_same_authorization_without_consuming_token_again(
    prepared_attempt: PreparedAttempt,
) -> None:
    before = prepared_attempt.consumption_ledger.read_bytes()
    authorization = load_registered_authorization(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        attempt_id=prepared_attempt.attempt_id,
        approval_key=prepared_attempt.approval_key,
    )
    assert authorization.attempt_id == prepared_attempt.attempt_id
    assert prepared_attempt.consumption_ledger.read_bytes() == before
```

增加独立反例：token snapshot、ledger、Stage8 manifest/lineage/seal、Git commit/tree、prepare manifest、symbol scope任一漂移；状态不是`awaiting_official_evidence`；coverage缺证券、额外证券、零事件无成功证据或哈希变化。

- [x] **Step 2: 运行RED测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_resume.py`

Expected: FAIL with `ModuleNotFoundError`。

- [x] **Step 3: 实现授权重建**

```python
def load_registered_authorization(
    *,
    code_root: Path,
    data_root: Path,
    attempt_id: str,
    approval_key: bytes,
) -> FinalTestAuthorization:
    record = _load_attempt_record(data_root, attempt_id)
    token = _verify_token_snapshot(record, approval_key)
    _verify_consumption_ledger(record, token)
    _verify_current_code_and_stage8(code_root, data_root, record)
    return _authorization_from_record(record)
```

该函数只读取已登记身份；不得调用`authorize_final_test`或`verify_test_opening_token`，因为后者会再次创建消费ledger。

- [x] **Step 4: 提取无写入coverage预检**

```python
@dataclass(frozen=True)
class ResumePreflight:
    authorization: FinalTestAuthorization
    preparation: FinalTestPreparation
    security_event_coverage_sha256: str
    corporate_action_coverage_sha256: str


def validate_final_execution_coverages(
    *,
    symbols: list[str],
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        validate_security_event_coverage(security_event_coverage_path, symbols=symbols),
        validate_corporate_action_coverage(corporate_action_coverage_root, symbols=symbols),
    )
```

`preflight_resume`先验证准备清单，再加载`symbol_scope.parquet`并调用该函数。coverage尚未就绪时只抛出门禁错误，不追加`failed`或`executing`状态。

- [x] **Step 5: 运行GREEN和coverage回归**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_resume.py tests/test_final_test_pipeline.py tests/test_corporate_action_coverage.py`

Expected: PASS。

- [x] **Step 6: 提交并进入任务审查**

```bash
git add src/ashare_multifactor/final_test/resume.py src/ashare_multifactor/final_test/execution_sources.py tests/test_final_test_resume.py
git commit -m "feat(final-test): verify resumable prepared attempts"
```

审查重点：原令牌只消费一次、resume完整复核四元组和准备身份、证据缺失不终结attempt。

### Task 4: 已准备attempt的执行与崩溃恢复

**Files:**
- Modify: `src/ashare_multifactor/final_test/pipeline.py`
- Modify: `src/ashare_multifactor/final_test/registry.py`
- Test: `tests/test_final_test_pipeline.py`

**Interfaces:**
- Consumes: `ResumePreflight`和精确coverage路径。
- Produces: `resume_final_test_release`续跑接口，返回`FinalTestPipelineResult`。

- [x] **Step 1: 写领取执行权后继续同一attempt的失败测试**

```python
def test_resume_claims_execution_once_and_publishes_same_attempt(
    prepared_attempt: PreparedAttempt,
    official_coverages: OfficialCoverages,
) -> None:
    result = resume_final_test_release(
        code_root=prepared_attempt.code_root,
        data_root=prepared_attempt.data_root,
        approval_key=prepared_attempt.approval_key,
        attempt_id=prepared_attempt.attempt_id,
        security_event_coverage_path=official_coverages.security,
        corporate_action_coverage_root=official_coverages.corporate,
        run_id="final-release",
    )
    assert result.attempt_id == prepared_attempt.attempt_id
    assert result.release is not None
    with pytest.raises(ValueError, match="already succeeded|state transition"):
        resume_final_test_release(
            code_root=prepared_attempt.code_root,
            data_root=prepared_attempt.data_root,
            approval_key=prepared_attempt.approval_key,
            attempt_id=prepared_attempt.attempt_id,
            security_event_coverage_path=official_coverages.security,
            corporate_action_coverage_root=official_coverages.corporate,
            run_id="final-release",
        )
```

增加反例：coverage预检失败时仍为`awaiting_official_evidence`；领取后coverage漂移写失败outcome；账务门禁失败保留attempt且无`CURRENT`；发布中断沿prepared-publication恢复。

- [x] **Step 2: 运行RED测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_pipeline.py -k 'resume or prepared_attempt'`

Expected: FAIL，缺少`resume_final_test_release`。

- [x] **Step 3: 将现有一体化函数拆为准备外壳和执行核心**

```python
def resume_final_test_release(
    *,
    code_root: Path,
    data_root: Path,
    approval_key: bytes,
    attempt_id: str,
    security_event_coverage_path: Path,
    corporate_action_coverage_root: Path,
    run_id: str,
) -> FinalTestPipelineResult:
    preflight = preflight_resume(
        code_root=code_root,
        data_root=data_root,
        approval_key=approval_key,
        attempt_id=attempt_id,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
    )
    append_attempt_state(
        registry_root,
        attempt_id=attempt_id,
        state="executing",
        identities={
            "prepare_manifest_sha256": preflight.preparation.manifest_sha256,
            "security_event_coverage_sha256": preflight.security_event_coverage_sha256,
            "corporate_action_coverage_sha256": preflight.corporate_action_coverage_sha256,
        },
    )
    return _execute_authorized_final_test(
        authorization=preflight.authorization,
        preparation=preflight.preparation,
        security_event_coverage_path=security_event_coverage_path,
        corporate_action_coverage_root=corporate_action_coverage_root,
        run_id=run_id,
    )
```

`_execute_authorized_final_test`复用现有execution inputs、signals、backtest、metrics、report和publish逻辑；删除从该核心内部重新授权或重建数据面板的路径。旧`run_final_test_release`改为抛出`ValueError("two-phase final-test workflow is required")`，不得保留可绕过prepare的生产入口。

- [x] **Step 4: 实现执行崩溃恢复**

执行工作目录增加唯一`execution_id`。若状态为`executing`且无终态：

```python
def recover_interrupted_execution(final_root: Path, attempt_id: str) -> Path:
    partial = final_root / "attempt_runs" / attempt_id
    archive = final_root / "interrupted_runs" / attempt_id / _next_recovery_id()
    _verify_partial_attempt_identity(partial, attempt_id)
    os.replace(partial, archive)
    append_execution_recovery_event(
        final_root,
        attempt_id=attempt_id,
        archived_path=archive,
    )
    return archive
```

只允许归档并以同一attempt、同一prepare/coverage哈希重新计算；归档文件不可删除或覆盖。若partial身份不完整或与attempt不符则失败关闭。

- [x] **Step 5: 运行GREEN和完整pipeline相关测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_pipeline.py tests/test_final_test_backtest.py tests/test_final_test_metrics.py`

Expected: PASS。

- [x] **Step 6: 提交并进入任务审查**

```bash
git add src/ashare_multifactor/final_test/pipeline.py src/ashare_multifactor/final_test/registry.py tests/test_final_test_pipeline.py
git commit -m "feat(final-test): resume prepared one-shot execution"
```

审查重点：pipeline不再消费令牌或重建证券范围；并发resume只有一个执行者；中断产物完整保留。

### Task 5: 两阶段CLI和端到端合成反例

**Files:**
- Modify: `src/ashare_multifactor/cli/final_test.py`
- Create: `tests/test_final_test_two_phase.py`
- Modify: `tests/test_final_test_pipeline.py`
- Modify: `docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md`

**Interfaces:**
- Consumes: `prepare_final_test`和`resume_final_test_release`。
- Produces: `final-test prepare`与`final-test resume`两个唯一生产入口。

- [ ] **Step 1: 写CLI契约和完整状态流失败测试**

```python
def test_cli_requires_explicit_prepare_or_resume() -> None:
    source = Path("src/ashare_multifactor/cli/final_test.py").read_text()
    assert 'choices=("prepare", "resume")' in source
    assert "--data-root" in source


def test_synthetic_two_phase_flow_uses_one_approval_and_one_attempt(tmp_path: Path) -> None:
    fixture = build_two_phase_fixture(tmp_path)
    prepared = prepare_final_test(
        code_root=fixture.code_root,
        data_root=fixture.data_root,
        opening_token_path=fixture.opening_token_path,
        approval_key=fixture.approval_key,
        attempt_id="attempt-001",
    )
    assert prepared.state == "awaiting_official_evidence"
    assert _token_consumption_count(tmp_path) == 1
    _write_exact_official_coverages(prepared.symbol_scope_path)
    result = resume_final_test_release(
        code_root=fixture.code_root,
        data_root=fixture.data_root,
        approval_key=fixture.approval_key,
        attempt_id="attempt-001",
        security_event_coverage_path=fixture.security_event_coverage_path,
        corporate_action_coverage_root=fixture.corporate_action_coverage_root,
        run_id="final-release",
    )
    assert result.release is not None
    assert result.release.run_id == "final-release"
    assert _attempt_ids(tmp_path) == {"attempt-001"}
    assert _token_consumption_count(tmp_path) == 1
```

增加端到端反例：无子命令拒绝、prepare传入coverage参数拒绝、resume传入opening token拒绝、resume前尝试读取结果目录不存在、北交所代码导致prepare失败、第二次resume拒绝。

- [ ] **Step 2: 运行RED测试**

Run: `PYTHONPATH=src ../../.venv/bin/python -m pytest -q tests/test_final_test_two_phase.py`

Expected: FAIL，因为CLI仍是一体化入口。

- [ ] **Step 3: 实现显式子命令**

```python
parser.add_argument("command", choices=("prepare", "resume"))
parser.add_argument("--root", type=Path, default=Path.cwd())
parser.add_argument("--data-root", type=Path, required=True)
```

`prepare`只接受opening token、批准密钥和attempt ID；`resume`只接受批准密钥、attempt ID、两类coverage和run ID。参数不属于所选子命令时由argparse拒绝。

- [ ] **Step 4: 更新阶段9主计划状态说明**

只记录两阶段实现及旧封印失效；Task 4–6和验收清单保持未勾选，直到真实最终测试完成。

- [ ] **Step 5: 运行GREEN、全量测试和ruff**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest -q
../../.venv/bin/ruff check .
git diff --check
```

Expected: 全部PASS，ruff输出`All checks passed!`，diff check无输出。

- [ ] **Step 6: 提交并完成任务级与整分支审查**

```bash
git add src/ashare_multifactor/cli/final_test.py tests/test_final_test_two_phase.py tests/test_final_test_pipeline.py docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md
git commit -m "feat(final-test): expose two-phase one-shot workflow"
```

任务审查通过后，对本计划全部提交做整分支审查；Critical和Important必须修复并复审，Minor必须记录处理结论。

### Task 6: 重新验证、双run、封印与授权检查点

**Files:**
- Generated: `processed/validation_evaluation/releases/<stage7-successor>`
- Generated: `artifacts/robustness/action_coverage_audit/<stage7-successor>`
- Generated: `processed/robustness/releases/<stage8-successor>`

**Interfaces:**
- Consumes: 全量测试通过且整分支审查Approved的干净Git身份。
- Produces: 新Stage7/8不可变successor、新sealed protocol和关闭状态opening ledger。

- [ ] **Step 1: 在干净身份下重新运行全量验证**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m pytest -q
../../.venv/bin/ruff check .
git status --short
```

Expected: pytest和ruff通过，Git状态无输出。

- [ ] **Step 2: Stage7双run和successor发布**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m ashare_multifactor.cli.validation reproduce \
  --root "$PWD" --run-id stage7_two_phase_<commit7>
PYTHONPATH=src ../../.venv/bin/python -m ashare_multifactor.cli.validation publish \
  --root "$PWD" --run-id <commit7>_stage7_validation_two_phase_successor
```

Expected: `outputs_identical`或`full_pipeline_reproducible`为true、`release_eligible`为true；新lineage明确supersede当前Stage7，旧release哈希不变，证券扫描无北交所代码。

- [ ] **Step 3: 重绑Stage8协议与公司行动覆盖审计**

将`configs/robustness_protocol.yaml`的`validation_release`更新为新Stage7 run ID，同步精确测试夹具并提交。以新Stage7 manifest和相同内容哈希的验证期公司行动重新生成ready audit；不得修改事件内容。

- [ ] **Step 4: Stage8双run和successor发布**

Run:

```bash
PYTHONPATH=src ../../.venv/bin/python -m ashare_multifactor.cli.robustness reproduce \
  --root "$PWD" --run-id stage8_two_phase_<commit7>
```

随后以本次robustness复现run ID和新action coverage audit根目录作为`successor_audit_root`，调用`publish_robustness_release`发布successor；两个实参都必须来自本步骤刚生成并核验的产物，不允许复用旧路径。

Expected: 两次核心文件哈希一致、`release_eligible=true`；新seal为protocol v2、`supported_markets=[sh,sz]`、`opening_token_status=closed`，旧Stage8 release哈希不变。

- [ ] **Step 5: 停在新授权点**

核验：

```text
processed/final_test/CURRENT.json 不存在
processed/final_test/attempts 不含新attempt
新seal对应opening ledger不存在
未读取2022–2025数据
```

向用户报告新Stage8 run ID、manifest SHA-256、lineage SHA-256和sealed protocol SHA-256，并取得新的精确开启确认。授权前不得运行`final-test prepare`。
