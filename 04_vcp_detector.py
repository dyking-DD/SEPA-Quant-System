#!/usr/bin/env python3
"""
SEPA量化系统 - VCP形态识别模块 v1.0
基于马克·米勒维尼的Volatility Contraction Pattern
"""
import sqlite3, numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path.home() / "SEPA_Quant_System_Pro"
DB_PATH  = BASE_DIR / "data" / "stocks.db"

def get_kline(code, days=150):
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql(
            f"SELECT * FROM daily_kline WHERE code='{code}' ORDER BY date DESC LIMIT {days}",
            conn, parse_dates=['date']
        )
    except:
        df = pd.DataFrame()
    conn.close()
    if df.empty: return None
    return df.sort_values('date').reset_index(drop=True)

def find_swing_lows(closes, min_distance=5, lookback=150):
    """寻找局部波段低点（间隔≥min_distance天）"""
    lows = []
    for i in range(min_distance, len(closes) - min_distance):
        window_left = closes[max(0, i-min_distance):i]
        window_right = closes[i+1:min(len(closes), i+min_distance+1)]
        if all(closes[i] < window_left) and all(closes[i] < window_right):
            lows.append((i, closes[i]))
    return lows

def find_swing_highs(closes, min_distance=5):
    """寻找局部波段高点"""
    highs = []
    for i in range(min_distance, len(closes) - min_distance):
        window_left = closes[max(0, i-min_distance):i]
        window_right = closes[i+1:min(len(closes), i+min_distance+1)]
        if all(closes[i] > window_left) and all(closes[i] > window_right):
            highs.append((i, closes[i]))
    return highs

def calculate_amplitude(high, low):
    """计算波段振幅"""
    return (high - low) / low

def identify_vcp(code, lookback=120, min_bands=2):
    """
    VCP形态识别主函数
    
    返回: {
        'is_vcp': bool,
        'vcp_score': int (0-100),
        'n_bands': int,
        'amplitude_decay': float,
        'volume_decay': float,
        'lows_ascending': bool,
        'breakout_price': float,
        'pivot_high': float,
        'details': [...]
    }
    """
    df = get_kline(code, lookback + 20)
    if df is None or len(df) < 40:
        return {'is_vcp': False, 'vcp_score': 0, 'reason': '数据不足'}
    
    closes = np.array(df['close'].values)
    volumes = np.array(df['volume'].values)
    highs = np.array(df['high'].values)
    lows = np.array(df['low'].values)
    
    # 找波段低点
    swing_lows = find_swing_lows(closes, min_distance=5, lookback=lookback)
    if len(swing_lows) < min_bands:
        return {'is_vcp': False, 'vcp_score': 0, 'reason': f'波段低点不足(找到{len(swing_lows)}个)'}
    
    # 取最近 N 个波段
    n = min(4, len(swing_lows))
    recent_lows = swing_lows[-n:]
    
    # 1. 检查低点是否逐次抬升（允许2%误差）
    low_prices = [l[1] for l in recent_lows]
    ascending = all(
        low_prices[i] >= low_prices[i-1] * 0.98 
        for i in range(1, len(low_prices))
    )
    asc_score = 30 if ascending else 0
    
    # 2. 计算每个波段的振幅收缩
    band_amplitudes = []
    band_volumes = []
    band_details = []
    
    for idx, (pos, low_price) in enumerate(recent_lows):
        # 找该低点对应的波段高点（前后各15天内）
        left_range = highs[max(0, pos-15):pos]
        right_range = highs[pos+1:min(len(highs), pos+16)]
        prev_low = lows[max(0, pos-1)]
        next_low = lows[pos]
        
        # 相邻波段高点
        all_near_highs = list(left_range) + list(right_range)
        if not all_near_highs:
            band_high = high_price
        else:
            band_high = max(all_near_highs)
        
        amp = (band_high - low_price) / low_price
        band_amplitudes.append(amp)
        
        # 该波段平均成交量
        vol_range = volumes[max(0, pos-10):min(len(volumes), pos+11)]
        band_volumes.append(np.mean(vol_range) if len(vol_range) > 0 else 1)
        
        band_details.append({
            'position': pos,
            'low': low_price,
            'high': band_high,
            'amplitude': amp,
            'avg_volume': band_volumes[-1]
        })
    
    # 3. 振幅收缩程度
    # 每个波段振幅应该比前一波段缩小≥20%
    decayed = 0
    decayed_ratio = 1.0
    for i in range(1, len(band_amplitudes)):
        if band_amplitudes[i] < band_amplitudes[i-1] * 0.80:
            decayed += 1
            decayed_ratio *= (band_amplitudes[i] / band_amplitudes[i-1])
    
    # 振幅收缩评分：完全收缩得40分，部分收缩按比例
    if len(band_amplitudes) >= 2:
        avg_decay = np.mean([band_amplitudes[i]/band_amplitudes[i-1] 
                            for i in range(1, len(band_amplitudes))])
        amp_score = max(0, 40 * (1 - avg_decay))  # 衰减越多分数越高
    else:
        amp_score = 20
    
    # 4. 成交量萎缩评分
    if len(band_volumes) >= 2:
        vol_decay = band_volumes[-1] / band_volumes[0] if band_volumes[0] > 0 else 1
        vol_score = max(0, 30 * (1 - vol_decay))  # 萎缩越多分数越高
    else:
        vol_score = 0
    
    # 5. 接近新高确认
    recent_high = np.max(closes[-20:])
    current = closes[-1]
    near_high_pct = (recent_high - current) / recent_high * 100
    near_high_score = 20 if near_high_pct <= 5 else (10 if near_high_pct <= 15 else 0)
    
    # 6. 综合评分
    vcp_score = min(100, int(amp_score + asc_score + vol_score + near_high_score))
    
    # 7. 判断是否VCP形态
    # 条件：振幅收缩(至少2个波段) + 低点抬升 + 成交量萎缩(≥30%) + 接近新高
    is_vcp = (
        decayed >= 1 and  # 至少1次收缩
        ascending and       # 低点抬升
        (band_volumes[-1] / band_volumes[0] < 0.75 if len(band_volumes) >= 2 else False) and  # 成交量萎缩
        near_high_pct <= 15  # 距新高不太远
    )
    
    # 突破价位
    pivot_high = recent_high
    
    return {
        'is_vcp': is_vcp,
        'vcp_score': vcp_score,
        'n_bands': len(band_amplitudes),
        'amplitude_decay': decayed_ratio,
        'volume_decay': band_volumes[-1] / band_volumes[0] if len(band_volumes) >= 2 and band_volumes[0] > 0 else 1.0,
        'lows_ascending': ascending,
        'breakout_price': pivot_high,
        'near_high_pct': near_high_pct,
        'current_price': current,
        'band_details': band_details,
        'score_breakdown': {
            'amplitude_score': round(amp_score, 1),
            'ascending_score': asc_score,
            'volume_score': round(vol_score, 1),
            'near_high_score': near_high_score,
        }
    }

def analyze_vcp_batch(codes):
    """批量分析多只股票的VCP形态"""
    results = []
    for code in codes:
        r = identify_vcp(code)
        r['code'] = code
        results.append(r)
    
    df = pd.DataFrame(results)
    df = df.sort_values('vcp_score', ascending=False)
    return df

def visualize_vcp(code):
    """打印VCP分析结果"""
    r = identify_vcp(code)
    print("=" * 55)
    print(f"  VCP形态分析: {code}")
    print("=" * 55)
    
    if r.get('reason'):
        print(f"  状态: 无法分析 - {r['reason']}")
        return
    
    print(f"  VCP形态: {'是 ✅' if r['is_vcp'] else '否 ❌'}")
    print(f"  VCP评分: {r['vcp_score']}/100")
    print(f"  波段数量: {r['n_bands']}")
    print(f"  低点抬升: {'是 ✅' if r['lows_ascending'] else '否 ❌'}")
    print(f"  成交量萎缩: {r['volume_decay']:.1%}")
    print(f"  振幅收缩: {r['amplitude_decay']:.1%}")
    print(f"  距波段高点: {r['near_high_pct']:.1f}%")
    print(f"  突破价位: {r['breakout_price']:.2f}")
    print(f"  当前价: {r['current_price']:.2f}")
    print()
    print(f"  评分明细:")
    for k, v in r['score_breakdown'].items():
        print(f"    {k}: {v}")
    
    if r.get('band_details'):
        print(f"\n  波段详情:")
        for i, b in enumerate(r['band_details'], 1):
            print(f"    波段{i}: 低={b['low']:.2f} 高={b['high']:.2f} 振幅={b['amplitude']*100:.1f}%")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        visualize_vcp(sys.argv[1])
    else:
        # 演示
        test_codes = ["300750","688041","002475","300059","688981"]
        print("VCP形态批量分析演示")
        print("=" * 55)
        for code in test_codes:
            r = identify_vcp(code)
            status = "VCP ✅" if r['is_vcp'] else "非VCP"
            print(f"  {code}: 评分={r['vcp_score']:>3}/100  {status}  量缩={r.get('volume_decay', 0):.0%}")
