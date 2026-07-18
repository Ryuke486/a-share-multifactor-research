# 板块9官方查询覆盖证据实施计划

> 已获用户批准：将CNInfo公告查询端点仅用于逐证券官方查询覆盖；实际公司行动与证券事件仍需逐行公告/PDF证据。不得改变策略、参数、成本、指标或样本边界。

## Task 1：封存当前无结果attempt并修复归档兼容性

**文件：** `final_test/incident_archive.py`、`final_test/preparation.py`、`tests/test_final_test_incident_archive.py`

- [x] 写入“已发布两阶段准备目录可整体归档”的失败反例。
- [x] 仅当状态中存在准备清单哈希时验证并允许唯一的当前attempt准备目录；未绑定准备目录继续拒绝。
- [x] 将`stage9-final-20260718-b9c615a`记为非权威失败并归档，保留其全部证据和失败原因。
- [x] 运行该任务相关回归、全量pytest与ruff后提交。

## Task 2：定义官方查询覆盖缓存与严格验证器

**文件：** 新建聚焦的`final_test/official_query_coverage.py`、`tests/test_final_test_official_query_coverage.py`

- [x] 先写反例：错误端点/方法、路径逃逸、缺页、重复页、响应哈希或总页数不一致均失败。
- [x] 实现规范请求摘要、页缓存身份和分页完成验证；不实现策略或事件经济字段解析。
- [x] 相关测试与ruff通过。

## Task 3：将查询覆盖角色接入执行源合同

**文件：** `configs/final_execution_sources.yaml`、`action_source_contract.py`、`corporate_action_coverage.py`、`execution_sources.py`及相应测试。

- [ ] 先写反例：查询端点不能被当作逐行事件证据；非零事件缺公告/PDF仍失败；零事件缺完整查询包失败。
- [ ] 新增受限`official_query_coverage`角色和逐证券×类别覆盖验证，保持原事件URL白名单不变。
- [ ] 相关测试、全量pytest和ruff通过。

## Task 4：实现可恢复的CNInfo查询采集入口

**文件：** 新建小型采集模块/CLI子命令及测试。

- [ ] 先用HTTP替身测试重试、超时、分页、持久化、断点恢复和无密钥落盘。
- [ ] 采集器只接受prepare发布的证券范围，只写attempt专属证据缓存，不读取因子/回测输出。
- [ ] 针对小范围官方查询做只读连通性验证；不将网络原始响应提交Git。

## Task 5：独立审查、重新双run并发布新封印

- [ ] 审查来源角色、路径安全、哈希链、零事件语义和未放宽事件事实门禁。
- [ ] 运行全量pytest、ruff、Stage7双run与successor发布。
- [ ] 重绑Stage8输入，运行Stage8双run、发布successor和新seal。
- [ ] 验证旧attempt和旧release未改写，最终测试根保持关闭，随后请求新的用户授权。

## Task 6：新attempt、官方证据、resume与检查点C

- [ ] 获得用户对新seal的单独授权；运行新的`prepare`，冻结新证券范围。
- [ ] 采集并验证全部证券的查询覆盖及非零事件公告/PDF证据。
- [ ] 同一attempt运行`resume`一次，发布并审计最终release。
- [ ] 完成检查点C，更新板块9状态并明确判断是否可开启板块10。
