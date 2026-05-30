#!/bin/bash
# 每日数据管道 v3.0 — daily_pipeline.py 统一调度
set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从 MySQL 读取 TUSHARE_TOKEN 并导出
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
TUSHARE_TOKEN=$(mysql --defaults-file=/etc/mysql/debian.cnf -N -e \
  "SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)
export TUSHARE_TOKEN

echo "验证Token: ${TUSHARE_TOKEN:0:10}..."

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 每日管道 v3.0 启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

# ─── 全管道运行 ───
#   1/6 K线拉取   2/6 缠论分析   3/6 季节判定
#   4/6 全量评分   5/6 监控快照   6/6 多周期回测
echo "" | tee -a $LOG_FILE
echo "🚀 全管道启动 (daily_pipeline.py)..." | tee -a $LOG_FILE
python3 daily_pipeline.py >> $LOG_FILE 2>&1

# ─── 完成 ───
echo "" >> $LOG_FILE
echo "✅ 管道完成: $(date)" | tee -a $LOG_FILE
echo "" >> $LOG_FILE
