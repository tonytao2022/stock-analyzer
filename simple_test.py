#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资预测模型 - 简化测试脚本
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "代码实现"))

print("=" * 60)
print("投资预测模型 - 简化测试")
print("=" * 60)

try:
    # 导入模块
    print("1. 导入模块...")
    from data_fetcher import DataFetcher
    from factor_calculator import FactorCalculator
    print("   ✅ 模块导入成功")
    
    # 测试数据获取
    print("\n2. 测试数据获取...")
    fetcher = DataFetcher()
    print("   ✅ DataFetcher初始化成功")
    
    # 测试股票代码
    stock_codes = ['300274.SZ', '300476.SZ']
    print(f"   分析股票: {', '.join(stock_codes)}")
    
    # 获取数据
    data = fetcher.get_all_data(stock_codes)
    print(f"   ✅ 数据获取成功，获取了 {len(data)} 只股票的数据")
    
    # 测试因子计算
    print("\n3. 测试因子计算...")
    calculator = FactorCalculator()
    print("   ✅ FactorCalculator初始化成功")
    
    # 计算因子
    factors = calculator.calculate_all_factors(stock_codes)
    print(f"   ✅ 因子计算成功，计算了 {len(factors)} 只股票的因子")
    
    # 关闭数据获取器
    fetcher.close()
    
    # 显示详细结果
    print("\n4. 分析结果:")
    print("-" * 60)
    
    for code, factor_data in factors.items():
        stock_name = factor_data.get('basic_info', {}).get('name', '未知')
        print(f"\n📊 {stock_name} ({code}):")
        print("-" * 40)
        
        # 显示基本信息
        basic_info = factor_data.get('basic_info', {})
        if basic_info:
            print("   基本信息:")
            for key, value in basic_info.items():
                print(f"     • {key}: {value}")
        
        # 计算总分
        total_score = 0
        max_score = 0
        dimension_scores = {}
        
        for factor_type in ['fundamental', 'technical', 'sentiment', 'cycle']:
            if factor_type in factor_data and 'total_score' in factor_data[factor_type]:
                score_info = factor_data[factor_type]['total_score']
                dimension_scores[factor_type] = score_info['value']
                total_score += score_info['value']
                max_score += score_info['max_score']
                
                # 显示各维度得分
                factor_name = {
                    'fundamental': '基本面',
                    'technical': '技术面',
                    'sentiment': '情绪面',
                    'cycle': '周期面'
                }.get(factor_type, factor_type)
                
                print(f"    {factor_name}: {score_info['value']:.1f}/{score_info['max_score']}")
        
        if max_score > 0:
            percentage = (total_score / max_score) * 100
            
            # 获取评级
            if percentage >= 85:
                rating = "🔥 强烈买入"
                action = "可考虑加仓"
            elif percentage >= 75:
                rating = "✅ 买入"
                action = "可考虑建仓"
            elif percentage >= 65:
                rating = "🔄 持有"
                action = "持有观察"
            elif percentage >= 55:
                rating = "⚠️ 谨慎持有"
                action = "考虑减仓"
            else:
                rating = "❌ 卖出"
                action = "建议卖出"
            
            print(f"\n   综合评分: {total_score:.1f}/{max_score} ({percentage:.1f}%)")
            print(f"   投资评级: {rating}")
            print(f"   操作建议: {action}")
            
            # 显示关键因子
            print(f"\n   关键因子分析:")
            self.print_key_factors(factor_data)
    
    print("\n" + "=" * 60)
    print("🎉 测试完成！投资预测模型运行正常。")
    print("\n📝 测试总结:")
    print("1. ✅ 数据获取模块工作正常")
    print("2. ✅ 因子计算模块工作正常")
    print("3. ✅ 四维评分体系运行正常")
    print("4. ✅ 投资建议生成正常")
    print("=" * 60)
    
except Exception as e:
    print(f"\n❌ 测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

def print_key_factors(factor_data):
    """打印关键因子"""
    key_factors = []
    
    # 基本面关键因子
    if 'fundamental' in factor_data:
        fund = factor_data['fundamental']
        if 'roe' in fund:
            roe = fund['roe']['value']
            if roe > 0.15:
                key_factors.append(f"ROE较高 ({roe:.1%})")
            elif roe < 0.05:
                key_factors.append(f"ROE较低 ({roe:.1%})")
        
        if 'gross_margin' in fund:
            margin = fund['gross_margin']['value']
            if margin > 0.30:
                key_factors.append(f"毛利率较高 ({margin:.1%})")
    
    # 技术面关键因子
    if 'technical' in factor_data:
        tech = factor_data['technical']
        if 'trend_strength' in tech:
            trend = tech['trend_strength']['value']
            if trend > 0.05:
                key_factors.append("上涨趋势较强")
            elif trend < -0.05:
                key_factors.append("下跌趋势明显")
    
    # 情绪面关键因子
    if 'sentiment' in factor_data:
        sent = factor_data['sentiment']
        if 'money_flow' in sent:
            flow = sent['money_flow']['value']
            if flow > 0:
                key_factors.append("资金净流入")
            else:
                key_factors.append("资金净流出")
    
    # 周期面关键因子
    if 'cycle' in factor_data:
        cycle = factor_data['cycle']
        if 'market_cycle' in cycle:
            desc = cycle['market_cycle'].get('description', '')
            if desc:
                key_factors.append(f"市场周期: {desc}")
    
    # 打印关键因子
    if key_factors:
        for factor in key_factors[:5]:  # 最多显示5条
            print(f"     • {factor}")
    else:
        print("     • 暂无特别关键因子")

if __name__ == "__main__":
    sys.exit(0)