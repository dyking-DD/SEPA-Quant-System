#!/usr/bin/env python3
"""
SEPA量化系统 - 财务数据引擎 v1.1
从baostock抓取5大类财务数据
"""
import os, sqlite3, time, warnings
import pandas as pd
import baostock as bs
from datetime import datetime
from pathlib import Path
from tqdm import tqdm

warnings.filterwarnings('ignore')
BASE_DIR = Path.home() / "SEPA_Quant_System_Pro"
DB_PATH  = BASE_DIR / "data" / "stocks.db"
LOG_PATH = BASE_DIR / "logs"  / "financial_fetcher.log"
os.makedirs(BASE_DIR / "logs", exist_ok=True)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f: f.write(line + "\n")

def bs_code(code):
    return f"sh.{code}" if code.startswith('6') else f"sz.{code}"

def stat_to_yq(stat_date):
    """从 stat_date 解析年份和季度 (stat_date格式: 2024-03-31)"""
    yr = int(stat_date[:4])
    mo = int(stat_date[5:7])
    q  = (mo - 1) // 3 + 1
    return yr, q

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = [
        ("financial_profit", ["roe_avg","np_margin","gp_margin","net_profit","eps_ttm","mb_revenue","total_share","liqa_share"]),
        ("financial_growth", ["yoy_equity","yoy_asset","yoy_ni","yoy_eps_basic","yoy_pni"]),
        ("financial_dupont", ["dupont_roe","dupont_asset_sto_equity","dupont_asset_turn","dupont_pnitoni","dupont_nitogr","dupont_tax_burden","dupont_intburden","dupont_ebittogr"]),
        ("financial_operation",["nr_turn_ratio","nr_turn_days","inv_turn_ratio","inv_turn_days","ca_turn_ratio","asset_turn_ratio"]),
        ("financial_balance", ["current_ratio","quick_ratio","cash_ratio","yoy_liability","liability_to_asset","asset_to_equity"]),
        ("financial_cashflow", ["ca_to_asset","nca_to_asset","tangible_asset_to_asset","ebit_to_interest","cfo_to_or","cfo_to_np","cfo_to_gr"]),
    ]
    for tbl, cols in tables:
        c.execute(f"""
        CREATE TABLE IF NOT EXISTS {tbl} (
            code TEXT, pub_date TEXT, stat_date TEXT,
            {','.join(f'{c} REAL' for c in cols)},
            year INTEGER, quarter INTEGER,
            PRIMARY KEY (code, year, quarter)
        )""")
    conn.commit()
    conn.close()
    log("数据库初始化完成 ✅")

def save_table(conn, table, cols, rows):
    for row in rows:
        code_b = row[0].replace('sh.','').replace('sz.','')
        pub  = row[1]
        stat = row[2]
        yr, q = stat_to_yq(stat)
        vals = []
        for v in row[3:]:
            try: vals.append(float(v) if v else None)
            except: vals.append(None)
        while len(vals) < len(cols): vals.append(None)
        placeholders = ','.join(['?'] * (len(cols)+5))
        sql = f"INSERT OR REPLACE INTO {table} (code,pub_date,stat_date,{','.join(cols)},year,quarter) VALUES ({placeholders})"
        try:
            conn.execute(sql, [code_b, pub, stat] + vals[:len(cols)] + [yr, q])
        except Exception as e:
            pass

def fetch_financial_for_code(code):
    """获取单只股票所有财务数据"""
    bcode = bs_code(code)
    results = {}
    fns = [
        ('profit',    bs.query_profit_data),
        ('growth',    bs.query_growth_data),
        ('dupont',    bs.query_dupont_data),
        ('operation', bs.query_operation_data),
        ('balance',   bs.query_balance_data),
        ('cashflow',  bs.query_cash_flow_data),
    ]
    today = datetime.now()
    for label, fn in fns:
        results[label] = []
        for year in range(2020, today.year + 1):
            for q in range(1, 5):
                if year == today.year and q > (today.month - 1) // 3 + 1:
                    break
                try:
                    rs = fn(bcode, year=year, quarter=q)
                    while rs.next():
                        results[label].append(rs.get_row_data())
                except:
                    pass
                time.sleep(0.03)
    return results

def main():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    codes = pd.read_sql("SELECT DISTINCT code FROM daily_kline ORDER BY code", conn)['code'].tolist()
    conn.close()
    
    log(f"共 {len(codes)} 只股票，开始获取财务数据...")
    bs.login()
    
    conn = sqlite3.connect(DB_PATH)
    tables = [
        ('financial_profit',    ['roe_avg','np_margin','gp_margin','net_profit','eps_ttm','mb_revenue','total_share','liqa_share']),
        ('financial_growth',    ['yoy_equity','yoy_asset','yoy_ni','yoy_eps_basic','yoy_pni']),
        ('financial_dupont',    ['dupont_roe','dupont_asset_sto_equity','dupont_asset_turn','dupont_pnitoni','dupont_nitogr','dupont_tax_burden','dupont_intburden','dupont_ebittogr']),
        ('financial_operation', ['nr_turn_ratio','nr_turn_days','inv_turn_ratio','inv_turn_days','ca_turn_ratio','asset_turn_ratio']),
        ('financial_balance',   ['current_ratio','quick_ratio','cash_ratio','yoy_liability','liability_to_asset','asset_to_equity']),
        ('financial_cashflow',  ['ca_to_asset','nca_to_asset','tangible_asset_to_asset','ebit_to_interest','cfo_to_or','cfo_to_np','cfo_to_gr']),
    ]
    
    total_saved = 0
    for i, code in enumerate(codes):
        try:
            r = fetch_financial_for_code(code)
            saved = 0
            for j, (label, (tbl, cols)) in enumerate(zip(
                ['profit','growth','dupont','operation','balance','cashflow'], tables
            )):
                if r[label]:
                    save_table(conn, tbl, cols, r[label])
                    saved += len(r[label])
            conn.commit()
            if saved > 0:
                print(f"  [{i+1}/{len(codes)}] {code} ✅ {saved}条财务数据")
                total_saved += saved
            else:
                print(f"  [{i+1}/{len(codes)}] {code} - 无数据")
        except Exception as e:
            print(f"  [{i+1}/{len(codes)}] {code} ❌ {e}")
        time.sleep(0.1)
    
    conn.close()
    bs.logout()
    log(f"✅ 全部完成！共保存 {total_saved} 条财务数据")

if __name__ == "__main__":
    main()
