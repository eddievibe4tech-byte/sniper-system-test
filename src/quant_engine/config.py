"""🛡️ 鋼板一：配置注入 — 所有閾值全集中於此，絕不散佈在邏輯碼中。

frozen dataclass 保證運行期不可變異；要調參數只能顯式構造新 Config，
每筆決策紀錄都帶 config_version，可完全審計重現。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketFSMConfig:
    """大盤狀態機閾值（滯回設計核心）"""
    # 滯回帶：進入牛市需高於季線 +1%，離開只需低於 -0.5%
    bull_enter_above_ma60: float = 0.010
    bull_exit_below_ma60: float = -0.005
    # 熊市需連續 N 天跌破年線，且幅度達 -1%
    bear_enter_below_ma200: float = -0.010
    bear_confirm_days: int = 3
    bear_exit_above_ma200: float = 0.005


@dataclass(frozen=True)
class PortfolioFSMConfig:
    """帳戶風控狀態機閾值（熔斷 + 恢復）"""
    defensive_drawdown: float = 0.05
    defensive_consecutive_losses: int = 3
    breaker_drawdown: float = 0.10
    breaker_lock_days: int = 7
    recover_drawdown: float = 0.02      # 回撤修復至 2% 內才降階


@dataclass(frozen=True)
class ExecutionConfig:
    """行為樹執行層閾值（止損 / 移動停利）"""
    stop_loss_pct: float = 0.08
    tp_trigger_profit_pct: float = 0.15
    tp_trail_pct: float = 0.10
    tp_close_ratio: float = 0.50


@dataclass(frozen=True)
class LifecycleConfig:
    """🛡️ Issue #4（量化引擎 v2.0）：正金字塔生命週期閾值。

    配置集中化（鋼板一協定）：加碼條件、Break-Even 緩衝、Trailing Stop
    回撤倍數、整股/零股邊界全部在此，邏輯碼嚴禁散佈魔術數字。
    """
    add_min_gain_atr: float = 1.0     # 加碼門檻：現價 ≥ 成本 + 1×ATR（趨勢延續才准加碼）
    be_buffer_pct: float = 0.003      # Break-Even 緩衝：停損上移至成本 ×(1+0.3%)（覆蓋交易成本）
    trail_atr_mult: float = 2.5       # Trailing Stop：進場後最高價回撤 2.5×ATR ➜ 優先出場
    lot_mode: str = "ROUND_LOT"       # 下單位模式："ROUND_LOT" 整股 / "ODD_LOT" 零股
    lot_size: int = 1000              # 台股整股單位：1,000 股 = 1 張
    min_order_amount: float = 5000.0  # 零股最低單筆金額：低于此額被手續費低消侵蝕 ➜ 攔截
    max_position_weight: float = 0.20  # 單一標的名目上限佔帳戶比例（與 sizer 政策一致，AC#1 加碼封頂）


@dataclass(frozen=True)
class EngineConfig:
    """引擎總配置：四個子域 + 版本號（寫入每筆 DecisionRecord 供審計）"""
    market: MarketFSMConfig = field(default_factory=MarketFSMConfig)
    portfolio: PortfolioFSMConfig = field(default_factory=PortfolioFSMConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    version: str = "1.0.0"
