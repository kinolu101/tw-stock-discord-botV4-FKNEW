"""
09:00 台股開盤候選股掃描器 V4 Cloud

V4 重點：
- 移除 Google Sheets / service_account.json
- 使用 Shioaji 掃描 09:00 候選股
- 結果寫入 candidates_0900.json，供 09:05 使用
- 可選擇發送 09:00 候選摘要到 Discord
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
import shioaji as sj
from dotenv import load_dotenv

TZ = ZoneInfo("Asia/Taipei")

OUTPUT_FILE = os.getenv("CANDIDATES_FILE", "candidates_0900.json")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "1.0"))
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "9.0"))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "3000"))
MIN_TURNOVER_TWD = float(os.getenv("MIN_TURNOVER_TWD", "300000000"))

EXCLUDE_KY = True
EXCLUDE_ETF = True
EXCLUDE_FINANCIAL = True
THEME_ONLY = True
TOP_PRINT_N = int(os.getenv("TOP_PRINT_N", "20"))

# 休市日可用 GitHub Secret / Variable 設定，例如：2026-01-01,2026-02-16
TWSE_HOLIDAYS = {
    d.strip()
    for d in os.getenv("TWSE_HOLIDAYS", "").split(",")
    if d.strip()
}

THEME_STOCKS: Dict[str, str] = {
    "3231": "AI伺服器",
    "2382": "AI伺服器 / 雲端",
    "6669": "AI伺服器 / 雲端資料中心",
    "2356": "AI伺服器 / 代工補漲",
    "2324": "AI伺服器 / 代工補漲",
    "2317": "AI伺服器 / 電子權值",
    "2376": "AI伺服器 / GPU / 主機板",
    "2377": "AI伺服器 / 主機板",
    "2388": "AI伺服器 / 電源",
    "3706": "AI伺服器 / 工業電腦",

    "2308": "電源 / AI伺服器",
    "3017": "AI散熱",
    "3324": "AI散熱",
    "3653": "AI散熱 / 機構件",
    "3483": "AI散熱",
    "2421": "風扇 / 散熱",

    "3661": "ASIC / AI晶片",
    "3443": "ASIC / AI晶片",
    "3035": "ASIC / IC設計",
    "2454": "IC設計 / AI晶片",
    "2330": "半導體權值",
    "2303": "半導體權值",
    "2363": "記憶體 / 半導體",
    "3444": "記憶體 / 半導體",
    "8299": "半導體設備 / 先進封裝",
    "3167": "半導體設備",
    "3583": "半導體設備",

    "2383": "高速材料 / CCL",
    "3037": "ABF載板 / PCB",
    "8046": "PCB / 載板",
    "3189": "PCB / 載板",
    "2368": "PCB / HDI",
    "6274": "PCB / 設備",
    "2313": "PCB / 連接器",
    "3533": "連接器 / 高速傳輸",
    "6412": "高速傳輸 / 訊號完整性",
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
    if THEME_ONLY and code not in THEME_STOCKS:
        return False
    return True


def is_day_tradeable(contract: Any) -> bool:
    value = getattr(contract, "day_trade", None)
    if value is None:
        return False
    text = str(value).lower()
    return "yes" in text or text in {"true", "1", "daytrade.yes"}


def get_stock_contracts(api: sj.Shioaji) -> List[Any]:
    contracts = []
    for exchange in ["TSE", "OTC"]:
        group = getattr(api.Contracts.Stocks, exchange, None)
        if group is None:
            continue
        for contract in group:
            if is_common_stock(contract) and is_day_tradeable(contract):
                contracts.append(contract)
    return contracts


def get_taiex_change_rate(api: sj.Shioaji) -> Optional[float]:
    try:
        idx = api.Contracts.Indexs.TSE["001"]
        snap = api.snapshots([idx])[0]
        return float(snap.change_rate)
    except Exception as exc:
        print(f"取得大盤漲跌幅失敗：{exc}")
        return None


def snapshot_batches(api: sj.Shioaji, contracts: List[Any], batch_size: int = 400):
    for i in range(0, len(contracts), batch_size):
        yield api.snapshots(contracts[i:i + batch_size])


def calc_turnover_100m(close_price: float, total_volume: float) -> float:
    return close_price * total_volume * 1000 / 100000000


def score_candidate(change_rate: float, volume: float, turnover_100m: float, theme: str) -> int:
    score = 0
    score += min(35, int(change_rate * 5))
    score += min(30, int(volume / 3000 * 8))
    score += min(25, int(turnover_100m / 3 * 12))
    if any(k in theme for k in ["AI", "ASIC", "散熱", "高速材料", "ABF"]):
        score += 10
    return min(score, 100)


def save_candidates(rows: List[Dict[str, Any]]) -> None:
    payload = {
        "generated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "candidates": rows,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def run() -> None:
    if not is_trade_day():
        msg = "今日台股未開盤，09:00 不執行選股。"
        print(msg)
        save_candidates([])
        return

    api = connect_shioaji()
    taiex_change_rate = get_taiex_change_rate(api)

    if taiex_change_rate is not None and taiex_change_rate < -1.5:
        msg = f"【09:00 候選股】大盤目前 {taiex_change_rate:.2f}%，大盤過弱，今日暫不推薦做多候選。"
        print(msg)
        save_candidates([])
        discord_send(msg)
        return

    contracts = get_stock_contracts(api)
    print(f"=== 09:00 V4 主流題材候選股開始掃描，共 {len(contracts)} 檔 ===")

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
            if total_volume <= MIN_VOLUME:
                continue
            if close_price <= open_price:
                continue

            turnover_100m = calc_turnover_100m(close_price, total_volume)
            if turnover_100m * 100000000 < MIN_TURNOVER_TWD:
                continue

            try:
                contract = api.Contracts.Stocks[s.code]
            except Exception:
                continue

            name = getattr(contract, "name", "")
            theme = THEME_STOCKS.get(s.code, "主流題材")
            score = score_candidate(change_rate, total_volume, turnover_100m, theme)

            rows.append({
                "股票代號": s.code,
                "股票名稱": name,
                "題材": theme,
                "漲幅": change_rate,
                "成交量": total_volume,
                "成交值(億)": round(turnover_100m, 2),
                "開盤價": open_price,
                "目前價": close_price,
                "是否可當沖": "是",
                "強度分數": score,
                "更新時間": updated_at,
            })

    rows.sort(key=lambda r: (int(r["強度分數"]), float(r["成交值(億)"]), float(r["成交量"])), reverse=True)
    save_candidates(rows)

    for r in rows[:TOP_PRINT_N]:
        print(
            f"候選 {r['股票代號']} {r['股票名稱']} 題材:{r['題材']} "
            f"漲幅:{r['漲幅']} 成交量:{r['成交量']} 成交值:{r['成交值(億)']}億 強度:{r['強度分數']}"
        )

    lines = [f"【09:00 候選股】共 {len(rows)} 檔"]
    for i, r in enumerate(rows[:5], 1):
        lines.append(
            f"{i}. {r['股票代號']} {r['股票名稱']}｜{r['題材']}｜"
            f"漲幅 {r['漲幅']}%｜量 {int(r['成交量'])}｜強度 {r['強度分數']}"
        )
    discord_send("\n".join(lines))
    print(f"=== 09:00 V4 完成，共 {len(rows)} 筆 ===")


if __name__ == "__main__":
    run()
