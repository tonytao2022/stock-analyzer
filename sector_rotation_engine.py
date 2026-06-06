#!/usr/bin/env python3
"""
板块轮动三因子评分引擎 v1.0
============================
MAY三因子融合模型 — 缠论因子×0.45 + 季节因子×0.35 + 资金因子×0.20
（权重根据season_state动态切换）

设计者: MAY + Tony
数据源: stock_basic.industry → 行业聚合 → sector_chanlun_cache / season_state / daily_kline
频率: 日频（收盘后16:00跑一次）

架构:
  SectorRotationEngine
  ├─ fetch_all_industries()      — 从stock_basic获取全部行业名单
  ├─ calc_chanlun_factor()       — 缠论因子（个股缠论分按行业聚合）
  ├─ calc_season_factor()        — 季节因子（season_state为每个行业打基础分）
  ├─ calc_money_factor()         — 资金因子（daily_kline量能潮汐）
  ├─ get_weight_mode()           — 从season_state读取动态权重
  ├─ score_all_industries()      — 三因子加权融合→行业综合评分
  └─ save_sector_cache()         — 写入sector_score_cache表

依赖:
  - db_config.get_connection()
  - season_engine (season_state表)
  - chanlun_structure / trend_score (缠论分)
  - daily_kline (成交量/额)
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
# 行业分类映射（111个细分 → 核心轮动板块聚合）
# ═══════════════════════════════════════════════════════════════

# 聚合组: 将111个细分行业映射到18个核心轮动板块
SECTOR_GROUP_MAP = {
    # 🖥️ 科技/成长
    '半导体': '半导体',
    '元器件': '电子',
    '电器仪表': '电子',
    '软件服务': '计算机',
    '互联网': '计算机',
    '通信设备': '通信',
    '电信运营': '通信',
    'IT设备': '计算机',

    # ⚡ 新能源/电力
    '电气设备': '电力设备',
    '新型电力': '电力',
    '水力发电': '电力',
    '火力发电': '电力',
    '供气供热': '公用事业',
    '水务': '公用事业',
    '环境保护': '环保',

    # 🏦 金融
    '银行': '银行',
    '证券': '非银金融',
    '保险': '非银金融',
    '多元金融': '非银金融',

    # 🏭 周期/制造
    '煤炭开采': '煤炭',
    '石油开采': '石油石化',
    '石油加工': '石油石化',
    '石油贸易': '石油石化',
    '普钢': '钢铁',
    '钢加工': '钢铁',
    '特种钢': '钢铁',
    '铜': '有色',
    '铝': '有色',
    '铅锌': '有色',
    '黄金': '有色',
    '小金属': '有色',
    '矿物制品': '化工',
    '化工原料': '化工',
    '化工机械': '化工',
    '化纤': '化工',
    '染料涂料': '化工',
    '橡胶': '化工',
    '塑料': '化工',
    '水泥': '建材',
    '玻璃': '建材',
    '陶瓷': '建材',
    '其他建材': '建材',
    '工程机械': '机械',
    '专用机械': '机械',
    '机械基件': '机械',
    '机床制造': '机械',
    '纺织机械': '机械',
    '轻工机械': '机械',
    '运输设备': '军工',
    '船舶': '军工',
    '航空': '军工',
    '焦炭加工': '煤炭',

    # 🚗 消费/汽车
    '汽车整车': '汽车',
    '汽车配件': '汽车',
    '汽车服务': '汽车',
    '摩托车': '汽车',
    '家用电器': '家电',
    '电器连锁': '家电',

    # 🏠 地产/基建
    '全国地产': '地产',
    '区域地产': '地产',
    '房产服务': '地产',
    '园区开发': '地产',
    '建筑工程': '建筑',
    '装修装饰': '建筑',
    '铁路': '建筑',
    '公路': '建筑',
    '路桥': '建筑',
    '港口': '交运',
    '机场': '交运',
    '水运': '交运',
    '空运': '交运',
    '仓储物流': '交运',
    '公共交通': '交运',
    '商贸代理': '商业',

    # 🍶 消费
    '白酒': '白酒',
    '啤酒': '食品饮料',
    '红黄酒': '食品饮料',
    '软饮料': '食品饮料',
    '乳制品': '食品饮料',
    '食品': '食品饮料',
    '饲料': '农业',
    '农业综合': '农业',
    '种植业': '农业',
    '渔业': '农业',
    '林业': '农业',
    '农用机械': '农业',
    '农药化肥': '农业',
    '纺织': '纺织服装',
    '服饰': '纺织服装',
    '日用化工': '纺织服装',
    '家居用品': '轻工',
    '造纸': '轻工',
    '文教休闲': '轻工',
    '旅游景点': '旅游',
    '旅游服务': '旅游',
    '酒店餐饮': '旅游',
    '商品城': '商业',
    '百货': '商业',
    '超市连锁': '商业',
    '批发业': '商业',
    '其他商业': '商业',
    '医药商业': '医药',
    '中成药': '医药',
    '化学制药': '医药',
    '生物制药': '医药',
    '医疗保健': '医药',
    '广告包装': '传媒',
    '影视音像': '传媒',
    '出版业': '传媒',
    '互联网(传媒)': '传媒',

    # 兜底
    '综合类': '综合',
    '航空(运输)': '军工',
    '多元金融(金融)': '非银金融',
    '矿物制品(材料)': '化工',
    '电器连锁(零售)': '商业',
}

# 行业核心板块列表（18个核心）
CORE_SECTORS = [
    '半导体', '电子', '计算机', '通信',           # 科技
    '电力设备', '电力', '公用事业', '环保',       # 新能源
    '银行', '非银金融',                           # 金融
    '煤炭', '石油石化', '钢铁', '有色', '化工',    # 周期
    '建材', '机械', '军工',                       # 制造
    '汽车', '家电',                               # 消费制造
    '地产', '建筑', '交运',                       # 基建
    '白酒', '食品饮料', '农业', '纺织服装',        # 消费
    '轻工', '旅游',                               # 轻消费
    '商业', '医药', '传媒',                       # 服务/医药
    '综合',                                       # 兜底
]

# 行业聚合映射（细分子行业 → 核心板块）
# 如果行业不在SECTOR_GROUP_MAP中, 保留原始行业名
def map_to_core_sector(sub_industry: str) -> str:
    """将细分行业映射到核心板块"""
    return SECTOR_GROUP_MAP.get(sub_industry, sub_industry)


# ═══════════════════════════════════════════════════════════════
# 季节因子——行业基础分映射（MAY方案）
# ═══════════════════════════════════════════════════════════════

# 每个季节下各核心板块的基础分修正
# 范围: -5 ~ +5, 对应season_factor的基础分偏移
SEASON_SECTOR_SCORE = {
    # 🌸 春季（进攻期）—— 科技+周期唱主角
    'spring': {
        '半导体': 5, '电子': 4, '计算机': 4, '通信': 3,
        '有色': 4, '化工': 3, '钢铁': 2,
        '机械': 3, '军工': 2,
        '电力设备': 3, '环保': 2,
        '传媒': 2,
        # 防御品种减分
        '银行': -3, '非银金融': -2, '公用事业': -2,
        '煤炭': -1, '石油石化': -1,
        '白酒': -1, '食品饮料': -1,
        '建筑': -1,
    },
    # ☀️ 夏季（持有期）—— 科技延续+消费补涨
    'summer': {
        '半导体': 5, '电子': 5, '计算机': 4, '通信': 3,
        '汽车': 3, '家电': 2,
        '白酒': 2, '食品饮料': 2,
        '医药': 3, '电力设备': 2,
        '军工': 1,
        # 减分
        '银行': -2, '煤炭': -2, '石油石化': -2,
        '钢铁': -1, '公用事业': -1,
    },
    # 🍂 秋季（防守期）—— 防御+金融+公共
    'autumn': {
        '银行': 4, '非银金融': 3, '公用事业': 4,
        '煤炭': 3, '石油石化': 3,
        '建筑': 2, '交运': 3,
        '家电': 2, '食品饮料': 2,
        # 减分
        '半导体': -4, '电子': -4, '计算机': -3, '通信': -3,
        '有色': -2, '化工': -2,
        '电力设备': -3, '汽车': -2,
        '传媒': -3, '军工': -2,
    },
    # ❄️ 冬季（休眠期）—— 纯防御
    'winter': {
        '银行': 5, '公用事业': 5,
        '煤炭': 4, '石油石化': 4,
        '公路铁路交通': 3,
        '食品饮料': 3, '医药': 2,
        '交运': 3, '建筑': 2,
        # 所有成长/周期品种大幅减分
        '半导体': -5, '电子': -5, '计算机': -5, '通信': -4,
        '有色': -4, '化工': -3, '钢铁': -3,
        '电力设备': -4, '汽车': -4,
        '地产': -3, '非银金融': -2,
        '传媒': -4, '军工': -4,
        '机械': -3, '建材': -2,
    },
    # 🌪️ 混沌（观望期）—— 中性偏向防御
    'chaos': {
        '银行': 2, '公用事业': 2,
        '食品饮料': 2, '医药': 2,
        '煤炭': 1, '石油石化': 1,
        '半导体': -2, '电子': -2, '计算机': -2,
        '有色': -1, '化工': -1, '钢铁': -1,
        '电力设备': -2, '汽车': -1,
        '传媒': -2,
    },
}

# 中性状态下各行业基础分
BASE_SECTOR_SCORE = {s: 50 for s in CORE_SECTORS}


# ═══════════════════════════════════════════════════════════════
# 权重切换
# ═══════════════════════════════════════════════════════════════

def get_weight_mode(season: str, raw_score: float, confidence: float) -> Tuple[str, Dict[str, float]]:
    """
    根据季节状态和置信度选择权重模式

    Args:
        season: 季节 (spring/summer/autumn/winter/chaos)
        raw_score: 季节原始分 (season_state.raw_score)
        confidence: 置信度 (season_state.confidence)

    Returns:
        (mode_name, weights_dict)
        weights_dict: {'chanlun': float, 'season': float, 'money': float}
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
# 主引擎
# ═══════════════════════════════════════════════════════════════

class SectorRotationEngine:
    """板块轮动三因子评分引擎"""

    def __init__(self, trade_date: Optional[str] = None):
        self.trade_date = trade_date or date.today().isoformat()
        self.cache = {}

    # ─── 工具 ───────────────────────────────────────────

    def _get_conn(self):
        return get_connection()

    def _get_latest_trade_date(self) -> str:
        """获取最新交易日"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(trade_date) FROM daily_kline 
            WHERE trade_date <= %s
        """, (self.trade_date,))
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            return row[0].isoformat() if hasattr(row[0], 'isoformat') else str(row[0])
        return self.trade_date

    def _normalize(self, value: float, min_val: float = 0, max_val: float = 100) -> float:
        """将值归一化到0-100区间"""
        if max_val <= min_val:
            return 50.0
        clamped = max(min_val, min(max_val, value))
        return round((clamped - min_val) / (max_val - min_val) * 100, 1)

    # ─── 1. 行业列表 ────────────────────────────────────

    def fetch_all_industries(self) -> List[str]:
        """
        从stock_basic获取全部行业列表 → 映射到核心板块

        Returns:
            核心板块名称列表（去重排序）
        """
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT industry FROM stock_basic 
            WHERE industry IS NOT NULL AND industry != ''
            ORDER BY industry
        """)
        raw_industries = [row['industry'] for row in cur.fetchall()]
        cur.close()

        # 映射到核心板块
        core_set = set()
        for ind in raw_industries:
            core = map_to_core_sector(ind)
            core_set.add(core)

        # 按CORE_SECTORS顺序排序（保持稳定性）
        ordered = [s for s in CORE_SECTORS if s in core_set]
        # 补充不在CORE_SECTORS中的
        extras = sorted(core_set - set(CORE_SECTORS))
        return ordered + extras

    def fetch_stock_codes_by_sector(self, sector: str) -> List[str]:
        """获取某个核心板块下所有股票代码"""
        conn = self._get_conn()
        cur = conn.cursor()

        # 先找映射到此核心板块的细分行业
        sub_industries = [k for k, v in SECTOR_GROUP_MAP.items() if v == sector]
        if not sub_industries:
            # 也可能sector本身就是细分行业名
            sub_industries = [sector]

        placeholders = ','.join(['%s'] * len(sub_industries))
        cur.execute(f"""
            SELECT ts_code FROM stock_basic
            WHERE industry IN ({placeholders})
        """, sub_industries)
        codes = [row['ts_code'] for row in cur.fetchall()]
        cur.close()
        return codes

    # ─── 2. 缠论因子 ────────────────────────────────────

    def calc_chanlun_factor(self, sector: str) -> float:
        """
        计算某个核心板块的缠论因子评分

        方法:
        1. 获取板块内所有股票的缠论结构评分
        2. 取中位数作为板块缠论分
        3. 归一化到0-100

        如果sector_chanlun_cache表已存在，优先从缓存读取
        """
        conn = self._get_conn()
        cur = conn.cursor()

        # 检查是否有缓存表
        cur.execute("SHOW TABLES LIKE 'sector_chanlun_cache'")
        has_cache = cur.fetchone() is not None

        if has_cache:
            cur.execute("""
                SELECT structure_score FROM sector_chanlun_cache
                WHERE sector_name=%s AND trade_date=%s
                ORDER BY trade_date DESC LIMIT 1
            """, (sector, self.trade_date))
            row = cur.fetchone()
            if row and row.get('structure_score') is not None:
                score = float(row['structure_score'])
                cur.close()
                return max(0, min(100, score))

        # 无缓存或缓存过期，实时计算
        codes = self.fetch_stock_codes_by_sector(sector)
        if not codes:
            cur.close()
            return 50.0  # 无数据返回中性

        # 取每只股票最新缠论结构分
        placeholders = ','.join(['%s'] * len(codes))
        cur.execute(f"""
            SELECT cl.ts_code, cl.structure_score, cl.buy_sell_point, cl.beichi_type, cl.beichi_strength
            FROM chanlun_structure cl
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) AS max_date
                FROM chanlun_structure
                WHERE ts_code IN ({placeholders}) AND trade_date <= %s
                GROUP BY ts_code
            ) latest ON cl.ts_code=latest.ts_code AND cl.trade_date=latest.max_date
        """, (*codes, self.trade_date))

        scores = []
        boost_total = 0
        count = 0
        for row in cur.fetchall():
            if row.get('structure_score') is not None:
                ss = float(row['structure_score'])
                # 买卖点加成
                bs = row.get('buy_sell_point', 'none') or 'none'
                bs_boost = {'buy3': 15, 'buy2': 8, 'buy1': 3,
                            'sell3': -15, 'sell2': -8, 'sell1': -3}.get(bs, 0)
                # 底背离加成
                bt = row.get('beichi_type', 'none') or 'none'
                bstr = float(row.get('beichi_strength', 0) or 0)
                if bt == 'bottom' and bstr > 40:
                    bs_boost += 10
                elif bt == 'top' and bstr > 40:
                    bs_boost -= 10

                adjusted = max(0, min(100, ss + bs_boost))
                scores.append(adjusted)
                boost_total += adjusted
                count += 1

        cur.close()

        if count == 0:
            return 50.0

        # 取中位数（防极端值影响）
        scores.sort()
        median_score = scores[len(scores) // 2] if scores else 50.0

        # 板块内置信度：高分/低分股票占比的纠偏
        high_ratio = sum(1 for s in scores if s >= 70) / count if count > 0 else 0
        if high_ratio > 0.4:
            median_score = min(100, median_score + 5)  # 大量高分=板块强势
        elif high_ratio < 0.1:
            median_score = max(0, median_score - 5)    # 几乎无高分=板块弱势

        return round(median_score, 1)

    def calc_batch_chanlun(self, sectors: List[str]) -> Dict[str, float]:
        """批量计算缠论因子（带缓存）"""
        result = {}
        for sector in sectors:
            result[sector] = self.calc_chanlun_factor(sector)
        return result

    # ─── 3. 季节因子 ────────────────────────────────────

    def get_season_state(self) -> Dict:
        """从season_state表读取最新季节判定"""
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

    def calc_season_factor(self, sector: str, season_state: Dict) -> float:
        """
        计算某个核心板块的季节因子评分

        方法:
        1. 基础分50
        2. +SEASON_SECTOR_SCORE[season]中的修正值
        3. 基于混沌子态微调
        4. 归一化到0-100

        Args:
            sector: 核心板块名
            season_state: get_season_state()的结果

        Returns:
            季节因子评分 0-100
        """
        season = season_state.get('season', 'chaos')
        raws = float(season_state.get('raw_score', 0))

        base = 50
        # 季节修正
        season_map = SEASON_SECTOR_SCORE.get(season, {})
        base += season_map.get(sector, 0)

        # 季节强度修正
        if season in ('spring', 'summer') and raws > 3:
            # 强春/夏季——进攻品种额外+2
            if sector in ('半导体', '电子', '计算机', '有色', '化工'):
                base += 2
        elif season in ('autumn', 'winter') and raws < -3:
            # 强秋/冬季——防御品种额外+2
            if sector in ('银行', '公用事业', '煤炭', '食品饮料', '医药'):
                base += 2

        # 混沌子态微调
        chaos_sub = season_state.get('chaos_subtype', '') or ''
        if chaos_sub == 'chaos_spring':
            # 偏多混沌: 科技品种+1
            if sector in ('半导体', '电子', '计算机', '通信', '电力设备', '汽车'):
                base += 1
        elif chaos_sub == 'chaos_autumn':
            # 偏空混沌: 防御品种+2
            if sector in ('银行', '公用事业', '煤炭', '食品饮料', '医药', '交运'):
                base += 1

        return max(0, min(100, base))

    def calc_batch_season(self, sectors: List[str], season_state: Dict) -> Dict[str, float]:
        """批量计算季节因子"""
        return {s: self.calc_season_factor(s, season_state) for s in sectors}

    # ─── 4. 资金因子（量能潮汐） ────────────────────────

    def calc_money_factor(self, sector: str, lookback: int = 5) -> float:
        """
        计算资金因子（量能潮汐）

        方法:
        1. 板块内所有股票最近5日平均量比（当前量/5日均量）
        2. 板块平均量比归一化到0-100
        3. 放量=资金流入, 缩量=资金流出

        Args:
            sector: 核心板块名
            lookback: 回看天数（用于计算均量基准）

        Returns:
            资金因子评分 0-100
        """
        conn = self._get_conn()
        cur = conn.cursor()

        codes = self.fetch_stock_codes_by_sector(sector)
        if not codes:
            cur.close()
            return 50.0

        placeholders = ','.join(['%s'] * len(codes))

        # 获取每只股票的最近N日量比
        cur.execute(f"""
            SELECT d.ts_code, d.vol, d.amount, d.volume_ratio
            FROM daily_kline d
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) AS max_date
                FROM daily_kline
                WHERE ts_code IN ({placeholders}) AND trade_date <= %s
                GROUP BY ts_code
            ) latest ON d.ts_code=latest.ts_code AND d.trade_date=latest.max_date
        """, (*codes, self.trade_date))

        vol_ratios = []
        amount_sum = 0.0
        count_with_data = 0

        for row in cur.fetchall():
            vr = row.get('volume_ratio')
            if vr is not None:
                vol_ratios.append(float(vr))
            amt = row.get('amount')
            if amt is not None:
                amount_sum += float(amt)
            count_with_data += 1

        cur.close()

        if not vol_ratios:
            return 50.0

        # 板块平均量比
        avg_vr = sum(vol_ratios) / len(vol_ratios)

        # 归一化：量比0.3≈0分, 量比1.0≈50分, 量比2.0≈100分
        # 使用分段线性：0.3→0, 0.5→20, 0.8→40, 1.0→50, 1.3→65, 1.7→85, 3.0→100
        if avg_vr <= 0.3:
            money_score = 0
        elif avg_vr <= 0.5:
            money_score = self._normalize(avg_vr, 0.3, 0.5) * 20 / 100
        elif avg_vr <= 0.8:
            money_score = 20 + self._normalize(avg_vr, 0.5, 0.8) * 20 / 100
        elif avg_vr <= 1.0:
            money_score = 40 + self._normalize(avg_vr, 0.8, 1.0) * 10 / 100
        elif avg_vr <= 1.3:
            money_score = 50 + self._normalize(avg_vr, 1.0, 1.3) * 15 / 100
        elif avg_vr <= 1.7:
            money_score = 65 + self._normalize(avg_vr, 1.3, 1.7) * 20 / 100
        elif avg_vr <= 3.0:
            money_score = 85 + self._normalize(avg_vr, 1.7, 3.0) * 15 / 100
        else:
            money_score = 100

        # 板块内一致性修正：高分股票占比高=资金共识强
        high_vr_ratio = sum(1 for vr in vol_ratios if vr > 1.2) / len(vol_ratios)
        if high_vr_ratio > 0.5:
            money_score += 5
        elif high_vr_ratio < 0.1:
            money_score -= 5

        return round(max(0, min(100, money_score)), 1)

    def calc_batch_money(self, sectors: List[str]) -> Dict[str, float]:
        """批量计算资金因子"""
        result = {}
        for sector in sectors:
            result[sector] = self.calc_money_factor(sector)
        return result

    # ─── 5. 三因子融合 ───────────────────────────────────

    def score_all_industries(self) -> Dict:
        """
        全行业三因子评分主流程

        Returns:
            {
                'trade_date': str,
                'season_state': {...},
                'weight_mode': str,
                'weights': {'chanlun': float, 'season': float, 'money': float},
                'sectors': {
                    sector_name: {
                        'chanlun_factor': float,
                        'season_factor': float,
                        'money_factor': float,
                        'composite_score': float,
                        'rank': int,
                    },
                    ...
                },
                'ranking': [sector_name, ...],  # 按总分降序
            }
        """
        actual_date = self._get_latest_trade_date()
        self.trade_date = actual_date
        print(f"📅 板块轮动评分: {actual_date}")

        # 1. 读取季节状态
        season_state = self.get_season_state()
        season = season_state['season']
        raw_score = season_state['raw_score']
        confidence = season_state['confidence']
        print(f"🌤️  市场季节: {season} | 得分: {raw_score:+.1f} | 置信度: {confidence:.0%}")

        # 2. 确定权重模式
        mode_name, weights = get_weight_mode(season, raw_score, confidence)
        print(f"⚖️  权重模式: {mode_name} | 缠论:{weights['chanlun']:.0%} 季节:{weights['season']:.0%} 资金:{weights['money']:.0%}")

        # 3. 获取行业列表
        sectors = self.fetch_all_industries()
        print(f"📋 核心板块数: {len(sectors)}")

        # 4. 批量计算三因子
        print("  📊 计算缠论因子...")
        chanlun_scores = self.calc_batch_chanlun(sectors)
        print("  🌤️  计算季节因子...")
        season_scores = self.calc_batch_season(sectors, season_state)
        print("  💰 计算资金因子...")
        money_scores = self.calc_batch_money(sectors)

        # 5. 加权融合
        sector_results = {}
        for sector in sectors:
            cl = chanlun_scores.get(sector, 50)
            ss = season_scores.get(sector, 50)
            ms = money_scores.get(sector, 50)

            composite = (cl * weights['chanlun'] +
                         ss * weights['season'] +
                         ms * weights['money'])
            composite = round(max(0, min(100, composite)), 1)

            sector_results[sector] = {
                'chanlun_factor': cl,
                'season_factor': ss,
                'money_factor': ms,
                'composite_score': composite,
            }

        # 6. 排序
        ranking = sorted(sector_results.keys(),
                         key=lambda s: sector_results[s]['composite_score'],
                         reverse=True)

        for i, sector in enumerate(ranking, 1):
            sector_results[sector]['rank'] = i

        print(f"\n{'='*60}")
        print(f"🏆 板块轮动评分 TOP 10")
        print(f"{'='*60}")
        for i, sector in enumerate(ranking[:10], 1):
            r = sector_results[sector]
            print(f"  {i:2d}. {sector:8s} | 综合:{r['composite_score']:5.1f} | "
                  f"缠论:{r['chanlun_factor']:5.1f} 季节:{r['season_factor']:5.1f} 资金:{r['money_factor']:5.1f}")

        return {
            'trade_date': actual_date,
            'season_state': season_state,
            'weight_mode': mode_name,
            'weights': weights,
            'sectors': sector_results,
            'ranking': ranking,
        }

    # ─── 6. 缓存入库 ────────────────────────────────────

    def save_to_db(self, result: Dict) -> int:
        """
        将评分结果写入sector_score_cache表

        Returns:
            写入行数
        """
        conn = self._get_conn()
        cur = conn.cursor()

        # 检查并创建表
        cur.execute("SHOW TABLES LIKE 'sector_score_cache'")
        if not cur.fetchone():
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
            conn.commit()

        saved = 0
        for sector in result['ranking']:
            r = result['sectors'][sector]
            cur.execute("""
                INSERT INTO sector_score_cache
                    (trade_date, sector_name, chanlun_factor, season_factor,
                     money_factor, composite_score, sector_rank,
                     weight_mode, season)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    chanlun_factor=VALUES(chanlun_factor),
                    season_factor=VALUES(season_factor),
                    money_factor=VALUES(money_factor),
                    composite_score=VALUES(composite_score),
                    sector_rank=VALUES(sector_rank),
                    weight_mode=VALUES(weight_mode),
                    season=VALUES(season)
            """, (
                result['trade_date'], sector,
                r['chanlun_factor'], r['season_factor'],
                r['money_factor'], r['composite_score'],
                r['rank'],
                result['weight_mode'],
                result['season_state']['season'],
            ))
            saved += 1

        conn.commit()
        cur.close()
        return saved


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行入口：执行板块轮动评分并输出报告"""
    import argparse
    parser = argparse.ArgumentParser(description='板块轮动三因子评分引擎')
    parser.add_argument('--date', type=str, help='指定日期 YYYY-MM-DD')
    parser.add_argument('--save', action='store_true', help='保存到数据库')
    parser.add_argument('--output', type=str, default='text',
                        choices=['text', 'json'], help='输出格式')
    args = parser.parse_args()

    engine = SectorRotationEngine(args.date)
    result = engine.score_all_industries()

    if args.save:
        saved = engine.save_to_db(result)
        print(f"\n💾 已保存 {saved} 条到数据库")

    if args.output == 'json':
        import json as j
        print(j.dumps({
            'trade_date': result['trade_date'],
            'season_state': result['season_state'],
            'weight_mode': result['weight_mode'],
            'weights': result['weights'],
            'ranking': result['ranking'],
            'sectors': {k: v for k, v in result['sectors'].items()},
        }, ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()