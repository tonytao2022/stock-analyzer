#!/bin/bash
# P6 分季评分双轨引擎 — 每日自动调度 v1.1
# ==============================
# 依赖: daily_pipeline (原数据管道)
# 时序: 数据管道→P6评分 (每日15:30后执行)

set -e

BASE_DIR="/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现"
cd "$BASE_DIR"

source venv/bin/activate 2>/dev/null || true

MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export MYSQL_PASS="$MYSQL_PWD"

LOG_FILE="/tmp/p6_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 P6双轨评分调度启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# 1. 检查数据
echo "" | tee -a $LOG_FILE
echo "🔍 检查今日数据..." | tee -a $LOG_FILE

# 2. P6全量评分+入库
echo "" | tee -a $LOG_FILE
echo "🏃 P6双轨评分(监控池)..." | tee -a $LOG_FILE

cd "$BASE_DIR"
python3 -c "
import sys, os, time
sys.path.insert(0, '$BASE_DIR')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from season_engine import SeasonEngine
from p6_dual_track_engine import daily_pipeline

start = time.time()
results = daily_pipeline(mode='watch_pool')
elapsed = time.time() - start
print(f'⏱️ 总用时: {elapsed:.0f}s')
" 2>&1 | tee -a $LOG_FILE

# 3. V14评分生成 + 写入 daily_v14_score 表
# V14 = 0.739 * composite_score + 10.35 (从历史数据拟合)
echo "" | tee -a $LOG_FILE
echo "🔢 生成V14评分..." | tee -a $LOG_FILE
cd "$BASE_DIR"
python3 -c "
import sys, os
sys.path.insert(0, '$BASE_DIR')
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', '')
from db_config import db_cursor
from p6_dual_track_engine import get_connection

# 获取最新交易日
conn = get_connection()
cur = conn.cursor()
cur.execute('SELECT MAX(trade_date) as d FROM strategy_signal')
trade_date = cur.fetchone()['d']
cur.close()
conn.close()

print(f'最新交易日: {trade_date}')

# 从 strategy_signal 获取今日评分
with db_cursor(commit=False) as cur:
    cur.execute('''
        SELECT ts_code, composite_score FROM strategy_signal
        WHERE trade_date=%s AND composite_score IS NOT NULL
    ''', (trade_date,))
    rows = cur.fetchall()
    print(f'读取 {len(rows)} 条评分数据')

# 计算 V14 并写入
import pymysql
conn = get_connection()
cur = conn.cursor()
saved = 0
for r in rows:
    code = r['ts_code']
    cs = float(r['composite_score'] or 0)
    # V14 = 0.739 * composite_score + 10.35<br>    # 再取整到一位小数
    v14 = round(min(100, max(0, 0.739 * cs + 10.35)), 1)
    p6_score = round(min(100, max(0, cs)), 1)
    try:
        cur.execute('''
            INSERT INTO daily_v14_score (ts_code, trade_date, v14_score, p6_score)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE v14_score=VALUES(v14_score), p6_score=VALUES(p6_score)
        ''', (code, trade_date, v14, p6_score))
        saved += 1
    except Exception as e:
        if saved < 3:
            print(f'  ⚠️ {code}: {e}')
    if saved % 100 == 0:
        conn.commit()
try:
    conn.commit()
except Exception:
    pass
try:
    cur.close()
except Exception:
    pass
try:
    conn.close()
except Exception:
    pass
print(f'V14评分已写入: {saved}/{len(rows)} 条 (trade_date={trade_date})')
" 2>&1 | tee -a $LOG_FILE

echo "" | tee -a $LOG_FILE
echo "✅ P6双轨评分管道完成: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
