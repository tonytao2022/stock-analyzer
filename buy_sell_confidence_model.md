# 买卖点置信度模型方案 v1.0

**设计：May | 2026-07-23**

## 一、问题背景

当前 `signal_confidence` 只是 `calibrated_score` 的简单映射：
- ≥80 → high
- ≥60 → medium
- <60 → low

这不叫"置信度模型"，只是评分标签的再表述。真正的置信度应该回答：**这个评分信号有多可靠？**

7/22 数据显示：

| 维度 | 典型值 | 含义 |
|:----|:-----:|:----|
| `calibrated_score` 均值 | 26.0 | 全市场评分一般 |
| `signal_confidence=high` 仅1只 (0.1%) | 评分≥80的只有1只 |
| 有惩罚分的股票 | 0只 | 惩罚分从未生效 |
| `safety_gate` / `gate_triggered` | 全为空 | 安全门机制从没用过 |
| `penalty_score` | 全部0 | 惩罚未工作 |

**结论：现有的置信度字段全部是空壳，需要重新设计。**

## 二、置信度模型架构

### 2.1 三因子置信度打分

对每只股票的每个信号计算三个独立置信度分量，然后加权合成：

```
confidence_total = w1 × C_structure + w2 × C_consensus + w3 × C_history
```

其中权重 w1:w2:w3 = 40:35:25（优先结构清晰度，其次因子一致性，最后历史表现）

### 2.2 C_structure — 结构置信度（40%）

基于缠论结构的清晰度评估：

| 条件 | 分数 | 权重 |
|:----|:---:|:----:|
| 结构评分 ≥70 且 有明确买卖点 | 90~100 | ×0.40 |
| 结构评分 ≥70 但无明确买卖点 | 65~75 | ×0.40 |
| 结构评分 40~70 | 40~60 | ×0.40 |
| 结构评分 <40 或 无结构数据 | 10~25 | ×0.40 |
| **秋老虎触发** | **-20分** | ×0.40 |

7/22 数据基准：high_struct(≥70) 占 24/833=2.9%，mid_struct(40-70) 占 74.6%，low_struct(<40) 占 22.6%

### 2.3 C_consensus — 因子一致性置信度（35%）

检查多个子因子方向是否一致：

| 一致模式 | 分数 | 说明 |
|:--------|:---:|:----|
| Trend+Struct+Mf 三个≥60 | 85~100 | 一致看多 |
| Trend+Struct+Mf 三个≤40 | 80~95 | 一致看空（信号可靠） |
| 两个≥60，一个<40 | 40~55 | 分歧 |
| 一个≥60，两个<40 | 20~35 | 弱分歧 |
| 三个在40~60之间 | 50~60 | 中性 |
| Trend与Struct方向相反 | 20~30 | 严重矛盾 |

7/22 数据基准：一致看多=58只/7.0%，一致看空=97只/11.6%，混合分歧=572只/68.7%

### 2.4 C_history — 历史表现置信度（25%）

基于该股票评分的历史稳定性：

| 条件 | 分数 | 说明 |
|:----|:---:|:----|
| 近5日评分变化≤5分 | 80~95 | 稳定信号 |
| 近5日评分变化5~15分 | 50~70 | 中等变化 |
| 近5日评分变化>15分 | 20~40 | 剧烈波动，不可靠 |
| 数据不足5日 | 40~50 | 中性偏保守 |

### 2.5 置信度五分法

| 复合分 | 等级 | 含义 | 建议 |
|:-----:|:----|:----|:----|
| ≥80 | **A级（高置信）** | 结构清晰+因子一致+历史稳定 | 可以按评分正常执行 |
| 60~79 | **B级（中置信）** | 基本可靠但有轻微矛盾 | 执行时仓位×0.8 |
| 40~59 | **C级（中低置信）** | 存在分歧 | 执行时仓位×0.5 |
| 20~39 | **D级（低置信）** | 多重矛盾 | 仅观察不执行 |
| <20 | **E级（不可信）** | 信号冲突严重 | 忽略 |

## 三、安全门机制 safety_gate

在置信度模型基础上，增加三道安全门：

### 3.1 Gate 1：评分-置信度背离检测

```
if calibrated_score ≥ 60 and confidence_level in ('D', 'E'):
    safety_gate = 'score_conflict'
    gate_triggered = True
    # 评分高但置信度低 → 是陷阱信号
    signal_label = 'TRAP'
    direction = 'avoid'
```

7/22 典型案：**芯源微** score=100/trend=100/structure=82 → 表面看多极强，但 mf=47 资金流出，历史回撤-52% → 置信度应降至 D 级 → safety_gate 触发。

### 3.2 Gate 2：持仓持续恶化

```
if 持仓股票 and 近5日评分连续下降 and 评分<20:
    safety_gate = 'deteriorating'
    gate_triggered = True
    # 即使原评分尚可也需强制降级
    direction = 'sell' (强制)
```

### 3.3 Gate 3：市场环境对冲

```
if 季节 in ('chaos_autumn', 'autumn', 'winter') and confidence_level in ('D', 'E'):
    safety_gate = 'adverse_market'
    gate_triggered = True
    # 弱市中低置信度信号一律忽略
    direction = 'avoid'
```

## 四、DB 数据结构

全部复用现有字段，不新增字段：

| 已有字段 | 新用法 |
|:--------|:------|
| `signal_confidence` | 改为存储五分法等级 `A/B/C/D/E` |
| `safety_gate` | 存储触发的门名 `score_conflict` / `deteriorating` / `adverse_market` |
| `gate_triggered` | true/false 是否被安全门拦截 |
| `signal_label` | 存储最终信号标签：`NORMAL` / `TRAP` / `AVOID` / `FORCE_SELL` |
| `penalty_score` | 保持原用途（技术面惩罚，不与置信度混淆） |

## 五、落地实现

### 5.1 新增模块

`/root/stock-system/backend/confidence_engine.py` — ~180行

```python
class ConfidenceEngine:
    def calc_structure_confidence(self, struct_score, buy_sell_point, autumn_tiger):
        """计算结构置信度 C_structure"""
        
    def calc_consensus_confidence(self, trend_score, struct_score, mf_score):
        """计算因子一致性置信度 C_consensus"""
        
    def calc_history_confidence(self, ts_code, trade_date, lookback=5):
        """计算历史表现置信度 C_history"""
        
    def run_confidence(self, ts_code, trade_date, signal_row):
        """主入口：三因子合成 + 安全门检查
        返回 {confidence_level, safety_gate, gate_triggered, signal_label}
        """
```

### 5.2 集成到 p6_dual_track_engine.py

在 `daily_pipeline()` 的评分落库前加入置信度检查：

```python
from confidence_engine import ConfidenceEngine
ce = ConfidenceEngine()
conf = ce.run_confidence(ts_code, ctx.trade_date, row)

# 覆写原有简单映射
signal_confidence = conf['confidence_level']  # A/B/C/D/E
safety_gate = conf['safety_gate']
gate_triggered = conf['gate_triggered']
signal_label = conf['signal_label']
```

### 5.3 不影响现有评分流程

- 置信度**不修改 `calibrated_score`**，只是给信号多一个可信度标签
- 现有买入线逻辑不变（买入线仍然看 `calibrated_score`）
- 但 `方向 + 标签` 组合可以让前端/策略引擎额外过滤

## 六、7/22回测模拟

用7/22数据过一遍预期效果：

| 场景 | 旧逻辑 | 新逻辑 | 变化 |
|:----|:------|:-------|:----|
| 芯源微 score=100/trend=100/struct=82/mf=47 | signal_confidence=high | 一致性分歧→C级, 安全门触发→TRAP | ✅ 识别潜在陷阱 |
| 深科技 score=5/trend=55/struct=52/mf=5 | signal_confidence=low | 一致看空+稳定低分→B级可信利空 | ✅ 确认卖出 |
| 高结构high_struct(24只) avg_calib=47 | 全low | C结构高→部分B级以上 | ✅ 结构好的获得更高置信度 |
| 一致看多(58只) avg_calib=54 | 全low | 因子一致→B~A级 | ✅ 真正优质信号突出 |

## 七、工作量评估

| 模块 | 代码量 | 工时 |
|:----|:-----:|:---:|
| confidence_engine.py | ~180行 | 1人天 |
| 集成到 p6_dual_track_engine | ~20行 | 0.5人天 |
| DB字段验证 + 测试 | — | 0.5人天 |
| **总计** | **~200行** | **2人天** |

## 八、实现状态 ✅ v1.0 已部署 (2026-07-23)

### 已完成的文件
| 文件 | 路径 | 状态 |
|:----|:----|:----:|
| confidence_engine.py | `/root/stock-system/backend/confidence_engine.py` | ✅ 已创建 |
| p6_dual_track_engine.py 集成 | 同上目录 | ✅ 已集成 |
| 方案文档 | `buy_sell_confidence_model.md` | ✅ |

### 7/22 全量回测结果（833只）
| 置信度 | 数量 | 占比 |
|:-----|:---:|:---:|
| A级(高置信) | 0 | 0% |
| B级(中置信) | 48 | 5.8% |
| C级(中低置信) | 746 | 89.6% |
| D级(低置信) | 39 | 4.7% |
| E级(不可信) | 0 | 0% |

### 安全门触发
| 门类型 | 触发数 | 示例 |
|:-----|:-----:|:----|
| score_conflict (TRAP) | 1 | 西部矿业 score=68但结构弱→标记 |
| adverse_market (AVOID) | 16 | chaos_autumn下低置信信号 |
| deteriorating (FORCE_SELL) | 0 | 持仓评分持续恶化的条件未满足 |

## 九、验证标准

上线验证期7天，检查三项：

1. **TRAP识别率**：被标记TRAP的股票，3日后的确反向运行比例≥65%
2. **误杀率**：被标记AVOID的股票，3日后上涨比例≤20%
3. **B级信号胜率**：标记B级+买入的信号，5日胜率≥55%（当前基准~48%）
