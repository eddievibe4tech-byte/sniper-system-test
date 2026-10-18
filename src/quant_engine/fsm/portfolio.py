"""🛡️ 鋼板三＋四：帳戶風控狀態機（熔斷時間鎖 + 降階恢復 + peak 更新）

三個關鍵修正點：
1. peak_capital 每次 update 都取 max —— 否則回撤計算失真
2. 熔斷鎖定期內無視一切輸入 —— 防「回本即解鎖」的偽安全感
3. 解鎖後先降階 DEFENSIVE、不直接回 NORMAL —— 帶傷復出先減碼
"""
from datetime import datetime, timedelta

from ..blackboard import Blackboard, PortfolioState
from ..config import PortfolioFSMConfig


class PortfolioFSM:
    def __init__(self, cfg: PortfolioFSMConfig):
        self.cfg = cfg

    def update(self, bb: Blackboard, now: datetime) -> None:
        # 🛡️ peak_capital 必須持續更新，否則回撤計算失真
        bb.peak_capital = max(bb.peak_capital, bb.current_capital)

        dd = (bb.peak_capital - bb.current_capital) / bb.peak_capital if bb.peak_capital > 0 else 0.0
        cur = bb.portfolio_state

        # 熔斷狀態：時間鎖內無視一切輸入
        if cur == PortfolioState.CIRCUIT_BREAKER:
            if bb.locked_until:
                lock_dt = datetime.fromisoformat(bb.locked_until)
                if now < lock_dt:
                    return
            if dd < self.cfg.recover_drawdown:
                bb.portfolio_state = PortfolioState.DEFENSIVE  # 降階而非直接回 NORMAL
                bb.locked_until = None
                bb.reasons.append("熔斷鎖定期滿且回撤修復，降階為防禦狀態")
            return

        # 正常狀態轉移
        if dd >= self.cfg.breaker_drawdown:
            bb.portfolio_state = PortfolioState.CIRCUIT_BREAKER
            bb.locked_until = (now + timedelta(days=self.cfg.breaker_lock_days)).isoformat()
            bb.reasons.append(f"帳戶回撤 {dd:.1%}，觸發熔斷鎖死 {self.cfg.breaker_lock_days} 天")
        elif dd >= self.cfg.defensive_drawdown or bb.consecutive_losses >= self.cfg.defensive_consecutive_losses:
            if cur != PortfolioState.DEFENSIVE:
                bb.portfolio_state = PortfolioState.DEFENSIVE
                bb.reasons.append(f"回撤 {dd:.1%} 或連虧 {bb.consecutive_losses} 筆，進入防禦")
        elif cur == PortfolioState.DEFENSIVE and dd < self.cfg.recover_drawdown and bb.consecutive_losses == 0:
            bb.portfolio_state = PortfolioState.NORMAL
            bb.reasons.append("回撤修復且無連虧，恢復正常運行")
