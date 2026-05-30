#!/usr/bin/env python3
"""
② 混沌三子态专项回测 — 独立脚本
==================================
分析 chaos/chaos_spring/chaos_autumn 三种混沌子态下的
持有期收益、胜率、夏普、IC系数、最佳持有期

输出: /tmp/backtest_02_chaos_substates.json + stdout表格
"""
import os, sys, json, time, math
from datetime import datetime, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('b02_chaos')

def spearman_rank(x, y):
    n = len(x)
    if n < 3: return 0
    rx = [sorted(x).index(v)+1 for v in x]
    ry = [sorted(y).index(v)+1 for v in y]
    d2 = sum((rx[i]-ry[i])**2 for i in range(n))
    return round(1 - 6*d2/(n*(n**2-1)), 4)

def load_data():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
        seasons = {r['trade_date']: r['season'] for r in cur.fetchall()}
        
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        pool = {r['ts_code']: r['name'] for r in cur.fetchall()}
        
        codes = list(pool.keys())
        ph = ','.join(['%s']*len(codes))
        cur.execute(f"""
            SELECT ts_code, trade_date, close FROM daily_kline 
            WHERE ts_code IN ({ph}) AND trade_date >= '2024-01-01'
            ORDER BY ts_code, trade_date
        """, codes)
        kline = defaultdict(dict)
        for r in cur.fetchall():
            kline[r['ts_code']][r['trade_date']] = float(r['close'] or 0)
        
        return seasons, pool, dict(kline)

def run():
    logger.info("加载数据...")
    seasons, pool, kline = load_data()
    logger.info(f"  season_state: {len(seasons)}天")
    logger.info(f"  watch_pool: {len(pool)}只")
    
    sub_states = ['chaos', 'chaos_spring', 'chaos_autumn']
    periods = [5, 10, 20, 30, 60]
    results = {s: {} for s in sub_states}
    
    # 各子态的历史交易日数统计
    substate_dates = defaultdict(set)
    for d, s in seasons.items():
        substate_dates[s].add(d)
    for s in sub_states:
        logger.info(f"  {s}: {len(substate_dates[s])}个交易日")
    
    for substate in sub_states:
        logger.info(f"回测 {substate}...")
        for period in periods:
            all_rets = []
            score_ret_pairs = []  # 用于IC分析
            
            for ts_code, kd in kline.items():
                dates = sorted(kd.keys())
                for i in range(len(dates) - period):
                    buy_date = dates[i]
                    if seasons.get(buy_date) != substate: continue
                    bp = kd[buy_date]
                    if bp <= 0: continue
                    sp = kd[dates[i+period]]
                    ret = (sp - bp) / bp * 100
                    all_rets.append(ret)
                    # 用前5日涨幅近似评分
                    if i >= 5:
                        prior = (kd[dates[i]] - kd[dates[i-5]]) / kd[dates[i-5]] * 100
                        score_ret_pairs.append((prior, ret))
            
            if not all_rets:
                results[substate][f'{period}d'] = {'trades': 0}
                continue
            
            n = len(all_rets)
            avg = sum(all_rets)/n
            wins = sum(1 for r in all_rets if r > 0)
            med = sorted(all_rets)[n//2]
            std = math.sqrt(sum((r-avg)**2 for r in all_rets)/n) if n > 1 else 0
            sharpe = avg/max(0.001, std)
            
            # 高分群 vs 低分群 (前20%动量)
            if score_ret_pairs:
                sorted_pairs = sorted(score_ret_pairs, key=lambda x: x[0])
                top_n = max(1, len(sorted_pairs)//5)
                top_ret = sum(p[1] for p in sorted_pairs[-top_n:])/top_n
                bot_ret = sum(p[1] for p in sorted_pairs[:top_n])/top_n
                ic = spearman_rank([p[0] for p in score_ret_pairs], [p[1] for p in score_ret_pairs])
            else:
                top_ret = bot_ret = ic = 0
            
            results[substate][f'{period}d'] = {
                'trades': n, 'win_rate': round(wins/n*100, 2),
                'avg_return': round(avg, 2), 'median': round(med, 2),
                'std': round(std, 2), 'sharpe': round(sharpe, 2),
                'max': round(max(all_rets), 2), 'min': round(min(all_rets), 2),
                'top20_ret': round(top_ret, 2), 'bot20_ret': round(bot_ret, 2),
                'ic': ic
            }
    
    # 打印
    print(f"\n{'='*120}")
    print(f" ② 混沌三子态专项回测")
    print(f"{'='*120}")
    for substate in sub_states:
        print(f"\n  ▶ {substate} ({len(substate_dates[substate])}个交易日):")
        best_p, best_sharpe = '', 0
        for p in ['5d','10d','20d','30d','60d']:
            if p in results[substate] and results[substate][p]['trades'] > 0:
                sh = results[substate][p]['sharpe']
                if sh > best_sharpe: best_p, best_sharpe = p, sh
        
        print(f"{'持有期':>8} {'样本':>8} {'胜率':>7} {'均收益':>9} {'中位收益':>9} {'波动率':>8} {'夏普':>7} {'IC':>8} {'Top20%':>9} {'Bot20%':>9} {'最优':>8}")
        print("-"*100)
        for p in ['5d','10d','20d','30d','60d']:
            if p not in results[substate] or results[substate][p]['trades'] == 0: continue
            r = results[substate][p]
            marker = '🏆' if p == best_p else ''
            print(f"{p:>8} {r['trades']:>8} {r['win_rate']:>5.1f}% {r['avg_return']:>8.2f}% {r['median']:>8.2f}% {r['std']:>7.2f}% {r['sharpe']:>6.2f} {r['ic']:>7.4f} {r['top20_ret']:>8.2f}% {r['bot20_ret']:>8.2f}% {marker:>8}")
    
    # 保存
    out = '/tmp/backtest_02_chaos_substates.json'
    with open(out, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果已保存: {out}")

if __name__ == '__main__':
    t0 = time.time()
    run()
    logger.info(f"耗时: {time.time()-t0:.0f}s")
