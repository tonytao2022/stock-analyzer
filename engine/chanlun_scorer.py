"""
L2 缠论结构代理评分 —— 优化2
============================
趋势(40%) + 动量(35%) + 波动(15%) + 量能(10%) + 多周期背离检测
"""

from dataclasses import dataclass
from typing import List, Dict
from .indicators import sma, rsi, roc, stddev


@dataclass
class ChanlunResult:
    total: float           # 综合评分 0-100
    trend: float           # 趋势分
    momentum: float        # 动量分
    volatility: float      # 波动分
    volume: float          # 量能分
    chanlun_signal: float  # 缠论背离信号 -100~+100


def score_chanlun_enhanced(
    rows: List[dict],
    season: str,
    industry: str,
) -> ChanlunResult:
    """
    v4.0 缠论增强:
    - 优先从数据库读取chanlun_structure(buy_sell_point/structure_score/beichi)
    - 数据为空时用多周期背离代理: MACD背离 + RSI背离 + MA乖离
    """
    closes = [float(r['close']) for r in rows]
    highs = [float(r['high']) for r in rows]
    lows = [float(r['low']) for r in rows]
    vols = [float(r.get('vol', 0) or 0) for r in rows]
    n = len(closes)

    if n < 120:
        return ChanlunResult(
            total=50.0, trend=50.0, momentum=50.0,
            volatility=50.0, volume=50.0, chanlun_signal=0.0,
        )

    close = closes[-1]

    # ── 趋势(40%) ──
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ma120 = sma(closes, 120)

    tr = 0.0
    if ma5 > ma10: tr += 8
    if ma5 > ma20: tr += 7
    if ma10 > ma20: tr += 10
    if ma20 > ma60: tr += 10
    if ma20 > ma120: tr += 5
    if close > ma5: tr += 5
    if close > ma20: tr += 5
    old_ma20 = sma(closes[:-20], 20) if n > 80 else ma20
    slope20 = (ma20 - old_ma20) / old_ma20 if old_ma20 > 0 else 0
    tr += max(0, min(25, (slope20 + 0.05) * 250))
    yh = max(closes[-250:]) if n >= 250 else max(closes)
    yl = min(closes[-250:]) if n >= 250 else min(closes)
    if yh > yl:
        tr += (close - yl) / (yh - yl) * 25
    trend_score = round(max(0, min(100, tr)), 1)

    # ── 动量(35%) ──
    r5 = roc(closes, 5)
    r10 = roc(closes, 10)
    r20 = roc(closes, 20)
    r14 = rsi(closes, 14)
    mo = 0.0
    mo += max(0, min(25, 12.5 + r5 * 50))
    mo += max(0, min(20, 10 + r10 * 30))
    mo += max(0, min(15, 7.5 + r20 * 20))
    mo += max(0, min(20, r14 * 0.2))
    acc = r5 - r20
    if acc > 0.02: mo += 10
    elif acc > 0: mo += 5
    if n >= 6:
        up_vol = sum(1 for i in range(-5, 0) if closes[i] > closes[i - 1] and vols[i] > vols[i - 1])
        mo += up_vol * 2
    momentum_score = round(max(0, min(100, mo)), 1)

    # ── 波动(15%, 反转版) ──
    vol20 = stddev(closes, 20)
    vol60 = stddev(closes, 60)
    daily_vol = vol20 / close if close > 0 else 0.02
    vl = 50.0
    if daily_vol < 0.005: vl += 5
    elif daily_vol < 0.01: vl += 15
    elif daily_vol < 0.02: vl += 10
    elif daily_vol >= 0.03 and daily_vol < 0.04: vl -= 10
    elif daily_vol >= 0.04: vl -= 20
    if vol60 > 0:
        vr = vol20 / vol60
        if vr < 0.7: vl += 10
        elif vr < 0.85: vl += 5
        elif vr > 1.5: vl -= 10
        elif vr > 1.2: vl -= 5
    if n >= 10:
        max_diff = (max(closes[-10:]) - min(closes[-10:])) / close
        if max_diff < 0.03: vl += 10
        elif max_diff < 0.06: vl += 5
        elif max_diff > 0.15: vl -= 10
    if n >= 20:
        h20 = max(closes[-20:])
        mdd = (h20 - close) / h20
        if mdd > 0.15: vl += 8
        elif mdd > 0.10: vl += 4
        elif mdd < 0.02: vl += 2
    volatility_score = round(max(0, min(100, vl)), 1)

    # ── 量能(10%) ──
    v20m = sma(vols, 20)
    v60m = sma(vols, 60)
    vr_day = vols[-1] / v20m if v20m > 0 else 1
    vo = 50.0
    if v60m > 0:
        vt = v20m / v60m
        if vt > 1.3: vo -= 8
        elif vt > 1.1: vo -= 3
        elif vt < 0.7: vo += 5
        elif vt < 0.9: vo += 3
    if vr_day > 2.0: vo -= 10
    elif vr_day > 1.5: vo -= 5
    elif 0.7 <= vr_day <= 1.3: vo += 3
    elif vr_day < 0.5: vo += 5
    if n >= 6:
        dn_vol = sum(1 for i in range(-5, 0) if closes[i] < closes[i - 1] and vols[i] > vols[i - 1])
        vo -= dn_vol * 3
    volume_score = round(max(0, min(100, vo)), 1)

    # ── 缠论代理: 多周期背离检测 ──
    chanlun_signal = 0.0  # -100~+100: 负=超跌反弹窗口, 正=趋势延续

    # MACD金叉/死叉 (12/26/9)
    ema12 = sma(closes, 12)
    ema26 = sma(closes, 26)
    if n >= 35:
        old_ema12 = sma(closes[-9:-1], 12) if n >= 38 else ema12
        old_ema26 = sma(closes[-9:-1], 26) if n >= 38 else ema26
        if ema12 > ema26 and old_ema12 <= old_ema26:
            chanlun_signal += 15  # 金叉

    # RSI背离: 价格创新高但RSI未创新高=顶背离
    if n >= 40:
        h20_p = max(closes[-30:-10])
        r20_p = rsi(closes[-30:-10], 14)
        h20_n = max(closes[-10:])
        r20_n = rsi(closes[-10:], 14)
        if h20_n > h20_p and r20_n < r20_p - 5:
            chanlun_signal -= 20  # 顶背离
        l20_p = min(closes[-30:-10])
        r20_p2 = rsi(closes[-30:-10], 14)
        l20_n = min(closes[-10:])
        r20_n2 = rsi(closes[-10:], 14)
        if l20_n < l20_p and r20_n2 > r20_p2 + 5:
            chanlun_signal += 20  # 底背离

    # MA乖离: 价格远离MA20=超跌/超涨
    if close > 0 and n >= 20:
        ma20_dev = (close - ma20) / ma20
        if ma20_dev < -0.1:
            chanlun_signal += 15  # 深度超跌
        elif ma20_dev < -0.05:
            chanlun_signal += 8
        elif ma20_dev > 0.1:
            chanlun_signal -= 10  # 追高危险

    # 连续K线方向
    if n >= 5:
        cons_up = sum(1 for i in range(-4, 0) if closes[i] > closes[i - 1])
        cons_dn = sum(1 for i in range(-4, 0) if closes[i] < closes[i - 1])
        if cons_up >= 4:
            chanlun_signal += 10
        elif cons_dn >= 4:
            chanlun_signal -= 5

    chanlun_signal = max(-100, min(100, chanlun_signal))

    # 合成
    total = trend_score * 0.40 + momentum_score * 0.35 + volatility_score * 0.15 + volume_score * 0.10
    # 缠论信号修正: ±15分
    total += chanlun_signal * 0.15

    return ChanlunResult(
        total=round(max(0, min(100, total)), 1),
        trend=trend_score,
        momentum=momentum_score,
        volatility=volatility_score,
        volume=volume_score,
        chanlun_signal=chanlun_signal,
    )
