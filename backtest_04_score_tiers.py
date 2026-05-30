#!/usr/bin/env python3
"""
④ 评分分档×持有期匹配 — 独立脚本
===================================
将评分按V分≥50/40-50/30-40/<30四档
分别回测1/2/3/5/10日持有期表现
找到各档最优持有期和夏普比

由于trend_score只有5天数据(5/25-29)，
本脚本也使用watch_pool_snapshot的截面数据做补充分析
并结合season_state的长期数据做季节分档回测

输出: /tmp/backtest_04_score_tiers.json + stdout表格
"""
import os, sys, json, time, math
from datetime import datetime, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('b04_tiers')

def load_trend_scores():
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT ts_code, trade_date, composite_score, cycle_score,
                   structure_score, emotion_score
            FROM trend_score ORDER BY ts_code, trade_date
        """)
        scores = defaultdict(list)
        for r in cur.fetchall():
            scores[r['ts_code']].append({
                'trade_date': r['trade_date'],
                'v_score': float(r['composite_score'] or 0),
                'trend': float(r['cycle_score'] or 0),
                'structure': float(r['structure_score'] or 0),
                'emotion': float(r['emotion_score'] or 0),
            })
        return dict(scores)

def load_snapshot():
    """watch_pool_snapshot的评分截面"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT ts_code, trade_date, v_score, season
            FROM watch_pool_snapshot ORDER BY ts_code, trade_date
        """)
        snap = defaultdict(list)
        for r in cur.fetchall():
            snap[r['ts_code']].append({
                'trade_date': r['trade_date'],
                'v_score': float(r['v_score'] or 0),
                'season': r['season'] or 'unknown'
            })
        return dict(snap)

def load_seasons():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
        return {r['trade_date']: r['season'] for r in cur.fetchall()}

def load_kline(codes, start='2024-01-01'):
    with db_cursor(commit=False) as cur:
        ph = ','.join(['%s']*len(codes)) if codes else ''
        if not ph: return {}
        cur.execute(f"""
            SELECT ts_code, trade_date, close FROM daily_kline 
            WHERE ts_code IN ({ph}) AND trade_date >= %s
            ORDER BY ts_code, trade_date
        """, list(codes) + [start])
        kline = defaultdict(dict)
        for r in cur.fetchall():
            kline[r['ts_code']][r['trade_date']] = float(r['close'] or 0)
        return dict(kline)

def run():
    logger.info("加载数据...")
    
    # ---- A. trend_score分档回测 (短持有期) ----
    scores = load_trend_scores()
    codes_a = list(scores.keys())
    kline_a = load_kline(codes_a, '2026-05-01')
    logger.info(f"A. trend_score分档: {len(scores)}只股票, {sum(len(v) for v in scores.values())}条")
    
    tiers = [('≥50优', 50, 101), ('40-50良', 40, 50), ('30-40中', 30, 40), ('<30一般', 0, 30)]
    short_periods = [1, 2, 3, 5]
    
    result_a = {}
    for tier_name, lo, hi in tiers:
        result_a[tier_name] = {}
        for period in short_periods:
            vals = []
            for ts_code, sl in scores.items():
                if ts_code not in kline_a: continue
                kd = kline_a[ts_code]
                dates = sorted(kd.keys())
                for s in sl:
                    v = s['v_score']
                    if v < lo or v >= hi: continue
                    try:
                        idx = dates.index(s['trade_date'])
                    except ValueError:
                        continue
                    if idx + period >= len(dates): continue
                    bp = kd[dates[idx]]
                    sp = kd[dates[idx+period]]
                    if bp <= 0: continue
                    ret = (sp - bp) / bp * 100
                    vals.append(ret)
            
            if not vals:
                result_a[tier_name][f'{period}d'] = {'trades': 0}
                continue
            
            n = len(vals)
            avg = sum(vals)/n
            wins = sum(1 for v in vals if v > 0)
            med = sorted(vals)[n//2]
            std = math.sqrt(sum((v-avg)**2 for v in vals)/n) if n > 1 else 0
            result_a[tier_name][f'{period}d'] = {
                'trades': n, 'win_rate': round(wins/n*100, 2),
                'avg_return': round(avg, 2), 'median': round(med, 2),
                'std': round(std, 2), 'sharpe': round(avg/max(0.001, std), 2),
                'max': round(max(vals), 2), 'min': round(min(vals), 2)
            }
    
    # ---- B. 季节分档回测 (基于season_state的历史K线) ----
    logger.info("B. 季节分档回测 (全量历史)...")
    seasons = load_seasons()
    
    season_tiers = [
        ('夏季(summer)', 'summer'),
        ('春混沌(chaos_spring)', 'chaos_spring'),
        ('基础混沌(chaos)', 'chaos'),
        ('秋混沌(chaos_autumn)', 'chaos_autumn'),
        ('秋季(autumn)', 'autumn')
    ]
    
    # 拿监控池的K线
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        pool_codes = [r['ts_code'] for r in cur.fetchall()]
    kline_b = load_kline(pool_codes, '2024-07-01')
    logger.info(f"  K线: {len(kline_b)}只股票")
    
    long_periods = [5, 10, 20, 30, 60]
    result_b = {}
    for season_name, season_key in season_tiers:
        result_b[season_name] = {}
        for period in long_periods:
            all_rets = []
            for ts_code, kd in kline_b.items():
                dates = sorted(kd.keys())
                for i in range(len(dates) - period):
                    buy_date = dates[i]
                    if seasons.get(buy_date) != season_key: continue
                    bp = kd[buy_date]
                    if bp <= 0: continue
                    sp = kd[dates[i+period]]
                    ret = (sp - bp) / bp * 100
                    all_rets.append(ret)
            
            if not all_rets:
                result_b[season_name][f'{period}d'] = {'trades': 0}
                continue
            
            n = len(all_rets)
            avg = sum(all_rets)/n
            wins = sum(1 for r in all_rets if r > 0)
            med = sorted(all_rets)[n//2]
            std = math.sqrt(sum((r-avg)**2 for r in all_rets)/n) if n > 1 else 0
            result_b[season_name][f'{period}d'] = {
                'trades': n, 'win_rate': round(wins/n*100, 2),
                'avg_return': round(avg, 2), 'median': round(med, 2),
                'std': round(std, 2), 'sharpe': round(avg/max(0.001, std), 2)
            }
    
    # ---- 打印 A ----
    print(f"\n{'='*100}")
    print(f" ④-A 评分分档 × 持有期 (trend_score截面)")
    print(f"{'='*100}")
    for tier_name, lo, hi in tiers:
        print(f"\n  ▶ {tier_name} (V={lo}~{hi}):")
        best_p, best_s = '', -999
        for p in ['1d','2d','3d','5d']:
            if p in result_a[tier_name] and result_a[tier_name][p]['trades'] > 0:
                sh = result_a[tier_name][p]['sharpe']
                if sh > best_s: best_p, best_s = p, sh
        
        print(f"{'持有期':>8} {'样本':>8} {'胜率':>7} {'均收益':>9} {'中位':>9} {'波动率':>8} {'夏普':>7} {'最优':>8}")
        print("-"*65)
        for p in ['1d','2d','3d','5d']:
            if p not in result_a[tier_name] or result_a[tier_name][p]['trades'] == 0: continue
            r = result_a[tier_name][p]
            marker = '🏆' if p == best_p else ''
            print(f"{p:>8} {r['trades']:>8} {r['win_rate']:>5.1f}% {r['avg_return']:>8.2f}% {r['median']:>8.2f}% {r['std']:>7.2f}% {r['sharpe']:>6.2f} {marker:>8}")
    
    # ---- 打印 B ----
    print(f"\n\n{'='*100}")
    print(f" ④-B 季节分档 × 持有期 (全量历史回测)")
    print(f"{'='*100}")
    for season_name, _ in season_tiers:
        print(f"\n  ▶ {season_name}:")
        best_p, best_s = '', -999
        for p in ['5d','10d','20d','30d','60d']:
            if p in result_b[season_name] and result_b[season_name][p]['trades'] > 0:
                sh = result_b[season_name][p]['sharpe']
                if sh > best_s: best_p, best_s = p, sh
        
        print(f"{'持有期':>8} {'样本':>8} {'胜率':>7} {'均收益':>9} {'中位':>9} {'波动率':>8} {'夏普':>7} {'最优':>8}")
        print("-"*65)
        for p in ['5d','10d','20d','30d','60d']:
            if p not in result_b[season_name] or result_b[season_name][p]['trades'] == 0: continue
            r = result_b[season_name][p]
            marker = '🏆' if p == best_p else ''
            print(f"{p:>8} {r['trades']:>8} {r['win_rate']:>5.1f}% {r['avg_return']:>8.2f}% {r['median']:>8.2f}% {r['std']:>7.2f}% {r['sharpe']:>6.2f} {marker:>8}")
    
    # ---- 保存 ----
    result = {
        'score_tiers': result_a,
        'season_tiers': result_b
    }
    out = '/tmp/backtest_04_score_tiers.json'
    with open(out, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果已保存: {out}")

if __name__ == '__main__':
    t0 = time.time()
    run()
    logger.info(f"耗时: {time.time()-t0:.0f}s")
