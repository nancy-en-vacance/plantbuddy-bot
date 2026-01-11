import os
import platform
from datetime import date, timedelta

import pandas as pd
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
        "Я помогаю помнить уход за растениями.\n\n"
        "Команды:\n"
        "/status — что нужно полить сегодня\n"
        "/water — отметить полив\n"
        "/debug — диагностика данных"
    ),
    "no_plants_today": "Сегодня поливать ничего не нужно 🌱",
    "status_header": "💧 Сегодня нужно полить:",
    "choose_plant": "Какое растение ты полила?",
    "water_done": "Полив отмечен для: {name}",
    "error": "Ошибка при обработке данных:\n{error}",
}


# ---------- Data helpers ----------

def load_plants() -> pd.DataFrame:
    df = pd.read_excel(PLANTS_FILE)

    # normalize column names
    rename_map = {
        "name_raw": "name",
        "last_watered_d": "last_watered",
        "water_interval_days": "water_interval_days",
        "suggested_interval_days": "suggested_interval_days",
        "next_due_if_suggested": "next_due",
    }
    for src, dst in rename_map.items():
        if src in df.columns:
            df.rename(columns={src: dst}, inplace=True)

    # dates
    if "last_watered" in df.columns:
        df["last_watered"] = pd.to_datetime(df["last_watered"], errors="coerce")

    # auto-generate next_due if missing
    if "next_due" not in df.columns:
        df["next_due"] = pd.NaT

    mask = df["next_due"].isna() & df["last_watered"].notna()
    df.loc[mask, "next_due"] = (
        df.loc[mask, "last_watered"]
        + pd.to_timedelta(df.loc[mask, "water_interval_days"], unit="D")
    )

    return df


def save_plants(df: pd.DataFrame):
    df.to_excel(PLANTS_FILE, index=False)


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT["start"])


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        today = pd.to_datetime(date.today())

        due = df[df["next_due"].notna() & (df["next_due"] <= today)]

        if due.empty:
            await update.message.reply_text(TEXT["no_plants_today"])
            return

        lines = []
        for _, row in due.iterrows():
            lines.append(
                f"- {row['name']} ({row['location']}) — до {row['next_due'].date()}"
            )

        await update.message.reply_text(
            TEXT["status_header"] + "\n" + "\n".join(lines)
        )

    except Exception as e:
        await update.message.reply_text(TEXT["error"].format(error=e))
        raise


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = load_plants()

    keyboard = [
        [InlineKeyboardButton(row["name"], callback_data=str(row["plant_id"]))]
        for _, row in df.iterrows()
    ]

    await update.message.reply_text(
        TEXT["choose_plant"],
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def water_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plant_id = int(query.data)

    df = load_plants()
    idx = df.index[df["plant_id"] == plant_id][0]

    today = pd.to_datetime(date.today())
    df.loc[idx, "last_watered"] = today
    df.loc[idx, "next_due"] = today + timedelta(
        days=int(df.loc[idx, "water_interval_days"])
    )

    save_plants(df)

    await query.edit_message_text(
        TEXT["water_done"].format(name=df.loc[idx, "name"])
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = load_plants()

    msg = (
        f"python: {platform.python_version()}\n"
        f"platform: {platform.platform()}\n"
        f"file: {PLANTS_FILE}\n"
        f"cwd: {os.getcwd()}\n"
        f"columns: {list(df.columns)}\n\n"
        f"sample:\n{df[['plant_id','name','last_watered','next_due']].head(3)}"
    )

    await update.message.reply_text(msg)


# ---------- App ----------

def main():
    token = os.environ["TELEGRAM_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("water", water))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(water_callback))

    app.run_polling()


if __name__ == "__main__":
    main()
