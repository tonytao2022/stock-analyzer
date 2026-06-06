#!/usr/bin/env python3
"""
fetch_sector_daily.py — 中信行业指数日线拉取脚本
============================================
数据源: Tushare ci_daily
目标表: stock_db.sector_index_daily
功能: 
  - 初始化拉取全部437个行业历史K线
  - 每日增量更新

用法:
  python3 fetch_sector_daily.py --init      # 首次全量拉取
  python3 fetch_sector_daily.py --today     # 只补当天
  python3 fetch_sector_daily.py --all       # 全量拉取（含历史+当天）
"""
import os, sys, time
import math
from datetime import datetime, date, timedelta
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tushare as ts
from db_config import get_connection, get_tushare_token

# Tushare 一级行业代码范围（CI005001~CI005024 共30个一级行业）
# 通过 ci_daily(trade_date=xxx) 取全量437个，然后过滤 level=L1
# Tushare 没有直接返回level字段，需要从code范围推断
# CI005001~CI005024 是一级行业
L1_CODE_RANGE = [f'CI005{i:03d}.CI' for i in range(1, 31)] + \
                [f'CI005{i:03d}.CI' for i in range(100, 132)]  # 扩展一级

# 常用一级行业中文名（从stock_basic.industry推断对照）
# ci_daily 不直接返回名称，需要用 ci_index_member 推算或用硬编码对照
SECTOR_NAMES = {
    'CI005001.CI': '银行', 'CI005002.CI': '综合', 'CI005003.CI': '煤炭',
    'CI005004.CI': '钢铁', 'CI005005.CI': '有色金属', 'CI005006.CI': '建筑',
    'CI005007.CI': '建材', 'CI005008.CI': '电力及公用事业', 'CI005009.CI': '基础化工',
    'CI005010.CI': '电子', 'CI005011.CI': '计算机', 'CI005012.CI': '传媒',
    'CI005013.CI': '通信', 'CI005014.CI': '食品饮料', 'CI005015.CI': '医药',
    'CI005016.CI': '农林牧渔', 'CI005017.CI': '纺织服装', 'CI005018.CI': '轻工制造',
    'CI005019.CI': '商贸零售', 'CI005020.CI': '房地产', 'CI005021.CI': '交通运输',
    'CI005022.CI': '国防军工', 'CI005023.CI': '汽车', 'CI005024.CI': '家电',
    'CI005025.CI': '电力设备及新能源', 'CI005026.CI': '机械', 'CI005027.CI': '石油化工',
    'CI005028.CI': '消费者服务', 'CI005029.CI': '非银行金融', 'CI005030.CI': '保险',
}


def setup_tushare():
    token = get_tushare_token()
    if not token:
        token = 'd2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad'
    ts.set_token(token)
    return ts.pro_api()


def _to_num(v, default=0.0):
    """安全转换数值，处理 NaN 和 None"""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def fetch_all_sectors(pro, trade_date: str = None) -> list:
    """拉取当日全部中信行业指数"""
    try:
        if trade_date:
            df = pro.ci_daily(trade_date=trade_date)
        else:
            df = pro.ci_daily()
        if df is None or df.empty:
            print(f"  ⚠️ ci_daily无数据 (trade_date={trade_date})")
            return []
        records = []
        for _, row in df.iterrows():
            code = row['ts_code']
            records.append({
                'ts_code': code,
                'trade_date': row.get('trade_date', trade_date),
                'open': _to_num(row.get('open')),
                'high': _to_num(row.get('high')),
                'low': _to_num(row.get('low')),
                'close': _to_num(row.get('close')),
                'pre_close': _to_num(row.get('pre_close')),
                'change': _to_num(row.get('change')),
                'pct_change': _to_num(row.get('pct_change')),
                'vol': _to_num(row.get('vol')),
                'amount': _to_num(row.get('amount')),
            })
        return records
    except Exception as e:
        print(f"  ❌ ci_daily 拉取失败: {e}")
        return []


def save_records(records: list, level: str = None, is_init: bool = False):
    """写入 sector_index_daily 表"""
    if not records:
        return 0
    conn = get_connection()
    cur = conn.cursor()
    saved = 0
    for r in records:
        code = r['ts_code']
        # 确定行业等级
        lv = level
        if not lv:
            if code.startswith('CI005') and code[7:9] != '00':
                lv = 'L1'
            elif code.startswith('CI005') or code.startswith('CI006'):
                lv = 'L2'
            else:
                lv = 'L3'
        # 名称
        name = SECTOR_NAMES.get(code, '')
        try:
            if is_init:
                sql = """
                    INSERT IGNORE INTO sector_index_daily
                    (ts_code, index_name, trade_date, open, high, low, close,
                     pre_close, `change`, pct_change, vol, amount, level)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """
            else:
                sql = """
                    INSERT INTO sector_index_daily
                    (ts_code, index_name, trade_date, open, high, low, close,
                     pre_close, `change`, pct_change, vol, amount, level)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), high=VALUES(high), low=VALUES(low),
                        close=VALUES(close), pre_close=VALUES(pre_close),
                        `change`=VALUES(`change`), pct_change=VALUES(pct_change),
                        vol=VALUES(vol), amount=VALUES(amount)
                """
            cur.execute(sql, (
                code, name, r['trade_date'], r['open'], r['high'], r['low'],
                r['close'], r['pre_close'], r['change'], r['pct_change'],
                r['vol'], r['amount'], lv
            ))
            saved += 1
        except Exception as e:
            print(f"  ⚠️ 入库失败 {code}/{r['trade_date']}: {e}")
    conn.commit()
    cur.close()
    conn.close()
    return saved


def init_history(pro):
    """全量历史拉取（2023-01-01 起）
    
    策略：
    1. 先通过 trade_cal 过滤出A股交易日，只在这些日期调用 ci_daily
    2. 每个交易日 ci_daily(trade_date=YYYYMMDD) 返回当日全量437个行业指数
    3. 减少不必要的API调用（跳过周末和节假日）
    
    加速优化：由于 Tushare 免费版200次/分钟，sleep从1.1s降至0.4s
    """
    print("=" * 60)
    print("📦 全量历史拉取（中信行业指数）")
    print("=" * 60)
    
    # 获取已有数据范围
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MIN(trade_date) as min_dt, MAX(trade_date) as max_dt FROM sector_index_daily")
    r = cur.fetchone()
    cur.close(); conn.close()
    
    start_date = '2023-01-01'
    end_date = date.today().isoformat()
    
    if r and r['max_dt']:
        max_dt = r['max_dt']
        if isinstance(max_dt, datetime):
            max_dt = max_dt.date()
        start = max_dt + timedelta(days=1)
        start_date = start.isoformat()
        dt_str = r['max_dt'].isoformat() if hasattr(r['max_dt'], 'isoformat') else str(r['max_dt'])
        print(f"  已有数据截止 {dt_str}，从 {start_date} 开始补充")
    
    # 获取A股交易日历，只取交易日 API: trade_cal
    try:
        cal = pro.trade_cal(exchange='SSE', start_date=start_date.replace('-', ''),
                            end_date=end_date.replace('-', ''))
        if cal is not None and len(cal) > 0:
            batch_days = sorted(cal[cal['is_open'] == 1]['cal_date'].tolist())
            print(f"  📅 通过交易日历过滤: {len(batch_days)} 个交易日")
        else:
            batch_days = []
    except Exception as e:
        print(f"  ⚠️ 交易日历获取失败: {e}，降级为逐日尝试")
        batch_days = []
        current = datetime.strptime(start_date, '%Y-%m-%d').date()
        e = datetime.strptime(end_date, '%Y-%m-%d').date()
        while current <= e:
            batch_days.append(current.isoformat().replace('-', ''))
            current += timedelta(days=1)
    
    if not batch_days:
        print("  ℹ️ 没有需要补充的数据")
        return
    
    total = 0
    no_data_days = 0
    api_calls = 0
    
    for i, day in enumerate(batch_days):
        records = fetch_all_sectors(pro, trade_date=day)
        api_calls += 1
        if records:
            n = save_records(records, is_init=True)
            total += n
        else:
            no_data_days += 1
        if (i+1) % 50 == 0:
            pct = (i+1)/len(batch_days)*100
            print(f"  ✅ 已处理 {i+1}/{len(batch_days)} 天 ({pct:.0f}%), "
                  f"入库 {total} 条, 跳 {no_data_days} 天, API: {api_calls} 次")
        # Tushare 免费版 200次/分钟，留余量 sleep 0.5s = 120次/分钟
        time.sleep(0.5)
    
    pct = 100
    print(f"  ✅ 已处理 {len(batch_days)}/{len(batch_days)} 天 ({pct:.0f}%), "
          f"入库 {total} 条, 跳 {no_data_days} 天, API: {api_calls} 次")
    print(f"\n✅ 历史拉取完成: 入库 {total} 条, 跳过 {no_data_days} 天(无数据)")


def today_update(pro):
    """今日增量更新"""
    print("=" * 60)
    print("📅 今日增量更新")
    print("=" * 60)
    
    today = date.today().isoformat().replace('-', '')
    records = fetch_all_sectors(pro, trade_date=today)
    
    if records:
        n = save_records(records, is_init=False)
        print(f"  ✅ 更新 {n} 条 ({today})")
        # 统计各行业等级
        # 一级行业代码范围 CI005001.CI ~ CI005030.CI
        l1_codes = {f'CI005{i:03d}.CI' for i in range(1, 31)}
        l1 = sum(1 for r in records if r['ts_code'] in l1_codes)
        print(f"  📊 其中一级行业 {l1} 个")
    else:
        print(f"  ⚠️ 今日({today})无数据，可能非交易日")
    
    return records


def main():
    import argparse
    ap = argparse.ArgumentParser(description='中信行业指数日线拉取')
    ap.add_argument('--init', action='store_true', help='全量历史拉取')
    ap.add_argument('--today', action='store_true', help='今日增量')
    ap.add_argument('--all', action='store_true', help='全量+今日')
    args = ap.parse_args()
    
    pro = setup_tushare()
    
    if args.init or args.all:
        init_history(pro)
    
    if args.today or args.all or not (args.init or args.today):
        today_update(pro)
    
    # 显示统计
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT ts_code) as sectors, COUNT(*) as `rows` FROM sector_index_daily")
    r = cur.fetchone()
    cur.close(); conn.close()
    print(f"\n📊 当前 sector_index_daily 表: {r['sectors']} 个行业, {r['rows']} 条K线")


if __name__ == '__main__':
    main()
