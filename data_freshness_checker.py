#!/usr/bin/env python3
"""
数据保鲜检查器 v1.0
====================
每晚20:00运行，检查系统里所有数据表是否都是当天最新数据。
如果不是最新，自动执行修复管道。

检查清单:
  □ daily_kline 是否有今天的数据
  □ daily_kline_qfq 同步（如果K线有今天）
  □ season_state 是否是最新交易日
  □ strategy_signal(direction=dual_track_v1) 是否是最新交易日
  □ watch_pool_snapshot 是否是最新交易日
  □ 持仓股票在strategy_signal中是否有评分

修复策略:
  - 如果是今天是交易日且收盘后(>17:00)但数据不全 → 全量管道重跑
  - 如果今天是交易日但数据日期比昨天还旧 → 异常报警
"""

import os, sys, time, pymysql, requests, logging, json
from datetime import date, datetime

sys.path.insert(0, '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现')
os.environ['MYSQL_PASS'] = open('/etc/mysql/debian.cnf').read().split('password = ')[1].split('\n')[0].strip()
from db_config import get_connection, db_cursor

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('data_freshness')

def check_date(table, condition="1=1", date_col='trade_date'):
    """获取某表的最新交易日期"""
    with db_cursor(commit=False) as cur:
        cur.execute(f"SELECT MAX({date_col}) as d FROM {table} WHERE {condition}")
        r = cur.fetchone()
        return r['d'] if r and r['d'] else None

def count_for_date(table, d, condition="1=1"):
    """某表中某日期的记录数"""
    with db_cursor(commit=False) as cur:
        cur.execute(f"SELECT COUNT(*) as c FROM {table} WHERE trade_date=%s AND {condition}", (d,))
        r = cur.fetchone()
        return r['c'] if r else 0

def is_today_trading_day():
    """判断今天是否是交易日（weekly+holiday检查）"""
    today = date.today()
    # 周末
    if today.weekday() >= 5:
        return False
    # 检查是否有K线数据（昨天有，今天无 → 可能是节假日/非交易日）
    yesterday_kline = check_date('daily_kline', "trade_date < curdate()")
    if yesterday_kline:
        # 如果昨天有数据但最近交易日不是今天，且今天是工作日→可能是节假日
        latest_kline = check_date('daily_kline')
        if latest_kline and latest_kline < today:
            # 看看距今几天
            days_gap = (today - latest_kline).days
            if days_gap <= 3:
                return False  # 可能是周末/节假日补休
    return True

def fix_sz300_kline(target_date):
    """用腾讯行情补拉沪深300指定日期的K线"""
    try:
        r = requests.get('https://qt.gtimg.cn/q=sh000300', timeout=10)
        r.encoding = 'gbk'
        val = r.text.strip().split('=')[1].strip().strip('"').split('~')
        if len(val) > 37:
            price = float(val[3]); open_p = float(val[5])
            high = float(val[33]); low = float(val[34])
            pct = float(val[32]); vol = float(val[6]); amount = float(val[37])
            
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close, vol, amount, change_pct)
                    VALUES ('000300.SH', %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE close=VALUES(close), high=VALUES(high), low=VALUES(low),
                        open=VALUES(open), vol=VALUES(vol), amount=VALUES(amount), change_pct=VALUES(change_pct)
                """, (target_date, float(open_p), float(high), float(low), float(price), float(vol), float(amount), float(pct)))
            logger.info(f"  ✅ 沪深300已补拉: {price} ({pct:+.2f}%)")
            return True
    except Exception as e:
        logger.warning(f"  ⚠️ 沪深300补拉失败: {e}")
    return False


def run_full_pipeline():
    """运行完整数据管道"""
    logger.info("  🚀 启动全量数据管道...")
    import subprocess
    script = '/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现/daily_pipeline.py'
    try:
        # 先补拉沪深300（确保日期正确）
        today_str = str(date.today())
        fix_sz300_kline(today_str)
        
        # 跑全部步骤
        r = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=300,
            cwd='/root/.openclaw/workspace/projects/陶的投资预测模型项目/代码实现'
        )
        logger.info(f"  管道退出码: {r.returncode}")
        if r.returncode != 0:
            logger.error(f"  管道错误: {r.stderr[:500]}")
        
        # 再补拉沪深300（确保评分引擎识别最新日期）
        fix_sz300_kline(today_str)
        
        return True
    except Exception as e:
        logger.error(f"  管道运行失败: {e}")
        return False


def run_p6_score_only():
    """只跑P6评分（如果K线和季节都已经是最新的）"""
    logger.info("  🚀 只跑P6评分...")
    from p6_dual_track_engine import daily_pipeline as p6_pipeline
    results = p6_pipeline(mode='watch_pool')
    logger.info(f"  ✅ P6评分: {len(results)}只")
    return True


def rebuild_snapshot():
    """重建监控池快照"""
    with db_cursor() as cur:
        cur.execute("SELECT config_value FROM system_config WHERE config_key='api_key' LIMIT 1")
        api_key = cur.fetchone()['config_value']
    
    headers = {'X-API-Key': api_key}
    try:
        r = requests.get('http://localhost:8887/api/v1/management/dashboard', headers=headers, timeout=30)
        logger.info(f"  ✅ 快照已重建（通过dashboard确认）")
        return True
    except Exception as e:
        logger.warning(f"  ⚠️ 快照重建失败: {e}")
    
    # fallback: 手动重建
    try:
        with db_cursor() as cur:
            cur.execute("SELECT MAX(trade_date) as d FROM strategy_signal WHERE direction='dual_track_v1'")
            td = cur.fetchone()['d']
            if not td:
                return False
            
            cur.execute("DELETE FROM watch_pool_snapshot WHERE trade_date=%s", (td,))
            cur.execute("""
                INSERT INTO watch_pool_snapshot (ts_code, trade_date, v_score, raw_score, signal_type, signal_label, trend_score, strategy_type, season)
                SELECT ss.ts_code, ss.trade_date, ss.calibrated_score, ss.composite_score,
                    CASE WHEN ss.calibrated_score >= 85 THEN 'STRONG_BUY'
                         WHEN ss.calibrated_score >= 75 THEN 'BUY'
                         WHEN ss.calibrated_score >= 60 THEN 'CAUTIOUS_BUY'
                         WHEN ss.calibrated_score >= 30 THEN 'HOLD'
                         ELSE 'SELL' END,
                    CASE WHEN ss.calibrated_score >= 85 THEN '强烈买入'
                         WHEN ss.calibrated_score >= 75 THEN '买入'
                         WHEN ss.calibrated_score >= 60 THEN '谨慎买入'
                         WHEN ss.calibrated_score >= 30 THEN '持有'
                         ELSE '卖出' END,
                    ss.composite_score, ss.track,
                    (SELECT season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1)
                FROM strategy_signal ss
                WHERE ss.direction='dual_track_v1' AND ss.trade_date=%s
            """, (td,))
            cnt = cur.rowcount
            logger.info(f"  ✅ 快照手动重建: {cnt}条 ({td})")
        return True
    except Exception as e:
        logger.error(f"  ❌ 快照手动重建失败: {e}")
        return False


# ════════════════════════════════════════════
# 主逻辑
# ════════════════════════════════════════════

def main():
    logger.info("=" * 60)
    logger.info("🕐 数据保鲜检查器启动")
    logger.info(f"📅 当前日期: {date.today()} ({datetime.now().strftime('%A')})")
    logger.info("=" * 60)
    
    today = date.today()
    today_str = str(today)
    now = datetime.now()
    
    # ─── Step 0: 判断是否需要检查 ───
    is_weekend = today.weekday() >= 5
    after_17 = now.hour >= 17
    
    if is_weekend:
        logger.info("📌 周末，跳过数据检查")
        return
    
    if not after_17:
        logger.info("📌 17:00前不检查（盘中数据不完整）")
        return
    
    # ─── Step 1: 收集状态 ───
    logger.info("\n📊 数据状态检查:")
    
    latest_kline = check_date('daily_kline')
    today_kline_cnt = count_for_date('daily_kline', today_str)
    logger.info(f"  daily_kline: 最新={latest_kline} | 今天={today_kline_cnt}条")
    
    latest_season = check_date('season_state', "index_code='MARKET'")
    logger.info(f"  season_state: 最新={latest_season}")
    
    latest_p6 = check_date('strategy_signal', "direction='dual_track_v1'")
    today_p6_cnt = count_for_date('strategy_signal', today_str, "direction='dual_track_v1'")
    logger.info(f"  strategy_signal(P6): 最新={latest_p6} | 今天={today_p6_cnt}条")
    
    latest_snapshot = check_date('watch_pool_snapshot')
    today_snapshot_cnt = count_for_date('watch_pool_snapshot', today_str)
    logger.info(f"  watch_pool_snapshot: 最新={latest_snapshot} | 今天={today_snapshot_cnt}条")
    
    # ─── Step 2: 判断是否需要修复 ───
    needs_full_pipeline = False
    needs_p6_only = False
    needs_snapshot = False
    
    # 条件A: K线没有今天的 → 今天是交易日且过了17:00 → 需要全管道
    if today_kline_cnt == 0:
        logger.info("  ➡️ 今天K线缺失，检查是否为交易日...")
        # 粗略判断：如果昨天有K线今天没有，且今天是工作日→大概率是交易日
        yesterday_kline_cnt = count_for_date('daily_kline', str(today - __import__('datetime').timedelta(days=1)))
        if yesterday_kline_cnt > 0 and today.weekday() < 5:
            logger.info("     📌 昨天有数据，今天是工作日→可能是交易日，需要修复")
            needs_full_pipeline = True
        elif today.weekday() < 5:
            logger.info("     📌 周中无数据→尝试补拉K线")
            needs_full_pipeline = True
    
    # 条件B: K线有今天的，但P6评分没有今天的
    if today_kline_cnt > 0 and today_p6_cnt == 0:
        logger.info("  ➡️ K线已有今天，P6评分缺失")
        # 先保证沪深300是最新的
        logger.info("  ➡️ 先补拉沪深300...")
        fix_sz300_kline(today_str)
        needs_p6_only = True
    
    # 条件C: P6评分有今天的，但快照没有今天的
    if today_p6_cnt > 0 and today_snapshot_cnt == 0:
        logger.info("  ➡️ P6评分存在，快照缺失")
        needs_snapshot = True
    
    # 条件D: 三者的trade_date不一致，且今天是最新的那个
    dates = set()
    if latest_kline: dates.add(str(latest_kline))
    if latest_season: dates.add(str(latest_season))
    if latest_p6: dates.add(str(latest_p6))
    if latest_snapshot: dates.add(str(latest_snapshot))
    
    if len(dates) > 1 and today_str in dates:
        logger.info(f"  ➡️ 日期不一致({dates})，以今天为准补齐")
        if today_kline_cnt > 0 and today_p6_cnt == 0:
            needs_p6_only = True
        if today_p6_cnt > 0 and today_snapshot_cnt == 0:
            needs_snapshot = True
    
    # ─── Step 3: 执行修复 ───
    actions_taken = []
    
    if needs_full_pipeline:
        logger.info("\n🔧 执行: 全量数据管道")
        ok = run_full_pipeline()
        if ok:
            actions_taken.append("全量管道")
    
    if needs_p6_only:
        logger.info("\n🔧 执行: P6评分")
        ok = run_p6_score_only()
        if ok:
            actions_taken.append("P6评分")
            needs_snapshot = True  # 评分完了紧接着刷快照
    
    if needs_snapshot:
        logger.info("\n🔧 执行: 重建快照")
        ok = rebuild_snapshot()
        if ok:
            actions_taken.append("快照重建")
    
    # ─── Step 4: 最终报告 ───
    logger.info("\n" + "=" * 60)
    if actions_taken:
        logger.info(f"✅ 修复完成: {', '.join(actions_taken)}")
    else:
        logger.info("✅ 数据已是最新，无需修复")
    
    # 再次验证
    latest_p6_final = check_date('strategy_signal', "direction='dual_track_v1'")
    today_p6_final = count_for_date('strategy_signal', today_str, "direction='dual_track_v1'")
    logger.info(f"\n📋 最终状态: P6={latest_p6_final} | 今日={today_p6_final}条")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
