# 板块8最终执行协议继任封印 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐2022–2025费用与公司行动输入契约，以2017–2021覆盖审计为硬门槛，生成可替代旧封印的板块8不可变继任协议。

**Architecture:** 费用区间验证、最终事件源契约、验证期覆盖审计和继任封印分别放在独立模块。板块8只绑定获取规则与审计证据，不读取最终行情或实际事件；板块9取得新授权后才生成attempt级事件manifest并交给阶段六执行器。

**Tech Stack:** Python 3.12+、Polars、PyYAML、pytest、现有manifest/lineage与release发布接口。

## Global Constraints

- 不读取或统计2022-01-01至2025-12-31的行情、因子、目标权重、成交或业绩。
- 不读取2026年数据。
- 不改变因子、候选组合、调仓规则、成本模型结构、预注册指标或样本划分。
- 不原地修改 `58adad4_stage8_robustness`。
- 不引入付费数据源。
- 任一输入缺失、冲突或哈希漂移时失败关闭。
- 未经用户明确说“可以提交了”，只保留工作区改动，不执行Git提交、推送或PR。

## Execution Status（2026-07-16）

- [x] Tasks 1–5 的代码、合成反例和相关回归完成；96项相关测试通过。
- [x] 首次2017–2021真实审计定位4条供应商日期异常，并完成官方证据绑定与精确更正实现。
- [x] 更正后完整开放期缓存复核：10,522条唯一事件、日期倒置0、业务主键重复0、4条官方更正全部生效。
- [x] 全量验证完成：738项测试通过，ruff通过。
- [ ] 在干净Git身份下执行板块7双run并发布不可变successor。
- [ ] 针对板块7successor生成ready覆盖审计，再执行板块8双run和successor发布。
- [ ] 取得新的板块9开启授权并生成attempt级2022–2025事件manifest。

真实发布依赖干净Git身份，因此当前按项目约束暂停在用户明确“可以提交了”的提交门前。

---

### Task 1: 完整费用区间与制度切换验证

**Files:**
- Modify: `configs/market_rules.yaml`
- Create: `src/ashare_multifactor/execution/fee_protocol.py`
- Test: `tests/test_fee_protocol.py`

**Interfaces:**
- Consumes: `FeeSchedule`、最终测试起止日期和市场集合。
- Produces: `validate_fee_protocol(schedule, start, end, markets) -> FeeProtocolEvidence`，包含边界费率与协议哈希所需的机器可读证据。

- [ ] **Step 1: 写费用边界失败测试**

```python
def test_final_fee_protocol_covers_both_policy_switches() -> None:
    schedule = load_market_rules(Path("configs/market_rules.yaml"))
    evidence = validate_fee_protocol(
        schedule, date(2022, 1, 1), date(2025, 12, 31), ("sh", "sz")
    )
    assert schedule.transfer_fee(date(2022, 4, 28), "sh", 1_000_000, 10_000) == 20
    assert schedule.transfer_fee(date(2022, 4, 29), "sh", 1_000_000, 10_000) == 10
    assert schedule.stamp_duty_rate(date(2023, 8, 27), "sell") == 0.001
    assert schedule.stamp_duty_rate(date(2023, 8, 28), "sell") == 0.0005
    assert evidence.coverage_end == date(2025, 12, 31)
```

- [ ] **Step 2: 运行测试并确认因现有区间截止2021而失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_fee_protocol.py`
Expected: FAIL，错误包含 `fee intervals do not cover` 或缺少 `fee_protocol` 模块。

- [ ] **Step 3: 补齐配置并实现严格验证器**

```python
@dataclass(frozen=True)
class FeeProtocolEvidence:
    coverage_start: date
    coverage_end: date
    markets: tuple[str, ...]
    checked_days: int


def validate_fee_protocol(
    schedule: FeeSchedule,
    start: date,
    end: date,
    markets: tuple[str, ...] = ("sh", "sz"),
) -> FeeProtocolEvidence:
    if start > end or set(markets) != {"sh", "sz"}:
        raise ValueError("invalid formal fee protocol bounds")
    checked = 0
    day = start
    while day <= end:
        for side in ("buy", "sell"):
            rate = schedule.stamp_duty_rate(day, side)
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("invalid stamp-duty rate")
        for market in markets:
            value = schedule.transfer_fee(day, market, 10_000.0, 1_000)
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid transfer fee")
        checked += 1
        day += timedelta(days=1)
    return FeeProtocolEvidence(start, end, markets, checked)
```

- [ ] **Step 4: 运行费用测试和既有执行规则回归**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_fee_protocol.py tests/test_execution_rules.py`
Expected: PASS。

- [ ] **Step 5: 记录工作区检查点，不提交**

Run: `git status --short configs/market_rules.yaml src/ashare_multifactor/execution/fee_protocol.py tests/test_fee_protocol.py`
Expected: 只显示本任务文件。

### Task 2: 冻结最终公司行动源与attempt manifest契约

**Files:**
- Create: `configs/final_execution_sources.yaml`
- Create: `src/ashare_multifactor/final_test/action_source_contract.py`
- Test: `tests/test_final_test_action_source_contract.py`

**Interfaces:**
- Consumes: `FinalTestAuthorization`、原始缓存根目录和预期证券集合。
- Produces: `FinalActionSourceContract`、`build_execution_input_manifest(...) -> dict[str, object]`、`verify_execution_input_manifest(...) -> dict[str, Path]`。

- [ ] **Step 1: 写允许域名、日期范围和哈希绑定失败测试**

```python
def test_manifest_binds_attempt_seal_period_and_hashed_files(tmp_path: Path) -> None:
    action = tmp_path / "corporate_actions.parquet"
    event = tmp_path / "security_events.parquet"
    pl.DataFrame(schema={"effective_date": pl.Date}).write_parquet(action)
    pl.DataFrame(schema={"effective_date": pl.Date}).write_parquet(event)
    manifest = build_execution_input_manifest(
        tmp_path / "manifest.json",
        authorization=_authorization(),
        files={"corporate_actions.parquet": action, "security_events.parquet": event},
    )
    assert manifest["attempt_id"] == "attempt-001"
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
```

- [ ] **Step 2: 运行测试并确认模块缺失导致失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_final_test_action_source_contract.py`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现不可变源契约与manifest校验**

```python
@dataclass(frozen=True)
class FinalActionSourceContract:
    start: date
    end: date
    query_year_type: str
    allowed_url_prefixes: tuple[str, ...]
    required_files: tuple[str, ...]


def verify_execution_input_manifest(
    path: Path, authorization: FinalTestAuthorization
) -> dict[str, Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "attempt_id": authorization.attempt_id,
        "sealed_protocol_sha256": authorization.sealed_protocol_sha256,
        "period": ["2022-01-01", "2025-12-31"],
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("execution-input manifest differs from authorization")
    return _verify_required_hashed_files(path.parent, payload["files"])
```

配置只允许BaoStock结构化查询与巨潮、上交所、深交所官方静态公告前缀，查询年限定2022–2025且 `yearType=operate`。

- [ ] **Step 4: 验证路径逃逸、非官方域名、缺失文件和哈希漂移均失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_final_test_action_source_contract.py`
Expected: PASS。

- [ ] **Step 5: 记录工作区检查点，不提交**

Run: `git status --short configs/final_execution_sources.yaml src/ashare_multifactor/final_test/action_source_contract.py tests/test_final_test_action_source_contract.py`
Expected: 只显示本任务文件。

### Task 3: 2017–2021公司行动元数据覆盖审计

**Files:**
- Modify: `src/ashare_multifactor/validation/corporate_action_source.py`
- Modify: `tests/test_validation_corporate_action_source.py`
- Create: `src/ashare_multifactor/final_test/action_coverage_audit.py`
- Test: `tests/test_action_coverage_audit.py`
- Modify: `src/ashare_multifactor/robustness/pipeline.py`

**Interfaces:**
- Consumes: BaoStock查询coverage元数据、标准化候选事件、板块7已发布公司行动、官方证据index。
- Produces: `ActionCoverageAudit`、`audit_validation_action_coverage(...) -> ActionCoverageAudit` 与五个机器可读审计表。

- [ ] **Step 1: 写100%查询覆盖和零差异硬门槛失败测试**

```python
def test_action_audit_requires_every_symbol_year_query_and_zero_diffs() -> None:
    with pytest.raises(ValueError, match="query coverage"):
        audit_validation_action_coverage(
            symbols=["000001"],
            query_coverage=pl.DataFrame({"symbol": ["000001"], "year": [2017], "status": ["ok"]}),
            candidate_actions=_actions(),
            published_actions=_actions(),
            official_evidence=_evidence(),
        )
```

- [ ] **Step 2: 运行测试并确认模块缺失导致失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_action_coverage_audit.py`
Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现覆盖、主键、冲突、账本与官方抽样门禁**

```python
@dataclass(frozen=True)
class ActionCoverageAudit:
    status: str
    summary: dict[str, object]
    query_coverage: pl.DataFrame
    event_differences: pl.DataFrame
    official_evidence_index: pl.DataFrame


def audit_validation_action_coverage(...) -> ActionCoverageAudit:
    expected = pl.DataFrame(
        {"symbol": [s for s in sorted(set(symbols)) for _ in range(5)],
         "year": list(range(2017, 2022)) * len(set(symbols))}
    )
    missing = expected.join(query_coverage, on=["symbol", "year"], how="anti")
    if missing.height:
        raise ValueError("validation action query coverage is incomplete")
    differences = _compare_action_keys(candidate_actions, published_actions)
    if differences.height:
        raise ValueError("validation corporate-action differences are not zero")
    _assert_official_sample_coverage(candidate_actions, official_evidence)
    return ActionCoverageAudit("ready", summary, query_coverage, differences, official_evidence)
```

BaoStock缓存写入时同步生成逐 `symbol × year` 的 `.coverage.parquet`，字段固定为 `symbol/year/status/row_count`；成功的零事件响应必须记录为 `status=ok, row_count=0`。

- [ ] **Step 4: 将审计产物加入板块8输入身份和run目录**

`robustness.pipeline` 在执行实验前验证audit状态为 `ready`，并把summary、diff、coverage、evidence index及其SHA-256加入run manifest。任一文件缺失或状态非ready时拒绝seal。

- [ ] **Step 5: 运行覆盖审计和板块7公司行动回归**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_action_coverage_audit.py tests/test_validation_corporate_action_source.py tests/test_validation_corporate_actions.py`
Expected: PASS。

### Task 3A: 官方日期异常与板块7不可变更正successor

**Files:**
- Create: `configs/validation_corporate_action_corrections.csv`
- Create: `configs/evidence/validation_action_corrections/*.json`
- Modify: `src/ashare_multifactor/validation/corporate_actions.py`
- Modify: `src/ashare_multifactor/validation/corporate_action_source.py`
- Modify: `src/ashare_multifactor/validation/full_run.py`
- Modify: `src/ashare_multifactor/validation/pipeline.py`
- Test: `tests/test_validation_backtest.py`
- Test: `tests/test_validation_corporate_action_source.py`
- Test: `tests/test_validation_pipeline.py`

- [x] 用失败测试覆盖除权日和支付日的窄范围官方更正。
- [x] 绑定4份官方公告结构化快照、URL和SHA-256。
- [x] 完整2017–2021缓存复核达到日期倒置0和唯一业务键重复0。
- [x] 板块7发布逻辑绑定并复验旧CURRENT、manifest和lineage哈希。
- [ ] 干净提交后完成两次板块7全流程运行并发布successor。
- [ ] 对新板块7successor重跑覆盖审计并确认差异为0。

### Task 4: 不可变板块8继任封印

**Files:**
- Create: `src/ashare_multifactor/robustness/successor_seal.py`
- Modify: `src/ashare_multifactor/robustness/test_protocol.py`
- Modify: `src/ashare_multifactor/robustness/pipeline.py`
- Test: `tests/test_stage8_successor_seal.py`
- Modify: `tests/test_sealed_test_protocol.py`

**Interfaces:**
- Consumes: 旧 `CURRENT.json`、旧release manifest、新费用证据、源契约哈希、覆盖审计哈希和双run复现结果。
- Produces: `Stage8Supersession`、protocol version 2 seal、successor lineage和新的CURRENT指针。

- [ ] **Step 1: 写旧release不可变与新seal绑定测试**

```python
def test_successor_seal_preserves_predecessor_and_binds_execution_contracts(tmp_path: Path) -> None:
    before = sha256_file(predecessor_manifest)
    supersession = build_stage8_supersession(
        predecessor_pointer=pointer,
        predecessor_manifest=predecessor_manifest,
        reason="final execution fee and corporate-action coverage incomplete",
    )
    sealed = seal_test_protocol(
        destination,
        protocol=protocol,
        code_identity=_identity(),
        validation_pointer=validation_pointer,
        market_rules_sha256="a" * 64,
        action_source_contract_sha256="b" * 64,
        action_coverage_audit_sha256="c" * 64,
        predecessor=supersession.as_dict(),
        report_template_sha256="d" * 64,
        opening_ledger_root=tmp_path / "ledger",
        gate={"sealed_test_protocol_allowed": True},
    )
    assert sha256_file(predecessor_manifest) == before
    assert sealed["protocol_version"] == 2
```

- [ ] **Step 2: 运行测试并确认新接口缺失导致失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_stage8_successor_seal.py tests/test_sealed_test_protocol.py`
Expected: FAIL，缺少 `successor_seal` 或新参数。

- [ ] **Step 3: 实现继任血缘和seal v2**

```python
@dataclass(frozen=True)
class Stage8Supersession:
    run_id: str
    manifest_sha256: str
    reason: str
    status: str = "superseded_for_final_execution"


def build_stage8_supersession(...) -> Stage8Supersession:
    if pointer["manifest_sha256"] != sha256_file(predecessor_manifest):
        raise ValueError("predecessor Stage-8 manifest changed")
    return Stage8Supersession(pointer["run_id"], pointer["manifest_sha256"], reason)
```

seal v2必须绑定 `action_source_contract_sha256`、`action_coverage_audit_sha256` 和predecessor身份；旧seal的HMAC签名不能通过新seal验证。

- [ ] **Step 4: 修改发布流程只允许完整successor更新CURRENT**

发布前依次复验：干净Git身份、双run相同、旧manifest未变、费用全覆盖、覆盖审计ready、全部新哈希存在。失败时删除staging但保留旧CURRENT。

- [ ] **Step 5: 运行继任发布相关测试**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_stage8_successor_seal.py tests/test_sealed_test_protocol.py tests/test_robustness_pipeline.py`
Expected: PASS。

### Task 5: 板块9任务4消费新事件manifest

**Files:**
- Modify: `src/ashare_multifactor/final_test/backtest.py`
- Modify: `tests/test_final_test_backtest.py`
- Modify: `docs/superpowers/plans/2026-07-15-09-one-shot-final-test.md`

**Interfaces:**
- Consumes: protocol v2 `FinalTestAuthorization`、Task 2验证过的execution-input manifest、Task 3 target weights和final daily panel。
- Produces: 阶段六执行器所需的只读输入对象；在实际事件manifest尚未生成时仍保持fail-closed。

- [ ] **Step 1: 写manifest哈希漂移和旧seal拒绝测试**

```python
def test_backtest_rejects_old_seal_and_tampered_action_manifest_before_execution(...):
    manifest = _write_valid_execution_manifest(...)
    manifest_action = manifest.parent / "corporate_actions.parquet"
    manifest_action.write_bytes(b"tampered")
    result = run_final_test_backtest(...)
    assert result.publishable is False
    assert result.preflight["execution_started"] is False
    assert "hash mismatch" in " ".join(result.gate_failures)
```

- [ ] **Step 2: 运行测试并确认当前实现只检查文件名而失败**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_final_test_backtest.py`
Expected: FAIL，篡改文件未被当前检查捕获。

- [ ] **Step 3: 用Task 2校验器替换本地弱manifest检查**

`backtest.py` 必须先验证authorization与seal v2，再调用 `verify_execution_input_manifest`；只在哈希、日期、schema和attempt绑定全部通过后解析Parquet并启动撮合。此任务不下载2022–2025事件，也不消费令牌。

- [ ] **Step 4: 更新板块9状态与阻断条件**

计划记录：代码路径已准备；实际Task 4继续等待干净successor release、新用户授权和attempt级事件manifest，不把测试实现标为最终回测已完成。

- [ ] **Step 5: 运行全部相关测试与ruff**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_fee_protocol.py tests/test_final_test_action_source_contract.py tests/test_action_coverage_audit.py tests/test_stage8_successor_seal.py tests/test_final_test_backtest.py tests/test_sealed_test_protocol.py tests/test_execution_rules.py tests/test_validation_corporate_actions.py tests/test_validation_corporate_action_source.py`
Expected: PASS。

Run: `.venv/bin/python -m ruff check src tests`
Expected: `All checks passed!`

### Task 6: 全量验证与真实板块8继任发布准备

**Files:**
- Modify: `.superpowers/sdd/progress.md`
- Create: `.superpowers/sdd/stage8-successor-preparation-report.md`

**Interfaces:**
- Consumes: Tasks 1–5实现与测试结果。
- Produces: 可供用户批准提交的完整差异、测试证据和真实发布命令清单。

- [ ] **Step 1: 运行完整测试**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q`
Expected: 全部PASS且无collection错误。

- [ ] **Step 2: 运行完整ruff**

Run: `.venv/bin/python -m ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 3: 验证未读取最终测试数据**

检查最终测试attempt ledger、`processed/final_test/CURRENT.json` 和2022–2025新产物均不存在；记录所有检查路径和结果。

- [ ] **Step 4: 暂停在提交门前**

由于真实板块8双run与successor release要求干净Git身份，向用户汇报已完成的实现与验证，并请求明确的“可以提交了”。在得到该授权前不提交、不运行真实release发布，也不获取最终期事件数据。
