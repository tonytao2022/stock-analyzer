#!/usr/bin/env python3
"""
P6真实引擎 · 多参数调优回测 v5.0
195只全量 × P6真实引擎评分 × 多方案对比
只做P6内部调参对比，不和V4比
"""
import sys, os, time, json
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')
os.environ['STOCK_USER'] = os.environ.get('STOCK_USER', 'tony')

from db_config import get_connection
from p6_dual_track_engine import score_stock, MarketContext
from season_engine import SeasonEngine

START = date(2023, 1, 1)

# ==================== 方案定义 ====================
# 每个方案: {name, buy_threshold, hold_days, stop_loss, 描述}
SCHEMES = [
    # 固定阈值组
    {"name":"固定45持20","buy":45,"hold":20,"stop":-0.10,"desc":"固定阈值45+持有20日"},
    {"name":"固定45持30","buy":45,"hold":30,"stop":-0.10,"desc":"固定阈值45+持有30日"},
    {"name":"固定45持60","buy":45,"hold":60,"stop":-0.10,"desc":"固定阈值45+持有60日"},
    {"name":"固定38持20","buy":38,"hold":20,"stop":-0.12,"desc":"固定阈值38+持有20日"},
    {"name":"固定38持30","buy":38,"hold":30,"stop":-0.12,"desc":"固定阈值38+持有30日"},
    {"name":"固定38持60","buy":38,"hold":60,"stop":-0.12,"desc":"固定阈值38+持有60日"},
    {"name":"固定30持15","buy":30,"hold":15,"stop":-0.12,"desc":"固定阈值30+持有15日(低门槛短持)"},
    {"name":"固定30持30","buy":30,"hold":30,"stop":-0.12,"desc":"固定阈值30+持有30日(低门槛长持)"},
    # 分季自适应组
    {"name":"MAY分季20","buy":-1,"hold":-1,"stop":-0.10,"desc":"分季自适应(夏45/秋30/混沌38)+持有按季","seasonal":True},
    {"name":"MAY分季30","buy":-2,"hold":-2,"stop":-0.10,"desc":"同上但统一持有30日","seasonal":True,"force_hold":30},
]

# 分季参数
SEASONAL_BUY = {'summer':45,'spring':42,'chaos_spring':42,'chaos':38,'chaos_autumn':35,'autumn':30,'winter':999,'panic':999,'recovery':38}
SEASONAL_HOLD = {'summer':30,'spring':25,'chaos_spring':20,'chaos':20,'chaos_autumn':15,'autumn':15,'winter':0,'panic':0,'recovery':20}

# ==================== 数据加载 ====================
print("加载数据...", flush=True)
conn = get_connection(); cur = conn.cursor()
cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
season_map = {}
for r in cur.fetchall():
    td = r['trade_date']
    if isinstance(td, str): td = date.fromisoformat(td)
    if td >= START: season_map[td] = r['season']

all_dates = sorted(season_map.keys())
sample_dates = [d for i, d in enumerate(all_dates) if i % 5 == 0]

cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
codes = [r['ts_code'] for r in cur.fetchall()]

kline = {}
for code in codes:
    cur.execute("SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC", (code,))
    rows = cur.fetchall()
    if len(rows) >= 120:
        kline[code] = rows
cur.close(); conn.close()

print(f"季节: {len(season_map)}天 采样: {len(sample_dates)}个 监控池: {len(codes)}只 K线: {len(kline)}只", flush=True)

# ==================== 预评分 ====================
print("P6真实引擎预评分...", flush=True)
import tqdm  # 试试有没有
try:
    from tqdm import tqdm as tq
except:
    tq = lambda x: x

score_cache = {}
se = SeasonEngine()
for i, td in enumerate(sample_dates):
    ctx = MarketContext(se.judge_market_season())
    for code in codes:
        try:
            r = score_stock(code, ctx)
            raw = r.get('score', 0)
            if raw > 0:
                score_cache.setdefault(code, {})[str(td)] = raw
        except: pass
    print(f"  评分: {i+1}/{len(sample_dates)} ({len(score_cache)}只覆盖)", flush=True)

print(f"预评分完成: {sum(len(v) for v in score_cache.values())}条", flush=True)

# ==================== 回测函数 ====================
def get_price(code, tgt):
    for r in kline.get(code, []):
        if str(r['trade_date']) == str(tgt): return float(r['close'])
    return None

def get_score(code, tgt):
    scores = score_cache.get(code, {})
    best_d, best_s = None, 0
    for ds, s in scores.items():
        d = date.fromisoformat(ds) if isinstance(ds, str) else ds
        if d <= tgt and (best_d is None or d > best_d): best_d, best_s = d, s
    return best_s if best_s > 0 else None

def run_one(name, buy, hold, stop, seasonal=False, force_hold=None):
    trades = []; pos = {}
    for i, today in enumerate(all_dates):
        if i % 400 == 0: print(f"  [{name}] {i}/{len(all_dates)}", flush=True)
        sea = season_map.get(today, 'chaos')
        
        if seasonal:
            buy_t = SEASONAL_BUY.get(sea, 38)
            hold_t = force_hold if force_hold else SEASONAL_HOLD.get(sea, 20)
            stop_t = -0.12 if sea in ('chaos','chaos_autumn','autumn') else -0.10
        else:
            buy_t, hold_t, stop_t = buy, hold, stop
        
        if buy_t >= 999: pos.clear(); continue
        
        tocl = []
        for code, p in list(pos.items()):
            price = get_price(code, today)
            if price is None: continue
            ret = (price - p['bp']) / p['bp']
            if price > p.get('peak', p['bp']): p['peak'] = price
            dd = (today - p['bd']).days
            if ret <= stop_t or (price - p['peak']) / p['peak'] <= -0.15:
                tocl.append((code, ret))
            elif dd >= hold_t:
                tocl.append((code, ret))
        for code, ret in tocl:
            p = pos.pop(code)
            trades.append({'bd':str(p['bd']),'sd':str(today),'hd':(today-p['bd']).days,'r':ret,'sc':p['sc'],'sea':p['sea']})
        for code in codes:
            if code in pos or len(pos) >= 10: continue
            price = get_price(code, today)
            if price is None: continue
            sc = get_score(code, today)
            if sc is None or sc < buy_t: continue
            pos[code] = {'bd':today,'bp':price,'peak':price,'sc':sc,'sea':sea}
    
    last = all_dates[-1]
    for code, p in pos.items():
        price = get_price(code, last) or p['bp']
        ret = (price - p['bp']) / p['bp']
        trades.append({'bd':str(p['bd']),'sd':str(last),'hd':(last-p['bd']).days,'r':ret,'sc':p['sc'],'sea':p['sea']})
    
    return trades

def calc(ts):
    if not ts: return None
    w = [t for t in ts if t['r'] > 0]; l = [t for t in ts if t['r'] <= 0]
    tg = sum(t['r'] for t in w) or 0.001; tl = abs(sum(t['r'] for t in l)) or 1
    return {
        'n': len(ts), 'wr': round(len(w)/len(ts)*100,1),
        'ar': round(sum(t['r'] for t in ts)/len(ts)*100,2),
        'aw': round(sum(t['r'] for t in w)/len(w)*100,2) if w else 0,
        'al': round(sum(t['r'] for t in l)/len(l)*100,2) if l else 0,
        'pf': round(tg/tl,2),
        'yearly': round(sum(t['r'] for t in ts)/len(ts)/3.3*100,2),
    }

# ==================== 跑所有方案 ====================
results = []
for s in SCHEMES:
    print(f"\n{'='*50}\n🏃 {s['name']}: {s['desc']}", flush=True)
    t0 = time.time()
    seasonal = s.get('seasonal', False)
    force_hold = s.get('force_hold')
    trades = run_one(s['name'], s['buy'], s['hold'], s['stop'], seasonal, force_hold)
    st = calc(trades)
    elapsed = time.time() - t0
    if st:
        print(f"  总交易:{st['n']}笔 胜率:{st['wr']}% 均收益:{st['ar']:+.2f}% 盈亏比:{st['pf']} 均盈:{st['aw']:+.2f}% 均亏:{st['al']:+.2f}%", flush=True)
        results.append({**s, **st, 'trades': trades, 'time': elapsed})

# ==================== 排名 ====================
print(f"\n{'='*60}")
print("📊 P6真实引擎 · 多参数调优排名")
print(f"{'='*60}")
print(f"{'排名':>4} {'方案':<14} {'笔数':>6} {'胜率':>7} {'均收益':>9} {'盈亏比':>7} {'均盈利':>8} {'年化':>7}")
print("-"*60)

# 按综合得分排序（胜率×盈亏比×均收益的几何均值）
for r in results:
    ar_std = (r['ar'] + 10) / 20  # 归一化(假设-10%到+10%)
    wr_std = r['wr'] / 100
    pf_std = min(r['pf'] / 3, 1)
    r['score'] = round((wr_std * pf_std * ar_std * 100), 1)

ranked = sorted(results, key=lambda x: x.get('score', 0), reverse=True)
for i, r in enumerate(ranked):
    print(f"{i+1:>4} {r['name']:<14} {r['n']:>6} {r['wr']:>6}% {r['ar']:>+8.2f}% {r['pf']:>7.2f} {r['aw']:>+7.2f}% {r['yearly']:>+6.2f}%")

print(f"\n🏆 最优方案: {ranked[0]['name']}: 胜率{ranked[0]['wr']}% 均收益{ranked[0]['ar']:+.2f}% 盈亏比{ranked[0]['pf']}")
print(f"  次优方案: {ranked[1]['name']}: 胜率{ranked[1]['wr']}% 均收益{ranked[1]['ar']:+.2f}% 盈亏比{ranked[1]['pf']}")
print(f"  第三方案: {ranked[2]['name']}: 胜率{ranked[2]['wr']}% 均收益{ranked[2]['ar']:+.2f}% 盈亏比{ranked[2]['pf']}")
