#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日模拟交易 v5.1 日内版 (Today Buy Tomorrow Sell)
策略:T+1日内交易,今天14:40买入,明天14:30卖出
目标:跑一个月看盈亏率

核心改进(v5.1 vs v5.0):
0. 🆕 VCP形态识别(SEPA核心算法)
1. 全A股股票池(不再限创业板+科创板)
2. 历史动量因子(20日涨幅排名)
3. 板块分散(单行业<=25%仓位)
4. 大盘情绪仓位调节(强/中/弱三档)
5. T+1强制卖出(次日14:30全部清仓)
6. 日内止损3%(防黑天鹅)

数据源:
- 东方财富:全A股列表 + 行业分类 + 实时行情
- 新浪财经:K线数据(MA/RSI/动量计算)

Crontab (see README for setup):\n  14:30 sell, 14:40 buy, weekdays only
"""

import sys, json, time, os, requests, argparse, subprocess
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np

# ==================== 配置 ====================
CAPITAL = 100000               # 初始本金
MAX_POSITIONS = 5              # 最大持仓数
MIN_POSITION_COST = 3000       # 单笔最小金额
MAX_POSITION_COST = 30000      # 单笔最大金额(单股<=30%本金)
MAX_SECTOR_RATIO = 0.25        # 单行业最大仓位占比
STOP_LOSS_PCT = 0.97           # 日内止损3%(次日检查)

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
        msg = f'[{title}]{content}'
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
                json={"msg_type": "text", "content": {"text": f"[{title}]{content}"}}, timeout=10)
        except:
            pass


# ==================== 东方财富:全A股列表 ====================
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
    """从东方财富获取全A股列表(含行业分类+实时行情),分页获取"""
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
        for pn in range(1, 150):  # 最多150页x100条=15000只
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
            # 提前退出:已获取足够
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
            # 跳过北交所(流动性差)
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


# ==================== 新浪:K线数据 ====================
def get_kline_data(code, datalen=30):
    """获取K线数据(用于MA/RSI/动量计算)"""
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


def calc_technicals(code, datalen=30):
    """一次性计算MA5/MA10/MA20/RSI/动量/量比"""
    kline = get_kline_data(code, datalen)
    if not kline or len(kline) < 10:
        return None

    closes = [float(d['close']) for d in kline]
    volumes = [float(d['volume']) for d in kline]

    # MA
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma_bullish = ma10 is not None and ma5 > ma10

    # 量比(今日成交量 / 5日均量)
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

    # 20日动量(核心新因子)
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


# ==================== VCP形态识别 (SEPA核心) ====================
def find_swing_lows(closes, min_distance=5):
    """找波段低点(谷)
    
    Args:
        closes: 收盘价数组
        min_distance: 最小间距(避免噪音)
    
    Returns:
        list of (position, price)
    """
    n = len(closes)
    swing_lows = []
    
    for i in range(min_distance, n - min_distance):
        window = closes[max(0, i - min_distance):min(n, i + min_distance + 1)]
        if closes[i] == min(window):
            # 避免相邻重复(取最左边的)
            if not swing_lows or i - swing_lows[-1][0] >= min_distance:
                swing_lows.append((i, closes[i]))
    
    return swing_lows


def identify_vcp(code, lookback=120, min_bands=2):
    """SEPA量化系统 - VCP形态识别增强版 v1.1
    
    VCP(Volatility Contraction Pattern)是Mark Minervini SEPA策略的核心形态,
    用于捕捉基底突破前的收缩整理,识别机构建仓信号。
    
    VCP评分构成:
    - 低点抬升:25分
    - 振幅收缩:35分(每次收缩+15)
    - 时间收缩:15分
    - 高点平齐:15分
    - 极地量:10分
    
    判定标准:
    - 至少1次振幅收缩 + 低点抬升 + 距离高点<12% + 高点平齐
    
    Args:
        code: 股票代码(新浪格式如 sh600519)
        lookback: 回看天数(默认120)
        min_bands: 最少波段数(默认2)
    
    Returns:
        dict: VCP分析结果
    """
    # 获取K线数据(多取40天保证计算精度)
    kline = get_kline_data(code, lookback + 40)
    if not kline or len(kline) < 60:
        return {
            'is_vcp': False, 'vcp_score': 0, 'reason': '数据不足',
            'is_breakout': False, 'near_high_pct': 100
        }
    
    closes = np.array([float(d['close']) for d in kline])
    volumes = np.array([float(d['volume']) for d in kline])
    highs = np.array([float(d['high']) for d in kline])
    lows = np.array([float(d['low']) for d in kline])
    
    # 找波段低点
    swing_lows = find_swing_lows(closes, min_distance=5)
    if len(swing_lows) < min_bands:
        return {
            'is_vcp': False, 'vcp_score': 0,
            'reason': f'波段低点不足(找到{len(swing_lows)}个)',
            'is_breakout': False, 'near_high_pct': 100
        }
    
    # 取最近N个波段(通常VCP看2-4个)
    n = min(4, len(swing_lows))
    recent_lows = swing_lows[-n:]
    
    # 1. 检查低点是否逐次抬升(允许2%误差)
    low_prices = [l[1] for l in recent_lows]
    ascending = all(low_prices[i] >= low_prices[i-1] * 0.98 
                    for i in range(1, len(low_prices)))
    asc_score = 25 if ascending else 0
    
    # 2. 计算波段详情
    band_details = []
    for idx, (pos, low_price) in enumerate(recent_lows):
        search_start = max(0, pos - 20)
        band_high = float(np.max(highs[search_start:pos])) if pos > 0 else float(highs[pos])
        
        duration = 0
        if idx > 0:
            duration = recent_lows[idx][0] - recent_lows[idx-1][0]
        
        amp = (band_high - low_price) / low_price
        
        end_vol = float(np.mean(volumes[max(0, pos-2):pos+1]))
        
        band_details.append({
            'pos': pos,
            'low': float(low_price),
            'high': band_high,
            'amplitude': amp,
            'duration': duration,
            'vol': end_vol
        })
    
    
    # 3. VCP核心算法判定
    
    # (1) 振幅收缩:后一个波段比前一个更紧
    amp_decay_count = 0
    for i in range(1, len(band_details)):
        if band_details[i]['amplitude'] < band_details[i-1]['amplitude'] * 0.9:
            amp_decay_count += 1
    amp_score = min(35, amp_decay_count * 15)
    
    # (2) 时间收缩:收缩周期变短
    time_decay = True
    if len(band_details) >= 3:
        time_decay = band_details[-1]['duration'] < band_details[-2]['duration']
    time_score = 15 if time_decay else 0
    
    # (3) 高点平齐度:VCP要求高点在同一压力线附近(上下4%以内)
    all_highs = [b['high'] for b in band_details]
    highs_consistent = (max(all_highs) - min(all_highs)) / min(all_highs) < 0.04
    high_score = 15 if highs_consistent else 0
    
    # (4) 极地量判定:形态最右侧成交量极度萎缩
    avg_vol_50 = float(np.mean(volumes[-50:]))
    last_3d_vol = float(np.mean(volumes[-3:]))
    ultra_low_vol = last_3d_vol < avg_vol_50 * 0.6
    vol_score = 10 if ultra_low_vol else 0
    
    # 4. 综合确认
    recent_high = float(np.max(highs[-60:]))
    current_price = float(closes[-1])
    near_high_pct = (recent_high - current_price) / recent_high * 100
    
    vcp_score = int(asc_score + amp_score + time_score + high_score + vol_score)
    
    # 判定标准:至少1次振幅收缩 + 低点抬升 + 距离高点不远 + 高点平齐
    is_vcp = (amp_decay_count >= 1 and ascending and 
              near_high_pct < 12 and highs_consistent)
    
    # 5. 判断是否即将突破
    ma5 = float(np.mean(closes[-5:]))
    ma10 = float(np.mean(closes[-10:]))
    ma20 = float(np.mean(closes[-20:]))
    price_above_ma = current_price > ma5 > ma10 > ma20
    
    # 突破当天成交量是否放大
    today_vol = float(volumes[-1])
    vol_breakout = today_vol > avg_vol_50 * 1.5
    
    is_breakout = (is_vcp and 
                   current_price >= recent_high * 0.98 and
                   price_above_ma)
    
    return {
        'is_vcp': is_vcp,
        'vcp_score': vcp_score,
        'n_bands': len(band_details),
        'lows_ascending': ascending,
        'highs_consistent': highs_consistent,
        'near_high_pct': round(near_high_pct, 2),
        'breakout_price': round(recent_high, 2),
        'current_price': round(current_price, 2),
        'is_breakout': is_breakout,
        'price_above_ma': price_above_ma,
        'vol_breakout': vol_breakout,
        'score_breakdown': {
            'asc_score': asc_score,
            'amp_score': amp_score,
            'time_score': time_score,
            'high_score': high_score,
            'vol_score': vol_score,
        },
        'band_details': band_details
    }


# ==================== 大盘情绪 ====================
def check_market_sentiment(log):
    """检查大盘情绪,返回 (行情dict, 情绪文字, 仓位比例)"""
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


# ==================== 综合评分 v5.1(7因子模型+VCP) ====================
def calculate_score_v5(stock, tech_data=None, vcp_data=None, sepa_data=None):
    """
    v5.1综合评分(7因子模型 + VCP)
    1. 资金流向评分 20%  -- 涨幅位置
    2. 历史动量评分 25%  -- 20日涨幅排名(核心)
    3. 技术面评分   15%  -- MA/RSI
    4. VCP形态     10%  -- 收缩突破(新增)
    5. 成交额规模   15%  -- 资金体量
    6. 量比评分     10%  -- 放量确认
    7. 板块动量      5%  -- 涨幅x成交额
    """
    change = stock['change']
    amount_yi = stock['amount_yi']

    # ── 1. 资金流向 (20%) ──
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

    # ── 3. 技术面 (15%) ──
    tech_score = 15
    if tech_data:
        tech_score = 0
        if tech_data.get('ma_bullish'):
            tech_score += 15
        if tech_data.get('price_vs_ma5', -99) > 0:
            tech_score += 8
        rsi = tech_data.get('rsi', 50)
        if rsi < 40:
            tech_score += 10    # 超卖反弹
        elif rsi < 55:
            tech_score += 12    # 健康区间
        elif rsi < 65:
            tech_score += 5
        else:
            tech_score -= 3     # 偏高警告
        if tech_data.get('ma20') and stock['price'] > tech_data['ma20']:
            tech_score += 5     # MA20支撑

    # ── 4. VCP形态 (10%) ── 新增!
    vcp_score = 0
    if vcp_data:
        vs = vcp_data.get('vcp_score', 0)
        is_vcp = vcp_data.get('is_vcp', False)
        is_breakout = vcp_data.get('is_breakout', False)
        
        if vs >= 80:
            vcp_score = 10
        elif vs >= 60:
            vcp_score = 8
        elif vs >= 40:
            vcp_score = 5
        elif vs >= 20:
            vcp_score = 2
        
        # 突破确认额外加分
        if is_breakout:
            vcp_score = min(12, vcp_score + 3)
        elif is_vcp:
            vcp_score = min(12, vcp_score + 2)

    # ── 4.5 SEPA趋势 (10%) ──
    sepa_score = 0
    if sepa_data:
        sep_pass = sepa_data.get('pass_trend', False)
        sep_stage = sepa_data.get('sepa_stage', 'unknown')
        sep_trend_score = sepa_data.get('trend_score', 0)
        if sep_pass:
            sepa_score = 15
            if sep_stage == 'accumulation':
                sepa_score = min(20, sepa_score + 5)
            elif sep_stage == 'preliminary':
                sepa_score = min(18, sepa_score + 3)
        else:
            pass_count = sepa_data.get('pass_count', 0)
            sepa_score = 8 if pass_count >= 2 else max(0, sep_trend_score // 3)

    # ── 5. 成交额规模 (13%) ──
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

    # ── 6. 量比 (10%) ──
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

    # ── 7. 板块动量 (5%) ──
    if 2 <= change <= 4 and amount_yi >= 10:
        sector_score = 15
    elif 1 <= change <= 5 and amount_yi >= 5:
        sector_score = 10
    else:
        sector_score = 5

    # ── 综合加权 ──
    total = (flow_score * 0.18 +
             momentum_score * 0.22 +
             tech_score * 0.12 +
             vcp_score * 0.10 +
             sepa_score * 0.10 +
             amount_score * 0.13 +
             vol_score * 0.10 +
             sector_score * 0.05)

    return max(20.0, min(98.0, round(total, 1)))


# ==================== 选股 v5.1 ====================
def screen_stocks(log):
    """v5.1选股:全A股筛选 + 7因子评分 + VCP形态"""
    log('[GET] 从东方财富获取全A股行情...')
    all_stocks = get_all_astocks()
    log(f'📊 获取到 {len(all_stocks)} 只A股')

    # 初筛条件(放宽涨幅区间)
    candidates = []
    for s in all_stocks:
        # 涨幅 0.5%~8%(扩大候选池)
        if not (0.5 <= s['change'] <= 8.0):
            continue
        # 成交额 >= 5亿(提高门槛)
        if s['amount_yi'] < 5.0:
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

    # 按涨幅+成交额初步排序,取前100只获取详细技术数据
    candidates.sort(
        key=lambda x: x['change'] * 0.4 + min(x['amount_yi'], 50) * 0.6,
        reverse=True
    )
    top_candidates = candidates[:100]  # 扩大到100只

    # 获取技术数据(MA/RSI/动量)
    log(f'[UP] 获取技术数据 + VCP形态(前{len(top_candidates)}只)...')
    for i, stock in enumerate(top_candidates):
        tech = calc_technicals(stock['code'])
        stock['tech_data'] = tech
        
        # VCP形态识别(只对评分较高的候选)
        if i < 30 and tech:  # 前30只做VCP分析
            try:
                vcp = identify_vcp(stock['code'], lookback=120)
                stock['vcp_data'] = vcp
            except Exception as e:
                stock['vcp_data'] = None
        else:
            stock['vcp_data'] = None
        
        if (i + 1) % 20 == 0:
            log(f'  技术+VCP数据进度 {i + 1}/{len(top_candidates)}')
        time.sleep(0.05)

    # RSI过滤:排除超买 RSI >= 65(更严格)
    before = len(top_candidates)
    filtered = []
    for s in top_candidates:
        rsi = s.get('tech_data', {}).get('rsi') if s.get('tech_data') else None
        if rsi is not None and rsi >= 65:  # 从70降到65
            continue
        filtered.append(s)
    log(f'  RSI过滤: {before} → {len(filtered)} (RSI>=65 已排除)')

    # 评分(含VCP因子)
    for stock in filtered:
        stock['total_score'] = calculate_score_v5(
            stock, 
            stock.get('tech_data'),
            stock.get('vcp_data')  # 新增VCP
        )

    # VCP优先排序逻辑
    def sort_key(x):
        score = x['total_score']
        vcp = x.get('vcp_data')
        # VCP形态额外加权
        if vcp and vcp.get('is_breakout'):
            score += 15  # 突破形态加15分
        elif vcp and vcp.get('is_vcp'):
            score += 8   # VCP形态加8分
        return score

    # 排除已持仓
    data = load_positions()
    held = [p['code'] for p in data['positions']]
    filtered = [c for c in filtered if c['code'] not in held]

    # 按VCP加权评分排序
    filtered.sort(key=sort_key, reverse=True)

    log(f'🏆 评分前15:')
    for i, c in enumerate(filtered[:15]):
        tech = c.get('tech_data') or {}
        vcp = c.get('vcp_data') or {}
        m20 = tech.get('momentum_20d', '-')
        rsi = tech.get('rsi', '-')
        vcp_s = vcp.get('vcp_score', 0)
        vcp_flag = '[OK]VCP' if vcp.get('is_vcp') else ('[BUY]突破' if vcp.get('is_breakout') else '')
        
        log(f'  {i+1}. {c["name"]}({c["code"]}) 评分{c["total_score"]:.0f} {vcp_flag}\n'
            f'     涨{c["change"]:.2f}% | {c["amount_yi"]:.1f}亿 | '
            f'动量20d={m20}% | RSI={rsi} | VCP={vcp_s} | [{c.get("industry", "?")}]')

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
            log(f'  ⏭️ {stock["name"]}[{industry}] 行业仓位已满({sector_cost[industry]:,.0f}/{max_sector:,.0f}),跳过')
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
        log(f'  ⚠️ {pos["name"]} 无法获取行情,跳过卖出')
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


# ==================== 卖出流程(T+1) ====================
def run_sell(log):
    """T+1卖出:次日14:30强制清仓"""
    log(f'')
    log(f'📉 ===== T+1卖出模式 =====')

    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(RUN_LOCK_SELL):
        with open(RUN_LOCK_SELL) as f:
            last_run = f.read().strip()
        if last_run == today_str:
            log(f'⏭️ 今日已执行过卖出,跳过')
            return []

    data = load_positions()

    if not data['positions']:
        log(f'📭 无持仓需要卖出')
        with open(RUN_LOCK_SELL, 'w') as f:
            f.write(today_str)
        return []

    log(f'📊 持仓 {len(data["positions"])} 只,开始卖出...')

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
            reason = '日内止损(<=-3%)'
        elif pnl_pct >= 8:
            reason = 'T+1盈利止盈'
        else:
            reason = 'T+1到期'

        sell_list.append((idx, reason, pnl_pct, current))

    # 从后往前卖(避免索引偏移)
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
        msg = f'[v5.0卖出]{datetime.now().strftime("%m-%d %H:%M")}\n\n'
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
    """T+1买入:14:40选股买入"""
    log(f'')
    log(f'[UP] ===== T+1买入模式 =====')

    today_str = datetime.now().strftime('%Y-%m-%d')
    if os.path.exists(RUN_LOCK_BUY):
        with open(RUN_LOCK_BUY) as f:
            last_run = f.read().strip()
        if last_run == today_str:
            log(f'⏭️ 今日已买入,跳过')
            return []

    data = load_positions()
    log(f'📊 账户: 现金={data["cash"]:,.0f} | 持仓={len(data["positions"])}只')

    # 大盘情绪
    log(f'')
    log(f'🌍 大盘情绪:')
    market, sentiment, position_ratio = check_market_sentiment(log)

    if position_ratio <= 0:
        log(f'⚠️ 大盘极弱,今日不买入')
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

    # 仓位分配(大盘情绪调节)
    available_cash = data['cash'] * position_ratio
    log(f'')
    log(f'💰 可用资金: {data["cash"]:,.0f} x {position_ratio:.0%} = {available_cash:,.0f}')

    allocs = allocate_with_sector_limit(candidates, available_cash, log)
    if not allocs:
        log(f'❌ 无符合条件的目标')
        with open(RUN_LOCK_BUY, 'w') as f:
            f.write(today_str)
        save_positions(data)
        return []

    total_cost = sum(a['actual_cost'] for a in allocs)
    log(f'  计划买入 {len(allocs)} 只,总 {total_cost:,.0f}')

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
            log(f'  [OK] {p["name"]} {p["shares"]}股@{p["buy_price"]:.2f} 成本{p["cost"]:,.0f}')

    save_positions(data)

    # 写入买入锁
    with open(RUN_LOCK_BUY, 'w') as f:
        f.write(today_str)

    # QQ推送
    if bought:
        msg = f'[v5.0买入]{datetime.now().strftime("%m-%d %H:%M")}\n'
        msg += f'大盘: {sentiment} | 仓位: {position_ratio * 100:.0f}%\n\n'
        for p in bought:
            msg += (f'📗{p["name"]}({p["code"]})\n'
                    f'{p["buy_price"]:.2f}x{p["shares"]}={p["cost"]:,.0f}\n'
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
    """初始化v5系统(重置数据)"""
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
    log(f'[OK] v5.0 系统已初始化,本金 {CAPITAL:,.0f}')


# ==================== 主流程 ====================
def main():
    parser = argparse.ArgumentParser(description='每日模拟交易 v5.0 日内版 (今天买明天卖)')
    parser.add_argument('--sell', action='store_true', help='T+1卖出模式(14:30执行)')
    parser.add_argument('--buy', action='store_true', help='选股买入模式(14:40执行)')
    parser.add_argument('--status', action='store_true', help='查看当前状态')
    parser.add_argument('--init', action='store_true', help='初始化系统(重置数据)')
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
        # 自动模式:根据时间判断
        hour = datetime.now().hour
        minute = datetime.now().minute
        if hour == 14 and minute < 35:
            log(f'⏰ 检测到14:30时段,执行卖出')
            run_sell(log)
        elif hour == 14 and minute >= 35:
            log(f'⏰ 检测到14:40时段,执行买入')
            run_buy(log)
        else:
            log(f'⏰ 非交易时间,只生成报告')

    # 始终生成报告
    data = load_positions()
    generate_report(log, data)


if __name__ == '__main__':
    main()

# ==================== SEPA趋势模板参数 ====================
# Trend Template: SEPA策略核心趋势条件
TREND_STRICT_MODE = False       # 严格模式:趋势4项需满足>=3项才买入

# ==================== SEPA趋势模板 ====================
def check_sepa_trend_template(tech_data, stock_price=None):
    if not tech_data or not tech_data.get('closes') or len(tech_data['closes']) < 200:
        return {'pass_trend': False, 'trend_score': 0, 'sepa_stage': 'unknown', 'pass_count': 0}
    closes = tech_data['closes']
    ma5 = tech_data.get('ma5', 0)
    ma20 = tech_data.get('ma20', 0)
    # 近似计算MA50, MA150, MA200
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma5 * 1.01
    ma150 = sum(closes[-150:]) / 150 if len(closes) >= 150 else ma20 * 0.98
    ma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else ma20 * 0.93
    current_price = stock_price if stock_price else closes[-1]
    trend_score = 0
    pass_count = 0
    # 条件1: 股价>MA150 且 股价>MA200
    if ma150 and ma200 and current_price > ma150 and current_price > ma200:
        trend_score += 15; pass_count += 1
    # 条件2: MA50>MA150>MA200 (均线多头排列)
    if ma50 > ma150 > ma200:
        trend_score += 15; pass_count += 1
    # 条件3: MA200向上拐头(近20日)
    if len(closes) >= 220 and ma200 > sum(closes[-220:-200]) / 20:
        trend_score += 10; pass_count += 1
    # 条件4: MA50>MA150
    if ma50 > ma150:
        trend_score += 5; pass_count += 1
    pass_trend = pass_count >= 3 if TREND_STRICT_MODE else pass_count >= 2
    sepa_stage = 'accumulation' if pass_count >= 3 and ma50 > ma150 > ma200 else ('preliminary' if pass_count >= 2 else 'waiting')
    return {'pass_trend': pass_trend, 'trend_score': trend_score, 'sepa_stage': sepa_stage, 'pass_count': pass_count}
