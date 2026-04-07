"""
数据获取模块 - 使用akshare获取A股实时和历史数据
支持：日线、财务数据、市场概览、龙虎榜、北向资金等
"""

import os
import sys
import time
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DATA_DIR

# 延迟导入akshare（避免启动时慢）
_imported = False
akshare = None

def _ensure_akshare():
    global akshare, _imported
    if not _imported:
        try:
            import akshare as aks
            akshare = aks
            _imported = True
        except ImportError:
            print("[WARN] akshare未安装，尝试安装...")
            os.system(f"{sys.executable} -m pip install akshare -q")
            import akshare as aks
            akshare = aks
            _imported = True

# ==================== 数据库管理 ====================

DB_PATH = DATA_DIR / "astock.db"

def get_db_connection():
    """获取数据库连接"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """初始化数据库表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 日线数据表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_bars (
            code TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (code, trade_date)
        )
    """)
    
    # 财务数据表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS financial_data (
            code TEXT, report_date TEXT,
            revenue REAL, revenue_yoy REAL,
            net_profit REAL, net_profit_yoy REAL, net_profit_qoq REAL,
            roe REAL, nprofit_cagr REAL,
            PRIMARY KEY (code, report_date)
        )
    """)
    
    # 股票基本信息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_info (
            code TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            market TEXT,
            list_date TEXT,
            total_share REAL,
            float_share REAL
        )
    """)
    
    # 龙虎榜数据表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lhb_data (
            trade_date TEXT, code TEXT, name TEXT,
            buy_amount REAL, sell_amount REAL,
            net_amount REAL, reason TEXT,
            PRIMARY KEY (trade_date, code)
        )
    """)
    
    # 北向资金表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS northbound (
            trade_date TEXT PRIMARY KEY,
            hk_shanghai_in REAL, hk_shanghai_out REAL,
            hk_shenzhen_in REAL, hk_shenzhen_out REAL,
            total_net_in REAL
        )
    """)
    
    conn.commit()
    conn.close()
    print(f"[OK] 数据库初始化完成: {DB_PATH}")

# ==================== 数据获取函数 ====================

def get_akshare_stock_info():
    """获取A股股票基本信息"""
    _ensure_akshare()
    try:
        df = akshare.stock_info_a_code_name()
        return df
    except Exception as e:
        print(f"[ERROR] 获取股票信息失败: {e}")
        return pd.DataFrame()


def get_realtime_quotes(codes: list) -> pd.DataFrame:
    """获取实时行情（腾讯接口）"""
    _ensure_akshare()
    try:
        df = akshare.stock_zh_a_spot_em()
        if codes:
            df = df[df['代码'].isin(codes)]
        return df
    except Exception as e:
        print(f"[ERROR] 获取实时行情失败: {e}")
        return pd.DataFrame()


def get_daily_bars(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取日线数据"""
    _ensure_akshare()
    try:
        # 格式转换
        if code.startswith('0') or code.startswith('3'):
            symbol = f"sz{code}"
        else:
            symbol = f"sh{code}"
        
        df = akshare.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date.replace('-', ''),
            end_date=end_date.replace('-', ''),
            adjust="qfq"
        )
        
        if not df.empty:
            df = df.rename(columns={
                '日期': 'trade_date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume',
                '成交额': 'amount', '涨跌幅': 'pct_chg'
            })
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
            df['code'] = code
            df = df[['code', 'trade_date', 'open', 'high', 'low', 'close', 'volume']]
            df['volume'] = df['volume'] * 100  # 手→股
        return df
    except Exception as e:
        print(f"[ERROR] 获取日线数据 {code} 失败: {e}")
        return pd.DataFrame()


def get_financial_data(code: str) -> dict:
    """获取财务数据（营收、净利润、ROE等）"""
    _ensure_akshare()
    result = {}
    try:
        # 获取利润表
        df_profit = akshare.stock_financial_report_sina(
            stock=code, symbol="利润表"
        )
        # 获取主要财务指标
        df_indicator = akshare.stock_financial_abstract_ths(
            symbol=code, indicator="主要指标"
        )
        
        if not df_profit.empty and not df_indicator.empty:
            latest = df_indicator.iloc[-1]
            result = {
                'revenue_yoy': float(latest.get('营业总收入同比(%)', 0) or 0),
                'net_profit_yoy': float(latest.get('净利润同比(%)', 0) or 0),
                'roe': float(latest.get('净资产收益率(%)', 0) or 0),
                'report_date': str(latest.get('报告日期', '')),
            }
    except Exception as e:
        print(f"[WARN] 获取财务数据 {code} 失败: {e}")
    return result


def get_market_anomaly() -> pd.DataFrame:
    """获取涨跌停异动数据"""
    _ensure_akshare()
    try:
        # 涨停股
        df_limit_up = akshare.stock_zt_pool_em(date=datetime.now().strftime('%Y%m%d'))
        # 跌停股
        df_limit_down = akshare.stock_dt_pool_em(date=datetime.now().strftime('%Y%m%d'))
        return df_limit_up, df_limit_down
    except Exception as e:
        print(f"[ERROR] 获取涨跌停数据失败: {e}")
        return pd.DataFrame(), pd.DataFrame()


def get_northbound_flow() -> pd.DataFrame:
    """获取北向资金流向"""
    _ensure_akshare()
    try:
        df = akshare.stock_hsgt_north_net_flow_em(
            symbol="北向资金", indicator="北向资金"
        )
        return df
    except Exception as e:
        print(f"[ERROR] 获取北向资金失败: {e}")
        return pd.DataFrame()


def get_lhb_data(trade_date: str = None) -> pd.DataFrame:
    """获取龙虎榜数据"""
    _ensure_akshare()
    if trade_date is None:
        trade_date = datetime.now().strftime('%Y%m%d')
    try:
        df = akshare.stock_lhb_detail_em(start_date=trade_date, end_date=trade_date)
        return df
    except Exception as e:
        print(f"[ERROR] 获取龙虎榜失败: {e}")
        return pd.DataFrame()


def get_main_capital_flow(codes: list) -> pd.DataFrame:
    """获取主力资金流向"""
    _ensure_akshare()
    try:
        df = akshare.stock_individual_fund_flow_em(symbol="全部")
        if codes:
            df = df[df['代码'].isin(codes)]
        return df
    except Exception as e:
        print(f"[ERROR] 获取主力资金流失败: {e}")
        return pd.DataFrame()


def get_stocks_by_market(market: str = "A股") -> list:
    """获取市场股票列表"""
    _ensure_akshare()
    try:
        df = akshare.stock_info_a_code_name()
        return df['code'].tolist()[:100]  # 限制数量便于测试
    except Exception as e:
        print(f"[ERROR] 获取股票列表失败: {e}")
        return []


# ==================== 批量更新数据 ====================

def update_all_daily_bars(codes: list, days: int = 250):
    """批量更新日线数据"""
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    conn = get_db_connection()
    success = 0
    fail = 0
    
    for i, code in enumerate(codes):
        try:
            df = get_daily_bars(code, start_date, end_date)
            if not df.empty:
                df.to_sql('daily_bars', conn, if_exists='append', index=False)
                success += 1
            else:
                fail += 1
            
            if (i + 1) % 50 == 0:
                print(f"[PROGRESS] 已处理 {i+1}/{len(codes)} 只股票")
            
            time.sleep(0.1)  # 避免请求过快
        
        except Exception as e:
            fail += 1
            print(f"[WARN] 更新 {code} 失败: {e}")
    
    conn.close()
    print(f"[完成] 成功 {success} 只，失败 {fail} 只")


def load_daily_bars_from_db(code: str, days: int = 250) -> pd.DataFrame:
    """从数据库加载日线数据"""
    conn = get_db_connection()
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    
    df = pd.read_sql(
        f"SELECT * FROM daily_bars WHERE code='{code}' AND trade_date>='{start_date}' ORDER BY trade_date",
        conn
    )
    conn.close()
    
    if not df.empty:
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.sort_values('trade_date').reset_index(drop=True)
    
    return df


# ==================== 主程序 ====================

if __name__ == "__main__":
    print("=" * 50)
    print("SEPA量化系统 - 数据引擎")
    print("=" * 50)
    
    init_database()
    
    # 测试：获取上证指数成分股前10只
    print("\n[测试] 获取A股股票列表...")
    codes = get_stocks_by_market()[:10]
    print(f"获取到 {len(codes)} 只股票: {codes[:5]}...")
    
    # 测试：获取单只股票日线
    if codes:
        test_code = '000001'  # 平安银行
        print(f"\n[测试] 获取 {test_code} 日线数据...")
        df = get_daily_bars(test_code, '2026-01-01', '2026-04-03')
        if not df.empty:
            print(f"获取到 {len(df)} 条记录，最新日期: {df['trade_date'].iloc[-1]}")
            print(df.tail(3))
        else:
            print("未获取到数据（可能停牌或代码错误）")
    
    print("\n[OK] 数据引擎测试完成！")
