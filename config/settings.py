"""
配置文件 - A股量化实盘系统 SEPA+VCP优化版
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
CONFIG_DIR = BASE_DIR / "config"

# 数据源配置
DATA_SOURCE = "akshare"  # akshare / tushare_pro

# ==================== 选股参数 ====================

FILTER_CONFIG = {
    # 层级1: 基础过滤
    "exclude_st": True,           # 剔除ST/*ST
    "min_listed_days": 120,       # 上市满120天（A股优化：原365天→120天）
    "exclude_limit_up": True,      # 剔除涨停股（买入时不追涨停）
    "exclude_limit_down": True,    # 剔除跌停股

    # 层级2: 趋势过滤（四层金牌标准）
    "trend_ma_short": 50,
    "trend_ma_mid": 150,
    "trend_ma_long": 200,
    "trend_ma_slope_min": 0,      # 200日均线最小斜率（>0表示上升）
    "trend_ma_slope_days": 20,    # 持续上升天数
    "price_from_52w_low_pct": 25, # 距52周低点≥25%
    "price_from_52w_high_pct": 25,# 距52周高点≤25%

    # 层级3: 基本面过滤
    "min_revenue_growth": 25,     # 营收同比≥25%
    "min_profit_growth_yoy": 30,  # 净利润同比≥30%
    "min_profit_growth_qoq": 0,   # 净利润环比>0
    "min_roe": 15,                # ROE≥15%
    "min_nprofit_cagr": 20,       # 三年净利润CAGR≥20%

    # 层级4: 成交量确认
    "volume_ma_short": 10,        # 短期均量天数
    "volume_ma_long": 120,        # 长期均量天数
    "volume_ratio_min": 1.0,      # 放量倍数（放量确认）

    # 层级5: 催化剂检测
    "catalyst_enabled": True,
    "profit_gap_min_pct": 5,      # 净利润断层：跳空高开≥5%
    "profit_gap_days": 5,         # 缺口5天内未被完全回补（原3天→5天，A股适配）
    "analyst_upgrade_min": 2,    # ≥2家机构上调盈利预测
    "catalyst_required": True,    # 催化剂是否为必须条件

    # 流动性过滤（新增）
    "min_daily_turnover": 5e8,   # 日均成交额≥5亿（A股适配）
}

# ==================== VCP形态参数 ====================

VCP_CONFIG = {
    "lookback_window": 120,       # VCP识别回溯窗口
    "min_bands": 2,               # 最少收缩波段数
    "amplitude_decay_ratio": 1.2, # 每个波段振幅至少缩小20%
    "volume_decay_threshold": 0.7,# 每个波段均量至少萎缩30%
    "vcp_score_threshold": 80,    # VCP评分阈值
    "pivot_breakout_pct": 0,     # 收盘价突破前高即确认
}

# ==================== 仓位管理参数 ====================

POSITION_CONFIG = {
    "kelly_fraction": 0.5,       # 凯利半仓（降低模型误差风险）
    "max_single_position": 0.25,  # 单只最大仓位25%
    "max_risk_per_trade": 0.025, # 单笔风险≤2.5%
    "market_bull_limit": 1.0,     # 市场强势：满仓
    "market_neutral_limit": 0.5, # 市场震荡：≤50%
    "market_bear_limit": 0.2,    # 市场弱势：≤20%
}

# ==================== 止损止盈参数（A股优化） ====================

STOP_LOSS_CONFIG = {
    # 三层止损防线
    "initial_stop_pct": 7,        # 初始止损：从入场价回撤7%（原2%，A股适配）
    "ma_stop_days": 50,           # 均线止损：跌破50日均线
    "trailing_stop_pct": 8,       # 移动止损：从最高点回撤8%

    # 止盈规则
    "first_profit_target": 20,   # 首个止盈目标20%
    "first_exit_pct": 30,        # 20%时减仓30%
    "second_profit_target": 50,  # 第二个止盈目标50%
    "second_exit_pct": 30,       # 50%时再减仓30%
    "profit_protect_stop": 10,   # 盈利30%后，止盈线上移至成本+10%
    "long_term_ma_stop_days": 150,# 长期趋势止损：跌破150日均线清仓
}

# ==================== 回测参数 ====================

BACKTEST_CONFIG = {
    "start_date": "2020-01-01",
    "end_date": "2026-04-03",
    "commission": 0.00015,        # 佣金万1.5
    "slippage": 0.001,           # 滑点0.1%
    "initial_cash": 1_000_000,   # 初始资金100万
}

# ==================== 市场判断 ====================

MARKET_CONFIG = {
    "index_code": "000001.SH",   # 上证指数
    "market_ma_days": 120,       # 判断120日均线
}
