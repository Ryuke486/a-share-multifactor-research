# 单因子卡片（2005–2016 研究期）

本目录是阶段四 14 个预注册因子的因子卡片，逐字节复制自权威产物 `artifacts/factor_research/cards/`，未做任何手工编辑。
每张卡片包含定义、经济解释、覆盖率、Rank IC、五分组、衰减、换手、子区间、冗余关系和分类原因。

- 来源清单：`artifacts/factor_research/manifest.json`（SHA-256 `d1987c69b46ae931cbc21cdf07b4c0661569c56c589d4f79a3fc17a420c92242`），复制时逐张核对了清单中的文件哈希；
- 各卡片哈希记录在 [SHA256SUMS](SHA256SUMS)，由 `tests/test_factor_card_copies.py` 核对，防止卡片被手工改动；
- 卡片只覆盖研究期，不是验证期、最终测试期或投资结论；价值因子的 point-in-time 口径未核验，最多为 watch。

下表由 `artifacts/factor_research/factor_classifications.csv` 生成（主期限 20 个交易观测，主分数变体）。

| 因子 | 因子族 | 分类 | 平均 Rank IC | BH q | Q5−Q1（20 日） | 原因 |
|---|---|---|---:|---:|---:|---|
| [`amihud_20`](amihud_20.md) | liquidity | candidate | 0.030348 | 0.000185 | 0.810370% | `candidate:all_thresholds_passed` |
| [`reversal_20`](reversal_20.md) | reversal | candidate | 0.077245 | 0.000000 | 1.752290% | `candidate:all_thresholds_passed` |
| [`reversal_5`](reversal_5.md) | reversal | candidate | 0.066631 | 0.000000 | 1.555400% | `candidate:all_thresholds_passed` |
| [`turnover_20`](turnover_20.md) | liquidity | candidate | 0.081988 | 0.000000 | 1.670706% | `candidate:all_thresholds_passed` |
| [`volatility_20`](volatility_20.md) | low_volatility | candidate | 0.051553 | 0.000007 | 0.379354% | `candidate:all_thresholds_passed` |
| [`volatility_60`](volatility_60.md) | low_volatility | candidate | 0.046630 | 0.000494 | 0.221262% | `candidate:all_thresholds_passed` |
| [`bp`](bp.md) | value | watch | 0.053338 | 0.000090 | 0.820648% | `watch:point_in_time_unverified` |
| [`downside_volatility_60`](downside_volatility_60.md) | low_volatility | watch | 0.012500 | 0.372661 | -0.461115% | `watch:fdr_threshold_not_met;non_positive_or_missing_q5_q1` |
| [`ep_ttm`](ep_ttm.md) | value | watch | 0.048015 | 0.000004 | 0.974981% | `watch:point_in_time_unverified` |
| [`log_market_cap`](log_market_cap.md) | size | watch | 0.029184 | 0.104028 | 1.364106% | `watch:fdr_threshold_not_met` |
| [`sp_ttm`](sp_ttm.md) | value | watch | 0.027457 | 0.003137 | 0.653832% | `watch:point_in_time_unverified` |
| [`momentum_120`](momentum_120.md) | momentum | reject | -0.050739 | 0.000119 | -0.760713% | `reject:non_positive_or_missing_mean_ic` |
| [`momentum_12_1`](momentum_12_1.md) | momentum | reject | -0.021738 | 0.085904 | -0.042569% | `reject:non_positive_or_missing_mean_ic` |
| [`momentum_60`](momentum_60.md) | momentum | reject | -0.071333 | 0.000000 | -1.292077% | `reject:non_positive_or_missing_mean_ic` |
