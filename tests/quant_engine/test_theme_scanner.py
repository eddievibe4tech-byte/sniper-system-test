import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.quant_engine.theme_scanner import calculate_natr, calculate_rsi, calculate_sma, scan_left_side, scan_right_side, ScanSignal
from src.quant_engine.theme_models import ThemeStock, Theme, ThemeConfig

# 輔助函數：生成模擬 K 線數據
def generate_mock_klines(num_days: int, start_date: str = "2023-01-01", initial_price: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range(start=start_date, periods=num_days, freq='D')
    
    data = {
        'Open': np.full(num_days, initial_price),
        'High': np.full(num_days, initial_price * 1.01),
        'Low': np.full(num_days, initial_price * 0.99),
        'Close': np.full(num_days, initial_price),
        'Volume': np.full(num_days, 100000)
    }
    df = pd.DataFrame(data, index=dates)
    # 增加一些波動讓指標有變化
    np.random.seed(42) # 為了可重現性
    df['Close'] = initial_price * (1 + np.random.randn(num_days) * 0.01)
    df['Open'] = df['Close'].shift(1).fillna(initial_price)
    df['High'] = df[['Open', 'Close']].max(axis=1) * (1 + np.random.rand(num_days) * 0.005)
    df['Low'] = df[['Open', 'Close']].min(axis=1) * (1 - np.random.rand(num_days) * 0.005)
    df['Volume'] = (100000 + np.random.randint(-50000, 50000, num_days)).astype(int) # 確保 Volume 是整數
    df.iloc[0, df.columns.get_loc('Open')] = initial_price # 確保第一天的開盤價
    df = df.round(2) # 保留兩位小數
    return df

# 輔助函數：創建 ThemeConfig 實例
def create_mock_theme_config(theme_id: str = "test_theme", seasonal_months: List[int] = None, symbol: str = "TEST.TW") -> ThemeConfig:
    if seasonal_months is None:
        seasonal_months = [datetime.now().month]
    stock = ThemeStock(symbol=symbol, role="individual", odd_lot_allowed=True)
    theme = Theme(theme_id=theme_id, seasonal_months=seasonal_months, stocks=[stock])
    return ThemeConfig(themes=[theme])

# 測試 calculate_natr
def test_calculate_natr():
    df = generate_mock_klines(num_days=30, initial_price=100.0)
    natr_series = calculate_natr(df)
    assert isinstance(natr_series, pd.Series)
    assert len(natr_series) == len(df)
    # 檢查 NATR 值是否在合理範圍內 (通常為正值)
    assert (natr_series >= 0).all() or natr_series.isnull().all()
    # 只有在數據量足夠時才檢查非零，否則前幾天可能是0
    if len(df) >= 14: # ATR period
        assert not natr_series.iloc[-1] == 0 # 除非價格不動或 ATR 為0

# 測試 calculate_rsi
def test_calculate_rsi():
    df = generate_mock_klines(num_days=30, initial_price=100.0)
    rsi_series = calculate_rsi(df)
    assert isinstance(rsi_series, pd.Series)
    assert len(rsi_series) == len(df)
    # RSI 值應在 0 到 100 之間，但前幾天可能為 NaN
    # 確保非 NaN 的值在 0-100 之間
    assert ((rsi_series.dropna() >= 0) & (rsi_series.dropna() <= 100)).all()

# 測試 calculate_sma
def test_calculate_sma():
    df = generate_mock_klines(num_days=30, initial_price=100.0)
    sma_series = calculate_sma(df, 'Close', 5)
    assert isinstance(sma_series, pd.Series)
    assert len(sma_series) == len(df)
    # 前幾天的 SMA 應該是 NaN
    assert sma_series.iloc[:4].isnull().all()
    assert sma_series.iloc[4:].notnull().all()

# 測試 scan_left_side
def test_scan_left_side_no_signal_bear_defense():
    df = generate_mock_klines(num_days=100)
    config = create_mock_theme_config()
    signals = scan_left_side(df, config, "BEAR_DEFENSE")
    assert len(signals) == 0

def test_scan_left_side_no_signal_insufficient_data():
    df = generate_mock_klines(num_days=10) # 不足 20 天歷史數據
    config = create_mock_theme_config()
    signals = scan_left_side(df, config, "NORMAL")
    assert len(signals) == 0

def test_scan_left_side_with_signal():
    # 模擬 NATR 和 RSI 符合左側條件的數據
    df = generate_mock_klines(num_days=100, initial_price=50)

    # scan_left_side 會自行重算 NATR/RSI，因此必須從 K 線本身製造條件：
    # 最後 5 根窄幅小跌 K 線 → NATR 低於 60 日 P25，且 RSI 跌破 45
    prev_close = df['Close'].iloc[-6]
    for idx in range(-5, 0):
        close = round(prev_close * 0.998, 2)
        df.iloc[idx, df.columns.get_loc('Open')] = prev_close
        df.iloc[idx, df.columns.get_loc('Close')] = close
        df.iloc[idx, df.columns.get_loc('High')] = prev_close + 0.02
        df.iloc[idx, df.columns.get_loc('Low')] = close - 0.02
        prev_close = close

    # 確保當前月份是題材的季節月份
    current_month = datetime.now().month
    config = create_mock_theme_config(seasonal_months=[current_month], symbol="2480.TW")
    signals = scan_left_side(df, config, "NORMAL")
    assert len(signals) == 1
    assert signals[0].symbol == "2480.TW"
    assert signals[0].signal_type == "left_side"
    assert "NATR" in signals[0].reason

def test_scan_left_side_no_signal_not_in_seasonal_month():
    df = generate_mock_klines(num_days=100)
    # 設置為非當前月份
    non_current_month = (datetime.now().month % 12) + 1
    config = create_mock_theme_config(seasonal_months=[non_current_month])
    signals = scan_left_side(df, config, "NORMAL")
    assert len(signals) == 0

def test_scan_left_side_no_signal_conditions_not_met():
    df = generate_mock_klines(num_days=100)
    # 為了手動設置，確保 NATR_14 和 RSI_14 已經在 df_temp 中計算
    df_temp_for_no_signal = df.copy()
    df_temp_for_no_signal['NATR_14'] = pd.Series(np.random.uniform(1.5, 3.0, size=len(df_temp_for_no_signal)), index=df_temp_for_no_signal.index)
    df_temp_for_no_signal.iloc[-1, df_temp_for_no_signal.columns.get_loc('NATR_14')] = 5.0 # 高 NATR
    df_temp_for_no_signal['RSI_14'] = pd.Series(np.random.uniform(50, 70, size=len(df_temp_for_no_signal)), index=df_temp_for_no_signal.index)
    df_temp_for_no_signal.iloc[-1, df_temp_for_no_signal.columns.get_loc('RSI_14')] = 60.0 # 高 RSI，確保是浮點數

    config = create_mock_theme_config()
    signals = scan_left_side(df_temp_for_no_signal, config, "NORMAL")
    assert len(signals) == 0

# 測試 scan_right_side
def test_scan_right_side_with_signal():
    # 模擬符合右側條件的數據
    df = generate_mock_klines(num_days=100, initial_price=100)
    
    # 直接設置 SMA 值
    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = ((df['Close'] - df['Close_SMA_5']) / df['Close_SMA_5']) * 100

    # 強制最後一天數據符合條件
    df.iloc[-1, df.columns.get_loc('Close')] = 105.00 # 突破 SMA_Close_20
    # 直接設置 Volume 為一個足夠大的整數值，確保滿足 volume_condition
    df.iloc[-1, df.columns.get_loc('Volume')] = 600000 # 直接設置為一個足夠大的數

    # 設置 Limit_Up_Price 為一個較高的值，確保不觸發漲停過濾
    df['Limit_Up_Price'] = 110.00 # 為了讓漲停價不是當前收盤價

    config = create_mock_theme_config(symbol="2480.TW")
    signals = scan_right_side(df, config)
    assert len(signals) == 1
    assert signals[0].symbol == "2480.TW"
    assert signals[0].signal_type == "right_side"
    assert "Volume" in signals[0].reason

def test_scan_right_side_no_signal_volume_low():
    df = generate_mock_klines(num_days=100, initial_price=100)
    
    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = ((df['Close'] - df['Close_SMA_5']) / df['Close_SMA_5']) * 100

    df.iloc[-1, df.columns.get_loc('Close')] = 105.00
    df.iloc[-1, df.columns.get_loc('Volume')] = 150000 # 不足 3 倍

    df['Limit_Up_Price'] = 110.00
    
    config = create_mock_theme_config()
    signals = scan_right_side(df, config)
    assert len(signals) == 0

def test_scan_right_side_no_signal_close_below_sma():
    df = generate_mock_klines(num_days=100, initial_price=100)

    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = ((df['Close'] - df['Close_SMA_5']) / df['Close_SMA_5']) * 100

    df.iloc[-1, df.columns.get_loc('Close')] = 95.00 # 低於 SMA_Close_20
    df.iloc[-1, df.columns.get_loc('Volume')] = 600000 # 確保是整數

    df['Limit_Up_Price'] = 110.00

    config = create_mock_theme_config()
    signals = scan_right_side(df, config)
    assert len(signals) == 0

def test_scan_right_side_filtered_by_limit_up_and_overheated():
    # 模擬漲停鎖死且乖離率 <= 8% 的情況
    df = generate_mock_klines(num_days=100, initial_price=100)
    
    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = 5.0 # 乖離率 <= 8%

    # scan_right_side 會自行重算 Bias_5d；收盤 110 時實際乖離為 8.03% (> 8)，
    # 改用 109 使實際乖離約 7.26%，才會落入「漲停且乖離 <= 8%」的過濾區
    df.iloc[-1, df.columns.get_loc('Close')] = 109.00 # 漲停價
    df.iloc[-1, df.columns.get_loc('Volume')] = 600000 # 大量，確保是整數

    df['Limit_Up_Price'] = 109.00 # 精確漲停價

    config = create_mock_theme_config()
    signals = scan_right_side(df, config)
    assert len(signals) == 0

def test_scan_right_side_not_filtered_by_limit_up_but_overheated():
    # 模擬漲停但乖離率 > 8% (不應被過濾)
    df = generate_mock_klines(num_days=100, initial_price=100)

    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = 10.0 # 乖離率 > 8%

    df.iloc[-1, df.columns.get_loc('Close')] = 110.00 # 漲停價
    df.iloc[-1, df.columns.get_loc('Volume')] = 600000 # 確保是整數

    df['Limit_Up_Price'] = 110.00

    config = create_mock_theme_config()
    signals = scan_right_side(df, config)
    assert len(signals) == 1 # 應該會產生訊號

def test_scan_right_side_not_filtered_by_overheated_but_not_limit_up():
    # 模擬乖離率 <= 8% 但不是漲停 (不應被過濾)
    df = generate_mock_klines(num_days=100, initial_price=100)

    df['SMA_Volume_5'] = pd.Series(np.full(len(df), 100000), index=df.index)
    df['SMA_Close_20'] = pd.Series(np.full(len(df), 100.0), index=df.index)

    df['Close_SMA_5'] = calculate_sma(df, 'Close', 5)
    df['Bias_5d'] = 5.0 # 乖離率 <= 8%

    df.iloc[-1, df.columns.get_loc('Close')] = 103.00 # 非漲停價
    df.iloc[-1, df.columns.get_loc('Volume')] = 600000 # 確保是整數

    df['Limit_Up_Price'] = 110.00

    config = create_mock_theme_config()
    signals = scan_right_side(df, config)
    assert len(signals) == 1 # 應該會產生訊號
