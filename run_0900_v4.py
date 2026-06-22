"""
09:00 Mr.Price 開盤策略候選股 V4

策略重點：
- 先用高周轉/高成交值與開盤漲幅建立觀察池
- 排除 ETF / KY / 不可當沖
- 避免一開盤漲太高，預設漲幅 1%～6%
- 結果寫入 candidates_0900.json，供 09:05 / 09:10 使用

注意：影片中的完整進場條件重點在「量能黃金交叉 + 多頭排列 + 股價站均價線 + 第二根 5 分 K 突破第一根爆量 K 高點」。
09:00 這支只先做觀察池，真正進場訊號放在 run_0910_v4.py。
"""

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import shioaji as sj
from dotenv import load_dotenv

TZ = ZoneInfo("Asia/Taipei")

OUTPUT_FILE = os.getenv("CANDIDATES_FILE", "candidates_0900.json")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "1.0"))
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "6.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "1000"))
MIN_TURNOVER_TWD = float(os.getenv("MIN_TURNOVER_TWD", "80000000"))
TOP_PRINT_N = int(os.getenv("TOP_PRINT_N", "20"))

EXCLUDE_KY = True
EXCLUDE_ETF = True
EXCLUDE_FINANCIAL = os.getenv("EXCLUDE_FINANCIAL", "false").lower() in {"1", "true", "yes"}

TWSE_HOLIDAYS = {
    d.strip()
    for d in os.getenv("TWSE_HOLIDAYS", "").split(",")
    if d.strip()
}



SUPPORT_RESISTANCE_DAYS = int(os.getenv("SUPPORT_RESISTANCE_DAYS", "20"))
SUPPORT_RESISTANCE_LOOKBACK_CALENDAR_DAYS = int(
    os.getenv("SUPPORT_RESISTANCE_LOOKBACK_CALENDAR_DAYS", "45")
)
SUPPORT_RESISTANCE_ZONE_RATIO = float(
    os.getenv("SUPPORT_RESISTANCE_ZONE_RATIO", "0.003")
)


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
    import math
    step = tick_size(price)
    return round(math.ceil(price / step - 1e-9) * step, 2)


def round_down_tick(price: float) -> float:
    import math
    step = tick_size(price)
    return round(math.floor(price / step + 1e-9) * step, 2)


def _kbars_to_intraday_df(kbars: Any) -> Optional[pd.DataFrame]:
    try:
        df = pd.DataFrame({
            "ts": pd.to_datetime(getattr(kbars, "ts")),
            "open": getattr(kbars, "Open"),
            "high": getattr(kbars, "High"),
            "low": getattr(kbars, "Low"),
            "close": getattr(kbars, "Close"),
            "volume": getattr(kbars, "Volume"),
        })
    except Exception as exc:
        print(f"日K資料轉換失敗：{exc}")
        return None

    if df.empty:
        return None
    if df["ts"].dt.tz is not None:
        df["ts"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
    return df.sort_values("ts")


def get_recent_daily_bars(
    api: sj.Shioaji,
    contract: Any,
    trading_days: int = SUPPORT_RESISTANCE_DAYS,
) -> Optional[pd.DataFrame]:
    """
    取得最近完整交易日的日K。
    使用約45個日曆日的分鐘K，彙整為日K，再取最後20個交易日。
    今天尚未完成的K棒不納入壓力支撐計算。
    """
    end_date = now_tw().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=SUPPORT_RESISTANCE_LOOKBACK_CALENDAR_DAYS)

    try:
        kbars = api.kbars(
            contract=contract,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        print(f"取得近20日日K失敗 {getattr(contract, 'code', '')}: {exc}")
        return None

    raw = _kbars_to_intraday_df(kbars)
    if raw is None or raw.empty:
        return None

    raw["date"] = raw["ts"].dt.date
    daily = raw.groupby("date", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    daily = daily[daily["volume"] > 0].tail(trading_days)
    if len(daily) < 5:
        return None
    return daily


def calculate_support_resistance(
    daily: pd.DataFrame,
    current_price: float,
) -> Dict[str, Any]:
    """
    以最近20個完整交易日尋找離現價最近的局部波段高、低點。
    找不到適合的局部高低點時，改用20日最高／最低。
    壓力支撐以中心價上下0.3%顯示為區間。
    """
    highs = daily["high"].astype(float)
    lows = daily["low"].astype(float)

    local_highs = []
    local_lows = []
    for i in range(2, len(daily) - 2):
        h = float(highs.iloc[i])
        l = float(lows.iloc[i])
        if h >= float(highs.iloc[i - 2:i + 3].max()):
            local_highs.append(h)
        if l <= float(lows.iloc[i - 2:i + 3].min()):
            local_lows.append(l)

    twenty_high = float(highs.max())
    twenty_low = float(lows.min())

    higher_levels = sorted({p for p in local_highs if p > current_price})
    lower_levels = sorted({p for p in local_lows if p < current_price}, reverse=True)

    resistance = higher_levels[0] if higher_levels else twenty_high
    support = lower_levels[0] if lower_levels else twenty_low

    resistance_low = round_down_tick(resistance * (1 - SUPPORT_RESISTANCE_ZONE_RATIO))
    resistance_high = round_up_tick(resistance * (1 + SUPPORT_RESISTANCE_ZONE_RATIO))
    support_low = round_down_tick(support * (1 - SUPPORT_RESISTANCE_ZONE_RATIO))
    support_high = round_up_tick(support * (1 + SUPPORT_RESISTANCE_ZONE_RATIO))

    resistance_distance = (
        (resistance - current_price) / current_price * 100
        if current_price > 0 else 0.0
    )
    support_distance = (
        (current_price - support) / current_price * 100
        if current_price > 0 else 0.0
    )

    if current_price > twenty_high:
        space_text = "已突破20日高點 🚀"
    elif resistance_distance < 0.8:
        space_text = "上方空間偏小 ⚠️"
    elif resistance_distance <= 2.0:
        space_text = "上方空間一般"
    else:
        space_text = "上方空間充足 ✅"

    return {
        "近期壓力中心": round(resistance, 2),
        "近期壓力區": f"{resistance_low}～{resistance_high}",
        "近期支撐中心": round(support, 2),
        "近期支撐區": f"{support_low}～{support_high}",
        "20日最高": round(twenty_high, 2),
        "20日最低": round(twenty_low, 2),
        "距壓力%": round(resistance_distance, 2),
        "距支撐%": round(support_distance, 2),
        "空間評估": space_text,
    }


def get_support_resistance_info(
    api: sj.Shioaji,
    contract: Any,
    current_price: float,
) -> Dict[str, Any]:
    daily = get_recent_daily_bars(api, contract)
    if daily is None:
        return {
            "近期壓力中心": None,
            "近期壓力區": "資料不足",
            "近期支撐中心": None,
            "近期支撐區": "資料不足",
            "20日最高": "資料不足",
            "20日最低": "資料不足",
            "距壓力%": None,
            "距支撐%": None,
            "空間評估": "資料不足",
        }
    return calculate_support_resistance(daily, current_price)

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


def is_common_stock(contract: Any) -> bool:
    code = getattr(contract, "code", "")
    name = getattr(contract, "name", "")
    if not code.isdigit() or len(code) != 4:
        return False
    if EXCLUDE_KY and "KY" in name.upper():
        return False
    if EXCLUDE_ETF and ("ETF" in name.upper() or code.startswith("00")):
        return False
    if EXCLUDE_FINANCIAL and (code.startswith("28") or "金" in name or "銀" in name or "保" in name):
        return False
    return True


def is_day_tradeable(contract: Any) -> bool:
    value = getattr(contract, "day_trade", None)
    if value is None:
        return False
    text = str(value).lower()
    return "yes" in text or text in {"true", "1", "daytrade.yes"}


def get_stock_contracts(api: sj.Shioaji) -> List[Any]:
    contracts: List[Any] = []
    for exchange in ["TSE", "OTC"]:
        group = getattr(api.Contracts.Stocks, exchange, None)
        if group is None:
            continue
        for contract in group:
            if is_common_stock(contract) and is_day_tradeable(contract):
                contracts.append(contract)
    return contracts


def snapshot_batches(api: sj.Shioaji, contracts: List[Any], batch_size: int = 400):
    for i in range(0, len(contracts), batch_size):
        yield api.snapshots(contracts[i:i + batch_size])


def calc_turnover_twd(close_price: float, total_volume: float) -> float:
    # 台股 total_volume 通常是張，1 張 = 1000 股
    return close_price * total_volume * 1000


def calc_turnover_100m(close_price: float, total_volume: float) -> float:
    return calc_turnover_twd(close_price, total_volume) / 100000000


def score_candidate(change_rate: float, volume: float, turnover_100m: float, close_price: float, open_price: float) -> int:
    score = 0
    score += min(30, int(change_rate * 5))
    score += min(30, int(volume / 3000 * 8))
    score += min(30, int(turnover_100m / 3 * 10))
    if close_price > open_price:
        score += 10
    return min(score, 100)


def save_candidates(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "strategy": "MrPrice_opening_watchlist_v4",
        "generated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "candidates": rows,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def format_discord(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "【09:00 Mr.Price開盤觀察池】共 0 檔"

    lines = [f"【09:00 Mr.Price開盤觀察池】共 {len(rows)} 檔"]
    for i, r in enumerate(rows[:TOP_PRINT_N], 1):
        pressure_distance = r.get("距壓力%")
        support_distance = r.get("距支撐%")
        pressure_text = (
            f"{pressure_distance:+.2f}%"
            if isinstance(pressure_distance, (int, float))
            else "資料不足"
        )
        support_text = (
            f"-{support_distance:.2f}%"
            if isinstance(support_distance, (int, float))
            else "資料不足"
        )
        lines.extend([
            f"{i}. {r['股票代號']} {r['股票名稱']}｜漲幅 {r['漲幅']}%｜量 {int(r['成交量'])}｜成交值 {r['成交值(億)']}億｜強度 {r['強度分數']}",
            f"   壓力區 {r.get('近期壓力區', '資料不足')}（距離 {pressure_text}）｜{r.get('空間評估', '')}",
            f"   支撐區 {r.get('近期支撐區', '資料不足')}（距離 {support_text}）｜20日高低 {r.get('20日最高', '資料不足')}／{r.get('20日最低', '資料不足')}",
        ])
    lines.append("\n壓力支撐使用最近20個完整交易日；壓力過近只提示、不直接排除。")
    return "\n".join(lines)


def run() -> None:
    if not is_trade_day():
        msg = "今日台股未開盤，09:00 不執行選股。"
        print(msg)
        save_candidates([])
        return

    api = connect_shioaji()
    contracts = get_stock_contracts(api)
    print(f"=== 09:00 Mr.Price 開盤觀察池掃描，共 {len(contracts)} 檔 ===")

    rows: List[Dict[str, Any]] = []
    updated_at = now_tw().strftime("%Y-%m-%d %H:%M:%S")

    for snaps in snapshot_batches(api, contracts):
        for s in snaps:
            try:
                change_rate = float(s.change_rate)
                total_volume = float(s.total_volume)
                open_price = float(s.open)
                close_price = float(s.close)
            except Exception:
                continue

            if not (MIN_CHANGE_RATE <= change_rate <= MAX_CHANGE_RATE):
                continue
            if total_volume < MIN_VOLUME:
                continue
            if close_price <= open_price:
                continue

            turnover_twd = calc_turnover_twd(close_price, total_volume)
            if turnover_twd < MIN_TURNOVER_TWD:
                continue

            try:
                contract = api.Contracts.Stocks[s.code]
            except Exception:
                continue

            score = score_candidate(change_rate, total_volume, turnover_twd / 100000000, close_price, open_price)
            rows.append({
                "股票代號": s.code,
                "股票名稱": getattr(contract, "name", ""),
                "題材": "高周轉率/開盤強勢",
                "漲幅": round(change_rate, 2),
                "成交量": total_volume,
                "成交值(億)": round(turnover_twd / 100000000, 2),
                "開盤價": open_price,
                "目前價": close_price,
                "是否可當沖": "是",
                "強度分數": score,
                "更新時間": updated_at,
            })

    rows.sort(key=lambda r: (float(r["漲幅"]), float(r["成交值(億)"]), float(r["成交量"])), reverse=True)
    rows = rows[:TOP_PRINT_N]

    # 僅對最後入選的標的計算近20個交易日壓力支撐，避免全市場大量呼叫。
    for row in rows:
        try:
            contract = api.Contracts.Stocks[str(row["股票代號"])]
            row.update(
                get_support_resistance_info(
                    api,
                    contract,
                    float(row["目前價"]),
                )
            )
        except Exception as exc:
            print(f"計算壓力支撐失敗 {row.get('股票代號', '')}: {exc}")
            row.update({
                "近期壓力區": "資料不足",
                "近期支撐區": "資料不足",
                "20日最高": "資料不足",
                "20日最低": "資料不足",
                "距壓力%": None,
                "距支撐%": None,
                "空間評估": "資料不足",
            })

    save_candidates(rows)

    for r in rows:
        print(f"{r['股票代號']} {r['股票名稱']} 漲幅:{r['漲幅']} 量:{r['成交量']} 成交值:{r['成交值(億)']}億 強度:{r['強度分數']}")

    discord_send(format_discord(rows))
    print(f"=== 09:00 Mr.Price 開盤觀察池完成，共 {len(rows)} 檔 ===")


if __name__ == "__main__":
    run()
