#!/usr/bin/env python3
"""
P6真实引擎回测 v4.0 — 每5日采样
实时输出到 stdout，无缓冲
"""
import sys, os, time
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')
os.environ['STOCK_USER'] = os.environ.get('STOCK_USER', 'tony')

from db_config import get_connection
from p6_dual_track_engine import score_stock, MarketContext
from season_engine import SeasonEngine

START = date(2023, 1, 1)
print("加载数据...", flush=True)

conn = get_connection(); cur = conn.cursor()

# 加载季节
cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
season_map = {}
for r in cur.fetchall():
    td = r['trade_date']
    if isinstance(td, str): td = date.fromisoformat(td)
    if td >= START: season_map[td] = r['season']

dates = sorted(season_map.keys())
# 每5日采样
sample_dates = [d for i, d in enumerate(dates) if i % 20 == 0]
print(f"季节: {len(season_map)}天 采样: {len(sample_dates)}个", flush=True)

cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
codes = [r['ts_code'] for r in cur.fetchall()]
cur.close(); conn.close()
print(f"监控池: {len(codes)}只", flush=True)

# 预评分
print("开始预评分(P6真实引擎)...", flush=True)
score_cache = {}
se = SeasonEngine()
total_dates = len(sample_dates)
t0 = time.time()

for i, td in enumerate(sample_dates):
    ctx = MarketContext(se.judge_market_season())
    for code in codes:
        try:
            r = score_stock(code, ctx)
            raw = r.get('score', 0)
            if raw > 0:
                score_cache.setdefault(code, {})[str(td)] = raw
        except: pass
    
    elapsed = time.time() - t0
    eta = (total_dates - i - 1) * (elapsed / (i + 1)) if i > 0 else 9999
    print(f"  评分: {i+1}/{total_dates} ({len(score_cache)}只覆盖) ETA:{eta/60:.0f}min", flush=True)

print(f"预评分完成: {sum(len(v) for v in score_cache.values())}条, {len(score_cache)}只", flush=True)

# 加载K线
conn = get_connection(); cur = conn.cursor()
kline = {}
for code in codes:
    cur.execute("SELECT trade_date, close FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC", (code,))
    rows = cur.fetchall()
    if len(rows) >= 120: kline[code] = rows
cur.close(); conn.close()
print(f"K线: {len(kline)}只", flush=True)

def get_price(code, tgt):
    for r in kline.get(code, []):
        if str(r['trade_date']) == str(tgt): return float(r['close'])
    return None

def get_score(code, tgt):
    scores = score_cache.get(code, {})
    best_d, best_s = None, 0
    for d_s, s in scores.items():
        d = date.fromisoformat(d_s)
        if d <= tgt and (best_d is None or d > best_d): best_d, best_s = d, s
    return best_s if best_s > 0 else None

# 回测
THRESHOLDS = {
    'summer':{'buy':45,'stop':-0.10,'hold':30},
    'spring':{'buy':42,'stop':-0.10,'hold':25},
    'chaos_spring':{'buy':42,'stop':-0.10,'hold':20},
    'chaos':{'buy':38,'stop':-0.12,'hold':20},
    'chaos_autumn':{'buy':35,'stop':-0.12,'hold':15},
    'autumn':{'buy':30,'stop':-0.12,'hold':15},
    'winter':{'buy':999}, 'panic':{'buy':999}, 'recovery':{'buy':38,'stop':-0.10,'hold':20},
}

print("开始回测...", flush=True)
trades = []; pos = {}
for i, today in enumerate(dates):
    if i % 200 == 0: print(f"  回测: {i}/{len(dates)}", flush=True)
    sea = season_map.get(today, 'chaos')
    c = THRESHOLDS[sea]
    if c['buy'] >= 999: pos.clear(); continue
    toc = []
    for code, p in list(pos.items()):
        price = get_price(code, today)
        if price is None: continue
        ret = (price - p['bp']) / p['bp']
        if price > p.get('peak', p['bp']): p['peak'] = price
        dd = (today - p['bd']).days
        if ret <= c['stop'] or (price - p['peak']) / p['peak'] <= -0.15:
            toc.append((code, ret))
        elif dd >= c['hold']:
            toc.append((code, ret))
    for code, ret in toc:
        p = pos.pop(code)
        trades.append({'c':code,'bd':str(p['bd']),'sd':str(today),'hd':(today-p['bd']).days,'r':ret,'sc':p['sc'],'sea':p['sea']})
    for code in codes:
        if code in pos or len(pos) >= 10: continue
        price = get_price(code, today)
        if price is None: continue
        sc = get_score(code, today)
        if sc is None or sc < c['buy']: continue
        pos[code] = {'bd':today,'bp':price,'peak':price,'sc':sc,'sea':sea}

last = dates[-1]
for code, p in pos.items():
    price = get_price(code, last) or p['bp']
    ret = (price - p['bp']) / p['bp']
    trades.append({'c':code,'bd':str(p['bd']),'sd':str(last),'hd':(last-p['bd']).days,'r':ret,'sc':p['sc'],'sea':p['sea']})

print(f"回测完成: {len(trades)}笔交易", flush=True)

# 统计
w = [t for t in trades if t['r'] > 0]
l = [t for t in trades if t['r'] <= 0]
tg = sum(t['r'] for t in w) or 0.001
tl = abs(sum(t['r'] for t in l)) or 1

seas = {}
for s, lb in [('summer','夏'),('spring','春'),('chaos_spring','弱春'),('chaos','混沌'),('chaos_autumn','弱秋'),('autumn','秋'),('winter','冬')]:
    g = [t for t in trades if t.get('sea') == s]
    if not g: continue
    wg = [t for t in g if t['r'] > 0]
    seas[lb] = {'n':len(g),'wr':round(len(wg)/len(g)*100,1),'ar':round(sum(t['r']for t in g)/len(g)*100,2)}

holds = {}
for d in [10,20,30,60]:
    g = [t for t in trades if t['hd'] <= d]
    if g: wg = [t for t in g if t['r']>0]; holds[f'≤{d}d'] = {'n':len(g),'wr':round(len(wg)/len(g)*100,1),'ar':round(sum(t['r']for t in g)/len(g)*100,2)}

print(f"""
{'='*55}
📊 P6真实引擎回测 — MAY分季自适应
{'='*55}
总交易: {len(trades)}笔
胜率: {round(len(w)/len(trades)*100,1) if trades else 0}%
均收益: {round(sum(t['r']for t in trades)/len(trades)*100,2) if trades else 0:+.2f}%
盈亏比: {round(tg/tl,2)}
均盈利: {round(sum(t['r']for t in w)/len(w)*100,2) if w else 0:+.2f}%
均亏损: {round(sum(t['r']for t in l)/len(l)*100,2) if l else 0:+.2f}%
年化(3.3年): {round(sum(t['r']for t in trades)/len(trades)/3.3*100,2) if trades else 0:+.2f}%/笔

--- 按季节 ---""", flush=True)

for lb in ['夏','春','弱春','混沌','弱秋','秋','冬']:
    if lb in seas: v = seas[lb]; print(f"  {lb}: {v['n']}笔 胜率{v['wr']}% 均收益{v['ar']:+.2f}%")

print("\n--- 按持有天数 ---", flush=True)
for k,v in sorted(holds.items()): print(f"  {k}: {v['n']}笔 胜率{v['wr']}% 均收益{v['ar']:+.2f}%")

print(f"""
--- 对比 ---
94只龙头(真实引擎): 54.0% | +9.08% | 2.71
195只全量(真实引擎): {round(len(w)/len(trades)*100,1) if trades else 0}% | {round(sum(t['r']for t in trades)/len(trades)*100,2) if trades else 0:+.2f}% | {round(tg/tl,2)}
""", flush=True)
