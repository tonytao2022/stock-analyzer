#!/usr/bin/env python3
"""
fetch_limitup_data.py — 打板数据拉取脚本
========================================
数据源: Tushare limit_list_ths / top_list
目标表: stock_db.limit_up_daily / dragon_tiger_daily / board_hot_ranking
功能:
  - 每日增量拉涨停池、连板池数据
  - 龙虎榜数据
  - 统计板块涨停热度排名

用法:
  python3 fetch_limitup_data.py              # 默认拉今天
  python3 fetch_limitup_data.py --date 20260605
  python3 fetch_limitup_data.py --all        # 拉最近5天（含今日）
"""
import os, sys, time, re
from datetime import datetime, date, timedelta
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tushare as ts
from db_config import get_connection, get_tushare_token

TUSHARE_TOKEN = 'd2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad'


def safe_float(v, default=0.0):
    """安全转float，处理None、NaN、空字符串"""
    if v is None:
        return default
    try:
        s = str(v).strip()
        if not s or s.lower() == 'nan' or s.lower() == 'none':
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def safe_int(v, default=0):
    """安全转int，处理None、NaN、空字符串"""
    if v is None:
        return default
    try:
        s = str(v).strip()
        if not s or s.lower() == 'nan' or s.lower() == 'none':
            return default
        return int(float(s))
    except (ValueError, TypeError):
        return default


def safe_str(v, default=''):
    """安全转str，处理None、NaN"""
    if v is None:
        return default
    s = str(v).strip()
    if not s or s.lower() == 'nan':
        return default
    return s


def parse_limit_times(tag):
    """从tag字段解析连板数，如 '7天5板'→5, '首板'→1, '3连板'→3"""
    t = str(tag or '')
    if not t or t.lower() == 'nan':
        return 1
    # 优先匹配 "N连板"
    m = re.search(r'(\d+)连板', t)
    if m:
        return int(m.group(1))
    # 匹配 "N板"
    m = re.search(r'(\d+)板', t)
    if m:
        return int(m.group(1))
    # 首板
    if '首板' in t:
        return 1
    return 1


def setup_tushare():
    token = get_tushare_token() or TUSHARE_TOKEN
    ts.set_token(token)
    return ts.pro_api()


def fetch_single_date(pro, trade_date_str):
    """拉取指定日期的打板数据并入库"""
    conn = get_connection()
    cur = conn.cursor()
    saved = {}

    # ──────────────────────────────────────────
    # 1. 涨停池
    # ──────────────────────────────────────────
    print(f"📊 涨停池: {trade_date_str}")
    try:
        df = pro.query('limit_list_ths', trade_date=trade_date_str, limit_type='涨停池')
        if df is not None and len(df) > 0:
            n = 0
            for _, r in df.iterrows():
                code = r.get('ts_code', '')
                if not code:
                    continue
                tag = safe_str(r.get('tag', ''))
                limit_times = parse_limit_times(tag)

                cur.execute("""
                    INSERT INTO limit_up_daily
                    (trade_date, ts_code, name, close, pct_chg, amount, limit_amount,
                     float_mv, turnover_ratio,
                     open_times, limit_times,
                     concept_tags, limit_type, tag, status,
                     first_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        pct_chg=VALUES(pct_chg),
                        amount=VALUES(amount),
                        limit_amount=VALUES(limit_amount),
                        float_mv=VALUES(float_mv),
                        turnover_ratio=VALUES(turnover_ratio),
                        open_times=VALUES(open_times),
                        limit_times=VALUES(limit_times),
                        concept_tags=VALUES(concept_tags),
                        tag=VALUES(tag),
                        status=VALUES(status),
                        first_time=VALUES(first_time)
                """, (
                    trade_date_str,
                    code,
                    safe_str(r.get('name', '')),
                    safe_float(r.get('price', 0)),
                    safe_float(r.get('pct_chg', 0)),
                    safe_float(r.get('turnover', 0)),         # Tushare 叫 turnover
                    safe_float(r.get('limit_amount', 0)),
                    safe_float(r.get('free_float', 0)),       # 流通市值
                    safe_float(r.get('turnover_rate', 0)),    # Tushare 叫 turnover_rate 存到 turnover_ratio
                    safe_int(r.get('open_num', 0)),           # 安全处理NaN
                    limit_times,
                    safe_str(r.get('lu_desc', '')),           # 文本描述→concept_tags
                    '涨停池',
                    tag[:10] if tag else '',
                    safe_str(r.get('status', ''))[:10],
                    ''                                         # first_time 留空(无对应字段)
                ))
                n += 1
            saved['limit_list_ths(涨停池)'] = n
            print(f"  ✅ 涨停池: {n}条")
        else:
            saved['limit_list_ths(涨停池)'] = 0
            print(f"  ⚠️ 涨停池: 0条")
    except Exception as e:
        print(f"  ❌ 涨停池失败: {e}")
        saved['limit_list_ths(涨停池)'] = f'error: {e}'

    time.sleep(1.2)

    # ──────────────────────────────────────────
    # 2. 连板池
    # ──────────────────────────────────────────
    print(f"📊 连板池: {trade_date_str}")
    try:
        df = pro.query('limit_list_ths', trade_date=trade_date_str, limit_type='连板池')
        if df is not None and len(df) > 0:
            n = 0
            for _, r in df.iterrows():
                code = r.get('ts_code', '')
                if not code:
                    continue
                tag = safe_str(r.get('tag', ''))
                limit_times = parse_limit_times(tag)
                if limit_times < 2:
                    limit_times = 2  # 连板池最低2板

                cur.execute("""
                    INSERT INTO limit_up_daily
                    (trade_date, ts_code, name, close, pct_chg, amount, limit_amount,
                     float_mv, turnover_ratio,
                     open_times, limit_times,
                     concept_tags, limit_type, tag, status,
                     first_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        pct_chg=VALUES(pct_chg),
                        amount=VALUES(amount),
                        limit_amount=VALUES(limit_amount),
                        float_mv=VALUES(float_mv),
                        turnover_ratio=VALUES(turnover_ratio),
                        open_times=VALUES(open_times),
                        limit_times=VALUES(limit_times),
                        concept_tags=VALUES(concept_tags),
                        tag=VALUES(tag),
                        status=VALUES(status),
                        first_time=VALUES(first_time)
                """, (
                    trade_date_str,
                    code,
                    safe_str(r.get('name', '')),
                    safe_float(r.get('price', 0)),
                    safe_float(r.get('pct_chg', 0)),
                    safe_float(r.get('turnover', 0)),
                    safe_float(r.get('limit_amount', 0)),
                    safe_float(r.get('free_float', 0)),
                    safe_float(r.get('turnover_rate', 0)),
                    safe_int(r.get('open_num', 0)),
                    limit_times,
                    safe_str(r.get('lu_desc', '')),
                    '连板池',
                    tag[:10] if tag else '',
                    safe_str(r.get('status', ''))[:10],
                    ''
                ))
                n += 1
            saved['limit_list_ths(连板池)'] = n
            print(f"  ✅ 连板池: {n}条")
        else:
            saved['limit_list_ths(连板池)'] = 0
            print(f"  ⚠️ 连板池: 0条(当天可能无连板数据)")
    except Exception as e:
        print(f"  ❌ 连板池失败: {e}")
        saved['limit_list_ths(连板池)'] = f'error: {e}'

    time.sleep(1.2)

    # ──────────────────────────────────────────
    # 3. 龙虎榜
    # ──────────────────────────────────────────
    print(f"📊 龙虎榜: {trade_date_str}")
    try:
        df = pro.query('top_list', trade_date=trade_date_str)
        if df is not None and len(df) > 0:
            n = 0
            for _, r in df.iterrows():
                l_buy = safe_float(r.get('l_buy', 0))
                l_sell = safe_float(r.get('l_sell', 0))
                # Tushare top_list 有 turnover_rate 字段（换手率），amount 是成交额
                # 注意：amount 值很大，不能存入 decimal(10,4) 的 turnover_ratio
                cur.execute("""
                    INSERT INTO dragon_tiger_daily
                    (trade_date, ts_code, name, close, pct_change,
                     turnover_ratio, l_buy, l_sell, net_buy, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        pct_change=VALUES(pct_change),
                        turnover_ratio=VALUES(turnover_ratio),
                        l_buy=VALUES(l_buy), l_sell=VALUES(l_sell),
                        net_buy=VALUES(net_buy), reason=VALUES(reason)
                """, (
                    trade_date_str,
                    r.get('ts_code', ''),
                    safe_str(r.get('name', '')),
                    safe_float(r.get('close', 0)),
                    safe_float(r.get('pct_change', 0)),
                    safe_float(r.get('turnover_rate', 0)),  # Tushare 真实换手率
                    l_buy,
                    l_sell,
                    l_buy - l_sell,
                    safe_str(r.get('reason', ''))[:500]
                ))
                n += 1
            saved['top_list'] = n
            print(f"  ✅ 龙虎榜: {n}条")
        else:
            saved['top_list'] = 0
            print(f"  ⚠️ 龙虎榜: 0条")
    except Exception as e:
        print(f"  ❌ 龙虎榜失败: {e}")
        saved['top_list'] = f'error: {e}'

    time.sleep(1.0)

    # ──────────────────────────────────────────
    # 4. 涨跌停价格参考（静默，作为辅助）
    # ──────────────────────────────────────────
    try:
        df_stk = pro.query('stk_limit', trade_date=trade_date_str)
        if df_stk is not None:
            print(f"  ℹ️  涨跌停价格参考: {len(df_stk)}条")
    except Exception:
        pass

    conn.commit()
    cur.close()
    conn.close()

    return saved


def compute_board_ranking(trade_date_str=None):
    """计算板块涨停热度排名"""
    if not trade_date_str:
        trade_date_str = date.today().strftime('%Y%m%d')

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO board_hot_ranking
        (trade_date, concept_name, limit_up_count, leader_stock, leader_code, leader_limit_times,
         total_stocks, up_ratio)
        SELECT
            %s as trade_date,
            COALESCE(sb.industry, '未知') as concept_name,
            COUNT(*) as limit_up_count,
            MAX(CASE WHEN lu.limit_times = max_lt.max_t THEN lu.name ELSE NULL END) as leader_stock,
            MAX(CASE WHEN lu.limit_times = max_lt.max_t THEN lu.ts_code ELSE NULL END) as leader_code,
            MAX(lu.limit_times) as leader_limit_times,
            0 as total_stocks,
            0 as up_ratio
        FROM limit_up_daily lu
        LEFT JOIN stock_basic sb ON lu.ts_code = sb.ts_code
        LEFT JOIN (
            SELECT sb2.industry, MAX(lu2.limit_times) as max_t
            FROM limit_up_daily lu2
            LEFT JOIN stock_basic sb2 ON lu2.ts_code = sb2.ts_code
            WHERE lu2.trade_date = %s AND lu2.limit_type IN ('涨停池','连板池')
            GROUP BY sb2.industry
        ) max_lt ON sb.industry = max_lt.industry
        WHERE lu.trade_date = %s AND lu.limit_type IN ('涨停池','连板池')
        GROUP BY sb.industry
        ORDER BY COUNT(*) DESC
        ON DUPLICATE KEY UPDATE
            limit_up_count=VALUES(limit_up_count),
            leader_stock=VALUES(leader_stock), leader_code=VALUES(leader_code),
            leader_limit_times=VALUES(leader_limit_times)
    """, (trade_date_str, trade_date_str, trade_date_str))

    conn.commit()
    affected = cur.rowcount
    print(f"  📊 板块热度排名: {affected}个板块")

    cur.close()
    conn.close()
    return affected


def main():
    import argparse
    ap = argparse.ArgumentParser(description='打板数据拉取')
    ap.add_argument('--date', type=str, default=None, help='日期 YYYYMMDD')
    ap.add_argument('--all', action='store_true', help='拉最近5天（含当日）')
    args = ap.parse_args()

    pro = setup_tushare()

    if args.all:
        today = date.today()
        days = [today - timedelta(days=i) for i in range(5)]
        for d in days:
            ds = d.strftime('%Y%m%d')
            print(f"\n{'='*50}")
            print(f"📅 {ds}")
            fetch_single_date(pro, ds)
            compute_board_ranking(ds)
    else:
        ds = args.date or date.today().strftime('%Y%m%d')
        fetch_single_date(pro, ds)
        compute_board_ranking(ds)

    # 统计
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT trade_date) as `days`, COUNT(*) as `cnt` FROM limit_up_daily")
    r = cur.fetchone()
    cur.execute("SELECT COUNT(DISTINCT trade_date) as `days`, COUNT(*) as `cnt` FROM dragon_tiger_daily")
    r2 = cur.fetchone()
    cur.close()
    conn.close()
    print(f"\n📊 数据库统计: limit_up_daily={r['cnt']}条({r['days']}天) | dragon_tiger_daily={r2['cnt']}条({r2['days']}天)")


if __name__ == '__main__':
    main()
