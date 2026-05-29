#!/bin/bash
# 每日数据管道 v2.0 — 慢但完整，数据真实性优先
set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从 MySQL 读取 TUSHARE_TOKEN
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
TUSHARE_TOKEN=$(mysql --defaults-file=/etc/mysql/debian.cnf -N -e "SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)
export TUSHARE_TOKEN

echo "验证Token: ${TUSHARE_TOKEN:0:10}..."

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 每日管道 v2.0 启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# ─── Step 0: 拉取最新K线（并发版） ───
echo "" | tee -a $LOG_FILE
echo "📥 Step 0: 拉取最新K线行情..." | tee -a $LOG_FILE
python3 /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现/fetch_today_qfq.py >> $LOG_FILE 2>&1

# ─── Step 1: 全量评分 ───
echo "" | tee -a $LOG_FILE
echo "⭐ Step 1: 全量评分（含ATR+量能潮汐因子）..." | tee -a $LOG_FILE
python3 -c "
import sys; sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
from score_engine import ScoreEngineV4
e = ScoreEngineV4()
mkt = e.get_market_context()
print(f'季节: {mkt[\"season\"]} 置信度: {mkt[\"confidence\"]:.0%}')
e.score_pool(save_db=True)
print('评分完成')
" >> $LOG_FILE 2>&1

# ─── Step 2: 每日动量报告 ───
echo "" | tee -a $LOG_FILE
echo "📊 Step 2: 每日动量报告..." | tee -a $LOG_FILE
python3 /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现/daily_momentum_report.py >> $LOG_FILE 2>&1

# ─── 完成 ───
echo "" >> $LOG_FILE
echo "✅ 管道完成: $(date)" | tee -a $LOG_FILE
echo "" >> $LOG_FILE
