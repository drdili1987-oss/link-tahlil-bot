"""Telegram bot: link yuborasiz -> Gemini ichini o'qib, o'zbekcha qisqa tahlil qaytaradi.

Muhit o'zgaruvchilari (.env):
  TELEGRAM_BOT_TOKEN  - @BotFather bergan token (majburiy)
  GEMINI_API_KEY      - Google AI Studio kaliti (majburiy)
  ALLOWED_USER_IDS    - ruxsat etilgan Telegram ID'lar, vergul bilan (bo'sh = hamma uchun ochiq)
  GEMINI_MODEL        - model nomi (standart: gemini-3.8-flash)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Message, Update
from telegram.constants import ChatAction, MessageEntityType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx URL'da bot tokenini log qiladi
log = logging.getLogger("linkbot")

# ---------------------------------------------------------------- sozlamalar
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_URLS = 5                 # bitta xabardan ko'pi bilan nechta link
GEMINI_TIMEOUT_S = 120       # YouTube tahlili uzoq davom etishi mumkin
TG_LIMIT = 4096              # Telegram xabar uzunligi chegarasi
COOLDOWN_S = 10              # bitta foydalanuvchi uchun so'rovlar orasidagi minimal vaqt

URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
# Qisqa/yo'naltiruvchi havolalar: Gemini ularni ocha olmaydi, shuning uchun bot avval asl manzilni topadi.
SHORT_HOSTS = {"share.google", "g.co", "goo.gl", "maps.app.goo.gl", "search.app", "bit.ly", "t.co",
               "tinyurl.com", "cutt.ly", "is.gd", "clck.ru", "vk.cc", "rb.gy", "shorturl.at", "ow.ly", "lnkd.in"}
REDIRECT_PARAMS = ("url", "q", "u", "link", "target")
META_REFRESH_RE = re.compile(r"""(?:url=|href=["']|location(?:\.href)?\s*=\s*["'])\s*(https?://[^"'\s<>;]+)""", re.I)
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"

PROMPT = """Sen tahlilchi yordamchisan. Quyidagi manba(lar) ichidagi ma'lumotni o'qib, o'zbek tilida (lotin yozuvida) qisqa tahlil yoz.

Format (oddiy matn, Markdown belgilarisiz — **, #, _ ishlatma):
📌 Mavzu: bir gapda nima haqida
🧩 Asosiy fikrlar:
• 3–6 ta eng muhim fikr, har biri bir qatorda
📊 Muhim raqam va faktlar: bo'lsa, ko'rsat; bo'lmasa bu bo'limni tashlab ket
✅ Xulosa: 1–2 gap — bu kim uchun va nima uchun foydali
⚠️ Diqqat: manba reklama, bir tomonlama yoki shubhali bo'lsa, ayt; aks holda bu bo'limni tashlab ket

Qoidalar:
- Faqat manbada bor narsani yoz, o'ylab topma.
- Manbani ocha olmasang yoki ichi bo'sh bo'lsa, buni ochiq ayt va tahlil qilma.
- Jami 1200 belgidan oshmasin.
"""

USER_ERRORS = {
    types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_PAYWALL: "pullik (paywall) sahifa",
    types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_UNSAFE: "xavfsizlik tekshiruvidan o'tmadi",
    types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_ERROR: "sahifani ochib bo'lmadi",
}


# ---------------------------------------------------------------- yordamchi funksiyalar
def extract_urls(message: Message) -> list[str]:
    """Xabar matnidan va yashirin havolalardan (text_link) URL'larni ajratadi, takrorsiz, tartibni saqlab."""
    text = message.text or message.caption or ""
    found: list[str] = []
    for ent in (message.entities or message.caption_entities or ()):
        if ent.type == MessageEntityType.TEXT_LINK and ent.url:
            found.append(ent.url)
    found += [u.rstrip(".,;:!?)]}") for u in URL_RE.findall(text)]

    out: list[str] = []
    for u in found:
        p = urlparse(u)
        if p.scheme in ("http", "https") and p.netloc and u not in out:
            out.append(u)
    return out[:MAX_URLS]


def is_youtube(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


def is_short(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in SHORT_HOSTS


def unwrap_redirect_param(url: str) -> str:
    """google.com/url?q=https://... kabi o'ram havoladan ichki manzilni chiqaradi."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith(("google.", "www.google.")) or host in SHORT_HOSTS:
        qs = parse_qs(p.query)
        for key in REDIRECT_PARAMS:
            for v in qs.get(key, []):
                if v.startswith(("http://", "https://")):
                    return v
    return url


async def resolve_url(url: str, client: httpx.AsyncClient) -> str:
    """Qisqa havolani asl manzilga aylantiradi. Uddalay olmasa — o'zgarishsiz qaytaradi."""
    if not is_short(url):
        return url
    try:
        async with client.stream("GET", url) as resp:
            final = str(resp.url)
            if is_short(final) and resp.status_code == 200:
                # HTTP redirect o'rniga sahifa ichida (meta refresh / JS) yo'naltirsa — boshini o'qiymiz
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > 65536:
                        break
                m = META_REFRESH_RE.search(html.unescape(body.decode("utf-8", "ignore")))
                if m:
                    final = m.group(1)
        final = unwrap_redirect_param(final)
        if final != url:
            log.info("Qisqa havola ochildi: %s -> %s", url, final)
        return final
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Qisqa havolani ochib bo'lmadi %s: %r", url, e)
        return url


async def resolve_all(urls: list[str]) -> list[str]:
    if not any(is_short(u) for u in urls):
        return urls
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=8, timeout=10,
                                 headers={"User-Agent": UA}) as client:
        resolved = await asyncio.gather(*(resolve_url(u, client) for u in urls))
    out: list[str] = []
    for u in resolved:
        if u not in out:
            out.append(u)
    return out


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Matnni Telegram chegarasiga sig'adigan bo'laklarga, iloji boricha qator chegarasida bo'ladi."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:          # qator topilmasa — bo'sh joydan, u ham bo'lmasa qattiq kesamiz
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:          # juda kichik bo'lak qoldirmaslik uchun
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def parse_allowed(raw: str | None) -> set[int]:
    return {int(x) for x in (raw or "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}


@dataclass
class Analyzer:
    client: genai.Client
    model: str = MODEL

    async def analyze(self, urls: list[str]) -> str:
        yt = [u for u in urls if is_youtube(u)]
        web = [u for u in urls if not is_youtube(u)]

        # YouTube'ni url_context o'qimaydi — video file_data sifatida beriladi.
        # Gemini bitta so'rovda bitta YouTube videoni ishonchli qabul qiladi, shuning uchun alohida chaqiramiz.
        tasks = [self._call_youtube(u) for u in yt]
        if web:
            tasks.append(self._call_web(web))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        parts: list[str] = []
        labels = [f"🎬 {u}" for u in yt] + (["🌐 " + ", ".join(web)] if web else [])
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                log.warning("Gemini xatosi (%s): %r", label, res)
                parts.append(f"{label}\n❌ Tahlil qilib bo'lmadi: {friendly_error(res)}")
            else:
                parts.append(f"{label}\n\n{res}" if len(labels) > 1 else res)
        return "\n\n━━━━━━━━━━\n\n".join(parts)

    async def _call_web(self, urls: list[str]) -> str:
        resp = await self._generate(
            contents=PROMPT + "\nManbalar:\n" + "\n".join(urls),
            tools=[types.Tool(url_context=types.UrlContext())],
        )
        text = (resp.text or "").strip()

        failed = []
        meta = resp.candidates[0].url_context_metadata if resp.candidates else None
        for m in (meta.url_metadata if meta and meta.url_metadata else []):
            if m.url_retrieval_status != types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS:
                failed.append(f"• {m.retrieved_url}: {USER_ERRORS.get(m.url_retrieval_status, 'ochilmadi')}")
        if failed:
            note = "⚠️ Ba'zi linklar ochilmadi:\n" + "\n".join(failed)
            text = f"{text}\n\n{note}" if text else note
        return text or "Javob bo'sh qaytdi. Link ochiq (login talab qilmaydigan) sahifa ekanini tekshiring."

    async def _call_youtube(self, url: str) -> str:
        resp = await self._generate(
            contents=types.Content(role="user", parts=[
                types.Part(file_data=types.FileData(file_uri=url)),
                types.Part(text=PROMPT + "\nManba: yuqoridagi YouTube video."),
            ]),
        )
        return (resp.text or "").strip() or "Video tahlili bo'sh qaytdi."

    async def _generate(self, contents, tools=None):
        config = types.GenerateContentConfig(tools=tools, temperature=0.3)
        return await asyncio.wait_for(
            self.client.aio.models.generate_content(model=self.model, contents=contents, config=config),
            timeout=GEMINI_TIMEOUT_S,
        )


def friendly_error(exc: Exception) -> str:
    s = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "vaqt tugadi, keyinroq qayta urinib ko'ring"
    if "429" in s or "quota" in s or "resource_exhausted" in s:
        return "Gemini limiti tugadi, birozdan so'ng urinib ko'ring"
    if "model" in s and ("not_found" in s or "no longer available" in s or "404" in s):
        return "Gemini modeli eskirgan — GEMINI_MODEL sozlamasini yangilash kerak"
    if "api key" in s or "permission" in s or "403" in s:
        return "Gemini kaliti noto'g'ri yoki ruxsat yo'q"
    if "private" in s or "not found" in s or "404" in s:
        return "video/sahifa yopiq yoki topilmadi"
    return "kutilmagan xato"


# ---------------------------------------------------------------- Telegram handlerlar
@dataclass
class BotState:
    analyzer: Analyzer
    allowed: set[int]
    last_call: dict[int, float] = field(default_factory=dict)


def state(ctx: ContextTypes.DEFAULT_TYPE) -> BotState:
    return ctx.application.bot_data["state"]


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "Menga istalgan link yuboring — maqola, sayt sahifasi yoki YouTube video. "
        "Ichini o'qib, qisqa tahlil qilib beraman.\n\n"
        f"Bitta xabarda {MAX_URLS} tagacha link yuborish mumkin.\n"
        "Sizning ID: " + str(update.effective_user.id)
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    st = state(ctx)

    if st.allowed and user.id not in st.allowed:
        await msg.reply_text(f"⛔ Bu bot yopiq. Sizning ID: {user.id} — admin ruxsat bersin.")
        return

    urls = extract_urls(msg)
    if not urls:
        await msg.reply_text("Link topilmadi. https:// bilan boshlanadigan havola yuboring.")
        return

    now = time.monotonic()
    wait = COOLDOWN_S - (now - st.last_call.get(user.id, 0))
    if wait > 0:
        await msg.reply_text(f"⏳ {int(wait) + 1} soniyadan keyin yuboring.")
        return
    st.last_call[user.id] = now

    status = await msg.reply_text(f"🔎 {len(urls)} ta link o'qilyapti…")
    typing = asyncio.create_task(keep_typing(ctx, msg.chat_id))
    try:
        urls = await resolve_all(urls)
        result = await st.analyzer.analyze(urls)
    finally:
        typing.cancel()
    if any(is_short(u) for u in urls):
        result += ("\n\nℹ️ Qisqa havolani (share.google va h.k.) ochib bo'lmadi. Uni brauzerda oching "
                   "va manzil qatoridagi to'liq linkni yuboring.")

    chunks = split_message(result)
    await status.edit_text(chunks[0], disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, disable_web_page_preview=True)


async def keep_typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Uzoq tahlil paytida 'yozmoqda…' holatini ushlab turadi (Telegram uni 5 s dan keyin o'chiradi)."""
    try:
        while True:
            await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        pass
    except Exception as e:  # typing muhim emas — asosiy javobni to'xtatmasin
        log.debug("typing: %r", e)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Handler xatosi", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Ichki xato yuz berdi. Qayta urinib ko'ring.")
        except Exception:
            pass


def build_app(token: str, api_key: str, allowed: set[int]) -> Application:
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.bot_data["state"] = BotState(Analyzer(genai.Client(api_key=api_key)), allowed)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    token, api_key = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("GEMINI_API_KEY")
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("GEMINI_API_KEY", api_key)) if not v]
    if missing:
        raise SystemExit("Yetishmayapti: " + ", ".join(missing) + " (.env faylini to'ldiring)")
    allowed = parse_allowed(os.getenv("ALLOWED_USER_IDS"))
    if not allowed:
        log.warning("ALLOWED_USER_IDS bo'sh — bot HAMMA uchun ochiq, Gemini limiti begonalarga sarflanishi mumkin")
    app = build_app(token, api_key, allowed)
    # Render RENDER_EXTERNAL_URL ni o'zi beradi -> webhook rejimi (bepul web service uxlab qolsa ham
    # Telegram xabari uni uyg'otadi). Kompyuterda bu o'zgaruvchi yo'q -> oddiy polling.
    base_url = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        secret = os.getenv("WEBHOOK_SECRET") or None
        port = int(os.getenv("PORT", "10000"))
        log.info("Webhook rejimi: %s/webhook (port %s). Model: %s", base_url, port, MODEL)
        app.run_webhook(listen="0.0.0.0", port=port, url_path="webhook",
                        webhook_url=f"{base_url.rstrip('/')}/webhook", secret_token=secret,
                        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    else:
        log.info("Polling rejimi. Model: %s", MODEL)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
