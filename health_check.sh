#!/bin/bash
# health_check.sh — 三服务健康检查 + 自动重启
# crontab: */5 * * * * /root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现/health_check.sh

set -e
BASE=/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现
LOG=/tmp/health_check.log
ALERT_COUNT_FILE=/tmp/health_alert_count

check_port() {
    local port=$1
    local name=$2
    local url="http://localhost:${port}/health"

    if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [OK] $name:$port" >> $LOG
        return 0
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [FAIL] $name:$port — restarting..." >> $LOG
        return 1
    fi
}

restart_service() {
    local port=$1
    local script=$2
    pkill -f "$script" 2>/dev/null
    sleep 1
    cd $BASE && source venv/bin/activate
    setsid python3 $BASE/$script > /tmp/${script%.*}_$(echo $port | cut -c1-4).log 2>&1 &
    echo "$(date '+%Y-%m-%d %H:%M:%S') [RESTART] $script port=$port PID=$!" >> $LOG
}

# 检查三个服务
failures=0
check_port 8887 "manager" || { restart_service 8887 manager_server.py; failures=$((failures+1)); }
check_port 8888 "trend" || { restart_service 8888 app.py; failures=$((failures+1)); }
check_port 8889 "signal" || { restart_service 8889 signal_server.py; failures=$((failures+1)); }

# 连续3次全部失败 → 告警（读计数器）
if [ $failures -eq 3 ]; then
    cnt=$(cat "$ALERT_COUNT_FILE" 2>/dev/null || echo 0)
    cnt=$((cnt+1))
    echo $cnt > "$ALERT_COUNT_FILE"
    if [ $cnt -ge 3 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [ALERT] All 3 services dead ×$cnt" >> $LOG
        # TODO: 发送告警通知（企业微信/邮件）
    fi
else
    echo 0 > "$ALERT_COUNT_FILE"
fi
