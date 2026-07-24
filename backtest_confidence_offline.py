#!/usr/bin/env python3
"""
backtest_confidence_offline.py v1.2 — 置信度引擎 5日收益离线回测
预加载所有历史评分，避免 confidence_engine 内单条查库
"""

import sys, os, time, csv
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection
from confidence_engine import ConfidenceEngine

START = '2026-07-01'
END = '2026-07-17'
LOOKBACK = 5

start_time = time.time()
conn = get_connection()
cur = conn.cursor()
ce = ConfidenceEngine()

# Step 1: 获取所有评分记录（含chanlun结构数据）
cur.execute("""
    SELECT s.trade_date, s.ts_code, s.calibrated_score,
           s.trend_score, s.mf_score, s.momentum_score, s.structure_score,
           s.season,
           c.structure_score as c_struct_score,
           c.buy_sell_point, c.autumn_tiger
    FROM strategy_signal s
    LEFT JOIN chanlun_structure c 
      ON s.ts_code = c.ts_code AND s.trade_date = c.trade_date
    WHERE s.trade_date >= %s AND s.trade_date <= %s
      AND s.calibrated_score IS NOT NULL
    ORDER BY s.trade_date, s.ts_code
""", (START, END))
rows = list(cur.fetchall())
print(f"评分记录: {len(rows)}", flush=True)

# Step 2: 预加载所有历史评分 {(ts_code, trade_date): calibrated_score}
cur.execute("""
    SELECT ts_code, trade_date, calibrated_score
    FROM strategy_signal
    WHERE trade_date >= %s AND trade_date <= DATE_ADD(%s, INTERVAL 5 DAY)
      AND trade_date < %s
    ORDER BY ts_code, trade_date
""", ('2026-06-26', END, END))  # 从6/26开始取，覆盖LOOKBACK=5的窗口
hist_scores = {}
for r in cur.fetchall():
    ts = r['ts_code']
    td = r['trade_date']
    sc = float(r['calibrated_score']) if r['calibrated_score'] is not None else None
    hist_scores[(ts, td)] = sc
print(f"历史评分缓存: {len(hist_scores)}条", flush=True)

# Step 3: 预加载K线涨幅
cur.execute("""
    SELECT ts_code, trade_date, change_pct
    FROM daily_kline
    WHERE trade_date >= %s AND trade_date <= DATE_ADD(%s, INTERVAL 21 DAY)
""", (START, END))
kline_data = { (r['ts_code'], r['trade_date']): float(r['change_pct']) for r in cur.fetchall() }
print(f"K线缓存: {len(kline_data)}条", flush=True)

cur.close()
conn.close()

# Step 4: 逐条计算（纯内存，不再调DB）
results = []
skipped = 0

for idx, r in enumerate(rows):
    td = r['trade_date']
    ts = r['ts_code']
    calib = float(r['calibrated_score']) if r['calibrated_score'] is not None else 0

    trend_sc = float(r['trend_score']) if r['trend_score'] is not None else None
    mf_sc = float(r['mf_score']) if r['mf_score'] is not None else None
    momentum_sc = float(r['momentum_score']) if r['momentum_score'] is not None else None
    struct_sc = float(r['structure_score']) if r['structure_score'] is not None else None
    c_struct_sc = float(r['c_struct_score']) if r['c_struct_score'] is not None else 0
    bs_point = r['buy_sell_point'] or 'none'
    autumn_tiger = bool(r['autumn_tiger'])

    # C_struct（无需DB）
    c_struct = ce.calc_structure_confidence(c_struct_sc, bs_point, autumn_tiger)

    # C_consensus（无需DB）
    c_consensus = ce.calc_consensus_confidence(trend_sc, struct_sc, mf_sc, momentum_sc)

    # C_history（手动计算代替调用，避免DB查询）
    lookback_dates = []
    for d in range(1, LOOKBACK + 1):
        hd = td - timedelta(days=d)
        key = (ts, hd)
        if key in hist_scores and hist_scores[key] is not None:
            lookback_dates.append(hist_scores[key])
        else:
            # 查前一个自然日（不是交易日）
            continue

    if len(lookback_dates) < 2:
        c_history = 40.0
    else:
        min_s = min(lookback_dates)
        max_s = max(lookback_dates)
        spread = max_s - min_s
        last_score = lookback_dates[0]
        first_score = lookback_dates[-1]
        trend = last_score - first_score

        if spread <= 5:
            vol_penalty = 0
        elif spread <= 15:
            vol_penalty = (spread - 5) * 2
        else:
            vol_penalty = 20 + (spread - 15) * 0.5
        vol_penalty = min(40, vol_penalty)

        trend_penalty = 15 if (abs(trend) > 20 and spread > 15) else 0
        c_history = max(0.0, min(100.0, 90 - vol_penalty - trend_penalty))

    # 合成
    total_score, level, desc = ce.composite_confidence(c_struct, c_consensus, c_history)

    # 安全门
    triggered_gates = []
    if calib >= 60 and level in ('D', 'E'):
        triggered_gates.append('score_conflict')
    elif calib < 20 and level in ('A', 'B'):
        triggered_gates.append('confidence_overrated')

    # 后续5日收益（复利）
    future_ret = None
    day_count = 0
    for d in range(1, 21):
        check_date = td + timedelta(days=d)
        key = (ts, check_date)
        if key in kline_data:
            pct = kline_data[key]
            if future_ret is None:
                future_ret = 1.0
            future_ret *= (1.0 + pct / 100.0)
            day_count += 1
            if day_count >= 5:
                break

    future_ret_pct = (future_ret - 1.0) * 100.0 if future_ret is not None else None
    if future_ret is None:
        skipped += 1

    results.append({
        'trade_date': str(td),
        'ts_code': ts,
        'calibrated': calib,
        'c_struct': round(c_struct, 1),
        'c_consensus': round(c_consensus, 1),
        'c_history': round(c_history, 1),
        'total_confidence': total_score,
        'level': level,
        'gates': '|'.join(triggered_gates) if triggered_gates else '-',
        'future_5d_ret': future_ret_pct,
        'season': r['season'] or 'chaos',
    })

    if (idx + 1) % 3000 == 0:
        print(f"  处理: {idx+1}/{len(rows)}", flush=True)

print(f"\n完成: {len(results)}条, 无未来数据: {skipped}", flush=True)

# Step 5: 统计
lb = defaultdict(lambda: {'n':0, 'ttl':0.0, 'pos':0, 'rets':[]})
gb = defaultdict(lambda: {'n':0, 'ttl':0.0, 'pos':0})
sl = defaultdict(lambda: defaultdict(lambda: {'n':0, 'ttl':0.0}))

for rec in results:
    fr = rec['future_5d_ret']
    if fr is None: continue
    lv = rec['level']
    lb[lv]['n'] += 1
    lb[lv]['ttl'] += fr
    lb[lv]['pos'] += 1 if fr > 0 else 0
    lb[lv]['rets'].append(fr)
    gk = '有安全门' if rec['gates'] != '-' else '无安全门'
    gb[gk]['n'] += 1
    gb[gk]['ttl'] += fr
    gb[gk]['pos'] += 1 if fr > 0 else 0
    sl[rec['season']][lv]['n'] += 1
    sl[rec['season']][lv]['ttl'] += fr

print("\n" + "=" * 65, flush=True)
print("置信度引擎 — 离线历史回测报告")
print(f"区间: {START} ~ {END} | 后续窗口: {LOOKBACK}日")
print("=" * 65, flush=True)

print(f"\n{'等级':>10s}  {'样本':>6s}  {'均值收益':>10s}  {'胜率':>6s}  {'中位收益':>10s}", flush=True)
print("-" * 50, flush=True)
for lv in sorted(lb.keys(), reverse=True):
    b = lb[lv]
    if b['n'] == 0: continue
    avg = b['ttl'] / b['n']
    wr = b['pos'] / b['n'] * 100
    med = sorted(b['rets'])[b['n'] // 2]
    ln = {'A':'A-高置信','B':'B-中置信','C':'C-中低','D':'D-低置信','E':'E-不可信'}
    print(f"{ln.get(lv,lv):>10s}  {b['n']:>6d}  {avg:>+8.2f}%  {wr:>5.1f}%  {med:>+8.2f}%", flush=True)

# A+B vs D+E
ab_n = lb['A']['n'] + lb['B']['n']
ab_ttl = lb['A']['ttl'] + lb['B']['ttl']
ab_pos = lb['A']['pos'] + lb['B']['pos']
de_n = lb['D']['n'] + lb['E']['n']
de_ttl = lb['D']['ttl'] + lb['E']['ttl']
de_pos = lb['D']['pos'] + lb['E']['pos']

print(f"\n【关键对比】A+B vs D+E", flush=True)
if ab_n > 0: print(f"  A+B高置信: n={ab_n:>5d}  均收益{ab_ttl/ab_n:+>+8.2f}%  胜率{ab_pos/ab_n*100:>5.1f}%", flush=True)
if de_n > 0: print(f"  D+E低置信: n={de_n:>5d}  均收益{de_ttl/de_n:+>+8.2f}%  胜率{de_pos/de_n*100:>5.1f}%", flush=True)

print(f"\n【安全门分组】", flush=True)
for gk in ['无安全门', '有安全门']:
    b = gb[gk]
    if b['n'] == 0: continue
    print(f"  {gk}: n={b['n']:>5d}  均收益{b['ttl']/b['n']:+>+8.2f}%  胜率{b['pos']/b['n']*100:>5.1f}%", flush=True)

print(f"\n【季节×置信度】5日收益均值", flush=True)
for season in sorted(sl.keys()):
    print(f"\n  [{season}]", flush=True)
    for lv in sorted(sl[season].keys(), reverse=True):
        b = sl[season][lv]
        if b['n'] > 0:
            print(f"    {lv}: n={b['n']:>4d}  均收益{b['ttl']/b['n']:+>+8.2f}%", flush=True)

out = f'backtest_confidence_report_{datetime.now():%Y%m%d_%H%M%S}.csv'
with open(out, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['trade_date','ts_code','calibrated','c_struct','c_consensus',
                                       'c_history','total_confidence','level','gates','future_5d_ret','season'])
    w.writeheader()
    w.writerows(results)
print(f"\n报告已保存: {out}", flush=True)
print(f"总耗时: {time.time() - start_time:.1f}秒" if 'start_time' in dir() else '', flush=True)
