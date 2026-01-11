#!/usr/bin/env python3
# PlantBuddyCare_bot — Render Web Service + Telegram webhook
# python-telegram-bot==20.7, pandas, openpyxl
# ENV: TELEGRAM_TOKEN, BASE_URL (e.g. https://plantbuddy-bot.onrender.com), PORT (auto by Render), PLANTS_FILE (optional)

import os
import re
import sys
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List

import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("plantbuddy")

TZ = ZoneInfo(os.getenv("TZ", "Asia/Kolkata"))
DATA_FILE = os.getenv("PLANTS_FILE", "plants.xlsx")

CB_TOGGLE = "w_toggle:"  # w_toggle:<plant_id>
CB_DONE = "w_done"
CB_CANCEL = "w_cancel"

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

ALIASES: Dict[str, str] = {
    "plant_id": "plant_id",
    "plantid": "plant_id",
    "id": "plant_id",

    "name": "name",
    "name_raw": "name",
    "plant_name": "name",

    "plant_type": "plant_type",
    "type": "plant_type",

    "location": "location",
    "room": "location",

    "last_watered": "last_watered",
    "last_watered_d": "last_watered",
    "last_waterered": "last_watered",  # typo-safe

    "water_interval_days": "water_interval_days",
    "water_interval_day": "water_interval_days",
    "water_interval": "water_interval_days",
    "water_int": "water_interval_days",

    "suggested_interval_days": "suggested_interval_days",
    "suggested_interval_day": "suggested_interval_days",
    "suggested_interval": "suggested_interval_days",

    "next_due": "next_due",
    "next_due_if_suggested": "next_due",
    "next_due_suggested": "next_due",

    "pot_type": "pot_type",
    "last_repot": "last_repot",
    "repot_priority": "repot_priority",
    "notes": "notes",
}

def _today() -> date:
    return datetime.now(TZ).date()

def _norm_col(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s

def ensure_next_due(df: pd.DataFrame) -> pd.DataFrame:
    # fill next_due if missing using last_watered + water_interval_days
    mask = df["next_due"].isna() & df["last_watered"].notna() & df["water_interval_days"].notna()
    if mask.any():
        df.loc[mask, "next_due"] = [
            (lw + timedelta(days=int(wi))) if isinstance(lw, date) and pd.notna(wi) else pd.NA
            for lw, wi in zip(df.loc[mask, "last_watered"], df.loc[mask, "water_interval_days"])
        ]
    return df

def load_df() -> pd.DataFrame:
    if not os.path.exists(DATA_FILE):
        raise FileNotFoundError(f"Не найден файл данных: {DATA_FILE}")

    df = pd.read_excel(DATA_FILE, engine="openpyxl")
    raw_cols = list(df.columns)

    df.columns = [_norm_col(c) for c in raw_cols]

    # alias rename
    rename_map = {}
    for c in df.columns:
        if c in ALIASES:
            rename_map[c] = ALIASES[c]
    df = df.rename(columns=rename_map)

    # ensure columns exist
    for col in CANON_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    # types
    df["plant_id"] = pd.to_numeric(df["plant_id"], errors="coerce").astype("Int64")
    for dcol in ["last_watered", "next_due", "last_repot"]:
        df[dcol] = pd.to_datetime(df[dcol], errors="coerce").dt.date
    for ncol in ["water_interval_days", "suggested_interval_days", "repot_priority"]:
        df[ncol] = pd.to_numeric(df[ncol], errors="coerce").astype("Int64")

    df = ensure_next_due(df)

    if df["plant_id"].notna().any():
        df = df.sort_values("plant_id")

    return df

def save_df(df: pd.DataFrame) -> None:
    out = df.copy()
    for dcol in ["last_watered", "next_due", "last_repot"]:
        out[dcol] = pd.to_datetime(out[dcol], errors="coerce")
    out = out[CANON_COLS]
    out.to_excel(DATA_FILE, index=False, engine="openpyxl")

def due_today(df: pd.DataFrame) -> pd.DataFrame:
    today = _today()
    df = ensure_next_due(df)
    return df[df["next_due"].notna() & (df["next_due"] <= today)].copy()

def label(row: pd.Series) -> str:
    nm = str(row.get("name") or "").strip()
    loc = str(row.get("location") or "").strip()
    return f"{nm} ({loc})" if loc else nm

def build_keyboard(df_names: pd.DataFrame, selected: List[int]) -> InlineKeyboardMarkup:
    rows = []
    for _, r in df_names.iterrows():
        if pd.isna(r["plant_id"]):
            continue
        pid = int(r["plant_id"])
        checked = "✅ " if pid in selected else ""
        rows.append([InlineKeyboardButton(f"{checked}{r['name']}", callback_data=f"{CB_TOGGLE}{pid}")])
    rows.append([
        InlineKeyboardButton("Готово", callback_data=CB_DONE),
        InlineKeyboardButton("Отмена", callback_data=CB_CANCEL),
    ])
    return InlineKeyboardMarkup(rows)

# -------- Handlers (RU) --------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "🌿 *Привет! Я PlantBuddy.*\n\n"
        "Команды:\n"
        "• /status — что нужно полить сегодня\n"
        "• /water — отметить полив (можно выбрать несколько)\n"
        "• /debug — диагностика\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    df = load_df()
    due = due_today(df)

    if due.empty:
        await update.message.reply_text("✅ Сегодня по графику поливать ничего не нужно.")
        return

    lines = ["💧 *Сегодня стоит полить:*"]
    for _, r in due.iterrows():
        nd = r["next_due"]
        nds = nd.isoformat() if isinstance(nd, date) else "—"
        lines.append(f"• {label(r)} — до {nds}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def water_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    df = load_df()
    context.user_data["water_df_cache"] = df[["plant_id", "name"]].copy()
    context.user_data["water_selected"] = []
    kb = build_keyboard(context.user_data["water_df_cache"], [])
    await update.message.reply_text("Что ты сегодня полила? Выбери растения (можно несколько) 👇", reply_markup=kb)

async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    pid = int((q.data or "").split(":", 1)[1])

    selected: List[int] = context.user_data.get("water_selected", [])
    if pid in selected:
        selected.remove(pid)
    else:
        selected.append(pid)
    context.user_data["water_selected"] = selected

    df_cache = context.user_data.get("water_df_cache")
    if df_cache is None:
        df_cache = load_df()[["plant_id", "name"]].copy()
        context.user_data["water_df_cache"] = df_cache

    await q.edit_message_reply_markup(reply_markup=build_keyboard(df_cache, selected))

async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    selected: List[int] = context.user_data.get("water_selected", [])
    if not selected:
        await q.edit_message_text("Ок, ничего не отметили. Если нужно — /water ещё раз.")
        context.user_data.pop("water_selected", None)
        context.user_data.pop("water_df_cache", None)
        return

    df = load_df()
    today = _today()

    updated_names = []
    for pid in selected:
        mask = df["plant_id"].astype("Int64") == pid
        if not mask.any():
            continue

        df.loc[mask, "last_watered"] = today
        wi = df.loc[mask, "water_interval_days"].iloc[0]
        if pd.notna(wi):
            df.loc[mask, "next_due"] = today + timedelta(days=int(wi))
        else:
            df.loc[mask, "next_due"] = pd.NA

        updated_names.append(str(df.loc[mask, "name"].iloc[0]))

    df = ensure_next_due(df)
    save_df(df)

    names_txt = ", ".join(updated_names) if updated_names else f"{len(selected)} шт."
    await q.edit_message_text(
        f"✅ Отметила полив: *{names_txt}*\nДата: {today.isoformat()}",
        parse_mode=ParseMode.MARKDOWN
    )

    context.user_data.pop("water_selected", None)
    context.user_data.pop("water_df_cache", None)

async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Ок, отменили. Если нужно — /water ещё раз.")
    context.user_data.pop("water_selected", None)
    context.user_data.pop("water_df_cache", None)

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "file": DATA_FILE,
        "cwd": os.getcwd(),
        "has TELEGRAM_TOKEN": bool(os.getenv("TELEGRAM_TOKEN")),
        "has BASE_URL": bool(os.getenv("BASE_URL")),
        "PORT": os.getenv("PORT", ""),
    }
    try:
        raw_df = pd.read_excel(DATA_FILE, engine="openpyxl")
        raw_cols = list(raw_df.columns)
        df = load_df()
        norm_cols = list(df.columns)
        sample = df[["plant_id", "name", "last_watered", "water_interval_days", "next_due"]].head(5).to_string(index=False)
    except Exception as e:
        raw_cols = []
        norm_cols = []
        sample = f"ERROR: {e!r}"

    text = (
        f"python: {info['python']}\n"
        f"platform: {info['platform']}\n"
        f"file: {info['file']}\n"
        f"cwd: {info['cwd']}\n"
        f"has TELEGRAM_TOKEN: {info['has TELEGRAM_TOKEN']}\n"
        f"has BASE_URL: {info['has BASE_URL']}\n"
        f"PORT: {info['PORT']}\n"
        f"raw columns: {raw_cols}\n"
        f"normalized columns: {norm_cols}\n"
        f"sample:\n{sample}\n"
    )
    await update.message.reply_text(f"```text\n{text}\n```", parse_mode=ParseMode.MARKDOWN)

# -------- Bootstrap (webhook) --------

def build_app() -> Application:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is missing")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("water", water_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))

    app.add_handler(CallbackQueryHandler(cb_toggle, pattern=f"^{re.escape(CB_TOGGLE)}"))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=f"^{re.escape(CB_DONE)}$"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern=f"^{re.escape(CB_CANCEL)}$"))

    return app

def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is missing")
    if not base_url:
        raise RuntimeError("BASE_URL is missing (e.g., https://plantbuddy-bot.onrender.com)")

    port = int(os.getenv("PORT", "10000"))
    listen = "0.0.0.0"

    app = build_app()

    # Keep a stable, non-guessable path
    webhook_path = f"telegram/{token[:10]}"
    webhook_url = f"{base_url}/{webhook_path}"

    log.info("Starting webhook on %s:%s", listen, port)
    log.info("Webhook URL: %s", webhook_url)

    app.run_webhook(
        listen=listen,
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
