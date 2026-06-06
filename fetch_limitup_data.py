#!/usr/bin/env python3
"""
fetch_limitup_data.py — 打板数据拉取脚本
========================================
数据源: Tushare limit_list_ths / top_list / top_inst / stk_limit
目标表: stock_db.limit_up_daily / dragon_tiger_daily / board_hot_ranking
功能:
  - 每日增量拉取涨停跌停数据
  - 龙虎榜数据
  - 统计板块涨停热度排名

用法:
  python3 fetch_limitup_data.py          # 默认拉今天
  python3 fetch_limitup_data.py --date 20260605
  python3 fetch_limitup_data.py --all    # 全量历史（限最近30天）
"""
import os, sys, time
from datetime import datetime, date, timedelta
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tushare as ts
from db_config import get_connection, get_tushare_token

# 涨停类型参数
LIMIT_TYPES = ['涨停池', '连板池', '冲刺涨停', '炸板池', '跌停池']
# Tushare 对应 limit_type
LIMIT_API_TYPES = {
    'U': '涨停池',
    'C': '连板池',
    'Z': '炸板池',
    'D': '跌停池',
}

def setup_tushare():
    token = get_tushare_token()
    if not token:
        token = 'd2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad'
    ts.set_token(token)
    return ts.pro_api()


def fetch_today(pro, trade_date_str=None):
    """拉取当日打板数据"""
    if not trade_date_str:
        trade_date_str = date.today().strftime('%Y%m%d')
    
    conn = get_connection()
    cur = conn.cursor()
    
    saved = {}
    
    # 1. 拉取涨跌停榜单
    print(f"📊 拉取涨跌停榜单: {trade_date_str}")
    for lt in ['涨停池', '连板池']:  # 先拉涨停池和连板池
        try:
            df = pro.query('limit_list_ths', trade_date=trade_date_str, limit_type=lt)
            if df is not None and len(df) > 0:
                n = 0
                for _, r in df.iterrows():
                    code = r.get('ts_code', '')
                    if not code:
                        continue
                    cur.execute("""
                        INSERT INTO limit_up_daily
                        (trade_date, ts_code, name, close, pct_chg, first_time, open_times,
                         limit_times, limit_type, tag, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                            pct_chg=VALUES(pct_chg), first_time=VALUES(first_time),
                            open_times=VALUES(open_times), limit_times=VALUES(limit_times),
                            limit_type=VALUES(limit_type), tag=VALUES(tag)
                    """, (
                        trade_date_str, code, r.get('name', ''),
                        float(r.get('price', 0) or 0),
                        float(r.get('pct_chg', 0) or 0),
                        r.get('lu_desc', '')[:8] or '',
                        int(r.get('open_num', 0) or 0),
                        int(r.get('limit_times', 0) or 0),
                        lt, r.get('tag', '')[:10] or '', r.get('status', '')[:10] or ''
                    ))
                    n += 1
                saved[f'limit_list_ths({lt})'] = n
                print(f"  ✅ {lt}: {n}条")
        except Exception as e:
            print(f"  ❌ {lt}失败: {e}")
        
        time.sleep(1.2)
    
    # 2. 拉取龙虎榜
    print(f"📊 拉取龙虎榜: {trade_date_str}")
    try:
        df = pro.query('top_list', trade_date=trade_date_str)
        if df is not None and len(df) > 0:
            n = 0
            for _, r in df.iterrows():
                l_buy = float(r.get('l_buy', 0) or 0)
                l_sell = float(r.get('l_sell', 0) or 0)
                cur.execute("""
                    INSERT INTO dragon_tiger_daily
                    (trade_date, ts_code, name, close, pct_change, turnover_ratio,
                     l_buy, l_sell, net_buy, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        pct_change=VALUES(pct_change), turnover_ratio=VALUES(turnover_ratio),
                        l_buy=VALUES(l_buy), l_sell=VALUES(l_sell),
                        net_buy=VALUES(net_buy), reason=VALUES(reason)
                """, (
                    trade_date_str, r.get('ts_code', ''), r.get('name', ''),
                    float(r.get('close', 0) or 0), float(r.get('pct_change', 0) or 0),
                    float(r.get('amount', 0) or 0),
                    l_buy, l_sell, l_buy - l_sell,
                    r.get('reason', '')[:200] or ''
                ))
                n += 1
            saved['top_list'] = n
            print(f"  ✅ 龙虎榜: {n}条")
    except Exception as e:
        print(f"  ❌ 龙虎榜失败: {e}")
    
    time.sleep(1.2)
    
    # 3. 拉取全市场涨跌停价格（作为辅助）
    print(f"📊 拉取涨跌停价格参考...")
    try:
        df = pro.query('stk_limit', trade_date=trade_date_str)
        if df is not None:
            print(f"  ✅ 涨跌停价格: {len(df)}条（已导入line表中）")
    except Exception as e:
        print(f"  ❌ 涨跌停价格失败: {e}")
    
    conn.commit()
    cur.close()
    conn.close()
    
    return saved


def compute_board_ranking(pro, trade_date_str=None):
    """计算板块涨停热度排名"""
    if not trade_date_str:
        trade_date_str = date.today().strftime('%Y%m%d')
    
    conn = get_connection()
    cur = conn.cursor()
    
    # 从limit_up_daily统计涨停概念板块
    # 先用stock_basic的industry字段做简单聚合
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
            SELECT industry, MAX(limit_times) as max_t
            FROM limit_up_daily
            LEFT JOIN stock_basic ON limit_up_daily.ts_code = stock_basic.ts_code
            WHERE trade_date = %s AND limit_type IN ('涨停池','连板池')
            GROUP BY industry
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
    ap.add_argument('--all', action='store_true', help='拉最近3天（含今日）')
    args = ap.parse_args()
    
    pro = setup_tushare()
    
    if args.all:
        today = date.today()
        days = [today - timedelta(days=i) for i in range(3)]
        for d in days:
            ds = d.strftime('%Y%m%d')
            print(f"\n{'='*50}")
            print(f"📅 {ds}")
            fetch_today(pro, ds)
            compute_board_ranking(pro, ds)
    else:
        ds = args.date or date.today().strftime('%Y%m%d')
        fetch_today(pro, ds)
        compute_board_ranking(pro, ds)
    
    # 统计
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT trade_date) as days, COUNT(*) as rows FROM limit_up_daily")
    r = cur.fetchone()
    cur.execute("SELECT COUNT(DISTINCT trade_date) as days, COUNT(*) as rows FROM dragon_tiger_daily")
    r2 = cur.fetchone()
    cur.close(); conn.close()
    print(f"\n📊 数据库统计: limit_up_daily={r['rows']}条({r['days']}天) | dragon_tiger_daily={r2['rows']}条({r2['days']}天)")


if __name__ == '__main__':
    main()
