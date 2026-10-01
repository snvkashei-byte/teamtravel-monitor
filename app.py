import os, json, logging
from flask import Flask, request, abort
import urllib.request, urllib.parse

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("teamtravel-monitor")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # должен совпадать с secret_token в setWebhook

CHATS = {
    -1003500764364: "Кыргызстан",
    -1003638029318: "Владивосток",
    -1003519749455: "Сахалин",
    -1003599281828: "Камчатка",
}

QUESTION_MARKERS = [
    "?", "подскажи", "скажи", "можно", "можете", "как ", "когда",
    "где ", "сколько", "есть ли", "будет ли", "что если", "нужно ли",
    "доступно", "свободно", "осталось", "помогите", "не знаю"
]

def is_question(text):
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in QUESTION_MARKERS)

def send_tg(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data), timeout=10) as r:
            result = json.loads(r.read())
            if not result.get("ok"):
                log.error("Telegram API error on %s: %s", method, result)
            return result
    except Exception as e:
        log.exception("Failed to call Telegram API method %s", method)
        return {"error": str(e)}

@app.route("/webhook", methods=["POST"])
def webhook():
    # 1) Проверка секрета — защита от произвольных POST на публичный URL
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming_secret != WEBHOOK_SECRET:
            log.warning("Webhook called with invalid or missing secret token")
            abort(403)

    update = request.get_json(silent=True) or {}
    msg = update.get("message")
    if not msg or not msg.get("text"):
        return "OK"

    chat_id = msg["chat"]["id"]
    chat_name = CHATS.get(chat_id)
    if not chat_name:
        return "OK"

    sender_info = msg.get("from", {})
    if sender_info.get("is_bot"):
        return "OK"

    # 2) Не уведомлять Николая о его же собственных сообщениях
    if sender_info.get("id") == ADMIN_CHAT_ID:
        return "OK"

    text = msg.get("text", "")
    if not is_question(text):
        return "OK"

    sender = (sender_info.get("first_name", "") + " " +
              sender_info.get("last_name", "")).strip() or "Неизвестно"
    note = (f"🔔 Новый вопрос — TeamTravel\n"
            f"📌 {chat_name}\n"
            f"👤 {sender}\n"
            f"💬 {text[:300]}\n\n"
            f"⏰ Требуется ответ")
    send_tg("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": note, "parse_mode": "HTML"})
    return "OK"

@app.route("/")
def index():
    return "TeamTravel Monitor OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
