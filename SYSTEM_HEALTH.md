# 股票智能分析系统 — 系统健康检查清单

## 每天15:30自动调度后需检查

### 1. 数据新鲜度
```sql
-- 检查所有关键表的最新交易日
SELECT MAX(trade_date) FROM daily_kline;                                      -- 应=今天或上周五
SELECT MAX(trade_date) FROM daily_kline WHERE ts_code='000300.SH';           -- 应=同上(指数)
SELECT MAX(trade_date) FROM season_state;                                     -- 应=同上
SELECT MAX(trade_date) FROM strategy_signal WHERE direction='dual_track_v1';  -- 应=同上(P6评分)
SELECT MAX(trade_date) FROM watch_pool_snapshot;                             -- 应=同上
```

### 2. 评分是否正常
```sql
-- P6入库数量
SELECT COUNT(*) FROM strategy_signal WHERE direction='dual_track_v1' AND trade_date='{最新日}'; -- 应>0
-- 监控快照
SELECT COUNT(*) FROM watch_pool_snapshot WHERE trade_date='{最新日}'; -- 应=监控池数量
```

### 3. P6管道日志
```bash
tail -20 /tmp/p6_pipeline_$(date +%Y%m%d).log  # 看是否有错误行
tail -20 /tmp/p6_service.log                     # systemd服务日志
```

## 已知隐患（需要修复）

### 优先级P0
- 无（今日已全部修复）

### 优先级P1
- [x] P6入库字段名check → 2026-06-01修复
- [x] 指数K线自动补拉 → daily_pipeline step_kline已加
- [x] season_engine trade_date → 000300.SH K线补齐后正常

### 优先级P2
- [ ] daily_kline 缺少 `is_valid` 索引（大量数据查询）
- [ ] system_config 中 P6 阈值参数未与代码同步（硬编码 fallback）
- [ ] watch_pool_snapshot 的 `total` 与 `strategy_signal` 记录数不一致（旧数据残留）
