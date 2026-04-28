#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日模拟交易 v5.0 日内版 (Today Buy, Tomorrow Sell)
策略：T+1日内交易，今天14:40买入，明天14:30卖出
目标：跑一个月看盈亏率

核心改进（相比v4.2）：
1. 全A股股票池（不再限创业板+科创板）
2. 历史动量因子（20日涨幅排名）
3. 板块分散（单行业≤25%仓位）
4. 大盘情绪仓位调节（强/中/弱三档）
5. T+1强制卖出（次日14:30全部清仓）
6. 日内止损3%（防黑天鹅）

数据源：
- 东方财富：全A股列表 + 行业分类 + 实时行情
- 新浪财经：K线数据（MA/RSI/动量计算）

Crontab (see README for setup):\n  14:30 sell, 14:40 buy, weekdays only
"""

import sys, json, time, os, requests, argparse, subprocess
from datetime import datetime, timedelta
from collections import defaultdict

# ==================== 配置 ====================
CAPITAL = 100000               # 初始本金
MAX_POSITIONS = 5              # 最大持仓数
MIN_POSITION_COST = 3000       # 单笔最小金额
MAX_POSITION_COST = 30000      # 单笔最大金额（单股≤30%本金）
MAX_SECTOR_RATIO = 0.25        # 单行业最大仓位占比
STOP_LOSS_PCT = 0.97           # 日内止损3%（次日检查）

# 大盘情绪仓位调节
SENTIMENT_STRONG_RATIO = 1.0   # 沪深300 > +0.5% → 满仓
SENTIMENT_NEUTRAL_RATIO = 0.7  # 沪深300 -0.5%~+0.5% → 70%仓
SENTIMENT_WEAK_RATIO = 0.3     # 沪深300 -1.5%~-0.5% → 30%仓
SENTIMENT_SKIP_THRESHOLD = -1.5  # 沪深300 < -1.5% → 不买

DATA_DIR = '/opt/stock-monitor/data'
POSITIONS_FILE = f'{DATA_DIR}/sim_positions_v5.json'
DAILY_REPORT_FILE = f'{DATA_DIR}/daily_report_v5.json'
RUN_LOCK_BUY = f'{DATA_DIR}/.run_lock_v5_buy'
RUN_LOCK_SELL = f'{DATA_DIR}/.run_lock_v5_sell'

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(f'{DATA_DIR}/logs', exist_ok=True)


# ==================== 日志 ====================
def get_log():
    log_file = f'{DATA_DIR}/logs/trade_v5_{datetime.now().strftime("%Y%m%d")}.log'
    def _log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        print(line)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    return _log


# ==================== QQ推送 ====================
def send_qq_message(title, content):
    """通过服务器 openclaw 发送 QQ 消息"""
    try:
        msg = f'【{title}】{content}'
        subprocess.run(
            ['openclaw', 'message', 'send', '--channel', 'qqbot',
             '--target', 'qqbot:c2c:5DB38D4F29AFE7612B92CFFFF1BF039B',
             '-m', msg],
            capture_output=True, text=True, timeout=15
        )
    except Exception as e:
        print(f'QQ推送失败: {e}')
        try:
            requests.post('https://open.feishu.cn/open-apis/bot/v2/hook/86f530d2-5817-42f7-9b8b-dc5204efb638',
                json={"msg_type": "text", "content": {"text": f"【{title}】{content}"}}, timeout=10)
        except:
            pass


# ==================== 东方财富：全A股列表 ====================
def to_sina_code(code):
    """东方财富代码转新浪代码格式"""
    code = str(code)
    if code.startswith('688'):   # 科创板
        return f'sh{code}'
    elif code.startswith('6'):   # 沪市主板
        return f'sh{code}'
    elif code.startswith('0') or code.startswith('3'):  # 深市主板/创业板
        return f'sz{code}'
    elif code.startswith('8') or code.startswith('4'):  # 北交所
        return f'bj{code}'
    return None


def get_all_astocks():
    """从东方财富获取全A股列表（含行业分类+实时行情），分页获取"""
    base_url = ('https://push2.eastmoney.com/api/qt/clist/get?'
                'po=1&np=1&fltt=2&invt=2&fid=f3&'
                'fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+f:!2&'
                'fields=f2,f3,f4,f6,f7,f8,f12,f14,f15,f16,f17,f18,f100')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://quote.eastmoney.com/center/gridlist.html'
    }

    try:
        all_items = []
        for pn in range(1, 150):  # 最多150页×100条=15000只
            url = f'{base_url}&pn={pn}&pz=100'
            try:
                r = requests.get(url, headers=headers, timeout=15)
                data = r.json()
            except Exception:
                continue
            if not data or not data.get('data'):
                continue
            items = data['data'].get('diff', [])
            if not items:
                break
            all_items.extend(items)
            time.sleep(0.03)
            # 提前退出：已获取足够
            if len(all_items) >= data['data'].get('total', 99999):
                break

        stocks = []
        for item in all_items:
            code = str(item.get('f12', ''))
            name = item.get('f14', '')
            price = item.get('f2', 0)
            change_pct = item.get('f3', 0)
            amount = item.get('f6', 0)
            high = item.get('f15', 0)
            low = item.get('f16', 0)
            open_p = item.get('f17', 0)
            prev_close = item.get('f18', 0)
            industry = item.get('f100', '未知')

            # 过滤无效
            if not code or not name or not isinstance(price, (int, float)) or price <= 0:
                continue
            if 'ST' in name or '退' in name or '*' in name or 'N' == name[0]:
                continue
            if not isinstance(change_pct, (int, float)):
                continue

            sina_code = to_sina_code(code)
            if not sina_code:
                continue
            # 跳过北交所（流动性差）
            if sina_code.startswith('bj'):
                continue

            amount_yi = amount / 100000000 if isinstance(amount, (int, float)) and amount else 0

            stocks.append({
                'code': sina_code,
                'raw_code': code,
                'name': name,
                'price': float(price),
                'change': float(change_pct),
                'amount_yi': amount_yi,
                'high': float(high) if isinstance(high, (int, float)) else 0,
                'low': float(low) if isinstance(low, (int, float)) else 0,
                'open': float(open_p) if isinstance(open_p, (int, float)) else 0,
                'prev_close': float(prev_close) if isinstance(prev_close, (int, float)) else 0,
                'industry': industry if industry and industry != '-' else '其他'
            })
        return stocks
    except Exception as e:
        print(f'东方财富API失败: {e}')
    return []


# ==================== 新浪：K线数据 ====================
def get_kline_data(code, datalen=30):
    """获取K线数据（用于MA/RSI/动量计算）"""
    try:
        url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
               f'CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen={datalen}')
        r = requests.get(url,
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'},
            timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or len(data) < 10:
            return None
        return data
    except:
        return None


def calc_technicals(code):
    """一次性计算MA5/MA10/MA20/RSI/动量/量比"""
    kline = get_kline_data(code, 30)
    if not kline or len(kline) < 10:
        return None

    closes = [float(d['close']) for d in kline]
    volumes = [float(d['volume']) for d in kline]

    # MA
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma_bullish = ma10 is not None and ma5 > ma10

    # 量比（今日成交量 / 5日均量）
    today_vol = volumes[-1]
    avg_vol_5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else today_vol
    vol_ratio = today_vol / avg_vol_5 if avg_vol_5 > 0 else 1.0

    # RSI(14)
    period = 14
    rsi = 50.0
    if len(closes) > period:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses_list = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses_list) / period
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        else:
            rsi = 100.0

    # 20日动量（核心新因子）
    momentum_20d = 0
    if len(closes) >= 20:
        momentum_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
    elif len(closes) >= 10:
        momentum_20d = (closes[-1] - closes[-10]) / closes[-10] * 100 * 1.5  # 近似放大

    # 5日动量
    momentum_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0

    current = closes[-1]
    price_vs_ma5 = (current - ma5) / ma5 * 100

    return {
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20,
        'ma_bullish': ma_bullish,
        'price_vs_ma5': price_vs_ma5,
        'vol_ratio': vol_ratio,
        'rsi': round(rsi, 1),
        'momentum_20d': round(momentum_20d, 2),
        'momentum_5d': round(momentum_5d, 2),
        'closes': closes
    }


# ==================== 大盘情绪 ====================
def check_market_sentiment(log):
    """检查大盘情绪，返回 (行情dict, 情绪文字, 仓位比例)"""
    try:
        url = 'https://hq.sinajs.cn/list=sh000300,sh000001,sz399006'
        r = requests.get(url,
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'},
            timeout=10)

        result = {}
        name_map = {'sh000300': '沪深300', 'sh000001': '上证指数', 'sz399006': '创业板指'}
        for line in r.text.strip().split('\n'):
            if 'hq_str_' not in line or '=' not in line:
                continue
            try:
                code = line.split('hq_str_')[1].split('=')[0]
                data = line.split('"')[1].split(',')
                if code in name_map and len(data) >= 10:
                    current = float(data[3])
                    prev_close = float(data[2])
                    if prev_close > 0:
                        result[name_map[code]] = (current - prev_close) / prev_close * 100
            except:
                continue

        hs300 = result.get('沪深300', 0)

        if hs300 >= 0.5:
            sentiment = '强势🔥'
            ratio = SENTIMENT_STRONG_RATIO
        elif hs300 >= -0.5:
            sentiment = '中性⚖️'
            ratio = SENTIMENT_NEUTRAL_RATIO
        elif hs300 >= SENTIMENT_SKIP_THRESHOLD:
            sentiment = '弱势⚠️'
            ratio = SENTIMENT_WEAK_RATIO
        else:
            sentiment = '极弱🛑'
            ratio = 0

        for name, chg in result.items():
            log(f'  {name}: {chg:+.2f}%')
        log(f'  情绪: {sentiment} | 建议仓位: {ratio * 100:.0f}%')

        return result, sentiment, ratio
    except Exception as e:
        log(f'大盘情绪获取失败: {e}')
        return {}, '未知❓', SENTIMENT_NEUTRAL_RATIO


# ==================== 综合评分 v5（6因子模型） ====================
def calculate_score_v5(stock, tech_data=None):
    """
    v5综合评分（6因子模型）
    1. 资金流向评分 25%  — 涨幅位置
    2. 历史动量评分 25%  — 20日涨幅排名（新！）
    3. 技术面评分   20%  — MA/RSI
    4. 成交额规模   15%  — 资金体量
    5. 量比评分     10%  — 放量确认
    6. 板块动量      5%  — 涨幅×成交额
    """
    change = stock['change']
    amount_yi = stock['amount_yi']

    # ── 1. 资金流向 (25%) ──
    if 2.0 <= change <= 4.0:
        flow_score = 40     # 最优区间
    elif 1.5 <= change < 2.0:
        flow_score = 30
    elif 4.0 < change <= 5.0:
        flow_score = 35
    elif 0.5 <= change < 1.5:
        flow_score = 20
    elif 5.0 < change <= 6.0:
        flow_score = 25
    else:
        flow_score = 10

    # ── 2. 历史动量 (25%) ──
    momentum_score = 20  # 默认中间值
    if tech_data:
        m20 = tech_data.get('momentum_20d', 0)
        m5 = tech_data.get('momentum_5d', 0)
        if 5 <= m20 <= 20:
            momentum_score = 40      # 有趋势但未过热
        elif 0 <= m20 < 5:
            momentum_score = 25
        elif 20 < m20 <= 35:
            momentum_score = 30
        elif m20 > 35:
            momentum_score = 15      # 过热风险
        else:
            momentum_score = 10      # 下跌趋势
        # 5日动量加分
        if 3 <= m5 <= 10:
            momentum_score = min(50, momentum_score + 10)
        elif 0 <= m5 < 3:
            momentum_score = min(50, momentum_score + 5)

    # ── 3. 技术面 (20%) ──
    tech_score = 15
    if tech_data:
        tech_score = 0
        if tech_data.get('ma_bullish'):
            tech_score += 20
        if tech_data.get('price_vs_ma5', -99) > 0:
            tech_score += 10
        rsi = tech_data.get('rsi', 50)
        if rsi < 40:
            tech_score += 8     # 超卖反弹
        elif rsi < 55:
            tech_score += 12    # 健康区间
        elif rsi < 65:
            tech_score += 5
        else:
            tech_score += 0     # 偏高
        if tech_data.get('ma20') and stock['price'] > tech_data['ma20']:
            tech_score += 5     # MA20支撑

    # ── 4. 成交额规模 (15%) ──
    if amount_yi >= 50:
        amount_score = 30
    elif amount_yi >= 20:
        amount_score = 25
    elif amount_yi >= 10:
        amount_score = 20
    elif amount_yi >= 5:
        amount_score = 15
    elif amount_yi >= 2:
        amount_score = 8
    else:
        amount_score = 3

    # ── 5. 量比 (10%) ──
    vol_score = 5
    if tech_data:
        vr = tech_data.get('vol_ratio', 1.0)
        if vr >= 3.0:
            vol_score = 20
        elif vr >= 2.0:
            vol_score = 15
        elif vr >= 1.5:
            vol_score = 10
        elif vr >= 1.0:
            vol_score = 5

    # ── 6. 板块动量 (5%) ──
    if 2 <= change <= 4 and amount_yi >= 10:
        sector_score = 15
    elif 1 <= change <= 5 and amount_yi >= 5:
        sector_score = 10
    else:
        sector_score = 5

    total = (flow_score * 0.25 +
             momentum_score * 0.25 +
             tech_score * 0.20 +
             amount_score * 0.15 +
             vol_score * 0.10 +
             sector_score * 0.05)

    return max(20.0, min(95.0, round(total, 1)))


# ==================== 选股 ====================
def screen_stocks(log):
    """v5选股：全A股筛选 + 6因子评分"""
    log('📡 从东方财富获取全A股行情...')
    all_stocks = get_all_astocks()
    log(f'📊 获取到 {len(all_stocks)} 只A股')

    # 初筛条件
    candidates = []
    for s in all_stocks:
        # 涨幅 0.5%~6%（尾盘有上涨动能）
        if not (0.5 <= s['change'] <= 6.0):
            continue
        # 成交额 ≥ 3亿
        if s['amount_yi'] < 3.0:
            continue
        # 价格 3~500元
        if s['price'] < 3.0 or s['price'] > 500.0:
            continue
        # 涨停/跌停不追
        if abs(s['change']) >= 9.8:
            continue
        candidates.append(s)

    log(f'🎯 初筛候选 {len(candidates)} 只')

    if not candidates:
        return []

    # 按涨幅+成交额初步排序，取前60只获取详细技术数据
    candidates.sort(
        key=lambda x: x['change'] * 0.4 + min(x['amount_yi'], 50) * 0.6,
        reverse=True
    )
    top_candidates = candidates[:60]

    # 获取技术数据（MA/RSI/动量）
    log(f'📈 获取技术数据（前{len(top_candidates)}只）...')
    for i, stock in enumerate(top_candidates):
        tech = calc_technicals(stock['code'])
        stock['tech_data'] = tech
        if (i + 1) % 15 == 0:
            log(f'  技术数据进度 {i + 1}/{len(top_candidates)}')
        time.sleep(0.05)

    # RSI过滤：排除超买 RSI ≥ 70
    before = len(top_candidates)
    filtered = []
    for s in top_candidates:
        rsi = s.get('tech_data', {}).get('rsi') if s.get('tech_data') else None
        if rsi is not None and rsi >= 70:
            continue
        filtered.append(s)
    log(f'  RSI过滤: {before} → {len(filtered)} (RSI≥70 已排除)')

    # 评分
    for stock in filtered:
        stock['total_score'] = calculate_score_v5(stock, stock.get('tech_data'))

    # 排除已持仓
    data = load_positions()
    held = [p['code'] for p in data['positions']]
    filtered = [c for c in filtered if c['code'] not in held]

    # 按评分排序
    filtered.sort(key=lambda x: x['total_score'], reverse=True)

    log(f'🏆 评分前15:')
    for i, c in enumerate(filtered[:15]):
        tech = c.get('tech_data') or {}
        m20 = tech.get('momentum_20d', '-')
        rsi = tech.get('rsi', '-')
        log(f'  {i + 1}. {c["name"]}({c["code"]}) 评分{c["total_score"]:.0f} | '
            f'涨{c["change"]:.2f}% | {c["amount_yi"]:.1f}亿 | '
            f'动量20d={m20}% | RSI={rsi} | [{c.get("industry", "?")}]')

    return filtered[:20]


# ==================== 板块分散仓位分配 ====================
def allocate_with_sector_limit(candidates, budget, log):
    """带行业分散的仓位分配"""
    if not candidates:
        return []

    candidates.sort(key=lambda x: x['total_score'], reverse=True)

    remaining = budget
    allocations = []
    sector_cost = defaultdict(float)

    for stock in candidates:
        if len(allocations) >= MAX_POSITIONS:
            break

        price = stock['price']
        industry = stock.get('industry', '其他')

        # 行业仓位上限
        max_sector = budget * MAX_SECTOR_RATIO
        if sector_cost[industry] >= max_sector:
            log(f'  ⏭️ {stock["name"]}[{industry}] 行业仓位已满({sector_cost[industry]:,.0f}/{max_sector:,.0f})，跳过')
            continue

        max_for_sector = max_sector - sector_cost[industry]
        max_allowed = min(MAX_POSITION_COST, remaining, max_for_sector)

        shares = int(max_allowed / price / 100) * 100
        if shares < 100 or shares * price < MIN_POSITION_COST:
            continue

        actual_cost = shares * price
        allocations.append({
            'stock': stock, 'shares': shares, 'actual_cost': actual_cost
        })
        remaining -= actual_cost
        sector_cost[industry] += actual_cost

    # 打印行业分布
    if allocations:
        log(f'  📊 行业分布:')
        for ind, cost in sorted(sector_cost.items(), key=lambda x: -x[1]):
            if cost > 0:
                log(f'    {ind}: {cost:,.0f} ({cost / budget * 100:.1f}%)')

    return allocations


# ==================== 新浪实时报价 ====================
def get_batch_quotes(codes):
    """批量获取新浪实时报价"""
    results = {}
    batch_size = 80
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
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
                    if len(data) < 10 or not data[0]:
                        continue
                    current = float(data[3])
                    prev_close = float(data[2])
                    results[code] = {
                        'name': data[0],
                        'price': current,
                        'prev_close': prev_close,
                        'change': (current - prev_close) / prev_close * 100 if prev_close > 0 else 0
                    }
                except:
                    continue
        except:
            continue
    return results


# ==================== 持仓管理 ====================
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {
        'positions': [], 'cash': CAPITAL, 'history': [],
        'start_date': datetime.now().strftime('%Y-%m-%d'),
        'total_trades': 0, 'total_pnl': 0,
        'version': 'v5.0'
    }


def save_positions(data):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def buy_stock(data, stock, shares, log):
    """模拟买入"""
    cost = shares * stock['price']
    if data['cash'] < cost:
        log(f'  ❌ 现金不足: {stock["name"]} 需要{cost:,.0f} 现金{data["cash"]:,.0f}')
        return None
    data['cash'] -= cost
    pos = {
        'code': stock['code'], 'name': stock['name'],
        'buy_price': stock['price'], 'shares': shares,
        'cost': cost, 'industry': stock.get('industry', '其他'),
        'buy_date': datetime.now().strftime('%Y-%m-%d'),
        'buy_time': datetime.now().strftime('%H:%M:%S'),
        'score': stock['total_score'],
        'reason': (f'评分{stock["total_score"]:.0f}|涨{stock["change"]:.1f}%|'
                   f'{stock["amount_yi"]:.1f}亿|{stock.get("industry", "?")}'),
        'status': 'open', 'peak_price': stock['price']
    }
    data['positions'].append(pos)
    data['total_trades'] += 1
    return pos


def sell_stock(data, idx, reason, log):
    """模拟卖出"""
    pos = data['positions'][idx]
    quotes = get_batch_quotes([pos['code']])
    if pos['code'] not in quotes:
        log(f'  ⚠️ {pos["name"]} 无法获取行情，跳过卖出')
        return None

    q = quotes[pos['code']]
    sp = q['price']
    sv = sp * pos['shares']
    pnl = sv - pos['cost']
    pnl_pct = pnl / pos['cost'] * 100

    data['cash'] += sv
    data['total_pnl'] += pnl

    trade = {
        **pos,
        'sell_price': sp, 'sell_value': sv,
        'sell_date': datetime.now().strftime('%Y-%m-%d'),
        'sell_time': datetime.now().strftime('%H:%M:%S'),
        'sell_reason': reason, 'pnl': pnl, 'pnl_pct': pnl_pct,
        'status': 'closed'
    }
    data['history'].append(trade)
    data['positions'].pop(idx)
    return trade


# ==================== 卖出流程（T+1） ====================
def run_sell(log):
    """T+1卖出：次日14:30强制清仓"""
    log(f'')
    log(f'📉 ===== T+1卖出模式 =====')

    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(RUN_LOCK_SELL):
        with open(RUN_LOCK_SELL) as f:
            last_run = f.read().strip()
        if last_run == today_str:
            log(f'⏭️ 今日已执行过卖出，跳过')
            return []

    data = load_positions()

    if not data['positions']:
        log(f'📭 无持仓需要卖出')
        with open(RUN_LOCK_SELL, 'w') as f:
            f.write(today_str)
        return []

    log(f'📊 持仓 {len(data["positions"])} 只，开始卖出...')

    sold = []
    quotes = get_batch_quotes([p['code'] for p in data['positions']])

    # 判断每只持仓的卖出原因
    sell_list = []
    for idx, pos in enumerate(data['positions']):
        if pos['code'] not in quotes:
            continue
        q = quotes[pos['code']]
        current = q['price']
        pnl_pct = (current - pos['buy_price']) / pos['buy_price'] * 100

        if current <= pos['buy_price'] * STOP_LOSS_PCT:
            reason = '日内止损(≤-3%)'
        elif pnl_pct >= 8:
            reason = 'T+1盈利止盈'
        else:
            reason = 'T+1到期'

        sell_list.append((idx, reason, pnl_pct, current))

    # 从后往前卖（避免索引偏移）
    for idx, reason, pnl_pct, current in reversed(sell_list):
        t = sell_stock(data, idx, reason, log)
        if t:
            sold.append(t)
            e = '🔴' if t['pnl'] <= 0 else '🟢'
            log(f'  {e} {t["name"]} @ {t["sell_price"]:.2f} ({reason}) '
                f'{t["pnl_pct"]:+.2f}% ({t["pnl"]:+,.0f}元)')

    if not sold:
        log(f'  无可卖出持仓')

    save_positions(data)

    # 写入卖出锁
    with open(RUN_LOCK_SELL, 'w') as f:
        f.write(today_str)

    # QQ推送
    if sold:
        msg = f'【v5.0卖出】{datetime.now().strftime("%m-%d %H:%M")}\n\n'
        total_pnl = sum(t['pnl'] for t in sold)
        for t in sold:
            e = '🔴' if t['pnl'] <= 0 else '🟢'
            msg += (f'{e}{t["name"]} {t["sell_reason"]}\n'
                    f'{t["buy_price"]:.2f}→{t["sell_price"]:.2f} '
                    f'{t["pnl_pct"]:+.2f}%({t["pnl"]:+,.0f})\n\n')
        msg += f'📌 本次总盈亏: {total_pnl:+,.0f}元'
        send_qq_message('v5.0量化-卖出', msg)

    return sold


# ==================== 买入流程 ====================
def run_buy(log):
    """T+1买入：14:40选股买入"""
    log(f'')
    log(f'📈 ===== T+1买入模式 =====')

    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(RUN_LOCK_BUY):
        with open(RUN_LOCK_BUY) as f:
            last_run = f.read().strip()
        if last_run == today_str:
            log(f'⏭️ 今日已买入，跳过')
            return []

    data = load_positions()
    log(f'📊 账户: 现金={data["cash"]:,.0f} | 持仓={len(data["positions"])}只')

    # 大盘情绪
    log(f'')
    log(f'🌍 大盘情绪:')
    market, sentiment, position_ratio = check_market_sentiment(log)

    if position_ratio <= 0:
        log(f'⚠️ 大盘极弱，今日不买入')
        with open(RUN_LOCK_BUY, 'w') as f:
            f.write(today_str)
        save_positions(data)
        return []

    # 选股
    candidates = screen_stocks(log)
    if not candidates:
        log(f'🎯 无候选股票')
        with open(RUN_LOCK_BUY, 'w') as f:
            f.write(today_str)
        save_positions(data)
        return []

    # 仓位分配（大盘情绪调节）
    available_cash = data['cash'] * position_ratio
    log(f'')
    log(f'💰 可用资金: {data["cash"]:,.0f} × {position_ratio:.0%} = {available_cash:,.0f}')

    allocs = allocate_with_sector_limit(candidates, available_cash, log)
    if not allocs:
        log(f'❌ 无符合条件的目标')
        with open(RUN_LOCK_BUY, 'w') as f:
            f.write(today_str)
        save_positions(data)
        return []

    total_cost = sum(a['actual_cost'] for a in allocs)
    log(f'  计划买入 {len(allocs)} 只，总 {total_cost:,.0f}')

    for a in allocs:
        s = a['stock']
        log(f'  📌 {s["name"]} {a["shares"]}股@{s["price"]:.2f}={a["actual_cost"]:,.0f} '
            f'评分{s["total_score"]:.0f} [{s.get("industry", "?")}]')

    # 执行买入
    log(f'')
    log(f'📗 执行买入...')
    bought = []
    for a in allocs:
        p = buy_stock(data, a['stock'], a['shares'], log)
        if p:
            bought.append(p)
            log(f'  ✅ {p["name"]} {p["shares"]}股@{p["buy_price"]:.2f} 成本{p["cost"]:,.0f}')

    save_positions(data)

    # 写入买入锁
    with open(RUN_LOCK_BUY, 'w') as f:
        f.write(today_str)

    # QQ推送
    if bought:
        msg = f'【v5.0买入】{datetime.now().strftime("%m-%d %H:%M")}\n'
        msg += f'大盘: {sentiment} | 仓位: {position_ratio * 100:.0f}%\n\n'
        for p in bought:
            msg += (f'📗{p["name"]}({p["code"]})\n'
                    f'{p["buy_price"]:.2f}×{p["shares"]}={p["cost"]:,.0f}\n'
                    f'{p["reason"]}\n\n')
        send_qq_message('v5.0量化-买入', msg)

    return bought


# ==================== 统计报告 ====================
def generate_report(log, data):
    """生成统计报告"""
    total_val = 0
    if data['positions']:
        quotes = get_batch_quotes([p['code'] for p in data['positions']])
        for p in data['positions']:
            price = quotes.get(p['code'], {}).get('price', p['buy_price'])
            total_val += p['shares'] * price

    total_assets = data['cash'] + total_val
    return_pct = (total_assets - CAPITAL) / CAPITAL * 100

    wins = len([t for t in data['history'] if t['pnl'] > 0])
    losses = len([t for t in data['history'] if t['pnl'] <= 0])
    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0

    # 本月统计
    this_month = datetime.now().strftime('%Y-%m')
    month_trades = [t for t in data['history'] if t.get('sell_date', '').startswith(this_month)]
    month_pnl = sum(t['pnl'] for t in month_trades)
    month_wins = len([t for t in month_trades if t['pnl'] > 0])
    month_total = len(month_trades)
    month_win_rate = month_wins / month_total * 100 if month_total > 0 else 0
    avg_pnl = month_pnl / month_total if month_total > 0 else 0

    log(f'')
    log(f'{"=" * 55}')
    log(f'📊 v5.0 日内交易统计')
    log(f'  总资产: {total_assets:,.0f} | 收益率: {return_pct:+.2f}%')
    log(f'  现金: {data["cash"]:,.0f} | 持仓: {len(data["positions"])}只')
    log(f'  累计: {total}笔 | 胜率: {win_rate:.0f}% | 总盈亏: {data["total_pnl"]:+,.0f}')
    log(f'  本月: {month_total}笔 | 胜率: {month_win_rate:.0f}% | 盈亏: {month_pnl:+,.0f}')
    log(f'  笔均盈亏: {avg_pnl:+,.0f}')
    log(f'{"=" * 55}')

    report = {
        'version': 'v5.0',
        'date': datetime.now().strftime('%Y-%m-%d'),
        'time': datetime.now().strftime('%H:%M:%S'),
        'cash': data['cash'],
        'total_assets': total_assets,
        'return_pct': return_pct,
        'positions': data['positions'],
        'summary': {
            'total_trades': data['total_trades'],
            'total_pnl': data['total_pnl'],
            'wins': wins, 'losses': losses,
            'win_rate': round(win_rate, 1),
            'month_trades': month_total,
            'month_pnl': month_pnl,
            'month_win_rate': round(month_win_rate, 1),
            'avg_pnl_per_trade': round(avg_pnl, 0)
        }
    }
    with open(DAILY_REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return report


# ==================== 初始化 ====================
def init_system(log):
    """初始化v5系统（重置数据）"""
    data = {
        'positions': [], 'cash': CAPITAL, 'history': [],
        'start_date': datetime.now().strftime('%Y-%m-%d'),
        'total_trades': 0, 'total_pnl': 0,
        'version': 'v5.0'
    }
    save_positions(data)
    # 清除锁文件
    for lock in [RUN_LOCK_BUY, RUN_LOCK_SELL]:
        if os.path.exists(lock):
            os.remove(lock)
    log(f'✅ v5.0 系统已初始化，本金 {CAPITAL:,.0f}')


# ==================== 主流程 ====================
def main():
    parser = argparse.ArgumentParser(description='每日模拟交易 v5.0 日内版 (今天买明天卖)')
    parser.add_argument('--sell', action='store_true', help='T+1卖出模式（14:30执行）')
    parser.add_argument('--buy', action='store_true', help='选股买入模式（14:40执行）')
    parser.add_argument('--status', action='store_true', help='查看当前状态')
    parser.add_argument('--init', action='store_true', help='初始化系统（重置数据）')
    args = parser.parse_args()

    log = get_log()
    log(f'')
    log(f'{"=" * 55}')
    log(f'🦐 v5.0 日内版 (Today Buy Tomorrow Sell)')
    log(f'   {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    log(f'{"=" * 55}')

    if args.init:
        init_system(log)
        return

    if args.status:
        data = load_positions()
        generate_report(log, data)
        return

    if args.sell:
        run_sell(log)
    elif args.buy:
        run_buy(log)
    else:
        # 自动模式：根据时间判断
        hour = datetime.now().hour
        minute = datetime.now().minute
        if hour == 14 and minute < 35:
            log(f'⏰ 检测到14:30时段，执行卖出')
            run_sell(log)
        elif hour == 14 and minute >= 35:
            log(f'⏰ 检测到14:40时段，执行买入')
            run_buy(log)
        else:
            log(f'⏰ 非交易时间，只生成报告')

    # 始终生成报告
    data = load_positions()
    generate_report(log, data)


if __name__ == '__main__':
    main()
