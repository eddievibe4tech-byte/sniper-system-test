import pandas as pd
import numpy as np
from typing import List, Dict
from dataclasses import dataclass

# 為了與 theme_models.py 互動，需要導入 ThemeConfig
from .theme_models import ThemeConfig, ThemeStock, Theme

@dataclass
class ScanSignal:
    symbol: str
    theme_id: str
    signal_type: str # "left_side" or "right_side"
    price: float
    reason: str

def calculate_natr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """計算 Normalized Average True Range (NATR)"""
    df_copy = df.copy()
    # 計算 True Range (TR)
    high_low = df_copy['High'] - df_copy['Low']
    high_prev_close = np.abs(df_copy['High'] - df_copy['Close'].shift(1))
    low_prev_close = np.abs(df_copy['Low'] - df_copy['Close'].shift(1))

    true_range = pd.DataFrame({
        'high_low': high_low,
        'high_prev_close': high_prev_close,
        'low_prev_close': low_prev_close
    }, index=df_copy.index).max(axis=1)

    # 計算 Average True Range (ATR)
    atr = true_range.ewm(span=period, adjust=False).mean()

    # 計算 NATR
    # 避免除以零，對於 Close 為 0 的情況設為 NaN 或 0
    natr = (atr / df_copy['Close']) * 100 # 通常 NATR 以百分比表示
    natr = natr.replace([np.inf, -np.inf], np.nan).fillna(0) # 處理潛在的 inf 值
    return natr

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """計算 Relative Strength Index (RSI)"""
    df_copy = df.copy()
    delta = df_copy['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(span=period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()

    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_sma(df: pd.DataFrame, column: str, period: int) -> pd.Series:
    """計算 Simple Moving Average (SMA)"""
    df_copy = df.copy()
    return df_copy[column].rolling(window=period).mean()

def scan_left_side(df: pd.DataFrame, theme_config: ThemeConfig, macro_state: str) -> List[ScanSignal]:
    """
    執行左側埋伏掃描。
    條件：當前月份在 seasonal_months 內。NATR_14 <= np.percentile(NATR_60d_history, 25) 且 RSI_14 <= 45。
    若 macro_state == "BEAR_DEFENSE"，強制回傳空 List。
    """
    if macro_state == "BEAR_DEFENSE":
        return []

    signals: List[ScanSignal] = []
    current_month = pd.Timestamp.now().month

    # 確保 DataFrame 包含必要的列
    if not all(col in df.columns for col in ['Open', 'High', 'Low', 'Close', 'Volume']):
        return []

    # 計算指標
    df_temp = df.copy()
    df_temp['NATR_14'] = calculate_natr(df_temp, period=14)
    df_temp['RSI_14'] = calculate_rsi(df_temp, period=14)

    # 獲取過去 60 天的 NATR 歷史數據，用於計算百分位數
    # 這裡假設 df 已經包含了足夠的歷史數據
    natr_history_60d = df_temp['NATR_14'].iloc[-60:].dropna()

    if len(natr_history_60d) < 20: # 至少需要足夠的數據點來計算有效的百分位數
        # 如果歷史數據不足，則無法有效計算百分位數，跳過左側掃描
        return []

    natr_25th_percentile = np.percentile(natr_history_60d, 25)

    # 只使用最新的數據進行判斷
    latest_data = df_temp.iloc[-1]

    if not theme_config.themes:
        return signals

    for theme in theme_config.themes:
        if current_month in theme.seasonal_months:
            if not theme.stocks:
                continue
            for stock in theme.stocks:
                # 檢查當前標的是否有足夠的數據
                if latest_data['NATR_14'] is not None and latest_data['RSI_14'] is not None and pd.notna(latest_data['NATR_14']) and pd.notna(latest_data['RSI_14']):
                    if latest_data['NATR_14'] <= natr_25th_percentile and latest_data['RSI_14'] <= 45:
                        signals.append(
                            ScanSignal(
                                symbol=stock.symbol,
                                theme_id=theme.theme_id,
                                signal_type="left_side",
                                price=latest_data['Close'],
                                reason=f"NATR({latest_data['NATR_14']:.2f}) <= 25th percentile({natr_25th_percentile:.2f}) & RSI({latest_data['RSI_14']:.2f}) <= 45 in seasonal month"
                            )
                        )
    return signals

def scan_right_side(df: pd.DataFrame, theme_config: ThemeConfig) -> List[ScanSignal]:
    """
    執行右側追擊掃描。
    條件：Volume_t >= SMA(Volume, 5) * 3 且 Close_t > SMA(Close, 20)。
    過熱與漲停過濾：排除 Close_t == Limit_Up_Price (漲停鎖死買不到) 且 5日乖離率 <= 8% 的標的。
    """
    signals: List[ScanSignal] = []

    # 確保 DataFrame 包含必要的列
    if not all(col in df.columns for col in ['Open', 'High', 'Low', 'Close', 'Volume']):
        return []

    df_temp = df.copy()
    df_temp['SMA_Volume_5'] = calculate_sma(df_temp, 'Volume', 5)
    df_temp['SMA_Close_20'] = calculate_sma(df_temp, 'Close', 20)

    # 計算 5 日乖離率 (Percentage Price Oscillator for 5-day deviation)
    df_temp['Close_SMA_5'] = calculate_sma(df_temp, 'Close', 5)
    df_temp['Bias_5d'] = ((df_temp['Close'] - df_temp['Close_SMA_5']) / df_temp['Close_SMA_5']) * 100

    # 只使用最新的數據進行判斷
    latest_data = df_temp.iloc[-1]

    limit_up_price = latest_data.get('Limit_Up_Price', float('inf')) # 如果沒有提供，則認為不會漲停

    # print(f"DEBUG: Latest data for right side scan: Volume={latest_data['Volume']:.0f}, SMA_Volume_5_x3={latest_data['SMA_Volume_5'] * 3:.0f}")
    # print(f"DEBUG: Close={latest_data['Close']:.2f}, SMA_Close_20={latest_data['SMA_Close_20']:.2f}")
    # print(f"DEBUG: Bias_5d={latest_data['Bias_5d']:.2f}, Limit_Up_Price={limit_up_price:.2f}")

    if not theme_config.themes:
        return signals

    for theme in theme_config.themes:
        if not theme.stocks:
            continue
        for stock in theme.stocks:
            # 檢查是否有足夠的數據且指標非空
            if (latest_data['Volume'] is not None and latest_data['SMA_Volume_5'] is not None and
                latest_data['Close'] is not None and latest_data['SMA_Close_20'] is not None and
                latest_data['Bias_5d'] is not None and
                pd.notna(latest_data['Volume']) and pd.notna(latest_data['SMA_Volume_5']) and
                pd.notna(latest_data['Close']) and pd.notna(latest_data['SMA_Close_20']) and
                pd.notna(latest_data['Bias_5d'])):

                # 右側邏輯
                volume_condition = latest_data['Volume'] >= latest_data['SMA_Volume_5'] * 3
                close_condition = latest_data['Close'] > latest_data['SMA_Close_20']

                # print(f"DEBUG: Stock {stock.symbol}: volume_condition={volume_condition}, close_condition={close_condition}")

                if volume_condition and close_condition:
                    # 過熱與漲停過濾
                    is_limit_up = np.isclose(latest_data['Close'], limit_up_price, atol=0.01) # 考慮浮點數精度
                    is_overheated = latest_data['Bias_5d'] <= 8

                    # print(f"DEBUG: Stock {stock.symbol}: is_limit_up={is_limit_up}, is_overheated={is_overheated}")
                    if not (is_limit_up and is_overheated):
                        signals.append(
                            ScanSignal(
                                symbol=stock.symbol,
                                theme_id=theme.theme_id,
                                signal_type="right_side",
                                price=latest_data['Close'],
                                reason=f"Volume({latest_data['Volume']:.0f}) >= 3x SMA_Volume_5({latest_data['SMA_Volume_5']:.0f}) & Close({latest_data['Close']:.2f}) > SMA_Close_20({latest_data['SMA_Close_20']:.2f})"
                            )
                        )
    return signals
