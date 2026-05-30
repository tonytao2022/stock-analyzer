#!/usr/bin/env python3
"""
③ 因子归因分析 — 独立脚本
=============================
基于trend_score(1631条/550只)的截面数据
分析 L1趋势/L2结构/L3情绪/综合 四个因子的IC系数与多空利差

输出: /tmp/backtest_03_factor_attribution.json + stdout表格
"""
import os, sys, json, time, math
from datetime import datetime, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('b03_factor')

def spearman_rank(x, y):
    n = len(x)
    if n < 3: return 0, 0.5
    rx = [sorted(x).index(v)+1 for v in x]
    ry = [sorted(y).index(v)+1 for v in y]
    d2 = sum((rx[i]-ry[i])**2 for i in range(n))
    return round(1 - 6*d2/(n*(n**2-1)), 4), round(0.5, 4)

def load_data():
    """加载trend_score + K线"""
    with db_cursor(commit=False) as cur:
        # trend_score
        cur.execute("""
            SELECT ts_code, trade_date, composite_score, cycle_score,
                   structure_score, emotion_score, raw_score
            FROM trend_score ORDER BY ts_code, trade_date
        """)
        scores = defaultdict(list)
        for r in cur.fetchall():
            scores[r['ts_code']].append({
                'trade_date': r['trade_date'],
                'v_score': float(r['composite_score'] or 0),
                'trend_score': float(r['cycle_score'] or 0),      # L1 趋势
                'momentum_score': float(r['structure_score'] or 0), # L2 结构/动量
                'emotion_score': float(r['emotion_score'] or 0),    # L3 情绪
                'raw_score': float(r['raw_score'] or 0),
            })
        
        # 所有有评分的股票K线
        all_codes = list(scores.keys())
        ph = ','.join(['%s']*len(all_codes))
        cur.execute(f"""
            SELECT ts_code, trade_date, close FROM daily_kline 
            WHERE ts_code IN ({ph}) AND trade_date >= '2026-01-01'
            ORDER BY ts_code, trade_date
        """, all_codes)
        kline = defaultdict(dict)
        for r in cur.fetchall():
            kline[r['ts_code']][r['trade_date']] = float(r['close'] or 0)
        
        return dict(scores), dict(kline)

def run():
    logger.info("加载数据...")
    scores, kline = load_data()
    ts_cnt = sum(len(v) for v in scores.values())
    logger.info(f"  trend_score: {len(scores)}只股票, {ts_cnt}条记录")
    logger.info(f"  有K线匹配: {len(kline)}只")
    
    # 四个因子定义
    factors = [
        ('L1_趋势(cycle_score)', 'trend_score'),
        ('L2_结构(structure_score)', 'momentum_score'),
        ('L3_情绪(emotion_score)', 'emotion_score'),
        ('综合(composite_score)', 'v_score')
    ]
    
    # 多持有期分析
    holding_periods = [1, 2, 3, 5, 10]
    
    result = {}
    for period in holding_periods:
        result[f'{period}d'] = {}
        for factor_name, field in factors:
            all_vals = []
            all_rets = []
            
            for ts_code, score_list in scores.items():
                if ts_code not in kline: continue
                kd = kline[ts_code]
                dates = sorted(kd.keys())
                
                for s in score_list:
                    buy_date = s['trade_date']
                    try:
                        idx = dates.index(buy_date)
                    except ValueError:
                        continue
                    if idx + period >= len(dates): continue
                    
                    bp = kd[buy_date]
                    sp = kd[dates[idx+period]]
                    if bp <= 0: continue
                    
                    ret = (sp - bp) / bp * 100
                    val = s[field]
                    all_vals.append(val)
                    all_rets.append(ret)
            
            if len(all_vals) < 10:
                result[f'{period}d'][factor_name] = {
                    'trades': len(all_vals), 'ic': 0, 'top30_ret': 0, 'bot30_ret': 0, 'spread': 0
                }
                continue
            
            ic, ic_p = spearman_rank(all_vals, all_rets)
            paired = sorted(zip(all_vals, all_rets), key=lambda x: x[0])
            n = len(paired)
            top_n = max(1, n // 3)
            top_ret = sum(p[1] for p in paired[-top_n:]) / top_n
            bot_ret = sum(p[1] for p in paired[:top_n]) / top_n
            
            # 因子收益 - 多空组合
            avg_ret = sum(all_rets) / n
            
            result[f'{period}d'][factor_name] = {
                'trades': n,
                'ic': ic,
                'ic_p': ic_p,
                'top30_ret': round(top_ret, 2),
                'bot30_ret': round(bot_ret, 2),
                'spread': round(top_ret - bot_ret, 2),
                'avg_market_ret': round(avg_ret, 2),
                'excess_ret': round(top_ret - avg_ret, 2)
            }
    
    # 打印
    print(f"\n{'='*110}")
    print(f" ③ 因子归因分析 — 多持有期IC + 利差")
    print(f"{'='*110}")
    
    print(f"\n{'因子':>25}", end='')
    for p in ['1d','2d','3d','5d','10d']:
        print(f" | {p:>14}", end='')
    print()
    print("-"*110)
    
    for factor_name, _ in factors:
        print(f"{factor_name:>25}", end='')
        for p in ['1d','2d','3d','5d','10d']:
            if p in result and factor_name in result[p]:
                r = result[p][factor_name]
                print(f" | IC{r['ic']:>5.3f} 利差{r['spread']:>5.1f}%", end='')
            else:
                print(f" | {'无数据':>14}", end='')
        print()
    
    # 最佳持有期 + 最佳因子
    print(f"\n{'='*80}")
    print(f" ③ 因子归因结论")
    print(f"{'='*80}")
    
    best_by_factor = {}
    for factor_name, _ in factors:
        best_p, best_ic, best_spread = '', -999, -999
        for p in ['1d','2d','3d','5d','10d']:
            if p in result and factor_name in result[p]:
                r = result[p][factor_name]
                if r['spread'] > best_spread:
                    best_spread = r['spread']
                    best_ic = r['ic']
                    best_p = p
        if best_p:
            best_by_factor[factor_name] = {'best_period': best_p, 'ic': best_ic, 'spread': best_spread}
            print(f"  {factor_name}: 最佳持有={best_p}, IC={best_ic}, 多空利差={best_spread}%")
    
    # 跨周期最强因子
    print(f"\n  最强因子(按利差):")
    all_metrics = []
    for p in ['1d','2d','3d','5d','10d']:
        if p not in result: continue
        for fn in [f[0] for f in factors]:
            if fn in result[p]:
                all_metrics.append((p, fn, result[p][fn]['spread'], result[p][fn]['ic']))
    all_metrics.sort(key=lambda x: -x[2])
    for p, fn, sp, ic in all_metrics[:5]:
        print(f"    {fn:>25} | {p:>3} | 利差{sp:>5.1f}% | IC{ic:>5.3f}")
    
    # 保存
    out = '/tmp/backtest_03_factor_attribution.json'
    with open(out, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果已保存: {out}")

if __name__ == '__main__':
    t0 = time.time()
    run()
    logger.info(f"耗时: {time.time()-t0:.0f}s")
