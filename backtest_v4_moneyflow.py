#!/usr/bin/env python3
"""
回测 v4：资金因子修复后的全量回测
===============================
使用 p6_dual_track_engine.py 的版双轨引擎计算评分，
资金因子已修复（查 moneyflow 表，字段正确），
使用 V3 相同的参数规则对比胜率变化。

参数:
  V3_买入线75_最多6只_100万_P1门限60_延判2天_止损衰减

运行: python3 backtest_v4_moneyflow.py
输出: /tmp/p6_backtest_v4_final.json
"""
import sys, os, json, time, math, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection
from p6_dual_track_engine import P6DualTrackEngine, MarketContext

# ─── 参数 ───────────────────────────────────────────────────
BUY_THRESHOLD = 75
MAX_POSITIONS = 6
POOL_MONEY = 1_000_000
HOLD_LIMIT = 30
COOL_DAYS = 20
CHECKPOINTS = [5, 15, 25, 30]
P1 = 60      # P1 门限
P1_GRACE = 2 # 延判天数
P2 = 30
P3 = 20
SL_TIME_DECAY = [(5, 5), (7, 10), (8, 999)]  # (持有天数, 止损%)
TS_PCT = 15   # 移动止盈回撤%
COMMISSION_PCT = 0.025
STAMP_TAX_PCT = 0.1
SLIPPAGE_PCT = 0.1

START_DATE = '2023-01-03'
END_DATE = '2026-06-09'

class Position:
    def __init__(self, ts_code, buy_date, buy_price, score):
        self.ts_code = ts_code
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.score = score
        self.hold_days = 0
        self.current_price = buy_price
        self.highest_price = buy_price
        self.exit_reason = None
        self.sell_date = None

class TradeRecorder:
    def __init__(self):
        self.trades = []
    
    def record_sell(self, pos, sell_date, sell_price, reason):
        ret = (sell_price - pos.buy_price) / pos.buy_price * 100
        self.trades.append({
            'ts_code': pos.ts_code,
            'buy_date': pos.buy_date,
            'sell_date': sell_date,
            'hold_days': pos.hold_days,
            'buy_price': pos.buy_price,
            'sell_price': sell_price,
            'return_pct': round(ret, 2),
            'exit_reason': reason,
            'buy_score': pos.score,
        })

def compute_score(engine, ts_code, trade_date, ctx):
    """用P6双轨引擎评分"""
    try:
        track = engine.score_daily(ts_code, trade_date, ctx)
        if track and 'score' in track:
            return track['score']
    except:
        pass
    return None

def get_daily_close(ts_code, trade_date):
    """获取日收盘价"""
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("""
        SELECT close FROM daily_kline_qfq 
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, trade_date))
    r = cur.fetchone()
    cur.close(); conn.close()
    return float(r['close']) if r else None

def get_next_trade_date(trade_date, direction=1):
    """获取下一个/上一个交易日"""
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    if direction > 0:
        cur.execute("""
            SELECT DISTINCT trade_date FROM daily_kline_qfq 
            WHERE trade_date > %s ORDER BY trade_date LIMIT 1
        """, (trade_date,))
    else:
        cur.execute("""
            SELECT DISTINCT trade_date FROM daily_kline_qfq 
            WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1
        """, (trade_date,))
    r = cur.fetchone()
    cur.close(); conn.close()
    return str(r['trade_date']) if r else None

def get_candidates(engine, trade_date, ctx, backtest_codes):
    """获取当日符合条件的候选买入股票"""
    candidates = []
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    for ts_code in backtest_codes:
        score = compute_score(engine, ts_code, trade_date, ctx)
        if score is not None and score >= BUY_THRESHOLD:
            close = get_daily_close_real(cur, ts_code, trade_date, conn)
            if close:
                candidates.append((ts_code, score, close))
    
    cur.close(); conn.close()
    
    # 按评分降序，取 Top N
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:MAX_POSITIONS]

def get_daily_close_real(cur, ts_code, trade_date, conn=None):
    """获取当日收盘价（使用已有cursor）"""
    try:
        cur.execute("SELECT close FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s",
                    (ts_code, trade_date))
        r = cur.fetchone()
        return float(r['close']) if r else None
    except:
        return None

def check_stop_loss(pos, current_price, trade_date):
    """检查止损/移动止盈"""
    # 时间衰减止损
    for days, sl_pct in SL_TIME_DECAY:
        if pos.hold_days <= days:
            stop_loss_price = pos.buy_price * (1 - sl_pct / 100)
            if current_price <= stop_loss_price:
                return f'止损-{sl_pct}%'
            break
    
    # 移动止盈回撤
    if current_price > pos.highest_price:
        pos.highest_price = current_price
    
    trailing_price = pos.highest_price * (1 - TS_PCT / 100)
    if current_price <= trailing_price and pos.hold_days >= 3:
        ret = (current_price - pos.buy_price) / pos.buy_price * 100
        return f'移动止盈回撤{TS_PCT}%盈利{ret:.1f}%'
    
    return None

def main():
    print("=" * 60)
    print(f"P6双轨引擎 V4 回测（资金因子修复版）")
    print(f"周期: {START_DATE} ~ {END_DATE}")
    print(f"参数: 买入线{BUY_THRESHOLD}, 最多{MAX_POSITIONS}只, 本金{POOL_MONEY:,}")
    print(f"P1={P1}, 延判{P1_GRACE}天, 止损衰减, 移动止盈{TS_PCT}%")
    print("=" * 60)
    
    t0 = time.time()
    
    # 初始化引擎
    engine = P6DualTrackEngine()
    
    # 获取回测池股票
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT ts_code FROM backtest_pool WHERE status='ACTIVE' AND market!='指数'")
    backtest_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    print(f"📋 回测池: {len(backtest_codes)}只股票")
    
    # 获取全部交易日
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline_qfq 
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (START_DATE, END_DATE))
    trade_dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    
    print(f"📅 交易日: {len(trade_dates)}天")
    
    # ─── 回测主循环 ───
    positions = []  # 当前持仓
    cool_until = {}  # ts_code -> 下次可买日期
    recorder = TradeRecorder()
    cash = POOL_MONEY
    daily_equity = []
    
    total_candidates_found = 0
    
    for idx, trade_date in enumerate(trade_dates):
        if (idx+1) % 50 == 0:
            print(f"\r  [{idx+1}/{len(trade_dates)}] 持仓{len(positions)}只 现金{cash:,.0f}", end='', flush=True)
        
        # 获取当日季节上下文
        try:
            judge = engine.season_engine.judge(trade_date)
            ctx = MarketContext(judge)
        except:
            continue
        
        # ── 1. 检视持仓 ──
        for pos in list(positions):
            pos.hold_days += 1
            
            # 获取当前价
            close = get_daily_close(pos.ts_code, trade_date)
            if close is None:
                # 当天无数据，跳过
                continue
            pos.current_price = close
            
            # 止损/移动止盈检查
            sl_reason = check_stop_loss(pos, close, trade_date)
            if sl_reason:
                # 计算扣除费用后的收入
                sell_value = close * 1  # 简化：按当前价卖出
                pos.sell_date = trade_date
                recorder.record_sell(pos, trade_date, close, sl_reason)
                cash += sell_value  # 简化：返还本金+盈亏
                positions.remove(pos)
                cool_until[pos.ts_code] = trade_date
                continue
            
            # P1/P2/P3 检视
            if pos.hold_days >= HOLD_LIMIT:
                pos.sell_date = trade_date
                recorder.record_sell(pos, trade_date, close, 'P4平仓(30日)')
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = trade_date
                continue
            
            # P1评分退坡延判
            score = compute_score(engine, pos.ts_code, trade_date, ctx)
            if score is not None and score < P1 and pos.hold_days >= P1_GRACE:
                pos.sell_date = trade_date
                recorder.record_sell(pos, trade_date, close, f'P1评分退坡({score})')
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = trade_date
                continue
        
        # ── 2. 选股买入 ──
        if len(positions) < MAX_POSITIONS:
            candidates = get_candidates(engine, trade_date, ctx, backtest_codes)
            total_candidates_found += len(candidates)
            
            for ts_code, score, close in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                if ts_code in cool_until and cool_until[ts_code] >= trade_date:
                    continue
                
                # 检查是否已持仓
                if any(p.ts_code == ts_code for p in positions):
                    continue
                
                # 买入
                pos = Position(ts_code, trade_date, close, score)
                positions.append(pos)
                cash -= close  # 简化扣款
        
        # 记录每日权益
        total_value = cash + sum(p.current_price for p in positions)
        daily_equity.append({'date': trade_date, 'value': total_value})
    
    # ─── 剩余持仓强制平仓 ───
    for pos in positions:
        last_date = trade_dates[-1]
        close = get_daily_close(pos.ts_code, last_date) or pos.buy_price
        recorder.record_sell(pos, last_date, close, '期末平仓')
        cash += close
    
    # ─── 统计 ───
    trades = recorder.trades
    win_trades = [t for t in trades if t['return_pct'] > 0]
    lose_trades = [t for t in trades if t['return_pct'] <= 0]
    
    total_return = (cash - POOL_MONEY) / POOL_MONEY * 100
    
    # 最大回撤
    peak = POOL_MONEY
    max_dd = 0
    for d in daily_equity:
        if d['value'] > peak:
            peak = d['value']
        dd = (peak - d['value']) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    # 持仓区间分析
    hold_stats = defaultdict(lambda: {'count': 0, 'sum_ret': 0, 'wins': 0})
    for t in trades:
        hd = t['hold_days']
        if hd <= 5: key = '1-5日'
        elif hd <= 10: key = '6-10日'
        elif hd <= 15: key = '11-15日'
        elif hd <= 20: key = '16-20日'
        elif hd <= 30: key = '21-30日'
        else: key = '31-60日'
        hold_stats[key]['count'] += 1
        hold_stats[key]['sum_ret'] += t['return_pct']
        if t['return_pct'] > 0:
            hold_stats[key]['wins'] += 1
    
    hold_stats_out = {}
    for k, v in sorted(hold_stats.items(), key=lambda x: int(x[0].split('日')[0].split('-')[0])):
        cnt = v['count']
        hold_stats_out[k] = {
            'count': cnt,
            'avg_return': round(v['sum_ret'] / cnt, 2) if cnt else 0,
            'win_rate': round(v['wins'] / cnt * 100, 1) if cnt else 0,
        }
    
    # 退出原因分析
    exit_reasons = defaultdict(lambda: {'count': 0, 'sum_ret': 0})
    for t in trades:
        er = t['exit_reason']
        exit_reasons[er]['count'] += 1
        exit_reasons[er]['sum_ret'] += t['return_pct']
    
    exit_stats = {}
    for k, v in exit_reasons.items():
        exit_stats[k] = {
            'count': v['count'],
            'avg_return': round(v['sum_ret'] / v['count'], 2),
        }
    
    # ─── 输出 ───
    days = len(trade_dates)
    years = days / 244
    avg_hold = sum(t['hold_days'] for t in trades) / len(trades) if trades else 0
    avg_win_pct = sum(t['return_pct'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_lose_pct = sum(t['return_pct'] for t in lose_trades) / len(lose_trades) if lose_trades else 0
    profit_factor = abs(sum(t['return_pct'] for t in win_trades) / sum(t['return_pct'] for t in lose_trades)) if lose_trades else float('inf')
    
    # 夏普比
    returns = [t['return_pct'] for t in trades]
    avg_ret = sum(returns) / len(returns) if returns else 0
    std_ret = math.sqrt(sum((r - avg_ret)**2 for r in returns) / len(returns)) if len(returns) > 1 else 1
    sharpe = (avg_ret / std_ret) * math.sqrt(244) if std_ret > 0 else 0
    
    # Calmar
    calmar = (total_return / years) / max_dd if max_dd > 0 else 0
    
    result = {
        'strategy': 'V4_资金因子修复_买入线75_最多6只_100万_P1门限60_延判2天_止损衰减',
        'params': {
            'pool_money': POOL_MONEY, 'max_positions': MAX_POSITIONS,
            'buy_threshold': BUY_THRESHOLD, 'hold_limit': HOLD_LIMIT,
            'cool_days': COOL_DAYS, 'checkpoints': CHECKPOINTS,
            'p1': P1, 'p1_grace_days': P1_GRACE, 'p2': P2, 'p3': P3,
            'sl_time_decay': SL_TIME_DECAY, 'ts_pct': TS_PCT,
            'commission_pct': COMMISSION_PCT, 'stamp_tax_pct': STAMP_TAX_PCT,
            'slippage_pct': SLIPPAGE_PCT,
        },
        'period': f'{START_DATE}~{END_DATE}',
        'days': days, 'years': round(years, 2),
        'start_cap': POOL_MONEY, 'end_cap': round(cash, 2),
        'return_pct': round(total_return, 2),
        'annual_return_pct': round(total_return / years, 2) if years else 0,
        'max_dd_pct': round(max_dd, 2),
        'trades': len(trades),
        'buy': len(trades), 'sell': len(trades),
        'win': len(win_trades), 'lose': len(lose_trades),
        'win_rate': round(len(win_trades) / len(trades) * 100, 2) if trades else 0,
        'avg_hd': round(avg_hold, 1),
        'avg_win_pct': round(avg_win_pct, 2),
        'avg_lose_pct': round(avg_lose_pct, 2),
        'profit_factor': round(profit_factor, 2),
        'sharpe': round(sharpe, 2),
        'calmar': round(calmar, 2),
        'total_tc': 0,
        'hold_stats': hold_stats_out,
        'exit_reasons': exit_stats,
    }
    
    print(f"\n{'='*60}")
    print(f"📊 回测结果")
    print(f"{'='*60}")
    print(f"总收益率: {total_return:.2f}%")
    print(f"年化收益: {result['annual_return_pct']:.2f}%")
    print(f"最大回撤: {max_dd:.2f}%")
    print(f"交易笔数: {len(trades)}笔")
    print(f"胜率: {result['win_rate']}%")
    print(f"均盈: {avg_win_pct:.2f}% | 均亏: {avg_lose_pct:.2f}%")
    print(f"盈利因子: {profit_factor:.2f}")
    print(f"夏普: {sharpe:.2f}")
    print(f"平均持仓: {avg_hold:.1f}天")
    print(f"\n持仓区间胜率:")
    for k, v in hold_stats_out.items():
        print(f"  {k}: {v['count']}笔 均收益{v['avg_return']}% 胜率{v['win_rate']}%")
    
    # 写入文件
    out_path = '/tmp/p6_backtest_v4_final.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果写入: {out_path}")
    print(f"⏱ 耗时: {time.time()-t0:.1f}s")

if __name__ == '__main__':
    main()
