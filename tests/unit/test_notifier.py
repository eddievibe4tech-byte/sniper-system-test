"""📲 雙軌推播模組測試：Payload 組裝 / Email 轉寄 / 無 Topic 跳過 / 異常吞掉"""
import json

import src.notifier as notifier


class _FakeResp:
    status_code = 200
    text = "ok"


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return captured


def test_skip_when_no_topic(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notifier.push_ntfy("t", "m") is False


def test_payload_contains_email_when_alert_email_set(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "sniper-test")
    monkeypatch.setenv("ALERT_EMAIL", "me@gmail.com")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    captured = _capture_post(monkeypatch)
    assert notifier.push_ntfy("標題", "內文", priority=5) is True
    p = captured["payload"]
    assert p["email"] == "me@gmail.com"
    assert p["priority"] == 5
    assert p["topic"] == "sniper-test"
    assert "click" not in p  # 未設 DASHBOARD_URL 不塞 click


def test_click_url_from_env_or_argument(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "sniper-test")
    monkeypatch.delenv("ALERT_EMAIL", raising=False)
    monkeypatch.setenv("DASHBOARD_URL", "https://example.github.io/dash/")
    captured = _capture_post(monkeypatch)
    notifier.push_ntfy("t", "m")
    assert captured["payload"]["click"] == "https://example.github.io/dash/"
    # 明確傳入 click_url 優先於環境變數
    notifier.push_ntfy("t", "m", click_url="https://other.url")
    assert captured["payload"]["click"] == "https://other.url"
    assert "email" not in captured["payload"]


def test_json_body_not_headers(monkeypatch):
    """中文走 JSON Body（utf-8），不受 HTTP Header 編碼限制。"""
    monkeypatch.setenv("NTFY_TOPIC", "sniper-test")
    captured = _capture_post(monkeypatch)
    notifier.push_ntfy("🚨 總經海嘯預警", "中文內容測試")
    body = json.dumps(captured["payload"], ensure_ascii=False)
    assert "總經海嘯預警" in body
    assert captured["headers"]["Content-Type"] == "application/json; charset=utf-8"


def test_network_exception_swallowed(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "sniper-test")

    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(notifier.requests, "post", boom)
    assert notifier.push_ntfy("t", "m") is False  # 不拋異常、不拖垮主流程
