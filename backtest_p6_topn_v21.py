#!/usr/bin/env python3
"""
P6 Top N 买入胜率验证 v2.1 — 多维度分解
=======================================
追加维度：
1. 评分阈值过滤（≥38 后再取 Top N）
2. 分年度（2023/2024/2025）
3. 评分区间切片（75+ / 38~75 / <38）
4. 分轨道 + 季节交叉
"""

import sys, os, math, json, time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')

from db_config import get_connection

# ============================================================
# 配置（复用 v2.0）
# ============================================================

START_DATE = date(2023, 1, 1)
END_DATE = date(2026, 5, 29)
TOP_N_VALUES = [3, 5]
HOLD_DAYS = [5, 10, 20, 30, 60]
STOP_LOSS_PCT = -0.10
TRAILING_STOP = -0.15

# ============================================================
# 数据加载 + 评分（同 v2.0）
# ============================================================

def _sma(arr, p):
    if len(arr) < p: return sum(arr)/len(arr) if arr else 0
    return sum(arr[-p:])/p

def _roc(arr, p):
    if len(arr) <= p: return 0
    return (arr[-1] - arr[-p-1]) / arr[-p-1]

def load_stock_list() -> List[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    ts_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return ts_codes

def load_kline_data(ts_code: str) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT d.trade_date, d.close, d.high, d.low, d.vol, d.amount,
               d.volume_ratio, d.turnover_rate,
               t.ma_5, t.ma_10, t.ma_20, t.ma_60, t.ma_120, t.ma_250,
               t.rsi_12 as rsi_14, t.atr_14
        FROM daily_kline d
        LEFT JOIN technical_indicator t ON d.ts_code=t.ts_code AND d.trade_date=t.trade_date
        WHERE d.ts_code=%s AND d.trade_date >= '2022-01-01' AND d.trade_date <= %s
        ORDER BY d.trade_date ASC
    """, (ts_code, END_DATE.isoformat()))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def load_season_data() -> Dict[date, Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, season, raw_score, index_code, confidence,
               chaos_subtype, scoring_strategy, regime
        FROM season_state WHERE index_code='MARKET'
        ORDER BY trade_date ASC
    """)
    season_map = {}
    for r in cur.fetchall():
        d = r['trade_date']
        if isinstance(d, str): d = datetime.strptime(d, '%Y-%m-%d').date()
        chaos_subtype = r.get('chaos_subtype') or None
        regime = r.get('regime', 'range') or 'range'
        scoring = r.get('scoring_strategy') or get_scoring_strategy(r['season'], chaos_subtype, regime)
        season_map[d] = {
            'season': r['season'],
            'raw_score': float(r.get('raw_score', 0) or 0),
            'confidence': float(r.get('confidence', 0.5) or 0.5),
            'chaos_subtype': chaos_subtype,
            'regime': regime,
            'scoring_strategy': scoring,
        }
    cur.close(); conn.close()
    return season_map

def score_p6_momentum(kline_rows: List[Dict]) -> float:
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 20: return 50
    close = closes[-1]; ma20 = float(kline_rows[-1].get('ma_20',0)or 0); ma60 = float(kline_rows[-1].get('ma_60',0)or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14',50)or 50); vr = float(kline_rows[-1].get('volume_ratio',1)or 1)
    tr = 35
    if ma20 > 0 and ma60 > 0:
        if close > ma20: tr += 15
        if ma20 > ma60: tr += 15
    if rsi_val > 55: tr += 10
    if rsi_val > 65: tr += 5
    trend_score = min(100, tr)
    mo = 40
    if len(closes) >= 5:
        r5 = _roc(closes, 5); mo += max(-15, min(15, r5*150))
    if len(closes) >= 10:
        r10 = _roc(closes, 10); mo += max(-10, min(10, r10*80))
    mo += (rsi_val - 50) * 0.5
    if vr > 1.5 and _roc(closes, 5) > 0.02: mo += 5
    momentum_raw = max(0, min(100, mo))
    return max(0, min(100, round(trend_score * 0.70 + momentum_raw * 0.30, 1)))

def score_p6_reversion(kline_rows: List[Dict]) -> float:
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 60: return 50
    close = closes[-1]; ma120 = float(kline_rows[-1].get('ma_120',0)or 0); ma250 = float(kline_rows[-1].get('ma_250',0)or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14',50)or 50); atr = float(kline_rows[-1].get('atr_14',0)or 0)
    od = 50
    if ma120 > 0:
        dev = (close - ma120) / ma120
        if dev < -0.15: od = 80
        elif dev < -0.10: od = 70
        elif dev < -0.05: od = 60
        elif dev > 0.10: od = 35
        elif dev > 0.05: od = 40
    if rsi_val < 25: od = min(90, od + 15)
    elif rsi_val < 30: od = min(85, od + 10)
    elif rsi_val < 40: od = min(75, od + 5)
    elif rsi_val > 70: od = max(20, od - 10)
    oversold = max(0, min(100, od))
    atr_pct = atr / close if close > 0 else 0.02
    if atr_pct < 0.015: vl = 70
    elif atr_pct < 0.025: vl = 60
    elif atr_pct < 0.04: vl = 50
    elif atr_pct < 0.06: vl = 35
    else: vl = 20
    volatility = vl
    st = 45
    if ma120 > 0 and ma250 > 0:
        if close < ma120: st += 10
        if close < ma250: st += 10
    if rsi_val < 40: st += 10
    if rsi_val > 60: st -= 8
    structure = max(0, min(100, st))
    return max(0, min(100, round(structure * 0.40 + oversold * 0.30 + volatility * 0.20, 1)))

def get_scoring_strategy(season, chaos_subtype=None, regime='range'):
    if season in ('autumn','winter'): return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_bearish': return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_neutral' and regime == 'bear': return 'reversion'
    return 'momentum'

def get_p6_score(ts_code, kline_rows, season_info):
    strategy = season_info.get('scoring_strategy', 'momentum')
    if strategy == 'momentum':
        raw = score_p6_momentum(kline_rows)
        calibrated = min(100, round(raw * 1.3, 1))
        return {'score': raw, 'calibrated': calibrated, 'track': 'momentum', 'strategy': strategy}
    else:
        raw = score_p6_reversion(kline_rows)
        return {'score': raw, 'calibrated': raw, 'track': 'reversion', 'strategy': strategy}

def simulate_trade(ts_code, kline_cache, eval_date, entry_price, hold_days):
    future_rows = [r for r in kline_cache[ts_code] if r['trade_date'] > eval_date]
    results = []
    for hd in hold_days:
        if len(future_rows) < hd: continue
        trigger_stop = False; exit_price = entry_price; exit_row_idx = hd - 1
        min_price = entry_price; max_price = entry_price
        for i, interim in enumerate(future_rows[:hd]):
            p = float(interim['close']); min_price = min(min_price, p); max_price = max(max_price, p)
            if p < entry_price * (1 + STOP_LOSS_PCT):
                trigger_stop = True; exit_price = p; exit_row_idx = i; break
            if max_price > entry_price * 1.05:
                if p < max_price * (1 + TRAILING_STOP):
                    trigger_stop = True; exit_price = p; exit_row_idx = i; break
        if not trigger_stop: exit_price = float(future_rows[hd-1]['close'])
        ret = (exit_price - entry_price) / entry_price
        results.append({'hold_days': hd, 'return': round(ret, 4), 'stop_triggered': trigger_stop,
                        'exit_date': future_rows[exit_row_idx]['trade_date'].isoformat()})
    return results


# ============================================================
# 回测核心 — 多维度分解
# ============================================================

def run_topn_backtest(ts_codes, season_map):
    kline_cache = {}
    all_trades = []
    all_dates = sorted(season_map.keys())
    trade_dates = [d for d in all_dates if d >= START_DATE]
    eval_dates = trade_dates[::5]
    total = len(eval_dates)

    print(f"📅 {len(trade_dates)}交易日, 评估{total}次 | 📈 {len(ts_codes)}只 | 🏆 Top{TOP_N_VALUES} | ⏱ {HOLD_DAYS}日")

    t0 = time.time()
    for idx, eval_date in enumerate(eval_dates):
        season_info = season_map.get(eval_date, {})
        if not season_info: continue
        if (idx+1) % 30 == 0 or idx == 0:
            print(f"   [{idx+1}/{total}] {eval_date} ({time.time()-t0:.0f}s)")

        scored = []
        for ts_code in ts_codes:
            if ts_code not in kline_cache: kline_cache[ts_code] = load_kline_data(ts_code)
            rows = kline_cache[ts_code]
            eval_rows = [r for r in rows if r['trade_date'] <= eval_date]
            if len(eval_rows) < 60: continue
            result = get_p6_score(ts_code, eval_rows, season_info)
            scored.append(result | {'ts_code': ts_code})

        if not scored: continue

        # 排序
        scored.sort(key=lambda x: x['calibrated'], reverse=True)

        # 分层：按校准分切片
        high_band = [s for s in scored if s['calibrated'] >= 75]
        mid_band = [s for s in scored if 38 <= s['calibrated'] < 75]
        low_band = [s for s in scored if s['calibrated'] < 38]
        threshold_filtered = [s for s in scored if s['calibrated'] >= 38]

        for top_n in TOP_N_VALUES:
            # 1. 纯Top N（无阈值过滤）
            top_pure = scored[:top_n]
            # 2. 阈值过滤后Top N
            top_filtered = threshold_filtered[:top_n]
            # 3. 高分带(75+)取前N
            top_high = high_band[:top_n]
            # 4. 中分带(38~75)取前N
            top_mid = mid_band[:top_n]

            for batch_name, batch in [
                ('pure', top_pure),
                ('filtered_ge38', top_filtered),
                ('high_75plus', top_high),
                ('mid_38to75', top_mid),
            ]:
                if not batch: continue
                for item in batch:
                    entry_price = None
                    for r in kline_cache[item['ts_code']]:
                        if r['trade_date'] == eval_date:
                            entry_price = float(r['close']); break
                    if entry_price is None or entry_price == 0: continue
                    trade_results = simulate_trade(item['ts_code'], kline_cache, eval_date, entry_price, HOLD_DAYS)
                    for tr in trade_results:
                        all_trades.append({
                            'ts_code': item['ts_code'],
                            'eval_date': eval_date.isoformat(),
                            'batch': batch_name,
                            'top_n': top_n,
                            'rank': scored.index(item)+1 if batch_name == 'pure' else (threshold_filtered.index(item)+1 if batch_name == 'filtered_ge38' else 0),
                            'score': item['score'],
                            'calibrated': item['calibrated'],
                            'track': item['track'],
                            'hold_days': tr['hold_days'],
                            'return': tr['return'],
                            'stop_triggered': tr['stop_triggered'],
                            'season': season_info.get('season','unknown'),
                            'regime': season_info.get('regime','unknown'),
                            'year': str(eval_date.year),
                        })

    print(f"✅ 完成! {time.time()-t0:.0f}s, {len(all_trades)}条记录")
    return {'trades': all_trades}


# ============================================================
# 统计
# ============================================================

def compute_stats(trades, label=""):
    if not trades: return {'label': label, 'trades': 0}
    w = [t for t in trades if t['return'] > 0]
    l = [t for t in trades if t['return'] <= 0]
    rets = [t['return'] for t in trades]
    mean_r = sum(rets)/len(rets)
    var_r = sum((r-mean_r)**2 for r in rets)/len(rets)
    std_r = math.sqrt(var_r)
    avg_hd = sum(t['hold_days'] for t in trades)/len(trades)
    sharpe = (mean_r/std_r * math.sqrt(250/avg_hd)) if std_r > 0 else 0
    return {
        'label': label, 'trades': len(trades),
        'win_rate': round(len(w)/len(trades)*100, 1),
        'avg_return': round(mean_r*100, 2),
        'avg_win': round(sum(t['return'] for t in w)/len(w)*100,2) if w else 0,
        'avg_loss': round(sum(t['return'] for t in l)/len(l)*100,2) if l else 0,
        'profit_factor': round(abs(sum(t['return'] for t in w)/sum(t['return'] for t in l)),2) if l and sum(t['return'] for t in l) != 0 else 'inf',
        'sharpe': round(sharpe, 2),
        'stop_rate': round(len([t for t in trades if t.get('stop_triggered')])/len(trades)*100, 1),
    }

# ============================================================
# 报告生成
# ============================================================

def generate_html_report(results):
    trades = results['trades']
    groups = defaultdict(list)
    for t in trades:
        groups[(t['batch'], t['top_n'], t['hold_days'])].append(t)

    season_groups = defaultdict(list)
    year_groups = defaultdict(list)
    regime_groups = defaultdict(list)
    for t in trades:
        season_groups[(t['batch'], t['top_n'], t['hold_days'], t['season'])].append(t)
        year_groups[(t['batch'], t['top_n'], t['hold_days'], t['year'])].append(t)
        regime_groups[(t['batch'], t['top_n'], t['hold_days'], t['regime'])].append(t)

    season_names = {'spring':'🌸春','summer':'☀️夏','autumn':'🍂秋','winter':'❄️冬','chaos':'🌪️混沌','chaos_spring':'🌤️弱春'}

    def hdr(text, a='green'): return f'<td class="{a}" style="font-weight:700">{text}</td>' if text in ('胜率','均收益','盈亏比','夏普') else f'<td>{text}</td>'

    _CSS = '''*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0a0e17;color:#e2e8f0;padding:40px;max-width:1400px;margin:auto}
h1{font-size:28px;margin-bottom:8px;background:linear-gradient(135deg,#f59e0b,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
h2{font-size:22px;margin:32px 0 12px;color:#f1f5f9;border-bottom:1px solid #1e293b;padding-bottom:8px}
h3{font-size:16px;margin:20px 0 8px;color:#94a3b8}
table{width:100%;border-collapse:collapse;margin:8px 0 20px;font-size:12px}
th{text-align:left;padding:6px 8px;color:#64748b;border-bottom:1px solid #1e293b;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid #1e293b}
tr:hover td{background:#1e293b}
.green{color:#22c55e!important;font-weight:700!important}
.red{color:#ef4444!important;font-weight:700!important}
.orange{color:#f59e0b}
.gray{color:#475569}
.header-sub{color:#475569;font-size:13px;margin-bottom:20px}
.section-desc{color:#64748b;font-size:12px;margin-bottom:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.bg-pure{background:#f59e0b;color:#000}
.bg-filtered{background:#3b82f6;color:#fff}
.bg-high{background:#22c55e;color:#000}
.bg-mid{background:#6366f1;color:#fff}
.best{background:linear-gradient(135deg,#22c55e,#16a34a);padding:2px 6px;border-radius:4px;color:#fff;font-size:11px}
.worst{background:linear-gradient(135deg,#ef4444,#dc2626);padding:2px 6px;border-radius:4px;color:#fff;font-size:11px}
'''
    html = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">\n'
    html += '<title>P6 Top N 多维度分解报告</title>\n<style>\n' + _CSS + '</style></head><body>\n'
    html += '<h1>📊 P6 Top N — 多维度分解报告</h1>\n'
    html += '<p class="header-sub">\n  区间: ' + str(START_DATE) + '~' + str(END_DATE) + ' | 195只 | 每5日评估 | -10%止损+15%移动止盈\n</p>\n'
    html += '<h2>📐 维度一：评分层面的四种取法对比</h2>\n'
    html += '<p class="section-desc">核心问题：纯排名 vs 阈值过滤后的排名，哪个更有效？</p>\n'
    html += '<table><tr><th>批次</th><th>Top N</th><th>持有期</th><th>笔数</th><th>胜率</th><th>均收益</th><th>盈亏比</th><th>夏普</th><th>止损率</th></tr>\n'
    for batch, b_label, b_class in [
        ('pure', '纯排名', 'bg-pure'),
        ('filtered_ge38', '阈值过滤≥38', 'bg-filtered'),
        ('high_75plus', '高分段≥75', 'bg-high'),
        ('mid_38to75', '中分段38~75', 'bg-mid'),
    ]:
        for top_n in TOP_N_VALUES:
            for hd in HOLD_DAYS:
                key = (batch, top_n, hd)
                group = groups.get(key, [])
                if not group: continue
                s = compute_stats(group)
                wc = 'green' if s['win_rate'] >= 50 else 'red'
                rc = 'green' if s['avg_return'] > 0 else 'red'
                html += f'<tr><td><span class="badge {b_class}">{b_label}</span></td>'
                html += f'<td>Top {top_n}</td><td>{hd}日</td>'
                html += f'<td>{s["trades"]}</td><td class="{wc}">{s["win_rate"]}%</td>'
                html += f'<td class="{rc}">{s["avg_return"]:+.2f}%</td>'
                html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td>'
                html += f'<td>{s["stop_rate"]}%</td></tr>\n'

    html += '</table>\n'

    # ========== 最优组合汇总 ==========
    html += '<h2>⭐ 各维度最优组合</h2>\n'
    all_combos = []
    for key, group in groups.items():
        s = compute_stats(group, str(key))
        all_combos.append((key, s))
    best5 = sorted(all_combos, key=lambda x: x[1]['sharpe'], reverse=True)[:5]

    html += '<table><tr><th>排名</th><th>维度</th><th>Top N</th><th>持有期</th><th>胜率</th><th>均收益</th><th>盈亏比</th><th>夏普</th></tr>\n'
    for i, (key, s) in enumerate(best5, 1):
        batch, top_n, hd = key
        b_label = {'pure':'纯排名','filtered_ge38':'阈值≥38','high_75plus':'高分≥75','mid_38to75':'中分38~75'}.get(batch, batch)
        html += f'<tr><td>{i}</td><td>{b_label}</td><td>Top {top_n}</td><td>{hd}日</td>'
        html += f'<td class="green">{s["win_rate"]}%</td><td class="green">{s["avg_return"]:+.2f}%</td>'
        html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td></tr>\n'
    html += '</table>\n'

    # ========== 分年度 ==========
    html += '<h2>📅 维度二：分年度（纯Top 3 × 20日）</h2>\n'
    batch_key = 'pure'
    base_key = (batch_key, 3, 20)
    html += '<table><tr><th>年份</th><th>季节分布</th><th>笔数</th><th>胜率</th><th>均收益</th><th>盈亏比</th><th>夏普</th></tr>\n'
    for year in sorted(set(t['year'] for t in trades if t['batch']==batch_key and t['top_n']==3 and t['hold_days']==20)):
        group = year_groups.get((batch_key, 3, 20, year), [])
        if not group: continue
        s = compute_stats(group, year)
        # 季节分布
        season_dist = defaultdict(int)
        for t in group: season_dist[t['season']] += 1
        dist_str = ' | '.join(f'{season_names.get(k,k)}:{v}' for k,v in sorted(season_dist.items(), key=lambda x:-x[1]))
        html += f'<tr><td><strong>{year}</strong></td><td style="font-size:11px">{dist_str}</td>'
        html += f'<td>{s["trades"]}</td><td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
        html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
        html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td></tr>\n'
    html += '</table>\n'

    # ========== 季节分解（全持有期） ==========
    html += '<h2>🍂 维度三：季节×持有期（纯Top 3）</h2>\n'
    html += '<table><tr><th>季节</th>'
    for hd in HOLD_DAYS: html += f'<th>{hd}日胜率</th><th>{hd}日收益</th><th>笔数</th>'
    html += '</tr>\n'
    for sk in ['spring','summer','autumn','winter','chaos_spring','chaos']:
        label = season_names.get(sk, sk)
        html += f'<tr><td>{label}</td>'
        any_data = False
        for hd in HOLD_DAYS:
            group = season_groups.get(('pure', 3, hd, sk), [])
            if group:
                any_data = True
                s = compute_stats(group)
                html += f'<td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
                html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
                html += f'<td>{s["trades"]}</td>'
            else:
                html += '<td class="gray">-</td><td class="gray">-</td><td class="gray">0</td>'
        html += '</tr>\n'
    html += '</table>\n'

    # ========== 市场状态分解 ==========
    html += '<h2>📈 维度四：市场状态×持有期（纯Top 3）</h2>\n'
    html += '<table><tr><th>市场状态</th>'
    for hd in HOLD_DAYS: html += f'<th>{hd}日胜率</th><th>{hd}日收益</th><th>笔数</th>'
    html += '</tr>\n'
    for reg in ['bull','range','bear']:
        label = {'bull':'🐂牛市','range':'⚖️震荡','bear':'🐻熊市'}.get(reg,reg)
        html += f'<tr><td>{label}</td>'
        for hd in HOLD_DAYS:
            group = regime_groups.get(('pure', 3, hd, reg), [])
            if group:
                s = compute_stats(group)
                html += f'<td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
                html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
                html += f'<td>{s["trades"]}</td>'
            else:
                html += '<td class="gray">-</td><td class="gray">-</td><td class="gray">0</td>'
        html += '</tr>\n'
    html += '</table>\n'

    # ========== 评分区间切片深度对比（20日持有） ==========
    html += '<h2>🎯 维度五：评分区间切片（20日持有）</h2>\n'
    html += '<p class="section-desc">高分段(≥75)、中分段(38~75)、低分段(<38)分别取前N只，20日持有表现</p>\n'
    html += '<table><tr><th>批次</th><th>Top N</th><th>笔数</th><th>胜率</th><th>均收益</th><th>盈亏比</th><th>夏普</th></tr>\n'
    for batch, b_label in [('high_75plus','高分段≥75'),('mid_38to75','中分段38~75'),('filtered_ge38','阈值≥38(含高中两段)'),('pure','纯排名(无过滤)')]:
        for top_n in TOP_N_VALUES:
            key = (batch, top_n, 20)
            group = groups.get(key, [])
            if not group: continue
            s = compute_stats(group)
            html += f'<tr><td>{b_label}</td><td>Top {top_n}</td>'
            html += f'<td>{s["trades"]}</td><td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
            html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
            html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td></tr>\n'
    html += '</table>\n'

    html += '</body></html>'
    return html


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--output', type=str, default='/tmp/p6_topn_multi_report.html')
    args = parser.parse_args()

    print('📂 加载数据...')
    ts_codes = load_stock_list()
    if args.limit > 0: ts_codes = ts_codes[:args.limit]
    season_map = load_season_data()
    print(f'   {len(ts_codes)}只, {len(season_map)}个季节判定')

    print('🏃 跑回测...')
    t0 = time.time()
    results = run_topn_backtest(ts_codes, season_map)
    print(f'⏱ {time.time()-t0:.0f}s')

    print('📄 生成报告...')
    html = generate_html_report(results)
    with open(args.output, 'w') as f: f.write(html)
    print(f'✅ {args.output}')
