#!/usr/bin/env python3
"""
数据导入脚本 — 第一步: stock_basic + stock_pool + daily_kline
从 Tushare 拉取沪深300成分股基础信息及近2年日K线，写入 MySQL stock_db
"""
import os, sys, time, logging
import pymysql
import tushare as ts
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection, get_tushare_token

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('import_data')

TOKEN = get_tushare_token()
if not TOKEN:
    log.error("TUSHARE_TOKEN 未设置!")
    sys.exit(1)

ts.set_token(TOKEN)
pro = ts.pro_api()

BATCH_SIZE = 500  # 每批写入数量
KLINE_YEARS = 2   # 拉取近N年K线

def ensure_tushare_call(fn, **kwargs):
    """带重试的 Tushare 调用"""
    for attempt in range(3):
        try:
            df = fn(**kwargs)
            if df is not None and len(df) > 0:
                return df
            log.warning(f"Tushare 返回空数据: {fn.__name__} {kwargs}, 重试 {attempt+1}/3")
        except Exception as e:
            log.warning(f"Tushare 调用失败: {e}, 重试 {attempt+1}/3")
            time.sleep(2 ** attempt)
    return None

def import_stock_basic():
    """Step 1: 导入全量A股基础信息"""
    log.info("=" * 50)
    log.info("Step 1: 导入 stock_basic (全量A股)")
    conn = get_connection()
    cur = conn.cursor()

    df = ensure_tushare_call(pro.stock_basic, exchange='', list_status='L',
                              fields='ts_code,symbol,name,area,industry,market,list_date,is_hs,act_name')
    if df is None:
        log.error("无法获取 stock_basic")
        conn.close()
        return []

    log.info(f"获取到 {len(df)} 只A股基本信息")
    inserted = 0
    for _, row in df.iterrows():
        try:
            cur.execute("""
                INSERT INTO stock_basic (ts_code, symbol, name, area, industry, market, list_date, is_hs, act_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE name=VALUES(name), industry=VALUES(industry), area=VALUES(area), is_hs=VALUES(is_hs)
            """, (row['ts_code'], row['symbol'], row['name'], row.get('area'), row.get('industry'),
                  row.get('market'), row.get('list_date'), row.get('is_hs'), row.get('act_name')))
            inserted += 1
            if inserted % 1000 == 0:
                conn.commit()
                log.info(f"  已写入 {inserted}/{len(df)} 条...")
        except Exception as e:
            log.warning(f"  写入 {row['ts_code']} 失败: {e}")

    conn.commit()
    log.info(f"✅ stock_basic 导入完成: {inserted} 条")

    # 获取沪深300成分股
    hs300 = ensure_tushare_call(pro.index_member, index_code='000300.SH',
                                 fields='index_code,con_code')
    hs300_codes = set()
    if hs300 is not None:
        hs300_codes = set(hs300['con_code'].tolist())
        log.info(f"获取到 {len(hs300_codes)} 只沪深300成分股")

    cur.close()
    conn.close()
    return list(hs300_codes)

def import_stock_pool(hs300_codes):
    """Step 2: 设置股票池（沪深300作为默认池）"""
    log.info("=" * 50)
    log.info("Step 2: 导入 stock_pool (沪深300)")
    conn = get_connection()
    cur = conn.cursor()

    # Also add some popular 创业板/SME stocks for broader coverage
    extra_codes = []
    try:
        # 创业板 + 中小板精选（市值前100）
        extra = ensure_tushare_call(pro.stock_basic, exchange='', list_status='L',
                                     fields='ts_code')
        if extra is not None:
            extra_codes = [c for c in extra['ts_code'].tolist()
                          if c not in hs300_codes and ('.SZ' in c or '.SH' in c)]
            # 只取前100只作为扩展池
            extra_codes = extra_codes[:100]
    except Exception as e:
        log.warning(f"扩展股票池获取失败: {e}")

    all_pool = list(hs300_codes) + extra_codes
    log.info(f"股票池总数: {len(all_pool)} (沪深300: {len(hs300_codes)}, 扩展: {len(extra_codes)})")

    today_str = date.today().isoformat()
    inserted = 0
    for i, code in enumerate(all_pool):
        try:
            pool_name = 'hs300' if code in hs300_codes else 'extended'
            priority = 1 if code in hs300_codes else 5
            cur.execute("""
                INSERT INTO stock_pool (ts_code, pool_name, status, add_date, add_reason, priority)
                VALUES (%s, %s, 'ACTIVE', %s, %s, %s)
                ON DUPLICATE KEY UPDATE pool_name=VALUES(pool_name), status='ACTIVE'
            """, (code, pool_name, today_str, '初始导入-沪深300' if pool_name == 'hs300' else '初始导入-扩展池', priority))
            inserted += 1
        except Exception as e:
            log.warning(f"  写入 stock_pool {code} 失败: {e}")

    conn.commit()
    cur.close()
    conn.close()
    log.info(f"✅ stock_pool 导入完成: {inserted} 条")
    return all_pool

def import_daily_kline(pool_codes):
    """Step 3: 拉取近2年日K线"""
    log.info("=" * 50)
    log.info(f"Step 3: 导入 daily_kline ({KLINE_YEARS}年, {len(pool_codes)}只股票)")
    conn = get_connection()
    cur = conn.cursor()

    end_date = date.today().strftime('%Y%m%d')
    start_date = (date.today() - timedelta(days=365 * KLINE_YEARS)).strftime('%Y%m%d')

    total_inserted = 0
    error_codes = []

    for i, code in enumerate(pool_codes):
        try:
            df = ensure_tushare_call(pro.daily, ts_code=code, start_date=start_date, end_date=end_date)
            if df is None or len(df) == 0:
                error_codes.append(code)
                continue

            df = df.sort_values('trade_date')
            batch = []
            for _, row in df.iterrows():
                batch.append((
                    code, row['trade_date'],
                    float(row['open']), float(row['high']), float(row['low']), float(row['close']),
                    float(row.get('pre_close', 0)) if row.get('pre_close') is not None else None,
                    float(row.get('pct_chg', 0)) if row.get('pct_chg') is not None else None,
                    float(row.get('vol', 0)) if row.get('vol') is not None else None,
                    float(row.get('amount', 0)) if row.get('amount') is not None else None,
                    float(row.get('turnover_rate', 0)) if row.get('turnover_rate') is not None else None,
                    float(row.get('volume_ratio', 0)) if row.get('volume_ratio') is not None else None,
                    float(row.get('pe', 0)) if row.get('pe') is not None else None,
                    float(row.get('pb', 0)) if row.get('pb') is not None else None,
                    float(row.get('total_mv', 0)) if row.get('total_mv') is not None else None,
                    'tushare'
                ))

            # 批量写入
            for j in range(0, len(batch), BATCH_SIZE):
                chunk = batch[j:j + BATCH_SIZE]
                cur.executemany("""
                    INSERT INTO daily_kline (ts_code, trade_date, open, high, low, close,
                        pre_close, change_pct, vol, amount, turnover_rate, volume_ratio,
                        pe, pb, total_mv, data_source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close),
                        pre_close=VALUES(pre_close), change_pct=VALUES(change_pct),
                        vol=VALUES(vol), amount=VALUES(amount),
                        turnover_rate=VALUES(turnover_rate), volume_ratio=VALUES(volume_ratio),
                        pe=VALUES(pe), pb=VALUES(pb), total_mv=VALUES(total_mv)
                """, chunk)
                conn.commit()

            total_inserted += len(batch)

            if (i + 1) % 20 == 0:
                log.info(f"  进度: {i+1}/{len(pool_codes)}, 已写入 {total_inserted} 条K线...")
            elif (i + 1) % 5 == 0:
                log.info(f"  [{i+1}/{len(pool_codes)}] {code} → {len(batch)}条K线")

            # Tushare 频率控制
            time.sleep(0.3)

        except Exception as e:
            log.warning(f"  获取 {code} K线失败: {e}")
            error_codes.append(code)

    cur.close()
    conn.close()
    log.info(f"✅ daily_kline 导入完成: {total_inserted} 条")
    if error_codes:
        log.warning(f"⚠️ {len(error_codes)} 只股票K线获取失败: {error_codes[:10]}...")
    return total_inserted


def main():
    log.info("=" * 60)
    log.info("🚀 股票智能分析管理系统 — 数据导入")
    log.info(f"  日期: {date.today().isoformat()}")
    log.info(f"  Tushare Token: {TOKEN[:8]}...")
    log.info("=" * 60)

    # Step 1: 股票基础信息
    hs300_codes = import_stock_basic()
    if not hs300_codes:
        log.warning("未获取到沪深300成分股，将使用全量A股作为股票池")
        # Fallback: 取前300只A股
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ts_code FROM stock_basic LIMIT 300")
        hs300_codes = [r['ts_code'] for r in cur.fetchall()]
        cur.close()
        conn.close()

    # Step 2: 股票池
    pool_codes = import_stock_pool(hs300_codes)

    # Step 3: K线数据
    kline_count = import_daily_kline(pool_codes)

    log.info("=" * 60)
    log.info(f"✅ 数据导入完成!")
    log.info(f"  股票基础信息: 全量A股")
    log.info(f"  股票池: {len(pool_codes)} 只")
    log.info(f"  K线数据: {kline_count} 条")
    log.info("=" * 60)


if __name__ == '__main__':
    main()
