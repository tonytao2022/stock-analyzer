#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
陶的投资预测模型 - 主执行脚本
紧急开发版本 v1.0
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "代码实现"))

from data_fetcher import DataFetcher
from factor_calculator import FactorCalculator

def main():
    """主函数"""
    print("=" * 60)
    print("陶的投资预测模型 - 紧急开发版本 v1.0")
    print("=" * 60)
    
    # 试点股票
    stock_codes = ['300274.SZ', '300476.SZ']
    
    print(f"分析股票: {', '.join(stock_codes)}")
    print()
    
    try:
        # 第一步：获取数据
        print("第一步：数据获取")
        print("-" * 40)
        fetcher = DataFetcher()
        data = fetcher.get_all_data(stock_codes)
        fetcher.close()
        
        print("\n" + "=" * 60)
        
        # 第二步：计算因子
        print("第二步：因子计算")
        print("-" * 40)
        calculator = FactorCalculator()
        factors = calculator.calculate_all_factors(stock_codes)
        
        print("\n" + "=" * 60)
        
        # 第三步：生成投资建议
        print("第三步：投资建议")
        print("-" * 40)
        
        if factors:
            print("\n投资建议摘要:")
            print("-" * 40)
            
            for code, factor_data in factors.items():
                stock_name = factor_data.get('basic_info', {}).get('name', '未知')
                
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
                    
                    print(f"\n📈 {stock_name} ({code}):")
                    print(f"   综合评分: {total_score:.1f}/{max_score} ({percentage:.1f}%)")
                    print(f"   投资评级: {rating}")
                    print(f"   操作建议: {action}")
                    
                    # 各维度分析
                    print(f"   维度分析:")
                    print(f"     • 基本面: {dimension_scores.get('fundamental', 0):.1f}/30")
                    print(f"     • 技术面: {dimension_scores.get('technical', 0):.1f}/25")
                    print(f"     • 情绪面: {dimension_scores.get('sentiment', 0):.1f}/25")
                    print(f"     • 周期面: {dimension_scores.get('cycle', 0):.1f}/20")
                    
                    # 关键因子提示
                    print(f"   关键提示:")
                    print_key_insights(factor_data)
        
        print("\n" + "=" * 60)
        print("🎉 分析完成！")
        print("\n注意事项:")
        print("1. 此为紧急开发版本，数据可能不完整")
        print("2. 投资建议仅供参考，不构成投资依据")
        print("3. 实际投资请结合更多信息和分析")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 程序执行出错: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

def print_key_insights(factor_data):
    """打印关键洞察"""
    insights = []
    
    # 基本面洞察
    if 'fundamental' in factor_data:
        fund = factor_data['fundamental']
        if 'roe' in fund and fund['roe']['value'] > 0.15:
            insights.append("ROE较高，盈利能力良好")
        if 'gross_margin' in fund and fund['gross_margin']['value'] > 0.30:
            insights.append("毛利率较高，产品有竞争力")
    
    # 技术面洞察
    if 'technical' in factor_data:
        tech = factor_data['technical']
        if 'trend_strength' in tech:
            position = tech['trend_strength']['value']
            if position > 0.05:
                insights.append("处于上涨趋势")
            elif position < -0.05:
                insights.append("处于下跌趋势")
    
    # 情绪面洞察
    if 'sentiment' in factor_data:
        sent = factor_data['sentiment']
        if 'money_flow' in sent and sent['money_flow']['value'] > 0:
            insights.append("资金呈流入状态")
    
    # 周期面洞察
    if 'cycle' in factor_data:
        cycle = factor_data['cycle']
        if 'market_cycle' in cycle:
            desc = cycle['market_cycle'].get('description', '')
            if desc:
                insights.append(f"市场处于{desc}")
    
    # 打印洞察
    if insights:
        for i, insight in enumerate(insights[:3]):  # 最多显示3条
            print(f"     • {insight}")
    else:
        print("     • 暂无特别提示")

if __name__ == "__main__":
    sys.exit(main())