"""
09:05 台股突破股掃描器 V4 Cloud

V4 重點：
- 從 candidates_0900.json 讀取 09:00 候選股
- 不使用 Google Sheets
- 篩選 09:05 突破股前 TOP_N
- 發送 Discord 通知
"""

import json
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import shioaji as sj
from dotenv import load_dotenv

TZ = ZoneInfo("Asia/Taipei")

INPUT_FILE = os.getenv("CANDIDATES_FILE", "candidates_0900.json")
OUTPUT_FILE = os.getenv("BREAKOUTS_FILE", "breakouts_0905.json")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "1.0"))
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "9.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "3000"))
MIN_TURNOVER_TWD = float(os.getenv("MIN_TURNOVER_TWD", "300000000"))
TOP_N = int(os.getenv("TOP_N", "2"))

TWSE_HOLIDAYS = {
    d.strip()
    for d in os.getenv("TWSE_HOLIDAYS", "").split(",")
    if d.strip()
}


def now_tw() -> datetime:
    return datetime.now(TZ)


def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def is_trade_day() -> bool:
    today = now_tw().strftime("%Y-%m-%d")
    return now_tw().weekday() < 5 and today not in TWSE_HOLIDAYS


def discord_send(content: str) -> None:
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK 未設定，略過 Discord 發送。")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        print(f"Discord 發送失敗：{exc}")


def connect_shioaji() -> sj.Shioaji:
    load_dotenv()
    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")
    simulation = str_to_bool(os.getenv("SHIOAJI_SIMULATION"), default=False)

    if not api_key or not secret_key:
        raise RuntimeError("缺少 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY")

    api = sj.Shioaji(simulation=simulation)
    api.login(api_key=api_key, secret_key=secret_key)
    print(f"Shioaji simulation={simulation}")
    return api


def is_day_tradeable(contract: Any) -> bool:
    value = getattr(contract, "day_trade", None)
    if value is None:
        return False
    text = str(value).lower()
    return "yes" in text or text in {"true", "1", "daytrade.yes"}


def calc_turnover_100m(close_price: float, total_volume: float) -> float:
    return close_price * total_volume * 1000 / 100000000


def get_ticks_df(api: sj.Shioaji, contract: Any) -> Optional[pd.DataFrame]:
    today = now_tw().date()
    ticks = api.ticks(contract=contract, date=today.strftime("%Y-%m-%d"))
    if ticks is None or len(ticks.ts) == 0:
        return None
    df = pd.DataFrame({
        "ts": pd.to_datetime(ticks.ts),
        "price": ticks.close,
        "volume": ticks.volume,
    })
    if df["ts"].dt.tz is not None:
        df["ts"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
    return df.sort_values("ts")


def get_first_5m_k(df: pd.DataFrame) -> Optional[Tuple[float, float, float]]:
    today = now_tw().date()
    start = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=0)
    end = start + timedelta(minutes=5)
    kdf = df[(df["ts"] >= start) & (df["ts"] < end)]
    if kdf.empty:
        return None
    return float(kdf.iloc[0]["price"]), float(kdf.iloc[-1]["price"]), float(kdf["volume"].sum())


def get_recent_5m_ma(df: pd.DataFrame) -> Optional[Dict[str, Optional[float]]]:
    if df.empty:
        return None
    k = df.set_index("ts")["price"].sort_index().resample("5min", label="right", closed="right").ohlc().dropna()
    if k.empty:
        return None
    close = k["close"]
    latest_close = float(close.iloc[-1])
    result: Dict[str, Optional[float]] = {"latest_close": latest_close}
    for n in [5, 10, 20, 60]:
        result[f"ma{n}"] = float(close.rolling(n).mean().iloc[-1]) if len(close) >= n else None
    return result


def tick_size(price: float) -> float:
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def round_up_tick(price: float) -> float:
    t = tick_size(price)
    return round(math.ceil(price / t) * t, 2)


def round_down_tick(price: float) -> float:
    t = tick_size(price)
    return round(math.floor(price / t) * t, 2)


def calc_trade_prices(open_price: float, close_price: float) -> Tuple[float, float, float, float]:
    entry = round_up_tick(max(open_price * 1.002, close_price))
    stop = round_down_tick(min(open_price * 0.99, entry * 0.985))
    tp1 = round_down_tick(entry * 1.02)
    tp2 = round_down_tick(entry * 1.04)
    return entry, stop, tp1, tp2


def score_stock(change_rate: float, total_volume: float, turnover_100m: float, first_k_volume: float, above_60ma: bool) -> int:
    score = 0
    score += min(30, int(change_rate * 5))
    score += min(25, int(total_volume / 3000 * 7))
    score += min(25, int(turnover_100m / 3 * 10))
    score += min(10, int(first_k_volume / 1000 * 3))
    score += 10 if above_60ma else 0
    return min(score, 100)


def load_candidates() -> List[Dict[str, Any]]:
    if not os.path.exists(INPUT_FILE):
        return []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("candidates", [])


def save_breakouts(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "generated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "breakouts": rows,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def format_discord(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "【09:05 突破股】今日無符合條件標的。"

    lines = ["【09:05 突破股 TOP 2】"]
    for i, r in enumerate(results, 1):
        lines.extend([
            "",
            f"{i}. {r['股票代號']} {r['股票名稱']}｜{r['題材']}",
            f"漲幅：{r['漲幅']}%｜成交量：{int(r['成交量'])}｜成交值：{r['成交值(億)']}億",
            f"第一根5分K：{r['第一根5分K']}｜突破開盤價：是",
            f"5MA：{r['5MA']}｜10MA：{r['10MA']}｜20MA：{r['20MA']}",
            f"60MA：{r['60MA']}｜狀態：{r['60MA狀態']}",
            f"進場：{r['進場價']}｜停損：{r['停損價']}｜停利1：{r['停利1']}｜停利2：{r['停利2']}",
            f"強度分數：{r['強度分數']}",
        ])
    return "\n".join(lines)


def run() -> None:
    if not is_trade_day():
        print("今日台股未開盤，09:05 不執行選股。")
        return

    candidates = load_candidates()
    if not candidates:
        msg = "【09:05 突破股】找不到 09:00 候選名單或候選為 0。"
        print(msg)
        save_breakouts([])
        discord_send(msg)
        return

    api = connect_shioaji()

    codes = [str(r["股票代號"]).strip() for r in candidates]
    contracts = []
    candidate_map = {str(r["股票代號"]).strip(): r for r in candidates}

    for code in codes:
        try:
            contracts.append(api.Contracts.Stocks[code])
        except Exception:
            continue

    snapshots = api.snapshots(contracts)
    snap_map = {s.code: s for s in snapshots}

    print(f"=== 09:05 V4 從候選名單篩選突破股，共 {len(candidates)} 檔 ===")

    results: List[Dict[str, Any]] = []
    updated_at = now_tw().strftime("%Y-%m-%d %H:%M:%S")

    for contract in contracts:
        code = contract.code
        s = snap_map.get(code)
        old = candidate_map.get(code, {})
        if s is None:
            continue

        try:
            change_rate = float(s.change_rate)
            total_volume = float(s.total_volume)
            open_price = float(s.open)
            close_price = float(s.close)
            volume_0900 = float(old.get("成交量") or 0)
        except Exception:
            continue

        if not is_day_tradeable(contract):
            continue
        if not (MIN_CHANGE_RATE <= change_rate <= MAX_CHANGE_RATE):
            continue
        if total_volume <= MIN_VOLUME:
            continue
        if total_volume <= volume_0900:
            continue
        if close_price <= open_price:
            continue

        turnover_100m = calc_turnover_100m(close_price, total_volume)
        if turnover_100m * 100000000 < MIN_TURNOVER_TWD:
            continue

        ticks_df = get_ticks_df(api, contract)
        if ticks_df is None:
            continue

        first_k = get_first_5m_k(ticks_df)
        if first_k is None:
            continue
        first_open, first_close, first_volume = first_k
        if first_close <= first_open:
            continue

        if first_close <= open_price:
            continue

        ma = get_recent_5m_ma(ticks_df)
        if ma is None:
            continue

        latest = first_close
        ma5, ma10, ma20, ma60 = ma["ma5"], ma["ma10"], ma["ma20"], ma["ma60"]

        if ma5 is None or ma10 is None or ma20 is None:
        continue

       first_k_break_ma = (
            first_close > ma5 and
            first_close > ma10 and
            first_close > ma20
        )

        if not first_k_break_ma:
            continue

        above_60ma = ma60 is not None and latest > ma60
        status_60 = "上方 ✅" if above_60ma else "下方 ⚠️"
        score = score_stock(change_rate, total_volume, turnover_100m, first_volume, above_60ma)
        entry, stop, tp1, tp2 = calc_trade_prices(open_price, close_price)

        result = {
            "股票代號": code,
            "股票名稱": getattr(contract, "name", ""),
            "題材": str(old.get("題材") or "主流題材"),
            "漲幅": change_rate,
            "成交量": total_volume,
            "成交值(億)": round(turnover_100m, 2),
            "開盤價": open_price,
            "目前價": close_price,
            "是否可當沖": "是",
            "是否突破開盤價": "是",
            "第一根5分K": "紅K ✅",
            "5MA": round(ma5, 2),
            "10MA": round(ma10, 2),
            "20MA": round(ma20, 2),
            "60MA": round(ma60, 2) if ma60 is not None else "資料不足",
            "60MA狀態": status_60,
            "進場價": entry,
            "停損價": stop,
            "停利1": tp1,
            "停利2": tp2,
            "強度分數": score,
            "更新時間": updated_at,
        }
        results.append(result)

    results.sort(key=lambda r: (int(r["強度分數"]), float(r["成交值(億)"]), float(r["成交量"])), reverse=True)
    top_results = results[:TOP_N]
    save_breakouts(top_results)

    for r in top_results:
        print(
            f"{r['股票代號']} {r['股票名稱']} 題材:{r['題材']} 漲幅:{r['漲幅']} "
            f"成交量:{r['成交量']} 成交值:{r['成交值(億)']}億 "
            f"第一根:{r['第一根5分K']} 5MA:{r['5MA']} 10MA:{r['10MA']} 20MA:{r['20MA']} "
            f"60MA:{r['60MA']} {r['60MA狀態']} 進場:{r['進場價']} 停損:{r['停損價']} "
            f"停利:{r['停利1']}/{r['停利2']} 強度:{r['強度分數']}"
        )

    discord_send(format_discord(top_results))
    print(f"=== 09:05 V4 完成，共 {len(top_results)} 檔 ===")


if __name__ == "__main__":
    run()
