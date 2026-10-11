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
    existing = ws.get_all_values()
    if not existing:
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        existing_ids = set()
    else:
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


def sync():
    gc = get_sheet_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    sync_balances(sh)
    sync_orders(sh)


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
