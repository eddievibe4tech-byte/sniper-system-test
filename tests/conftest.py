"""pytest 全域 conftest：單元測試環境隔離

🔴 Alpha Vantage Fallback（五層鏈）引入 data/financial_cache.json 後，
test_financial_statements_422.py 等既有測試若共用 repo 真實快取路徑，
會出現「前一檔測試寫入快取 → 後一檔測試誤命中 cache:」的跨測試污染。
此處以 autouse fixture 將 CACHE_PATH 重導到每個測試獨立的 tmp 目錄，
保證測試彼此隔離、且絕不污染 repo 的 data/。
"""
import pytest


@pytest.fixture(autouse=True)
def isolate_financial_cache(tmp_path, monkeypatch):
    """把財報快取路徑重導到 tmp_path（每個測試獨立、自動失效還原）"""
    from src import financial_cache as fc
    monkeypatch.setattr(fc, "CACHE_PATH", tmp_path / "financial_cache.json")
