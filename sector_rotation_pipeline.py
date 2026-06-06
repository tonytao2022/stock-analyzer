#!/usr/bin/env python3
"""
板块轮动调度管道 v1.0
======================
每日/每周调度的总入口。
串接: fetch_sector_daily → sector_chanlun_analyzer → sector_rotation_score_ci

方案A（中信指数）为主评分体系，方案B（stock_basic.industry 聚合）作为交叉验证。

用法:
  python3 sector_rotation_pipeline.py --daily     # 每日16:00 收盘后
  python3 sector_rotation_pipeline.py --weekly    # 每周一08:30 评分+调仓
  python3 sector_rotation_pipeline.py --daily --engine=v2   # 以方案B运行

设计者: MAY + Tony
"""

import sys, os, json, subprocess, time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import get_connection
import warnings
warnings.filterwarnings("ignore")


# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable or 'python3'

FETCH_SECTOR = os.path.join(SCRIPT_DIR, 'fetch_sector_daily.py')
CHANLUN_ANALYZER = os.path.join(SCRIPT_DIR, 'sector_chanlun_analyzer.py')
SCORE_CI = os.path.join(SCRIPT_DIR, 'sector_rotation_score_ci.py')
ENGINE_V2 = os.path.join(SCRIPT_DIR, 'sector_rotation_engine.py')
RULES_V2 = os.path.join(SCRIPT_DIR, 'sector_rotation_rules.py')

LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# 日志工具
# ═══════════════════════════════════════════════════════════════

def log(msg: str, level: str = 'INFO'):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [{level}] {msg}")


def run_script(script_path: str, args: List[str] = None,
               label: str = '', timeout: int = 600) -> bool:
    """
    运行一个Python脚本并捕获输出

    Args:
        script_path: 脚本路径
        args: 额外参数列表
        label: 日志标签
        timeout: 超时秒数

    Returns:
        True=成功, False=失败
    """
    cmd = [PYTHON, script_path]
    if args:
        cmd.extend(args)
    tag = label or os.path.basename(script_path)

    log(f"🚀 运行 {tag}: {' '.join(cmd)}")
    t0 = time.time()

    log_file = os.path.join(
        LOG_DIR,
        f"{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    try:
        with open(log_file, 'w') as f:
            result = subprocess.run(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                cwd=SCRIPT_DIR,
            )
        elapsed = time.time() - t0
        if result.returncode == 0:
            log(f"✅ {tag} 成功 ({elapsed:.1f}s) → {log_file}")
            return True
        else:
            log(f"❌ {tag} 失败 (code={result.returncode}, {elapsed:.1f}s) → {log_file}")
            # 打印最后10行
            with open(log_file) as f:
                lines = f.readlines()
                for line in lines[-10:]:
                    log(f"  {line.rstrip()}", 'ERROR')
            return False
    except subprocess.TimeoutExpired:
        log(f"⏰ {tag} 超时 ({timeout}s)", 'ERROR')
        return False
    except Exception as e:
        log(f"💥 {tag} 异常: {e}", 'ERROR')
        return False


# ═══════════════════════════════════════════════════════════════
# 每日管道
# ═══════════════════════════════════════════════════════════════

def daily_pipeline(engine_mode: str = 'ci') -> bool:
    """
    每日16:00 收盘后调度

    方案A流程（默认）:
      1. fetch_sector_daily.py --today   (拉当日增量数据)
      2. sector_chanlun_analyzer.py --analyze (缠论分析)
      3. sector_rotation_score_ci.py --save (三因子评分入库)

    方案B（--engine=v2）:
      1. fetch_sector_daily.py --today
      2. sector_chanlun_analyzer.py --analyze
      3. sector_rotation_engine.py 自评分+入库

    Args:
        engine_mode: 'ci' (方案A) | 'v2' (方案B)

    Returns:
        bool: 是否全部成功
    """
    log(f"{'='*60}")
    log(f"📅 板块轮动每日管道 开始 ({datetime.now().isoformat()[:19]})")
    log(f"{'='*60}")
    log(f"📊 引擎模式: {'方案A(中信指数)' if engine_mode=='ci' else '方案B(行业聚合)'}")

    success = True

    # ─── 步骤1: 拉取行业指数数据 ───────────────────────
    log(f"\n{'─'*40}")
    log("步骤1/3: 拉取行业指数数据")

    ok = run_script(FETCH_SECTOR, ['--today'], 'fetch_sector_daily')
    if not ok:
        log("⚠️  步骤1失败，继续尝试后续步骤（可能有缓存）", 'WARN')
        # 不中断流程 — 可能有昨天缓存

    # ─── 步骤2: 缠论分析 ───────────────────────────────
    log(f"\n{'─'*40}")
    log("步骤2/3: 行业缠论分析")

    ok = run_script(CHANLUN_ANALYZER, ['--analyze'], 'chanlun_analyzer')
    if not ok:
        log("❌ 缠论分析失败，管道终止", 'ERROR')
        return False

    # ─── 步骤3: 评分入库 ───────────────────────────────
    log(f"\n{'─'*40}")
    log("步骤3/3: 三因子评分入库")

    if engine_mode == 'ci':
        ok = run_script(SCORE_CI, ['--save'], 'score_ci')
    else:
        # 方案B: sector_rotation_engine.py 自带评分+入库
        ok = run_script(ENGINE_V2, ['--save', '--date', date.today().isoformat()], 'score_v2')

    if not ok:
        log("❌ 评分入库失败", 'ERROR')
        return False

    log(f"\n{'─'*40}")
    log(f"✅ 每日管道完成 ({engine_mode})")
    log(f"{'='*60}")
    return True


# ═══════════════════════════════════════════════════════════════
# 每周管道
# ═══════════════════════════════════════════════════════════════

def weekly_pipeline(engine_mode: str = 'ci') -> bool:
    """
    每周一08:30 评分+调仓建议

    流程:
      1. 先执行 —daily 全流程（拉数据+缠论+评分）
      2. 从 sector_rotation_score 读取最新评分
      3. 对比上周排名 → 计算排名变化
      4. 输出调仓建议（买入TOP5/卖出TOP5/维持）
      5. 写入 sector_rotation_score.advice 和 reason_chain
      6. 方案B同时跑，写入不同记录

    Args:
        engine_mode: 'ci' | 'v2'

    Returns:
        bool: 是否全部成功
    """
    log(f"{'='*60}")
    log(f"📊 板块轮动每周管道 开始 ({datetime.now().isoformat()[:19]})")
    log(f"{'='*60}")

    # ─── 步骤1: 每日全流程 ───────────────────────────────
    log(f"\n{'─'*40}")
    log("步骤1/5: 执行每日全流程")

    if not daily_pipeline(engine_mode):
        log("⚠️  每日管道部分失败，继续生成周报", 'WARN')

    # ─── 步骤2: 读取最新评分 ─────────────────────────────
    log(f"\n{'─'*40}")
    log("步骤2/5: 读取最新评分")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT MAX(trade_date) AS latest FROM sector_rotation_score
    """)
    latest_row = cur.fetchone()
    if not latest_row or not latest_row.get('latest'):
        log("❌ sector_rotation_score 无数据", 'ERROR')
        cur.close()
        conn.close()
        return False

    latest_date = latest_row['latest']
    if hasattr(latest_date, 'isoformat'):
        latest_date = latest_date.isoformat()

    log(f"📈 最新评分日期: {latest_date}")

    # 取本周评分
    cur.execute("""
        SELECT ts_code, composite_score, rank_pos, advice, reason_chain
        FROM sector_rotation_score
        WHERE trade_date = %s
        ORDER BY rank_pos ASC
    """, (latest_date,))
    current_rows = cur.fetchall()

    if not current_rows:
        log("❌ 未找到评分数据", 'ERROR')
        cur.close()
        conn.close()
        return False

    current_scores = {}
    for r in current_rows:
        current_scores[r['ts_code']] = {
            'score': float(r['composite_score']),
            'rank': int(r['rank_pos']) if r.get('rank_pos') else -1,
            'advice': r.get('advice', ''),
            'reason': r.get('reason_chain', ''),
        }

    log(f"📋 当前行业数: {len(current_scores)}")

    # ─── 步骤3: 对比上周排名 ─────────────────────────────
    log(f"\n{'─'*40}")
    log("步骤3/5: 对比上周排名")

    prev_date_dt = (datetime.strptime(latest_date, '%Y-%m-%d').date() -
                    timedelta(days=7))
    prev_date = prev_date_dt.isoformat()

    cur.execute("""
        SELECT ts_code, composite_score, rank_pos
        FROM sector_rotation_score
        WHERE trade_date = %s
    """, (prev_date,))
    prev_rows = cur.fetchall()

    prev_scores = {}
    for r in prev_rows:
        prev_scores[r['ts_code']] = {
            'score': float(r['composite_score']),
            'rank': int(r['rank_pos']) if r.get('rank_pos') else -1,
        }

    log(f"📋 上周评分日期: {prev_date} ({len(prev_scores)} 个行业)")

    # ─── 步骤4: 计算排名变化，生成调仓建议 ──────────────
    log(f"\n{'─'*40}")
    log("步骤4/5: 生成调仓建议")

    changes = []
    for code, cur_info in current_scores.items():
        cur_rank = cur_info['rank']
        cur_score = cur_info['score']

        prev_info = prev_scores.get(code, {})
        prev_rank = prev_info.get('rank', -1)
        prev_score = prev_info.get('score', None)
        rank_change = prev_rank - cur_rank if prev_rank > 0 else 0  # >0=上升
        score_change = (cur_score - prev_score) if prev_score else 0

        direction = 'new'
        if prev_rank > 0:
            direction = 'up' if rank_change > 0 else ('down' if rank_change < 0 else 'stable')

        changes.append({
            'ts_code': code,
            'current_score': cur_score,
            'prev_score': prev_score,
            'score_change': round(score_change, 1) if prev_score else None,
            'current_rank': cur_rank,
            'prev_rank': prev_rank if prev_rank > 0 else None,
            'rank_change': rank_change if prev_rank > 0 else None,
            'direction': direction,
            'advice': cur_info['advice'],
        })

    # 买入TOP5: 评分最高且建议买入/上升中的
    buy_candidates = [c for c in changes
                      if c['direction'] in ('up', 'new') and c['current_score'] >= 60]
    buy_candidates.sort(key=lambda x: (-x['current_score'], -(x['rank_change'] or 0)))
    top_buy = buy_candidates[:5]

    # 卖出TOP5: 评分最低且下跌中的
    sell_candidates = [c for c in changes
                       if c['direction'] == 'down' and c['current_score'] < 50]
    sell_candidates.sort(key=lambda x: (x['current_score'], -(x['rank_change'] or 0)))
    top_sell = sell_candidates[:5]

    # 维持
    hold_candidates = [c for c in changes
                       if c['direction'] in ('stable', 'up') and c not in top_buy
                       and c['current_score'] >= 40]
    hold_candidates.sort(key=lambda x: -x['current_score'])
    top_hold = hold_candidates[:10]

    # ─── 输出调仓报告 ────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"📊 板块轮动调仓建议 ({latest_date})")
    print(f"{'='*60}")

    # 读取当前季节
    cur.execute("""
        SELECT season, raw_score, confidence, weight_mode
        FROM (
            SELECT s.season, s.raw_score, s.confidence,
                   'ci' as weight_mode
            FROM season_state s
            WHERE s.index_code='MARKET'
            ORDER BY s.trade_date DESC LIMIT 1
        ) t
    """)
    ss_row = cur.fetchone()
    if ss_row:
        print(f"🌤️  市场季节: {ss_row['season']} | "
              f"得分: {float(ss_row['raw_score']):+.1f} | "
              f"置信度: {float(ss_row['confidence']):.0%}")

    print(f"\n🟢 买入 TOP5:")
    print(f"{'':4s}{'行业':14s} {'评分':>6s} {'变化':>8s} {'排名':>6s}")
    for idx, c in enumerate(top_buy):
        rank_str = f"↑{c['rank_change']:+d}" if c['rank_change'] else "NEW"
        score_str = f"{c['score_change']:+.1f}" if c['score_change'] else "NEW"
        print(f"  {idx+1:2d}. {c['ts_code']:12s} {c['current_score']:6.1f} "
              f"{score_str:>8s} {rank_str:>6s}")

    print(f"\n🔴 卖出 TOP5:")
    print(f"{'':4s}{'行业':14s} {'评分':>6s} {'变化':>8s} {'排名':>6s}")
    for idx, c in enumerate(top_sell):
        rank_str = f"{c['rank_change']:+d}" if c['rank_change'] else "NEW"
        score_str = f"{c['score_change']:+.1f}" if c['score_change'] else "NEW"
        print(f"  {idx+1:2d}. {c['ts_code']:12s} {c['current_score']:6.1f} "
              f"{score_str:>8s} {rank_str:>6s}")

    print(f"\n⚪ 维持持有 TOP10:")
    for idx, c in enumerate(top_hold):
        print(f"  {idx+1:2d}. {c['ts_code']:12s} (评分: {c['current_score']:5.1f})")

    # ─── 步骤5: 写入 advice 和 reason_chain ──────────────
    log(f"\n{'─'*40}")
    log("步骤5/5: 更新调仓建议")

    updates = 0
    for c in changes:
        # 综合决定最终的 advice
        if c in top_buy:
            final_advice = 'buy'
        elif c in top_sell:
            final_advice = 'sell'
        elif c['direction'] == 'down' and c['current_score'] < 50:
            final_advice = 'sell'
        elif c['current_score'] >= 60:
            final_advice = 'buy'
        elif c['current_score'] >= 40:
            final_advice = 'hold'
        else:
            final_advice = 'sell'

        reason = _build_weekly_reason(c, final_advice)

        cur.execute("""
            UPDATE sector_rotation_score
            SET advice=%s, reason_chain=%s,
                prev_rank_c=%s, rank_change_c=%s
            WHERE ts_code=%s AND trade_date=%s
        """, (
            final_advice, reason,
            c['prev_rank'], c['rank_change'],
            c['ts_code'], latest_date,
        ))
        updates += 1

    conn.commit()
    cur.close()
    conn.close()

    log(f"💾 已更新 {updates} 条调仓建议")

    print(f"\n{'─'*40}")
    print(f"✅ 每周管道完成")
    print(f"{'='*60}")
    return True


def _build_weekly_reason(c: Dict, advice: str) -> str:
    """构建周报推理链"""
    parts = []

    if c['direction'] == 'up':
        parts.append(f"排名↑{c['rank_change']}")
    elif c['direction'] == 'down':
        parts.append(f"排名↓{abs(c['rank_change'])}")
    elif c['direction'] == 'new':
        parts.append("新进榜单")

    if c['score_change'] is not None:
        if c['score_change'] > 3:
            parts.append(f"评分+{c['score_change']:.1f}")
        elif c['score_change'] < -3:
            parts.append(f"评分{c['score_change']:.1f}")

    if advice == 'buy':
        parts.append("建议增持")
    elif advice == 'sell':
        parts.append("建议减持")
    else:
        parts.append("建议维持")

    return "+".join(parts)


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description='板块轮动调度管道')
    ap.add_argument('--daily', action='store_true', help='每日模式')
    ap.add_argument('--weekly', action='store_true', help='每周模式')
    ap.add_argument('--engine', type=str, default='ci',
                    choices=['ci', 'v2'],
                    help='评分引擎: ci(方案A,中信指数) / v2(方案B,行业聚合)')
    args = ap.parse_args()

    if args.weekly:
        weekly_pipeline(args.engine)
    elif args.daily:
        daily_pipeline(args.engine)
    else:
        print("用法:")
        print("  python3 sector_rotation_pipeline.py --daily")
        print("  python3 sector_rotation_pipeline.py --weekly")
        print("  python3 sector_rotation_pipeline.py --daily --engine=v2")
        ap.print_help()


if __name__ == '__main__':
    main()
