# 完全上雲部署（電腦不用開）

目標：GitHub Actions 每天自動掃描 → 結果發佈到 GitHub Pages → iPhone 上的 PWA 隨時讀最新。**你的電腦完全不用開。**

> 重點限制（誠實說明）：**掃描運算不可能在手機上跑**，一定要有台機器算。這裡那台機器 = GitHub 的免費雲端跑者，不是你的電腦。手機負責「看」與「觸發」。

---

## A. 一次性設定（約 15 分鐘）

### 1. 把專案推上 GitHub
```
git init                     # 若還不是 git repo
git add -A
git add -f data/signal_ledger.db data/stock_names.json data/scan_state   # 種子：保留你已累積的實戰紀錄
git commit -m "init: cloud scan + mobile PWA"
git branch -M main
git remote add origin https://github.com/<你的帳號>/<repo名>.git
git push -u origin main
```
> `data/` 其餘大檔（price_volume.db 等）維持 gitignore——雲端會自己抓、用快取，不進版控。

### 2. 開啟 Pages
Repo → **Settings → Pages → Build and deployment → Source 選「GitHub Actions」**。

### 3. 開啟 Actions（若被預設關閉）
Repo → **Settings → Actions → General → Allow all actions**，並確認 Workflow permissions = **Read and write**。

### 4. 跑第一次
Repo → **Actions → daily-scan → Run workflow**。
- 第一次因為沒有快取，會冷抓全部股票，約 5–10 分鐘。
- 跑完後 **Actions → 該次 → deploy** 會顯示你的網址：`https://<帳號>.github.io/<repo名>/`

### 5. 裝到 iPhone
用 **Safari** 打開上面網址 → 分享鈕 → **加入主畫面**。完成，這就是全螢幕 App，離線也能看上次結果。

---

## B. 之後怎麼運作

- **自動**：每個交易日台灣時間 **17:00 與 18:00** 各掃一次（`.github/workflows/scan.yml` 的 cron，UTC 09:00 / 10:00；18:00 那次補抓 17:00 時還沒公布完的當日資料）。手機打開就是最新。
- **手機手動重掃**：手機瀏覽器登入 GitHub → 該 repo → **Actions → daily-scan → Run workflow**（或用 GitHub 手機 App）。約 1–2 分鐘後手機 PWA 下拉/按 ↻ 就更新。
  > 純 App 內按鈕觸發需要把 token 放進網頁＝不安全，所以用官方 Actions 頁面觸發最穩妥。真的想要 App 內按鈕，之後可加一個 Cloudflare Worker 代理（進階，再說）。

---

## B2. 開啟手機版 AI 報告（選配）

手機 App 有「AI 報告」按鈕，會顯示**當前市場篩選**（全部 / OTC / TSE）對應的報告——例如切到 OTC，就只把 OTC 的股票送給 AI。報告在**雲端掃描時**用你的金鑰預先各產一份，所以金鑰不會外流到網頁。

沒設金鑰也能用（會退化成本地文字摘要）。要真 AI，把金鑰加成 repo secret（擇一）：
```
gh secret set GEMINI_API_KEY      # 貼上你的 Gemini 金鑰（主要）
gh secret set GROQ_API_KEY        # 選配，Gemini 失敗時的備援
```
或網頁：repo → Settings → Secrets and variables → Actions → New repository secret。設好後下次掃描就有真 AI 報告。

## C. 想讓程式碼 / 選股資料不公開？（私有替代方案）

GitHub 免費帳號的 Pages 只能從**公開** repo 發佈。若你不想公開：

- **選項 1**：repo 設私有 + 用 **Cloudflare Pages**（免費、可連私有 GitHub repo）。Cloudflare Pages 專案設定：Build output directory = `mobile`。它會在每次 push 自動部署；`scan.yml` 裡的 `deploy` job 可移除，改由 Actions 只負責 commit（Cloudflare 監聽 push）。
- **選項 2**：升級 GitHub Pro（約 US$4/月）即可用私有 repo 的 Pages，設定不變。

---

## D. 誠實的注意事項

1. **yfinance 在雲端偶爾被限流**。有 DB 快取時每天只抓幾檔（風險低）；冷啟動抓 300 檔風險較高。失敗就重跑一次，快取熱了就穩。TSE/OTC/TDCC 用的是官方端點，穩定。
2. **公開 repo = 你的程式與每日選股都公開**。介意就走 C 的私有方案。
3. **cron 是 UTC 且可能延遲** 幾十分鐘，屬正常。要改時間改 `scan.yml` 的 cron。
4. **Actions 免費額度**：私有 repo 每月 2000 分鐘，日掃約用 ~110 分鐘，足夠；公開 repo 不限。
5. **快取可能被回收**（7 天未用或超容量），那次會冷啟動較慢，不影響正確性。
6. ledger（實戰紀錄）會 commit 回 repo 持續累積，不會因快取被回收而遺失。
