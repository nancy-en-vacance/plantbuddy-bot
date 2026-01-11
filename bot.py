# PlantBuddyCare_bot — Telegram plant care tracker (webhook + Render Web Service)
# - Russian UI
# - /water supports multi-select
# - Normalizes column names
# - Auto-generates next_due (last_watered + water_interval_days)
# - /debug for diagnostics
#
# Env vars (Render -> Environment):
#   TELEGRAM_TOKEN = BotFather token
#   BASE_URL = https://<your-service>.onrender.com   (optional if Render gives RENDER_EXTERNAL_URL)
# Optional:
#   PORT = 10000  (Render provides PORT)
#   PLANTS_FILE = plants.xlsx

import os
import re
import sys
import json
import asyncio
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Tuple, Optional, List

import pandas as pd
from flask import Flask, request

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("plantbuddy")

PLANTS_FILE = os.getenv("PLANTS_FILE", "plants.xlsx")
PORT = int(os.getenv("PORT", "10000"))

# Render обычно даёт внешний URL в переменной RENDER_EXTERNAL_URL
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
BASE_URL = os.getenv("BASE_URL") or RENDER_EXTERNAL_URL

WEBHOOK_PATH = "/webhook"
HEALTH_PATH = "/"

_file_lock = threading.Lock()

ALIASES: Dict[str, str] = {
    "plant_id": "plant_id",
    "id": "plant_id",
    "name": "name",
    "name_raw": "name",
    "plant_name": "name",
    "plant_type": "plant_type",
    "type": "plant_type",
    "location": "location",
    "last_watered": "last_watered",
    "last_watered_date": "last_watered",
    "last_watered_at": "last_watered",
    "last_watered_d": "last_watered",
    "water_interval_days": "water_interval_days",
    "water_interval_day": "water_interval_days",
    "water_interval": "water_interval_days",
    "water_int": "water_interval_days",
    "suggested_interval_days": "suggested_interval_days",
    "suggested_interval": "suggested_interval_days",
    "next_due": "next_due",
    "next_due_if_suggested": "next_due",
    "next_due_suggested": "next_due",
    "pot_type": "pot_type",
    "last_repot": "last_repot",
    "repot_priority": "repot_priority",
    "notes": "notes",
}

CANON_COLS = [
    "plant_id",
    "name",
    "plant_type",
    "location",
    "last_watered",
    "water_interval_days",
    "suggested_interval_days",
    "next_due",
    "pot_type",
    "last_repot",
    "repot_priority",
    "notes",
]


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def normalize_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str], List[str]]:
    raw_cols = list(df.columns)
    mapping = {}
    for c in raw_cols:
        key = _slug(str(c))
        mapping[c] = ALIASES.get(key, key)
    df = df.rename(columns=mapping)
    normalized_cols = list(df.columns)
    for col in CANON_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df, raw_cols, normalized_cols


def _to_date(x) -> Optional[date]:
    if pd.isna(x) or x is None or x == "":
        return None
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    try:
        return pd.to_datetime(x).date()
    except Exception:
        return None


def _to_int(x) -> Optional[int]:
    if pd.isna(x) or x is None or x == "":
        return None
    try:
        return int(float(x))
    except Exception:
        return None


def compute_next_due_row(row: pd.Series) -> Optional[date]:
    lw = _to_date(row.get("last_watered"))
    interval = _to_int(row.get("water_interval_days"))
    if lw and interval and interval > 0:
        return lw + timedelta(days=interval)
    return None


def load_plants() -> Tuple[pd.DataFrame, List[str], List[str]]:
    with _file_lock:
        df = pd.read_excel(PLANTS_FILE)

    df, raw_cols, norm_cols = normalize_columns(df)

    df["plant_id"] = df["plant_id"].apply(_to_int)
    df["last_watered"] = df["last_watered"].apply(_to_date)
    df["next_due"] = df["next_due"].apply(_to_date)
    df["water_interval_days"] = df["water_interval_days"].apply(_to_int)

    # автоген next_due если пусто
    missing = df["next_due"].isna()
    if missing.any():
        df.loc[missing, "next_due"] = df.loc[missing].apply(compute_next_due_row, axis=1)

    return df, raw_cols, norm_cols


def save_plants(df: pd.DataFrame) -> None:
    out = df.copy()
    for c in CANON_COLS:
        if c not in out.columns:
            out[c] = pd.NA
    if "plant_id" in out.columns:
        out = out.sort_values(by="plant_id", kind="stable")
    with _file_lock:
        out.to_excel(PLANTS_FILE, index=False)


def list_plants(df: pd.DataFrame) -> List[Tuple[int, str]]:
    items = []
    for _, r in df.iterrows():
        pid = _to_int(r.get("plant_id"))
        name = str(r.get("name") or "").strip()
        if pid is None:
            continue
        if not name:
            name = f"Растение #{pid}"
        items.append((pid, name))
    return items


def build_multiselect_keyboard(plants: List[Tuple[int, str]], selected: set) -> InlineKeyboardMarkup:
    rows = []
    for pid, name in plants:
        mark = "✅ " if pid in selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"toggle:{pid}")])
    rows.append(
        [
            InlineKeyboardButton(text="Готово", callback_data="done"),
            InlineKeyboardButton(text="Отмена", callback_data="cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌿 Привет! Я PlantBuddy.\n"
        "Я помогаю помнить уход за растениями.\n\n"
        "Команды:\n"
        "/status — что нужно полить сегодня\n"
        "/water — отметить полив (можно выбрать несколько)\n"
        "/debug — диагностика"
    )
    await update.message.reply_text(text)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df, _, _ = load_plants()
    today = date.today()
    due = df[df["next_due"].notna() & (df["next_due"] <= today)].copy()

    if due.empty:
        await update.message.reply_text("Сегодня ничего поливать не нужно ✅")
        return

    lines = ["Сегодня нужно полить:"]
    for _, r in due.iterrows():
        name = r.get("name") or "Без названия"
        loc = r.get("location") or ""
        nd = r.get("next_due")
        nd_s = nd.isoformat() if isinstance(nd, date) else str(nd)
        lines.append(f"• {name}" + (f" ({loc})" if loc else "") + f" — срок {nd_s}")

    await update.message.reply_text("\n".join(lines))


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df, _, _ = load_plants()
    plants = list_plants(df)

    selected = context.user_data.get("water_selected", set())
    if not isinstance(selected, set):
        selected = set()
    context.user_data["water_selected"] = selected

    kb = build_multiselect_keyboard(plants, selected)
    await update.message.reply_text("Что ты полила? Можно выбрать несколько 👇", reply_markup=kb)


async def water_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    df, _, _ = load_plants()
    plants = list_plants(df)

    selected = context.user_data.get("water_selected", set())
    if not isinstance(selected, set):
        selected = set()

    if data.startswith("toggle:"):
        pid = int(data.split(":", 1)[1])
        if pid in selected:
            selected.remove(pid)
        else:
            selected.add(pid)
        context.user_data["water_selected"] = selected
        await query.edit_message_reply_markup(reply_markup=build_multiselect_keyboard(plants, selected))
        return

    if data == "cancel":
        context.user_data["water_selected"] = set()
        await query.edit_message_text("Ок, отменено.")
        return

    if data == "done":
        if not selected:
            await query.edit_message_text("Ты ничего не выбрала — ок 🙂")
            return

        today = date.today()
        df.loc[df["plant_id"].isin(list(selected)), "last_watered"] = today
        df.loc[df["plant_id"].isin(list(selected)), "next_due"] = df.loc[
            df["plant_id"].isin(list(selected))
        ].apply(compute_next_due_row, axis=1)

        save_plants(df)

        by_id = {pid: name for pid, name in plants}
        names = [by_id.get(pid, f"#{pid}") for pid in sorted(selected)]

        context.user_data["water_selected"] = set()
        await query.edit_message_text("Записала полив на сегодня ✅\n" + "\n".join(f"• {n}" for n in names))
        return


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df, raw_cols, norm_cols = load_plants()
    sample = df[["plant_id", "name", "last_watered", "water_interval_days", "next_due"]].head(5)
    info = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "file": PLANTS_FILE,
        "cwd": os.getcwd(),
        "has TELEGRAM_TOKEN": bool(os.getenv("TELEGRAM_TOKEN")),
        "has BASE_URL/RENDER_EXTERNAL_URL": bool(BASE_URL),
        "raw columns": raw_cols,
        "normalized columns": norm_cols,
        "sample": sample.to_dict(orient="records"),
    }
    await update.message.reply_text(
        "```json\n" + json.dumps(info, ensure_ascii=False, indent=2) + "\n```",
        parse_mode="Markdown",
    )


flask_app = Flask(__name__)
_app: Optional[Application] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


@flask_app.get(HEALTH_PATH)
def health():
    return "ok", 200


@flask_app.post(WEBHOOK_PATH)
def webhook():
    global _app, _loop
    if _app is None or _loop is None:
        return "not ready", 503
    update_json = request.get_json(force=True, silent=True) or {}
    update = Update.de_json(update_json, _app.bot)
    fut = asyncio.run_coroutine_threadsafe(_app.process_update(update), _loop)
    try:
        fut.result(timeout=0.5)
    except Exception:
        pass
    return "ok", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


async def async_init_app(app: Application):
    await app.initialize()
    await app.start()

    if not BASE_URL:
        raise RuntimeError(
            "BASE_URL missing. Добавь BASE_URL в Render (например https://plantbuddy-bot.onrender.com) "
            "или используй RENDER_EXTERNAL_URL."
        )

    url = BASE_URL.rstrip("/") + WEBHOOK_PATH
    await app.bot.set_webhook(url=url)
    log.info("Webhook set to %s", url)


def main():
    global _app, _loop

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is missing")

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    _app = Application.builder().token(token).build()
    _app.add_handler(CommandHandler("start", start))
    _app.add_handler(CommandHandler("status", status))
    _app.add_handler(CommandHandler("water", water))
    _app.add_handler(CommandHandler("debug", debug))
    _app.add_handler(CallbackQueryHandler(water_callback))

    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask listening on 0.0.0.0:%s", os.getenv("PORT", "10000"))

    _loop.run_until_complete(async_init_app(_app))
    log.info("PlantBuddy started (webhook mode).")

    _loop.run_forever()


if __name__ == "__main__":
    main()
