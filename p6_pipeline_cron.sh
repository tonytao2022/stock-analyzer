#!/bin/bash
# P6 统一评分管道 — 每日20:00执行
# ==============================
# 2026-06-03 重构
# 职责：一次跑完所有需要当日更新的表
# 
# 执行顺序：
#   1. P6双轨评分 → strategy_signal（唯一评分源表）
#   2. 监控池快照刷新 → watch_pool_snapshot（供前端监控池/持仓面板）
#   3. 阶梯策略评估 → strategy_signal_daily（供策略信号/持仓操作建议）
#
# 数据依赖链：
#   daily_kline_qfq ← 原数据管道(15:00)
#     → P6双轨评分(20:00)
#       → strategy_signal（核心评分表）
#         → watch_pool_snapshot（快照，前端展示用）
#         → strategy_signal_daily（策略信号，API用）

set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 导出密码
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export MYSQL_PASS="$MYSQL_PWD"

LOG_FILE="/tmp/p6_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 P6统一评分管道启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# 0. 检查数据就绪
echo "" | tee -a $LOG_FILE
echo "🔍 检查K线数据..." | tee -a $LOG_FILE
TODAY=$(python3 -c "from db_config import get_connection; c=get_connection(); cur=c.cursor(); cur.execute('SELECT MAX(trade_date) FROM daily_kline'); r=cur.fetchone(); print(r['MAX(trade_date)'] if r else 'None')" 2>&1)
echo "   最新交易日: $TODAY" | tee -a $LOG_FILE

START_TIME=$(date +%s)

# ─── 步骤1: P6双轨评分 → strategy_signal ───────────────────
echo "" | tee -a $LOG_FILE
echo "🏃 [1/3] P6双轨评分..." | tee -a $LOG_FILE
python3 << 'PYEOF' >> $LOG_FILE 2>&1
import sys, os, time
sys.path.insert(0, '.')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from p6_dual_track_engine import daily_pipeline
start = time.time()
results = daily_pipeline(mode='watch_pool')
print(f"  ⏱️ P6评分用时: {time.time()-start:.0f}s | {len(results)}只")
for r in results[:5]:
    print(f"  🏆 {r['ts_code']} | P6={r['score']:.1f} 校准={r['calibrated_score']:.1f} 轨道={r['track']}")
PYEOF
echo "   ✅ [1/3] P6双轨评分完成" | tee -a $LOG_FILE

# ─── 步骤2: 同步刷新watch_pool_snapshot ─────────────────────
echo "" | tee -a $LOG_FILE
echo "🔄 [2/3] 同步刷新监控池快照..." | tee -a $LOG_FILE
SNAP_RESULT=$(python3 << 'PYEOF' 2>&1
import sys, json
sys.path.insert(0, '.')
from manager_server import app
with app.test_client() as client:
    r = client.post('/api/v1/management/watch-pool/refresh', headers={'X-API-Key': '90a275cbcc004fd5'})
    d = json.loads(r.data)
    if d.get('code') == 0:
        print(f"✅ 快照刷新: {d['data']['updated']}/{d['data']['total']} 只, 交易日 {d['data']['trade_date']}")
    else:
        print(f"❌ 快照刷新失败: {d}")
PYEOF
)
echo "   $SNAP_RESULT" | tee -a $LOG_FILE

# ─── 步骤3: 阶梯策略评估 → strategy_signal_daily ────────────
echo "" | tee -a $LOG_FILE
echo "📊 [3/3] 阶梯策略评估..." | tee -a $LOG_FILE
python3 /opt/stock-analyzer/step_strategy_engine.py "$TODAY" >> $LOG_FILE 2>&1
echo "   ✅ [3/3] 阶梯策略评估完成" | tee -a $LOG_FILE

# ─── 完成 ───
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
echo "" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
echo "✅ P6统一评分管道完成: $(date)" | tee -a $LOG_FILE
echo "   总用时: ${DURATION}s" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
