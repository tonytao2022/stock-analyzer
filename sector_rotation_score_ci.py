#!/usr/bin/env python3
"""
中信行业指数三因子评分引擎 v1.0
=================================
方案A：基于中信一级行业指数（CI005xxx）的评分体系
- 数据源: sector_index_daily (Tushare ci_daily) → 中信指数
- 缠论因子: 从 sector_chanlun_cache 读取 structure_score，归一化到 0-100
- 季节因子: 从 season_state 获取当前季节，按 MAY 行业-季节打分表评分
- 资金因子: 从 sector_index_daily 获取最近 5 日量比
- 权重: 根据 season_state 动态切换（MAY 权重表）

方案B（sector_rotation_engine.py）保留作为交叉验证。
本引擎写入 sector_rotation_score 表。

设计者: MAY + Tony
"""

import sys, os, json, math
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════════════════════
# 中信一级行业代码 → 核心板块中文名（映射表）
# ═══════════════════════════════════════════════════════════════

# sector_index_daily 可能存储为 XXX.CI 或 XXX
# Tushare ci_daily 返回 ts_code 格式为 "CI005001.CI"
CI_INDEX_MAP = {
    'CI005001': '银行',
    'CI005002': '综合',
    'CI005003': '煤炭',
    'CI005004': '钢铁',
    'CI005005': '有色金属',
    'CI005006': '建筑',
    'CI005007': '建材',
    'CI005008': '电力及公用事业',
    'CI005009': '基础化工',
    'CI005010': '电子',
    'CI005011': '计算机',
    'CI005012': '传媒',
    'CI005013': '通信',
    'CI005014': '食品饮料',
    'CI005015': '医药',
    'CI005016': '农林牧渔',
    'CI005017': '纺织服装',
    'CI005018': '轻工制造',
    'CI005019': '商贸零售',
    'CI005020': '房地产',
    'CI005021': '交通运输',
    'CI005022': '国防军工',
    'CI005023': '汽车',
    'CI005024': '家电',
    'CI005025': '电力设备及新能源',
    'CI005026': '机械',
    'CI005027': '石油化工',
    'CI005028': '消费者服务',
    'CI005029': '非银行金融',
    'CI005030': '保险',
}

# 逆映射（中文 → CI代码）
SECTOR_TO_CI = {v: k for k, v in CI_INDEX_MAP.items()}

# 方案A的核心板块列表（与CI一级行业对应）
CORE_SECTORS_CI = list(CI_INDEX_MAP.values())


# ═══════════════════════════════════════════════════════════════
# 季节因子——行业基础分映射（MAY方案）
# 与 sector_rotation_engine.py 保持一致
# ═══════════════════════════════════════════════════════════════

SEASON_SECTOR_SCORE = {
    'spring': {
        '电子': 5, '计算机': 4, '通信': 3,
        '有色金属': 4, '基础化工': 3, '钢铁': 2,
        '机械': 3, '国防军工': 2,
        '电力设备及新能源': 3,
        '传媒': 2,
        # 防御品种减分
        '银行': -3, '非银行金融': -2, '电力及公用事业': -2,
        '煤炭': -1, '石油化工': -1,
        '食品饮料': -1,
        '建筑': -1,
    },
    'summer': {
        '电子': 5, '计算机': 4, '通信': 3,
        '汽车': 3, '家电': 2,
        '食品饮料': 2,
        '医药': 3, '电力设备及新能源': 2,
        '国防军工': 1,
        # 减分
        '银行': -2, '煤炭': -2, '石油化工': -2,
        '钢铁': -1, '电力及公用事业': -1,
    },
    'autumn': {
        '银行': 4, '非银行金融': 3, '电力及公用事业': 4,
        '煤炭': 3, '石油化工': 3,
        '建筑': 2, '交通运输': 3,
        '家电': 2, '食品饮料': 2,
        # 减分
        '电子': -4, '计算机': -3, '通信': -3,
        '有色金属': -2, '基础化工': -2,
        '电力设备及新能源': -3, '汽车': -2,
        '传媒': -3, '国防军工': -2,
    },
    'winter': {
        '银行': 5, '电力及公用事业': 5,
        '煤炭': 4, '石油化工': 4,
        '交通运输': 3, '食品饮料': 3, '医药': 2,
        '建筑': 2,
        # 所有成长/周期品种大幅减分
        '电子': -5, '计算机': -5, '通信': -4,
        '有色金属': -4, '基础化工': -3, '钢铁': -3,
        '电力设备及新能源': -4, '汽车': -4,
        '房地产': -3, '非银行金融': -2,
        '传媒': -4, '国防军工': -4,
        '机械': -3, '建材': -2,
    },
    'chaos': {
        '银行': 2, '电力及公用事业': 2,
        '食品饮料': 2, '医药': 2,
        '煤炭': 1, '石油化工': 1,
        '电子': -2, '计算机': -2,
        '有色金属': -1, '基础化工': -1, '钢铁': -1,
        '电力设备及新能源': -2, '汽车': -1,
        '传媒': -2,
    },
}

# 混沌子态修正
CHAOS_SUBTYPE_ADJ = {
    'chaos_spring': {  # 偏多混沌
        '电子': 1, '计算机': 1, '通信': 1,
        '电力设备及新能源': 1, '汽车': 1,
    },
    'chaos_autumn': {  # 偏空混沌
        '银行': 1, '电力及公用事业': 1,
        '煤炭': 1, '食品饮料': 1, '医药': 1,
        '交通运输': 1,
    },
}


# ═══════════════════════════════════════════════════════════════
# 权重切换（与 sector_rotation_engine.py 一致）
# ═══════════════════════════════════════════════════════════════

def get_weight_mode(season: str, raw_score: float, confidence: float) -> Tuple[str, Dict[str, float]]:
    """
    根据季节状态选择权重模式

    Returns:
        (mode_name, {'chanlun': float, 'season': float, 'money': float})
    """
    if season == 'summer' and confidence > 0.6:
        return 'attacking', {'chanlun': 0.45, 'season': 0.35, 'money': 0.20}
    elif season == 'spring' and raw_score > 5:
        return 'attacking', {'chanlun': 0.40, 'season': 0.35, 'money': 0.25}
    elif season in ('autumn', 'winter'):
        return 'defensive', {'chanlun': 0.30, 'season': 0.25, 'money': 0.45}
    elif season == 'chaos':
        return 'defensive', {'chanlun': 0.25, 'season': 0.30, 'money': 0.45}
    else:
        return 'neutral', {'chanlun': 0.35, 'season': 0.30, 'money': 0.35}


# ═══════════════════════════════════════════════════════════════
# 评分引擎主类
# ═══════════════════════════════════════════════════════════════

class SectorRotationScoreCI:
    """
    中信行业指数三因子评分引擎（方案A）
    流程:
        fetch_sector_daily.py → sector_index_daily
            → sector_chanlun_analyzer.py → sector_chanlun_cache
                → sector_rotation_score_ci.py → sector_rotation_score
    """

    def __init__(self, trade_date: Optional[str] = None):
        self.trade_date = trade_date or date.today().isoformat()
        self._latest_trade_date = None

    # ─── 工具方法 ───────────────────────────────────────

    def _get_conn(self):
        return get_connection()

    def _get_latest_trade_date(self) -> str:
        """获取最新交易日"""
        if self._latest_trade_date:
            return self._latest_trade_date
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(trade_date) FROM sector_index_daily
            WHERE trade_date <= %s
        """, (self.trade_date,))
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            dt = row[0].isoformat() if hasattr(row[0], 'isoformat') else str(row[0])
            self._latest_trade_date = dt
            return dt
        return self.trade_date

    # ─── 1. 当前季节状态 ─────────────────────────────────

    def get_current_season(self) -> Dict:
        """
        从 season_state 表读取最新季节判定

        Returns:
            {'season': 'chaos', 'raw_score': 0, 'confidence': 0.5,
             'regime': 'range', 'chaos_subtype': '', ...}
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, season, raw_score, confidence, regime,
                   chaos_subtype, scoring_strategy, hengjiyuan_level
            FROM season_state
            WHERE index_code='MARKET'
            ORDER BY trade_date DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        if row:
            return {
                'trade_date': row.get('trade_date'),
                'season': row.get('season', 'chaos'),
                'raw_score': float(row.get('raw_score', 0) or 0),
                'confidence': float(row.get('confidence', 0.5) or 0.5),
                'regime': row.get('regime', 'range'),
                'chaos_subtype': row.get('chaos_subtype', '') or '',
                'scoring_strategy': row.get('scoring_strategy', 'reversion'),
                'hengjiyuan_level': row.get('hengjiyuan_level', 'weak_heng'),
            }
        return {'season': 'chaos', 'raw_score': 0, 'confidence': 0.5, 'regime': 'range'}

    # ─── 2. 获取中信行业列表 ─────────────────────────────

    def get_ci_sectors(self) -> List[str]:
        """
        从 sector_index_daily 获取所有中信一级行业名称

        Returns:
            行业名称列表，按 CI005001-CI005030 顺序
        """
        conn = self._get_conn()
        cur = conn.cursor()
        # 先尝试有名称的
        cur.execute("""
            SELECT DISTINCT ts_code, index_name FROM sector_index_daily
            WHERE level='L1' AND index_name IS NOT NULL AND index_name != ''
            ORDER BY ts_code
        """)
        rows = cur.fetchall()
        cur.close()

        if rows:
            # 如果有名称，用名称
            result = []
            seen = set()
            for r in rows:
                name = r.get('index_name', '').strip()
                if name and name not in seen:
                    result.append(name)
                    seen.add(name)
            if result:
                return result

        # 降级：用 CI_INDEX_MAP
        return list(CI_INDEX_MAP.values())

    def _get_ci_code(self, sector_name: str) -> str:
        """行业名称 → CI代码"""
        return SECTOR_TO_CI.get(sector_name, '')

    # ─── 3. 缠论因子 ─────────────────────────────────────

    def get_chanlun_score(self, sector_name: str, trade_date: str) -> float:
        """
        从 sector_chanlun_cache 读取行业缠论评分，归一化到 0-100

        Args:
            sector_name: 行业名称（中文）
            trade_date: 交易日

        Returns:
            score 0-100，无数据返回 50
        """
        conn = self._get_conn()
        cur = conn.cursor()

        ci_code = self._get_ci_code(sector_name)
        if not ci_code:
            # 尝试直接用 sector_name 作为 ts_code 查
            pass

        # 先查 sector_chanlun_cache（按 ts_code 存储为 CI005001.CI 格式）
        cache_code = f"{ci_code}.CI" if ci_code else sector_name
        cur.execute("""
            SELECT structure_score, buy_sell_point, beichi_type, beichi_strength,
                   autumn_tiger, tiger_confidence, zoushi_type
            FROM sector_chanlun_cache
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 1
        """, (cache_code, trade_date))
        row = cur.fetchone()
        cur.close()

        if row and row.get('structure_score') is not None:
            ss = float(row['structure_score'])
            bs = row.get('buy_sell_point', 'none') or 'none'
            bt = row.get('beichi_type', 'none') or 'none'
            bstr = float(row.get('beichi_strength', 0) or 0)
            at = bool(row.get('autumn_tiger', 0))

            # 买卖点加成
            bs_boost = {
                'buy3': 15, 'buy2': 8, 'buy1': 3,
                'sell3': -15, 'sell2': -8, 'sell1': -3,
            }.get(bs, 0)

            # 背驰加成
            if bt == 'bottom' and bstr > 40:
                bs_boost += 10
            elif bt == 'top' and bstr > 40:
                bs_boost -= 10

            # 秋老虎加成
            if at:
                bs_boost += 10

            score = max(0, min(100, ss + bs_boost))
            return round(score, 1)

        # 无缠论数据：中性
        return 50.0

    # ─── 4. 季节因子 ─────────────────────────────────────

    def calc_season_factor(self, sector_name: str, season_state: Dict) -> float:
        """
        计算季节因子评分

        逻辑:
        - 基础分 50
        - +SEASON_SECTOR_SCORE[season] 中的修正
        - +混沌子态修正
        - +季节强度修正

        Args:
            sector_name: 行业名
            season_state: get_current_season() 结果

        Returns:
            季节因子评分 0-100
        """
        season = season_state.get('season', 'chaos')
        raw_score = float(season_state.get('raw_score', 0))
        chaos_sub = season_state.get('chaos_subtype', '') or ''

        base = 50

        # 季节修正
        season_map = SEASON_SECTOR_SCORE.get(season, {})
        base += season_map.get(sector_name, 0)

        # 季节强度修正
        if season in ('spring', 'summer') and raw_score > 3:
            if sector_name in ('电子', '计算机', '有色金属', '基础化工'):
                base += 2
        elif season in ('autumn', 'winter') and raw_score < -3:
            if sector_name in ('银行', '电力及公用事业', '煤炭', '食品饮料', '医药'):
                base += 2

        # 混沌子态修正
        if chaos_sub:
            chaos_adj = CHAOS_SUBTYPE_ADJ.get(chaos_sub, {})
            base += chaos_adj.get(sector_name, 0)

        return max(0, min(100, base))

    # ─── 5. 资金因子（量比） ──────────────────────────────

    def calc_money_factor(self, sector_name: str, trade_date: str, lookback: int = 5) -> float:
        """
        计算资金因子——从 sector_index_daily 最近5日量比

        方法:
        1. 取行业指数最近5日成交量 vs 前20日均量
        2. 计算量比 → 归一化到0-100

        Args:
            sector_name: 行业名
            trade_date: 交易日
            lookback: 比较基准天数

        Returns:
            资金因子评分 0-100
        """
        conn = self._get_conn()
        cur = conn.cursor()

        ci_code = self._get_ci_code(sector_name)
        if not ci_code:
            cur.close()
            return 50.0

        search_code = f"{ci_code}.CI"

        # 取最近 lookback 日的 vol
        cur.execute("""
            SELECT vol, amount, trade_date
            FROM sector_index_daily
            WHERE ts_code=%s AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT %s
        """, (search_code, trade_date, lookback + 20))

        rows = cur.fetchall()
        cur.close()

        if len(rows) < lookback + 1:
            return 50.0

        # 最近5日平均vol
        recent_vols = [float(r['vol']) for r in rows[:lookback]]
        recent_avg = sum(recent_vols) / len(recent_vols) if recent_vols else 0

        # 前20日平均vol（基准）
        base_vols = [float(r['vol']) for r in rows[lookback:]]
        base_avg = sum(base_vols) / len(base_vols) if base_vols else 0

        if base_avg <= 0 or recent_avg <= 0:
            return 50.0

        vol_ratio = recent_avg / base_avg

        # 量比归一化
        if vol_ratio <= 0.3:
            money_score = 0
        elif vol_ratio <= 0.5:
            money_score = (vol_ratio - 0.3) / 0.2 * 20
        elif vol_ratio <= 0.8:
            money_score = 20 + (vol_ratio - 0.5) / 0.3 * 20
        elif vol_ratio <= 1.0:
            money_score = 40 + (vol_ratio - 0.8) / 0.2 * 10
        elif vol_ratio <= 1.3:
            money_score = 50 + (vol_ratio - 1.0) / 0.3 * 15
        elif vol_ratio <= 1.7:
            money_score = 65 + (vol_ratio - 1.3) / 0.4 * 20
        elif vol_ratio <= 3.0:
            money_score = 85 + (vol_ratio - 1.7) / 1.3 * 15
        else:
            money_score = 100

        return round(max(0, min(100, money_score)), 1)

    # ─── 6. 综合评分 ─────────────────────────────────────

    def calc_composite_score(self, sector_name: str, trade_date: str,
                             season_state: Dict, weights: Dict[str, float]) -> Dict:
        """
        sector_rotation_score_ci 单行业综合评分

        三因子融合:
            composite = chanlun * w_chanlun + season * w_season + money * w_money

        Returns:
            {
                'chanlun_score': float,
                'season_score': float,
                'money_score': float,
                'composite_score': float,
            }
        """
        cl = self.get_chanlun_score(sector_name, trade_date)
        ss = self.calc_season_factor(sector_name, season_state)
        ms = self.calc_money_factor(sector_name, trade_date)

        composite = (cl * weights['chanlun'] +
                     ss * weights['season'] +
                     ms * weights['money'])
        composite = round(max(0, min(100, composite)), 1)

        return {
            'chanlun_score': cl,
            'season_score': ss,
            'money_score': ms,
            'composite_score': composite,
        }

    # ─── 7. 全行业评分 ────────────────────────────────────

    def score_all_sectors(self) -> Dict:
        """
        全行业三因子评分

        Returns:
            {
                'trade_date': str,
                'season_state': {...},
                'weight_mode': str,
                'weights': {'chanlun': float, 'season': float, 'money': float},
                'results': {
                    sector_name: {
                        'chanlun_score': float,
                        'season_score': float,
                        'money_score': float,
                        'composite_score': float,
                        'rank': int,
                    },
                },
                'ranking': [sector_name, ...],
            }
        """
        actual_date = self._get_latest_trade_date()
        self.trade_date = actual_date
        print(f"📅 方案A(中信指数)评分: {actual_date}")

        # 1. 读取季节状态
        season_state = self.get_current_season()
        season = season_state['season']
        raw_score = season_state['raw_score']
        confidence = season_state['confidence']
        print(f"🌤️  市场季节: {season} | 得分: {raw_score:+.1f} | 置信度: {confidence:.0%}")

        # 2. 确定权重模式
        mode_name, weights = get_weight_mode(season, raw_score, confidence)
        print(f"⚖️  权重模式: {mode_name} | 缠论:{weights['chanlun']:.0%} 季节:{weights['season']:.0%} 资金:{weights['money']:.0%}")

        # 3. 获取行业列表
        sectors = self.get_ci_sectors()
        print(f"📋 中信一级行业数: {len(sectors)}")

        # 4. 批量评分
        results = {}
        print("  📊 计算三因子评分...")
        for i, sector in enumerate(sectors):
            r = self.calc_composite_score(sector, actual_date, season_state, weights)
            results[sector] = r
            if (i + 1) % 10 == 0:
                print(f"    ✅ {i+1}/{len(sectors)}")

        # 5. 排序
        ranking = sorted(results.keys(),
                         key=lambda s: results[s]['composite_score'],
                         reverse=True)
        for i, sector in enumerate(ranking, 1):
            results[sector]['rank'] = i

        # 6. 打印TOP10
        print(f"\n{'='*60}")
        print(f"🏆 (方案A) 板块轮动评分 TOP 10")
        print(f"{'='*60}")
        for i, sector in enumerate(ranking[:10], 1):
            r = results[sector]
            print(f"  {i:2d}. {sector:12s} | 综合:{r['composite_score']:5.1f} | "
                  f"缠论:{r['chanlun_score']:5.1f} 季节:{r['season_score']:5.1f} 资金:{r['money_score']:5.1f}")

        return {
            'trade_date': actual_date,
            'season_state': season_state,
            'weight_mode': mode_name,
            'weights': weights,
            'results': results,
            'ranking': ranking,
        }

    # ─── 8. 入库 ──────────────────────────────────────────

    def save_to_db(self, result: Dict) -> int:
        """
        评分结果写入 sector_rotation_score 表

        Returns:
            写入行数
        """
        conn = self._get_conn()
        cur = conn.cursor()

        # 确保表存在
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_rotation_score (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(16) NOT NULL COMMENT '行业CI代码',
                trade_date DATE NOT NULL,
                season VARCHAR(20) COMMENT '当前季节',
                weight_mode VARCHAR(20) COMMENT '权重模式',
                chanlun_score DECIMAL(5,2) COMMENT '缠论因子分',
                season_score DECIMAL(5,2) COMMENT '季节因子分',
                money_score DECIMAL(5,2) COMMENT '资金因子分',
                composite_score DECIMAL(5,2) COMMENT '综合评分',
                rank_pos INT COMMENT '排名',
                prev_rank_c INT COMMENT '上周排名',
                rank_change_c INT COMMENT '排名变化',
                advice VARCHAR(20) COMMENT '调仓建议 buy/sell/hold',
                reason_chain VARCHAR(200) COMMENT '推理链',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_ci_date (ts_code, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()

        season = result['season_state']['season']
        weight_mode = result['weight_mode']
        trade_date = result['trade_date']
        ranking = result['ranking']

        # 获取上周排名
        prev_ranks = self._get_prev_week_ranks(trade_date)

        saved = 0
        for sector in ranking:
            r = result['results'][sector]
            ci_code = self._get_ci_code(sector)
            ts_code = f"{ci_code}.CI" if ci_code else sector

            prev_info = prev_ranks.get(sector, {})
            prev_rank = prev_info.get('rank', -1)
            rank_change = r['rank'] - prev_rank if prev_rank > 0 else 0

            # 调仓建议
            advice = self._generate_advice(r['composite_score'], prev_info.get('composite_score', None))

            # 推理链
            reason = self._build_reason_chain(
                season, weight_mode, sector,
                r['chanlun_score'], r['season_score'], r['money_score'],
                rank_change
            )

            cur.execute("""
                INSERT INTO sector_rotation_score
                    (ts_code, trade_date, season, weight_mode,
                     chanlun_score, season_score, money_score, composite_score,
                     rank_pos, prev_rank_c, rank_change_c,
                     advice, reason_chain)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    season=VALUES(season),
                    weight_mode=VALUES(weight_mode),
                    chanlun_score=VALUES(chanlun_score),
                    season_score=VALUES(season_score),
                    money_score=VALUES(money_score),
                    composite_score=VALUES(composite_score),
                    rank_pos=VALUES(rank_pos),
                    prev_rank_c=VALUES(prev_rank_c),
                    rank_change_c=VALUES(rank_change_c),
                    advice=VALUES(advice),
                    reason_chain=VALUES(reason_chain)
            """, (
                ts_code, trade_date, season, weight_mode,
                r['chanlun_score'], r['season_score'], r['money_score'],
                r['composite_score'],
                r['rank'], prev_rank if prev_rank > 0 else None, rank_change if rank_change else None,
                advice, reason,
            ))
            saved += 1

        conn.commit()
        cur.close()
        return saved

    def _get_prev_week_ranks(self, trade_date: str) -> Dict[str, Dict]:
        """获取上周同行业排名"""
        dt = datetime.strptime(trade_date[:10], '%Y-%m-%d').date()
        prev_week = dt - timedelta(days=7)

        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT ts_code, rank_pos, composite_score
            FROM sector_rotation_score
            WHERE trade_date = %s
        """, (prev_week.isoformat(),))
        rows = cur.fetchall()
        cur.close()

        result = {}
        # 将 ts_code (CI005001.CI) 映射回中文名
        for r in rows:
            code = r['ts_code']
            # 去掉.CI后缀
            base_code = code.replace('.CI', '')
            sector_name = CI_INDEX_MAP.get(base_code, code)
            result[sector_name] = {
                'rank': int(r['rank_pos']) if r.get('rank_pos') else -1,
                'composite_score': float(r['composite_score']) if r.get('composite_score') else None,
            }
        return result

    def _generate_advice(self, current_score: float, prev_score: Optional[float]) -> str:
        """生成调仓建议"""
        if current_score >= 75:
            return 'buy'
        elif current_score >= 60:
            if prev_score is not None and current_score > prev_score:
                return 'buy'
            return 'hold'
        elif current_score >= 40:
            if prev_score is not None and current_score < prev_score:
                return 'sell'
            return 'hold'
        else:
            return 'sell'

    def _build_reason_chain(self, season: str, weight_mode: str,
                            sector: str, chanlun: float,
                            season_factor: float, money: float,
                            rank_change: int) -> str:
        """构建推理链"""
        parts = [f"{season}+{weight_mode}"]

        if chanlun >= 75:
            parts.append("缠论强势")
        elif chanlun >= 60:
            parts.append("缠论稳定")
        elif chanlun <= 30:
            parts.append("缠论弱势")

        if season_factor >= 55:
            parts.append("季节利好")
        elif season_factor <= 45:
            parts.append("季节利空")

        if money >= 70:
            parts.append("资金流入")
        elif money <= 30:
            parts.append("资金流出")

        if rank_change > 3:
            parts.append("排名↑")
        elif rank_change < -3:
            parts.append("排名↓")

        return "+".join(parts)


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行入口"""
    import argparse
    ap = argparse.ArgumentParser(description='中信行业指数三因子评分（方案A）')
    ap.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    ap.add_argument('--save', action='store_true', help='保存到数据库')
    ap.add_argument('--output', type=str, default='text', choices=['text', 'json'])
    args = ap.parse_args()

    engine = SectorRotationScoreCI(args.date)
    result = engine.score_all_sectors()

    if args.save:
        saved = engine.save_to_db(result)
        print(f"\n💾 已保存 {saved} 条到 sector_rotation_score")

    if args.output == 'json':
        import json as j
        print(j.dumps({
            'trade_date': result['trade_date'],
            'season_state': result['season_state'],
            'weight_mode': result['weight_mode'],
            'weights': result['weights'],
            'ranking': result['ranking'],
            'results': {k: {kk: vv for kk, vv in v.items() if kk != 'rank'}
                       for k, v in result['results'].items()},
        }, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
