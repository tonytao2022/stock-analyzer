#!/bin/bash
# 每日数据管道 v2.0 — 确保所有表最新后再跑评分
# 执行顺序: 拉数据 → 季节判定 → 评分 → 动量报告 → 快照
set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从 MySQL 读取 TUSHARE_TOKEN
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export TUSHARE_TOKEN=*** -u debian-sys-maint -p"$MYSQL_PWD" openclaw_config -N -e \
  "SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "=========================================" | tee -a $LOG_FILE
echo "📡 每日管道 v2.0 启动: $(date)" | tee -a $LOG_FILE
echo "=========================================" | tee -a $LOG_FILE

# ─── Step 0: 拉取最新K线数据 ───
echo "📥 Step 0: 拉取最新K线..." | tee -a $LOG_FILE
python3 -c "
import os, tushare as _ts, pymysql, time
from datetime import datetime, timedelta

tk = os.environ.get('TUSHARE_TOKEN','')
_ts.set_token(tk)
pro = _ts.pro_api()

pwd = ''
with open('/etc/mysql/debian.cnf') as f:
    for l in f:
        if 'password' in l: pwd = l.split('=')[-1].strip().strip('\"').strip(\"'\");

conn = pymysql.connect(host='127.0.0.1',port=3306,user='debian-sys-maint',password=pwd,da…db')
cur = conn.cursor()

# 取最新交易日
df = pro.trade_cal(exchange='SSE', start_date='20260520', end_date='20260528')
if df is not None and len(df) > 0:
    cal = df[df['is_open']==1]
    if len(cal) > 0:
        latest = cal.iloc[-1]['cal_date']
        print(f'最新交易日: {latest}')
        
        # 拉所有回测池股票的K线
        cur.execute(\"SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'\")
        codes = [r[0] for r in cur.fetchall()]
        print(f'股票数: {len(codes)}只')
        
        # 拉daily
        for i, code in enumerate(codes):
            try:
                df2 = pro.daily(ts_code=code, start_date='20260525', end_date=latest)
                if df2 is not None and len(df2) > 0:
                    for _, r2 in df2.iterrows():
                        cur.execute('''
                            INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, pre_close, change_pct, vol, amount)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), vol=VALUES(vol), amount=VALUES(amount)
                        ''', (code, r2['trade_date'], float(r2['open']), float(r2['high']), float(r2['low']),
                              float(r2['close']), float(r2['pre_close']), float(r2['pct_chg']), float(r2['vol']), float(r2['amount'])))
                    conn.commit()
            except:
                pass
            if (i+1) % 50 == 0:
                print(f'  进度: {i+1}/{len(codes)}')
            time.sleep(0.3)
        
        # 更新daily_kline_qfq（前复权）
        print('更新前复权K线...')
        for i, code in enumerate(codes):
            try:
                df3 = pro.daily(ts_code=code, start_date='20260101', end_date=latest)
                if df3 is not None and len(df3) > 0:
                    for _, r3 in df3.iterrows():
                        cur.execute('''
                            INSERT INTO daily_kline_qfq (ts_code, trade_date, open, high, low, close, vol, change_pct)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), vol=VALUES(vol)
                        ''', (code, r3['trade_date'], float(r3['open']), float(r3['high']), float(r3['low']),
                              float(r3['close']), float(r3['vol']), float(r3['pct_chg'])))
                    conn.commit()
            except:
                pass
            if (i+1) % 50 == 0:
                print(f'  复权进度: {i+1}/{len(codes)}')
            time.sleep(0.2)
        
        print(f'✅ K线已更新到 {latest}')
    else:
        print('⚠️ 未找到交易日')
else:
    print('⚠️ 交易日历获取失败')
conn.close()
" >> $LOG_FILE 2>&1

# ─── Step 1: 季节判定 ───
echo "🌤️ Step 1: 季节判定..." | tee -a $LOG_FILE
python3 -c "
from four_season_model_v3 import judge_season
judge_season()
" >> $LOG_FILE 2>&1 || echo '⚠️ 季节判定跳过' >> $LOG_FILE

# ─── Step 2: 全量评分 ───
echo "⭐ Step 2: 全量评分（含ATR+量能潮汐因子）..." | tee -a $LOG_FILE
python3 -c "
from score_engine import ScoreEngineV4
e=ScoreEngineV4()
e.score_pool(save_db=True)
" >> $LOG_FILE 2>&1

# 验证评分日期
echo "验证评分最新日期:" >> $LOG_FILE
mysql -u debian-sys-maint -p"$MYSQL_PWD" stock_db -e "SELECT MAX(trade_date) as d FROM trend_score" 2>/dev/null >> $LOG_FILE

# ─── Step 3: 每日动量报告 ───
echo "📊 Step 3: 每日动量报告..." | tee -a $LOG_FILE
python3 daily_momentum_report.py >> $LOG_FILE 2>&1 || echo '⚠️ 跳过' >> $LOG_FILE

# ─── 完成 ───
echo "" >> $LOG_FILE
echo "✅ 管道完成: $(date)" | tee -a $LOG_FILE
echo "" >> $LOG_FILE
