# 检查点B第三次审计：公司行动多证券重建边界

**审计日期：** 2026-07-15  
**审计对象：** `main`  
**审计提交：** `e967df7`  
**当前权威release：** `de57558_stage6_final`  
**manifest SHA-256：** `bda056f61985e8f1c116f0e4835297ce80bb692327af0f9769ff6575de523368`  
**审计结论：** `blocked`——暂不得开启板块7。

## 1. 本轮已通过项目

- 阶段六已通过合并提交 `fbf2674` 进入 `main`；
- 全量测试：`588 passed in 68.14s`；
- `ruff check src tests` 通过；
- `CURRENT.json` 指向 `de57558_stage6_final`；
- release已记录替代 `f358569_stage6_remediated` 及原因；
- 研究期仍为2005–2016，核心数据中2017年及以后记录为0；
- 完整成本账本最大逐日对账差约 `5.96e-8` 元；
- 三场景最大逐日对账差约 `2.38e-7` 元；
- 后复权影子NAV最大单日绝对偏差约0.3741%，低于0.5%门槛；
- 未解释长期stale证券为0；
- 单证券“公司行动日没有受影响pending时不再平衡”反例已有测试；
- pending买单、pending卖单、部分成交、现金分红应收、同日新信号、同证券多个公司行动、整手及未来字段不变性已有专项测试。

上述结果说明阶段六的发布、账务、样本封存和大部分公司行动逻辑已经有效，但不能覆盖本轮确认的多证券同日边界错误。

## 2. 阻断问题

### B-04：同日多证券公司行动时，无pending证券仍被重建

**严重度：** 阻断  
**影响：** 订单、成交、换手、费用、现金、持仓和NAV。

#### 根因

`src/ashare_multifactor/execution/broker.py:255-272` 的外层条件只要确认 `action_symbols` 中任意一只证券存在pending，就会进入重建流程；但重建时传入的仍是全部 `action_symbols`：

```python
and any(item.symbol in action_symbols for item in pending)
...
symbols=action_symbols
```

因此，如果同一天：

- A股发生公司行动且存在旧pending；
- B股也发生公司行动，但没有pending；

执行器会正确撤销并重建A，同时错误地按冻结权重为B生成新订单。这相当于在公司行动日对B进行额外再平衡，超出“仅修正跨公司行动pending订单”的研究规则。

#### 真实release证据

在 `de57558_stage6_final` 中确认：

- 35笔非新信号日、且该证券没有对应 `corporate_action_rebase` 撤单的新订单；
- 其中34笔订单发生成交；
- 共形成103笔trade记录；
- 涉及成交金额约 `3,879,402.00` 元。

例如，2005-06-16共18只证券发生公司行动，只有 `600376` 存在被 `corporate_action_rebase` 撤销的pending，但无pending的 `600393` 也被生成新买单。

#### 为什么现有测试没有发现

`test_corporate_action_without_affected_pending_does_not_rebalance` 只覆盖“当日所有公司行动证券都没有pending”的单证券情形。

它没有覆盖“同日多证券中，部分有pending、部分无pending”的混合情形，因此外层 `any(...)` 门槛会被A股触发，然后将B股一并送入重建。

## 3. 推荐修复

不要只判断是否存在受影响pending，而应显式缩小重建证券集合：

```python
affected_pending = [item for item in pending if item.symbol in action_symbols]

if affected_pending:
    affected_symbols = {item.symbol for item in affected_pending}

    # 只撤销真正受影响的旧pending
    ...

    order_specs = build_target_order_specs(
        ledger,
        active_target_weights,
        market,
        last_close,
        symbols=affected_symbols,
    )
```

推荐同时将 `affected_pending` 的计算放在条件之前，避免先用 `any(...)` 遍历一次，进入分支后再重复过滤。

这一修复只纠正订单重建范围，不改变因子、目标权重、交易频率、成本或其他冻结研究设计。

## 4. 必需回归测试

新增一个多证券混合反例：

1. 同一交易日A、B都发生公司行动；
2. A存在跨日pending，B不存在pending；
3. 断言A的旧pending被 `corporate_action_rebase` 撤销并按新股数尺度重建；
4. 断言B没有新订单、成交或 `corporate_action_rebase` 事件；
5. 反转证券输入顺序后结果不变；
6. 对买单和卖单至少各覆盖一次。

测试应在当前实现上失败，在修复后通过。

## 5. release和审计处理

由于该错误已在真实release中产生成交，不能只修改代码而继续使用 `de57558_stage6_final`。

推荐流程：

1. 保留 `de57558_stage6_final` 作为历史证据；
2. 将其标记为 `superseded_due_to_mixed_symbol_corporate_action_rebalance`；
3. 在修复后的干净Git提交上执行两个独立完整期run；
4. 比较31个核心文件哈希；
5. 重新检查订单、成交、现金、应收、持仓、NAV、影子NAV和stale门禁；
6. 确认非新信号日中，无旧pending证券的公司行动重建订单数为0；
7. 发布新的不可变release并原子切换 `CURRENT.json`；
8. 在本审计文档追加复查结果，不覆盖本次 `blocked` 结论。

## 6. 板块7放行清单

只有以下条件全部满足后，才能开启板块7：

- [ ] 多证券同日公司行动的重建范围已缩小为 `affected_symbols`；
- [ ] 多证券混合反例已经红—绿验证；
- [ ] 全量pytest和ruff通过；
- [ ] 两个独立完整期run核心结果一致；
- [ ] 上述35笔可疑订单在新release中降为0；
- [ ] 新release来自干净Git提交；
- [ ] 新release账务、影子NAV和stale门禁通过；
- [ ] 旧release保留但明确标记为非权威；
- [ ] `CURRENT.json` 已指向新release；
- [ ] 所有release数据仍不超过2016-12-31；
- [ ] 阶段五因子、目标权重和成本参数未改变；
- [ ] 检查点B追加最终复查证据；
- [ ] 用户明确批准进入板块7。

## 7. 当前结论

阶段六已经通过大部分工程、账务和研究封存门禁，但 `de57558_stage6_final` 仍包含由多证券同日公司行动引入的额外再平衡成交。该问题直接影响回测结果，因此本轮结论为 `blocked`，不得使用当前release开启板块7。

---

## 8. 复查结果（2026-07-15）

**复查状态：** `passed_after_remediation_pending_merge`

原始`blocked`结论保留。B-04已在隔离分支关闭，等待合并main及main环境复验。

- 修复提交：`b995878`。重建前显式计算`affected_pending`与`affected_symbols`，撤单、pending过滤和目标重建均只作用于该集合。
- 新增多证券混合买入、卖出和输入顺序反转反例；相关专项测试7项通过。
- 全量测试：`590 passed`；`ruff check src tests`通过。
- 双次run：`checkpoint_b3_repro_a_b995878`、`checkpoint_b3_repro_b_b995878`；31个核心文件哈希一致。
- 第三次审计所列可疑订单降为0，对应trade为0、成交金额为0。
- 新权威release：`b995878_stage6_authoritative`。
- manifest SHA-256：`1a6f3cea593fa14cd7e687fae26b33e1da5f2df86c5616742aff0eb22697128e`。
- 新lineage记录替代`de57558_stage6_final`，原因为`superseded_due_to_mixed_symbol_corporate_action_rebalance`。
- 完整成本期末NAV：698,330,742.65元；三场景最大逐日对账差约`2.38e-7`元。
- 影子最大单日绝对偏差约0.3741%，未解释长期stale证券为0；2017年及以后仍未读取。

合并main并复验后，检查点B可恢复为`passed_after_remediation`。板块7仍需用户另行明确批准。
