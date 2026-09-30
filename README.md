# FXEA — 用 FABLE 5 打造全天候 AI 交易員

研究、訊號、風險管理,全天候持續運作。這個專案依照 8 張概念圖的架構實作一套
**Python 外匯交易系統**:市場掃描 → 訊號引擎 → 交易計畫 → 風險模組 → 全天候監控循環 →
最終決策備忘錄,**最後由你做決定**(核准 / 觀察清單 / 拒絕)。

```
        ┌──────────┐    ┌──────────┐
        │  研 究   │    │  訊 號   │      研究:Claude(FABLE 5)審視每一筆候選設定
        └────┬─────┘    └────┬─────┘      訊號:突破 / 拉回 / 動能 / 趨勢延續 / 反轉
             │   ┌───────┐   │            風險:部位大小 / 曝險 / 回撤 / 波動 / 最大虧損
             └───┤FABLE 5├───┘            全天候:掃描 → 訊號 → 風險 → 監控 → 警示 → …
             ┌───┤AI交易員├───┐
        ┌────┴─────┐    ┌────┴─────┐
        │  風 險   │    │ 全天候監控│
        └──────────┘    └──────────┘
```

## 架構對照

| 圖 | 主題 | 模組 | 重點 |
|---|---|---|---|
| 1 | 總覽 | `fxea/` | 研究 → 訊號 → 風險 → 全天候 |
| 2 | 市場掃描器 | `fxea/scanner.py` | 價格走勢、成交量、趨勢(H1/H4)、新聞、自選清單 → **偵測到交易機會** |
| 3 | 訊號引擎 | `fxea/signals.py` | 掃描 → 偵測 → 評分;五種型態依機率排序,標記高勝率機會 |
| 4 | 交易規劃器 | `fxea/planner.py` | 進場區、獲利目標(關鍵壓力位)、停損(結構下方)、失效條件、R:R、方向、週期、信心 |
| 5 | 風險模組 | `fxea/risk.py` | 五項檢查全部通過 → 繼續;任一未通過 → 阻擋 |
| 1 | 研究 | `fxea/research.py` | `ClaudeAnalyst`(結構化輸出)+ `RuleBasedAnalyst`(離線備援) |
| 6 | 監控循環 | `fxea/monitor.py` | 自選清單更新、觸發警示、狀態監控、主動檢視;不會停機 |
| 7 | 最終決策備忘錄 | `fxea/memo.py`、`fxea/approval.py` | ID、設定摘要、訊號強度、風險等級、交易計畫、最終狀態 → **需要人工核准** |
| — | 執行層 | `fxea/execution/`、`fxea/mt5_bridge.py`、`fxea/ig_bridge.py` | 紙上交易 / MT5 / IG REST |

## 快速開始

```bash
pip install -r requirements.txt
python -m pytest -q                       # 52 個離線測試

# 合成行情 + 紙上交易,跑 40 輪(每輪 = 1 根 H1)
python -m fxea.cli run --cycles 40 --interval 0
python -m fxea.cli pending                 # 看等待你審核的備忘錄
python -m fxea.cli decide FD-001-0101 approve   # 或 watchlist / reject
python -m fxea.cli status
python -m fxea.cli scan                    # 只掃描一次,列出快照與訊號
```

`pip install -e .` 之後可直接用 `fxea run` 等指令。

### 一份備忘錄長這樣

```
╔══════════════════════════════════════════════════════════════╗
║ 最終決策備忘錄                               ID: FD-003-0101 ║
║ EURUSD  做多  拉回                日期: 01/01/2026 09:00 UTC ║
╟──────────────────────────────────────────────────────────────╢
║ 1 設定摘要    趨勢一致:是                                    ║
║               市況:趨勢                                      ║
║               時間週期:1 小時 / 4 小時                       ║
╟──────────────────────────────────────────────────────────────╢
║ 2 訊號強度    信心分數:★★★★☆                                 ║
║               強度:強(訊號評分 78)                           ║
╟──────────────────────────────────────────────────────────────╢
║ 3 風險等級    風險評級:中等                                  ║
║               最大虧損符合計畫:是                            ║
║               部位:0.20 手,風險 100.00(1.00%)               ║
╟──────────────────────────────────────────────────────────────╢
║ 4 交易計畫    進場:1.08320   進場區:1.08295 – 1.08345        ║
║               停損:1.07850   停利:1.09240                    ║
║               風險報酬比:1:2.15   信心:高                    ║
║               失效:1.07800                                   ║
╟──────────────────────────────────────────────────────────────╢
║ 5 最終狀態    可進行決策                                     ║
╚══════════════════════════════════════════════════════════════╝
  ✔ 部位大小檢查 … ✔ 曝險上限檢查 … ✔ 回撤檢查 … ✔ 波動檢查 … ✔ 最大虧損檢查 …
  研究(claude:claude-fable-5-1):…
  ─── 需要人工審核 ───
  [核准] 執行交易     [觀察清單] 監控條件     [拒絕] 不要交易
  fxea decide FD-003-0101 approve | watchlist | reject
```

## 監控循環每一輪做什麼(圖 6)

1. **自選清單更新** — 設定的清單 + 有部位 / 有待審備忘錄的商品
2. **掃描** — 每個商品產生 `MarketSnapshot`
3. **監控** — 紙上交易檢查停損/停利;待審與觀察清單備忘錄檢查失效條件、進場區條件、逾時
4. **執行** — 已核准且未執行的備忘錄下單(價格離開進場區 > 1 ATR 或已失效則不執行並告知)
5. **訊號 → 計畫 → 風險 → 研究 → 備忘錄** — 每個有機會的商品最多一份進行中的備忘錄
6. **狀態監控** — `state/status.json`;每 N 輪發一則摘要警示
7. **主動檢視** — 獲利 ≥ 1R、部位停滯、備忘錄久候 → 提醒

警示輸出到主控台、`state/alerts.jsonl`,也可設 `alerts.webhook_url`(POST JSON)。
所有事件寫入 `state/journal.jsonl`,備忘錄在 `state/memos/*.json`。

## 即時模式:IG 帳戶

### 建議:MT5(你已經在用)

在裝有 IG MT5 終端的 Windows 機器上:

```bash
pip install MetaTrader5
```

`config/default.yaml`:

```yaml
data_source: mt5
broker: mt5
mt5:
  magic: 20260930
  deviation: 20
  symbol_map: {}     # 券商代碼不同時對應,例如 {EURUSD: EURUSD.i}
```

停損/停利以 MT5 伺服器端掛單;本地只追蹤 ticket ↔ 備忘錄與日內損益。

### 備用:IG REST API(Linux 主機、沒有 MT5 時)

```bash
export IG_API_KEY=… IG_IDENTIFIER=… IG_PASSWORD=…
```

```yaml
data_source: ig
broker: ig
ig:
  demo: true                       # 先用模擬帳戶跑通
  epics: {EURUSD: CS.D.EURUSD.MINI.IP}
instruments:
  EURUSD: {contract_size: 10000, lot_min: 0.5, lot_step: 0.5}   # 迷你合約 1 口 ≈ 1 美元/點
```

IG REST 有歷史價格配額(約每週 10,000 點),`IGFeed` 會快取並只增量抓最新幾根。

> 兩個橋接皆依官方文件撰寫、以假伺服器做單元測試,但**尚未在本開發環境(Linux)對真實
> IG 帳戶實測**。上線前務必先用模擬帳戶 + `approval.mode: human` 跑幾天。

## 研究層:Claude(FABLE 5)

```yaml
research:
  provider: claude          # 需要 ANTHROPIC_API_KEY(或 ant auth login)
  model: claude-fable-5-1   # 也可 claude-fable-5 / claude-opus-5-5
  effort: high
  fallbacks: true           # 伺服器端拒答備援
```

- 使用官方 `anthropic` SDK 的 `beta.messages.parse`,以 `AnalystOutput` 為結構化輸出 schema。
- Fable 5.x 思考永遠開啟,程式不傳 `thinking`,深度以 `output_config.effort` 控制。
- 認證失敗 / 速率限制 / 連線錯誤 / 模型拒答 → 自動退回 `RuleBasedAnalyst`,並在備忘錄的
  疑慮欄註明。**Claude 只評估,不下單**;下單永遠要過風險模組與你的核准。
- 系統提示放在快取前綴(`cache_control`),同一天多次呼叫可省輸入成本。

## 設定重點(`config/default.yaml`)

| 區塊 | 用途 |
|---|---|
| `watchlist` | 主攻 `EURUSD`;可再加 `GBPUSD`、`USDJPY`… |
| `scanner` | 成交量門檻、變動 % 門檻、關鍵價位距離、新聞視窗 |
| `signals` | `min_score`(預設 60)、每商品最多幾個訊號 |
| `planner` | 最低 R:R(2.0)、ATR 停損倍數、進場區半寬、失效距離 |
| `risk` | 每筆風險 1%、總曝險 5%、最多 4 筆、回撤 10%、日虧損 3%、ATR 百分位 5–95、新聞禁區 30 分 |
| `approval` | `human`(預設)/ `auto`(僅紙上交易);待審逾時 240 分 |
| `monitor` | 循環間隔、狀態/主動檢視頻率、阻擋冷卻期 |

風險模組的「最大虧損」以**最壞情況**計算:今日已實現虧損 + 所有未平倉部位同時停損 + 本筆。

## 資料來源

- `synthetic`:可重現的隨機漫步 + 趨勢段(示範/測試)
- `csv`:`{csv_dir}/{SYMBOL}_H1.csv`(`time,open,high,low,close,volume`)回放
- `mt5` / `ig`:即時
- 新聞:`news_file` 指向 JSON 陣列 `[{"time": "...", "currency": "USD", "impact": "high", "title": "NFP"}]`

## 專案結構

```
fxea/
  models.py       所有資料模型(對應每張圖的節點)
  scanner.py      圖 2   signals.py   圖 3   planner.py  圖 4   risk.py  圖 5
  research.py     圖 1   monitor.py   圖 6   memo.py / approval.py  圖 7
  indicators.py   EMA / ATR / RSI / 擺盪高低點 / K 線型態
  instruments.py  點值、手數、合約規格
  data/           synthetic / csv / 新聞
  execution/      Broker 介面、PaperBroker
  mt5_bridge.py   MT5 行情 + 券商      ig_bridge.py  IG REST 行情 + 券商
  alerts.py  state.py  app.py  cli.py
tests/            52 個離線測試(含假 IG 伺服器、假 Claude 客戶端)
config/default.yaml
```

## 免責

這是交易輔助系統,不是投資建議。任何真實資金操作前,請先用模擬帳戶驗證,並保持
`approval.mode: human`。
