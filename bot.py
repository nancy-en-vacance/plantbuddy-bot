import os
import platform
from datetime import date, datetime
import pandas as pd

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

PLANTS_FILE = "plants.xlsx"


# ---------- helpers: schema / normalization ----------

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
    cols = {c.strip(): c for c in df.columns}
    lower_map = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in cols:
            return cols[cand]
        if cand.lower().strip() in lower_map:
            return lower_map[cand.lower().strip()]
    return None


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # rename known columns -> canonical names
    rename_map = {}
    for canon, variants in CANONICAL.items():
        found = _find_col(df, variants)
        if found and found != canon:
            rename_map[found] = canon
    if rename_map:
        df = df.rename(columns=rename_map)

    # coerce types
    if "plant_id" in df.columns:
        # keep as int-ish but safe
        df["plant_id"] = df["plant_id"].apply(lambda x: int(x) if pd.notna(x) and str(x).strip() != "" else x)

    # dates
    for dcol in ["last_watered", "next_due"]:
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce").dt.date

    # intervals
    if "water_interval_days" in df.columns:
        df["water_interval_days"] = pd.to_numeric(df["water_interval_days"], errors="coerce")

    return df


def ensure_next_due(df: pd.DataFrame) -> pd.DataFrame:
    """
    Create/refresh next_due if missing.
    Rule:
      - if next_due is NaT/None and last_watered + water_interval_days exists => compute
      - otherwise keep existing next_due
    """
    df = df.copy()

    if "next_due" not in df.columns:
        df["next_due"] = pd.NaT

    # make sure parsing is consistent
    df["next_due"] = pd.to_datetime(df["next_due"], errors="coerce").dt.date if df["next_due"].dtype != object else df["next_due"]

    if "last_watered" in df.columns:
        last = pd.to_datetime(df["last_watered"], errors="coerce")
    else:
        last = pd.to_datetime(pd.Series([None] * len(df)))

    if "water_interval_days" in df.columns:
        interval = pd.to_numeric(df["water_interval_days"], errors="coerce")
    else:
        interval = pd.to_numeric(pd.Series([None] * len(df)), errors="coerce")

    # compute where next_due missing
    next_due_series = pd.to_datetime(df["next_due"], errors="coerce")
    missing = next_due_series.isna() & last.notna() & interval.notna()

    computed = (last + pd.to_timedelta(interval, unit="D")).dt.date
    df.loc[missing, "next_due"] = computed[missing]

    return df


def validate_schema(df: pd.DataFrame) -> list[str]:
    problems = []
    for col in REQUIRED_FOR_BASIC:
        if col not in df.columns:
            problems.append(f"Missing required column: {col}")
    return problems


def load_plants() -> pd.DataFrame:
    df = pd.read_excel(PLANTS_FILE)
    df = normalize_df(df)
    df = ensure_next_due(df)
    return df


def save_plants(df: pd.DataFrame) -> None:
    # keep canonical columns; also keep original extra columns
    df.to_excel(PLANTS_FILE, index=False)


# ---------- bot commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I’m PlantBuddy.\n"
        "Commands:\n"
        "/status — what needs watering today\n"
        "/water — mark watering\n"
        "/debug — diagnostics"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text("Schema error:\n" + "\n".join(problems))
            return

        today = date.today()
        # ensure next_due exists and is date
        df = ensure_next_due(df)
        df["next_due"] = pd.to_datetime(df["next_due"], errors="coerce").dt.date

        due = df[df["next_due"].notna() & (df["next_due"] <= today)].copy()

        if due.empty:
            await update.message.reply_text("Nothing to water today.")
            return

        # stable output
        due = due.sort_values(["next_due", "name"], ascending=[True, True])
        lines = []
        for _, r in due.iterrows():
            nd = r["next_due"]
            nm = str(r["name"])
            loc = str(r["location"]) if "location" in due.columns and pd.notna(r.get("location")) else ""
            suffix = f" ({loc})" if loc and loc != "nan" else ""
            lines.append(f"- {nm}{suffix} — due {nd}")

        await update.message.reply_text("Today you should water:\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error in /status:\n{e}")
        raise


async def water(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        df = load_plants()
        problems = validate_schema(df)
        if problems:
            await update.message.reply_text("Schema error:\n" + "\n".join(problems))
            return

        buttons = []
        for _, row in df.iterrows():
            pid = str(row["plant_id"])
            nm = str(row["name"])
            buttons.append([InlineKeyboardButton(nm, callback_data=pid)])

        await update.message.reply_text(
            "Which plant did you water?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        await update.message.reply_text(f"Error in /water:\n{e}")
        raise


async def watered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        plant_id = query.data
        df = load_plants()

        # match id
        def _match(val):
            try:
                return str(int(val)) == str(int(plant_id))
            except Exception:
                return str(val) == str(plant_id)

        mask = df["plant_id"].apply(_match)
        if not mask.any():
            await query.edit_message_text("Plant not found.")
            return

        today = date.today()
        df.loc[mask, "last_watered"] = today

        interval = pd.to_numeric(df.loc[mask, "water_interval_days"].iloc[0], errors="coerce")
        if pd.isna(interval):
            interval = 0

        df.loc[mask, "next_due"] = (pd.to_datetime(today) + pd.to_timedelta(float(interval), unit="D")).date()

        save_plants(df)

        nm = str(df.loc[mask, "name"].iloc[0])
        nd = df.loc[mask, "next_due"].iloc[0]
        await query.edit_message_text(f"Saved: {nm}\nNext due: {nd}")
    except Exception as e:
        await query.edit_message_text(f"Error:\n{e}")
        raise


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = []
        info.append(f"python: {platform.python_version()}")
        info.append(f"platform: {platform.platform()}")
        info.append(f"file: {PLANTS_FILE}")
        info.append(f"cwd: {os.getcwd()}")

        # env presence (not values)
        info.append(f"has TELEGRAM_TOKEN: {'TELEGRAM_TOKEN' in os.environ}")
        info.append(f"has BASE_URL: {'BASE_URL' in os.environ}")

        df_raw = pd.read_excel(PLANTS_FILE)
        info.append(f"raw columns: {list(df_raw.columns)}")

        df = normalize_df(df_raw)
        df = ensure_next_due(df)
        info.append(f"normalized columns: {list(df.columns)}")

        problems = validate_schema(df)
        if problems:
            info.append("schema problems: " + "; ".join(problems))

        # sample rows
        sample = df.head(3)[[c for c in ["plant_id", "name", "last_watered", "water_interval_days", "next_due"] if c in df.columns]]
        info.append("sample:\n" + sample.to_string(index=False))

        await update.message.reply_text("\n".join(info))
    except Exception as e:
        await update.message.reply_text(f"Error in /debug:\n{e}")
        raise


def main():
    token = os.environ["TELEGRAM_TOKEN"]
    base_url = os.environ["BASE_URL"].rstrip("/")
    port = int(os.environ.get("PORT", "10000"))

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("water", water))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(watered))

    # webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=token,
        webhook_url=f"{base_url}/{token}",
    )


if __name__ == "__main__":
    main()
