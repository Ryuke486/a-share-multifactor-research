# Task 6 实现报告

## 结果

- 新增一次性 final-test 管线，串联授权、精确 2022–2025 数据构建、执行源生成/绑定、信号、回测、封存指标与报告、不可变 release 发布。
- `publishable=false` 只写入 attempt 产物、完整性清单和 append-only 结果，不切换 `CURRENT.json`。
- 成功发布后同时由 `CURRENT.json` 和 authoritative outcome 双重锁定，禁止第二次发布。
- lineage 绑定精确日期、开启批准、seal、Git commit/tree、上游 release/manifest、执行源、Python/依赖环境及 UTC 执行时间。
- release 内生成 `checkpoint_c.json`，明确停止开发、未进入交付。
- CLI 只暴露授权令牌、批准密钥、attempt/release 身份，无参数搜索或绕过授权入口。

## TDD 与验证

- RED 1：新测试因缺失 `final_test.pipeline` 失败。
- RED 2：执行源日期归一化接口缺失，测试导入失败。
- RED 3：删除 outcome 后仍应由 authoritative `CURRENT.json` 锁定，以及失败 attempt 缺少 manifest，两项测试失败。
- GREEN：Task 6 测试 4 项通过；final-test 相关集成测试 81 项通过。
- 最终全量结果：`pytest -q` 765 项通过；`ruff check .` 通过；`git diff --check` 通过。

## 边界与关注点

- 本任务未读取真实 `Data/` 2022–2025，未生成或消费真实开启令牌，未修改实际 `processed/final_test/CURRENT.json`。
- 真实运行会对最终股票集逐证券、逐年查询 BaoStock，成本较高；必须等待新的干净 Git 身份、Stage 7/8 successor 和新用户授权。
- `security_events.parquet` 使用封存 schema 显式生成；若正式执行前发现 2022–2025 合并/核销官方事件，必须在新授权前以官方证据完成封存，不得在看到测试结果后追加。
