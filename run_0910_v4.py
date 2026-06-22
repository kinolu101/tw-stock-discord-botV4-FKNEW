
# -*- coding: utf-8 -*-
"""
Mr.Price 開盤策略共用邏輯說明：
- 量能 5MA / 60MA 黃金交叉：用 5 分 K 成交量均線判斷
- 多頭排列：優先用 10MA > 20MA > 30MA > 60MA；資料不足時用 5MA > 10MA > 20MA 簡化
- 均價線：用當日 VWAP 近似
- 爆量 K 高點：第一根 5 分 K 的 high
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
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "1.0"))
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "6.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "1000"))
MIN_TURNOVER_TWD = float(os.getenv("MIN_TURNOVER_TWD", "80000000"))
TOP_N = int(os.getenv("TOP_N", "3"))
TOP_PRINT_N = int(os.getenv("TOP_PRINT_N", "20"))
MAX_KBAR_CHECK = int(os.getenv("MAX_KBAR_CHECK", "40"))
ALLOW_NEAR_BULL = os.getenv("ALLOW_NEAR_BULL", "true").lower() in {"1", "true", "yes"}
REQUIRE_CLOSE_BREAK_FIRST_HIGH = os.getenv("REQUIRE_CLOSE_BREAK_FIRST_HIGH", "true").lower() in {"1", "true", "yes"}

TWSE_HOLIDAYS = {
    d.strip()
    for d in os.getenv("TWSE_HOLIDAYS", "").split(",")
    if d.strip()
}

EXCLUDE_KY = True
EXCLUDE_ETF = True
EXCLUDE_FINANCIAL = os.getenv("EXCLUDE_FINANCIAL", "false").lower() in {"1", "true", "yes"}



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


def is_day_tradeable(contract: Any) -> bool:
    value = getattr(contract, "day_trade", None)
    if value is None:
        return False
    text = str(value).lower()
    return "yes" in text or text in {"true", "1", "daytrade.yes"}


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
    return close_price * total_volume * 1000


def calc_turnover_100m(close_price: float, total_volume: float) -> float:
    return calc_turnover_twd(close_price, total_volume) / 100000000


def load_candidates() -> List[Dict[str, Any]]:
    if not os.path.exists(INPUT_FILE):
        return []
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("candidates", [])


def build_snapshot_candidates(api: sj.Shioaji, limit: int = MAX_KBAR_CHECK) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    contracts = get_stock_contracts(api)
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
            })
    rows.sort(key=lambda r: (float(r["漲幅"]), float(r["成交值(億)"]), float(r["成交量"])), reverse=True)
    return rows[:limit]


def get_contract(api: sj.Shioaji, code: str) -> Optional[Any]:
    try:
        return api.Contracts.Stocks[code]
    except Exception:
        return None


def kbars_to_df(kbars: Any) -> Optional[pd.DataFrame]:
    try:
        ts = getattr(kbars, "ts")
        df = pd.DataFrame({
            "ts": pd.to_datetime(ts),
            "open": getattr(kbars, "Open"),
            "high": getattr(kbars, "High"),
            "low": getattr(kbars, "Low"),
            "close": getattr(kbars, "Close"),
            "volume": getattr(kbars, "Volume"),
        })
        amount = getattr(kbars, "Amount", None)
        if amount is not None:
            df["amount"] = amount
        else:
            df["amount"] = df["close"] * df["volume"]
    except Exception as exc:
        print(f"kbars 轉 DataFrame 失敗：{exc}")
        return None
    if df.empty:
        return None
    if df["ts"].dt.tz is not None:
        df["ts"] = df["ts"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None)
    return df.sort_values("ts")


def get_5m_bars(api: sj.Shioaji, contract: Any, days_back: int = 14) -> Optional[pd.DataFrame]:
    end_date = now_tw().date()
    start_date = end_date - timedelta(days=days_back)
    try:
        kbars = api.kbars(contract=contract, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"))
    except Exception as exc:
        print(f"取得 kbars 失敗 {getattr(contract, 'code', '')}: {exc}")
        return None
    raw = kbars_to_df(kbars)
    if raw is None or raw.empty:
        return None
    raw = raw.set_index("ts")
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }
    bars = raw.resample("5min", label="left", closed="left").agg(agg).dropna()
    bars = bars[bars["volume"] > 0]
    if bars.empty:
        return None
    return bars


def get_bar_at(bars: pd.DataFrame, hour: int, minute: int) -> Optional[pd.Series]:
    idx = datetime.combine(now_tw().date(), datetime.min.time()).replace(hour=hour, minute=minute)
    try:
        row = bars.loc[idx]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        return row
    except KeyError:
        return None


def volume_golden_cross(bars: pd.DataFrame, current_time: datetime) -> bool:
    if len(bars) < 62:
        return False
    vol = bars["volume"].astype(float)
    vma5 = vol.rolling(5).mean()
    vma60 = vol.rolling(60).mean()
    try:
        pos = bars.index.get_loc(current_time)
        if isinstance(pos, slice):
            pos = pos.stop - 1
        if isinstance(pos, (list, tuple)):
            pos = pos[-1]
    except Exception:
        return False

    def crossed_at(i: int) -> bool:
        if i <= 0 or pd.isna(vma5.iloc[i]) or pd.isna(vma60.iloc[i]) or pd.isna(vma5.iloc[i - 1]) or pd.isna(vma60.iloc[i - 1]):
            return False
        return vma5.iloc[i] > vma60.iloc[i] and vma5.iloc[i - 1] <= vma60.iloc[i - 1]

    # 影片提到：昨天最後一盤或今天 9:00 其中一根出現量能黃金交叉都可關注
    return crossed_at(pos) or crossed_at(pos - 1)


def price_trend_state(bars: pd.DataFrame, current_time: datetime) -> Tuple[bool, str, Dict[str, Optional[float]]]:
    close = bars["close"].astype(float)
    ma: Dict[str, Optional[float]] = {}
    for n in [5, 10, 20, 30, 60]:
        ma[f"ma{n}"] = float(close.rolling(n).mean().loc[current_time]) if len(close.loc[:current_time]) >= n else None

    ma5, ma10, ma20, ma30, ma60 = ma["ma5"], ma["ma10"], ma["ma20"], ma["ma30"], ma["ma60"]
    if ma10 is not None and ma20 is not None and ma30 is not None and ma60 is not None and ma10 > ma20 > ma30 > ma60:
        return True, "多頭排列10>20>30>60 ✅", ma
    if ALLOW_NEAR_BULL and ma5 is not None and ma10 is not None and ma20 is not None and ma5 > ma10 > ma20:
        return True, "短線多頭5>10>20 ✅", ma
    return False, "趨勢未多頭 ⚠️", ma


def calc_vwap_today(bars: pd.DataFrame, until_time: datetime) -> Optional[float]:
    today_start = datetime.combine(now_tw().date(), datetime.min.time()).replace(hour=9, minute=0)
    day = bars[(bars.index >= today_start) & (bars.index <= until_time)]
    if day.empty or day["volume"].sum() <= 0:
        return None
    return float(day["amount"].sum() / day["volume"].sum())


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


def calc_trade_prices(entry_base: float, first_low: float) -> Tuple[float, float, float, float]:
    entry = round_up_tick(entry_base)
    stop = round_down_tick(min(first_low, entry * 0.985))
    tp1 = round_down_tick(entry * 1.02)
    tp2 = round_down_tick(entry * 1.04)
    return entry, stop, tp1, tp2


def score_stock(change_rate: float, total_volume: float, turnover_100m: float, volume_cross: bool, trend_ok: bool, above_vwap: bool, breakout: bool) -> int:
    score = 0
    score += min(20, int(change_rate * 4))
    score += min(20, int(total_volume / 3000 * 6))
    score += min(20, int(turnover_100m / 3 * 8))
    score += 15 if volume_cross else 0
    score += 15 if trend_ok else 0
    score += 10 if above_vwap else 0
    score += 20 if breakout else 0
    return min(score, 100)

OUTPUT_FILE = os.getenv("BREAKOUTS_FILE", "mrprice_0910_breakouts.json")


def save_results(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "strategy": "MrPrice_0910_second_bar_breakout_v4",
        "generated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "results": rows,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def format_discord(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "【09:10 Mr.Price突破爆量K】今日無符合突破條件標的。"
    lines = [f"【09:10 Mr.Price突破爆量K TOP {len(results)}】"]
    for i, r in enumerate(results, 1):
        lines.extend([
            "",
            f"{i}. {r['股票代號']} {r['股票名稱']}｜漲幅 {r['漲幅']}%｜成交值 {r['成交值(億)']}億",
            f"條件：{r['突破條件']}｜量能：{r['量能黃金交叉']}｜趨勢：{r['趨勢狀態']}｜均價線：{r['均價線狀態']}",
            f"第一根高點：{r['第一根高點']}｜第二根高點：{r['第二根高點']}｜第二根收盤：{r['第二根收盤']}",
            f"壓力區：{r.get('近期壓力區', '資料不足')}｜支撐區：{r.get('近期支撐區', '資料不足')}｜{r.get('空間評估', '')}",
            f"20日高低：{r.get('20日最高', '資料不足')}／{r.get('20日最低', '資料不足')}｜距壓力：{r.get('距壓力%', '資料不足')}%",
            f"進場：{r['進場價']}｜停損：{r['停損價']}｜停利1：{r['停利1']}｜停利2：{r['停利2']}｜強度 {r['強度分數']}",
        ])
    return "\n".join(lines)


def run() -> None:
    if not is_trade_day():
        print("今日台股未開盤，09:10 不執行選股。")
        return
    api = connect_shioaji()
    candidates = load_candidates()
    if not candidates:
        print("找不到 candidates_0900.json，改用即時 snapshot 重建觀察池。")
        candidates = build_snapshot_candidates(api, MAX_KBAR_CHECK)
    else:
        candidates = candidates[:MAX_KBAR_CHECK]

    print(f"=== 09:10 Mr.Price 第二根5分K突破確認，共 {len(candidates)} 檔 ===")
    results: List[Dict[str, Any]] = []
    first_time = datetime.combine(now_tw().date(), datetime.min.time()).replace(hour=9, minute=0)
    second_time = datetime.combine(now_tw().date(), datetime.min.time()).replace(hour=9, minute=5)

    for old in candidates:
        code = str(old.get("股票代號", "")).strip()
        contract = get_contract(api, code)
        if contract is None or not is_day_tradeable(contract):
            continue
        bars = get_5m_bars(api, contract)
        if bars is None:
            continue
        first = get_bar_at(bars, 9, 0)
        second = get_bar_at(bars, 9, 5)
        if first is None or second is None:
            continue

        volume_cross = volume_golden_cross(bars, first_time)
        trend_ok, trend_text, ma = price_trend_state(bars, second_time)
        vwap = calc_vwap_today(bars, second_time)

        first_high = float(first["high"])
        first_low = float(first["low"])
        first_close = float(first["close"])
        first_vol = float(first["volume"])
        second_high = float(second["high"])
        second_close = float(second["close"])
        second_vol = float(second["volume"])

        above_vwap = vwap is not None and second_close >= vwap
        if REQUIRE_CLOSE_BREAK_FIRST_HIGH:
            breakout = second_close > first_high
            breakout_text = "第二根收盤突破第一根爆量K高點 ✅"
            entry_base = second_close
        else:
            breakout = second_high > first_high
            breakout_text = "第二根高點突破第一根爆量K高點 ✅"
            entry_base = first_high

        if not volume_cross:
            continue
        if not trend_ok:
            continue
        if not above_vwap:
            continue
        if not breakout:
            continue
        if second_vol <= 0:
            continue

        try:
            change_rate = float(old.get("漲幅", 0))
            total_volume = float(old.get("成交量", 0))
            turnover_100m = float(old.get("成交值(億)", 0))
        except Exception:
            continue

        entry, stop, tp1, tp2 = calc_trade_prices(entry_base, first_low)
        score = score_stock(change_rate, total_volume, turnover_100m, volume_cross, trend_ok, above_vwap, breakout)

        level_info = {
            key: old.get(key)
            for key in [
                "近期壓力中心", "近期壓力區",
                "近期支撐中心", "近期支撐區",
                "20日最高", "20日最低",
                "距壓力%", "距支撐%", "空間評估",
            ]
            if old.get(key) is not None
        }
        if not level_info or level_info.get("近期壓力區") in {None, "資料不足"}:
            level_info = get_support_resistance_info(api, contract, second_close)

        result = {
            "股票代號": code,
            "股票名稱": getattr(contract, "name", old.get("股票名稱", "")),
            "漲幅": round(change_rate, 2),
            "成交量": total_volume,
            "成交值(億)": round(turnover_100m, 2),
            "量能黃金交叉": "是 ✅",
            "趨勢狀態": trend_text,
            "均價線狀態": "站上VWAP ✅",
            "VWAP": round(vwap, 2) if vwap is not None else "資料不足",
            "突破條件": breakout_text,
            "第一根高點": round(first_high, 2),
            "第一根低點": round(first_low, 2),
            "第一根收盤": round(first_close, 2),
            "第一根量": first_vol,
            "第二根高點": round(second_high, 2),
            "第二根收盤": round(second_close, 2),
            "第二根量": second_vol,
            "進場價": entry,
            "停損價": stop,
            "停利1": tp1,
            "停利2": tp2,
            "強度分數": score,
            "更新時間": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        }
        result.update(level_info)
        results.append(result)

    results.sort(key=lambda r: (int(r["強度分數"]), float(r["成交值(億)"]), float(r["成交量"])), reverse=True)
    top_results = results[:TOP_N]
    save_results(top_results)
    discord_send(format_discord(top_results))
    print(f"=== 09:10 Mr.Price 第二根5分K突破完成，共 {len(top_results)} 檔 ===")


if __name__ == "__main__":
    run()
