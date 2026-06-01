#!/usr/bin/env python3
"""
P6 Top N 买入胜率验证 v2.0
==========================
不再对比 v4 vs P6，而是问一个核心问题：
"P6评分排在最前面的3/5/10只股票，买入后持有不同天数，胜率和收益是多少？"

核心设计：
1. 每日P6评分全量 > 按校准分排序 > 取前N只
2. 记录入场价格后，追踪后续5/10/20/30/60日的收益
3. 含-10%止损和移动回撤15%止盈（同实盘规则）
4. 按季节/市场状态分拆，看哪类市场Top N最靠谱
5. 对比"Top N" vs "评分≥阈值全部买入" 两种逻辑的胜率差异
"""

import sys, os, math, json, time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')

from db_config import get_connection

# ============================================================
# 配置
# ============================================================

START_DATE = date(2023, 1, 1)
END_DATE = date(2026, 5, 29)

# 要测试的Top N
TOP_N_VALUES = [3, 5, 10]

# 要测试的持有期（交易日）
HOLD_DAYS = [5, 10, 20, 30, 60]

# 止损
STOP_LOSS_PCT = -0.10        # -10%止损
TRAILING_STOP = -0.15        # 移动回撤15%止盈

# ============================================================
# 数据加载（复用原脚本）
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
               t.rsi_12 as rsi_14, t.atr_14,
               t.boll_upper, t.boll_mid, t.boll_lower
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
        SELECT trade_date, season, raw_score, 
               index_code, confidence, chaos_subtype, scoring_strategy, regime
        FROM season_state 
        WHERE index_code='MARKET'
        ORDER BY trade_date ASC
    """)
    season_map = {}
    for r in cur.fetchall():
        d = r['trade_date']
        if isinstance(d, str):
            d = datetime.strptime(d, '%Y-%m-%d').date()
        chaos_subtype = r.get('chaos_subtype') if r.get('chaos_subtype') else None
        regime = r.get('regime', 'range') if r.get('regime') else 'range'
        scoring = r.get('scoring_strategy') if r.get('scoring_strategy') else get_scoring_strategy(r['season'], chaos_subtype, regime)
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


# ============================================================
# P6 双轨评分（同原引擎）
# ============================================================

def score_p6_momentum(kline_rows: List[Dict]) -> float:
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 20: return 50
    close = closes[-1]
    ma20 = float(kline_rows[-1].get('ma_20', 0) or 0)
    ma60 = float(kline_rows[-1].get('ma_60', 0) or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14', 50) or 50)
    vr = float(kline_rows[-1].get('volume_ratio', 1) or 1)
    tr = 35
    if ma20 > 0 and ma60 > 0:
        if close > ma20: tr += 15
        if ma20 > ma60: tr += 15
    if rsi_val > 55: tr += 10
    if rsi_val > 65: tr += 5
    trend_score = min(100, tr)
    mo = 40
    if len(closes) >= 5:
        r5 = _roc(closes, 5)
        mo += max(-15, min(15, r5*150))
    if len(closes) >= 10:
        r10 = _roc(closes, 10)
        mo += max(-10, min(10, r10*80))
    mo += (rsi_val - 50) * 0.5
    if vr > 1.5 and _roc(closes, 5) > 0.02: mo += 5
    momentum_raw = max(0, min(100, mo))
    final = trend_score * 0.70 + momentum_raw * 0.30
    return max(0, min(100, round(final, 1)))

def score_p6_reversion(kline_rows: List[Dict]) -> float:
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 60: return 50
    close = closes[-1]
    ma120 = float(kline_rows[-1].get('ma_120', 0) or 0)
    ma250 = float(kline_rows[-1].get('ma_250', 0) or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14', 50) or 50)
    atr = float(kline_rows[-1].get('atr_14', 0) or 0)
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
    final = structure * 0.40 + oversold * 0.30 + volatility * 0.20
    return max(0, min(100, round(final, 1)))

def get_scoring_strategy(season: str, chaos_subtype: str = None, regime: str = 'range') -> str:
    if season in ('autumn', 'winter'):
        return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_bearish':
        return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_neutral' and regime == 'bear':
        return 'reversion'
    return 'momentum'

def get_p6_score(ts_code: str, kline_rows: List[Dict], season_info: dict) -> Dict:
    strategy = season_info.get('scoring_strategy', 'momentum')
    if strategy == 'momentum':
        raw = score_p6_momentum(kline_rows)
        calibrated = min(100, round(raw * 1.3, 1))
        return {'score': raw, 'calibrated': calibrated, 'track': 'momentum', 'strategy': strategy}
    else:
        raw = score_p6_reversion(kline_rows)
        return {'score': raw, 'calibrated': raw, 'track': 'reversion', 'strategy': strategy}


# ============================================================
# 回测核心 — Top N 买入验证
# ============================================================

def simulate_trade(ts_code: str, kline_cache: dict, eval_date: date, 
                   entry_price: float, hold_days: List[int]) -> List[Dict]:
    """
    模拟一笔交易在不同持有期的表现（含止损）
    
    Returns: [{hold_days, return, stop_triggered, exit_date, max_drawdown}, ...]
    """
    future_rows = [r for r in kline_cache[ts_code] if r['trade_date'] > eval_date]
    results = []
    
    for hd in hold_days:
        if len(future_rows) < hd:
            continue
        
        trigger_stop = False
        exit_price = entry_price
        exit_row_idx = hd - 1
        min_price = entry_price
        max_price = entry_price
        
        for i, interim in enumerate(future_rows[:hd]):
            p = float(interim['close'])
            min_price = min(min_price, p)
            max_price = max(max_price, p)
            
            # -10%止损
            if p < entry_price * (1 + STOP_LOSS_PCT):
                trigger_stop = True
                exit_price = p
                exit_row_idx = i
                break
            
            # 移动回撤15%止盈（从最高点回撤15%就出）
            if max_price > entry_price * 1.05:  # 至少涨5%后才启用
                if p < max_price * (1 + TRAILING_STOP):
                    trigger_stop = True
                    exit_price = p
                    exit_row_idx = i
                    break
        
        if not trigger_stop:
            exit_price = float(future_rows[hd - 1]['close'])
        
        ret = (exit_price - entry_price) / entry_price
        max_dd = (min_price - max_price) / max_price if max_price > entry_price else (min_price - entry_price) / entry_price
        
        results.append({
            'hold_days': hd,
            'return': round(ret, 4),
            'stop_triggered': trigger_stop,
            'exit_date': future_rows[exit_row_idx]['trade_date'].isoformat(),
            'max_drawdown': round(max_dd, 4),
            'max_runup': round((max_price - entry_price) / entry_price, 4),
        })
    
    return results


def run_topn_backtest(ts_codes: List[str], season_map: dict) -> Dict:
    """
    Top N 买入胜率回测
    
    每日P6评分 -> 排序 -> 取前N只 -> 记录后续收益
    返回按 top_n 和 hold_days 分组的统计
    """
    kline_cache = {}
    all_trades = []  # 记录每笔交易原始数据，后续按top_n分组
    
    all_dates = sorted(season_map.keys())
    trade_dates = [d for d in all_dates if d >= START_DATE]
    
    # 每隔5个交易日评估一次（减少高频噪声）
    eval_dates = trade_dates[::5]
    total_dates = len(eval_dates)
    
    print(f"📅 回测区间: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)}交易日, 评估{total_dates}次)")
    print(f"📈 股票池: {len(ts_codes)} 只")
    print(f"🏆 测试 Top N: {TOP_N_VALUES}")
    print(f"⏱  测试持有期: {HOLD_DAYS} 日")
    print()
    
    t0 = time.time()
    
    for idx, eval_date in enumerate(eval_dates):
        season_info = season_map.get(eval_date, {})
        if not season_info:
            continue
        
        if (idx + 1) % 20 == 0 or idx == 0:
            elapsed = time.time() - t0
            print(f"   [{idx+1}/{total_dates}] {eval_date} ({elapsed:.0f}s)")
        
        # --- 全量P6评分 ---
        scored = []
        for ts_code in ts_codes:
            if ts_code not in kline_cache:
                kline_cache[ts_code] = load_kline_data(ts_code)
            
            rows = kline_cache[ts_code]
            eval_rows = [r for r in rows if r['trade_date'] <= eval_date]
            if len(eval_rows) < 60:
                continue
            
            result = get_p6_score(ts_code, eval_rows, season_info)
            scored.append({
                'ts_code': ts_code,
                'score': result['score'],
                'calibrated': result['calibrated'],
                'track': result['track'],
            })
        
        if not scored:
            continue
        
        # --- 按校准分排序 ---
        scored.sort(key=lambda x: x['calibrated'], reverse=True)
        ranked = scored  # 排序后的完整列表
        
        # --- 对每个Top N值记录交易 ---
        for top_n in TOP_N_VALUES:
            top = ranked[:top_n]
            for item in top:
                # 获取入场价格
                entry_price = None
                for r in kline_cache[item['ts_code']]:
                    if r['trade_date'] == eval_date:
                        entry_price = float(r['close'])
                        break
                
                if entry_price is None or entry_price == 0:
                    continue
                
                # 模拟交易
                trade_results = simulate_trade(
                    item['ts_code'], kline_cache, eval_date, entry_price, HOLD_DAYS
                )
                
                for tr in trade_results:
                    all_trades.append({
                        'ts_code': item['ts_code'],
                        'eval_date': eval_date.isoformat(),
                        'top_n': top_n,
                        'rank': ranked.index(item) + 1,
                        'score': item['score'],
                        'calibrated': item['calibrated'],
                        'track': item['track'],
                        'entry_price': entry_price,
                        'hold_days': tr['hold_days'],
                        'return': tr['return'],
                        'stop_triggered': tr['stop_triggered'],
                        'exit_date': tr['exit_date'],
                        'max_drawdown': tr['max_drawdown'],
                        'max_runup': tr['max_runup'],
                        'season': season_info.get('season', 'unknown'),
                        'regime': season_info.get('regime', 'unknown'),
                        'chaos_subtype': season_info.get('chaos_subtype', ''),
                    })
    
    elapsed = time.time() - t0
    print(f"\n✅ 回测完成！耗时 {elapsed:.0f}s, 总交易记录: {len(all_trades)} 条")
    
    return {'trades': all_trades}


# ============================================================
# 统计与报告
# ============================================================

def compute_stats(trades: List[Dict], label: str = "") -> Dict:
    """计算一组交易的统计"""
    if not trades:
        return {'label': label, 'trades': 0}
    
    winners = [t for t in trades if t['return'] > 0]
    losers = [t for t in trades if t['return'] <= 0]
    
    total_ret = sum(t['return'] for t in trades)
    win_ret = sum(t['return'] for t in winners)
    loss_ret = sum(t['return'] for t in losers)
    
    returns = [t['return'] for t in trades]
    mean_ret = sum(returns) / len(returns)
    var_ret = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    std_ret = math.sqrt(var_ret)
    
    # 最大回撤（简单版本：最大亏损）
    max_loss = min(returns) if returns else 0
    
    # 夏普（假设无风险利率0，年化因子 sqrt(250/持有期))
    avg_hold = sum(t['hold_days'] for t in trades) / len(trades) if trades else 20
    sharpe = (mean_ret / std_ret * math.sqrt(250 / avg_hold)) if std_ret > 0 else 0
    
    stats = {
        'label': label,
        'trades': len(trades),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': round(len(winners) / len(trades) * 100, 1),
        'avg_return': round(mean_ret * 100, 2),
        'avg_win': round(win_ret / len(winners) * 100, 2) if winners else 0,
        'avg_loss': round(loss_ret / len(losers) * 100, 2) if losers else 0,
        'profit_factor': round(abs(win_ret / loss_ret), 2) if losers and loss_ret != 0 else 'inf',
        'total_return_pct': round(total_ret * 100, 2),
        'max_loss': round(max_loss * 100, 2),
        'std_return': round(std_ret * 100, 2),
        'sharpe': round(sharpe, 2),
        'stop_rate': round(len([t for t in trades if t.get('stop_triggered', False)]) / len(trades) * 100, 1),
    }
    return stats


def generate_html_report(results: Dict):
    """生成 HTML 报告"""
    trades = results['trades']
    
    # ---- 按Top N + 持有期分组 ----
    groups = defaultdict(list)
    for t in trades:
        groups[(t['top_n'], t['hold_days'])].append(t)
    
    # ---- 按季节分组 ----
    season_groups = defaultdict(list)
    for t in trades:
        season_groups[(t['top_n'], t['hold_days'], t['season'])].append(t)
    
    # ---- 按市场状态分组 ----
    regime_groups = defaultdict(list)
    for t in trades:
        regime_groups[(t['top_n'], t['hold_days'], t['regime'])].append(t)
    
    # ---- 按轨道分组 ----
    track_groups = defaultdict(list)
    for t in trades:
        track_groups[(t['top_n'], t['hold_days'], t['track'])].append(t)
    
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>P6 Top N 买入胜率验证报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0a0e17;color:#e2e8f0;padding:40px;max-width:1200px;margin:auto}}
h1{{font-size:28px;margin-bottom:8px;background:linear-gradient(135deg,#f59e0b,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
h2{{font-size:22px;margin:36px 0 12px;color:#f1f5f9}}
h3{{font-size:17px;margin:24px 0 10px;color:#94a3b8;border-bottom:1px solid #1e293b;padding-bottom:6px}}
.card{{background:#111827;border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #1e293b}}
table{{width:100%;border-collapse:collapse;margin:8px 0}}
th{{text-align:left;padding:8px 10px;font-size:11px;color:#64748b;border-bottom:1px solid #1e293b;white-space:nowrap}}
td{{padding:8px 10px;font-size:12px;border-bottom:1px solid #1e293b}}
tr:hover td{{background:#1e293b}}
.green{{color:#22c55e;font-weight:700}}
.red{{color:#ef4444;font-weight:700}}
.orange{{color:#f59e0b}}
.blue{{color:#3b82f6}}
.gray{{color:#64748b}}
.section-desc{{color:#64748b;font-size:13px;margin-bottom:16px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
.badge-top3{{background:#f59e0b;color:#000}}
.badge-top5{{background:#3b82f6;color:#fff}}
.badge-top10{{background:#6366f1;color:#fff}}
.best{{background:linear-gradient(135deg,#22c55e,#16a34a);padding:2px 8px;border-radius:4px;color:#fff;font-weight:700;font-size:11px}}
.worst{{background:linear-gradient(135deg,#ef4444,#dc2626);padding:2px 8px;border-radius:4px;color:#fff;font-weight:700;font-size:11px}}
.metric-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:8px 0}}
.metric-box{{background:#1e293b;padding:10px;border-radius:6px;text-align:center}}
.ml{{font-size:10px;color:#64748b}}
.mv{{font-size:18px;font-weight:700}}
.summary-row{{display:flex;gap:12px;margin:12px 0;flex-wrap:wrap}}
.summary-card{{background:#1e293b;border-radius:8px;padding:12px 16px;min-width:150px;flex:1}}
.sc-title{{font-size:11px;color:#64748b}}
.sc-val{{font-size:22px;font-weight:700}}
.diff-pos{{color:#22c55e}}
.diff-neg{{color:#ef4444}}
.header-sub{{color:#475569;font-size:13px;margin-bottom:20px}}
</style></head><body>
<h1>🏆 P6 Top N 买入胜率验证</h1>
<p class="header-sub">
  回测区间: {START_DATE} ~ {END_DATE} | 评估频率: 每5交易日 | 
  止损: -10% | 移动止盈: 15%回撤<br>
  不含买入阈值过滤 — 只看排名前N只绝对表现
</p>
"""
    
    # ==================================================================
    # 第一部分：核心结果 — 按Top N + 持有期矩阵
    # ==================================================================
    html += '<h2>📊 核心矩阵：Top N × 持有期</h2>\n'
    html += '<p class="section-desc">每个格子代表：胜率 | 平均收益率 | 盈亏比 | 交易笔数</p>\n'
    
    for top_n in TOP_N_VALUES:
        badge_class = f'badge-top{top_n}' if top_n in (3, 5, 10) else ''
        html += f'<h3><span class="badge {badge_class}">Top {top_n}</span> 在不同持有期的表现</h3>\n'
        html += '<table><tr><th>持有期</th><th>交易笔数</th><th>胜率</th><th>平均收益率</th><th>盈亏比</th><th>夏普</th><th>止损触发率</th><th>评价</th></tr>\n'
        
        for hd in HOLD_DAYS:
            key = (top_n, hd)
            group = groups.get(key, [])
            stats = compute_stats(group)
            
            wr = stats['win_rate']
            avg = stats['avg_return']
            pf = stats['profit_factor']
            sharpe = stats['sharpe']
            stop_r = stats['stop_rate']
            
            # 评价
            if wr >= 55 and avg > 2 and pf > 1.5:
                rating = f'<span class="best">优秀</span>'
            elif wr >= 45 and avg > 0 and pf > 1.2:
                rating = f'<span class="badge" style="background:#22c55e;color:#000">良好</span>'
            elif avg > 0:
                rating = f'<span class="badge" style="background:#f59e0b;color:#000">一般</span>'
            else:
                rating = f'<span class="worst">无效</span>'
            
            html += f'<tr><td><strong>{hd}日</strong></td><td>{stats["trades"]}</td>'
            html += f'<td class="{"green" if wr>=50 else "red"}">{wr}%</td>'
            html += f'<td class="{"green" if avg>0 else "red"}">{avg:+.2f}%</td>'
            html += f'<td>{pf}</td><td class="{"green" if sharpe>0.5 else "gray"}">{sharpe}</td>'
            html += f'<td>{stop_r}%</td><td>{rating}</td></tr>\n'
        
        html += '</table>\n'
    
    # ==================================================================
    # 第二部分：最佳组合
    # ==================================================================
    html += '<h2>🌟 最佳 & 最差组合</h2>\n'
    
    all_stats = []
    for key, group in groups.items():
        s = compute_stats(group, f"Top{key[0]} × {key[1]}日")
        all_stats.append(s)
    
    best = sorted(all_stats, key=lambda x: x.get('sharpe', -999), reverse=True)[:3]
    worst = sorted(all_stats, key=lambda x: x.get('sharpe', 999))[:3]
    
    html += '<div class="summary-row">'
    for b in best:
        html += f'<div class="summary-card" style="border-left:3px solid #22c55e">'
        html += f'<div class="sc-title">{b["label"]}</div>'
        html += f'<div class="sc-val" style="color:#22c55e">+{b["avg_return"]:.2f}%</div>'
        html += f'<div style="font-size:11px;color:#64748b">胜率{b["win_rate"]}% | 夏普{b["sharpe"]} | {b["trades"]}笔</div></div>\n'
    for w in worst:
        html += f'<div class="summary-card" style="border-left:3px solid #ef4444">'
        html += f'<div class="sc-title">{w["label"]}</div>'
        html += f'<div class="sc-val" style="color:#ef4444">{w["avg_return"]:+.2f}%</div>'
        html += f'<div style="font-size:11px;color:#64748b">胜率{w["win_rate"]}% | 夏普{w["sharpe"]} | {w["trades"]}笔</div></div>\n'
    html += '</div>\n'
    
    # ==================================================================
    # 第三部分：季节分解
    # ==================================================================
    html += '<h2>🍂 季节分解（Top 3 × 不同持有期）</h2>\n'
    html += '<p class="section-desc">聚焦最佳买入（Top 3），分季节看胜率变化</p>\n'
    
    season_names = {
        'spring': '🌸 春季', 'summer': '☀️ 夏季', 'autumn': '🍂 秋季', 'winter': '❄️ 冬季',
        'chaos': '🌪️ 混沌', 'chaos_spring': '🌤️ 弱春',
    }
    
    html += '<table><tr><th>季节</th>'
    for hd in HOLD_DAYS:
        html += f'<th>{hd}日胜率</th><th>{hd}日均收益</th>'
    html += '<th>总笔数</th></tr>\n'
    
    for season_key in ['spring', 'summer', 'autumn', 'winter', 'chaos_spring', 'chaos']:
        label = season_names.get(season_key, season_key)
        html += f'<tr><td>{label}</td>'
        total_cnt = 0
        for hd in HOLD_DAYS:
            group = season_groups.get((3, hd, season_key), [])
            if group:
                s = compute_stats(group)
                total_cnt = max(total_cnt, s['trades'])
                wr_class = "green" if s['win_rate'] >= 50 else "red"
                ret_class = "green" if s['avg_return'] > 0 else "red"
                html += f'<td class="{wr_class}">{s["win_rate"]}%</td><td class="{ret_class}">{s["avg_return"]:+.2f}%</td>'
            else:
                html += '<td class="gray">-</td><td class="gray">-</td>'
        html += f'<td>{total_cnt}</td></tr>\n'
    
    html += '</table>\n'
    
    # ==================================================================
    # 第四部分：市场状态分解
    # ==================================================================
    html += '<h2>📈 市场状态分解（Top 3）</h2>\n'
    
    html += '<table><tr><th>市场状态</th>'
    for hd in HOLD_DAYS:
        html += f'<th>{hd}日胜率</th><th>{hd}日均收益</th>'
    html += '<th>总笔数</th></tr>\n'
    
    for reg in ['bull', 'range', 'bear']:
        reg_label = {'bull': '🐂 牛市', 'range': '⚖️ 震荡', 'bear': '🐻 熊市'}.get(reg, reg)
        html += f'<tr><td>{reg_label}</td>'
        total_cnt = 0
        for hd in HOLD_DAYS:
            group = regime_groups.get((3, hd, reg), [])
            if group:
                s = compute_stats(group)
                total_cnt = max(total_cnt, s['trades'])
                wr_class = "green" if s['win_rate'] >= 50 else "red"
                ret_class = "green" if s['avg_return'] > 0 else "red"
                html += f'<td class="{wr_class}">{s["win_rate"]}%</td><td class="{ret_class}">{s["avg_return"]:+.2f}%</td>'
            else:
                html += '<td class="gray">-</td><td class="gray">-</td>'
        html += f'<td>{total_cnt}</td></tr>\n'
    
    html += '</table>\n'
    
    # ==================================================================
    # 第五部分：动量 vs 回归轨道对比
    # ==================================================================
    html += '<h2>🚀 动量轨道 vs 🎯 回归轨道（Top 3）</h2>\n'
    
    html += '<table><tr><th>轨道</th>'
    for hd in HOLD_DAYS:
        html += f'<th>{hd}日胜率</th><th>{hd}日均收益</th>'
    html += '<th>总笔数</th></tr>\n'
    
    for track in ['momentum', 'reversion']:
        track_label = '🚀 动量轨道' if track == 'momentum' else '🎯 回归轨道'
        html += f'<tr><td>{track_label}</td>'
        total_cnt = 0
        for hd in HOLD_DAYS:
            group = track_groups.get((3, hd, track), [])
            if group:
                s = compute_stats(group)
                total_cnt = max(total_cnt, s['trades'])
                wr_class = "green" if s['win_rate'] >= 50 else "red"
                ret_class = "green" if s['avg_return'] > 0 else "red"
                html += f'<td class="{wr_class}">{s["win_rate"]}%</td><td class="{ret_class}">{s["avg_return"]:+.2f}%</td>'
            else:
                html += '<td class="gray">-</td><td class="gray">-</td>'
        html += f'<td>{total_cnt}</td></tr>\n'
    
    html += '</table>\n'
    
    # ==================================================================
    # 第六部分：Top 3 vs Top 5 vs Top 10 直接对比（20日持有）
    # ==================================================================
    html += '<h2>⚔️ Top N 直接对比（20日持有期）</h2>\n'

    html += '<table><tr><th>排名范围</th><th>交易笔数</th><th>胜率</th><th>平均收益率</th><th>盈亏比</th><th>夏普</th><th>止损率</th></tr>\n'

    for top_n in TOP_N_VALUES:
        key = (top_n, 20)
        group = groups.get(key, [])
        if group:
            s = compute_stats(group)
            html += f'<tr><td><strong>Top {top_n}</strong></td><td>{s["trades"]}</td>'
            html += f'<td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
            html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
            html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td><td>{s["stop_rate"]}%</td></tr>\n'

    html += '</table>'

    # ==================================================================
    # 第七部分：分年度
    # ==================================================================
    html += '<h2>📅 分年度表现（Top 3 × 20日持有）</h2>\n'

    year_groups = defaultdict(list)
    for t in trades:
        if t['top_n'] == 3 and t['hold_days'] == 20:
            year = t['eval_date'][:4]
            year_groups[year].append(t)

    html += '<table><tr><th>年份</th><th>交易笔数</th><th>胜率</th><th>平均收益率</th><th>盈亏比</th><th>夏普</th></tr>\n'
    for year in sorted(year_groups.keys()):
        group = year_groups[year]
        s = compute_stats(group)
        html += f'<tr><td><strong>{year}</strong></td><td>{s["trades"]}</td>'
        html += f'<td class="{"green" if s["win_rate"]>=50 else "red"}">{s["win_rate"]}%</td>'
        html += f'<td class="{"green" if s["avg_return"]>0 else "red"}">{s["avg_return"]:+.2f}%</td>'
        html += f'<td>{s["profit_factor"]}</td><td>{s["sharpe"]}</td></tr>\n'

    html += '</table>\n'

    # ==================================================================
    # 第八部分：原始数据采样
    # ==================================================================
    html += '<h2>📝 Top 3 最优信号示例</h2>\n'
    html += '<table><tr><th>股票</th><th>买入日</th><th>评分</th><th>排名</th><th>轨道</th><th>持有</th><th>收益</th></tr>\n'

    top3_20d = [t for t in trades if t['top_n'] == 3 and t['hold_days'] == 20]
    top3_20d.sort(key=lambda x: x['return'], reverse=True)
    for t in top3_20d[:15]:
        color = '#ef4444' if t['return'] > 0 else '#22c55e'
        html += f'<tr><td>{t["ts_code"]}</td><td>{t["eval_date"][:10]}</td><td>{t["score"]}</td>'
        html += f'<td>#{t["rank"]}</td><td>{"🚀" if t["track"]=="momentum" else "🎯"}</td>'
        html += f'<td>{t["hold_days"]}日</td>'
        html += f'<td style="color:{color};font-weight:700">{t["return"]*100:+.2f}%</td></tr>\n'

    html += '</table>\n'

    # ==================================================================
    # 页脚
    # ==================================================================
    html += f'''
<div style="margin-top:40px;padding:16px;background:#111827;border-radius:8px;border:1px solid #1e293b">
<h3 style="color:#94a3b8;border:none;margin:0 0 8px">📌 分析说明</h3>
<ol style="color:#64748b;font-size:13px;padding-left:20px;line-height:1.8">
<li><strong>评估频率</strong>：每5个交易日做一次全量评分和排序（避免高频噪声）</li>
<li><strong>买入规则</strong>：按校准分排序，直接取前N只，<strong>不设最低评分阈值</strong>（想看到底Top N的绝对品质）</li>
<li><strong>止损规则</strong>：-10%硬止损 + 从高点回撤15%移动止盈（与实盘一致）</li>
<li><strong>持有期</strong>：5/10/20/30/60日，每个持有期独立统计（同一笔交易按最先触发的持有期计）</li>
<li><strong>关键问题</strong>：如果只买系统评分最高的3只，长期来看胜率和收益能否覆盖风险？</li>
</ol>
</div>
'''

    html += '</body></html>'
    return html


# ============================================================
# 入口
# ============================================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='P6 Top N 买入胜率验证')
    parser.add_argument('--limit', type=int, default=0, help='限制股票数量（调试用）')
    parser.add_argument('--output', type=str, default='/tmp/p6_topn_report.html', help='HTML输出路径')
    parser.add_argument('--data', type=str, default='', help='导出原始数据到JSON')
    args = parser.parse_args()

    print('=' * 60)
    print('🏆 P6 Top N 买入胜率验证')
    print('=' * 60)
    print()

    # 加载数据
    print('📂 加载股票列表...')
    ts_codes = load_stock_list()
    if args.limit > 0:
        ts_codes = ts_codes[:args.limit]
        print(f'   (限制 {args.limit} 只，调试模式)')
    print(f'   {len(ts_codes)} 只')

    print('📂 加载季节数据...')
    season_map = load_season_data()
    print(f'   {len(season_map)} 个交易日')
    print()

    # 跑回测
    print('🏃 运行 Top N 买入回测...')
    t0 = time.time()
    results = run_topn_backtest(ts_codes, season_map)
    elapsed = time.time() - t0
    print(f'   ⏱ 耗时: {elapsed:.0f}s\n')

    # 保存原始数据
    if args.data:
        serializable_trades = []
        for t in results['trades']:
            tc = dict(t)
            serializable_trades.append(tc)
        with open(args.data, 'w') as f:
            json.dump(serializable_trades, f, indent=2)
        print(f'💾 原始数据已保存: {args.data}')

    # 生成报告
    print('📄 生成HTML报告...')
    html = generate_html_report(results)
    with open(args.output, 'w') as f:
        f.write(html)
    print(f'✅ 报告已生成: {args.output}')

    # 终端摘要
    print()
    print('=' * 60)
    print('📊 摘要')
    print('=' * 60)

    groups = defaultdict(list)
    for t in results['trades']:
        groups[(t['top_n'], t['hold_days'])].append(t)

    for top_n in TOP_N_VALUES:
        print(f'\n--- Top {top_n} ---')
        for hd in HOLD_DAYS:
            key = (top_n, hd)
            group = groups.get(key, [])
            if group:
                s = compute_stats(group)
                print(f'  {hd:2d}日 | 笔:{s["trades"]:4d} | 胜率:{s["win_rate"]:5.1f}% | 均收益:{s["avg_return"]:+7.2f}% | 盈亏比:{s["profit_factor"]} | 夏普:{s["sharpe"]}')

    print(f'\n✅ 完成! 报告位置: {args.output}')
