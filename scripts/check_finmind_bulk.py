"""
FinMind 批量端點參數相容性守門腳本（#90 加固）

驗證「批量查詢」（stock_id 傳空字串 → 請求完全不帶 data_id 參數）
不會被 FinMind 拒絕（400）。這類「端點參數不相容」的 bug 曾因
_make_request 永遠附帶 data_id=（空字串）而讓海選全部抓失敗，
卻被偽裝成「市場太弱」。掛進 ci.yml 在合併前擋下復發。

使用方式：
    FINMIND_TOKEN=xxx python scripts/check_finmind_bulk.py

- 有設定 token：直接打 FinMind API 驗證批量端點回傳正常。
- 未設定 token（如公開 CI job）：以 mock 驗證 _make_request 的
  參數契約 —— 批量查詢的請求 URL 必須「不含 data_id」。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from finmind_client import FinMindClient  # noqa: E402


def check_live(client):
    """直接打 FinMind：批量端點不得因缺少 data_id 而回 400/None"""
    ok = True
    for ds, sd, ed in [("TaiwanStockMonthRevenue", "2026-08-01", "2026-08-31"),
                       ("TaiwanStockInstitutionalInvestorsBuySell", "2026-09-24", "2026-09-24")]:
        data = client._make_request(ds, "", start_date=sd, end_date=ed)
        print(f"{ds}: {'OK ' + str(len(data)) + ' rows' if data is not None else 'FAIL'}")
        ok &= data is not None
    return ok


def check_contract():
    """無 token 環境（CI）：用假 response 驗證請求參數契約"""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": 200, "msg": "success", "data": [{"stock_id": "2330", "revenue": 1}]}

    client = FinMindClient(token="dummy_token_for_contract_check")

    def fake_get(url, params=None, timeout=None):
        captured.update(params or {})
        return FakeResp()

    client.session.get = fake_get

    # 批量查詢：request_params 不得出現 data_id
    client._make_request("TaiwanStockMonthRevenue", "",
                         start_date="2026-08-01", end_date="2026-08-31")
    bulk_ok = "data_id" not in captured
    print(f"bulk request params: {sorted(captured.keys())} -> "
          f"{'OK (no data_id)' if bulk_ok else 'FAIL (data_id leaked)'}")

    # 個股查詢：必須帶 data_id=<stock_id>
    captured.clear()
    client._make_request("TaiwanStockPrice", "2330",
                         start_date="2026-09-01", end_date="2026-09-25")
    single_ok = captured.get("data_id") == "2330"
    print(f"single-stock request params: {sorted(captured.keys())} -> "
          f"{'OK (data_id=2330)' if single_ok else 'FAIL (data_id missing/wrong)'}")

    return bulk_ok and single_ok


if __name__ == "__main__":
    token = os.getenv('FINMIND_TOKEN', '')
    if token:
        client = FinMindClient(token=token)
        ok = check_live(client)
    else:
        print("⚠️ 未設定 FINMIND_TOKEN，改跑參數契約（mock）檢查")
        ok = check_contract()
    sys.exit(0 if ok else 1)
