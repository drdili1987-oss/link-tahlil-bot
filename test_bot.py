import asyncio, time, types as pytypes
from unittest.mock import AsyncMock, MagicMock
from google.genai import types
import bot

def msg(text, entities=()):
    m = MagicMock(); m.text = text; m.caption = None; m.entities = list(entities); m.caption_entities = None
    return m

def test_extract():
    m = msg("Qarang: https://kun.uz/news/1, va (https://youtu.be/abc). takror https://kun.uz/news/1 ftp://x.uz")
    assert bot.extract_urls(m) == ["https://kun.uz/news/1", "https://youtu.be/abc"]
    e = MagicMock(type="text_link", url="https://gazeta.uz/a")
    assert bot.extract_urls(msg("bu yerda", [e])) == ["https://gazeta.uz/a"]
    assert bot.extract_urls(msg("link yo'q")) == []
    assert len(bot.extract_urls(msg(" ".join(f"https://a.uz/{i}" for i in range(9))))) == bot.MAX_URLS

def test_youtube():
    assert bot.is_youtube("https://www.youtube.com/watch?v=x") and bot.is_youtube("https://youtu.be/x")
    assert not bot.is_youtube("https://notyoutube.com/x")

def test_split():
    t = "\n".join("a" * 100 for _ in range(100))
    ch = bot.split_message(t)
    assert all(len(c) <= 4096 for c in ch) and "".join(ch).replace("\n", "") == t.replace("\n", "")
    assert bot.split_message("x" * 9000) == ["x" * 4096, "x" * 4096, "x" * 808]

def test_allowed():
    assert bot.parse_allowed("123, 456,abc,") == {123, 456}
    assert bot.parse_allowed(None) == set()

def fake_resp(text, statuses=()):
    meta = types.UrlContextMetadata(url_metadata=[types.UrlMetadata(retrieved_url=u, url_retrieval_status=s) for u, s in statuses])
    r = MagicMock(); r.text = text; r.candidates = [MagicMock(url_context_metadata=meta)]
    return r

def _no_fetch(monkeypatch):
    async def none(url, client): return None
    monkeypatch.setattr(bot, "fetch_page", none)

def test_analyze_mixed(monkeypatch):
    _no_fetch(monkeypatch)
    client = MagicMock()
    calls = []
    async def gen(model, contents, config):
        calls.append((contents, config))
        if isinstance(contents.parts[0].file_data, types.FileData):
            return fake_resp("VIDEO TAHLIL")
        return fake_resp("SAYT TAHLIL", [("https://ok.uz", types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_SUCCESS),
                                         ("https://pay.com", types.UrlRetrievalStatus.URL_RETRIEVAL_STATUS_PAYWALL)])
    client.aio.models.generate_content = gen
    out = asyncio.run(bot.Analyzer(client).analyze(["https://youtu.be/abc", "https://ok.uz", "https://pay.com"]))
    assert "VIDEO TAHLIL" in out and "SAYT TAHLIL" in out and "pay.com: pullik" in out
    web = [c for c in calls if c[0].parts[0].file_data is None][0]
    assert web[1].tools[0].url_context is not None and "https://ok.uz" in web[0].parts[-1].text
    yt = [c for c in calls if c[0].parts[0].file_data is not None][0]
    assert yt[0].parts[0].file_data.file_uri == "https://youtu.be/abc" and not yt[1].tools
    assert web[1].thinking_config.thinking_level == types.ThinkingLevel.LOW

def test_analyze_own_fetch_no_tools(monkeypatch):
    async def page(url, client): return bot.Page(url, title="Sarlavha X", text="Matn " * 200)
    monkeypatch.setattr(bot, "fetch_page", page)
    seen = {}
    async def gen(model, contents, config):
        seen["c"], seen["cfg"] = contents, config
        return fake_resp("TAHLIL")
    client = MagicMock(); client.aio.models.generate_content = gen
    out = asyncio.run(bot.Analyzer(client).analyze(["https://kun.uz/a"]))
    assert out == "TAHLIL" and not seen["cfg"].tools
    assert "Sarlavha X" in seen["c"].parts[1].text

def test_analyze_error(monkeypatch):
    _no_fetch(monkeypatch)
    client = MagicMock()
    async def gen(**kw): raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
    client.aio.models.generate_content = gen
    out = asyncio.run(bot.Analyzer(client).analyze(["https://kun.uz"]))
    assert "limiti tugadi" in out

def test_handler_flow():
    analyzer = MagicMock(); analyzer.analyze = AsyncMock(return_value="NATIJA " + "z" * 5000)
    st = bot.BotState(analyzer, allowed={1})
    ctx = MagicMock(); ctx.application.bot_data = {"state": st}; ctx.bot.send_chat_action = AsyncMock()
    status = MagicMock(); status.edit_text = AsyncMock()
    m = msg("https://kun.uz/x"); m.chat_id = 5; m.reply_text = AsyncMock(return_value=status)
    upd = MagicMock(); upd.effective_message = m; upd.effective_user.id = 1
    asyncio.run(bot.on_message(upd, ctx))
    analyzer.analyze.assert_awaited_once_with(["https://kun.uz/x"])
    status.edit_text.assert_awaited_once()
    assert m.reply_text.await_count == 2          # status + 2-bo'lak
    # cooldown
    asyncio.run(bot.on_message(upd, ctx))
    assert "soniyadan" in m.reply_text.await_args.args[0]
    # begona foydalanuvchi
    upd.effective_user.id = 999
    asyncio.run(bot.on_message(upd, ctx))
    assert "yopiq" in m.reply_text.await_args.args[0]

def test_build_app():
    app = bot.build_app("123:ABC", "k", set())
    assert len(app.handlers[0]) == 2

def test_short_detect_and_unwrap():
    assert bot.is_short("https://share.google/D59aMBfPC0mDoB5AW") and not bot.is_short("https://kun.uz/x")
    assert bot.unwrap_redirect_param("https://www.google.com/url?q=https://kun.uz/a&sa=U") == "https://kun.uz/a"
    assert bot.unwrap_redirect_param("https://kun.uz/?q=https://evil.com") == "https://kun.uz/?q=https://evil.com"

def test_resolve_http_redirect_and_meta():
    import httpx
    def handler(req):
        if req.url.host == "share.google" and req.url.path == "/A":
            return httpx.Response(302, headers={"location": "https://www.google.com/url?q=https://kun.uz/news/7"})
        if req.url.host == "www.google.com":
            return httpx.Response(302, headers={"location": "https://kun.uz/news/7"})
        if req.url.host == "share.google" and req.url.path == "/B":
            return httpx.Response(200, text='<meta http-equiv="refresh" content="0;url=https://gazeta.uz/x?a=1&amp;b=2">')
        if req.url.host == "share.google":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="ok")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as c:
            a = await bot.resolve_url("https://share.google/A", c)
            b = await bot.resolve_url("https://share.google/B", c)
            e = await bot.resolve_url("https://share.google/C", c)
            n = await bot.resolve_url("https://kun.uz/z", c)
        return a, b, e, n
    a, b, e, n = asyncio.run(run())
    assert a == "https://kun.uz/news/7"
    assert b == "https://gazeta.uz/x?a=1&b=2"
    assert e == "https://share.google/C" and n == "https://kun.uz/z"

def test_friendly_model_error():
    e = RuntimeError("404 NOT_FOUND. This model models/gemini-2.5-flash is no longer available to new users.")
    assert "modeli eskirgan" in bot.friendly_error(e)

def test_retry_then_fallback(monkeypatch):
    _no_fetch(monkeypatch)
    monkeypatch.setattr(bot, "RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(bot, "FALLBACK_MODEL", "fallback-m")
    used = []
    async def gen(model, contents, config):
        used.append(model)
        if model != "fallback-m":
            raise RuntimeError("503 UNAVAILABLE high demand")
        return fake_resp("OK")
    client = MagicMock(); client.aio.models.generate_content = gen
    out = asyncio.run(bot.Analyzer(client, model="main-m").analyze(["https://kun.uz"]))
    assert out.startswith("OK") and used == ["main-m"] * 3 + ["fallback-m"]

def test_non_transient_no_retry(monkeypatch):
    _no_fetch(monkeypatch)
    monkeypatch.setattr(bot, "RETRY_DELAYS_S", (0, 0))
    used = []
    async def gen(model, contents, config):
        used.append(model); raise RuntimeError("403 PERMISSION_DENIED api key")
    client = MagicMock(); client.aio.models.generate_content = gen
    out = asyncio.run(bot.Analyzer(client, model="main-m").analyze(["https://kun.uz"]))
    assert "kaliti" in out and used == ["main-m"]


def test_extract_and_clean():
    ex = bot._TextExtractor()
    ex.feed("<html><head><title>T &amp; Q</title><script>var x=1</script></head><body><nav>menu</nav><p>Birinchi gap.</p><p>Ikkinchi</p></body></html>")
    assert ex.title == "T & Q" and "var x" not in ex.text() and "menu" not in ex.text() and "Birinchi gap." in ex.text()
    assert bot.clean_url("https://9to5google.com/a/?shem=dsdf,x&utm_source=y&id=5") == "https://9to5google.com/a/?id=5"
    assert bot.clean_url("https://kun.uz/a") == "https://kun.uz/a"

def test_fetch_page_html_pdf_short():
    import httpx
    long = "<html><title>Yangilik</title><body>" + "<p>Bu juda muhim yangilik matni.</p>" * 30 + "</body></html>"
    def h(req):
        if req.url.path == "/html": return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=long)
        if req.url.path == "/pdf": return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 x")
        if req.url.path == "/js": return httpx.Response(200, headers={"content-type": "text/html"}, text="<div id=app></div>")
        return httpx.Response(404)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return [await bot.fetch_page(f"https://x.uz/{p}", c) for p in ("html", "pdf", "js", "nope")]
    a, b, js, nope = asyncio.run(run())
    assert a.title == "Yangilik" and "muhim yangilik" in a.text
    assert b.pdf.startswith(b"%PDF") and js is None and nope is None

def test_total_budget(monkeypatch):
    _no_fetch(monkeypatch)
    monkeypatch.setattr(bot, "RETRY_DELAYS_S", (0, 0))
    monkeypatch.setattr(bot, "TOTAL_BUDGET_S", 6)
    async def gen(model, contents, config):
        await asyncio.sleep(10)
    client = MagicMock(); client.aio.models.generate_content = gen
    t0 = time.monotonic()
    out = asyncio.run(bot.Analyzer(client).analyze(["https://kun.uz"]))
    assert time.monotonic() - t0 < 8 and "vaqt tugadi" in out
