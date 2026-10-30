# q-trade

> 微型本金（約 USD 100、每筆風險 USD 1）的輕量化自動 scalping 架構：**風控 + 訂單路由 + tick 級回測 + 驗證流程**。
> 支援 MT5（MQL5 EA / Python `MetaTrader5`）與 TradingView（Pine 警報 + FastAPI Webhook）。

**先讀 [`docs/GUIDE.md`](docs/GUIDE.md) §0。** 一句話：fx-board 的 5 年研究顯示公開規則扣成本前沒有優勢，扣成本後期望值 ≈ 負的成本；
而 1 分鐘 scalping 的成本是停損的 0.5–1.4 倍（1 小時只有 0.07–0.16 倍）。這個 repo 先保證你**活下來、量得到真實成本**，
再用事先寫定的規則判定有沒有優勢。示範策略（EMA 交叉 + ATR）只是測試管線用的負載，**不是**優勢。

## 內容

| 路徑 | 作用 |
|---|---|
| `docs/GUIDE.md` | 完整指南：標的與經紀商選型、MT5 vs TradingView、架構與部署、風控、程式碼、驗證流程、常見陷阱 |
| `qtrade/risk.py` | RiskGuard：USD 1 固定風險、日停損（最壞情況）、回撤停機、頻率限制、連虧冷卻、點差三重過濾、時段／換日／新聞、資料新鮮度、KILL 檔 |
| `qtrade/sizing.py`, `qtrade/costs.py` | 按金額風險算手數（**絕不向上捨入到最小手數**）；成本佔 R、打和勝率 |
| `qtrade/router.py` | 去重 → 券商報價重錨定停損 → 倉位 → 風控 → 下單（SL/TP 掛伺服器）→ journal |
| `qtrade/broker/mt5.py`, `paper.py` | MT5 實盤介面；模擬券商（回測與試跑） |
| `qtrade/webhook.py` | TradingView webhook：驗證、冪等、3 秒內回應、單一 worker、`/health` |
| `qtrade/backtest.py` | tick 級回測，跑的是**實盤同一套** Router + RiskGuard；`--compare` 同時跑零成本對照 |
| `qtrade/forward.py` | 前向判定（fx-board 規則：只判定一次、Šidák、日分群標準誤）與執行品質報告 |
| `qtrade/data/` | tick 檔讀取、Dukascopy tick 下載、隨機漫步對照資料 |
| `mql5/Experts/QScalp.mq5` | MQL5 EA 骨架：同一套風控與示範規則（未經 MetaEditor 編譯驗證） |
| `pine/qscalp_alert.pine` | Pine v6 警報，送出 webhook JSON |
| `scripts/cost_by_timeframe.py` | 從真實 K 線算各時框的成本佔停損比例 |

## 快速開始

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,server]"          # Windows 實盤再加 [mt5]
python -m pytest -q                      # 50 項測試

python -m qtrade.cli costs --cost 0.00009 --stops 0.0002,0.0003,0.0005,0.001 --unit 0.0001
python -m qtrade.cli backtest --ticks synthetic:10 --symbol EURUSD --compare --any-session
cp config.example.toml config.toml && export QTRADE_SECRET='至少16個字元'
python -m qtrade.cli serve --config config.toml       # paper 模式，任何作業系統都可試跑
python -m qtrade.cli forward --journal journal.sqlite
```

## 免責

研究與工程用途，**不是投資建議**。槓桿產品可以令你損失全部本金；先用模擬帳戶，再用你可以全損的小額真錢做前向驗證。

## License

MIT
