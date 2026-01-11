import os
import pandas as pd
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

PLANTS_FILE = "plants.xlsx"

def load_plants():
    return pd.read_excel(PLANTS_FILE)

def save_plants(df):
    df.to_excel(PLANTS_FILE, index=False)

async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌿 Hi! I’m PlantBuddy.\n"
        "I help you remember plant care so you don’t have to.\n\n"
        "Commands:\n"
        "/status — what needs watering today\n"
        "/water — mark watering"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # вся логика
    except Exception as e:
        await update.message.reply_text(
            f"Error while reading plant data:\n{e}"
        )
        raise


    if "next_due_if_suggested" in df.columns:
        df["next_due_if_suggested"] = pd.to_datetime(df["next_due_if_suggested"], errors="coerce").dt.date

    due = df[df["next_due_if_suggested"].notna() & (df["next_due_if_suggested"] <= today)]

    if due.empty:
        await update.message.reply_text("✅ Nothing to water today. Your plants are happy.")
        return

    names = ", ".join(due["name"].astype(str).tolist())
    await update.message.reply_text(f"💧 Today you should water:\n{names}")

async def water(update, context: ContextTypes.DEFAULT_TYPE):
    df = load_plants()

    def pid(x):
        try:
            return str(int(x))
        except Exception:
            return str(x)

    buttons = [
        [InlineKeyboardButton(str(row["name"]), callback_data=pid(row["plant_id"]))]
        for _, row in df.iterrows()
    ]

    await update.message.reply_text(
        "Which plant did you water?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def watered(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plant_id = query.data
    df = load_plants()
    today = datetime.now().date()

    def match_pid(val):
        try:
            return str(int(val)) == str(int(plant_id))
        except Exception:
            return str(val) == str(plant_id)

    mask = df["plant_id"].apply(match_pid)

    if not mask.any():
        await query.edit_message_text("⚠️ Plant not found.")
        return

    df.loc[mask, "last_watered"] = today
    interval = df.loc[mask, "water_interval_days"].iloc[0]

    try:
        interval_days = float(interval)
    except Exception:
        interval_days = 0

    df.loc[mask, "next_due"] = today + pd.to_timedelta(interval_days, unit="D")
    save_plants(df)

    await query.edit_message_text("🌱 Got it! Watering saved.")

def main():
    token = os.environ["TELEGRAM_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("water", water))
    app.add_handler(CallbackQueryHandler(watered))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=token,
        webhook_url=f"{base_url}/{token}",
    )

if __name__ == "__main__":
    main()
