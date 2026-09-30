# FXEA — 用 FABLE 5 打造全天候 AI 交易員

研究、訊號、風險管理,全天候持續運作。這個專案依照 7 張概念圖的架構實作一套
**Python 外匯交易系統**:市場掃描 → 訊號引擎 → 交易計畫 → 風險模組 → 全天候監控循環 →
最終決策備忘錄,**最後由你做決定**(核准 / 觀察清單 / 拒絕)。

> 這是一套**交易流程與風控框架**,不是一個已驗證有優勢的策略。內建的訊號規則是教科書式的
> 通用啟發法,預設參數沒有針對任何商品最佳化;請先用 `fxea backtest` 與模擬帳戶驗證,
> 再決定要不要、以及怎麼用它。

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
python -m pytest -q                       # 76 個離線測試

# 合成行情 + 紙上交易,跑 40 輪(每輪 = 1 根 H1)
python -m fxea.cli run --cycles 40 --interval 0
python -m fxea.cli pending                 # 看等待你審核的備忘錄
python -m fxea.cli decide FD-001-0101 approve   # 或 watchlist / reject
python -m fxea.cli status                  # 帳戶、部位、待審、風險模式
python -m fxea.cli scan                    # 只掃描一次,列出快照與訊號
python -m fxea.cli resume                  # 回撤斷路器暫停後,人工恢復交易
python -m fxea.cli backtest --csv-dir data --from 2026-01-01   # 用歷史 CSV 回測
python -m fxea.cli backtest --csv-dir data --from 2026-01-01 --by-type          # 五種訊號各自回測
python -m fxea.cli backtest --csv-dir data --from 2026-01-01 --research claude  # AI 交易員層回測(花 API 費用)
python -m fxea.cli --set scanner.trading_hours_utc=[7,17] backtest --csv-dir data --from 2026-01-01  # 任一設定都能臨時覆寫
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
3. **監控** — 紙上交易用最新 K 棒高低點檢查停損/停利;回撤斷路器更新狀態;
   待審與觀察清單備忘錄檢查失效條件、進場區條件、逾時
4. **執行** — 已核准且未執行的備忘錄下單(價格離開進場區 > 1 ATR、已失效、或交易暫停中則不執行並告知)
5. **訊號 → 計畫 → 風險 → 研究 → 備忘錄** — 每個有機會的商品最多一份進行中的備忘錄;暫停中不產生
6. **狀態監控** — `state/status.json`;每 N 輪發一則摘要警示
7. **主動檢視** — 獲利 ≥ 1R、部位停滯、備忘錄久候 → 提醒

警示輸出到主控台、`state/alerts.jsonl`,也可設 `alerts.webhook_url`(POST JSON)。
所有事件寫入 `state/journal.jsonl`,備忘錄在 `state/memos/*.json`。

## 回撤斷路器

權益從高水位回落 ≥ `risk.max_drawdown_pct` → 進入**暫停**:不開新倉、已核准未執行的備忘錄失效、
發一則 CRITICAL 警示,之後每 24 小時提醒一次;`fxea status` 顯示 `halted`。循環本身不停,仍然監控既有部位。

恢復方式:

- `drawdown_cooldown_hours: 0`(預設,真實資金建議):等你執行 `fxea resume`。預設進入**恢復期**——
  回撤從目前權益重新起算、每筆風險 × `recovery_risk_scale`(0.5),權益回到暫停前高點才回復全額;
  `fxea resume --full-risk` 直接回復全額。
- `drawdown_cooldown_hours: 72`(紙上交易 / 回測建議):冷卻期滿自動進入恢復期。

為什麼需要它:沒有這個狀態機,回撤檢查失敗後沒有部位 → 權益不變 → 永遠通不過檢查,
系統會**安靜地停止交易**且不會自行恢復——這正是第一版回測暴露的問題。

## 回測(`fxea backtest`)

```bash
# 1. 在裝有 MT5 的 Windows 匯出 H1 歷史(time,open,high,low,close,volume)
python scripts/export_mt5_csv.py EURUSD 2025-11-15 data/
# 2. 回測:--from 之前的資料只當指標暖機,正式從 2026-01-01 起算
python -m fxea.cli backtest --csv-dir data --symbol EURUSD --from 2026-01-01 --cooldown-hours 72
```

輸出 `backtests/<時間戳>/trades.csv`、`equity.csv` 與統計:勝率(± 標準誤)、損益兩平勝率、
平均獲利/虧損、賠率、每筆期望值、獲利因子、最大回撤(高點/低點日期)、斷路器觸發次數、
最長無交易間隔、以及一行「優勢」判讀。

回測衡量的是**機械規則本身**(掃描 → 訊號 → 計畫 → 風險 → 斷路器),以規則式研究 + 自動核准執行,
不含 Claude 研究層與人工篩選。保真度:進出場計點差、停損/停利用 K 棒高低點(同棒觸及兩者先算停損)、
訊號只在收盤後產生;沒有滑價與隔夜利息。

怎麼讀結果:

- **勝率要和「損益兩平勝率」比**,不是和 50% 比。賠率 2:1 時損益兩平約 33%。
- **差距小於 2 個標準誤就當作沒有優勢**。64 筆交易的勝率標準誤約 ±6 個百分點;要有底氣說「有優勢」,
  通常需要幾百筆交易加上樣本外驗證。
- 調整參數後,一定用**沒有參與調參的期間**再跑一次。

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

## 超越機械規則:AI 交易員層(Claude / FABLE 5)

機械規則負責「找候選」;AI 負責「像交易員一樣判斷」。三個原則:**AI 看得到完整的盤、AI 的判斷有牙齒、
AI 的價值可以被回測驗證**。

**AI 看得到什麼**(每個通過風險檢查的候選設定):最近 60 根 H1 + 40 根 H4 K 棒(CSV)、兩個週期的
EMA/RSI/ATR、關鍵價位、新聞、訊號引擎的訊號與理由、機械交易計畫、風險模組五項檢查、未平倉部位、
以及**同商品近期由本系統執行的交易與結果**(它自己的戰績,含 R 倍數)。

**AI 能決定什麼**(全部有界限,程式驗證後才採用):

| 決定 | 機制 | 界限 |
|---|---|---|
| `execute` / `watch` / `skip` | `approval.honor_research: true` 時,auto 模式採納:執行 → 核准;觀察 → 觀察清單,價格回到進場區才核准,逾時失效;略過 → 拒絕。human 模式則顯示在備忘錄供你參考 | — |
| 信念部位 `risk_fraction` | 每筆風險 × 0.25–1.0,風險模組重算手數 | 夾在 0.25–1.0 |
| 調整進場 / 停損 / 目標 | `apply_research_adjustments`:採用後失效價位、進場區、R:R 全部重算,風險模組重新驗算 | 進場位移 ≤ 1 ATR;停損距進場 0.5–3 ATR;調整後 R:R ≥ 最低要求;幾何不成立整組退回 |
| `key_observations` | 寫進備忘錄:它從 K 棒看到、規則沒抓到的事 | — |

```yaml
research:
  provider: claude          # 需要 ANTHROPIC_API_KEY(或 ant auth login)
  model: claude-fable-5-1   # 也可 claude-fable-5 / claude-opus-5-5 / claude-sonnet-5-5
  effort: high
  fallbacks: true           # 伺服器端拒答備援
  candles_h1: 60
  candles_h4: 40
  recent_trades: 10
  allow_plan_adjustment: true
  allow_conviction_sizing: true
  only_when_ready: true     # 被風險阻擋的設定不花 AI 呼叫
approval:
  honor_research: true      # auto 模式採納 AI 建議(human 模式仍由你決定)
```

**怎麼驗證 AI 有沒有價值**:同一份歷史資料跑兩次——`fxea backtest ...`(機械規則)與
`fxea backtest ... --research claude`(AI 層),比較樣本外的期望值、獲利因子與最大回撤。回測結束會印
Claude 呼叫次數、token 用量與估計費用(每個候選設定約 4K 輸入 token,Fable 5.1 約 0.05–0.07 美元;
Opus 5.5 約 0.03 美元)。`--research claude` 在 API 失敗時會**中止回測**而不是悄悄退回規則式。

**即時模式的成本控制**:`monitor.min_minutes_between_memos`(預設 60)限制同商品兩份備忘錄的最短間隔,
所以 AI 呼叫頻率約等於「每根 H1 最多一次 × 商品數」,不會跟著 60 秒的監控循環走。

實作細節:官方 `anthropic` SDK 的 `beta.messages.parse`,以 `AnalystOutput` 為結構化輸出 schema;
Fable 5.x 思考永遠開啟,程式不傳 `thinking`,深度以 `output_config.effort` 控制;系統提示放在快取前綴。
即時模式下認證失敗 / 速率限制 / 連線錯誤 / 模型拒答 → 退回 `RuleBasedAnalyst` 並在備忘錄疑慮欄註明。
**Claude 只評估,不下單**;下單永遠要過風險模組與核准閘門。

### 老實說 AI 層能不能贏

不知道,所以才做成可以回測的。LLM 擅長綜合脈絡(結構、新聞、自己的戰績)與紀律,不是預言機;
如果 `--research claude` 在樣本外沒有贏過機械規則,那就是它在這個商品、這個週期沒有加值,
不要因為它是 AI 就相信它。下一步的候選:給 AI 看圖(用 matplotlib 把 K 棒畫成圖,連同 CSV 一起餵)、
接即時財經日曆取代靜態新聞檔、即時模式加網路搜尋工具(不可回測,只能紙上驗證)。

## 實驗開關

找優勢的三個便宜實驗(都不是曲線擬合),用 `--set` 臨時覆寫或寫進 YAML:

```bash
# 1. 五種訊號型態各自回測,看損益到底來自哪一種
python -m fxea.cli backtest --csv-dir data --from 2026-01-01 --by-type
# 2. 只做與 H4 趨勢同向的設定(both = H1 與 H4 都同向)
python -m fxea.cli --set signals.require_trend_alignment=h4 backtest --csv-dir data --from 2026-01-01
# 3. EURUSD 只做倫敦/紐約時段(UTC 小時,含起不含迄)
python -m fxea.cli --set scanner.trading_hours_utc=[7,17] backtest --csv-dir data --from 2026-01-01
# 也可以只留特定型態
python -m fxea.cli --set signals.enabled_types=[pullback,trend_continuation] backtest --csv-dir data --from 2026-01-01
```

判斷標準不變:樣本外、幾百筆、z > 2;每個實驗的樣本數都比全部更少,差距更容易只是雜訊。

## 設定重點(`config/default.yaml`)

| 區塊 | 用途 |
|---|---|
| `watchlist` | 主攻 `EURUSD`;可再加 `GBPUSD`、`USDJPY`… |
| `scanner` | 成交量門檻、變動 % 門檻、關鍵價位距離、新聞視窗、交易時段 `trading_hours_utc` |
| `signals` | `min_score`(預設 60)、每商品最多幾個訊號、`enabled_types`、`require_trend_alignment` |
| `planner` | 最低 R:R(2.0)、ATR 停損倍數、進場區半寬、失效距離、AI 調整界限 |
| `research` | 分析器(rules / claude)、模型、effort、AI 看幾根 K 棒、允許哪些 AI 決定 |
| `risk` | 每筆風險 1%、總曝險 5%、最多 4 筆、回撤 10%、日虧損 3%、ATR 百分位 5–95、新聞禁區 30 分、斷路器冷卻/恢復期風險 |
| `approval` | `human`(預設)/ `auto`(僅紙上交易);待審逾時 240 分;`honor_research` |
| `monitor` | 循環間隔、狀態/主動檢視頻率、阻擋冷卻期、同商品備忘錄最短間隔 |
| `account` | 紙上交易起始資金、點差(`spread_pips`,null = 商品預設 1 點) |

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
  risk.py         也含 RiskGovernor(回撤斷路器狀態機)
  backtest.py     CSV 回放回測與統計
  indicators.py   EMA / ATR / RSI / 擺盪高低點 / K 線型態
  instruments.py  點值、手數、合約規格、點差
  data/           synthetic / csv / 新聞
  execution/      Broker 介面、PaperBroker(點差、盤中觸價)
  mt5_bridge.py   MT5 行情 + 券商      ig_bridge.py  IG REST 行情 + 券商
  alerts.py  state.py  app.py  cli.py
scripts/export_mt5_csv.py   從 MT5 匯出 H1 歷史 CSV
tests/            76 個離線測試(含假 IG 伺服器、假 Claude 客戶端)
config/default.yaml
```

## 免責

這是交易輔助系統,不是投資建議。任何真實資金操作前,請先用模擬帳戶驗證,並保持
`approval.mode: human`。
