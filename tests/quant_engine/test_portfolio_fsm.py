"""🛡️ 鋼板三＋四驗證：熔斷時間鎖、降階恢復、peak_capital 單調遞增"""
from datetime import datetime, timedelta

from src.quant_engine.fsm.portfolio import PortfolioFSM
from src.quant_engine.config import PortfolioFSMConfig
from src.quant_engine.blackboard import Blackboard, PortfolioState


def make_bb(peak, current, losses=0):
    return Blackboard(market_index=1000, market_ma60=1000, market_ma200=950,
                      peak_capital=peak, current_capital=current,
                      consecutive_losses=losses)


def test_peak_capital_always_increases():
    """🛡️ peak_capital 必須單調遞增"""
    fsm = PortfolioFSM(PortfolioFSMConfig())
    bb = make_bb(peak=1_000_000, current=950_000)
    fsm.update(bb, datetime.now())
    assert bb.peak_capital == 1_000_000

    bb.current_capital = 1_100_000
    fsm.update(bb, datetime.now())
    assert bb.peak_capital == 1_100_000

    bb.current_capital = 1_050_000  # 下跌也不影響 peak
    fsm.update(bb, datetime.now())
    assert bb.peak_capital == 1_100_000


def test_circuit_breaker_time_lock():
    """🛡️ 熔斷鎖定期內無視一切輸入"""
    fsm = PortfolioFSM(PortfolioFSMConfig(breaker_lock_days=7))
    bb = make_bb(peak=1_000_000, current=880_000)  # 回撤 12%
    now = datetime.now()

    fsm.update(bb, now)
    assert bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER

    # 第 3 天：帳戶恢復到 1_000_000，但仍在鎖定期
    bb.current_capital = 1_000_000
    bb.peak_capital = 1_000_000
    fsm.update(bb, now + timedelta(days=3))
    assert bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER  # 仍鎖死

    # 第 8 天：鎖定期滿且回撤修復
    fsm.update(bb, now + timedelta(days=8))
    assert bb.portfolio_state == PortfolioState.DEFENSIVE  # 降階而非直接回 NORMAL


def test_defensive_from_consecutive_losses():
    """🛡️ 連虧 3 筆觸發防禦"""
    fsm = PortfolioFSM(PortfolioFSMConfig())
    bb = make_bb(peak=1_000_000, current=995_000, losses=3)
    fsm.update(bb, datetime.now())
    assert bb.portfolio_state == PortfolioState.DEFENSIVE


def test_defensive_from_drawdown():
    """回撤達 5% 進入防禦（未達熔斷線）"""
    fsm = PortfolioFSM(PortfolioFSMConfig())
    bb = make_bb(peak=1_000_000, current=945_000)  # 回撤 5.5%
    fsm.update(bb, datetime.now())
    assert bb.portfolio_state == PortfolioState.DEFENSIVE


def test_recovery_to_normal():
    """防禦狀態下：回撤修復 < 2% 且無連虧 → 恢復正常"""
    fsm = PortfolioFSM(PortfolioFSMConfig())
    bb = make_bb(peak=1_000_000, current=945_000)
    fsm.update(bb, datetime.now())
    assert bb.portfolio_state == PortfolioState.DEFENSIVE

    bb.current_capital = 990_000  # 回撤 1% < 2%
    fsm.update(bb, datetime.now())
    assert bb.portfolio_state == PortfolioState.NORMAL


def test_breaker_unlocked_but_drawdown_not_recovered_stays_locked():
    """鎖定期滿但回撤未修復至 2% 內 → 維持熔斷，下次再檢查"""
    fsm = PortfolioFSM(PortfolioFSMConfig(breaker_lock_days=7))
    bb = make_bb(peak=1_000_000, current=880_000)
    now = datetime.now()
    fsm.update(bb, now)
    assert bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER

    # 第 8 天，僅修復到回撤 5%（> recover 2%）→ 仍熔斷
    bb.current_capital = 950_000
    fsm.update(bb, now + timedelta(days=8))
    assert bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER
