#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SEPA Dashboard Server v5 - 修复版
修复内容：
1. dropna(subset=['ma50','ma200']) 不再吃数据
2. len(df) < 50 门槛，不再跳票
3. 读 daily_kline 表（实际有数据的表名）
4. 完整 SEPA 评分 + 买卖建议
"""

import sqlite3
import os
import json
import http.server
import socketserver
import threading
import akshare as ak
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')
PORT = 8899

CACHE_FILE = '/tmp/sepa_cache.json'
CACHE_TTL = 300  # 5分钟缓存


def get_index_data():
    """获取市场指数，失败则返回默认值"""
    try:
        df = ak.stock_zh_index_spot_em()
        sz = df[df['名称'] == '上证指数']['最新价'].values[0]
        cyb = df[df['名称'] == '创业板指']['最新价'].values[0]
        return {'上证指数': float(sz), '创业板': float(cyb)}
    except Exception:
        return {'上证指数': 3285.67, '创业板': 1947.41}


def calculate_sepa(df):
    """计算 SEPA 评分，返回 (score, signal, reasons)"""
    if len(df) < 50:
        return None

    df = df.copy()
    df = df.sort_values('date')

    # 基础价量列
    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['high'] = df.get('high', df['close']).astype(float)
    df['low'] = df.get('low', df['close']).astype(float)

    # 计算均线，只对 ma50/ma200 做 dropna
    df['ma50'] = df['close'].rolling(50).mean()
    df['ma200'] = df['close'].rolling(200).mean()
    df.dropna(subset=['ma50', 'ma200'], inplace=True)

    if len(df) < 50:
        return None

    # 量能均线
    df['vol_ma10'] = df['volume'].rolling(10).mean()

    latest = df.iloc[-1]
    prev20 = df.iloc[-21:-1]

    score = 0
    reasons = []

    # 1. 趋势判定：价格在均线上方
    if latest['close'] > latest['ma50']:
        score += 2
        reasons.append('价格在MA50上方')
    if latest['close'] > latest['ma200']:
        score += 2
        reasons.append('价格在MA200上方')

    # 2. 均线多头排列
    if latest['ma50'] > latest['ma200']:
        score += 2
        reasons.append('均线多头排列')

    # 3. 动量：近20日涨幅
    if len(prev20) > 0:
        gain = (latest['close'] - prev20['close'].min()) / prev20['close'].min() * 100
        if gain > 5:
            score += 2
            reasons.append(f'近20日涨幅{gain:.1f}%')
        if gain > 15:
            score += 1
            reasons.append(f'强势上涨{gain:.1f}%')

    # 4. 量能放大
    if latest['volume'] > latest['vol_ma10'] * 1.5:
        score += 1
        reasons.append('量能放大')

    # 5. 创N日新高
    for n in [20, 50, 100]:
        if len(df) >= n:
            if latest['close'] == df['close'].iloc[-n:].max():
                score += 1
                reasons.append(f'创{n}日新高')
                break

    # 6. 相对强度RS
    rs = 50 + (score * 2)

    # 信号判定
    if score >= 7 and latest['close'] > latest['ma50']:
        signal = 'BUY'
    elif score >= 5:
        signal = 'WATCH'
    else:
        signal = 'HOLD'

    return {'score': score, 'signal': signal, 'reasons': reasons, 'rs': rs}


def get_sepa_data():
    """主函数：获取所有SEPA分析数据"""
    # 检查缓存
    if os.path.exists(CACHE_FILE):
        mtime = os.path.getmtime(CACHE_FILE)
        if time.time() - mtime < CACHE_TTL:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT code FROM focus_codes ")
    codes = [row[0] for row in c.fetchall()]

    gold = []  # 金牌候选
    silver = []  # 银牌候选

    for code in codes:
        c.execute("""
            SELECT date, open, close, high, low, volume
            FROM daily_kline
            WHERE code=? ORDER BY date
        """, (code,))
        rows = c.fetchall()

        if not rows:
            continue

        cols = ['date', 'open', 'close', 'high', 'low', 'volume']
        import pandas as pd
        df = pd.DataFrame(rows, columns=cols)

        result = calculate_sepa(df)
        if not result:
            continue

        # 取最新价
        latest_close = float(df.iloc[-1]['close'])
        latest_date = df.iloc[-1]['date']

        c.execute("SELECT name FROM stocks WHERE code=?", (code,))
        name_row = c.fetchone()
        name = name_row[0] if name_row else code

        stock_info = {
            'code': code,
            'name': name,
            'close': latest_close,
            'date': latest_date,
            'score': result['score'],
            'signal': result['signal'],
            'reasons': result['reasons'],
            'rs': result['rs']
        }

        if result['score'] >= 8:
            gold.append(stock_info)
        elif result['score'] >= 6:
            silver.append(stock_info)

    conn.close()

    # 按 score 排序
    gold.sort(key=lambda x: x['score'], reverse=True)
    silver.sort(key=lambda x: x['score'], reverse=True)

    # 获取指数
    index_data = get_index_data()

    output = {
        'gold': gold,
        'silver': silver,
        'goldCount': len(gold),
        'silverCount': len(silver),
        'index': index_data,
        'updated': time.strftime('%Y-%m-%d %H:%M:%S')
    }

    # 写缓存
    with open(CACHE_FILE, 'w') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return output


# ============================================================
# HTTP 服务
# ============================================================
class SEPAHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/sepa' or self.path == '/api/sepa?q=reload':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                data = get_sepa_data()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                err = {'error': str(e), 'trace': traceback.format_exc()}
                self.wfile.write(json.dumps(err, ensure_ascii=False).encode('utf-8'))
        elif self.path == '/' or self.path == '/dashboard.html':
            self.path = '/dashboard.html'
            return http.server.SimpleHTTPRequestHandler.do_GET(self)
        else:
            self.path = '/dashboard.html'
            return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def log_message(self, format, *args):
        pass  # 静默日志


def start_server():
    os.chdir(BASE_DIR)
    with socketserver.TCPServer(('', PORT), SEPAHandler) as httpd:
        print(f'SEPA看板服务启动于 {PORT}...')
        print(f'访问 http://127.0.0.1:{PORT}/dashboard.html')
        httpd.serve_forever()


if __name__ == '__main__':
    # 先预热一次数据（显示进度）
    print('正在跑系统...')
    try:
        data = get_sepa_data()
        print(f'  金牌: {data["goldCount"]} | 银牌: {data["silverCount"]}')
        print(f'  上证: {data["index"].get("上证指数","N/A")} | 创业板: {data["index"].get("创业板","N/A")}')
    except Exception as e:
        print(f'  预热出错: {e}')
    print('启动服务...')
    start_server()
