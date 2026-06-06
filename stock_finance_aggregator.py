#!/usr/bin/env python3
"""
stock_finance_aggregator.py — 个股深度分析数据聚合层
=====================================================
聚合 Tushare 多接口数据返回前端用的全维度财务/技术因子字典。

暴露:
  get_stock_finance_overview(ts_code)  → dict
  get_stock_factors(ts_code)           → dict

数据源:
  income / balancesheet / cashflow / fina_indicator
  dividend / forecast / daily_basic / stk_factor / moneyflow
"""
import os, sys, time, json, functools, logging, math
from datetime import datetime, date, timedelta
from typing import Optional

import tushare as ts
import pandas as pd


def _sf(v, scale=1):
    """safe_float: 处理NaN/None,返回float或0"""
    if scale is None: scale = 1
    try:
        val = float(v) if v is not None else 0
        return 0 if math.isnan(val) else round(val / scale, 2)
    except (ValueError, TypeError):
        return 0

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection

# ─── 日志 ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s %(message)s')
logger = logging.getLogger('stock_aggregator')

# ─── 全局配置 ────────────────────────────────────────────────
DATA_ERROR_MARKER = -1
MAX_RETRIES = 0
RETRY_WAIT = 15        # 秒

# 缓存 TTL（秒）
CACHE_TTL = {
    'finance': 3600,     # 财务数据 1h
    'factors': 600,      # 技术因子 10min
    'forecast': 7200,    # 业绩预告 2h
    'dividend': 86400,   # 分红数据 1d
}

# ─── Tushare 初始化 ──────────────────────────────────────────
_pro_instance: Optional[ts.pro_api] = None

def _get_pro():
    global _pro_instance
    if _pro_instance is not None:
        return _pro_instance
    token = ""
    # 1) 从环境变量
    token = os.environ.get('TUSHARE_TOKEN', '')
    # 2) 从 DB api_credentials
    if not token:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT api_key FROM api_credentials WHERE name='TUSHARE_TOKEN' AND is_active=1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                token = row['api_key'] if isinstance(row, dict) else row[0]
        except Exception as e:
            logger.warning(f"读取DB TUSHARE_TOKEN失败: {e}")
    if not token:
        raise RuntimeError("TUSHARE_TOKEN 未配置（env / api_credentials 都为空）")
    ts.set_token(token)
    _pro_instance = ts.pro_api()
    logger.info("Tushare Pro 初始化完成")
    return _pro_instance


# ════════════════════════════════════════════════════════════
# 节流装饰器 — Tushare 1.1秒频次限制
# ════════════════════════════════════════════════════════════
_last_call_time = 0.0

def throttle(min_interval: float = 1.1):
    """确保两次调用间隔至少 min_interval 秒"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            global _last_call_time
            now = time.time()
            elapsed = now - _last_call_time
            if elapsed < min_interval:
                sleep_s = min_interval - elapsed
                time.sleep(sleep_s)
            _last_call_time = time.time()
            return func(*args, **kwargs)
        return wrapper
    return decorator


# ════════════════════════════════════════════════════════════
# 重试 + -1标记
# ════════════════════════════════════════════════════════════
def _safe_call(func, *args, name="api_call", **kwargs):
    """
    铁律: API失败→等15秒→重试3次→仍失败返回空DataFrame
    Returns: (DataFrame, success_bool)
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = func(*args, **kwargs)
            if result is not None and (isinstance(result, pd.DataFrame) and not result.empty):
                return result, True
            if attempt < MAX_RETRIES:
                logger.warning(f"  ⚠ {name} 第{attempt+1}次返回空, {RETRY_WAIT}s后重试")
                time.sleep(RETRY_WAIT)
        except Exception as e:
            if attempt < MAX_RETRIES:
                logger.warning(f"  ⚠ {name} 第{attempt+1}次失败: {e}, {RETRY_WAIT}s后重试")
                time.sleep(RETRY_WAIT)
            else:
                logger.error(f"  ❌ {name} 重试{MAX_RETRIES}次后仍失败: {e}")
                return pd.DataFrame(), False
    return pd.DataFrame(), False


# ════════════════════════════════════════════════════════════
# 缓存层
# ════════════════════════════════════════════════════════════
_cache = {}  # key -> (timestamp, data)

def _cache_get(key: str, ttl: int) -> Optional[dict]:
    """带TTL的内存缓存获取"""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts_val, data = entry
    if time.time() - ts_val > ttl:
        del _cache[key]
        return None
    return data

def _cache_set(key: str, data, ttl: int):
    _cache[key] = (time.time(), data)


# ════════════════════════════════════════════════════════════
# Tushare 数据查询（已含节流）
# ════════════════════════════════════════════════════════════
@throttle(1.1)
def _ts_query(api_method, **params):
    """带节流+重试的统一tushare查询入口"""
    pro = _get_pro()
    func = getattr(pro, api_method, None)
    if func is None:
        logger.error(f"Tushare 无此接口: {api_method}")
        return pd.DataFrame(), False
    return _safe_call(func, **params, name=f"tushare.{api_method}")


# ════════════════════════════════════════════════════════════
# 主函数1: 个股财务概览
# ════════════════════════════════════════════════════════════
def get_stock_finance_overview(ts_code: str) -> dict:
    """
    返回全维度财务概览字典。
    ts_code 示例: '000001.SZ', '600519.SH'
    """
    cache_key = f"finance::{ts_code}"
    cached = _cache_get(cache_key, CACHE_TTL['finance'])
    if cached:
        logger.info(f"[cache] 命中财务概览 {ts_code}")
        return cached

    logger.info(f"📊 开始聚合财务数据: {ts_code}")

    # 统一参数字典
    today_str = date.today().isoformat()
    current_year = date.today().year

    result = {
        'ts_code': ts_code,
        'query_time': today_str,
        'basic_info': {},                # PE/PB/总市值
        'latest_indicators': {},         # 最新ROE/毛利率/资产负债率/每股收益
        'annual_trend': [],              # 近5年营收趋势
        'capital_structure': [],         # 近4季资本结构
        'dividend_records': [],          # 分红记录
        'forecast_list': [],             # 业绩预告
        'error': None,
    }

    try:
        # ── 1. 每日指标（最新PE/PB/总市值） ──
        df_daily, ok = _ts_query('daily_basic', ts_code=ts_code, trade_date=today_str)
        if not ok or df_daily.empty:
            # 回退到最近交易日
            df_daily, ok = _ts_query('daily_basic', ts_code=ts_code, start_date=(date.today() - timedelta(days=10)).isoformat(), end_date=today_str)
        if ok and not df_daily.empty:
            latest = df_daily.iloc[0]
            result['basic_info'] = {
                'pe': float(latest.get('pe', 0) or 0),
                'pb': float(latest.get('pb', 0) or 0),
                'total_mv': float(latest.get('total_mv', 0) or 0) / 1e8 if latest.get('total_mv') else 0,
                'circ_mv': float(latest.get('circ_mv', 0) or 0) / 1e8 if latest.get('circ_mv') else 0,
                'turnover_rate': float(latest.get('turnover_rate', 0) or 0),
                'volume_ratio': float(latest.get('volume_ratio', 0) or 0),
                'trade_date': str(latest.get('trade_date', '')),
            }

        # ── 2. 财务指标（最新ROE/毛利率/每股收益） ──
        # 取最近4个季度
        df_fina, ok = _ts_query('fina_indicator', ts_code=ts_code,
                                start_date=f"{current_year-2}0101",
                                end_date=today_str)
        if ok and not df_fina.empty:
            df_sorted = df_fina.sort_values('end_date', ascending=False)
            latest_row = df_sorted.iloc[0] if len(df_sorted) > 0 else None
            if latest_row is not None:
                result['latest_indicators'] = {
                    'roe': float(latest_row.get('roe', 0) or 0),
                    'gross_profit_margin': float(latest_row.get('gross_profit_margin', 0) or 0),
                    'debt_to_assets': float(latest_row.get('debt_to_assets', 0) or 0),
                    'eps': float(latest_row.get('eps', 0) or 0),
                    'bps': float(latest_row.get('bps', 0) or 0),
                    'end_date': str(latest_row.get('end_date', '')),
                    'roa': float(latest_row.get('roa', 0) or 0),
                    'roe_dt': float(latest_row.get('roe_dt', 0) or 0),  # 摊薄ROE
                    'profit_dedt': float(latest_row.get('profit_dedt', 0) or 0),  # 扣非净利润
                }
            # 近5年ROE趋势
            yearly_roe = df_sorted.drop_duplicates(subset=['end_date']).head(20)
            result['roe_trend'] = [
                {'end_date': str(r['end_date']),
                 'roe': _sf(r.get('roe', 0) or 0),
                 'eps': _sf(r.get('eps', 0) or 0)}
                for _, r in yearly_roe.iterrows()
            ]

        # ── 3. 近5年营收趋势（利润表） ──
        df_income, ok = _ts_query('income', ts_code=ts_code,
                                   start_date=f"{current_year-5}0101",
                                   end_date=today_str)
        if ok and not df_income.empty:
            df_income = df_income.sort_values('end_date', ascending=False)
            result['annual_trend'] = []
            seen_dates = set()
            for _, r in df_income.iterrows():
                ed = str(r['end_date'])
                if ed not in seen_dates:
                    seen_dates.add(ed)
                    result['annual_trend'].append({
                        'end_date': ed,
                        'revenue': _sf(r.get('revenue', 0) or 0) / 1e8,
                        'n_income': _sf(r.get('n_income', 0) or 0) / 1e8,
                        'operate_profit': _sf(r.get('operate_profit', 0) or 0) / 1e8,
                        'report_type': str(r.get('report_type', '')),
                    })
                    if len(result['annual_trend']) >= 8:  # 最多8期
                        break

        # ── 4. 近4季资本结构（资产负债表） ──
        df_bs, ok = _ts_query('balancesheet', ts_code=ts_code,
                               start_date=f"{current_year-2}0101",
                               end_date=today_str)
        if ok and not df_bs.empty:
            df_bs = df_bs.sort_values('end_date', ascending=False)
            result['capital_structure'] = []
            seen_bs = set()
            for _, r in df_bs.iterrows():
                ed = str(r['end_date'])
                if ed not in seen_bs:
                    seen_bs.add(ed)
                    entry = {
                        'end_date': ed,
                        'total_assets': _sf(r.get('total_assets', 0) or 0) / 1e8,
                        'total_liab': _sf(r.get('total_liab', 0) or 0) / 1e8,
                        'total_hldr_eqy_excl_min_int': _sf(r.get('total_hldr_eqy_excl_min_int', 0) or 0) / 1e8,
                    }
                    result['capital_structure'].append(entry)
                    if len(result['capital_structure']) >= 4:
                        break

        # ── 5. 近4季现金流（现金流量表） ──
        df_cf, ok = _ts_query('cashflow', ts_code=ts_code,
                               start_date=f"{current_year-2}0101",
                               end_date=today_str)
        if ok and not df_cf.empty:
            df_cf = df_cf.sort_values('end_date', ascending=False)
            result['cashflow'] = []
            seen_cf = set()
            for _, r in df_cf.iterrows():
                ed = str(r['end_date'])
                if ed not in seen_cf:
                    seen_cf.add(ed)
                    result['cashflow'].append({
                        'end_date': ed,
                        'n_cashflow_act': _sf(r.get('n_cashflow_act', 0) or 0) / 1e8,
                        'n_cashflow_inv': _sf(r.get('n_cashflow_inv', 0) or 0) / 1e8,
                        'n_cashflow_fin': _sf(r.get('n_cashflow_fin', 0) or 0) / 1e8,
                        'free_cashflow': _sf(r.get('free_cashflow', 0) or 0) / 1e8,
                    })
                    if len(result['cashflow']) >= 4:
                        break

        # ── 6. 分红记录 ──
        df_div, ok = _ts_query('dividend', ts_code=ts_code,
                                start_date=f"{current_year-5}0101",
                                end_date=today_str)
        if ok and not df_div.empty:
            df_div = df_div.sort_values('ex_date', ascending=False) if 'ex_date' in df_div.columns else df_div
            result['dividend_records'] = []
            for _, r in df_div.iterrows():
                result['dividend_records'].append({
                    'ex_date': str(r.get('ex_date', '')),
                    'pay_date': str(r.get('pay_date', '')),
                    'dividend_per_share': _sf(r.get('dividend_per_share', 0) or 0),
                    'dividend_type': str(r.get('dividend_type', '')),
                    'base_share': _sf(r.get('base_share', 0) or 0) / 1e8,
                })

        # ── 7. 业绩预告 ──
        df_fc, ok = _ts_query('forecast', ts_code=ts_code,
                               start_date=f"{current_year-1}0101",
                               end_date=today_str)
        if ok and not df_fc.empty:
            df_fc = df_fc.sort_values('end_date', ascending=False) if 'end_date' in df_fc.columns else df_fc
            result['forecast_list'] = [
                {
                    'end_date': str(r.get('end_date', '')),
                    'ann_date': str(r.get('ann_date', '')),
                    'type': str(r.get('type', '')),
                    'p_change_min': _sf(r.get('p_change_min', 0) or 0) if r.get('p_change_min') else None,
                    'p_change_max': _sf(r.get('p_change_max', 0) or 0) if r.get('p_change_max') else None,
                    'net_profit_min': _sf(r.get('net_profit_min', 0) or 0) / 1e8,
                    'net_profit_max': _sf(r.get('net_profit_max', 0) or 0) / 1e8,
                    'content': str(r.get('content', '')),
                }
                for _, r in df_fc.head(5).iterrows()
            ]

    except Exception as e:
        logger.error(f"get_stock_finance_overview({ts_code}) 失败: {e}")
        result['error'] = str(e)

    _cache_set(cache_key, result, CACHE_TTL['finance'])
    return result


# ════════════════════════════════════════════════════════════
# 主函数2: 个股技术因子
# ════════════════════════════════════════════════════════════
def get_stock_factors(ts_code: str) -> dict:
    """
    返回个股最新技术因子字典。
    ts_code 示例: '000001.SZ', '600519.SH'
    """
    cache_key = f"factors::{ts_code}"
    cached = _cache_get(cache_key, CACHE_TTL['factors'])
    if cached:
        logger.info(f"[cache] 命中技术因子 {ts_code}")
        return cached

    logger.info(f"📈 开始聚合技术因子: {ts_code}")

    today_str = date.today().isoformat()
    ten_days_ago = (date.today() - timedelta(days=15)).isoformat()

    result = {
        'ts_code': ts_code,
        'query_time': today_str,
        'macd': {},              # 近10日MACD
        'rsi': {},               # 近10日RSI
        'kdj': {},               # 近10日KDJ
        'turnover': {},          # 换手率
        'volume_ratio': {},      # 量比
        'pe_pb': {},             # 当前PE/PB
        'money_flow': {},        # 资金流向（近5日）
        'error': None,
    }

    try:
        # ── 1. 从 daily_basic 取 PE/PB/换手率/量比 ──
        df_daily, ok = _ts_query('daily_basic', ts_code=ts_code,
                                  start_date=ten_days_ago, end_date=today_str)
        if ok and not df_daily.empty:
            df_daily = df_daily.sort_values('trade_date', ascending=False)
            latest = df_daily.iloc[0]
            result['pe_pb'] = {
                'pe': float(latest.get('pe', 0) or 0),
                'pb': float(latest.get('pb', 0) or 0),
                'total_mv': float(latest.get('total_mv', 0) or 0) / 1e8 if latest.get('total_mv') else 0,
                'circ_mv': float(latest.get('circ_mv', 0) or 0) / 1e8 if latest.get('circ_mv') else 0,
                'trade_date': str(latest.get('trade_date', '')),
            }
            result['turnover'] = {
                'latest': float(latest.get('turnover_rate', 0) or 0),
                'avg_5d': float(df_daily.head(5)['turnover_rate'].mean()) if 'turnover_rate' in df_daily.columns else 0,
            }
            result['volume_ratio'] = {
                'latest': float(latest.get('volume_ratio', 0) or 0),
                'trade_date': str(latest.get('trade_date', '')),
            }

        # ── 2. 从 stk_factor 取技术因子（MACD/RSI/KDJ） ──
        df_factor, ok = _ts_query('stk_factor', ts_code=ts_code,
                                   start_date=ten_days_ago, end_date=today_str)
        if ok and not df_factor.empty:
            df_factor = df_factor.sort_values('trade_date', ascending=False)

            # MACD - 最新10日
            macd_data = []
            for _, r in df_factor.head(10).iterrows():
                macd_data.append({
                    'trade_date': str(r['trade_date']),
                    'macd': _sf(r.get('macd', 0) or 0),
                    'macd_dea': _sf(r.get('macd_dea', 0) or 0),
                    'macd_dif': _sf(r.get('macd_dif', 0) or 0),
                })
            result['macd'] = {
                'latest': macd_data[0] if macd_data else {},
                'trend': macd_data,
            }

            # RSI - 最新10日
            rsi_data = []
            for _, r in df_factor.head(10).iterrows():
                rsi_data.append({
                    'trade_date': str(r['trade_date']),
                    'rsi_6': _sf(r.get('rsi_6', 0) or 0),
                    'rsi_12': _sf(r.get('rsi_12', 0) or 0),
                    'rsi_24': _sf(r.get('rsi_24', 0) or 0),
                })
            result['rsi'] = {
                'latest': rsi_data[0] if rsi_data else {},
                'trend': rsi_data,
            }

            # KDJ - 最新10日
            kdj_data = []
            for _, r in df_factor.head(10).iterrows():
                kdj_data.append({
                    'trade_date': str(r['trade_date']),
                    'kdj_k': _sf(r.get('kdj_k', 0) or 0),
                    'kdj_d': _sf(r.get('kdj_d', 0) or 0),
                    'kdj_j': _sf(r.get('kdj_j', 0) or 0),
                })
            result['kdj'] = {
                'latest': kdj_data[0] if kdj_data else {},
                'trend': kdj_data,
            }

            # 补充换手率/量比（如果stk_factor里有）
            if 'turnover_rate' in df_factor.columns:
                latest_tr = df_factor.iloc[0]
                result['turnover']['latest_factor'] = float(latest_tr.get('turnover_rate', 0) or 0)
            if 'volume_ratio' in df_factor.columns:
                latest_tr = df_factor.iloc[0]
                result['volume_ratio']['latest_factor'] = float(latest_tr.get('volume_ratio', 0) or 0)

        # ── 3. 个股资金流向 ──
        # moneyflow 接口: ts_code, start_date, end_date
        df_mf, ok = _ts_query('moneyflow', ts_code=ts_code,
                               start_date=ten_days_ago, end_date=today_str)
        if ok and not df_mf.empty:
            df_mf = df_mf.sort_values('trade_date', ascending=False)
            result['money_flow'] = {
                'latest': {},
                'trend': [],
            }
            for _, r in df_mf.head(5).iterrows():
                entry = {
                    'trade_date': str(r['trade_date']),
                    'buy_sm_vol': _sf(r.get('buy_sm_vol', 0) or 0),
                    'sell_sm_vol': _sf(r.get('sell_sm_vol', 0) or 0),
                    'buy_md_vol': _sf(r.get('buy_md_vol', 0) or 0),
                    'sell_md_vol': _sf(r.get('sell_md_vol', 0) or 0),
                    'buy_lg_vol': _sf(r.get('buy_lg_vol', 0) or 0),
                    'sell_lg_vol': _sf(r.get('sell_lg_vol', 0) or 0),
                    'buy_elg_vol': _sf(r.get('buy_elg_vol', 0) or 0),
                    'sell_elg_vol': _sf(r.get('sell_elg_vol', 0) or 0),
                    'net_mf_vol': _sf(r.get('net_mf_vol', 0) or 0),
                    'trade_count': _sf(r.get('trade_count', 0) or 0),
                }
                result['money_flow']['trend'].append(entry)

            if len(df_mf) > 0:
                latest_mf = df_mf.iloc[0]
                result['money_flow']['latest'] = {
                    'trade_date': str(latest_mf['trade_date']),
                    'net_mf_vol': float(latest_mf.get('net_mf_vol', 0) or 0),
                    'buy_lg_vol': float(latest_mf.get('buy_lg_vol', 0) or 0),
                    'sell_lg_vol': float(latest_mf.get('sell_lg_vol', 0) or 0),
                    'buy_elg_vol': float(latest_mf.get('buy_elg_vol', 0) or 0),
                    'sell_elg_vol': float(latest_mf.get('sell_elg_vol', 0) or 0),
                    'net_main_vol': (float(latest_mf.get('buy_lg_vol', 0) or 0) +
                                     float(latest_mf.get('buy_elg_vol', 0) or 0) -
                                     float(latest_mf.get('sell_lg_vol', 0) or 0) -
                                     float(latest_mf.get('sell_elg_vol', 0) or 0)),
                }

    except Exception as e:
        logger.error(f"get_stock_factors({ts_code}) 失败: {e}")
        result['error'] = str(e)

    _cache_set(cache_key, result, CACHE_TTL['factors'])
    return result


# ════════════════════════════════════════════════════════════
# 批量刷新缓存（预加载）
# ════════════════════════════════════════════════════════════
def preload_cache(ts_codes: list):
    """批量预加载指定股票到缓存"""
    logger.info(f"🔄 预加载缓存: {len(ts_codes)} 只股票")
    for ts_code in ts_codes:
        try:
            get_stock_finance_overview(ts_code)
            get_stock_factors(ts_code)
        except Exception as e:
            logger.warning(f"  ⚠ 预加载 {ts_code} 失败: {e}")


# ─── 独立测试 ──────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    test_code = '688256.SH'  # 寒武纪
    print(f"\n{'='*60}")
    print(f"📊 测试财务概览: {test_code}")
    print(f"{'='*60}")
    fin = get_stock_finance_overview(test_code)
    pprint.pprint(fin, depth=2)

    print(f"\n{'='*60}")
    print(f"📈 测试技术因子: {test_code}")
    print(f"{'='*60}")
    fac = get_stock_factors(test_code)
    pprint.pprint(fac, depth=2)
