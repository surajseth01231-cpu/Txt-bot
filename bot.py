import os
import re
import json
import asyncio
import traceback
from typing import List, Tuple, Optional

import aiohttp
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError, UserIsBlocked
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
)

# ==============================================================================
# CONFIGURATION (Read directly from Render / Environment Variables)
# ==============================================================================
def _parse_api_id() -> int:
    raw = (os.environ.get("API_ID") or "0").strip()
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else 0


API_ID = _parse_api_id()
API_HASH = (os.environ.get("API_HASH") or "").strip()
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

ACCESS_PASSWORD = (os.environ.get("ACCESS_PASSWORD") or "Boss@2011123").strip()
DEFAULT_CREDIT = (os.environ.get("DEFAULT_CREDIT") or "Extracted by @isurajsethh").strip()

CONFIG_FILE = "bot_state.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ==============================================================================
# FAST IN-MEMORY + BACKGROUND STATE STORE (NO BLOCKING DISK I/O ON CLICKS)
# ==============================================================================
def _default_user_data() -> dict:
    return {
        "authenticated": False,
        "state": None,
        "channel_id": None,
        "topic_group_id": None,
        "credit_name": DEFAULT_CREDIT,
        "pending_items": [],
        "pending_quality": "best",
        "stop_requested": False,
        "topic_cache": {},
    }


def load_state() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"users": {}}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "users" not in data:
            return {"users": {}}
        for uid, udata in data["users"].items():
            base = _default_user_data()
            base.update(udata)
            data["users"][uid] = base
        return data
    except Exception:
        try:
            os.replace(CONFIG_FILE, f"{CONFIG_FILE}.bak")
        except Exception:
            pass
        return {"users": {}}


def _write_state_sync(payload: str) -> None:
    tmp_path = f"{CONFIG_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp_path, CONFIG_FILE)


STORE = load_state()
_persist_task: Optional[asyncio.Task] = None


def save_state(state_data: dict) -> None:
    global _persist_task
    try:
        serialized = json.dumps(state_data, ensure_ascii=False, indent=2)
        loop = asyncio.get_running_loop()
        if _persist_task is None or _persist_task.done():
            _persist_task = loop.create_task(asyncio.to_thread(_write_state_sync, serialized))
    except RuntimeError:
        try:
            _write_state_sync(json.dumps(state_data, ensure_ascii=False, indent=2))
        except Exception:
            pass
    except Exception as e:
        print(f"[State Save Warning] {e}")


def get_user(user_id: int) -> dict:
    key = str(user_id)
    if key not in STORE["users"]:
        STORE["users"][key] = _default_user_data()
        save_state(STORE)
    return STORE["users"][key]


def is_unlocked(user_id: int) -> bool:
    return bool(get_user(user_id).get("authenticated"))


# ==============================================================================
# KEYBOARDS (Exact 4 Home Buttons)
# ==============================================================================
def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📢 Set Channel / Group", callback_data="btn_setchannel"),
                InlineKeyboardButton("✍️ Add Your Name", callback_data="btn_addname"),
            ],
            [
                InlineKeyboardButton("📌 Group Topic Transfer", callback_data="btn_topicgroup"),
                InlineKeyboardButton("📂 TXT Extract", callback_data="btn_txtextract"),
            ],
        ]
    )


def quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 360p", callback_data="qual_360"),
                InlineKeyboardButton("🎬 480p", callback_data="qual_480"),
                InlineKeyboardButton("🎬 720p", callback_data="qual_720"),
            ],
            [
                InlineKeyboardButton("⚡ Best Available", callback_data="qual_best"),
            ],
        ]
    )


# ==============================================================================
# UNIVERSAL TXT PARSER & SUBJECT / MEDIA CLASSIFIER
# ==============================================================================
URL_PATTERN = re.compile(r"(https?://\S+)")
KNOWN_SUBJECTS = [
    ("Physics", ["physics", "भौतिक", "भौतिकी", "gravitation", "गति", "ऊर्जा", "मात्रक"]),
    ("Chemistry", ["chemistry", "रसायन", "organic", "mole", "अवधारणा", "तत्व", "मिश्रण"]),
    ("Mathematics", ["maths", "math", "mathematics", "गणित", "algebra", "calculus", "trigonometry"]),
    ("Biology", ["biology", "जीव", "botany", "zoology", "cell", "genetics"]),
    ("Hindi", ["hindi", "हिन्दी", "हिंदी", "व्याकरण", "गद्य", "काव्य"]),
    ("English", ["english", "अंग्रेजी", "grammar", "prose", "poetry"]),
]


def detect_subject(raw_line: str, title: str) -> str:
    brackets = re.findall(r"\(([^()]+)\)", raw_line)
    ignored = {"home", "all study material - 2027", "live class", "dpp & solution", "demo lecture", "test"}

    for b in brackets:
        b_clean = b.strip()
        b_low = b_clean.lower()
        if b_low in ignored or b_low.startswith("all study material"):
            continue
        for subj_name, keywords in KNOWN_SUBJECTS:
            if any(k in b_low for k in keywords):
                return subj_name
        if len(b_clean) <= 30 and not re.search(r"\d+\.", b_clean):
            return b_clean

    combined = f"{raw_line} {title}".lower()
    for subj_name, keywords in KNOWN_SUBJECTS:
        if any(k in combined for k in keywords):
            return subj_name

    return "General"


def classify_media(url: str, raw_line: str, title: str) -> str:
    clean_url = url.split("?")[0].lower()
    text_hint = f"{raw_line} {title}".lower()

    if clean_url.endswith(".pdf") or ".pdf" in clean_url or "notes" in text_hint or "dpp" in text_hint:
        return "pdf"
    if clean_url.endswith((".png", ".jpg", ".jpeg", ".webp")) or "thumbnail" in text_hint:
        return "image"
    if clean_url.endswith((".html", ".htm")) or "test" in text_hint or "quiz" in text_hint or "mock" in text_hint:
        return "html"
    if clean_url.endswith((".m3u8", ".mp4", ".mkv", ".webm", ".ts")) or "/play/" in clean_url or "lecture" in text_hint or "class" in text_hint:
        return "video"
    return "file"


def clean_title_from_line(raw_line: str, url: str) -> str:
    before_url = raw_line.split(url)[0].strip()
    before_url = re.sub(r"[:\-|]+$", "", before_url).strip()
    parts = re.findall(r"\)([^()]+)$", before_url)
    candidate = parts[-1].strip() if parts else before_url
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate if candidate else "Untitled Material"


def parse_txt_content(text: str) -> List[dict]:
    parsed = []
    for idx, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = URL_PATTERN.search(line)
        if not match:
            continue
        url = match.group(1).strip().rstrip(".,;)")
        title = clean_title_from_line(line, url)
        subject = detect_subject(line, title)
        media_type = classify_media(url, line, title)
        parsed.append(
            {
                "index": idx,
                "title": title,
                "url": url,
                "subject": subject,
                "media_type": media_type,
                "raw_line": line,
            }
        )
    return parsed


def sanitize_filename(name: str, max_len: int = 65) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\n\r]+', "_", name).strip(" ._")
    return (cleaned[:max_len] or "file")


# ==============================================================================
# SHARED HTTP SESSION & FAST FILE / VIDEO DOWNLOADERS
# ==============================================================================
_http_session: Optional[aiohttp.ClientSession] = None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=120, connect=20)
        _http_session = aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout)
    return _http_session


async def download_http_file(url: str, dest_path: str) -> Tuple[bool, str]:
    try:
        session = await get_http_session()
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            written = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(262144):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            if written < 32:
                return False, "Empty/tiny response"
            return True, "OK"
    except asyncio.TimeoutError:
        return False, "Download timeout"
    except Exception as e:
        return False, str(e)


async def build_test_html_file(item: dict, credit_name: str, dest_path: str) -> Tuple[bool, str]:
    ok, _ = await download_http_file(item["url"], dest_path)
    if ok and os.path.exists(dest_path):
        try:
            with open(dest_path, "r", encoding="utf-8", errors="ignore") as f:
                sample = f.read(2048).lower()
            if "<html" in sample or "<!doctype html" in sample:
                return True, "OK"
        except Exception:
            pass

    title = item["title"]
    subject = item["subject"]
    url = item["url"]
    html_doc = f"""<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
  .card {{ max-width: 780px; margin: auto; background: #1e293b; border-radius: 12px; padding: 24px; border: 1px solid #334155; }}
  .badge {{ display: inline-block; background: #2563eb; color: #fff; padding: 4px 10px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }}
  h1 {{ font-size: 22px; margin: 8px 0 16px; }}
  .meta {{ color: #94a3b8; font-size: 14px; margin-bottom: 20px; }}
  .btn {{ display: inline-block; background: #16a34a; color: #fff; text-decoration: none; padding: 12px 20px; border-radius: 8px; font-weight: bold; }}
  .footer {{ margin-top: 24px; padding-top: 14px; border-top: 1px solid #334155; color: #cbd5e1; font-size: 13px; }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">{subject} • Test / Study Resource</div>
    <h1>{title}</h1>
    <div class="meta">Source Endpoint: {url}</div>
    <a class="btn" href="{url}" target="_blank" rel="noopener noreferrer">Open Test / Resource</a>
    <div class="footer">{credit_name}</div>
  </div>
</body>
</html>
"""
    try:
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        return True, "OK"
    except Exception as e:
        return False, str(e)


async def download_video_file(url: str, quality: str, base_out_without_ext: str) -> Tuple[Optional[str], str]:
    height_map = {
        "360": "[height<=360]",
        "480": "[height<=480]",
        "720": "[height<=720]",
        "best": "",
    }
    h_filter = height_map.get(quality, "")
    fmt_selector = f"bestvideo{h_filter}+bestaudio/best{h_filter}/best"
    out_template = f"{base_out_without_ext}.%(ext)s"

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--retries", "3",
        "--fragment-retries", "4",
        "--concurrent-fragments", "8",
        "--buffer-size", "16K",
        "-f", fmt_selector,
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        return None, "Video download timed out"
    except Exception as e:
        return None, str(e)

    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = f"{base_out_without_ext}{ext}"
        if os.path.exists(candidate) and os.path.getsize(candidate) > 1024:
            return candidate, "OK"

    prefix = os.path.basename(base_out_without_ext)
    parent = os.path.dirname(base_out_without_ext) or "."
    for fname in os.listdir(parent):
        if fname.startswith(prefix) and not fname.endswith((".part", ".ytdl", ".temp")):
            fpath = os.path.join(parent, fname)
            if os.path.getsize(fpath) > 1024:
                return fpath, "OK"

    err_msg = stderr.decode("utf-8", errors="ignore").strip().splitlines()
    last_err = err_msg[-1] if err_msg else "Stream unavailable or DRM protected"
    return None, last_err


# ==============================================================================
# FORUM TOPIC AUTO-CREATION & UPLOAD ROUTING
# ==============================================================================
async def ensure_forum_topic(client: Client, chat_id: int, topic_name: str, user_data: dict) -> Optional[int]:
    cache = user_data.setdefault("topic_cache", {})
    cache_key = f"{chat_id}:{topic_name.lower()}"
    if cache_key in cache:
        return cache[cache_key]

    try:
        if hasattr(client, "create_forum_topic"):
            topic = await client.create_forum_topic(chat_id=chat_id, title=topic_name[:128])
            thread_id = getattr(topic, "id", None) or getattr(topic, "message_thread_id", None)
            if thread_id:
                cache[cache_key] = int(thread_id)
                save_state(STORE)
                return int(thread_id)
    except Exception as e:
        print(f"[Topic Create Warning for '{topic_name}']: {e}")

    return None


async def send_with_retry(coro_factory, max_retries: int = 4):
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except UserIsBlocked:
            raise
        except RPCError:
            if attempt == max_retries:
                raise
            await asyncio.sleep(1.5 * attempt)
        except Exception:
            if attempt == max_retries:
                raise
            await asyncio.sleep(1.0 * attempt)


async def upload_extracted_item(
    client: Client,
    user_data: dict,
    item: dict,
    quality: str,
) -> Tuple[bool, str]:
    credit = user_data.get("credit_name") or DEFAULT_CREDIT
    caption = f"**{item['title']}**

{credit}"

    topic_group_id = user_data.get("topic_group_id")
    channel_id = user_data.get("channel_id")

    destinations: List[Tuple[int, Optional[int]]] = []
    media_type = item["media_type"]
    subject = item["subject"]

    if topic_group_id:
        subj_thread = await ensure_forum_topic(client, int(topic_group_id), subject, user_data)
        destinations.append((int(topic_group_id), subj_thread))
        if media_type == "pdf":
            notes_thread = await ensure_forum_topic(client, int(topic_group_id), "All Subjects Notes", user_data)
            destinations.append((int(topic_group_id), notes_thread))
    elif channel_id:
        destinations.append((int(channel_id), None))
    else:
        return False, "No channel or topic group configured"

    temp_files: List[str] = []
    base_name = sanitize_filename(f"{item['index']}_{item['title']}")
    base_path = os.path.join(DOWNLOAD_DIR, base_name)

    try:
        if media_type == "pdf":
            file_path = f"{base_path}.pdf"
            temp_files.append(file_path)
            ok, reason = await download_http_file(item["url"], file_path)
            if not ok:
                return False, reason

            for target_id, thread_id in destinations:
                await send_with_retry(
                    lambda tid=target_id, th=thread_id, fp=file_path: client.send_document(
                        chat_id=tid,
                        document=fp,
                        caption=caption,
                        message_thread_id=th,
                    )
                )
            return True, "PDF Uploaded"

        elif media_type == "image":
            ext = ".png"
            for cand_ext in (".png", ".jpg", ".jpeg", ".webp"):
                if cand_ext in item["url"].lower():
                    ext = cand_ext
                    break
            file_path = f"{base_path}{ext}"
            temp_files.append(file_path)
            ok, reason = await download_http_file(item["url"], file_path)
            if not ok:
                return False, reason

            for target_id, thread_id in destinations:
                await send_with_retry(
                    lambda tid=target_id, th=thread_id, fp=file_path: client.send_photo(
                        chat_id=tid,
                        photo=fp,
                        caption=caption,
                        message_thread_id=th,
                    )
                )
            return True, "Photo Uploaded"

        elif media_type == "html":
            file_path = f"{base_path}.html"
            temp_files.append(file_path)
            ok, reason = await build_test_html_file(item, credit, file_path)
            if not ok:
                return False, reason

            for target_id, thread_id in destinations:
                await send_with_retry(
                    lambda tid=target_id, th=thread_id, fp=file_path: client.send_document(
                        chat_id=tid,
                        document=fp,
                        caption=caption,
                        message_thread_id=th,
                    )
                )
            return True, "HTML File Uploaded"

        else:
            vid_path, reason = await download_video_file(item["url"], quality, base_path)
            if vid_path:
                temp_files.append(vid_path)
                for target_id, thread_id in destinations:
                    await send_with_retry(
                        lambda tid=target_id, th=thread_id, vp=vid_path: client.send_video(
                            chat_id=tid,
                            video=vp,
                            caption=caption,
                            supports_streaming=True,
                            message_thread_id=th,
                        )
                    )
                return True, "Video Uploaded"

            html_fallback = f"{base_path}.html"
            temp_files.append(html_fallback)
            await build_test_html_file(item, credit, html_fallback)
            for target_id, thread_id in destinations:
                await send_with_retry(
                    lambda tid=target_id, th=thread_id, hf=html_fallback: client.send_document(
                        chat_id=tid,
                        document=hf,
                        caption=f"**{item['title']}** (Stream Fallback HTML)

{credit}",
                        message_thread_id=th,
                    )
                )
            return False, f"Video fallback HTML sent ({reason})"

    except Exception as e:
        return False, str(e)
    finally:
        for tf in temp_files:
            if tf and os.path.exists(tf):
                try:
                    os.remove(tf)
                except Exception:
                    pass


# ==============================================================================
# FAST BATCH RUNNER WITH THROTTLED LIVE PROGRESS
# ==============================================================================
async def run_extraction_batch(
    client: Client,
    message: Message,
    user_id: int,
    start_idx: int,
    end_idx: int,
    quality: str,
) -> None:
    u = get_user(user_id)
    items: List[dict] = u.get("pending_items") or []
    u["stop_requested"] = False
    u["state"] = None
    save_state(STORE)

    if not items:
        await message.reply_text("No parsed items found. Send a `.txt` file first.")
        return

    total_items = len(items)
    start_idx = max(1, min(start_idx, total_items))
    end_idx = max(start_idx, min(end_idx, total_items))
    batch = items[start_idx - 1 : end_idx]

    status_msg = await message.reply_text(
        f"Starting extraction: **{start_idx} to {end_idx}** (Total: `{total_items}`) | Quality: `{quality}`"
    )

    uploaded_count = 0
    failed_count = 0
    last_ui_update = 0.0

    for offset, item in enumerate(batch, start=start_idx):
        if get_user(user_id).get("stop_requested"):
            await status_msg.edit_text(
                f"Stopped at `#{offset}` | Uploaded: `{uploaded_count}` | Fallback/Issues: `{failed_count}`",
                reply_markup=home_keyboard(),
            )
            return

        now = asyncio.get_running_loop().time()
        if now - last_ui_update >= 3.0 or offset == start_idx:
            last_ui_update = now
            try:
                await status_msg.edit_text(
                    f"`#{offset}/{total_items}` [{item['media_type'].upper()} | {item['subject']}]
"
                    f"**{item['title'][:75]}**
"
                    f"Uploaded: `{uploaded_count}` | Fallback/Issues: `{failed_count}`"
                )
            except Exception:
                pass

        ok, _ = await upload_extracted_item(client, get_user(user_id), item, quality)
        if ok:
            uploaded_count += 1
        else:
            failed_count += 1

        await asyncio.sleep(0.4)

    try:
        await status_msg.edit_text(
            f"Completed `{start_idx}-{end_idx}` of `{total_items}`
"
            f"Uploaded: `{uploaded_count}` | Fallback/Issues: `{failed_count}`",
            reply_markup=home_keyboard(),
        )
    except Exception:
        await message.reply_text(
            f"Completed! Uploaded: `{uploaded_count}` | Fallback/Issues: `{failed_count}`",
            reply_markup=home_keyboard(),
        )


# ==============================================================================
# BOT APPLICATION & INSTANT HANDLERS
# ==============================================================================
app = Client(
    "sanskari_txt_extractor_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=".",
)


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if not is_unlocked(uid):
        u["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u["state"] = None
    save_state(STORE)
    ch = u.get("channel_id") or "Not Set"
    tg = u.get("topic_group_id") or "Not Set"
    cr = u.get("credit_name") or DEFAULT_CREDIT

    await message.reply_text(
        f"**Bot Ready**

"
        f"**Channel/Group:** `{ch}`
"
        f"**Topic Group:** `{tg}`
"
        f"**Credit:** `{cr}`",
        reply_markup=home_keyboard(),
    )


@app.on_message(filters.command("id") & filters.private)
async def cmd_id(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    reply_info = ""
    if message.reply_to_message:
        r = message.reply_to_message
        fwd_id = r.forward_from_chat.id if r.forward_from_chat else None
        reply_info = f"
Replied Chat ID: `{r.chat.id}`" + (f"
Forwarded Chat ID: `{fwd_id}`" if fwd_id else "")

    await message.reply_text(f"User ID: `{uid}`
Chat ID: `{message.chat.id}`{reply_info}")


@app.on_message(filters.command("setchannel") & filters.private)
async def cmd_setchannel(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u = get_user(uid)
    u["state"] = "WAITING_CHANNEL"
    save_state(STORE)
    await message.reply_text("Channel/Group ID bhejein (e.g. `-1001234567890`) ya message forward karein:")


@app.on_message(filters.command("addname") & filters.private)
async def cmd_addname(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u = get_user(uid)
    u["state"] = "WAITING_NAME"
    save_state(STORE)
    await message.reply_text("Apna credit/name bhejein (e.g. `Extracted by @isurajsethh`):")


@app.on_message(filters.command("grouptopictransfer") & filters.private)
async def cmd_grouptopictransfer(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u = get_user(uid)
    u["state"] = "WAITING_TOPIC_GROUP"
    save_state(STORE)
    await message.reply_text("Topic Forum Group ID bhejein (e.g. `-1001234567890`):")


@app.on_message(filters.command("txt") & filters.private)
async def cmd_txt(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u = get_user(uid)
    if not u.get("channel_id") and not u.get("topic_group_id"):
        await message.reply_text("Pehle Channel/Group ya Topic Group set karein.", reply_markup=home_keyboard())
        return

    u["state"] = "WAITING_TXT"
    save_state(STORE)
    await message.reply_text("Apni `.txt` file bhejein:")


@app.on_message(filters.command("stop") & filters.private)
async def cmd_stop(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    u = get_user(uid)
    u["stop_requested"] = True
    u["state"] = None
    save_state(STORE)
    await message.reply_text("Stop requested.", reply_markup=home_keyboard())


@app.on_message(filters.command("reset") & filters.private)
async def cmd_reset(client: Client, message: Message):
    uid = message.from_user.id
    if not is_unlocked(uid):
        get_user(uid)["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    STORE["users"][str(uid)] = {
        "authenticated": True,
        "state": None,
        "channel_id": None,
        "topic_group_id": None,
        "credit_name": DEFAULT_CREDIT,
        "pending_items": [],
        "pending_quality": "best",
        "stop_requested": False,
        "topic_cache": {},
    }
    save_state(STORE)
    await message.reply_text("Reset complete.", reply_markup=home_keyboard())


@app.on_callback_query()
async def on_callback(client: Client, query: CallbackQuery):
    uid = query.from_user.id
    u = get_user(uid)

    if not is_unlocked(uid):
        u["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await query.answer("Password required!", show_alert=True)
        await query.message.reply_text("Password enter karein:")
        return

    data = query.data
    await query.answer()

    if data == "btn_setchannel":
        u["state"] = "WAITING_CHANNEL"
        save_state(STORE)
        await query.message.reply_text("Channel/Group ID bhejein (e.g. `-1001234567890`) ya message forward karein:")

    elif data == "btn_addname":
        u["state"] = "WAITING_NAME"
        save_state(STORE)
        await query.message.reply_text("Apna credit/name bhejein (e.g. `Extracted by @isurajsethh`):")

    elif data == "btn_topicgroup":
        u["state"] = "WAITING_TOPIC_GROUP"
        save_state(STORE)
        await query.message.reply_text("Topic Forum Group ID bhejein (e.g. `-1001234567890`):")

    elif data == "btn_txtextract":
        if not u.get("channel_id") and not u.get("topic_group_id"):
            await query.message.reply_text("Pehle Channel/Group ya Topic Group set karein.", reply_markup=home_keyboard())
            return
        u["state"] = "WAITING_TXT"
        save_state(STORE)
        await query.message.reply_text("Apni `.txt` file bhejein:")

    elif data.startswith("qual_"):
        q_val = data.split("_", 1)[1]
        u["pending_quality"] = q_val
        u["state"] = "WAITING_RANGE"
        save_state(STORE)
        total = len(u.get("pending_items") or [])
        await query.message.reply_text(
            f"Quality `{q_val}` selected. Total: `{total}`.
"
            f"Range likhein (e.g. `1-50` ya `all`):"
        )


@app.on_message(filters.private & filters.text & ~filters.command(["start", "id", "setchannel", "addname", "grouptopictransfer", "txt", "stop", "reset"]))
async def on_text(client: Client, message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    txt = (message.text or "").strip()

    if not is_unlocked(uid):
        if txt == ACCESS_PASSWORD:
            u["authenticated"] = True
            u["state"] = None
            save_state(STORE)
            await message.reply_text("Unlocked!", reply_markup=home_keyboard())
        else:
            u["state"] = "WAITING_PASSWORD"
            save_state(STORE)
            await message.reply_text("Galat password. Sahi password enter karein:")
        return

    state = u.get("state")

    if state == "WAITING_CHANNEL":
        target_id = None
        if message.forward_from_chat:
            target_id = message.forward_from_chat.id
        else:
            try:
                target_id = int(txt)
            except ValueError:
                await message.reply_text("Valid numeric ID bhejein (e.g. `-1001234567890`) ya channel message forward karein:")
                return
        u["channel_id"] = target_id
        u["state"] = None
        save_state(STORE)
        await message.reply_text(f"Channel/Group set: `{target_id}`", reply_markup=home_keyboard())

    elif state == "WAITING_TOPIC_GROUP":
        target_id = None
        if message.forward_from_chat:
            target_id = message.forward_from_chat.id
        else:
            try:
                target_id = int(txt)
            except ValueError:
                await message.reply_text("Valid Forum Group numeric ID bhejein (e.g. `-1001234567890`):")
                return
        u["topic_group_id"] = target_id
        u["state"] = None
        save_state(STORE)
        await message.reply_text(f"Topic Group set: `{target_id}`", reply_markup=home_keyboard())

    elif state == "WAITING_NAME":
        u["credit_name"] = txt
        u["state"] = None
        save_state(STORE)
        await message.reply_text(f"Credit set: `{txt}`", reply_markup=home_keyboard())

    elif state == "WAITING_RANGE":
        items = u.get("pending_items") or []
        total = len(items)
        if not total:
            u["state"] = None
            save_state(STORE)
            await message.reply_text("No items in queue.", reply_markup=home_keyboard())
            return

        if txt.lower() == "all":
            s_idx, e_idx = 1, total
        else:
            m = re.match(r"^(\d+)\s*-\s*(\d+|all)$", txt.lower())
            if not m:
                await message.reply_text(f"Format likhein jaise `1-25` ya `all` (Total `{total}`):")
                return
            s_idx = int(m.group(1))
            e_idx = total if m.group(2) == "all" else int(m.group(2))

        quality = u.get("pending_quality") or "best"
        await run_extraction_batch(client, message, uid, s_idx, e_idx, quality)


@app.on_message(filters.private & filters.document)
async def on_document(client: Client, message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if not is_unlocked(uid):
        u["state"] = "WAITING_PASSWORD"
        save_state(STORE)
        await message.reply_text("Password enter karein:")
        return

    if u.get("state") != "WAITING_TXT":
        return

    if not u.get("channel_id") and not u.get("topic_group_id"):
        await message.reply_text("Pehle Channel/Group ya Topic Group set karein.", reply_markup=home_keyboard())
        return

    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await message.reply_text("Kripya `.txt` file hi bhejein.")
        return

    dl_path = await message.download(file_name=os.path.join(DOWNLOAD_DIR, sanitize_filename(doc.file_name)))
    try:
        with open(dl_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    finally:
        if dl_path and os.path.exists(dl_path):
            os.remove(dl_path)

    items = parse_txt_content(content)
    if not items:
        await message.reply_text("Is `.txt` file mein koi valid link nahi mila.", reply_markup=home_keyboard())
        return

    u["pending_items"] = items
    u["state"] = "WAITING_QUALITY"
    save_state(STORE)

    counts = {"video": 0, "pdf": 0, "html": 0, "image": 0, "file": 0}
    for it in items:
        counts[it["media_type"]] = counts.get(it["media_type"], 0) + 1

    await message.reply_text(
        f"**Found `{len(items)}` items**
"
        f"Videos: `{counts['video']}` | PDFs: `{counts['pdf']}` | HTML/Tests: `{counts['html']}` | Images: `{counts['image']}`

"
        f"Video quality chunein:",
        reply_markup=quality_keyboard(),
    )


async def main():
    while True:
        try:
            if not API_ID or not API_HASH or not BOT_TOKEN:
                print("[Config Error] Set API_ID, API_HASH, and BOT_TOKEN in Render Environment Variables.")
                await asyncio.sleep(10)
                continue

            await app.start()
            print("Sanskari TXT Extractor Bot is running...")
            await asyncio.Event().wait()
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 2)
        except Exception as e:
            print(f"[Main Loop Recovery] {e}")
            traceback.print_exc()
            try:
                if app.is_initialized:
                    await app.stop()
            except Exception:
                pass
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except RuntimeError:
        asyncio.run(main())
