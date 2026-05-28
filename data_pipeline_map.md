# 股票智能分析管理系统 — 数据管道编排

## 数据依赖关系

```
数据源层 (Tushare Pro)
     │
     ├─ pro.daily() ───────────────────┐
     │    ↓                            │
     │  daily_kline (原始K线)           │
     │  (5526只, 268824条)              │
     │                                 │
     ├─ pro.daily() + adj_factor ──────┤
     │    ↓                            │
     │  daily_kline_qfq (前复权K线)      │
     │  (前复权处理, 113465条)           │
     │                                 │
     ├─ pro.moneyflow() ───────────────┤
     │    ↓                            │
     │  money_flow (资金流向)            │
     │  (594条, 滞后的)                  │
     │                                 │
     └─ 其他接口                         │
                                         │
                                         ▼
计分引擎层 (score_engine.py)
     │
     ├─ 依赖: daily_kline_qfq (主) / daily_kline (回退)
     │    ↓
     ├─ trend_score (综合评分) ←──────────┐
     │  (983条, 最新2026-05-27)            │
     │    ↓                               │
     ├─ strategy_signal (策略信号) ←───────┤
     │  (495条, 最新2026-05-27)            │
     │    ↓                               │
     └─ backtest_score_daily (回测评分) ←─┘
       (113010条, 最新2026-05-27)
     
辅助分析层
     │
     ├─ season_state (季节判定)
     │  (475条, 依赖 daily_kline + 大盘指数)
     │
     ├─ money_flow (资金流向, 辅助维度)
     │  (594条, 滞后)
     │
     ├─ technical_indicator (技术指标)
     │  (36323条, 滞后)
     │
     ├─ momentum_daily_index (动量环境)
     │  (1条, 依赖 trend_score + daily_kline_qfq)
     │
     └─ chanlun_structure (缠论结构)
       (86条, 依赖 daily_kline_qfq)

展示层 (前端)
     │
     ├─ dashboard (驾驶舱)
     │  依赖: daily-summary + watch-pool/snapshot
     │
     ├─ portfolio (持仓)
     │  依赖: portfolio_holdings + rt_k(实时价)
     │
     └─ signals (信号卡片)
       依赖: strategy_signal + daily_kline
```

## 数据更新流程（标准管道步骤）

### 步骤编号与依赖顺序

```
Step 0: 拉取原始数据 (Tushare Pro)
        ├─ pro.daily()               → daily_kline
        ├─ pro.daily() + adj_factor  → daily_kline_qfq
        ├─ pro.moneyflow()           → money_flow
        └─ pro.daily()               → technical_indicator
        
Step 1: 季节判定
        依赖: daily_kline (大盘指数数据)
        写入: season_state
        
Step 2: 缠论分析
        依赖: daily_kline_qfq
        写入: chanlun_structure
        
Step 3: ⭐ 全量评分 (核心步骤)
        依赖: daily_kline_qfq (优先) / daily_kline (回退)
              season_state (获取当前季节)
        写入: trend_score + strategy_signal
        注意: _load_kline中已做回退逻辑，优先用qfq，不足时回退到原始K线
        
Step 4: 每日动量报告
        依赖: trend_score + strategy_signal
        写入: momentum_daily_index
        
Step 5: 监控池快照
        依赖: strategy_signal
        写入: watch_pool_snapshot

Step 6: 数据驾驶舱缓存
        依赖: 所有上层数据
        写入: daily_snapshot
```

### 时间线

| 时间 | 触发 | 执行步骤 |
|:----:|:----|:--------|
| 09:30-15:00 | 盘中 | 用户手动点"刷新评分"，只跑Step 3+4 |
| **15:30** | **cron** | **完整管道 Step 0~6**（今日闭市后） |
| 任意时间 | 用户操作 | 持仓页面通过rt_k获取实时价，不依赖上述步骤 |

## 当前数据滞后原因

1. **复权K线 (daily_kline_qfq) 只有5月27日16只**
   - 因为之前导入复权K线时，只处理了backtest_pool中的175只
   - 扩容到326只后没有重新跑复权K线导入
   - **修复**: 复用 `backtest_score.py` 框架，但需要先批量拉取复权数据

2. **资金流向和技术指标滞后到5月26日**
   - 这些是辅助维度，不影响核心评分
   - 优先级较低，可在管道Step 0中统一拉取

3. **要解决的根本问题**: 确保每次管道运行时，Step 0先把原始数据拉到最新，再跑Step 1~6
