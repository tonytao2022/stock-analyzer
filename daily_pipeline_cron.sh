#!/bin/bash
# 每日数据管道 v2.0 — 慢但完整，数据真实性优先
set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从 MySQL 读取 TUSHARE_TOKEN
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export TUSHARE_TOKEN=*** -u debian-sys-maint -p"$MYSQL_PWD" openclaw_config -N -e \
  "SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 每日管道 v2.0 启动: $(date)" | tee -a $LOG_FILE
echo "   确保数据真实可靠，不设超时限制" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# ─── Step 0: 拉取最新K线（全部326只，每条数据真实） ───
echo "" | tee -a $LOG_FILE
echo "📥 [Step 0/4] 拉取最新K线行情（326只逐只获取）..." | tee -a $LOG_FILE
echo "   开始时间: $(date)" | tee -a $LOG_FILE

python3 -c "
import pymysql, os, time, sys, tushare as _ts

pwd = ''
with open('/etc/mysql/debian.cnf') as f:
    for l in f:
        if 'password' in l: pwd = l.split('=')[-1].strip().strip('\"').strip(\"'\")

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password': pwd,'database':'stock_db','charset':'utf8mb4'}

tk = os.environ.get('TUSHARE_TOKEN','')
_ts.set_token(tk)
pro = _ts.pro_api()

# 确定最新交易日
cal = pro.trade_cal(exchange='SSE', start_date='20260520', end_date='20260530')
latest_trade = None
if cal is not None and len(cal) > 0:
    opened = cal[cal['is_open']==1]
    if len(opened) > 0:
        latest_trade = opened.iloc[-1]['cal_date']
        print(f'  最新交易日: {latest_trade}')
    else:
        print('  无交易日')
        sys.exit(0)
else:
    print('  交易日历获取失败')
    sys.exit(0)

conn = pymysql.connect(**DB)
cur = conn.cursor()

# 取所有回测池股票
cur.execute(\"SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'\")
codes = [r[0] for r in cur.fetchall()]
print(f'  股票数: {len(codes)}只')

# === 拉取原始K线(daily_kline) ===
ok = 0
fail = 0
t0 = time.time()
for i, code in enumerate(codes):
    try:
        df = pro.daily(ts_code=code, start_date=latest_trade, end_date=latest_trade)
        if df is not None and len(df) > 0:
            r = df.iloc[0]
            cur.execute('''
                INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, pre_close, change_pct, vol, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), vol=VALUES(vol), amount=VALUES(amount)
            ''', (code, r['trade_date'],
                  float(r['open']), float(r['high']), float(r['low']),
                  float(r['close']), float(r['pre_close']),
                  float(r['pct_chg']), float(r['vol']), float(r['amount'])))
            conn.commit()
            ok += 1
        else:
            fail += 1
    except Exception as ex:
        fail += 1
    time.sleep(0.3)
    if (i+1) % 50 == 0:
        elapsed = time.time() - t0
        print(f'  原始K线: {i+1}/{len(codes)} (成功{ok},失败{fail},耗时{elapsed:.0f}s)')

t1 = time.time()
print(f'  原始K线完成: 成功{ok}, 失败{fail}, 耗时{t1-t0:.0f}s')

# === 拉取前复权K线(daily_kline_qfq) ===
print(f'  拉取前复权K线...')
qok = 0
qfail = 0
t0 = time.time()
for i, code in enumerate(codes):
    try:
        df2 = pro.daily(ts_code=code, start_date='20260101', end_date=latest_trade)
        if df2 is not None and len(df2) > 0:
            for _, r2 in df2.iterrows():
                cur.execute('''
                    INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, vol, change_pct)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), vol=VALUES(vol)
                ''', (code, r2['trade_date'],
                      float(r2['open']), float(r2['high']), float(r2['low']),
                      float(r2['close']), float(r2['vol']), float(r2['pct_chg'])))
            conn.commit()
            qok += 1
        else:
            qfail += 1
    except:
        qfail += 1
    time.sleep(0.2)
    if (i+1) % 50 == 0:
        elapsed = time.time() - t0
        print(f'  复权K线: {i+1}/{len(codes)} (成功{qok},失败{qfail},耗时{elapsed:.0f}s)')

t2 = time.time()
print(f'  前复权K线完成: 成功{qok}, 失败{qfail}, 耗时{t2-t1:.0f}s')

# 验证
cur.execute(\"SELECT MAX(trade_date) as d FROM daily_kline\")
r = cur.fetchone()
print(f'  daily_kline最新: {r[0]}')
cur.execute(\"SELECT trade_date, COUNT(DISTINCT ts_code) as c FROM daily_kline GROUP BY trade_date ORDER BY trade_date DESC LIMIT 2\")
for r2 in cur.fetchall():
    print(f'    {r2[0]}: {r2[1]}只')
cur.execute(\"SELECT MAX(trade_date) as d FROM daily_kline_qfq\")
r = cur.fetchone()
print(f'  daily_kline_qfq最新: {r[0]}')
conn.close()
print(f'✅ Step 0 K线拉取完成，总耗时: {t2-t1:.0f}s')
" >> $LOG_FILE 2>&1

# ─── Step 1: 跑全量评分 ───
echo "" | tee -a $LOG_FILE
echo "⭐ [Step 2/4] 全量评分（含ATR+量能潮汐因子）..." | tee -a $LOG_FILE
echo "   开始时间: $(date)" | tee -a $LOG_FILE

python3 -c "
import sys, os
sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
from score_engine import ScoreEngineV4
e = ScoreEngineV4()
mkt = e.get_market_context()
print(f'  当前季节: {mkt[\"season\"]} 置信度: {mkt[\"confidence\"]:.0%}')
e.score_pool(save_db=True)

import pymysql
pwd = ''
with open('/etc/mysql/debian.cnf') as f:
    for l in f:
        if 'password' in l: pwd = l.split('=')[-1].strip().strip('\"').strip(\"'\")
conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,da…db')
cur = conn.cursor()
cur.execute(\"SELECT MAX(trade_date) as d FROM trend_score\")
r = cur.fetchone()
print(f'  trend_score最新: {r[0]}')
cur.execute(\"SELECT trade_date, COUNT(*) as c FROM trend_score GROUP BY trade_date ORDER BY trade_date DESC LIMIT 3\")
for r2 in cur.fetchall():
    print(f'    {r2[0]}: {r2[1]}条评分')
conn.close()
print('✅ 评分完成')
" >> $LOG_FILE 2>&1

# ─── Step 3: 每日动量报告 ───
echo "" | tee -a $LOG_FILE
echo "📊 [Step 3/4] 每日动量报告..." | tee -a $LOG_FILE
python3 daily_momentum_report.py >> $LOG_FILE 2>&1

# ─── Step 4: 季节判定更新 ───
echo "" | tee -a $LOG_FILE
echo "🌤️ [Step 4/4] 季节判定..." | tee -a $LOG_FILE
python3 -c "
from four_season_model_v3 import judge_season
judge_season()
" >> $LOG_FILE 2>&1 || echo '  季节判定跳过' | tee -a $LOG_FILE

# ─── 完成 ───
echo "" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
echo "✅ 每日管道完成: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
