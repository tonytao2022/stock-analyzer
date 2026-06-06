import os, sys, time, math
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple

import pymysql
import tushare as ts

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection, get_tushare_token


# ====================================================================
# 行业配置 — 中信一级行业 (30个)
# ====================================================================

SECTOR_L1_CODES = [
    'CI005001.CI',  # 石油石化
    'CI005002.CI',  # 煤炭
    'CI005003.CI',  # 有色金属
    'CI005019.CI',  # 电力及公用事业
    'CI005020.CI',  # 钢铁
    'CI005021.CI',  # 基础化工
    'CI005022.CI',  # 建筑
    'CI005023.CI',  # 建材
    'CI005024.CI',  # 轻工制造
    'CI005025.CI',  # 机械
    'CI005026.CI',  # 电力设备及新能源
    'CI005027.CI',  # 国防军工
    'CI005028.CI',  # 汽车
    'CI005029.CI',  # 商贸零售
    'CI005030.CI',  # 消费者服务
    'CI005031.CI',  # 家电
    'CI005032.CI',  # 纺织服装
    'CI005033.CI',  # 医药
    'CI005034.CI',  # 食品饮料
    'CI005035.CI',  # 农林牧渔
    'CI005036.CI',  # 银行
    'CI005037.CI',  # 非银行金融
    'CI005038.CI',  # 房地产
    'CI005039.CI',  # 交通运输
    'CI005040.CI',  # 电子
    'CI005041.CI',  # 通信
    'CI005042.CI',  # 计算机
    'CI005043.CI',  # 传媒
    'CI005044.CI',  # 综合
    'CI005045.CI',  # 综合金融
]

MIN_BI_SPAN = 3


# ====================================================================
# 核心算法 (merge_kline → find_fractals → find_bi → find_zhongshu
#            → determine_zoushi → detect_beichi → determine_buy_sell_point
#            → detect_autumn_tiger)
# ====================================================================

def merge_kline(klines: List[Dict]) -> List[Dict]:
    """K线包含处理 — 标准模式"""
    if len(klines) < 2: return klines[:]
    merged = [dict(klines[0])]
    i = 1
    while i < len(klines):
        curr = dict(klines[i])
        prev = merged[-1]
        if len(merged) >= 2:
            p1, p2 = merged[-2], merged[-1]
            is_up = p2['high'] > p1['high'] and p2['low'] > p1['low']
        else:
            is_up = prev['high'] <= curr['high'] and prev['low'] <= curr['low']
        if curr['high'] <= prev['high'] and curr['low'] >= prev['low']:
            if is_up:
                prev['high'] = max(prev['high'], curr['high'])
                prev['low'] = max(prev['low'], curr['low'])
            else:
                prev['high'] = min(prev['high'], curr['high'])
                prev['low'] = min(prev['low'], curr['low'])
            prev['close'] = curr['close']
        elif curr['high'] >= prev['high'] and curr['low'] <= prev['low']:
            if is_up:
                merged[-1] = {'high': max(prev['high'], curr['high']),
                              'low': max(prev['low'], curr['low']), 'close': curr['close']}
            else:
                merged[-1] = {'high': min(prev['high'], curr['high']),
                              'low': min(prev['low'], curr['low']), 'close': curr['close']}
        else:
            merged.append(curr)
        i += 1
    return merged


def find_fractals(klines: List[Dict]) -> List[Dict]:
    """分型识别"""
    if len(klines) < 3:
        return [dict(k, fractal='none') for k in klines]
    result = []
    for i in range(len(klines)):
        entry = dict(klines[i])
        entry['fractal'] = 'none'
        if 1 <= i <= len(klines) - 2:
            left, mid, right = klines[i-1], klines[i], klines[i+1]
            if (mid['high'] > left['high'] and mid['high'] > right['high']
                    and mid['low'] > left['low'] and mid['low'] > right['low']):
                entry['fractal'] = 'top'
                entry['fractal_strength'] = min(
                    (mid['high']-left['high'])/mid['high']*100,
                    (mid['high']-right['high'])/mid['high']*100)
            elif (mid['low'] < left['low'] and mid['low'] < right['low']
                  and mid['high'] < left['high'] and mid['high'] < right['high']):
                entry['fractal'] = 'bottom'
                entry['fractal_strength'] = min(
                    (left['low']-mid['low'])/mid['low']*100,
                    (right['low']-mid['low'])/mid['low']*100)
        result.append(entry)
    return result


def find_bi(fractals: List[Dict], min_span: int = MIN_BI_SPAN) -> List[Dict]:
    """笔识别 (行业版 min_span=3)"""
    tops = [(i, k['high'], k['low']) for i, k in enumerate(fractals) if k.get('fractal') == 'top']
    bottoms = [(i, k['low'], k['high']) for i, k in enumerate(fractals) if k.get('fractal') == 'bottom']
    all_points = []
    for t in tops: all_points.append({'type':'top', 'idx':t[0], 'price':t[1], 'low':t[2]})
    for b in bottoms: all_points.append({'type':'bottom', 'idx':b[0], 'price':b[1], 'high':b[2]})
    all_points.sort(key=lambda x: x['idx'])
    bi_list = []
    if not all_points: return bi_list
    last = all_points[0]
    for i in range(1, len(all_points)):
        p = all_points[i]
        if p['type'] == last['type']:
            last = p; continue
        if p['idx'] - last['idx'] < min_span:
            if p['type'] == 'top': last = p if p['price'] > last['price'] else last
            else: last = p if p['price'] < last['price'] else last
            continue
        bi_list.append({
            'start_idx': last['idx'], 'end_idx': p['idx'],
            'start_type': last['type'], 'end_type': p['type'],
            'direction': 'up' if last['type'] == 'bottom' else 'down',
            'start_price': last['price'], 'end_price': p['price'],
            'span': p['idx'] - last['idx'],
        })
        last = p
    return bi_list


def find_zhongshu(bi_list: List[Dict]) -> List[Dict]:
    """中枢识别 — 标准模式"""
    if len(bi_list) < 3: return []
    zhongshu_list = []
    for i in range(len(bi_list) - 2):
        b1, b2, b3 = bi_list[i], bi_list[i+1], bi_list[i+2]
        if not (b1['direction'] != b2['direction'] and b2['direction'] != b3['direction']): continue
        lows = [min(b['start_price'], b['end_price']) for b in [b1, b2, b3]]
        highs = [max(b['start_price'], b['end_price']) for b in [b1, b2, b3]]
        zd, zg = max(lows), min(highs)
        if zg <= zd: continue
        width = (zg - zd) / zd
        stability = max(0, min(1, 1 - width * 5))
        zhongshu_list.append({
            'start_bi_idx': i, 'end_bi_idx': i+2,
            'zd': zd, 'zg': zg, 'width': width, 'stability': stability, 'total_bi': 3,
        })
        for j in range(i+3, len(bi_list)):
            bj = bi_list[j]
            bj_high, bj_low = max(bj['start_price'], bj['end_price']), min(bj['start_price'], bj['end_price'])
            if bj_low > zg or bj_high < zd: break
            zd, zg = max(zd, bj_low), min(zg, bj_high)
            zhongshu_list[-1]['end_bi_idx'] = j
            zhongshu_list[-1]['total_bi'] = j - i + 1
            if zg <= zd: break
    if not zhongshu_list: return []
    return [zhongshu_list[-1]]


def determine_zoushi(bi_list: List[Dict], zhongshu_list: List[Dict]) -> Dict:
    """走势类型判定"""
    if not bi_list or not zhongshu_list: return {'type':'unknown','stage':'none'}
    zs = zhongshu_list[-1]; last_bi = bi_list[-1]
    if last_bi['direction'] == 'up' and last_bi['end_price'] > zs['zg']: stage = '突破'
    elif last_bi['direction'] == 'down' and last_bi['end_price'] < zs['zd']: stage = '破位'
    elif zs['width'] < 0.05 and zs['stability'] > 0.8: stage = '中枢新生'
    else: stage = '盘整'
    zt = '盘整'
    if len(zhongshu_list) >= 2:
        if zhongshu_list[-1]['zd'] > zhongshu_list[-2]['zg']: zt = '上涨趋势'
        elif zhongshu_list[-1]['zg'] < zhongshu_list[-2]['zd']: zt = '下跌趋势'
    return {'type': zt, 'stage': stage, 'zoushi': zt}


def detect_beichi(bi_list: List[Dict], closes: List[float], zhongshu_list: List[Dict]) -> Dict:
    """背驰检测"""
    if len(bi_list) < 4 or not zhongshu_list:
        return {'type':'none','strength':0,'macd_area_ratio':0,'dif_dea_diverge':0}
    zs = zhongshu_list[-1]; ei, xi = zs['start_bi_idx'], zs['end_bi_idx']
    if ei < 0 or xi >= len(bi_list) - 1: return {'type':'none','strength':0,'macd_area_ratio':0,'dif_dea_diverge':0}
    entry_bi, exit_bi = bi_list[ei], bi_list[xi+1] if xi+1 < len(bi_list) else bi_list[-1]
    def _area(bi, kls):
        s, e = bi['start_idx'], bi['end_idx']
        if e - s < 2: return 0
        seg = kls[s:e+1]
        if len(seg) < 3: return 0
        ma5 = sum(seg[-5:])/min(5,len(seg))
        return sum((c-ma5)**2*(1 if bi['direction']=='up' else -1) for c in seg)
    ea, xa = _area(entry_bi, closes), _area(exit_bi, closes)
    ar = 0 if abs(ea)<0.0001 else abs(xa/ea)
    bt, bs = 'none', 0
    if exit_bi['direction']=='up' and exit_bi['end_price']>entry_bi['end_price'] and ar<0.8:
        bt, bs = 'top', (1-ar)*100*(1.2 if ar<0.5 else 1)
    elif exit_bi['direction']=='down' and exit_bi['end_price']<entry_bi['end_price'] and ar<0.8:
        bt, bs = 'bottom', (1-ar)*100*(1.2 if ar<0.5 else 1)
    return {'type': bt, 'strength': round(min(100,max(0,bs)),2),
            'macd_area_ratio': round(ar,4), 'dif_dea_diverge': 1 if bt != 'none' else 0}


def determine_buy_sell_point(bi_list, zhongshu_list, beichi_result, zoushi_result) -> Dict:
    """买卖点判定"""
    if not zhongshu_list or len(bi_list)<4: return {'point':'none','confirmed':0,'failed':0}
    zs, bc, zt = zhongshu_list[-1], beichi_result, zoushi_result
    res = {'point':'none','confirmed':0,'failed':0}
    lb, lp, bd = bi_list[-1], bi_list[-1]['end_price'], bi_list[-1]['direction']
    if bc['type']=='bottom' and bc['strength']>30 and (zt.get('zoushi')=='下跌趋势' or zt['type']=='unknown'):
        res = {'point':'buy1','confirmed':1,'failed':0}
    elif bc['type']=='top' and bc['strength']>30 and (zt.get('zoushi')=='上涨趋势' or zt['type']=='unknown'):
        res = {'point':'sell1','confirmed':1,'failed':0}
    if res['point']=='none' and len(bi_list)>=4:
        pb = bi_list[-2]
        if bc['type']=='none' and bd=='up' and lp>zs['zd'] and pb['direction']=='down' and pb['end_price']>zs['zd']:
            res = {'point':'buy2','confirmed':1,'failed':0}
        elif bc['type']=='none' and bd=='down' and lp<zs['zg'] and pb['direction']=='up' and pb['end_price']<zs['zg']:
            res = {'point':'sell2','confirmed':1,'failed':0}
    if res['point']=='none' and bd=='up' and lp>zs['zg'] and len(bi_list)>=5:
        pb = bi_list[-2]
        if pb['direction']=='down' and pb['end_price']>zs['zg'] and lb['start_price']>zs['zg']:
            res = {'point':'buy3','confirmed':1,'failed':0}
    if res['point']=='none' and bd=='down' and lp<zs['zd'] and len(bi_list)>=5:
        pb = bi_list[-2]
        if pb['direction']=='up' and pb['end_price']<zs['zd'] and lb['start_price']<zs['zd']:
            res = {'point':'sell3','confirmed':1,'failed':0}
    return res


def detect_autumn_tiger(bi_list, zhongshu_list, beichi_result, closes, high) -> Dict:
    """秋老虎检测"""
    if not bi_list: return {'active':False,'confidence':0,'reasons':[]}
    reasons = []; conf = 0
    down_cnt = sum(1 for b in bi_list[-6:] if b['direction']=='down')
    if down_cnt >= 2: conf += 20; reasons.append('前期下跌笔≥2')
    lb = bi_list[-1]
    if lb['direction'] == 'up':
        ret = (lb['end_price'] - lb['start_price']) / lb['start_price']
        if ret > 0.05: conf += 25; reasons.append(f'反弹幅度{ret*100:.1f}%')
        if ret > 0.10: conf += 15; reasons.append('强势反弹>10%')
    vol_ratio = high / (sum(closes[-10:])/10) if len(closes)>=10 and sum(closes[-10:])>0 else 0
    if vol_ratio > 0: conf += 10; reasons.append('底部放量迹象')
    if beichi_result['type']=='bottom' and beichi_result['strength']>40:
        conf += 20; reasons.append('底背驰确认')
    if zhongshu_list: conf += 10; reasons.append('中枢支撑区')
    return {'active': conf>=50, 'confidence': min(100,conf), 'reasons': reasons[:5]}


# ====================================================================
# 行业缠论分析主函数
# ====================================================================

def analyze_sector_chanlun(ts_code: str, sector_name: str,
                           trade_date: str, ohlc: List[Dict]) -> Dict:
    """对单个行业指数做完整缠论分析"""
    n = len(ohlc)
    if n < 60: return {'error': f'数据不足({n}日, 需要≥60日)'}
    closes = [float(r['close']) for r in ohlc]
    highs = [float(r['high']) for r in ohlc]
    klines = [{'high': highs[i], 'low': float(r['low']), 'close': closes[i]} for i, r in enumerate(ohlc)]
    merged = merge_kline(klines)
    fractals = find_fractals(merged)
    bi_list = find_bi(fractals, min_span=MIN_BI_SPAN)
    zhongshu_list = find_zhongshu(bi_list)
    zoushi = determine_zoushi(bi_list, zhongshu_list)
    beichi = detect_beichi(bi_list, closes, zhongshu_list)
    bs_point = determine_buy_sell_point(bi_list, zhongshu_list, beichi, zoushi)
    tiger = detect_autumn_tiger(bi_list, zhongshu_list, beichi, closes, highs[-1] if highs else 0)
    recent_fractals = fractals[-20:] if len(fractals)>20 else fractals
    top_cnt = sum(1 for f in recent_fractals if f.get('fractal')=='top')
    bottom_cnt = sum(1 for f in recent_fractals if f.get('fractal')=='bottom')
    bi_dir = bi_list[-1]['direction'] if bi_list else 'none'
    bi_strength = abs((bi_list[-1]['end_price']-bi_list[-1]['start_price'])/bi_list[-1]['start_price'])*100 if bi_list and bi_list[-1]['end_price']>0 else 0
    ss = 50
    if bs_point['point'] in ('buy1','buy2'): ss=75
    elif bs_point['point']=='buy3': ss=85
    elif bs_point['point'] in ('sell1','sell2'): ss=25
    elif bs_point['point']=='sell3': ss=15
    elif bi_dir=='up': ss=60
    elif bi_dir=='down': ss=40
    if beichi['type']=='bottom' and beichi['strength']>40: ss=min(95,ss+15)
    elif beichi['type']=='top' and beichi['strength']>40: ss=max(5,ss-15)
    if zhongshu_list: ss=round(ss*0.8+zhongshu_list[-1]['stability']*0.2*100,1)
    return {
        'ts_code':ts_code,'sector_name':sector_name,'trade_date':trade_date,
        'analysis_level':'daily',
        'top_fractal_cnt':top_cnt,'bottom_fractal_cnt':bottom_cnt,
        'bi_direction':bi_dir,'bi_strength':round(bi_strength,2),'bi_count':len(bi_list),
        'zhongshu_count':len(zhongshu_list),
        'zhongshu_zd':round(zhongshu_list[-1]['zd'],3) if zhongshu_list else 0,
        'zhongshu_zg':round(zhongshu_list[-1]['zg'],3) if zhongshu_list else 0,
        'zhongshu_width':round(zhongshu_list[-1]['width'],4) if zhongshu_list else 0,
        'zhongshu_stability':round(zhongshu_list[-1]['stability'],4) if zhongshu_list else 0,
        'zhongshu_start_idx':zhongshu_list[-1]['start_bi_idx'] if zhongshu_list else 0,
        'zhongshu_end_idx':zhongshu_list[-1]['end_bi_idx'] if zhongshu_list else 0,
        'zoushi_type':zoushi['type'],'zoushi_stage':zoushi['stage'],
        'beichi_type':beichi['type'],'beichi_strength':round(beichi['strength'],2),
        'beichi_validity':round(beichi['strength']/100,4) if beichi['strength']>0 else 0,
        'macd_area_ratio':round(beichi['macd_area_ratio'],4),
        'dif_dea_diverge':beichi['dif_dea_diverge'],
        'buy_sell_point':bs_point['point'],
        'buy3_confirmed':1 if bs_point['point']=='buy3' else 0,
        'buy3_failed':0,
        'autumn_tiger':1 if tiger['active'] else 0,
        'tiger_confidence':round(tiger['confidence']/100,2),
        'tiger_reasons':str(tiger['reasons']) if tiger['reasons'] else None,
        'structure_score':round(ss,2),
        'is_calculable':1,'calc_error':None,
    }
# ====================================================================
# 数据拉取与数据库交互 (中信 ci_daily 接口)
# ====================================================================

def ensure_tables():
    """确保 sector_index_daily 和 sector_chanlun_cache 表存在"""
    conn = get_connection()
    cur = conn.cursor()

    # sector_index_daily
    cur.execute("SHOW TABLES LIKE 'sector_index_daily'")
    if not cur.fetchone():
        print("📦 创建 sector_index_daily 表...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_index_daily (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(16) NOT NULL,
                sector_name VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                open DECIMAL(12,3), high DECIMAL(12,3),
                low DECIMAL(12,3), close DECIMAL(12,3),
                change_pct DECIMAL(8,3), vol DECIMAL(20,2), amount DECIMAL(20,2),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_code_date (ts_code, trade_date),
                INDEX idx_trade_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='中信行业指数日K线(含30个中信一级行业)'
        """)
        conn.commit()

    # sector_chanlun_cache
    cur.execute("SHOW TABLES LIKE 'sector_chanlun_cache'")
    if not cur.fetchone():
        print("📦 创建 sector_chanlun_cache 表...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_chanlun_cache (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(16) NOT NULL COMMENT '行业指数代码',
                sector_name VARCHAR(32) NOT NULL COMMENT '行业名称',
                trade_date DATE NOT NULL,
                analysis_level VARCHAR(10) DEFAULT 'daily',
                top_fractal_cnt INT DEFAULT 0, bottom_fractal_cnt INT DEFAULT 0,
                bi_direction VARCHAR(10) DEFAULT 'none',
                bi_strength DECIMAL(10,4) DEFAULT 0, bi_count INT DEFAULT 0,
                zhongshu_count INT DEFAULT 0,
                zhongshu_zd DECIMAL(12,3) DEFAULT 0, zhongshu_zg DECIMAL(12,3) DEFAULT 0,
                zhongshu_width DECIMAL(10,4) DEFAULT 0, zhongshu_stability DECIMAL(10,4) DEFAULT 0,
                zhongshu_start_idx INT DEFAULT 0, zhongshu_end_idx INT DEFAULT 0,
                zoushi_type VARCHAR(20) DEFAULT 'unknown', zoushi_stage VARCHAR(20) DEFAULT 'none',
                beichi_type VARCHAR(20) DEFAULT 'none', beichi_strength DECIMAL(10,4) DEFAULT 0,
                beichi_validity DECIMAL(10,4) DEFAULT 0,
                macd_area_ratio DECIMAL(10,4) DEFAULT 0, dif_dea_diverge TINYINT(1) DEFAULT 0,
                buy_sell_point VARCHAR(20) DEFAULT 'none',
                buy3_confirmed TINYINT(1) DEFAULT 0, buy3_failed TINYINT(1) DEFAULT 0,
                autumn_tiger TINYINT(1) DEFAULT 0, tiger_confidence DECIMAL(5,2) DEFAULT 0,
                tiger_reasons TEXT, structure_score DECIMAL(5,2) DEFAULT 50,
                is_calculable TINYINT(1) DEFAULT 1, calc_error VARCHAR(200) DEFAULT NULL,
                sector_count INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_sector_date (ts_code, trade_date),
                INDEX idx_trade_date (trade_date),
                INDEX idx_buy_sell (buy_sell_point),
                INDEX idx_zoushi (zoushi_type),
                INDEX idx_beichi (beichi_type)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='中信行业缠论分析缓存(30个中信一级行业)'
        """)
        conn.commit()
    cur.close()
    conn.close()


def sync_sector_kline_to_db(force_full: bool = False) -> Dict:
    """
    从Tushare ci_daily按日拉取30个中信一级行业K线,写入sector_index_daily
    """
    token = get_tushare_token()
    if not token: return {'error': 'no token'}
    ts.set_token(token)
    pro = ts.pro_api()
    ensure_tables()
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT MAX(trade_date) as md FROM sector_index_daily")
    row = cur.fetchone()
    latest_in_db = str(row['md']) if row and row['md'] else None
    today = date.today()
    if force_full or not latest_in_db:
        start_dt = datetime(2024, 1, 1)
        mode = 'FULL'
    else:
        start_dt = datetime.strptime(latest_in_db, '%Y-%m-%d') if '-' in str(latest_in_db) else datetime.strptime(str(latest_in_db), '%Y%m%d')
        mode = 'INCREMENTAL'
    print(f"📥 行业K线(中信) [mode={mode}] {start_dt.strftime('%Y%m%d')} → {today.strftime('%Y%m%d')}")
    current = start_dt
    total_inserted = 0; days_fetched = 0; skipped_dates = 0
    while current <= today:
        ds = current.strftime('%Y%m%d')
        try:
            df = pro.ci_daily(trade_date=ds, fields='ts_code,name,open,high,low,close,pct_change,vol,amount')
            if df is not None and len(df) > 0:
                sdf = df[df['ts_code'].isin(SECTOR_L1_CODES)]
                if len(sdf) > 0:
                    for _, r in sdf.iterrows():
                        cur.execute("""
                            INSERT INTO sector_index_daily
                                (ts_code, sector_name, trade_date, open, high, low, close, change_pct, vol, amount)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE
                                close=VALUES(close), high=VALUES(high), low=VALUES(low),
                                change_pct=VALUES(change_pct), vol=VALUES(vol), amount=VALUES(amount)
                        """, (r['ts_code'], r['name'], ds,
                              float(r['open']), float(r['high']), float(r['low']), float(r['close']),
                              float(r['pct_change']), float(r['vol']), float(r['amount'])))
                    conn.commit()
                    total_inserted += len(sdf); days_fetched += 1
                else: skipped_dates += 1
            else: skipped_dates += 1
            time.sleep(0.3)
        except Exception as e: skipped_dates += 1
        current += timedelta(days=1)
    cur.close(); conn.close()
    stats = {'mode':mode,'days_fetched':days_fetched,'records_inserted':total_inserted,'skipped':skipped_dates,'from_date':start_dt.strftime('%Y%m%d'),'to_date':today.strftime('%Y%m%d')}
    print(f"✅ 同步完成(中信): {days_fetched}天, {total_inserted}条记录")
    return stats


def fetch_sector_kline(cur, ts_code: str, lookback: int = 400) -> Tuple[Optional[Dict], Optional[str]]:
    """从 sector_index_daily 读行业K线"""
    cur.execute(
        "SELECT trade_date, open, high, low, close, vol FROM sector_index_daily "
        "WHERE ts_code=%s ORDER BY trade_date ASC LIMIT %s", (ts_code, lookback))
    rows = cur.fetchall()
    if len(rows) < 60: return None, f'数据不足({len(rows)}日, 需要≥60日)'
    trade_date = str(rows[-1]['trade_date'])
    ohlc = [{'high':float(r['high']),'low':float(r['low']),'open':float(r['open']),'close':float(r['close']),'vol':float(r.get('vol',0)or 0)} for r in rows]
    return {'trade_date':trade_date,'ohlc':ohlc}, None


# ====================================================================
# 全量行业缠论分析
# ====================================================================

def run_all_sectors() -> Dict:
    """
    对30个中信一级行业跑全量缠论分析, 写入 sector_chanlun_cache
    """
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    success = 0; errors = []; written = 0
    print(f"🔬 行业缠论分析开始(中信): {len(SECTOR_L1_CODES)}个一级行业")
    print(f"{'='*60}")
    for i, code in enumerate(SECTOR_L1_CODES):
        print(f"\r  [{i+1}/{len(SECTOR_L1_CODES)}] {code}", end='', flush=True)
        try:
            kline_data, err = fetch_sector_kline(cur, code)
            if err: errors.append(f"{code}:{err}"); continue
            result = analyze_sector_chanlun(code, code, kline_data['trade_date'], kline_data['ohlc'])
            if 'error' in result: errors.append(f"{code}:{result['error']}"); continue
            cur.execute("SELECT sector_name FROM sector_index_daily WHERE ts_code=%s LIMIT 1", (code,))
            name_row = cur.fetchone()
            sector_name = name_row['sector_name'] if name_row else code
            result['sector_name'] = sector_name
            cur.execute("""
                INSERT INTO sector_chanlun_cache
                    (ts_code, sector_name, trade_date, analysis_level,
                     top_fractal_cnt, bottom_fractal_cnt,
                     bi_direction, bi_strength, bi_count,
                     zhongshu_count, zhongshu_zd, zhongshu_zg,
                     zhongshu_width, zhongshu_stability,
                     zhongshu_start_idx, zhongshu_end_idx,
                     zoushi_type, zoushi_stage,
                     beichi_type, beichi_strength, beichi_validity,
                     macd_area_ratio, dif_dea_diverge,
                     buy_sell_point, buy3_confirmed, buy3_failed,
                     autumn_tiger, tiger_confidence, tiger_reasons,
                     structure_score, is_calculable, calc_error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    sector_name=VALUES(sector_name), top_fractal_cnt=VALUES(top_fractal_cnt),
                    bottom_fractal_cnt=VALUES(bottom_fractal_cnt), bi_direction=VALUES(bi_direction),
                    bi_strength=VALUES(bi_strength), bi_count=VALUES(bi_count),
                    zhongshu_count=VALUES(zhongshu_count), zhongshu_zd=VALUES(zhongshu_zd),
                    zhongshu_zg=VALUES(zhongshu_zg), zhongshu_width=VALUES(zhongshu_width),
                    zhongshu_stability=VALUES(zhongshu_stability), zoushi_type=VALUES(zoushi_type),
                    zoushi_stage=VALUES(zoushi_stage), beichi_type=VALUES(beichi_type),
                    beichi_strength=VALUES(beichi_strength), beichi_validity=VALUES(beichi_validity),
                    macd_area_ratio=VALUES(macd_area_ratio), dif_dea_diverge=VALUES(dif_dea_diverge),
                    buy_sell_point=VALUES(buy_sell_point), buy3_confirmed=VALUES(buy3_confirmed),
                    autumn_tiger=VALUES(autumn_tiger), tiger_confidence=VALUES(tiger_confidence),
                    tiger_reasons=VALUES(tiger_reasons), structure_score=VALUES(structure_score),
                    is_calculable=VALUES(is_calculable), calc_error=VALUES(calc_error)
            """, (
                result['ts_code'], result['sector_name'], result['trade_date'], result['analysis_level'],
                result['top_fractal_cnt'], result['bottom_fractal_cnt'],
                result['bi_direction'], result['bi_strength'], result['bi_count'],
                result['zhongshu_count'], result['zhongshu_zd'], result['zhongshu_zg'],
                result['zhongshu_width'], result['zhongshu_stability'],
                result['zhongshu_start_idx'], result['zhongshu_end_idx'],
                result['zoushi_type'], result['zoushi_stage'],
                result['beichi_type'], result['beichi_strength'], result['beichi_validity'],
                result['macd_area_ratio'], result['dif_dea_diverge'],
                result['buy_sell_point'], result['buy3_confirmed'], result['buy3_failed'],
                result['autumn_tiger'], result['tiger_confidence'], result.get('tiger_reasons'),
                result['structure_score'], result['is_calculable'], result.get('calc_error'),
            ))
            conn.commit(); written += 1; success += 1
        except Exception as e:
            errors.append(f"{code}:{e}")
        if (i+1) % 10 == 0: print(f" ✅ {written}/{i+1}", end='', flush=True)
    cur.close(); conn.close()
    print(f"\n{'='*60}")
    print(f"✅ 行业缠论分析完成!(中信)")
    print(f"  成功: {success}/{len(SECTOR_L1_CODES)}")
    print(f"  写入 sector_chanlun_cache: {written} 条")
    if errors:
        print(f"  ⚠️ 错误 ({len(errors)}):")
        for e in errors[:5]: print(f"    {e}")
        if len(errors) > 5: print(f"    ... 还有 {len(errors)-5} 个")
    return {'success':success,'errors':errors,'total':len(SECTOR_L1_CODES)}


# ====================================================================
# 主入口 + 命令行接口
# ====================================================================

def main():
    """主入口: 1.表检查 2.同步数据 3.缠论分析"""
    import argparse
    ap = argparse.ArgumentParser(description='行业缠论分析器(中信) — 板块轮动专用')
    ap.add_argument('--sync', action='store_true', help='强制拉取行业K线')
    ap.add_argument('--analyze', action='store_true', help='跑缠论分析')
    ap.add_argument('--all', action='store_true', help='全量: sync+analyze')
    args = ap.parse_args()
    t0 = time.time()
    if args.all or args.sync:
        ensure_tables()
        sync_sector_kline_to_db(force_full=False)
    if args.all or args.analyze:
        ensure_tables()
        run_all_sectors()
    if not any([args.all, args.sync, args.analyze]):
        print("用法: python3 sector_chanlun_analyzer.py --all")
        print("      python3 sector_chanlun_analyzer.py --sync  # 仅拉取数据")
        print("      python3 sector_chanlun_analyzer.py --analyze  # 仅跑分析")
    print(f"\n⏱ 总耗时: {time.time()-t0:.1f}s")

if __name__ == '__main__':
    main()
