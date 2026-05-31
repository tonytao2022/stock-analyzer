# 股票系统硬编码修复执行计划
## 2026-06-01 凌晨定案

---

## 总策略：架构级统一 → 文件级整改 → 验证

不是逐文件修80处，而是先建立统一入口，再批量替换所有文件的自建DB代码。

---

## 批次一：P0 配置入口层（Tony负责）

### T1.1 db_config.py 升级 — 最关键一环
**目标**：成为全系统唯一数据库连接入口
**改动文件**：`{代码实现}/db_config.py`（两份都要改：主仓库+stock-system）

| 改动项 | 当前 | 改为 |
|:-------|:-----|:-----|
| 密码读取 | 仅读取 `/etc/mysql/debian.cnf` | `os.environ.get('MYSQL_PASS')` 优先，fallback 读 debian.cnf |
| DB_CONFIG | 直接写死 `host/port/user` | POOL_SIZE=5 参数化，端口从环境变量获取 |
| get_connection | @functools.lru_cache | 改成连接池 |
| 默认用户ID | `return 'tony'` 硬编码 | 改为 `os.environ.get('STOCK_USER', 'tony')` |

**产出**：`db_config_v2.py`（或原地升级）— 单一 import 即可获得连接

### T1.2 manager_server.py 去硬编码 — 文件最大、影响最广
**目标**：消除15处散落pymysql.connect，消除交叉引用硬编码端口
**改动文件**：`manager_server.py`（主仓库 + stock-system 两份）

| 改动项 |
|:--------|
| 所有 `pymysql.connect(host=..., password=...)` → `get_connection()` |
| 所有交叉引用 `http://localhost:8887/xxx` → 从环境变量或 `system_config` 读取 |
| 10处硬编码文件路径 → 统一从 `config.INI` 或环境变量读取 |

### T1.3 服务端口环境变量化
**目标**：`app.py:8888` / `manager_server.py:8887` / `signal_server.py:8889`

**改动**：
```python
# 原
app.run(host='0.0.0.0', port=8888)
# 改
app.run(host='0.0.0.0', port=int(os.environ.get('STOCK_PORT_8888', 8888)))
```

---

## 批次二：P0~P1 文件层整改（Hugo负责）

### H2.1 所有 backtest_*.py — 最多、但模式最简单
**涉及文件**（10个）：

| 文件 | 当前问题 | 修复方式 |
|:-----|:---------|:---------|
| `backtest_10stocks.py` | import了db_config但仍有硬编码 | 改原有self.DB引用为db_config.get_connection() |
| `backtest_20day_winrate.py` | 同上 | 同上 |
| `backtest_20random.py` | 同上 | 同上 |
| `backtest_5stocks_daily.py` | 同上 | 同上 |
| `backtest_5stocks_trade.py` | 同上 | 同上 |
| `backtest_exit_reason.py` | 但有自建DB_CONFIG+get_connection | 替换函数体 |
| `backtest_hengjiyuan.py` | 自建DB_CONFIG | 删掉，改import |
| `backtest_multi_cycle.py` | 自建DB_CONFIG | 删掉，改import |
| `backtest_score.py` | 自建DB_CONFIG | 删掉，改import |
| `backtest_signal_tracking.py` | 自建DB_CONFIG | 删掉，改import |
| `backtest_trade_sim.py` | 自建CFG字典 | 删掉，改import |
| `backtest_ladder_strategy.py` | **明文密码！** | **删密码行，改import** |
| `backtest_verify_p0_fix.py` | 自建DB + 明文密码 | **删密码行，改import** |

**操作模式**：每条替换都是同一个模式
```python
# 删掉
# DB_CONFIG = {.... password='...'}  # 整个字典删掉

# 改 import
from db_config import get_connection

# 所有 .connect改为 get_connection
```

### H2.2 所有 batch_*.py — 4个文件，同样模式
| 文件 | 修复 |
|:-----|:------|
| `batch_chanlun_analyzer.py` | 删自建DB，改import |
| `batch_full_market_score.py` | 同上 |
| `batch_money_flow.py` | 同上 |
| `batch_technical_indicator.py` | 同上 |

### H2.3 每日跑批文件 — 5个文件
| 文件 | 当前 | 修复 |
|:-----|:-----|:------|
| `daily_pipeline.py` | import了但6处127.0.0.1 | 统一用import |
| `daily_momentum_report.py` | 自建DB | 改import |
| `fetch_today.py` | 自建DB | 改import |
| `fetch_today_qfq.py` | 自建DB | 改import |
| `fetch_backtest_kline.py` | 自建DB_CONFIG | 改import |

### H2.4 评分引擎文件 — 3个文件
| 文件 | 修复 |
|:-----|:------|
| `score_engine.py` | 自建DB → 改import |
| `season_engine.py` | 自建DB_CONFIG → 改import |
| `four_season_model_v3.py` | 自建DB → 改import |

### H2.5 其他
| 文件 | 修复 |
|:-----|:------|
| `data_sync.py` | **明文密码！删改** |
| `regime_switcher.py` | **明文密码！删改** |
| `data_fetcher.py` | 自建DB → 改import |
| `factor_analysis.py` | 自建DB → 改import |

---

## 批次三：P1 次要硬编码（Tony + Hugo 并行）

| 分类 | 负责 | 改动项 |
|:-----|:-----|:--------|
| 用户ID硬编码 | Tony | db_config.py → 改环境变量读取 |
| 日期硬编码 | Hugo | `_load_kline` 默认日期从DB最大日期算 |
| 服务交叉引用 | Tony | daily_pipeline中 http://localhost:8887 改环境变量 |
| 文件路径硬编码 | Hugo | manager_server.py中10处硬路径 → 配置化 |
| SQL表名硬编码 | 二人 | 建议阶段（改动量大，仅做记录，非紧急） |

---

## 批次四：P2 低优先（记录留后）

| 分类 | 说明 |
|:-----|:------|
| 日志rotate | manager_server.py中 systemd + logrotate配置 |
| qfq缺失4只 | 补全数据 |
| 混沌子态回测 | 待Tony决定是否启动 |

---

## 验证方案

每批次完成后：

1. **语法检查**：`python3 -m py_compile xxx.py` 全部通过
2. **启动验证**：三服务能正常启动，健康检查返回200
3. **管道测试**：手动触发一次 daily_pipeline，确认评分/信号正常生成
4. **回滚预案**：每批次修完 git commit，出问题
