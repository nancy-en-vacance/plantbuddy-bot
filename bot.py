import os
import pandas as pd
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

PLANTS_FILE = "plants.xlsx"

def load_plants():
    return pd.read_excel(PLANTS_FILE)

def save_plants(df):
    df.to_excel(PLANTS_FILE, index=False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌿 Hi! I’m PlantBuddy.\n"
        "I help you remember plant care so you don’t have to.\n\n"
        "Commands:\n"
        "/status — what needs watering today\n"
        "/water — mark watering"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = load_plants()
    today = datetime.now().date()
    due = df[df["next_due"] <= today]

    if due.empty:
        await update.message.reply_text("✅ Nothing to water today. Your plants are happy.")
        return

    names = ", ".join(due["name"].tolist())
    await update.message.reply_text(f"💧 Today you should water:\n{names}")

async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = load_plants()
    buttons = [
        [InlineKeyboardButton(row["name"], callback_data=str(row["plant_id"]))]
        for _, row in df.iterrows()
    ]

    await update.message.reply_text(
        "Which plant did you water?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def watered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plant_id = int(query.data)
    df = load_plants()
    today = datetime.now().date()

    df.loc[df["plant_id"] == plant_id, "last_watered"] = today
    df.loc[df["plant_id"] == plant_id, "next_due"] = (
        today + pd.to_timedelta(
            df.loc[df["plant_id"] == plant_id, "water_interval_days"], unit="D"
        )
    )

    save_plants(df)
    await query.edit_message_text("🌱 Got it! Watering saved.")

def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("water", water))
    app.add_handler(CallbackQueryHandler(watered))

    app.run_polling()

if __name__ == "__main__":
    main()
