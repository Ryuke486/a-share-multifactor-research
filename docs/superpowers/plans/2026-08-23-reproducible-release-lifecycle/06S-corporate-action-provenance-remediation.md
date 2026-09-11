# 任务 06S：公司行动官方来源优先级整改

**状态：** 已完成

## 授权与目标

- 用户已授权：先尝试找回历史 BaoStock 原始字节；若失败，再尝试生产数据契约整改。
- 路线 1 已在本机、系统临时根、UUID 匹配的迁移外盘及其 artifacts/processed 归档目录中完成只读搜索，未发现 177037 字节或 SHA-256 `ffdfdc8dd0c4835009da53a69ad88c5984d34407d7612887867e4a3006048bab` 的文件字节。
- 本任务的最小修复目标：当供应商同时返回“已修正行”和“被官方证据修正后映射到同一经济事件的旧行”时，只保留官方证据更强的来源谱系，不改变金额、日期、股份比例或成交账务。

## 测试缝隙

唯一行为缝隙是公开接口 `load_validation_corporate_actions()`：

1. 输入包含同一公司行动的供应商修正行与旧异常行；
2. 旧异常行有已冻结 CNInfo 修正证据；
3. 输出必须只有一个经济事件，保留 `+cninfo_action_correction` 来源；
4. 用修复后的实际 BaoStock 原始文件重建全表，与两个认证运行进行全字段多重集比较。

## 边界

- 允许修改 `validation/corporate_actions.py`、相关行为测试、本任务书和审计记录。
- 不修改 `Data/`、历史 release、`audit.publication`、公共 CLI、因子、目标权重、费用或样本划分。
- 不读取 2022–2025 结果，不进入 Stage 9。
- 不提交、合并、推送、真实发布或切换 `CURRENT.json`。

## 验收顺序

- [x] 红灯：供应商新旧两行规范化到同一经济键时，旧实现错误保留弱来源。
- [x] 绿灯：显式的官方证据优先级去重，现金与送转事件均有公开接口回归。
- [x] 真实重建的 `corporate_actions.parquet` 与两个认证运行 26445 行全字段零差异。
- [x] Stage 7 `publish=false` 影子暂存通过，98 个研究、回测、账务及核心输出哈希零差异。
- [x] Stage 8 successor `publish=false` 影子验收通过；核心文件与两棵 successor 证据树一致，opening token 为 `closed`。
- [x] 完整 pytest、Ruff 与差异检查通过。

机器可读证据：

- `docs/audits/2026-08-27-candidate1-task6s-route-assessment.json`
- `docs/audits/2026-08-27-candidate1-task6s-external-source-revision-waiver.json`

# 强制停点

本任务只能完成临时根中的整改验证和 `publish=false` 暂存。真实发布、切换指针、提交、合并、推送或 Stage 9 均不在授权范围。

## HANDOFF

```yaml
task: 06S-corporate-action-provenance-remediation
status: complete
route_1_historical_bytes_recovered: false
route_1_external_volume_uuid: 830826CA-FDDD-33CE-9DF6-A997F38B741B
route_2_consumer_equivalence: passed
consumer_semantic_differences: 0
stage7_shadow_staging: passed; 98 core files; 0 hash mismatches
stage8_shadow_staging: passed; successor trees exact; opening_token_status=closed
focused_pytest: 67 passed
full_pytest: 1606 passed, 1 skipped in 532.65s
ruff: passed
dual_axis_review_open_findings: 0
waiver: docs/audits/2026-08-27-candidate1-task6s-external-source-revision-waiver.json
publication_performed: false
candidate_1_complete: true
```
