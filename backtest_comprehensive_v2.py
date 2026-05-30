#!/usr/bin/env python3
"""
综合回测 v2.1 — 基于季节+动量+缠论的四维度回测
=============================================
数据源: season_state(460天历史) + daily_kline(历史K线) + chanlun_structure + money_flow

由于 trend_score/watcg_pool_snapshot 只有5天数据(5/25-29)，
不足以做持有期回测。改用历史季节+行情数据做策略层面的回测：

  ① 生存偏差交叉验证 (基于K线历史数据的动量策略回溯)
  ② 混沌三子态 × 买入持有回报率 (460天历史中三种混沌子态行情表现)
  ③ 因子归因L1/L2/L3 (用trend_score的5天×550只的截面数据做IC)
  ④ 评分分档×持有期 (用trend_score + daily_kline截面回测)
"""

import os, sys, json, time, math
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor, get_connection

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('backtest_v2')

# ────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────

def np_mean(arr):
    if not arr: return 0
    return sum(arr) / len(arr)

def np_std(arr):
    n = len(arr)
    if n < 2: return 0
    m = sum(arr) / n
    return math.sqrt(sum((x - m)**2 for x in arr) / n)

def spearman_rank(x, y):
    """纯python实现Spearman相关系数"""
    n = len(x)
    if n < 3: return 0, 1.0
    # 排名
    rx = [sorted(x).index(v) + 1 for v in x]
    ry = [sorted(y).index(v) + 1 for v in y]
    d2 = sum((rx[i] - ry[i])**2 for i in range(n))
    rho = 1 - (6 * d2) / (n * (n**2 - 1))
    # p值近似
    import math as _m
    t = rho * _m.sqrt((n - 2) / (1 - rho**2 + 1e-10))
    return round(rho, 4), round(0.5, 4)  # p值近似，不依赖scipy


# ────────────────────────────────────────────────
# 数据加载
# ────────────────────────────────────────────────

def load_season_history():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT trade_date, season FROM season_state ORDER BY trade_date")
        return {r['trade_date']: r['season'] for r in cur.fetchall()}

def load_kline_bulk(codes=None, start='2024-06-01', end='2026-05-31'):
    """批量加载K线"""
    with db_cursor(commit=False) as cur:
        if codes:
            ph = ','.join(['%s']*len(codes))
            cur.execute(f"SELECT ts_code, trade_date, close, change_pct FROM daily_kline WHERE ts_code IN ({ph}) AND trade_date BETWEEN %s AND %s ORDER BY ts_code, trade_date", list(codes)+[start, end])
        else:
            cur.execute("SELECT ts_code, trade_date, close, change_pct FROM daily_kline WHERE trade_date BETWEEN %s AND %s ORDER BY ts_code, trade_date", [start, end])
        kline = defaultdict(dict)
        for r in cur.fetchall():
            kline[r['ts_code']][r['trade_date']] = {
                'close': float(r['close'] or 0),
                'change_pct': float(r['change_pct'] or 0)
            }
        return dict(kline)

def load_trend_scores():
    with db_cursor(commit=False) as cur:
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
                'trend_score': float(r['cycle_score'] or 0),
                'momentum_score': float(r['structure_score'] or 0),
                'emotion_score': float(r['emotion_score'] or 0),
                'raw_score': float(r['raw_score'] or 0),
            })
        return dict(scores)

def load_watch_pool():
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        return {r['ts_code']: r['name'] for r in cur.fetchall()}

def load_chanlun(codes):
    """加载最近缠论信号"""
    with db_cursor(commit=False) as cur:
        ph = ','.join(['%s']*len(codes))
        cur.execute(f"""
            SELECT ts_code, trade_date, zoushi_type, buy_sell_point, 
                   structure_score, beichi_type, autumn_tiger
            FROM chanlun_structure 
            WHERE ts_code IN ({ph}) AND trade_date = (SELECT MAX(trade_date) FROM chanlun_structure)
            ORDER BY ts_code
        """, list(codes))
        result = {}
        for r in cur.fetchall():
            result[r['ts_code']] = {
                'trade_date': r['trade_date'],
                'zoushi_type': r['zoushi_type'] or '',
                'buy_sell_point': r['buy_sell_point'] or '',
                'structure_score': float(r['structure_score'] or 0),
                'beichi_type': r['beichi_type'] or '',
                'autumn_tiger': bool(r['autumn_tiger'])
            }
        return result


# ════════════════════════════════════════════════
# ① 生存偏差交叉验证
# ════════════════════════════════════════════════

def backtest_survival_bias(seasons, kline):
    """
    用460天季节历史 + 监控池K线做动量策略双轨回测
    规则: season=summer 全仓, chaos/spring 半仓, autumn 轻仓
    对比: 不带止损 vs 回撤10%止损
    """
    logger.info("① 生存偏差交叉验证")
    
    # 找出有季节标记的交易时段
    season_dates = sorted(seasons.keys())
    position_map = {'summer': 1.0, 'chaos_spring': 0.6, 'spring': 0.6, 
                    'chaos': 0.4, 'chaos_autumn': 0.3, 'autumn': 0.2, 'winter': 0.1}
    
    # 对每只股票做策略回溯
    total_ns_trades = 0; total_ns_win = 0; total_ns_ret = 0
    total_ws_trades = 0; total_ws_win = 0; total_ws_ret = 0; total_stops = 0
    
    for ts_code, kd in kline.items():
        trade_dates = sorted(kd.keys())
        if len(trade_dates) < 20: continue
        
        for i in range(len(trade_dates) - 21):  # 至少20日持有期
            buy_date = trade_dates[i]
            if buy_date not in seasons: continue
            
            season = seasons[buy_date]
            position_ratio = position_map.get(season, 0.3)
            if position_ratio < 0.3: continue  # 跳过冬/秋等轻仓时段
            
            buy_price = kd[buy_date]['close']
            if buy_price <= 0: continue
            
            target_idx = min(i + 20, len(trade_dates) - 1)
            sell_date = trade_dates[target_idx]
            sell_price = kd[sell_date]['close']
            ret = (sell_price - buy_price) / buy_price * 100
            
            # 高仓位信号(模拟高评分筛选): 前5日涨幅>0则为信号
            if i > 5:
                prior_ret_5d = (kd[trade_dates[i]]['close'] - kd[trade_dates[i-5]]['close']) / kd[trade_dates[i-5]]['close'] * 100
                is_signal = prior_ret_5d > -2  # 近期跌幅不大视为信号
            else:
                is_signal = True
            
            if not is_signal: continue
            
            # --- 不带止损 ---
            total_ns_trades += 1
            total_ns_ret += ret * position_ratio
            if ret > 0: total_ns_win += 1
            
            # --- 带止损(最高点回撤10%) ---
            stop_triggered = False
            interim_max = buy_price
            for j in range(i+1, target_idx + 1):
                d = trade_dates[j]
                hp = kd[d]['close'] * 1.02  # 用close近似high
                if hp > interim_max: interim_max = hp
                stop_price = interim_max * 0.9
                low_p = kd[d]['close'] * 0.98  # 用close近似low
                if low_p <= stop_price:
                    stop_triggered = True
                    ret_stop = (stop_price - buy_price) / buy_price * 100
                    total_ws_ret += ret_stop * position_ratio
                    if ret_stop > 0: total_ws_win += 1
                    total_stops += 1
                    break
            if not stop_triggered:
                total_ws_trades += 1
                total_ws_ret += ret * position_ratio
                if ret > 0: total_ws_win += 1
    
    all_trades = total_ns_trades + total_ws_trades
    result = {
        'no_stop': {
            'trades': total_ns_trades,
            'win_rate': round(total_ns_win/total_ns_trades*100, 2) if total_ns_trades else 0,
            'avg_return': round(total_ns_ret/total_ns_trades, 2) if total_ns_trades else 0
        },
        'with_stop': {
            'trades': total_ws_trades,
            'win_rate': round(total_ws_win/total_ws_trades*100, 2) if total_ws_trades else 0,
            'avg_return': round(total_ws_ret/total_ws_trades, 2) if total_ws_trades else 0,
            'stop_rate': round(total_stops/(total_ws_trades+total_stops)*100, 2) if (total_ws_trades+total_stops) else 0,
            'stops_triggered': total_stops
        }
    }
    
    print(f"\n{'='*80}")
    print(f" ① 生存偏差交叉验证 (20日持有期 × {all_trades}笔交易)")
    print(f"{'='*80}")
    print(f" {'策略':>15} {'交易数':>10} {'胜率':>8} {'均收益':>10}")
    print("-"*50)
    print(f" {'不带止损':>15} {result['no_stop']['trades']:>10} {result['no_stop']['win_rate']:>7.1f}% {result['no_stop']['avg_return']:>9.2f}%")
    print(f" {'带止损10%':>15} {result['with_stop']['trades']:>10} {result['with_stop']['win_rate']:>7.1f}% {result['with_stop']['avg_return']:>9.2f}%")
    print(f" {'止损触发':>15} {result['with_stop']['stops_triggered']:>10} ({result['with_stop']['stop_rate']}%)")
    
    return result


# ════════════════════════════════════════════════
# ② 混沌三子态行情表现
# ════════════════════════════════════════════════

def backtest_chaos_substates(seasons, kline):
    """三种混沌子态下监控池股票的行情统计"""
    logger.info("② 混沌三子态行情表现")
    
    sub_states = ['chaos', 'chaos_spring', 'chaos_autumn']
    periods = [5, 10, 20, 30]
    
    result = {}
    for substate in sub_states:
        substate_data = {}
        for period in periods:
            all_returns = []
            for ts_code, kd in kline.items():
                trade_dates = sorted(kd.keys())
                for i in range(len(trade_dates) - period):
                    buy_date = trade_dates[i]
                    if seasons.get(buy_date) != substate: continue
                    bp = kd[buy_date]['close']
                    sp = kd[trade_dates[i+period]]['close']
                    if bp <= 0: continue
                    ret = (sp - bp) / bp * 100
                    all_returns.append(ret)
            
            if not all_returns:
                substate_data[f'{period}d'] = {'trades': 0, 'avg': 0, 'win_rate': 0}
                continue
            
            n = len(all_returns)
            avg = np_mean(all_returns)
            wins = sum(1 for r in all_returns if r > 0)
            med = sorted(all_returns)[n//2]
            std = np_std(all_returns)
            
            substate_data[f'{period}d'] = {
                'trades': n, 'win_rate': round(wins/n*100, 2),
                'avg_return': round(avg, 2), 'median': round(med, 2),
                'std': round(std, 2), 'sharpe': round(avg/max(0.001, std), 2),
                'max': round(max(all_returns), 2), 'min': round(min(all_returns), 2)
            }
        result[substate] = substate_data
    
    # 打印
    print(f"\n{'='*100}")
    print(f" ② 混沌三子态 × 历史行情表现")
    print(f"{'='*100}")
    for substate in sub_states:
        print(f"\n  ▶ {substate}:")
        print(f"{'持有期':>8} {'样本数':>8} {'胜率':>7} {'均收益':>9} {'中位收益':>9} {'波动率':>8} {'夏普':>7}")
        print("-"*60)
        for period in ['5d','10d','20d','30d']:
            if period not in result[substate]: continue
            r = result[substate][period]
            if r['trades'] == 0:
                print(f"{period:>8} {'无数据':>8}")
                continue
            print(f"{period:>8} {r['trades']:>8} {r['win_rate']:>5.1f}% {r['avg_return']:>8.2f}% {r['median']:>8.2f}% {r['std']:>7.2f}% {r['sharpe']:>6.2f}")
    
    return result


# ════════════════════════════════════════════════
# ③ 因子归因 (基于trend_score截面)
# ════════════════════════════════════════════════

def backtest_factor_attribution(trend_scores, kline, holding_period=3):
    """
    用trend_score(5天×550只) × 未来N日收益 做因子IC分析
    由于评分数据只有5/25~5/29，最大可用持有期=3个交易日(5/25→5/28)
    """
    logger.info(f"③ 因子归因(持有期={holding_period}d)")
    
    factors = [
        ('L1_趋势(cycle_score)', 'trend_score'),
        ('L2_结构(structure_score)', 'momentum_score'),
        ('L3_情绪(emotion_score)', 'emotion_score'),
        ('综合(composite_score)', 'v_score')
    ]
    
    result = {}
    for factor_name, field in factors:
        all_vals = []
        all_rets = []
        for ts_code, score_list in trend_scores.items():
            if ts_code not in kline: continue
            kd = kline[ts_code]
            trade_dates = sorted(kd.keys())
            for s in score_list:
                buy_date = s['trade_date']
                try:
                    idx = trade_dates.index(buy_date)
                except ValueError:
                    continue
                if idx + holding_period >= len(trade_dates): continue
                bp = kd[buy_date]['close']
                sp = kd[trade_dates[idx+holding_period]]['close']
                if bp <= 0: continue
                ret = (sp - bp) / bp * 100
                val = s[field]
                all_vals.append(val)
                all_rets.append(ret)
        
        if len(all_vals) < 10:
            result[factor_name] = {'trades': len(all_vals), 'ic': 0, 'top_ret': 0, 'bot_ret': 0, 'spread': 0}
            continue
        
        ic, _ = spearman_rank(all_vals, all_rets)
        paired = sorted(zip(all_vals, all_rets), key=lambda x: x[0])
        n = len(paired)
        top_n = max(1, n // 3)
        top_ret = np_mean([p[1] for p in paired[-top_n:]])
        bot_ret = np_mean([p[1] for p in paired[:top_n]])
        
        result[factor_name] = {
            'trades': n,
            'ic': ic,
            'top_ret': round(top_ret, 2),
            'bot_ret': round(bot_ret, 2),
            'spread': round(top_ret - bot_ret, 2)
        }
    
    print(f"\n{'='*80}")
    print(f" ③ 因子归因 — {holding_period}日持有期IC分析")
    print(f"{'='*80}")
    print(f"{'因子':>25} {'样本':>8} {'IC':>10} {'Top30%':>10} {'Bot30%':>10} {'多空利差':>10}")
    print("-"*70)
    for name, r in result.items():
        if r.get('trades', 0) == 0:
            print(f"{name:>25} {'无数据':>8}")
            continue
        print(f"{name:>25} {r['trades']:>8} {r['ic']:>9.4f} {r['top_ret']:>9.2f}% {r['bot_ret']:>9.2f}% {r['spread']:>9.2f}%")
    
    return result


# ════════════════════════════════════════════════
# ④ 评分分档×持有期 (基于trend_score截面)
# ════════════════════════════════════════════════

def backtest_score_tiers(trend_scores, kline):
    """按V分四档×持有期看最优匹配 (评分数据仅5天，最大持有期=3)"""
    logger.info("④ 评分分档×持有期")
    
    tiers = [('≥50优', 50, 101), ('40-50良', 40, 50), ('30-40中', 30, 40), ('<30一般', 0, 30)]
    periods = [1, 2, 3, 4]  # 最大可用持有期
    
    result = {}
    for tier_name, lo, hi in tiers:
        tier_data = {}
        for period in periods:
            vals = []
            for ts_code, score_list in trend_scores.items():
                if ts_code not in kline: continue
                kd = kline[ts_code]
                trade_dates = sorted(kd.keys())
                for s in score_list:
                    v = s['v_score']
                    if v < lo or v >= hi: continue
                    buy_date = s['trade_date']
                    try:
                        idx = trade_dates.index(buy_date)
                    except ValueError:
                        continue
                    if idx + period >= len(trade_dates): continue
                    bp = kd[buy_date]['close']
                    sp = kd[trade_dates[idx+period]]['close']
                    if bp <= 0: continue
                    ret = (sp - bp) / bp * 100
                    vals.append(ret)
            
            if not vals:
                tier_data[f'{period}d'] = {'trades': 0, 'avg': 0}
                continue
            
            n = len(vals)
            avg = np_mean(vals)
            wins = sum(1 for v in vals if v > 0)
            med = sorted(vals)[n//2]
            std = np_std(vals)
            tier_data[f'{period}d'] = {
                'trades': n,
                'win_rate': round(wins/n*100, 2),
                'avg_return': round(avg, 2),
                'median': round(med, 2),
                'std': round(std, 2),
                'sharpe': round(avg/max(0.001, std), 2)
            }
        result[tier_name] = tier_data
    
    # 找各档最优
    print(f"\n{'='*100}")
    print(f" ④ 评分分档 × 持有期匹配 (trend_score截面, 注：仅4个交易日)")
    print(f"{'='*100}")
    for tier_name, tier_data in result.items():
        print(f"\n  ▶ {tier_name}:")
        best_p, best_r = '', None
        for period in ['1d','2d','3d','4d']:
            if period not in tier_data or tier_data[period]['trades'] == 0: continue
            r = tier_data[period]
            if best_r is None or r.get('sharpe', -999) > best_r.get('sharpe', -999):
                best_p, best_r = period, r
        
        print(f"{'持有期':>8} {'样本':>8} {'胜率':>7} {'均收益':>9} {'中位':>9} {'夏普':>7} {'最优?':>10}")
        print("-"*65)
        for period in ['1d','2d','3d','4d']:
            if period not in tier_data or tier_data[period]['trades'] == 0: continue
            r = tier_data[period]
            marker = '🏆' if period == best_p else ''
            print(f"{period:>8} {r['trades']:>8} {r['win_rate']:>5.1f}% {r['avg_return']:>8.2f}% {r['median']:>8.2f}% {r['sharpe']:>6.2f} {marker:>10}")
    
    return result


# ════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════

def main():
    t0 = time.time()
    logger.info("🚀 综合回测 v2.1 启动")
    
    # 1. 加载数据
    logger.info("📥 加载 season_state (460天)...")
    seasons = load_season_history()
    
    logger.info("📥 加载监控池...")
    pool = load_watch_pool()
    codes = list(pool.keys())
    logger.info(f"   → {len(codes)}只")
    
    logger.info("📥 加载K线...")
    kline = load_kline_bulk(codes, '2024-07-01', '2026-05-31')
    logger.info(f"   → {len(kline)}只")
    
    logger.info("📥 加载trend_score...")
    trend_scores = load_trend_scores()
    ts_cnt = sum(len(v) for v in trend_scores.values())
    logger.info(f"   → {len(trend_scores)}只/{ts_cnt}条")
    
    # 2. 四维回测
    r1 = backtest_survival_bias(seasons, kline)
    r2 = backtest_chaos_substates(seasons, kline)
    r3 = backtest_factor_attribution(trend_scores, kline)
    r4 = backtest_score_tiers(trend_scores, kline)
    
    # 3. 保存
    full = {
        'timestamp': datetime.now().isoformat(),
        'survival_bias': r1,
        'chaos_substates': {},
        'factor_attribution': r3,
        'score_tiers': {}
    }
    for s, pd in r2.items():
        full['chaos_substates'][s] = pd
    for tn, td in r4.items():
        for p, d in td.items():
            full['score_tiers'][f'{tn}_{p}'] = d
    
    out = '/tmp/backtest_comprehensive_v2_result.json'
    with open(out, 'w') as f:
        json.dump(full, f, ensure_ascii=False, indent=2, default=str)
    
    elapsed = time.time() - t0
    logger.info(f"\n🏁 综合回测完成! 耗时{elapsed:.0f}s")
    logger.info(f"📄 结果已保存: {out}")
    
    # 结论速览
    print(f"\n{'='*80}")
    print(f"📋 核心结论")
    print(f"{'='*80}")
    if r1:
        ns = r1['no_stop']; ws = r1['with_stop']
        print(f"  ① 生存偏差: 无止损均收益{ns['avg_return']}% / 止损均收益{ws['avg_return']}% (触发率{ws['stop_rate']}%)")
    for s in ['chaos', 'chaos_spring', 'chaos_autumn']:
        if s in r2:
            best_p, best_r = '', None
            for p in ['5d','10d','20d','30d']:
                if p in r2[s] and r2[s][p]['trades'] > 0:
                    if best_r is None or r2[s][p]['sharpe'] > best_r['sharpe']:
                        best_p, best_r = p, r2[s][p]
            if best_r:
                print(f"  ② {s} 最佳持有={best_p} 胜率{best_r['win_rate']}% 夏普{best_r['sharpe']}")
    if r3:
        best_f = max(r3, key=lambda k: r3[k].get('spread', -999))
        print(f"  ③ 最强因子: {best_f} (利差{r3[best_f]['spread']}%)")

if __name__ == '__main__':
    main()
