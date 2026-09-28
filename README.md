# BTC 長期投資策略研究模組

本版本在原本的 BTC AI 專案中新增研究：

**不同長期 Bitcoin 投資策略之風險與績效比較**

## 比較的三種策略

1. **Buy & Hold**：研究期第一天一次投入全部初始資金，持有至期末。
2. **DCA**：把同一筆初始資金分成固定月數（預設 12 個月），從研究起點開始每隔一個曆月投入一次；尚未投入的資金視為現金，報酬率設為 0%。
3. **MA20/MA60**：MA20 > MA60 時持有 BTC，否則持有現金；訊號使用當日收盤資料並於收盤後調整，避免使用未來價格資料。

三種策略採相同的初始資金、相同研究期間，預設每次 BTC 交易成本為 **0.1%**，期末不強制賣出 BTC。

## 比較指標

- 累積報酬率
- CAGR（年化報酬率）
- 年化波動率
- Sharpe Ratio（Rf = 0，365 日年化）
- Maximum Drawdown（最大回撤）
- 期末資產價值（初始資金 = 1）

另外會顯示：

- DCA 實際投入月數與有效平均買入成本
- MA20/60 持倉切換訊號數
- MA20/60 BTC 市場曝險比例

## LINE / Agent 可以這樣問

- `策略回測`
- `策略歷史回測`
- `長期投資策略比較 5 年`
- `不同長期 Bitcoin 投資策略之風險與績效比較`
- `DCA vs Buy & Hold 10年`
- `比較 DCA 與均線策略`
- `長期策略比較 5 年，DCA 18 個月`
- `2020/1/1 到 2025/12/31 長期策略比較`

如果只想看原本的單一均線回測，仍可輸入：

- `MA20/60 回測`

## 主要新增函式

```python
get_btc_long_term_strategy_comparison(
    transaction_cost=0.001,
    years=5,
    dca_months=12,
    start_date=None,
    end_date=None,
)
```

回傳的 `strategies` 內包含：

```python
result["strategies"]["buy_hold"]
result["strategies"]["dca"]
result["strategies"]["ma20_60"]
```

## 資料來源

- Binance BTC/USDT 日 K：若完整研究區間（含 MA60 暖機資料）可由 Binance 涵蓋。
- CoinGecko BTC/USD：研究起點早於 Binance 可完整涵蓋的歷史時，整段改用 CoinGecko，不混接兩種價格來源。

## 研究方法參考

- FINRA — *The Pros and Cons of Dollar-Cost Averaging*  
  https://www.finra.org/investors/insights/dollar-cost-averaging
- Vanguard — *Cost averaging: Invest now or temporarily hold your cash?*  
  https://corporate.vanguard.com/content/dam/corp/research/pdf/cost_averaging_invest_now_or_temporarily_hold_your_cash.pdf
- Finance Research Letters — *The profitability of technical trading rules in the Bitcoin market*  
  https://doi.org/10.1016/j.frl.2019.08.011
- Fidelity Digital Assets — *A Closer Look at Bitcoin's Volatility*  
  https://www.fidelitydigitalassets.com/research-and-insights/closer-look-bitcoins-volatility

> 注意：Vanguard 的 cost averaging 研究主要使用傳統資產，不應把其歷史結果直接視為 Bitcoin 的結論。本專題的 Bitcoin 結果由程式依指定期間自行回測。

## 檔案

- `btc_agent_final.py`：新增研究核心、Agent routing、格式化與 AI 客觀解讀。
- `app_project_final.py`：更新功能選單文字與示範問題。
- `requirements.txt`：不需新增套件，沿用原本依賴。

## 驗證

已完成：

- Python 語法編譯檢查（`py_compile`）
- 使用合成 BTC 日資料離線測試三種策略計算
- 測試 DCA 月數解析、策略比較問句 routing

因目前執行環境無法直接連線到 Binance / CoinGecko API，因此沒有在此環境產生真實市場數值；在你的本機或 Render 部署環境連網後，會使用實際歷史資料計算。
