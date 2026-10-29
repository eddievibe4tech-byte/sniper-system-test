"""📲 雙軌手機推播：ntfy.sh (App 彈窗 + Gmail 自動轉寄)

設計重點（PR Review 修正版）：
- JSON Payload 傳輸：徹底解決 HTTP Header 中文編碼 `UnicodeEncodeError` 崩潰問題。
- Email 雙軌備援：讀取 `ALERT_EMAIL` 環境變數，交由 ntfy 伺服器原生轉寄 Gmail，
  免申請 Google 應用程式密碼、免管理 SMTP（零維護成本）。
- Priority 動態化：預設 4 (High)，讓 ColorOS/Android 觸發彈窗與響鈴；
  上層 PushBehaviorTree 可依市場情境傳入 2~5。
- Click URL：點擊通知直接開啟 GitHub Pages 狙擊看板 (`DASHBOARD_URL`)。
"""
import os
import logging

import requests

logger = logging.getLogger(__name__)

NTFY_ENDPOINT = "https://ntfy.sh"

# ntfy.sh 訊息長度上限（bytes），預留 buffer 避免被伺服器拒絕
NTFY_MAX_MESSAGE_BYTES = 4000


def push_ntfy(title: str, message: str, priority: int = 4, click_url: str = "") -> bool:
    """發送 ntfy 推播（可選 Gmail 雙軌轉寄）。

    Args:
        title: 推播標題（支援中文，走 JSON Body 不受 Header 編碼限制）
        message: 推播內文
        priority: ntfy 優先級 1~5（3=預設, 4=High, 5=Max/驚嘆號）
        click_url: 點擊跳轉網址；未傳入則降級讀取環境變數 DASHBOARD_URL

    Returns:
        True 表示 ntfy 回 200；任何異常一律吞掉回 False（推播失敗不拖垮決策主流程）。
    """
    topic = os.getenv("NTFY_TOPIC", "")
    if not topic:
        logger.warning("⚠️ NTFY_TOPIC 未設定，跳過推播與 Email 轉寄")
        return False

    # 🆕 讀取 Gmail 轉寄地址（選配）：ntfy 原生 email 轉寄，免 SMTP 密碼
    alert_email = os.getenv("ALERT_EMAIL", "")

    # 🟢 PR Review 修正：Priority 範圍驗證，限制在 ntfy 有效範圍 1~5
    priority = max(1, min(5, int(priority)))

    # 🟢 PR Review 修正：訊息長度檢查（ntfy 上限 4096 bytes，超長自動截斷）
    message_bytes = message.encode("utf-8")
    if len(message_bytes) > NTFY_MAX_MESSAGE_BYTES:
        logger.warning(f"⚠️ 推播訊息過長 ({len(message_bytes)} bytes)，已截斷至 {NTFY_MAX_MESSAGE_BYTES} bytes")
        message = message_bytes[:NTFY_MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore")

    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,  # 4 = High（確保 Android/OPPO 彈窗與響鈴）
        "tags": ["chart_increasing", "moneybag"],
    }

    # 🆕 雙軌備援：ntfy 會自動把推播內容打包寄到這組信箱
    if alert_email:
        payload["email"] = alert_email
        logger.info(f"📧 已啟用 Email 雙軌轉寄至: {alert_email}")

    # 若有設定 GitHub Pages 網址，點擊通知直接開瀏覽器看看板
    dashboard_url = click_url or os.getenv("DASHBOARD_URL", "")
    if dashboard_url:
        payload["click"] = dashboard_url

    try:
        r = requests.post(
            NTFY_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,  # Email 轉寄可能需要稍長一點的 timeout
        )
        ok = (r.status_code == 200)
        logger.info("✅ ntfy 推播與轉寄成功" if ok else f"⚠️ ntfy 失敗: {r.status_code} - {r.text}")
        return ok
    except Exception as e:
        logger.warning(f"ntfy 推播異常: {e}")
        return False
