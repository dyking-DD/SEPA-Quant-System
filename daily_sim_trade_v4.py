#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日模拟交易 v4.2 激进版
聚焦：创业板(sz300) + 科创板(sh688)
核心：激进止盈止损 + RSI过滤 + 均线确认
"""
import sys, json, time, os, requests
from datetime import datetime

sys.path.insert(0, '/opt/stock-monitor')

CAPITAL = 100000
MAX_POSITIONS = 5
MIN_POSITION = 3000
MAX_POSITION = 50000
STOP_LOSS_PCT = 0.95       # 止损 5%（激进型）
TAKE_PROFIT_PCT = 1.10     # 止盈10%（激进型）
TRAILING_STOP_PCT = 0.97   # 涨超8%后移动止损（激进型）
MAX_HOLD_DAYS = 5      # 持仓5天（激进型）
INDEX_DROP_THRESHOLD = -1.5

DATA_DIR = '/opt/stock-monitor/data'
POSITIONS_FILE = f'{DATA_DIR}/sim_positions_v4.json'
DAILY_REPORT_FILE = f'{DATA_DIR}/daily_report_v4.json'
RUN_LOCK = f'{DATA_DIR}/.run_lock_v4'

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(f'{DATA_DIR}/logs', exist_ok=True)

# ==================== 日志 ====================
def get_log():
    log_file = f'{DATA_DIR}/logs/trade_v4_{datetime.now().strftime("%Y%m%d")}.log'
    def _log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    return _log

# ==================== QQ推送 ====================
def send_qq_message(title, content):
    try:
        import subprocess
        msg = f'【{title}】{content}'
        subprocess.run(
            ['openclaw', 'message', 'send', '--channel', 'qqbot', '--target', 'qqbot:c2c:5DB38D4F29AFE7612B92CFFFF1BF039B', '-m', msg],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        print(f'QQ推送失败: {e}')
        try:
            import requests as req
            req.post('https://open.feishu.cn/open-apis/bot/v2/hook/86f530d2-5817-42f7-9b8b-dc5204efb638',
                json={"msg_type": "text", "content": {"text": f"【{title}】{content}"}}, timeout=10)
        except:
            pass


# ==================== 股票池：创业板 + 科创板 ====================
def get_gy_ck_codes():
    """获取创业板(sz300) + 科创板(sh688) 股票代码"""
    codes = []
    for i in range(1, 800):
        codes.append(f'sz300{i:03d}')
    # 科创板只保留001-400（后400只很多没有历史数据）
    for i in range(1, 400):
        codes.append(f'sh688{i:03d}')
    return codes

# ==================== 数据获取 ====================
def get_batch_quotes(codes):
    results = {}
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        url = 'https://hq.sinajs.cn/list=' + ','.join(batch)
        try:
            r = requests.get(url,
                headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'},
                timeout=10)
            for line in r.text.strip().split('\n'):
                if 'hq_str_' not in line or '=' not in line:
                    continue
                try:
                    code = line.split('hq_str_')[1].split('=')[0]
                    data = line.split('"')[1].split(',')
                    if len(data) < 10 or not data[0] or data[0] in ('未知','N/A',''):
                        continue
                    name = data[0]
                    current = float(data[3])
                    prev_close = float(data[2])
                    open_p = float(data[1])
                    high = float(data[4])
                    low = float(data[5])
                    volume = float(data[8])
                    amount = float(data[9])
                    if current <= 0 or prev_close <= 0:
                        continue
                    change = (current - prev_close) / prev_close * 100
                    amount_yi = amount / 100000000
                    results[code] = {
                        'name': name, 'price': current, 'open': open_p,
                        'high': high, 'low': low, 'prev_close': prev_close,
                        'change': change, 'volume': volume,
                        'amount': amount, 'amount_yi': amount_yi
                    }
                except:
                    continue
        except:
            continue
        time.sleep(0.08)
    return results

# ==================== MA数据（新浪API） ====================
def get_ma_data(code):
    """获取MA5/MA10和量比（新浪API）"""
    try:
        url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=15'
        r = requests.get(url,
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'},
            timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or len(data) < 6:
            return None
        
        closes = [float(d['close']) for d in data]
        ma5 = data[-1].get('ma_price5')
        ma5 = ma5 if ma5 else sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
        
        today_vol = float(data[-1]['volume'])
        ma_vol = data[-1].get('ma_volume5')
        vol_ratio = today_vol / ma_vol if ma_vol and ma_vol > 0 else 1.0
        
        current = closes[-1]
        ma_bullish = bool(ma10 and ma5 > ma10)
        price_vs_ma5 = (current - ma5) / ma5 * 100 if ma5 else 0
        
        return {
            'ma5': ma5, 'ma10': ma10,
            'ma_bullish': ma_bullish,
            'price_vs_ma5': price_vs_ma5,
            'vol_ratio': vol_ratio,
            'closes': closes
        }
    except:
        return None



# ==================== RSI数据（基于K线数据） ====================
def get_rsi(code, period=14):
    try:
        url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=40'
        r = requests.get(url,
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'},
            timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or len(data) < period + 1:
            return None
        closes = [float(d['close']) for d in data]
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 1)
    except:
        return None

# ==================== 资金流向估算 ====================
def estimate_capital_flow(code, change, amount_yi):
    """
    估算个股资金流向强度
    涨幅1.5-5% + 成交额>8亿 = 资金关注
    涨幅2-4%是最优区间
    """
    score = 0.0
    
    # 涨幅位置评分（资金最爱的区间：2%~4%）
    if 2.0 <= change <= 4.0:
        score += 40
    elif 1.5 <= change < 2.0:
        score += 30
    elif 4.0 < change <= 5.0:
        score += 35
    elif 5.0 < change <= 6.0:
        score += 25
    elif 0.5 <= change < 1.5:
        score += 15
    else:
        score += 5
    
    # 成交额规模（资金体量）
    if amount_yi >= 80:
        score += 30
    elif amount_yi >= 50:
        score += 25
    elif amount_yi >= 30:
        score += 20
    elif amount_yi >= 15:
        score += 15
    elif amount_yi >= 8:
        score += 10
    elif amount_yi >= 3:
        score += 5
    else:
        score += 1
    
    # 涨幅+成交额双重确认（资金真买入了）
    if 2.0 <= change <= 5.0 and amount_yi >= 15:
        score += 20
    
    return max(15.0, min(90.0, score))

# ==================== 综合评分 ====================
def calculate_score(stock, ma_data=None):
    """
    综合评分（完全确定，无随机）
    资金流向 40% + 技术面 35% + 成交额规模 25%
    """
    change = stock['change']
    amount_yi = stock['amount_yi']
    
    # 1. 资金流向评分 (40%)
    flow_score = estimate_capital_flow(stock['code'], change, amount_yi)
    
    # 2. 技术面评分 (35%)
    tech_score = 0.0
    if ma_data:
        if ma_data.get('ma_bullish'):
            tech_score += 25
        if ma_data.get('price_vs_ma5', -99) > 0:
            tech_score += 10
        if ma_data.get('vol_ratio', 1.0) >= 2.0:
            tech_score += 12
        elif ma_data.get('vol_ratio', 1.0) >= 1.5:
            tech_score += 7
        closes = ma_data.get('closes', [])
        if len(closes) >= 3:
            trend = (closes[-1] - closes[-3]) / closes[-3] * 100
            if trend > 3:
                tech_score += 8
            elif trend < -3:
                tech_score -= 5
    else:
        tech_score += 15  # 无数据给基础分
    
    # 3. 成交额规模 (25%)
    amount_score = 0.0
    if amount_yi >= 80:
        amount_score = 30
    elif amount_yi >= 50:
        amount_score = 25
    elif amount_yi >= 30:
        amount_score = 20
    elif amount_yi >= 15:
        amount_score = 15
    elif amount_yi >= 8:
        amount_score = 10
    elif amount_yi >= 3:
        amount_score = 5
    else:
        amount_score = 1
    
    total = flow_score * 0.4 + tech_score * 0.35 + amount_score * 0.25
    return max(20.0, min(92.0, total))

# ==================== 选股 ====================
def screen_stocks():
    log = get_log()
    log('📡 获取创业板+科创板行情...')
    
    codes = get_gy_ck_codes()
    quotes = get_batch_quotes(codes)
    log(f'📊 获取到 {len(quotes)} 只')

    # 过滤
    candidates = []
    for code, q in quotes.items():
        name = q['name']
        if 'ST' in name or '退' in name or '*' in name:
            continue
        if q['change'] >= 9.8 or q['change'] <= -9.8:
            continue
        if not (1.0 <= q['change'] <= 6.0):
            continue
        if q['amount_yi'] < 3.0:
            continue
        if q['price'] < 2.0 or q['price'] > 500.0:
            continue
        
        stock = {
            'code': code, 'name': q['name'],
            'price': q['price'], 'open': q['open'],
            'high': q['high'], 'low': q['low'],
            'change': q['change'], 'amount_yi': q['amount_yi'],
            'prev_close': q['prev_close']
        }
        candidates.append(stock)

    log(f'🎯 候选 {len(candidates)} 只')

    if not candidates:
        return []

    # 获取MA数据（前30只）
    log('📈 获取MA数据...')
    for i, stock in enumerate(candidates[:80]):
        ma = get_ma_data(stock['code'])
        stock['ma_data'] = ma
        stock['vol_ratio'] = ma['vol_ratio'] if ma else 1.0
        if i > 0 and i % 10 == 0:
            log(f'  MA进度 {i}/80')
        time.sleep(0.06)

    # 获取RSI数据（前30候选，过滤超买）
    log('RSI getting...')
    for i, stock in enumerate(candidates[:30]):
        rsi = get_rsi(stock['code'])
        stock['rsi'] = rsi
        if i > 0 and i % 10 == 0:
            log(f'  RSI {i}/30')
        time.sleep(0.06)

    # RSI过滤：排除RSI>=65
    before_rsi = len(candidates)
    for stock in candidates:
        rsi = stock.get('rsi')
        if rsi is not None and rsi >= 65:
            stock['filtered'] = True
    candidates = [c for c in candidates if not c.get('filtered')]
    log(f'  RSI filter: {before_rsi} -> {len(candidates)} (RSI>=65 removed)')

    # 评分
    for stock in candidates:
        stock['total_score'] = calculate_score(stock, stock.get('ma_data'))

    candidates.sort(key=lambda x: x['total_score'], reverse=True)

    # 排除已持仓
    data = load_positions()
    held = [p['code'] for p in data['positions']]
    candidates = [c for c in candidates if c['code'] not in held]

    log(f'🏆 评分前10:')
    for i, c in enumerate(candidates[:10]):
        ma_str = f'MA5={c["ma_data"]["ma5"]:.1f}' if c.get('ma_data') else 'MA5=N/A'
        vol_str = f'量比{c.get("vol_ratio",1):.1f}'
        log(f'  {i+1}. {c["name"]}({c["code"]}) 评分{c["total_score"]:.0f} | '
            f'涨{c["change"]:.2f}% | 成交{c["amount_yi"]:.1f}亿 | {ma_str} | {vol_str}')

    return candidates[:20]

# ==================== 仓位分配 ====================
def allocate(candidates, budget):
    if not candidates:
        return []

    candidates.sort(key=lambda x: x['total_score'], reverse=True)
    
    remaining = budget
    allocations = []
    
    for stock in candidates:
        if len(allocations) >= MAX_POSITIONS:
            break
        price = stock['price']
        max_allowed = min(MAX_POSITION, remaining)
        shares = int(max_allowed / price / 100) * 100
        if shares < 100 or shares * price < 2000:  # 至少投2000元
            continue
        actual_cost = shares * price
        allocations.append({
            'stock': stock, 'shares': shares, 'actual_cost': actual_cost
        })
        remaining -= actual_cost
    
    return allocations

# ==================== 持仓管理 ====================
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {
        'positions': [], 'cash': CAPITAL, 'history': [],
        'start_date': datetime.now().strftime('%Y-%m-%d'),
        'total_trades': 0, 'total_pnl': 0
    }

def save_positions(data):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def buy(data, stock, shares):
    cost = shares * stock['price']
    if data['cash'] < cost:
        return None
    data['cash'] -= cost
    pos = {
        'code': stock['code'], 'name': stock['name'],
        'buy_price': stock['price'], 'buy_high': stock['high'],
        'shares': shares, 'cost': cost,
        'buy_date': datetime.now().strftime('%Y-%m-%d'),
        'buy_time': datetime.now().strftime('%H:%M:%S'),
        'score': stock['total_score'],
        'reason': f'评分{stock["total_score"]:.0f}|涨{stock["change"]:.1f}%|成交{stock["amount_yi"]:.1f}亿',
        'status': 'open', 'peak_price': stock['price'], 'hold_days': 0
    }
    data['positions'].append(pos)
    data['total_trades'] += 1
    return pos

def sell(data, idx, reason):
    pos = data['positions'][idx]
    quotes = get_batch_quotes([pos['code']])
    if pos['code'] not in quotes:
        return None
    q = quotes[pos['code']]
    sp = q['price']
    sv = sp * pos['shares']
    pnl = sv - pos['cost']
    pnl_pct = pnl / pos['cost'] * 100
    data['cash'] += sv
    data['total_pnl'] += pnl
    trade = {**pos, 'sell_price': sp, 'sell_value': sv,
        'sell_date': datetime.now().strftime('%Y-%m-%d'),
        'sell_time': datetime.now().strftime('%H:%M:%S'),
        'sell_reason': reason, 'pnl': pnl, 'pnl_pct': pnl_pct, 'status': 'closed'}
    data['history'].append(trade)
    data['positions'].pop(idx)
    return trade

def check_sell(pos, quote):
    current = quote['price']
    pnl_pct = (current - pos['buy_price']) / pos['buy_price'] * 100
    if current > pos.get('peak_price', pos['buy_price']):
        pos['peak_price'] = current
    
    # 止损
    if current <= pos['buy_price'] * STOP_LOSS_PCT:
        return '止损', pnl_pct
    # 固定止盈 6%
    if current >= pos['buy_price'] * TAKE_PROFIT_PCT:
        return '止盈', pnl_pct
    # 移动止盈（涨5%后用成本价98%保护）
    if pos['peak_price'] >= pos['buy_price'] * 1.05:
        if current <= pos['buy_price'] * TRAILING_STOP_PCT:
            return '移动止盈', pnl_pct
    # 持仓超期
    buy_date = datetime.strptime(pos['buy_date'], '%Y-%m-%d')
    hold_days = (datetime.now() - buy_date).days + (datetime.now().hour - 14) / 24
    pos['hold_days'] = hold_days
    if hold_days >= MAX_HOLD_DAYS:
        return '到期卖', pnl_pct
    if hold_days >= 1 and pnl_pct < -2.0:
        return '尾盘止损', pnl_pct
    return None, pnl_pct

# ==================== 大盘检查 ====================
def check_market():
    log = get_log()
    quotes = get_batch_quotes(['sh000300', 'sh000001', 'sz399006'])
    result = {}
    names = {'sh000300': '沪深300', 'sh000001': '上证', 'sz399006': '创业板'}
    for code, name in names.items():
        if code in quotes:
            result[name] = quotes[code]['change']
            log(f'  {name}: {quotes[code]["change"]:+.2f}%')
    return result

# ==================== 主流程 ====================
def main():
    log = get_log()
    log(f'')
    log(f'{"="*50}')
    log(f'📈 v4.1 专注版 - {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    log(f'{"="*50}')

    # 日内重复运行保护
    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(RUN_LOCK):
        with open(RUN_LOCK) as f:
            last_run = f.read().strip()
        if last_run == today_str:
            log(f'⏭️ 今日已买入，跳过选股（{last_run}）')
            skip_buy = True
        else:
            with open(RUN_LOCK, 'w') as f:
                f.write(today_str)
            skip_buy = False
    else:
        with open(RUN_LOCK, 'w') as f:
            f.write(today_str)
        skip_buy = False

    data = load_positions()
    log(f'')
    log(f'📊 账户: 现金={data["cash"]:,.0f} | 持仓={len(data["positions"])}只')

    # 大盘检查
    log(f'')
    log(f'🌍 大盘状态:')
    market = check_market()

    # 持仓检查（先卖后买）
    log(f'')
    log(f'🔍 持仓检查...')
    sold = []
    if data['positions']:
        quotes = get_batch_quotes([p['code'] for p in data['positions']])
        to_sell = []
        for idx, pos in enumerate(data['positions']):
            if pos['code'] not in quotes:
                continue
            reason, pnl = check_sell(pos, quotes[pos['code']])
            if reason:
                to_sell.append((idx, reason))
        for idx, reason in reversed(to_sell):
            t = sell(data, idx, reason)
            if t:
                sold.append(t)
                e = '📉' if t['pnl'] <= 0 else '📈'
                log(f'  {e} {t["name"]} @ {t["sell_price"]:.2f} ({reason}) {t["pnl_pct"]:+.2f}%')

    if not sold:
        log(f'  无需卖出')

    # 选股买入
    if skip_buy:
        log(f'⏭️ 跳过新买入')
    else:
        log(f'')
        log(f'🎯 选股中...')
        candidates = screen_stocks()

        if candidates:
            # 大盘过滤
            hs300 = market.get('沪深300', 0)
            if hs300 < INDEX_DROP_THRESHOLD:
                log(f'⚠️ 沪深300 {hs300:.1f}% 跌幅超阈值，跳过买入')
            else:
                log(f'')
                log(f'💰 仓位分配（预算 {data["cash"]:,.0f}）...')
                allocs = allocate(candidates, data['cash'])
                total_cost = sum(a['actual_cost'] for a in allocs)
                log(f'  买入 {len(allocs)} 只，总 {total_cost:,.0f}')

                for a in allocs:
                    s = a['stock']
                    log(f'  {s["name"]} {a["shares"]}股@{s["price"]:.2f}={a["actual_cost"]:,.0f} 评分{s["total_score"]:.0f}')

                log(f'')
                log(f'📗 模拟买入...')
                bought = []
                for a in allocs:
                    p = buy(data, a['stock'], a['shares'])
                    if p:
                        bought.append(p)
                        log(f'  ✅ {p["name"]} {p["shares"]}股@{p["buy_price"]:.2f} 成本{p["cost"]:,.0f}')

                if bought:
                    msg = f'【v4.1买入】{datetime.now().strftime("%H:%M")}\n'
                    for p in bought:
                        msg += f'{p["name"]}({p["code"]})\n{p["buy_price"]:.2f}x{p["shares"]}={p["cost"]:,.0f}\n{p["reason"]}\n\n'
                    send_qq_message('v4.1量化-买入', msg)
        else:
            log(f'  无候选股票')

    save_positions(data)

    # 汇总
    total_val = sum(p['shares'] * get_batch_quotes([p['code']]).get(p['code'], {}).get('price', p['buy_price'])
                    for p in data['positions'])
    total_assets = data['cash'] + total_val

    log(f'')
    log(f'{"="*50}')
    log(f'📊 汇总: 总资产={total_assets:,.0f} | 收益={(total_assets-CAPITAL)/CAPITAL*100:+.2f}%')
    log(f'  现金={data["cash"]:,.0f} | 持仓={len(data["positions"])}只')

    wins = len([t for t in data['history'] if t['pnl'] > 0])
    losses = len([t for t in data['history'] if t['pnl'] <= 0])
    if data['history']:
        log(f'  已平={len(data["history"])}笔 | 胜率={wins/(wins+losses)*100:.0f}% | 总盈亏={data["total_pnl"]:+,.0f}')

    # 保存报告
    report = {
        'date': today_str, 'time': datetime.now().strftime('%H:%M:%S'),
        'cash': data['cash'], 'total_assets': total_assets,
        'return_pct': (total_assets-CAPITAL)/CAPITAL*100,
        'positions': [{'code':p['code'],'name':p['name'],'shares':p['shares'],
                       'buy_price':p['buy_price'],'cost':p['cost']} for p in data['positions']],
        'summary': {
            'total_trades': data['total_trades'], 'total_pnl': data['total_pnl'],
            'wins': wins, 'losses': losses,
            'win_rate': wins/(wins+losses)*100 if (wins+losses) > 0 else 0
        }
    }
    with open(DAILY_REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    if sold:
        msg = f'【v4.1卖出】{datetime.now().strftime("%H:%M")}\n'
        for t in sold:
            e = '🔴' if t['pnl'] <= 0 else '🟢'
            msg += f'{e}{t["name"]} {t["sell_reason"]}\n{t["buy_price"]:.2f}→{t["sell_price"]:.2f} {t["pnl_pct"]:+.2f}%({t["pnl"]:+,.0f}元)\n\n'
        send_qq_message('v4.1量化-卖出', msg)

    return report

if __name__ == '__main__':
    main()
