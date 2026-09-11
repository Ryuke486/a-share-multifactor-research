# 任务 02：建立共享可复现发布生命周期

**状态：** 已完成
**服务总目标：** 先用简单适配器证明一个小而深的三入口生命周期，再让任何真实阶段依赖它。

## 前置条件

- 用户明确授权“开始任务 02”。
- 任务 01 `HANDOFF.status=complete`，兼容测试在当前 worktree 仍通过。
- 已读取任务 01 的入口、字节与错误合同清单。

## 输入

- [权威设计](../../specs/2026-08-23-reproducible-release-lifecycle-design.md)
- 任务 01 新增的全部特征测试与固定快照
- `src/ashare_multifactor/audit/identity.py`
- `src/ashare_multifactor/audit/records.py`
- `src/ashare_multifactor/audit/publication.py`，只读参考

## 允许修改

- 新增 `src/ashare_multifactor/audit/reproducible_release.py`
- 新增 `tests/test_reproducible_release_lifecycle.py`
- 只在任务 01 测试本身有事实错误时修正测试，并在 `HANDOFF` 单独说明

## 本任务不修改

- `validation/`、`robustness/` 和两个 CLI；
- `audit/__init__.py` 与 `audit/publication.py`；
- 现有配置、数据、产物、发布和指针；
- Stage 6、Stage 9。

## 产出接口

内部模块只包含三个生命周期入口：

```python
run_once(adapter, code_root, *, run_id=None)
reproduce(adapter, code_root, *, run_id_prefix=None)
release(adapter, code_root, *, run_id, publish, options=None, certificate=None)
```

适配器协议只表达三项能力：

```python
bind(...)
execute(...)
prepare_release(...)
```

可使用少量不可变内部值对象承载绑定上下文和发布准备结果，但不得让调用者必须理解生命周期内部文件操作。

## 执行步骤（TDD）

- [x] 用两个简单测试适配器先写失败测试：一个模拟 Stage 7 兼容配置，一个模拟 Stage 8 兼容配置。
- [x] 覆盖 `run_once` 的新目录创建、适配器执行、运行中代码/输入漂移拒绝、失败目录清理和运行清单落盘。
- [x] 覆盖 `reproduce` 的不同运行 ID、固定第二运行源、代码/输入一致、核心文件逐哈希比较、dirty 发布资格和证书落盘。
- [x] 覆盖 `release` 必须重读或重建证书并重新核验认证源运行，不能信任可变摘要。
- [x] 覆盖 `publish=false` 只返回暂存结果；`publish=true` 才调用既有 `publish_release`。
- [x] 实现 Stage 7 v1 与 Stage 8 v1 两个内置兼容编码器，先只由测试适配器使用。
- [x] 保持直接 `Path`/`tmp_path` 文件系统；不得增加文件系统 port、注册表或插件加载。
- [x] 内部失败在边界处保持现有异常类别；不得建立公共异常层级。
- [x] 确认新模块没有从 `audit/__init__.py` 导出，也没有新增 CLI。
- [x] 运行生命周期测试、任务 01 特征测试和 audit publication 回归测试。

## 验收

- [x] 三入口覆盖成功、漂移、篡改、dirty、缺文件、目标已存在和执行失败清理路径。
- [x] 适配器协议只有 `bind`、`execute`、`prepare_release` 三项阶段能力。
- [x] 两个兼容配置由生命周期拥有，测试证明其差异。
- [x] `reproduce` 不发布，认证源固定为第二运行。
- [x] `release` 发布前重新核验源运行。
- [x] `audit.publication` 行为和文件零变化。
- [x] Stage 7/8 生产 pipeline 尚未接入，任务 01 特征测试仍通过。
- [x] 相关测试和 Ruff 通过。
- [x] 真实指针和历史发布零变化，最终测试指针仍不存在。

## 失败关闭

- 若需要第四个生命周期入口或第四个适配器能力才能完成，停止并回到设计评审；不得用临时钩子绕过三能力边界。
- 若只能通过修改 `audit.publication` 才能测试发布，改用注入可调用发布原语或测试替身；仍无法实现则停止。
- 若兼容编码器无法表达现有字节，记录差异并停止，不修改快照接受新格式。

# 强制停点

满足验收后停止。不得创建 Stage 7/8 适配器，不得修改 pipeline 或 CLI。

## HANDOFF

```yaml
task: 02-build-lifecycle-core
status: complete
baseline_handoff: 01-characterize-compatibility
files_changed:
  - src/ashare_multifactor/audit/reproducible_release.py
  - tests/test_reproducible_release_lifecycle.py
lifecycle_entry_points:
  - run_once
  - reproduce
  - release
adapter_capabilities:
  - bind
  - execute
  - prepare_release
compatibility_profiles:
  - stage7_v1
  - stage8_v1
verification:
  - lifecycle and compatibility baseline: 64 passed
  - related validation/robustness/publication suite: 157 passed
  - ruff check .: passed
  - git diff --check: passed
public_api_added: false
publication_module_changed: false
current_pointers_unchanged: true
next_task: 03-migrate-stage7
next_task_authorized: true
```
