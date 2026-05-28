#!/bin/bash
# 陶的投资预测模型 - 运行脚本
# 紧急开发版本 v1.0

echo "================================================"
echo "陶的投资预测模型 - 启动"
echo "================================================"

# 检查Python环境
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到Python3，请先安装Python3"
    exit 1
fi

# 检查依赖
echo "检查Python依赖..."
python3 -c "import pandas, numpy, sqlite3" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "安装必要依赖..."
    pip3 install pandas numpy
fi

# 创建虚拟环境（可选）
if [ ! -d "venv" ]; then
    echo "创建Python虚拟环境..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

# 设置环境变量
export PYTHONPATH=$(pwd):$PYTHONPATH

# 执行主程序
echo ""
echo "开始执行分析..."
echo "================================================"

python3 main.py

# 检查执行结果
if [ $? -eq 0 ]; then
    echo ""
    echo "================================================"
    echo "✅ 分析完成！结果已生成"
    echo "================================================"
    
    # 显示生成的文件
    echo ""
    echo "生成的文件:"
    echo "--------------------------------"
    find ../output -type f -name "*.json" -o -name "*.csv" 2>/dev/null | head -10
    
    # 显示最新报告
    latest_report=$(find ../output -name "factors_*.json" -type f -printf "%T+ %p\n" 2>/dev/null | sort -r | head -1 | cut -d' ' -f2-)
    if [ -n "$latest_report" ]; then
        echo ""
        echo "最新分析报告: $latest_report"
    fi
else
    echo ""
    echo "================================================"
    echo "❌ 分析过程中出现错误"
    echo "================================================"
    exit 1
fi

echo ""
echo "使用说明:"
echo "1. 直接运行: ./run.sh"
echo "2. 手动运行: python3 main.py"
echo "3. 查看数据: python3 data_fetcher.py"
echo "4. 查看因子: python3 factor_calculator.py"
echo ""
echo "项目目录:"
echo "  data/      - 数据存储"
echo "  output/    - 输出结果"
echo "  config/    - 配置文件"
echo "================================================"