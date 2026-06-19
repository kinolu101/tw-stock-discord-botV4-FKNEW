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
        lines.append(
            f"{i}. {r['股票代號']} {r['股票名稱']}｜漲幅 {r['漲幅']}%｜量 {int(r['成交量'])}｜成交值 {r['成交值(億)']}億｜強度 {r['強度分數']}"
        )
    lines.append("\n後續重點：09:05 確認第一根5分K量能，09:10 看第二根是否突破第一根爆量K高點。")
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
    save_candidates(rows)

    for r in rows:
        print(f"{r['股票代號']} {r['股票名稱']} 漲幅:{r['漲幅']} 量:{r['成交量']} 成交值:{r['成交值(億)']}億 強度:{r['強度分數']}")

    discord_send(format_discord(rows))
    print(f"=== 09:00 Mr.Price 開盤觀察池完成，共 {len(rows)} 檔 ===")


if __name__ == "__main__":
    run()
