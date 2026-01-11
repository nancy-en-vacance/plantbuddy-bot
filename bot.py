import os
import threading
import platform
import asyncio
from datetime import date

import pandas as pd
from flask import Flask, request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =============================
# Config
# =============================

PLANTS_FILE = os.environ.get("PLANTS_FILE", "plants.xlsx")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # e.g. https://plantbuddy-bot.onrender.com
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/telegram")  # keep default
PORT = int(os.environ.get("PORT", "10000"))

TEXT = {
    "start": (
        "🌿 Привет! Я PlantBuddy.\n\n"
        "Команды:\n"
        "/status — что нужно полить сегодня\n"
        "/water — отметить полив (можно выбрать несколько)\n"
        "/debug — диагностика"
    ),
    "schema_error_head": "Проблемы с таблицей:\n",
    "status_no_data": "Пока нет данных по датам следующего полива (next_due).",
    "status_none": "Сегодня поливать ничего не нужно.",
    "status_header_soon": "✅ Срочно ничего не нужно. Ближайшие:",
    "status_header_due": "💧 Полив:",
    "water_choose": "Выбери растения, которые ты полила, затем нажми «Готово».",
    "water_none_selected": "Ничего не выбрано. Отметь хотя бы одно растение.",
    "water_saved": "✅ Полив отмечен:\n{lines}",
    "error": "Ошибка:\n{e}",
    "webhook_ok": "ok",
    "webhook_not_set": "Не задан BASE_URL — webhook не установлен.",
}

# =============================
# Columns normalization
# =============================

CANONICAL = {
    "plant_id": ["plant_id", "id", "plantid", "plant id"],
    "name": ["name", "name_raw", "plant_name", "plant name", "title"],
    "location": ["location", "room", "place"],
    "plant_type": ["plant_type", "type", "plant type"],
    # common typos/variants
    "last_watered": [
        "last_watered", "last_watered_d", "last_watered_date", "last_watering",
        "last_waterered", "last watered", "last waterered"
    ],
    "water_interval_days": [
        "water_interval_days", "water_interval_day", "water_int", "water_interval",
        "water interval days", "water interval"
    ],
    "suggested_interval_days": ["suggested_interval_days", "suggested_interval_day", "suggested interval days"],
    "next_due": [
        "next_due", "next_due_if_suggested", "next_due_suggested", "next_watering",
        "next due", "next due if suggested"
    ],
    "pot_type": ["pot_type", "pot type"],
    "last_repot": ["last_repot", "last repot"],
    "repot_priority": ["repot_priority", "repot priority"],
    "notes": ["notes", "note"],
}

REQUIRED_FOR_BASIC = ["plant_id", "name", "water_interval_days"]


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower_map = {str(c).lower().strip(): c for c in df.columns}
    for cand in candidates:
        key = str(cand).lower().strip()
        if key in lower_map:
            return lower_map[key]
    return None


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to canonical names (without modifying original file yet)."""
    df = df.copy()
    rename_map = {}
    for canon, variants in CANONICAL.items():
        found = _find_col(df, variants)
        if found and found != canon:
            rename_map[found] = canon
    if rename_map:
        df = df.rename(columns=rename_map)

    # Types
    if "plant_id" in df.columns:
        df["plant_id"] = pd.to_numeric(df["plant_id"], errors="coerce").astype("Int64")

    for dcol in ["last_watered", "next_due", "last_repot"]:
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce").dt.date

    for ncol in ["water_interval_days", "suggested_interval_days", "repot_priority"]:
        if ncol in df.columns:
            df[ncol] = pd.to_numeric(df[ncol], errors="coerce")

    # Ensure optional columns exist (nice for saving back consistently)
    for opt in ["location", "plant_type", "pot_type", "last_repot", "repot_priority", "notes", "suggested_interval_days"]:
        if opt not in df.columns:
            df[opt] = pd.NA

    return df


def ensure_next_due(df: pd.DataFrame) -> pd.DataFrame:
    """Autogenerate next_due when missing: last_watered + water_interval_days."""
    df = df.copy()
    if "next_due" not in df.columns:
        df["next_due"] = pd.NaT

    last = pd.to_datetime(df.get("last_watered"), errors="coerce")
    interval = pd.to_numeric(df.get("water_interval_days"), errors="coerce")
    next_due = pd.to_datetime(df.get("next_due"), errors="coerce")

    missing = next_due.isna() & last.notna() & interval.notna()
    computed = (last + pd.to_timedelta(interval, unit="D")).dt.date
    df.loc[missing, "next_due"] = computed[missing]

    df["next_due"] = pd.to_datetime(df["next_due"], errors="coerce").dt.date
    return df


def validate_schema(df: pd.DataFrame) -> list[str]:
    problems = []
    for col in REQUIRED_FOR_BASIC:
        if col not in df.columns:
            problems.append(f"- нет колонки: {col}")
    if "plant_id" in df.columns and df["plant_id"].isna().all():
        problems.append("- plant_id пустой (нужны числа 1,2,3...)")
    return problems


def load_plants() -> pd.DataFrame:
    df = pd.read_excel(PLANTS_FILE)
    df = normalize_df(df)
    df = ensure_next_due(df)
    return df


def save_plants(df: pd.DataFrame) -> None:
    """Save with canonical column names (nice & stable)."""
    # Put key columns first
    col_order = [
        "plant_id", "name", "plant_type", "location",
        "last_watered", "water_interval_days", "suggested_interval_days",
        "next_due", "pot_type", "last_repot", "repot_priority", "notes"
    ]
    existing = [c for c in col_order if c in df.columns]
    rest = [c for c in df.columns if c not in existing]
    out = df[existing + rest].copy()
    out.to_excel(PLANTS_FILE, index=False)


# =============================
# Telegram UI helpers
# =============================

def _get_selected(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    sel = context.user_data.get("water_sel")
    if sel is None:
        sel = set()
        context.user_data["water_sel"] = sel
    return sel


def build_water_keyboard(df: pd.DataFrame, selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    # Stable sort: by location then name
    view = df.copy()
    if "location" in view.columns:
        view["__loc"] = view["location"].fillna("")
    else:
        view["__loc"] = ""
    view["__name"] = view["name"].fillna("").astype(str)
    view = view.sort_values(["__loc", "__name"])

    for _, r in view.iterrows():
        if pd.isna(r.get("plant_id")):
            continue
        pid = int(r["plant_id"])
        nm = str(r.get("name", ""))
        prefix = "☑️ " if pid in selected else "⬜️ "
        rows.append([InlineKeyboardButton(prefix + nm, callback_data=f"T:{pid}")])

    rows.append([
        InlineKeyboardButton("✅ Готово", callback_data="DONE"),
        InlineKeyboardButton("🔄 Сброс", callback_data="RESET"),
    ])
    return InlineKeyboardMarkup(rows)


# =============================
# Handlers
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["start"])


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text(TEXT["schema_error_head"] + "\n".join(problems))
            return

        today = date.today()

        due_df = df[df["next_due"].notna()].copy()
        if due_df.empty:
            await update.message.reply_text(TEXT["status_no_data"])
            return

        due_df["delta_days"] = due_df["next_due"].apply(lambda d: (d - today).days)

        # "Urgent" window: overdue or within 2 days
        view_df = due_df[due_df["delta_days"] <= 2].copy()
        if view_df.empty:
            view_df = due_df.sort_values(["delta_days", "name"]).head(3)
            header = TEXT["status_header_soon"]
        else:
            header = TEXT["status_header_due"]

        view_df = view_df.sort_values(["delta_days", "name"])

        lines = []
        for _, r in view_df.iterrows():
            nm = str(r.get("name", ""))
            loc = str(r.get("location", "") or "")
            loc_part = f" ({loc})" if loc and loc != "nan" else ""

            dd = int(r["delta_days"])
            if dd < 0:
                when = f"просрочено на {abs(dd)} дн."
            elif dd == 0:
                when = "сегодня"
            elif dd == 1:
                when = "завтра"
            else:
                when = f"через {dd} дн."

            lines.append(f"- {nm}{loc_part} — {when} (до {r['next_due']})")

        await update.message.reply_text(header + "\n" + "\n".join(lines))

    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(e=e))
        raise


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text(TEXT["schema_error_head"] + "\n".join(problems))
            return

        context.user_data["water_sel"] = set()
        kb = build_water_keyboard(df, context.user_data["water_sel"])
        await update.message.reply_text(TEXT["water_choose"], reply_markup=kb)

    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(e=e))
        raise


async def water_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await query.edit_message_text(TEXT["schema_error_head"] + "\n".join(problems))
            return

        data = query.data
        selected = _get_selected(context)

        if data.startswith("T:"):
            pid = int(data.split(":", 1)[1])
            if pid in selected:
                selected.remove(pid)
            else:
                selected.add(pid)

            kb = build_water_keyboard(df, selected)
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if data == "RESET":
            context.user_data["water_sel"] = set()
            kb = build_water_keyboard(df, context.user_data["water_sel"])
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if data == "DONE":
            if not selected:
                await query.edit_message_text(TEXT["water_none_selected"])
                return

            today = date.today()
            updated_lines = []

            for pid in sorted(selected):
                mask = df["plant_id"].astype("Int64") == pid
                if not mask.any():
                    continue

                df.loc[mask, "last_watered"] = today

                interval = pd.to_numeric(df.loc[mask, "water_interval_days"].iloc[0], errors="coerce")
                if pd.isna(interval):
                    interval = 0

                df.loc[mask, "next_due"] = (pd.to_datetime(today) + pd.to_timedelta(float(interval), unit="D")).date()

                nm = str(df.loc[mask, "name"].iloc[0])
                nd = df.loc[mask, "next_due"].iloc[0]
                updated_lines.append(f"- {nm} → след. полив {nd}")

            save_plants(df)
            context.user_data["water_sel"] = set()

            await query.edit_message_text(TEXT["water_saved"].format(lines="\n".join(updated_lines)))
            return

    except Exception as e:
        await query.edit_message_text(TEXT["error"].format(e=e))
        raise


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df_raw = pd.read_excel(PLANTS_FILE)
        df = ensure_next_due(normalize_df(df_raw))

        raw_cols = list(df_raw.columns)
        norm_cols = list(df.columns)

        sample_cols = [c for c in ["plant_id", "name", "last_watered", "water_interval_days", "next_due"] if c in df.columns]
        sample = df.head(5)[sample_cols]

        msg = (
            f"python: {platform.python_version()}\n"
            f"platform: {platform.platform()}\n"
            f"file: {PLANTS_FILE}\n"
            f"cwd: {os.getcwd()}\n"
            f"has TELEGRAM_TOKEN: {'TELEGRAM_TOKEN' in os.environ}\n"
            f"has BASE_URL: {bool(BASE_URL)}\n"
            f"raw columns: {raw_cols}\n"
            f"normalized columns: {norm_cols}\n"
            f"sample:\n{sample.to_string(index=False)}"
        )
        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(e=e))
        raise


# =============================
# Webhook + Flask (Render Web Service)
# =============================

application = None
_loop = None
_started = False

def _start_async_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def _run_coro(coro):
    """Run coroutine on the background event loop."""
    if _loop is None:
        raise RuntimeError("Async loop not started")
    return asyncio.run_coroutine_threadsafe(coro, _loop)

async def _async_init_app():
    global application
    token = os.environ["TELEGRAM_TOKEN"]
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("water", water))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CallbackQueryHandler(water_callback))

    await application.initialize()
    await application.start()

    # Set webhook if BASE_URL provided
    if BASE_URL:
        url = f"{BASE_URL}{WEBHOOK_PATH}"
        await application.bot.set_webhook(url=url, drop_pending_updates=True)

def ensure_started_once():
    global _started
    if _started:
        return
    _started = True

    # background loop
    t = threading.Thread(target=_start_async_loop, daemon=True)
    t.start()

    # init application on that loop
    fut = _run_coro(_async_init_app())
    fut.result(timeout=60)  # fail fast on deploy if token is wrong


flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    ensure_started_once()
    return TEXT["webhook_ok"]

@flask_app.get("/health")
def health():
    ensure_started_once()
    return TEXT["webhook_ok"]

@flask_app.post(WEBHOOK_PATH)
def telegram_webhook():
    ensure_started_once()
    if application is None:
        return "not ready", 503

    data = request.get_json(force=True, silent=True) or {}
    update = Update.de_json(data, application.bot)

    # process update async
    _run_coro(application.process_update(update))
    return TEXT["webhook_ok"]


if __name__ == "__main__":
    ensure_started_once()
    flask_app.run(host="0.0.0.0", port=PORT)
