# 關卡 2 事先登記：EURUSD 5 分鐘／15 分鐘示範規則（真實報價回測）

定稿日期：2026-10-30。在**任何**策略回報計算之前寫定，並在跑回測之前單獨提交（git 記錄可以核對先後）。之後只可加附錄，不可修改本文規則；要改就另立新文件、註明日期與理由，並視為新研究。依據：`docs/GUIDE.md` §5.1 關卡 2。

## 0. 寫定前已經看過甚麼（研究者自由度的自我申報）
- fx-board 的 5 年 1h 研究：7 個公開規則全部沒有優勢（`docs/GUIDE.md` §0）。
- 本 repo 在**隨機漫步**上跑過示範規則（1 分鐘）：零成本 ≈ 0、淨 ≈ −成本。那不是真實資料。
- 測試下載器時下載了 **2026-10-21 一天**（在本研究期間內），只看了點差分佈（各小時中位數 0.3–0.4 pip，21:00 UTC 換日時中位數 1.7 pip）。**沒有在任何真實資料上跑過策略。**
- 規則、參數全部是 repo 既有的預設值（`qtrade/strategies.py`、`config.example.toml`）。為本研究新定的只有：時框（5／15 分鐘，來自 GUIDE §0 的建議）、時間停損 12 根、研究期間、執行模型（§3），理由都寫在下面。

## 1. 假設與檢驗數
- **H1**：示範規則在 EURUSD **5 分鐘**扣成本後期望值 > 0。
- **H2**：同一規則在 EURUSD **15 分鐘**扣成本後期望值 > 0。
- **檢驗數 m = 2**，整體誤判率 1%：Šidák 門檻 z̄ = `family_z(2)` ≈ 2.576。
- 事先預期（寫下來，以便之後對照）：兩者都**沒有**優勢；淨平均 ≈ −平均成本，5 分鐘約 −0.25 至 −0.35R，15 分鐘約 −0.15 至 −0.20R；零成本平均接近 0。

## 2. 規則（固定，不優化）
- `qtrade/strategies.py` 的 `EmaCrossAtr`：收盤 K 線（mid）EMA 9／21 交叉；停損 = 1.5 × ATR(14)；目標 = 1.5 × 停損；訊號在新 K 線第一個 tick 決定。
- 停損、目標由 Router 從 bid（買）／ask（賣）量起；USD 1 風險含點差與手續費（`qtrade/router.py`）。
- **時間停損 12 根 K 線**（5 分鐘 = 60 分鐘、15 分鐘 = 180 分鐘）。理由：停損 1.5 ATR、目標 2.25 ATR 時，隨機漫步下預期觸及時間約 5 根；12 根只截斷尾部。不按結果調整。
- 同一時間最多 1 筆，持倉期間的新訊號被 `MAX_POSITIONS` 擋下。

## 3. 資料與執行模型
- **資料**：Dukascopy EURUSD 每日 1 分鐘 BID／ASK K 線，**2026-05-04 至 2026-10-23**（25 週，只取平日），以 `python -m qtrade.data.dukascopy --minutes --weekdays-only` 轉成每分鐘 4 個合成 tick（同 MT5 測試器「1 minute OHLC」模式）。**這不是真 tick 測試**：分鐘內的走向是假設的，分鐘內的點差尖峰會遺失。
- **基準成本**：Dukascopy 點差（下限 2 點 = 0.2 pip）+ 每手來回 USD 7 + 每次市價成交滑點 1 點（0.1 pip）。
- **時延**：0（在決策 tick 成交）。理由：合成 tick 相隔 20 秒，無法模擬 100 毫秒時延；改用滑點代表時延成本。
- **壓力測試**：點差 +2 點、滑點 2 點（成本約為基準的 1.5–2 倍）。
- **風控**：`config.example.toml` 的預設值（時段 07:00–16:30 UTC、換日封鎖、點差上限 12 點、點差／停損 ≤ 0.2、點差尖峰 2 倍、頻率限制），**但關掉日停損與回撤停機**（`--no-account-stops`）：判定的是規則本身每一筆的期望值，不能讓帳戶停機截斷樣本。另外用預設帳戶風控再跑一次，只用來報告「USD 100 帳戶會發生甚麼」，不參與判定。

## 4. 指令（照抄執行；`<M1>` = 下載目錄）
```bash
# 判定用（H1 / H2）
python -m qtrade.cli backtest --ticks <M1> --symbol EURUSD --config config.example.toml --bar-min 5  --max-hold-bars 12 \
  --latency-ms 0 --slippage-points 1 --min-spread-points 2 --no-account-stops --m 2 --compare --out stage2-5m.json
python -m qtrade.cli backtest --ticks <M1> --symbol EURUSD --config config.example.toml --bar-min 15 --max-hold-bars 12 \
  --latency-ms 0 --slippage-points 1 --min-spread-points 2 --no-account-stops --m 2 --compare --out stage2-15m.json
# 壓力測試：同上，加 --add-spread-points 2，--slippage-points 2，去掉 --compare
# 帳戶視角（不參與判定）：同判定用指令，去掉 --no-account-stops 與 --compare
# 點差校準：python scripts/spread_by_hour.py <M1>
```

## 5. 判定（每個假設一次，事先寫定）
以下四項**全部**符合，該假設才進入關卡 3（模擬盤前向）；任何一項不符＝沒有：
1. `qtrade.forward.judge`（m = 2）狀態為 `confirming`：按進場時間排序的**前 200 筆**，日分群標準誤，下界 = 平均 − z̄ × 標準誤 > 0，且平均 ≥ +0.05R。不足 200 筆 → 不能確認。
2. 零成本平均與淨平均同為正。
3. 壓力測試的淨平均 > 0。
4. 多、空兩邊的淨平均都 > 0。

不論結果，全部數字寫入本文附錄。兩個假設都不通過時（預期如此），本研究的用途是：確認管線在真實報價上運作、量度真實成本佔 R 的比例、用點差分佈校準 `SPREAD_ABS` 與時段設定；**示範規則不會用真錢交易**。

## 6. 已知限制（事先承認）
- 只有 25 週、一個品種：即使通過，也只是「這段期間」的證據，必須經關卡 3–4 前向確認。
- Dukascopy 是 ECN 報價，零售帳戶的點差可能更寬；所以點差下限與壓力測試是必要的。
- 1 分鐘 OHLC 合成 tick 對 5 分鐘、停損數 pip 的規則誤差不大，但不是零：同一分鐘內同時觸及停損與目標時，結果取決於假設的走向。
