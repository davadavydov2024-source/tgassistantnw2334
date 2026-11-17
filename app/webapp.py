"""Мини-приложение Telegram (Web App): подключение с инструкцией, профиль/тарифы и веб-админка.
Включается, если в .env задан WEBAPP_URL (публичный https-адрес). Все запросы подписаны Telegram (initData)."""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import InlineKeyboardButton, WebAppInfo
from aiohttp import web

from . import config, db, subtools

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def url(tab: str = "") -> str:
    return f"{config.WEBAPP_URL}/?tab={tab}" if tab else f"{config.WEBAPP_URL}/"


def button(text: str, tab: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url(tab)))


def _user_from_init(raw: str):
    data = dict(parse_qsl(raw, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got)
    if not ok or time.time() - int(data.get("auth_date", "0") or 0) > 86400:
        return None
    try:
        return json.loads(data["user"])
    except Exception:
        return None


@web.middleware
async def _mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = _user_from_init(request.headers.get("X-Init-Data", ""))
        if not user:
            return web.json_response({"error": "Откройте приложение из Telegram"}, status=401)
        if user["id"] in db.BANNED:
            return web.json_response({"error": "Доступ закрыт"}, status=403)
        request["uid"], request["tg"] = user["id"], user
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        log.exception("webapp error")
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)


def _staff(request):
    if not db.is_staff(request["uid"]):
        raise web.HTTPForbidden(text=json.dumps({"error": "Нет доступа"}), content_type="application/json")


def _admin(request):
    if request["uid"] != config.ADMIN_ID:
        raise web.HTTPForbidden(text=json.dumps({"error": "Только для главного админа"}), content_type="application/json")


async def _subs(uid: int) -> list:
    return [{"tier": t, "name": subtools.NAME[t], "until": u} for t, u in sorted((await db.active_subs(uid)).items())]


async def api_me(request):
    uid = request["uid"]
    row = await db.fetchone("SELECT COUNT(*) c FROM connections WHERE user_id=?", (uid,))
    return web.json_response({
        "id": uid, "name": request["tg"].get("first_name", ""), "bot": request.app["bot_username"],
        "connected": row["c"], "tier": await db.get_tier(uid), "subs": await _subs(uid),
        "staff": db.is_staff(uid), "admin": uid == config.ADMIN_ID,
        "prices": config.PRICES, "sub_days": config.SUB_DAYS, "maint": db.STATE["maint"],
    })


async def api_overview(request):
    _staff(request)
    now = time.time()
    one = lambda sql, a=(): db.fetchone(sql, a)
    users = (await one("SELECT COUNT(DISTINCT user_id) c FROM connections"))["c"]
    active = (await one("SELECT COUNT(*) c FROM settings WHERE key='last_seen' AND CAST(value AS REAL) > ?", (now - 86400,)))["c"]
    by = {r["tier"]: r["c"] for r in await db.fetchall("SELECT tier, COUNT(*) c FROM subscriptions WHERE until > ? GROUP BY tier", (now,))}
    stars = (await one("SELECT COALESCE(SUM(stars),0) c FROM payments WHERE payload NOT LIKE '%:refunded'"))["c"]
    top = await db.fetchall("SELECT cmd, SUM(n) n FROM cmdstat GROUP BY cmd ORDER BY n DESC LIMIT 5")
    log_rows = await db.recent_log(15)
    return web.json_response({
        "users": users, "active24": active, "premium": by.get(1, 0), "vip": by.get(2, 0), "banned": len(db.BANNED),
        "mods": len(db.MODS), "stars": stars, "maint": db.STATE["maint"],
        "top": [{"cmd": r["cmd"], "n": r["n"]} for r in top],
        "log": [{"ts": r["ts"], "admin": r["admin_id"], "text": r["text"]} for r in log_rows],
    })


async def api_users(request):
    _staff(request)
    q = request.query.get("q", "").strip().lstrip("@")
    sql = "SELECT user_id, MAX(first_name) f, MAX(last_name) l, MAX(username) u, MAX(ts) ts FROM connections GROUP BY user_id"
    args = ()
    if q:
        if q.isdigit():
            sql += " HAVING user_id=?"
            args = (int(q),)
        else:
            sql += " HAVING LOWER(COALESCE(MAX(username),'')||' '||COALESCE(MAX(first_name),'')||' '||COALESCE(MAX(last_name),'')) LIKE ?"
            args = (f"%{q.lower()}%",)
    rows = await db.fetchall(sql + " ORDER BY ts DESC LIMIT 40", args)
    out = [{"id": r["user_id"], "name": f"{r['f'] or ''} {r['l'] or ''}".strip() or "—", "user": r["u"] or "",
            "tier": await db.get_tier(r["user_id"]), "banned": r["user_id"] in db.BANNED} for r in rows]
    return web.json_response({"users": out})


async def api_user(request):
    _staff(request)
    uid = int(request.query.get("id", "0"))
    row = await db.fetchone("SELECT MAX(first_name) f, MAX(last_name) l, MAX(username) u, COUNT(*) c FROM connections WHERE user_id=?", (uid,))
    msgs = (await db.fetchone("SELECT COUNT(*) c FROM msgs m JOIN connections c ON c.conn_id=m.conn_id WHERE c.user_id=?", (uid,)))["c"]
    seen = await db.get_s(uid, "last_seen")
    return web.json_response({
        "id": uid, "name": f"{row['f'] or ''} {row['l'] or ''}".strip() or "—", "user": row["u"] or "", "conns": row["c"],
        "msgs": msgs, "seen": float(seen) if seen else None, "subs": await _subs(uid),
        "banned": uid in db.BANNED, "mod": uid in db.MODS, "is_admin": uid == config.ADMIN_ID,
    })


async def api_action(request):
    _admin(request)  # менять подписки и банить может только главный админ (мини-админам — просмотр)
    d = await request.json()
    uid, act = int(d.get("id", 0)), d.get("action", "")
    if not uid or uid == config.ADMIN_ID:
        return web.json_response({"error": "Нельзя применить к этому ID"}, status=400)
    note = ""
    if act == "give":
        items = [(t, max(1, min(int(d.get("days", 30)), 3650))) for t in subtools.WORDS.get(str(d.get("tier", "")), [])]
        if not items:
            return web.json_response({"error": "Неизвестный тариф"}, status=400)
        await db.grant_many(uid, items)
        note = f"выдал {subtools.fmt_items(items)}"
    elif act == "revoke":
        await db.revoke(uid)
        note = "снял подписки"
    elif act == "ban":
        await db.ban(uid, "из мини-приложения")
        note = "бан"
    elif act == "unban":
        await db.unban(uid)
        note = "разбан"
    elif act == "mod_add":
        await db.add_mod(uid)
        note = "назначил мини-админа"
    elif act == "mod_del":
        await db.del_mod(uid)
        note = "снял мини-админа"
    else:
        return web.json_response({"error": "Неизвестное действие"}, status=400)
    await db.alog(request["uid"], f"[web] {uid}: {note}")
    return web.json_response({"ok": True, "note": note})


async def api_maint(request):
    _admin(request)
    on = bool((await request.json()).get("on"))
    await db.set_maint(on)
    await db.alog(request["uid"], f"[web] техработы: {'вкл' if on else 'выкл'}")
    return web.json_response({"ok": True, "maint": on})


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def start(bot):
    """Запускает веб-сервер рядом с ботом. Без WEBAPP_URL ничего не делает."""
    if not config.WEBAPP_URL:
        return None
    app = web.Application(middlewares=[_mw])
    app["bot_username"] = (await bot.me()).username
    app.add_routes([
        web.get("/", index), web.get("/api/me", api_me), web.get("/api/admin/overview", api_overview),
        web.get("/api/admin/users", api_users), web.get("/api/admin/user", api_user),
        web.post("/api/admin/action", api_action), web.post("/api/admin/maint", api_maint),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    log.info("Мини-приложение: %s (порт %s)", config.WEBAPP_URL, config.WEB_PORT)
    return runner
