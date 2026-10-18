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
class EngineConfig:
    """引擎總配置：三個子域 + 版本號（寫入每筆 DecisionRecord 供審計）"""
    market: MarketFSMConfig = field(default_factory=MarketFSMConfig)
    portfolio: PortfolioFSMConfig = field(default_factory=PortfolioFSMConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    version: str = "1.0.0"
