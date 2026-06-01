#!/usr/bin/env python3
"""
P6双轨评分 全量回测 v2.0
=========================
190+监控池股票 × 2023-01 ~ 2026-05
按P6建议规则: 校准分≥60→STRONG_BUY, ≥45→BUY
持有20/30/60日对比, 止损-10%/回撤15%
"""

import sys, os, math, json, time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = os.environ.get('MYSQL_PASS', 'iXve1rVBXfdA4tL9')

from db_config import get_connection

# ==================== 配置 ====================
START_DATE = date(2023, 1, 1)
END_DATE = date(2026, 5, 29)

# P6买入阈值（校准分）
BUY_THRESHOLD = 45       # 普通买入
STROKG_BUY_THRESHOLD = 60  # 强烈买入

# 止损
STOP_LOSS_PCT = -0.10
TRAILING_STOP = -0.15

# 持有方案
HOLD_SCHEMES = [20, 30, 60]

# ==================== 核心回测 ====================

def get_season_map():
    """加载历史季节判定"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, season, raw_score, confidence
        FROM season_state WHERE index_code='MARKET'
        ORDER BY trade_date
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    
    season_map = {}
    for r in rows:
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        if td < START_DATE: continue
        season_map[td] = {
            'season': r['season'],
            'raw_score': float(r['raw_score'] or 0),
            'confidence': float(r['confidence'] or 0.5),
        }
    return season_map


def get_score_on_date(ts_code, target_date, kline_cache=None):
    """获取某日P6双轨评分"""
    # 从缓存取K线
    if kline_cache is not None and ts_code in kline_cache:
        rows = kline_cache[ts_code]
    else:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, close, high, low, vol
            FROM daily_kline_qfq
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date ASC
        """, (ts_code, target_date))
        rows = cur.fetchall()
        cur.close(); conn.close()
        if kline_cache is not None:
            kline_cache[ts_code] = rows
    
    if len(rows) < 120:
        return None
    
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r['vol'] or 0) for r in rows]
    
    # === 动量评分（简化版：基于缠论趋势×0.7 + 动量×0.3）===
    n = len(closes)
    latest = closes[-1]
    
    # 趋势分：MA位置判断
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    ma120 = sum(closes[-120:]) / 120
    
    trend_score = 50
    if latest > ma20 and ma20 > ma60:
        trend_score = 65
    elif latest > ma20:
        trend_score = 55
    elif latest > ma60:
        trend_score = 45
    else:
        trend_score = 35
    
    # 买卖点模拟（基于均线交叉）
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    if ma5 > ma10 and closes[-1] > closes[-2]:
        trend_score += 10  # 短期金叉+上涨
    elif ma5 < ma10 and closes[-1] < closes[-2]:
        trend_score -= 10  # 死叉+下跌
    
    # 动量分
    r5 = (closes[-1] - closes[-6]) / closes[-6] if n >= 6 else 0
    r10 = (closes[-1] - closes[-11]) / closes[-11] if n >= 11 else 0
    r20 = (closes[-1] - closes[-21]) / closes[-21] if n >= 21 else 0
    
    momentum = 50
    momentum += max(-15, min(15, r5 * 150))
    momentum += max(-10, min(10, r10 * 80))
    momentum += max(-8, min(8, r20 * 50))
    
    raw_score = trend_score * 0.70 + momentum * 0.30
    raw_score = max(0, min(100, raw_score))
    
    # === 百分位校准（简化） ===
    # 用固定映射
    if raw_score >= 85: cal_score = 70
    elif raw_score >= 75: cal_score = 55
    elif raw_score >= 65: cal_score = 42
    elif raw_score >= 55: cal_score = 32
    elif raw_score >= 45: cal_score = 25
    elif raw_score >= 35: cal_score = 18
    else: cal_score = 10
    
    return {
        'raw_score': raw_score,
        'calibrated_score': cal_score,
        'trend_score': trend_score,
        'momentum_score': momentum,
    }


def run_backtest(ts_codes, season_map, hold_days):
    """运行回测"""
    all_dates = sorted([d for d in season_map.keys() if d >= START_DATE])
    print(f"  区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}交易日)")
    
    # 预加载所有K线
    print("  预加载K线数据...")
    kline_cache = {}
    conn = get_connection()
    cur = conn.cursor()
    for code in ts_codes:
        cur.execute("""
            SELECT trade_date, close, high, low, vol
            FROM daily_kline_qfq
            WHERE ts_code=%s
            ORDER BY trade_date ASC
        """, (code,))
        rows = cur.fetchall()
        if len(rows) >= 120:
            kline_cache[code] = rows
    cur.close(); conn.close()
    print(f"  加载完成: {len(kline_cache)}/{len(ts_codes)}只满足数据要求")
    
    trades = []
    positions = {}
    
    for i, today in enumerate(all_dates):
        if i % 100 == 0:
            print(f"    {i}/{len(all_dates)}...", end='\r')
        
        season_info = season_map[today]
        
        # 检查持仓止损
        to_close = []
        for code, pos in positions.items():
            # 从缓存取收盘价
            if code not in kline_cache:
                continue
            rows = kline_cache[code]
            price = None
            for r in rows:
                if str(r['trade_date']) == str(today):
                    price = float(r['close'])
                    break
            if price is None:
                continue
            if price is None:
                continue
            
            ret = (price - pos['buy_price']) / pos['buy_price']
            
            # 止损
            if ret <= STOP_LOSS_PCT:
                to_close.append((code, ret, 'stop_loss'))
            # 移动止盈
            elif pos.get('peak_price', pos['buy_price']) and (price - pos['peak_price']) / pos['peak_price'] <= TRAILING_STOP:
                to_close.append((code, ret, 'trailing_stop'))
            else:
                # 更新峰值
                if price > pos.get('peak_price', 0):
                    pos['peak_price'] = price
                
                # 检查持有到期
                days_held = (today - pos['buy_date']).days
                if days_held >= hold_days:
                    to_close.append((code, ret, 'time_exit'))
        
        for code, ret, reason in to_close:
            pos = positions.pop(code)
            trades.append({
                'ts_code': code,
                'buy_date': str(pos['buy_date']),
                'sell_date': str(today),
                'hold_days': (today - pos['buy_date']).days,
                'return': ret,
                'score': pos['score'],
                'season': pos['season'],
                'exit_reason': reason,
            })
        
        # 新买入
        for code in ts_codes:
            if code in positions:
                continue
            if len(positions) >= 10:
                break
            
            score = get_score_on_date(code, today, kline_cache)
            if score is None:
                continue
            
            cal = score['calibrated_score']
            if cal < BUY_THRESHOLD:
                continue
            
            # 从缓存取收盘价
            rows = kline_cache.get(code)
            if not rows:
                continue
            price = None
            for r in rows:
                if str(r['trade_date']) == str(today):
                    price = float(r['close'])
                    break
            if price is None:
                continue
            
            positions[code] = {
                'buy_date': today,
                'buy_price': price,
                'peak_price': price,
                'score': cal,
                'season': season_info['season'],
            }
    
    # 平掉剩余持仓
    today = all_dates[-1]
    for code, pos in positions.items():
        rows = kline_cache.get(code)
        price = pos['buy_price']
        if rows:
            for r in rows:
                if str(r['trade_date']) == str(today):
                    price = float(r['close'])
                    break
        ret = (price - pos['buy_price']) / pos['buy_price']
        trades.append({
            'ts_code': code,
            'buy_date': str(pos['buy_date']),
            'sell_date': str(today),
            'hold_days': (today - pos['buy_date']).days,
            'return': ret,
            'score': pos['score'],
            'season': pos['season'],
            'exit_reason': 'end',
        })
    
    return trades


def calc_stats(trades):
    """计算统计指标"""
    if not trades:
        return {'total_trades': 0, 'win_rate': 0, 'avg_return': 0}
    
    wins = [t for t in trades if t['return'] > 0]
    losses = [t for t in trades if t['return'] <= 0]
    
    win_rate = len(wins) / len(trades) * 100
    avg_return = sum(t['return'] for t in trades) / len(trades) * 100
    avg_win = sum(t['return'] for t in wins) / len(wins) * 100 if wins else 0
    avg_loss = sum(t['return'] for t in losses) / len(losses) * 100 if losses else 0
    
    total_gain = sum(t['return'] for t in wins) if wins else 0
    total_loss = abs(sum(t['return'] for t in losses)) if losses else 1
    profit_factor = total_gain / total_loss if total_loss > 0 else float('inf')
    
    # 分持有天数
    days_stats = {}
    for days in [10, 20, 30, 60]:
        group = [t for t in trades if t['hold_days'] <= days]
        if group:
            wins_g = [t for t in group if t['return'] > 0]
            days_stats[f'≤{days}日'] = {
                'cnt': len(group),
                'win_rate': len(wins_g) / len(group) * 100,
                'avg_return': sum(t['return'] for t in group) / len(group) * 100,
            }
    
    # 分季节
    season_stats = {}
    for s in ['spring', 'summer', 'autumn', 'winter', 'chaos', 'chaos_spring']:
        group = [t for t in trades if t.get('season') == s]
        if group:
            wins_s = [t for t in group if t['return'] > 0]
            season_stats[s] = {
                'cnt': len(group),
                'win_rate': len(wins_s) / len(group) * 100,
                'avg_return': sum(t['return'] for t in group) / len(group) * 100,
            }
    
    return {
        'total_trades': len(trades),
        'win_rate': round(win_rate, 1),
        'avg_return': round(avg_return, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'total_return_pct': round(sum(t['return'] for t in trades) / len(trades) * 100, 2),
        'by_days': days_stats,
        'by_season': season_stats,
    }


def print_report(stats, hold_days, label=''):
    """打印报告"""
    print(f"\n{'='*60}")
    print(f"📊 P6回测{label} | 持有{hold_days}日")
    print(f"{'='*60}")
    print(f"总交易: {stats['total_trades']}笔")
    print(f"胜率: {stats['win_rate']}%")
    print(f"平均收益: {stats['avg_return']:+.2f}%")
    print(f"平均盈利: {stats['avg_win']:+.2f}%")
    print(f"平均亏损: {stats['avg_loss']:+.2f}%")
    print(f"盈亏比: {stats['profit_factor']}")
    print()
    print("--- 按持有天数 ---")
    for k, v in sorted(stats['by_days'].items()):
        print(f"  {k}: {v['cnt']}笔 胜率{v['win_rate']:.1f}% 均收益{v['avg_return']:+.2f}%")
    print()
    print("--- 按季节 ---")
    for s, season_name in [('spring','🌸春季'),('summer','☀️夏季'),('chaos_spring','🌤️弱春'),
                           ('autumn','🍂秋季'),('chaos','🌪️混沌'),('winter','❄️冬季')]:
        if s in stats['by_season']:
            v = stats['by_season'][s]
            print(f"  {season_name}: {v['cnt']}笔 胜率{v['win_rate']:.1f}% 均收益{v['avg_return']:+.2f}%")


def generate_html(all_results):
    """生成HTML报告"""
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>P6全量回测报告</title>
<style>
body{background:#0f172a;color:#e2e8f0;font-family:system-ui;padding:30px;max-width:1200px;margin:auto}
h1{color:#3b82f6;border-bottom:2px solid #1e2d4a;padding-bottom:10px}
h2{color:#94a3b8;margin-top:30px}
table{width:100%;border-collapse:collapse;margin:12px 0}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #1e2d4a;font-size:13px}
th{background:#1e2d4a;color:#94a3b8;font-weight:600}
.card{background:#1e293b;border:1px solid #1e2d4a;border-radius:8px;padding:16px;margin:12px 0}
.card-title{font-size:15px;color:#3b82f6;font-weight:600;margin-bottom:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.metric{background:#1e293b;border:1px solid #1e2d4a;border-radius:8px;padding:12px;text-align:center}
.metric-label{font-size:11px;color:#64748b;margin-bottom:4px}
.metric-value{font-size:20px;font-weight:700;color:#e2e8f0}
.win{color:#22c55e}.loss{color:#ef4444}
.best{background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3)}
</style></head><body>
<h1>📊 P6双轨评分 全量回测报告</h1>
<p>回测区间: 2023-01-01 ~ 2026-05-29 | 监控池股票</p>
"""
    
    for hd in HOLD_SCHEMES:
        key = f'hold_{hd}'
        if key not in all_results: continue
        s = all_results[key]['stats']
        
        html += f"<h2>📆 持有{hd}日</h2>"
        html += '<div class="grid">'
        for label, val in [('总交易', f'{s["total_trades"]}笔'), ('胜率', f'{s["win_rate"]}%'),
                           ('均收益', f'{s["avg_return"]:+.2f}%'), ('盈亏比', f'{s["profit_factor"]}')]:
            cls = 'win' if '%' in val and '+' in val else ''
            html += f'<div class="metric"><div class="metric-label">{label}</div><div class="metric-value {cls}">{val}</div></div>'
        html += '</div>'
        
        # 分持有天数
        html += '<div class="card"><div class="card-title">📈 按持有天数</div><table><tr><th>区间</th><th>笔数</th><th>胜率</th><th>均收益</th></tr>'
        for k, v in sorted(s['by_days'].items()):
            cls = 'win' if v['avg_return'] > 0 else 'loss'
            html += f'<tr><td>{k}</td><td>{v["cnt"]}</td><td>{v["win_rate"]:.1f}%</td><td class="{cls}">{v["avg_return"]:+.2f}%</td></tr>'
        html += '</table></div>'
        
        # 分季节
        html += '<div class="card"><div class="card-title">🍂 按季节</div><table><tr><th>季节</th><th>笔数</th><th>胜率</th><th>均收益</th></tr>'
        for s_name, label in [('spring','🌸春季'),('summer','☀️夏季'),('chaos_spring','🌤️弱春'),
                              ('autumn','🍂秋季'),('chaos','🌪️混沌'),('winter','❄️冬季')]:
            if s_name in s['by_season']:
                v = s['by_season'][s_name]
                cls = 'win' if v['avg_return'] > 0 else 'loss'
                html += f'<tr><td>{label}</td><td>{v["cnt"]}</td><td>{v["win_rate"]:.1f}%</td><td class="{cls}">{v["avg_return"]:+.2f}%</td></tr>'
        html += '</table></div>'
        
        # 前20笔交易
        html += '<div class="card"><div class="card-title">🏆 Top 20 交易</div><table><tr><th>#</th><th>股票</th><th>买入</th><th>卖出</th><th>持有</th><th>评分</th><th>季节</th><th>收益</th></tr>'
        top_trades = sorted(all_results[key]['trades'], key=lambda x: x['return'], reverse=True)[:20]
        for i, t in enumerate(top_trades):
            cls = 'win' if t['return'] > 0 else 'loss'
            html += f'<tr><td>{i+1}</td><td>{t["ts_code"]}</td><td>{t["buy_date"][:10]}</td><td>{t["sell_date"][:10]}</td><td>{t["hold_days"]}d</td><td>{t["score"]:.0f}</td><td>{t["season"]}</td><td class="{cls}">{t["return"]*100:+.2f}%</td></tr>'
        html += '</table></div>'
    
    # 三方案对比
    html += '<h2>⚖️ 三种持有方案对比</h2>'
    html += '<div class="card"><table><tr><th>方案</th><th>笔数</th><th>胜率</th><th>均收益</th><th>盈亏比</th></tr>'
    best_ratio = 0
    best_key = ''
    for hd in HOLD_SCHEMES:
        key = f'hold_{hd}'
        if key not in all_results: continue
        s = all_results[key]['stats']
        cls = 'best' if s['profit_factor'] > best_ratio else ''
        if s['profit_factor'] > best_ratio:
            best_ratio = s['profit_factor']
            best_key = f'持有{hd}日'
        html += f'<tr class="{cls}"><td>持有{hd}日</td><td>{s["total_trades"]}</td><td>{s["win_rate"]}%</td><td>{s["avg_return"]:+.2f}%</td><td>{s["profit_factor"]}</td></tr>'
    html += '</table>'
    html += f'<p style="margin-top:12px;color:#22c55e">🏆 最优方案: <strong>{best_key}</strong></p></div>'
    
    html += '</body></html>'
    
    path = '/tmp/p6_full_backtest_report.html'
    with open(path, 'w') as f:
        f.write(html)
    print(f'\n📊 报告已保存: {path}')
    return path


# ==================== 主入口 ====================
if __name__ == '__main__':
    import multiprocessing
    
    print("=" * 60)
    print("🚀 P6双轨评分 全量回测启动")
    print(f"   持有方案: {HOLD_SCHEMES}")
    print(f"   买入阈值: ≥{BUY_THRESHOLD}")
    print("=" * 60)
    
    # 获取监控池
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    ts_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    print(f"\n📋 监控池: {len(ts_codes)}只股票")
    
    # 加载季节数据
    print("\n🌤️ 加载历史季节...")
    season_map = get_season_map()
    print(f"   交易日: {len(season_map)}天")
    
    all_results = {}
    
    for hold_days in HOLD_SCHEMES:
        print(f"\n{'='*60}")
        print(f"📆 回测: 持有{hold_days}日")
        print(f"{'='*60}")
        
        start = time.time()
        trades = run_backtest(ts_codes, season_map, hold_days)
        elapsed = time.time() - start
        
        stats = calc_stats(trades)
        print_report(stats, hold_days, f'({elapsed:.0f}s)')
        
        all_results[f'hold_{hold_days}'] = {
            'trades': trades,
            'stats': stats,
        }
    
    # 生成HTML报告
    path = generate_html(all_results)
