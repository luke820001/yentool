# YenTool 手機版 PWA

把掃描結果做成可安裝到 iPhone 主畫面的網頁 App。**不需要 Xcode、不需要 Apple 開發者帳號、不需要上架審核。**

## 檔案

| 檔案 | 用途 |
|---|---|
| `index.html` / `app.js` / `styles.css` | PWA 本體（前端） |
| `manifest.webmanifest` | 讓它能「加到主畫面」變成 App |
| `sw.js` | Service Worker，離線快取（僅 https/localhost 生效） |
| `icons/` | 主畫面圖示 |
| `scan_result.json` | 掃描結果資料（由掃描器自動產生） |
| `serve.py` | 本機測試用的小型伺服器 |

## 資料怎麼來

主程式每次掃描完（`scanner/result_export.py`），除了原本的 `scan_result_latest.csv`，
會**同時**在這裡寫一份 `scan_result.json`。所以只要跑一次掃描，這裡的資料就會更新。

## 在 iPhone 安裝（同一個 Wi-Fi）

1. 電腦上跑：
   ```
   python mobile/serve.py
   ```
   會印出一個 `http://192.168.x.x:8000/` 的網址（iPhone 用那個）。
2. iPhone 用 **Safari** 打開該網址。
3. 點下方分享鈕 → **加入主畫面** → 完成。
4. 主畫面會出現 YenTool 圖示，點開就是全螢幕 App。

> 注意：透過區網 http 開啟時，iOS 不會啟用 Service Worker（需要 https），
> 所以「離線快取」不會生效，但 App 本身可正常使用（開啟時連線抓最新資料）。

## 想要真正離線 / 隨處可用（選配）

把整個 `mobile/` 資料夾（含 `scan_result.json`）丟到任何 **https** 靜態空間即可：
- Cloudflare Pages、GitHub Pages（私有 repo）、Netlify 等皆免費。
- 掃描完把更新後的 `scan_result.json` 上傳，手機端下拉/重整就更新。
- 這樣 Service Worker 會生效，可離線看上次結果、載入更快。

## 操作

- 點任一檔股票卡片 → 展開法人 / 集保 / 距離 / 均線 / 訊號燈細節。
- 右上 ↻ 重新抓最新資料。
- 上方搜尋框可濾代號或名稱。
- 卡片左邊色條：金=蓄勢分≥70，橘=≥50。
