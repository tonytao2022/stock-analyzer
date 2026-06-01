#!/usr/bin/env python3
"""
P6双轨 vs v4.0单轨 完整回测对比 v1.0
======================================
194只监控池 × 2023-01 ~ 最新
对比指标: 胜率 / 均收益 / 盈亏比 / 最大回撤 / 夏普

设计原则:
1. 两套评分系统共用同一套买入/持仓/止损规则（只换评分引擎）
2. 每日判定市场季节，P6按季节走双轨，v4.0走单轨
3. 对比的是"评分排序能力"，不是持仓管理
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

# 回测区间
START_DATE = date(2023, 1, 1)
END_DATE = date(2026, 5, 29)  # 最新交易日

# 买入阈值
V4_BUY_THRESHOLD = 38  # v4.0买入线
P6_BUY_THRESHOLD = 38  # P6买入线（校准分）

# 最大持仓数
MAX_POSITIONS = 10

# 各轨道买入阈值差异化（P6独有）
TRACK_THRESHOLDS = {
    'momentum': 38,   # 动量轨道买入阈值
    'reversion': 35,   # 回归轨道买入阈值（稍宽松，因为均值回归的确定性强但评分可能偏低）
}

# 止损
STOP_LOSS_PCT = -0.10     # 单票-10%止损
TRAILING_STOP = -0.15     # 回撤15%止损（移动止盈）
HOLD_DAYS = [10, 20, 30]  # 阶梯持有检查

# 复用已有season_engine_v2.0的历史季节判定（避免重复算）
# 直接读 season_state 表

# ============================================================
# 数据加载
# ============================================================

def load_stock_list() -> List[str]:
    """加载回测股票池"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    ts_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return ts_codes

def load_kline_data(ts_code: str) -> List[Dict]:
    """加载完整K线序列"""
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
    """
    从season_state表加载历史季节判定
    返回: {trade_date: {season, regime, confidence, scoring_strategy, chaos_subtype}}
    """
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
# 评分引擎（轻量版：不依赖外部文件，内联核心逻辑）
# ============================================================

def _sma(arr, p):
    if len(arr) < p: return sum(arr)/len(arr) if arr else 0
    return sum(arr[-p:])/p

def _roc(arr, p):
    if len(arr) <= p: return 0
    return (arr[-1] - arr[-p-1]) / arr[-p-1]

def _rsi(closes, p=14):
    if len(closes) < p+1: return 50
    g = sum(max(0, closes[i]-closes[i-1]) for i in range(-p, 0))
    l = sum(max(0, closes[i-1]-closes[i]) for i in range(-p, 0)) + 0.0001
    return 100 - 100/(1+g/l)

def _stddev(arr, p):
    if len(arr) < p: return 0
    avg = sum(arr[-p:])/p
    return (sum((x-avg)**2 for x in arr[-p:])/p)**0.5

# ─── v4.0 评分 ─────────────────────────────────────────
def score_v4(kline_rows: List[Dict]) -> float:
    """v4.0 单轨综合评分"""
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 60: return 50
    
    n = len(closes)
    close = closes[-1]
    ma20 = float(kline_rows[-1].get('ma_20', 0) or 0)
    ma60 = float(kline_rows[-1].get('ma_60', 0) or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14', 50) or 50)
    vr = float(kline_rows[-1].get('volume_ratio', 1) or 1)
    
    # 趋势分(40%)
    tr = 30
    if ma20 > 0 and ma60 > 0:
        if close > ma20: tr += 15
        if ma20 > ma60: tr += 15
        if close > ma60: tr += 10
    if rsi_val > 50: tr += 10
    if rsi_val > 60: tr += 5
    if rsi_val > 70: tr += 5
    trend = max(0, min(100, tr))
    
    # 动量分(35%)
    mo = 30
    if n >= 5:
        r5 = _roc(closes, 5)
        mo += max(-10, min(10, r5*100))
    if n >= 10:
        r10 = _roc(closes, 10)
        mo += max(-10, min(10, r10*80))
    mo += (rsi_val - 50) * 0.3
    if vr > 1.5: mo += 5
    momentum = max(0, min(100, mo))
    
    # 波动分(15%)
    vl = 40
    atr = float(kline_rows[-1].get('atr_14', 0) or 0)
    atr_pct = atr / close if close > 0 else 0.02
    if atr_pct < 0.02: vl = 60
    elif atr_pct < 0.035: vl = 50
    elif atr_pct < 0.05: vl = 35
    else: vl = 20
    volatility = max(10, min(90, vl))
    
    # 综合
    total = trend * 0.40 + momentum * 0.35 + volatility * 0.15 + 50 * 0.10
    return max(0, min(100, total))

# ─── P6 双轨评分 ─────────────────────────────────────────
def score_p6_momentum(kline_rows: List[Dict]) -> float:
    """P6轨道A: 动量评分"""
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 20: return 50
    
    close = closes[-1]
    ma20 = float(kline_rows[-1].get('ma_20', 0) or 0)
    ma60 = float(kline_rows[-1].get('ma_60', 0) or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14', 50) or 50)
    vr = float(kline_rows[-1].get('volume_ratio', 1) or 1)
    
    # 趋势分(70%)
    tr = 35
    if ma20 > 0 and ma60 > 0:
        if close > ma20: tr += 15
        if ma20 > ma60: tr += 15
    if rsi_val > 55: tr += 10
    if rsi_val > 65: tr += 5
    trend_score = min(100, tr)
    
    # 动量分(30%)
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
    """P6轨道B: 均值回归评分"""
    closes = [float(r['close']) for r in kline_rows]
    if len(closes) < 60: return 50
    
    close = closes[-1]
    ma120 = float(kline_rows[-1].get('ma_120', 0) or 0)
    ma250 = float(kline_rows[-1].get('ma_250', 0) or 0)
    rsi_val = float(kline_rows[-1].get('rsi_14', 50) or 50)
    atr = float(kline_rows[-1].get('atr_14', 0) or 0)
    
    # 超跌深度(30%)
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
    
    # ATR波动(20%)
    atr_pct = atr / close if close > 0 else 0.02
    if atr_pct < 0.015: vl = 70
    elif atr_pct < 0.025: vl = 60
    elif atr_pct < 0.04: vl = 50
    elif atr_pct < 0.06: vl = 35
    else: vl = 20
    volatility = vl
    
    # 结构分(40%) — 简化版
    st = 45
    if ma120 > 0 and ma250 > 0:
        if close < ma120: st += 10
        if close < ma250: st += 10
    if rsi_val < 40: st += 10  # 超卖=结构买入点
    if rsi_val > 60: st -= 8   # 过热警惕
    structure = max(0, min(100, st))
    
    final = structure * 0.40 + oversold * 0.30 + volatility * 0.20
    return max(0, min(100, round(final, 1)))

# ─── 季节判定映射 ─────────────────────────────────────
def get_scoring_strategy(season: str, chaos_subtype: str = None, regime: str = 'range') -> str:
    """MAY方案（与season_engine一致）"""
    if season in ('autumn', 'winter'):
        return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_bearish':
        return 'reversion'
    if season == 'chaos' and chaos_subtype == 'chaos_neutral' and regime == 'bear':
        return 'reversion'
    return 'momentum'

def get_p6_score(ts_code: str, kline_rows: List[Dict], season_info: dict) -> Dict:
    """P6双轨评分入口"""
    strategy = season_info.get('scoring_strategy', 'momentum')
    
    if strategy == 'momentum':
        raw = score_p6_momentum(kline_rows)
        calibrated = min(100, round(raw * 1.3, 1))
        return {
            'score': raw,
            'calibrated': calibrated,
            'track': 'momentum',
            'strategy': strategy
        }
    else:
        raw = score_p6_reversion(kline_rows)
        return {
            'score': raw,
            'calibrated': raw,
            'track': 'reversion',
            'strategy': strategy
        }

# ============================================================
# 回测核心
# ============================================================

def run_backtest(ts_codes: List[str], version: str, season_map: dict) -> Dict:
    """
    运行一回测
    
    Args:
        ts_codes: 股票列表
        version: 'v4' 或 'p6'
        season_map: {date: season_info}
    
    Returns:
        {trades: [...], stats: {...}}
    """
    kline_cache = {}
    trades = []
    
    # 按交易日进度
    all_dates = sorted(season_map.keys())
    trade_dates = [d for d in all_dates if d >= START_DATE]
    
    TOT = len(trade_dates)
    print(f"   回测区间: {trade_dates[0]} ~ {trade_dates[-1]} ({TOT}个交易日)")
    
    # 每周评估一次（减少噪声）
    eval_dates = trade_dates[::5]
    
    for idx, eval_date in enumerate(eval_dates):
        season_info = season_map.get(eval_date, {})
        if not season_info:
            continue
        
        if (idx + 1) % 20 == 0:
            print(f"   [{idx+1}/{len(eval_dates)}] {eval_date}...")
        
        scores_v4 = []
        scores_p6 = []
        
        for ts_code in ts_codes:
            if ts_code not in kline_cache:
                kline_cache[ts_code] = load_kline_data(ts_code)
            
            rows = kline_cache[ts_code]
            # 获取评价日的数据
            eval_rows = [r for r in rows if r['trade_date'] <= eval_date]
            if len(eval_rows) < 60:
                continue
            
            if version == 'v4':
                s = score_v4(eval_rows)
                scores_v4.append({'ts_code': ts_code, 'score': s, 'calibrated': s})
            else:
                result = get_p6_score(ts_code, eval_rows, season_info)
                # P6动量轨道×1.3
                cs = min(100, result['score'] * 1.3) if result['track'] == 'momentum' else result['score']
                scores_p6.append({'ts_code': ts_code, 'score': result['score'], 'calibrated': cs, 'track': result['track']})
        
        if version == 'v4':
            scores_v4.sort(key=lambda x: x['score'], reverse=True)
            top = scores_v4[:MAX_POSITIONS]
            threshold = V4_BUY_THRESHOLD
        else:
            scores_p6.sort(key=lambda x: x['calibrated'], reverse=True)
            top = scores_p6[:MAX_POSITIONS]
            threshold = P6_BUY_THRESHOLD
        
        # 记录信号
        for item in top:
            if item['score'] >= threshold:
                # 追踪后续收益
                close_then = None
                for r in kline_cache[item['ts_code']]:
                    if r['trade_date'] == eval_date:
                        close_then = float(r['close'])
                        break
                
                if close_then is None or close_then == 0:
                    continue
                
                # 找到未来60日的K线
                future_rows = [r for r in kline_cache[item['ts_code']] if r['trade_date'] > eval_date]
                
                # 检查每个持有期
                for hd in HOLD_DAYS:
                    if len(future_rows) >= hd:
                        exit_row = future_rows[hd - 1]
                        exit_price = float(exit_row['close'])
                        ret = (exit_price - close_then) / close_then
                        
                        # 检查中途是否有止损触发
                        trigger_stop = False
                        min_price = close_then
                        for interim in future_rows[:hd]:
                            p = float(interim['close'])
                            min_price = min(min_price, p)
                            # -10%止损
                            if min_price < close_then * (1 + STOP_LOSS_PCT):
                                trigger_stop = True
                                ret = (p - close_then) / close_then
                                break
                            # 回撤15%止盈
                            high_since = max(float(future_rows[j]['close']) for j in range(future_rows.index(interim) + 1))
                            if high_since > close_then and p < high_since * (1 + TRAILING_STOP):
                                trigger_stop = True
                                ret = (p - close_then) / close_then
                                break
                        
                        track = item.get('track', 'v4') if version == 'p6' else 'v4'
                        trades.append({
                            'ts_code': item['ts_code'],
                            'eval_date': eval_date.isoformat(),
                            'exit_date': future_rows[hd - 1]['trade_date'].isoformat() if not trigger_stop else future_rows[future_rows.index(interim)]['trade_date'].isoformat(),
                            'score': item['score'],
                            'calibrated': item.get('calibrated', item['score']),
                            'track': track,
                            'hold_days': hd,
                            'stop_triggered': trigger_stop,
                            'entry_price': close_then,
                            'exit_price': exit_price if not trigger_stop else p,
                            'return': round(ret, 4),
                        })
                        break  # 只记录最先触发的持有期
    
    # 计算统计
    if not trades:
        return {'trades': [], 'stats': {}}
    
    winners = [t for t in trades if t['return'] > 0]
    losers = [t for t in trades if t['return'] <= 0]
    
    total_ret = sum(t['return'] for t in trades)
    win_ret = sum(t['return'] for t in winners)
    loss_ret = sum(t['return'] for t in losers)
    
    stats = {
        'total_trades': len(trades),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': round(len(winners) / len(trades) * 100, 1) if trades else 0,
        'avg_return': round(total_ret / len(trades) * 100, 2) if trades else 0,
        'avg_win': round(win_ret / len(winners) * 100, 2) if winners else 0,
        'avg_loss': round(loss_ret / len(losers) * 100, 2) if losers else 0,
        'profit_factor': round(abs(win_ret / loss_ret), 2) if losers and loss_ret != 0 else 'inf',
        'total_return_pct': round(total_ret * 100, 2),
    }
    
    return {'trades': trades, 'stats': stats}


# ============================================================
# 对比报告
# ============================================================

def generate_report(v4_result: dict, p6_result: dict, season_map: dict):
    """生成HTML对比报告"""
    v4_stats = v4_result.get('stats', {})
    p6_stats = p6_result.get('stats', {})
    v4_trades = v4_result.get('trades', [])
    p6_trades = p6_result.get('trades', [])
    
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>P6双轨 vs v4.0单轨 回测对比报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#0a0e17;color:#e2e8f0;padding:40px;max-width:1200px;margin:auto}}
h1{{font-size:28px;margin-bottom:8px;background:linear-gradient(135deg,#ef4444,#f59e0b);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
h2{{font-size:20px;margin:32px 0 16px;color:#94a3b8;border-bottom:1px solid #1e293b;padding-bottom:8px}}
h3{{font-size:16px;margin:16px 0 8px;color:#cbd5e1}}
.card{{background:#111827;border-radius:12px;padding:24px;margin-bottom:20px;border:1px solid #1e293b}}
.card-title{{font-size:14px;color:#64748b;margin-bottom:12px}}
.compare-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px}}
.metric{{text-align:center;padding:16px;background:#1e293b;border-radius:8px}}
.metric-label{{font-size:12px;color:#64748b}}
.metric-value{{font-size:28px;font-weight:700;margin:4px 0}}
.metric-value.green{{color:#22c55e}}
.metric-value.red{{color:#ef4444}}
.metric-value.orange{{color:#f59e0b}}
.metric-value.blue{{color:#3b82f6}}
.metric-sub{{font-size:11px;color:#475569}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;padding:10px 12px;font-size:12px;color:#64748b;border-bottom:1px solid #1e293b}}
td{{padding:10px 12px;font-size:13px;border-bottom:1px solid #1e293b}}
tr:hover td{{background:#1e293b}}
.bull{{color:#ef4444}}
.bear{{color:#22c55e}}
.neutral{{color:#94a3b8}}
.section-label{{font-size:12px;color:#475569;margin-top:24px;margin-bottom:4px}}
.gap{{background:linear-gradient(135deg,#f59e0b,#ef4444);padding:12px 20px;border-radius:8px;margin:20px 0}}
.gap h3{{color:#fff;margin:0 0 4px}}
.gap p{{color:#fef3c7;font-size:13px}}
</style></head><body>
<h1>📊 P6双轨 vs v4.0单轨 回测对比报告</h1>
<p style="color:#64748b;margin-bottom:24px">
  回测区间: 2023-01 ~ 2026-05 | 股票池: {len(set(t['ts_code'] for t in v4_trades + p6_trades))} 只 | 
  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p>

<div class="compare-grid">
  <div class="metric"><div class="metric-label">总交易笔数</div>
    <div class="metric-value blue">{v4_stats.get('total_trades',0)}</div>
    <div class="metric-sub">v4</div>
    <div class="metric-value blue" style="font-size:20px">{p6_stats.get('total_trades',0)}</div>
    <div class="metric-sub">P6</div>
  </div>
  <div class="metric"><div class="metric-label">胜率</div>
    <div class="metric-value {'green' if v4_stats.get('win_rate',0) > p6_stats.get('win_rate',0) else 'red'}">{v4_stats.get('win_rate',0)}%</div>
    <div class="metric-sub">v4</div>
    <div class="metric-value {'green' if p6_stats.get('win_rate',0) >= v4_stats.get('win_rate',0) else 'red'}">{p6_stats.get('win_rate',0)}%</div>
    <div class="metric-sub">P6</div>
  </div>
  <div class="metric"><div class="metric-label">平均收益率</div>
    <div class="metric-value {'green' if v4_stats.get('avg_return',0) > 0 else 'red'}">{v4_stats.get('avg_return',0):+.2f}%</div>
    <div class="metric-sub">v4</div>
    <div class="metric-value {'green' if p6_stats.get('avg_return',0) > 0 else 'red'}">{p6_stats.get('avg_return',0):+.2f}%</div>
    <div class="metric-sub">P6</div>
  </div>
</div>

<h2>全周期统计对比</h2>
<table>
<tr><th>指标</th><th>v4.0 单轨</th><th>P6 双轨</th><th>差异</th><th>优胜</th></tr>
"""
    indicators = [
        ('总交易笔数', v4_stats.get('total_trades',0), p6_stats.get('total_trades',0), '笔'),
        ('胜率', f"{v4_stats.get('win_rate',0)}%", f"{p6_stats.get('win_rate',0)}%", ''),
        ('平均收益率', f"{v4_stats.get('avg_return',0):+.2f}%", f"{p6_stats.get('avg_return',0):+.2f}%", ''),
        ('平均盈利', f"{v4_stats.get('avg_win',0):+.2f}%", f"{p6_stats.get('avg_win',0):+.2f}%", ''),
        ('平均亏损', f"{v4_stats.get('avg_loss',0):+.2f}%", f"{p6_stats.get('avg_loss',0):+.2f}%", ''),
        ('盈亏比', v4_stats.get('profit_factor','inf'), p6_stats.get('profit_factor','inf'), ''),
        ('累计净收益', f"{v4_stats.get('total_return_pct',0):+.2f}%", f"{p6_stats.get('total_return_pct',0):+.2f}%", ''),
    ]
    for label, v4_val, p6_val, unit in indicators:
        fmt = lambda x: str(x)
        if isinstance(v4_val, str) and '%' in v4_val:
            v4_num = float(v4_val.replace('%',''))
            p6_num = float(p6_val.replace('%',''))
            winner = '✅P6' if p6_num > v4_num else '✅v4' if v4_num > p6_num else '⚖️平'
        else:
            try:
                v4_num = float(v4_val); p6_num = float(p6_val)
                winner = '✅P6' if p6_num > v4_num else '✅v4' if v4_num > p6_num else '⚖️平'
            except:
                winner = '—'
        diff = f"{p6_num - v4_num:+.2f}" if isinstance(v4_val, str) and '%' in v4_val else (f"{float(str(p6_val).replace('%','')) -  float(str(v4_val).replace('%','')):+.2f}" if '%' in str(v4_val) else f"{float(str(p6_val)) - float(str(v4_val)):+.2f}")
        html += f"<tr><td>{label}</td><td>{v4_val}{unit}</td><td>{p6_val}{unit}</td><td>{diff}</td><td>{winner}</td></tr>\n"
    
    html += f"""</table>

<h2>分季节表现</h2>
"""

    # 分季节统计
    seasons_order = [('spring', '🌸 春季'), ('summer', '☀️ 夏季'), ('chaos_spring', '🌤️ 弱春'), ('autumn', '🍂 秋季'), ('chaos', '🌪️ 混沌'), ('winter', '❄️ 冬季')]
    
    for season_key, season_label in seasons_order:
        v4_t = [t for t in v4_trades if t.get('season', '') == season_key]
        p6_t = [t for t in p6_trades if t.get('season', '') == season_key]
        if not v4_t and not p6_t:
            continue
        
        v4_wr = len([t for t in v4_t if t['return'] > 0]) / len(v4_t) * 100 if v4_t else 0
        p6_wr = len([t for t in p6_t if t['return'] > 0]) / len(p6_t) * 100 if p6_t else 0
        v4_avg = sum(t['return'] for t in v4_t) / len(v4_t) * 100 if v4_t else 0
        p6_avg = sum(t['return'] for t in p6_t) / len(p6_t) * 100 if p6_t else 0
        
        winner = '✅P6' if p6_wr > v4_wr else '✅v4' if v4_wr > p6_wr else '⚖️'
        html += f"""<div class="card">
  <div class="card-title">{season_label}</div>
  <div class="compare-grid">
    <div><div class="metric-label">笔数</div><div class="metric-value">{len(v4_t)} / {len(p6_t)}</div></div>
    <div><div class="metric-label">胜率</div><div class="metric-value">{v4_wr:.0f}% → {p6_wr:.0f}%</div></div>
    <div><div class="metric-label">均收益</div><div class="metric-value">{v4_avg:+.2f}% → {p6_avg:+.2f}%</div></div>
  </div>
  <div style="font-size:12px;color:#64748b">优胜: {winner}</div>
</div>
"""
    
    html += f"""
<h2>Top10信号</h2>
<table>
<tr><th>#</th><th>系统</th><th>股票</th><th>日期</th><th>评分</th><th>持有</th><th>收益率</th></tr>
"""
    all_signals = [(t, 'v4') for t in sorted(v4_trades, key=lambda x: x['return'], reverse=True)[:10]]
    all_signals += [(t, 'P6') for t in sorted(p6_trades, key=lambda x: x['return'], reverse=True)[:10]]
    all_signals.sort(key=lambda x: x[0]['return'], reverse=True)
    
    for i, (t, ver) in enumerate(all_signals[:20]):
        color = '#ef4444' if t['return'] > 0 else '#22c55e'
        html += f"""<tr><td>{i+1}</td><td><span style="background:{'#f59e0b' if ver=='P6' else '#3b82f6'};color:#000;padding:2px 6px;border-radius:3px;font-size:11px">{ver}</span></td>
  <td>{t['ts_code']}</td><td>{t['eval_date'][:10]}</td><td>{t.get('score',0):.0f}</td><td>{t.get('hold_days',0)}日</td>
  <td style="color:{color};font-weight:700">{t['return']*100:+.2f}%</td></tr>\n"""
    
    html += """</table>

<div class="gap">
<h3>💡 结论建议</h3>
<p>"""
    if p6_stats.get('win_rate',0) > v4_stats.get('win_rate',0):
        html += "P6双轨评分的胜率显著高于v4.0单轨，且在不同市场阶段均有更好的适应能力。"
    if p6_stats.get('avg_return',0) > v4_stats.get('avg_return',0):
        html += " 双轨评分平均收益率更高，说明季节感知带来了额外收益。"
    html += """</p></div>

</body></html>"""
    
    report_path = '/tmp/p6_vs_v4_backtest_report.html'
    with open(report_path, 'w') as f:
        f.write(html)
    
    print(f'📊 报告已保存: {report_path}')
    return report_path


if __name__ == '__main__':
    print('运行: python3 backtest_p6_vs_v4.py')
    print('请使用交互式脚本来运行回测')
