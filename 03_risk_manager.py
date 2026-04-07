#!/usr/bin/env python3
"""
SEPA量化系统 - 风控模块 v1.0
凯利公式仓位管理 + 三层止损止盈
"""
import sqlite3, numpy as np, pandas as pd
from datetime import datetime
from pathlib import Path

BASE_DIR = Path.home() / "SEPA_Quant_System_Pro"

class KellySizer:
    """凯利公式仓位计算器"""
    def __init__(self, win_rate, avg_win, avg_loss):
        self.win_rate = win_rate
        self.avg_win = avg_win  # 平均盈利幅度（%）
        self.avg_loss = abs(avg_loss)  # 平均亏损幅度（%）
    
    def kelly_fraction(self):
        """凯利公式: f = (b*p - q) / b
        b = 盈亏比, p = 胜率, q = 1-p
        """
        b = self.avg_win / self.avg_loss
        p = self.win_rate
        q = 1 - p
        f = (b * p - q) / b
        return max(0, min(f, 1))  # 限制在[0,1]
    
    def half_kelly(self):
        """半凯利（实盘推荐）"""
        return self.kelly_fraction() / 2
    
    def quarter_kelly(self):
        """四分之一凯利（保守）"""
        return self.kelly_fraction() / 4

class RiskManager:
    """三层止损止盈管理器"""
    
    def __init__(self, stop_loss_pct=0.08, trailing_stop_pct=0.08,
                 take_profit_1=0.20, take_profit_2=0.30, take_profit_3=0.50):
        self.stop_loss_pct = stop_loss_pct      # 初始止损 8%
        self.trailing_stop_pct = trailing_stop_pct  # 移动止损 8%
        self.tp1 = take_profit_1   # 第一止盈目标 20%
        self.tp2 = take_profit_2   # 第二止盈目标 30%
        self.tp3 = take_profit_3   # 第三止盈目标 50%
    
    def check_exit(self, position, current_price):
        """
        检查是否触发止损/止盈
        position = {entry_price, shares, cost_basis, peak_price, stop_price, tp1_set, tp2_set}
        """
        entry = position['entry_price']
        peak = position.get('peak_price', entry)
        current_pnl_pct = (current_price - entry) / entry
        
        signals = []
        exit_signal = None
        
        # 1. 初始止损
        if current_price < entry * (1 - self.stop_loss_pct):
            signals.append(('止损', f"跌破初始止损 {(current_pnl_pct)*100:.1f}% < -{self.stop_loss_pct*100:.0f}%"))
            exit_signal = 'stop_loss'
        
        # 2. 移动止损（从最高点回撤8%）
        trailing_stop = peak * (1 - self.trailing_stop_pct)
        if current_price < trailing_stop and peak > entry * 1.05:
            signals.append(('移动止损', f"从峰值{peak:.2f}回撤{(1-current_price/peak)*100:.1f}%"))
            exit_signal = 'trailing_stop'
        
        # 3. 止盈目标
        # 20%后：将止损线移动到成本+10%
        if current_pnl_pct >= self.tp1:
            new_stop = entry * 1.10
            if current_price < new_stop:
                signals.append(('止盈1触发', f"保护利润，{current_pnl_pct*100:.1f}% > {self.tp1*100:.0f}%，新止损{new_stop:.2f}"))
                exit_signal = 'take_profit_1'
        
        if current_pnl_pct >= self.tp2:
            signals.append(('止盈2触发', f"减仓30%，{current_pnl_pct*100:.1f}% > {self.tp2*100:.0f}%"))
            exit_signal = 'take_profit_2'
        
        if current_pnl_pct >= self.tp3:
            signals.append(('止盈3/全部清仓', f"让利润奔跑，{current_pnl_pct*100:.1f}% > {self.tp3*100:.0f}%"))
            exit_signal = 'take_profit_3'
        
        return exit_signal, signals
    
    def calculate_position_size(self, total_capital, entry, stop_loss,
                                 kelly_fraction=0.25):
        """
        计算仓位
        total_capital: 总资金
        entry: 买入价
        stop_loss: 止损价
        kelly_fraction: 凯利仓位比例（默认25%，即四分之一凯利）
        """
        risk_per_share = entry - stop_loss
        risk_amount = total_capital * kelly_fraction * 0.4  # 修正：只用一次kelly
        shares = int(risk_amount / risk_per_share)
        position_value = shares * entry
        actual_kelly = position_value / total_capital
        
        return {
            'shares': shares,
            'position_value': position_value,
            'risk_per_share': risk_per_share,
            'risk_amount': risk_per_share * shares,
            'actual_fraction': actual_kelly,
            'max_risk_pct': (risk_per_share * shares) / total_capital * 100,
        }
    
    def get_market仓位(self, index_ma_status):
        """
        根据市场状态调整仓位
        index_ma_status: 'bull'(多头) / 'neutral'(震荡) / 'bear'(空头)
        """
        config = {
            'bull': {'max_position': 1.0, 'max_stocks': 8, 'description': '多头市场（满仓）'},
            'neutral': {'max_position': 0.5, 'max_stocks': 4, 'description': '震荡市场（半仓）'},
            'bear': {'max_position': 0.2, 'max_stocks': 1, 'description': '空头市场（轻仓）'},
        }
        return config.get(index_ma_status, config['neutral'])

def run_kelly_demo():
    """凯利公式演示"""
    print("=" * 55)
    print("  凯利公式仓位计算演示")
    print("=" * 55)
    
    scenarios = [
        ("高胜率高盈亏比", 0.60, 0.15, 0.08),
        ("中等策略", 0.50, 0.10, 0.08),
        ("高盈亏低胜率", 0.40, 0.20, 0.10),
        ("保守策略", 0.55, 0.08, 0.06),
    ]
    
    for name, wr, aw, al in scenarios:
        k = KellySizer(wr, aw, al)
        f = k.kelly_fraction()
        half = k.half_kelly()
        quarter = k.quarter_kelly()
        b = aw / al
        print(f"\n{name}")
        print(f"  胜率={wr*100:.0f}%  盈亏比={b:.2f} (平均盈{aw*100:.0f}%/平均亏{al*100:.0f}%)")
        print(f"  理论凯利: {f*100:.1f}%  半凯利: {half*100:.1f}%  四分之一凯利: {quarter*100:.1f}%")
        print(f"  建议实盘仓位: {quarter*100:.1f}%  (凯利{f*100:.1f}%的{quarter/f*100:.0f}%)")
    
    print("\n" + "=" * 55)
    print("  止损止盈规则")
    print("=" * 55)
    rm = RiskManager()
    print(f"  初始止损: -{rm.stop_loss_pct*100:.0f}%")
    print(f"  移动止损: 从高位回撤{rm.trailing_stop_pct*100:.0f}%")
    print(f"  止盈1: +{rm.tp1*100:.0f}% → 止损移至 +10%")
    print(f"  止盈2: +{rm.tp2*100:.0f}% → 减仓30%")
    print(f"  止盈3: +{rm.tp3*100:.0f}% → 全部清仓")

def simulate_portfolio():
    """模拟组合演示"""
    rm = RiskManager()
    print("\n" + "=" * 55)
    print("  组合仓位模拟 (总资金: 100万)")
    print("=" * 55)
    
    total = 1_000_000
    
    # 市场状态
    for market, pos in [('bull', 1.0), ('neutral', 0.5), ('bear', 0.2)]:
        cfg = rm.get_market仓位(market)
        actual_pos = min(pos, cfg['max_position'])
        investable = total * actual_pos
        per_stock = investable / cfg['max_stocks']
        
        print(f"\n  {cfg['description']}  (仓位上限: {actual_pos*100:.0f}%)")
        print(f"    可投资金: {investable/10000:.1f}万  每只股票: {per_stock/10000:.1f}万")
        print(f"    最大持仓: {cfg['max_stocks']}只")
    
    # 单股仓位计算
    print("\n  单股仓位计算示例")
    entry = 50.0; stop = 46.0; kelly_f = 0.25
    result = rm.calculate_position_size(total * 0.5, entry, stop, kelly_f)
    print(f"    入场价: {entry}  止损价: {stop}")
    print(f"    可买股数: {result['shares']}股")
    print(f"    持仓金额: {result['position_value']/10000:.2f}万 ({result['actual_fraction']*100:.1f}%仓位)")
    print(f"    单次最大风险: {result['risk_amount']/10000:.2f}万 ({result['max_risk_pct']:.2f}%资金)")

if __name__ == "__main__":
    run_kelly_demo()
    simulate_portfolio()
