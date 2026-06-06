#!/usr/bin/env python3
"""
板块轮动调仓规则引擎 v1.0
==========================
基于SectorRotationEngine的三因子评分输出，执行MAY的调仓逻辑：
- 每周一评分排序 → 对比上周排名
- 评分差>5%触发调仓（防御态>8%）
- 防御态最多调3只，攻击态最多调5只
- 输出：建议买入TOP5 + 建议卖出TOP5 + 维持持有清单

设计者: MAY
架构:
  RotationRules
  ├─ run_weekly_scoring()         — 执行或读取本周评分
  ├─ compare_weekly_ranking()     — 对比本周vs上周排名/评分
  ├─ is_threshold_breached()      — 是否超过调仓阈值
  ├─ generate_adjustment_plan()   — 生成调仓建议
  └─ format_report()              — 输出调仓报告（文本/HTML）

依赖:
  - sector_rotation_engine.SectorRotationEngine
  - db_config.get_connection()
"""

import sys, os, json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection
from sector_rotation_engine import SectorRotationEngine, get_weight_mode
import warnings
warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════════════════════
# 调仓阈值
# ═══════════════════════════════════════════════════════════════

THRESHOLD_CONFIG = {
    'attacking': {'trigger_pct': 5.0, 'max_trades': 5, 'desc': '攻击态'},
    'neutral':   {'trigger_pct': 5.0, 'max_trades': 4, 'desc': '中性态'},
    'defensive': {'trigger_pct': 8.0, 'max_trades': 3, 'desc': '防御态'},
}

BUY_TOP_N = 5
SELL_TOP_N = 5


# ═══════════════════════════════════════════════════════════════
# 调仓规则引擎
# ═══════════════════════════════════════════════════════════════

class RotationRules:
    """板块轮动调仓规则引擎"""

    def __init__(self, trade_date: Optional[str] = None):
        self.trade_date = trade_date or date.today().isoformat()
        self.engine = SectorRotationEngine(self.trade_date)

    # ─── 工具方法 ───────────────────────────────────────

    def _get_conn(self):
        return get_connection()

    def _ensure_schema(self):
        """初始化数据库表"""
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_score_cache (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                sector_name VARCHAR(50) NOT NULL,
                chanlun_factor DECIMAL(6,2) DEFAULT 50.00,
                season_factor DECIMAL(6,2) DEFAULT 50.00,
                money_factor DECIMAL(6,2) DEFAULT 50.00,
                composite_score DECIMAL(6,2) DEFAULT 50.00,
                sector_rank INT DEFAULT 0,
                weight_mode VARCHAR(20) DEFAULT 'neutral',
                season VARCHAR(20) DEFAULT 'chaos',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_sector_date (trade_date, sector_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_rotation_log (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                week_label VARCHAR(10) NOT NULL COMMENT '周标签 如 2026W23',
                season VARCHAR(20) NOT NULL,
                weight_mode VARCHAR(20) NOT NULL,
                total_sectors INT DEFAULT 0,
                trigger_flag TINYINT(1) DEFAULT 0 COMMENT '是否触发调仓',
                trigger_type VARCHAR(20) DEFAULT 'none',
                max_trades INT DEFAULT 0,
                buy_suggestions JSON DEFAULT NULL,
                sell_suggestions JSON DEFAULT NULL,
                hold_list JSON DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_week (week_label)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)

        conn.commit()
        cur.close()

    def _get_week_label(self, date_str: str) -> str:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        iso = d.isocalendar()
        return f"{iso[0]}W{iso[1]:02d}"

    def _get_previous_week_label(self, date_str: str) -> str:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        prev = d - timedelta(days=7)
        iso = prev.isocalendar()
        return f"{iso[0]}W{iso[1]:02d}"

    # ─── 评分获取（缓存优先） ───────────────────────────

    def run_weekly_scoring(self, force_recalc: bool = False) -> Dict:
        """
        获取本周评分。
        如果当天已缓存且force_recalc=False，从sector_score_cache读取。
        否则重新计算并写缓存。
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM sector_score_cache WHERE trade_date=%s",
                    (self.trade_date,))
        cached_count = cur.fetchone()['cnt']
        cur.close()

        if cached_count > 0 and not force_recalc:
            return self._read_from_cache()

        result = self.engine.score_all_industries()
        saved = self.engine.save_to_db(result)
        print(f"  💾 缓存已写入: {saved} 个板块")
        return result

    def _read_from_cache(self) -> Dict:
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, sector_name, chanlun_factor, season_factor,
                   money_factor, composite_score, sector_rank,
                   weight_mode, season
            FROM sector_score_cache
            WHERE trade_date=%s ORDER BY sector_rank ASC
        """, (self.trade_date,))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return self.run_weekly_scoring(force_recalc=True)

        sectors = {}
        ranking = []
        for row in rows:
            sn = row['sector_name']
            sectors[sn] = {
                'chanlun_factor': float(row['chanlun_factor']),
                'season_factor': float(row['season_factor']),
                'money_factor': float(row['money_factor']),
                'composite_score': float(row['composite_score']),
                'rank': int(row['sector_rank']),
            }
            ranking.append(sn)

        season_state = {'season': rows[0].get('season', 'chaos')}
        weight_mode = rows[0].get('weight_mode', 'neutral')

        return {
            'trade_date': str(rows[0]['trade_date']),
            'season_state': season_state,
            'weight_mode': weight_mode,
            'weights': get_weight_mode(season_state.get('season', 'chaos'), 0, 0.5)[1],
            'sectors': sectors,
            'ranking': ranking,
        }

    # ─── 上周评分 ───────────────────────────────────────

    def _load_week_cache(self, week_label: str) -> Optional[Dict]:
        """加载某周的历史缓存"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, sector_name, composite_score, sector_rank
            FROM sector_score_cache
            WHERE DATE_FORMAT(trade_date, '%%xW%%v')=%s
            ORDER BY sector_rank ASC
        """, (week_label,))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return None

        sectors = {}
        ranking = []
        for row in rows:
            sn = row['sector_name']
            sectors[sn] = {
                'composite_score': float(row['composite_score']),
                'rank': int(row['sector_rank']),
            }
            ranking.append(sn)
        return {'sectors': sectors, 'ranking': ranking}

    # ─── 排名对比 ───────────────────────────────────────

    def compare_weekly_ranking(self, current: Dict, previous: Optional[Dict]) -> Dict:
        """
        对比本周vs上周评分排名

        Returns:
            {
                'has_previous': bool,
                'current_date': str,
                'changes': { sector_name: { current_score, previous_score,
                    score_change, score_change_pct, current_rank, previous_rank,
                    rank_change, direction }, ... },
                'worst_declines': [sector,...],
                'best_climbers': [sector,...],
            }
        """
        result = {
            'has_previous': previous is not None,
            'current_date': current.get('trade_date', ''),
            'previous_date': None,
            'changes': {},
            'worst_declines': [],
            'best_climbers': [],
        }

        if previous:
            prev_sectors = previous.get('sectors', {})
            prev_dates = [current.get('trade_date') for _ in [1]]
            result['previous_date'] = prev_dates[0] if prev_dates else None
        else:
            prev_sectors = {}

        for sector in current.get('ranking', []):
            cur_data = current['sectors'].get(sector, {})
            cur_score = cur_data.get('composite_score', 50)
            cur_rank = cur_data.get('rank', 999)

            prev_data = prev_sectors.get(sector)
            if prev_data:
                prev_score = prev_data.get('composite_score', 50)
                prev_rank = prev_data.get('rank', 999)
                score_change = round(cur_score - prev_score, 1)
                score_change_pct = round(
                    (cur_score - prev_score) / prev_score * 100 if prev_score > 0 else 0, 1)
                rank_change = prev_rank - cur_rank  # >0=排名上升

                if rank_change > 0:
                    direction = 'up'
                elif rank_change < 0:
                    direction = 'down'
                else:
                    direction = 'stable'
            else:
                prev_score = None
                prev_rank = None
                score_change = None
                score_change_pct = None
                rank_change = None
                direction = 'new'

            result['changes'][sector] = {
                'current_score': cur_score,
                'previous_score': prev_score,
                'score_change': score_change,
                'score_change_pct': score_change_pct,
                'current_rank': cur_rank,
                'previous_rank': prev_rank,
                'rank_change': rank_change,
                'direction': direction,
            }

        # 排名变化排序
        sorted_changes = sorted(
            [(s, d.get('rank_change', 0) or 0) for s, d in result['changes'].items()],
            key=lambda x: x[1])
        result['worst_declines'] = [s for s, _ in sorted_changes[:5]]
        result['best_climbers'] = [s for s, _ in reversed(sorted_changes[-5:])]

        return result

    # ─── 调仓阈值判断 ───────────────────────────────────

    def is_threshold_breached(self, current: Dict, comparison: Dict) -> Tuple[bool, str, int]:
        """
        判断是否触发调仓

        Returns:
            (triggered, mode_name, max_trades)
        """
        if not comparison.get('has_previous'):
            return False, 'none', 0

        ss = self.engine.get_season_state()
        mode_name, _ = get_weight_mode(
            ss.get('season', 'chaos'),
            float(ss.get('raw_score', 0)),
            float(ss.get('confidence', 0.5)))

        cfg = THRESHOLD_CONFIG.get(mode_name, THRESHOLD_CONFIG['neutral'])
        trigger_pct = cfg['trigger_pct']
        max_trades = cfg['max_trades']

        max_change = max(
            (abs(d.get('score_change_pct', 0) or 0)
             for d in comparison['changes'].values()), default=0)

        triggered = max_change >= trigger_pct
        status = '⚠️ 触发调仓!' if triggered else '✅ 未触发'
        print(f"  {status} {cfg['desc']} 最大变化:{max_change:+.1f}% {'≥' if triggered else '<'}{trigger_pct}% | 上限:{max_trades}只")

        return triggered, mode_name, max_trades

    # ─── 生成调仓计划 ───────────────────────────────────

    def generate_adjustment_plan(self, current: Dict, comparison: Dict,
                                 triggered: bool, mode_name: str,
                                 max_trades: int) -> Dict:
        """
        生成调仓建议

        Returns:
            { triggered, mode, max_trades, buy_list, sell_list, hold_list, reason }
        """
        ranking = current.get('ranking', [])
        changes = comparison.get('changes', {})

        # 买入候选: 评分高+排名上升优先
        buy_candidates = []
        for sector in ranking:
            r = current['sectors'].get(sector, {})
            ch = changes.get(sector, {})
            score = r.get('composite_score', 50)
            direction = ch.get('direction', 'new')
            buy_candidates.append({
                'sector': sector, 'score': score,
                'rank': r.get('rank', 999),
                'score_change': ch.get('score_change'),
                'direction': direction,
                'chanlun': r.get('chanlun_factor'),
                'money': r.get('money_factor'),
            })

        buy_candidates.sort(key=lambda x: (
            x['score'],
            1 if x['direction'] in ('up', 'new') else 0,
        ), reverse=True)

        # 卖出候选: 评分低+排名下降
        sell_candidates = []
        for sector in reversed(ranking):
            r = current['sectors'].get(sector, {})
            ch = changes.get(sector, {})
            score = r.get('composite_score', 50)
            rank_change = ch.get('rank_change', 0) or 0
            direction = ch.get('direction', 'stable')
            if direction == 'down' or score < 50:
                sell_candidates.append({
                    'sector': sector, 'score': score,
                    'rank': r.get('rank', 999),
                    'rank_change': -rank_change,
                    'direction': direction,
                })

        sell_candidates.sort(key=lambda x: (1 if x['direction'] == 'down' else 0, -x['score']))

        buy_list = buy_candidates[:min(BUY_TOP_N, max_trades)]
        sell_list = sell_candidates[:min(SELL_TOP_N, max_trades)]

        buy_sectors = {b['sector'] for b in buy_list}
        sell_sectors = {s['sector'] for s in sell_list}
        hold_list = [s for s in ranking if s not in buy_sectors and s not in sell_sectors]

        cfg = THRESHOLD_CONFIG.get(mode_name, THRESHOLD_CONFIG['neutral'])
        reason = (f"得分变化超过{cfg['trigger_pct']}%阈值，{cfg['desc']}最多调{max_trades}只"
                  if triggered else f"未超过阈值，{cfg['desc']}模式无需大调")

        plan = {
            'triggered': triggered, 'mode': mode_name,
            'max_trades': max_trades,
            'buy_list': buy_list, 'sell_list': sell_list,
            'hold_list': hold_list, 'reason': reason,
        }

        # 终端打印
        print(f"\n{'='*50}")
        print(f"📋 调仓计划 | {cfg['desc']}")
        print(f"   {'⚡ 触发' if triggered else '✅ 未触发'}: {reason}")
        print(f"{'='*50}")
        print(f"\n🟢 建议买入 TOP {len(buy_list)}:")
        for b in buy_list:
            chg = f"(变化{b.get('score_change', 0):+.1f})" if b.get('score_change') is not None else "(新)"
            print(f"  {b['sector']:10s} | 分:{b['score']:5.1f} {chg}")
        print(f"\n🔴 建议卖出 TOP {len(sell_list)}:")
        for s in sell_list:
            print(f"  {s['sector']:10s} | 分:{s['score']:5.1f}")
        print(f"\n⚪ 维持持有: {len(hold_list)} 个")

        return plan

    # ─── 保存调仓记录 ───────────────────────────────────

    def save_rotation_log(self, plan: Dict, current: Dict, comparison: Dict) -> bool:
        conn = self._get_conn()
        cur = conn.cursor()
        week_label = self._get_week_label(self.trade_date)
        season = current.get('season_state', {}).get('season', 'chaos')

        cur.execute("""
            INSERT INTO sector_rotation_log
                (trade_date, week_label, season, weight_mode,
                 total_sectors, trigger_flag, trigger_type, max_trades,
                 buy_suggestions, sell_suggestions, hold_list)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                trade_date=VALUES(trade_date), season=VALUES(season),
                weight_mode=VALUES(weight_mode),
                total_sectors=VALUES(total_sectors),
                trigger_flag=VALUES(trigger_flag),
                trigger_type=VALUES(trigger_type),
                max_trades=VALUES(max_trades),
                buy_suggestions=VALUES(buy_suggestions),
                sell_suggestions=VALUES(sell_suggestions),
                hold_list=VALUES(hold_list)
        """, (
            self.trade_date, week_label, season, plan['mode'],
            len(current.get('ranking', [])),
            1 if plan['triggered'] else 0, plan['mode'], plan['max_trades'],
            json.dumps(plan['buy_list'], ensure_ascii=False, default=str),
            json.dumps(plan['sell_list'], ensure_ascii=False, default=str),
            json.dumps(plan['hold_list'], ensure_ascii=False, default=str),
        ))
        conn.commit()
        cur.close()
        print(f"  💾 调仓记录已保存 (周: {week_label})")
        return True

    # ─── 格式化文本报告 ─────────────────────────────────

    def format_text_report(self, current: Dict, comparison: Dict, plan: Dict) -> str:
        td = current.get('trade_date', '')
        ss = current.get('season_state', {})
        season = ss.get('season', 'chaos')
        mode_cn = {'attacking': '攻击态', 'neutral': '中性态', 'defensive': '防御态'}.get(plan['mode'], plan['mode'])

        lines = [
            f"{'='*60}",
            f"🔄 板块轮动调仓报告",
            f"日期: {td} | 季节: {season} | 模式: {mode_cn}",
            f"{'='*60}",
            f"\n📊 板块评分排名 TOP 15:",
            f"{'排名':>4s} {'板块':12s} {'总分':>6s} {'变化':>7s} {'方向'}",
            "-" * 45,
        ]
        for sector in current.get('ranking', [])[:15]:
            r = current['sectors'].get(sector, {})
            ch = comparison['changes'].get(sector, {})
            rank = r.get('rank', 0)
            score = r.get('composite_score', 0)
            sc = ch.get('score_change')
            sc_str = f"{sc:+.1f}" if sc is not None else '  new'
            arrow = {'up': '↑', 'down': '↓', 'stable': '→', 'new': '✦'}.get(ch.get('direction', ''), '·')
            lines.append(f"{rank:>4d} {sector:12s} {score:>6.1f} {sc_str:>7s}  {arrow}")

        lines.extend([
            f"\n🏆 上升最快: {', '.join(comparison.get('best_climbers', [])[:3])}",
            f"📉 下降最多: {', '.join(comparison.get('worst_declines', [])[:3])}",
            f"\n{'='*60}",
            f"📋 调仓建议",
            f"状态: {'⚡ 触发调仓' if plan['triggered'] else '✅ 维持'} | {plan['reason']}",
            f"最多调仓: {plan['max_trades']} 只",
        ])

        lines.append(f"\n🟢 建议买入 TOP {len(plan['buy_list'])}:")
        for i, b in enumerate(plan['buy_list'], 1):
            sc = f"(变化{b.get('score_change', 0):+.1f})" if b.get('score_change') is not None else "(新)"
            lines.append(f"  {i}. {b['sector']:10s} 评分:{b['score']:5.1f} {sc}")

        lines.append(f"\n🔴 建议卖出 TOP {len(plan['sell_list'])}:")
        for i, s in enumerate(plan['sell_list'], 1):
            lines.append(f"  {i}. {s['sector']:10s} 评分:{s['score']:5.1f}")

        lines.append(f"\n⚪ 维持持有 ({len(plan['hold_list'])} 个):")
        for i in range(0, len(plan['hold_list']), 6):
            lines.append(f"  {' | '.join(plan['hold_list'][i:i+6])}")

        lines.append(f"\n{'='*60}")
        lines.append("⚠️ 本报告基于三因子评分引擎输出，仅供参考。")
        return '\n'.join(lines)

    # ─── 周轮动主管道 ───────────────────────────────────

    def weekly_rotation_pipeline(self, force_recalc: bool = False) -> Dict:
        """
        每周轮动管道（周一收盘后调用）

        步骤:
        1. 评分（或从缓存读）
        2. 加载上周缓存
        3. 对比排名
        4. 判断阈值
        5. 生成调仓计划
        6. 保存记录

        Returns:
            { 'current': ..., 'comparison': ..., 'plan': ... }
        """
        print(f"\n{'='*60}")
        print(f"🔄 板块轮动周评分管道")
        print(f"{'='*60}")

        # 1. 确保表存在
        self._ensure_schema()

        # 2. 本周评分
        print(f"📅 日期: {self.trade_date}")
        current = self.run_weekly_scoring(force_recalc=force_recalc)

        # 3. 上周缓存
        prev_week = self._get_previous_week_label(self.trade_date)
        print(f"📅 上周: {prev_week}")
        previous = self._load_week_cache(prev_week)

        # 4. 对比
        print(f"  ▶ 对比排名 {'(有上周数据)' if previous else '(无上周数据)'}")
        comparison = self.compare_weekly_ranking(current, previous)

        # 5. 阈值
        triggered, mode_name, max_trades = self.is_threshold_breached(current, comparison)

        # 6. 调仓计划
        plan = self.generate_adjustment_plan(current, comparison, triggered, mode_name, max_trades)

        # 7. 保存
        self.save_rotation_log(plan, current, comparison)

        return {
            'current': current,
            'comparison': comparison,
            'plan': plan,
        }


# ═══════════════════════════════════════════════════════════════
# CLI入口
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description='板块轮动调仓规则引擎')
    parser.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    parser.add_argument('--force', action='store_true', help='强制重新计算评分')
    parser.add_argument('--output', type=str, default='text',
                        choices=['text', 'html'], help='输出格式')
    args = parser.parse_args()

    rules = RotationRules(args.date)
    result = rules.weekly_rotation_pipeline(force_recalc=args.force)

    if args.output == 'text':
        print(rules.format_text_report(
            result['current'], result['comparison'], result['plan']))
    else:
        # HTML输出
        from sector_rotation_engine import generate_html_report
        html = rules.format_html_report(
            result['current'], result['comparison'], result['plan'])
        out_file = f"rotation_report_{result['current']['trade_date']}.html"
        with open(out_file, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"\n✅ HTML报告已输出: {out_file}")


if __name__ == '__main__':
    main()
