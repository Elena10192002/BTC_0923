from flask import Flask, request, abort

import os
import json
from dotenv import load_dotenv

load_dotenv()

import btc_agent_final as btc_agent
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)

from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)


# ============================================================
# Flask / LINE Bot 設定
# ============================================================

app = Flask(__name__)

configuration = Configuration(
    access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
)

handler = WebhookHandler(
    os.getenv("LINE_CHANNEL_SECRET")
)

# ============================================================
# 個人化提醒 SQLite 資料庫
# ============================================================
ALERT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "btc_ai.db",)

# 雲端外部排程呼叫 /scheduler-check 時使用的保護金鑰。
# 請在 Render Environment Variables 設定 SCHEDULER_SECRET。
SCHEDULER_SECRET = os.getenv("SCHEDULER_SECRET")


def init_alert_db():
    """建立個人化提醒資料表。"""

    with sqlite3.connect(ALERT_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_user_id TEXT NOT NULL,
                condition_type TEXT NOT NULL,
                threshold REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_triggered_at TEXT)""")

        # 舊版 alerts.db 可能還沒有 condition_met 欄位，
        # 啟動時自動補上，不需要刪除原本資料庫。
        alert_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}

        if "condition_met" not in alert_columns:
            conn.execute("ALTER TABLE alerts ADD COLUMN condition_met INTEGER")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                line_user_id TEXT PRIMARY KEY,
                daily_push_time TEXT,
                daily_push_enabled INTEGER NOT NULL DEFAULT 0,
                last_daily_push_date TEXT,
                volatility_alert INTEGER NOT NULL DEFAULT 0,
                volatility_threshold REAL NOT NULL DEFAULT 3.0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

        settings_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(user_settings)").fetchall()
        }
        if "volatility_alert" not in settings_columns:
            conn.execute(
                "ALTER TABLE user_settings "
                "ADD COLUMN volatility_alert INTEGER NOT NULL DEFAULT 0"
            )
        if "volatility_threshold" not in settings_columns:
            conn.execute(
                "ALTER TABLE user_settings "
                "ADD COLUMN volatility_threshold REAL NOT NULL DEFAULT 3.0"
            )


def add_alert(line_user_id, condition_type, threshold):
    """新增提醒，回傳提醒 ID。"""

    with sqlite3.connect(ALERT_DB) as conn:
        cursor = conn.execute(
            """
            INSERT INTO alerts (
                line_user_id,
                condition_type,
                threshold
            )
            VALUES (?, ?, ?)
            """,
            (
                line_user_id,
                condition_type,
                float(threshold),),)
        return cursor.lastrowid


def get_active_alerts(line_user_id=None):
    """取得啟用中的提醒。"""

    with sqlite3.connect(ALERT_DB) as conn:
        if line_user_id:
            cursor = conn.execute(
                """
                SELECT id, line_user_id, condition_type, threshold
                FROM alerts
                WHERE enabled = 1
                  AND line_user_id = ?
                ORDER BY id
                """,
                (line_user_id,),)
        else:
            cursor = conn.execute(
                """
                SELECT id, line_user_id, condition_type, threshold
                FROM alerts
                WHERE enabled = 1
                ORDER BY id
                """)
        return cursor.fetchall()


def delete_alert(alert_id, line_user_id):
    """刪除使用者自己的指定提醒。"""

    with sqlite3.connect(ALERT_DB) as conn:
        cursor = conn.execute(
            """
            DELETE FROM alerts
            WHERE id = ?
              AND line_user_id = ?
            """,
            (alert_id, line_user_id),)
        return cursor.rowcount > 0


def get_alert_condition_state(alert_id):
    """取得提醒上一次是否處於『已達條件』狀態。

    回傳：
    - None：第一次檢查，尚未建立初始狀態
    - False：上一次未達條件
    - True：上一次已達條件
    """

    with sqlite3.connect(ALERT_DB) as conn:
        row = conn.execute(
            """
            SELECT condition_met
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()

    if row is None or row[0] is None:
        return None

    return bool(row[0])


def set_alert_condition_state(
    alert_id,
    condition_met,
    triggered=False,):
    """儲存目前條件狀態；真的觸發通知時才更新 last_triggered_at。"""

    with sqlite3.connect(ALERT_DB) as conn:
        if triggered:
            conn.execute(
                """
                UPDATE alerts
                SET condition_met = ?,
                    last_triggered_at = ?
                WHERE id = ?
                """,
                (
                    1 if condition_met else 0,
                    datetime.now().isoformat(timespec="seconds"),
                    alert_id,),)
        else:
            conn.execute(
                """
                UPDATE alerts
                SET condition_met = ?
                WHERE id = ?
                """,
                (
                    1 if condition_met else 0,
                    alert_id,),)


def set_daily_push_time(line_user_id, push_time):
    """設定或更新使用者每日 BTC 摘要推播時間。"""

    with sqlite3.connect(ALERT_DB) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (
                line_user_id,
                daily_push_time,
                daily_push_enabled,
                last_daily_push_date,
                updated_at)
            VALUES (?, ?, 1, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(line_user_id) DO UPDATE SET
                daily_push_time = excluded.daily_push_time,
                daily_push_enabled = 1,
                last_daily_push_date = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (line_user_id, push_time),)


def get_daily_push_setting(line_user_id):
    """取得使用者的每日摘要推播設定。"""

    with sqlite3.connect(ALERT_DB) as conn:
        cursor = conn.execute(
            """
            SELECT daily_push_time, daily_push_enabled, last_daily_push_date
            FROM user_settings
            WHERE line_user_id = ?
            """,
            (line_user_id,),)
        return cursor.fetchone()


def disable_daily_push(line_user_id):
    """關閉使用者每日摘要推播。"""

    with sqlite3.connect(ALERT_DB) as conn:
        cursor = conn.execute(
            """
            UPDATE user_settings
            SET daily_push_enabled = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE line_user_id = ?
            """,
            (line_user_id,),)
        return cursor.rowcount > 0


def get_due_daily_push_users(current_time, current_date):
    """取得現在應收到每日摘要、且今天尚未推播的使用者。"""

    with sqlite3.connect(ALERT_DB) as conn:
        cursor = conn.execute(
            """
            SELECT line_user_id
            FROM user_settings
            WHERE daily_push_enabled = 1
              AND daily_push_time = ?
              AND (
                    last_daily_push_date IS NULL
                    OR last_daily_push_date != ?
                  )
            """,
            (current_time, current_date),)
        return [row[0] for row in cursor.fetchall()]


def mark_daily_push_sent(line_user_id, current_date):
    """記錄今天已推播，避免同一天重複傳送。"""

    with sqlite3.connect(ALERT_DB) as conn:
        conn.execute(
            """
            UPDATE user_settings
            SET last_daily_push_date = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE line_user_id = ?
            """,
            (current_date, line_user_id),)


def set_volatility_alert(line_user_id, enabled, threshold=None):
    """設定 1h 劇烈波動提醒；未提供 threshold 時保留原門檻。"""
    with sqlite3.connect(ALERT_DB) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (
                line_user_id, volatility_alert, volatility_threshold, updated_at
            )
            VALUES (?, ?, COALESCE(?, 3.0), CURRENT_TIMESTAMP)
            ON CONFLICT(line_user_id) DO UPDATE SET
                volatility_alert = excluded.volatility_alert,
                volatility_threshold = CASE
                    WHEN ? IS NULL THEN user_settings.volatility_threshold
                    ELSE ?
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (line_user_id, 1 if enabled else 0, threshold, threshold, threshold),
        )


def get_volatility_setting(line_user_id):
    """取得使用者的 1h 劇烈波動設定。"""
    with sqlite3.connect(ALERT_DB) as conn:
        row = conn.execute(
            """
            SELECT volatility_alert, volatility_threshold
            FROM user_settings
            WHERE line_user_id = ?
            """,
            (line_user_id,),
        ).fetchone()
    if row is None:
        return {"enabled": False, "threshold": 3.0}
    return {"enabled": bool(row[0]), "threshold": float(row[1])}


def get_all_volatility_settings():
    """取得所有已開啟 1h 劇烈波動提醒的使用者與門檻。"""
    with sqlite3.connect(ALERT_DB) as conn:
        rows = conn.execute(
            """
            SELECT line_user_id, volatility_threshold
            FROM user_settings
            WHERE volatility_alert = 1
            ORDER BY line_user_id
            """
        ).fetchall()
    return [{"user_id": row[0], "threshold": float(row[1])} for row in rows]


def parse_daily_push_time_command(user_text):
    """解析「設定每日提醒 09:30」這類個人化時間指令。"""

    text = user_text.strip().replace("：", ":")

    match = re.match(
        r"^(?:設定)?(?:每日提醒|每日摘要|每日推播)\s*(\d{1,2}):(\d{2})$",text,)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return {"error": "時間格式錯誤，請使用 00:00～23:59，例如：設定每日提醒 09:30"}

    return {"time": f"{hour:02d}:{minute:02d}"}


def alert_condition_text(condition_type, threshold):
    """把提醒條件轉成適合 LINE 顯示的文字。"""

    mapping = {
        "price_above": f"BTC 價格 ≥ ${threshold:,.2f}",
        "price_below": f"BTC 價格 ≤ ${threshold:,.2f}",
        "change_up": f"24H 漲幅 ≥ {threshold:.2f}%",
        "change_down": f"24H 跌幅 ≥ {threshold:.2f}%",
        "rsi_above": f"RSI14 ≥ {threshold:.2f}",
        "rsi_below": f"RSI14 ≤ {threshold:.2f}",}

    return mapping.get(
        condition_type,
        f"{condition_type} = {threshold}",)


def parse_alert_command(user_text):
    """解析使用者在 LINE 輸入的個人化提醒條件。"""

    text = (
        user_text.strip()
        .replace(",", "")
        .replace("％", "%"))

    patterns = [
        (
            r"^(?:設定)?提醒(?:我)?\s*(?:btc|bitcoin|比特幣)?\s*(?:價格)?\s*(?:高於|超過|>=|≥)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)$",
            "price_above",
        ),
        (
            r"^(?:設定)?提醒(?:我)?\s*(?:btc|bitcoin|比特幣)?\s*(?:價格)?\s*(?:低於|跌破|<=|≤)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)$",
            "price_below",
        ),
        (
            r"^(?:設定)?提醒(?:我)?\s*(?:24h|24小時)?\s*(?:漲幅|上漲)\s*(?:高於|超過|>=|≥)?\s*([0-9]+(?:\.[0-9]+)?)\s*%?$",
            "change_up",
        ),
        (
            r"^(?:設定)?提醒(?:我)?\s*(?:24h|24小時)?\s*(?:跌幅|下跌)\s*(?:高於|超過|>=|≥)?\s*([0-9]+(?:\.[0-9]+)?)\s*%?$",
            "change_down",
        ),
        (
            r"^(?:設定)?提醒(?:我)?\s*rsi(?:14)?\s*(?:高於|超過|>=|≥)\s*([0-9]+(?:\.[0-9]+)?)$",
            "rsi_above",
        ),
        (
            r"^(?:設定)?提醒(?:我)?\s*rsi(?:14)?\s*(?:低於|<=|≤)\s*([0-9]+(?:\.[0-9]+)?)$",
            "rsi_below",
        ),
    ]

    for pattern, condition_type in patterns:
        match = re.match(
            pattern,
            text,
            flags=re.IGNORECASE,)

        if match:
            threshold = float(match.group(1))

            if condition_type.startswith("rsi_") and not 0 <= threshold <= 100:
                return {
                    "error": "RSI 門檻請設定在 0～100 之間。"}

            return {
                "condition_type": condition_type,
                "threshold": threshold,}

    return None


# 程式載入時先確保資料表存在
init_alert_db()



# ============================================================
# n8n 連線測試
# ============================================================

@app.route("/n8n-test", methods=["GET"])
def n8n_test():

    return {
        "status": "ok",
        "message": "n8n 已成功連接 Python"
    }
# ============================================================
# n8n：取得 BTC 市場行情
# ============================================================

@app.route("/btc-market", methods=["GET"])
def btc_market():

    market_data = btc_agent.get_btc_market()

    return market_data

# ============================================================
# 主動推播 / 市場監測
# ============================================================

def push_text(user_id, message):
    """主動推播文字訊息到指定 LINE 使用者。"""
    if not user_id:
        return

    if len(message) > 4500:
        message = message[:4450] + "\n\n……內容過長，已省略部分文字。"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=message)]
            )
        )


def get_usdt_twd_rate():
    """取得 USD/TWD 參考匯率，供 USDT 價格換算成約當新台幣顯示。"""
    import yfinance as yf

    try:
        fx = yf.Ticker("TWD=X").history(period="1d", interval="1m")
        if fx.empty:
            fx = yf.Ticker("TWD=X").history(period="5d", interval="1d")
        if fx.empty:
            return None
        return float(fx["Close"].dropna().iloc[-1])
    except Exception as exc:
        print(f"⚠️ USD/TWD 匯率取得失敗：{exc}")
        return None


def format_twd_reference(usdt_price):
    """將 USDT 價格換算成約當新台幣；匯率失敗時不影響主要功能。"""
    rate = get_usdt_twd_rate()
    if rate is None:
        return None
    return float(usdt_price) * rate


def _flex_text(text, size="sm", weight=None, color=None, wrap=True, flex=None):
    """建立 LINE Flex Message 的文字元件 dict。"""
    item = {
        "type": "text",
        "text": str(text),
        "size": size,
        "wrap": wrap,
    }
    if weight:
        item["weight"] = weight
    if color:
        item["color"] = color
    if flex is not None:
        item["flex"] = flex
    return item


def build_daily_btc_flex(
    market,
    technical,
    news_analysis,
    selected_news,
    full_report,
    market_sentiment="暫無判定",
):
    """每日 BTC 摘要卡片：只用於主動每日推播，不影響其他提醒與互動功能。"""

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    price = market.get("price")
    change_24h = market.get("change_24h")
    rsi = technical.get("rsi14")
    if change_24h is None:
        change_text = "N/A"
        change_color = "#6B7280"
    else:
        change_text = f"{change_24h:+.2f}%"
        change_color = "#16A34A" if change_24h >= 0 else "#DC2626"

    price_text = f"{price:,.2f} USDT" if price is not None else "N/A"

    # 卡片只放摘要，不重複完整報告。
    if rsi is None:
        rsi_text = "N/A"
    elif rsi >= 70:
        rsi_text = f"{rsi:.2f}（超買）"
    elif rsi <= 30:
        rsi_text = f"{rsi:.2f}（超賣）"
    else:
        rsi_text = f"{rsi:.2f}（中性）"

    summary_rows = [
        ("市場價格", price_text, "#111827"),
        ("24H 漲跌", change_text, change_color),
        ("RSI14", rsi_text, "#111827"),
        ("新聞情緒", str(market_sentiment), "#111827"),
    ]

    body_contents = [
        _flex_text("📊 每日市場摘要", size="xl", weight="bold", color="#111827"),
        _flex_text(now.strftime("%Y/%m/%d"), size="sm", color="#6B7280"),
        {
            "type": "separator",
            "margin": "lg",
            "color": "#E5E7EB",
        },
    ]

    for label, value, value_color in summary_rows:
        body_contents.append({
            "type": "box",
            "layout": "horizontal",
            "margin": "md",
            "contents": [
                _flex_text(label, size="sm", color="#6B7280", flex=4),
                _flex_text(value, size="sm", weight="bold",
                           color=value_color, flex=6),
            ],
        })

    if selected_news:
        first_title = str(
            selected_news[0].get("display_title")
            or selected_news[0].get("title")
            or ""
        ).strip()
        if first_title:
            if len(first_title) > 36:
                first_title = first_title[:36] + "…"
            body_contents.extend([
                {
                    "type": "separator",
                    "margin": "lg",
                    "color": "#E5E7EB",
                },
                _flex_text("今日焦點", size="sm", weight="bold",
                           color="#374151"),
                _flex_text(first_title, size="sm", color="#4B5563"),
            ])

    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "color": "#2F80ED",
                    "action": {
                        "type": "message",
                        "label": "查看完整分析",
                        "text": "查看今日完整分析",
                    },
                }
            ],
        },
    }

    # LINE Bot SDK v3 的 FlexMessage.contents 需要 FlexContainer，
    # 不能直接把 Python dict 塞進去，否則送出時可能變成空 container，
    # LINE API 會回 400：At least one block must be specified。
    bubble_container = FlexContainer.from_json(
        json.dumps(bubble, ensure_ascii=False)
    )

    return FlexMessage(
        alt_text=f"BTC 每日市場摘要｜{now.strftime('%Y/%m/%d')}",
        contents=bubble_container,
    )


def push_daily_flex(user_id, flex_message):
    """只供每日摘要使用的 Flex 主動推播。"""
    if not user_id:
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[flex_message],
            )
        )


def push_flex_message(user_id, flex_message):
    """主動推播 Flex Message；供劇烈波動與個人化條件提醒共用。"""
    if not user_id:
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[flex_message],
            )
        )


def _build_alert_flex(
    title,
    rows,
    status_text="條件已達成",
    status_color="#16A34A",
    note=None,
    alt_text="BTC 提醒",
):
    """建立統一風格的提醒卡片，不改變任何提醒判斷邏輯。"""
    now = datetime.now(ZoneInfo("Asia/Taipei"))

    body_contents = [
        _flex_text(title, size="xl", weight="bold", color="#111827"),
        _flex_text(now.strftime("%Y/%m/%d %H:%M"), size="sm", color="#6B7280"),
        {
            "type": "separator",
            "margin": "lg",
            "color": "#E5E7EB",
        },
    ]

    for label, value, value_color in rows:
        body_contents.append({
            "type": "box",
            "layout": "horizontal",
            "margin": "md",
            "contents": [
                _flex_text(label, size="sm", color="#6B7280", flex=4),
                _flex_text(value, size="sm", weight="bold",
                           color=value_color, flex=6),
            ],
        })

    body_contents.extend([
        {
            "type": "separator",
            "margin": "lg",
            "color": "#E5E7EB",
        },
        _flex_text(status_text, size="sm", weight="bold", color=status_color),
    ])

    if note:
        body_contents.append(
            _flex_text(note, size="xs", color="#6B7280")
        )

    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "20px",
            "contents": body_contents,
        },
    }

    container = FlexContainer.from_json(
        json.dumps(bubble, ensure_ascii=False)
    )
    return FlexMessage(
        alt_text=alt_text,
        contents=container,
    )


def build_volatility_alert_flex(
    current_price,
    change_pct,
    threshold,
    twd_price=None,
    is_test=False,
    reached=True,
):
    """建立 1h 劇烈波動提醒卡片。"""
    change_color = "#16A34A" if change_pct >= 0 else "#DC2626"

    rows = [
        ("BTC 價格", f"{current_price:,.2f} USDT", "#111827"),
    ]
    if twd_price is not None:
        rows.append(("約當台幣", f"NT${twd_price:,.0f}", "#111827"))
    rows.extend([
        ("1H 漲跌", f"{change_pct:+.2f}%", change_color),
        ("個人門檻", f"±{threshold:g}%", "#111827"),
    ])

    if is_test and not reached:
        status_text = "測試訊息｜目前尚未達門檻"
        status_color = "#D97706"
        note = "此則僅用來測試 Push 功能。"
        title = "🧪 BTC 波動提醒測試"
    elif is_test:
        status_text = "測試訊息｜目前已達門檻"
        status_color = "#16A34A"
        note = "此則僅用來測試 Push 功能。"
        title = "🧪 BTC 波動提醒測試"
    else:
        status_text = "條件已達成"
        status_color = "#16A34A"
        note = "想了解市場狀況？直接問我"
        title = "🚨 BTC 劇烈波動"

    return _build_alert_flex(
        title=title,
        rows=rows,
        status_text=status_text,
        status_color=status_color,
        note=note,
        alt_text=f"BTC 1H 波動提醒｜{change_pct:+.2f}%",
    )


def build_personal_alert_flex(condition_type, threshold, current_value):
    """建立價格／24H／RSI 個人化條件提醒卡片。"""
    configs = {
        "price_above": (
            "🚨 BTC 價格突破提醒",
            "目前價格",
            f"${current_value:,.2f}",
            "設定目標",
            f"≥ ${threshold:,.2f}",
            "#16A34A",
        ),
        "price_below": (
            "🚨 BTC 價格跌破提醒",
            "目前價格",
            f"${current_value:,.2f}",
            "設定目標",
            f"≤ ${threshold:,.2f}",
            "#DC2626",
        ),
        "change_up": (
            "📈 BTC 24H 漲幅提醒",
            "目前 24H",
            f"{current_value:+.2f}%",
            "設定門檻",
            f"≥ +{threshold:.2f}%",
            "#16A34A",
        ),
        "change_down": (
            "📉 BTC 24H 跌幅提醒",
            "目前 24H",
            f"{current_value:+.2f}%",
            "設定門檻",
            f"≤ -{threshold:.2f}%",
            "#DC2626",
        ),
        "rsi_above": (
            "📊 BTC RSI 突破提醒",
            "目前 RSI14",
            f"{current_value:.2f}",
            "設定門檻",
            f"≥ {threshold:.2f}",
            "#D97706",
        ),
        "rsi_below": (
            "📊 BTC RSI 跌破提醒",
            "目前 RSI14",
            f"{current_value:.2f}",
            "設定門檻",
            f"≤ {threshold:.2f}",
            "#2563EB",
        ),
    }

    title, value_label, value_text, threshold_label, threshold_text, value_color = (
        configs[condition_type]
    )

    return _build_alert_flex(
        title=title,
        rows=[
            (value_label, value_text, value_color),
            (threshold_label, threshold_text, "#111827"),
        ],
        status_text="條件已達成",
        status_color="#16A34A",
        note="條件回到門檻外後，之後再次跨越時會再通知。",
        alt_text=title.replace("🚨 ", "").replace("📈 ", "").replace("📉 ", "").replace("📊 ", ""),
    )


# 暫存每位使用者最近一次每日完整報告。
# 使用者點卡片「查看完整分析」時，直接顯示當次推播原本的完整內容。
_latest_daily_reports = {}


def build_daily_btc_report():
    """整合行情、技術、新聞與跨市場資料，產生適合 LINE 的精簡 Daily Brief。"""

    market = btc_agent.get_btc_market()
    technical = btc_agent.get_btc_technical()
    # Daily 新聞使用「推播當下往前 24 小時」的精確時間窗。
    # Rich Menu / Pull 的「最新消息」仍維持 get_btc_news() 原本近期邏輯。
    daily_end = datetime.now(ZoneInfo("Asia/Taipei"))
    daily_start = daily_end - timedelta(hours=24)

    raw_news = btc_agent.get_btc_news(
        start_time=daily_start,
        end_time=daily_end,
    )

    # Daily 優先使用「推播當下往前 24 小時」。
    # 若嚴格 24h 視窗沒有足夠新聞，改用與「最新消息」相同的近期新聞來源，
    # 避免互動查得到新聞、Daily 卻顯示 0 則的體驗落差。
    daily_news_mode = "24h"
    if not raw_news:
        raw_news = btc_agent.get_btc_news()
        daily_news_mode = "recent_fallback"

    cross_market = btc_agent.get_cross_market()

    news_analysis = btc_agent.analyze_btc_news_with_ai(
        raw_news,
        top_n=3,
        daily_mode=(daily_news_mode == "24h"),
    )
    selected_news = news_analysis.get("selected_news", [])

    # 有些情況 raw_news 非空，但 Daily AI 篩選後仍為 0 則。
    # 此時再以「最新消息」的近期邏輯重試一次。
    if not selected_news and daily_news_mode == "24h":
        fallback_news = btc_agent.get_btc_news()
        if fallback_news:
            fallback_analysis = btc_agent.analyze_btc_news_with_ai(
                fallback_news,
                top_n=3,
            )
            fallback_selected = fallback_analysis.get("selected_news", [])
            if fallback_selected:
                raw_news = fallback_news
                news_analysis = fallback_analysis
                selected_news = fallback_selected
                daily_news_mode = "recent_fallback"

    brief_data = {
        "market": {
            "change_1h": market.get("change_1h"),
            "change_24h": market.get("change_24h"),
            "price": market.get("price"),
            "high_24h": market.get("high_24h"),
            "low_24h": market.get("low_24h"),
        },
        "technical": {
            "close": technical.get("close"),
            "ma5": technical.get("ma5"),
            "ma20": technical.get("ma20"),
            "ma60": technical.get("ma60"),
            "rsi14": technical.get("rsi14"),
            "rsi_signal": technical.get("rsi_signal"),
            "macd": technical.get("macd"),
            "macd_signal_line": technical.get("macd_signal_line"),
            "macd_histogram": technical.get("macd_histogram"),
        },
        "news": {
            "overall_sentiment": news_analysis.get("overall_sentiment", "未分類"),
            "selected": [
                {
                    "title": item.get("title"),
                    "sentiment": item.get("sentiment"),
                    "summary": item.get("summary"),
                }
                for item in selected_news
            ],
        },
        "cross_market": {
            "recent_performance_window": cross_market.get("recent_performance_window"),
            "recent_performance_pct": {
                "BTC": (cross_market.get("recent_performance_pct") or {}).get("BTC"),
                "ETH": (cross_market.get("recent_performance_pct") or {}).get("ETH"),
            },
            "rolling_window": cross_market.get("rolling_window"),
            "latest_rolling_correlation_with_btc": {
                "ETH": (
                    cross_market.get("latest_rolling_correlation_with_btc") or {}
                ).get("ETH")
            },
        },
    }

    ai_prompt = f"""
你是 BTC AI 投資資訊助手。

以下資料已由 Python / API / 爬蟲取得與計算：
{json.dumps(brief_data, ensure_ascii=False, indent=2)}

請產生適合 LINE 每日主動推播的「AI 今日解讀」，繁體中文，約 90～140 個中文字。

規則：
1. 你的工作是跨模組統整，不是重新報數字。
2. 不得在解讀中重複任何精確價格、百分比、MA、RSI、MACD、相關係數等數值。
3. 只寫 2～3 句，約 70～110 個中文字。
4. 價格與均線的相對位置只能依輸入數值判斷；若無法確認就不要描述。
5. MACD 與 Signal 只能描述目前兩者的相對位置，不得自行推論成交量、買賣力道或資金流。
6. 新聞情緒只代表「入選新聞文本」的敘事傾向，不代表整體市場情緒或未來價格。
7. 相關性只表示報酬率在該觀察窗內的統計連動，不代表因果，也不代表資金流向。
8. 跨市場部分只可解讀輸入中的 BTC、ETH 與 BTC×ETH 關係；不得提及黃金、S&P 500、NASDAQ 或其他 Daily 畫面未呈現的市場。
9. 不得把新聞直接寫成價格變動原因。
9. 禁止使用沒有輸入資料直接支持的描述，包括：
   「買盤力道」「賣壓」「資金流入」「資金流出」「資金動向」「籌碼動向」
   「上升通道」「下降通道」「強勢震盪」「多空博弈」
   「法規環境改善／惡化」「市場信心增強／減弱」。
10. 不使用「建議投資人」「可以關注」「適合進場」等投資建議語氣。
11. 不提供買進、賣出、做多、做空或未來價格預測。
12. 不要加標題，不要使用 Markdown。
""".strip()

    # Flex 卡片的「新聞情緒」直接沿用入選重點新聞的整體文本情緒，
    # 不再額外呼叫 Gemini 判斷整體市場多空。
    market_sentiment = str(
        news_analysis.get("overall_sentiment", "未分類") or "未分類"
    ).strip()

    ai_insight = btc_agent._call_gemini_text(ai_prompt)
    # Daily 專用額外容錯：共用 Gemini 函式內部已會重試；若仍為空，
    # 再補一次 Daily 呼叫，不影響互動 Agent 或其他功能。
    if not ai_insight:
        ai_insight = btc_agent._call_gemini_text(ai_prompt)
    if not ai_insight:
        ai_insight = (
            "今日市場、技術、新聞與跨市場資訊已完成整理；"
            "AI 綜合解讀暫時無法取得，請以各區塊資料為準。"
        )

    def short_title(text, max_chars=31):
        text = str(text or "").strip()
        return text if len(text) <= max_chars else text[:max_chars] + "…"

    lines = []

    price = market.get("price")
    twd = market.get("price_twd_approx")
    change_24h = market.get("change_24h")
    high_24h = market.get("high_24h")
    low_24h = market.get("low_24h")

    lines.append("💰 市場")
    if price is not None:
        price_line = f"{price:,.2f} USDT"
        if twd is not None:
            price_line += f"（約 NT$ {twd:,.0f}）"
        lines.append(price_line)
    if change_24h is not None:
        lines.append(f"24h｜{change_24h:+.2f}%")
    if high_24h is not None and low_24h is not None:
        lines.append(f"區間｜{low_24h:,.0f}～{high_24h:,.0f} USDT")

    lines.extend(["", "📊 技術面"])
    ma5 = technical.get("ma5")
    ma20 = technical.get("ma20")
    ma60 = technical.get("ma60")
    if all(v is not None for v in [ma5, ma20, ma60]):
        lines.append(f"MA5｜{ma5:,.0f}｜MA20｜{ma20:,.0f}｜MA60｜{ma60:,.0f}")

    rsi = technical.get("rsi14")
    if rsi is not None:
        rsi_label = technical.get("rsi_signal") or ""
        suffix = f"（{rsi_label.replace('區', '')}）" if rsi_label else ""
        lines.append(f"RSI14｜{rsi:.2f}{suffix}")

    macd = technical.get("macd")
    signal = technical.get("macd_signal_line")
    if macd is not None and signal is not None:
        lines.append(f"MACD｜{macd:,.0f}｜Signal｜{signal:,.0f}")

    lines.extend(["", "📰 今日焦點"])
    if selected_news:
        for idx, item in enumerate(selected_news, start=1):
            lines.append(
                f"{idx}｜{short_title(item.get('display_title') or item.get('title'))}"
            )
            lines.append(f"情緒｜{item.get('sentiment', '未分類')}")
            url = str(item.get("url", "")).strip()
            if url:
                lines.append(f"🔗 原文｜{url}")
    else:
        lines.append("目前沒有足夠的近期 BTC 相關新聞。")

    lines.append(f"整體新聞情緒｜{news_analysis.get('overall_sentiment', '未分類')}")

    performance = cross_market.get("recent_performance_pct", {})
    corr = cross_market.get("latest_rolling_correlation_with_btc", {})
    perf_window = cross_market.get("recent_performance_window", 7)
    corr_window = cross_market.get("rolling_window", 30)

    lines.extend(["", "🌎 跨市場"])
    btc_perf = performance.get("BTC")
    eth_perf = performance.get("ETH")
    if btc_perf is not None and eth_perf is not None:
        lines.append(
            f"近 {perf_window} 個共同交易區間｜"
            f"BTC {btc_perf:+.2f}%｜ETH {eth_perf:+.2f}%"
        )
    eth_corr = corr.get("ETH")
    if eth_corr is not None:
        lines.append(f"近 {corr_window} 個觀察值｜BTC × ETH 相關係數 {eth_corr:.3f}")

    lines.extend([
        "",
        "🤖 AI 今日解讀",
        ai_insight,
        "",
        "ℹ️ 市場資訊整理與 AI 解讀，不代表未來走勢或投資建議。",
    ])

    report_text = "\n".join(lines)

    return {
        "text": report_text,
        "market": market,
        "technical": technical,
        "news_analysis": news_analysis,
        "selected_news": selected_news,
        "market_sentiment": market_sentiment,
    }

# ============================================================
# 個人化每日摘要時間：到指定時間後主動 LINE 推播
# ============================================================
def check_daily_push_schedule():
    """每分鐘檢查哪些使用者到了自己的每日摘要推播時間。"""

    now = datetime.now(ZoneInfo("Asia/Taipei"))
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    due_users = get_due_daily_push_users(
        current_time,
        current_date,
    )

    if not due_users:
        return 0

    print(f"⏰ 個人化每日摘要：{current_time} 有 {len(due_users)} 位使用者待推播。")

    try:
        daily = build_daily_btc_report()
    except Exception as exc:
        print(f"❌ 產生個人化每日摘要失敗：{exc}")
        return 0

    sent_count = 0

    for user_id in due_users:
        try:
            _latest_daily_reports[user_id] = daily["text"]

            flex_message = build_daily_btc_flex(
                daily["market"],
                daily["technical"],
                daily["news_analysis"],
                daily["selected_news"],
                daily["text"],
                daily["market_sentiment"],
            )
            push_daily_flex(user_id, flex_message)
            mark_daily_push_sent(user_id, current_date)
            sent_count += 1
            print(f"✅ 已於 {current_time} 推播每日摘要給 {user_id}")
        except Exception as exc:
            print(f"❌ 個人化每日摘要推播失敗 ({user_id})：{exc}")

    return sent_count

def get_btc_1h_change():
    """用 Binance 1h K 線計算目前這一小時相對前一根收盤的漲跌幅。"""
    import requests

    url = "https://data-api.binance.vision/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 2
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    if len(data) < 2:
        raise RuntimeError("Binance 1h K 線資料不足")

    previous_close = float(data[-2][4])
    current_price = float(data[-1][4])

    change_pct = (current_price / previous_close - 1) * 100

    return {
        "previous_close": previous_close,
        "current_price": current_price,
        "change_pct": change_pct
    }


# 簡單的記憶體防重複機制：
# 每位使用者的波動回到自己的門檻內後，才允許下一次觸發。
_volatility_alert_active_users = set()


def check_market_alerts(force_volatility_test=False):
    """檢查每位使用者的 1 小時個人化波動門檻。"""
    global _volatility_alert_active_users

    try:
        volatility = get_btc_1h_change()
        change_pct = float(volatility["change_pct"])
        volatility_settings = get_all_volatility_settings()
        volatility_twd_price = format_twd_reference(volatility["current_price"])

        for setting in volatility_settings:
            user_id = setting["user_id"]
            threshold = float(setting["threshold"])
            reached = abs(change_pct) >= threshold
            should_push = reached or force_volatility_test

            if should_push and (user_id not in _volatility_alert_active_users or force_volatility_test):
                flex_message = build_volatility_alert_flex(
                    current_price=volatility["current_price"],
                    change_pct=change_pct,
                    threshold=threshold,
                    twd_price=volatility_twd_price,
                    is_test=force_volatility_test,
                    reached=reached,
                )
                push_flex_message(user_id, flex_message)
                print(f"✅ 波動提醒已推播 user={user_id[:6]}... threshold=±{threshold:g}%")
                if reached:
                    _volatility_alert_active_users.add(user_id)
            elif not reached:
                _volatility_alert_active_users.discard(user_id)
    except Exception as exc:
        print(f"❌ 市場提醒監測失敗：{exc}")


# ============================================================
# 個人化提醒 Push 介面（沿用本檔既有 push_text）
# ============================================================
def push_alert_message(user_id, flex_message):
    """個人化條件提醒統一改用 Flex Card 主動推播。"""
    push_flex_message(user_id, flex_message)

# ============================================================
# 個人化提醒：讀取條件並觸發 LINE 通知
# ============================================================
def check_personal_alerts(line_user_id=None):
    """檢查個人化提醒，只在『跨越門檻』時通知。

    邏輯：
    1. 第一次檢查只記錄目前狀態，不通知。
    2. 上一次未達條件，本次達成：通知一次。
    3. 持續達成條件：不重複通知。
    4. 條件恢復：重新待命。
    5. 日後再次跨越門檻：可再次通知。

    line_user_id=None：排程檢查所有使用者。
    傳入 LINE User ID：只檢查該使用者。
    """

    alerts = get_active_alerts(line_user_id)

    if not alerts:
        print("🔍 個人化提醒：目前沒有啟用中的提醒。")
        return 0

    print(f"🔍 個人化提醒：開始檢查 {len(alerts)} 筆條件……")

    condition_types = {row[2] for row in alerts}

    market = None
    technical = None

    try:
        if condition_types & {
            "price_above",
            "price_below",
            "change_up",
            "change_down",
        }:
            market = btc_agent.get_btc_market()

        if condition_types & {
            "rsi_above",
            "rsi_below",
        }:
            technical = btc_agent.get_btc_technical()

    except Exception as exc:
        print(f"❌ 個人化提醒取得市場資料失敗：{exc}")
        return 0

    triggered_count = 0

    for (
        alert_id,
        user_id,
        condition_type,
        threshold,
    ) in alerts:

        try:
            condition_met = False
            current_value = None

            if condition_type == "price_above":
                price = market["price"]
                condition_met = price >= threshold
                current_value = price

            elif condition_type == "price_below":
                price = market["price"]
                condition_met = price <= threshold
                current_value = price

            elif condition_type == "change_up":
                change_24h = market["change_24h"]
                condition_met = change_24h >= threshold
                current_value = change_24h

            elif condition_type == "change_down":
                change_24h = market["change_24h"]
                condition_met = change_24h <= -threshold
                current_value = change_24h

            elif condition_type == "rsi_above":
                rsi = technical["rsi14"]
                condition_met = rsi >= threshold
                current_value = rsi

            elif condition_type == "rsi_below":
                rsi = technical["rsi14"]
                condition_met = rsi <= threshold
                current_value = rsi

            else:
                continue

            previous_state = get_alert_condition_state(alert_id)

            # 第一次檢查：只建立基準狀態。
            # 例如建立提醒時價格已經高於門檻，不會立刻誤判成「突破」。
            if previous_state is None:
                set_alert_condition_state(
                    alert_id,
                    condition_met,)

                state_text = "已達條件" if condition_met else "未達條件"
                print(
                    f"ℹ️ 提醒 #{alert_id} 初始狀態：{state_text}，本次不推播。")
                continue

            # False -> True：真正跨越門檻，通知一次
            if (not previous_state) and condition_met:
                flex_message = build_personal_alert_flex(
                    condition_type,
                    threshold,
                    current_value,
                )
                push_alert_message(
                    user_id,
                    flex_message,)

                set_alert_condition_state(
                    alert_id,
                    True,
                    triggered=True,)

                triggered_count += 1

                print(
                    f"✅ 個人化提醒 #{alert_id} 已跨越門檻並推播。")
                continue

            # True -> False：條件恢復，重新待命
            if previous_state and (not condition_met):
                set_alert_condition_state(
                    alert_id,
                    False,)

                print(
                    f"♻️ 個人化提醒 #{alert_id} 已恢復到門檻外，重新待命。")
                continue

            # 狀態沒改變：只同步狀態，不通知
            set_alert_condition_state(
                alert_id,
                condition_met,)

        except Exception as exc:
            print(
                f"❌ 個人化提醒 #{alert_id} 執行失敗：{exc}")

    return triggered_count


# ============================================================
# 手動查看提醒目前狀態
# ============================================================
def build_personal_alert_status_report(line_user_id):
    """立即查看提醒是否已達目標。

    這個函式只讀取最新市場資料並回報目前狀態：
    - 不會主動 Push
    - 不會修改 condition_met
    - 不會停用或刪除提醒
    """

    alerts = get_active_alerts(line_user_id)

    condition_types = {row[2] for row in alerts}

    market = None
    technical = None

    if condition_types & {
        "price_above",
        "price_below",
        "change_up",
        "change_down",}:
        market = btc_agent.get_btc_market()

    if condition_types & {
        "rsi_above",
        "rsi_below",}:
        technical = btc_agent.get_btc_technical()

    daily_setting = get_daily_push_setting(line_user_id)
    volatility_setting = get_volatility_setting(line_user_id)
    volatility_enabled = volatility_setting["enabled"]
    volatility_threshold = volatility_setting["threshold"]

    volatility_data = None
    if volatility_enabled:
        volatility_data = get_btc_1h_change()

    lines = ["🔍 個人化提醒檢查", ""]

    lines.append("⏰ 每日 BTC 摘要")
    if daily_setting and daily_setting[1]:
        lines.append(f"已開啟 ✅｜每天 {daily_setting[0]}")
    else:
        lines.append("尚未設定／已關閉")
    lines.append("")

    lines.append("🔔 條件提醒")
    if not alerts:
        lines.append("目前沒有啟用中的條件提醒")
        lines.append("")

    for display_no, (
        _alert_id,
        _user_id,
        condition_type,
        threshold,
    ) in enumerate(alerts, start=1):

        # -------------------------
        # BTC 價格高於
        # -------------------------
        if condition_type == "price_above":
            price = market["price"]
            reached = price >= threshold

            lines.append(
                f"{display_no}. BTC 價格 ≥ ${threshold:,.2f}")
            lines.append(
                f"   目前：${price:,.2f}")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   已高於目標：${price - threshold:,.2f}")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：${threshold - price:,.2f}")

        # -------------------------
        # BTC 價格低於
        # -------------------------
        elif condition_type == "price_below":
            price = market["price"]
            reached = price <= threshold

            lines.append(
                f"{display_no}. BTC 價格 ≤ ${threshold:,.2f}")
            lines.append(
                f"   目前：${price:,.2f}")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   已低於目標：${threshold - price:,.2f}")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：${price - threshold:,.2f}")

        # -------------------------
        # 24H 漲幅
        # -------------------------
        elif condition_type == "change_up":
            change_24h = market["change_24h"]
            reached = change_24h >= threshold

            lines.append(
                f"{display_no}. 24H 漲幅 ≥ {threshold:.2f}%")
            lines.append(
                f"   目前：{change_24h:+.2f}%")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   超過目標：{change_24h - threshold:.2f} 個百分點")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：{threshold - change_24h:.2f} 個百分點")

        # -------------------------
        # 24H 跌幅
        # -------------------------
        elif condition_type == "change_down":
            change_24h = market["change_24h"]
            target_change = -threshold
            reached = change_24h <= target_change

            lines.append(
                f"{display_no}. 24H 跌幅 ≥ {threshold:.2f}%")
            lines.append(
                f"   目前：{change_24h:+.2f}%")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   超過目標跌幅：{target_change - change_24h:.2f} 個百分點")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：{change_24h - target_change:.2f} 個百分點")

        # -------------------------
        # RSI 高於
        # -------------------------
        elif condition_type == "rsi_above":
            rsi = technical["rsi14"]
            reached = rsi >= threshold

            lines.append(
                f"{display_no}. RSI14 ≥ {threshold:.2f}")
            lines.append(
                f"   目前：{rsi:.2f}")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   高於目標：{rsi - threshold:.2f}")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：{threshold - rsi:.2f}")

        # -------------------------
        # RSI 低於
        # -------------------------
        elif condition_type == "rsi_below":
            rsi = technical["rsi14"]
            reached = rsi <= threshold

            lines.append(
                f"{display_no}. RSI14 ≤ {threshold:.2f}")
            lines.append(
                f"   目前：{rsi:.2f}")

            if reached:
                lines.append("   ✅ 已達成")
                lines.append(
                    f"   低於目標：{threshold - rsi:.2f}")
            else:
                lines.append("   ❌ 尚未達成")
                lines.append(
                    f"   距離目標：{rsi - threshold:.2f}")

        lines.append("")

    lines.append("⚡ 劇烈波動")
    if volatility_enabled:
        change_pct = volatility_data["change_pct"]
        reached = abs(change_pct) >= volatility_threshold
        lines.append(
            f"已開啟 ✅｜1h ±{volatility_threshold:g}%"
        )
        lines.append(f"目前 1h：{change_pct:+.2f}%")
        if reached:
            lines.append("✅ 已達到設定門檻")
        else:
            lines.append("❌ 尚未達到設定門檻")
    else:
        lines.append("已關閉 🔕")

    lines.extend([
        "",
        "ℹ️ 此功能只查看目前狀態，不會觸發、停用或刪除提醒。"
    ])

    return "\n".join(lines).strip()




# ============================================================
# 雲端排程入口
# ============================================================

@app.route("/scheduler-check", methods=["GET"])
def scheduler_check():
    """供外部雲端排程定期呼叫，重用既有的三組主動推播檢查。"""

    token = request.args.get("token", "")

    if not SCHEDULER_SECRET:
        return {
            "status": "error",
            "message": "SCHEDULER_SECRET is not configured"
        }, 503

    if token != SCHEDULER_SECRET:
        return {
            "status": "error",
            "message": "unauthorized"
        }, 401

    results = {
        "daily_push_sent": 0,
        "personal_alerts_triggered": 0,
        "volatility_check": "not_run",
    }
    errors = {}

    try:
        results["daily_push_sent"] = check_daily_push_schedule()
    except Exception as exc:
        errors["daily_push"] = str(exc)

    try:
        results["personal_alerts_triggered"] = check_personal_alerts()
    except Exception as exc:
        errors["personal_alerts"] = str(exc)

    try:
        check_market_alerts()
        results["volatility_check"] = "completed"
    except Exception as exc:
        errors["volatility"] = str(exc)

    return {
        "status": "ok" if not errors else "partial_error",
        "results": results,
        "errors": errors,
        "time": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds"),
    }, 200


# ============================================================
# LINE Webhook
# ============================================================

@app.route("/callback", methods=["POST"])
def callback():

    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)

    except InvalidSignatureError:
        abort(400)

    return "OK"


# ============================================================
# 接收 LINE 訊息
# ============================================================

@handler.add(
    MessageEvent,
    message=TextMessageContent
)
def handle_message(event):

    user_text = event.message.text.strip()
    user_id = event.source.user_id


    # 每日 Flex 卡片按鈕：顯示該次推播原本的完整分析
    if user_text == "查看今日完整分析":
        cached_report = _latest_daily_reports.get(user_id)

        if cached_report:
            reply = (
                "🌞 BTC DAILY｜完整分析\n\n"
                f"{cached_report}\n\n"
                "💬 想深入了解？直接問我"
            )
        else:
            # 例如 Render 重啟後記憶體暫存消失，重新產生一份最新完整分析。
            try:
                daily = build_daily_btc_report()
                _latest_daily_reports[user_id] = daily["text"]
                reply = (
                    "🌞 BTC DAILY｜完整分析\n\n"
                    f"{daily['text']}\n\n"
                    "💬 想深入了解？直接問我"
                )
            except Exception as exc:
                reply = f"❌ 完整每日分析暫時無法取得：{exc}"

    # 設定個人化每日摘要時間
    elif parse_daily_push_time_command(user_text) is not None:
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法設定每日提醒時間。"
        else:
            parsed_time = parse_daily_push_time_command(user_text)

            if "error" in parsed_time:
                reply = f"⚠️ {parsed_time['error']}"
            else:
                push_time = parsed_time["time"]
                set_daily_push_time(
                    user_id,
                    push_time,)

                reply = (
                    "✅ 每日 BTC 摘要時間設定完成\n\n"
                    f"每天推播時間：{push_time}\n"
                    "時區：Asia/Taipei\n\n"
                    "之後系統會在你設定的時間主動傳送每日 BTC 市場摘要。")

    # 查看個人化每日摘要時間
    elif user_text in [
        "查看每日提醒",
        "查看每日摘要",
        "每日提醒設定",]:
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法讀取每日提醒設定。"
        else:
            setting = get_daily_push_setting(user_id)

            if not setting or not setting[1]:
                reply = (
                    "⏰ 目前尚未啟用每日 BTC 摘要推播。\n\n"
                    "例如可以輸入：\n"
                    "設定每日提醒 09:30")
            else:
                push_time, _, last_date = setting
                last_text = last_date or "尚未推播"
                reply = (
                    "⏰ 我的每日 BTC 摘要設定\n\n"
                    f"推播時間：每天 {push_time}\n"
                    "時區：Asia/Taipei\n"
                    f"最近推播日期：{last_text}")

    # 關閉個人化每日摘要
    elif user_text in [
        "關閉每日提醒",
        "取消每日提醒",
        "關閉每日摘要",]:
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法關閉每日提醒。"
        else:
            setting = get_daily_push_setting(user_id)

            if not setting or not setting[1]:
                reply = "ℹ️ 你的每日 BTC 摘要目前已是關閉狀態。"
            else:
                disable_daily_push(user_id)
                reply = "✅ 已關閉每日 BTC 摘要主動推播。"

    # 查看提醒
    elif user_text in ["查看提醒", "我的提醒", "提醒列表"]:
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法讀取個人化提醒。"
        else:
            alerts = get_active_alerts(user_id)

            if not alerts:
                reply = (
                    "🔔 目前沒有啟用中的個人化提醒。\n\n"
                    "例如可以輸入：\n"
                    "提醒價格高於 120000")
            else:
                lines = [
                    "🔔 我的個人化提醒",
                    "",]

                for display_no, (
                    _alert_id,
                    _,
                    condition_type,
                    threshold,
                ) in enumerate(alerts, start=1):
                    lines.append(
                        f"{display_no}. {alert_condition_text(condition_type, threshold)}")

                lines.extend([
                    "",
                    "若要刪除，可輸入：刪除提醒 1",])

                reply = "\n".join(lines)

    # 刪除提醒
    # 使用者輸入的是目前「查看提醒」畫面上的第幾筆，
    # 程式再對應到 SQLite 真正的 alert_id。
    elif re.match(
        r"^刪除提醒\s*\d+$",
        user_text,):
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法刪除提醒。"
        else:
            display_no = int(
                re.search(r"\d+", user_text).group())

            alerts = get_active_alerts(user_id)

            if display_no < 1 or display_no > len(alerts):
                reply = (
                    f"⚠️ 目前沒有第 {display_no} 筆提醒。\n"
                    "請先輸入「查看提醒」確認目前的提醒編號。")
            else:
                actual_alert_id = alerts[display_no - 1][0]

                deleted = delete_alert(
                    actual_alert_id,
                    user_id,
                )

                if deleted:
                    reply = (
                        f"✅ 已刪除第 {display_no} 筆提醒。\n"
                        "提醒列表會自動重新從 1 開始連續顯示。")
                else:
                    reply = "❌ 刪除提醒失敗，請稍後再試。"

    # 防呆：看起來像刪除提醒，但格式不正確時，不交給 Gemini 猜測。
    elif (
        "刪除" in user_text
        and (
            "提醒" in user_text
            or "編號" in user_text
            or "#" in user_text
        )
    ):
        reply = (
            "⚠️ 無法辨識要刪除的提醒。\n"
            "請先輸入「個人化設置」確認目前的條件提醒，\n"
            "再輸入例如「刪除提醒 1」。"
        )

    # 手動查看目前提醒狀態
    elif user_text == "檢查提醒":
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法檢查提醒。"
        else:
            try:
                reply = build_personal_alert_status_report(
                    user_id)
            except Exception as exc:
                reply = f"❌ 檢查提醒失敗：{exc}"

    # 新增提醒
    elif parse_alert_command(user_text) is not None:
        if not user_id:
            reply = "❌ 無法取得 LINE User ID，因此無法建立個人化提醒。"
        else:
            parsed_alert = parse_alert_command(user_text)

            if "error" in parsed_alert:
                reply = f"⚠️ {parsed_alert['error']}"
            else:
                condition_type = parsed_alert["condition_type"]
                threshold = parsed_alert["threshold"]

                alert_id = add_alert(
                    user_id,
                    condition_type,
                    threshold,)

                reply = (
                    "✅ 個人化提醒已建立\n\n"
                    f"條件：{alert_condition_text(condition_type, threshold)}\n\n"
                    "系統每 5 分鐘檢查一次市場資料；"
                    "條件成立時會主動 LINE 通知。\n"
                    "同一輪條件成立時不會重複通知；回到門檻外後，之後再次跨越可再次通知。")


    # ========================================================
    # 5. 設定個人化劇烈波動門檻
    # 例如：設定波動提醒 2.5%
    # ========================================================

    elif (
        user_text.startswith("設定波動提醒")
        or user_text.startswith("設定劇烈波動提醒")
    ):

        numbers = re.findall(
            r"\d+(?:\.\d+)?",
            user_text
        )

        if not numbers:
            reply = (
                "請輸入想設定的 1 小時波動門檻，例如：\n\n"
                "「設定波動提醒 3%」\n"
                "「設定波動提醒 2.5%」"
            )
        else:
            threshold = float(numbers[0])

            if threshold <= 0 or threshold > 100:
                reply = "波動門檻請設定為大於 0% 且不超過 100%。"
            else:
                set_volatility_alert(
                    user_id,
                    True,
                    threshold
                )

                # 門檻變更後重置該使用者的防重複狀態
                _volatility_alert_active_users.discard(user_id)

                reply = (
                    "⚡ BTC 波動提醒已設定\n\n"
                    "監控區間｜1 小時\n"
                    f"提醒門檻｜±{threshold:g}%\n\n"
                    f"當 BTC 一小時漲跌幅達到 ±{threshold:g}% 時，\n"
                    "系統將主動透過 LINE 通知你。"
                )


    # ========================================================
    # 5. 開啟劇烈波動提醒
    # ========================================================

    elif user_text in [
        "開啟波動提醒",
        "開啟劇烈波動提醒"
    ]:

        set_volatility_alert(
            user_id,
            True
        )

        volatility_setting = get_volatility_setting(user_id)
        threshold = volatility_setting["threshold"]

        reply = (
            "⚡ 劇烈波動提醒已開啟\n\n"
            f"觸發條件｜1h 漲跌幅達 ±{threshold:g}%\n"
            "如要修改，可輸入「設定波動提醒 3%」。"
        )


    # ========================================================
    # 6. 關閉劇烈波動提醒
    # ========================================================

    elif user_text in [
        "關閉波動提醒",
        "關閉劇烈波動提醒"
    ]:

        set_volatility_alert(
            user_id,
            False
        )

        reply = (
            "🔕 劇烈波動提醒已關閉"
        )


    # ========================================================
    # 7. 個人化提醒設定
    # ========================================================

    elif user_text in [
        "個人化設置",
        "個人化設定",
        "個人化提醒",
        "提醒設定",
        "劇烈波動提醒"
    ]:

        # btc_ai.db：1h 劇烈波動設定
        volatility_setting = get_volatility_setting(user_id)
        volatility_enabled = volatility_setting["enabled"]
        volatility_threshold = volatility_setting["threshold"]

        # btc_ai.db：每日摘要、價格／24H／RSI 條件提醒
        personal_alerts = get_active_alerts(user_id)
        daily_setting = get_daily_push_setting(user_id)

        lines = [
            "⚙️ 我的個人化設定",
            "",
            "⏰ 每日 BTC 摘要"
        ]

        if daily_setting and daily_setting[1]:
            lines.append(f"已開啟 ✅｜每天 {daily_setting[0]}")
            lines.append("設定方式｜設定每日提醒 09:30")
            lines.append("關閉方式｜關閉每日提醒")
        else:
            lines.append("尚未設定")
            lines.append("設定方式｜設定每日提醒 09:30")

        lines.extend([
            "",
            "🔔 條件提醒"
        ])

        if personal_alerts:
            for display_no, (
                _alert_id,
                _line_user_id,
                condition_type,
                threshold,
            ) in enumerate(personal_alerts, start=1):
                lines.append(
                    f"{display_no}｜{alert_condition_text(condition_type, threshold)}"
                )
        else:
            lines.append("尚未設定")

        lines.extend([
            "",
            "設定方式｜輸入：",
            "提醒價格高於 85000",
            "提醒價格低於 75000",
            "提醒24H漲幅 5%",
            "提醒24H跌幅 5%",
            "提醒RSI高於 70",
            "提醒RSI低於 30",
        ])

        volatility_text = "已開啟 ✅" if volatility_enabled else "已關閉 🔕"

        if personal_alerts:
            lines.extend([
                "",
                "刪除方式｜輸入「刪除提醒 1」",
            ])

        lines.extend([
            "",
            "⚡ 劇烈波動",
            f"{volatility_text}｜1h ±{volatility_threshold:g}%",
        ])

        if volatility_enabled:
            lines.append("關閉方式｜關閉波動提醒")

        lines.append("設定方式｜設定波動提醒 3%")
        lines.extend([
            "",
            "💡 時間與各項門檻數值皆可自行輸入設定",
        ])

        reply = "\n".join(lines)


    # ========================================================
    # 8. 推播功能測試（Demo / 開發用）
    # ========================================================

    elif user_text == "推播測試":

        try:
            daily = build_daily_btc_report()
            _latest_daily_reports[user_id] = daily["text"]

            flex_message = build_daily_btc_flex(
                daily["market"],
                daily["technical"],
                daily["news_analysis"],
                daily["selected_news"],
                daily["text"],
                daily["market_sentiment"],
            )

            push_daily_flex(user_id, flex_message)

            reply = (
                "✅ 已執行每日 BTC 推播測試，"
                "請查看是否收到另一則摘要訊息。"
            )

        except Exception as exc:
            reply = f"❌ 推播測試失敗：{exc}"


    elif user_text == "異常推播測試":

        try:
            volatility = get_btc_1h_change()
            change_pct = float(volatility["change_pct"])

            volatility_setting = get_volatility_setting(user_id)
            threshold = volatility_setting["threshold"]
            twd_price = format_twd_reference(volatility["current_price"])
            reached = abs(change_pct) >= threshold

            flex_message = build_volatility_alert_flex(
                current_price=volatility["current_price"],
                change_pct=change_pct,
                threshold=threshold,
                twd_price=twd_price,
                is_test=True,
                reached=reached,
            )
            push_flex_message(user_id, flex_message)

            reply = (
                "✅ 已執行異常波動 Push 測試。"
                f"正式監測會在 1 小時漲跌幅達 ±{threshold:g}% 時自動推播。"
            )

        except Exception as exc:
            reply = f"❌ 異常推播測試失敗：{exc}"


    elif user_text == "提醒卡片測試":

        try:
            market = btc_agent.get_btc_market()
            technical = btc_agent.get_btc_technical()

            price = float(market["price"])
            change_24h = float(market["change_24h"])
            rsi = float(technical["rsi14"])

            # 純 UI Demo：使用目前市場數值搭配可觸發的示範門檻。
            # 不新增／修改 SQLite 提醒，也不改 condition_met。
            demo_cards = [
                build_personal_alert_flex(
                    "price_above",
                    max(price - 1000, 0),
                    price,
                ),
                build_personal_alert_flex(
                    "change_up" if change_24h >= 0 else "change_down",
                    max(abs(change_24h) - 0.10, 0),
                    change_24h,
                ),
                build_personal_alert_flex(
                    "rsi_above" if rsi >= 50 else "rsi_below",
                    max(rsi - 5, 0) if rsi >= 50 else min(rsi + 5, 100),
                    rsi,
                ),
            ]

            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=demo_cards,
                    )
                )

            reply = (
                "✅ 已執行個人提醒卡片測試。\n"
                "已另外推送價格、24H 漲跌與 RSI 三張示範卡片；"
                "此測試不會新增、刪除或改變你的正式提醒設定。"
            )

        except Exception as exc:
            reply = f"❌ 提醒卡片測試失敗：{exc}"


    # ========================================================
    # 8. 功能選單
    # ========================================================

    elif user_text.lower() in [
        "功能", "幫助", "help", "menu",
        "嗨", "你好", "哈囉", "hello", "hi"
    ]:

        reply = (
            "🤖 BTC AI 投資資訊助手\n\n"
            "嗨！我是你的 BTC 智慧資訊助手 👋\n\n"
            "我可以協助你掌握：\n"
            "💰 即時市場行情\n"
            "📊 技術指標與趨勢\n"
            "📰 BTC 最新消息\n"
            "📈 策略歷史回測\n"
            "🌎 跨市場關聯\n"
            "🔔 個人化價格與波動提醒\n\n"
            "👇 點選下方功能選單即可快速查詢\n"
            "💬 也可以直接用自然語言問我問題"
        )


# ========================================================
# 9. 無明確問題
# ========================================================

    elif user_text in [
        "?",
        "？",
        "...",
        "……",
        "。",
        "嗯",
        "呃"
    ]:

        reply = (
            "🤖 想了解 BTC 哪方面的資訊呢？\n\n"

            "你可以直接用自己的方式問我，"
            "不需要輸入固定指令。\n\n"

            "例如：\n"
            "💰「現在一顆多少？」\n"
            "📊「現在看起來偏強還是偏弱？」\n"
            "📰「最近有什麼重要消息？」\n"
            "📈「以前照均線操作會怎樣？」\n"
            "🌎「最近跟美股有一起動嗎？」\n"
            "💡「最近 BTC 怎麼了？」\n\n"

            "也可以輸入「個人化設置」管理提醒。"
        )


    # ========================================================
    # 10. Rich Menu「最新消息」快捷入口
    # ========================================================

    elif user_text.strip() in [
        "最新消息",
        "最新新聞",
        "BTC新聞",
        "BTC 新聞"
    ]:

        try:
            news_items = btc_agent.get_btc_news()
            analysis = btc_agent.analyze_btc_news_with_ai(
                news_items,
                top_n=3
            )

            selected_news = analysis.get(
                "selected_news",
                []
            )

            if not selected_news:
                reply = "📰 目前沒有取得足夠的近期 BTC 相關新聞。"
            else:
                lines = ["📰 BTC 重點新聞", ""]

                for index, item in enumerate(
                    selected_news,
                    start=1
                ):
                    date_text = item.get("date", "")
                    try:
                        date_text = datetime.strptime(
                            date_text,
                            "%Y-%m-%d"
                        ).strftime("%m/%d")
                    except Exception:
                        pass

                    lines.extend([
                        f"{index}｜{item.get('title', '')}",
                        f"{date_text}｜{item.get('source', '')}",
                    ])

                    summary = str(
                        item.get("summary", "")
                    ).strip()
                    if summary:
                        lines.append(
                            f"重點｜{summary[:120]}"
                        )

                    sentiment = item.get(
                        "sentiment",
                        "未分類"
                    )
                    reason = str(
                        item.get(
                            "sentiment_reason",
                            ""
                        )
                    ).strip()

                    sentiment_line = (
                        f"文本情緒｜{sentiment}"
                    )
                    if reason:
                        sentiment_line += f"｜{reason}"

                    lines.extend([
                        sentiment_line,
                        f"🔗 原文｜{item.get('url', '')}",
                        ""
                    ])

                overall = analysis.get(
                    "overall_sentiment",
                    "未分類"
                )
                insight = analysis.get(
                    "ai_insight",
                    ""
                )

                lines.extend([
                    f"🧭 整體新聞文本情緒｜{overall}",
                    "",
                    "🤖 AI 解讀",
                    insight or "目前無法取得 AI 新聞解讀。",
                    "",
                    "ℹ️ 新聞文本情緒只反映本次新聞內容的敘事傾向，不代表未來價格漲跌。"
                ])

                reply = "\n".join(lines)

        except Exception as exc:
            reply = f"❌ 新聞資料取得失敗：{exc}"


    # ========================================================
    # 10. Rich Menu「跨市場」快捷入口
    # ========================================================

    elif user_text.strip() == "跨市場":

        try:
            data = btc_agent.get_cross_market()

            performance = data.get(
                "recent_performance_pct",
                {}
            )
            correlation = data.get(
                "latest_rolling_correlation_with_btc",
                {}
            )

            performance_window = data.get(
                "recent_performance_window",
                7
            )
            rolling_window = data.get(
                "rolling_window",
                30
            )

            labels = {
                "BTC": "BTC",
                "ETH": "ETH",
                "SP500": "標普 500",
                "NASDAQ": "那斯達克",
                "Gold": "黃金"
            }

            def format_pct(key):
                value = performance.get(key)
                if value is None:
                    return "N/A"
                return f"{value:+.2f}%"

            def format_corr(key):
                value = correlation.get(key)
                if value is None:
                    return "N/A"
                return f"{value:.3f}"

            lines = [
                "🌎 跨市場觀察",
                "",
                f"📈 近期市場表現",
                f"期間｜近 {performance_window} 個共同交易區間",
            ]

            for key in [
                "BTC",
                "ETH",
                "Gold",
                "SP500",
                "NASDAQ"
            ]:
                lines.append(
                    f"{labels[key]}｜{format_pct(key)}"
                )

            lines.extend([
                "",
                f"🔗 與 BTC 報酬率相關性",
                f"期間｜近 {rolling_window} 個共同交易觀察值",
            ])

            for key in [
                "ETH",
                "Gold",
                "SP500",
                "NASDAQ"
            ]:
                lines.append(
                    f"{labels[key]}｜{format_corr(key)}"
                )

            ai_insight = btc_agent.generate_cross_market_ai_insight(
                data
            )

            if ai_insight:
                lines.extend([
                    "",
                    "🤖 AI 解讀",
                    ai_insight,
                ])

            lines.extend([
                "",
                "ℹ️ 相關係數反映市場報酬率的連動程度，不代表因果關係。",
            ])

            reply = "\n".join(lines)

        except Exception as exc:
            reply = f"❌ 跨市場資料取得失敗：{exc}"


    # ========================================================
    # 11. 其他問題 → BTC AI Agent
    # ========================================================

    else:

        reply = btc_agent.ask_btc_agent(
                user_text
        )


    # ========================================================
    # 回覆 LINE
    # ========================================================

    with ApiClient(configuration) as api_client:

        line_bot_api = MessagingApi(
            api_client
        )

        line_bot_api.reply_message_with_http_info(

            ReplyMessageRequest(

                reply_token=event.reply_token,

                messages=[
                    TextMessage(
                        text=reply
                    )
                ]

            )

        )
# ============================================================
# 啟動 Flask
# ============================================================

if __name__ == "__main__":

    scheduler = BackgroundScheduler(
        timezone="Asia/Taipei"
    )

    # 每 1 分鐘檢查各使用者設定的每日摘要時間
    scheduler.add_job(
        check_daily_push_schedule,
        trigger="interval",
        minutes=1,
        id="personal_daily_push_checker",
        replace_existing=True
    )

    # 每 5 分鐘檢查個人化市場提醒
    scheduler.add_job(
        check_personal_alerts,
        trigger="interval",
        minutes=5,
        id="personal_alert_checker",
        replace_existing=True,
        max_instances=1
    )

    # 每 5 分鐘檢查個人化 1 小時波動門檻
    scheduler.add_job(
        check_market_alerts,
        trigger="interval",
        minutes=5,
        id="market_alert_monitor",
        replace_existing=True,
        max_instances=1
    )

    scheduler.start()

    print("⏰ 個人化每日 BTC 摘要：每 1 分鐘檢查使用者設定時間")
    print("🔔 個人化提醒：每 5 分鐘檢查一次")
    print("🔎 1h 波動提醒監測：每 5 分鐘檢查一次")

    app.run(
        port=5000,
        debug=False,
        use_reloader=False
    )