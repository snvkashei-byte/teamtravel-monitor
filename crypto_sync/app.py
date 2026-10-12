import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-sync")

BASE_URL = "https://api.bytick.com"
API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1tca0CIl2takJTUNwTIAR74fXbF1B-zBcZ74I5-8aCgo")
INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# T-Invest API (Т-Банк Инвестиции) — живая цена акции YDEX (Яндекс, MOEX).
# Токен выпускается в личном кабинете: tbank.ru/invest/settings/api/ (права "только чтение").
TINVEST_API_TOKEN = os.environ.get("TINVEST_API_TOKEN", "")
TINVEST_BASE_URL = "https://invest-public-api.tbank.ru/rest"
TINVEST_TICKER = "YDEX"
TINVEST_CLASS_CODE = "TQBR"  # основной режим торгов акциями на MOEX

app = Flask(__name__)


# ---------- Bybit API ----------

def _sign(timestamp, recv_window, payload_str):
    raw = f"{timestamp}{API_KEY}{recv_window}{payload_str}"
    return hmac.new(API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def bybit_get(path, params=None):
    params = params or {}
    query_string = urlencode(params)
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    sign = _sign(timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": recv_window,
    }
    url = f"{BASE_URL}{path}"
    if query_string:
        url += f"?{query_string}"
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
    return data["result"]


def get_wallet_balance():
    """Баланс Unified-аккаунта (Spot + UM futures) по монетам."""
    result = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    balances = {}
    for acc in result.get("list", []):
        for c in acc.get("coin", []):
            coin = c["coin"]
            qty = float(c.get("walletBalance") or 0)
            usd = float(c.get("usdValue") or 0)
            if qty or usd:
                balances[coin] = {"qty": qty, "usd": usd}
    return balances


def get_earn_positions(category):
    """category: 'FlexibleSaving' или 'OnChain'."""
    result = bybit_get("/v5/earn/position", {"category": category})
    positions = {}
    for p in result.get("list", []):
        coin = p["coin"]
        amount = float(p.get("amount") or 0)
        positions[coin] = positions.get(coin, 0) + amount
    return positions


def get_recent_orders(category="spot", limit=50):
    result = bybit_get("/v5/order/history", {"category": category, "limit": str(limit)})
    return result.get("list", [])


PRICE_SYMBOLS = ["BTC", "SOL", "ATOM", "ETH", "ARB", "GRAM"]


def get_public_prices():
    """Публичные (без подписи) текущие цены спота с Bybit."""
    prices = {}
    for coin in PRICE_SYMBOLS:
        try:
            result = bybit_get("/v5/market/tickers", {"category": "spot", "symbol": f"{coin}USDT"})
            lst = result.get("list", [])
            if lst:
                prices[coin] = float(lst[0]["lastPrice"])
        except Exception:  # noqa: BLE001
            log.exception("price fetch failed for %s", coin)
    return prices


# ---------- T-Invest API (YDEX) ----------

_ydex_figi_cache = None


def tinvest_post(method, payload):
    url = f"{TINVEST_BASE_URL}/{method}"
    headers = {
        "Authorization": f"Bearer {TINVEST_API_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _quotation_to_float(q):
    """T-Invest API возвращает денежные величины как {units, nano}."""
    if not q:
        return None
    units = int(q.get("units", 0) or 0)
    nano = int(q.get("nano", 0) or 0)
    return units + nano / 1e9


def get_ydex_figi():
    """FIGI акции YDEX на MOEX. Кешируется в памяти процесса — не меняется между перезапусками редко."""
    global _ydex_figi_cache
    if _ydex_figi_cache:
        return _ydex_figi_cache
    data = tinvest_post(
        "tinkoff.public.invest.api.contract.v1.InstrumentsService/ShareBy",
        {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": TINVEST_CLASS_CODE, "id": TINVEST_TICKER},
    )
    figi = data.get("instrument", {}).get("figi")
    if not figi:
        raise RuntimeError(f"T-Invest: не удалось найти FIGI для {TINVEST_TICKER}: {data}")
    _ydex_figi_cache = figi
    return figi


def get_ydex_price():
    """Текущая (последняя) цена акции YDEX в рублях, или None при ошибке/отсутствии токена."""
    if not TINVEST_API_TOKEN:
        return None
    try:
        figi = get_ydex_figi()
        data = tinvest_post(
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices",
            {"instrumentId": [figi]},
        )
        prices = data.get("lastPrices", [])
        if not prices:
            return None
        return _quotation_to_float(prices[0].get("price"))
    except Exception:  # noqa: BLE001
        log.exception("ydex price fetch failed")
        return None


# ---------- Google Sheets ----------

def get_sheet_client():
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(sh, title, cols):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=200, cols=cols)


# ---------- Sync logic ----------

def sync_balances(sh):
    balances = get_wallet_balance()
    flexible = get_earn_positions("FlexibleSaving")
    onchain = get_earn_positions("OnChain")
    coins = sorted(set(balances) | set(flexible) | set(onchain))

    header = ["Монета", "Spot (кол-во)", "Spot (USD)", "Earn Flexible", "Earn OnChain", "Итого (кол-во)", "Обновлено"]
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    rows = []
    for coin in coins:
        spot_qty = balances.get(coin, {}).get("qty", 0)
        spot_usd = balances.get(coin, {}).get("usd", 0)
        fl = flexible.get(coin, 0)
        oc = onchain.get(coin, 0)
        total = spot_qty + fl + oc
        rows.append([coin, spot_qty, round(spot_usd, 2), fl, oc, total, now])

    ws = get_or_create_worksheet(sh, "Bybit API", len(header) + 2)
    ws.clear()
    ws.update("A1", [header] + rows, value_input_option="USER_ENTERED")
    log.info("balances synced: %d coins", len(rows))


def sync_orders(sh):
    header = ["Дата", "Пара", "Сторона", "Кол-во", "Цена", "Сумма (USDT)", "Статус", "Order ID"]
    ws = get_or_create_worksheet(sh, "Bybit Ордера", len(header) + 2)
    first_cell = ws.acell("A1").value
    if first_cell != header[0]:
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        existing_ids = set()
    else:
        existing = ws.get_all_values()
        existing_ids = {row[7] for row in existing[1:] if len(row) > 7}

    orders = get_recent_orders("spot", 50)
    new_rows = []
    for o in orders:
        if o.get("orderStatus") not in ("Filled", "PartiallyFilled"):
            continue
        oid = o.get("orderId")
        if not oid or oid in existing_ids:
            continue
        ts = int(o.get("updatedTime") or o.get("createdTime") or 0)
        date_str = (
            datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d.%m.%Y %H:%M")
            if ts
            else ""
        )
        new_rows.append(
            [
                date_str,
                o.get("symbol"),
                o.get("side"),
                o.get("cumExecQty"),
                o.get("avgPrice"),
                o.get("cumExecValue"),
                o.get("orderStatus"),
                oid,
            ]
        )
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    log.info("orders synced: %d new rows", len(new_rows))


def sync_prices(sh):
    prices = get_public_prices()
    ydex_price = get_ydex_price()
    if ydex_price is not None:
        prices["YDEX"] = ydex_price
    if not prices:
        return
    ws = sh.worksheet("Факт цены")
    coin_col = ws.col_values(1)
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    updates = []
    seen = set()
    for i, coin in enumerate(coin_col, start=1):
        if coin in prices:
            updates.append({"range": f"B{i}:C{i}", "values": [[prices[coin], now]]})
            seen.add(coin)
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    # Монеты/тикеры, которых ещё нет в листе (например, YDEX при первом запуске) — дописываем новыми строками.
    missing = [coin for coin in prices if coin not in seen]
    if missing:
        new_rows = [[coin, prices[coin], now, ""] for coin in missing]
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    log.info("prices synced: %d updated, %d new", len(updates), len(missing))


def sync():
    gc = get_sheet_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    sync_balances(sh)
    sync_orders(sh)
    sync_prices(sh)


# ---------- Web server / scheduler ----------

@app.route("/")
def health():
    return "OK", 200


@app.route("/sync")
def manual_sync():
    try:
        sync()
        return "Синхронизация выполнена", 200
    except Exception as e:  # noqa: BLE001
        log.exception("manual sync failed")
        return f"Ошибка: {e}", 500


def loop():
    while True:
        try:
            if API_KEY and API_SECRET and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
                sync()
            else:
                log.warning("не заданы переменные окружения, синхронизация пропущена")
        except Exception:  # noqa: BLE001
            log.exception("sync failed")
        time.sleep(INTERVAL_MIN * 60)


_started = False


def start_background():
    global _started
    if not _started:
        _started = True
        t = threading.Thread(target=loop, daemon=True)
        t.start()


start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
