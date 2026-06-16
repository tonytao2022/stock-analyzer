#!/usr/bin/env python3
"""
V11快速回测 — 直接从数据库中读取评分/K线/季节数据
避免重复调评分引擎，适合全量A股回测
用时预估: 30秒~2分钟（5514只×833天）
"""
import sys, os, time, pymysql
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = 'iXve1rVBXfdA4tL9'

from db_config import get_connection

START = date(2023, 1, 1)

def main():
    t0 = time.time()
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 1. 加载季节数据（全量）
    print("加载季节数据...", flush=True)
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    season_map = {}
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        if td >= START: season_map[td] = r['season']
    dates = sorted(season_map.keys())
    print(f"  季节: {len(season_map)}天, {len(dates)}个交易日", flush=True)

    # 2. 加载评分数据（strategy_signal — 只覆盖最近5天）
    print("加载评分数据...", flush=True)
    cur.execute("SELECT ts_code, trade_date, calibrated_score, score FROM strategy_signal ORDER BY trade_date")
    score_by_code = defaultdict(dict)
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        sc = r['calibrated_score'] if r['calibrated_score'] and r['calibrated_score'] > 0 else r.get('score', 0)
        if sc and sc > 0:
            score_by_code[r['ts_code']][td] = float(sc)
    print(f"  评分: {len(score_by_code)}只有评分数据", flush=True)

    # 3. 加载K线（只读close）
    print("加载K线数据...", flush=True)
    cur.execute("SELECT ts_code, trade_date, close FROM daily_kline_qfq ORDER BY ts_code, trade_date")
    kline = defaultdict(list)
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        kline[r['ts_code']].append((td, float(r['close'])))
    print(f"  K线: {len(kline)}只股票", flush=True)

    # 4. 加载监控池
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    watch_codes = set(r['ts_code'] for r in cur.fetchall())
    print(f"  监控池: {len(watch_codes)}只", flush=True)
    
    cur.close()
    conn.close()
    
    # 5. 评分插值函数（用最近的评分代替当天评分）
    def get_score(code, tgt_date):
        scores = score_by_code.get(code, {})
        if not scores:
            return None
        # 找最近的有效评分
        best_d, best_s = None, 0
        for d, s in scores.items():
            if d <= tgt_date and (best_d is None or d > best_d):
                best_d, best_s = d, s
        return best_s if best_s > 0 else None

    # 6. 价格查询（二分加速）
    import bisect
    price_cache = {}
    for code, rows in kline.items():
        dates_list = [r[0] for r in rows]
        prices_list = [r[1] for r in rows]
        price_cache[code] = (dates_list, prices_list)

    def get_price(code, tgt):
        if code not in price_cache:
            return None
        d_list, p_list = price_cache[code]
        idx = bisect.bisect_right(d_list, tgt) - 1
        if idx < 0:
            return None
        return p_list[idx]

    # 7. V11参数矩阵
    THRESHOLDS = {
        'summer': {'buy': 72, 'stop': -0.12, 'hold': 60, 'stop2': -0.09, 'p4_min': 55, 'p4_extend': 15, 'trailing': 0.18},
        'spring': {'buy': 70, 'stop': -0.08, 'hold': 20, 'stop2': -0.06, 'p4_min': 60, 'p4_extend': 5, 'trailing': 0.12, 't2_enable': True},
        'chaos_spring': {'buy': 75, 'stop': -0.10, 'hold': 25, 'stop2': -0.08, 'p4_min': 65, 'p4_extend': 5, 'trailing': 0.12, 't2_enable': False},
        'chaos': {'buy': 75, 'stop': -0.10, 'hold': 25, 'stop2': -0.08, 'p4_min': 65, 'p4_extend': 5, 'trailing': 0.12, 't2_enable': False},
        'chaos_autumn': {'buy': 75, 'stop': -0.10, 'hold': 25, 'stop2': -0.08, 'p4_min': 65, 'p4_extend': 5, 'trailing': 0.12, 't2_enable': False},
        'autumn': {'buy': 75, 'stop': -0.08, 'hold': 25, 'stop2': -0.06, 'p4_min': 65, 'p4_extend': 5, 'trailing': 0.12, 't2_enable': True},
        'winter': {'buy': 85, 'stop': -0.05, 'hold': 10, 'stop2': -0.04, 'p4_min': 999, 'trailing': 0.08, 't2_enable': False},
        'panic': {'buy': 999},
        'recovery': {'buy': 999},
    }

    TICKERS = ['summer', 'spring', 'chaos_spring', 'chaos', 'chaos_autumn', 'autumn', 'winter']

    # 8. 运行回测
    print("运行回测...", flush=True)
    all_trades = []

    # 处理所有有评分的股票 + 监控池股票
    all_codes = set(score_by_code.keys()) | watch_codes
    print(f"  回测标的: {len(all_codes)}只 ({len(score_by_code)}只有评分, {len(watch_codes)}只监控池)", flush=True)

    # 按股票逐个回测
    processed = 0
    for code in all_codes:
        processed += 1
        if processed % 500 == 0:
            print(f"  进度: {processed}/{len(all_codes)}", flush=True)

        if code not in price_cache:
            continue

        pos = None  # {'buy_date', 'buy_price', 'peak', 'season'}
        trades = []

        for today in dates:
            sea = season_map.get(today, 'chaos')
            cfg = THRESHOLDS[sea]
            
            if cfg['buy'] >= 999:
                pos = None
                continue

            price = get_price(code, today)
            if price is None:
                continue

            # --- 卖出检查 ---
            if pos is not None:
                ret = (price - pos['buy_price']) / pos['buy_price']
                if price > pos.get('peak', pos['buy_price']):
                    pos['peak'] = price
                hold_days = (today - pos['buy_date']).days

                sell_reason = None
                # T1: 回撤止损
                if ret <= cfg['stop']:
                    sell_reason = 'T1_stop'
                # T2: 移动回撤止损
                elif cfg.get('t2_enable', False) and pos['peak'] > pos['buy_price']:
                    dd_from_peak = (price - pos['peak']) / pos['peak']
                    if dd_from_peak <= cfg.get('trailing', -0.12):
                        sell_reason = 'T2_trailing'
                # 持有到期平仓
                elif hold_days >= cfg['hold']:
                    # P4: 高分延长期
                    sc = get_score(code, today)
                    p4_ext = cfg.get('p4_extend', 0)
                    p4_min = cfg.get('p4_min', 999)
                    if p4_ext > 0 and sc is not None and sc >= p4_min and hold_days < cfg['hold'] + p4_ext:
                        pass  # 延长
                    else:
                        sell_reason = 'hold_expire'

                if sell_reason:
                    trades.append({
                        'code': code, 'buy_date': str(pos['buy_date']),
                        'sell_date': str(today), 'hold_days': hold_days,
                        'ret': ret, 'score': pos.get('score', 0),
                        'season': pos['season'], 'sell_reason': sell_reason
                    })
                    pos = None

            # --- 买入检查 ---
            if pos is None:
                sc = get_score(code, today)
                if sc is not None and sc >= cfg['buy']:
                    pos = {
                        'buy_date': today, 'buy_price': price,
                        'peak': price, 'score': sc, 'season': sea
                    }

        # 持仓到最后强制平仓
        if pos is not None:
            last_price = get_price(code, dates[-1]) or pos['buy_price']
            ret = (last_price - pos['buy_price']) / pos['buy_price']
            trades.append({
                'code': code, 'buy_date': str(pos['buy_date']),
                'sell_date': str(dates[-1]),
                'hold_days': (dates[-1] - pos['buy_date']).days,
                'ret': ret, 'score': pos.get('score', 0),
                'season': pos['season'], 'sell_reason': 'force_close'
            })

        all_trades.extend(trades)

    # 9. 统计
    total_trades = len(all_trades)
    win_trades = [t for t in all_trades if t['ret'] > 0]
    loss_trades = [t for t in all_trades if t['ret'] <= 0]

    win_rate = len(win_trades) / total_trades * 100 if total_trades else 0
    avg_ret = sum(t['ret'] for t in all_trades) / total_trades * 100 if total_trades else 0
    total_gain = sum(t['ret'] for t in win_trades)
    total_loss = abs(sum(t['ret'] for t in loss_trades)) or 1
    profit_factor = total_gain / total_loss

    elapsed = time.time() - t0

    print(f"""
{'='*60}
📊 V11快速回测结果 — 全量A股 + 季节自适应
{'='*60}
运行时间: {elapsed:.1f}秒
回测区间: {dates[0]} ~ {dates[-1]} ({len(dates)}个交易日)

总交易: {total_trades}笔
交易标的: {len(set(t['code'] for t in all_trades))}只
胜  率: {win_rate:.1f}%
均收益: {avg_ret:+.2f}%
盈亏比: {profit_factor:.2f}
均盈利: {sum(t['ret'] for t in win_trades) / len(win_trades) * 100:.2f}% ({len(win_trades)}笔)
均亏损: {sum(t['ret'] for t in loss_trades) / len(loss_trades) * 100:.2f}% ({len(loss_trades)}笔)

--- 按季节 ---""", flush=True)

    season_stats = defaultdict(lambda: {'trades': [], 'wins': 0})
    for t in all_trades:
        s = t['season']
        season_stats[s]['trades'].append(t)
        if t['ret'] > 0:
            season_stats[s]['wins'] += 1

    for label, sk in [('夏', 'summer'), ('春', 'spring'), ('弱春', 'chaos_spring'), ('混沌', 'chaos'), ('弱秋', 'chaos_autumn'), ('秋', 'autumn'), ('冬', 'winter')]:
        if sk in season_stats:
            ts = season_stats[sk]['trades']
            n = len(ts)
            wr = season_stats[sk]['wins'] / n * 100
            ar = sum(t['ret'] for t in ts) / n * 100
            print(f"  {label}: {n}笔 胜率{wr:.1f}% 均收益{ar:+.2f}%")

    print("\n--- 按持有天数 ---", flush=True)
    for d in [10, 20, 30, 60, 90]:
        g = [t for t in all_trades if t['hold_days'] <= d]
        if g:
            wg = [t for t in g if t['ret'] > 0]
            print(f"  ≤{d}d: {len(g)}笔 胜率{len(wg)/len(g)*100:.1f}% 均收益{sum(t['ret'] for t in g)/len(g)*100:+.2f}%")

    print("\n--- 按卖出原因 ---", flush=True)
    for reason in ['T1_stop', 'T2_trailing', 'hold_expire', 'force_close']:
        g = [t for t in all_trades if t['sell_reason'] == reason]
        if g:
            wg = [t for t in g if t['ret'] > 0]
            wr = len(wg) / len(g) * 100
            ar = sum(t['ret'] for t in g) / len(g) * 100
            print(f"  {reason}: {len(g)}笔 胜率{wr:.1f}% 均收益{ar:+.2f}%")

    print(f"\n--- 按评分区间 ---", flush=True)
    for low, high in [(70, 75), (75, 80), (80, 85), (85, 90), (90, 100)]:
        g = [t for t in all_trades if low <= t['score'] < high]
        if g:
            wg = [t for t in g if t['ret'] > 0]
            wr = len(wg) / len(g) * 100
            ar = sum(t['ret'] for t in g) / len(g) * 100
            print(f"  {low}-{high}: {len(g)}笔 胜率{wr:.1f}% 均收益{ar:+.2f}%")

    print(f"""

对比指标:
  标的范围: {len(all_codes)}只 (监控池{len(watch_codes)}+有评分{len(score_by_code)})
  V11参数: 夏72/春70/混沌75/秋75/冬85
  评分覆盖广度: {len(score_by_code)}只股票, {sum(len(v) for v in score_by_code.values())}条评分
""", flush=True)

    return all_trades

if __name__ == '__main__':
    main()
