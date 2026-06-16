#!/usr/bin/env python3
"""
V11全量回测 v2 — 批量评分+回测分离
Step 1: 批量评分（每个采样点所有票一起算，用nohup后台跑，逐行flush）
Step 2: 回测（数据库已有评分，秒出结果）

评分阶段优化：评分引擎调用时只取当天的日K+技术指标+资金流，不重复加载历史数据
"""
import sys, os, time, pymysql, json
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['MYSQL_PASS'] = 'iXve1rVBXfdA4tL9'

from db_config import get_connection
from p6_dual_track_engine import score_stock, MarketContext
from season_engine import SeasonEngine

def _make_ctx(sea_str, trade_date_str):
    """构造MarketContext需要的dict格式"""
    return MarketContext({
        'market_season': sea_str,
        'sub_state': sea_str,
        'trade_date': trade_date_str,
    })

START = date(2023, 1, 1)

def step1_batch_score():
    """阶段1: 批量评分 — 每20日采样评分并入库"""
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 加载季节
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    season_map = {}
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        if td >= START: season_map[td] = r['season']
    dates = sorted(season_map.keys())
    # 跳过前200个交易日（K线积累期，评分不够高）
    sample_dates = [d for i, d in enumerate(dates) if i >= 200 and i % 20 == 0]
    
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    
    print(f"📋 评分阶段: {len(sample_dates)}个采样点 × {len(codes)}只 = {len(sample_dates)*len(codes)}次调用", flush=True)
    
    se = SeasonEngine()
    t0 = time.time()
    scored = 0
    
    # 建临时表存回测评分
    cur2 = conn.cursor()
    cur2.execute("""
        CREATE TABLE IF NOT EXISTS backtest_scores (
            ts_code VARCHAR(20),
            trade_date DATE,
            score DECIMAL(10,2),
            season VARCHAR(20),
            PRIMARY KEY (ts_code, trade_date)
        )
    """)
    conn.commit()
    
    for i, td in enumerate(sample_dates):
        ctx = _make_ctx(season_map.get(td, 'chaos'), str(td))
        batch = []
        for code in codes:
            try:
                r = score_stock(code, ctx)
                raw = r.get('score', 0)
                if raw and raw > 0:
                    batch.append((code, str(td), float(raw), ctx.current_season))
            except:
                pass
        
        if batch:
            cur2.executemany(
                "INSERT INTO backtest_scores (ts_code, trade_date, score, season) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE score=VALUES(score), season=VALUES(season)",
                batch
            )
            conn.commit()
            scored += len(batch)
        
        elapsed = time.time() - t0
        eta = (len(sample_dates) - i - 1) * (elapsed / (i + 1)) if i > 0 else 0
        print(f"  [{i+1}/{len(sample_dates)}] {td} -> {len(batch)}只有效评分 (累计{scored}, ETA {eta/60:.0f}min)", flush=True)
    
    cur2.close()
    conn.close()
    print(f"✅ 评分完成: {scored}条", flush=True)
    return len(sample_dates), len(codes)

def step2_backtest():
    """阶段2: 从 backtest_scores 表跑回测"""
    t0 = time.time()
    conn = get_connection()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 季节
    cur.execute("SELECT trade_date, season FROM season_state WHERE index_code='MARKET' ORDER BY trade_date")
    season_map = {}
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        if td >= START: season_map[td] = r['season']
    dates = sorted(season_map.keys())
    
    # 评分
    print("加载评分数据...", flush=True)
    cur.execute("SELECT ts_code, trade_date, score FROM backtest_scores ORDER BY trade_date")
    scores = defaultdict(dict)
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        sc = r['score']
        if sc and sc > 0:
            scores[r['ts_code']][td] = float(sc)
    print(f"  评分: {len(scores)}只", flush=True)
    
    # K线
    print("加载K线数据...", flush=True)
    cur.execute("SELECT ts_code, trade_date, close FROM daily_kline_qfq WHERE trade_date >= %s ORDER BY ts_code, trade_date", (START,))
    kline = defaultdict(list)
    for r in cur.fetchall():
        td = r['trade_date']
        if isinstance(td, str): td = date.fromisoformat(td)
        kline[r['ts_code']].append((td, float(r['close'])))
    print(f"  K线: {len(kline)}只", flush=True)
    
    cur.close()
    conn.close()
    
    # 二分加速
    import bisect
    price_cache = {}
    for code, rows in kline.items():
        d_list = [r[0] for r in rows]
        p_list = [r[1] for r in rows]
        price_cache[code] = (d_list, p_list)
    
    def get_price(code, tgt):
        if code not in price_cache: return None
        d_list, p_list = price_cache[code]
        idx = bisect.bisect_right(d_list, tgt) - 1
        return p_list[idx] if idx >= 0 else None
    
    def get_score(code, tgt):
        sd = scores.get(code, {})
        best_d, best_s = None, 0
        for d, s in sd.items():
            if d <= tgt and (best_d is None or d > best_d):
                best_d, best_s = d, s
        return best_s if best_s > 0 else None
    
    # V11参数
    CFG = {
        'summer': {'buy':72,'stop':-0.12,'hold':60,'trail':0.18,'p4_min':55,'p4_ext':15,'t2':True},
        'spring': {'buy':70,'stop':-0.08,'hold':20,'trail':0.12,'p4_min':60,'p4_ext':5,'t2':True},
        'chaos_spring': {'buy':75,'stop':-0.10,'hold':25,'trail':0.12,'p4_min':65,'p4_ext':5,'t2':False},
        'chaos': {'buy':75,'stop':-0.10,'hold':25,'trail':0.12,'p4_min':65,'p4_ext':5,'t2':False},
        'chaos_autumn': {'buy':75,'stop':-0.10,'hold':25,'trail':0.12,'p4_min':65,'p4_ext':5,'t2':False},
        'autumn': {'buy':75,'stop':-0.08,'hold':25,'trail':0.12,'p4_min':65,'p4_ext':5,'t2':True},
        'winter': {'buy':85,'stop':-0.05,'hold':10,'trail':0.08,'p4_min':999,'t2':False},
        'panic': {'buy':999}, 'recovery': {'buy':999},
    }
    
    all_codes = list(scores.keys())
    print(f"回测标的: {len(all_codes)}只, 回测区间: {dates[0]}~{dates[-1]} ({len(dates)}天)", flush=True)
    
    all_trades = []
    for idx, code in enumerate(all_codes):
        if (idx+1) % 50 == 0:
            print(f"  回测进度: {idx+1}/{len(all_codes)} (当前{len(all_trades)}笔交易)", flush=True)
        
        if code not in price_cache: continue
        pos = None
        
        for today in dates:
            sea = season_map.get(today, 'chaos')
            c = CFG[sea]
            if c['buy'] >= 999:
                pos = None; continue
            
            px = get_price(code, today)
            if px is None: continue
            
            if pos:
                ret = (px - pos['buy_price']) / pos['buy_price']
                if px > pos.get('peak', pos['buy_price']): pos['peak'] = px
                hd = (today - pos['buy_date']).days
                
                reason = None
                if ret <= c['stop']:
                    reason = 'T1_stop'
                elif c['t2'] and pos['peak'] > pos['buy_price']:
                    dd = (px - pos['peak']) / pos['peak']
                    if dd <= -c.get('trail', 0.12):
                        reason = 'T2_trailing'
                elif hd >= c['hold']:
                    sc = get_score(code, today)
                    if c.get('p4_ext', 0) > 0 and sc and sc >= c.get('p4_min', 999) and hd < c['hold'] + c['p4_ext']:
                        pass
                    else:
                        reason = 'hold_expire'
                
                if reason:
                    all_trades.append({
                        'c':code,'bd':str(pos['buy_date']),'sd':str(today),
                        'hd':hd,'r':ret,'sc':pos['sc'],'sea':pos['sea'],'sr':reason
                    })
                    pos = None
            
            if pos is None:
                sc = get_score(code, today)
                if sc is not None and sc >= c['buy']:
                    pos = {'buy_date':today,'buy_price':px,'peak':px,'sc':sc,'sea':sea}
        
        if pos:
            lp = get_price(code, dates[-1]) or pos['buy_price']
            ret = (lp - pos['buy_price']) / pos['buy_price']
            all_trades.append({
                'c':code,'bd':str(pos['buy_date']),'sd':str(dates[-1]),
                'hd':(dates[-1]-pos['buy_date']).days,'r':ret,'sc':pos['sc'],
                'sea':pos['sea'],'sr':'force_close'
            })
    
    # 统计
    t = all_trades; n = len(t)
    w = [x for x in t if x['r']>0]; l = [x for x in t if x['r']<=0]
    wr = len(w)/n*100 if n else 0
    ar = sum(x['r'] for x in t)/n*100 if n else 0
    pf = sum(x['r'] for x in w) / (abs(sum(x['r'] for x in l)) or 1) if w and l else 0
    aw = sum(x['r'] for x in w)/len(w)*100 if w else 0
    al = sum(x['r'] for x in l)/len(l)*100 if l else 0
    
    print(f"""
{'='*60}
📊 V11全量回测结果
{'='*60}
运行时间: {time.time()-t0:.1f}秒
区间: {dates[0]} ~ {dates[-1]} ({len(dates)}天)
标的: {len(set(x['c'] for x in t))}只 (回测{len(all_codes)}只)
交易: {n}笔
胜率: {wr:.1f}%
均收益: {ar:+.2f}%
盈亏比: {pf:.2f}
均盈利: {aw:+.2f}%
均亏损: {al:+.2f}%
""")
    
    for label, sk in [('夏','summer'),('春','spring'),('弱春','chaos_spring'),('混沌','chaos'),('弱秋','chaos_autumn'),('秋','autumn'),('冬','winter')]:
        g = [x for x in t if x['sea']==sk]
        if g:
            wg = [x for x in g if x['r']>0]
            print(f"  {label}: {len(g)}笔 胜率{len(wg)/len(g)*100:.1f}% 均收益{sum(x['r'] for x in g)/len(g)*100:+.2f}%")
    
    print("")
    for rs in ['T1_stop','T2_trailing','hold_expire','force_close']:
        g = [x for x in t if x['sr']==rs]
        if g:
            wg = [x for x in g if x['r']>0]
            print(f"  {rs}: {len(g)}笔 胜率{len(wg)/len(g)*100:.1f}% 均收益{sum(x['r'] for x in g)/len(g)*100:+.2f}%")
    
    print(f"""
对比指标:
  V11参数: 夏72/春70/混沌75/秋75/冬85
  (回测评分源自backtest_scores表, 20日采样)
""")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'backtest':
        step2_backtest()
    else:
        step1_batch_score()
