#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资预测模型 - 完整功能测试
"""

print("=" * 70)
print("陶的投资预测模型 - 完整功能测试报告")
print("=" * 70)

def print_key_factors(factor_data):
    """打印关键因子"""
    key_factors = []
    
    # 检查各维度关键因子
    if 'fundamental' in factor_data:
        fund = factor_data['fundamental']
        if 'roe' in fund and fund['roe']['value'] > 0.15:
            key_factors.append(f"ROE较高 ({fund['roe']['value']:.1%})")
        if 'gross_margin' in fund and fund['gross_margin']['value'] > 0.30:
            key_factors.append(f"毛利率较高 ({fund['gross_margin']['value']:.1%})")
    
    if 'technical' in factor_data:
        tech = factor_data['technical']
        if 'trend_strength' in tech and tech['trend_strength']['value'] > 0.05:
            key_factors.append("上涨趋势较强")
        elif 'trend_strength' in tech and tech['trend_strength']['value'] < -0.05:
            key_factors.append("下跌趋势明显")
    
    if 'sentiment' in factor_data:
        sent = factor_data['sentiment']
        if 'money_flow' in sent and sent['money_flow']['value'] > 0:
            key_factors.append("资金净流入")
        elif 'money_flow' in sent and sent['money_flow']['value'] < 0:
            key_factors.append("资金净流出")
    
    if 'cycle' in factor_data:
        cycle = factor_data['cycle']
        if 'market_cycle' in cycle:
            stage = cycle['market_cycle']['value']
            if stage == '春':
                key_factors.append("处于春季播种期")
            elif stage == '夏':
                key_factors.append("处于夏季成长期")
            elif stage == '秋':
                key_factors.append("处于秋季收获期")
            elif stage == '冬':
                key_factors.append("处于冬季休整期")
    
    # 打印关键因子
    if key_factors:
        for factor in key_factors[:3]:
            print(f"     • {factor}")
    else:
        print("     • 暂无特别关键因子")

# 模拟因子计算器
class MockFactorCalculator:
    """模拟因子计算器"""
    
    def calculate_all_factors(self, stock_codes):
        """计算所有因子"""
        import random
        results = {}
        
        for code in stock_codes:
            # 根据代码确定股票名称
            stock_map = {
                '300274.SZ': '阳光电源',
                '300476.SZ': '胜宏科技',
                '300750.SZ': '宁德时代',
                '002415.SZ': '海康威视',
                '000858.SZ': '五粮液'
            }
            name = stock_map.get(code, f'股票{code}')
            
            # 生成模拟因子数据（带有一定倾向性）
            if code == '300476.SZ':  # 胜宏科技 - 模拟优秀股票
                factors = self._generate_excellent_factors(code, name)
            elif code == '300274.SZ':  # 阳光电源 - 模拟一般股票
                factors = self._generate_average_factors(code, name)
            elif code == '300750.SZ':  # 宁德时代 - 模拟良好股票
                factors = self._generate_good_factors(code, name)
            else:
                factors = self._generate_random_factors(code, name)
            
            results[code] = factors
        
        return results
    
    def _generate_excellent_factors(self, code, name):
        """生成优秀股票的因子数据"""
        import random
        return {
            'basic_info': {
                'name': name,
                'code': code,
                'industry': '电子',
                'market_cap': 800.5,
                'pe_ratio': 28.3
            },
            'fundamental': {
                'total_score': {'value': 26.5, 'max_score': 30, 'description': '基本面评分'},
                'roe': {'value': 0.218, 'weight': 0.3, 'description': '净资产收益率'},
                'revenue_growth': {'value': 0.352, 'weight': 0.25, 'description': '营收增长率'},
                'gross_margin': {'value': 0.387, 'weight': 0.2, 'description': '毛利率'}
            },
            'technical': {
                'total_score': {'value': 22.8, 'max_score': 25, 'description': '技术面评分'},
                'trend_strength': {'value': 0.078, 'weight': 0.4, 'description': '趋势强度'},
                'momentum': {'value': 0.72, 'weight': 0.3, 'description': '动量指标'},
                'volume_ratio': {'value': 1.65, 'weight': 0.3, 'description': '量比'}
            },
            'sentiment': {
                'total_score': {'value': 23.2, 'max_score': 25, 'description': '情绪面评分'},
                'news_sentiment': {'value': 0.42, 'weight': 0.3, 'description': '新闻情绪'},
                'money_flow': {'value': 125000000, 'weight': 0.4, 'description': '资金流向'},
                'search_index': {'value': 85600, 'weight': 0.3, 'description': '搜索指数'}
            },
            'cycle': {
                'total_score': {'value': 18.5, 'max_score': 20, 'description': '周期面评分'},
                'market_cycle': {'value': '夏', 'weight': 0.5, 'description': '市场周期阶段'},
                'order_degree': {'value': 0.82, 'weight': 0.3, 'description': '市场有序度'},
                'dragon_score': {'value': 0.75, 'weight': 0.2, 'description': '龙头辨识度'}
            }
        }
    
    def _generate_good_factors(self, code, name):
        """生成良好股票的因子数据"""
        import random
        return {
            'basic_info': {
                'name': name,
                'code': code,
                'industry': '电气设备',
                'market_cap': 1200.8,
                'pe_ratio': 32.5
            },
            'fundamental': {
                'total_score': {'value': 22.3, 'max_score': 30, 'description': '基本面评分'},
                'roe': {'value': 0.185, 'weight': 0.3, 'description': '净资产收益率'},
                'revenue_growth': {'value': 0.253, 'weight': 0.25, 'description': '营收增长率'},
                'gross_margin': {'value': 0.287, 'weight': 0.2, 'description': '毛利率'}
            },
            'technical': {
                'total_score': {'value': 18.7, 'max_score': 25, 'description': '技术面评分'},
                'trend_strength': {'value': 0.032, 'weight': 0.4, 'description': '趋势强度'},
                'momentum': {'value': 0.58, 'weight': 0.3, 'description': '动量指标'},
                'volume_ratio': {'value': 1.25, 'weight': 0.3, 'description': '量比'}
            },
            'sentiment': {
                'total_score': {'value': 19.5, 'max_score': 25, 'description': '情绪面评分'},
                'news_sentiment': {'value': 0.18, 'weight': 0.3, 'description': '新闻情绪'},
                'money_flow': {'value': 45000000, 'weight': 0.4, 'description': '资金流向'},
                'search_index': {'value': 65200, 'weight': 0.3, 'description': '搜索指数'}
            },
            'cycle': {
                'total_score': {'value': 16.2, 'max_score': 20, 'description': '周期面评分'},
                'market_cycle': {'value': '春', 'weight': 0.5, 'description': '市场周期阶段'},
                'order_degree': {'value': 0.68, 'weight': 0.3, 'description': '市场有序度'},
                'dragon_score': {'value': 0.65, 'weight': 0.2, 'description': '龙头辨识度'}
            }
        }
    
    def _generate_average_factors(self, code, name):
        """生成一般股票的因子数据"""
        import random
        return {
            'basic_info': {
                'name': name,
                'code': code,
                'industry': '电气设备',
                'market_cap': 650.2,
                'pe_ratio': 35.8
            },
            'fundamental': {
                'total_score': {'value': 17.8, 'max_score': 30, 'description': '基本面评分'},
                'roe': {'value': 0.125, 'weight': 0.3, 'description': '净资产收益率'},
                'revenue_growth': {'value': 0.152, 'weight': 0.25, 'description': '营收增长率'},
                'gross_margin': {'value': 0.218, 'weight': 0.2, 'description': '毛利率'}
            },
            'technical': {
                'total_score': {'value': 14.2, 'max_score': 25, 'description': '技术面评分'},
                'trend_strength': {'value': -0.015, 'weight': 0.4, 'description': '趋势强度'},
                'momentum': {'value': 0.42, 'weight': 0.3, 'description': '动量指标'},
                'volume_ratio': {'value': 0.85, 'weight': 0.3, 'description': '量比'}
            },
            'sentiment': {
                'total_score': {'value': 15.8, 'max_score': 25, 'description': '情绪面评分'},
                'news_sentiment': {'value': -0.08, 'weight': 0.3, 'description': '新闻情绪'},
                'money_flow': {'value': -12000000, 'weight': 0.4, 'description': '资金流向'},
                'search_index': {'value': 32500, 'weight': 0.3, 'description': '搜索指数'}
            },
            'cycle': {
                'total_score': {'value': 12.5, 'max_score': 20, 'description': '周期面评分'},
                'market_cycle': {'value': '秋', 'weight': 0.5, 'description': '市场周期阶段'},
                'order_degree': {'value': 0.45, 'weight': 0.3, 'description': '市场有序度'},
                'dragon_score': {'value': 0.38, 'weight': 0.2, 'description': '龙头辨识度'}
            }
        }
    
    def _generate_random_factors(self, code, name):
        """生成随机股票的因子数据"""
        import random
        return {
            'basic_info': {
                'name': name,
                'code': code,
                'industry': random.choice(['医药生物', '食品饮料', '计算机', '通信']),
                'market_cap': random.uniform(200, 1500),
                'pe_ratio': random.uniform(20, 45)
            },
            'fundamental': {
                'total_score': {'value': random.uniform(12, 28), 'max_score': 30, 'description': '基本面评分'},
                'roe': {'value': random.uniform(0.05, 0.25), 'weight': 0.3, 'description': '净资产收益率'},
                'revenue_growth': {'value': random.uniform(-0.05, 0.35), 'weight': 0.25, 'description': '营收增长率'},
                'gross_margin': {'value': random.uniform(0.15, 0.45), 'weight': 0.2, 'description': '毛利率'}
            },
            'technical': {
                'total_score': {'value': random.uniform(10, 23), 'max_score': 25, 'description': '技术面评分'},
                'trend_strength': {'value': random.uniform(-0.08, 0.08), 'weight': 0.4, 'description': '趋势强度'},
                'momentum': {'value': random.uniform(0.3, 0.8), 'weight': 0.3, 'description': '动量指标'},
                'volume_ratio': {'value': random.uniform(0.6, 1.8), 'weight': 0.3, 'description': '量比'}
            },
            'sentiment': {
                'total_score': {'value': random.uniform(12, 24), 'max_score': 25, 'description': '情绪面评分'},
                'news_sentiment': {'value': random.uniform(-0.3, 0.5), 'weight': 0.3, 'description': '新闻情绪'},
                'money_flow': {'value': random.uniform(-50000000, 80000000), 'weight': 0.4, 'description': '资金流向'},
                'search_index': {'value': random.randint(20000, 90000), 'weight': 0.3, 'description': '搜索指数'}
            },
            'cycle': {
                'total_score': {'value': random.uniform(8, 19), 'max_score': 20, 'description': '周期面评分'},
                'market_cycle': {'value': random.choice(['春', '夏', '秋', '冬']), 'weight': 0.5, 'description': '市场周期阶段'},
                'order_degree': {'value': random.uniform(0.3, 0.85), 'weight': 0.3, 'description': '市场有序度'},
                'dragon_score': {'value': random.uniform(0.2, 0.8), 'weight': 0.2, 'description': '龙头辨识度'}
            }
        }

# 主测试函数
def main():
    print("1. 初始化测试环境...")
    calculator = MockFactorCalculator()
    print("   ✅ 因子计算器初始化成功")
    
    print("\n2. 选择测试股票...")
    test_stocks = ['300274.SZ', '300476.SZ', '300750.SZ', '002415.SZ', '000858.SZ']
    stock_names = {
        '300274.SZ': '阳光电源',
        '300476.SZ': '胜宏科技',
        '300750.SZ': '宁德时代',
        '002415.SZ': '海康威视',
        '000858.SZ': '五粮液'
    }
    
    print(f"   测试 {len(test_stocks)} 只股票:")
    for code in test_stocks:
        print(f"     • {stock_names[code]} ({code})")
    
    print("\n3. 执行因子计算...")
    factors = calculator.calculate_all_factors(test_stocks)
    print(f"   ✅ 成功计算了 {len(factors)} 只股票的因子")
    
    print("\n4. 生成分析报告:")
    print("=" * 70)
    
    # 汇总统计
    total_scores = []
    ratings_count = {
        '强烈买入': 0,
        '买入': 0,
        '持有': 0,
        '谨慎持有': 0,
        '卖出': 0
    }
    
    for code in test_stocks:
        factor_data = factors[code]
        name = factor_data['basic_info']['name']
        
        print(f"\n📈 {name} ({code}):")
        print("-" * 50)
        
        # 显示基本信息
        basic = factor_data['basic_info']
        print(f"   行业: {basic['industry']}")
        print(f"   市值: {basic['market_cap']:.1f}亿元")
        print(f"   市盈率: {basic['pe_ratio']:.1f}")
        
        # 计算各维度得分
        total_score = 0
        max_score = 0
        
        print(f"\n   四维评分:")
        for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
            if factor_type in factor_data:
                score_info = factor_data[factor_type]['total_score']
                score = score_info['value']
                max_val = score_info['max_score']
                
                total_score += score
                max_score += max_val
                
                # 显示各维度得分
                factor_name = {
                    'fundamental': '基本面',
                    'technical': '技术面',
                    'sentiment': '情绪面',
                    'cycle': '周期面'
                }[factor_type]
                
                percentage = (score / max_val) * 100
                print(f"     • {factor_name}: {score:.1f}/{max_val} ({percentage:.1f}%)")
        
        # 计算综合评分
        if max_score > 0:
            percentage = (total_score / max_score) * 100
            total_scores.append(percentage)
            
            # 投资评级
            if percentage >= 85:
                rating = "🔥 强烈买入"
                action = "可考虑加仓"
                ratings_count['强烈买入'] += 1
            elif percentage >= 75:
                rating = "✅ 买入"
                action = "可考虑建仓"
                ratings_count['买入'] += 1
            elif percentage >= 65:
                rating = "🔄 持有"
                action = "持有观察"
