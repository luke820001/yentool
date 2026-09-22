# YenTool Changelog

---

## 2026-09-22 （兩輪回測：13 個候選全數否決，但挖到了標題數字本身）

### fix: 手機上寫的 71.7%，是一個**被否決**的變體的數字

同一批 556 筆、同一個 E3 引擎，只換後期收利門檻：

| 門檻 | 近 3 年 | 舊窗口 |
|---|---|---|
| 沒有這一段 | 69.08% | 67.62% |
| **+1%（`DEFAULT_RULE`，實際出貨）** | **70.81%** | **69.52%** |
| 0%（回測登錄簿 H 節自己記為否決） | 71.68% | 69.52% |

標題一直寫的 71.7% 是最下面那一列。**舊窗口兩者完全相同**，所以沒人發現。

根因不在文件，在量測工具：`archive/research/sandbox_entry_gate.py` 的 `run()`
手寫了四條腿（stop / tp / arm / lock），而 `DEFAULT_RULE` 有六條——
`late_profit` 根本沒傳進去。已改成一律從 `DEFAULT_RULE` 取值，並加上回歸測試：
每一條腿都必須從 `DEFAULT_RULE` 來，plan 裡不得出現手寫的門檻數字。

手機、README、TASKS、STRATEGY、回測登錄簿的標題數字全改為 **70.8%**。
後期收利這一段**仍然是採用的**，只是它值 +1.7pp 而不是 +2.6pp。

### 二輪回測：13 個候選，一個都沒通過

交叉回測九個、「標的太少」四個，全部寫進回測登錄簿 I 與 J 節（含被否決的那些，
因為帶數字的否決才擋得住同一個想法再回來）。

- 登錄簿自己列的第一個未測方向（隔日開盤跳空上限）**以相反方向關閉**：
  被它擋掉的交易勝率 82.9%、平均 +7.05%，留下來的只有 69.0% / +1.51%。
- 開放上市：近 3 年 785 筆 69.4% / +1.54%（基準 70.8% / +1.95%）。機制是
  **CORE+ 閘門在上櫃值 +5.2pp，在上市只值 +0.4pp**——在上市板根本沒有在選股。
- 板內排名：讓名單**變少不是變多**（556 → 364），因為合併排名一直在當品質前篩。
- 第 2 天也能買：**不稀釋**（71.08%），但完全不過品質閘門的名單是 71.89%——
  這個閘門在第 2 天的股票上值 0。而且它只救回 14 個掛零月裡的 1 個。
- 大盤閘門改軟（七種）：沒一個兩窗口同時贏基準；**就算把大盤閘門整個拿掉，
  最長空窗仍然是 219 天**。

新增八種「假訊號的偉裝方式」。最鋒利的一條：**先量可及母體**。
有個候選跑出來與基準**逐位元相同**，因為 +2% 鎖利代表已武裝的部位不可能在 +2%
以下賣出，所以那條規則能碰到的 88 筆**本來就全部是贏的**。

### 更正：我先前講的一個數字是錯的

我說「66 次掃描只有 3 次被大盤擋下」，那是從 signal_ledger 讀的，而那個欄位
2026-09-09 才開始寫入，2,734 列裡有 2,264 列是 NULL。直接用指數重算：
2026-06-25 至 09-22 的 63 個交易日裡有 **28 天**大盤是關門的，光 7 月就是 22 天裡的 18 天。

---

## 2026-09-22 (封存可編輯，以及它底下的五個缺陷)

擁有者：「獲利資料的封存應該要可以編輯」。做下去才發現，封存紀錄新到得了的那幾條編輯路徑，
本來就是壞的，連活倉也一樣。

### 封存紀錄根本打不開

封存之後那筆不再渲染持倉卡片，而「補登／更正成交」只掛在卡片上，所以「績效與歷史」裡的
封存列是**永久唯讀**的。現在那一列有一個「更正」按鈕，直接開舊有的成交明細表——沒有新的
動作代碼、沒有新的視窗。

### fix: 賣出股數的上限拿錯了狀態

`saveExecution` 拿 `pos.open_shares` 當上限，而那是**整段歷史跑完之後**的股數。封存與已平倉的
紀錄依定義是 0，所以要更正「把這筆交易結清的那一筆賣出」——也就是封存紀錄最常要改的那一筆
——會被「只持有 0 股」驛回，**而那個 0 正是這筆賣出自己造成的**。活倉也中招：買 2000、賣 1000，
要把那筆賣出改成 1500 會被拿 1000 比。

上限改成「這筆成交當下持有的股數」，由排序後的成交折疊算出來，表單提示與儲存時的檢查
用**同一個**來源。順帶擋住了一個舊行為：舊上限會放行「賣出日期早於買進日期」的紀錄。

### fix: 一筆更正可以讓後面那筆賣出的價金整筆消失

只管被編輯的那一列，等於沒有管它後面的成交。把第一筆賣出改大，第二筆就會對著 0 股折疊，
而 `replay()` 在那裡只是 `continue` 跳過——它的註解寫著「defensive; addExecution refuses this」，
但 addExecution 從來沒有拒絕過。結果是那筆賣出的收入**沒有錯誤、沒有標記、直接不見**。
鏡像的情況是把前面的買進改小，持股會變成負數。

現在兩個寫入者（`addExecution`、`voidExecution`）都在**開交易之前**檢查整段折疊，並且指名
是哪一筆衝突。撤銷誤登也因此第一次有可能被拒絕（撤掉兩筆買進中的一筆，底下的賣出就不夠股）。

### fix: 更正後可能留下一筆「看不見的持股」

撤銷結清的賣出、或補登一筆買進，都會讓封存紀錄又有股數。舊行為是它**繼續封存**：
不出現在持倉頁、不計入待處理、不抓行情，**卻仍然每天被估值並計入帳戶總額**。
你會持有一筆 App 從此不再提起的股票。現在這種紀錄會自動回到持倉清單並告知你，
`archived_at` 保留不刪。

### fix: 補登成交不會重算十日成果，連標記都沒有

只有「更正」會呼叫 `restateCycle`，「補登」不會。於是已實現損益動了、凍結的十日成果沒動，
而且連「已更正」那一行都不會出現——比更正路徑更糟，因為畫面上沒有任何東西說兩個數字不一致。

### fix: 凍結成果的重建旗標寫了沒人讀

`restateCycle` 會標記 `needs_rebuild`，但全檔沒有任何地方讀它。現在有人讀了，而且重建時區分兩種情況：
已平倉的部位，**金額是終結的**（持股歸零時成本歸零），所以金額跟著更正走；但估值日與當日
收盤只保留最近 30 個交易日，超過就重建不出來，這時保留原凍結值並在列上說明。
還有股票的部位則不會拿「今天的未實現損益」去覛掉一個凍結數字。

### fix: 縮短的歷史會留下孤兒估值列

估值列（marks）是唯一只寫不刪的衍生資料。把賣出往前移、或撤掉第一筆買進，舊列就留在原地，
於是帳面總損益跟已實現淨損益永久差了一個更正的金額，而畫面上沒有任何東西說哪一個才是現在的。
現在會清掉，**但只限報價視窗內**（視窗外的舊列不在備份裡，刪了就回不來），且與寫入同一個交易。

### 其他

- 已撤銷（誤登）的部位不再出現在「已凍結的十日成果」表裡——同一頁下方才宣告它不計入損益。
- 明細視窗不再印出「D null」。
- 重建時「相對實際成本」不再變成空值（`avg_cost` 在平倉時歸零，改讀估值列當下的成本）。
- 成交明細裡的「補登賣出」只在還有股數時顯示；封存紀錄按下只會得到一句錯誤提示。
- 手機資源版本 v26 → v27。

### 發佈後的第一次開啟會動到數字

孤兒估值列的清除是無條件的，所以任何已經帶著舊列的部位，帳面總損益與價差總損益會在
第一次 `load()` 時改變，沒有編輯、也不會問你。**新的數字才是對的**；會變的那些本來就錯。

---

## 2026-09-22 (大盤資料落後時的誤判)

### fix: App 對市場講了一句不成立的話

加權指數走的是另一個資料源，會比個股晚一個交易日。9/21 就是如此：指數當天其實站上了 20 與
60 日均線，只是那根 K 還沒發布，於是買進閘門否決了 46 列全部，而兩個畫面都寫「大盤未站上
20/60MA」。否決本身沒錯，錯的是那句話。現在拆成 `regime` 與 `regime_stale` 兩個代碼。

### fix: 掃描計時器沒發現這件事

資料日是今天、沒降級、自檢也沒失敗，所以計時器的任何一條重試條件都沒觸發，那一天就這樣全數
不可買到底。現在 `meta.regime.is_current` 為假也算未完成，晚一點重跑就能把整天的訊號救回來；
若資料一直沒追上，重試次數用完就照舊發布，與原本相同。

---

## 2026-09-21 (第八批：程式碼稽核修正)

第二輪稽核不看文件，只看程式碼和實際資料庫。找到 7 項確認的缺陷、5 項可能的、
8 項風險。以下是每一項的實際影響與修法，全部附回歸測試（246 → 286 項）。

### fix: ETF 的升降單位跟個股不同，而錯的方向會讓停損變寬

台股 ETF / ETN 未滿 50 元跳 0.01、50 元以上跳 **0.05**；個股在 100-500 元跳 0.50。
`scanner/tick.py` 與手機都只有個股那一張表。資料庫已經證實：9/18、9/21 在
100-500 元區間，412 檔個股**全數**收在 0.50 的倍數上，32 檔 ETF 則收在 0.05 上（只有 4
檔恰好也落在 0.50）。以 0050、106.75 成交為例：鎖利價真值 108.85，舊程式印 108.50 ——
**停損比規則更寬**，是會賠錢的那一邊。今天才開始把 ETF 放進 universe.json，所以這是
剛上線的問題。兩邊都改成依股票代號選表，15 欄位的 `price_off_tick` 自檢也跟著改。

### fix: 手機把「後段獲利了結」早一天標成必守

`day_index` 兩邊都是 1 起算，後端在**第 8 天**觸發；手機寫的是 `lateFrom - 1`，第 7 天就把
「收盤在這之上 → 隔日開盤就收下」這一行標成**必守**。你會在第 8 天開盤賣掉，而後端
還在等第 9 天。已對齊，並以 `replay_exit` 的實測值釘住（第 7 天不觸發、第 8 天觸發）。

### fix: universe.json 的均線跨過了資料空洞

price_volume.db 在 9/09 之前每天約 2,160 檔，9/10-9/17 只剩入選名單的 ~320 檔，
9/18 起才回到全市場。以 2330 為例，存的序列是 09-18、09-09、09-08...所以「5 日均價」是
**5 筆跨 15 個日曆天、中間漏了 8 個交易日**的平均，`Ret_5D_Pct` 比的是 09-03。手機就是拿
這個值畫「站上/跌破 5 日均價（到期可續抱/就出場）」。現在任何會跨過缺口的數字一律給 null，
每筆資料加上 `Sessions_Span` / `Gap_Sessions` 說明原因。

順帶修好回補佇列：舊條件是「總筆數 < 60」，根本看不到空洞（2330 有 63 筆、跨 69
個交易日），而且排序是看「最新一日成交值」，恰好把今天沒行情的股票排到最後面。
現在佇列第一名就是 2330。

### fix: 全市場快照把滞留欄位清成 NULL

`data` 有 12 欄，快照只提供 8 欄，而 `INSERT OR REPLACE` 是先刪再插 ——
`MA5_Volume`、`Min_Volume_20`、`Max_Price_20`、`Min_Price_20` 在 9/21 被清掉 990/1292 筆。
chip_verifier 自己會重算，所以掃描沒壞，但 `analyzer/signal_evaluator` 跟研究工具讀的是
儲存值。改成 UPDATE 後 INSERT（不需要索引），並加上一個自愈函式把已經被清掉的補回來。

同一段程式還在掃描路徑裡建立 `ux_data_sid_date` 唱獨特索引 —— 那是
`storage/data_store.migrate_stock_store` 的工作，而那個函式的文件寫明「掃描路徑絕不可以呼叫」，
因為它會先備份、並標記 `user_version`。已移除。

### fix: 法人資料從來沒進過 universe.json

`scan_headless.py` 匯入了 `get_inst_features` 却從未呼叫，`inst` 是寫死的 null。
所以 `universe_export.build` 裡整段法人欄位都是死程式，一檔系統沒推薦過的持股會被
告知「本次掃描沒有這檔的法人資料」—— 永遠。現在 **2,141 檔**帶法人資料。

### fix: tracked 區塊完全沒有被檢查過

欄位註冊、升降單位、成交價位階恒等式——全部只跑 `rows`，而 `tracked` 是**持股的人**在看的那一塊。
現在同一套規則會跑在 tracked 上，錯誤碼加 `tracked_` 前綴以免混淆，分級降為 warning：
要看得見，但不能讓一張正常名單的掃描失敗而重跑。另外 `attach_recommendations` 也跑到
tracked 上（不會新建推薦，只挂回凍結欄位）。

### fix: 桂面版發布會把 tracked 整塊刪掉

`gui/scan_worker.py` 跑的是同一個 `export_scan_result` 但不建 tracked，所以雲端掃完之後
開桂面程式，手機依賴的掉榜持股資料就不見了。改成「沒建就汿留上一份」，並標記它是哪一場
掃描建的；手機卡片會直接寫「這份追蹤資料是 X 那一次掃描留下的」。

並且桂面版現在會**自己建**這一塊：同樣把近期被推薦的名字強制加入候選池、同樣分出 tracked 列。
兩邊發布的 payload 從此一致，而不是「桂面版少一塊、靠汿留補回來」。

### fix: 同一列上「第 12 天續抱」跟「鎖利第 6 天已出場」並存

9/21 發布的 101 列裡有 **18 列**如此。價格出場就是持有結束，行事曆不再適用。
新增 `Hold_Status = "exited"`，`Hold_Day` 改成**出場那一天**、剩餘天數 0，桂面版不再重算
已結束的持有，欄位自檢也不再拿「天數相加」去問一筆已經收掉的交易。

### fix: 法人視窗被削到個股自己的最後一筆

交易所的三大法人表是**整板每日**，一檔股票沒出現就是那天法人沒交易，是 0，不是缺一天。
舊程式把視窗右邊削到個股自己的最後一筆，於是 `Inst_Date` 往前跨一天、`chip_basis` 判成
「法人資料比行情舊」，手機就把討論藏起來——而實際上資料是最新的。

### fix: `simulate_exit` 關不掉後段獲利了結

`replay_exit` 收 `late_from` / `late_gain`，`simulate_exit` 不收也不轉交，所以一個要測
「沒停利、沒鎖利」的參數掃描，測到的其實是一個仍然會後段獲利的規則。這正是這個
模組把數字当**參數**而不是常數的原因。已轉交，並允許 `late_from=None` 關掉這一段。

### fix: 手機的 5 日均價跟後端不是同一組

手機先把沒報價的場次濾掉再取最後 5 筆，所以只要缺一天報價，它的「5 日均價」就跨 6 天以上。
改成先取最後 5 個**場次**，其中有任何一天沒價就不給均價。

### fix: 兩項欄位上下限描述的是名單、不是指標本身

- `Inst_Pct` 上下限 ±100 → ±500：籌码兩三天量的大單在令股上是常態，舊上限會直接擋掉整份發布。
- `RS_Score` 下限 0 → -2000：相對強度是超額報酬，掉榜的名字負值很正常（56 檔裡 12 檔）。
- `Risk_Pct` 改為可為空（它是從 `Strict_Stop_Loss` 推的，收盤缺失時就是空）。

### fix: 停牌的股票不再讓整場掃描失敗

有上榜但沒收盤的股票確實是問題，但如果所有資料來源都沒有更新的 K 棒（停牌或下市），
重跑掃描也不會變好——而 scan-timer 看到 fail 就會重跑。改為 warning。

### fix: 真實交易日被誤刪的安全邊界太窄

現在涵蓋率在 ~320（只有入選名單）到 ~2,300（全市場）之間擺動四倍。一個真實的「只有名單」日
夾在兩個全市場日中間，門檻會是 260、而它是 320。`purge_nonsession_bars` 是全專案唯一會 **DELETE**
價格列的地方，刪錯一天就是 9/20 那次的損害。加上絕對下限 `SESSION_FLOOR = 100`：
比任何觀察到的假 K 棒大一個數量級，比任何真實場次小一個數量級。

### 實測：「兩種價格基準」這項風險目前沒有發生

稽核指出 price_volume.db 同時被兩個寫入者共用：快照寫交易所原始價，yfinance 寫還原價
（auto_adjust=True），除息後會在序列裡留下一個跌價幅度的阶梯。新增了每次掃描的對帳，
並在候選股抓完之後把交易所公告價補回去。第一次量出來是「61/1393 不同」，**但那是我的
對帳程式錯了**：兩個板不一定發佈同一個交易日（那天 TSE 還停在 09-18、OTC 已經是 09-21），
所以拿錯日子比。改成以（股票, 日期）配對後：**2,311 筆全數相符，差異 0**。風險機制是真的，
目前資料裡沒發生；監控留著，除息那天會立刻講話。

### 其他

- 批次行情回傳的價格比公告價差一點點（13.1499996185303 其實是 13.15、2402.926758 其實是 2402.93）：寫入前先四舍五入到兩位小數，舊資料也一次性整理（**482,795 筆**）。看起來無害，但每一個發布的價位都是這個數字乘上什麼，1e-5 的雜訊對齊升降單位之後會變成**整整一檔**的誤差（8/01 以來的成交價裡有 3,086 個價位因此算錯一檔，最大 5.00）。
- 凍結的 `Initial_Stop_Price` / `Initial_Target_Price` 上畫面前對齊升降單位（舊紀錄是升降單位
  上線之前寫的）。
- `split_tracked` 的上限真的按「最近被推薦」保留，而不是按成交值排名。
- 三處 `with sqlite3.connect(...)` 沒關連線（它管的是交易、不是句柄），Windows 上會鎖檔。

---

## 2026-09-21（第七批：全專案一致性稽核）

使用者要求「整個專案做完各種回測後，再完整檢查修正」。對照**程式裡的實際常數**
逐一檢查文件與兩端介面，以下是查到並修好的問題。

### fix: 桌面版與後端對「要不要出場」會給相反答案（行為缺陷）

`gui/app.py` 的 `_live_hold()` 只實作了大盤延後出場，不知道當天新增的**個股續抱**規則。
後端標成 `delay`（收盤仍站上 5 日均價、續抱）的列，在桌面被重新推導成「今日出場」——
**一個畫面叫你賣、另一個叫你抱**。

修法：抽出 `_still_riding()`，條件與 `holding_tracker._still_strong` 完全一致
（含「嚴格大於」這個細節），延後出場改成 `disturbed or _still_riding(row)`，
提示文字也會說明是哪一個條件成立。新增 `tests/test_ui_parity.py`
逐案比對兩邊的判定（含相等、None、字串輸入），以後不會再各走各的。

### fix: 手機一直可能載到舊版程式（今天所有介面修正都可能看不到）

`index.html` 要的是 `app.js?v=16`，而 Service Worker 已經到 v25。
SW 自己的快取有去掉查詢字串所以沒事，但**瀏覽器的 HTTP 快取是用完整網址當鍵**，
手機可能一直拿兩週前的 `app.js`。新增 `tests/test_mobile_assets.py`：
`index.html` 的 `?v=` 必須等於 `sw.js` 的 `VERSION`，否則測試失敗。

### fix: 畫面上仍寫著已經不成立的規則

- 手機持倉卡：「漲到 +6% 後上調到 +2%」→ 改為「收盤站上 +2.5% 後，隔一個交易日起」，
  且改為讀取常數而不是寫死數字。
- 手機到期提示：「第 10 天已到，依策略應於收盤出場」→ 補上續抱規則，
  否則它與同一張卡片下方的「明日委託」自相矛盾。
- 桌面欄位標籤「鎖利啟動(漲6%)」→「鎖利啟動(收盤站上+2.5%，隔日生效)」。
- 桌面決策卡引用 67.1%／69.1%（後期收利之前的中間值），而手機寫 71.7%——
  **同一套規則兩端報不同勝率**。已統一。
- 手機回測窗口寫「2020-2026」，實際是 2017-2026。

### fix: 文件裡的作廢數字

- `README.md` 仍說「勝率未經新資料驗證」——已於當天驗證完成，改為現行數字並說明舊數字為何作廢。
- `docs/STRATEGY.md`：§3.5／§3.6 的 68.5%／71.4%／71% 都出自作廢的 E2 引擎，
  已標明引擎並補上 E3 的數字；§7 速查表把 `Strict_Stop_Loss` 等**依收盤價**計算的欄位
  寫成 `fill×`，與 `Fill_*` 欄位混淆（附錄 D.3.C 正是在講這件事），已分列；
  「連 4 週跌破 55% 就退回 2026-07-06 規則」會同時撤銷兩項已驗證的改動，已修正門檻與說明。
- `docs/history/` 兩份研究文件加上作廢橫幅（內文不動），並點出其中「已定案、不得重測」
  清單本身也已過期。
- `docs/BACKTEST_LOG.md`：後期收利那一列把採用的數字標成「門檻 0%」，但採用值是 +1%。
- 新增「兩個數字常被搞混」一節：勝率 71.7% / 以金額計 70.8% / 滑價後 70.8% / 達+25% 2.3%
  是四個不同的量測；564 筆與 556 筆是兩份不同的重放樣本。
- `CHANGELOG` 當天最大的一項改動（後期收利，69.1→71.7%）原本**完全沒有條目**，已補。
- `scan_mode.STRATEGY_VERSION` 的變更紀錄只寫了鎖利，漏了後期收利、續抱規則與分批賣出。

---

## 2026-09-21（第六批）

### fix: 全市場只有「今天一根 K」，等三個月才長好不是答案

**Files:** `scanner/market_snapshot.py`、`scan_headless.py`、`tests/test_ingestion_parsing.py`

線上稽核發現的：發布出去的宇宙檔有 2,318 檔，但**日 K 根數的中位數是 1**。
雲端只拿到當日快照、沒有歷史，所以大部分股票只有價格，算不出任何均價——
持有它的人還是得不到建議，而且要等約三個月快照累積才會自己長好。

每次掃描現在額外回補一批缺歷史的名字（`BACKFILL_PER_SCAN = 150`），
**依當日成交值排序**，因為那才是可能被持有的。幾天內就能覆蓋全市場，
而不是一次向資料源要兩千檔。

**同時解決一個會永久浪費預算的問題**：債券 ETF 與 A 股代號在批次來源上根本抓不到，
不記錄的話每次掃描都會重試同一批永遠填不了的名字，真正能補的反而永遠排不到。
新增 `backfill_attempts` 表：連續 3 次失敗就跳過 30 天，成功則清除紀錄。
實測 23 檔已進入跳過名單。

本機價量庫目前 2,336 檔、僅 145 檔仍歷史不足。

---

## 2026-09-21（第五批）

### fix: ETF 也要能查到（0050、0056 原本被我的過濾條件排除）

**Files:** `scanner/market_snapshot.py`、`scanner/universe_export.py`、
`mobile/app.js`、`mobile/sw.js`（shell v24）、`tests/test_ingestion_parsing.py`

**Trigger:** 「ETF 也會顯示嗎」。查下去發現兩個問題：

1. **四碼 ETF 被排除。** 我寫的代號過濾只接受 5-7 碼的 00 開頭，
   但台股 ETF 代號長度不一：**0050 是四碼**、00878 五碼、00400A 六碼。
   最可能被持有的那兩檔（0050、0056）剛好是被排除的那一種。改成接受 4-7 碼後，
   當日快照的 ETF 從 0 檔變成 117 檔。
2. **新納入的標的要滿 60 根 K 才會發布。** 對全市場儲存開始的第一天而言，
   每一檔 ETF 都只有一根 K，於是三個月內什麼都查不到——但它的收盤價當下就有。
   改成「有多少講多少」：記錄照常發布，`Bars` 欄說明有幾根 K，
   算不出來的均價一律留 null，手機顯示「目前只有 N 根日 K，均價還算不出來」。

另外用既有的批次抓取補了 ETF 歷史：355 檔中 **229 檔已有 60 根以上**，
0050 / 0056 / 00878 都有完整的 5/20/60 日均價。其餘 117 檔是冷門或新上市，
會隨著每天的全市場快照自己長出來。

新增 7 項解析測試（各種長度的 ETF 代號、權證要被排除、停牌不寫入、缺開高低要回退到收盤價）。

---

## 2026-09-21（第四批）

### fix: 登錄持倉時自動帶入股票名稱

**Files:** `mobile/app.js`、`mobile/sw.js`（shell v23）

**Trigger:** 「標的名稱沒辦法自動被代入」——手動新增持倉時名稱欄永遠空白，
卡片就只好顯示「2330／2330」。

以前做不到是因為手機只有當日名單那幾十檔的名稱。現在 `universe.json` 有 1,847 檔，
所以新增 `stockName()`：依序查 今日名單 → 近期推薦 → 全市場 → 報價檔的名稱表。

- 輸入代號的當下就填入名稱（你自己打過就不覆蓋）。
- 表單一打開就去取全市場檔，所以連系統沒推薦過的代號也查得到。
- 查不到時**明白說出來**（欄位提示「查不到這個代號，可自行輸入」），而不是靜默留白。
- 早先存下來、名稱等於代號的舊持倉，現在渲染時會補上真正的名稱。

實測：2454 → 聯發科、1815 → 富喬、9999 → 提示查不到。

---

## 2026-09-21（第三批）

### fix: 全市場快照差點把真正的交易日從日曆上刪掉

**Files:** `scanner/data_integrity.py`、`tests/test_data_integrity.py`

上一批把全市場日 K 寫進價量庫之後，近期日期有 ~2,300 檔、歷史日期只有幾百檔。
`nonsession_dates` 是拿「整段 40 天的中位數」當基準，低於 20% 就判定「那天市場沒開」，
於是**覆蓋率的落差被讀成休市**：本機資料庫當場誤判 6 個真正的交易日。

這會直接造成持有天數與進場日錯誤——正是 2026-09-20 假 K 棒事故的同一種傷害，
只是方向相反（那次是多了一天，這次是少了六天）。**在雲端跑到之前抓到。**

改法分兩步，都有測試：

1. **與鄰近日期比較**，不與整段比較。覆蓋率換檔時，鄰近日期屬於同一個régime。
   誤判從 6 天降到 1 天。
2. **與前後兩側「較少的那一側」比較**。換檔當天前後各屬一個régime，取較低者，
   所以換檔本身永遠不會被誤判；而假 K 棒對周圍每一天都稀疏，照樣被抓出來。
   誤判降到 0。

新增 8 項測試涵蓋：覆蓋率上升、覆蓋率下降、換檔當天、假 K 棒在最新日、
假 K 棒在中間、假 K 棒在稀疏期間。

---

## 2026-09-21（第二批）

### feat: 你登錄買入的任何一檔，都給得出建議——包含系統沒推薦過的

**Files:** `scanner/market_snapshot.py`（新）、`scanner/universe_export.py`（新）、
`scanner/tracked_rows.py`（新）、`tests/test_tracked_rows.py`（新）、
`scan_headless.py`、`scanner/result_export.py`、`scanner/chip_signal.py`、
`config/settings.py`、`mobile/app.js`、`mobile/sw.js`（shell v22）

**Trigger:** 「1815 是前幾個交易日推薦、我有買入，現在又跟我說沒有在名單，
變成我沒有辦法有其他數據參考」、「即使沒有在系統建議的標的，也能協助推薦我購買的標的相關建議」。

**根本原因：** 價量庫只為每次掃描的那 ~300 檔候選成長。名單一掉，
那檔的法人、均線、支撐全部停止計算——偏偏那正是持有者最需要它們的時候。

**修法分三層：**

1. **全市場每日 K 棒**（`market_snapshot.py`）。兩個交易所的公開端點本來就一次回傳
   全市場的開高低收，而舊程式只取了收盤價和成交量就丟掉其餘欄位。現在每次掃描
   多兩個請求，把全市場 2,303 檔的日 K 寫回價量庫（實測 7.6 秒）。
   資料庫從 1,967 檔變成 2,328 檔，而且每天都是最新的。
   只存得起來的標的：4-5 碼個股與 00 開頭的 ETF；六碼權證與無成交的列不寫入。
2. **全市場精簡資料檔**（`universe_export.py` → `mobile/universe.json`）。
   1,847 檔、955 KB，含收盤、5/10/20/60 日均價、20 日高低、ATR、法人買賣超。
   **獨立檔案、手機只在「有持倉不在名單上」時才下載**，主 payload 維持 127 KB。
3. **近期推薦的完整列**（`tracked_rows.py` → payload 的 `tracked` 區）。
   掃描時強制把近 30 個交易日推薦過的股票帶進候選，替它們算出
   **完整的 118 欄**（含出場計畫、持有天數），因為那些最可能是你正持有的。

**手機端：** 查找順序改為 今日名單 → 近期推薦 → 全市場。持倉卡會誠實說明它在哪一層：
「已不在今日名單，但仍在近期推薦追蹤中」／「系統沒有推薦過這檔，仍從全市場掃描取得資料」／
「不在掃描範圍內（成交值太小），只能用報價估值」。
所有價位一律以**你登錄的成交價**計算，卡片上直接寫明。

**隱私不變：** 持倉只存在手機，不上傳。後端發布母集合，比對在裝置上完成。

---

### fix: 兩個當場抓到的顯示錯誤

實機測試新功能時發現，兩個都會誤導：

- **價格少了 100 倍**：App 內部以「分」為單位，新加的個股現況欄位卻傳了「元」，
  2,460 顯示成 24.60。
- **新鮮度訊息講反了**：證交所全市場端點當晚還停在 09-18，法人表卻已經是 09-21，
  程式一律寫成「法人資料落後行情」。`chip_basis` 現在分 `current` / `lag` / `ahead`
  三態，畫面說出真正落後的是哪一邊。兩種情況都不做籌碼判定（需要同日的配對）。

---

### fix: `recent_pick_ids` 沒有關閉資料庫連線

`with sqlite3.connect(...)` 管的是交易，不是連線；handle 一直開著，
在 Windows 上會鎖住檔案。由測試抓到。

---

## 2026-09-21

### fix: 鎖利改成「收盤判定、隔日生效」——先前的勝率有一大半是模擬器產生的

**Files:** `scanner/exit_rules.py`、`scanner/scan_mode.py`、`scanner/holding_tracker.py`、
`archive/research/sandbox_lock_delay.py`（新）、`tests/test_exit_plan.py`、
`mobile/app.js`、`gui/app.py`、`docs/STRATEGY.md`（附錄 E）

**Trigger:** 使用者指定「鎖利改成隔日才生效」。查下去發現這不只是時點微調。

舊規則：**盤中**觸及 `fill × 1.06` 就把停損上移到 `fill × 1.02`，而且回測允許**同一根 K 棒**
當天就被那條剛成立的鎖利掃出場。掃描在收盤後跑、手機晚上才顯示、單子隔天才送得出去——
那條保護在現實中不存在。

同一批 564 筆 CORE+ 交易，只改執行時點：

| 口徑 | 近 3 年勝率 | 每筆平均 |
|---|---|---|
| 舊程式回報 | 69.7% | −0.43% |
| 依實際可執行方式 | **63.0%** | +1.54% |

**6.7 個百分點是模擬器造成的。**

**修正 + 重新調參：** 鎖利改為「**收盤**站上門檻才算啟動，**隔一個交易日起**生效」。
用收盤而非盤中最高價，是因為收盤價是手機、後端、帳本三邊共同拿得到的數字，實測也較好
（67.1% vs 65.0%）。門檻重搜後 +6% → **+2.5%**（鎖利價維持 +2%）：
近 3 年 **63.0% → 67.1%**、每筆 +1.54 → +1.98，舊窗口 63.3% → 65.1%，
28/28 季不低於原參數，bootLo 58.2 → 62.3。

**滑價壓力測試（本輪最關鍵的檢驗）：** 把每筆停損/鎖利成交往壞的方向移 0.5%，
`lock = 0.01` 從 67.6% 崩到 **29.8%**（鎖在 +1% 扣掉 0.585% 成本幾乎等於零，
一點滑價就整批翻成虧損），採用的 `lock = 0.02` 仍有 66.8%。
勝率/bootLo/前後半段/季度穩定度這四項傳統檢驗對兩者給的分數幾乎一樣——只有滑價測試分得出來。

`exit_rules.replay_exit` 的事件順序改為：開盤 → 該根實際觸及的最低出場價 → 停利 → **收盤判定啟動**。
`simulate_exit`、`signal_ledger._simulate_rule`、`holding_tracker` 的 `Plan_Stop` 全部共用同一份實作，
與新寫的研究引擎逐筆比對 564 筆 **零差異**。

---

### feat: 後期收利——虧損全部集中在「撐到最後一天」的那一群

**Files:** `scanner/exit_rules.py`、`scanner/holding_tracker.py`、
`archive/research/sandbox_stability.py`（新）、`tests/test_exit_plan.py`、
`mobile/app.js`、`gui/app.py`

把採用中的規則**按出場方式拆開**，就看得到錢虧在哪（近 3 年）：

| 出場方式 | 佔比 | 平均 |
|---|---|---|
| 鎖利 | 45% | +1.2% |
| 停利 | 22% | +20.6% |
| 災難停損 | 6% | -20.5% |
| **期滿（第 10 天）** | **28%** | **-6.9%** |

幾乎所有虧損都在「撐到第 10 天、卻從未收在 +2.5% 以上」的那一群。

**採用**：第 8 天起，收盤只要還高於成交價 +1%（扣掉 0.585% 來回成本後仍為正）
就**隔日開盤出場**，不把獲利帶進最後一天。
近 3 年 69.1 → **71.7%**、更早的資料 67.6 → 69.5%，期望值不變
（+2.00→+1.95 / +1.91→+2.04），兩窗口前後半段全升，**25/25 季**不低於前版，
滑價 0.5% 後仍有 70.8%。

**對照組證明機制**：把後期還在**虧損**的部位砍掉（同樣縮短持有）勝率掉到 55.5%——
有效的是「收下獲利」，不是「早點出場」。

**門檻為什麼是 +1% 不是 0%**：0% 在近 3 年高 0.9pp，但兩者只有 **9 筆交易**不同
（0% 贏 6、+1% 贏 3），是硬幣；而收在平盤扣掉手續費與證交稅其實是虧的，沒有獲利可收。

單根 K 棒的事件順序因此變成：**昨晚掛的收利單在開盤成交** → 開盤對停利／停損 →
該根實際觸及的最低出場價 → 停利 → 收盤判定鎖利啟動 → 收盤判定後期收利。

---

### feat: 「明日委託」——收盤後就把隔天要掛的單算好

**Files:** `archive/research/sandbox_daily_plan.py`（新）、`scanner/scan_mode.py`、
`scanner/holding_tracker.py`、`scanner/result_checks.py`、`mobile/app.js`、`gui/app.py`

**Trigger:** 使用者：「當天的收盤就要給隔天的建議，跌破多少減碼或加碼，上漲多少分批獲利或繼續跟著漲。」

新的回測引擎把這件事做對：**每張單都由某個收盤決定，只能在之後的 K 棒成交**。
先前所有階梯研究都是在同一根 K 棒內依假設順序觸發，所以 2026-09-17 那批結論在這裡**全部重測**。

| 做法 | 近 3 年 | 舊窗口 | 裁決 |
|---|---|---|---|
| 基準（現行規則） | 67.1% / +1.98% | 65.1% / +1.66% | — |
| **第 10 天收盤仍站上 5 日均價就續抱** | **69.1% / +2.00%** | 67.0% / +1.72% | **採用** |
| 跌 8% 加碼半倉 | 69.7% / +3.01% | 68.8% / +2.66% | 選用 |
| 漲 15% 賣一半 | 67.6% / +1.77% | 65.1% / +1.61% | 選用 |
| 三者全上 | **70.5%** | 67.4% | — |
| 跌破減碼（4 個價位 × 2 種比例） | 全數更差 | 全數更差 | **二次否決** |
| 多段鎖利 | ±0.5pp | ±0.5pp | 否決 |

**續抱規則採用**（`holding_tracker._still_strong`）：不多佔資金、不動停損、平均報酬不變，
純粹的勝率改善；與既有的大盤延後出場以「或」結合（大盤那條單獨只值 +0.2pp，個股才是有效的）。

**加碼列為選用而非規則**：報酬率的分母是投入資金，加碼會讓分母變大。改用絕對金額檢查後，
加碼確實同時提高勝率與金額（28/28 季），**但最大單筆虧損由 −21.6% 擴大到 −29.0%**。
這是資金配置的決定，卡片上明寫會放大虧損。

手機持倉卡新增「明日委託」表格：停利 / 選用賣一半 / 鎖利啟動門檻 / **後期收利價** / 停損 / 選用加碼，
每列標明「必守」或「選用」，價位可直接掛單。PWA shell v18。

---

### fix: 所有價位對齊台股升降單位（使用者回報：小數點掛不出去）

**Files:** `scanner/tick.py`（新）、`tests/test_tick.py`（新）、`scanner/scan_mode.py`、
`scanner/holding_tracker.py`、`scanner/result_checks.py`、`mobile/app.js`、`gui/app.py`

```
191.50 × 0.80 = 153.20   ← 100–500 元一檔 0.50，掛不出去
191.50 × 1.20 = 229.80   ← 同上
```

不只是難看：等於把進位留給使用者在 09:00 臨場處理，而那正是 −20% 停損悄悄變成
−19.7% 或 −20.3% 的地方。`scanner/tick.py` 實作升降單位表，
**賣出方向捨去、停利方向進位**，原則是「絕不顯示一個比實際掛得到更好的價格」。
一檔最多移動 0.5%，落在已壓力測試過的範圍內。

欄位自檢新增 **`price_off_tick`**：15 個價位欄只要掉出升降單位就判錯，
`Risk_Pct` 也改成對「實際掛得到的停損」計算（所以不再剛好是 20.0%）。
這是讓同類問題以後自己被抓出來的那條規則。

---

### research: 籌碼能不能決定隔天買賣——問題問得對，答案是否定的

**Files:** `ingestion/inst_history.py`（新）、`tools/backfill_inst_history.py`（新）、
`ingestion/inst_trades.py`（改寫）、`scanner/chip_signal.py`（新）、
`archive/research/sandbox_chip_manage.py`（新）、`tests/test_inst_history.py`（新）、
`tests/test_chip_signal.py`（新）

**Trigger:** 使用者：「標的的建議應該要看當天的籌碼，建議隔天是否賣出或是再追加。」

**先補資料（順帶結案 F16）：** 找到 TPEX 可查**任意日期**的三大法人端點（2018-03 起），
TWSE T86 本來就可以指定日期（注意 2017-12 欄位改名，且自營商合計要用前綴比對，
否則會抓到外資自營商那欄）。正式的每日更新改為**兩市場各自對交易日曆補缺日**，
5 日窗改以該市場交易日重建（缺席日計 0，並回報 `Inst_Sessions`），
新增自營商、三大法人合計、連續買賣超日數、法人資料日等欄位。

**結果（562 筆 CORE+，89% 有法人資料）：**

| 規則 | 近 3 年勝率 | 基準 |
|---|---|---|
| 法人賣超就隔日出場 | 47.4% | 67.1% |
| 賣超 ≥ 均量 2% 才出場 | 51.2% | 67.1% |
| 連 3 日賣超才出場 | 61.3% | 67.1% |
| 虧損中又被賣超就出場 | **46.2%** | 67.1% |
| 法人買超就加碼 | 56.4~59.0% | 67.1% |

**為什麼**（全上櫃 137,925 個股日）：今日法人買賣超占均量，對**隔日收盤對收盤**秩相關
**+0.041（t=+11.6）**，確實存在；對**隔日開盤對收盤**是 **−0.003（t=−0.9）**。
**訊號整個在隔日跳空裡**——收盤後才看得到，最快隔日開盤才能成交，那時已經結束。

籌碼欄位因此保留為**確認資訊**：`chip_signal.CHIP_RULES` 兩條腿都關閉，
持倉列直接寫「籌碼僅供確認」，不輸出看起來像建議的 `hold`。

---

## 2026-09-20

### fix: 擋掉「還沒開盤的交易日」假 K 棒（自檢時實際抓到）

**Files:** `scanner/data_integrity.py`、`scanner/chip_verifier.py`、`scanner/holding_tracker.py`、
`scanner/quote_feed.py`、`tests/test_data_integrity.py`（新）

**Trigger:** 改完停損後手動觸發一次雲端掃描驗收，結果欄位自檢 **fail**：
`quotes.as_of` 是 2026-09-20（週日），行情資料日卻是 09-18，43 檔全被判「該交易日沒有收盤價」。

**真正的原因（不是這次改動造成的）：** yfinance 對**還沒開盤的下一個交易日**會回傳一根佔位 K 棒。
實測 1930 檔裡有 22 檔拿到日期 2026-09-20 的 K 棒：OHLC 複製 09-18 收盤、**帶著少量成交量**
（2912：48,692 股 vs 當天 273 萬股），所以 `ingestion.price_volume_multi._drop_synthetic_bars`
的「零成交量」過濾抓不到它。更糟的是有些名字的高低價直接是下一個交易日的**漲跌停價**
（1301 收 65.0，那根的 high 71.5 / low 58.5）。

**它悄悄弄壞三件事：**

1. **交易日曆多一天。** 日曆是「所有股票日期的聯集」，假日期就變成一個 session：
   持有天數多算一天，3017 甚至拿到 `Entry_Date = 2026-09-20`（週日進場）。
2. **報價檔整份失效。** `quotes.sessions` 以假日期結尾，98% 的市場沒有那天的價格，
   於是每一列都被判報價缺口，欄位自檢 fail、手機出現紅色橫幅。
3. **最安靜也最嚴重：假的最低價會餵進出場回放。** 漲跌停帶的 low 大約是收盤 -10%，
   足以判出一次「根本沒發生過的停損」。

**修法——問市場，不要問日曆：** 個股層級無法分辨假 K 棒與真的冷門交易日，但全市場可以：
**近期某個日期只有一小部分股票有 K 棒，它就不是交易日。**
`data_integrity.nonsession_dates()`（近 40 個日期、低於中位數 20% 即判定非交易日；
只看近期，所以早年只有 2-3 檔的稀疏回補日期不受影響）。

- `chip_verifier.verify_candidates` 在全市場載入後就地剔除那些列（指標不會看到假高低價），
  並從價量庫刪除（`purge_nonsession_bars`）——放在這裡，桌面版與雲端都會生效。
- `holding_tracker._trading_calendar` 與 `quote_feed._recent_sessions` 各加一道同樣的防線，
  即使資料庫在修正前就被污染也不會算錯。
- 11 項新測試涵蓋：假日期判定、稀疏舊日期不可誤刪、冷清但真實的交易日不可誤刪、
  實際刪除、兩個讀取點的防線。

---

### fix: 欄位自檢不再把「資料可信但有軟性標記」當成警告

**Files:** `scanner/result_checks.py`、`tests/test_result_checks.py`

`data_integrity` 明確分成兩類：**硬錯誤**（NaN、非正數、OHLC 順序錯、重複日期）會讓該檔
`Integrity_OK = False`；**軟性觀察**（單日跳動 >10.5%、資料缺口、歷史太短）只是附註，資料仍可信。
但欄位檢查寫成「`Integrity_OK` 為真卻帶任何標記就警告」，於是每天都對健康的股票亮黃燈——
2026-09-20 的發布就是兩檔 OTC 只因為 `jump` 標記被點名。

改成只有硬錯誤標記才算矛盾（警告），軟性標記改列為資訊。

---

### feat: 停損放寬到 -20%，並提供「先買一半、跌 10% 再補」的選用買法

**Files:** `archive/research/sandbox_scale_ladder.py`（新）、`scanner/scan_mode.py`、
`scanner/exit_rules.py`、`scanner/holding_tracker.py`、`scanner/result_checks.py`、
`gui/app.py`、`mobile/app.js`、`mobile/styles.css`、`mobile/index.html`、`mobile/sw.js`（shell v16）、
`tests/test_exit_plan.py`、`tests/test_result_checks.py`、`docs/STRATEGY.md`、`docs/TASKS.md`

**Trigger:** 1815 富喬 9/10 以 125 進場，第 6 天收 112（-10.4%），畫面持續顯示「續抱」，
且 9/10 之後沒有任何新推薦。使用者問：跌破某個價位是不是該賣一部分、更低再買回？
跌下來是不是該加碼、漲上去是不是該分批賣？並要求新聞（升降息、戰爭）納入評估。
另外指定「近三年為主，不要被太舊的資料稀釋」。

**回測（`sandbox_scale_ladder.py`，564 筆 CORE+ 首日進場 2017-2026，含手續費與證交稅）：**
新寫的多筆委託路徑模擬器，已與 `exit_rules.replay_exit` 交叉比對（3000 條合成路徑只有 53 條不同，
全是 replay_exit「同根 K 最壞順序」的保守假設造成）。主要看近 3 年（n=346），舊資料只驗方向。

| 做法 | 近 3 年 勝率 / 每筆 | 判定 |
|---|---|---|
| 現行（停損 -15%） | 66.8% / +1.71% | 基準 |
| 跌 8%/10% 先賣一半 | 59.8~62.1% / 更低 | 否決（舊資料同方向） |
| 先賣一半、更低買回 | 63.0~64.2% / +2.2% | 否決（勝率降、最大虧損 -25%） |
| 漲 +10%/+20% 分批賣 | 54.0~56.9% / 持平 | 否決（砍掉右尾） |
| **停損 -20%** | **68.5% / +2.23%** | **採用** |
| **半倉＋跌 10% 加碼＋停損 -20%** | **71.4% / +1.49%**（最差一成 -9.0%） | **採用為選用買法** |

**新聞（使用者要求的「每天搜尋影響股市的新聞」）：** 用 AI 讀歷史新聞無法回測（模型已知後來結果）。
改測可取得的市場反應代理——台股開盤前最後一個美股交易日的 SOX／VIX：

- 美股大跌（SOX 1 日 <= -3%、VIX >= 25）就不進場：**被跳過的反而更好**（近 3 年 75% 勝 / +3.45%）。
- 持有中遇美股大跌隔天開盤先出：勝率 -3~-5pp、每筆 -0.4~-0.6pp。
- 結論：照新聞恐慌反應在歷史上是虧的，與抄底艙的結論一致。新聞只適合當顯示資訊，不改買賣規則。

**採用門檻**（`sandbox_scale_ladder.py gate`）：兩個候選在近 3 年／舊資料／全樣本的
win、bootLo、h1、h2 全部 >= 原規則，20/20 季勝率不低於原規則。停損寬度是平台不是尖峰
（-25% 與不停損只多約 0.3pp，最差單筆一路惡化到 -33%），所以停在 -20%。

**實作重點：**

1. `PRELAUNCH_STOP_PCT` 0.15 → 0.20，`exit_rules.DEFAULT_RULE` 同步；既有持倉一併套用新停損
   （使用者決定）。`STRATEGY_VERSION` → `prelaunch-2026-09-20`。
2. 新增 `PRELAUNCH_ADD_PCT = 0.10`：掃描列多出 `Add_Price`，持倉追蹤多出 `Plan_Add_Price`
   與 `Add_Hit_Date`（第一次跌到加碼價、且部位還在的日期；停損那根算進去，鎖利/停利那根不算）。
3. **所有價位錨定第一筆成交價**，不是攤平後的均價——PWA 的 `activePlan` 原本用移動加權平均，
   分批買法會把停損一路往下拖。`replay()` 因此多回傳 `first_buy_price` / `cycle_buys`，
   賣光再買回會重新起算一輪。舊持倉在載入時就地推導，不改寫 IndexedDB。
4. 持倉卡片的建議句不再只寫「續抱」：改成「續抱。跌破 X 先出場；分批買法可在 Y 補另一半；
   漲到 Z 後停損上調到 W」，並在收盤到加碼價時進「待處理」清單。已出場的列不顯示加碼價。
5. `result_checks` 註冊三個新欄位並檢查價位恆等式（`Add_Price = 收盤 x 0.9`、
   `Plan_Add_Price = fill x 0.9`、加碼日不得早於進場日）。

**注意：** 2026-09-20 之前的 ledger `rule_return_pct` 是 -15% 停損算出來的，之後是 -20%，
要比較請用 `picks.rule_version` 分群（已記在 TASKS）。

---

## 2026-09-14

### feat: 雲端每次掃描後逐欄自我檢測，發現缺口就自動補齊

**Files:** `scanner/result_checks.py`（新）、`tools/check_scan_result.py`（新）、
`tests/test_result_checks.py`（新）、`scanner/quote_feed.py`、`scan_headless.py`、
`config/settings.py`、`.github/workflows/scan.yml`、`.github/scripts/scan_timer.sh`、
`mobile/app.js`、`mobile/index.html`、`mobile/sw.js`（shell v14）

**Trigger:** 重新檢視 2026-09-14 雲端實際發布的 `scan_result.json`（42 列 × 96 欄）、
`quotes.json`、`recommendations.json` 與 Actions 執行紀錄。之前的防線都在上游
（feed 門檻、單根 K 棒完整性、買進閘門），沒有任何一道檢查「手機真正拿到的檔案」。

**實際發現並修正：**

1. **同日兩個 run 互相搶 push，排程 run 整個變紅、Pages 沒部署。** 13:14Z 的排程 run
   與 13:13Z 的 dispatch run 從同一個 base commit 出發，後者先 push，前者 `git push`
   被拒（non-fast-forward）→ job 失敗，且因為上傳 Pages 的步驟排在 commit 之後，
   這次掃描根本沒有發布。修正：上傳 Pages 移到 commit 之前；push 失敗時 fetch +
   `rebase -X theirs` 重試 5 次，衝突時本次狀態檔優先（同一天的資料，較晚產生者為準）。
2. **掉榜個股停止更新，持倉估值斷線（F04 回歸）。** 只有當天候選名單會被抓價，
   落榜後 `price_volume.db` 就不再更新，`quotes.json` 尾端變 null。近 30 個交易日
   帳本挑過的 95 檔中有 20 檔尾端缺價，最長 18 個交易日（2867，8/19 之後）。
   修正：`quote_feed.refresh_tracked_prices()` 在匯出前補抓「近 30 日曾入選、最新
   K 棒落後」的名單（走既有 `multi_fetch_and_save_batch`）。
3. **沒有任何欄位層級檢查。** 新增 `scanner/result_checks.py`：96 欄註冊表
   （型別 / 可否為空 / 合理範圍 / 手機是否讀取）、跨欄恆等式（停損 < 進場 < 目標、
   `Hold_Day + Hold_Remaining == Hold_Total`、`Fill_* = Entry_Open × 倍數`、
   `Core_Plus` 對三個門檻、`Buy_Ready` ⇒ 每道閘門、`Sup/Res_Gap_Pct` 公式、收盤在當日
   高低之間…）、meta 一致性（data_date vs session / 日曆 / 大盤判定 / 報價檔 as_of、
   count vs rows、degraded / data_lag）、報價檔覆蓋（每檔入選股都有當日收盤且與
   `Close_Price` 相符；近期入選股不得尾端缺價）、建議檔（active 建議必須掛回列上、
   名稱不得亂碼）。

**流程：** `scan_headless` 匯出後立即檢測並把結果寫進 `meta.checks`；`scan.yml` 再跑一次
`tools/check_scan_result.py --annotate` 產生 Actions 註記；歷史留在 `data/scan_checks.json`
（滾動 60 筆，隨 ledger 一起 commit）。**「錯誤」= 型別 / 空值 / 恆等式破壞 → `status=fail`**，
檔案照樣發布（有解釋的壞檔勝過沒解釋的舊檔）但手機顯示紅色橫幅、`scan-timer` 視為未完成
並在 19:30 前持續重掃；「警告」= 範圍或報價缺口，資料可用。手機「研究」頁新增
「欄位自我檢測（雲端）」明細表。

**新增「停損（跌破先出）」欄位與出場訊號（使用者 2026-09-14 提出：沒有一個欄位說跌破多少先賣）。**
`scanner/exit_rules.py` 新增 `replay_exit()`（`simulate_exit` 改為呼叫它，帳本評分與現場註記共用同一套
事件順序），`holding_tracker` 從推估成交日重播到今天，每列輸出：
`Plan_Stop`（唯一要盯的價位：未進場＝參考價 × 0.85；已進場＝成交價 × 0.85；漲到 +6% 啟動鎖利後上調到
成交價 × 1.02，只升不降）、`Plan_Armed`、`Exit_Signal`（`stop` / `lock` / `tp` / `time`，規則已經出場）、
`Exit_Signal_Date` / `Exit_Signal_Price`、`Exit_Note`。手機卡片的「參考停損」改為「停損（跌破先出）」，
已觸發者顯示紅色「已跌破停損 · 先出場（日期 @價位）」，建議頁頂端彙總已觸發檔數。欄位自我檢測同步加入
恆等式（待進場列 Plan_Stop == 參考停損；已進場列 == 成交價 × 0.85 或 × 1.02；有訊號必有日期與價位）。
`tests/test_exit_plan.py` 13 個測試，含 `replay_exit` 與 `simulate_exit` 逐棒一致性。

**手機「立即更新」不再需要金鑰。** 沒設金鑰時按鈕改開 GitHub 的 Run workflow 頁，用手機
瀏覽器的 GitHub 登入按一下即可；金鑰只在想 App 內一鍵觸發＋自動輪詢時才需要（改為「進階」）。
每日更新本來就由 `scan-timer` 在雲端自動完成，與金鑰無關。

**對 2026-09-14 實際 payload 的檢測結果：** 0 錯誤、1 警告（`quotes_gap_tracked` × 18，
即上面第 2 點，補抓上線後應歸零）。50 個新單元測試，全部 125 個測試通過。

---

## 2026-09-09

### refactor+fix: 把「系統說了什麼」和「你真的買了什麼」分開

**Files:** 全專案重整 + `portfolio/`（新）、`tests/`（新）、`tools/`（新）、
`archive/`（新）、`scanner/`、`storage/`、`gui/`、`mobile/`、`gemini_hook/`、
`config/settings.py`、`.github/workflows/scan.yml`、`docs/TASKS.md`（新）、`README.md`（新）

**Trigger:** `docs/專案完整分析與優化方案_2026-09-09.md` —— 一份 618 行的完整稽核，
列出 26 個缺陷、14 張建議資料表、5 個介面頁面與 24 個研究方向。該報告**只做設計，
沒有改程式**，並明講「尚未將新交易規則或新介面套用到正式程式」。本次把它落地。

**一句話的根因：系統把「今天重算的清單」當成「當初的建議」和「使用者真的持倉」。**
建議買價每天跟著收盤重算；`Entry_Open` 是推估日的開盤價卻被當成成交；手機持倉掉榜就沒價格；
沒有股數、沒有成交明細、移除持倉直接刪資料。

#### 資料夾整理

根目錄 55 個 `.py` → 2 個。58 個檔案用 `git mv` 搬移（歷史完整保留為 R100）：
`archive/research/`（46 個 `debug_*`／`eval_*`／`sandbox_*`，是 STRATEGY.md 數字的出處，
所以封存不刪）、`archive/legacy/`、`tools/`、`docs/history/`。
搬移後這些腳本要 `PYTHONPATH=.` 才能執行，已在 `archive/README.md` 寫明並實測 47 支全部 import 可解析。

#### `portfolio/` —— 新的交易帳本（零外部相依）

四種價格分開，不再共用一欄（報告 §5.1）：**首日建議價**（第一次通過完整閘門時固定，永不改寫）、
**最新觀察參考**（每天重算）、**實際成交價**（只有成交紀錄能建立）、**當前有效停損／停利**。
金額全程 `Decimal` 存成 TEXT——報告 §9.1 要求「不要用浮點誤差決定剛好有沒有獲利」。
費稅版本化（0.1425% 可調、最低 20 元、賣出稅 0.3%），升降單位依價位分級。

61 項測試，**用的是報告自己的數字不是自己編的**：§6.2 十日表（D10 累計 +10,000）、
§6.3 費稅（145.35／159.60／336.00 → 淨額 9,359.05）、§12 驗收情境。
其中兩項專門釘死報告警告過的錯誤：把每日累計值相加會得到 45,000 而不是 10,000；
`gross`／`net_book`／`net_if_liquidated` 是三個不同的數字，不可共用「總獲利」標籤。

#### 缺陷修正（20 項完成、4 項部分、2 項待辦）

F01 首日建議固定並在兩端顯示。F02 桌面改用成交價錨定的水位並標示「價格基準」。
F04 新增 `quotes.json` 行情包（今日名單 ∪ 帳本追蹤），掉榜持倉仍有價格。
F05 手機持倉改為 IndexedDB 成交帳本。F06 買進閘門補上資料日期、`Integrity_OK`、
未知持倉狀態三道封鎖。F07 後端裁決為權威，前端只能降級不能放寬。
F09 修正同日事件順序。F15 大盤改為 fail closed 並回傳新鮮度。F17 picks 記錄買進裁決。
F18 空名單照常發布，離開碼 0／1／2 分開。F20 Service Worker 固定快取鍵。
F25 批次寫入改為單一交易。詳見 `docs/TASKS.md`。

**行為變更（會影響掃描結果）：** `_safe_bool` 原本用 `astype(bool)`，而 `float("nan")` 是 truthy，
所以「算不出來的條件」會變成「通過的條件」（報告 §15 已重現 `Core_Plus=NaN` → `Buy_Ready=True`）。
修正後，`Cond_*` 為 NaN 的列不再被選入。

#### 兩個報告沒抓到、但更要緊的發現

1. **採用中的出場參數是在有錯的模擬器上選出來的。**
   `archive/research/eval_winrate_round2.sim_trail` 的註解（51–53 行）描述的正是 F09 修正後的
   順序，程式碼做的卻相反——**和 F09 是同一個錯**。§D.4 宣稱「與獨立模擬器交叉比對最大差
   0.005pp」只證明兩份程式錯得一樣。連帶：停損 15／停利 20／啟動 6／鎖利 2 現在屬於
   **未經正確驗證**。已在 `docs/STRATEGY.md` D.4 就地加註。

2. **2026-09-09 的資料更新有 763 檔沒有真正更新。**
   `data/audit_20260909/refresh_result.json` 顯示 1,968 檔裡 1,201 檔 `updated`、
   **763 檔 `invalid_ohlc_preserved_original`**（保留舊資料），而 `errors` 是空陣列、
   `price_checks` 都是 `ok`。報告 §13 P1 寫的是「14 檔」。39% 的宇宙和 14 檔是不同量級的問題。
   隔離的 6,572 筆 K 棒影響 565 檔，原因全部是 `provider_ohlc_inconsistent`，與 F08 同一根因。

#### 發布安全

`tools/check_ledger_public.py` + CI 步驟：帳本必須只有建議、不能有真實成交才准提交。
排程本身不會寫入持倉，所以 `positions` 非空就代表本機的真實帳本被誤加入版控——
public repo 的 git 歷史刪不掉，寧可讓 build 失敗（報告 §9.2）。

---

## 2026-08-06

### fix: the app now enforces the buy rule it was validated on

**Files:** `scanner/scan_mode.py`, `scanner/holding_tracker.py`,
`scanner/signal_ledger.py`, `scan_headless.py`, `gui/scan_worker.py`,
`mobile/app.js`, `mobile/styles.css`, `docs/STRATEGY.md`

**Trigger:** a month of live use felt like a very low win rate, with the
suspicion that "the suggested buy is always the close, so I always buy high".
Audited 2026-07-01..08-06 (25 sessions, 946 rows) against the cloud ledger,
freshly refetched daily bars and the live PWA payload. Full write-up in
`docs/STRATEGY.md` appendix D.

**The suspicion did not survive the data.** Next-day open vs signal close was
+0.02% mean / 0.00% median across all picks. The entry price was never the
problem. Three implementation defects were:

1. **The market gate was advisory.** `renderTradeable()` and the 核心+ badge
   checked `rank<20 && Core_Plus && OTC` only. `regime_map()` in the backtests
   every published win rate comes from defines risk_on as TAIEX above BOTH its
   20MA and 60MA. Result: on 10 of 25 days the banner said "hold off on new
   positions" while the counter underneath said "N stocks meet the buy rule".
   16 of the month's 21 核心+ signals fired on non-green days; the 15 that
   completed won 26.7% (median -15.00%, straight into the disaster stop) vs
   80% for the 5 on green days.
2. **79% of the list was carry-over.** Hysteresis retains a name down to rank
   80, so 752 of 946 displayed rows were already listed the previous day and
   118 (12%) were past their own 10-bar exit -- each re-rendered as a fresh buy
   with today's close as the "entry reference". The ~71% figure is the
   streak==1 view.
3. **Risk levels were recomputed off today's close.** On the 2026-08-06 payload
   36 of the 39 already-entered rows (92%) showed a stop that did not belong to
   their position (mean error 6.5%, max 26.7%) and 9 showed a "profit lock"
   price BELOW their own fill.

**Changes (selection logic untouched -- the list, ranking and hysteresis are
identical, so the 6-year replay still describes it):**

- `scan_mode.mark_buy_ready()` -> `Buy_Ready` / `Buy_Block`: the whole rule as
  one flag (gate + top-20 + OTC + Core_Plus + fresh signal). An unreadable
  regime blocks rather than passes. Runs after `annotate_holding` in both the
  headless and GUI pipelines.
- `holding_tracker` ships `Entry_Open` (the open on Entry_Date) plus
  `Fill_Stop_Loss` / `Fill_Trail_Arm_Price` / `Fill_Trail_Lock_Price` /
  `Fill_Target_Price` derived from it.
- PWA: badge is now 核心+ 可買 / 核心+ 暫不可買·<reason>, recomputed at view
  time; the counter names the blocker; an entered row shows its fill-anchored
  levels and P&L instead of a buy price; overdue rows are dimmed; the ＋持有
  prompt defaults to the entry-day open instead of today's close.
- `signal_ledger.outcomes` gains `rule_entry` / `rule_return_pct` /
  `rule_exit`: next-open entry through the adopted exit stack. The old
  `fwd_return_pct` (close entry, no exits) was measuring a strategy nobody
  trades -- 19.2% win / -10.72% against the rule's 37.7% / -6.52% on the same
  signals. Existing rows backfill on the next scan.

**Verified:** ledger simulation cross-checked against an independently written
simulator over 681 rows, max difference 0.005pp; PWA checked in-browser on the
green path, the blocked path, and a legacy payload with none of the new columns.

**Sample-size warning:** 20 completed 核心+ trades and 5 green-light ones in a
single -15% month. The direction matches the 6-year replay; the percentages
must not be read as win-rate expectations.

---

## 2026-07-06

### feat: ledger-validated trade rules for mode_prelaunch + decision-card UI

**Files:** `scanner/scan_mode.py`, `gui/app.py`, `gemini_hook/prompt_builder.py`

**Evidence** (real-trade simulation on the signal ledger + 16-scan-day replay;
full numbers in `docs/EVAL_PLAYBOOK.md`, reproducible via `eval_realtrade.py`):
the limit-below entry was adverse selection (filled picks +0.54% vs no-fill
+9.67% over 5d) and the ~6% stop sat inside daily noise (turned a +3.03% mean
into +0.16%). The prelaunch alpha concentrates in OTC names (win 71% vs 64%).

**Rule change (mode_prelaunch ONLY -- other modes untouched, no as-directed
evidence for them yet):** `Suggested_Buy_Price` = signal-day close, i.e. a
reference for a next-day OPEN market entry (no limit order);
`Strict_Stop_Loss` = reference * 0.90 (disaster stop, recompute off the actual
fill); `Risk_Pct` = 10.0. Exit is time-based: 5th trading-day close.

**UI:** new "Market" column + "OTC only" display filter (default ON; hides
confirmed TSE rows only, unknown market stays visible); per-mode rule-card
banner (prelaunch shows the entry/stop/exit card, momentum_leader shows a
negative-expectancy warning -- 23% win as-directed); regime banner now appends
position advice (risk_on: normal, otherwise: halve NEW positions only -- a
hard gate also cuts rebound cohorts, so it sizes rather than blocks).

**IMPORTANT for evaluation:** picks recorded from 2026-07-07 onward carry the
new buy/stop semantics. Before/after comparisons must split on this date.

### feat: prelaunch liquidity measured in turnover VALUE, not share count

**Files:** `scanner/market_filter.py`, `analyzer/trend_analysis.py`,
`eval_realtrade.py`

**Problem:** the prelaunch prefilter required >=300k SHARES/day and ranked the
pool by share volume; the Launch_Score gate required vol_ma20 > 300 lots. Both
structurally exclude high-priced stocks: 7769 (median ~700 lots but ~15e8
TWD/day, ranking #63 by value vs #453 by lots) was completely invisible to the
scanner through a 9x run, despite Launch_Score >= 70 firing across 11 months
when computed on its history. 37 of the 55 stocks priced >=1000 TWD (June
2026) were missing from the fetch universe, including 3443 (~157e8 TWD/day).

**Evidence:** A/B replay on research_prices.db (full market, 2025-09..2026-06,
~190 scan days, ~7.5k trades), everything constant except lots->value: win
rate improved in every cell -- all-picks hold 49.3->50.5%, OTC hold
51.3->52.1%, all+stop10 47.2->48.2%, OTC+stop10 49.1->49.6%; mean +0.22 to
+0.37pp per trade; 7769 pick-days 38->96.

**Change (mode_prelaunch only):** prefilter floor = single-day turnover
>= 0.5e8 TWD (0.5x safety of the downstream gate), pool ranked by turnover,
cap 300 unchanged; Launch_Score gate = 20d-avg turnover >= 1e8 TWD (replaces
the 300-lot floor -- note this now also EXCLUDES low-priced names under 1e8
value that previously passed on share count). Other modes untouched.
eval_realtrade.py replay updated to match; its ledger sanity overlap on scan
days before 2026-07-07 reads slightly lower by construction.

---

## 2026-06-25

### data: forward-performance ledger -- close the open loop

**Files:** `scanner/signal_ledger.py` (new), `backfill_ledger.py` (new),
`gui/scan_worker.py`, `config/settings.py` (`SIGNAL_LEDGER_FILE`),
`scanner/chip_verifier.py` (adds `Data_Date`, `Market`)

**Problem:** the scanner was open-loop -- it emitted a shortlist and forgot it
(`scan_result_latest.csv` overwrites itself, `scan_state` stores only IDs). So
the live system had NO record of what it recommended or whether it worked; the
only evidence was the survivorship-biased research backtest.

**Fix:** an append-only SQLite ledger (`signal_ledger.db`). Every scan records
its picks (`scan_session, mode, stock_id, scores, buy/stop, bar_date`,
idempotent per day) and backfills realized 5/10/20-day forward returns + MFE/MAE
once the bars exist. Wired into `scan_worker` so **pressing Scan auto-completes**:
matured-but-missing names (picks that left the universe and stopped updating) are
re-fetched in the same pass. `python backfill_ledger.py` runs it headless +
prints live hit-rate / Surge_Score calibration. Forward return anchors the entry
close on the backfill series (not the scan-time snapshot) so a dividend
re-adjustment between scan and backfill cannot skew it.

### data: per-stock integrity audit (B) -- non-destructive flags

**Files:** `scanner/data_integrity.py` (new), `audit_data.py` (new),
`scanner/chip_verifier.py` (adds `Integrity_OK`, `Integrity_Flags`, `Recent_Jump`)

**Problem:** every score is a deterministic function of the stored OHLCV, so one
bad bar silently corrupts MA / breakout / RS / forward-return for that name, with
nothing to surface it.

**Fix:** hard, non-speculative checks per series -- NaN / non-positive, OHLC
ordering, duplicate dates, close-to-close moves > +-10.5% (TW daily limit, so a
larger move is an un-adjusted corporate action / feed glitch / no-limit-board
stock), exact trading-day gaps (vs the whole-market date union), and
short-history (MA60 < 60 bars, 52w/RS < 240). Attached to the scan output as
columns; **never alters a score or drops a row** -- a >10% move can be a real
no-limit stock, so the call stays with the caller. Audit of the live db (451
stocks): 0 NaN / OHLC / dup errors, 1 internal gap, 28 over-limit jumps across 18
stocks (ALL reproduced by the yfinance adjusted feed -> no basis seams), 99.6%
fully clean.

### scan: breakout reference fixed to the prior-20 HIGH (was max of closes)

**Files:** `analyzer/signal_evaluator.py` (`Is_Breakout_Signal`),
`scanner/chip_verifier.py` + `scanner/scan_mode.py` (`Max_Price_20_Prev` ->
`High_20_Prev`), `debug_audit_all.py`, `debug_breakout_validate.py` (new)

**Problem:** `Max_Price_20` was `close.rolling(20).max()`, so `mode_breakout` and
`Is_Breakout_Signal` fired on "close > max of prior 20 CLOSES" -- a stock could
trigger while still below its actual recent highs (e.g. 8042 closed 195.5 above
the 192.5 close-max but the real prior high was 206, i.e. 5% BELOW its high).
Inconsistent with `Donchian_Break`, which already used highs.

**Fix:** breakout reference is now the prior-20 intraday HIGH excluding today
(`High_20_Prev`), the conventional range breakout. Point-in-time validation on
`research_prices.db` (1957 stocks, 6y, `debug_breakout_validate.py`): NEW vs OLD
forward-20d P(+10%) **47.9% vs 47.3%**, lift **1.51 vs 1.49**, signals **27147 vs
37173**; the 10028 removed signals were weaker (**45.6%**). Strictly removes false
breakouts (high-max >= close-max, so it can only drop, never add).

### analyzer: MA60 / 52w-high require a full window

**Files:** `analyzer/support_resistance.py`, `analyzer/trend_analysis.py`
(`_MIN_52W_BARS = 240`)

**Problem:** `calc_moving_averages` used `min_periods=1`, so a 30-bar stock got an
"MA60" that was a 30-bar mean -- and that MA60 is the `close > MA60` trend gate
and the buy/stop anchor. `calc_52w_position` / `calc_launch_score` used
`min_periods=63`, labeling a 3-month high as a 52-week high for young listings.

**Fix:** MA20/60/10 require their full window (None otherwise, so a short-history
name fails the gate instead of passing on a half-formed average); 52w-high
requires 240 bars. Impact measured on the live db: only **1** stock loses MA60
and **16** lose the 52w metric; **200 healthy stocks checked, 0 changes** to MA60
or Dist_52W -- the fix touches only short-history names.

### audit: price basis verified single -- "unify basis" (A) not needed

A 40-stock sample showed **zero** divergence from the yfinance adjusted feed, and
all 28 over-limit jumps were source-level (no raw/adjusted seams in the pipeline).
Raw and adjusted differ only at ex-dividend/ex-rights gaps, which the integrity
jump check (`recent_jump` within 60 bars = the longest short-term MA) already
catches. Decision: do not re-architect the price basis; rely on the B guard.

---

## 2026-06-22

### scan: early pre-launch mode + hysteresis selection (kills daily list churn)

**Files:** `analyzer/trend_analysis.py` (new `calc_launch_score`),
`scanner/scan_mode.py` (new `mode_prelaunch`, `select_with_hysteresis`),
`scanner/scan_state.py` (new), `scanner/market_filter.py`,
`scanner/chip_verifier.py`, `gui/scan_worker.py`, `config/scan_modes.json`

**Problem:** the daily recommendation list changed almost completely day-to-day
("事後諸葛"). Root cause: the headline modes triggered on the SAME bar as the move
(`mode_breakout`: close>20d-high + today vol>5d*2; `mode_short_explosion`: today
gain>=4% + near-high + vol>5d*2.5). Replayed on the research db
(`debug_churn_persistence.py`, 1431 days, 2.6M bars): short_explosion had
day-to-day list overlap **0.05**, survived **1 day**, and a NEGATIVE forward-20d
median (**-1.9%**); breakout 0.13 / 1 day / -1.3% -- i.e. they flag the climax,
when the move is already over. `mode_squeeze` was anti-predictive (lift **0.30**).

**Fix:** select on the *pre-launch state*, not the event.
- **`Launch_Score`** (`calc_launch_score`): 0-100, validated on the research db
  (`debug_early_design.py`). 3m momentum .30 + 5d freshness ("not-yet-run") .25 +
  near-52w .20 + up-volume accumulation .15 + box tightness .10, gated by
  close>60MA & liquidity. Adds `Ret_5D_Pct`. Pivot-proximity / volume-expansion
  terms were tested and dropped (they pulled selection back toward the climax).
- **`mode_prelaunch`** ranks by `Launch_Score` and is now the default mode.
- **`select_with_hysteresis`** (enter top 20 / hold top 80) + a per-mode prior-ID
  store (`scanner/scan_state.py`), with held names force-included in the
  `market_filter` prefilter so they cannot be washed out by a quiet-volume day.

Live-flow replay (`debug_prelaunch_live_sim.py`): list overlap **0.05-0.13 ->
0.79**, median time-on-list **1 -> ~6 days**, forward >=25%/20d lift **~2.5**,
with the median name flagged at only **~0% trailing-5d** (before the run, not
after). `breakout` / `short_explosion` relabeled as monitors (not buy lists);
anti-predictive `squeeze` dropped from `config/scan_modes.json` (function kept).

Caveat: the research db has no chip/inst data, so the institutional-accumulation
angle (`Foreign_Net_5D` etc.) is not yet folded into `Launch_Score`.

### GUI: slim the main list 21 -> 9 columns, diagnostics to the detail panel

**File:** `gui/app.py`

The main table was 21 columns wide. Trimmed to 9 -- identity (`代號`/`名稱`), the
trade plan (`建議買入`/`停損價`/`風險%`), the ranking score (`起漲分`), and two
at-a-glance context columns (`3月漲幅%`, `外資5日`). Everything diagnostic moved
into the double-click detail panel: new `訊號燈號` section (`箱縮`/`吸籌`/`大戶`/
`MA多頭`/`Donchian`/`MACD金叉`), new `距離（%）` section (`距支撐`/`距壓力`/
`距52週高`/`RS超額`), and `噴發分` added to the `噴發要素` section.

---

## 2026-06-18

### scoring: recalibrate Surge_Score to spread (it was saturating)

**File:** `analyzer/trend_analysis.py`

Among the filtered candidates shown in a scan the score sat at 80-100 (42% >=80),
because the normalization caps were too low (ATR maxed at 6%, momentum at 50%).
Widened the denominators (ATR/0.11, ret60/1.0, ret20/0.45, dist60/0.45) so the
candidate median is ~50 and p90 ~73, while the >=30% top-decile lift is unchanged
(3.34 -> 3.31). See `debug_surge_dist.py`.

### chip: free whole-market shareholding (TDCC) replaces paid FinMind

**Files:** `ingestion/tdcc_holders.py` (new), `scanner/chip_verifier.py`,
`gui/app.py`

`大戶/散戶持股%` was blank ("-") because the FinMind chip path is paid/disabled.
New `ingestion/tdcc_holders.py` pulls the TDCC open-data shareholding-distribution
(one request, whole market ~3990 stocks, weekly, free) and derives large-holder
(>=400 lots) / retail (<=50 lots) percentages, with week-over-week change building
over time. Wired into the scan as a single weekly-cached request.

### chip: daily institutional net buy/sell (TWSE T86 + TPEX), free

**Files:** `ingestion/inst_trades.py` (new), `scanner/chip_verifier.py`,
`gui/app.py`

New `ingestion/inst_trades.py` fetches the three-institution daily net buy/sell
(foreign / trust / dealer) for the whole market -- TWSE T86 (dated, supports
backfill) + TPEX openapi -- in lots, storing daily snapshots. Adds `Foreign_Net`,
`Trust_Net`, `Foreign_Net_5D`, `Inst_Buy_Days` columns and a detail-panel section.
TPEX dates are ROC (`1150618`) and are converted to Gregorian.

### chip: validated -- holdings LEVEL is not predictive; flow is a confirmation

**Files:** `gui/app.py`, validation scripts (`debug_chip_vs_return.py`,
`debug_inst_backtest.py`)

- The static large-holder / retail percentage does NOT predict moves: cross-
  sectionally large% correlates -0.06 with trailing return (very-high large% =
  locked/dead float), retail% +0.07. So holdings level was NOT added to the score.
- Backtested foreign flow on 76 backfilled days: foreign 5-day net as a FILTER on
  high-surge candidates raises precision (23.5% -> 24.9% for a >=30% move), but
  BLENDING it into the score additively HURTS (down to ~22-23%). So Surge_Score
  stays pure price/volume; `Foreign_Net_5D` is surfaced as a main-table
  confirmation column (prefer foreign-buying among high-surge names).

---

## 2026-06-17

### scoring: replace Explosion_Score with Surge_Score as the headline metric

**Files:** `analyzer/trend_analysis.py` (new `calc_surge_score`),
`scanner/chip_verifier.py`, `scanner/scan_mode.py`, `gui/app.py`

**Problem:** Backtests on the full-universe research db (6 years, 1946 stocks)
showed `Explosion_Score` (box-tightness + volume dry-up + bias) is *inverted* —
its top decile had a lift of **0.47** for a >=30%/20-day move (anti-predictive).
Ranking by it picked the worst stocks. Validated across overlapping, independent
(non-overlapping), and cross-sectional tests; the inversion held every year.

**Fix:** New `Surge_Score` (0-100) = momentum x volatility x volume, gated by
trend (price > 60MA). Components by validated power: ATR(volatility) lift 2.54,
3-/1-month momentum ~2.3, up-volume bias minor; distance-to-52w-high and box
tightness carry NO signal and are excluded. Top-decile lift **3.34** for a
>=30% move. Added `ATR_Pct` column. GUI headline column `爆發分` -> `噴發分`,
plus a `波動%` column. `Explosion_Score` kept as `蓄勢分` for the squeeze mode.
Momentum modes now rank by `Surge_Score` (`_SORT_KEYS`).

### scan_mode: forward-looking momentum_leader + honest mode relabels

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`,
`scanner/market_filter.py`, `scanner/chip_verifier.py`

- **New `mode_momentum_leader`** ("起漲前動能"): empirically-derived pre-launch
  momentum screen (above 60MA, MA stack, 3M gain >=20%, 1M >=5%, up-volume bias).
  Added `Gain_3M_Pct` / `Gain_1M_Pct`.
- **`mode_bottom` rebuilt** from falling-knife (lift 0.83) to **Strong Pullback**
  ("強勢回檔買點": uptrend leader dipped to ~20MA) — backtested lift **1.55**.
- **`mode_squeeze` relabeled** "經典爆發蓄勢" -> "低波蓄勢(高勝率穩健)" (lift 0.30
  for explosions; it is a low-volatility steady mode, not an explosion screen).
- **Mode-aware ranking** (`sort_for_mode`): momentum modes were being sorted by
  `Explosion_Score`, which is inverse-correlated; now ranked by `Surge_Score`.

### data fetch: batch yfinance + bulk write, freshness, decouple chip

**Files:** `ingestion/price_volume_multi.py`, `storage/data_store.py`,
`scanner/chip_verifier.py`, `config/settings.py`

- **Batch yfinance** (`fetch_yfinance_batch`, `multi_fetch_and_save_batch`) + a
  single-transaction `bulk_upsert_stocks`: full-market fetch ~70s -> ~15s.
- **Freshness fix:** staleness is now judged against the latest trading day
  (`_latest_trading_day`, 14:00 EOD cutoff + weekend rollback) instead of a fixed
  2-day window, so a scan picks up TODAY's bar instead of lagging up to 2 days.
- **Chip (Cond_B) decoupled** from scans (`CHIP_FETCH_IN_SCAN`, default off):
  the FinMind 1.5s-throttled serial loop no longer hangs the scan; cache-only.
- **yfinance log noise silenced** (404/delisted on the .TW-vs-.TWO probe).

### calc fixes: stale rolling columns, support, dryup; full audit

**Files:** `scanner/chip_verifier.py`, `analyzer/support_resistance.py`,
`analyzer/signal_evaluator.py`

- **Max_Price_20 drift fix:** stored rolling-derived columns could drift out of
  sync with re-fetched / auto-adjusted close, feeding a stale prior-high into the
  breakout signal. Now recomputed from raw close/volume in the analysis loop.
- **Adaptive support:** added `Support_20L` and `Support_Used` = nearest level
  below price among MA10/MA20/20-day low. The old 60-day low sat 30-50% under
  price for runners (8042: 距支撐 283% -> ~18%) and was not actionable.
- **Volume_Dryup smoothed** to a 3-day average volume (was single-day), so
  Explosion/Cond_A no longer whipsaw on one spike (3236 case).
- **Full column audit** (`debug_audit_all.py`): all other columns verified
  correct to rounding (MAs, S/R, gaps, gains, MACD, Donchian, booleans).

### trade plan: stop-below-buy invariant + risk-banded stop

**File:** `scanner/scan_mode.py`

`add_trade_columns` previously could produce `stop >= buy` (e.g. 8042) and stops
1-2% from entry that get shaken out. Backtest: tight stops on volatile momentum
names get stopped prematurely ~30%. Fix: structural stop clamped into a
[6%, 13%] band below entry (always below buy, never too tight); added `Risk_Pct`.

### GUI: manual lookup, sortable columns, regime banner, trend report

**Files:** `gui/app.py`, `gui/scan_worker.py`, `scanner/market_regime.py` (new),
`scanner/regime_report.py` (new), `scanner/result_export.py` (new),
`scanner/market_filter.py`

- **Manual single-stock lookup** button: resolves market + Chinese name from the
  live snapshot (with a persistent `stock_names.json` cache — first lookup caches
  ~11500 names, later lookups are instant), runs the full pipeline, shows the row.
- **Clickable column-header sorting** by underlying value (numbers/bools/blanks),
  toggle direction, arrow indicator.
- **Market-regime banner:** TAIEX above/below its MAs -> momentum-edge tailwind
  vs headwind (the 2022 bear collapsed the edge to lift ~1.07).
- **"趨勢報告" button:** refreshes the research db (incremental) and writes a
  recent-2y explosion-fingerprint report (`data/scan_results/regime_report.md`).
- **Scan result CSV export** after every full scan (`scan_result_latest.csv`,
  latest only, utf-8-sig for Excel) with all 57+ computed columns.

### research infrastructure: full-universe multi-year backtest db

**Files:** `build_research_db.py` (new), `scanner/regime_report.py` (new),
plus validation scripts (`debug_*.py`)

- **`build_research_db.py`:** builds/maintains `data/research_prices.db` — the
  full listed universe (~1946 stocks) with ~6 years history (spans the 2022
  bear), stored separately from the live scan db. Self-healing & incremental:
  each run fills only the gaps (new/short -> full backfill; stale -> top-up;
  fresh -> skip), recomputing derived columns over the full merged series.
- Used to re-validate every conclusion on **unbiased, multi-regime** data after
  the audit found the live db was a momentum-survivor sample (median +99.5%
  return, 49% of stocks doubled, 0% halved — and no bear market).

**Key empirical findings (drive the above):** momentum-continuation (not quiet
consolidation) precedes explosions; ATR is the single best predictor of big
moves; the edge is regime-robust in trending years (~1.4x) but collapses in the
2022 bear; +10%/20d is noise now (base 24%), the meaningful move is >=30% (5%).

---

## 2026-06-14

### price/volume: multi-source fetcher with yfinance + TWSE/TPEX official API

**Files:** `ingestion/price_volume_multi.py` (new), `scanner/chip_verifier.py`,
`requirements.txt`

**Problem:**
FinMind free tier returns HTTP 402 after ~50–80 API calls per day.
Scanning 200 candidates generates 400 calls (price + chip per stock),
exhausting the quota mid-scan and leaving the remaining stocks without data.

**Fix:**
New module `ingestion/price_volume_multi.py` provides `multi_fetch_and_save`
which tries three sources in priority order:

| Priority | Source | Notes |
|----------|--------|-------|
| 1 | **yfinance** | Free, fast. TSE: `2330.TW`, OTC: `3008.TWO`. No token. |
| 2 | **TWSE API** (TSE) / **TPEX API** (OTC) | Official exchange endpoints. Free, no token. Fetches month-by-month, with 0.4 s politeness delay between months. |
| 3 | **FinMind** | Original source, kept as last resort. |

`chip_verifier.py` now calls `multi_fetch_and_save(stock_id, market=market)`
instead of `pv_fetcher.fetch_and_save(stock_id)`. The `market` value
(`"TSE"` / `"OTC"`) is read from the candidates DataFrame row so the correct
exchange API is chosen.

Combined with the existing cache check (`PRICE_CACHE_DAYS = 3`), FinMind is
only reached when both yfinance and the official exchange API fail **and** the
local cache is stale — effectively avoiding 402 errors in normal operation.

---

### chip_verifier: local cache to avoid FinMind 402 rate-limit errors

**File:** `scanner/chip_verifier.py`

**Problem:**
Scanning 200 candidates generates ~400 FinMind API calls per run
(price + chip per stock). The free-tier quota is exhausted mid-scan,
returning HTTP 402 for the remaining stocks. Those stocks then have no
local data, so they are silently dropped from results.

**Fix:**
Added `_is_cache_fresh(file_path, stock_id, max_age_days)` which reads the
local Excel sheet for a stock and checks whether the most recent date row
is within `max_age_days` of today.

Before each `fetch_and_save` call, the cache is checked:

| Data type | Cache threshold | Constant |
|-----------|----------------|----------|
| Price / volume | 3 calendar days | `PRICE_CACHE_DAYS = 3` |
| Chip (shareholding) | 8 calendar days | `CHIP_CACHE_DAYS = 8` |

If the cache is fresh the API call is skipped entirely and a `cache hit`
line is printed instead. On the first scan of the day, all 200 stocks are
fetched as before. On every subsequent scan in the same session (or same
day), zero API calls are made for stocks already cached.

---

### chip_verifier: remove is_golden / is_breakout gate

**File:** `scanner/chip_verifier.py`

**Problem:**
`verify_candidates` only added a stock to the result set when it satisfied
`is_golden OR is_breakout` in the past 5 days.  This created a double-filter
architecture:

1. `chip_verifier` — gated by `is_golden | is_breakout`
2. `apply_scan_mode` — mode-specific Pandas masks

The gate made most scan modes find zero targets:

- `mode_bottom` targets stocks *below* MA60 in accumulation phase.
  Those stocks never trigger `is_golden` (no tight-box breakout) or
  `is_breakout` (no 20-day high breach), so they were invisible to every scan
  mode before even reaching `apply_scan_mode`.
- `mode_squeeze` required `Cond_A AND Cond_C` to pass the gate; stocks with
  `Cond_A` only (volume not yet biased upward) were silently dropped.
- `mode_short_explosion` / `mode_breakout` could occasionally survive, but
  the gate's `BREAKOUT_VOLUME_MULTIPLIER = 2.5` was the same threshold as
  scan mode's 2× check, making survival near-impossible.

**Fix:**
- Removed the `if is_golden or is_breakout:` conditional block.
- `is_golden` and `is_breakout` are now **informational columns** in the
  result DataFrame, not selection gates.
- `Cond_B` is no longer a hard requirement for `is_golden`; it remains a
  column so `mode_bottom` can use it as a filter condition.
- `best_row` (the most recent signal row) is replaced by `latest`
  (the most recent calendar row) for all per-row metric reads.
- `calc_all`, `calc_trend_analysis`, and `_get_volume_stats` are now called
  **for every candidate**, not only those that passed the old gate.
- `apply_scan_mode` is now the **sole** filter layer.

**Impact:**
- Each scan now returns up to 200 rows before scan-mode filtering.
- Scan time is roughly the same (expensive I/O already ran for all 200
  candidates; only the conditional `calc_all` / `calc_trend_analysis` calls
  move outside the `if` block).

---

### scan_mode: add mode_short_explosion (Short-Term Explosion)

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`,
`scanner/chip_verifier.py`

**New mode key:** `mode_short_explosion`

**Filter conditions (all must be True):**

| # | Condition | Column(s) used |
|---|-----------|----------------|
| 1 | 20d avg volume > 1000 lots | `Vol_MA20` |
| 2 | Intraday amplitude ≥ 5% | `High_Today`, `Low_Today` |
| 3a | Daily gain ≥ 4% vs previous close | `Close_Price`, `Close_Prev` |
| 3b | Close within 1.5% of day high | `High_Today`, `Close_Price` |
| 4 | Volume > 5d avg × 2.5 | `Vol_Today`, `Vol_MA5` |
| 5 | Close > MA5 > MA10 | `MA5`, `MA10` |

New columns added to `_get_volume_stats` (and result dict):
`High_Today`, `Low_Today`, `Close_Prev`

NaN guard: if any required price column is None for a stock, that row is
excluded (safe Pandas NaN propagation, no explicit isnull checks needed).

---

### scan_mode: add modes 1–3 (prior session)

**Files:** `scanner/scan_mode.py`, `config/scan_modes.json`

| Mode key | Label | Key conditions |
|----------|-------|----------------|
| `mode_squeeze` | Classic Squeeze - 經典爆發蓄勢 | price<150, Vol_MA20>500, close>MA60, Cond_A |
| `mode_breakout` | Momentum Breakout - 動能突破發動 | Vol_MA20>1000, close>20d-high, vol>MA5×2 |
| `mode_bottom` | Bottom Accumulation - 跌深大戶建倉 | close<MA60, MACD_Hist_Turn, Cond_B |

---

### market_filter: expand pre-filter pool

**File:** `scanner/market_filter.py`, `config/settings.py`

- `PREFILTER_TOP_N = 200` (was `VOLUME_TOP_N = 50`)
- Price cap removed from pre-filter for all modes except `mode_squeeze`
  (`mode_squeeze` pre-applies `close < 150` to avoid scanning irrelevant
  high-price stocks before the mode filter stage).

---

### large_holder: fix KeyError on missing 'percent' column

**File:** `ingestion/large_holder.py`

Added early return in `_transform` when the API response does not include a
`percent` column (occurs for certain stock categories on FinMind).
Previously caused a `KeyError: 'percent'` logged for every such stock.
