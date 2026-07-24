#!/usr/bin/env python3
"""
confidence_engine.py — 买卖点置信度模型 v1.0
==============================================
设计: May | 2026-07-23

三因子合成: C_structure(40%) + C_consensus(35%) + C_history(25%)
五分法等级: A(>=80) → B(60-79) → C(40-59) → D(20-39) → E(<20)
安全门: score_conflict / deteriorating / adverse_market
"""

import sys
import os
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection


class ConfidenceEngine:
    """买卖点置信度评估引擎"""

    # 五分法阈值
    LEVEL_THRESHOLDS = [
        (80, 'A', '高置信'),
        (60, 'B', '中置信'),
        (40, 'C', '中低置信'),
        (20, 'D', '低置信'),
        (0, 'E', '不可信'),
    ]

    def calc_structure_confidence(self, struct_score: Optional[float],
                                  buy_sell_point: Optional[str],
                                  autumn_tiger: bool) -> float:
        """
        结构置信度 C_structure (0~100)
        基于缠论结构清晰度 + 买卖点确认 + 秋老虎惩罚
        """
        if struct_score is None:
            return 15.0  # 无结构数据 → 极低置信

        ss = float(struct_score)
        bs = (buy_sell_point or 'none').lower()

        # 基础分 (调高中等区间)
        if ss >= 75:
            base = 85.0
        elif ss >= 60:
            base = 65.0
        elif ss >= 40:
            base = 50.0
        else:
            base = 25.0

        # 买卖点确认加分
        bs_boost = {
            'buy3': 15, 'buy2': 10, 'buy1': 5,
            'sell3': 15, 'sell2': 10, 'sell1': 5,
        }.get(bs, 0)
        # 未确认买卖点 → 中性偏减
        if bs in ('none', '', None):
            bs_boost = -5 if ss >= 60 else 0

        # 秋老虎惩罚
        tiger_penalty = -20 if autumn_tiger else 0

        raw = base + bs_boost + tiger_penalty
        return max(0.0, min(100.0, raw))

    def calc_consensus_confidence(self, trend_score: Optional[float],
                                  struct_score: Optional[float],
                                  mf_score: Optional[float],
                                  momentum_score: Optional[float]) -> float:
        """
        因子一致性置信度 C_consensus (0~100)
        检查 trend/structure/mf/momentum 方向是否一致
        """
        scores = []
        for s in [trend_score, struct_score, mf_score, momentum_score]:
            if s is not None:
                scores.append(float(s))

        if len(scores) < 2:
            return 40.0  # 因子不足 → 中性保守

        # 统计各因子方向
        n_bull = sum(1 for s in scores if s >= 60)
        n_bear = sum(1 for s in scores if s <= 40)
        n = len(scores)

        # 场景化调整：有效因子数 < 4 时放宽一致门槛
        # 原因：momentum_score 经常全NULL，导致3因子天然比4因子更容易落入分歧
        relaxed = (n < 4)

        # 一致看多
        if n_bull == n:
            return 85.0
        # 一致看空（也可靠）
        if n_bear == n:
            return 80.0
        # 一致看空 → 超跌一致性检测（建议1：chaos期一致看空也是可靠信号）
        if n_bear >= n - 1:
            # 3因子都≤40或2/3≤40 → 超跌一致性，给70而不是60
            return 70.0 if relaxed else 60.0
        # 多数一致看多
        if n_bull >= n - 1:
            return 60.0

        # 多数一致（宽松模式）：建议2，有效因子不足4时，n-2也算多数一致
        if relaxed:
            if n_bull >= n - 2:
                return 60.0
            if n_bear >= n - 2:
                return 60.0

        # 分歧
        bull_ratio = n_bull / n
        bear_ratio = n_bear / n

        # 强弱分歧
        if bull_ratio >= 0.5:
            return 45.0
        if bear_ratio >= 0.5:
            return 45.0
        # 严重分歧
        return 30.0

    def calc_history_confidence(self, ts_code: str,
                                trade_date: date,
                                lookback: int = 5) -> float:
        """
        历史表现置信度 C_history (0~100)
        近N日评分变化越小 → 置信度越高
        """
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT calibrated_score FROM stock_db_v2.strategy_signal
                WHERE ts_code=%s AND trade_date < %s
                ORDER BY trade_date DESC LIMIT %s
            """, (ts_code, trade_date, lookback))
            rows = cur.fetchall()
            cur.close()
            conn.close()

            if not rows or len(rows) < 2:
                return 40.0  # 历史不足 → 中性保守

            scores = [float(list(r.values())[0] or 0) for r in rows]
            min_s = min(scores)
            max_s = max(scores)
            spread = max_s - min_s
            last_score = scores[0]
            first_score = scores[-1]
            trend = last_score - first_score

            # 波动惩罚
            if spread <= 5:
                vol_penalty = 0
            elif spread <= 15:
                vol_penalty = (spread - 5) * 2
            else:
                vol_penalty = 20 + (spread - 15) * 0.5

            vol_penalty = min(40, vol_penalty)

            # 趋势惩罚（剧烈单向变化也不可靠）
            if abs(trend) > 20 and spread > 15:
                trend_penalty = 15
            else:
                trend_penalty = 0

            raw = 90 - vol_penalty - trend_penalty
            return max(0.0, min(100.0, raw))

        except Exception:
            return 30.0  # 查询失败 → 保守

    def composite_confidence(self, c_struct: float, c_consensus: float,
                              c_history: float) -> Tuple[float, str, str]:
        """
        三因子合成 + 五分法

        Returns:
            (总分, 等级, 等级描述)
        """
        total = c_struct * 0.40 + c_consensus * 0.35 + c_history * 0.25

        for threshold, level, desc in self.LEVEL_THRESHOLDS:
            if total >= threshold:
                return round(total, 1), level, desc

        return round(total, 1), 'E', '不可信'

    def check_safety_gates(self, ts_code: str, trade_date: date,
                           calibrated_score: float,
                           confidence_level: str,
                           direction: str,
                           season: str,
                           is_holding: bool) -> Dict:
        """
        三道安全门检查

        Returns:
            {'gate_triggered': bool, 'safety_gate': str|None,
             'signal_label': str, 'direction': str}
        """
        result = {
            'gate_triggered': False,
            'safety_gate': None,
            'signal_label': 'NORMAL',
            'direction': direction,
        }

        # Gate 1: 评分-置信度背离
        if calibrated_score >= 60 and confidence_level in ('D', 'E'):
            result['gate_triggered'] = True
            result['safety_gate'] = 'score_conflict'
            result['signal_label'] = 'TRAP'
            result['direction'] = 'avoid'
            return result

        # Gate 2: 持仓持续恶化
        if is_holding:
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("""
                    SELECT calibrated_score FROM stock_db_v2.strategy_signal
                    WHERE ts_code=%s AND trade_date < %s
                    ORDER BY trade_date DESC LIMIT 5
                """, (ts_code, trade_date))
                rows = cur.fetchall()
                cur.close()
                conn.close()

                if len(rows) >= 3:
                    scores = [float(list(r.values())[0] or 0) for r in rows]
                    # 连续下降且最新<20
                    if all(scores[i] >= scores[i+1] for i in range(len(scores)-1)) and scores[0] < 20:
                        result['gate_triggered'] = True
                        result['safety_gate'] = 'deteriorating'
                        result['signal_label'] = 'FORCE_SELL'
                        result['direction'] = 'sell'
                        return result
            except Exception:
                pass

        # Gate 3: 弱市 + 低置信
        adverse_seasons = ('chaos_autumn', 'autumn', 'winter')
        if season in adverse_seasons and confidence_level in ('D', 'E'):
            result['gate_triggered'] = True
            result['safety_gate'] = 'adverse_market'
            result['signal_label'] = 'AVOID'
            result['direction'] = 'avoid'
            return result

        # ════════════════════════════════════════════════════
        # Gate 4: 高分不涨检测（高位横盘潜力陷阱）
        # 2026-07-23 May 设计
        # 逻辑: 评分≥80 + 近10日涨幅≤3% → 高位横盘潜力陷阱
        # 典型场景: 追高后被套，评分虚高只因前期涨幅还没回撤
        # ════════════════════════════════════════════════════
        if calibrated_score >= 80:
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("""
                    SELECT close FROM daily_kline
                    WHERE ts_code=%s AND trade_date <= %s
                    ORDER BY trade_date DESC LIMIT 11
                """, (ts_code, trade_date))
                rows = cur.fetchall()
                cur.close()
                conn.close()

                if len(rows) >= 11:
                    prices = [float(r['close']) for r in reversed(rows)]
                    r10 = (prices[-1] - prices[0]) / prices[0]  # 近10日涨幅
                    
                    # 条件: 高分 + 涨幅极小(≤3%) 
                    if r10 <= 0.03:
                        # 额外检查成交量：近5日均量 vs 前20日均量
                        conn2 = get_connection()
                        cur2 = conn2.cursor()
                        cur2.execute("""
                            SELECT vol FROM daily_kline
                            WHERE ts_code=%s AND trade_date <= %s
                            ORDER BY trade_date DESC LIMIT 25
                        """, (ts_code, trade_date))
                        vol_rows = cur2.fetchall()
                        cur2.close()
                        conn2.close()
                        
                        if len(vol_rows) >= 25:
                            vols = [float(v['vol']) for v in vol_rows]
                            vol_5d = sum(vols[:5]) / 5
                            vol_20d = sum(vols[:20]) / 20
                            vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0
                        else:
                            vol_ratio = 1.0
                        
                        # 缩量横盘 → 典型的高位陷阱
                        if vol_ratio < 1.2 or r10 <= 0.01:
                            trap_level = 'POTENTIAL' if r10 <= 0.03 and r10 > 0.01 else 'HIGH' 
                            result['gate_triggered'] = True
                            result['safety_gate'] = 'high_price_stagnation'
                            result['signal_label'] = f'TRAP_{trap_level}'
                            result['direction'] = 'avoid'
                            # 附加信息
                            result['trap_r10'] = round(r10 * 100, 1)
                            result['trap_vol_ratio'] = round(vol_ratio, 2)
                            return result
            except Exception:
                pass

        return result

    def run_confidence(self, ts_code: str, trade_date: date,
                       signal_row: Dict, is_holding: bool = False) -> Dict:
        """
        完整置信度评估入口

        Args:
            ts_code: 股票代码
            trade_date: 交易日
            signal_row: strategy_signal 的一条记录字典，需要包含:
                calibrated_score, trend_score, structure_score, mf_score,
                momentum_score, buy_sell_point, direction, season
            is_holding: 是否当前持仓

        Returns:
            {'confidence_score': float, 'confidence_level': str,
             'level_desc': str, 'gate_triggered': bool,
             'safety_gate': str|None, 'signal_label': str,
             'direction': str}
        """
        # 结构置信度
        c_struct = self.calc_structure_confidence(
            signal_row.get('structure_score'),
            signal_row.get('buy_sell_point'),
            bool(signal_row.get('autumn_tiger', False))
        )

        # 因子一致性置信度
        c_consensus = self.calc_consensus_confidence(
            signal_row.get('trend_score'),
            signal_row.get('structure_score'),
            signal_row.get('mf_score'),
            signal_row.get('momentum_score'),
        )

        # 历史置信度
        c_history = self.calc_history_confidence(ts_code, trade_date)

        # 合成
        conf_score, conf_level, level_desc = self.composite_confidence(
            c_struct, c_consensus, c_history
        )

        # 安全门
        gate_result = self.check_safety_gates(
            ts_code, trade_date,
            float(signal_row.get('calibrated_score', 0) or 0),
            conf_level,
            signal_row.get('direction', 'unknown'),
            signal_row.get('season', 'chaos'),
            is_holding
        )

        return {
            'confidence_score': conf_score,
            'confidence_level': conf_level,
            'level_desc': level_desc,
            'c_structure': round(c_struct, 1),
            'c_consensus': round(c_consensus, 1),
            'c_history': round(c_history, 1),
            **gate_result,
        }


# ─── 快速测试 ───────────────────────────────────────
if __name__ == '__main__':
    ce = ConfidenceEngine()

    # 测试1: 芯源微7/22场景（高结构+资金背离→TRAP）
    print("=== 测试1: 芯源微 TRAP场景 ===")
    r1 = ce.run_confidence('688037.SH', date(2026, 7, 22), {
        'calibrated_score': 35.80,
        'trend_score': 100.0,
        'structure_score': 82.3,
        'mf_score': 47.0,
        'momentum_score': 50.0,
        'buy_sell_point': 'none',
        'direction': 'dormant',
        'season': 'chaos_autumn',
        'autumn_tiger': False,
    }, is_holding=True)
    for k, v in r1.items():
        print(f"  {k:20s} = {v}")

    # 测试2: 一致性看多场景
    print("\n=== 测试2: 一致看多 B级场景 ===")
    r2 = ce.run_confidence('600887.SH', date(2026, 7, 22), {
        'calibrated_score': 100.0,
        'trend_score': 85.0,
        'structure_score': 78.0,
        'mf_score': 90.0,
        'momentum_score': 82.0,
        'buy_sell_point': 'buy2',
        'direction': 'buy',
        'season': 'chaos_autumn',
        'autumn_tiger': False,
    })
    for k, v in r2.items():
        print(f"  {k:20s} = {v}")

    # 测试3: 一致看空（深科技场景）
    print("\n=== 测试3: 一致看空 B级场景 ===")
    r3 = ce.run_confidence('000021.SZ', date(2026, 7, 22), {
        'calibrated_score': 5.0,
        'trend_score': 55.0,
        'structure_score': 51.8,
        'mf_score': 5.0,
        'momentum_score': 20.0,
        'buy_sell_point': 'none',
        'direction': 'dormant',
        'season': 'chaos_autumn',
        'autumn_tiger': False,
    })
    for k, v in r3.items():
        print(f"  {k:20s} = {v}")

    # 测试4: 单独的C_history
    print("\n=== 测试4: 鼎泰高科历史稳定性 ===")
    ch = ce.calc_history_confidence('301377.SZ', date(2026, 7, 22))
    print(f"  C_history = {ch:.1f} (期望:<40, 因评分剧烈波动)")

    # 测试5: Gate 4 高分不涨检测 (模拟)
    print("\n=== 测试5: Gate 4 高分不涨检测 ===")
    r5 = ce.run_confidence('688981.SH', date(2026, 7, 22), {
        'calibrated_score': 82.0,
        'trend_score': 85.0,
        'structure_score': 78.0,
        'mf_score': 70.0,
        'momentum_score': 75.0,
        'buy_sell_point': 'buy2',
        'direction': 'buy',
        'season': 'chaos',
        'autumn_tiger': False,
    })
    print(f"  高分{82.0} + 10日涨幅<=3% = Gate4触发")
    print(f"  safety_gate: {r5['safety_gate']}")
    print(f"  signal_label: {r5['signal_label']}")
    print(f"  direction: {r5['direction']}")
