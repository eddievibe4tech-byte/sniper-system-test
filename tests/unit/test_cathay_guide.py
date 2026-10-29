"""🏦 國泰三階分流指南測試：純整張 / 混合部位 / 純零股 邊界"""
from src.cathay_guide import generate_cathay_guide

PLAN = {"entry_type": "pullback", "entry_zone": [48.0, 50.0],
        "stop_loss": 45.0, "take_profit_1": 56.0}


def test_pure_full_lot():
    """預算 100,000 / 現價 50 → 2000 股純整張 → 二擇一智慧單。"""
    g = generate_cathay_guide("2330", 50.0, PLAN, 100000)
    assert g["method"] == "二擇一智慧單"
    assert g["full_lots"] == 2 and g["odd_shares"] == 0
    assert g["warning"] == ""


def test_mixed_position():
    """預算 75,000 / 現價 50 → 1500 股混合部位 → 尾數 500 需另設小鈴鐺。"""
    g = generate_cathay_guide("2330", 50.0, PLAN, 75000)
    assert g["method"].startswith("混合部位")
    assert g["full_lots_shares"] == 1000 and g["odd_shares"] == 500
    assert "剩餘 500 股請另設小鈴鐺" in g["warning"]


def test_pure_odd_shares():
    """預算 30,000 / 現價 50 → 600 股純零股 → 小鈴鐺＋手動紀律警告。"""
    g = generate_cathay_guide("2330", 50.0, PLAN, 30000)
    assert g["method"] == "小鈴鐺到價警示"
    assert g["full_lots"] == 0 and g["odd_shares"] == 600
    assert "無法掛智慧單" in g["warning"]


def test_no_entry_plan_returns_none():
    g = generate_cathay_guide("2330", 50.0, {"entry_type": "none"}, 100000)
    assert g is None


def test_invalid_inputs_return_none():
    assert generate_cathay_guide("2330", 0, PLAN, 100000) is None
    assert generate_cathay_guide("2330", 50.0, PLAN, 0) is None
    assert generate_cathay_guide("2330", 99999.0, PLAN, 3000) is None  # 買不起 1 股


def test_missing_plan_still_generates_defaults():
    """trading_plan=None（如 STOP_LOSS 派工）→ 給自訂停損/停利的兜底文案。"""
    g = generate_cathay_guide("2330", 50.0, None, 100000)
    assert g["method"] == "二擇一智慧單"
    assert "自訂停損價" in g["stop_loss"]["condition"]


def test_entry_zone_single_element_no_indexerror():
    """🔴 PR Review 修正驗證：entry_zone 只有 1 個元素時不應拋出 IndexError，
    應降級為「附近限價買進」文案。"""
    g = generate_cathay_guide("2330", 500.0, {"entry_zone": [500.0]}, 50000)
    assert g is not None
    assert "附近限價買進" in g["entry"]["condition"]
    assert "區間分批進場" not in g["entry"]["condition"]


def test_entry_zone_empty_list_fallback():
    """entry_zone 為空列表 → 同樣降級兜底文案。"""
    g = generate_cathay_guide("2330", 50.0, {"entry_zone": []}, 100000)
    assert "附近限價買進" in g["entry"]["condition"]
