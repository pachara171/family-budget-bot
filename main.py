import os
import re
import json
import tempfile
from datetime import datetime
from dotenv import load_dotenv

import flask
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent
)

import gspread
from google.oauth2.service_account import Credentials
import requests
import base64

load_dotenv()

# เขียน credentials ลงไฟล์ชั่วคราวสำหรับ google.auth.default()
_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if _creds_json:
    import tempfile
    _f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    _f.write(_creds_json)
    _f.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _f.name

app = Flask(__name__)

# LINE setup
configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

# Google Sheets setup
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if creds_json:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write(creds_json)
        creds_path = f.name
else:
    creds_path = os.environ["GOOGLE_CREDENTIALS_PATH"]
creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
# creds = Credentials.from_service_account_file(
#     os.environ["GOOGLE_CREDENTIALS_PATH"], scopes=SCOPES
# )
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"]).worksheet("transactions")

# Google Vision setup
# vision_client = vision.ImageAnnotatorClient(credentials=creds)

# ---- หมวดหมู่ ----
EXPENSE_CATEGORIES = {
    "อาหาร": ["ข้าว", "อาหาร", "กาแฟ", "ชา", "ขนม", "น้ำ", "ร้าน", "ก๋วยเตี๋ยว", "หมู", "ไก่", "ปลา"],
    "เดินทาง": ["รถ", "น้ำมัน", "แท็กซี่", "grab", "บัส", "รถไฟ", "ค่าเดินทาง", "ค่าโดยสาร"],
    "ที่พัก": ["ค่าเช่า", "ค่าน้ำ", "ค่าไฟ", "อินเตอร์เน็ต", "ค่าโทรศัพท์"],
    "สุขภาพ": ["หมอ", "ยา", "โรงพยาบาล", "คลินิก"],
    "ช้อปปิ้ง": ["ซื้อ", "เสื้อ", "กางเกง", "รองเท้า", "ห้าง", "ตลาด"],
    "ความบันเทิง": ["หนัง", "เกม", "ท่องเที่ยว", "เที่ยว"],
    "รายรับ": ["เงินเดือน", "โบนัส", "รายได้", "รับเงิน", "ได้รับ"],
}

def guess_category(text):
    text_lower = text.lower()
    for cat, keywords in EXPENSE_CATEGORIES.items():
        if any(k in text_lower for k in keywords):
            return cat
    return "อื่นๆ"

def parse_transaction(text):
    """
    รองรับหลายรูปแบบ:
    - "ข้าว 50"         → จ่าย / อาหาร / 50
    - "ข้าว 50 บาท"
    - "+5000 เงินเดือน" → รับ / รายรับ / 5000
    - "รับ 5000 เงินเดือน"
    """
    text = text.strip()

    # ตรวจว่าเป็นรายรับไหม
    is_income = bool(re.match(r'^\+', text)) or text.startswith("รับ") or text.startswith("รายรับ")

    # ดึงตัวเลข
    numbers = re.findall(r'[\d,]+(?:\.\d+)?', text.replace(',', ''))
    if not numbers:
        return None
    amount = float(numbers[0].replace(',', ''))

    # ดึง note (ลบตัวเลขและ keyword ออก)
    note = re.sub(r'[\d,]+(?:\.\d+)?', '', text)
    note = re.sub(r'\b(บาท|รับ|จ่าย|รายรับ|รายจ่าย)\b', '', note)
    note = re.sub(r'^\+', '', note).strip()

    tx_type = "รายรับ" if is_income else "รายจ่าย"
    category = guess_category(text) if is_income else guess_category(note or text)
    if is_income:
        category = "รายรับ"

    return {
        "type": tx_type,
        "category": category,
        "amount": amount,
        "note": note or text,
    }

def save_to_sheet(tx, user_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = [now, tx["type"], tx["category"], tx["amount"], tx["note"], user_name]
    sheet.append_row(row)

def get_monthly_summary(year=None, month=None):
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    all_rows = sheet.get_all_records()
    month_str = f"{year}-{month:02d}"

    income = 0
    expense = 0
    cats = {}

    for row in all_rows:
        if not row.get("datetime", "").startswith(month_str):
            continue
        amt = float(row.get("amount", 0))
        if row.get("type") == "รายรับ":
            income += amt
        else:
            expense += amt
            cat = row.get("category", "อื่นๆ")
            cats[cat] = cats.get(cat, 0) + amt

    balance = income - expense
    sign = "+" if balance >= 0 else ""

    lines = [
        f"📊 สรุปเดือน {month}/{year}",
        f"💰 รายรับ: {income:,.0f} บาท",
        f"💸 รายจ่าย: {expense:,.0f} บาท",
        f"{'📈' if balance >= 0 else '📉'} คงเหลือ: {sign}{balance:,.0f} บาท",
        "",
        "📂 แยกหมวดหมู่รายจ่าย:",
    ]
    for cat, amt in sorted(cats.items(), key=lambda x: -x[1]):
        lines.append(f"  • {cat}: {amt:,.0f} บาท")

    return "\n".join(lines)

def search_transactions(keyword):
    all_rows = sheet.get_all_records()
    results = [r for r in all_rows if keyword.lower() in str(r.get("note", "")).lower()
               or keyword.lower() in str(r.get("category", "")).lower()]

    if not results:
        return f'🔍 ไม่พบรายการที่มีคำว่า "{keyword}"'

    lines = [f'🔍 พบ {len(results)} รายการสำหรับ "{keyword}":', ""]
    for r in results[-10:]:  # แสดงล่าสุด 10 รายการ
        sign = "+" if r["type"] == "รายรับ" else "-"
        lines.append(f"{r['datetime'][:10]}  {sign}{float(r['amount']):,.0f}  {r['note']}")

    return "\n".join(lines)

# def extract_amount_from_slip(image_bytes):
#     """OCR สลิปธนาคารด้วย Google Vision"""
#     image = vision.Image(content=image_bytes)
#     response = vision_client.text_detection(image=image)

#     if response.error.message:
#         return None, None

#     full_text = response.text_annotations[0].description if response.text_annotations else ""

#     # หายอดเงิน (รูปแบบ: 1,234.56 หรือ 1234.56)
#     amounts = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b', full_text)
#     amounts = [float(a.replace(',', '')) for a in amounts if float(a.replace(',', '')) > 0]

#     # เอายอดที่ใหญ่ที่สุด (มักเป็นยอดโอน)
#     main_amount = max(amounts) if amounts else None

#     return main_amount, full_text[:200]

def extract_amount_from_slip(image_bytes):
    """OCR สลิปด้วย Google Vision REST API"""
    # encode รูปเป็น base64
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    # ขอ token จาก service account
    import google.auth.transport.requests
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-vision"])
    credentials.refresh(google.auth.transport.requests.Request())
    token = credentials.token

    url = "https://vision.googleapis.com/v1/images:annotate"
    payload = {
        "requests": [{
            "image": {"content": b64},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, json=payload, headers=headers).json()

    try:
        full_text = resp["responses"][0]["textAnnotations"][0]["description"]
    except (KeyError, IndexError):
        return None, None

    amounts = re.findall(r'\b\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b', full_text)
    amounts = [float(a.replace(',', '')) for a in amounts if float(a.replace(',', '')) > 0]
    main_amount = max(amounts) if amounts else None

    return main_amount, full_text[:200]

def get_help_text():
    return """📖 วิธีใช้งาน Budget Bot

💬 บันทึกรายจ่าย:
  ข้าว 50
  ค่าน้ำมัน 300 บาท
  กาแฟ 65

💬 บันทึกรายรับ:
  +25000 เงินเดือน
  รับ 5000 ค่าจ้าง

📊 ดูสรุป:
  สรุป

🔍 ค้นหา:
  ค้นหา ข้าว
  ค้นหา ค่าน้ำมัน

🧾 อ่านสลิป:
  ส่งรูปสลิปมาเลย → bot จะถามยืนยัน

❓ ความช่วยเหลือ:
  help หรือ ช่วยเหลือ"""

# ---- Webhook ----
@app.route("/health")
def health():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # ดึงชื่อ user (Group จะได้ member profile)
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        try:
            if hasattr(event.source, 'group_id'):
                profile = line_bot_api.get_group_member_profile(
                    event.source.group_id, user_id
                )
            else:
                profile = line_bot_api.get_profile(user_id)
            user_name = profile.display_name
        except Exception:
            user_name = "Unknown"

        # กรณีต่างๆ
        if text.lower() in ["help", "ช่วยเหลือ", "วิธีใช้"]:
            reply = get_help_text()

        elif text.startswith("สรุป"):
            reply = get_monthly_summary()

        elif text.startswith("ค้นหา "):
            keyword = text[6:].strip()
            reply = search_transactions(keyword)

        else:
            tx = parse_transaction(text)
            if tx:
                save_to_sheet(tx, user_name)
                sign = "+" if tx["type"] == "รายรับ" else "-"
                reply = (
                    f"✅ บันทึกแล้ว!\n"
                    f"{'💰' if tx['type'] == 'รายรับ' else '💸'} {tx['type']}\n"
                    f"📂 หมวด: {tx['category']}\n"
                    f"💵 {sign}{tx['amount']:,.0f} บาท\n"
                    f"📝 {tx['note']}"
                )
            else:
                reply = "❓ ไม่เข้าใจคำสั่ง พิมพ์ help เพื่อดูวิธีใช้"

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # Download รูป
        image_content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b"".join(image_content)

        # OCR
        amount, raw_text = extract_amount_from_slip(image_bytes)

        if amount:
            reply = (
                f"🧾 อ่านสลิปได้!\n"
                f"💵 ยอดที่พบ: {amount:,.2f} บาท\n\n"
                f"ต้องการบันทึกเป็นรายจ่ายไหม?\n"
                f"ตอบ: บันทึกสลิป {amount:.0f} [หมวด]\n"
                f"เช่น: บันทึกสลิป {amount:.0f} อาหาร"
            )
        else:
            reply = "❌ อ่านสลิปไม่ได้ ลองถ่ายใหม่ให้ชัดขึ้น หรือพิมพ์บันทึกเองนะครับ"

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)