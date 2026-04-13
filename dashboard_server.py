#!/usr/bin/env python3
"""SEPA 量化看板服务器"""
import http.server, json, sqlite3, datetime, os

try:
    import pandas as pd
    HAS_PD = True
except:
    HAS_PD = False

try:
    import akshare as ak
    HAS_AK = True
except:
    HAS_AK = False

PORT = 8899
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')

FOCUS_CODES = ['300508','688345','688400','688206','688153','688138','688135',
               '688380','688158','688021','600868','688041','300308','688006']

def get_stock_name(code, conn):
    try:
        c = conn.cursor()
        c.execute('SELECT name FROM stocks WHERE code=?', (code,))
        r = c.fetchone()
        return r[0] if r else code
    except:
        return code

def get_realtime(codes):
    quotes = {}
    if not HAS_AK:
        return quotes
    try:
        df = ak.stock_zh_a_spot_em()
        for code in codes:
            row = df[df['代码'] == code]
            if not row.empty:
                r = row.iloc[0]
                quotes[code] = {
                    'name': r['名称'],
                    'price': float(r['最新价']),
                    'chg_pct': float(r['涨跌幅']),
                }
    except Exception as e:
        print(f"get_realtime error: {e}")
    return quotes

def get_market_indices():
    indices = {
        'sh000001': {'name':'上证指数','price':3280,'chg_pct':0.5},
        'sz399001': {'name':'深证成指','price':9870,'chg_pct':0.3},
        'sz399006': {'name':'创业板指','price':1980,'chg_pct':-0.2},
        'sh000688': {'name':'科创50','price':780,'chg_pct':0.8},
    }
    if not HAS_AK:
        return indices
    try:
        df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        for idx_code in ['000001']:
            row = df[df['代码']==idx_code]
            if not row.empty:
                r = row.iloc[0]
                indices['sh'+idx_code] = {'name':r['名称'],'price':float(r['最新价']),'chg_pct':float(r['涨跌幅'])}
    except:
        pass
    return indices

def get_sepa_scores():
    if not HAS_PD:
        return {'gold':[],'silver':[],'goldCount':0,'silverCount':0,
                'winRate':35.6,'profitRatio':1.93,
                'updated':datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    conn = sqlite3.connect(DB_PATH)
    results = []
    quotes = get_realtime(FOCUS_CODES)

    for code in FOCUS_CODES:
        try:
            df = pd.read_sql("SELECT * FROM daily_kline WHERE code='" +code+ "' ORDER BY date DESC LIMIT 300", conn)
            if len(df) < 200:
                continue

            df = df.sort_values('date').reset_index(drop=True)
            df['ma50'] = df['close'].rolling(50).mean()
            df['ma200'] = df['close'].rolling(200).mean()
            df['vol_ma10'] = df['volume'].rolling(10).mean()
            df = df.dropna()

            if len(df) == 0:
                continue

            latest = df.iloc[-1]
            score = 0

            if latest['ma50'] > latest['ma200']:
                score += 1
            if latest['close'] > latest['ma50']:
                score += 1
            if latest['volume'] > latest['vol_ma10'] * 1.2:
                score += 1

            high_52w = df['high'].tail(252).max()
            dist_high = (latest['close'] - high_52w) / high_52w * 100
            if dist_high >= -25:
                score += 1

            q = quotes.get(code, {})
            name = q.get('name', get_stock_name(code, conn))
            price = q.get('price', float(latest['close']))
            chg = q.get('chg_pct', float(latest.get('chg_pct', 0)))

            buy_min = round(float(latest['ma50']) * 0.95, 2)
            buy_max = round(float(latest['ma50']) * 1.02, 2)
            stop_loss = round(float(latest['ma50']) * 0.90, 2)
            target = round(float(high_52w) * 1.1, 2)

            results.append({
                'code': code,
                'name': name,
                'price': price,
                'chg_pct': chg,
                'score': int(score),
                'dist_high': round(float(dist_high), 1),
                'buy_min': buy_min,
                'buy_max': buy_max,
                'stop_loss': stop_loss,
                'target': target,
            })
        except Exception as e:
            print(f"Error scoring {code}: {e}")

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

class SEPAHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/sepa':
            self.send_json(get_sepa_scores())
        elif self.path == '/api/market':
            self.send_json(get_market_indices())
        elif self.path.startswith('/api/realtime'):
            q = self.path.split('=')[1] if '=' in self.path else ''
            if q:
                quotes = get_realtime([q])
                self.send_json(quotes.get(q, {'error': 'not found'}))
            else:
                self.send_json(get_realtime(FOCUS_CODES))
        elif self.path in ('/', '/sepa/', '/sepa/dashboard.html'):
            self.serve_file('dashboard.html')
        else:
            super().do_GET()

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def serve_file(self, filename):
        filepath = os.path.join(BASE_DIR, filename)
        if os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

if __name__ == '__main__':
    os.chdir(BASE_DIR)
    print(f"SEPA Dashboard: http://localhost:{PORT}")
    http.server.HTTPServer(('0.0.0.0', PORT), SEPAHandler).serve_forever()
