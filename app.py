import os, json
from flask import Flask, request
import urllib.request, urllib.parse

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

CHATS = {
    -1001003500764364: "Кыргызстан",
    -1001003638029318: "Владивосток",
    -1001003519749455: "Сахалин",
    -1001003599281828: "Камчатка",
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
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message")
    if not msg or not msg.get("text"):
        return "OK"
    chat_id = msg["chat"]["id"]
    chat_name = CHATS.get(chat_id)
    if not chat_name:
        return "OK"
    if msg.get("from", {}).get("is_bot"):
        return "OK"
    text = msg.get("text", "")
    if not is_question(text):
        return "OK"
    sender = (msg.get("from", {}).get("first_name", "") + " " +
              msg.get("from", {}).get("last_name", "")).strip() or "Неизвестно"
    note = (f"🔔 <b>Новый вопрос — TeamTravel</b>\n"
            f"📌
