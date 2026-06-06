-- ============================================================
--  打板监控模块 — 数据库建表脚本
--  数据库: stock_db
--  数据源: Tushare (6000分已解锁)
--  接口: limit_list_ths / top_list / top_inst / stk_limit
-- ============================================================

-- 表1: limit_up_daily — 每日涨停数据（核心表）
CREATE TABLE IF NOT EXISTS limit_up_daily (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL COMMENT '交易日期',
    ts_code VARCHAR(16) NOT NULL COMMENT '股票代码',
    name VARCHAR(50) COMMENT '股票名称',
    industry VARCHAR(50) COMMENT '所属行业',
    close DECIMAL(12,3) COMMENT '收盘价(涨停价)',
    pct_chg DECIMAL(8,4) COMMENT '涨跌幅%',
    amount DECIMAL(20,2) COMMENT '成交金额',
    limit_amount DECIMAL(20,2) COMMENT '封单金额',
    float_mv DECIMAL(20,2) COMMENT '流通市值',
    turnover_ratio DECIMAL(10,4) COMMENT '换手率%',
    fd_amount DECIMAL(20,2) COMMENT '封单/流通市值比',
    first_time VARCHAR(8) COMMENT '首次涨停时间 HHMMSS',
    last_time VARCHAR(8) COMMENT '最后封板时间 HHMMSS',
    open_times INT COMMENT '开板次数',
    up_stat VARCHAR(10) COMMENT '涨停属性: U涨停/D跌停',
    limit_times INT COMMENT '连板数',
    board_type VARCHAR(20) COMMENT '涨停池类型: 涨停池/连板池/炸板池/跌停池',
    concept_tags VARCHAR(200) COMMENT '所属概念标签',
    limit_type VARCHAR(10) COMMENT '涨停类型: U=涨停 D=跌停',
    status VARCHAR(10) COMMENT '状态',
    tag VARCHAR(10) COMMENT '标签',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stock_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_limit_times (limit_times),
    INDEX idx_first_time (first_time),
    INDEX idx_industry (industry)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='每日涨停数据表（打板核心）';

-- 表2: dragon_tiger_daily — 龙虎榜每日数据
CREATE TABLE IF NOT EXISTS dragon_tiger_daily (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL COMMENT '交易日期',
    ts_code VARCHAR(16) NOT NULL COMMENT '股票代码',
    name VARCHAR(50) COMMENT '股票名称',
    close DECIMAL(12,3) COMMENT '收盘价',
    pct_change DECIMAL(8,4) COMMENT '涨跌幅%',
    turnover_ratio DECIMAL(10,4) COMMENT '换手率%',
    l_buy DECIMAL(20,2) COMMENT '龙虎榜买入金额(万元)',
    l_sell DECIMAL(20,2) COMMENT '龙虎榜卖出金额(万元)',
    net_buy DECIMAL(20,2) COMMENT '净买入金额(万元)',
    reason VARCHAR(500) COMMENT '上榜原因',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stock_date (ts_code, trade_date),
    INDEX idx_trade_date (trade_date),
    INDEX idx_net_buy (net_buy)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='龙虎榜每日统计表';

-- 表3: board_hot_ranking — 板块涨停热度排行榜
CREATE TABLE IF NOT EXISTS board_hot_ranking (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date DATE NOT NULL COMMENT '交易日期',
    concept_name VARCHAR(50) NOT NULL COMMENT '概念板块名称',
    limit_up_count INT COMMENT '涨停家数',
    leader_stock VARCHAR(50) COMMENT '龙头股名称',
    leader_code VARCHAR(16) COMMENT '龙头股代码',
    leader_limit_times INT COMMENT '龙头连板数',
    total_stocks INT COMMENT '板块成分股总数',
    up_ratio DECIMAL(5,2) COMMENT '涨停占比%',
    avg_limit_score DECIMAL(5,2) COMMENT '平均涨停评分',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_concept (trade_date, concept_name),
    INDEX idx_trade_date (trade_date),
    INDEX idx_limit_count (limit_up_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='板块涨停热度排名表';
