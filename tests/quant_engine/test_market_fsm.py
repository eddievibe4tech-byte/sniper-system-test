"""🛡️ 鋼板二驗證：大盤滯回狀態機邊界測試（含 Review 修訂）

Review 重點：
- 致命#1/#2：below_ma200_days 改由黑板承載（非 FSM 實例變數），
  同批次重複 update 不再累加擊穿熊市門檻；亦支援歷史序列純計算值傳入。
- 測試盲點修正：滯回測試必須先真正進入 BULL，再驗證滯回帶內「不退出」。
"""
from src.quant_engine.fsm.market import MarketFSM
from src.quant_engine.config import MarketFSMConfig
from src.quant_engine.blackboard import Blackboard, MarketState


def make_bb(idx, ma60=1000, ma200=950):
    return Blackboard(market_index=idx, market_ma60=ma60, market_ma200=ma200,
                      peak_capital=1_000_000, current_capital=1_000_000)


def test_hysteresis_prevents_chattering():
    """🛡️ 真正的滯回驗證（Review 測試盲點修正）：
    1. 拉高至 +1.5% → 確認進入 BULL
    2. 回落至 -0.2%（跌破季線但未達 -0.5% 退出門檻）→ 仍維持 BULL（抗雜訊成功）
    3. 再跌至 -0.6%（低於退出門檻）→ 轉出 SIDEWAYS
    """
    fsm = MarketFSM(MarketFSMConfig())
    bb = make_bb(idx=1000, ma60=1000, ma200=950)

    # Step 1：指數拉高到 1015（高於季線 +1%）→ 進入牛市
    bb.market_index = 1015
    fsm.update(bb)
    assert bb.market_state == MarketState.BULL

    # Step 2：回跌至 998（已跌破季線，但仍在 -0.5% 退出門檻之上）
    bb.market_index = 998
    fsm.update(bb)
    assert bb.market_state == MarketState.BULL  # 🛡️ 滯回核心：不跳變

    # Step 3：進一步跌破 994（低於季線 -0.6% < -0.5% 門檻）→ 退出牛市
    bb.market_index = 994
    fsm.update(bb)
    assert bb.market_state == MarketState.SIDEWAYS


def test_sideeways_within_band_no_transition():
    """初始 SIDEWAYS、均線附近小震盪：任何方向都不該觸發狀態轉移"""
    fsm = MarketFSM(MarketFSMConfig())
    bb = make_bb(idx=1000, ma60=1000, ma200=950)

    for idx in (1000, 997, 994, 1005):
        bb.market_index = idx
        fsm.update(bb)
        assert bb.market_state == MarketState.SIDEWAYS  # 未達 +1% / 熊市門檻


def test_bear_requires_consecutive_days():
    """🛡️ 熊市需連續 N 天跌破年線（黑板持久化計數器路徑）"""
    fsm = MarketFSM(MarketFSMConfig(bear_confirm_days=3))
    bb = make_bb(idx=930, ma60=1000, ma200=950)

    for _ in range(2):
        fsm.update(bb)
        assert bb.market_state != MarketState.BEAR
    fsm.update(bb)  # 第 3 天
    assert bb.market_state == MarketState.BEAR


def test_batch_loop_does_not_break_bear_confirmation():
    """🛡️ 致命缺陷#1 回歸測試：同一日對 50 檔股票重複呼叫 update，
    只要外部傳入的 trading_days_below 是「當日真實天數」，熊市就不會被單日擊穿。
    （舊版 bug：計數器在 FSM 實例上，一天跑 50 檔直接累到 50 → bear_confirm_days=3 瞬間失效）
    """
    fsm = MarketFSM(MarketFSMConfig(bear_confirm_days=3))
    bb = make_bb(idx=930, ma60=1000, ma200=950)

    # 模擬一日批次：50 檔股票、同一天、今日才第一天跌破 → 永遠不該進熊市
    for _ in range(50):
        fsm.update(bb, trading_days_below=1)
        assert bb.market_state != MarketState.BEAR

    # 隔日（Day 2）、後日（Day 3）由持久化/歷史計算傳入真實天數 → 第 3 天才確認
    fsm.update(bb, trading_days_below=2)
    assert bb.market_state != MarketState.BEAR
    fsm.update(bb, trading_days_below=3)
    assert bb.market_state == MarketState.BEAR


def test_trading_days_below_pure_function_path():
    """🛡️ 致命缺陷#2 正面驗證：trading_days_below 由外部歷史序列純計算傳入，
    FSM 完全無實例狀態——新實例（等同隔天全新 CI Runner）行為一致。
    """
    cfg = MarketFSMConfig(bear_confirm_days=3)
    fsm_day1 = MarketFSM(cfg)
    bb = make_bb(idx=930, ma60=1000, ma200=950)
    fsm_day1.update(bb, trading_days_below=2)   # 歷史算出已連兩日跌破
    assert bb.market_state != MarketState.BEAR

    # 隔天：全新 FSM 實例（CI Runner 重置），但黑板狀態從 JSON 還原
    fsm_day2 = MarketFSM(cfg)
    fsm_day2.update(bb, trading_days_below=3)
    assert bb.market_state == MarketState.BEAR


def test_single_day_fake_breakout_does_not_confirm_bear():
    """🛡️ 單日假跌破年線後回升，計數歸零，不誤入熊市"""
    fsm = MarketFSM(MarketFSMConfig(bear_confirm_days=3))
    bb = make_bb(idx=930, ma60=1000, ma200=950)
    fsm.update(bb)
    fsm.update(bb)          # 已連兩日跌破
    bb.market_index = 960   # 假跌破隔天收復年線
    fsm.update(bb)
    assert bb.below_ma200_days == 0  # 計數確實歸零
    bb.market_index = 930   # 再跌破，計數重新從 1 開始
    fsm.update(bb)
    assert bb.market_state != MarketState.BEAR


def test_invalid_ma_data_skips_update():
    """資料異常（MA<=0）時狀態不變，引擎不被髒資料帶跑"""
    fsm = MarketFSM(MarketFSMConfig())
    bb = make_bb(idx=1000, ma60=0, ma200=950)
    fsm.update(bb)
    assert bb.market_state == MarketState.SIDEWAYS
