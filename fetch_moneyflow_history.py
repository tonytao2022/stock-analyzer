#!/usr/bin/env python3
"""
资金流向历史数据补拉器 v1.0
===========================
从 Tushare moneyflow 拉取历史数据 → moneyflow 表（真实资金流向表）
覆盖回测期：2023-01-01 ~ 2026-06-12
"""
import os, sys, time, pymysql, tushare as ts
from db_config import get_connection
from datetime import datetime, date, timedelta

def get_token():
    tk = os.environ.get('TUSHARE_TOKEN', '')
    if tk: return tk
    conn = pymysql.connect(**{**get_connection(), 'database':'openclaw_config'})
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
    r = cur.fetchone()
    cur.close(); conn.close()
    return r[0] if r else ''

def ensure_table():
    """确保 moneyflow 表存在（建表）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyflow (
            id              BIGINT AUTO_INCREMENT PRIMARY KEY,
            ts_code         VARCHAR(16) NOT NULL,
            trade_date      DATE NOT NULL,
            buy_sm_vol      DECIMAL(16,0),
            buy_sm_amount   DECIMAL(16,2),
            sell_sm_vol     DECIMAL(16,0),
            sell_sm_amount  DECIMAL(16,2),
            buy_md_vol      DECIMAL(16,0),
            buy_md_amount   DECIMAL(16,2),
            sell_md_vol     DECIMAL(16,0),
            sell_md_amount  DECIMAL(16,2),
            buy_lg_vol      DECIMAL(16,0),
            buy_lg_amount   DECIMAL(16,2),
            sell_lg_vol     DECIMAL(16,0),
            sell_lg_amount  DECIMAL(16,2),
            buy_elg_vol     DECIMAL(16,0),
            buy_elg_amount  DECIMAL(16,2),
            sell_elg_vol    DECIMAL(16,0),
            sell_elg_amount DECIMAL(16,2),
            net_mf_vol      DECIMAL(16,0),
            net_mf_amount   DECIMAL(16,2),
            create_time     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_ts_date (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.close(); conn.close()

def get_stock_codes():
    """获取所有需拉取股票的 ts_code"""
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    cur.execute("""
        SELECT DISTINCT ts_code FROM backtest_pool WHERE `status`='ACTIVE' AND market!='指数'
    """)
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes

def get_existing_dates(codes):
    """获取已存在的 (ts_code, trade_date) 集合，避免重复拉取"""
    if not codes:
        return set()
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 分批查避免SQL过长
    batch_size = 100
    existing = set()
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        placeholders = ','.join(['%s'] * len(batch))
        cur.execute(f"""
            SELECT ts_code, trade_date FROM moneyflow 
            WHERE ts_code IN ({placeholders})
        """, batch)
        for r in cur.fetchall():
            existing.add((r['ts_code'], str(r['trade_date'])))
    cur.close(); conn.close()
    
    print(f"  已有 {len(existing)} 条记录，将跳过")
    return existing

def fetch_moneyflow_history(pro, ts_code, start_date, end_date):
    """拉取单只股票资金流向历史"""
    try:
        df = pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception as e:
        print(f"  ⚠️ {ts_code} 拉取失败: {e}")
        return None

def prepare_row(row):
    """将 Tushare DataFrame 行转为插入值元组"""
    cols = [
        'buy_sm_vol','buy_sm_amount','sell_sm_vol','sell_sm_amount',
        'buy_md_vol','buy_md_amount','sell_md_vol','sell_md_amount',
        'buy_lg_vol','buy_lg_amount','sell_lg_vol','sell_lg_amount',
        'buy_elg_vol','buy_elg_amount','sell_elg_vol','sell_elg_amount',
        'net_mf_vol','net_mf_amount',
    ]
    values = []
    for c in cols:
        v = row.get(c)
        if v is None or (isinstance(v, float) and (v != v)):  # NaN
            v = 0
        values.append(float(v))
    return [row['ts_code'], row['trade_date']] + values

def main():
    token = get_token()
    if not token:
        print("❌ TUSHARE_TOKEN 未配置")
        return
    ts.set_token(token)
    pro = ts.pro_api()
    
    # 确保表存在
    ensure_table()
    
    # 获取股票池
    codes = get_stock_codes()
    print(f"📋 待拉取: {len(codes)} 只股票")
    
    # 已有数据，跳过
    existing = get_existing_dates(codes)
    
    # 分4期拉取，每期约10个月，避免 Tushare 限制
    periods = [
        ("20230101", "20231031", "2023年1月-10月"),
        ("20231101", "20240831", "2023年11月-2024年8月"),
        ("20240901", "20250612", "2024年9月-2025年6月"),
    ]
    # 最新一期：补充最近2周
    today = date.today()
    two_weeks_ago = today - timedelta(days=14)
    periods.append((two_weeks_ago.strftime('%Y%m%d'), today.strftime('%Y%m%d'), "近2周补拉"))
    
    total_written = 0
    total_skipped = 0
    
    for start_str, end_str, label in periods:
        print(f"\n{'='*60}")
        print(f"📅 阶段: {label} ({start_str} ~ {end_str})")
        
        batch_written = 0
        batch_skipped = 0
        
        conn = get_connection()
        cur = conn.cursor()
        
        for i, code in enumerate(codes):
            if (i+1) % 5 == 0 or i == 0:
                print(f"\r  [{i+1}/{len(codes)}] {code} | 已写{batch_written}条", end='', flush=True)
            
            df = fetch_moneyflow_history(pro, code, start_str, end_str)
            if df is None:
                batch_skipped += 1
                continue
            
            for _, row in df.iterrows():
                trade_date_str = row['trade_date']
                if (code, trade_date_str) in existing:
                    batch_skipped += 1
                    continue
                
                vals = prepare_row(row)
                try:
                    cur.execute("""
                        INSERT IGNORE INTO moneyflow
                            (ts_code, trade_date,
                             buy_sm_vol, buy_sm_amount, sell_sm_vol, sell_sm_amount,
                             buy_md_vol, buy_md_amount, sell_md_vol, sell_md_amount,
                             buy_lg_vol, buy_lg_amount, sell_lg_vol, sell_lg_amount,
                             buy_elg_vol, buy_elg_amount, sell_elg_vol, sell_elg_amount,
                             net_mf_vol, net_mf_amount)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, vals)
                    batch_written += 1
                except Exception as e:
                    print(f"\n  ❌ INSERT失败 {code} {trade_date_str}: {e}")
            
            time.sleep(0.35)  # Tushare 频率限制
            
            # 每20只提交一次
            if (i+1) % 20 == 0:
                conn.commit()
        
        conn.commit()
        cur.close(); conn.close()
        
        total_written += batch_written
        total_skipped += batch_skipped
        print(f"\n  ✅ 写{batch_written}条 | 跳过{batch_skipped}")
    
    print(f"\n{'='*60}")
    print(f"🏁 全部完成! 总写入: {total_written}条, 总跳过: {total_skipped}")
    
    # 最终统计
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT COUNT(*) as cnt FROM moneyflow")
    r = cur.fetchone()
    cur.execute("SELECT COUNT(DISTINCT ts_code) as stocks, MIN(trade_date) as mi, MAX(trade_date) as ma FROM moneyflow")
    s = cur.fetchone()
    cur.close(); conn.close()
    print(f"📊 moneyflow 表现状: {r['cnt']}条, {s['stocks']}只, {s['mi']} ~ {s['ma']}")

if __name__ == '__main__':
    t0 = time.time()
    main()
    print(f"⏱ 总耗时: {time.time()-t0:.1f}s")
