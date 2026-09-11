# 候选 1：可复现发布生命周期执行地图

**状态：** 任务 01–06 及 06S 已完成；06R 历史失败保留。2026-09-11 已同步到主仓库本地工作树，详见 `docs/audits/2026-09-11-candidate1-remediation.md`（主仓库）。
**权威设计：** [Stage 7–8 可复现发布生命周期设计](../../specs/2026-08-23-reproducible-release-lifecycle-design.md)

## 目标

把候选 1 拆成 6 个有序、可独立启动和独立收口的实施任务。每次对话只执行用户明确点名的一份任务文件，并在其强制停点结束。

## 固定边界

- 范围仅为 Stage 7–8 的双运行、认证、发布准备和既有发布调用编排。
- 严格兼容现有函数、参数、CLI、返回值、文件字节和错误合同。
- Stage 7/8 的历史差异原样保留。
- 最终测试期继续封存；不得读取或运行 Stage 9。
- `Data/`、现有发布、现有运行证据和 `CURRENT.json` 作为只读基线。
- 在独立 worktree 实施；本流程不授权提交、合并、推送或发布。
- 任何任务失败都保留失败测试和诊断证据，不用下一任务掩盖当前缺口。

## 启动前公共检查

每个任务开始时都必须：

1. 完整阅读 `AGENTS.md`、权威设计和本执行地图；
2. 阅读本任务文件及其所有前置任务 `HANDOFF`；
3. 检查主仓库与 worktree 状态、当前提交和 `origin/main` 关系；
4. 核验 Stage 7/8 `CURRENT.json`、release manifest 与 lineage 一致；
5. 确认 `processed/final_test/CURRENT.json` 不存在；
6. 解析可用的项目 Python 环境；缺少依赖时报告阻塞，不用系统解释器伪造通过；
7. 记录本任务输入、输出、不触碰范围和停止条件。

## 统一命令口径

任务开始时把实际可用的项目解释器绝对路径记录为 `CANDIDATE1_PYTHON`。正式测试统一从候选 1 worktree 根运行：

```bash
PYTHONPATH=src "$CANDIDATE1_PYTHON" -m pytest <本任务测试路径> -q
PYTHONPATH=src "$CANDIDATE1_PYTHON" -m pytest
"$CANDIDATE1_PYTHON" -m ruff check .
```

- 任务 01–04 先运行本任务相关测试；是否运行全量测试由各任务验收决定。
- 任务 05–06 必须运行完整 pytest 和 Ruff。
- 解释器或依赖不可用时，把任务收口为 `blocked`，记录缺失项和已完成的只读检查；不得改用缺依赖的系统 Python 生成虚假绿灯。

## 任务序列

| 任务 | 文件 | 前置 | 本任务完成标志 |
|---:|---|---|---|
| 01 | [锁定兼容行为](01-characterize-compatibility.md) | 无 | 旧入口和字节口径由测试固定 |
| 02 | [建立共享生命周期](02-build-lifecycle-core.md) | 01 | 三入口模块通过简单适配器测试 |
| 03 | [迁移 Stage 7](03-migrate-stage7.md) | 02 | Stage 7 外观委托且完全兼容 |
| 04 | [迁移 Stage 8](04-migrate-stage8.md) | 03 | Stage 8 外观委托且差异保留 |
| 05 | [删除重复编排](05-remove-duplication.md) | 04 | 共享编排只有一个实现来源 |
| 06 | [最终与影子验证](06-verify-and-handoff.md) | 05 | 全量、历史读取与影子验证闭环 |

依赖是严格串行的。后续任务可见不构成开始授权；前一任务缺少合格 `HANDOFF` 时，下一任务必须失败关闭。

## Worktree 规则

- 第一次获准实施时，在主仓库外观检查完成后创建一个 `codex/` 前缀的候选 1 专用分支和 `.worktrees/` 下的隔离 worktree。
- 6 个任务复用同一 worktree，除非发生必须隔离的新失败分支；不得在 `main` 直接实现。
- 每项任务开始前记录 worktree HEAD 和 dirty 文件；只修改本任务允许的文件。
- 任务文件中的“建议提交点”只是可审查边界，不是提交授权。

## 统一验收口径

- 固定身份、输入、时间和资源值时，运行清单、证书、lineage 与错误行为字节兼容。
- 真实新运行允许代码身份及派生血缘改变，领域核心结果必须等价。
- 历史发布保持可读且不可变。
- Stage 7/8 真实 `CURRENT.json` 在整个候选 1 实施期间不切换。
- 任务 06 之前不做真实全量研究重跑；任务 06 也只做临时根影子认证和暂存。

# 强制停点

本文件只负责导航，不授权执行任何任务。用户必须明确说“开始任务 01”或点名对应任务文件后，代理才能进入该任务。

## HANDOFF

```yaml
status: complete
next_authorized_task: null
required_user_action: null
implementation_started: true
final_test_opened: false
current_pointer_switch_authorized: false
commit_merge_push_authorized: false
```

