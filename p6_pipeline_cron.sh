#!/bin/bash
# P6 分季评分双轨引擎 — 每日自动调度 v1.0
# ==============================
# 2026-06-01
# 依赖: daily_pipeline_cron.sh (原数据管道)
# 时序: 数据管道→P6评分 (每日15:30后执行)

set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 导出密码环境变量(MYSQL_PASS优先于debian.cnf)
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export MYSQL_PASS="$MYSQL_PWD"

LOG_FILE="/tmp/p6_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 P6双轨评分调度启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# 1. 检查原始数据管道是否已完成
echo "" | tee -a $LOG_FILE
echo "🔍 检查今日数据..." | tee -a $LOG_FILE
TODAY=$(python3 -c "from db_config import get_connection; c=get_connection(); cur=c.cursor(); cur.execute('SELECT MAX(trade_date) FROM daily_kline'); r=cur.fetchone(); cur.close(); c.close(); print(r['MAX(trade_date)'] if r else 'None')" 2>&1)
echo "   最新交易日: $TODAY" | tee -a $LOG_FILE

# 2. P6季节判定
echo "" | tee -a $LOG_FILE
echo "🌤️ 季节判定..." | tee -a $LOG_FILE
python3 << 'PYEOF' >> $LOG_FILE 2>&1
import sys, os
sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from season_engine import SeasonEngine
e = SeasonEngine()
r = e.judge_market_season()
print(f"season={r.get('market_season')} regime={r.get('market_regime')} "
      f"strategy={r.get('market_scoring_strategy')} confidence={r.get('market_confidence')}")
PYEOF
echo "   ✅" | tee -a $LOG_FILE

# 3. P6全量评分+入库
echo "" | tee -a $LOG_FILE
echo "🏃 P6双轨评分(监控池195只)..." | tee -a $LOG_FILE
python3 << 'PYEOF' >> $LOG_FILE 2>&1
import sys, os, time
sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from season_engine import SeasonEngine
from p6_dual_track_engine import MarketContext, batch_score
from db_config import get_connection

start = time.time()
engine = SeasonEngine()
ctx = MarketContext(engine.judge_market_season())

conn = get_connection(); cur = conn.cursor()
cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
ts_codes = [r['ts_code'] for r in cur.fetchall()]
cur.close(); conn.close()

results = batch_score(ts_codes, ctx)

conn = get_connection(); cur = conn.cursor()
saved = 0
for r in results:
    try:
        cur.execute("""
            INSERT INTO strategy_signal 
                (ts_code, trade_date, track, composite_score, calibrated_score, scoring_strategy, direction)
            VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1')
            ON DUPLICATE KEY UPDATE
                track=VALUES(track), calibrated_score=VALUES(calibrated_score),
                scoring_strategy=VALUES(scoring_strategy), direction='dual_track_v1'
        """, (r['ts_code'], ctx.trade_date, r['track'],
              r['score'], r['calibrated_score'],
              'momentum' if r['track']=='momentum' else 'reversion'))
        saved += 1
    except: pass
conn.commit(); cur.close(); conn.close()

elapsed = time.time() - start
print(f"✅ 入库 {saved}条 | 用时{elapsed:.0f}s | "
      f"市场:{ctx.season}/{ctx.regime} 策略:{ctx.scoring_strategy}")

# Top5
print(f"🏆 Top5:")
for r in results[:5]:
    print(f"  {r['ts_code']} | P6={r['score']:.1f} 校准={r['calibrated_score']:.1f} 轨道={r['track']}")
PYEOF
echo "   ✅ 完成" | tee -a $LOG_FILE

# 4. 同步刷新watch_pool_snapshot（供前端监控池/持仓等页面使用）
echo "" | tee -a $LOG_FILE
echo "🔄 同步刷新监控池快照..." | tee -a $LOG_FILE
cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现 && source venv/bin/activate
SNAP_RESULT=$(python3 -c "
import sys, json
sys.path.insert(0, '.')
from manager_server import app
with app.test_client() as client:
    r = client.post('/api/v1/management/watch-pool/refresh', headers={'X-API-Key': '90a275cbcc004fd5'})
    d = json.loads(r.data)
    if d.get('code') == 0:
        print(f'✅ 快照刷新: {d[\"data\"][\"updated\"]}/{d[\"data\"][\"total\"]} 只, 交易日 {d[\"data\"][\"trade_date\"]}')
    else:
        print(f'❌ 快照刷新失败: {d}')
" 2>&1)
echo "   $SNAP_RESULT" | tee -a $LOG_FILE

# 5. 完成
echo "" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
echo "✅ P6双轨评分管道完成: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
