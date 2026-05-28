#!/bin/bash
# 每日数据管道启动脚本 (cron 调用)
cd /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
source venv/bin/activate 2>/dev/null || true

# 从 MySQL 读取 TUSHARE_TOKEN
MYSQL_PWD=$(grep password /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
export TUSHARE_TOKEN=$(mysql -u debian-sys-maint -p"$MYSQL_PWD" openclaw_config -N -e \
  "SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1 LIMIT 1" 2>/dev/null)

LOG_FILE="/tmp/daily_pipeline_$(date +%Y%m%d).log"
echo "=========================================" >> $LOG_FILE
echo "📡 每日管道启动: $(date)" >> $LOG_FILE
echo "=========================================" >> $LOG_FILE

# 跑全量评分（评分引擎内部会从daily_kline读取最新数据）
python3 -c "
from score_engine import ScoreEngineV4
e=ScoreEngineV4()
e.score_pool(save_db=True)
print('✅ 评分完成')
" >> $LOG_FILE 2>&1

# 每日动量报告
python3 daily_momentum_report.py >> $LOG_FILE 2>&1 || echo '⚠️ 动量报告跳过' >> $LOG_FILE

echo "✅ 管道完成: $(date)" >> $LOG_FILE
echo "" >> $LOG_FILE
