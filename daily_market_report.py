#!/usr/bin/env python3
"""
========================================================
  每日市场总结与持仓推荐报告 v2.0
  数据来源：MySQL stock_db（评分引擎/持仓/行情/季节判定）
  输出：HTML 邮件正文（结构化、数据驱动、零模拟数据）
========================================================
"""
import os
import sys
import json
import smtplib
import argparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from decimal import Decimal

# ---------- MySQL 连接 ----------
MYSQL_PASS = os.environ.get("MYSQL_PASS")
if not MYSQL_PASS:
    try:
        with open("/etc/mysql/debian.cnf") as f:
            for line in f:
                if "password" in line:
                    MYSQL_PASS = line.split("=")[1].strip()
                    break
    except Exception:
        MYSQL_PASS = None

import pymysql

def get_conn():
    return pymysql.connect(
        host="127.0.0.1",
        user="debian-sys-maint",
        password=MYSQL_PASS,
        database="stock_db",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

# ============================================================
#  数据采集层
# ============================================================

def fetch_season_state():
    """获取最新市场季节判定"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT trade_date, season, regime, chaos_subtype,
                       hengjiyuan_level, position_advice, confidence,
                       raw_score, hengjiyuan_score, confidence_mult
                FROM season_state
                WHERE index_code='MARKET'
                ORDER BY trade_date DESC LIMIT 1
            """)
            return cur.fetchone()
    finally:
        conn.close()


def fetch_holdings():
    """获取当前持仓"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts_code, name, qty, cost_price, current_price,
                       profit_pct, market_value, profit_amount, advice,
                       buy_date, lock_active, lock_until, stop_line
                FROM portfolio_holdings
                WHERE status='HOLDING'
                ORDER BY buy_date
            """)
            rows = cur.fetchall()
            # 修正 cost_price 异常值（OCR导入的负数等）
            for r in rows:
                cp = r["cost_price"]
                if cp is None or cp <= 0:
                    r["cost_price"] = None
                    r["profit_pct"] = None
                    r["profit_amount"] = None
                if r["current_price"] and r["cost_price"] and r["cost_price"] > 0:
                    r["profit_pct"] = round((float(r["current_price"]) - float(r["cost_price"])) / float(r["cost_price"]) * 100, 2)
                    r["profit_amount"] = round((float(r["current_price"]) - float(r["cost_price"])) * int(r["qty"]), 2)
                if r["market_value"] and r["current_price"]:
                    r["market_value"] = round(float(r["current_price"]) * int(r["qty"]), 2)
            return rows
    finally:
        conn.close()


def fetch_top_signals(latest_date=None, min_score=75, limit=10):
    """获取最新交易日的高评分LONG信号"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if not latest_date:
                cur.execute("SELECT MAX(trade_date) d FROM strategy_signal")
                row = cur.fetchone()
                latest_date = row["d"]
            cur.execute("""
                SELECT ss.ts_code, sb.name, ss.composite_score,
                       ss.direction, ss.operation_mode,
                       ss.buy_sell_point, ss.reason_chain,
                       ss.signal_confidence, ss.hengjiyuan_level
                FROM strategy_signal ss
                LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code
                WHERE ss.trade_date = %s
                  AND ss.composite_score >= %s
                  AND ss.gate_triggered = 0
                  AND (ss.direction IS NOT NULL AND ss.direction != '')
                ORDER BY ss.composite_score DESC
                LIMIT %s
            """, (latest_date, min_score, limit))
            return cur.fetchall()
    finally:
        conn.close()


def fetch_watch_pool_top(latest_date=None, limit=10):
    """获取监控池最新快照高评分"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 用多版本过滤得到最新交易日
            cur.execute("SELECT MAX(trade_date) d FROM watch_pool_snapshot")
            row = cur.fetchone()
            td = row["d"]
            cur.execute("""
                SELECT ts_code, name, v_score, change_pct, close_price,
                       signal_label, season, ret_5d, ret_10d, ret_20d
                FROM watch_pool_snapshot
                WHERE trade_date = %s
                ORDER BY v_score DESC
                LIMIT %s
            """, (td, limit))
            return cur.fetchall()
    finally:
        conn.close()


def fetch_sector_top_gainers(latest_date=None, limit=5):
    """获取当日行业板块涨幅TOP5（基于监控池+全A股聚合）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if not latest_date:
                cur.execute("SELECT MAX(trade_date) d FROM daily_kline")
                row = cur.fetchone()
                latest_date = row["d"]
            cur.execute("""
                SELECT sb.industry,
                       COUNT(*) AS stock_cnt,
                       ROUND(AVG(dk.change_pct), 2) AS avg_chg_pct,
                       ROUND(SUM(dk.amount), 0) AS total_amount
                FROM daily_kline dk
                JOIN stock_basic sb ON dk.ts_code = sb.ts_code
                WHERE dk.trade_date = %s
                  AND sb.industry IS NOT NULL AND sb.industry != ''
                GROUP BY sb.industry
                HAVING COUNT(*) >= 3
                ORDER BY avg_chg_pct DESC
                LIMIT %s
            """, (latest_date, limit))
            return cur.fetchall()
    finally:
        conn.close()


def fetch_index_snapshot():
    """获取主要指数涨跌幅"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT trade_date, season, raw_score, position_advice
                FROM season_state
                WHERE index_code='MARKET'
                ORDER BY trade_date DESC LIMIT 3
            """)
            rows = cur.fetchall()
            # 取最后一、二、三个交易日做趋势对比
            return rows
    finally:
        conn.close()


def fetch_strategy_config():
    """获取当前策略参数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT config_key, config_value, description
                FROM stock_db.system_config
                WHERE config_key LIKE 'strategy_%'
                   OR config_key LIKE 'buy_%'
                   OR config_key LIKE 'p%'
                   OR config_key LIKE 'stop_%'
                   OR config_key LIKE 'trailing%'
                   OR config_key LIKE 'cool%'
                   OR config_key LIKE 'max_hold%'
                ORDER BY config_key
            """)
            rows = cur.fetchall()
            if not rows:
                cur.execute("SELECT * FROM strategy_config ORDER BY id DESC LIMIT 5")
                rows = cur.fetchall()
            return rows
    finally:
        conn.close()


# ============================================================
#  HTML 报告生成
# ============================================================

def fmt(val, dec=2):
    """安全格式化数值"""
    if val is None:
        return "—"
    try:
        return f"{float(val):.{dec}f}"
    except (ValueError, TypeError):
        return str(val)


def fmt_pct(val):
    """百分比带符号"""
    if val is None:
        return "—"
    v = float(val)
    s = "+" if v > 0 else ""
    return f"{s}{v:.2f}%"


def fmt_price(val):
    if val is None:
        return "—"
    return f"{float(val):.3f}"


def generate_html(season, holdings, top_signals, watch_top, gainers, index_trend, strategy_cfg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    td = season["trade_date"].strftime("%Y-%m-%d") if season and season.get("trade_date") else "—"

    # ---------- 市场总览 ----------
    s = season or {}
    season_label = s.get("season", "—")
    regime = s.get("regime") or "—"
    heng = s.get("hengjiyuan_level", "—") or "—"
    position_advice = s.get("position_advice", "—") or "—"
    confidence = fmt(s.get("confidence"), 0) if s.get("confidence") else "—"
    hg_score = fmt(s.get("hengjiyuan_score"), 1) if s.get("hengjiyuan_score") else "—"

    # 季节emoji
    season_emoji = {
        "summer": "☀️", "spring": "🌸", "autumn": "🍂", "winter": "❄️",
        "chaos": "🌀", "chaos_spring": "🌱🌀", "chaos_summer": "☀️🌀",
        "chaos_autumn": "🍂🌀", "chaos_winter": "❄️🌀"
    }.get(season_label, "❓")

    # 季节中文映射
    season_cn_map = {
        "summer": "夏季", "spring": "春季", "autumn": "秋季", "winter": "冬季",
        "chaos": "混沌", "chaos_spring": "混沌·春季", "chaos_summer": "混沌·夏季",
        "chaos_autumn": "混沌·秋季", "chaos_winter": "混沌·冬季"
    }
    hengjiyuan_cn_map = {
        "strong_heng": "强恒纪元", "weak_heng": "弱恒纪元",
        "strong_luan": "强乱纪元", "weak_luan": "弱乱纪元"
    }
    op_mode_cn_map = {
        "attack": "进攻", "normal": "稳健", "defense": "防守", "dormant": "休眠"
    }
    sig_conf_cn_map = {
        "high": "高置信", "medium": "中置信", "low": "低置信"
    }
    season_cn = season_cn_map.get(season_label, season_label or "—")
    heng_cn = hengjiyuan_cn_map.get(heng, heng or "—")

    # 持仓汇总
    total_mv = sum(float(h["market_value"] or 0) for h in holdings)
    total_profit = 0
    for h in holdings:
        if h["profit_amount"] is not None:
            total_profit += float(h["profit_amount"])
    win_count = sum(1 for h in holdings if h["profit_pct"] is not None and h["profit_pct"] > 0)
    loss_count = sum(1 for h in holdings if h["profit_pct"] is not None and h["profit_pct"] < 0)

    # 指数趋势
    index_rows = ""
    for i, row in enumerate(index_trend or []):
        arrow = "🟢" if i == 0 else ("🟡" if i == 1 else "🔴")
        index_rows += f"""
        <tr>
            <td style="padding:6px 10px;text-align:center">{arrow}</td>
            <td style="padding:6px 10px">{row['trade_date'].strftime('%m-%d')}</td>
            <td style="padding:6px 10px">{season_cn_map.get(row.get('season',''), row.get('season','—'))}</td>
            <td style="padding:6px 10px;text-align:right">{fmt(row.get('raw_score'),1)}</td>
            <td style="padding:6px 10px;font-size:12px">{row.get('position_advice','—')[:30]}</td>
        </tr>"""

    # 持仓行
    holdings_rows = ""
    for h in holdings:
        pct = h["profit_pct"]
        if pct is not None and pct > 0:
            badge = "🟢"
            pct_class = "up"
        elif pct is not None and pct < 0:
            badge = "🔴"
            pct_class = "down"
        else:
            badge = "⚪"
            pct_class = ""
        lock_badge = "🔒" if h.get("lock_active") else ""
        holdings_rows += f"""
        <tr>
            <td style="padding:8px 10px"><strong>{h['name']}</strong><br><span style="font-size:11px;color:#999">{h['ts_code'][:8]}</span></td>
            <td style="padding:8px 10px;text-align:right">{int(h['qty'])}股</td>
            <td style="padding:8px 10px;text-align:right">{fmt_price(h['cost_price'])}</td>
            <td style="padding:8px 10px;text-align:right">{fmt_price(h['current_price'])}</td>
            <td style="padding:8px 10px;text-align:right;font-weight:bold" class="{pct_class}">{fmt_pct(pct)}</td>
            <td style="padding:8px 10px;text-align:right">¥{fmt(h['market_value'],0)}</td>
            <td style="padding:8px 10px;text-align:center">
                <span class="tag tag-{'buy' if '持有' in (h['advice'] or '') else ('sell' if '卖出' in (h['advice'] or '') else 'hold')}">{h['advice'] or '—'}</span>
                {lock_badge}
            </td>
            <td style="padding:8px 10px;text-align:center;font-size:13px">{h['buy_date'].strftime('%m-%d') if h.get('buy_date') else '—'}</td>
        </tr>"""

    # 理由链中文映射
    reason_cn_map = {
        "chaos": "混沌", "range": "震荡", "bull": "牛市", "bear": "熊市",
        "summer": "夏季", "spring": "春季", "autumn": "秋季", "winter": "冬季",
        "动量轨道": "动量轨道", "回归轨道": "回归轨道",
        "none": "", "秋老虎": "秋老虎",
    }
    def translate_reason(chain: str) -> str:
        if not chain:
            return "—"
        # 按固定映射替换（保留中文已有部分）
        result = chain
        result = result.replace("chaos+range", "混沌·震荡")
        result = result.replace("chaos", "混沌")
        result = result.replace("range", "震荡")
        result = result.replace("bull", "牛市")
        result = result.replace("bear", "熊市")
        result = result.replace("summer", "夏季")
        result = result.replace("spring", "春季")
        result = result.replace("autumn", "秋季")
        result = result.replace("winter", "冬季")
        result = result.replace("盘整", "盘整")
        result = result.replace("秋老虎", "·秋老虎")
        result = result.replace("结构稳定", "·结构稳定")
        result = result.replace("结构强势", "·结构强势")
        result = result.replace("动量轨道", "·动量轨道")
        result = result.replace("回归轨道", "·回归轨道")
        result = result.replace("none", "")
        # 如果以 + 开头，去掉
        if result.startswith("+"):
            result = result[1:]
        return result[:80] or "—"

    # 推荐买入 TOP
    buy_rows = ""
    for i, sig in enumerate(top_signals, 1):
        score = float(sig["composite_score"])
        bar_width = int(score)
        score_color = "#00c853" if score >= 85 else ("#ffc107" if score >= 70 else "#ff6b6b")
        reason = translate_reason(sig.get('reason_chain', ''))
        buy_rows += f"""
        <tr>
            <td style="padding:8px 10px;text-align:center;font-weight:500">{i}</td>
            <td style="padding:8px 10px"><strong style="font-size:14px;color:#1a56db">{sig['name']}</strong><br><span style="font-size:11px;color:#999">{sig['ts_code'][:8]}</span></td>
            <td style="padding:8px 10px;text-align:right;font-weight:bold;font-size:15px;color:{score_color}">{fmt(score,1)}</td>
            <td style="padding:8px 10px;min-width:100px">
                <div class="score-bar-bg">
                    <div class="score-bar-fill" style="width:{bar_width}%;background:{score_color}"></div>
                </div>
            </td>
            <td style="padding:8px 10px;text-align:center"><span class="tag tag-{'buy' if score >= 75 else 'hold'}">{op_mode_cn_map.get(sig.get('operation_mode',''), sig.get('operation_mode','—')) or '—'}</span></td>
            <td style="padding:8px 10px;font-size:12px;color:#666">{reason}</td>
        </tr>"""

    # 行业板块涨幅 Top
    gainer_rows = ""
    for i, g in enumerate(gainers, 1):
        chg = float(g["avg_chg_pct"])
        chg_class = "up" if chg > 0 else "down"
        gainer_rows += f"""
        <tr>
            <td style="padding:6px 10px;text-align:center;font-weight:500">{i}</td>
            <td style="padding:6px 10px"><strong>{g['industry']}</strong></td>
            <td style="padding:6px 10px;text-align:center">{int(g['stock_cnt'])}只</td>
            <td style="padding:6px 10px;text-align:right;font-weight:bold" class="{chg_class}">{fmt_pct(chg)}</td>
            <td style="padding:6px 10px;text-align:right">{fmt(float(g['total_amount'] or 0)/100000000,2)}亿</td>
        </tr>"""

    # 监控池 Top
    watch_rows = ""
    for i, w in enumerate(watch_top, 1):
        watch_rows += f"""
        <tr>
            <td style="padding:4px 8px;text-align:center">{i}</td>
            <td style="padding:4px 8px">{w['name']}<br><span style="font-size:11px;color:#888">{w['ts_code'][:8]}</span></td>
            <td style="padding:4px 8px;text-align:right">{fmt(w['v_score'],1)}</td>
            <td style="padding:4px 8px;text-align:right" class="{'up' if w['change_pct'] and float(w['change_pct'])>0 else 'down'}">{fmt_pct(w['change_pct'])}</td>
            <td style="padding:4px 8px">{w.get('signal_label','—') or '—'}</td>
        </tr>"""

    # 策略配置
    cfg_rows = ""
    for c in strategy_cfg:
        cfg_rows += f"""
        <tr>
            <td style="padding:3px 8px;font-size:12px">{c.get('config_key',c.get('key','—'))}</td>
            <td style="padding:3px 8px;font-size:12px">{str(c.get('config_value',c.get('value','—')))[:40]}</td>
        </tr>"""

    # ---------- 最终装配 ----------
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8">
<style>
body {{ font-family: -apple-system, 'Segoe UI', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif; background:#ffffff; color:#333333; padding:30px; max-width:820px; margin:auto; }}
h1 {{ color:#1a56db; font-size:24px; border-bottom:3px solid #1a56db; padding-bottom:10px; }}
h2 {{ color:#d97706; font-size:17px; margin-top:22px; border-left:4px solid #d97706; padding-left:12px; }}
table {{ width:100%; border-collapse:collapse; margin:8px 0; }}
th {{ background:#e8f0fe; color:#555; font-size:12px; padding:8px 10px; text-align:left; border-bottom:2px solid #d0d5dd; }}
td {{ padding:6px 10px; border-bottom:1px solid #e5e7eb; }}
.up {{ color:#dc2626; font-weight:500; }}
.down {{ color:#16a34a; font-weight:500; }}
.card {{ background:#f8fafc; border-radius:12px; padding:18px; margin:15px 0; border:1px solid #e2e8f0; }}
.big-number {{ font-size:28px; font-weight:bold; color:#1a56db; }}
.footer {{ margin-top:25px; padding:14px; background:#f9fafb; border-radius:8px; font-size:12px; color:#999; text-align:center; border-top:1px solid #e5e7eb; }}
.summary-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin:12px 0; }}
.summary-item {{ background:#f8fafc; border-radius:10px; padding:14px; text-align:center; border:1px solid #e2e8f0; }}
.summary-label {{ font-size:11px; color:#888; }}
.summary-value {{ font-size:18px; font-weight:bold; margin-top:4px; }}
.section-card {{ background:#ffffff; border-radius:12px; padding:14px; margin:12px 0; border:1px solid #e5e7eb; box-shadow:0 1px 3px rgba(0,0,0,0.04); }}
.score-bar-bg {{ height:14px; background:#e5e7eb; border-radius:7px; overflow:hidden; }}
.score-bar-fill {{ height:100%; border-radius:7px; }}
.tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:500; }}
.tag-buy {{ background:#dcfce7; color:#166534; }}
.tag-hold {{ background:#fef3c7; color:#92400e; }}
.tag-sell {{ background:#fee2e2; color:#991b1b; }}
</style>
</head>
<body style="background:#ffffff;color:#333333;padding:30px;max-width:820px;margin:auto;">

<h1 style="color:#1a56db;font-size:24px;border-bottom:3px solid #1a56db;padding-bottom:10px;">📊 每日市场总结报告</h1>
<p style="font-size:13px;color:#888;margin-top:5px;">生成时间：{now} | 数据截止：{td}</p>

<!-- ====== 市场总览 ====== -->
<div style="background:#f0f7ff;border-radius:12px;padding:18px;margin:15px 0;border:1px solid #d0e3ff;">
<div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
        <span style="display:inline-block;padding:4px 14px;border-radius:12px;font-size:14px;font-weight:bold;
            {'background:#fff3cd;color:#856404;' if 'chaos' in (season_label or '') else ('background:#d4edda;color:#155724;' if regime in ['bull','summer','spring'] else ('background:#f8d7da;color:#721c24;' if regime in ['bear','winter','autumn'] else 'background:#fff3cd;color:#856404;'))}">
            {season_emoji} {season_cn}
        </span>
        <span style="margin-left:10px;font-size:14px;color:#666;">恒纪元：{heng_cn}</span>
    </div>
    <div style="text-align:right">
        <span style="font-size:28px;font-weight:bold;color:#1a56db;">{fmt(s.get('raw_score'),1)}</span>
        <span style="font-size:12px;color:#888;">/ 100</span>
    </div>
</div>
<p style="margin-top:12px;font-size:14px;color:#333;"><strong>💡 策略建议：</strong>{position_advice}</p>
<p style="font-size:12px;color:#888;">季节判定置信度：{confidence}% | 恒纪元评分：{hg_score} | 当前{season_cn}期买入阈值75分</p>
</div>

<!-- ====== 市场趋势 ====== -->
<h2>📈 最近3日季节趋势</h2>
<table>
<tr><th>方向</th><th>日期</th><th>季节</th><th>评分</th><th>建议</th></tr>
{index_rows}
</table>

<!-- ====== 持仓总览 ====== -->
<h2>💼 当前持仓</h2>
<div class="summary-grid">
    <div class="summary-item">
        <div class="summary-label">持仓市值</div>
        <div class="summary-value" style="color:#00d4ff;">¥{fmt(total_mv,0)}</div>
    </div>
    <div class="summary-item">
        <div class="summary-label">总盈亏</div>
        <div class="summary-value" style="color:{'#00c853' if total_profit >= 0 else '#ff4d4f'}">
            {fmt_pct(total_profit / total_mv * 100 if total_mv > 0 else 0)}
            <span style="font-size:13px;">（¥{fmt(total_profit,0)}）</span>
        </div>
    </div>
    <div class="summary-item">
        <div class="summary-label">持仓数量</div>
        <div class="summary-value">{len(holdings)}只 · 🟢{win_count} 🔴{loss_count}</div>
    </div>
</div>

<table>
<tr>
    <th>名称</th>
    <th style="text-align:right">数量</th>
    <th style="text-align:right">成本价</th>
    <th style="text-align:right">现价</th>
    <th style="text-align:right">盈亏</th>
    <th style="text-align:right">市值</th>
    <th style="text-align:center">建议</th>
    <th style="text-align:center">买入日</th>
</tr>
{holdings_rows}
</table>

<!-- ====== 推荐买入 ====== -->
<h2>🎯 今日推荐关注（评分引擎 TOP ≥75）</h2>
<div class="card">
<p style="font-size:12px;color:#888;margin-top:0;">基于评分引擎 dual_track_v1 策略 · 综合评分含趋势/结构/情绪三维权重 · 当前{season_label}期买入阈值75分</p>
<table>
<tr><th>#</th><th>名称</th><th style="text-align:right">评分</th><th>评分条</th><th>模式</th><th>理由</th></tr>
{buy_rows}
</table>
</div>

<!-- ====== 行业板块涨幅 ====== -->
<h2>🔥 行业板块涨幅 TOP5</h2>
<table>
<tr><th>#</th><th style="text-align:left">行业</th><th style="text-align:center">成份股</th><th style="text-align:right">平均涨幅</th><th style="text-align:right">总成交额</th></tr>
{gainer_rows}
</table>

<!-- ====== 监控池评分 ====== -->
<h2>🏆 监控池评分 TOP10</h2>
<table>
<tr><th>#</th><th>名称</th><th style="text-align:right">综合评分</th><th style="text-align:right">涨跌幅</th><th>信号</th></tr>
{watch_rows}
</table>

<!-- ====== 当前策略 ====== -->
<h2>⚙️ 当前策略参数</h2>
<table>
<tr><th>参数名</th><th>当前值</th></tr>
{cfg_rows}
</table>

<div class="footer">
<p>⚠️ 本报告基于真实交易数据和评分引擎输出生成，仅供参考，不构成投资建议。</p>
<p style="font-size:11px;">Auto-generated by Main · 股票智能分析管理系统 · {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>

</body>
</html>"""
    return html


# ============================================================
#  邮件发送
# ============================================================

def send_email(html_content, subject=None):
    """通过QQ邮箱 SMTP 发送 HTML 邮件"""
    if not subject:
        subject = f"📊 每日市场总结报告 | {datetime.now().strftime('%Y-%m-%d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "12211662@qq.com"
    msg["To"] = "12211662@qq.com"

    part = MIMEText(html_content, "html", "utf-8")
    msg.attach(part)

    try:
        server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        server.login("12211662@qq.com", "undsqrmrgquycacg")
        server.sendmail("12211662@qq.com", ["12211662@qq.com"], msg.as_string())
        server.quit()
        return True, "邮件发送成功"
    except Exception as e:
        return False, f"邮件发送失败: {e}"


# ============================================================
#  主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="每日市场总结与持仓推荐报告")
    parser.add_argument("--send", action="store_true", help="发送到邮箱")
    parser.add_argument("--output", type=str, default=None, help="输出HTML到文件")
    parser.add_argument("--min-score", type=float, default=75, help="最低推荐评分")
    parser.add_argument("--limit", type=int, default=10, help="推荐数量")
    args = parser.parse_args()

    print(f"📊 开始生成每日市场报告... {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # 采集数据
    season = fetch_season_state()
    if not season:
        print("❌ 无法获取市场季节判定")
        sys.exit(1)
    print(f"✅ 季节判定: {season.get('season','?')} | {season.get('position_advice','?')}")

    holdings = fetch_holdings()
    print(f"✅ 持仓: {len(holdings)} 只")

    top_signals = fetch_top_signals(min_score=args.min_score, limit=args.limit)
    print(f"✅ 高评分信号: {len(top_signals)} 只 (≥{args.min_score}分)")

    watch_top = fetch_watch_pool_top(limit=10)
    print(f"✅ 监控池评分: {len(watch_top)} 只")

    gainers = fetch_sector_top_gainers(limit=5)
    print(f"✅ 行业板块涨幅TOP: {len(gainers)} 个")

    index_trend = fetch_index_snapshot()
    strategy_cfg = fetch_strategy_config()

    # 生成 HTML
    html = generate_html(season, holdings, top_signals, watch_top, gainers, index_trend, strategy_cfg)
    print(f"✅ HTML报告生成完成 ({len(html)} 字节)")

    # 保存本地
    if args.output:
        out_path = args.output
    else:
        out_path = f"/root/.openclaw/workspace/reports/daily_report_{datetime.now().strftime('%Y%m%d')}.html"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已保存: {out_path}")

    # 发送邮件
    if args.send:
        ok, msg = send_email(html)
        print(f"{'✅' if ok else '❌'} {msg}")
    else:
        print("🔕 未发送邮件（--send 参数未指定）")

    print("🏁 完成")


if __name__ == "__main__":
    main()
