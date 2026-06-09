"""
JPW Auto-Reach Pro — standalone Telegram bot (single file).
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Requirements (install once):
    pip install python-telegram-bot==21.10 requests

Run:
    export BOT_TOKEN="<your main bot token>"
    export LOGS_BOT_TOKEN="<your logs bot token>"       # optional
    python jpw_auto_reach_pro.py

What it does:
    /start  →  Branded login prompt
    User sends `<TechID> <Password>` (any format)
    Bot logs in → finds the in-progress / today's WO → marks Reached.
    Chat auto-clears, prompt comes back, ready for next.
    Logs are mirrored to the Logs Bot (anyone who /starts it).

No database. State is kept in memory (cleanup on restart).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date
from typing import Any

import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

# ============================================================
#  CONFIG
# ============================================================
# ============================================================
#  CONFIG
# ============================================================
# Tokens load karne ke 3 tareeke (preference order):
#   1. Environment variables (BOT_TOKEN, LOGS_BOT_TOKEN)
#   2. .env file in the same folder as this script
#   3. Hardcoded defaults below (change these if you fork the bot)
DEFAULT_BOT_TOKEN = "8632751849:AAGAjZg9JjwPOVF3Wx0VzfWrVYwS7LUER2k"
DEFAULT_LOGS_BOT_TOKEN = "8725768803:AAHU2GlPnYWdYH2-eZuXW76D3Qy1WT_AzMk"


def _load_dotenv() -> None:
    """Tiny .env loader (no external dependency)."""
    from pathlib import Path
    p = Path(__file__).resolve().parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or DEFAULT_BOT_TOKEN or "").strip()
LOGS_BOT_TOKEN = (
    os.environ.get("LOGS_BOT_TOKEN") or DEFAULT_LOGS_BOT_TOKEN or ""
).strip()

BASE = "https://jpw.jio.com"
UA = (
    "Mozilla/5.0 (Linux; Android 12; moto g(60) Build/S2RIS32.32-20-7-11; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/148.0.7778.217 Mobile Safari/537.36"
)
DEFAULT_LAT = "28.6139"
DEFAULT_LON = "77.2090"
ASK_CREDS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jpw")

PROMPT = (
    "🤖 *JPW AUTO-REACH PRO* 🤖\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "⚡ Fast Reach System\n"
    "📍 Smart GPS Support\n"
    "🗺️ Map Error Fixed\n"
    "🚀 High-Speed Performance\n"
    "🛡️ Secure Access\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🔐 *Login Required*\n\n"
    "👤 Technician ID\n"
    "🔑 Password"
)

# In-memory log subscribers
LOG_SUBS: set[int] = set()
LOGS_BOT: Bot | None = Bot(token=LOGS_BOT_TOKEN) if LOGS_BOT_TOKEN else None


# ============================================================
#  JIO CLIENT
# ============================================================
class JioError(Exception):
    pass


def jio_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": BASE,
        "X-Requested-With": "com.jio.jpss",
        "Accept": "*/*",
        "Content-Type": "application/json",
    })
    return s


def login(session: requests.Session, username: str, password: str) -> None:
    r = session.post(
        f"{BASE}/api/login/SAML/UserLogin",
        json={
            "UserName": username, "Password": password,
            "Handset": "android", "FCMID": "fake",
            "DeviceId": "fake", "AppVersion": "2.0.7",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    if not d.get("IsSuccessful"):
        raise JioError(d.get("ErrorInfo", {}).get("UserMessage") or "Login failed")


def list_work_orders(session: requests.Session, username: str) -> list[dict]:
    r = session.post(
        f"{BASE}/lco/api/workorder-inquiry/WorkOrder/GetWorkOrderList",
        json={
            "TechnicianID": username, "IsHSOUser": False,
            "WorkOrderStatus": [""], "PageSize": 200,
            "offsetValue": 0, "TechnicianDesignationType": "Technician",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    if not d.get("IsSuccessful"):
        raise JioError(d.get("ErrorInfo", {}).get("UserMessage") or "List failed")
    return d.get("lstWorkOrders") or []


def send_action(session: requests.Session, username: str, wo_id: str,
                action_code: str, lat: str, lon: str) -> dict:
    r = session.post(
        f"{BASE}/lco/api/workorder-maintenance/WorkOrder/UpdateWorkOrder",
        json={
            "ActionCode": action_code, "BuildingID": "",
            "StatusCode": "CL09",
            "TechnicianLatitude": str(lat),
            "TechnicianLongitude": str(lon),
            "UpdatedBy": username, "WorkOrderID": wo_id,
            "WorkOrderSubType": "", "WorkOrderType": "",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def coords_of(wo: dict) -> tuple[str, str]:
    addr = (wo.get("CustomerDetails") or {}).get("Address") or {}
    lat = addr.get("Latitude") or wo.get("Latitude")
    lon = addr.get("Longitude") or wo.get("Longitude")
    return (str(lat), str(lon)) if lat and lon else (DEFAULT_LAT, DEFAULT_LON)


def pick_active(wos: list[dict]) -> dict | None:
    today = date.today().isoformat()

    def apt(w: dict) -> str:
        return (w.get("AppointmentStartDate") or "")[:10]

    for w in wos:
        if w.get("StatusDesc") == "In Progress" and w.get("ActionCode") != "ZA26":
            return w
    for w in wos:
        if w.get("StatusDesc") == "In Progress":
            return w
    for status in ("Assigned", "Confirmed Interested"):
        for w in wos:
            if w.get("StatusDesc") == status and apt(w) == today:
                return w
    upcoming = [w for w in wos
                if w.get("StatusDesc") in ("Assigned", "Confirmed Interested")
                and apt(w) >= today]
    upcoming.sort(key=lambda w: apt(w))
    return upcoming[0] if upcoming else None


def auto_reach_pipeline(username: str, password: str) -> dict[str, Any]:
    sess = jio_session()
    try:
        login(sess, username, password)
        wos = list_work_orders(sess, username)
        active = pick_active(wos)
        if not active:
            return {"action_taken": "no_active_wo"}
        wo_id = active["WorkOrderID"]
        cust = (active.get("CustomerDetails") or {})
        lat, lon = coords_of(active)
        result: dict[str, Any] = {
            "work_order_id": wo_id,
            "status_desc": active.get("StatusDesc"),
            "before_action": active.get("ActionCode"),
            "customer_name": cust.get("FullName") or active.get("FullName"),
            "latitude": lat, "longitude": lon, "steps": [],
        }
        if active.get("ActionCode") == "ZA26":
            result["action_taken"] = "skipped_already_reached"
            return result
        status = active.get("StatusDesc")
        if status == "In Progress":
            upd = send_action(sess, username, wo_id, "ZA26", lat, lon)
            result["steps"].append({"action": "ZA26", "ok": upd.get("IsSuccessful")})
            if upd.get("IsSuccessful"):
                result["action_taken"] = "marked_reached"
            else:
                result["action_taken"] = "update_failed"
                result["error"] = (upd.get("ErrorInfo") or {}).get("UserMessage")
            return result
        if status in ("Assigned", "Confirmed Interested"):
            blocker = next((w for w in wos if w.get("StatusDesc") == "In Progress"), None)
            if blocker and blocker.get("WorkOrderID") != wo_id:
                result["action_taken"] = "blocked_by_previous_wo"
                result["blocker_work_order"] = blocker.get("WorkOrderID")
                result["blocker_customer"] = (
                    (blocker.get("CustomerDetails") or {}).get("FullName")
                    or blocker.get("FullName")
                )
                return result
            beg = send_action(sess, username, wo_id, "ZA25", lat, lon)
            result["steps"].append({"action": "ZA25", "ok": beg.get("IsSuccessful")})
            if not beg.get("IsSuccessful"):
                result["action_taken"] = "begin_journey_failed"
                result["error"] = (beg.get("ErrorInfo") or {}).get("UserMessage")
                return result
            upd = send_action(sess, username, wo_id, "ZA26", lat, lon)
            result["steps"].append({"action": "ZA26", "ok": upd.get("IsSuccessful")})
            if upd.get("IsSuccessful"):
                result["action_taken"] = "marked_reached"
            else:
                result["action_taken"] = "update_failed"
                result["error"] = (upd.get("ErrorInfo") or {}).get("UserMessage")
            return result
        result["action_taken"] = "unsupported_status"
        return result
    finally:
        sess.close()


# ============================================================
#  CREDENTIAL PARSER
# ============================================================
_USER_RE = re.compile(r"^(\d{10,15})(\S.*)$")


def parse_credentials(text: str) -> tuple[str | None, str | None]:
    t = (text or "").strip()
    if not t:
        return None, None
    parts = re.split(r"\s+", t, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    m = _USER_RE.match(t)
    return (m.group(1), m.group(2)) if m else (None, None)


# ============================================================
#  LOGS BROADCAST
# ============================================================
async def broadcast(text: str) -> None:
    if not LOGS_BOT:
        return
    for cid in list(LOG_SUBS):
        try:
            await LOGS_BOT.send_message(chat_id=cid, text=text)
        except Exception:
            pass


# ============================================================
#  MAIN BOT HANDLERS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for mid in ctx.user_data.get("cleanup", []):
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    ctx.user_data["cleanup"] = []
    sent = await update.message.reply_text(PROMPT, parse_mode=ParseMode.MARKDOWN)
    ctx.user_data["cleanup"].append(sent.message_id)
    return ASK_CREDS


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled. /start to begin again.")
    return ConversationHandler.END


async def _reset(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, delay: int = 2):
    cleanup = list(ctx.user_data.get("cleanup", []))
    ctx.user_data["cleanup"] = []

    async def _job():
        await asyncio.sleep(delay)
        for mid in cleanup:
            try:
                await ctx.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        try:
            sent = await ctx.bot.send_message(
                chat_id=chat_id, text=PROMPT, parse_mode=ParseMode.MARKDOWN,
            )
            ctx.user_data["cleanup"] = [sent.message_id]
        except Exception:
            pass

    asyncio.create_task(_job())


async def receive_credentials(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    try:
        await ctx.bot.delete_message(chat_id=chat_id,
                                     message_id=update.message.message_id)
    except Exception:
        pass

    username, password = parse_credentials(text)
    if not username or not password:
        m = await update.effective_chat.send_message("❌ Format galat. /start retry.")
        ctx.user_data.setdefault("cleanup", []).append(m.message_id)
        await _reset(ctx, chat_id, delay=3)
        return ASK_CREDS

    progress = await update.effective_chat.send_message("⚡ Processing…")
    ctx.user_data.setdefault("cleanup", []).append(progress.message_id)

    t0 = time.monotonic()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, auto_reach_pipeline, username, password,
        )
    except JioError as e:
        elapsed = time.monotonic() - t0
        await progress.edit_text(f"❌ {e}\n⏱️ {elapsed:.1f}s")
        await broadcast(f"❌ ERROR\nTech: {username}\nTime: {elapsed:.1f}s\nError: {e}")
        await _reset(ctx, chat_id, delay=3)
        return ASK_CREDS
    except Exception as e:
        elapsed = time.monotonic() - t0
        await progress.edit_text(f"❌ Network: {e}\n⏱️ {elapsed:.1f}s")
        await broadcast(f"❌ NETWORK\nTech: {username}\nTime: {elapsed:.1f}s\nError: {e}")
        await _reset(ctx, chat_id, delay=3)
        return ASK_CREDS

    elapsed = time.monotonic() - t0
    wo_id = result.get("work_order_id")
    act = result.get("action_taken")

    if act == "marked_reached":
        steps = " + ".join(s["action"] for s in (result.get("steps") or []))
        text_out = (
            f"✅ *REACHED!*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"WO: `{wo_id}`\n"
            f"Customer: {result.get('customer_name') or '—'}\n"
            f"Steps: *{steps}*\n"
            f"⏱️ *{elapsed:.1f}s*"
        )
    elif act == "skipped_already_reached":
        text_out = (
            f"ℹ️ *Already Reached*\nWO: `{wo_id}` (ZA26)\n"
            f"Phone par complete/hold karke retry karo.\n"
            f"⏱️ *{elapsed:.1f}s*"
        )
    elif act == "no_active_wo":
        text_out = f"ℹ️ Aaj koi WO assigned nahi mila.\n⏱️ {elapsed:.1f}s"
    elif act == "blocked_by_previous_wo":
        text_out = (
            f"⚠️ *Blocked* — previous WO `{result.get('blocker_work_order')}` "
            f"({result.get('blocker_customer') or '—'}) abhi In-Progress hai.\n"
            f"Phone se complete/hold karo phir retry karo.\n"
            f"⏱️ {elapsed:.1f}s"
        )
    elif act == "begin_journey_failed":
        text_out = f"❌ Begin Journey failed: {result.get('error')}\n⏱️ {elapsed:.1f}s"
    else:
        text_out = f"❌ {result.get('error') or act}\n⏱️ {elapsed:.1f}s"

    await progress.edit_text(text_out, parse_mode=ParseMode.MARKDOWN)
    status_emoji = {
        "marked_reached": "✅", "skipped_already_reached": "ℹ️",
        "no_active_wo": "📭", "blocked_by_previous_wo": "⛔",
        "begin_journey_failed": "❌", "update_failed": "❌",
        "unsupported_status": "⚠️",
    }.get(act, "•")
    status_text_map = {
        "marked_reached": "REACHED", "skipped_already_reached": "ALREADY REACHED",
        "no_active_wo": "NO ACTIVE WO", "blocked_by_previous_wo": "BLOCKED",
        "begin_journey_failed": "BEGIN JOURNEY FAILED",
        "update_failed": "REACH FAILED", "unsupported_status": "UNSUPPORTED",
    }
    log_lines = [
        f"{status_emoji} {status_text_map.get(act, act or 'UNKNOWN')}",
        f"WO: {wo_id or '—'}",
        f"Tech: {username}",
        f"Time: {elapsed:.1f}s",
    ]
    if result.get("customer_name"):
        log_lines.append(f"Customer: {result.get('customer_name')}")
    if act == "blocked_by_previous_wo" and result.get("blocker_work_order"):
        log_lines.append(f"Blocker: {result.get('blocker_work_order')}")
    if act in ("begin_journey_failed", "update_failed") and result.get("error"):
        log_lines.append(f"Error: {result.get('error')}")
    await broadcast("\n".join(log_lines))
    await _reset(ctx, chat_id, delay=2)
    return ASK_CREDS


# ============================================================
#  LOGS BOT HANDLERS
# ============================================================
async def logs_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    LOG_SUBS.add(update.effective_chat.id)
    await update.message.reply_text(
        "📡 Logs subscription active.\nUnsubscribe: /stop"
    )


async def logs_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    LOG_SUBS.discard(update.effective_chat.id)
    await update.message.reply_text("🛑 Unsubscribed.")


# ============================================================
#  MAIN
# ============================================================
def build_main_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_CREDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_credentials),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel),
                   CommandHandler("start", cmd_start)],
        per_message=False,
    )
    app.add_handler(conv)
    return app


def build_logs_app() -> Application | None:
    if not LOGS_BOT_TOKEN:
        return None
    app = Application.builder().token(LOGS_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", logs_start))
    app.add_handler(CommandHandler("stop", logs_stop))
    return app


def main() -> None:
    """Run both bots in a single event loop (correct async coordination)."""
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN not configured.\n\n"
            "3 ways to set it:\n"
            "  1) Open jpw_auto_reach_pro.py — set DEFAULT_BOT_TOKEN at the top\n"
            "  2) Create a `.env` file next to the script with:\n"
            "       BOT_TOKEN=123456:AAA...\n"
            "       LOGS_BOT_TOKEN=...\n"
            "  3) Export env vars before running:\n"
            "       export BOT_TOKEN=...\n"
            "       python jpw_auto_reach_pro.py\n"
        )
    asyncio.run(_async_main())


async def _async_main() -> None:
    main_app = build_main_app()
    logs_app = build_logs_app()

    await main_app.initialize()
    await main_app.start()
    await main_app.updater.start_polling(drop_pending_updates=True)
    log.info("Main bot polling started")

    if logs_app:
        await logs_app.initialize()
        await logs_app.start()
        await logs_app.updater.start_polling(drop_pending_updates=True)
        # Module-level Bot() needs initialize() once before send_message
        try:
            await LOGS_BOT.initialize()
        except Exception:
            pass
        log.info("Logs bot polling started")

    # Block until interrupted
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if logs_app:
            try:
                await logs_app.updater.stop()
                await logs_app.stop()
                await logs_app.shutdown()
            except Exception:
                pass
        try:
            await main_app.updater.stop()
            await main_app.stop()
            await main_app.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bye")
