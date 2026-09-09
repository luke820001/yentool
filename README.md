# YenTool

台股盤後掃描與持倉追蹤。每個交易日盤後由 GitHub Actions 自動掃描，結果發布到 GitHub Pages，
手機以 PWA open 即可查看；桌面另有 Tkinter 介面。

> **現行規格的單一入口是 [`docs/TASKS.md`](docs/TASKS.md)。**
> 設計與缺陷分析在 [`docs/專案完整分析與優化方案_2026-09-09.md`](docs/專案完整分析與優化方案_2026-09-09.md)，
> 策略與其證據在 [`docs/STRATEGY.md`](docs/STRATEGY.md)。
> `docs/history/` 是已被取代的舊結論，閱讀時請注意適用版本。

## 專案結構

```
scan_headless.py     排程入口（GitHub Actions 跑這支）
main_gui.py          桌面入口

ingestion/           抓資料：價量、法人、TDCC、大盤
analyzer/            指標：起漲分、動能分、蓄勢分、趨勢與支撐壓力
scanner/             選股主流程：市場初篩 → 籌碼驗證 → 模式篩選 → 排名 →
                     持倉標註 → 買進資格 → 匯出
portfolio/           交易帳本：首日建議、真實成交、逐日估值、D10 結算
storage/             SQLite 存取層
gui/                 桌面介面（Tkinter）
mobile/              手機 PWA（靜態檔，Pages 直接發布這個資料夾）
gemini_hook/         AI 摘要（Gemini 主、Groq 備、本地摘要保底）
config/              路徑與參數

tests/               驗收測試（stdlib unittest，不需額外安裝）
tools/               維運腳本：資料稽核、回填、研究庫建置、發布前檢查
archive/             封存區，不參與掃描 —— 執行方式見 archive/README.md
docs/                現行規格、待辦與歷史紀錄
data/                資料庫與輸出（不進版控，見下）
```

## 兩本帳，不要混用

這是 2026-09-09 改版的核心，**把「系統說了什麼」和「你真的買了什麼」分開**：

| | `data/signal_ledger.db` | `data/portfolio_ledger.db` |
|---|---|---|
| 記什麼 | 每次掃描的名單與事後的模擬報酬 | 固定的首日建議、真實成交、逐日損益、D10 結算 |
| 誰寫入 | 掃描程式 | **只有使用者登錄成交**才會產生持倉 |
| 可否公開 | 可以 | 建議部分可以；**持倉與成交不可** |

`positions` / `executions` 只會因為有人記錄真實交易而產生，排程不會寫入。
發布前由 `tools/check_ledger_public.py` 把關：帳本裡只要出現真實成交就拒絕提交，
因為 public repo 的 git 歷史刪不掉（報告 §9.2）。

四種價格是四件事，不可共用同一欄（報告 §5.1）：

- **首日建議價** `Initial_Buy_Price` — 第一次通過完整買進規則時固定，之後永不改寫。
- **最新觀察參考** `Suggested_Buy_Price` — 每天依收盤重算，可以變。
- **實際成交價／平均成本** — 只有成交紀錄能建立，掃描不能覆寫。
- **當前有效停損／停利** — 依成交成本初始化，之後由事件更新。

## 執行

```bash
pip install -r requirements.txt
python scan_headless.py mode_prelaunch    # 一次完整掃描並發布
python main_gui.py                        # 桌面介面
python mobile/serve.py                    # 本機預覽手機版
```

`scan_headless.py` 的離開碼是有意義的，CI 依此區分三種結果：

| 碼 | 意思 |
|---:|---|
| 0 | 掃描完成並已發布 —— **包含「今天沒有任何標的合格」這種正常結果** |
| 1 | 抓不到行情，沒有東西可發布，保留前一次資料 |
| 2 | 掃描本身出錯 |

## 測試

```bash
python -m unittest discover -s tests -v
```

不需要 pytest：CI 只安裝 `requirements.txt`，所以測試一律用標準函式庫的 unittest。
測試內容直接取自報告的數字而非自己編的：§6.2 的十日損益表、§6.3 的費稅
（145.35 / 159.60 / 336.00 → 淨獲利 9,359.05）、§12 的驗收情境。

## 部署

`.github/workflows/scan.yml` 於台灣時間 14:30 / 17:00 / 18:00 執行，然後：

1. 發布 `mobile/` 到 GitHub Pages（含 `scan_result.json` 與 `quotes.json`）。
2. 只提交會累積的小檔案：兩本帳本、股名快取、名單留榜狀態。
3. 大型行情資料庫走 `actions/cache`，不進版控。

`quotes.json` 是持倉估值的來源：它涵蓋「今日名單 ∪ 帳本追蹤中的股票」，
所以一檔股票掉出名單之後，手機仍然有它的收盤價可以繼續算損益（報告 F04）。

## data/ 與版控

`.gitignore` 有 `data/`，但 **`.gitignore` 只影響尚未被追蹤的檔案** ——
`price_volume.db`(87MB)、`inst_trades.db`(11MB) 等在加入 ignore 之前就已進版控，
所以仍然被追蹤，`.git` 目前約 73MB。要不要 `git rm --cached` 讓它們脫離版控，
以及那份 447MB 且已失效的 `_feat_cache.pkl` 要不要刪，
兩件事的取捨都記在 [`docs/TASKS.md`](docs/TASKS.md) 的「待擁有者決定」。

## 已知限制

不要把下面幾點當成已經解決：

- **勝率數字未經新資料驗證。** 舊文件的「順風年約 71%、六年約 64%」是先前特定樣本與
  規則下的研究結果；2026-09-09 的稽核發現執行假設與版本落差，尚未重新驗證。
- **價格基準未分離（F08）。** `price_volume.db` 混有 yfinance 還原價與交易所未還原價，
  且沒有來源欄位，所以 `quotes.json` 的 `price_basis` 標為 `unverified`，不可直接與券商對帳。
- **持倉存在手機本機。** 目前沒有私人後端，換裝置或清除瀏覽器資料會遺失，請使用匯出功能備份。
