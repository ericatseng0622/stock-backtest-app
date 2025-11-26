import streamlit as st
import backtrader as bt
import backtrader.indicators as btind
import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
from datetime import date # 確保只 import 了 date

# ==============================================================================
# 策略核心與回測邏輯 (MyCombinedStrategy)
# ==============================================================================

class MyCombinedStrategy(bt.Strategy):
    params = (
        ('volume_limit_A', 10000), 
        ('volume_limit_B', 1000), 
        ('k_bar_pct', 0.035), 
        ('consolidation_pct', 0.05),
        ('logic', 'OR'), # 傳入組合邏輯
        ('position_size', 100), # 增加部位大小參數，方便調整
    )

    def __init__(self):
        # 數據追蹤 (Backtrader 預期 OHLCV 都是小寫)
        self.dataclose = self.datas[0].close
        self.dataopen = self.datas[0].open
        self.datavolume = self.datas[0].volume
        self.order = None
        self.position_size = self.p.position_size

        # 指標計算
        self.ma5 = btind.SimpleMovingAverage(self.datas[0], period=5)
        self.ma10 = btind.SimpleMovingAverage(self.datas[0], period=10)
        self.ma20 = btind.SimpleMovingAverage(self.datas[0], period=20)
        self.ma60 = btind.SimpleMovingAverage(self.datas[0], period=60)
        self.macd = btind.MACD(self.datas[0])
        self.macd_cross = btind.CrossOver(self.macd.macd, self.macd.signal) 
        
        # 確保 log 函式在 backtrader 環境下
        self.log_messages = []

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        self.log_messages.append(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f'買入執行, 價格: {order.executed.price:.2f}, 成本: {order.executed.comm:.2f}')
            elif order.issell():
                self.log(f'賣出執行, 價格: {order.executed.price:.2f}, 成本: {order.executed.comm:.2f}')
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('訂單失敗/取消/拒絕')

    # ------------------------------------------------------------------------------------------
    # 策略一: 強勢整理
    # ------------------------------------------------------------------------------------------
    def check_strategy_1(self):
        """檢查策略一 (強勢整理) 的所有買入條件"""
        if len(self.datas[0]) < 60: return False # 確保有足夠的數據
        
        # 1. 多頭排列 (MA60 < MA20 < Close)
        cond_1_排列 = (self.ma60[0] < self.ma20[0]) and (self.ma20[0] < self.dataclose[0])
        
        # 2. MA 糾結 (MA5, MA20 接近)
        max_ma = max(self.ma5[0], self.ma20[0])
        min_ma = min(self.ma5[0], self.ma20[0])
        # 容忍度計算 (max_ma / min_ma 必須小於 1 + 參數)
        cond_2_糾結 = (max_ma / min_ma) < (1 + self.p.consolidation_pct) 
        
        # 3. 量能放大 (萬張以上)
        cond_3_量能 = (self.datavolume[0] > self.p.volume_limit_A * 1000)
        
        return cond_1_排列 and cond_2_糾結 and cond_3_量能

    # ------------------------------------------------------------------------------------------
    # 策略二: 長紅起漲
    # ------------------------------------------------------------------------------------------
    def check_strategy_2(self):
        """檢查策略二 (長紅起漲) 的所有買入條件"""
        if len(self.datas[0]) < 20: return False # 確保有足夠的數據
        
        # 1. 多頭排列 (MA20 < MA10 < Close)
        cond_1_排列 = (self.ma20[0] < self.ma10[0]) and (self.ma10[0] < self.dataclose[0])
        
        # 2. 量能放大 (千張以上)
        cond_2_量能 = (self.datavolume[0] > self.p.volume_limit_B * 1000)
        
        # 3. 長紅 K 棒 (漲幅超過 k_bar_pct)
        cond_3_長紅 = (self.dataclose[0] > self.dataopen[0]) and \
                      ((self.dataclose[0] - self.dataopen[0]) / self.dataopen[0] > self.p.k_bar_pct)
        
        # 4. MACD 向上交叉 (金叉)
        cond_4_MACD = (self.macd_cross[0] > 0)
        
        return cond_1_排列 and cond_2_量能 and cond_3_長紅 and cond_4_MACD
    
    # ------------------------------------------------------------------------------------------

    def next(self):
        if self.order: return
            
        signal_s1 = self.check_strategy_1()
        signal_s2 = self.check_strategy_2()

        # 根據 UI 傳入的參數決定組合邏輯
        if self.p.logic == 'AND':
            final_buy_signal = signal_s1 and signal_s2 
            logic_str = 'S1 AND S2'
        else: # 預設為 OR
            final_buy_signal = signal_s1 or signal_s2
            logic_str = 'S1 OR S2'

        # --- 買入邏輯 ---
        if not self.position:
            if final_buy_signal:
                self.log(f'買入訊號出現 ({logic_str})！')
                self.order = self.buy(size=self.position_size)

        # --- 賣出/平倉邏輯 (出場心法：跌破 MA20) ---
        else:
            if self.dataclose[0] < self.ma20[0]:
                self.log('平倉訊號出現 (跌破 MA20)')
                self.order = self.sell(size=self.position_size)

# ==============================================================================
# 資料獲取與回測執行函數
# ==============================================================================

@st.cache_data(ttl=3600)
def get_data(ticker, start, end):
    """從 Yahoo Finance 獲取歷史股價資料並緩存，並徹底處理欄位名稱"""
    try:
        data = yf.download(ticker, start=start, end=end, auto_adjust=True)
        
        # 1. 檢查是否下載成功
        if data.empty:
            st.error(f"錯誤：無法下載 {ticker} 的數據，請檢查股票代號或日期範圍。")
            return pd.DataFrame()

        # 2. 處理 MultiIndex (針對 yfinance 返回多層欄位的情況，通常發生在台灣股或多支股票)
        if isinstance(data.columns, pd.MultiIndex):
            # 修正: 取得 Level 0 (即 OHLCV 標籤)，而非 Level 1 (股票代號)
            data.columns = data.columns.get_level_values(0)
        
        # 3. 欄位名稱轉換為小寫並強制轉為字串
        data.columns = [str(col).lower() for col in data.columns]
        
        # 4. 統一收盤價名稱 (將 'adj close' 視為 'close')
        if 'adj close' in data.columns:
            data = data.rename(columns={'adj close': 'close'})
            
        # 確保 'volume' 欄位存在 (yfinance有時會返回 'Volume' 或 'volume')
        if 'Volume' in data.columns:
             data = data.rename(columns={'Volume': 'volume'})

        # 5. 檢查所需的欄位是否存在
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in data.columns]
        
        if missing_cols:
            st.error(f"❌ 數據缺少 Backtrader 所需的欄位: {', '.join(missing_cols)}")
            st.info(f"✅ 現有欄位: {', '.join(data.columns)}")
            return pd.DataFrame()
        
        # 6. 只保留 Backtrader 所需的欄位
        data = data[required_cols]

        return data
        
    except Exception as e:
        st.error(f"資料獲取失敗: {e}")
        return pd.DataFrame()

def run_backtest(data, logic, initial_cash=100000.0):
    """執行回測模擬並返回結果"""
    cerebro = bt.Cerebro()
    
    # Backtrader 期望欄位名稱為小寫，這已在 get_data 中處理
    data_feed = bt.feeds.PandasData(dataname=data) 
    cerebro.adddata(data_feed)
    
    # 傳遞組合邏輯參數
    cerebro.addstrategy(MyCombinedStrategy, logic=logic) 
    
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.001)

    # 設置分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')

    # 執行並繪製圖表
    results = cerebro.run()
    
    # 繪製淨值曲線 (使用 Matplotlib 轉 Streamlit)
    # 注意：這裡只取結果集中的第一個，因為我們只跑了一次策略
    fig = cerebro.plot(style='candlestick', volume=False, iplot=False)[0][0]
    
    # 提取績效指標
    result_data = results[0]
    final_value = cerebro.broker.getvalue()
    
    # 處理夏普比率可能為 NaN 的情況
    sharpe_ratio = result_data.analyzers.sharpe.get_analysis().get('sharperatio', 'N/A')
    if sharpe_ratio is not None and sharpe_ratio != 'N/A':
        sharpe_ratio = f"{sharpe_ratio:.2f}"
    
    metrics = {
        '最終資金': f"${final_value:,.2f}",
        '總報酬率': f"{((final_value - initial_cash) / initial_cash) * 100:.2f}%",
        # 確保有值才格式化
        '年化報酬率 (CAGR)': f"{result_data.analyzers.returns.get_analysis().get('rnorm100', 0):.2f}%", 
        '最大資金回撤 (MDD)': f"{result_data.analyzers.drawdown.get_analysis().get('max', {}).get('drawdown', 0):.2f}%",
        '夏普比率 (Sharpe)': sharpe_ratio,
        '交易日誌': result_data.log_messages
    }
    
    return metrics, fig


# ==============================================================================
# Streamlit 界面 (App UI)
# ==============================================================================

st.set_page_config(layout="wide", page_title="股票心法績效回歸模擬器")

st.title("📈 股票心法績效回歸模擬器")
st.caption("基於您的多頭排列/長紅起漲心法 (策略一 & 策略二) 的 Backtrader 模擬 Web App。")

# 側邊欄參數設定
st.sidebar.header("📜 回測參數設定")

ticker = st.sidebar.text_input("股票代碼 (e.g., AAPL, 2330.TW)", value='AAPL').upper()
# 修正：使用已引入的 date 類別
start_date = st.sidebar.date_input("起始日期", value=date(2018, 1, 1)) 
end_date = st.sidebar.date_input("結束日期", value=date(2023, 1, 1))
initial_cash = st.sidebar.number_input("起始資金 ($)", value=100000.0, step=10000.0)

# 額外增加策略參數控制
st.sidebar.subheader("策略參數微調")
volume_limit_A = st.sidebar.slider("策略一: 量能門檻 (萬張)", 5, 20, 10, key='volA')
k_bar_pct = st.sidebar.slider("策略二: 長紅K門檻 (%)", 0.01, 0.05, 0.035, step=0.005, format='%.3f', key='kpct')

st.sidebar.header("🧠 策略組合邏輯")
logic_mode = st.sidebar.radio(
    "如何組合策略一和策略二？",
    ('OR', 'AND'),
    help="OR: 任一策略條件滿足即買入。AND: 兩個策略條件都滿足才買入。"
)

# 處理點擊事件
if st.sidebar.button("開始回測"):
    if start_date >= end_date:
        st.error("起始日期必須早於結束日期！")
    else:
        with st.spinner(f"正在獲取 {ticker} 數據並執行回測..."):
            # 傳遞給 get_data 的日期需要是字串
            data = get_data(ticker, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
            
            if not data.empty:
                # 執行回測
                metrics, fig = run_backtest(data, logic_mode, initial_cash)
                
                # 主界面顯示結果
                st.header(f"回測結果：{ticker} ({start_date.year} - {end_date.year})")
                
                # 顯示績效指標
                cols = st.columns(4)
                cols[0].metric("總報酬率", metrics['總報酬率'])
                cols[1].metric("年化報酬率 (CAGR)", metrics['年化報酬率 (CAGR)'])
                cols[2].metric("最大回撤 (MDD)", metrics['最大資金回撤 (MDD)'])
                cols[3].metric("最終資金", metrics['最終資金'])
                
                st.metric("夏普比率 (Sharpe Ratio)", metrics['夏普比率 (Sharpe)'])

                # 顯示淨值曲線圖
                st.subheader("資金淨值曲線圖")
                st.pyplot(fig) # 在 Streamlit 中顯示 Matplotlib 圖表

                # 顯示交易日誌
                st.subheader("詳細交易日誌")
                if metrics['交易日誌']:
                    log_df = pd.DataFrame({'Log': metrics['交易日誌']})
                    st.dataframe(log_df, use_container_width=True)
                else:
                    st.info("該策略在回測期間沒有任何交易。")
            else:
                st.error("回測失敗：無法獲取資料或數據為空。請檢查股票代號或日期範圍。")