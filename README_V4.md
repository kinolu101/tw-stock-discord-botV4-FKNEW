# TW Stock Discord Bot V4

V4 是雲端版：

- 不需要 Google Sheets
- 不需要 service_account.json
- 不需要 Railway
- 不需要 Oracle Cloud
- 使用 GitHub Actions 每個開盤日台灣時間 09:00 自動執行
- 09:00 產生候選股 `candidates_0900.json`
- 同一個工作流程等待到 09:05 後篩選突破股
- 09:05 將 TOP 2 發送到 Discord Webhook

## GitHub Secrets 必填

到：

Settings → Secrets and variables → Actions → New repository secret

新增：

```text
DISCORD_WEBHOOK
SHIOAJI_API_KEY
SHIOAJI_SECRET_KEY
SHIOAJI_SIMULATION
```

`SHIOAJI_SIMULATION` 建議填：

```text
false
```

## 可選 Variables

到：

Settings → Secrets and variables → Actions → Variables

可新增：

```text
TWSE_HOLIDAYS
```

格式：

```text
2026-01-01,2026-02-16
```

## 檔案說明

```text
run_0900_v4.py
run_0905_v4.py
requirements.txt
.github/workflows/tw_stock_v4.yml
```

## 手動測試

到 GitHub 專案上方 Actions：

```text
TW Stock Discord Bot V4
→ Run workflow
```

手動執行一次。

注意：非 09:00～09:05 時段手動跑，可能因即時行情或第一根 5 分 K 資料不足而沒有結果，這是正常的。
