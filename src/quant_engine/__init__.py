"""quant_engine — FSM + Behavior Tree 確定性量化決策引擎（Phase 1 library 雛形）

設計原則：
- 零第三方依賴（純標準庫），可直接抽離為獨立 library（Phase 3）
- 閾值全部集中在 config.py（配置注入，無魔法數字散佈各處）
- FSM 滯回防跳變、熔斷時間鎖、部分平倉追蹤 —— 五塊鋼板皆有測試覆蓋
"""
from .config import EngineConfig, MarketFSMConfig, PortfolioFSMConfig, ExecutionConfig
from .engine import DecisionEngine
from .records import DecisionRecord

__version__ = "1.0.0"
__all__ = ["DecisionEngine", "EngineConfig", "DecisionRecord"]
