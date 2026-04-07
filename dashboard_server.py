#!/usr/bin/env python3
"""
SEPA量化系统 - Web看板服务器 v1.0
提供 API: /api/market, /api/sepa, /api/realtime
"""
import os, sqlite3, json, time, threading, datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import pandas as pd
import requests

BASE_DIR = Path.home() / "SEPA_Quant_System_Pro"
DB_PATH = BASE_DIR / "data" / "stocks.db"

PORT = 8899

# ===== 实时行情（腾讯API）=====
def get_realtime(codes):
    if not codes: return {}
    syms = ",".join([f"sh{c}" if c.startswith("6") else f"sz{c}" for c in codes])
    try:
        r = requests.get(f"http://qt.gtimg.cn/q={syms}", timeout=5)
        quotes = {}
        for line in r.text.strip().split("\n"):
            if '="' not in line: continue
            val = line.split('="')[1].rstrip('";')
            p = val.split('~')
            if len(p) < 35: continue
            try:
                code = p[1].strip()
                sym = code[:2]
                c = code[2:]
                quotes[c] = {
                    'name': p[1], 'price': float(p[3]), 'close': float(p[4]),
                    'open': float(p[5]), 'vol': float(p[6]),
                    'high': float(p[33]), 'low': float(p[34]),
                    'chg_pct': float(p[32]) or 0,
                    'amount': float(p[37]),
                }
            except: continue
        return quotes
    except:
        return {}

# ===== 市场指数 =====
def get_market():
    indices = {
        'sh000001': '上证指数', 'sz399001': '深证成指',
        'sz399006': '创业板指', 'sh000688': '科创50'
    }
    quotes = get_realtime(list(indices.keys()))
    result = {}
    for code, name in indices.items():
        if code in quotes:
            q = quotes[code]
            result[code] = {'name': name, 'price': q['price'], 'chg_pct': q['chg_pct']}
    return result

# ===== SEPA评分 =====
def get_sepa_scores():
    """从数据库读取最新的SEPA评分"""
    conn = sqlite3.connect(DB_PATH)
    
    # 尝试从现有分析结果读取
    try:
        df = pd.read_sql("SELECT * FROM sepa_results ORDER BY date DESC LIMIT 100", conn)
    except:
        df = pd.DataFrame()
    
    # 从K线数据计算实时SEPA评分
    codes = pd.read_sql("SELECT DISTINCT code FROM daily_kline", conn)['code'].tolist()
    conn.close()
    
    # 只分析重点股
    focus_codes = ['300750','300308','600791','002025','600036','002594','002371',
                   '603986','688256','688041','688111','688012','600519','002049']
    
    results = []
    quotes = get_realtime(focus_codes)
    
    conn = sqlite3.connect(DB_PATH)
    for code in focus_codes:
        if code not in quotes: continue
        try:
            df = pd.read_sql(f"SELECT * FROM daily_kline WHERE code='{code}' ORDER BY date DESC LIMIT 300", conn)
            if len(df) < 200: continue
            
            # 计算MA
            df = df.sort_values('date').reset_index(drop=True)
            df['ma50'] = df['close'].rolling(50).mean()
            df['ma200'] = df['close'].rolling(200).mean()
            df['vol_ma10'] = df['volume'].rolling(10).mean()
            df = df.dropna()
            
            latest = df.iloc[-1]
            prev = df.iloc[-5]  # 5天前
            
            score = 0
            checks = {}
            
            # 1. 趋势：MA50 > MA200
            checks['trend'] = latest['ma50'] > latest['ma200']
            if checks['trend']: score += 1
            
            # 2. 动量：close > MA50
            checks['momentum'] = latest['close'] > latest['ma50']
            if checks['momentum']: score += 1
            
            # 3. 放量：今日量 > 1.2x均量
            checks['volume'] = latest['volume'] > latest['vol_ma10'] * 1.2
            if checks['volume']: score += 1
            
            # 4. 距52周高点
            high_52w = df['high'].tail(252).max()
            dist_high = (latest['close'] - high_52w) / high_52w * 100
            checks['near_high'] = dist_high >= -25
            if checks['near_high']: score += 1
            
            q = quotes.get(code, {})
            
            results.append({
                'code': code,
                'name': q.get('name', code),
                'price': q.get('price'),
                'chg_pct': q.get('chg_pct', 0),
                'score': score,
                'checks': checks,
                'dist_high': dist_high,
                'ma50': latest['ma50'],
                'ma200': latest['ma200'],
                'high_52w': high_52w,
            })
        except Exception as e:
            pass
    
    conn.close()
    
    gold = [r for r in results if r['score'] >= 4]
    silver = [r for r in results if 2 <= r['score'] <= 3]
    
    return {
        'gold': gold,
        'silver': silver,
        'goldCount': len(gold),
        'silverCount': len(silver),
        'winRate': 35.6,
        'profitRatio': 1.93,
        'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

# ===== HTTP Server =====
class SEPAHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)
    
    def do_GET(self):
        if self.path == '/api/market':
            self.send_json(get_market())
        elif self.path == '/api/sepa':
            self.send_json(get_sepa_scores())
        elif self.path == '/api/realtime':
            codes = pd.read_sql("SELECT DISTINCT code FROM daily_kline", sqlite3.connect(DB_PATH))['code'].tolist()
            self.send_json(get_realtime(codes))
        else:
            super().do_GET()
    
    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
    
    def log_message(self, format, *args):
        pass  # 静默日志

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), SEPAHandler)
    print(f"🦞 SEPA看板服务器启动: http://localhost:{PORT}/dashboard.html")
    print(f"📊 API: http://localhost:{PORT}/api/sepa | /api/market | /api/realtime")
    server.serve_forever()

if __name__ == '__main__':
    run_server()
