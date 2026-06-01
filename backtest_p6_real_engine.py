#!/usr/bin/env python3
"""
P6真实引擎 全量回测 v3.0
=========================
用线上真实p6_dual_track_engine评分 + 分季自适应阈值
195只监控池 × 2023-01 ~ 2026-05
"""

import sys, os, time, json
from datetime import date, datetime, timedelta
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')
os.environ['STOCK_USER'] = os.environ.get('STOCK_USER', 'tony')

from db_config import get_connection

# 真实引擎
from season_engine import SeasonEngine as RealSeasonEngine
from p6_dual_track_engine import score_stock as real_score_stock
from p6_dual_track_engine import MarketContext as RealMC

START_DATE = date(2023, 1, 1)

# MAY分季自适应阈值
THRESHOLDS = {
    'summer':       {'buy':45, 'stop':-0.10, 'hold':30},
    'spring':       {'buy':42, 'stop':-0.10, 'hold':25},
    'chaos_spring': {'buy':42, 'stop':-0.10, 'hold':20},
    'chaos':        {'buy':38, 'stop':-0.12, 'hold':20},
    'chaos_autumn': {'buy':35, 'stop':-0.12, 'hold':15},
    'autumn':       {'buy':30, 'stop':-0.12, 'hold':15},
    'winter':       {'buy':999, 'stop':-0.10, 'hold':0},
    'panic':        {'buy':999, 'stop':-0.10, 'hold':0},
    'recovery':     {'buy':38, 'stop':-0.10, 'hold':20},
}

# 对比基准：统一阈值45
F45 = {k:{'buy':45,'stop':-0.10,'hold':20} for k in THRESHOLDS}
F45.update({'winter':{'buy':999},'panic':{'buy':999}})

def get_price(code, tgt, cache):
    """从预加载K线缓存取收盘价"""
    for r in cache.get(code, []):
        if str(r['trade_date']) == str(tgt):
            return float(r['close'])
    return None

def run(cfg_dict, label, kline, codes, season_map):
    """用真实引擎跑回测"""
    dates = sorted(season_map.keys())
    trades = []
    positions = {}
    tot = len(dates)
    
    # 缓存MarketContext（季节判定结果）
    ctx_cache = {}
    
    for i, today in enumerate(dates):
        if i % 100 == 0:
            print(f"  {label}: {i}/{tot}", end='\r')
        
        sea = season_map.get(today, 'chaos')
        c = cfg_dict[sea]
        
        if c['buy'] >= 999:
            positions.clear()
            continue
        
        # 获取今日MarketContext
        if sea not in ctx_cache:
            try:
                se = RealSeasonEngine()
                ctx_cache[sea] = RealMC(se.judge_market_season())
            except:
                pass
        
        # 检查持仓（止损/到期）
        to_close = []
        for code, pos in list(positions.items()):
            price = get_price(code, today, kline)
            if price is None:
                continue
            ret = (price - pos['bp']) / pos['bp']
            if price > pos.get('peak', pos['bp']):
                pos['peak'] = price
            dd = (today - pos['bd']).days
            if ret <= c['stop'] or (price - pos['peak']) / pos['peak'] <= -0.15:
                to_close.append((code, ret))
            elif dd >= c['hold']:
                to_close.append((code, ret))
        
        for code, ret in to_close:
            p = positions.pop(code)
            trades.append({
                'c': code,
                'bd': str(p['bd']), 'sd': str(today),
                'hd': (today - p['bd']).days, 'r': ret,
                's': p['sc'], 'sea': p['sea']
            })
        
        # 新买入（使用真实引擎评分）
        for code in codes:
            if code in positions or len(positions) >= 10:
                continue
            price = get_price(code, today, kline)
            if price is None:
                continue
            
            try:
                # === 使用真实p6_dual_track_engine评分 ===
                # 模拟当日的MarketContext
                se = RealSeasonEngine()
                ctx = RealMC(se.judge_market_season())
                result = real_score_stock(code, ctx)
                raw_score = result.get('score', 0)
                if raw_score < c['buy']:
                    continue
            except Exception:
                continue
            
            positions[code] = {
                'bd': today, 'bp': price, 'peak': price,
                'sc': raw_score, 'sea': sea
            }
    
    # 平仓
    last = dates[-1]
    for code, p in positions.items():
        price = get_price(code, last, kline) or p['bp']
        ret = (price - p['bp']) / p['bp']
        trades.append({
            'c': code, 'bd': str(p['bd']), 'sd': str(last),
            'hd': (last - p['bd']).days, 'r': ret,
            's': p['sc'], 'sea': p['sea']
        })
    
    return trades


def calc_stats(ts):
    if not ts:
        return {'n': 0}
    w = [t for t in ts if t['r'] > 0]
    l = [t for t in ts if t['r'] <= 0]
    tg = sum(t['r'] for t in w) or 0.001
    tl = abs(sum(t['r'] for t in l)) or 1
    
    seas = {}
    for s, lb in [('summer','夏'),('spring','春'),('chaos_spring','弱春'),
                  ('chaos','混沌'),('chaos_autumn','弱秋'),('autumn','秋'),('winter','冬')]:
        g = [t for t in ts if t.get('sea') == s]
        if g:
            wg = [t for t in g if t['r'] > 0]
            seas[lb] = {
                'n': len(g),
                'wr': round(len(wg) / len(g) * 100, 1),
                'ar': round(sum(t['r'] for t in g) / len(g) * 100, 2)
            }
    
    holds = {}
    for d in [10, 20, 30, 60]:
        g = [t for t in ts if t['hd'] <= d]
        if g:
            wg = [t for t in g if t['r'] > 0]
            holds[f'≤{d}d'] = {
                'n': len(g),
                'wr': round(len(wg) / len(g) * 100, 1),
                'ar': round(sum(t['r'] for t in g) / len(g) * 100, 2)
            }
    
    return {
        'n': len(ts),
        'wr': round(len(w) / len(ts) * 100, 1),
        'ar': round(sum(t['r'] for t in ts) / len(ts) * 100, 2),
        'aw': round(sum(t['r'] for t in w) / len(w) * 100, 2) if w else 0,
        'al': round(sum(t['r'] for t in l) / len(l) * 100, 2) if l else 0,
        'pf': round(tg / tl, 2),
        'seas': seas,
        'holds': holds,
    }


def print_report(stats, label, elapsed):
    print(f"\n📊 {label} ({elapsed:.0f}s)")
    print(f"总交易: {stats['n']}笔 | 胜率: {stats['wr']}% | "
          f"均收益: {stats['ar']:+.2f}% | 盈亏比: {stats['pf']} | "
          f"均盈: {stats['aw']:+.2f}% | 均亏: {stats['al']:+.2f}% | "
          f"年化: {stats['ar']/3.3:+.2f}%")
    print("季节: ", end='')
    for lb in ['夏','春','弱春','混沌','弱秋','秋']:
        if lb in stats['seas']:
            v = stats['seas'][lb]
            print(f"{lb}:{v['n']}笔{v['wr']}%{v['ar']:+.2f}% ", end='')
    print()
    print("持有: ", end='')
    for k, v in sorted(stats['holds'].items()):
        print(f"{k}:{v['wr']}%{v['ar']:+.2f}% ", end='')
    print()


# ==================== 主入口 ====================
if __name__ == '__main__':
    print("=" * 60)
    print("🚀 P6真实引擎 全量回测")
    print("   引擎: p6_dual_track_engine.score_stock (线上真实)")
    print("   回测区间: 2023-01 ~ 2026-05")
    print("   两种策略: MAY分季自适应 vs 统一阈值45")
    print("=" * 60)
    
    # 加载数据
    print("\n📦 加载数据...")
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    season_map = {}
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str):
            td = date.fromisoformat(td)
        if td >= START_DATE:
            season_map[td] = r['season']
    
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    
    # 加载K线
    kline = {}
    for code in codes:
        cur.execute(
            "SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",
            (code,))
        rows = cur.fetchall()
        if len(rows) >= 120:
            kline[code] = rows
    cur.close()
    conn.close()
    
    print(f"   季节: {len(season_map)}天 | 监控池: {len(codes)}只 | K线: {len(kline)}只")
    
    # 跑回测
    results = {}
    for lbl, cfg in [('MAY分季自适应', THRESHOLDS), ('统一阈值45', F45)]:
        print(f"\n{'=' * 50}")
        print(f"🏃 {lbl}")
        t0 = time.time()
        trades = run(cfg, lbl, kline, codes, season_map)
        elapsed = time.time() - t0
        st = calc_stats(trades)
        print_report(st, lbl, elapsed)
        results[lbl] = st
    
    # 对比
    print(f"\n{'=' * 50}")
    print("⚖️ 真实引擎对比")
    print(f"{'策略':<14} {'笔数':>6} {'胜率':>7} {'均收益':>9} {'盈亏比':>7} {'年化':>7}")
    print("-" * 50)
    for lbl in ['统一阈值45', 'MAY分季自适应']:
        s = results[lbl]
        print(f"{lbl:<14} {s['n']:>6} {s['wr']:>6}% {s['ar']:>+8.2f}% {s['pf']:>7.2f} {s['ar']/3.3:>+6.2f}%")
