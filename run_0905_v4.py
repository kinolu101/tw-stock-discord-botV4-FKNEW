# -*- coding: utf-8 -*-
"""09:05 反彈失敗放空：第一根完整5分K確認（僅訊號，不送委託）。"""
import json
import os
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import shioaji as sj
from dotenv import load_dotenv

TZ = ZoneInfo('Asia/Taipei')
INPUT_FILE = os.getenv('CANDIDATES_FILE', 'candidates_0900.json')
OUTPUT_FILE = os.getenv('BREAKOUTS_FILE', 'breakouts_0905.json')
MIN_FIRST_BAR_SHARES = float(os.getenv('SHORT_MIN_FIRST_BAR_SHARES', '50000'))
MIN_BODY_RATIO = float(os.getenv('SHORT_MIN_BODY_RATIO', '0.40'))
MAX_LOWER_WICK_RATIO = float(os.getenv('SHORT_MAX_LOWER_WICK_RATIO', '0.35'))
MAX_FIRST_BAR_DROP_PCT = float(os.getenv('SHORT_MAX_FIRST_BAR_DROP_PCT', '5.0'))


def now_tw():
    return datetime.now(TZ)


def send_discord(message):
    url = os.getenv('DISCORD_WEBHOOK', '')
    if not url:
        print('未設定 DISCORD_WEBHOOK，僅列印：', message)
        return
    resp = requests.post(url, json={'content': message}, timeout=15)
    resp.raise_for_status()


def load_candidates():
    with open(INPUT_FILE, encoding='utf-8') as f:
        data = json.load(f)
    generated = datetime.strptime(data['generated_at'], '%Y-%m-%d %H:%M:%S').date()
    if generated != now_tw().date():
        raise RuntimeError('09:00 候選檔不是今天產生，停止使用舊資料')
    return data.get('candidates', [])


def first_bar_from_ticks(ticks, today):
    """只取 09:00:00 <= ts < 09:05:00；時間為台北時間。"""
    df = pd.DataFrame({'ts': pd.to_datetime(ticks.ts), 'price': ticks.close, 'volume': ticks.volume})
    if df.empty:
        return None
    if df['ts'].dt.tz is not None:
        df['ts'] = df['ts'].dt.tz_convert(TZ).dt.tz_localize(None)
    start = datetime.combine(today, dt_time(9, 0))
    end = datetime.combine(today, dt_time(9, 5))
    df = df[(df.ts >= start) & (df.ts < end) & (df.volume > 0)].sort_values('ts')
    if df.empty:
        return None
    prices = df.price.astype(float)
    volume = df.volume.astype(float)
    return {'open': float(prices.iloc[0]), 'high': float(prices.max()),
            'low': float(prices.min()), 'close': float(prices.iloc[-1]),
            'volume_shares': float(volume.sum()),
            'vwap': float((prices * volume).sum() / volume.sum())}


def qualify(bar):
    span = bar['high'] - bar['low']
    if span <= 0 or bar['open'] <= 0:
        return False, '無有效波幅'
    body_ratio = (bar['open'] - bar['close']) / span
    lower_wick_ratio = (min(bar['open'], bar['close']) - bar['low']) / span
    drop_pct = (bar['open'] - bar['close']) / bar['open'] * 100
    checks = [
        (bar['close'] < bar['open'], '非黑K'),
        (bar['close'] < bar['vwap'], '收盤未低於VWAP'),
        (body_ratio >= MIN_BODY_RATIO, '黑K實體不足'),
        (lower_wick_ratio <= MAX_LOWER_WICK_RATIO, '下影線過長'),
        (bar['volume_shares'] >= MIN_FIRST_BAR_SHARES, '第一根成交股數不足'),
        (drop_pct <= MAX_FIRST_BAR_DROP_PCT, '第一根跌幅過大'),
    ]
    for ok, reason in checks:
        if not ok:
            return False, reason
    return True, '符合第一根黑K賣壓條件'


def run():
    load_dotenv()
    now = now_tw()
    if now.weekday() >= 5 or now.time() < dt_time(9, 5):
        raise RuntimeError('僅在交易日 09:05 後執行，不能使用未完成5分K')
    if not os.getenv('SHIOAJI_API_KEY') or not os.getenv('SHIOAJI_SECRET_KEY'):
        raise RuntimeError('缺少 Shioaji GitHub Secrets')
    candidates = load_candidates()
    api = sj.Shioaji(simulation=os.getenv('SHIOAJI_SIMULATION', 'false').lower() == 'true')
    api.login(api_key=os.environ['SHIOAJI_API_KEY'], secret_key=os.environ['SHIOAJI_SECRET_KEY'])
    results = []
    try:
        for row in candidates:
            code = str(row.get('股票代號', '')).strip()
            try:
                contract = api.Contracts.Stocks[code]
                # contract.day_trade 只是初步資訊，不代表帳戶已核准先賣後買。
                ticks = api.ticks(contract=contract, date=now.strftime('%Y-%m-%d'))
                bar = first_bar_from_ticks(ticks, now.date())
                if not bar:
                    print(code, '沒有第一根成交資料')
                    continue
                ok, reason = qualify(bar)
                print(code, reason)
                if not ok:
                    continue
                results.append({**row, '第一根開盤': bar['open'], '第一根高點': bar['high'],
                                '第一根低點': bar['low'], '第一根收盤': bar['close'],
                                '第一根量(股)': bar['volume_shares'], 'VWAP': round(bar['vwap'], 3),
                                '策略階段': '09:05第一根黑K確認，等待09:10反彈失敗',
                                '先賣後買資格': '未驗證'})
            except Exception as exc:
                print(f'{code} 資料讀取失敗：{exc}')
    finally:
        api.logout()
    payload = {'strategy': 'short_bounce_failure_0905_v4',
               'generated_at': now_tw().strftime('%Y-%m-%d %H:%M:%S'),
               'count': len(results), 'results': results}
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    lines = ['【09:05 空方第一根黑K確認｜觀察訊號】']
    if not results:
        lines.append('目前沒有符合條件的候選股。')
    for r in results:
        lines.append(f"🔻 {r['股票代號']} {r['股票名稱']}｜首根 O/H/L/C：{r['第一根開盤']}/{r['第一根高點']}/{r['第一根低點']}/{r['第一根收盤']}｜VWAP {r['VWAP']}｜量 {r['第一根量(股)']:.0f} 股")
    lines.append('09:10 尚須確認反彈失敗；目前非放空進場訊號。先賣後買帳戶資格、限制與券源未驗證。')
    send_discord('\n'.join(lines)[:1900])
    print('09:05 篩選完成', len(results))


if __name__ == '__main__':
    run()
