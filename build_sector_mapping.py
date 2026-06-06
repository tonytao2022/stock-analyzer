#!/usr/bin/env python3
"""
build_sector_mapping.py — 监控池股票→中信行业归属映射
===================================================
数据源: Tushare ci_index_member
目标表: stock_db.sector_mapping

用法:
  python3 build_sector_mapping.py          # 全量更新
  python3 build_sector_mapping.py --watch  # 只更新监控池股票
"""
import os, sys, time
from datetime import datetime
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tushare as ts
from db_config import get_connection, get_tushare_token


def setup_tushare():
    token = get_tushare_token()
    if not token:
        token = 'd2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad'
    ts.set_token(token)
    return ts.pro_api()


def get_watch_pool() -> list:
    """从watch_pool获取活跃股票"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [(r['ts_code'], r['name']) for r in rows]


def fetch_stock_sector(pro, ts_code: str):
    """查一只股票的所属中信行业"""
    try:
        df = pro.ci_index_member(ts_code=ts_code)
        if df is not None and len(df) > 0:
            r = df.iloc[0]
            return {
                'ts_code': ts_code,
                'l1_code': r.get('l1_code', ''),
                'l1_name': r.get('l1_name', ''),
                'l2_code': r.get('l2_code', ''),
                'l2_name': r.get('l2_name', ''),
                'l3_code': r.get('l3_code', ''),
                'l3_name': r.get('l3_name', ''),
                'in_date': str(r.get('in_date', ''))[:10] if r.get('in_date') else None,
            }
        return None
    except Exception as e:
        return None


def build_mapping(pro, stocks: list, show_progress=True):
    """构建映射表"""
    conn = get_connection()
    cur = conn.cursor()
    
    total = len(stocks)
    mapped = 0
    errors = []
    
    for i, (code, name) in enumerate(stocks):
        result = fetch_stock_sector(pro, code)
        if result:
            try:
                cur.execute("""
                    INSERT INTO sector_mapping
                    (ts_code, stock_name, l1_code, l1_name, l2_code, l2_name,
                     l3_code, l3_name, in_date, is_active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                    ON DUPLICATE KEY UPDATE
                        stock_name=VALUES(stock_name),
                        l1_code=VALUES(l1_code), l1_name=VALUES(l1_name),
                        l2_code=VALUES(l2_code), l2_name=VALUES(l2_name),
                        l3_code=VALUES(l3_code), l3_name=VALUES(l3_name),
                        in_date=VALUES(in_date), is_active=1
                """, (
                    code, name,
                    result['l1_code'], result['l1_name'],
                    result['l2_code'], result['l2_name'],
                    result['l3_code'], result['l3_name'],
                    result['in_date']
                ))
                mapped += 1
            except Exception as e:
                errors.append(f"{code}: {e}")
        else:
            errors.append(f"{code}: 查无行业")
            # 标记为非活跃
            cur.execute("UPDATE sector_mapping SET is_active=0 WHERE ts_code=%s", (code,))
        
        if show_progress and (i+1) % 10 == 0:
            conn.commit()
            print(f"  ✅ {i+1}/{total}, 已映射 {mapped} 只")
        
        time.sleep(1.1)  # Tushare频次限制
    
    conn.commit()
    cur.close()
    conn.close()
    
    return mapped, errors


def show_statistics():
    """显示统计信息"""
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT l1_name, COUNT(*) as cnt
        FROM sector_mapping
        WHERE is_active=1 AND l1_name IS NOT NULL AND l1_name != ''
        GROUP BY l1_name
        ORDER BY cnt DESC
        LIMIT 25
    """)
    rows = cur.fetchall()
    
    print(f"\n📊 监控池中信一级行业分布:")
    for r in rows:
        name = r['l1_name'] or '未知'
        print(f"  {name}: {r['cnt']}只")
    
    cur.execute("SELECT COUNT(DISTINCT ts_code) as total, SUM(is_active) as active FROM sector_mapping")
    r = cur.fetchone()
    print(f"\n📈 总计: {r['total']} 只, 活跃: {r['active']} 只")
    
    cur.close(); conn.close()


def main():
    import argparse
    ap = argparse.ArgumentParser(description='监控池→中信行业映射')
    ap.add_argument('--watch', action='store_true', help='只更新监控池')
    ap.add_argument('--no-progress', action='store_true', help='不显示进度')
    args = ap.parse_args()
    
    pro = setup_tushare()
    
    stocks = get_watch_pool()
    print(f"📋 待映射股票: {len(stocks)} 只")
    
    show_progress = not args.no_progress
    mapped, errors = build_mapping(pro, stocks, show_progress)
    
    print(f"\n✅ 映射完成: {mapped}/{len(stocks)} 只成功")
    if errors:
        print(f"⚠️ 失败 {len(errors)} 只:")
        for e in errors[:10]:
            print(f"  {e}")
        if len(errors) > 10:
            print(f"  ... 还有 {len(errors)-10} 只")
    
    show_statistics()


if __name__ == '__main__':
    main()
