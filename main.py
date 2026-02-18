# main.py
# Telegram course-export bot (Heroku-ready, polling + Flask)

import os
import tempfile
import logging
from pathlib import Path
import time
import re
from threading import Thread

import requests
import telebot
from flask import Flask
from telebot import types
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8554751285:AAH_AroJhNog3XPk3fDw4y5tP-bM0uPFC1A")
BASE_URL = "https://backend.multistreaming.site/api"
USER_ID_FOR_ACTIVE = "1448640"

BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise SystemExit("❌ BOT_TOKEN set kar pehle (env ya code me).")

print("🔥 main.py imported, initializing bot & Flask app...")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
app = Flask("render_web")

# In-memory state
user_state = {}          # chat_id -> "await_course_id" / None
user_batches = {}        # chat_id -> dict(id -> batch)
user_batches_list = {}   # chat_id -> list of batches (pagination)
user_selected = {}       # chat_id -> selected batch

PAGE_SIZE = 10


# ---------------- GENERIC HELPERS ----------------
def safe_send(send_func, *args, **kwargs):
    try:
        return send_func(*args, **kwargs)
    except Exception:
        logging.exception("safe_send error")
        return None


def safe_json_get(r: requests.Response):
    try:
        return r.json()
    except Exception as e:
        logging.warning("safe_json_get failed: %s", e)
        return {}


@app.route("/")
def home():
    return "✅ Bot is running (Flask alive)."


# ---------------- BATCH FETCHING (ALL COURSES) ----------------
def get_active_batches():
    """
    Backend ke /courses?userId=... se saare courses (active + inactive) aajayenge.
    Return (ok, batches_list)
    """
    url = f"{BASE_URL}/courses?userId={USER_ID_FOR_ACTIVE}"
    print(f"🌐 Fetching batches from: {url}")
    logging.info(f"Fetching batches from: {url}")
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=15)
        data = safe_json_get(r)

        # direct state check
        if isinstance(data, dict) and data.get("state") == 200 and isinstance(data.get("data"), list):
            print(f"📦 Received {len(data['data'])} items (state=200).")
            return True, data["data"]

        # fallback data list
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            print(f"📦 Received {len(data['data'])} items (data list).")
            return True, data["data"]

        print("⚠️ Unexpected batch response format.")
        return False, []
    except Exception:
        logging.exception("get_active_batches error")
        return False, []


# ---------------- COURSE / CLASS HELPERS ----------------
def get_course_classes(course_id):
    """Fetch classes for a course_id using classes?populate=full. Returns (ok, classes_list)."""
    url = f"{BASE_URL}/courses/{course_id}/classes?populate=full"
    print(f"🌐 Fetching classes for course_id={course_id}")
    logging.info(f"get_course_classes: {url}")
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=20)
        data = safe_json_get(r)
        if isinstance(data, dict) and data.get("state") == 200 and isinstance(data.get("data"), list):
            return True, data["data"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "classes" in inner and isinstance(inner["classes"], list):
                return True, inner["classes"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return True, data["data"]
        return False, []
    except Exception:
        logging.exception("get_course_classes error")
        return False, []


def find_pdf_from_active(course_id, batches=None):
    """Search batches list for batchInfoPdfUrl. Return list (may be empty)."""
    try:
        if batches is None:
            ok, batches = get_active_batches()
            if not ok:
                return []
        for b in batches:
            if str(b.get("id")) == str(course_id) or str(b.get("_id")) == str(course_id):
                pdf = b.get("batchInfoPdfUrl") or b.get("batch_info_pdf") or b.get("pdf") or ""
                if not pdf:
                    return []
                if isinstance(pdf, list):
                    return [p for p in pdf if p]
                if isinstance(pdf, str):
                    parts = re.split(r"[\n,;]+", pdf)
                    return [p.strip() for p in parts if p.strip()]
        return []
    except Exception:
        logging.exception("find_pdf_from_active error")
        return []


def _extract_subject_from_title(title, fallback=None):
    """Extract a compact subject token for bracket prefix."""
    try:
        if "||" in title:
            parts = [p.strip() for p in title.split("||")]
            if len(parts) > 1:
                second = parts[1]
                if "|" in second:
                    return second.split("|")[0].strip()
                return second.strip()
        if "|" in title:
            parts = [p.strip() for p in title.split("|")]
            for p in parts:
                if p and not re.search(r"(?i)class[\s-]*\d+", p):
                    return p
        if fallback:
            return fallback
        return "Course"
    except Exception:
        return fallback or "Course"


def normalize_video_entries(class_item):
    """Extract primary link, mp4s, and PDFs from class_item."""
    title = (
        class_item.get("title")
        or class_item.get("classTitle")
        or class_item.get("name")
        or class_item.get("heading")
        or "Untitled"
    )

    candidate_links = []

    # direct keys
    direct_keys = [
        "class_link", "videoLink", "video_link", "video_url", "videoUrl",
        "link", "url", "playbackUrl", "playback_url", "streamUrl", "stream_url"
    ]
    for k in direct_keys:
        v = class_item.get(k)
        if isinstance(v, str) and v:
            candidate_links.append(v)

    # m3u8 related
    m3u8_keys = [
        "masterPlaylist", "master_playlist",
        "hlsLink", "hls_link",
        "secureLink", "secure_link",
        "m3u8", "m3u8Url", "m3u8_url",
        "playlist", "playlistUrl"
    ]
    for k in m3u8_keys:
        v = class_item.get(k)
        if isinstance(v, str) and v:
            candidate_links.append(v)

    # arrays
    array_keys = ["rawSources", "sources", "recordings", "files", "videoFiles", "videos", "assets"]
    for k in array_keys:
        arr = class_item.get(k)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and it:
                    candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str) and vv:
                            candidate_links.append(vv)

    # nested objects
    nested_keys = ["playback", "video", "stream", "media"]
    for nk in nested_keys:
        obj = class_item.get(nk)
        if isinstance(obj, dict):
            for subk in ("url", "file", "m3u8", "mp4", "hls", "src"):
                vv = obj.get(subk)
                if isinstance(vv, str) and vv:
                    candidate_links.append(vv)
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, str):
                    candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str):
                            candidate_links.append(vv)

    # embed / iframe
    for k in ("embed", "iframe", "embedHtml"):
        v = class_item.get(k)
        if isinstance(v, str) and "http" in v:
            m = re.search(r"https?://[^\s'\"<>]+", v)
            if m:
                candidate_links.append(m.group(0))

    # dedupe links
    seen = set()
    clean_candidates = []
    for u in candidate_links:
        if not isinstance(u, str) or not u.strip():
            continue
        u = u.strip()
        if u not in seen:
            seen.add(u)
            clean_candidates.append(u)

    hls_links = [u for u in clean_candidates if "m3u8" in u or "playlist-mpl" in u or "hls" in u.lower()]
    other_links = [u for u in clean_candidates if u not in hls_links]

    # mp4 list
    mp4_list = []
    for u in clean_candidates:
        low = u.lower()
        if low.endswith(".mp4") or ".mp4?" in low:
            mp4_list.append(u)

    explicit_mp4 = class_item.get("mp4Recordings") or class_item.get("mp4_recordings") or class_item.get("mp4records")
    if isinstance(explicit_mp4, list):
        for it in explicit_mp4:
            if isinstance(it, str) and it.strip():
                if it not in mp4_list:
                    mp4_list.append(it.strip())
            elif isinstance(it, dict):
                for subk in ("url", "file", "mp4"):
                    vv = it.get(subk)
                    if isinstance(vv, str) and vv.strip() and vv not in mp4_list:
                        mp4_list.append(vv.strip())

    mp4_seen = set()
    mp4_clean = []
    for m in mp4_list:
        if m not in mp4_seen:
            mp4_seen.add(m)
            mp4_clean.append(m)

    # PDFs
    class_pdfs = []
    pdf_keys = ["classPdf", "class_pdf", "pdfs", "materials", "resources", "files"]
    for key in pdf_keys:
        arr = class_item.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and ".pdf" in it.lower():
                    class_pdfs.append(it.strip())
                elif isinstance(it, dict):
                    for subk in ("url", "file", "pdf"):
                        vv = it.get(subk)
                        if isinstance(vv, str) and ".pdf" in vv.lower():
                            class_pdfs.append(vv.strip())

    for k in ("pdf", "pdfUrl", "pdf_url", "file"):
        v = class_item.get(k)
        if isinstance(v, str) and ".pdf" in v.lower():
            class_pdfs.append(v.strip())

    pdf_seen = set()
    pdf_clean = []
    for p in class_pdfs:
        if p not in pdf_seen:
            pdf_seen.add(p)
            pdf_clean.append(p)

    # primary link
    primary_link = ""
    if hls_links:
        primary_link = hls_links[0]
    elif other_links:
        primary_link = other_links[0]
    else:
        primary_link = ""

    # decide if we include mp4s separately
    include_mp4s = False if primary_link and (
        "m3u8" in primary_link or "hls" in primary_link.lower() or "playlist-mpl" in primary_link
    ) else True

    return {
        "title": title,
        "class_link": primary_link,
        "mp4Recordings": mp4_clean if include_mp4s else [],
        "classPdf": pdf_clean
    }


def build_txt_for_course(course_id, course_title=None):
    """Build TXT content and summary for a course."""
    ok, classes = get_course_classes(course_id)
    batches_ok, batches = get_active_batches()

    if not ok:
        return False, "ERROR: Failed to fetch classes for this course.", {}

    items_to_process = []
    try:
        if (
            isinstance(classes, list)
            and classes
            and isinstance(classes[0], dict)
            and classes[0].get("topicName")
            and classes[0].get("classes")
        ):
            for topic_block in classes:
                for cls in topic_block.get("classes", []):
                    items_to_process.append(cls)
        else:
            items_to_process = classes if isinstance(classes, list) else []
    except Exception:
        items_to_process = classes if isinstance(classes, list) else []

    lines = []
    total_videos = 0
    total_mp4 = 0
    total_m3u8 = 0
    total_youtube = 0
    total_pdfs = 0

    for cls in items_to_process:
        normalized = normalize_video_entries(cls)
        title = normalized.get("title", "Untitled")
        subject = _extract_subject_from_title(title, fallback=(course_title or "Course"))

        primary = normalized.get("class_link") or ""
        if primary:
            lines.append(f"[{subject}] {title} : {primary}")
            total_videos += 1
            u = primary.lower()
            if "m3u8" in u or "playlist" in u or "hls" in u:
                total_m3u8 += 1
            elif "youtube" in u:
                total_youtube += 1
            else:
                total_mp4 += 1
        elif normalized.get("mp4Recordings"):
            for m in normalized.get("mp4Recordings"):
                lines.append(f"[{subject}] {title} : {m}")
                total_videos += 1
                total_mp4 += 1

        for p in normalized.get("classPdf", []):
            lines.append(f"[{subject}] {title} : {p}")
            total_pdfs += 1

    # course level pdfs
    course_level_pdfs = find_pdf_from_active(course_id, batches if batches_ok else None)
    if isinstance(course_level_pdfs, str):
        if course_level_pdfs and course_level_pdfs.lower() != "no pdf":
            course_level_pdfs = [u.strip() for u in re.split(r"[\n,;]+", course_level_pdfs) if u.strip()]
        else:
            course_level_pdfs = []

    if isinstance(course_level_pdfs, list) and course_level_pdfs:
        subj = course_title or "Course"
        for p in course_level_pdfs:
            lines.append(f"[{subj}] {subj} : {p}")
            total_pdfs += 1

    txt_content = "\n".join(lines)
    summary_text = (
        f"📊 Export Summary:\n"
        f"🔗 Total Links: {len(lines)}\n"
        f"🎬 Videos: {total_videos}\n"
        f"📄 PDFs: {total_pdfs}"
    )
    txt_content += "\n\n" + summary_text

    summary_dict = {
        "total_links": len(lines),
        "total_videos": total_videos,
        "total_mp4": total_mp4,
        "total_m3u8": total_m3u8,
        "total_youtube": total_youtube,
        "total_pdfs": total_pdfs,
        "summary_text": summary_text
    }

    return True, txt_content, summary_dict


# ---------------- PAGINATION UI ----------------
def send_batch_list(chat_id, page=0, message_id=None):
    """Send paginated batch list (10 per page) with Next/Previous buttons."""
    batches = user_batches_list.get(chat_id, [])
    if not batches:
        safe_send(bot.send_message, chat_id, "❌ No batches found. Try again later.")
        return

    total = len(batches)
    total_pages = (total - 1) // PAGE_SIZE + 1

    if page < 0:
        page = 0
    if page > total_pages - 1:
        page = total_pages - 1

    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_batches = batches[start_idx:end_idx]

    msg_lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        " *WELCOME TO YOUR COURSE HUB!* ",
        " *Select your batch from below:* ",
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for i, b in enumerate(page_batches, start=start_idx + 1):
        title = b.get("title") or b.get("name") or "Untitled"
        bid = b.get("id") or b.get("_id") or ""
        msg_lines.append(f"📌 *{i}. {title}*")
        msg_lines.append(f"   🆔 Batch ID: `{bid}`")
        msg_lines.append("────────────────────────")

    msg_lines.append(f"\n📄 Page {page + 1} of {total_pages}")
    msg_lines.append("✨ Send the *Batch ID* to continue.")
    msg_lines.append("💡 Tip: Copy the Batch ID above to avoid mistakes!")
    msg_lines.append("━━━━━━━━━━━━━━━━━━━")

    text = "\n".join(msg_lines)

    kb = types.InlineKeyboardMarkup()
    buttons = []
    if page > 0:
        buttons.append(types.InlineKeyboardButton("⬅️ Previous", callback_data=f"page:{page - 1}"))
    if page < total_pages - 1:
        buttons.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"page:{page + 1}"))
    if buttons:
        kb.row(*buttons)  # left-right layout

    if message_id is None:
        print(f"📤 Sending batch list page {page+1} to chat_id={chat_id}")
        safe_send(bot.send_message, chat_id, text, parse_mode="Markdown", reply_markup=kb)
    else:
        print(f"✏️ Editing batch list message to page {page+1} (chat_id={chat_id})")
        try:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="Markdown",
                reply_markup=kb
            )
        except ApiTelegramException:
            logging.exception("edit_message_text failed, sending new message")
            safe_send(bot.send_message, chat_id, text, parse_mode="Markdown", reply_markup=kb)


# ---------------- FORCE SUB CONFIG ----------------
CHANNEL_ID = -1003339107224   # your private channel id
INVITE_LINK = "https://t.me/sujaluse"   # your private invite link



# ---------------- BOT HANDLERS ----------------
@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id
    print(f"⚡ /start from chat_id={chat_id}")
    logging.info(f"/start from chat_id={chat_id}")

    # ===== FORCE SUBSCRIPTION CHECK =====
    try:
        member = bot.get_chat_member(CHANNEL_ID, chat_id)
        status = member.status
    except Exception:
        status = None

    if status not in ["member", "administrator", "creator"]:
        join_btn = InlineKeyboardMarkup()
        join_btn.add(
            InlineKeyboardButton(
                "💥 Join Our Channel 💥",
                url=INVITE_LINK
            )
        )
        bot.send_message(
            chat_id,
            "🔴 To use this bot, please join our channel first.\n\nAfter joining, click /start",
            reply_markup=join_btn
        )
        return
    # ===== END FORCE SUBSCRIPTION =====

    ok, batches = get_active_batches()
    if not ok or not batches:
        bot.send_message(chat_id, "❌ *Unable to fetch batch list. Try again later.*", parse_mode="Markdown")
        return

    user_batches[chat_id] = {str(b.get("id") or b.get("_id")): b for b in batches}
    user_batches_list[chat_id] = batches
    user_state[chat_id] = "await_course_id"

    send_batch_list(chat_id, page=0)



@bot.callback_query_handler(func=lambda call: isinstance(call.data, str) and call.data.startswith("page:"))
def handle_page_callback(call):
    chat_id = call.message.chat.id

    # OPTIONAL: Pagination par bhi force check (recommended)
    try:
        member = bot.get_chat_member(CHANNEL_ID, chat_id)
        if member.status not in ["member", "administrator", "creator"]:
            join_btn = InlineKeyboardMarkup()
            join_btn.add(InlineKeyboardButton("💥 Join Our Channel 💥", url=INVITE_LINK))
            bot.answer_callback_query(call.id, "Please join the channel first!", show_alert=True)
            bot.send_message(chat_id, "🔴 You must join the channel to continue.", reply_markup=join_btn)
            return
    except:
        return

    try:
        page = int(call.data.split(":", 1)[1])
    except Exception:
        return

    print(f"📲 Pagination callback chat_id={chat_id}, page={page}")
    logging.info(f"Pagination callback chat_id={chat_id}, page={page}")
    send_batch_list(chat_id, page=page, message_id=call.message.message_id)


@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "await_course_id")
def handle_course_id(message):
    chat_id = message.chat.id
    batch_id = (message.text or "").strip()
    print(f"📥 Batch ID from chat_id={chat_id}: {batch_id}")
    logging.info(f"Batch ID from chat_id={chat_id}: {batch_id}")

    if not batch_id:
        bot.reply_to(message, "❌ Please send a valid Batch ID (string).")
        return

    selected = user_batches.get(chat_id, {}).get(batch_id)
    if not selected:
        bot.reply_to(message, f"❌ Invalid Batch ID: {batch_id}. Make sure it's exact.")
        return

    user_selected[chat_id] = selected
    course_title = selected.get("title") or "Course"
    bot.send_message(chat_id, "⏳ Fetching course data...")

    ok, txt, summary = build_txt_for_course(batch_id, course_title=course_title)
    if not ok:
        bot.send_message(chat_id, f"❌ Failed to fetch course data for ID: {batch_id}")
        return

    tmp_path = None
    try:
        safe_title = re.sub(r"[^\w\s-]", "", course_title).strip().replace(" ", "_")
        tmp_file_name = f"𓍯𝙎𝙪𝙟𝙖𝙡⚝{safe_title}.txt"
        tmp_path = os.path.join(tempfile.gettempdir(), tmp_file_name)
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.write(txt)


        THUMBNAIL_PATH = "thumbnail.jpg" 

        with open(tmp_path, "rb") as doc, open(THUMBNAIL_PATH, "rb") as thumb:
            bot.send_document(
                chat_id,
                doc,
                thumb=thumb,
                caption=f"Batch Name:- {course_title}\n\n{summary.get('summary_text','')}"
            )
    except Exception:
        logging.exception("Error sending document")
        bot.send_message(chat_id, "❌ Error while preparing/sending file.")
    finally:
        try:
            if tmp_path and Path(tmp_path).exists():
                os.remove(tmp_path)
        except Exception:
            pass

    user_state[chat_id] = None
    user_selected.pop(chat_id, None)
    user_batches.pop(chat_id, None)
    user_batches_list.pop(chat_id, None)


@bot.message_handler(func=lambda m: True)
def fallback(message):
    chat_id = message.chat.id
    print(f"💬 Fallback msg from {chat_id}: {message.text!r}")
    logging.info(f"Fallback msg from {chat_id}: {message.text!r}")
    bot.send_message(
        chat_id,
        "Use /start to list batches and export a course. If you're in the flow, follow instructions."
    )


# ---------------- BOT START HELPERS (for wsgi.py) ----------------
def remove_webhook_at_start():
    try:
        bot.remove_webhook()
        print("🧹 Webhook removed (switching to polling).")
        logging.info("Webhook removed (polling mode).")
    except Exception:
        logging.exception("Failed to remove webhook")


def start_bot():
    """Blocking polling loop (used by background thread)."""
    remove_webhook_at_start()
    print("▶ Starting bot.infinity_polling()...")
    logging.info("Starting bot.infinity_polling()")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception:
        logging.exception("bot.infinity_polling crashed")


def start_background_bot():
    """
    Public function for wsgi.py:
        from main import app, start_background_bot
        start_background_bot()
    """
    print("🚀 start_background_bot() called - launching polling thread...")
    logging.info("start_background_bot() called")
    t = Thread(target=start_bot, daemon=True)
    t.start()
    return t


# ---------------- LOCAL RUN ----------------
if __name__ == "__main__":
    # Local run: start bot thread + Flask
    start_background_bot()
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 Running Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
