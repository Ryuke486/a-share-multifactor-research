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
- GREEN：本轮交接审计相关测试 71 项通过。
- 最终全量结果：`pytest -q` 807 项通过；`ruff check src tests` 通过；`git diff --check` 通过。

## 边界与关注点

- 本任务未读取真实 `Data/` 2022–2025，未生成或消费真实开启令牌，未修改实际 `processed/final_test/CURRENT.json`。
- 真实运行会对最终股票集逐证券、逐年查询 BaoStock，成本较高；必须等待新的干净 Git 身份、Stage 7/8 successor 和新用户授权。
- `security_events.parquet` 使用封存 schema 显式生成；若正式执行前发现 2022–2025 合并/核销官方事件，必须在新授权前以官方证据完成封存，不得在看到测试结果后追加。

## 审查修复追加

- 组合指标已改为期初边界 NAV 加研究/验证/测试严格切片；研究和验证均来自 Stage 7 同一封存候选的连续账本。
- 删除无证据的空安全事件假设；即使零事件也必须提供 `ready` 官方证据、全证券范围哈希和查询覆盖记录，CLI 强制显式传入。
- 发布改为 `prepared -> CURRENT -> succeeded` 两阶段，可从与 release lineage 一致的 `CURRENT+prepared` 恢复。
- attempt/release ID、根目录、指针和复制输入增加路径逃逸与 symlink 拒绝。
- 失败后的新 attempt 只能在同 seal、Git 和完全相同 raw inventory 下复用已发布面板；执行输入改为 attempt 级绑定。
- 权威 release 内自包含 final daily panel、raw inventory、BaoStock 原始响应/覆盖、官方证据、安全事件覆盖和执行 manifest，lineage 与 release manifest 双重绑定。

## 复审追加

- security-event coverage 升级为可机器解析的规范表，对 final symbol 逐一校验唯一成功查询、精确期间、市场/来源、事件数和官方证据索引；回测加载前再次验证。
- 路径保护扩展到 attempts、attempt_inputs、execution_input_sources、data-reuse、failed-claims 和 data-recovery，复用源目录也拒绝 symlink/逃逸。
- 无面板的 failed claim 会原子归档后允许新 attempt 重建；`publishing+完整面板` 验证 manifest 后恢复为 published 并写入不可变恢复事件。
- orders 按首个实际订单事件日归期，并按期末最后事件提取每个 order_id 的唯一终态，避免跨年误归期和未成交重复计数。

## 第三轮复审追加

- security events 与 coverage 按 `(source_symbol, market, source)` 做逐键精确对账；拒绝 coverage 外事件、缺失事件、数量错配及 evidence/source/market 不一致。
- coverage 入口先解析为规范绝对路径；manifest、证据、coverage 和事件支持文件必须位于同一规范根内，整条路径不得包含 symlink；相对 CLI 路径已有回归覆盖。
- `publishing` 恢复改为 append-only 两阶段审计：原子写 `prepared`（绑定 claim/data manifest 哈希），原子切换 claim，再原子写 `completed`；可从 `prepared+publishing` 或 `prepared+published` 幂等续跑。

## 第四轮复审追加

- 新增唯一的六位证券代码到市场映射，覆盖沪、深、北交易所边界；coverage 必须由 `source_symbol` 推导并精确匹配 `market`，再校验 `market/source`，拒绝 `000001/sh/sse` 等自洽但错误的组合。
- prepared recovery 绑定 attempt、approval、Git commit/tree、seal、robustness release 与 data manifest；恢复 publishing claim 时重新计算当前文件 SHA256 并以 constant-time 比较，任一身份字段或 claim 内容漂移均 fail-closed。

## 第五轮复审追加

- 封存执行源合同正式加入北交所：仅允许 `https://www.bse.cn/disclosure/` 官方前缀，并冻结 `sh/sse`、`sz/szse`、`bj/bse` 三组市场来源；合同文件变化继续由协议 SHA256 绑定。
- coverage、官方 evidence、security events 与 BaoStock 查询前缀统一复用六位证券代码市场映射；`920`、`8`、`4` 边界均按北交所处理，不再落入旧的沪深首位分支。
- 新增 BJ/BSE 成功、错误交易所、错误来源 URL 和删除 BSE 合同条目导致 hash/合同校验失败的合成反例；未读取真实最终测试数据。

## 用户确认后的沪深范围冻结

- 用户明确选择排除北交所后，研究协议新增有序 `supported_markets: [sh, sz]`；ResearchConfig、因子研究设置及协议哈希共同绑定该范围。
- 六位证券代码市场映射保留沪、深、北通用能力，但 MVP 与完整因子研究的动态股票池在历史计数和流动性排名前即过滤市场；研究、验证和最终信号共享同一路径。
- final execution 合同改为仅支持沪深；coverage、事件证据和最终目标发现 `4/8/92` 北交所证券立即 fail-closed，执行源查询范围只取最终沪深 symbol 全集。
- seal、Stage 8 lineage、final lineage 和最终报告均记录 `[sh, sz]`，授权 gate 同时核对研究配置、seal 与 successor lineage，范围漂移不能继续执行。

## 历史交接内容审计

- 新增通用 `assert_supported_markets` 内容断言；与股票池过滤不同，已发布或上游文件发现范围外证券会带交接 label、字段和证券代码立即失败，不会静默删除或改写旧 release。
- Stage 7 在读取 Stage 5 targets、Stage 6 execution/actions/events，以及发布新 targets、continuous targets、execution/actions/events 和含 symbol 的连续账本前均执行断言。
- Stage 8 执行实验及发布/seal 消费 validation release inputs 时重复检查；final signal 与 backtest handoff 再检查 Stage 7 daily、targets、execution、actions 和 events。
