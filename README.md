# Link tahlil boti (Telegram + Gemini)

Botga link yuborasiz — u sahifa yoki YouTube video ichini o'qib, o'zbekcha qisqa tahlil qaytaradi:
📌 Mavzu · 🧩 Asosiy fikrlar · 📊 Raqamlar · ✅ Xulosa · ⚠️ Diqqat.

## Nimalarni o'qiy oladi
| Link turi | Holat |
|---|---|
| Ochiq sayt, maqola, yangilik, blog, PDF | ✅ |
| YouTube video (ochiq) | ✅ (video sifatida tahlil qilinadi) |
| Pullik (paywall) sahifa | ❌ bot "pullik sahifa" deb aytadi |
| Instagram, Facebook, yopiq Telegram kanal — login talab qiladigan sahifalar | ❌ odatda ochilmaydi |
| Google Docs/Sheets | ❌ |

## 1. Kalitlarni olish
1. **Telegram token:** Telegram'da @BotFather → `/newbot` → nom bering → token keladi.
2. **Gemini kaliti:** https://aistudio.google.com/apikey → "Create API key".

⚠️ Token va kalitni hech kimga, hech qayerga (chat, GitHub, skrinshot) tashlamang.

## 2. Kompyuterda ishga tushirish
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env      # Mac/Linux: cp .env.example .env
```
`.env` faylini oching va to'ldiring:
```
TELEGRAM_BOT_TOKEN=12345:ABC...
GEMINI_API_KEY=AIza...
ALLOWED_USER_IDS=
```
Ishga tushiring:
```bash
python bot.py
```
Botga `/start` yozing — u sizning **ID**'ingizni ko'rsatadi. Shu ID'ni (ustozniki bilan birga) `ALLOWED_USER_IDS=111,222` ga yozib, botni qayta ishga tushiring. Aks holda botni topgan har kim sizning Gemini limitingizni ishlatadi.

## 3. Serverda ishlatish (Render, bepul)
Bot Render'da **Web Service** sifatida webhook rejimida ishlaydi: Render `RENDER_EXTERNAL_URL` ni o'zi beradi, bot uni ko'rib Telegram'ga webhook o'rnatadi. Server uxlab qolsa, kelgan xabar uni uyg'otadi (birinchi javob ~1 daqiqa kechikishi mumkin).
- Build: `pip install -r requirements.txt` · Start: `python bot.py`
- Environment: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `ALLOWED_USER_IDS`, `WEBHOOK_SECRET` (ixtiyoriy tasodifiy satr).
Kompyuterda `RENDER_EXTERNAL_URL` yo'q, shuning uchun bot oddiy polling rejimida ishlaydi.

## Qisqa havolalar
`share.google`, `bit.ly`, `goo.gl`, `t.co` kabi havolalarni bot avval o'zi ochib, asl sahifani topadi va Gemini'ga shuni beradi.

## Sozlamalar
- `GEMINI_MODEL` — model nomi (standart `gemini-2.5-flash`).
- `bot.py` boshida: `MAX_URLS` (bir xabarda nechta link), `COOLDOWN_S` (so'rovlar orasidagi vaqt), `PROMPT` (tahlil formati).

## Test
```bash
pip install pytest
python -m pytest -q
```
