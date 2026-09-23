import time
import requests
import pandas as pd
import json
import re
import yfinance as yf
import os

from bs4 import BeautifulSoup
from io import StringIO
# ============================================================
# Skill 1：取得 Bitcoin 即時市場行情
# ============================================================

def get_usdt_twd_rate():
    """取得 USD/USDT 對台幣的近似參考匯率。"""
    try:
        fx = yf.Ticker("TWD=X")

        data = fx.history(period="1d", interval="1m")
        if data.empty:
            data = fx.history(period="5d", interval="1d")

        if data.empty:
            return None

        rate = float(data["Close"].dropna().iloc[-1])
        return rate if rate > 0 else None

    except Exception:
        return None


BINANCE_BTCUSDT_START_DATE = pd.Timestamp("2017-08-17")


def _normalize_naive_date(value):
    """統一轉成無時區的日日期，避免 tz-aware / tz-naive 比較錯誤。"""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def _get_binance_daily_range(start_date, end_date=None):
    """用 Binance BTC/USDT 日 K 取得指定日期區間的起點、終點價格與漲跌。"""
    start_ts = _normalize_naive_date(start_date)
    end_ts = (
        pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
        if end_date is None
        else _normalize_naive_date(end_date)
    )

    if start_ts >= end_ts:
        raise ValueError("開始日期必須早於結束日期")

    url = "https://data-api.binance.vision/api/v3/klines"
    all_klines = []
    current_start = int(start_ts.timestamp() * 1000)
    final_end = int((end_ts + pd.Timedelta(days=1)).timestamp() * 1000)

    while current_start < final_end:
        page_params = {
            "symbol": "BTCUSDT",
            "interval": "1d",
            "startTime": current_start,
            "endTime": final_end,
            "limit": 1000
        }
        response = requests.get(url, params=page_params, timeout=30)
        response.raise_for_status()
        page = response.json()

        if not page:
            break

        all_klines.extend(page)
        next_start = int(page[-1][0]) + 24 * 60 * 60 * 1000
        if next_start <= current_start:
            break
        current_start = next_start

        if len(page) < 1000:
            break

    if len(all_klines) < 2:
        raise RuntimeError("指定期間的 Binance 日 K 資料不足")

    start_price = float(all_klines[0][4])
    current_price = float(all_klines[-1][4])

    return {
        "unit": "date_range",
        "start_date": pd.to_datetime(all_klines[0][0], unit="ms").strftime("%Y-%m-%d"),
        "end_date": pd.to_datetime(all_klines[-1][0], unit="ms").strftime("%Y-%m-%d"),
        "requested_start_date": start_ts.strftime("%Y-%m-%d"),
        "requested_end_date": end_ts.strftime("%Y-%m-%d"),
        "start_price": start_price,
        "current_price": current_price,
        "change_pct": (current_price / start_price - 1) * 100,
        "data_source": "Binance",
        "market": "BTC/USDT",
        "data_source_note": "完整查詢區間可由 Binance BTC/USDT 涵蓋，因此使用 Binance。"
    }


def _get_coingecko_daily_range(start_date, end_date=None):
    """用 CoinGecko BTC/USD 歷史日資料取得指定日期區間價格變化。"""
    start_ts = _normalize_naive_date(start_date)
    end_ts = (
        pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
        if end_date is None
        else _normalize_naive_date(end_date)
    )

    if start_ts >= end_ts:
        raise ValueError("開始日期必須早於結束日期")

    url = "https://www.coingecko.com/price_charts/export/bitcoin/usd.csv"
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))

    if "event_date" not in df.columns or "close_price_usd" not in df.columns:
        raise RuntimeError("CoinGecko 歷史資料欄位格式異常")

    df["event_date"] = (
        pd.to_datetime(df["event_date"], errors="coerce", utc=True)
        .dt.tz_localize(None)
        .dt.normalize()
    )
    df["close_price_usd"] = pd.to_numeric(df["close_price_usd"], errors="coerce")

    df = (
        df[["event_date", "close_price_usd"]]
        .dropna(subset=["event_date", "close_price_usd"])
        .drop_duplicates(subset=["event_date"], keep="last")
        .sort_values("event_date")
        .reset_index(drop=True)
    )

    selected = df[
        (df["event_date"] >= start_ts)
        & (df["event_date"] <= end_ts)
    ].copy()

    if len(selected) < 2:
        raise RuntimeError("指定期間的 CoinGecko BTC/USD 歷史資料不足")

    first = selected.iloc[0]
    last = selected.iloc[-1]
    start_price = float(first["close_price_usd"])
    current_price = float(last["close_price_usd"])

    actual_start_ts = first["event_date"]
    actual_end_ts = last["event_date"]

    data_range_note = None
    if actual_start_ts > start_ts:
        data_range_note = (
            f"使用者指定起點為 {start_ts.strftime('%Y-%m-%d')}，"
            f"但此資料來源在本次可用資料中最早自 "
            f"{actual_start_ts.strftime('%Y-%m-%d')} 起，"
            f"因此實際計算期間為 "
            f"{actual_start_ts.strftime('%Y-%m-%d')} 至 "
            f"{actual_end_ts.strftime('%Y-%m-%d')}。"
        )

    return {
        "unit": "date_range",
        "start_date": actual_start_ts.strftime("%Y-%m-%d"),
        "end_date": actual_end_ts.strftime("%Y-%m-%d"),
        "requested_start_date": start_ts.strftime("%Y-%m-%d"),
        "requested_end_date": end_ts.strftime("%Y-%m-%d"),
        "start_price": start_price,
        "current_price": current_price,
        "change_pct": (current_price / start_price - 1) * 100,
        "data_source": "CoinGecko",
        "market": "BTC/USD",
        "data_source_note": (
            "查詢起點早於 Binance BTC/USDT 可用歷史，因此整段使用 "
            "CoinGecko BTC/USD，不混接兩種價格來源。"
        ),
        "data_range_note": data_range_note
    }


def _get_btc_daily_range_auto(start_date, end_date=None):
    """完整區間只用一個來源：Binance 可涵蓋就用 Binance，否則整段 CoinGecko。"""
    start_ts = _normalize_naive_date(start_date)

    if start_ts >= BINANCE_BTCUSDT_START_DATE:
        return _get_binance_daily_range(start_date, end_date)

    return _get_coingecko_daily_range(start_date, end_date)

def get_btc_change(hours=1):
    """用 Binance 1 小時 K 線計算指定整數小時的價格變化。"""
    if not isinstance(hours, int) or hours < 1:
        raise ValueError("小時區間必須為 1 小時以上的整數")

    if hours > 999:
        raise ValueError("較長期間請改用天數查詢")

    url = "https://data-api.binance.vision/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": hours + 1
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    klines = response.json()

    if len(klines) < hours + 1:
        raise RuntimeError(f"不足以計算近 {hours} 小時漲跌")

    start_price = float(klines[0][4])
    current_price = float(klines[-1][4])

    return {
        "unit": "hour",
        "hours": hours,
        "start_price": start_price,
        "current_price": current_price,
        "change_pct": (current_price / start_price - 1) * 100
    }


def get_btc_day_change(days=1):
    """以實際日期往前推指定天數，使用 Binance 日 K 計算。"""
    if not isinstance(days, int) or days < 1:
        raise ValueError("天數必須為 1 天以上的整數")

    end_date = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    start_date = end_date - pd.DateOffset(days=days)

    result = _get_btc_daily_range_auto(start_date, end_date)
    result.update({"unit": "day", "days": days, "hours": days * 24})
    return result


def get_btc_month_change(months=1):
    """以實際曆月往前推 X 個月，不用固定 30 天近似。"""
    if not isinstance(months, int) or months < 1:
        raise ValueError("月數必須為 1 個月以上的整數")

    end_date = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    start_date = end_date - pd.DateOffset(months=months)

    result = _get_btc_daily_range_auto(start_date, end_date)
    result.update({"unit": "month", "months": months})
    return result


def get_btc_year_change(years=1):
    """以實際曆年往前推 X 年，不用固定 365 天近似。"""
    if not isinstance(years, int) or years < 1:
        raise ValueError("年數必須為 1 年以上的整數")

    end_date = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    start_date = end_date - pd.DateOffset(years=years)

    result = _get_btc_daily_range_auto(start_date, end_date)
    result.update({"unit": "year", "years": years})
    return result


def get_btc_custom_range(start_date, end_date):
    """查詢使用者指定的起訖日期，並依區間自動選擇單一資料來源。"""
    result = _get_btc_daily_range_auto(start_date, end_date)
    result["unit"] = "custom_range"
    return result


def get_btc_market():

    # --------------------------------------------------------
    # 1. Binance 24 小時市場行情 API
    # --------------------------------------------------------

    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    params = {"symbol": "BTCUSDT"}

    response = requests.get(
        url,
        params=params,
        timeout=30
    )
    response.raise_for_status()
    data = response.json()

    price = float(data["lastPrice"])

    # --------------------------------------------------------
    # 2. 補充 1 小時價格變化
    # --------------------------------------------------------

    try:
        change_1h = get_btc_change(1)["change_pct"]
    except Exception:
        change_1h = None

    # --------------------------------------------------------
    # 3. 補充台幣參考價
    # --------------------------------------------------------

    usdt_twd_rate = get_usdt_twd_rate()
    price_twd = (
        price * usdt_twd_rate
        if usdt_twd_rate is not None
        else None
    )

    # --------------------------------------------------------
    # 4. 整理市場資訊
    # --------------------------------------------------------

    market_data = {
        "symbol": "BTCUSDT",
        "price": price,
        "price_twd_approx": price_twd,
        "usdt_twd_rate": usdt_twd_rate,
        "change_1h": change_1h,
        "change_24h": float(data["priceChangePercent"]),
        "high_24h": float(data["highPrice"]),
        "low_24h": float(data["lowPrice"]),
        "volume_btc": float(data["volume"]),
        "volume_usdt": float(data["quoteVolume"]),
        "trades_24h": int(data["count"])
    }

    return market_data
# ============================================================
# Skill 1B：依使用者指定的小時數取得 BTC 漲跌
# ============================================================

def get_btc_period_change(
    hours=None,
    days=None,
    months=None,
    years=None,
    start_date=None,
    end_date=None
):
    if start_date is not None and end_date is not None:
        return get_btc_custom_range(start_date, end_date)
    if years is not None:
        return get_btc_year_change(years)
    if months is not None:
        return get_btc_month_change(months)
    if days is not None:
        return get_btc_day_change(days)
    if hours is not None:
        return get_btc_change(hours)
    raise ValueError("請指定 hours、days、months、years 或起訖日期")


# ============================================================
# Skill 2：取得 Bitcoin 技術指標
# ============================================================

def get_btc_technical():

    # --------------------------------------------------------
    # 1. 取得 Binance BTC 日 K 線
    # --------------------------------------------------------

    url = "https://data-api.binance.vision/api/v3/klines"

    params = {
        "symbol": "BTCUSDT",
        "interval": "1d",
        "limit": 500
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()


    # --------------------------------------------------------
    # 2. 整理 K 線資料
    # --------------------------------------------------------

    df = pd.DataFrame(
        data,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore"
        ]
    )

    # 轉換日期
    df["date"] = pd.to_datetime(
        df["open_time"],
        unit="ms"
    )

    # 將價格轉成數值
    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )


    # --------------------------------------------------------
    # 3. 計算 MA5、MA20、MA60
    # --------------------------------------------------------

    df["MA5"] = (
        df["close"]
        .rolling(window=5)
        .mean()
    )

    df["MA20"] = (
        df["close"]
        .rolling(window=20)
        .mean()
    )

    df["MA60"] = (
        df["close"]
        .rolling(window=60)
        .mean()
    )


    # --------------------------------------------------------
    # 4. 計算 RSI14
    # --------------------------------------------------------

    delta = df["close"].diff()

    gain = delta.clip(lower=0)

    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14,
        adjust=False,
        min_periods=14
    ).mean()

    rs = avg_gain / avg_loss

    df["RSI14"] = (
        100 - (100 / (1 + rs))
    )


    # --------------------------------------------------------
    # 5. 計算 MACD
    # --------------------------------------------------------

    ema12 = df["close"].ewm(
        span=12,
        adjust=False
    ).mean()

    ema26 = df["close"].ewm(
        span=26,
        adjust=False
    ).mean()

    df["MACD"] = ema12 - ema26

    df["MACD_signal"] = (
        df["MACD"]
        .ewm(
            span=9,
            adjust=False
        )
        .mean()
    )

    df["MACD_histogram"] = (
        df["MACD"]
        - df["MACD_signal"]
    )


    # --------------------------------------------------------
    # 6. 取得最新一筆資料
    # --------------------------------------------------------

    latest = df.iloc[-1]


    # --------------------------------------------------------
    # 7. 判斷 MA 訊號
    # --------------------------------------------------------

    if latest["MA20"] > latest["MA60"]:
        ma_signal = "偏多"

    elif latest["MA20"] < latest["MA60"]:
        ma_signal = "偏空"

    else:
        ma_signal = "中性"


    # --------------------------------------------------------
    # 8. 判斷 RSI
    # --------------------------------------------------------

    if latest["RSI14"] >= 70:
        rsi_signal = "超買區"

    elif latest["RSI14"] <= 30:
        rsi_signal = "超賣區"

    else:
        rsi_signal = "中性區"


    # --------------------------------------------------------
    # 9. 判斷 MACD
    # --------------------------------------------------------

    if latest["MACD"] > latest["MACD_signal"]:
        macd_signal = "偏多"

    elif latest["MACD"] < latest["MACD_signal"]:
        macd_signal = "偏空"

    else:
        macd_signal = "中性"


    # --------------------------------------------------------
    # 10. 整理 Skill 回傳結果
    # --------------------------------------------------------

    technical_data = {

        "date": latest["date"].strftime("%Y-%m-%d"),

        "close": float(latest["close"]),

        "ma5": float(latest["MA5"]),

        "ma20": float(latest["MA20"]),

        "ma60": float(latest["MA60"]),

        "ma_signal": ma_signal,

        "rsi14": float(latest["RSI14"]),

        "rsi_signal": rsi_signal,

        "macd": float(latest["MACD"]),

        "macd_signal_line": float(
            latest["MACD_signal"]
        ),

        "macd_histogram": float(
            latest["MACD_histogram"]
        ),

        "macd_signal": macd_signal
    }


    # --------------------------------------------------------
    # 11. 回傳結果
    # --------------------------------------------------------

    return technical_data
# ============================================================
# Skill 3：取得 Bitcoin 最新相關新聞
# ============================================================

def get_btc_news(days=3, max_news=10, start_time=None, end_time=None):

    # ========================================================
    # 新聞來源 1：BlockTempo
    # ========================================================

    def fetch_blocktempo():

        url = "https://www.blocktempo.com/all-posts/"

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        news = []

        for article in soup.find_all("article"):

            title = ""
            article_url = ""

            for link in article.find_all("a", href=True):

                link_text = link.get_text(
                    " ",
                    strip=True
                )

                href = link.get(
                    "href",
                    ""
                )

                if (
                    len(link_text) > 10
                    and "Read more" not in link_text
                ):
                    title = link_text
                    article_url = href
                    break

            if not title or not article_url:
                continue

            article_text = article.get_text(
                " ",
                strip=True
            )

            # 顯示日期 + 精確發布時間分開保存。
            # 優先讀取 HTML <time datetime="...">；若網站只提供日期，
            # published_at 保持 None，避免把 00:00 誤當成真實發布時間。
            published_at = None
            time_tag = article.find("time")
            if time_tag and time_tag.get("datetime"):
                published_at = str(time_tag.get("datetime")).strip()

            date_match = re.search(
                r"\d{4}-\d{2}-\d{2}",
                published_at or article_text
            )

            published_date = (
                date_match.group()
                if date_match
                else None
            )

            summary = ""

            for p in article.find_all("p"):

                p_text = p.get_text(
                    " ",
                    strip=True
                )

                if len(p_text) > 20:
                    summary = p_text
                    break

            news.append({
                "date": published_date,
                "published_at": published_at,
                "title": title,
                "summary": summary,
                "url": article_url,
                "source": "BlockTempo"
            })

        return news


    # ========================================================
    # 新聞來源 2：鏈新聞 ABMedia
    # ========================================================

    def fetch_abmedia():

        url = "https://abmedia.io/blog"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120 Safari/537.36"
            )
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        candidate_urls = []

        # 從「最新文章」頁取得文章網址。
        # 排除分類頁、作者頁、標籤頁與站內功能頁。
        excluded_paths = (
            "/blog",
            "/category/",
            "/author/",
            "/tag/",
            "/contact",
            "/about",
        )

        for link in soup.find_all("a", href=True):

            href = link.get(
                "href",
                ""
            ).strip()

            if href.startswith("/"):
                href = "https://abmedia.io" + href

            if not href.startswith("https://abmedia.io/"):
                continue

            path_part = href.replace(
                "https://abmedia.io",
                "",
                1
            )

            if any(
                path_part.startswith(prefix)
                for prefix in excluded_paths
            ):
                continue

            # 文章網址通常為網域後直接接 slug。
            if path_part.count("/") != 1:
                continue

            href = href.split("#")[0].split("?")[0]

            if href not in candidate_urls:
                candidate_urls.append(href)

        news = []

        # 最新文章頁前段已足夠作為候選池，
        # 避免一次對網站發出過多請求。
        for article_url in candidate_urls[:25]:

            try:

                article_response = requests.get(
                    article_url,
                    headers=headers,
                    timeout=20
                )

                if article_response.status_code != 200:
                    continue

                article_soup = BeautifulSoup(
                    article_response.text,
                    "html.parser"
                )

                h1 = article_soup.find("h1")

                if h1 is None:
                    continue

                title = h1.get_text(
                    " ",
                    strip=True
                )

                if len(title) < 8:
                    continue

                # 優先從 JSON-LD 取得正式發布時間
                published_date = None
                published_at = None

                for script in article_soup.find_all(
                    "script",
                    type="application/ld+json"
                ):

                    raw = script.string

                    if not raw:
                        continue

                    try:
                        ld = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    items = (
                        ld
                        if isinstance(ld, list)
                        else [ld]
                    )

                    for item in items:

                        if not isinstance(item, dict):
                            continue

                        date_value = item.get(
                            "datePublished"
                        )

                        if date_value:
                            published_at = str(date_value).strip()
                            published_date = published_at[:10]
                            break

                    if published_date:
                        break

                # 備援：HTML meta article:published_time
                if published_date is None:

                    meta_date = article_soup.find(
                        "meta",
                        attrs={
                            "property":
                            "article:published_time"
                        }
                    )

                    if (
                        meta_date
                        and meta_date.get("content")
                    ):
                        published_at = str(meta_date["content"]).strip()
                        published_date = published_at[:10]

                # 摘要優先使用 meta description
                summary = ""

                meta_desc = article_soup.find(
                    "meta",
                    attrs={"name": "description"}
                )

                if (
                    meta_desc
                    and meta_desc.get("content")
                ):
                    summary = meta_desc[
                        "content"
                    ].strip()

                # 若沒有 meta description，
                # 再取文章中第一段較完整的文字
                if not summary:

                    for p in article_soup.find_all("p"):

                        p_text = p.get_text(
                            " ",
                            strip=True
                        )

                        if len(p_text) > 30:
                            summary = p_text
                            break

                news.append({
                    "date": published_date,
                    "published_at": published_at,
                    "title": title,
                    "summary": summary,
                    "url": article_url,
                    "source": "ABMedia"
                })

            except requests.exceptions.RequestException:
                continue

        return news


    # ========================================================
    # 新聞來源 3：Yahoo Finance（僅供 Daily 精確時間窗）
    # ========================================================

    def fetch_yahoo_finance():
        news = []

        try:
            ticker = yf.Ticker("BTC-USD")
            raw_items = ticker.get_news(count=30)
        except Exception:
            return news

        for raw in raw_items or []:
            try:
                content = raw.get("content") if isinstance(raw, dict) else None
                content = content if isinstance(content, dict) else {}

                title = (
                    content.get("title")
                    or raw.get("title")
                    or ""
                ).strip()

                summary = (
                    content.get("summary")
                    or content.get("description")
                    or raw.get("summary")
                    or ""
                ).strip()

                provider = content.get("provider")
                if isinstance(provider, dict):
                    provider = (
                        provider.get("displayName")
                        or provider.get("name")
                    )

                source = (
                    provider
                    or raw.get("publisher")
                    or "Yahoo Finance"
                )

                published_at = (
                    content.get("pubDate")
                    or raw.get("pubDate")
                )

                # 相容 yfinance 舊式 Unix timestamp。
                if not published_at and raw.get("providerPublishTime"):
                    try:
                        published_at = pd.to_datetime(
                            raw.get("providerPublishTime"),
                            unit="s",
                            utc=True
                        ).isoformat()
                    except Exception:
                        published_at = None

                url = ""
                canonical = content.get("canonicalUrl")
                clickthrough = content.get("clickThroughUrl")

                if isinstance(canonical, dict):
                    url = canonical.get("url", "")
                elif isinstance(canonical, str):
                    url = canonical

                if not url:
                    if isinstance(clickthrough, dict):
                        url = clickthrough.get("url", "")
                    elif isinstance(clickthrough, str):
                        url = clickthrough

                if not url:
                    url = raw.get("link", "") or raw.get("url", "")

                if not title or not url or not published_at:
                    continue

                published_ts = pd.to_datetime(
                    published_at,
                    errors="coerce",
                    utc=True
                )
                if pd.isna(published_ts):
                    continue

                news.append({
                    "date": published_ts.tz_convert(
                        "Asia/Taipei"
                    ).strftime("%Y-%m-%d"),
                    "published_at": published_ts.isoformat(),
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "source": str(source).strip() or "Yahoo Finance"
                })

            except Exception:
                continue

        return news


    # ========================================================
    # 1. 合併新聞來源
    # ========================================================

    news_list = []

    try:
        blocktempo_news = fetch_blocktempo()
        news_list.extend(blocktempo_news)
    except requests.exceptions.RequestException as e:
        print(
            f"BlockTempo 新聞取得失敗：{e}"
        )
        blocktempo_news = []

    try:
        abmedia_news = fetch_abmedia()
        news_list.extend(abmedia_news)
    except requests.exceptions.RequestException as e:
        print(
            f"ABMedia 新聞取得失敗：{e}"
        )
        abmedia_news = []

    # Daily 才加入 Yahoo Finance；Rich Menu「最新消息」維持原本來源。
    if start_time is not None or end_time is not None:
        yahoo_news = fetch_yahoo_finance()
        news_list.extend(yahoo_news)

    if not news_list:
        return []


    # ========================================================
    # 2. 整理 DataFrame
    # ========================================================

    news_df = pd.DataFrame(
        news_list
    )

    news_df["date"] = pd.to_datetime(
        news_df["date"],
        errors="coerce"
    )

    # published_at 僅供需要「精確時間窗」的 Daily Push 使用。
    # 沒有可信 timestamp 的新聞不會被硬塞進 rolling 24h。
    if "published_at" not in news_df.columns:
        news_df["published_at"] = None

    news_df["published_at"] = pd.to_datetime(
        news_df["published_at"],
        errors="coerce",
        utc=True
    )

    news_df = news_df.dropna(
        subset=["date", "title", "url"]
    ).copy()

    if start_time is not None or end_time is not None:
        strict_news = news_df.dropna(
            subset=["published_at"]
        ).copy()

        if start_time is not None:
            start_ts = pd.Timestamp(start_time)
            if start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("Asia/Taipei")
            start_ts = start_ts.tz_convert("UTC")
            strict_news = strict_news[
                strict_news["published_at"] >= start_ts
            ]

        if end_time is not None:
            end_ts = pd.Timestamp(end_time)
            if end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("Asia/Taipei")
            end_ts = end_ts.tz_convert("UTC")
            strict_news = strict_news[
                strict_news["published_at"] <= end_ts
            ]

        news_df = strict_news


    # ========================================================
    # 3. 去除重複新聞
    # ========================================================

    # 先依 URL 去重
    news_df = news_df.drop_duplicates(
        subset=["url"]
    )

    # 再依標題去重，避免兩來源轉載相同標題
    news_df["title_key"] = (
        news_df["title"]
        .str.lower()
        .str.replace(
            r"\s+",
            "",
            regex=True
        )
        .str.replace(
            r"[^\w\u4e00-\u9fff]",
            "",
            regex=True
        )
    )

    news_df = news_df.drop_duplicates(
        subset=["title_key"]
    )


    # ========================================================
    # 4. 建立兩層相關性
    #    A：Bitcoin 直接相關
    #    B：泛 Crypto 相關，僅在 A 不足時補入
    # ========================================================

    direct_btc_keywords = [
        "bitcoin",
        "btc",
        "比特幣",
        "比特币"
    ]

    crypto_keywords = [
        "crypto",
        "cryptocurrency",
        "加密貨幣",
        "加密货币",
        "虛擬貨幣",
        "虚拟货币",
        "區塊鏈",
        "区块链",
        "數位資產",
        "数字资产",
        "交易所",
        "stablecoin",
        "穩定幣",
        "稳定币"
    ]

    search_text = (
        news_df["title"].fillna("")
        + " "
        + news_df["summary"].fillna("")
    ).str.lower()

    direct_pattern = "|".join(
        map(
            re.escape,
            direct_btc_keywords
        )
    )

    crypto_pattern = "|".join(
        map(
            re.escape,
            crypto_keywords
        )
    )

    news_df["btc_direct"] = (
        search_text.str.contains(
            direct_pattern,
            regex=True,
            na=False
        )
    )

    news_df["crypto_related"] = (
        search_text.str.contains(
            crypto_pattern,
            regex=True,
            na=False
        )
    )

    relevant_news = news_df[
        news_df["btc_direct"]
        | news_df["crypto_related"]
    ].copy()

    if relevant_news.empty:
        return []


    # ========================================================
    # 5. 篩選近期新聞
    # ========================================================

    # 一般 Pull「最新消息」維持原本近期新聞邏輯；
    # Daily 若已指定 start_time/end_time，前面已完成精確 rolling window。
    if start_time is None and end_time is None:
        latest_date = relevant_news[
            "date"
        ].max()

        start_date = (
            latest_date
            - pd.Timedelta(days=days)
        )

        relevant_news = relevant_news[
            relevant_news["date"] >= start_date
        ].copy()


    # ========================================================
    # 6. Bitcoin 直接相關優先
    # ========================================================

    direct_news = (
        relevant_news[
            relevant_news["btc_direct"]
        ]
        .sort_values(
            "date",
            ascending=False
        )
    )

    supplemental_news = (
        relevant_news[
            ~relevant_news["btc_direct"]
            & relevant_news["crypto_related"]
        ]
        .sort_values(
            "date",
            ascending=False
        )
    )

    selected = direct_news.head(
        max_news
    ).copy()

    remaining = (
        max_news - len(selected)
    )

    if remaining > 0:

        selected = pd.concat(
            [
                selected,
                supplemental_news.head(
                    remaining
                )
            ],
            ignore_index=True
        )


    # ========================================================
    # 7. 整理成 Agent / Gemini 使用格式
    # ========================================================

    result = []

    for _, row in selected.iterrows():

        result.append({
            "date":
                row["date"].strftime(
                    "%Y-%m-%d"
                ),
            "title":
                row["title"],
            "summary":
                row["summary"],
            "url":
                row["url"],
            "source":
                row["source"],
            "relevance":
                (
                    "BTC直接相關"
                    if row["btc_direct"]
                    else "Crypto補充"
                )
        })

    return result


# ============================================================
# Skill 4：Bitcoin MA20 / MA60 策略歷史回測
# ============================================================

def get_btc_backtest(transaction_cost=0.001, years=5, start_date=None, end_date=None):
    """回測 BTC MA20/MA60 策略。

    資料來源規則：
    1. 若完整回測區間（含 MA60 暖機資料）可由 Binance BTC/USDT 涵蓋，
       整段使用 Binance BTC/USDT 日 K。
    2. 若回測區間早於 Binance 可完整涵蓋的範圍，
       整段改用 CoinGecko BTC/USD，不混接兩種資料。
    3. 未指定日期時，預設回測近 years 年（預設 5 年）。
    """
    from io import StringIO

    BINANCE_START_DATE = pd.Timestamp("2017-08-17")
    WARMUP_DAYS = 90  # 足夠 MA60 暖機

    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()

    # --------------------------------------------------------
    # 1. 決定正式回測區間
    # --------------------------------------------------------
    if start_date is not None or end_date is not None:
        if not start_date or not end_date:
            raise ValueError("自訂回測期間必須同時提供開始日期與結束日期")

        requested_start = pd.Timestamp(start_date).normalize()
        requested_end = pd.Timestamp(end_date).normalize()

        if requested_start > requested_end:
            raise ValueError("回測開始日期不可晚於結束日期")
        if requested_end > today:
            requested_end = today

        backtest_years = round((requested_end - requested_start).days / 365.25, 2)
        custom_range = True
    else:
        if not isinstance(years, int) or years < 1:
            raise ValueError("回測年數必須為 1 年以上的整數")

        requested_end = today
        requested_start = requested_end - pd.DateOffset(years=years)
        backtest_years = years
        custom_range = False

    # MA60 需要正式回測開始日前的暖機資料。
    fetch_start = requested_start - pd.Timedelta(days=WARMUP_DAYS)

    # 只有連暖機區間都能被 Binance 涵蓋時，才整段使用 Binance。
    use_binance = fetch_start >= BINANCE_START_DATE

    # --------------------------------------------------------
    # 2. 取得資料：Binance 主來源；早期歷史改用 CoinGecko
    # --------------------------------------------------------
    if use_binance:
        data_source = "Binance"
        market = "BTC/USDT"

        url = "https://data-api.binance.vision/api/v3/klines"
        all_rows = []
        current_start = fetch_start

        while current_start <= requested_end:
            params = {
                "symbol": "BTCUSDT",
                "interval": "1d",
                "startTime": int(current_start.timestamp() * 1000),
                "endTime": int((requested_end + pd.Timedelta(days=1)).timestamp() * 1000 - 1),
                "limit": 1000,
            }

            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            rows = response.json()

            if not rows:
                break

            all_rows.extend(rows)

            last_open_time = pd.to_datetime(rows[-1][0], unit="ms")
            next_start = last_open_time + pd.Timedelta(days=1)

            if next_start <= current_start:
                break

            current_start = next_start

            if len(rows) < 1000:
                break

        if not all_rows:
            raise RuntimeError("Binance 回測資料取得失敗")

        df = pd.DataFrame(
            all_rows,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base", "taker_buy_quote", "ignore"
            ]
        )

        df["event_date"] = pd.to_datetime(df["open_time"], unit="ms").dt.normalize()
        df["close_price_usd"] = pd.to_numeric(df["close"], errors="coerce")
        df = (
            df[["event_date", "close_price_usd"]]
            .dropna(subset=["event_date", "close_price_usd"])
            .drop_duplicates(subset=["event_date"], keep="last")
            .sort_values("event_date")
            .reset_index(drop=True)
        )

    else:
        data_source = "CoinGecko"
        market = "BTC/USD"

        url = "https://www.coingecko.com/price_charts/export/bitcoin/usd.csv"
        headers = {"User-Agent": "Mozilla/5.0"}

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        df = pd.read_csv(StringIO(response.text))

        df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
        df["close_price_usd"] = pd.to_numeric(df["close_price_usd"], errors="coerce")
        df = (
            df[["event_date", "close_price_usd"]]
            .dropna(subset=["event_date", "close_price_usd"])
            .drop_duplicates(subset=["event_date"], keep="last")
            .sort_values("event_date")
            .reset_index(drop=True)
        )

    # 只保留需要的資料範圍，避免不必要的歷史資料影響閱讀與除錯。
    df = df[
        (df["event_date"] >= fetch_start)
        & (df["event_date"] <= requested_end)
    ].copy().reset_index(drop=True)

    if len(df) < 60:
        raise RuntimeError("回測資料不足，無法計算 MA60")

    # --------------------------------------------------------
    # 3. 計算 MA20 / MA60 與持倉訊號
    # --------------------------------------------------------
    df["MA20"] = df["close_price_usd"].rolling(20).mean()
    df["MA60"] = df["close_price_usd"].rolling(60).mean()
    df["position"] = (df["MA20"] > df["MA60"]).astype(int)

    # --------------------------------------------------------
    # 4. 切出正式回測區間
    # --------------------------------------------------------
    backtest = df[
        (df["event_date"] >= requested_start)
        & (df["event_date"] <= requested_end)
        & df["MA20"].notna()
        & df["MA60"].notna()
    ].copy().reset_index(drop=True)

    if len(backtest) < 2:
        raise RuntimeError("指定回測期間資料不足")

    # --------------------------------------------------------
    # 5. 在正式回測區間內重新計算報酬與策略報酬
    # --------------------------------------------------------
    backtest["daily_return"] = backtest["close_price_usd"].pct_change(fill_method=None)

    # 使用前一天已知的持倉訊號，避免 look-ahead bias。
    # 回測第一天不假設在區間開始前已經持有，因此第一天策略報酬設為 0。
    backtest["strategy_return"] = (
        backtest["position"].shift(1) * backtest["daily_return"]
    ).fillna(0.0)

    # --------------------------------------------------------
    # 6. 計算持倉切換／交易訊號與交易成本
    # --------------------------------------------------------
    backtest["trade"] = backtest["position"].diff().abs().fillna(0.0)

    if int(backtest.loc[0, "position"]) == 1:
        backtest.loc[0, "trade"] = 1.0

    trade_signal_count = int(backtest["trade"].sum())

    backtest["strategy_return_cost"] = (
        (1 + backtest["strategy_return"])
        * ((1 - transaction_cost) ** backtest["trade"])
        - 1
    )

    # --------------------------------------------------------
    # 7. 建立累積資產曲線
    # --------------------------------------------------------
    backtest["buy_hold_growth"] = (
        1 + backtest["daily_return"].fillna(0.0)
    ).cumprod()

    backtest["strategy_growth"] = (
        1 + backtest["strategy_return"]
    ).cumprod()

    backtest["strategy_cost_growth"] = (
        1 + backtest["strategy_return_cost"]
    ).cumprod()

    # --------------------------------------------------------
    # 8. 最大回撤 / Sharpe Ratio（原本邏輯保留）
    # --------------------------------------------------------
    def max_drawdown(growth):
        running_max = growth.cummax()
        drawdown = growth / running_max - 1
        return drawdown.min()

    def sharpe_ratio(returns):
        returns = returns.dropna()
        if len(returns) == 0 or returns.std() == 0:
            return 0.0
        # 本專題採簡化 Sharpe Ratio：假設無風險利率 Rf = 0。
        # BTC 為 24/7 市場，因此以 sqrt(365) 年化。
        # 使用樣本標準差（ddof=1）。
        return returns.mean() / returns.std(ddof=1) * (365 ** 0.5)

    buy_hold_returns = backtest["daily_return"].fillna(0.0)
    strategy_returns = backtest["strategy_return"]
    strategy_cost_returns = backtest["strategy_return_cost"]

    buy_hold_return = backtest["buy_hold_growth"].iloc[-1] - 1
    strategy_return = backtest["strategy_growth"].iloc[-1] - 1
    strategy_cost_return = backtest["strategy_cost_growth"].iloc[-1] - 1

    buy_hold_volatility = buy_hold_returns.std(ddof=1) * (365 ** 0.5)
    strategy_volatility = strategy_returns.std(ddof=1) * (365 ** 0.5)
    strategy_cost_volatility = strategy_cost_returns.std(ddof=1) * (365 ** 0.5)

    # --------------------------------------------------------
    # 9. 回傳 Agent 需要的結果
    # --------------------------------------------------------
    result = {
        "start_date": backtest["event_date"].iloc[0].strftime("%Y-%m-%d"),
        "end_date": backtest["event_date"].iloc[-1].strftime("%Y-%m-%d"),
        "requested_start_date": requested_start.strftime("%Y-%m-%d"),
        "requested_end_date": requested_end.strftime("%Y-%m-%d"),
        "data_range_note": (
            (
                f"使用者指定起點為 {requested_start.strftime('%Y-%m-%d')}，"
                f"但實際可計算策略的起點為 "
                f"{backtest['event_date'].iloc[0].strftime('%Y-%m-%d')}。"
                f"這是因為資料可用範圍與 MA60 計算所需歷史資料限制，"
                f"因此本次回測實際期間為 "
                f"{backtest['event_date'].iloc[0].strftime('%Y-%m-%d')} 至 "
                f"{backtest['event_date'].iloc[-1].strftime('%Y-%m-%d')}。"
            )
            if backtest["event_date"].iloc[0] > requested_start
            else None
        ),
        "backtest_years": backtest_years,
        "custom_range": custom_range,
        "strategy": "MA20/MA60",
        "data_source": data_source,
        "market": market,
        "data_source_note": (
            "完整回測區間可由 Binance BTC/USDT 涵蓋，因此使用 Binance。"
            if data_source == "Binance"
            else "回測區間早於 Binance 可完整涵蓋的歷史，因此整段使用 CoinGecko BTC/USD。"
        ),
        "transaction_cost": transaction_cost,
        "transaction_cost_note": "每次買入或賣出持倉切換各扣一次成本，採乘法複利處理",
        "sharpe_note": "Sharpe Ratio 假設無風險利率 Rf=0，使用日報酬與 sqrt(365) 年化",
        "trade_count": trade_signal_count,
        "trade_count_label": "交易訊號（買／賣持倉切換）",
        "trade_count_note": "買進或賣出各計 1 次，不代表完整進出場組數",
        "buy_hold": {
            "cumulative_return": float(buy_hold_return),
            "annualized_volatility": float(buy_hold_volatility),
            "max_drawdown": float(max_drawdown(backtest["buy_hold_growth"])),
            "sharpe_ratio": float(sharpe_ratio(buy_hold_returns)),
        },
        "ma_strategy": {
            "cumulative_return": float(strategy_return),
            "annualized_volatility": float(strategy_volatility),
            "max_drawdown": float(max_drawdown(backtest["strategy_growth"])),
            "sharpe_ratio": float(sharpe_ratio(strategy_returns)),
        },
        "ma_strategy_after_cost": {
            "cumulative_return": float(strategy_cost_return),
            "annualized_volatility": float(strategy_cost_volatility),
            "max_drawdown": float(max_drawdown(backtest["strategy_cost_growth"])),
            "sharpe_ratio": float(sharpe_ratio(strategy_cost_returns)),
        },
    }

    return result

# ============================================================
# Skill 5：Bitcoin 跨市場關聯分析
# ============================================================

def get_cross_market(start="2025-01-01", rolling_window=30):

    # --------------------------------------------------------
    # 1. 設定市場商品
    # --------------------------------------------------------

    symbols = {
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "SP500": "^GSPC",
        "NASDAQ": "^IXIC",
        "Gold": "GC=F"
    }


    # --------------------------------------------------------
    # 2. 下載收盤價
    # --------------------------------------------------------

    close_data = pd.DataFrame()

    for name, symbol in symbols.items():

        data = yf.download(
            symbol,
            start=start,
            auto_adjust=False,
            progress=False
        )

        if data.empty:
            continue

        close_data[name] = data["Close"].squeeze()


    # --------------------------------------------------------
    # 3. 保留各市場都有資料的共同日期
    # --------------------------------------------------------

    common_close = close_data.dropna().copy()


    # --------------------------------------------------------
    # 4. 計算每日報酬率
    # --------------------------------------------------------

    returns = (
        common_close
        .pct_change(fill_method=None)
        .dropna()
    )


    # --------------------------------------------------------
    # 5. 整體相關係數
    # --------------------------------------------------------

    correlation = returns.corr()

    btc_correlation = {
        market: float(
            correlation.loc["BTC", market]
        )
        for market in [
            "ETH",
            "SP500",
            "NASDAQ",
            "Gold"
        ]
    }


    # --------------------------------------------------------
    # 6. 計算 BTC 與其他市場的滾動相關
    # --------------------------------------------------------

    rolling_corr = {}

    for market in [
        "ETH",
        "SP500",
        "NASDAQ",
        "Gold"
    ]:

        corr_series = (
            returns["BTC"]
            .rolling(rolling_window)
            .corr(returns[market])
            .dropna()
        )

        if len(corr_series) > 0:

            rolling_corr[market] = float(
                corr_series.iloc[-1]
            )

        else:

            rolling_corr[market] = None


    # --------------------------------------------------------
    # 7. 計算近期各市場表現
    #    以最近 7 個「共同交易觀察值」比較，不稱為 7 個日曆天。
    #    7 個報酬區間需要 8 筆共同收盤價。
    # --------------------------------------------------------

    performance_window = 7
    recent_performance = {}

    if len(common_close) >= performance_window + 1:
        recent_prices = common_close.tail(performance_window + 1)

        for market in [
            "BTC",
            "ETH",
            "SP500",
            "NASDAQ",
            "Gold"
        ]:
            start_price = float(recent_prices[market].iloc[0])
            end_price = float(recent_prices[market].iloc[-1])

            recent_performance[market] = (
                (end_price / start_price - 1) * 100
            )
    else:
        recent_performance = {
            market: None
            for market in [
                "BTC",
                "ETH",
                "SP500",
                "NASDAQ",
                "Gold"
            ]
        }


    # --------------------------------------------------------
    # 8. 取得最近共同交易日期
    # --------------------------------------------------------

    latest_date = (
        common_close
        .index[-1]
        .strftime("%Y-%m-%d")
    )


    # --------------------------------------------------------
    # 9. 整理 Skill 回傳結果
    # --------------------------------------------------------

    result = {

        "start_date":
            common_close.index[0]
            .strftime("%Y-%m-%d"),

        "latest_common_date":
            latest_date,

        "recent_performance_window":
            performance_window,

        "recent_performance_window_note":
            "共同交易觀察值，非日曆天數",

        "recent_performance_pct":
            recent_performance,

        "rolling_window":
            rolling_window,

        "rolling_window_note":
            "共同交易觀察值，非日曆天數",

        "overall_correlation_with_btc":
            btc_correlation,

        "latest_rolling_correlation_with_btc":
            rolling_corr
    }


    return result


    
# ============================================================
# Gemini：跨市場 AI 解讀
# ============================================================

def generate_cross_market_ai_insight(cross_market_data):
    """只根據 Python 已計算的跨市場數值產生簡短自然語言解讀。"""

    prompt = f"""
你是 BTC AI 投資資訊助手。

以下資料已由 Python 計算完成：
{json.dumps(cross_market_data, ensure_ascii=False, indent=2)}

請只根據這些數值，使用繁體中文產生 2～3 句「AI 解讀」。

規則：
1. 不要重新計算或修改任何數字。
2. 只能引用 LINE 畫面會顯示的兩組資料：
   - recent_performance_pct：近期各市場漲跌幅
   - latest_rolling_correlation_with_btc：BTC 與其他市場的近期滾動相關係數
3. 禁止引用 overall_correlation_with_btc，即使輸入資料中存在該欄位，也不要在解讀中提到它的數值或稱為長期相關性。
4. 可以比較近期市場漲跌方向，以及 BTC 與 ETH、S&P 500、NASDAQ、黃金的近期連動程度。
5. recent_performance_window 與 rolling_window 都是「共同交易觀察值」，不是日曆天數。
6. 不得把相關性描述成因果關係。
7. 不得因 BTC 與黃金正相關就宣稱 BTC 是避險資產。
8. 不提供買進、賣出、做多、做空或價格預測。
9. 不要使用 Markdown 標記。
10. 不要加標題，只輸出解讀文字。
11. 不得使用「攀升、下降、增加、減少、轉強、轉弱、顯著變化」等暗示與前一期比較的詞，
    因為目前只提供當期 rolling correlation，沒有前一期比較資料。
12. 描述相關性時使用「近期相關係數為／約為」或「近期連動程度較高／較低」。
13. 控制在約 90～140 個中文字內。
"""

    return _call_gemini_text(prompt)


# ============================================================
# Gemini：新聞重要性篩選 + 文本情緒 + AI 解讀
# ============================================================

def analyze_btc_news_with_ai(news_items, top_n=3, daily_mode=False):
    """從既有新聞候選中選出重點 Top N，保留原始 URL 並分析文本情緒。

    重要性不是瀏覽次數；目前新聞來源沒有一致可比較的 views 欄位。
    排序依據為 BTC 直接相關性、事件重要程度、時效性、事件去重與來源多樣性。
    """

    if not news_items:
        return {
            "selected_news": [],
            "overall_sentiment": "無資料",
            "ai_insight": "目前沒有足夠的近期 BTC 新聞可供分析。"
        }

    # 僅把爬蟲實際取得的欄位交給 Gemini；URL 最後仍由 Python 原始資料映射，
    # 避免 LLM 自行生成網址。
    candidates = []
    for idx, item in enumerate(news_items, start=1):
        candidates.append({
            "candidate_id": idx,
            "date": item.get("date"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "source": item.get("source"),
            "relevance": item.get("relevance"),
        })

    prompt = f"""
你是 BTC AI 投資資訊助手。

以下是程式實際取得的近期新聞候選：
{json.dumps(candidates, ensure_ascii=False, indent=2)}

請選出最多 {top_n} 則最值得 BTC 使用者優先閱讀的新聞，並分析「新聞文本情緒」。

重要性排序原則：
1. BTC 直接相關性優先。
2. 事件本身的重要程度：重大政策／監管、Bitcoin ETF 或大型機構動態、
   BTC 顯著市場事件、重大交易所／資安事件、重要總體金融事件優先。
3. 時效性。
4. 避免選到同一事件的重複報導。
5. 前述條件接近時，再考慮來源多樣性。
6. 不要因為標題煽動就提高重要性。
7. 目前資料沒有跨網站一致可比較的瀏覽次數，因此不得聲稱依 views／點閱數排名。
8. 若 daily_mode = true，這是每日主動推播：只有真正具有 BTC／加密市場資訊價值的事件才可入選。
   公司週年、品牌活動、一般合作、普通產品宣傳、例行高層公開信等低重要性內容不要為了湊數入選。
   可以只選 0～{top_n} 則，不必湊滿 {top_n} 則。

daily_mode = {daily_mode}

情緒定義：
- 正向：該新聞文字對 BTC／加密市場主要呈現較正面的市場敘事。
- 中性：資訊性、正負因素並存，或方向不明確。
- 負向：該新聞文字主要呈現較負面的市場敘事。
情緒只是「新聞文本語氣／敘事傾向」，不是未來漲跌預測。

overall_sentiment 規則：
- overall_sentiment 必須只根據 selected 入選新聞的 title 與 summary 綜合判斷，不得把未入選候選新聞納入整體情緒。
- 若 selected 為空，overall_sentiment 必須回傳「無資料」。
- overall_sentiment 描述的是「入選重要新聞的整體文本傾向」，不是整體市場多空，也不是未來價格預測。

情緒判斷補充規則：
- 不得只因 BTC 價格上漲就直接判定為正向，也不得只因價格下跌就直接判定為負向。
- 若同一則新聞同時包含明顯正面與負面／風險訊號，例如價格大漲但伴隨大規模爆倉、清算、政策風險或其他重大不確定性，優先判為「中性」，並在 reason 簡短說明正負因素並存。
- 情緒必須依 title 與 summary 的整體敘事判斷，不可只看標題中的單一漲跌字眼。

請只回傳合法 JSON，不要 Markdown code fence：
{{
  "selected": [
    {{
      "candidate_id": 1,
      "display_title_zh": "若原標題為英文，翻成自然精簡的繁體中文；若原標題已是中文，保持原意即可",
      "sentiment": "正向",
      "reason": "一句很短的分類理由"
    }}
  ],
  "overall_sentiment": "偏正向",
  "ai_insight": "2～3句、約100～160字的整體新聞解讀"
}}

規則：
- selected 最多 {top_n} 則；Daily 不必湊滿三則，寧可少選也不要納入低重要性內容。
- candidate_id 必須來自候選資料，不得捏造。
- display_title_zh 只用於 LINE 顯示：英文標題翻成自然、精簡的繁體中文；中文標題維持原意。
- display_title_zh 不得加入原標題沒有的事實、數字、人物或判斷。
- 不得補造新聞內容、價格、政策、人物說法或因果關係。
- ai_insight 只能概括入選新聞共同呈現的主題與文本傾向。
- ai_insight 不得把新聞內容直接寫成 BTC 價格變動的原因。
- 除非入選新聞本身明確提供相應證據，否則不得使用「資金流入／流出」「資金動能」「買盤力道」「賣壓」「籌碼」「市場韌性」「市場信心」「上升／下降通道」等延伸判斷。
- 若提及機構或監管觀點，必須表述為「新聞／報導／受訪者的觀點」，不得改寫成系統自己的事實判斷。
- 不提供投資建議或價格預測。
"""

    raw = _call_gemini_text(prompt)
    parsed = _parse_json_text(raw)

    if not isinstance(parsed, dict):
        # Pull 保留原本容錯；Daily 不在 AI 篩選失敗時盲目推送候選新聞。
        selected_raw = [] if daily_mode else news_items[:top_n]
        return {
            "selected_news": [
                {
                    **item,
                    "sentiment": "未分類",
                    "sentiment_reason": ""
                }
                for item in selected_raw
            ],
            "overall_sentiment": "未分類",
            "ai_insight": (
                "AI 新聞篩選暫時無法取得。"
                if daily_mode
                else "AI 新聞解讀暫時無法取得，以下仍提供近期 BTC 相關新聞原文。"
            )
        }

    selected_news = []
    seen_ids = set()

    for selected in parsed.get("selected", []):
        try:
            candidate_id = int(selected.get("candidate_id"))
        except (TypeError, ValueError):
            continue

        if (
            candidate_id < 1
            or candidate_id > len(news_items)
            or candidate_id in seen_ids
        ):
            continue

        seen_ids.add(candidate_id)
        original = dict(news_items[candidate_id - 1])

        sentiment = selected.get("sentiment", "未分類")
        if sentiment not in {"正向", "中性", "負向"}:
            sentiment = "未分類"

        original["sentiment"] = sentiment
        original["sentiment_reason"] = str(
            selected.get("reason", "")
        ).strip()

        display_title_zh = str(
            selected.get("display_title_zh", "")
        ).strip()
        if daily_mode and display_title_zh:
            original["display_title"] = display_title_zh
        else:
            original["display_title"] = original.get("title", "")

        selected_news.append(original)

        if len(selected_news) >= top_n:
            break

    # Pull 維持原本補足邏輯；Daily 不為了湊 Top 3 塞入低重要性新聞。
    if (
        not daily_mode
        and len(selected_news) < min(top_n, len(news_items))
    ):
        for idx, item in enumerate(news_items, start=1):
            if idx in seen_ids:
                continue
            original = dict(item)
            original["sentiment"] = "未分類"
            original["sentiment_reason"] = ""
            selected_news.append(original)
            if len(selected_news) >= min(top_n, len(news_items)):
                break

    return {
        "selected_news": selected_news,
        "overall_sentiment": str(
            parsed.get("overall_sentiment", "未分類")
        ).strip(),
        "ai_insight": str(
            parsed.get("ai_insight", "")
        ).strip()
    }


def _parse_json_text(text):
    if not text:
        return None

    cleaned = text.strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _call_gemini_text(prompt):
    """共用 Gemini 文字呼叫；失敗時回傳空字串。"""

    data = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        ]
    }

    for attempt in range(2):
        try:
            response = requests.post(
                GEMINI_URL,
                params={"key": API_KEY},
                json=data,
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                return (
                    result["candidates"][0]
                    ["content"]["parts"][0]["text"]
                ).strip()

            if response.status_code == 503:
                time.sleep(3)
                continue

            return ""

        except requests.exceptions.RequestException:
            time.sleep(3)

    return ""


# ============================================================
# Gemini Agent 設定
# ============================================================

import json

# 先使用你自己的 Gemini API Key
# 不要把 API Key 分享給別人或上傳到 GitHub
#API_KEY = "你的API_KEY"

MODEL = "gemini-3.1-flash-lite"

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{MODEL}:generateContent"
)
API_KEY = os.getenv("GEMINI_API_KEY")

# ============================================================
# Gemini Agent 可以使用的 Skills
# ============================================================

SKILL_DESCRIPTIONS = """
你是一個專門處理 Bitcoin（BTC）市場資訊的 AI Agent。

你可以使用以下 6 個 Skills：

1. get_btc_market
   取得 Bitcoin 即時價格、24 小時漲跌、
   最高價、最低價、成交量等市場資訊。
   注意：這個 Skill 的漲跌幅只代表「近 24 小時」，
   不能代表近一週、近一個月或更長期間。

2. get_btc_period_change
   取得使用者指定期間的價格變化，支援整數小時、天、週、月、年，以及指定起訖日期。
   例如 3 小時、12 小時、60 天、一週、3 個月、1 年，或 2025/1/1 到 2025/12/31。
   當使用者明確詢問「近 X 小時」、「近 X 天／週／月／年」或指定起訖日期
   的漲跌、價格變化或期間表現時使用。

3. get_btc_technical
   取得 Bitcoin 日線技術指標，
   包含 MA5、MA20、MA60、RSI14、MACD。

4. get_btc_news
   取得近期 Bitcoin / Crypto 相關新聞。

5. get_btc_backtest
   取得 MA20/MA60 策略歷史回測結果，
   並與 Buy & Hold 比較。

6. get_cross_market
   分析 Bitcoin 與 ETH、S&P 500、
   NASDAQ、黃金之間的跨市場資訊，
   包含近期各市場表現與 BTC 的市場相關性。

請根據使用者真正想了解的內容選擇 Skill，
使用者不需要說出 Skill 名稱或固定關鍵字。

例如：
「現在一顆多少」→ get_btc_market

「近 3 小時漲多少」→ get_btc_period_change

「12 小時 BTC 變化如何」→ get_btc_period_change

「近三天的變化如何」→ get_btc_period_change

「這禮拜 BTC 表現如何」→ get_btc_period_change

「近 3 個月 BTC 變化」→ get_btc_period_change

「近一年 BTC 漲多少」→ get_btc_period_change

「2025/1/1 到 2025/12/31 BTC 漲多少」→ get_btc_period_change

「現在看起來偏強還是偏弱」
→ get_btc_market + get_btc_technical

「最近 BTC 怎麼了」
→ get_btc_market + get_btc_news

「現在有什麼值得注意」
→ get_btc_market + get_btc_technical + get_btc_news

「最近跟美股有一起動嗎」
→ get_cross_market

「以前照均線操作會怎樣」
→ get_btc_backtest

重要規則：

1. 不要因為使用者沒有使用固定關鍵字就拒絕回答，
   請理解自然語言的真正意圖。

2. 如果問題中的「最近」沒有明確指定期間，
   可以選擇相關 Skill，
   但後續回答只能依 Skill 實際提供的時間範圍說明，
   不可以自行假設「最近」等於 24 小時、7 天或 30 天。

3. 如果使用者詢問 BTC 以外的個別股票、
   ETF、其他加密貨幣等商品，
   而目前 Skills 無法提供該商品的完整分析，
   不要選擇不相關的 Skill。

4. 如果問題與金融或 Bitcoin 完全無關，
   不要選擇任何 Skill。

5. 不要使用與問題無關的 Skill。
"""

# ============================================================
# Gemini Agent：根據問題選擇 Skills
# ============================================================

def choose_skills(user_question):

    prompt = f"""
{SKILL_DESCRIPTIONS}

使用者問題：
{user_question}

你的任務不只是選擇 Skill，
還要判斷使用者的意圖是否足夠明確。

請從以下三種狀態選擇一種：

1. OK
   使用者的問題已經足夠明確，
   而且目前的 Skills 可以處理。
   此時選擇需要的 Skills。

2. CLARIFY
   使用者的問題與金融、Bitcoin 或市場相關，
   但資訊不足，存在多種合理解讀，
   不應自行猜測使用者真正想問什麼。

   例如：
   「ETF呢」
   → 可能是在問 Bitcoin ETF，
     也可能是在問一般 ETF。

   「現在匯率呢」
   → 沒有說明是哪兩種貨幣，
     不可以自行理解成 BTC/USDT。

   此時不要選擇任何 Skill。

3. OUT_OF_SCOPE
   使用者的問題超出目前系統支援範圍。

   例如：
   「2330如何」
   → 目前沒有台股個股分析 Skill。

   「你今天吃啥」
   → 與 Bitcoin / 金融分析無關。

   此時不要選擇任何 Skill。


如果 status = OK，
你只能從以下名稱選擇：

- get_btc_market
- get_btc_period_change
- get_btc_technical
- get_btc_news
- get_btc_backtest
- get_cross_market


重要規則：

- 不要因為自然語言比較口語就判定 CLARIFY。
  例如「現在一顆多少」已經足以理解是在問 BTC 價格，
  應該判定 OK。

- 「最近 BTC 怎麼了」雖然時間沒有精確指定，
  仍可以使用現有市場與新聞資料回答，
  應判定 OK。
  回答階段再說明資料實際涵蓋期間。

- 只有當問題存在多種合理解讀，
  而且不同解讀需要不同資料時，
  才使用 CLARIFY。

請只回傳 JSON，不要加入其他文字。

格式：

{{
    "status": "OK",
    "skills": ["get_btc_market"]
}}

或：

{{
    "status": "CLARIFY",
    "skills": []
}}

或：

{{
    "status": "OUT_OF_SCOPE",
    "skills": []
}}
"""

    data = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    for attempt in range(2):

        try:

            response = requests.post(
                GEMINI_URL,
                params={"key": API_KEY},
                json=data,
                timeout=10
            )

            if response.status_code == 200:

                result = response.json()

                return (
                    result["candidates"][0]
                    ["content"]["parts"][0]["text"]
                )

            elif response.status_code == 503:

                print(
                    f"Gemini 選擇 Skill 忙碌中，"
                    f"第 {attempt + 1} 次重試..."
                )

                time.sleep(3)

            else:

                print(
                    f"Gemini API 錯誤 "
                    f"{response.status_code}："
                    f"{response.text}"
                )

                return None

        except requests.exceptions.RequestException as e:

            print(
                f"Gemini 連線錯誤：{e}"
            )

            time.sleep(3)

    return None

SKILL_MAP = {
    "get_btc_market": get_btc_market,
    "get_btc_period_change": get_btc_period_change,
    "get_btc_technical": get_btc_technical,
    "get_btc_news": get_btc_news,
    "get_btc_backtest": get_btc_backtest,
    "get_cross_market": get_cross_market
}

def _chinese_number_to_int(value):
    mapping = {
        "零": 0, "〇": 0,
        "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9
    }

    if value in mapping:
        return mapping[value]

    if "十" in value:
        left, _, right = value.partition("十")
        tens = mapping.get(left, 1) if left else 1
        ones = mapping.get(right, 0) if right else 0
        return tens * 10 + ones

    return None


def _normalize_date_string(value):
    """把 2025/1/1、2025-1-1、2025.1.1 統一成 YYYY-MM-DD。"""
    parts = re.split(r"[-/\\.]", value)
    if len(parts) != 3:
        return None

    try:
        dt = pd.Timestamp(
            year=int(parts[0]),
            month=int(parts[1]),
            day=int(parts[2])
        )
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _extract_requested_period(user_question):
    """解析小時、天、週、月、年，以及 YYYY/MM/DD 起訖日期。"""

    # 自訂起訖日期，例如：
    # 2025/1/1 到 2025/12/31
    date_matches = re.findall(
        r"\d{4}[-/\\.]\d{1,2}[-/\\.]\d{1,2}",
        user_question
    )

    if len(date_matches) >= 2:
        start_date = _normalize_date_string(date_matches[0])
        end_date = _normalize_date_string(date_matches[1])

        if start_date and end_date:
            return {
                "start_date": start_date,
                "end_date": end_date
            }

    # 小數時間先攔截，避免 0.5 小時被誤判成 5 小時。
    decimal_match = re.search(
        r"(\d+\.\d+)\s*(小時|小时|h|hr|hrs|天|日|個月|个月|月|年)",
        user_question,
        flags=re.IGNORECASE
    )
    if decimal_match:
        return {
            "unsupported_decimal": True,
            "value": float(decimal_match.group(1)),
            "unit": decimal_match.group(2)
        }

    # 阿拉伯數字：3小時、60天、3個月、2年
    match = re.search(
        r"(?:近|最近|過去|前)?\s*(\d{1,3})\s*"
        r"(小時|小时|h|hr|hrs|天|日|個月|个月|月|年)",
        user_question,
        flags=re.IGNORECASE
    )

    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()

        if value < 1:
            return None

        if unit in ("年",):
            return {"years": value}
        if unit in ("個月", "个月", "月"):
            return {"months": value}
        if unit in ("天", "日"):
            return {"days": value}

        return {"hours": value}

    # 中文數字：三天、六十天、三個月、兩年
    match = re.search(
        r"(?:近|最近|過去|前)?\s*"
        r"([零〇一二兩两三四五六七八九十]{1,4})\s*"
        r"(小時|小时|天|日|個月|个月|月|年)",
        user_question
    )

    if match:
        value = _chinese_number_to_int(match.group(1))
        if value is None or value < 1:
            return None

        unit = match.group(2)

        if unit == "年":
            return {"years": value}
        if unit in ("個月", "个月", "月"):
            return {"months": value}
        if unit in ("天", "日"):
            return {"days": value}

        return {"hours": value}

    # 週／禮拜換算成天。
    match = re.search(
        r"(?:近|最近|過去|前)?\s*(\d{1,2})\s*(週|周|禮拜|礼拜)",
        user_question
    )
    if match:
        return {"days": int(match.group(1)) * 7}

    if re.search(
        r"(這|本|近|最近|過去)?\s*(一)?\s*(週|周|禮拜|礼拜)",
        user_question
    ):
        return {"days": 7}

    return None


def _extract_requested_hours(user_question):
    """保留舊函式名稱相容性。"""
    period = _extract_requested_period(user_question)
    if isinstance(period, dict) and "hours" in period:
        return period["hours"]
    return None


def _extract_backtest_years(user_question):
    """回測期間：未指定時預設 5 年；支援近 X 年（整數）。"""
    if not re.search(r"(回測|策略)", user_question, flags=re.IGNORECASE):
        return 5

    match = re.search(
        r"(?:近|最近|過去|前)?\s*(\d{1,2})\s*年",
        user_question
    )
    if match:
        years = int(match.group(1))
        return years if years >= 1 else 5

    match = re.search(
        r"(?:近|最近|過去|前)?\s*([一二兩两三四五六七八九十]{1,4})\s*年",
        user_question
    )
    if match:
        years = _chinese_number_to_int(match.group(1))
        return years if years and years >= 1 else 5

    return 5


def run_skills(skill_names, user_question=""):

    results = {}
    requested_period = _extract_requested_period(user_question)

    for skill_name in skill_names:

        if skill_name not in SKILL_MAP:
            continue

        if skill_name == "get_btc_backtest":
            # 回測也支援明確起訖日期，例如 2013/1/1～2020/12/31。
            # 若沒有明確日期，維持原本「未指定=5年、近X年=X年」的邏輯。
            if (
                requested_period
                and requested_period.get("start_date")
                and requested_period.get("end_date")
            ):
                results[skill_name] = get_btc_backtest(
                    start_date=requested_period["start_date"],
                    end_date=requested_period["end_date"]
                )
            else:
                backtest_years = _extract_backtest_years(user_question)
                results[skill_name] = get_btc_backtest(years=backtest_years)
            continue

        if skill_name == "get_btc_period_change":

            if not requested_period:
                continue

            if requested_period.get("unsupported_decimal"):
                results[skill_name] = {
                    "unsupported": True,
                    "reason": (
                        "目前時間區間僅支援整數小時、天、月或年；"
                        "最小小時單位為 1 小時。"
                    )
                }
                continue

            results[skill_name] = get_btc_period_change(
                hours=requested_period.get("hours"),
                days=requested_period.get("days"),
                months=requested_period.get("months"),
                years=requested_period.get("years"),
                start_date=requested_period.get("start_date"),
                end_date=requested_period.get("end_date")
            )

        else:
            results[skill_name] = SKILL_MAP[skill_name]()

    return results


def agent_get_data(user_question):

    skill_answer = choose_skills(user_question)

    if skill_answer is None:
        return None

    # 清理 Gemini 回傳的 JSON
    skill_answer = skill_answer.strip()
    skill_answer = skill_answer.replace("```json", "")
    skill_answer = skill_answer.replace("```", "")
    skill_answer = skill_answer.strip()

    try:
        skill_data = json.loads(skill_answer)

    except json.JSONDecodeError:

        print("Gemini Skill JSON 解析失敗：")
        print(skill_answer)

        return None

    # --------------------------------------------------------
    # 讀取 Agent 判斷結果
    # --------------------------------------------------------

    status = skill_data.get(
        "status",
        "OUT_OF_SCOPE"
    )

    skill_names = skill_data.get(
        "skills",
        []
    )

    # --------------------------------------------------------
    # CLARIFY：問題資訊不足
    # --------------------------------------------------------

    if status == "CLARIFY":

        print("\n=== Agent 判斷 ===")
        print("CLARIFY")

        return {
            "question": user_question,
            "status": "CLARIFY",
            "selected_skills": [],
            "results": {}
        }

    # --------------------------------------------------------
    # OUT_OF_SCOPE：超出目前系統範圍
    # --------------------------------------------------------

    if status == "OUT_OF_SCOPE":

        print("\n=== Agent 判斷 ===")
        print("OUT_OF_SCOPE")

        return {
            "question": user_question,
            "status": "OUT_OF_SCOPE",
            "selected_skills": [],
            "results": {}
        }

    # --------------------------------------------------------
    # OK 但 Gemini 沒有選任何 Skill
    # 防止異常情況
    # --------------------------------------------------------

    if status == "OK" and not skill_names:

        print("\n=== Agent 判斷 ===")
        print("OK，但沒有選擇 Skill")

        return {
            "question": user_question,
            "status": "CLARIFY",
            "selected_skills": [],
            "results": {}
        }

    # --------------------------------------------------------
    # OK：執行 Agent 選擇的 Skills
    # --------------------------------------------------------

    results = run_skills(
        skill_names,
        user_question=user_question
    )

    print("\n=== Agent 判斷 ===")
    print("OK")

    print("\n=== Agent 選擇的 Skills ===")
    print(skill_names)

    print("\n=== Skills 實際取得的資料 ===")
    print(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2
        )
    )

    return {
        "question": user_question,
        "status": "OK",
        "selected_skills": skill_names,
        "results": results
    }

def _is_direct_btc_market_question(user_question):
    """Conservatively detect simple BTC spot-price / market-quote questions.

    These questions do not need Gemini to decide which Skill to call, so Render can
    answer them even when Gemini is temporarily busy.
    """
    text = str(user_question or "").strip().lower()
    if not text:
        return False

    btc_terms = ("btc", "bitcoin", "比特幣", "比特币", "一顆", "一颗")
    market_terms = (
        "多少", "多少錢", "多少钱", "價格", "价格", "行情",
        "現價", "现价", "即時價格", "即时价格", "現在價格", "现在价格"
    )
    analysis_terms = (
        "技術", "技术", "rsi", "macd", "ma5", "ma20", "ma60", "均線", "均线",
        "新聞", "新闻", "回測", "回测", "相關", "相关", "分析", "趨勢", "趋势",
        "近一週", "近一周", "近一個月", "近一个月", "近一年", "最近"
    )

    has_btc = any(term in text for term in btc_terms)
    has_market = any(term in text for term in market_terms)
    has_analysis = any(term in text for term in analysis_terms)

    return has_btc and has_market and not has_analysis


def _generate_market_ai_insight(market):
    """Use Gemini once to interpret already-fetched market data; return empty text on failure."""
    prompt = f"""
你是 BTC AI 投資資訊助手。

以下 BTC 即時市場資料已經由程式取得：
{json.dumps(market, ensure_ascii=False, indent=2)}

請只根據以上資料，用繁體中文產生 2～3 句簡潔的「AI 解讀」。
規則：
1. 不要重新計算、修改或捏造任何數字。
2. 可綜合目前價格、近 1 小時漲跌、近 24 小時漲跌、24 小時高低區間、成交量與成交額，整理目前市場狀態。
3. 不要只是逐項重述數字；請說明短線與 24 小時變動是否一致、目前價格在 24 小時區間的大致位置，以及成交資訊可提供的客觀觀察。
4. 不得自行加入未提供的歷史平均成交量，因此不能宣稱成交量「放大」或「萎縮」。
5. 不得加入多頭、空頭、突破、支撐、壓力、超買、超賣等技術判斷。
6. 不預測未來價格，不提供買進、賣出、做多、做空或其他投資建議。
7. 不要使用 Markdown，不要加標題，只輸出解讀文字。
8. 約 70～120 個中文字。
"""
    return _call_gemini_text(prompt).strip()


def _format_direct_btc_market(market):
    """Format BTC market data first, then prefer Gemini interpretation with a safe fallback."""
    price = market.get("price")
    price_twd = market.get("price_twd_approx")
    change_1h = market.get("change_1h")
    change_24h = market.get("change_24h")
    high_24h = market.get("high_24h")
    low_24h = market.get("low_24h")
    volume_btc = market.get("volume_btc")
    volume_usdt = market.get("volume_usdt")
    trades_24h = market.get("trades_24h")

    def pct(value):
        if value is None:
            return "暫無資料"
        return f"{value:+.2f}%"

    lines = [
        "💰 BTC 即時行情",
        "",
        "💵 即時價格",
        f"{price:,.2f} USDT" if price is not None else "暫無資料",
    ]
    if price_twd is not None:
        lines.append(f"約 NT$ {price_twd:,.0f}")

    lines += [
        "",
        "📈 漲跌表現",
        f"1H｜{pct(change_1h)}",
        f"24H｜{pct(change_24h)}",
        "",
        "📊 24 小時市場",
        f"最高｜{high_24h:,.2f} USDT" if high_24h is not None else "最高｜暫無資料",
        f"最低｜{low_24h:,.2f} USDT" if low_24h is not None else "最低｜暫無資料",
        f"成交量｜{volume_btc:,.2f} BTC" if volume_btc is not None else "成交量｜暫無資料",
        f"成交額｜{volume_usdt:,.2f} USDT" if volume_usdt is not None else "成交額｜暫無資料",
        f"成交筆數｜{trades_24h:,}" if trades_24h is not None else "成交筆數｜暫無資料",
        "",
        "🤖 AI 解讀",
    ]

    observations = []
    if change_1h is not None and change_24h is not None:
        if change_1h > 0 and change_24h > 0:
            observations.append("近 1 小時與近 24 小時價格變化目前皆為正值。")
        elif change_1h < 0 and change_24h < 0:
            observations.append("近 1 小時與近 24 小時價格變化目前皆為負值。")
        else:
            observations.append("近 1 小時與近 24 小時的價格變化方向目前並不一致。")

    if price is not None and high_24h is not None and low_24h is not None and high_24h > low_24h:
        position = (price - low_24h) / (high_24h - low_24h)
        if position >= 2 / 3:
            observations.append("目前價格位於近 24 小時區間相對較高的位置。")
        elif position <= 1 / 3:
            observations.append("目前價格位於近 24 小時區間相對較低的位置。")
        else:
            observations.append("目前價格位於近 24 小時高低區間的中段。")

    if not observations:
        observations.append("目前先提供 Binance BTC/USDT 的即時市場數據供參考。")

    ai_insight = _generate_market_ai_insight(market)
    if ai_insight:
        lines.append(ai_insight)
    else:
        # Gemini is an enhancement layer only. Keep the market feature usable
        # during temporary API failures by falling back to deterministic observations.
        lines.append("".join(observations[:2]))

    lines += ["", "ℹ️ 資料說明"]
    if price_twd is not None:
        lines.append("台幣價格為匯率換算參考；市場資料來自 Binance BTC/USDT。")
    else:
        lines.append("目前台幣匯率換算暫無資料；市場資料來自 Binance BTC/USDT。")

    return "\n".join(lines)



def _is_direct_btc_technical_question(user_question):
    """Detect explicit BTC technical-analysis questions without using Gemini for routing."""
    text = str(user_question or "").strip().lower()
    if not text:
        return False

    technical_terms = (
        "技術面", "技术面", "技術分析", "技术分析", "技術指標", "技术指标",
        "rsi", "macd", "ma5", "ma20", "ma60", "均線", "均线"
    )
    return any(term in text for term in technical_terms)


def _generate_technical_ai_insight(technical):
    """Use Gemini only for interpretation; technical data itself never depends on Gemini."""
    prompt = f"""
你是 BTC AI 投資資訊助手。

以下技術指標已由 Python 計算完成：
{json.dumps(technical, ensure_ascii=False, indent=2)}

請只根據以上資料，用繁體中文產生 2～3 句簡潔的「AI 解讀」。
規則：
1. 不要重新計算、修改或捏造任何數字。
2. 可綜合 MA5、MA20、MA60、RSI14、MACD、Signal、Histogram 解讀目前技術面。
3. 清楚區分指標呈現的訊號；若指標方向不一致，要直接說明訊號分歧，不要硬下單一結論。
4. 不提供買進、賣出、做多、做空建議，也不預測未來價格。
5. 不要使用 Markdown，不要加標題，只輸出解讀文字。
6. 約 80～140 個中文字。
"""
    return _call_gemini_text(prompt).strip()


def _format_direct_btc_technical(technical):
    """Format technical data first, then add Gemini interpretation when available."""
    close = technical.get("close")
    ma5 = technical.get("ma5")
    ma20 = technical.get("ma20")
    ma60 = technical.get("ma60")
    ma_signal = technical.get("ma_signal")
    rsi14 = technical.get("rsi14")
    rsi_signal = technical.get("rsi_signal")
    macd = technical.get("macd")
    signal = technical.get("macd_signal_line")
    histogram = technical.get("macd_histogram")
    macd_signal = technical.get("macd_signal")

    def num(value, digits=2):
        return "暫無資料" if value is None else f"{value:,.{digits}f}"

    ai_insight = _generate_technical_ai_insight(technical)
    if not ai_insight:
        ai_insight = "AI 技術面解讀暫時無法取得；上方技術指標仍為即時取得並由 Python 計算的結果。"

    lines = [
        "📊 BTC 技術面",
        "",
        "💵 最新日線價格",
        f"{num(close)} USDT",
        "",
        "📈 均線",
        f"MA5｜{num(ma5)}",
        f"MA20｜{num(ma20)}",
        f"MA60｜{num(ma60)}",
        f"均線訊號｜{ma_signal or '暫無資料'}",
        "",
        "📉 RSI",
        f"RSI14｜{num(rsi14)}（{rsi_signal or '暫無資料'}）",
        "",
        "📊 MACD",
        f"MACD｜{num(macd)}",
        f"Signal｜{num(signal)}",
        f"Histogram｜{num(histogram)}",
        f"MACD 訊號｜{macd_signal or '暫無資料'}",
        "",
        "🤖 AI 解讀",
        ai_insight,
        "",
        "ℹ️ 資料說明",
        "技術指標依 Binance BTC/USDT 日 K 計算；技術指標僅反映歷史價格資料，不代表未來走勢。",
    ]
    return "\n".join(lines)


def _is_direct_btc_backtest_question(user_question):
    """Detect explicit BTC backtest requests without using Gemini for routing."""
    text = (user_question or "").strip().lower()
    if not text:
        return False
    terms = ("策略回測", "回測", "backtest", "ma20/ma60", "ma20 ma60")
    return any(term in text for term in terms)


def _generate_backtest_ai_insight(backtest):
    """Use Gemini only to interpret already-computed backtest results."""
    prompt = f"""
你是 BTC 投資資訊助手。以下所有數值都已由 Python 完成回測計算：
{json.dumps(backtest, ensure_ascii=False, indent=2)}

請只根據上述資料，以繁體中文寫 2～3 句「AI 解讀」。
要求：
1. 客觀比較 MA20/MA60 策略（以含成本結果為正式比較）與 Buy & Hold 的累積報酬、Sharpe Ratio、最大回撤。
2. 可以提及交易訊號與交易成本造成的差異。
3. 不得修改、補造任何數字，不得推測未提供的市場原因。
4. 不得預測未來價格，不得提供買進、賣出或投資建議。
5. 不要使用 Markdown 標題或條列，只輸出解讀正文。
""".strip()
    return _call_gemini_text(prompt)


def _format_pct(value):
    return f"{float(value) * 100:+,.2f}%"


def _format_backtest_metric_block(label, metrics):
    return [
        label,
        f"累積報酬｜{_format_pct(metrics.get('cumulative_return', 0))}",
        f"Sharpe｜{float(metrics.get('sharpe_ratio', 0)):.2f}",
        f"最大回撤｜{_format_pct(metrics.get('max_drawdown', 0))}",
    ]


def _format_direct_btc_backtest(backtest):
    """Format computed backtest data first; Gemini failure never hides the results."""
    start = backtest.get("start_date", "-")
    end = backtest.get("end_date", "-")
    years = backtest.get("backtest_years")
    custom = bool(backtest.get("custom_range"))
    source = backtest.get("data_source", "-")
    market = backtest.get("market", "-")
    cost = float(backtest.get("transaction_cost", 0))
    trades = backtest.get("trade_count", 0)

    period = f"{start}～{end}"
    if not custom and years:
        period += f"（近 {years} 年）"

    lines = [
        "📈 MA20/MA60 策略回測",
        "",
        f"期間｜{period}",
        f"資料來源｜{source} {market}",
        "",
        "📊 策略績效",
    ]
    lines += _format_backtest_metric_block("MA 策略（含成本）", backtest.get("ma_strategy_after_cost", {}))
    lines += [""]
    lines += _format_backtest_metric_block("Buy & Hold", backtest.get("buy_hold", {}))
    lines += [
        f"交易訊號（買／賣持倉切換）｜{trades}",
        "",
        "💰 交易成本",
        f"每次持倉切換成本｜{cost * 100:.2f}%",
        f"策略累積報酬（未扣成本）｜{_format_pct(backtest.get('ma_strategy', {}).get('cumulative_return', 0))}",
        f"策略累積報酬（含成本）｜{_format_pct(backtest.get('ma_strategy_after_cost', {}).get('cumulative_return', 0))}",
    ]

    note = backtest.get("data_range_note")
    if note:
        lines += ["", "⚠️ 資料範圍", str(note)]

    ai = _generate_backtest_ai_insight(backtest)
    lines += ["", "🤖 AI 解讀"]
    if ai:
        lines.append(ai.strip())
    else:
        lines.append("AI 回測解讀暫時無法取得；以上回測數據仍可正常參考。")

    lines += [
        "",
        "ℹ️ 回測提醒",
        "歷史回測不代表未來績效或投資建議；Sharpe Ratio 採 Rf=0、日報酬與 sqrt(365) 年化。",
    ]
    return "\n".join(lines)

def ask_btc_agent(user_question):

    # Simple BTC price/market questions are deterministic and do not require
    # Gemini intent selection. This keeps the original data fields while
    # preventing a temporary Gemini 503 from breaking the most common query.
    if _is_direct_btc_market_question(user_question):
        try:
            market = get_btc_market()
            return _format_direct_btc_market(market)
        except Exception as e:
            print(f"BTC 即時行情取得失敗：{e}")
            return "BTC 即時行情目前暫時無法取得，請稍後再試。"

    # Explicit technical-analysis questions bypass Gemini skill selection.
    # Gemini is called only after Python has successfully calculated the indicators.
    if _is_direct_btc_technical_question(user_question):
        try:
            technical = get_btc_technical()
            return _format_direct_btc_technical(technical)
        except Exception as e:
            print(f"BTC 技術分析取得失敗：{e}")
            return "BTC 技術分析資料目前暫時無法取得，請稍後再試。"

    # Explicit backtest requests bypass Gemini skill selection.
    # Existing date/year parsing and Binance/CoinGecko source selection are preserved.
    if _is_direct_btc_backtest_question(user_question):
        try:
            requested_period = _extract_requested_period(user_question)
            if (
                requested_period
                and requested_period.get("start_date")
                and requested_period.get("end_date")
            ):
                backtest = get_btc_backtest(
                    start_date=requested_period["start_date"],
                    end_date=requested_period["end_date"]
                )
            else:
                backtest = get_btc_backtest(years=_extract_backtest_years(user_question))
            return _format_direct_btc_backtest(backtest)
        except Exception as e:
            print(f"BTC 策略回測取得失敗：{e}")
            return "BTC 策略回測資料目前暫時無法取得，請稍後再試。"

    agent_result = agent_get_data(
        user_question
    )

    # Gemini 暫時無法使用
    if agent_result is None:

        return (
            "系統目前暫時忙碌中，"
            "請稍後再試。"
        )

    status = agent_result.get(
        "status",
        "OUT_OF_SCOPE"
    )

    # ========================================================
    # CLARIFY：使用者問題有多種合理解讀
    # ========================================================

    if status == "CLARIFY":

        prompt = f"""
你是 BTC AI 投資資訊助手。

使用者輸入：
{user_question}

系統已判斷這個問題與金融、Bitcoin 或市場相關，
但目前資訊不足，存在多種合理解讀。

請用繁體中文提出「一句簡短的澄清問題」，
讓使用者補充真正想查的內容。

例如：

使用者：「ETF呢」
可以回答：
「你是想了解 Bitcoin ETF 的相關消息，
還是一般 ETF 商品呢？」

使用者：「現在匯率呢」
可以回答：
「你想查哪一組匯率呢？例如美元兌台幣；
目前這個助手主要提供 BTC 市場資訊。」

重要規則：

1. 不要自行猜測使用者真正想問什麼。
2. 不要自行提供市場數字。
3. 不要假裝系統有目前不存在的 Skill。
4. 問題保持簡短自然。
5. 回答控制在 1～2 句。
"""

        for attempt in range(2):

            try:

                response = requests.post(
                    GEMINI_URL,
                    params={"key": API_KEY},
                    headers={
                        "Content-Type": "application/json"
                    },
                    json={
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": prompt
                                    }
                                ]
                            }
                        ]
                    },
                    timeout=10
                )

                if response.status_code == 200:

                    data = response.json()

                    return (
                        data["candidates"][0]
                        ["content"]["parts"][0]["text"]
                    )

                elif response.status_code == 503:

                    time.sleep(3)

                else:

                    break

            except requests.exceptions.RequestException:

                time.sleep(3)

        # Gemini 無法回覆時的備用回答
        return (
            "🤔 我還不太確定你想查的是哪一項，"
            "可以再告訴我具體想了解的內容嗎？"
        )

    # ========================================================
    # OUT_OF_SCOPE：目前系統不支援
    # ========================================================

    if status == "OUT_OF_SCOPE":

        prompt = f"""
你是 BTC AI 投資資訊助手。

使用者輸入：
{user_question}

目前系統主要支援：

- Bitcoin 即時行情
- Bitcoin 技術分析
- Bitcoin 相關新聞
- MA20/MA60 歷史策略回測
- Bitcoin 與 ETH、美股、黃金的跨市場關聯
- BTC 個人化提醒

目前沒有足夠的 Skills 可以完整分析
BTC 以外的個別股票、ETF 或其他金融商品。

請根據使用者輸入，用繁體中文簡短回覆。

如果使用者詢問其他金融商品，
請說明目前版本主要支援 BTC，
因此暫時無法提供該商品的完整分析。

如果使用者是在問你能做什麼，
請簡短介紹目前支援的 BTC 功能。

如果問題與 BTC / 金融完全無關，
請自然說明這不在目前助手的分析範圍，
並引導使用者詢問 BTC 相關問題。

不要自行回答沒有資料支援的金融問題。
不要捏造市場資料。
回答控制在 2～4 句。
"""

        for attempt in range(2):

            try:

                response = requests.post(
                    GEMINI_URL,
                    params={"key": API_KEY},
                    headers={
                        "Content-Type": "application/json"
                    },
                    json={
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": prompt
                                    }
                                ]
                            }
                        ]
                    },
                    timeout=10
                )

                if response.status_code == 200:

                    data = response.json()

                    return (
                        data["candidates"][0]
                        ["content"]["parts"][0]["text"]
                    )

                elif response.status_code == 503:

                    time.sleep(3)

                else:

                    break

            except requests.exceptions.RequestException:

                time.sleep(3)

        return (
            "🤖 我目前主要是 BTC AI 投資資訊助手。\n\n"
            "可以協助你了解 Bitcoin 即時行情、"
            "技術分析、新聞、策略回測與跨市場關聯。"
        )

    # ========================================================
    # OK：有選擇 Skill，產生正式回答
    # ========================================================

    final_answer = generate_final_answer(
        user_question,
        agent_result["results"]
    )

    return final_answer

def generate_final_answer(user_question, skill_results):

    prompt = f"""
你是一位 Bitcoin AI 財經資訊助手。

使用者問題：
{user_question}

以下是系統透過金融資料 Skills 實際取得的資料：
{json.dumps(skill_results, ensure_ascii=False, indent=2)}

請根據以上資料回答使用者。

LINE 顯示規則：

- 這是手機 LINE Bot，不是研究報告；優先讓使用者快速看懂。
- 禁止使用 Markdown 標記，不要輸出 #、##、###、**、__、```。
- 可以使用少量 emoji、換行與「｜」整理資訊。
- 數字優先、解釋其次，避免重複同一個數字或結論。
- 不要因為取得很多 Skill 資料就全部列出，只回答問題需要的部分。
- 單純行情問題請優先顯示：價格（USDT）、約當台幣參考價、1h 漲跌、24h 漲跌、24h 高點、24h 低點、24h 成交量（BTC）、24h 成交額（USDT）、24h 成交筆數。
- price_twd_approx 是依 USD/TWD 近似換算的台幣參考價，顯示時必須使用「約 NT$」或「台幣參考｜約 NT$」，不得描述成台灣交易所的 BTC 成交價。
- 如果 price_twd_approx 為 null，就只顯示 USDT，不要自行估算台幣。
- change_1h 只代表近 1 小時價格變化；change_24h 只代表近 24 小時。
- Binance 的成交量與成交額屬於 Binance BTC/USDT 市場資料，不要描述成全球成交量。
- 一般分析盡量控制在約 350～650 個中文字內。
- 若有 get_btc_period_change：
  * unit = "hour" 時，顯示「近 X 小時漲跌｜±X.XX%」。
  * unit = "day" 時，顯示「近 X 天漲跌｜±X.XX%」；7 天可自然表達為近一週。
  * unit = "month" 時，顯示「近 X 個月漲跌｜±X.XX%」。
  * unit = "year" 時，顯示「近 X 年漲跌｜±X.XX%」。
  * unit = "custom_range" 或 "date_range" 時，依 start_date 至 end_date 顯示指定期間漲跌。
  * 月與年使用實際日期往前推算，不要自行換算成固定 30 天或 365 天。
  * 若 unsupported = true，請直接告知目前僅支援整數時間區間，最小小時單位為 1 小時；不得把 0.5 小時誤解成 5 小時。
  * 不要把指定區間的漲跌誤稱為 24h 漲跌。
  * 若 Skill 回傳 data_source 與 market，期間變化回答必須顯示「資料來源｜data_source market」。
  * 若 data_source = Binance，顯示「資料來源｜Binance BTC/USDT」。
  * 若 data_source = CoinGecko，顯示「資料來源｜CoinGecko BTC/USD」。
  * 起點早於 Binance BTC/USDT 可用歷史時，系統會整段使用 CoinGecko；不得把 CoinGecko 的 USD 價格寫成 USDT。
  * 必須以 Skill 實際回傳的 start_date、end_date 顯示可用資料期間；若實際起點晚於使用者指定起點，不得假裝完整涵蓋原指定期間。
  * 若 data_range_note 不為 null，必須在回答中加入「⚠️ 資料範圍」並忠實簡述該 note，清楚說明使用者指定起點與實際可用起點。
  * change_pct 是「起點價格與終點價格的累積變化」，只能描述為「從起點至終點累積上漲／下跌 X%」。
  * 不得只根據起終點累積報酬，寫成「期間內持續成長」、「長期成長趨勢」、「一路上漲／下跌」或其他暗示中間路徑的敘述，因為此 Skill 沒有提供完整期間路徑分析。
- 技術分析若有 get_btc_technical，優先顯示：
  📊 BTC 技術面
  價格｜...
  MA5｜...
  MA20｜...
  MA60｜...
  RSI14｜...（訊號）
  MACD｜...
  Signal｜...
  Histogram｜...
  💡 觀察
  最多 1～2 句。
- MACD 顯示與判讀規則：
  * macd 對應 MACD 主線，macd_signal_line 對應 Signal Line，macd_histogram 對應 Histogram。
  * 不要把「偏多／偏空」直接接在 MACD 數值後面。
  * 若 MACD > Signal，可客觀描述「MACD 位於 Signal 之上」。
  * 若 MACD < Signal，可客觀描述「MACD 位於 Signal 之下」。
  * Histogram > 0 可描述為正值；Histogram < 0 可描述為負值。
  * 不要只因 MACD 本身為正值或負值，就推論整體市場偏多或偏空。
- 技術判讀必須依數值關係描述，不要籠統寫「均線呈現偏多/偏空格局」：
  * 價格 > MA5：可說價格位於 MA5 之上；價格 < MA5：可說位於 MA5 之下。
  * 價格 > MA20：可說站上 MA20；價格 < MA20：可說尚未站回 MA20。
  * MA20 > MA60：只可描述中期均線高於長期均線，不等同於短線全面偏多。
  * 若價格位於 MA5 與 MA20 之間，明確寫「價格位於 MA5 與 MA20 之間」。
- 目前 get_btc_technical 已提供 MACD、Signal Line 與 Histogram，因此可依三者的實際數值關係做客觀描述；但不要把單一 MACD 訊號直接等同於整體市場多空。
- 回測若有 get_btc_backtest，優先顯示期間、策略、累積報酬、Sharpe、最大回撤、交易訊號；
  trade_count 必須標示為「交易訊號（買／賣持倉切換）」或「持倉切換」，不要寫成容易誤解的「交易次數」；
  買進或賣出各計 1 次，不代表完整進出場組數。回測期間以 Skill 實際回傳的期間為準；使用者未指定時預設近 5 年，若明確指定「近 X 年」則依指定年數回測，不要自行改變期間。
  回測數值由 Python 計算，必須依 Skill 實際回傳結果解讀，不得自行修改、補造或推測數值。
  回測資料來源必須依 Skill 的 data_source 與 market 顯示，不得自行猜測；若為 Binance 顯示 Binance BTC/USDT，若為 CoinGecko 顯示 CoinGecko BTC/USD。
  若使用者指定明確起訖日期（例如 2013/1/1～2020/12/31），必須以 Skill 實際回傳的 start_date、end_date 為準。
  若回測 Skill 的 data_range_note 不為 null，必須另外顯示「⚠️ 資料範圍」，說明指定起點與實際回測起點不同；不得把 requested_start_date 當成實際回測開始日。
- 當回覆 MA20/MA60 策略回測時，統一使用以下視覺結構：
  第一行「📈 MA20/MA60 策略回測」
  第二行顯示「期間｜起始日～結束日」；若為近 X 年回測可補充（X 年），自訂日期區間則不要硬湊整數年數。
  下一行顯示「資料來源｜Binance BTC/USDT」或「資料來源｜CoinGecko BTC/USD」，必須依 Skill 實際回傳。
  接著「📊 策略績效」，比較 MA 策略（含成本）與 Buy & Hold 的累積報酬、Sharpe、最大回撤，並列出交易訊號。
  接著「💰 交易成本」，顯示未扣成本與扣除交易成本後的策略累積報酬。
  接著「🤖 AI 解讀」，只能依 Skill 實際回傳數值做 2～3 句客觀比較。
  最後「ℹ️ 回測提醒」，說明歷史回測不代表未來績效或投資建議，並簡短保留 Sharpe 的 Rf=0、日報酬、sqrt(365) 年化設定。
- 回測 AI 解讀不得自行新增 Skill 未提供的市場原因、策略優劣原因或未來預測。
- 當回覆 BTC 即時行情時，統一使用以下視覺結構：
  第一行「💰 BTC 即時行情」
  接著「💵 即時價格」，顯示 USDT 價格與約略台幣參考值。
  接著「📈 漲跌表現」，顯示近 1 小時與近 24 小時漲跌幅。
  接著「📊 24 小時市場」，顯示高點、低點、成交量、成交額、成交筆數。
  接著「🤖 AI 解讀」，只能依 Skill 回傳的價格與漲跌資料做 2～3 句客觀描述，例如目前價格在 24 小時區間中的相對位置、1h 與 24h 變動差異。
  最後「ℹ️ 資料說明」，說明台幣為匯率換算參考值，市場資訊來自 Binance BTC/USDT。
- 即時行情 AI 解讀不得自行加入「多頭、空頭、突破、支撐、壓力、追多、追空」等技術判斷，不得預測未來價格或提供買賣建議。
- 當回覆 BTC 技術分析時，統一使用以下視覺結構：
  第一行「📊 BTC 技術面」
  接著「💰 價格」，顯示目前 BTC USDT 價格。
  接著「📈 技術指標」，依 Skill 實際回傳顯示 MA5、MA20、MA60、RSI14、MACD、Signal、Histogram。
  接著「🤖 AI 解讀」，只能根據上述指標做 2～3 句客觀比較；若不同指標訊號不一致，應明確說明「不同技術指標呈現的訊號並不完全一致」，避免強行下單一多空結論。
  最後「ℹ️ 指標提醒」，說明技術指標僅反映歷史價格變化，不代表未來走勢或投資建議。
- 技術分析 AI 解讀不得自行新增 Skill 未提供的指標、價格目標、支撐壓力位或未來預測。
  Sharpe Ratio 必須註明「Rf=0、365 日年化」；不要暗示已納入非零無風險利率。
  若呈現 MA 策略的正式比較，優先使用 ma_strategy_after_cost 的成本後數值，並清楚標示「含成本」；若同時呈現成本前數值，必須明確區分。
  「觀察」只能根據實際提供的指標，例如累積報酬、Sharpe 比率、最大回撤、交易訊號與交易成本後績效。
  不得加入資料未提供的績效或風險指標；例如未提供波動率時，不要自行宣稱「波動度較低」或「波動度管理較佳」。
  比較策略與 Buy & Hold 時，使用客觀數值描述，例如「累積報酬較高／較低」、「最大回撤幅度較小／較大」，避免直接下「策略較好」、「較穩健」等總體結論。
  明確說明結果僅反映該回測期間、參數與成本設定下的歷史模擬，不代表未來績效或投資建議。
  不要寫成長篇報告。
- 跨市場若有 get_cross_market：
  * 可先顯示 recent_performance_pct 中 BTC、ETH、S&P 500、NASDAQ、黃金的近期漲跌幅。
  * recent_performance_window 是共同交易觀察值，不是日曆天數。
  * 再顯示 latest_rolling_correlation_with_btc 中 ETH、S&P 500、NASDAQ、黃金與 BTC 的近期相關係數。
  * rolling_window 是共同交易觀察值，不要稱為日曆 30 天。
  * 最後用 1～2 句客觀描述連動程度；相關係數不代表因果關係。
- 新聞最多優先整理 3 則，每則使用固定格式：
  1｜新聞標題
  MM/DD｜來源
  重點｜依該則新聞 title 與 summary 濃縮成 1 句
- 每則新聞都必須顯示資料中的 date 與 source，不得省略來源。
- 新聞標題盡量忠於原始 title，不要自行改寫成新的事件敘述。
- 新聞重點只能根據該則新聞實際提供的 title 與 summary 濃縮，不得自行補入未出現在資料中的政策決定、漲跌原因、價格、人物說法或其他事件細節。
- 如果 summary 資訊不足，就只整理 title 能支持的內容，不要自行補齊。
- 多則新聞不得互相混用內容，也不得把某一則新聞的資訊歸到另一則新聞。
- 新聞選擇優先順序必須固定為：
  1. BTC 直接相關性
  2. 新聞時效性
  3. 避免重複事件
  4. 來源多樣性
- 整理 3 則新聞時，優先只選 relevance =「BTC直接相關」的候選。
- 只有當「BTC直接相關」候選不足 3 則時，才可以使用 relevance =「Crypto補充」的新聞補足。
- 不得為了同時呈現 BlockTempo 與 ABMedia，而用 Zcash、其他個別幣種、公鏈或與 BTC 無直接關係的新聞取代 BTC 直接相關新聞。
- 若 BlockTempo 與 ABMedia 都有同樣符合 BTC 直接相關性與時效性的合適新聞，可再優先讓來源多樣化。
- 若兩個來源報導同一事件，優先保留資訊較完整的一則，另一個名額改選不同事件，避免 3 則新聞內容高度重複。
- 新聞最後可以加「💡 觀察」1 句，但只能概括這些新聞共同受到關注的主題，不得宣稱它們造成 BTC 漲跌或資金流向。
- 綜合分析最多使用「市場／技術／焦點／觀察」四個短區塊。

回答規則：

1. 使用繁體中文。

2. 直接回答使用者真正想問的問題，
   不需要使用者輸入固定關鍵字。

3. 不得自行捏造資料、數字、日期、新聞或市場事件。

4. 必須嚴格區分不同資料的時間範圍。

   - get_btc_market 的 change_1h 只代表近 1 小時。
   - get_btc_market 的 change_24h、
     high_24h、low_24h 等資料只代表近 24 小時。
   - get_btc_period_change 的 change_pct 只代表該 Skill 回傳的 hours 小時區間。

   - 不得使用 24 小時漲跌幅，
     直接推論 BTC 近一週、近一個月
     或更長期間的漲跌表現。

5. 如果使用者使用「最近」、「近期」、
   「這陣子」等沒有明確期間的詞：

   - 可以先使用目前取得的資料回答。
   - 但必須明確說明資料涵蓋的時間尺度。
   - 如果目前只有 24 小時資料，
     請使用「若以近 24 小時來看」等表達。
   - 不可以直接把 24 小時結果說成
     「最近整體市場就是如此」。
   - 若問題需要更長期間資料才能確定，
     請坦白說明目前資料不足以判斷
     近 7 日、30 日或其他期間。

6. 如果使用新聞資料：

   - 新聞中提到的價格、漲跌或市場狀態，
     屬於該新聞事件發生時的描述。
   - 不得把新聞中的歷史價格
     當成目前即時價格。
   - 如果同時有 get_btc_market，
     目前價格必須以 get_btc_market 為準。
   - 必要時明確說明：
     「新聞中提到的價格為事件發生時的行情，
     不代表目前即時價格。」

7. 不要把新聞事件直接說成造成 BTC 漲跌的原因，
   除非資料本身可以支持因果關係。
   可以使用：
   「市場關注」
   「可能影響市場情緒」
   「與近期市場波動同時受到關注」
   等較謹慎的說法。

8. 技術指標請用一般投資者容易理解的方式解釋。

9. 技術指標只能視為市場觀察工具，
   不得把單一指標描述成確定的未來漲跌訊號。

10. 回測結果屬於歷史模擬結果，
    不代表未來績效。

11. 相關係數只代表市場報酬率的連動程度，
    不代表因果關係。

12. 不要因為 BTC 與黃金近期正相關，
    就直接宣稱 BTC 已成為避險資產。
    只能描述兩者近期價格變動的同步程度。

13. 不提供直接買進、賣出、做多或做空指示。

14. 如果資料不足以支持某個結論，
    請直接說資料不足，
    不要自行補充不存在的資訊。

15. 回答長度必須依照使用者問題的複雜程度調整。

    - 如果只是詢問單一資訊，
      例如目前價格、24 小時漲跌幅、
      某個技術指標：
      請直接回答重點，通常控制在 1～3 句，
      不要主動展開完整市場分析。

    - 如果是簡單判斷型問題，
      例如「最近是不是跌很多」、
      「現在偏強還是偏弱」：
      請先直接回答結論，再提供最必要的數據與限制，
      通常控制在 3～6 句。

    - 只有當使用者明確要求「分析」、「詳細分析」、
      「整理目前市場狀況」、「比較」或問題本身需要
      多個面向才能回答時，
      才提供較完整的分段分析。

    不要因為系統取得了很多資料，
    就把所有資料全部告訴使用者。
    只使用回答當前問題真正需要的資訊。

16. 只有當使用者的問題涉及期間比較、漲跌幅、
    趨勢，或使用「最近」、「近期」、「這陣子」、
    「跌很多」、「漲很多」等需要時間尺度判斷的問題時，
    才主動說明目前資料的時間範圍與限制。

    如果使用者只是詢問目前價格，
    例如「現在一顆多少」、「BTC 現在多少」，
    請直接回答目前價格與必要的即時市場資訊，
    不要額外補充「缺少 7 日、30 日資料」
    或其他與問題無關的時間範圍限制。

17. 不要使用「高檔」、「低檔」、「歷史高位」、
    「歷史低位」等相對價格位置描述，
    除非目前取得的資料足以支持該判斷。

18. 不要自行加入目前 Skills 沒有實際計算或提供的
    支撐位、壓力位或其他技術結論。
    所有市場判斷都必須能由目前取得的 Skill 資料支持。
"""

    for attempt in range(2):

        try:

            response = requests.post(
                GEMINI_URL,
                params={"key": API_KEY},
                headers={
                    "Content-Type": "application/json"
                },
                json={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": prompt
                                }
                            ]
                        }
                    ]
                },
                timeout=10
            )

            if response.status_code == 200:

                data = response.json()

                return (
                    data["candidates"][0]
                    ["content"]["parts"][0]["text"]
                )

            elif response.status_code == 503:

                print(
                    f"Gemini 忙碌中，第 "
                    f"{attempt + 1} 次重試..."
                )

                time.sleep(3)

            else:

                return (
                    f"Gemini API 錯誤 "
                    f"{response.status_code}："
                    f"{response.text}"
                )

        except requests.exceptions.RequestException as e:

            print(
                f"Gemini 連線錯誤：{e}"
            )

            time.sleep(3)

    return "Gemini 目前使用量較高，請稍後再試。"