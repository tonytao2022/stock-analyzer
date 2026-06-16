#!/bin/bash
# 每日数据管道 v4.0 — 统一调度（独立子进程，防OOM）
# 执行时间: 工作日17:00（收盘后Tushare数据出全）
# 
# 步骤:
#   1/7 K线拉取     2/7 资金流向     3/7 缠论分析
#   4/7 季节判定     5/7 全量评分     6/7 监控池快照
#   7/7 多周期回测
set -e

cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从MySQL读取密码和Token
MYSQL_PWD=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export MYSQL_PASS="$MYSQL_PWD"

TUSHARE_TOKEN=$(mysql --defaults-file=/etc/mysql/debian.cnf -N -e \
  "SELECT api_key FROM openclaw_config.api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)
export TUSHARE_TOKEN

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "============================================================" | tee -a $LOG_FILE
echo "📡 每日管道 v4.0 启动: $(date)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE

START_TIME=$(date +%s)

echo "" | tee -a $LOG_FILE
echo "🚀 全管道启动 (7步，独立子进程防OOM)..." | tee -a $LOG_FILE
python3 -u daily_pipeline.py >> $LOG_FILE 2>&1

END_TIME=$(date +%s)
echo "" >> $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
echo "✅ 管道完成: $(date) (总用时$((END_TIME-START_TIME))s)" | tee -a $LOG_FILE
echo "============================================================" | tee -a $LOG_FILE
