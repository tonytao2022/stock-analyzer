#!/usr/bin/env python3
"""
P6双轨引擎 V4 全量回测（资金因子修复版）
=========================================
先用优化过的评分函数批量预计算每日评分，再模拟逐日交易。
避免逐日逐票查库的低效。

参数同V3: 买入线75, 最多6只, 100万, P1门限60, 延判2天, 止损衰减
"""
import sys, os, json, time, math, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection

# ─── 参数 ───────────────────────────────────────────────────
BUY_THRESHOLD = 75
MAX_POSITIONS = 6
POOL_MONEY = 1_000_000
HOLD_LIMIT = 30
COOL_DAYS = 20
CHECKPOINTS = [5, 15, 25, 30]
P1_TH = 60       # P1 门限
P1_GRACE = 2     # 延判天数
P2_TH = 30
P3_TH = 20
SL_TIME_DECAY = [(5, 5), (7, 10), (8, 999)]
TS_PCT = 15
COMMISSION_PCT = 0.025
STAMP_TAX_PCT = 0.1
SLIPPAGE_PCT = 0.1

START_DATE = '2023-01-03'
END_DATE = '2026-06-09'

class Position:
    __slots__ = ('ts_code','buy_date','buy_price','score','hold_days',
                 'current_price','highest_price','exit_reason','sell_date')
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

def precompute_scores(backtest_codes, trade_dates):
    """预计算每日评分 - 批量从DB读取避免重复查询"""
    print("  预计算评分...")
    
    # 一次性读取所有需要的评分相关数据
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 1. 季节判定
    cur.execute("""
        SELECT trade_date, season, regime, confidence, scoring_strategy
        FROM season_state 
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (START_DATE, END_DATE))
    season_map = {}
    for r in cur.fetchall():
        season_map[str(r['trade_date'])] = {
            'market_season': r['season'],
            'market_regime': r['regime'],
            'market_confidence': float(r['confidence'] or 0.5),
            'market_scoring_strategy': r['scoring_strategy'],
            'trade_date': str(r['trade_date']),
        }
    
    # 构建评分函数需要的数据缓存
    # 2. 缠论结构
    cur.execute("""
        SELECT ts_code, trade_date, structure_score
        FROM chanlun_structure
        WHERE trade_date >= %s AND trade_date <= %s
    """, (START_DATE, END_DATE))
    chanlun_map = defaultdict(dict)
    for r in cur.fetchall():
        chanlun_map[r['ts_code']][str(r['trade_date'])] = {
            'structure_score': float(r['structure_score'] or 50),
        }
    
    # 3. 资金流向（修正后查 moneyflow 表）
    cur.execute("""
        SELECT ts_code, trade_date,
               COALESCE(net_mf_amount, 0) as net_mf,
               COALESCE(buy_lg_amount - sell_lg_amount, 0) as lg_net,
               COALESCE(buy_elg_amount - sell_elg_amount, 0) as elg_net
        FROM moneyflow
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY ts_code, trade_date
    """, (START_DATE, END_DATE))
    
    mf_map = defaultdict(list)
    rows = cur.fetchall()
    print(f"    已读取 {len(rows)} 条资金流向数据")
    for r in rows:
        mf_map[r['ts_code']].append({
            'trade_date': str(r['trade_date']),
            'net_mf': float(r['net_mf']),
            'lg_net': float(r['lg_net']),
            'elg_net': float(r['elg_net']),
        })
    
    # 4. 日K线（用于动量因子计算）- 取前120日窗口
    cur.execute("""
        SELECT ts_code, trade_date, close
        FROM daily_kline_qfq
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY ts_code, trade_date
    """, (START_DATE, END_DATE))
    
    kline_map = defaultdict(list)
    kline_rows = cur.fetchall()
    print(f"    已读取 {len(kline_rows)} 条K线数据")
    for r in kline_rows:
        kline_map[r['ts_code']].append({
            'trade_date': str(r['trade_date']),
            'close': float(r['close']),
        })
    
    cur.close(); conn.close()
    
    # 简化的评分函数
    def calc_score(ts_code, trade_date_str):
        """简化版评分：季节判定 + 缠论 + 资金因子"""
        season_info = season_map.get(trade_date_str)
        if not season_info:
            return None
        
        season = season_info['market_season']
        regime = season_info['market_regime']
        
        # 确定轨道
        is_momentum = season in ('summer', 'spring') or \
                      (season == 'chaos' and regime in ('bull', 'bullish'))
        
        # 缠论分
        cl_data = chanlun_map.get(ts_code, {}).get(trade_date_str, {})
        cl_score = cl_data.get('structure_score', 50)
        
        # 资金因子（5日累计）
        mf_list = mf_map.get(ts_code, [])
        mf_5d_total = 0
        lg_net_total = 0
        elg_net_total = 0
        for mf in mf_list:
            if mf['trade_date'] <= trade_date_str:
                mf_5d_total += mf['net_mf']
                lg_net_total += mf['lg_net']
                elg_net_total += mf['elg_net']
            else:
                break
        
        # 资金评分
        if mf_5d_total > 10000:
            mf_score = 85
        elif mf_5d_total > 5000:
            mf_score = 75
        elif mf_5d_total > 0:
            mf_score = 60
        elif mf_5d_total > -5000:
            mf_score = 40
        elif mf_5d_total > -10000:
            mf_score = 25
        else:
            mf_score = 15
        
        total_smart = lg_net_total + elg_net_total
        if total_smart > 5000:
            mf_score = min(100, mf_score + 15)
        elif total_smart > 0:
            mf_score = min(100, mf_score + 5)
        elif total_smart < -5000:
            mf_score = max(0, mf_score - 15)
        elif total_smart < 0:
            mf_score = max(0, mf_score - 5)
        
        # 动量/超跌简化计算
        kline_list = kline_map.get(ts_code, [])
        ret_5d = 0
        ret_20d = 0
        for i, k in enumerate(kline_list):
            if k['trade_date'] == trade_date_str:
                if i >= 4:
                    ret_5d = (k['close'] - kline_list[i-4]['close']) / kline_list[i-4]['close'] * 100
                if i >= 19:
                    ret_20d = (k['close'] - kline_list[i-19]['close']) / kline_list[i-19]['close'] * 100
                break
        
        if is_momentum:
            # 动量轨道: 缠论×0.50 + 动量×0.25 + 资金×0.25
            momentum_score = 50 + ret_5d * 1.5 + ret_20d * 0.8
            momentum_score = max(0, min(100, momentum_score))
            final = cl_score * 0.50 + momentum_score * 0.25 + mf_score * 0.25
        else:
            # 回归轨道: 缠论×0.40 + 超跌×0.25 + ATR(简化) + 资金×0.15
            oversold = 50 - ret_20d * 1.2  # 跌越多分越高
            oversold = max(0, min(100, oversold))
            final = cl_score * 0.40 + oversold * 0.25 + 50 * 0.10 + mf_score * 0.15
        
        return max(0, min(100, round(final, 1)))
    
    # 批量预计算
    all_scores = {}  # {trade_date: [(ts_code, score), ...]}
    for td in trade_dates:
        scores = []
        for code in backtest_codes:
            s = calc_score(code, td)
            if s is not None:
                scores.append((code, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        all_scores[td] = scores
    
    print(f"    预计算完成: {len(trade_dates)}天 × {len(backtest_codes)}只")
    return all_scores

def main():
    print("=" * 60)
    print(f"P6双轨 V4 回测（资金因子修复版）")
    print(f"参数: 买入线{BUY_THRESHOLD} 最多{MAX_POSITIONS}只 P1门限{P1_TH} 延判{P1_GRACE}天")
    print(f"周期: {START_DATE} ~ {END_DATE}")
    print("=" * 60)
    
    t0 = time.time()
    
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 获取回测池
    cur.execute("SELECT ts_code FROM backtest_pool WHERE `status`='ACTIVE' AND market!='指数'")
    backtest_codes = [r['ts_code'] for r in cur.fetchall()]
    
    # 交易日
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline_qfq 
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (START_DATE, END_DATE))
    trade_dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    
    print(f"📋 {len(backtest_codes)}只股票 × {len(trade_dates)}天")
    
    # 预计算评分
    t1 = time.time()
    pre_scores = precompute_scores(backtest_codes, trade_dates)
    print(f"⏱ 预计算耗时: {time.time()-t1:.0f}s")
    
    # 建立收盘价快速查询
    close_cache = {}
    for td in trade_dates:
        close_cache[td] = {}
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    for code in backtest_codes:
        cur.execute("""
            SELECT trade_date, close FROM daily_kline_qfq 
            WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
            ORDER BY trade_date
        """, (code, START_DATE, END_DATE))
        for r in cur.fetchall():
            close_cache[str(r['trade_date'])][code] = float(r['close'])
    cur.close(); conn.close()
    
    # ─── 回测主循环 ───
    positions = []
    cool_until = {}
    recorder = []
    cash = POOL_MONEY
    
    for idx, td in enumerate(trade_dates):
        pos_td = len(positions)
        cash_td = cash
        
        # 1. 检视持仓
        for pos in list(positions):
            pos.hold_days += 1
            close = close_cache[td].get(pos.ts_code)
            if close is None:
                continue
            pos.current_price = close
            
            # 止损
            for d, sl_pct in SL_TIME_DECAY:
                if pos.hold_days <= d:
                    stop_loss_price = pos.buy_price * (1 - sl_pct / 100)
                    if close <= stop_loss_price:
                        ret = (close - pos.buy_price) / pos.buy_price * 100
                        recorder.append({'ts_code': pos.ts_code, 'buy_date': pos.buy_date,
                            'sell_date': td, 'hold_days': pos.hold_days,
                            'return_pct': round(ret, 2), 'exit_reason': f'止损-{sl_pct}%'})
                        cash += close
                        positions.remove(pos)
                        cool_until[pos.ts_code] = td
                    break
            
            if pos not in positions:  # 已被移除
                continue
            
            # 移动止盈
            if close > pos.highest_price:
                pos.highest_price = close
            trailing_price = pos.highest_price * (1 - TS_PCT / 100)
            if close <= trailing_price and pos.hold_days >= 3:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code': pos.ts_code, 'buy_date': pos.buy_date,
                    'sell_date': td, 'hold_days': pos.hold_days,
                    'return_pct': round(ret, 2), 'exit_reason': f'移动止盈回撤{TS_PCT}%盈{ret:.1f}%'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # 强制平仓
            if pos.hold_days >= HOLD_LIMIT:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code': pos.ts_code, 'buy_date': pos.buy_date,
                    'sell_date': td, 'hold_days': pos.hold_days,
                    'return_pct': round(ret, 2), 'exit_reason': 'P4平仓(30日)'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
                continue
            
            # P1评分退坡（使用预计算评分）
            scores_td = pre_scores.get(td, [])
            score = None
            for code, s in scores_td:
                if code == pos.ts_code:
                    score = s
                    break
            if score is not None and score < P1_TH and pos.hold_days >= P1_GRACE:
                ret = (close - pos.buy_price) / pos.buy_price * 100
                recorder.append({'ts_code': pos.ts_code, 'buy_date': pos.buy_date,
                    'sell_date': td, 'hold_days': pos.hold_days,
                    'return_pct': round(ret, 2), 'exit_reason': f'P1评分退坡({score})'})
                cash += close
                positions.remove(pos)
                cool_until[pos.ts_code] = td
        
        # 2. 买入
        if len(positions) < MAX_POSITIONS:
            candidates = pre_scores.get(td, [])
            for code, score in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                if score < BUY_THRESHOLD:
                    continue
                if code in cool_until and cool_until[code] >= td:
                    continue
                if any(p.ts_code == code for p in positions):
                    continue
                
                close = close_cache[td].get(code)
                if close is None:
                    continue
                
                pos = Position(code, td, close, score)
                positions.append(pos)
                cash -= close
        
        if (idx+1) % 100 == 0:
            print(f"\r  [{idx+1}/{len(trade_dates)}] 持仓{len(positions)}只 现金{cash:,.0f} 交易{len(recorder)}笔", end='', flush=True)
    
    # 期末平仓
    for pos in positions:
        close = close_cache[trade_dates[-1]].get(pos.ts_code, pos.buy_price)
        ret = (close - pos.buy_price) / pos.buy_price * 100
        recorder.append({'ts_code': pos.ts_code, 'buy_date': pos.buy_date,
            'sell_date': trade_dates[-1], 'hold_days': pos.hold_days,
            'return_pct': round(ret, 2), 'exit_reason': '期末平仓'})
        cash += close
    
    print(f"\r  [{len(trade_dates)}/{len(trade_dates)}] 持仓0只 现金{cash:,.0f} 交易{len(recorder)}笔")
    
    # ─── 统计 ───
    trades = recorder
    win = [t for t in trades if t['return_pct'] > 0]
    lose = [t for t in trades if t['return_pct'] <= 0]
    
    total_return = (cash - POOL_MONEY) / POOL_MONEY * 100
    years = len(trade_dates) / 244
    
    # 持仓区间
    hold_stats = {}
    buckets = [(1,5,'1-5日'), (6,10,'6-10日'), (11,15,'11-15日'), 
               (16,20,'16-20日'), (21,30,'21-30日'), (31,60,'31-60日')]
    for lo, hi, key in buckets:
        items = [t for t in trades if lo <= t['hold_days'] <= hi]
        if items:
            hold_stats[key] = {
                'count': len(items),
                'avg_return': round(sum(t['return_pct'] for t in items)/len(items), 2),
                'win_rate': round(len([t for t in items if t['return_pct']>0])/len(items)*100, 1),
            }
    
    # 退出原因
    exit_stats = defaultdict(lambda: {'count': 0, 'sum_ret': 0})
    for t in trades:
        er = t['exit_reason']
        exit_stats[er]['count'] += 1
        exit_stats[er]['sum_ret'] += t['return_pct']
    exit_out = {k: {'count': v['count'], 'avg_return': round(v['sum_ret']/v['count'], 2)}
                for k, v in exit_stats.items()}
    
    avg_hd = sum(t['hold_days'] for t in trades)/len(trades) if trades else 0
    avg_win = sum(t['return_pct'] for t in win)/len(win) if win else 0
    avg_lose = sum(t['return_pct'] for t in lose)/len(lose) if lose else 0
    pf = abs(sum(t['return_pct'] for t in win)/sum(t['return_pct'] for t in lose)) if lose else 999
    
    returns = [t['return_pct'] for t in trades]
    avg_r = sum(returns)/len(returns) if returns else 0
    std_r = math.sqrt(sum((r-avg_r)**2 for r in returns)/len(returns)) if len(returns) > 1 else 1
    sharpe = (avg_r/std_r)*math.sqrt(244) if std_r > 0 else 0
    
    result = {
        'strategy': 'V4_资金因子修复版_买入线75_最多6只_100万_P1门限60_延判2天_止损衰减',
        'params': {
            'buy_threshold': BUY_THRESHOLD, 'max_positions': MAX_POSITIONS,
            'pool_money': POOL_MONEY, 'hold_limit': HOLD_LIMIT,
            'p1': P1_TH, 'p1_grace': P1_GRACE, 'ts_pct': TS_PCT,
            'sl_time_decay': SL_TIME_DECAY,
        },
        'period': f'{START_DATE}~{END_DATE}',
        'years': round(years, 2),
        'start_cap': POOL_MONEY, 'end_cap': round(cash, 2),
        'return_pct': round(total_return, 2),
        'annual_return_pct': round(total_return/years, 2) if years else 0,
        'trades': len(trades),
        'win_rate': round(len(win)/len(trades)*100, 2) if trades else 0,
        'avg_hd': round(avg_hd, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(avg_lose, 2),
        'profit_factor': round(pf, 2),
        'sharpe': round(sharpe, 2),
        'hold_stats': hold_stats,
        'exit_reasons': exit_out,
    }
    
    print(f"\n{'='*60}")
    print(f"📊 回测结果对比 V3 vs V4(资金因子修复)")
    print(f"{'='*60}")
    print(f"总收益率: {total_return:.2f}%")
    print(f"年化收益: {result['annual_return_pct']:.2f}%")
    print(f"胜率: {result['win_rate']}% (V3: 33.33%)")
    print(f"交易笔数: {len(trades)}笔 (V3: 36笔)")
    print(f"均盈: {avg_win:.2f}% | 均亏: {avg_lose:.2f}%")
    print(f"盈利因子: {pf:.2f}")
    print(f"夏普: {sharpe:.2f}")
    print(f"平均持仓: {avg_hd:.1f}天")
    print(f"\n持仓区间:")
    for k, v in sorted(hold_stats.items(), 
                        key=lambda x: int(x[0].split('日')[0].split('-')[0])):
        print(f"  {k}: {v['count']}笔 均收益{v['avg_return']}% 胜率{v['win_rate']}%")
    
    out_path = '/tmp/p6_backtest_v4_final.json'
    with open(out_path, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果写入: {out_path}")
    print(f"⏱ 总耗时: {time.time()-t0:.0f}s")
    return result

if __name__ == '__main__':
    main()
