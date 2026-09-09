# archive/ — 封存區

這裡的檔案**不參與**每日掃描，也不被 `scanner/`、`ingestion/`、`gui/`、`mobile/` 匯入。
保留而不刪除，是因為 `docs/STRATEGY.md` 與 `docs/history/EVAL_PLAYBOOK.md` 引用它們作為
勝率與參數的證據來源；刪掉就等於讓現行策略數字失去出處。

## research/ — 一次性研究與除錯腳本（46 個）

| 前綴 | 數量 | 內容 |
|---|---:|---|
| `eval_*` | 9 | 勝率／參數搜尋與覆蓋層評估，STRATEGY.md 的數字多半出自這裡 |
| `sandbox_*` | 13 | `docs/history/SANDBOX_PLAN.md` 的假設驗證（H2、red team、TSE 採用等） |
| `debug_*` | 24 | 針對單一問題的最小重現，用完即止 |

### 執行方式

這些腳本原本放在專案根目錄，直接 `python eval_xxx.py` 就能 import `scanner.*`。
搬進子目錄後不能再這樣執行 —— Python 會把**腳本所在目錄**放進 `sys.path`，而不是目前的工作目錄，
所以 `from scanner.scan_mode import ...` 會 `ModuleNotFoundError`。

請從**專案根目錄**執行，並把根目錄加進 `PYTHONPATH`：

```bash
PYTHONPATH=. python archive/research/eval_winrate_final.py
```

Windows PowerShell：

```powershell
$env:PYTHONPATH="."; python archive\research\eval_winrate_final.py
```

這樣 `sys.path` 同時有專案根目錄（解析 `scanner.*`）與腳本目錄（解析彼此的
`from eval_prelaunch_overlays import regime_map` 這類同層匯入），兩種 import 都成立。

> 這裡沒有放 `__init__.py`：它們是要被「執行」的腳本，不是要被「匯入」的套件。
> 加了 `__init__.py` 反而會讓 `python -m archive.research.xxx` 這種跑法把同層匯入弄壞。

## legacy/ — 已被取代的舊流程

| 檔案 | 為什麼封存 |
|---|---|
| `main.py` | 舊的 watchlist / FinMind / 分點流程，**不等同**目前的主要掃描。報告 §2.2 明確要求標成 legacy，避免被誤當成正式操作入口。正式入口是 `scan_headless.py`（排程）與 `main_gui.py`（桌面）。 |
| `migrate_to_sqlite.py` | Excel → SQLite 的一次性遷移，早已執行完畢。 |
| `price_volume.xlsx` | 遷移前的舊價量表，只作對帳用。已列入 `.gitignore`，不進版控。 |
| `main_gui.spec` | 與 `TaiwanScanner.spec` 重複且內容不一致（報告 F26）。保留單一入口 `TaiwanScanner.spec`。 |
