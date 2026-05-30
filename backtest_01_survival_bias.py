#!/usr/bin/env python3
"""
① 生存偏差交叉验证 — 独立回测脚本
===================================
对比带止损(回撤10%) vs 不带止损的策略效果差异
基于460天season_state + 95只监控池K线

输出: /tmp/backtest_01_survival_bias.json + stdout表格
"""
import os, sys, json, time, math
from datetime import datetime, date
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('b01_survival')

def load_data():
    with db_cursor(commit=False) as cur:
        # 季节
        cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
        seasons = {r['trade_date']: r['season'] for r in cur.fetchall()}
        
        # 监控池
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        pool = {r['ts_code']: r['name'] for r in cur.fetchall()}
        
        # K线 (全量)
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
    logger.info(f"  有K线: {len(kline)}只")
    
    position_map = {'summer': 1.0, 'chaos_spring': 0.6, 'spring': 0.6,
                    'chaos': 0.4, 'chaos_autumn': 0.3, 'autumn': 0.2}
    
    results = {}  # periods: 5,10,20,30,60
    
    for period in [5, 10, 20, 30, 60]:
        logger.info(f"回测持有期={period}日...")
        
        ns_trades = 0; ns_wins = 0; ns_ret_sum = 0.0; ns_ret_list = []
        ws_trades = 0; ws_wins = 0; ws_ret_sum = 0.0; ws_ret_list = []
        stop_count = 0
        
        for ts_code, kd in kline.items():
            dates = sorted(kd.keys())
            if len(dates) < period + 5: continue
            
            for i in range(len(dates) - period):
                buy_date = dates[i]
                if buy_date not in seasons: continue
                season = seasons[buy_date]
                pos_ratio = position_map.get(season, 0.3)
                if pos_ratio < 0.3: continue
                
                # 简单信号筛选: 前5日不暴跌
                if i >= 5:
                    prior_ret = (kd[dates[i]] - kd[dates[i-5]]) / kd[dates[i-5]] * 100
                    if prior_ret < -5: continue  # 暴跌过滤
                
                bp = kd[buy_date]
                if bp <= 0: continue
                
                sell_date = dates[i + period]
                sp = kd[sell_date]
                ret = (sp - bp) / bp * 100
                
                # --- 不带止损 ---
                ns_trades += 1
                ns_ret_sum += ret * pos_ratio
                ns_ret_list.append(ret * pos_ratio)
                if ret > 0: ns_wins += 1
                
                # --- 带止损(最高回撤10%) ---
                stop_hit = False
                interim_max = bp
                for j in range(i+1, i+period+1):
                    d = dates[j]
                    hp = kd[d] * 1.02  # 用close近似high
                    if hp > interim_max: interim_max = hp
                    if kd[d] <= interim_max * 0.9:
                        stop_hit = True
                        ret_stop = (interim_max*0.9 - bp) / bp * 100
                        ws_ret_sum += ret_stop * pos_ratio
                        ws_ret_list.append(ret_stop * pos_ratio)
                        if ret_stop > 0: ws_wins += 1
                        stop_count += 1
                        break
                if not stop_hit:
                    ws_trades += 1
                    ws_ret_sum += ret * pos_ratio
                    ws_ret_list.append(ret * pos_ratio)
                    if ret > 0: ws_wins += 1
        
        total_trades = ns_trades + ws_trades
        results[f'{period}d'] = {
            'total_signals': total_trades,
            'no_stop': {
                'trades': ns_trades,
                'wins': ns_wins,
                'win_rate': round(ns_wins/ns_trades*100, 2) if ns_trades else 0,
                'avg_return': round(ns_ret_sum/ns_trades, 2) if ns_trades else 0,
                'total_return': round(ns_ret_sum, 2),
                'std': round(math.sqrt(sum((x - ns_ret_sum/ns_trades)**2 for x in ns_ret_list)/max(1,ns_trades)), 2) if ns_trades > 1 else 0
            },
            'with_stop': {
                'trades': ws_trades,
                'wins': ws_wins,
                'win_rate': round(ws_wins/ws_trades*100, 2) if ws_trades else 0,
                'avg_return': round(ws_ret_sum/ws_trades, 2) if ws_trades else 0,
                'total_return': round(ws_ret_sum, 2),
                'std': round(math.sqrt(sum((x - ws_ret_sum/ws_trades)**2 for x in ws_ret_list)/max(1,ws_trades)), 2) if ws_trades > 1 else 0,
                'stop_rate': round(stop_count/total_trades*100, 2) if total_trades else 0,
                'stops_triggered': stop_count
            }
        }
        
        r = results[f'{period}d']
        ns = r['no_stop']; ws = r['with_stop']
        print(f"  [{period:>2}d] 总信号{total_trades:>6} | "
              f"无止损: 胜率{ns['win_rate']:>5.1f}% 均收益{ns['avg_return']:>7.2f}% STD{ns['std']:>5.2f} | "
              f"止损: 胜率{ws['win_rate']:>5.1f}% 均收益{ws['avg_return']:>7.2f}% 止损率{ws['stop_rate']:>5.1f}%")
    
    # 打印汇总
    print(f"\n{'='*100}")
    print(f" ① 生存偏差交叉验证 — 汇总")
    print(f"{'='*100}")
    print(f"{'持有期':>8} {'总信号':>8} | {'不带止损':>35} | {'带止损(回撤10%)':>45}")
    print("-"*100)
    for p in ['5d','10d','20d','30d','60d']:
        if p not in results: continue
        r = results[p]; ns = r['no_stop']; ws = r['with_stop']
        print(f"{p:>8} {r['total_signals']:>8} | "
              f"胜率{ns['win_rate']:>5.1f}% 均{ns['avg_return']:>7.2f}% σ{ns['std']:>5.2f} | "
              f"胜率{ws['win_rate']:>5.1f}% 均{ws['avg_return']:>7.2f}% σ{ws['std']:>5.2f} 止损{ws['stop_rate']:>5.1f}%({ws['stops_triggered']})")
    
    # 保存
    out = '/tmp/backtest_01_survival_bias.json'
    with open(out, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"结果已保存: {out}")

if __name__ == '__main__':
    t0 = time.time()
    run()
    logger.info(f"耗时: {time.time()-t0:.0f}s")
