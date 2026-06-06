-- ============================================================
--  板块轮动预测引擎 — 数据库建表脚本
--  数据库: stock_db
--  基于: 中信行业指数（ci_daily）+ 监控池映射
-- ============================================================

-- 表1: sector_index_daily — 中信行业指数日线（数据源）
CREATE TABLE IF NOT EXISTS sector_index_daily (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code VARCHAR(16) NOT NULL COMMENT '中信行业代码 CI005001.CI',
    index_name VARCHAR(50) COMMENT '行业中文名称',
    trade_date DATE NOT NULL COMMENT '交易日',
    open DECIMAL(12,4) COMMENT '开盘点位',
    high DECIMAL(12,4) COMMENT '最高点位',
    low DECIMAL(12,4) COMMENT '最低点位',
    close DECIMAL(12,4) COMMENT '收盘点位',
    pre_close DECIMAL(12,4) COMMENT '昨日收盘点位',
    `change` DECIMAL(12,4) COMMENT '涨跌点位',
    pct_change DECIMAL(8,4) COMMENT '涨跌幅%',
    vol DECIMAL(20,2) COMMENT '成交量(万股)',
    amount DECIMAL(20,2) COMMENT '成交额(万元)',
    level VARCHAR(10) COMMENT '行业等级: L1一级/L2二级/L3三级',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_sector_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='中信行业指数日线表 — 板块轮动数据源';


-- 表2: sector_mapping — 监控池股票→中信行业归属映射
CREATE TABLE IF NOT EXISTS sector_mapping (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code VARCHAR(16) NOT NULL COMMENT '股票代码',
    stock_name VARCHAR(50) COMMENT '股票名称',
    l1_code VARCHAR(16) COMMENT '中信一级行业代码 CI005001.CI',
    l1_name VARCHAR(50) COMMENT '一级行业名称',
    l2_code VARCHAR(16) COMMENT '中信二级行业代码',
    l2_name VARCHAR(50) COMMENT '二级行业名称',
    l3_code VARCHAR(16) COMMENT '中信三级行业代码',
    l3_name VARCHAR(50) COMMENT '三级行业名称',
    in_date DATE COMMENT '纳入中信行业指数日期',
    is_active TINYINT DEFAULT 1 COMMENT '是否在watch_pool中',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stock (ts_code),
    INDEX idx_l1 (l1_code),
    INDEX idx_l2 (l2_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='监控池股票→中信行业归属映射表';


-- 表3: sector_chanlun_cache — 行业缠论分析结果缓存
CREATE TABLE IF NOT EXISTS sector_chanlun_cache (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code VARCHAR(16) NOT NULL COMMENT '中信行业代码',
    trade_date DATE NOT NULL COMMENT '分析交易日',
    structure_score DECIMAL(5,2) COMMENT '缠论结构评分(0-100)',
    buy_sell_point VARCHAR(20) COMMENT '买卖点: buy1/buy2/buy3/sell1/sell2/sell3/none',
    zoushi_type VARCHAR(20) COMMENT '走势类型: 上涨/下跌/盘整/unknown',
    beichi_type VARCHAR(20) COMMENT '背驰类型: top/bottom/none',
    bi_direction VARCHAR(10) COMMENT '笔方向: up/down',
    bi_strength DECIMAL(10,4) COMMENT '笔强度',
    zhongshu_count INT COMMENT '中枢数量',
    zhongshu_width DECIMAL(10,4) COMMENT '中枢宽度(振幅比)',
    macd_area_ratio DECIMAL(10,4) COMMENT 'MACD面积比(背驰判据)',
    autumn_tiger TINYINT DEFAULT 0 COMMENT '秋老虎标记',
    tiger_confidence DECIMAL(5,2) COMMENT '秋老虎置信度',
    is_calculable TINYINT DEFAULT 1,
    calc_error VARCHAR(200),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_sector_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_score (structure_score),
    INDEX idx_bsp (buy_sell_point)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='行业缠论分析缓存表';


-- 表4: sector_rotation_score — 板块轮动综合评分（三因子融合输出）
CREATE TABLE IF NOT EXISTS sector_rotation_score (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code VARCHAR(16) NOT NULL COMMENT '中信行业代码',
    trade_date DATE NOT NULL COMMENT '评分日期',
    season VARCHAR(20) COMMENT '当前市场季节',
    weight_mode VARCHAR(20) COMMENT '权重模式: attacking/defensive/neutral',
    chanlun_score DECIMAL(5,2) COMMENT '缠论因子分(0-100)',
    season_score DECIMAL(5,2) COMMENT '季节因子分',
    money_score DECIMAL(5,2) COMMENT '资金因子分',
    composite_score DECIMAL(5,2) COMMENT '综合评分',
    `rank` INT COMMENT '当轮排名',
    prev_rank_c INT INT COMMENT '上轮排名',
    rank_change_c INT INT COMMENT '排名变化',
    advice VARCHAR(20) COMMENT '操作建议: BUY/HOLD/FLAT/SELL',
    reason_chain VARCHAR(200) COMMENT '评分理由链',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_sector_week (ts_code, trade_date),
    INDEX idx_rank (rank),
    INDEX idx_composite (composite_score),
    INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='板块轮动综合评分表（三因子融合输出）';
