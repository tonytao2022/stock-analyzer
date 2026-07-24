#!/bin/bash
# 数据库备份脚本 v3 — mysqldump+gzip → git push
# 备份 stock_db_v2（V2新系统主库）
set -e

BACKUP_DIR="/root/stock-system-v2/db_backup"
REPO_DIR="/root/stock-system-v2"
DUMP_DB="stock_db_v2"
DUMP_FILE_BASE="${BACKUP_DIR}/stock_db_v2_$(date +%Y%m%d)"
DUMP_GZ="${DUMP_FILE_BASE}.sql.gz"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "========================================"
echo "📦 数据库备份开始: ${TIMESTAMP}"
echo "========================================"

mkdir -p "${BACKUP_DIR}"

# 读MySQL密码
MYSQL_PASS=$(grep 'password' /etc/mysql/debian.cnf | head -1 | awk -F'= ' '{print $2}' | xargs)
MYSQL_USER="debian-sys-maint"

# 删除旧备份（保留7天）
find "${BACKUP_DIR}" -name "stock_db_v2_*.sql.gz" -mtime +7 -delete 2>/dev/null || true

echo "📤 导出 ${DUMP_DB} 数据库 (gzip压缩)..."
mysqldump -u"${MYSQL_USER}" -p"${MYSQL_PASS}" \
  --single-transaction \
  --routines \
  --triggers \
  --set-gtid-purged=OFF \
  --complete-insert \
  --skip-lock-tables \
  ${DUMP_DB} | gzip > "${DUMP_GZ}"

DUMP_SIZE=$(stat --format=%s "${DUMP_GZ}" 2>/dev/null || stat -f%z "${DUMP_GZ}" 2>/dev/null)
DUMP_SIZE_HUMAN=$(numfmt --to=iec ${DUMP_SIZE} 2>/dev/null || echo "${DUMP_SIZE} bytes")

# 查表数
TABLE_COUNT=$(mysql -u"${MYSQL_USER}" -p"${MYSQL_PASS}" -N -e \
  "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='${DUMP_DB}' AND TABLE_TYPE='BASE TABLE'" 2>/dev/null)

echo "✅ 导出完成: ${DUMP_SIZE_HUMAN} (${TABLE_COUNT}张表)"

# 删除未压缩版本
rm -f "${DUMP_FILE_BASE}.sql"

# ── Git 提交到 stock-system-v2 仓库 ──
cd "${REPO_DIR}"

git add "db_backup/stock_db_v2_$(date +%Y%m%d).sql.gz"

if git diff --cached --quiet -- db_backup/ 2>/dev/null; then
  echo "ℹ️ 数据库无变更，跳过提交"
else
  echo "📤 提交到 stock-system-v2 仓库..."
  git commit -m "chore: 数据库备份 ${TIMESTAMP}

数据库: ${DUMP_DB} (${TABLE_COUNT}张表)
文件: stock_db_v2_$(date +%Y%m%d).sql.gz
大小: ${DUMP_SIZE_HUMAN}
备份方式: mysqldump | gzip"

  echo "📤 推送到 GitHub..."
  git push origin master 2>&1 | tail -5
fi

echo ""
echo "✅ 备份完成!"
echo "   数据库: ${DUMP_DB}"
echo "   文件: stock_db_v2_$(date +%Y%m%d).sql.gz"
echo "   大小: ${DUMP_SIZE_HUMAN}"
echo "   时间: $(date '+%Y-%m-%d %H:%M:%S')"
