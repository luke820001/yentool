# YenTool 專案完整健檢報告

> **處理狀態（2026-09-20 當天，commit 0a840c6 / 3fd98ff / e4c0058 / 18c82d0）**
> 每一項都先驗證過再決定，下面是結果；報告原文保留不改。
>
> | 項目 | 狀態 | 實際做法 |
> |---|---|---|
> | AUD-001 | ✅ 已修（但歸因不同） | 那 21 項契約錯誤全部來自**版本庫裡那份 2026-07-10 的舊檔**，手機讀的 Pages 版本通過 `--strict`。真正的風險是 scan.yml 的 Pages 上傳沒有條件：行情抓不到（exit 1）那一輪會把七月的 checkout 上傳到手機。已改成上傳與部署都要 `published == 'true'`，兩個 payload 檔不再納入版本控管 |
> | AUD-002 | ✅ 已修 | 先實測六個端點在**開啟驗證**下全部 200，確認繞過沒有任何必要，再移除 `verify=False`、`disable_warnings` 與 `verify_ssl` 參數 |
> | AUD-003 | ⏸ 不修（非缺陷） | 雲端是權威、電腦常關著，本機資料落後是設計結果。單人操作不需要 generation manifest |
> | AUD-004 | ✅ 已修 | `.github/workflows/tests.yml`：push/PR 在 Python 3.12 跑全套測試 + `pip check` + compileall + 發布契約 |
> | AUD-005 | ❓ 待你決定 | 你先前已明確拒絕 PAT 流程，這段很可能是死程式碼；但它是使用者可見功能，不自行移除 |
> | AUD-006 | ✅ 已修 | `constraints.txt` 鎖定實測通過的版本（CI 當時解析到 yfinance 1.7.0，本機是 1.4.1），兩個工作流程都用 `-c` 安裝 |
> | AUD-007 | ⏸ 待你決定 | 報告沒考慮到：被追蹤的 DB 是 **Actions 快取失效時的冷啟動備援**。拿掉後冷抓約 2000 檔既慢又會撞頻率限制 |
> | AUD-008 | ✅ 已修 | 摘要拆成 hard-valid 100% / signal-ready 99.1% / research-ready 77.1% |
> | AUD-009 | 🔸 部分完成 | 新增 `tests/test_ingestion_parsing.py`（12 項 mock HTTP 解析測試），測試數 155 → 167。GUI 與 PWA 的自動化測試仍未做 |
>
> 同日另外自行發現並修掉、報告未涵蓋的缺陷：yfinance 對「尚未開盤的交易日」回傳的佔位 K 棒
> （帶成交量，所以零成交量過濾抓不到）污染交易日曆、報價檔與**停損回放**，見 CHANGELOG 2026-09-20。


- 檢測日期：2026-09-20（Asia/Taipei）
- 檢測環境：Windows 11、CPython 3.13.5
- 專案根目錄：`D:\YenTool`
- 結論：**程式核心單元測試全數通過，但當前提交的手機發布資料不符現行契約，且資料日期彼此不一致。此版本不應被視為可信任的交易資料發布基線。**

## 1. 摘要

| 等級 | 數量 | 重點 |
|---|---:|---|
| P0 緊急 | 1 | 已提交的 PWA 掃描資料有 21 類契約錯誤，與報價檔跨月混用 |
| P1 高 | 4 | TLS 驗證被關閉、資料日期分裂、CI 不跑測試、手機長期儲存 GitHub 寫入 Token |
| P2 中 | 4 | 依賴不可重現、大型 DB 仍被追蹤、資料稽核摘要誤導、測試範圍缺口 |

最優先順序：

1. 先停止把現有 `mobile/scan_result.json` 當成可用產品資料，重新產生同一批次的 scan/quotes/recommendations，直到嚴格稽核通過。
2. 移除所有 `verify=False` 與關閉 TLS 警告的程式。
3. 在 PR/push 加入 Python 3.12 的單元測試、發布契約測試與前端測試，不要等每日掃描才發現問題。

## 2. 已確認問題

### AUD-001（P0）當前發布資料與現行契約不相容

**證據**

- `python tools/check_scan_result.py --no-write --strict` 回傳離開碼 3：`status=fail`，36 列、81 欄、21 類 error、12 類 warning。
- 15 個手機必讀欄位缺失，包括 `Buy_Ready`、`Buy_Block`、`Initial_Buy_Price`、`Recommendation_ID`、`Plan_Stop`、`Exit_Signal` 等。
- 36/36 列的 `Strict_Stop_Loss` 與 `Risk_Pct` 不符現行 prelaunch 規則，3 列 `Core_Plus` 不符門檻。
- 36/36 列的掃描收盤價與 `quotes.json` 不同。
- `mobile/scan_result.json:1` 的掃描時間是 2026-07-10，列資料日是 2026-07-09，而 `mobile/quotes.json:1` 是 2026-09-09；掃描 meta 又沒有 `data_date`。
- 當前仍有 30 列顯示進場/持有狀態，卻沒有 `Entry_Open`。

**影響**

手機頁面可能顯示過期或互相矛盾的價格、停損、持有狀態與買進資格。這是交易語意級錯誤，不只是畫面展示問題。

**建議**

- 使用同一次掃描同時產生 `scan_result.json`、`quotes.json`、`recommendations.json`，加入共用 `generation_id`/`data_date`。
- 建立一份符合現行 schema 的「空結果」基線，不要在 repo 保留舊 schema 真實選股結果。
- PR/push 必須執行 `check_scan_result.py --no-write --strict`；每日排程可以顯示失敗報告，但不應把失敗產物標示成可用交易資料。

### AUD-002（P1）四個資料擷取路徑關閉 TLS 憑證驗證

**位置**

- `ingestion/tdcc_holders.py:26,41`
- `ingestion/tdcc_history.py:37,68,82`
- `ingestion/inst_trades.py:16,53,92`
- `ingestion/price_volume_multi.py:15,160`

**問題**

程式不只使用 `verify=False`，還全域關閉 `InsecureRequestWarning`。中間人、DNS/代理污染或被篡改回應都可能被當成正式行情寫入 SQLite，後續指標和選股結果也會被污染。

**建議**

- 恢復預設憑證驗證，必要時只指定可審核的 CA bundle，不要關閉驗證。
- 對狀態碼、Content-Type、schema、日期、筆數與數值邊界做 fail-closed 驗證。
- 增加 mocked HTTP 測試，包括憑證錯誤、HTML 假冒 JSON/CSV、超時、半邊市場更新成功等情境。

### AUD-003（P1）資料庫、帳本與發布檔的日期已分裂

**實測日期**

| 資料 | 最新日期 |
|---|---|
| `mobile/scan_result.json` rows | 2026-07-09 |
| `mobile/quotes.json` | 2026-09-09 |
| `price_volume.db` | 2026-09-09 |
| `inst_trades.db` | 2026-09-09 |
| `taiex.db` | 2026-09-09 |
| `large_holder.db` / `tdcc_history.db` | 2026-09-04 |
| `signal_ledger.db` picks | 2026-09-18 |
| `signal_ledger.db` outcomes as-of | 2026-09-18 |

`signal_ledger` 比本機行情庫新 9 天，手機掃描又比報價舊兩個月，因此這個 checkout 無法從任一組本機資料完整重現當前帳本/發布狀態。

另外，內建稽核發現：

- 425 檔有內部交易日缺口，共缺 4,378 bars。
- 11 檔近 60 bars 有超過 10.5% 的收盤跳動，會影響現行 MA/突破。
- 6 檔少於 60 bars，69 檔少於 240 bars。

**建議**

- 為每個產物記錄 source snapshot ID、各資料源 as-of 與生成版本，不只記一個總 `data_date`。
- 發布前驗證「行情、法人、大盤、TDCC、ledger、quotes、scan rows」的可接受日期關係。
- 重建一致快照後才重算帳本 outcomes；現有回測/勝率不應作為對外結論。

### AUD-004（P1）CI 不會在 push/PR 執行已有的 155 項測試

`.github/workflows/scan.yml:12-45` 只有 schedule 與 manual dispatch；workflow 安裝依賴後直接執行生產掃描，沒有 `python -m unittest discover -s tests -v`。全個 `.github/` 也沒有 unittest/pytest/ruff/mypy/bandit 或 pull_request/push 觸發。

**影響**

已有回歸測試無法阻止有問題的 commit 進入 main；只有下次每日掃描才可能發現，而生產掃描還涉及網路與真實資料變動，無法取代可重現的測試。

**建議**

- 新增 PR/push workflow：Python 3.12（生產版）執行 unittest、compileall、`pip check`、發布契約稽核與 PyInstaller build。
- 排程 workflow 則保留線上整合與真實資料測試。

### AUD-005（P1）手機網頁長期儲存可寫 GitHub Actions 的 Token，且無 CSP

`mobile/app.js:2036,2077-2084,2190-2215` 把 fine-grained GitHub Token 保存到 IndexedDB，後續用 Bearer 權限觸發 workflow。`mobile/index.html` 沒有 Content-Security-Policy。

本次沒發現明顯未轉義的資料注入，`innerHTML` 路徑有 `esc()` 防護；但只要未來出現一個 same-origin XSS，Token 就可被讀取並外傳。手機瀏覽器/備份被存取時，長效憑證也會擴大影響。

**建議**

- 優先移除 Token 模式，保留已有的 GitHub 手動 Run workflow 連結。
- 若一定要手機一鍵觸發，改用可撤銷、短效、只能觸發單一任務的中介憑證，並加入嚴格 CSP、Trusted Types（可行時）與完整 XSS 測試。

### AUD-006（P2）依賴有上限，但仍無法重現

`requirements.txt:15-19` 全部是寬鬆範圍，例如 `pandas>=2.0.0,<4.0.0`、`yfinance>=0.2.0,<2.0.0`。CI 每次都會重新解析一組當時最新版本，並沒有 lock file 或 hash。實測本機是 Python 3.13.5，CI 則是 `.github/workflows/scan.yml:69-71` 的 Python 3.12。

**建議**

- 生成並維護 Python 3.12 的 hashed lock（可用 pip-tools/uv），每月或由 Dependabot/Renovate 受控更新。
- 在 CI 同時測生產版 3.12 與開發版 3.13，直到兩者統一。

### AUD-007（P2）大型行情 DB 仍被 Git 追蹤，與 ignore/cache 設計衝突

`.gitignore:3-13` 正確說明 `data/*` 應忽略，但 Git 仍追蹤：

- `data/price_volume.db`：83.80 MiB
- `data/inst_trades.db`：10.67 MiB
- `data/large_holder.db`：1.39 MiB
- `data/taiex.db` 及其他狀態檔

當前工作樹的 5 個預存資料檔已有修改，本次完全保留。當前 tracked worktree 約 99.9 MiB，`.git` loose objects 約 145.48 MiB。workflow 又用 Actions cache 還原同一群檔案，造成 checkout、cache、rebase/autostash 三種狀態疊加。

**建議**

- 在確認遠端 cache/bootstrap 策略後，對可重建 DB 執行 `git rm --cached`，改由 Actions cache/artifact 專責管理。
- 若需要一份 bootstrap snapshot，應用有版本、checksum 與明確 schema 的壓縮 artifact，不要把每日會改的 SQLite 直接追蹤在 main。

### AUD-008（P2）資料稽核的「fully clean」摘要會忽略交易日缺口

`tools/audit_data.py:46-48` 會統計 gaps，但 `tools/audit_data.py:57-59` 計算 `clean` 時只排除 hard error、recent jump 與 short MA60，沒有排除 `gaps > 0`、`short_52w` 或其他 soft flags，卻輸出「fully clean for short-term signals 99.1%」。

因此 99.1% 不能解讀為完整無缺口；實際同一份報告中有 425 檔、4,378 根缺 bars。

**建議**

將摘要拆成 `hard-valid`、`signal-ready`、`research-ready`，分別列出會排除的 flags；或至少把現有文案改為「passes hard checks and current short-term guards」。

### AUD-009（P2）測試集中於策略/帳本，擷取、GUI 與 PWA 幾乎無自動驗證

目前 7 個測試檔主要覆蓋 `scanner.exit_rules`、`scanner.holding_tracker`、`scanner.data_integrity`、`scanner.result_checks`、`scanner.scan_mode`、`scanner.quote_feed` 與 `portfolio` ledger/money/sync。沒有針對：

- `ingestion/*` HTTP 解析與不完整更新。
- `mobile/app.js`/Service Worker/IndexedDB 的 JavaScript 單元、schema migration 與瀏覽器 E2E。
- Tkinter GUI 的啟動、關鍵互動與打包後 smoke test。
- 真實 Python 3.12 CI 環境。

源碼約 12,137 行，測試約 2,042 行；本機未安裝 coverage，所以不能用這個行數比直接代表覆蓋率。

## 3. 已通過項目

| 檢查 | 結果 |
|---|---|
| `python -m unittest discover -s tests -v` | **155/155 通過**，1.945s |
| Python `compileall` | 通過 |
| `python -m pip check` | 通過，無 broken requirements |
| JSON 解析 | `mobile/*.json`、`data/*.json`、`config/scan_modes.json` 全數可解析 |
| SQLite `PRAGMA integrity_check` | 8 個 DB 全數 `ok` |
| 公開帳本護欄 | 通過：1 筆 recommendation，沒有私人成交/持倉列 |
| 秘密樣式掃描 | tracked text 未找到明顯 API key/private key；`.env` 未被追蹤 |
| `git diff --check` | 通過 |
| PyInstaller | 成功產生 Windows onedir 版，2,426 files、164.2 MiB |

PyInstaller 有列出多個來自 pandas/scipy/curl_cffi 的可選 hidden-import 警告；建置本身成功，但本次沒有將這些警告當作已證實的執行期缺陷。

## 4. 已在專案文件中承認、本次仍確認未完成的風險

`docs/TASKS.md` 已記錄下列問題，本次沒有將它們誤判為新發現：

- F08：raw/adjusted 價格未分離，`price_basis=unverified`。
- F16：上櫃法人歷史缺日，兩市場的最近 5 日定義尚未補齊。
- F03/F13/F14/F19 仍是部分完成：模擬進場後端命名、holding gap tolerance、官方休市日曆、outcomes 逐日累計。
- TDCC 與被隔離 K 線追查、研究快取重建、樣本外驗證、跨裝置私人持倉同步仍未完成。
- 舊勝率不能代表現行策略，目前資料狀態也不支持對外宣稱已驗證勝率。

## 5. 本次檢測限制

- **沒有執行真實線上完整掃描**：這會連線外部交易所/第三方 API，並改寫使用者當前的多個 SQLite/JSON；本次避免覆寫已存在的 5 個工作樹修改。
- **Python 3.12 未能本機驗證**：電腦只有 3.13.5，而生產 CI 用 3.12。
- **JavaScript 語法/lint 未執行**：本機沒有 Node/npm 或專案前端工具鏈。
- **測試覆蓋率無數值**：本機未安裝 coverage，專案也沒有 coverage 設定。
- **PWA 互動畫面驗收未完成**：`mobile/serve.py` 可正常啟動，但 Windows Computer Use 的瀏覽器核心連續兩次因 `windows sandbox failed: helper_unknown_error: setup refresh had errors` 退出，因此沒有宣稱實機 UI 通過。
- **未做即時 CVE 資料庫查詢**：專案未安裝 `pip-audit`/Dependabot 輸出；`pip check` 只能證明目前相依關係完整，不等於沒有已知漏洞。

## 6. 建議修正路線

### 0～1 天

1. 重生成且嚴格驗證三份發布 JSON，解決 AUD-001。
2. 恢復 TLS 驗證，將不能安全連線的資料源標成失敗，不要安靜降級。
3. 新增 PR/push 測試 workflow，先把現有 155 項測試與 strict payload check 接上。

### 1 週

4. 為各資料源與輸出加入 snapshot/generation manifest，修復日期分裂。
5. 增加 ingestion mocked tests、Service Worker/IndexedDB 前端測試與一個 PWA browser smoke test。
6. 移除手機 GitHub Token 長期儲存，或改用最小權限短效中介機制。
7. 修正 `audit_data.py` 的摘要定義和命名。

### 1～2 週

8. 引入 hashed lock、Python 3.12/3.13 matrix、coverage 與依賴漏洞掃描。
9. 完成大型 DB 脫離 Git 的遷移與恢復演練。
10. 在資料版本統一後，才重建研究資料與績效 outcomes。

## 7. 工作樹保護說明

健檢前已存在下列修改，本次未還原、未覆寫、未納入報告檔以外的修改：

- `data/inst_trades.db`
- `data/large_holder.db`
- `data/price_volume.db`
- `data/stock_names.json`
- `data/taiex.db`

本次建置產生的臨時 PyInstaller 目錄已在確認路徑位於 `D:\YenTool` 後刪除，沒有留下測試產物。
