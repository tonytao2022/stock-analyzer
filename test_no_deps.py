#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资预测模型 - 无依赖测试版本
使用模拟数据测试核心算法
"""

import sys
import json
import random
from datetime import datetime

print("=" * 60)
print("投资预测模型 - 无依赖测试版本")
print("=" * 60)

# 模拟数据生成器
class MockDataGenerator:
    """模拟数据生成器"""
    
    @staticmethod
    def generate_stock_data(code, name):
        """生成模拟股票数据"""
        return {
            'basic_info': {
                'name': name,
                'code': code,
                'industry': random.choice(['电气设备', '医药生物', '电子', '计算机', '食品饮料']),
                'market_cap': random.uniform(50, 5000),
                'pe_ratio': random.uniform(10, 60),
                'pb_ratio': random.uniform(1, 8),
                'listing_date': '2010-01-01'
            },
            'price_data': {
                'current_price': random.uniform(10, 300),
                'change_percent': random.uniform(-5, 5),
                'volume': random.randint(1000000, 50000000),
                'turnover': random.uniform(100000000, 5000000000),
                'high': random.uniform(10, 320),
                'low': random.uniform(8, 280),
                'open': random.uniform(10, 300),
                'close': random.uniform(10, 300)
            },
            'financials': {
                'roe': random.uniform(5, 25),
                'revenue_growth': random.uniform(-10, 50),
                'gross_margin': random.uniform(15, 60),
                'debt_ratio': random.uniform(20, 70),
                'net_profit_growth': random.uniform(-20, 100),
                'operating_cash_flow': random.uniform(-500000000, 5000000000)
            },
            'technical_indicators': {
                'ma5': random.uniform(10, 300),
                'ma10': random.uniform(10, 300),
                'ma20': random.uniform(10, 300),
                'ma60': random.uniform(10, 300),
                'rsi': random.uniform(20, 80),
                'macd': random.uniform(-2, 2),
                'kdj_k': random.uniform(0, 100),
                'kdj_d': random.uniform(0, 100),
                'kdj_j': random.uniform(0, 100)
            },
            'sentiment_data': {
                'news_sentiment': random.uniform(-1, 1),
                'search_index': random.randint(1000, 100000),
                'social_hot': random.uniform(0, 1),
                'institutional_rating': random.choice(['买入', '增持', '中性', '减持', '卖出']),
                'target_price': random.uniform(10, 350)
            },
            'cycle_data': {
                'market_cycle': random.choice(['春', '夏', '秋', '冬']),
                'order_degree': random.uniform(0, 1),  # 有序度
                'dragon_score': random.uniform(0, 1),  # 龙头辨识度
                'turn_point_probability': random.uniform(0, 1)  # 转折点概率
            }
        }

# 四维评分计算器
class FourDimensionScorer:
    """四维评分计算器"""
    
    @staticmethod
    def calculate_fundamental_score(data):
        """计算基本面得分 (0-30分)"""
        financials = data.get('financials', {})
        basic_info = data.get('basic_info', {})
        
        score = 0
        max_score = 30
        
        # 1. ROE评分 (0-8分)
        roe = financials.get('roe', 0)
        if roe > 20:
            score += 8
        elif roe > 15:
            score += 6
        elif roe > 10:
            score += 4
        elif roe > 5:
            score += 2
        
        # 2. 营收增长评分 (0-7分)
        revenue_growth = financials.get('revenue_growth', 0)
        if revenue_growth > 30:
            score += 7
        elif revenue_growth > 20:
            score += 5
        elif revenue_growth > 10:
            score += 3
        elif revenue_growth > 0:
            score += 1
        
        # 3. 毛利率评分 (0-5分)
        gross_margin = financials.get('gross_margin', 0)
        if gross_margin > 40:
            score += 5
        elif gross_margin > 30:
            score += 3
        elif gross_margin > 20:
            score += 1
        
        # 4. 负债率评分 (0-4分)
        debt_ratio = financials.get('debt_ratio', 50)
        if debt_ratio < 30:
            score += 4
        elif debt_ratio < 50:
            score += 2
        elif debt_ratio > 70:
            score -= 2
        
        # 5. PE估值评分 (0-6分)
        pe_ratio = basic_info.get('pe_ratio', 30)
        if pe_ratio < 15:
            score += 6
        elif pe_ratio < 25:
            score += 3
        elif pe_ratio > 40:
            score -= 2
        
        # 确保分数在合理范围内
        score = max(0, min(score, max_score))
        
        return {
            'value': score,
            'max_score': max_score,
            'percentage': (score / max_score) * 100,
            'details': {
                'roe_score': min(8, max(0, (roe - 5) / 15 * 8)),
                'growth_score': min(7, max(0, (revenue_growth + 10) / 40 * 7)),
                'margin_score': min(5, max(0, (gross_margin - 15) / 25 * 5)),
                'debt_score': min(4, max(0, (70 - debt_ratio) / 40 * 4)),
                'valuation_score': min(6, max(0, (40 - pe_ratio) / 25 * 6))
            }
        }
    
    @staticmethod
    def calculate_technical_score(data):
        """计算技术面得分 (0-25分)"""
        technical = data.get('technical_indicators', {})
        price_data = data.get('price_data', {})
        
        score = 0
        max_score = 25
        
        # 1. 趋势强度评分 (0-8分)
        current_price = price_data.get('current_price', 0)
        ma20 = technical.get('ma20', current_price)
        
        if current_price > ma20 * 1.05:
            score += 8  # 强势上涨
        elif current_price > ma20:
            score += 5  # 上涨趋势
        elif current_price > ma20 * 0.95:
            score += 2  # 弱势震荡
        else:
            score += 0  # 下跌趋势
        
        # 2. 动量指标评分 (0-7分)
        rsi = technical.get('rsi', 50)
        if 30 < rsi < 70:
            score += 7  # 健康区间
        elif 20 < rsi < 80:
            score += 4  # 可接受区间
        else:
            score += 0  # 超买超卖
        
        # 3. 成交量配合评分 (0-5分)
        change_percent = price_data.get('change_percent', 0)
        volume = price_data.get('volume', 0)
        
        if change_percent > 0 and volume > 10000000:
            score += 5  # 放量上涨
        elif change_percent > 0:
            score += 3  # 缩量上涨
        elif change_percent < 0 and volume > 10000000:
            score += 0  # 放量下跌
        else:
            score += 1  # 缩量下跌
        
        # 4. 支撑阻力评分 (0-5分)
        high = price_data.get('high', current_price)
        low = price_data.get('low', current_price)
        
        position = (current_price - low) / (high - low) if high > low else 0.5
        
        if 0.3 < position < 0.7:
            score += 5  # 中间位置，方向不明
        elif position > 0.7:
            score += 3  # 接近阻力
        else:
            score += 2  # 接近支撑
        
        score = max(0, min(score, max_score))
        
        return {
            'value': score,
            'max_score': max_score,
            'percentage': (score / max_score) * 100,
            'details': {
                'trend_score': score,
                'momentum_score': min(7, max(0, abs(rsi - 50) / 20 * 7)),
                'volume_score': min(5, max(0, volume / 50000000 * 5)),
                'support_resistance_score': min(5, max(0, abs(position - 0.5) * 10))
            }
        }
    
    @staticmethod
    def calculate_sentiment_score(data):
        """计算情绪面得分 (0-25分)"""
        sentiment = data.get('sentiment_data', {})
        price_data = data.get('price_data', {})
        
        score = 0
        max_score = 25
        
        # 1. 新闻情绪评分 (0-6分)
        news_sentiment = sentiment.get('news_sentiment', 0)
        score += max(0, (news_sentiment + 1) / 2 * 6)
        
        # 2. 搜索热度评分 (0-5分)
        search_index = sentiment.get('search_index', 10000)
        score += min(5, search_index / 20000)
        
        # 3. 机构评级评分 (0-7分)
        rating = sentiment.get('institutional_rating', '中性')
        rating_scores = {'买入': 7, '增持': 5, '中性': 3, '减持': 1, '卖出': 0}
        score += rating_scores.get(rating, 3)
        
        # 4. 价格动量评分 (0-7分)
        change_percent = price_data.get('change_percent', 0)
        if change_percent > 3:
            score += 7  # 强势上涨
        elif change_percent > 1:
            score += 5  # 温和上涨
        elif change_percent > -1:
            score += 3  # 震荡
        elif change_percent > -3:
            score += 1  # 温和下跌
        else:
            score += 0  # 大幅下跌
        
        score = max(0, min(score, max_score))
        
        return {
            'value': score,
            'max_score': max_score,
            'percentage': (score / max_score) * 100,
            'details': {
                'news_score': min(6, max(0, (news_sentiment + 1) / 2 * 6)),
                'search_score': min(5, max(0, search_index / 20000)),
                'institution_score': rating_scores.get(rating, 3),
                'momentum_score': min(7, max(0, (change_percent + 5) / 10 * 7))
            }
        }
    
    @staticmethod
    def calculate_cycle_score(data):
        """计算周期面得分 (0-20分) - May的核心思想"""
        cycle_data = data.get('cycle_data', {})
        
        score = 0
        max_score = 20
        
        # 1. 市场周期评分 (0-8分)
        market_cycle = cycle_data.get('market_cycle', '秋')
        cycle_scores = {'春': 8, '夏': 6, '秋': 3, '冬': 0}
        score += cycle_scores.get(market_cycle, 3)
        
        # 2. 有序度评分 (0-5分)
        order_degree = cycle_data.get('order_degree', 0.5)
        score += order_degree * 5
        
        # 3. 龙头辨识度评分 (0-4分)
        dragon_score = cycle_data.get('dragon_score', 0.5)
        score += dragon_score * 4
        
        # 4. 转折点概率评分 (0-3分)
        turn_point = cycle_data.get('turn_point_probability', 0.5)
        # 转折点概率适中时得分最高（既不是确定性太强也不是完全随机）
        turn_score = 3 * (1 - abs(turn_point - 0.5) * 2)
        score += max(0, turn_score)
        
        score = max(0, min(score, max_score))
        
        return {
            'value': score,
            'max_score': max_score,
            'percentage': (score / max_score) * 100,
            'details': {
                'cycle_stage_score': cycle_scores.get(market_cycle, 3),
                'order_degree_score': order_degree * 5,
                'dragon_score': dragon_score * 4,
                'turn_point_score': max(0, turn_score)
            }
        }

def main():
    """主测试函数"""
    print("1. 生成模拟数据...")
    generator = MockDataGenerator()
    scorer = FourDimensionScorer()
    
    # 测试股票
    test_stocks = [
        ('300274.SZ', '阳光电源'),
        ('300476.SZ', '胜宏科技'),
        ('300750.SZ', '宁德时代'),
        ('002415.SZ', '海康威视'),
        ('000858.SZ', '五粮液')
    ]
    
    print(f"   生成 {len(test_stocks)} 只股票的模拟数据")
    
    results = {}
    
    print("\n2. 计算四维评分...")
    print("-" * 60)
    
    for code, name in test_stocks:
        print(f"\n📊 分析 {name} ({code}):")
        print("-" * 40)
        
        # 生成数据
        stock_data = generator.generate_stock_data(code, name)
        
        # 计算各维度得分
        fundamental = scorer.calculate_fundamental_score(stock_data)
        technical = scorer.calculate_technical_score(stock_data)
        sentiment = scorer.calculate_sentiment_score(stock_data)
        cycle = scorer.calculate_cycle_score(stock_data)
        
        # 计算总分
        total_score = fundamental['value'] + technical['value'] + sentiment['value'] + cycle['value']
        max_total = fundamental['max_score'] + technical['max_score'] + sentiment['max_score'] + cycle['max_score']
        percentage = (total_score / max_total) * 100 if max_total > 0 else 0
        
        # 显示各维度得分
        print(f"   基本面: {fundamental['value']:.1f}/{fundamental['max_score']} ({fundamental['percentage']:.1f}%)")
        print(f"   技术面: {technical['value']:.1f}/{technical['max_score']} ({technical['percentage']:.1f}%)")
        print(f"   情绪面: {sentiment['value']:.1f}/{sentiment['max_score']} ({sentiment['percentage']:.1f}%)")
        print(f"   周期面: {cycle['value']:.1f}/{cycle['max_score']} ({cycle['percentage']:.1f}%)")
        
        print(f"\n   综合评分: {total_score:.1f}/{max_total} ({percentage:.1f}%)")
        
        # 投资评级
        if percentage >= 85:
            rating = "🔥 强烈买入"
            action = "可考虑加仓"
            color = "\033[92m"  # 绿色
        elif percentage >= 75:
            rating = "✅ 买入"
            action = "可考虑建仓"
            color = "\033[92m"  # 绿色
        elif percentage >= 65:
            rating = "🔄 持有"
            action = "持有观察"
            color = "\033[93m"  # 黄色
        elif percentage >= 55:
            rating = "⚠️ 谨慎持有"
            action = "考虑减仓"
            color = "\033[93m"  # 黄色
        else:
            rating = "❌ 卖出"
            action = "建议卖出"
            color = "\033[91m"  # 红色
        
        print(f"   投资评级: {color}{rating}\033[0m")
        print(f"   操作建议: {action}")
        
        # 关键洞察
        print(f"\n   关键洞察:")
        insights = []
        
        # 基本面洞察
        if fundamental['value'] > 20:
            insights.append("基本面优秀")
        elif fundamental['value'] < 10:
            insights.append("基本面较弱")
        
        # 技术面洞察
        if technical['value'] > 15:
            insights.append("技术形态良好")
        
        # 周期面洞察
        cycle_stage = stock_data['cycle_data']['market_cycle']
        if cycle_stage == '春':
            insights.append("处于春季播种期")
        elif cycle_stage == '夏':
            insights.append("处于夏季成长期")
        elif cycle_stage == '秋':
            insights.append("处于秋季收获期")
        elif cycle_stage == '