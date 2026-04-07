#!/usr/bin/env python3
"""SEPA量化系统 - 环境检查"""
import sys
print("=" * 50)
print("  SEPA量化实盘系统 - 环境检查")
print("=" * 50)

# 检查Python版本
v = sys.version_info
print(f"\nPython版本: {v.major}.{v.minor}.{v.micro}  {'✅' if v.major==3 and v.minor>=8 else '⚠️ 建议3.8+'}")

# 检查核心依赖
deps = {
    'akshare': 'akshare - 金融数据',
    'pandas': 'pandas - 数据分析',
    'numpy': 'numpy - 数值计算',
    'requests': 'requests - HTTP请求',
    'scipy': 'scipy - 科学计算',
    'yaml': 'yaml - 配置管理',
    'schedule': 'schedule - 定时任务',
}
print("\n【依赖包检查】")
for mod, desc in deps.items():
    try:
        m = __import__(mod)
        ver = getattr(m, '__version__', 'unknown')
        print(f"  ✅ {mod:12s} ({ver}) - {desc}")
    except ImportError:
        print(f"  ❌ {mod:12s} 缺失   - {desc}")

# 检查数据接口
print("\n【数据接口测试】")
import requests
headers = {'Referer': 'http://gu.qq.com', 'User-Agent': 'Mozilla/5.0'}
try:
    r = requests.get("http://qt.gtimg.cn/q=sh000001", headers=headers, timeout=5)
    print(f"  ✅ 腾讯实时行情API (qt.gtimg.cn)")
except:
    print(f"  ❌ 腾讯实时行情API (qt.gtimg.cn)")

try:
    r = requests.get("http://hq.sinajs.cn/list=sh000001", headers={'Referer':'http://finance.sina.com.cn','User-Agent':'Mozilla/5.0'}, timeout=5)
    print(f"  ✅ 新浪实时行情API (hq.sinajs.cn)")
except:
    print(f"  ❌ 新浪实时行情API (hq.sinajs.cn)")

# akshare数据接口测试
try:
    import akshare as ak
    info = ak.stock_individual_info_em(symbol="000001")
    print(f"  ✅ AKShare实时行情 (东方财富)")
except Exception as e:
    print(f"  ⚠️ AKShare实时行情: {str(e)[:50]}")

print("\n【目录结构】")
import os
base = os.path.expanduser("~/SEPA_Quant_System_Pro")
for d in ['data', 'logs', 'scripts', 'strategies', 'backtests', 'dashboard']:
    path = os.path.join(base, d)
    exists = "✅" if os.path.exists(path) else "❌"
    print(f"  {exists} {d}/")

print("\n" + "=" * 50)
print("环境检查完成！")
print("下一步: python3 01_data_fetcher.py")
print("=" * 50)
