# 将延期的最终测试代码移出 main（方案 A）

## 目标

v1.0 已把 Stage 9–10 归档为可选 v2.0，但 `final_test/` 及其 CLI 仍占源码约 63%，使仓库的主体看起来是公告采集、证据封存与事故恢复，而不是因子研究。本任务把只服务于延期最终测试的代码从 main 移除，以 Git 标签完整保存，并保证一次 `git revert` 可逐字节恢复。

用户于 2026-10-05 选择方案 A，并授权推送存档标签、移除 `pypdf` 依赖。本任务推翻了[2026-09-11 步骤 3](2026-09-11-repository-streamlining/03-v1-boundaries.md)中"不能直接删除 final_test/"的约束，理由见下文"封存门禁的现状"。

## 封存门禁的现状（决策依据）

`final_test/gate.py` 的 `authorize_final_test` 在开启最终测试前逐一核对 Stage 8 封印记录中的 227 个源码文件哈希、Python 版本和依赖版本，任何不一致都会以"refreezing is forbidden"拒绝。2026-10-05 核对时，main 已经无法通过该门禁：

- 6 个被封印源码在 2026-09-11 的 `1a9feb1`、`cdf4f3d` 中改变；
- 运行环境从 CPython 3.14.6 / polars 1.42.1 升级到 3.14.7 / 1.43.2，numpy、scipy、matplotlib 也有变化。

2026-09-11 边界验收的"不需要新封印"只比较了三个域身份，没有覆盖门禁的整树核对。因此，无论是否移除代码，v2.0 开启最终测试都需要新的 Stage 8 封印（独立授权），或回到封印提交 `a1076c2` 并重建 3.14.6 环境。移除代码不会让 main 失去它当前仍具备的能力。

## 存档

- 标签 `archive/stage9-final-test` 指向 `d7c2828`（移除前最后一个含最终测试代码的 main 提交），已推送到 GitHub。
- 移除放在单独一个提交中；`git revert <该提交>` 即可恢复全部文件。

## 范围

移除：

- `src/ashare_multifactor/final_test/`、`cli/final_test.py`、`cli/final_test_review.py`、`ashare-final-test` 命令入口；
- `ashare-robustness` 的 `rehearse-evidence`、`publish-evidence-successor` 命令；
- 只能经由最终测试门禁或上述两个命令到达的 `robustness` 模块：`change_impact`、`evidence_workflow_readiness`、`evidence_workflow_rehearsal`、`evidence_workflow_successor_release`；
- 对应测试：`test_final_test_*.py`、`test_final_root_binding_lifecycle.py`，以及混合测试中依赖上述代码的用例（逐个判断，不误删 v1.0 覆盖）；
- `pypdf` 依赖（`pyproject.toml` 与锁定文件）。

保留：

- 其余 `robustness` 模块（Stage 8 流水线、发布适配、封印与审计读取）；
- `configs/final_execution_sources.yaml`、`docs/templates/stage9-final-test-report-template.md` 等 Stage 8 复现仍读取的输入；
- `protocol_identities.py` 中的历史源码路径清单（历史合同，不改写）；
- 全部 `processed/`、`artifacts/`、release、`CURRENT.json`、attempt、token 与历史文档。

## 验证

- [x] 基线：Stage 5–8 `CURRENT.json` 哈希、最终测试 `CURRENT.json` 不存在、补充产物哈希。
- [x] 边界测试改为：`final_test` 包不存在；v1.0 入口（含 `ashare-robustness`）不加载任何最终测试模块。
- [x] 全量 pytest、Ruff、`git diff --check`、`ashare-delivery check --verify-sources`。
- [x] 补充步骤重跑，产物与基线逐字节一致；Stage 5–8 指针不变；最终测试 `CURRENT.json` 仍不存在。
- [x] 恢复演练：在临时工作树对移除提交执行 `git revert`，完整测试套件恢复到移除前的数量并通过。
- [x] CI 增加 Linux 腿：首次以不阻塞方式运行（run 37326943900），macOS 与 Linux 的 Python 3.12/3.14 四条腿全部通过（各 861 项测试、Ruff、交付检查），随后 Linux 腿改为必须通过。

## HANDOFF（执行后更新）

```yaml
task: archive-final-test-code
status: complete
archive_tag: archive/stage9-final-test -> d7c2828 (pushed)
removal_commit: 7f09a10
removed: 142 files, 70,201 lines (final_test package, two CLIs, two robustness CLI commands, four v2-only robustness modules, final-test tests, pypdf)
tests_after: 861 passed in 69 s (1,680 before)
restore_drill: git revert 7f09a10 in a temporary worktree; restored paths identical to d7c2828; full suite 1,679 passed, 1 skipped in 439 s
delivery_check_verify_sources: passed (268 results)
supplement_outputs: byte-identical to the pre-removal baseline (16 files)
current_pointers_unchanged: true
final_test_current_absent: true
linux_ci: run 37326943900 passed on ubuntu-latest (Python 3.12: 861 passed in 113 s; Python 3.14: 861 passed in 159 s) and on macos-latest; continue-on-error removed, all four legs required
```
