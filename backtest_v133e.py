#!/usr/bin/env python3
"""
V13.3e 回测脚本 v1.0
======================
两种模式：
1. fast 模式（默认）— 直接从 strategy_signal.composite_score 读取历史评分，快速回测
2. engine 模式 — 调用引擎 p6.score_stock() 逐日逐股重新评分（慢但精准）

用法:
    python3 backtest_v133e.py --mode fast --start 2025-01-01 --end 2026-07-21
                             --pool watch_pool --buy-line 60 --max-hold 60
    
    python3 backtest_v133e.py --mode engine --engine /opt/stock-analyzer/p6_dual_track_engine.py
                             --start 2025-06-01 --end 2025-07-01 (局部验证用)
"""
import os, sys, time, csv, json, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import List, Dict, Optional

ENGINE_PATH = '/opt/stock-analyzer/p6_dual_track_engine.py'
COST_RATE = 0.003  # 交易成本: 万3佣金+千1印花税


# ─── 数据库工具 ──────────────────────────────────────────────
def get_db():
    pwd = os.environ.get('MYSQL_PASS', '')
    db_user = os.environ.get('DB_USER', 'debian-sys-maint')
    return pymysql.connect(host='127.0.0.1', user=db_user, password=pwd,
                          database='stock_db_v2', cursorclass=pymysql.cursors.DictCursor)


# ─── FAST模式加实盘规则：真实资金、T+1、仓位限制 ──────────────
def backtest_fast(start_date: str, end_date: str, pool: str, buy_line: int,
                  max_hold_days: int, stop_loss_pct: float):
    """
    实盘级回测，规则：
    - 起始资金 100 万
    - 单只股票首次买入上限 10 万（约1手，按实际价格调整）
    - 最多持有 10 只股票
    - T+1：买入次日才能卖出
    - 每日最多买入 5 只
    - 每笔交易扣除成本（佣金+印花税）
    """
    INITIAL_CAPITAL = 1_000_000
    MAX_POSITIONS = 10
    MAX_DAILY_BUYS = 5
    POSITION_SIZE = 100_000  # 单票10万
    
    print(f"\n{'='*60}")
    print(f"  V13.3e 实盘回测")
    print(f"  起始资金: {INITIAL_CAPITAL/10000:.0f}万")
    print(f"  日期: {start_date} ~ {end_date}")
    print(f"  买入线: {buy_line} | 最大持有: {max_hold_days}天 | 止损: {stop_loss_pct}%")
    print(f"  单票10万 | 最多{MAX_POSITIONS}只 | T+1 | 日买最多{MAX_DAILY_BUYS}只")
    print(f"{'='*60}")
    
    conn = get_db()
    cur = conn.cursor()
    
    # 1. 交易日历
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline_qfq
        WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date
    """, (start_date, end_date))
    trade_dates = [str(r['trade_date']) for r in cur.fetchall()]
    print(f"  📅 交易日: {len(trade_dates)}天 ({trade_dates[0]}~{trade_dates[-1]})")
    
    # 2. 股票池
    if pool == 'watch_pool':
        cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    else:
        cur.execute("SELECT DISTINCT ts_code FROM stock_basic WHERE 1=1")
    ts_codes = [r['ts_code'] for r in cur.fetchall()]
    print(f"  📋 股票池: {len(ts_codes)}只")
    
    # 3. 评分 + K线
    cur.execute("""
        SELECT s.ts_code, s.trade_date, s.composite_score, s.season,
               k.close AS price
        FROM strategy_signal s
        JOIN daily_kline_qfq k ON s.ts_code=k.ts_code AND s.trade_date=k.trade_date
        WHERE s.trade_date >= %s AND s.trade_date <= %s
    """, (start_date, end_date))
    rows = cur.fetchall()
    print(f"  📊 评分数据: {len(rows)}条")
    cur.close(); conn.close()
    
    scores_by_stock = defaultdict(dict)
    for r in rows:
        code = r['ts_code']
        d = str(r['trade_date'])
        scores_by_stock[code][d] = {
            'score': float(r['composite_score']) if r['composite_score'] is not None else 0,
            'season': r.get('season', 'unknown'),
            'close': float(r.get('price', 0)),
        }
    
    # ═══ 实盘交易引擎 ═══
    cash = INITIAL_CAPITAL  # 可用现金
    capital_peak = INITIAL_CAPITAL
    positions = {}  # {code: {'entry_date', 'entry_price', 'entry_idx', 'shares', 'amount'}}
    trades = []
    total_dates = len(trade_dates)
    
    def calc_net_profit(entry_price, exit_price, shares):
        buy_cost = entry_price * shares * COST_RATE
        sell_cost = exit_price * shares * COST_RATE
        gross_pnl = (exit_price - entry_price) * shares
        net_pnl = gross_pnl - buy_cost - sell_cost
        return net_pnl, gross_pnl / (entry_price * shares) * 100
    
    for di, t_date in enumerate(trade_dates):
        if di % 100 == 0:
            total_assets = cash + sum(p['shares'] * p['entry_price'] for p in positions.values())
            print(f"  进度: {di}/{total_dates} ({t_date}) 资产:{total_assets:,.0f}")
        
        # 当日所有评分
        day_scores = {}
        for code in ts_codes:
            if code in scores_by_stock and t_date in scores_by_stock[code]:
                day_scores[code] = scores_by_stock[code][t_date]
        
        # ─── 卖出检查 ───
        for code, pos in list(positions.items()):
            if code not in day_scores:
                continue
            price = day_scores[code]['close']
            if price == 0:
                continue
            hold_days = di - pos['entry_idx']
            # T+1：至少持有一天
            if hold_days < 1:
                continue
            profit_pct = (price - pos['entry_price']) / pos['entry_price'] * 100
            
            sell = False
            reason = ''
            if profit_pct <= -stop_loss_pct:
                sell, reason = True, 'STOP_LOSS'
            elif hold_days >= max_hold_days:
                sell, reason = True, 'MAX_HOLD'
            elif hold_days >= 5 and day_scores[code]['score'] < (buy_line - 20):
                sell, reason = True, 'SCORE_DROP'
            
            if sell:
                net_pnl, pnl_pct = calc_net_profit(pos['entry_price'], price, pos['shares'])
                cash += price * pos['shares']
                trades.append({
                    'ts_code': code,
                    'entry_date': pos['entry_date'],
                    'exit_date': t_date,
                    'hold_days': hold_days,
                    'entry_price': round(pos['entry_price'], 2),
                    'exit_price': round(price, 2),
                    'shares': pos['shares'],
                    'net_pnl': round(net_pnl),
                    'profit_pct': round(pnl_pct, 2),
                    'entry_score': pos['entry_score'],
                    'exit_reason': reason,
                    'entry_season': pos.get('season', ''),
                    'exit_season': day_scores[code].get('season', ''),
                })
                del positions[code]
        
        # ─── 买入检查 ───
        available_slots = MAX_POSITIONS - len(positions)
        if available_slots > 0 and cash > POSITION_SIZE:
            sorted_codes = sorted(day_scores.items(), key=lambda x: x[1]['score'], reverse=True)
            daily_buys = 0
            for code, info in sorted_codes:
                if daily_buys >= MAX_DAILY_BUYS or len(positions) >= MAX_POSITIONS:
                    break
                if code in positions:
                    continue
                if info['score'] < buy_line:
                    continue
                price = info['close']
                if price <= 0 or cash < price * 100:
                    continue
                # 买入 10 万市值（约100股整数倍）
                target_shares = int(POSITION_SIZE / price / 100) * 100
                if target_shares < 100:
                    target_shares = int(cash / price / 100) * 100
                if target_shares < 100:
                    continue
                cost = target_shares * price * (1 + COST_RATE)
                if cost > cash:
                    target_shares = int(cash / (price * (1 + COST_RATE)) / 100) * 100
                    if target_shares < 100:
                        continue
                    cost = target_shares * price * (1 + COST_RATE)
                
                cash -= cost
                positions[code] = {
                    'entry_date': t_date,
                    'entry_price': price,
                    'entry_idx': di,
                    'shares': target_shares,
                    'amount': cost,
                    'entry_score': info['score'],
                    'season': info.get('season', ''),
                }
                daily_buys += 1
    
    # 期末清算
    for code, pos in list(positions.items()):
        if code not in scores_by_stock:
            continue
        last_dates = [d for d in scores_by_stock[code].keys()]
        if not last_dates:
            continue
        last_date = max(last_dates)
        info = scores_by_stock[code][last_date]
        price = info['close']
        if price > 0:
            net_pnl, pnl_pct = calc_net_profit(pos['entry_price'], price, pos['shares'])
            cash += price * pos['shares']
            trades.append({
                'ts_code': code,
                'entry_date': pos['entry_date'],
                'exit_date': last_date,
                'hold_days': len(trade_dates) - pos['entry_idx'],
                'entry_price': round(pos['entry_price'], 2),
                'exit_price': round(price, 2),
                'shares': pos['shares'],
                'net_pnl': round(net_pnl),
                'profit_pct': round(pnl_pct, 2),
                'entry_score': pos['entry_score'],
                'exit_reason': 'FORCE_CLOSE',
                'entry_season': pos.get('season', ''),
                'exit_season': info.get('season', ''),
            })
    
    final_assets = cash
    total_pnl = final_assets - INITIAL_CAPITAL
    total_return = total_pnl / INITIAL_CAPITAL * 100
    
    print(f"\n{'='*60}")
    print(f"  📊 实盘回测结果")
    print(f"{'='*60}")
    
    if not trades:
        print(f"\n  ❌ 零笔交易！最终资产: {final_assets:,.0f}")
        return trades
    
    win_trades = [t for t in trades if t['net_pnl'] > 0]
    loss_trades = [t for t in trades if t['net_pnl'] <= 0]
    total_net_pnl = sum(t['net_pnl'] for t in trades)
    win_rate = len(win_trades) / len(trades) * 100
    
    print(f"\n  起始资产: {INITIAL_CAPITAL:,.0f}")
    print(f"  最终资产: {final_assets:,.0f}")
    print(f"  **总收益: {total_pnl:+,.0f} ({total_return:+.2f}%)")
    print(f"  总交易笔数: {len(trades)}")
    print(f"  胜率: {win_rate:.1f}% ({len(win_trades)}胜/{len(loss_trades)}负)")
    
    if win_trades:
        print(f"  盈利总额: {sum(t['net_pnl'] for t in win_trades):+,.0f}")
    if loss_trades:
        print(f"  亏损总额: {sum(t['net_pnl'] for t in loss_trades):+,.0f}")
    
    # 最大回撤估算
    peak = INITIAL_CAPITAL
    max_drawdown = 0
    running_capital = INITIAL_CAPITAL
    for t in trades:
        running_capital += t['net_pnl']
        if running_capital > peak:
            peak = running_capital
        dd = (peak - running_capital) / peak * 100
        if dd > max_drawdown:
            max_drawdown = dd
    print(f"  最大回撤: {max_drawdown:.2f}%")
    
    # 按持有期
    print(f"\n{'─'*40}")
    print(f"  按持有期分组")
    print(f"{'─'*40}")
    for days in [5, 10, 20, 30, 45, 60]:
        group = [t for t in trades if t['hold_days'] <= days]
        if not group:
            continue
        g_wins = [t for t in group if t['net_pnl'] > 0]
        g_pnl = sum(t['net_pnl'] for t in group)
        print(f"  ≤{days}日: {len(group)}笔 | 胜率{len(g_wins)/len(group)*100:.1f}% | 收益{g_pnl:+,.0f}")
    
    # 按季节
    print(f"\n{'─'*40}")
    print(f"  按退出季节分组")
    print(f"{'─'*40}")
    season_data = defaultdict(lambda: {'trades': [], 'pnl': 0, 'wins': 0})
    for t in trades:
        s = t.get('exit_season', 'unknown')
        season_data[s]['trades'].append(t)
        season_data[s]['pnl'] += t['net_pnl']
        if t['net_pnl'] > 0:
            season_data[s]['wins'] += 1
    for s, d in sorted(season_data.items(), key=lambda x: -len(x[1]['trades'])):
        cnt = len(d['trades'])
        print(f"  {s}: {cnt}笔 | 胜率{d['wins']/cnt*100:.1f}% | 收益{d['pnl']:+,.0f}")
    
    # 按退出原因
    print(f"\n{'─'*40}")
    print(f"  按退出原因分组")
    print(f"{'─'*40}")
    reason_data = defaultdict(lambda: {'trades': [], 'pnl': 0, 'wins': 0})
    for t in trades:
        r = t['exit_reason']
        reason_data[r]['trades'].append(t)
        reason_data[r]['pnl'] += t['net_pnl']
        if t['net_pnl'] > 0:
            reason_data[r]['wins'] += 1
    for r, d in sorted(reason_data.items(), key=lambda x: -len(x[1]['trades'])):
        cnt = len(d['trades'])
        print(f"  {r}: {cnt}笔 | 胜率{d['wins']/cnt*100:.1f}% | 收益{d['pnl']:+,.0f}")
    
    csv_path = f"backtest_v133e_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    print(f"\n  💾 详细报告: {csv_path}")
    
    return trades


# ─── 输出统计 ──────────────────────────────────────────────
def print_result(trades: List[Dict]):
    print(f"\n{'='*60}")
    print(f"  📊 回测结果")
    print(f"{'='*60}")
    
    if not trades:
        print("\n  ❌ 零笔交易！")
        return
    
    win_trades = [t for t in trades if t['profit_pct'] > 0]
    loss_trades = [t for t in trades if t['profit_pct'] <= 0]
    total_pnl = sum(t['profit_pct'] for t in trades)
    win_rate = len(win_trades) / len(trades) * 100
    
    avg_win = sum(t['profit_pct'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_loss = sum(t['profit_pct'] for t in loss_trades) / len(loss_trades) if loss_trades else 0
    
    print(f"\n  总交易笔数: {len(trades)}")
    print(f"  总收益: {total_pnl:.2f}%")
    print(f"  胜率: {win_rate:.1f}% ({len(win_trades)}胜/{len(loss_trades)}负)")
    print(f"  平均盈利: +{avg_win:.2f}%")
    print(f"  平均亏损: {avg_loss:.2f}%")
    print(f"  盈亏比: {abs(avg_win/avg_loss) if avg_loss != 0 else 0:.2f}")
    
    # 按持有期
    print(f"\n{'─'*40}")
    print(f"  按持有期分组")
    print(f"{'─'*40}")
    for days in [5, 10, 20, 30, 60]:
        group = [t for t in trades if t['hold_days'] <= days]
        if not group:
            continue
        g_wins = [t for t in group if t['profit_pct'] > 0]
        g_pnl = sum(t['profit_pct'] for t in group)
        print(f"  ≤{days}日: {len(group)}笔 | 胜率{len(g_wins)/len(group)*100:.1f}% | 总收益{g_pnl:+.2f}%")
    
    # 按季节
    print(f"\n{'─'*40}")
    print(f"  按退出季节分组")
    print(f"{'─'*40}")
    season_data = defaultdict(lambda: {'trades': [], 'wins': 0})
    for t in trades:
        s = t.get('exit_season', 'unknown')
        season_data[s]['trades'].append(t)
        if t['profit_pct'] > 0:
            season_data[s]['wins'] += 1
    for s, d in sorted(season_data.items(), key=lambda x: -len(x[1]['trades'])):
        cnt = len(d['trades'])
        wr = d['wins']/cnt*100
        pnl = sum(t['profit_pct'] for t in d['trades'])
        print(f"  {s}: {cnt}笔 | 胜率{wr:.1f}% | 收益{pnl:+.2f}%")
    
    # 按退出原因
    print(f"\n{'─'*40}")
    print(f"  按退出原因分组")
    print(f"{'─'*40}")
    reason_data = defaultdict(lambda: {'trades': [], 'wins': 0})
    for t in trades:
        r = t['exit_reason']
        reason_data[r]['trades'].append(t)
        if t['profit_pct'] > 0:
            reason_data[r]['wins'] += 1
    for r, d in sorted(reason_data.items(), key=lambda x: -len(x[1]['trades'])):
        cnt = len(d['trades'])
        wr = d['wins']/cnt*100
        pnl = sum(t['profit_pct'] for t in d['trades'])
        print(f"  {r}: {cnt}笔 | 胜率{wr:.1f}% | 收益{pnl:+.2f}%")
    
    # 保存CSV
    csv_path = f"backtest_v133e_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=trades[0].keys())
        writer.writeheader()
        writer.writerows(trades)
    print(f"\n  💾 详细报告: {csv_path}")


# ─── 入口 ─────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='V13.3e 回测脚本')
    parser.add_argument('--mode', default='fast', choices=['fast', 'engine'], help='回测模式')
    parser.add_argument('--engine', default=ENGINE_PATH, help='引擎文件路径(engine模式)')
    parser.add_argument('--start', default='2025-06-01', help='开始日期')
    parser.add_argument('--end', default='2026-07-21', help='结束日期')
    parser.add_argument('--pool', default='watch_pool', choices=['watch_pool', 'full_market'], help='股票池')
    parser.add_argument('--buy-line', type=int, default=60, help='买入线')
    parser.add_argument('--max-hold', type=int, default=60, help='最大持有天数')
    parser.add_argument('--stop-loss', type=float, default=12.0, help='止损百分比')
    args = parser.parse_args()
    
    if args.mode == 'engine':
        # 引擎模式: 加载生产引擎，逐日逐股重新评分
        from importlib import util as imp_util
        spec = imp_util.spec_from_file_location("p6_dual_track_engine", args.engine)
        p6 = imp_util.module_from_spec(spec)
        spec.loader.exec_module(p6)
        # NOTE: 引擎模式需额外实现，当前用fast模式
        print("⚠️ 引擎模式暂未实现完整，先用fast模式")
        backtest_fast(args.start, args.end, args.pool, args.buy_line,
                     args.max_hold, args.stop_loss)
    else:
        backtest_fast(args.start, args.end, args.pool, args.buy_line,
                     args.max_hold, args.stop_loss)
