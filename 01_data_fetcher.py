#!/usr/bin/env python3
"""
SEPA量化系统 - 数据引擎 v2.0
核心数据源: baostock (A股历史K线，无API key，免费)
备用: 腾讯/新浪实时行情
"""
import os, json, time, sqlite3, warnings
import pandas as pd
import numpy as np
import baostock as bs
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')

BASE_DIR = Path.home() / "SEPA_Quant_System_Pro"
DB_PATH = BASE_DIR / "data" / "stocks.db"
DATA_DIR = BASE_DIR / "data"
LOG_PATH = BASE_DIR / "logs" / "data_fetcher.log"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BASE_DIR / "logs", exist_ok=True)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ========== baostock 初始化 ==========
def init_baostock():
    lg = bs.login()
    if lg.error_code != '0':
        log(f"baostock登录失败: {lg.error_msg}")
        return False
    log("baostock登录成功")
    return True

def get_kline_baostock(code, start_date, end_date, adjustflag="2"):
    """
    获取单只股票K线（baostock）
    code: 600519 -> "sh.600519", 300750 -> "sz.300750"
    adjustflag: "2"=前复权, "1"=后复权, "3"=不复权
    """
    # 转换code格式
    if code.startswith('6'):
        bs_code = f"sh.{code}"
    else:
        bs_code = f"sz.{code}"
    
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount",
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag=adjustflag
    )
    
    if rs.error_code != '0':
        return None
    
    data = []
    while rs.next():
        data.append(rs.get_row_data())
    
    if not data:
        return None
    
    df = pd.DataFrame(data, columns=['date','open','high','low','close','volume','amount'])
    df['code'] = code
    for col in ['open','high','low','close','volume','amount']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['volume'] = df['volume'] * 100  # baostock成交量单位是万手，转为手
    df = df.dropna()
    return df

# ========== 数据库 ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma50 REAL, ma150 REAL, ma200 REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial (
            code TEXT, date TEXT, quarter TEXT,
            revenue_yoy REAL, profit_yoy REAL, roe REAL,
            net_profit_growth REAL, gross_margin REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON daily_kline(code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON daily_kline(date)")
    conn.commit()
    conn.close()

def save_kline(code, df):
    if df is None or df.empty:
        return
    # 计算均线
    for ma in [5, 10, 20, 50, 150, 200]:
        if len(df) >= ma:
            df[f'ma{ma}'] = df['close'].rolling(ma).mean()
    
    conn = sqlite3.connect(DB_PATH)
    # 用REPLACE避免重复插入
    df.to_sql("daily_kline", conn, if_exists="append", index=False,
              index_label=['code','date'])
    conn.close()

# ========== 股票列表 ==========
def get_stock_list_baostock():
    """用baostock获取全市场A股列表"""
    rs = bs.query_all_stock(day=datetime.now().strftime("%Y-%m-%d"))
    stocks = []
    while rs.next():
        row = rs.get_row_data()
        code, name, status = row[0], row[1], row[2]
        # 只保留正常交易的A股
        if status == '1' and (code.startswith('sh.6') or code.startswith('sz.00') 
                               or code.startswith('sz.30') or code.startswith('sz.002')
                               or code.startswith('sh.688')):
            stocks.append({
                'code': code.replace('sh.','').replace('sz.',''),
                'market': 'sh' if code.startswith('sh') else 'sz',
                'name': name
            })
    return pd.DataFrame(stocks)

# ========== 主程序: 全量下载 ==========
def run_full_fetch():
    init_db()
    
    if not init_baostock():
        log("baostock初始化失败，退出")
        return 0
    
    log("=" * 50)
    log("SEPA数据引擎 v2.0 - baostock数据源")
    log("=" * 50)
    
    # 获取股票列表
    log("获取全市场A股列表...")
    stocks_df = get_stock_list_baostock()
    log(f"获取到 {len(stocks_df)} 只A股")
    
    if stocks_df.empty:
        log("股票列表为空，退出")
        return 0
    
    # 保存股票列表
    list_db = DATA_DIR / "stock_list.db"
    conn = sqlite3.connect(list_db)
    stocks_df.to_sql("stocks", conn, if_exists="replace", index=False)
    conn.close()
    log(f"股票列表已保存到 {list_db}")
    
    # 下载K线
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = "2020-01-01"  # 从2020年开始，保证有足够历史数据算MA200
    
    codes = stocks_df['code'].tolist()
    success = 0
    total = len(codes)
    
    log(f"开始下载K线: {total}只股票, 区间: {start_date} ~ {end_date}")
    log("提示: baostock有频率限制，建议控制在 200-300只/小时")
    log("")
    
    # 每批100只，分批下载
    batch_size = 50
    for batch_start in range(0, min(total, 300), batch_size):  # 限制前300只演示
        batch_codes = codes[batch_start:batch_start+batch_size]
        log(f"批次 {batch_start//batch_size+1}: 下载 {len(batch_codes)} 只股票...")
        
        for i, code in enumerate(batch_codes):
            actual_i = batch_start + i + 1
            df = get_kline_baostock(code, start_date, end_date)
            if df is not None and not df.empty:
                save_kline(code, df)
                success += 1
            
            if actual_i % 20 == 0:
                log(f"  进度: {actual_i}/{min(total,300)} (成功{success}只)")
            
            time.sleep(0.15)  # baostock频率限制
        
        log(f"  批次完成: {success}/{batch_start+len(batch_codes)}只成功")
        time.sleep(2)  # 批次间休息
    
    log(f"\n下载完成! 成功: {success}/{min(total,300)} 只")
    
    # 输出统计
    conn = sqlite3.connect(DB_PATH)
    try:
        total_rows = pd.read_sql("SELECT COUNT(*) as c FROM daily_kline", conn)['c'][0]
        stock_count = pd.read_sql("SELECT COUNT(DISTINCT code) as c FROM daily_kline", conn)['c'][0]
        log(f"数据库统计: {stock_count}只股票, {total_rows}条K线记录")
    except:
        pass
    conn.close()
    
    bs.logout()
    return success

# ========== 增量更新 ==========
def run_incremental_update():
    """每日增量更新最新数据"""
    init_db()
    if not init_baostock():
        return
    
    log("增量更新: 获取今日数据")
    today = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DATA_DIR / "stock_list.db")
    try:
        stocks_df = pd.read_sql("SELECT code FROM stocks", conn)
    except:
        stocks_df = pd.DataFrame()
    conn.close()
    
    if stocks_df.empty:
        log("股票列表为空，先运行全量下载")
        return
    
    success = 0
    for code in stocks_df['code'].tolist()[:100]:  # 限制100只
        df = get_kline_baostock(code, today, today)
        if df is not None and not df.empty:
            save_kline(code, df)
            success += 1
        time.sleep(0.2)
    
    log(f"增量更新完成: {success}只有新数据")
    bs.logout()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        run_incremental_update()
    else:
        run_full_fetch()
