#!/usr/bin/env python3
import sqlite3, requests, re
from datetime import datetime

DB = '/root/SEPA-Quant-System/data/stocks.db'
THRESHOLD = 3.0

def get_sina_quotes(codes):
    """新浪实时行情"""
    syms = []
    for c in codes:
        syms.append(('sh' if c.startswith('6') else 'sz') + c)
    url = "http://hq.sinajs.cn/list=" + ','.join(syms)
    r = requests.get(url, timeout=10)
    r.encoding = 'gbk'
    data = {}
    for line in r.text.strip().split('\n'):
        m = re.search(r'_(sh|sz)(\d+)="(.+)"', line)
        if m:
            parts = m.group(3).split(',')
            if len(parts) >= 10:
                data[m.group(2)] = {'name': parts[0], 'price': parts[3], 'change': parts[3] and parts[2] and str(round((float(parts[3])-float(parts[2]))/float(parts[2])*100, 2))+'%' or '--'}
    return data

def main():
    conn = sqlite3.connect(DB)
    codes = [r[0] for r in conn.execute("SELECT code FROM focus_codes").fetchall()]
    conn.close()
    print(f"=== {datetime.now()} === {len(codes)} 只")
    d = get_sina_quotes(codes)
    for c in codes:
        if c in d:
            print(f"{d[c]['name']}({c}): {d[c]['price']} {d[c]['change']}")
        else:
            print(f"{c}: 无数据")

if __name__ == '__main__':
    main()
