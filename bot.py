import os
import threading
import platform
from datetime import date

import pandas as pd
from flask import Flask

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

PLANTS_FILE = "plants.xlsx"

TEXT = {
    "start": (
        "🌿 Привет! Я PlantBuddy.\n\n"
        "Команды:\n"
        "/status — что нужно полить сегодня\n"
        "/water — отметить полив (можно выбрать несколько)\n"
        "/debug — диагностика"
    ),
    "status_none": "Сегодня поливать ничего не нужно.",
    "status_head": "💧 Сегодня нужно полить:",
    "water_choose": "Выбери растения, которые ты полила, затем нажми «Готово».",
    "water_none_selected": "Ничего не выбрано. Отметь хотя бы одно растение.",
    "water_saved": "✅ Полив отмечен:\n{lines}",
    "error": "Ошибка:\n{e}",
}

CANONICAL = {
    "plant_id": ["plant_id", "id", "plantid"],
    "name": ["name", "name_raw", "plant_name", "title"],
    "location": ["location", "room", "place"],
    "plant_type": ["plant_type", "type"],
    "last_watered": ["last_watered", "last_watered_d", "last_watered_date", "last_watering"],
    "water_interval_days": ["water_interval_days", "water_interval_day", "water_int", "water_interval"],
    "next_due": ["next_due", "next_due_if_suggested", "next_due_suggested", "next_watering"],
}

REQUIRED_FOR_BASIC = ["plant_id", "name", "water_interval_days"]


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower_map = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        key = cand.lower().strip()
        if key in lower_map:
            return lower_map[key]
    return None


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename_map = {}
    for canon, variants in CANONICAL.items():
        found = _find_col(df, variants)
        if found and found != canon:
            rename_map[found] = canon
    if rename_map:
        df = df.rename(columns=rename_map)

    if "plant_id" in df.columns:
        df["plant_id"] = pd.to_numeric(df["plant_id"], errors="coerce").astype("Int64")

    for dcol in ["last_watered", "next_due"]:
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce").dt.date

    if "water_interval_days" in df.columns:
        df["water_interval_days"] = pd.to_numeric(df["water_interval_days"], errors="coerce")

    return df


def ensure_next_due(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "next_due" not in df.columns:
        df["next_due"] = pd.NaT

    last = pd.to_datetime(df["last_watered"], errors="coerce") if "last_watered" in df.columns else pd.to_datetime(pd.Series([None] * len(df)))
    interval = pd.to_numeric(df["water_interval_days"], errors="coerce") if "water_interval_days" in df.columns else pd.to_numeric(pd.Series([None] * len(df)), errors="coerce")
    next_due = pd.to_datetime(df["next_due"], errors="coerce")

    missing = next_due.isna() & last.notna() & interval.notna()
    computed = (last + pd.to_timedelta(interval, unit="D")).dt.date
    df.loc[missing, "next_due"] = computed[missing]

    df["next_due"] = pd.to_datetime(df["next_due"], errors="coerce").dt.date
    return df


def validate_schema(df: pd.DataFrame) -> list[str]:
    problems = []
    for col in REQUIRED_FOR_BASIC:
        if col not in df.columns:
            problems.append(f"Нет обязательной колонки: {col}")
    return problems


def load_plants() -> pd.DataFrame:
    df = pd.read_excel(PLANTS_FILE)
    df = normalize_df(df)
    df = ensure_next_due(df)
    return df


def save_plants(df: pd.DataFrame) -> None:
    df.to_excel(PLANTS_FILE, index=False)


def _get_selected(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    sel = context.user_data.get("water_sel")
    if sel is None:
        sel = set()
        context.user_data["water_sel"] = sel
    return sel


def build_water_keyboard(df: pd.DataFrame, selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for _, r in df.iterrows():
        if pd.isna(r["plant_id"]):
            continue
        pid = int(r["plant_id"])
        nm = str(r["name"])
        prefix = "☑️ " if pid in selected else "⬜️ "
        rows.append([InlineKeyboardButton(prefix + nm, callback_data=f"T:{pid}")])

    rows.append([
        InlineKeyboardButton("✅ Готово", callback_data="DONE"),
        InlineKeyboardButton("🔄 Сброс", callback_data="RESET"),
    ])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["start"])


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text("\n".join(problems))
            return

        today = date.today()
        due = df[df["next_due"].notna() & (df["next_due"] <= today)].copy()

        if due.empty:
            await update.message.reply_text(TEXT["status_none"])
            return

        due = due.sort_values(["next_due", "name"], ascending=[True, True])

        lines = []
        for _, r in due.iterrows():
            nm = str(r["name"])
            loc = str(r["location"]) if "location" in due.columns and pd.notna(r.get("location")) else ""
            suffix = f" ({loc})" if loc and loc != "nan" else ""
            lines.append(f"- {nm}{suffix} — до {r['next_due']}")

        await update.message.reply_text(TEXT["status_head"] + "\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(e=e))
        raise


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text("\n".join(problems))
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
            await query.edit_message_text("\n".join(problems))
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

        sample_cols = [c for c in ["plant_id", "name", "last_watered", "water_interval_days", "next_due"] if c in df.columns]
        sample = df.head(3)[sample_cols]

        msg = (
            f"python: {platform.python_version()}\n"
            f"platform: {platform.platform()}\n"
            f"file: {PLANTS_FILE}\n"
            f"cwd: {os.getcwd()}\n"
            f"has TELEGRAM_TOKEN: {'TELEGRAM_TOKEN' in os.environ}\n"
            f"columns: {list(df.columns)}\n\n"
            f"sample:\n{sample.to_string(index=False)}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(e=e))
        raise


# ---------- Render Web Service: open port + run bot in background ----------

_bot_started = False

def run_bot_polling():
    token = os.environ["TELEGRAM_TOKEN"]
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("water", water))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(water_callback))

    # polling blocks, so we run it in a daemon thread
    app.run_polling(close_loop=False)


flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "ok"

@flask_app.get("/health")
def health():
    return "ok"


def ensure_bot_started_once():
    global _bot_started
    if _bot_started:
        return
    _bot_started = True
    t = threading.Thread(target=run_bot_polling, daemon=True)
    t.start()


if __name__ == "__main__":
    ensure_bot_started_once()
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)
