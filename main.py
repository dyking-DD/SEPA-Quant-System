#!/usr/bin/env python3
"""SEPA量化实盘系统 - 主入口"""
import sys
from pathlib import Path

cmds = {
    'check': '00_环境检查',
    'data': '01_data_fetcher',
    'filter': '02_stock_filter',
    'risk': '03_risk_manager',
    'vcp': '04_vcp_detector',
    'backtest': '05_backtest_engine',
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    
    if cmd == 'help':
        print("SEPA量化实盘系统 - 使用说明")
        print("=" * 45)
        print("python3 main.py check      # 环境检查")
        print("python3 main.py data       # 下载全市场数据")
        print("python3 main.py update     # 增量更新数据")
        print("python3 main.py filter     # 运行10层选股")
        print("python3 main.py risk       # 风控模块演示")
        print("python3 main.py vcp <code># VCP形态分析")
        print("python3 main.py backtest  # 启动回测")
        print("python3 main.py all       # 完整流程")
    elif cmd == 'all':
        print("执行完整流程...")
        import subprocess
        for c, m in list(cmds.items())[1:]:  # skip check
            subprocess.run([sys.executable, f"{m}.py"])
    elif cmd and cmd in cmds:
        mod = cmds[cmd]
        print(f"执行模块: {mod}.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location(mod, f"{mod}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        if cmd == 'filter' and hasattr(m, 'run_filter'):
            m.run_filter()
        elif cmd == 'backtest' and hasattr(m, 'BacktestEngine'):
            bt = m.BacktestEngine()
            bt.run()
    else:
        print("用法: python3 main.py [check|data|filter|risk|vcp|backtest|all|help]")
