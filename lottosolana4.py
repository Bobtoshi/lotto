#!/usr/bin/env python3
"""
Production‐style Solana Lottery Bot (WITH OPERATOR CUT)

Features:
  - Users can create or import a Solana wallet.
  - Users buy lottery tickets (daily, weekly, and hourly) which transfer SOL from their wallet to the operator’s wallet.
  - Lottery draws occur automatically at scheduled times.
  - A live status screen shows active lotteries (draw times, ticket sales, prize pools, countdown, predicted winnings).
  - Three winners per draw are chosen with payouts of 70%, 20%, and 10% of the *remaining* pot after operator cut.
  - Users can withdraw funds.
  - Wallet settings allow users to view their private key, import a new wallet, delete their wallet, change style, and update preferences.
  - After wallet creation/import, users must set a short username.
  - The main menu displays the user’s username, current SOL balance, and active ticket count.
  - Wallet Info shows the wallet address as a separate message that is removed when the main menu appears.
  - A Transaction History view (with pagination) is available.
  - A Help/About section explains lottery rules and fees.
  - Periodic notifications are sent to users (daily and weekly) based on their preferences.
  - The UI always refreshes the main menu (old menus are deleted) so that it appears at the bottom.
  
IMPORTANT:
  - Every Solana transaction requires fees (paid in SOL).
  - Test thoroughly on a testnet before deploying on Mainnet.
  - Replace placeholder values (RPC URL, keys, tokens) with your own secure values.
"""

import os
import sys
import logging
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone, time as dt_time
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Optional: Sentry error reporting (install with: pip install sentry-sdk) ---
import sentry_sdk
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=1.0)

if sys.platform != "win32":
    asyncio.WindowsSelectorEventLoopPolicy = asyncio.DefaultEventLoopPolicy

# === Use the Solathon Library ===
from solathon import AsyncClient, Keypair, PublicKey, Transaction
from solathon.core.instructions import transfer

# === CONFIGURATION ===
SOL_RPC_URL = "https://api.mainnet-beta.solana.com"  # Replace with your RPC URL
solana_client = AsyncClient(SOL_RPC_URL)
if not hasattr(solana_client, 'get_latest_blockhas'):
    solana_client.get_latest_blockhas = solana_client.get_latest_blockhash

TICKET_PRICE_SOL = 0.001
WEEKLY_TICKET_PRICE_SOL = TICKET_PRICE_SOL

OPERATOR_PRIVATE_KEY_BASE58 = "5h9ryZAZUaY5RcmXA4x8raS9kVGVzbmrXy2RDpbtJMsqHDswBH9XW9SwLHX6YsMn45BwVMfDz29nsgzKRbw2zdtP"
try:
    OPERATOR_KEYPAIR = Keypair.from_private_key(OPERATOR_PRIVATE_KEY_BASE58)
    OPERATOR_WALLET_ADDRESS = OPERATOR_KEYPAIR.public_key.base58_encode().decode('utf-8')
except Exception as e:
    OPERATOR_KEYPAIR = None
    OPERATOR_WALLET_ADDRESS = None
    logging.error("Operator wallet not configured properly: %s", e)

OPERATOR_PAYOUT_WALLET_ADDRESS_STR = "9zrCrAVyhk7t6hoB15ziE5JZJUSQFdomXzuA8tQ4JMgA"
try:
    OPERATOR_PAYOUT_WALLET_ADDRESS = PublicKey(OPERATOR_PAYOUT_WALLET_ADDRESS_STR)
except Exception as e:
    OPERATOR_PAYOUT_WALLET_ADDRESS = None
    logging.error("Operator payout wallet address not configured properly: %s", e)

TELEGRAM_BOT_TOKEN = "7773715989:AAFdWHld5J1z7cQTMLGcqJToo7eCejHu2f4"  # Replace with your bot token

OPERATOR_CUT_PERCENT = 0.10
SET_USERNAME = 100   # Conversation state for setting username
# We no longer need text-input for language/notification; we use inline keyboards:
SET_LANGUAGE = 101   # For language selection
SET_NOTIFICATION = 102  # For notification frequency

# === LOGGING SETUP ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_WALLET = "solana_wallet_data.db"
DB_LOTTERY = "solana_lottery_data.db"

# === DATABASE INIT & MIGRATION ===
def init_wallet_db():
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            wallet_address TEXT,
            private_key TEXT,
            created_at TEXT,
            style TEXT DEFAULT 'monospace',
            username TEXT DEFAULT '',
            language TEXT DEFAULT 'en',
            notification_frequency TEXT DEFAULT 'immediate'
        )
    """)
    conn.commit()
    conn.close()

def update_wallet_db_schema():
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in c.fetchall()]
    if "style" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN style TEXT DEFAULT 'monospace'")
        conn.commit()
        logger.info("Database schema updated: 'style' column added.")
    if "username" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        conn.commit()
        logger.info("Database schema updated: 'username' column added.")
    if "language" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'en'")
        conn.commit()
        logger.info("Database schema updated: 'language' column added.")
    if "notification_frequency" not in columns:
        c.execute("ALTER TABLE users ADD COLUMN notification_frequency TEXT DEFAULT 'immediate'")
        conn.commit()
        logger.info("Database schema updated: 'notification_frequency' column added.")
    conn.close()

def init_lottery_db():
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS lottery_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            lottery_type TEXT,
            draw_date TEXT,
            ticket_count INTEGER,
            amount_spent REAL,
            won REAL DEFAULT 0.0,
            drawn INTEGER DEFAULT 0,
            purchased_at TEXT,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS lottery_pot (
            lottery_type TEXT PRIMARY KEY,
            pot REAL DEFAULT 0.0,
            ticket_price REAL,
            draw_time TEXT
        )
    """)
    for lottery_type, ticket_price, draw_time in [
        ("hourly", TICKET_PRICE_SOL, "every hour"),
        ("daily", TICKET_PRICE_SOL, "20:00"),
        ("weekly", WEEKLY_TICKET_PRICE_SOL, "Sunday 20:00"),
    ]:
        c.execute(
            "INSERT OR IGNORE INTO lottery_pot (lottery_type, ticket_price, draw_time, pot) VALUES (?, ?, ?, 0.0)",
            (lottery_type, ticket_price, draw_time)
        )
    conn.commit()
    conn.close()

# === DATABASE HELPERS ===
def get_user(telegram_id: int):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("SELECT telegram_id, wallet_address, private_key, created_at, style, username, language, notification_frequency FROM users WHERE telegram_id=?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row

def create_user(telegram_id: int, wallet_address: str, private_key: str, style: str = "monospace", username: str = "", language: str = "en", notification_frequency: str = "immediate"):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (telegram_id, wallet_address, private_key, created_at, style, username, language, notification_frequency) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (telegram_id, wallet_address, private_key, datetime.now(timezone.utc).isoformat(), style, username, language, notification_frequency)
    )
    conn.commit()
    conn.close()

def update_username(telegram_id: int, username: str):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("UPDATE users SET username=? WHERE telegram_id=?", (username, telegram_id))
    conn.commit()
    conn.close()

def update_language(telegram_id: int, language: str):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("UPDATE users SET language=? WHERE telegram_id=?", (language, telegram_id))
    conn.commit()
    conn.close()

def update_notification_frequency(telegram_id: int, frequency: str):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("UPDATE users SET notification_frequency=? WHERE telegram_id=?", (frequency, telegram_id))
    conn.commit()
    conn.close()

def update_user_style(telegram_id: int, style: str):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("UPDATE users SET style=? WHERE telegram_id=?", (style, telegram_id))
    conn.commit()
    conn.close()

def delete_user(telegram_id: int):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()

def add_ticket(telegram_id: int, lottery_type: str, draw_date: str, ticket_count: int, amount_spent: float):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("INSERT INTO lottery_tickets (telegram_id, lottery_type, draw_date, ticket_count, amount_spent, purchased_at) VALUES (?, ?, ?, ?, ?, ?)",
              (telegram_id, lottery_type, draw_date, ticket_count, amount_spent, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

def get_user_tickets(telegram_id: int):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("SELECT lottery_type, draw_date, ticket_count, amount_spent, won, drawn, purchased_at FROM lottery_tickets WHERE telegram_id=? ORDER BY purchased_at DESC",
              (telegram_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_lottery_pot(lottery_type: str, additional_amount: float):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("UPDATE lottery_pot SET pot = pot + ? WHERE lottery_type=?", (additional_amount, lottery_type))
    conn.commit()
    conn.close()

def reset_lottery_pot(lottery_type: str):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("UPDATE lottery_pot SET pot = 0.0 WHERE lottery_type=?", (lottery_type,))
    conn.commit()
    conn.close()

def get_lottery_info(lottery_type: str):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("SELECT pot, ticket_price, draw_time FROM lottery_pot WHERE lottery_type=?", (lottery_type,))
    row = c.fetchone()
    conn.close()
    return row

def mark_tickets_drawn(lottery_type: str, draw_date: str):
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("UPDATE lottery_tickets SET drawn = 1 WHERE lottery_type=? AND draw_date=? AND drawn=0",
              (lottery_type, draw_date))
    conn.commit()
    conn.close()

# === SOL WALLET FUNCTIONS ===
def create_new_wallet():
    keypair = Keypair()
    wallet_address = keypair.public_key.base58_encode().decode('utf-8')
    private_key_base58 = keypair.private_key.base58_encode().decode('utf-8')
    print("Created wallet_address:", wallet_address, "Length:", len(wallet_address))
    return wallet_address, private_key_base58

def import_wallet_from_secret(secret_key_str: str):
    try:
        keypair = Keypair.from_private_key(secret_key_str)
        return keypair.public_key.base58_encode().decode('utf-8'), secret_key_str
    except Exception as e:
        logger.error("Failed to import wallet: %s", e)
        return None, None

async def get_onchain_balance(wallet_address: str) -> float:
    try:
        response = await solana_client.get_balance(PublicKey(wallet_address.strip()))
        if isinstance(response, dict):
            lamports_value = response["result"]["value"]
        else:
            lamports_value = response
        return float(lamports_value) / 1_000_000_000
    except Exception as e:
        logger.error("Error getting balance: %s", e)
        return 0.0

async def transfer_sol(from_private_key: str, to_address: str, amount_sol: float, *, max_transfer_if_insufficient: bool = False) -> str:
    try:
        sender = Keypair.from_private_key(from_private_key)
        sender_address = sender.public_key.base58_encode().decode('utf-8')
        sender_balance = await get_onchain_balance(sender_address)
        MIN_RENT_EXEMPT_SOL = 0.002
        max_transferable = sender_balance - MIN_RENT_EXEMPT_SOL
        if max_transferable <= 0:
            logger.error("Sender's balance (%.6f SOL) is insufficient for the minimum rent exemption.", sender_balance)
            return None
        if not max_transfer_if_insufficient:
            if sender_balance < amount_sol + MIN_RENT_EXEMPT_SOL:
                logger.error("Sender's balance (%.6f SOL) insufficient for %.6f SOL transfer plus minimum rent exemption (%.6f SOL).",
                             sender_balance, amount_sol, MIN_RENT_EXEMPT_SOL)
                return None
        else:
            if amount_sol > max_transferable:
                logger.warning("Requested %.6f SOL exceeds maximum transferable %.6f SOL. Adjusting.", amount_sol, max_transferable)
                amount_sol = max_transferable
        receiver = PublicKey(to_address.strip())
        lamports = int(amount_sol * 1_000_000_000)
        instruction = transfer(
            from_public_key=sender.public_key,
            to_public_key=receiver,
            lamports=lamports
        )
        transaction = Transaction(instructions=[instruction], signers=[sender])
        blockhash_info = await solana_client.get_latest_blockhash()
        actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
        transaction.recent_blockhash = actual_blockhash
        tx_result = await solana_client.send_transaction(transaction)
        return tx_result
    except Exception as e:
        logger.error("Error in transfer_sol: %s", e)
        return None

# === TIME & DRAW SCHEDULING HELPERS ===
def get_next_draw_datetime(lottery_type: str) -> datetime:
    now = datetime.now(timezone.utc)
    if lottery_type == "hourly":
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return next_hour
    elif lottery_type == "daily":
        draw_today = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return draw_today if now < draw_today else draw_today + timedelta(days=1)
    elif lottery_type == "weekly":
        days_ahead = (6 - now.weekday()) % 7
        draw_datetime = now.replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        return draw_datetime if now < draw_datetime else draw_datetime + timedelta(days=7)
    else:
        return now

def get_countdown_str(lottery_type: str) -> str:
    next_draw = get_next_draw_datetime(lottery_type)
    now = datetime.now(timezone.utc)
    seconds = int((next_draw - now).total_seconds())
    if seconds < 0:
        seconds = 0
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs:02d}h {mins:02d}m {secs:02d}s"

def get_tickets_stats(lottery_type: str) -> (int, float):
    next_draw_iso = get_next_draw_datetime(lottery_type).isoformat()
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("SELECT SUM(ticket_count) FROM lottery_tickets WHERE lottery_type=? AND draw_date=? AND drawn=0",
              (lottery_type, next_draw_iso))
    row = c.fetchone()
    conn.close()
    total_tickets = row[0] if row[0] else 0
    lottery_info = get_lottery_info(lottery_type)
    current_pot = lottery_info[0] if lottery_info else 0.0
    return total_tickets, current_pot

def get_live_status_text() -> str:
    hourly_draw = get_next_draw_datetime("hourly")
    hourly_tickets, hourly_pot = get_tickets_stats("hourly")
    hourly_countdown = get_countdown_str("hourly")
    daily_draw = get_next_draw_datetime("daily")
    weekly_draw = get_next_draw_datetime("weekly")
    daily_tickets, daily_pot = get_tickets_stats("daily")
    weekly_tickets, weekly_pot = get_tickets_stats("weekly")
    daily_countdown = get_countdown_str("daily")
    weekly_countdown = get_countdown_str("weekly")
    text = (
        "Live Lottery Status\n\n"
        "Hourly Lottery\n"
        f"Draw Time: {hourly_draw.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"Tickets Purchased: {hourly_tickets}\n"
        f"Current Pot: {hourly_pot:.4f} SOL\n"
        f"Time Remaining: {hourly_countdown}\n\n"
        "Daily Lottery\n"
        f"Draw Time: {daily_draw.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"Tickets Purchased: {daily_tickets}\n"
        f"Current Pot: {daily_pot:.4f} SOL\n"
        f"Time Remaining: {daily_countdown}\n\n"
        "Weekly Lottery\n"
        f"Draw Time: {weekly_draw.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"Tickets Purchased: {weekly_tickets}\n"
        f"Current Pot: {weekly_pot:.4f} SOL\n"
        f"Time Remaining: {weekly_countdown}"
    )
    return text

def get_lottery_info_text(lottery_type: str) -> str:
    next_draw = get_next_draw_datetime(lottery_type)
    tickets, pot = get_tickets_stats(lottery_type)
    countdown = get_countdown_str(lottery_type)
    return (f"Draw Time: {next_draw.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"Tickets Purchased: {tickets}\n"
            f"Current Pot: {pot:.4f} SOL\n"
            f"Time Remaining: {countdown}")

def schedule_draw(lottery_type: str, job_queue):
    next_draw_dt = get_next_draw_datetime(lottery_type)
    now = datetime.now(timezone.utc)
    delay = (next_draw_dt - now).total_seconds()
    if delay < 0:
        delay = 0
    job_queue.run_once(
        draw_job_callback,
        delay,
        name=lottery_type,
        data={'lottery_type': lottery_type, 'draw_datetime': next_draw_dt}
    )
    logger.info("Scheduled next %s draw at %s (in %.0f seconds)", lottery_type, next_draw_dt.isoformat(), delay)

async def draw_job_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    lottery_type = data['lottery_type']
    scheduled_draw_dt = data['draw_datetime']
    draw_date_str = scheduled_draw_dt.isoformat()
    logger.info("Initiating %s lottery draw for scheduled draw time %s", lottery_type, draw_date_str)
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("SELECT telegram_id, ticket_count FROM lottery_tickets WHERE lottery_type=? AND draw_date=? AND drawn=0",
              (lottery_type, draw_date_str))
    rows = c.fetchall()
    conn.close()
    if not rows:
        result_message = f"{lottery_type.title()} Lottery Draw:\nNo tickets were purchased for the draw scheduled at {draw_date_str}."
    else:
        entries = []
        for telegram_id, ticket_count in rows:
            entries.extend([telegram_id] * ticket_count)
        winners = []
        remaining_entries = list(entries)
        if remaining_entries:
            first = secrets.choice(remaining_entries)
            winners.append(first)
            remaining_entries = [x for x in remaining_entries if x != first]
        if remaining_entries:
            second = secrets.choice(remaining_entries)
            winners.append(second)
            remaining_entries = [x for x in remaining_entries if x != second]
        if remaining_entries:
            third = secrets.choice(remaining_entries)
            winners.append(third)
        lottery_info = get_lottery_info(lottery_type)
        total_pot = lottery_info[0] if lottery_info else 0.0
        operator_cut = total_pot * OPERATOR_CUT_PERCENT
        remaining_pot = total_pot - operator_cut
        if OPERATOR_PAYOUT_WALLET_ADDRESS:
            operator_cut_tx_hash = await transfer_sol(OPERATOR_PRIVATE_KEY_BASE58, str(OPERATOR_PAYOUT_WALLET_ADDRESS), operator_cut)
            if operator_cut_tx_hash:
                logger.info(f"Operator cut of {operator_cut:.4f} SOL transferred to {OPERATOR_PAYOUT_WALLET_ADDRESS_STR}. TX: {operator_cut_tx_hash}")
            else:
                logger.error(f"Failed to transfer operator cut of {operator_cut:.4f} SOL.")
        else:
            logger.warning("Operator payout wallet address not configured. Operator cut not transferred.")
            remaining_pot = total_pot
        prizes = [remaining_pot * 0.70, remaining_pot * 0.20, remaining_pot * 0.10]
        results = []
        for i, winner_id in enumerate(winners):
            prize = prizes[i] if i < len(prizes) else 0.0
            winner = get_user(winner_id)
            if winner:
                tx_hash = await transfer_sol(OPERATOR_PRIVATE_KEY_BASE58, winner[1], prize, max_transfer_if_insufficient=True)
                if tx_hash:
                    results.append((winner_id, prize, tx_hash))
                    conn = sqlite3.connect(DB_LOTTERY)
                    c = conn.cursor()
                    c.execute("UPDATE lottery_tickets SET won = won + ? WHERE telegram_id=? AND lottery_type=? AND draw_date=?",
                              (prize, winner_id, lottery_type, draw_date_str))
                    conn.commit()
                    conn.close()
                else:
                    results.append((winner_id, prize, "Transfer failed"))
            else:
                results.append((winner_id, prize, "Winner not found"))
        result_message = f"{lottery_type.title()} Lottery Draw Results\n"
        if OPERATOR_PAYOUT_WALLET_ADDRESS:
            result_message += f"\nOperator Cut ({OPERATOR_CUT_PERCENT*100:.0f}%): {operator_cut:.4f} SOL transferred to {OPERATOR_PAYOUT_WALLET_ADDRESS_STR}"
        if results:
            for idx, (winner_id, prize, tx) in enumerate(results, start=1):
                winner_user = get_user(winner_id)
                username = winner_user[5] if winner_user and winner_user[5] else f"User {winner_id}"
                result_message += f"\n\n{idx} Place: {username}\nPrize: {prize:.4f} SOL\nTX: {tx}"
        else:
            result_message += "\nNo winners selected."
    participants = set([row[0] for row in rows])
    broadcast_message = f"{lottery_type.title()} Lottery Draw Completed!\n\n" + result_message + "\n\nThank you for participating in this draw."
    for participant in participants:
        try:
            await context.bot.send_message(chat_id=participant, text=style_message(broadcast_message, participant), parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send broadcast to Telegram ID %s: %s", participant, e)
    logger.info(result_message)
    schedule_draw(lottery_type, context.job_queue)

# === PERIODIC NOTIFICATIONS ===
async def daily_notification_job(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE notification_frequency=?", ("daily",))
    users = c.fetchall()
    conn.close()
    hourly = get_next_draw_datetime("hourly").strftime("%H:%M UTC")
    daily = get_next_draw_datetime("daily").strftime("%H:%M UTC")
    weekly = get_next_draw_datetime("weekly").strftime("%A %H:%M UTC")
    message = (f"Daily Update:\nUpcoming Draws:\n"
               f"Hourly: {hourly}\nDaily: {daily}\nWeekly: {weekly}\n"
               "Don't forget to buy your tickets!")
    for (telegram_id,) in users:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=style_message(message, telegram_id), parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send daily notification to %s: %s", telegram_id, e)

async def weekly_notification_job(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(timezone.utc).weekday() != 6:  # Only send on Sunday
        return
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE notification_frequency=?", ("weekly",))
    users = c.fetchall()
    conn.close()
    hourly = get_next_draw_datetime("hourly").strftime("%H:%M UTC")
    daily = get_next_draw_datetime("daily").strftime("%H:%M UTC")
    weekly = get_next_draw_datetime("weekly").strftime("%A %H:%M UTC")
    message = (f"Weekly Update:\nUpcoming Draws:\n"
               f"Hourly: {hourly}\nDaily: {daily}\nWeekly: {weekly}\n"
               "Good luck and don't forget to buy your tickets!")
    for (telegram_id,) in users:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=style_message(message, telegram_id), parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send weekly notification to %s: %s", telegram_id, e)

# === TRANSACTION HISTORY (Pagination) ===
async def transaction_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    tickets = get_user_tickets(telegram_id)
    page = 1
    data = query.data
    if data.startswith("history_page_"):
        try:
            page = int(data.split("_")[-1])
        except:
            page = 1
    page_size = 5
    total = len(tickets)
    start = (page - 1) * page_size
    end = start + page_size
    page_tickets = tickets[start:end]
    if not page_tickets:
        text = "No transaction history available."
    else:
        lines = [f"Transaction History (Page {page}):"]
        for lottery_type, draw_date, ticket_count, amount_spent, won, drawn, purchased_at in page_tickets:
            status = "Won" if won > 0 else ("Drawn" if drawn else "Pending")
            lines.append(f"{purchased_at}: {lottery_type.title()} – {ticket_count} ticket(s), Spent: {amount_spent} SOL, Won: {won} SOL, Status: {status}")
        text = "\n".join(lines)
    kb_buttons = []
    if start > 0:
        kb_buttons.append(InlineKeyboardButton("Previous", callback_data=f"history_page_{page-1}"))
    if end < total:
        kb_buttons.append(InlineKeyboardButton("Next", callback_data=f"history_page_{page+1}"))
    kb_buttons.append(InlineKeyboardButton("Main Menu", callback_data="main_menu"))
    keyboard = [kb_buttons]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# === LANGUAGE & NOTIFICATION INLINE MENUS ===
async def language_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("English", callback_data="lang_en")],
        [InlineKeyboardButton("Spanish", callback_data="lang_es")],
        [InlineKeyboardButton("French", callback_data="lang_fr")],
        [InlineKeyboardButton("Back", callback_data="settings_menu")]
    ]
    await update.callback_query.edit_message_text(
        text=style_message("Select your language:", update.effective_user.id),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split("_")[-1]
    telegram_id = update.effective_user.id
    update_language(telegram_id, lang_code)
    await query.edit_message_text(
        text=style_message(f"Language set to {lang_code.upper()}.", telegram_id),
        parse_mode="HTML"
    )
    await show_main_menu(update, context)

async def notification_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Immediate", callback_data="notif_immediate")],
        [InlineKeyboardButton("Daily", callback_data="notif_daily")],
        [InlineKeyboardButton("Weekly", callback_data="notif_weekly")],
        [InlineKeyboardButton("Back", callback_data="settings_menu")]
    ]
    await update.callback_query.edit_message_text(
        text=style_message("Select your notification frequency:", update.effective_user.id),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def set_notification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    frequency = query.data.split("_")[-1]
    telegram_id = update.effective_user.id
    update_notification_frequency(telegram_id, frequency)
    await query.edit_message_text(
        text=style_message(f"Notification frequency set to {frequency}.", telegram_id),
        parse_mode="HTML"
    )
    await show_main_menu(update, context)

# === HELP/ABOUT SECTION ===
async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "About the Lottery Bot:\n\n"
        "• Each lottery ticket costs a fixed amount of SOL and incurs transaction fees.\n"
        "• Lottery draws occur automatically at scheduled times.\n"
        "• Prizes are distributed as 70%, 20%, and 10% of the remaining pot after a 10% operator cut.\n"
        "• Use the Transaction History to review your past entries.\n"
        "• You can set your language and notification preferences in Settings.\n\n"
        "For more help, contact support."
    )
    keyboard = [[InlineKeyboardButton("Back", callback_data="settings_menu")]]
    await query.edit_message_text(
        text=style_message(text, update.effective_user.id),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

# === WALLET SETTINGS HANDLERS ===
async def wallet_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📋 View Private Key", callback_data="view_private_key")],
        [InlineKeyboardButton("🔄 Import New Wallet", callback_data="import_wallet")],
        [InlineKeyboardButton("❌ Delete Wallet", callback_data="delete_wallet")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    text = "Wallet Settings\n\nSelect an option:"
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def view_private_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or not user[2]:
        await query.edit_message_text(text=style_message("No private key found.", telegram_id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]), parse_mode="HTML")
        return
    text = "Your Private Key (Base58 encoded). Below is a separate message."
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]), parse_mode="HTML")
    await query.message.chat.send_message(text=f"<code>{user[2]}</code>", parse_mode="HTML")

async def delete_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Delete", callback_data="confirm_delete_wallet")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    text = "Are you sure you want to delete your wallet? This action is irreversible."
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def confirm_delete_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    delete_user(telegram_id)
    await query.edit_message_text(text=style_message("Your wallet has been deleted. Please use /start to create a new wallet.", telegram_id), parse_mode="HTML")

async def copy_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user:
        wallet_address = user[1]
        await query.answer(text=f"{wallet_address}\n\n(Copied to clipboard)", show_alert=True)
    else:
        await query.answer(text="No wallet found.", show_alert=True)

async def copy_private_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user and user[2]:
        private_key = user[2]
        await query.answer(text=f"{private_key}\n\n(Copied to clipboard)", show_alert=True)
    else:
        await query.answer(text="No wallet found.", show_alert=True)

# === STYLE SETTINGS HANDLERS ===
async def change_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Monospace", callback_data="set_style_monospace")],
        [InlineKeyboardButton("Fancy", callback_data="set_style_fancy")],
        [InlineKeyboardButton("Bold", callback_data="set_style_bold")],
        [InlineKeyboardButton("Italic", callback_data="set_style_italic")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    text = "Select your preferred style:"
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def set_style_monospace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "monospace")
    await query.edit_message_text(text=style_message("Style updated to Monospace.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_fancy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "fancy")
    await query.edit_message_text(text=style_message("Style updated to Fancy.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_bold_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "bold")
    await query.edit_message_text(text=style_message("Style updated to Bold.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_italic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "italic")
    await query.edit_message_text(text=style_message("Style updated to Italic.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

# === USERNAME SETTING CONVERSATION HANDLERS ===
async def set_username_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please enter your new short username:", update.effective_user.id), parse_mode="HTML")
    return SET_USERNAME

async def receive_username_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    new_username = update.message.text.strip()
    update_username(telegram_id, new_username)
    await update.message.reply_text(style_message(f"Your username has been updated to: {new_username}", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)
    return ConversationHandler.END

# === LANGUAGE SETTING CONVERSATION HANDLERS (now using inline keyboards) ===
# (Handlers for language are defined above as language_menu_callback and set_language_callback)

# === NOTIFICATION SETTING CONVERSATION HANDLERS (using inline keyboards) ===
# (Handlers for notifications are defined above as notification_menu_callback and set_notification_callback)

# === HELP/ABOUT SECTION ===
async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "About the Lottery Bot:\n\n"
        "• Each lottery ticket costs a fixed amount of SOL and incurs transaction fees.\n"
        "• Lottery draws occur automatically at scheduled times.\n"
        "• Prizes are distributed as 70%, 20%, and 10% of the remaining pot after a 10% operator cut.\n"
        "• Use the Transaction History to review your past entries.\n"
        "• You can set your language and notification preferences in Settings.\n\n"
        "For more help, contact support."
    )
    keyboard = [[InlineKeyboardButton("Back", callback_data="settings_menu")]]
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# === WITHDRAWAL CONVERSATION HANDLER ===
WITHDRAW_ADDRESS, WITHDRAW_AMOUNT = range(2, 4)

async def withdraw_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please enter the destination Solana wallet address:", update.effective_user.id), parse_mode="HTML")
    return WITHDRAW_ADDRESS

async def receive_withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    try:
        PublicKey(address)
    except Exception:
        await update.message.reply_text("Invalid wallet address. Please enter a valid Solana address:")
        return WITHDRAW_ADDRESS
    context.user_data["withdraw_address"] = address
    await update.message.reply_text("Please enter the amount of SOL to withdraw:")
    return WITHDRAW_AMOUNT

async def receive_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except Exception:
        await update.message.reply_text("Invalid amount. Please enter a positive number:")
        return WITHDRAW_AMOUNT
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await update.message.reply_text("User not found. Please use /start to set up your wallet.")
        return ConversationHandler.END
    wallet_address = user[1]
    current_balance = await get_onchain_balance(wallet_address)
    if amount > current_balance:
        await update.message.reply_text(f"Insufficient funds. Your current balance is {current_balance:.4f} SOL. Please enter a lower amount:")
        return WITHDRAW_AMOUNT
    tx_hash = await transfer_sol(user[2], context.user_data.get("withdraw_address"), amount)
    if tx_hash:
        await update.message.reply_text(f"Withdrawal successful!\nTransaction hash: {tx_hash}")
    else:
        await update.message.reply_text("Withdrawal failed. Please try again later.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# === TICKET PURCHASE HANDLERS ===
async def cancel_buy_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if "pending_purchase" in context.user_data:
        context.user_data.pop("pending_purchase")
    keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text=style_message("Ticket purchase cancelled.", update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await show_main_menu(update, context)

async def buy_ticket_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("daily")
    ticket_price = lottery_info[1] if lottery_info else TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "daily", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_daily")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("daily").isoformat()
        add_ticket(telegram_id, "daily", draw_dt, 1, ticket_price)
        update_lottery_pot("daily", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

async def buy_ticket_weekly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("weekly")
    ticket_price = lottery_info[1] if lottery_info else WEEKLY_TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "weekly", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_weekly")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_weekly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("weekly").isoformat()
        add_ticket(telegram_id, "weekly", draw_dt, 1, ticket_price)
        update_lottery_pot("weekly", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

# === HOURLY LOTTERY HANDLERS ===
async def hourly_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Hourly Lottery\n\n" + get_lottery_info_text("hourly")
    keyboard = [
        [InlineKeyboardButton("Buy Hourly Ticket", callback_data="buy_ticket_hourly")],
        [InlineKeyboardButton("Back to Lottery Menu", callback_data="lotteries_menu")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def buy_ticket_hourly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("hourly")
    ticket_price = lottery_info[1] if lottery_info else TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "hourly", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_hourly")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_hourly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("hourly").isoformat()
        add_ticket(telegram_id, "hourly", draw_dt, 1, ticket_price)
        update_lottery_pot("hourly", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

# === LOTTERIES MENU & CALLBACKS ===
async def lotteries_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hourly_info = get_lottery_info("hourly")
    daily_info = get_lottery_info("daily")
    weekly_info = get_lottery_info("weekly")
    hourly_draw = get_next_draw_datetime("hourly").strftime('%H:%M')
    daily_draw = get_next_draw_datetime("daily").strftime('%H:%M')
    weekly_draw = get_next_draw_datetime("weekly").strftime('%A %H:%M')
    keyboard = [
        [InlineKeyboardButton(f"Hourly Lottery - {hourly_info[1]} SOL\nNext Draw: {hourly_draw}", callback_data="hourly_lottery")],
        [InlineKeyboardButton(f"Daily Lottery - {daily_info[1]} SOL\nNext Draw: {daily_draw}", callback_data="daily_lottery")],
        [InlineKeyboardButton(f"Weekly Lottery - {weekly_info[1]} SOL\nNext Draw: {weekly_draw}", callback_data="weekly_lottery")],
        [InlineKeyboardButton("Back to Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message("Select a lottery:", update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def daily_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Daily Lottery\n\n" + get_lottery_info_text("daily")
    keyboard = [
        [InlineKeyboardButton("Buy Daily Ticket", callback_data="buy_ticket_daily")],
        [InlineKeyboardButton("Back to Lottery Menu", callback_data="lotteries_menu")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def weekly_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Weekly Lottery\n\n" + get_lottery_info_text("weekly")
    keyboard = [
        [InlineKeyboardButton("Buy Weekly Ticket", callback_data="buy_ticket_weekly")],
        [InlineKeyboardButton("Back to Lottery Menu", callback_data="lotteries_menu")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# === MAIN TELEGRAM BOT HANDLERS ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user is None:
        help_text = (
            "Welcome to the Solana Lottery Bot!\n\n"
            "• Create or import a wallet to get started.\n"
            "• After wallet creation, set your username and preferences.\n"
            "• Use the main menu to access lotteries, view your tickets, check live status, and more.\n"
            "• For help, check the Help/About section in Settings."
        )
        keyboard = [
            [InlineKeyboardButton("Create Wallet", callback_data="create_wallet")],
            [InlineKeyboardButton("Import Wallet", callback_data="import_wallet")],
            [InlineKeyboardButton("Help/About", callback_data="help")]
        ]
        await update.message.reply_text(text=style_message(help_text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await show_main_menu(update, context)

async def create_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    if get_user(telegram_id) is not None:
        await query.edit_message_text(text=style_message("You already have a wallet.", telegram_id), parse_mode="HTML")
        return
    wallet_address, private_key_base58 = create_new_wallet()
    create_user(telegram_id, wallet_address, private_key_base58)
    text = (f"Wallet Created Successfully!\n\nAddress: {wallet_address}\n\nPrivate Key (Base58 Encoded):\n{private_key_base58}\n\nPlease set a short username for yourself.")
    keyboard = [[InlineKeyboardButton("Set Username", callback_data="set_username_start")]]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def import_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please send your Solana wallet private key (Base58 encoded) to import:", update.effective_user.id), parse_mode="HTML")
    return 1

async def receive_import_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    private_key_input = update.message.text.strip()
    wallet_address, private_key_base58 = import_wallet_from_secret(private_key_input)
    if not wallet_address:
        await update.message.reply_text("Failed to import wallet. Please ensure the private key is valid (Base58 encoded).")
        return ConversationHandler.END
    create_user(telegram_id, wallet_address, private_key_base58)
    text = f"Wallet imported successfully!\n\nAddress: {wallet_address}\n\nPlease set a short username for yourself."
    keyboard = [[InlineKeyboardButton("Set Username", callback_data="set_username_start")]]
    await update.message.reply_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ConversationHandler.END

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# --- Modified Main Menu Function ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    # Delete previous main menu and wallet address messages if they exist.
    if "main_menu_msg_id" in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data["main_menu_msg_id"])
        except Exception as e:
            logger.error("Failed to delete previous main menu message: %s", e)
        context.user_data.pop("main_menu_msg_id", None)
    if "wallet_address_msg_id" in context.user_data:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data["wallet_address_msg_id"])
        except Exception as e:
            logger.error("Failed to delete wallet address message: %s", e)
        context.user_data.pop("wallet_address_msg_id", None)
    user = get_user(telegram_id)
    if user:
        username = user[5] if user[5] else f"User {telegram_id}"
        balance = await get_onchain_balance(user[1])
        tickets = get_user_tickets(telegram_id)
        active_ticket_count = sum(ticket[2] for ticket in tickets if ticket[5] == 0) if tickets else 0
        info_text = f"Username: {username}\nBalance: {balance:.4f} SOL\nActive Tickets: {active_ticket_count}\n\n"
    else:
        info_text = ""
    keyboard = [
        [InlineKeyboardButton("Lotteries", callback_data="lotteries_menu")],
        [InlineKeyboardButton("Live Status", callback_data="live_status")],
        [InlineKeyboardButton("Wallet Info", callback_data="wallet_info")],
        [InlineKeyboardButton("My Tickets", callback_data="view_tickets")],
        [InlineKeyboardButton("Transaction History", callback_data="history")],
        [InlineKeyboardButton("Settings", callback_data="settings_menu")],
        [InlineKeyboardButton("Help/About", callback_data="help")]
    ]
    text = info_text + "Main Menu\n\nSelect an option below:"
    sent_message = await context.bot.send_message(chat_id=update.effective_chat.id, text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    context.user_data["main_menu_msg_id"] = sent_message.message_id

async def live_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    text = get_live_status_text()
    keyboard = [
        [InlineKeyboardButton("Refresh", callback_data="live_status")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def wallet_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id), parse_mode="HTML")
        return
    balance = await get_onchain_balance(user[1])
    text = f"Wallet Info\n\nBalance: {balance:.4f} SOL"
    keyboard = [
        [InlineKeyboardButton("Withdraw Funds", callback_data="withdraw_funds")],
        [InlineKeyboardButton("Wallet Settings", callback_data="wallet_settings")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    wallet_msg = await query.message.chat.send_message(text=f"<code>{user[1]}</code>", parse_mode="HTML")
    context.user_data["wallet_address_msg_id"] = wallet_msg.message_id

async def view_tickets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    tickets = get_user_tickets(telegram_id)
    if not tickets:
        text = "You have not purchased any tickets yet."
    else:
        lines = ["Your Tickets:"]
        for lottery_type, draw_date, ticket_count, amount_spent, won, drawn, purchased_at in tickets:
            status = "Won" if won and won > 0 else ("Drawn" if drawn else "Pending")
            lines.append(f"{lottery_type.title()} (Draw: {draw_date}) – {ticket_count} ticket(s), Spent: {amount_spent} SOL, Status: {status}")
        text = "\n".join(lines)
    keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def stats_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    conn = sqlite3.connect(DB_LOTTERY)
    c = conn.cursor()
    c.execute("SELECT SUM(amount_spent), SUM(won) FROM lottery_tickets WHERE telegram_id=?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    total_spent = row[0] if row[0] else 0.0
    total_won = row[1] if row[1] else 0.0
    text = f"Your Stats\n\nTotal Spent: {total_spent} SOL\nTotal Won: {total_won} SOL"
    keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Wallet Settings", callback_data="wallet_settings")],
        [InlineKeyboardButton("Change Style", callback_data="change_style")],
        [InlineKeyboardButton("Set Username", callback_data="set_username_start")],
        [InlineKeyboardButton("Set Language", callback_data="set_language_start")],
        [InlineKeyboardButton("Set Notification", callback_data="set_notification_start")],
        [InlineKeyboardButton("Help/About", callback_data="help")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message("Settings\n\nSelect an option:", update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# === TRANSACTION HISTORY HANDLER ===
async def transaction_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    tickets = get_user_tickets(telegram_id)
    page = 1
    data = query.data
    if data.startswith("history_page_"):
        try:
            page = int(data.split("_")[-1])
        except:
            page = 1
    page_size = 5
    total = len(tickets)
    start = (page - 1) * page_size
    end = start + page_size
    page_tickets = tickets[start:end]
    if not page_tickets:
        text = "No transaction history available."
    else:
        lines = [f"Transaction History (Page {page}):"]
        for lottery_type, draw_date, ticket_count, amount_spent, won, drawn, purchased_at in page_tickets:
            status = "Won" if won > 0 else ("Drawn" if drawn else "Pending")
            lines.append(f"{purchased_at}: {lottery_type.title()} – {ticket_count} ticket(s), Spent: {amount_spent} SOL, Won: {won} SOL, Status: {status}")
        text = "\n".join(lines)
    kb_buttons = []
    if start > 0:
        kb_buttons.append(InlineKeyboardButton("Previous", callback_data=f"history_page_{page-1}"))
    if end < total:
        kb_buttons.append(InlineKeyboardButton("Next", callback_data=f"history_page_{page+1}"))
    kb_buttons.append(InlineKeyboardButton("Main Menu", callback_data="main_menu"))
    keyboard = [kb_buttons]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# === PERIODIC NOTIFICATIONS ===
async def daily_notification_job(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE notification_frequency=?", ("daily",))
    users = c.fetchall()
    conn.close()
    hourly = get_next_draw_datetime("hourly").strftime("%H:%M UTC")
    daily = get_next_draw_datetime("daily").strftime("%H:%M UTC")
    weekly = get_next_draw_datetime("weekly").strftime("%A %H:%M UTC")
    message = (f"Daily Update:\nUpcoming Draws:\n"
               f"Hourly: {hourly}\nDaily: {daily}\nWeekly: {weekly}\nDon't forget to buy your tickets!")
    for (telegram_id,) in users:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=style_message(message, telegram_id), parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send daily notification to %s: %s", telegram_id, e)

async def weekly_notification_job(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now(timezone.utc).weekday() != 6:
        return
    conn = sqlite3.connect(DB_WALLET)
    c = conn.cursor()
    c.execute("SELECT telegram_id FROM users WHERE notification_frequency=?", ("weekly",))
    users = c.fetchall()
    conn.close()
    hourly = get_next_draw_datetime("hourly").strftime("%H:%M UTC")
    daily = get_next_draw_datetime("daily").strftime("%H:%M UTC")
    weekly = get_next_draw_datetime("weekly").strftime("%A %H:%M UTC")
    message = (f"Weekly Update:\nUpcoming Draws:\n"
               f"Hourly: {hourly}\nDaily: {daily}\nWeekly: {weekly}\nGood luck and buy your tickets!")
    for (telegram_id,) in users:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=style_message(message, telegram_id), parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send weekly notification to %s: %s", telegram_id, e)

# === HELPER: Apply Style Based on User Preference ===
def style_message(text: str, telegram_id: int) -> str:
    user = get_user(telegram_id)
    style = "monospace"
    if user and len(user) >= 5 and user[4]:
        style = user[4]
    if style in ["default", "monospace"]:
        return f"<pre>{text}</pre>"
    elif style == "fancy":
        return f"╔══════════════════╗\n<b><i>{text}</i></b>\n╚══════════════════╝"
    elif style == "bold":
        return f"<b>{text}</b>"
    elif style == "italic":
        return f"<i>{text}</i>"
    else:
        return text

# === WALLET SETTINGS HANDLERS ===
async def wallet_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📋 View Private Key", callback_data="view_private_key")],
        [InlineKeyboardButton("🔄 Import New Wallet", callback_data="import_wallet")],
        [InlineKeyboardButton("❌ Delete Wallet", callback_data="delete_wallet")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    text = "Wallet Settings\n\nSelect an option:"
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def view_private_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or not user[2]:
        await query.edit_message_text(text=style_message("No private key found.", telegram_id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]), parse_mode="HTML")
        return
    text = "Your Private Key (Base58 encoded). Below is a separate message."
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]), parse_mode="HTML")
    await query.message.chat.send_message(text=f"<code>{user[2]}</code>", parse_mode="HTML")

async def delete_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Delete", callback_data="confirm_delete_wallet")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    text = "Are you sure you want to delete your wallet? This action is irreversible."
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def confirm_delete_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    delete_user(telegram_id)
    await query.edit_message_text(text=style_message("Your wallet has been deleted. Please use /start to create a new wallet.", telegram_id), parse_mode="HTML")

async def copy_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user:
        wallet_address = user[1]
        await query.answer(text=f"{wallet_address}\n\n(Copied to clipboard)", show_alert=True)
    else:
        await query.answer(text="No wallet found.", show_alert=True)

async def copy_private_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user and user[2]:
        private_key = user[2]
        await query.answer(text=f"{private_key}\n\n(Copied to clipboard)", show_alert=True)
    else:
        await query.answer(text="No wallet found.", show_alert=True)

# === STYLE SETTINGS HANDLERS ===
async def change_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Monospace", callback_data="set_style_monospace")],
        [InlineKeyboardButton("Fancy", callback_data="set_style_fancy")],
        [InlineKeyboardButton("Bold", callback_data="set_style_bold")],
        [InlineKeyboardButton("Italic", callback_data="set_style_italic")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    text = "Select your preferred style:"
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def set_style_monospace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "monospace")
    await query.edit_message_text(text=style_message("Style updated to Monospace.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_fancy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "fancy")
    await query.edit_message_text(text=style_message("Style updated to Fancy.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_bold_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "bold")
    await query.edit_message_text(text=style_message("Style updated to Bold.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

async def set_style_italic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    update_user_style(telegram_id, "italic")
    await query.edit_message_text(text=style_message("Style updated to Italic.", telegram_id), parse_mode="HTML")
    await settings_menu_callback(update, context)

# === CONVERSATION HANDLERS REGISTRATION ===
# Username, Language, and Notification settings now use inline keyboards.
async def set_username_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please enter your new short username:", update.effective_user.id), parse_mode="HTML")
    return SET_USERNAME

async def receive_username_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    new_username = update.message.text.strip()
    update_username(telegram_id, new_username)
    await update.message.reply_text(style_message(f"Your username has been updated to: {new_username}", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)
    return ConversationHandler.END

# --- LANGUAGE and Notification inline menus are handled by language_menu_callback, set_language_callback,
# notification_menu_callback, and set_notification_callback (defined above).

# === HELP/ABOUT SECTION HANDLERS ===
# help_callback defined above.

# === WITHDRAWAL CONVERSATION HANDLER ===
WITHDRAW_ADDRESS, WITHDRAW_AMOUNT = range(2, 4)

async def withdraw_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please enter the destination Solana wallet address:", update.effective_user.id), parse_mode="HTML")
    return WITHDRAW_ADDRESS

async def receive_withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    try:
        PublicKey(address)
    except Exception:
        await update.message.reply_text("Invalid wallet address. Please enter a valid Solana address:")
        return WITHDRAW_ADDRESS
    context.user_data["withdraw_address"] = address
    await update.message.reply_text("Please enter the amount of SOL to withdraw:")
    return WITHDRAW_AMOUNT

async def receive_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except Exception:
        await update.message.reply_text("Invalid amount. Please enter a positive number:")
        return WITHDRAW_AMOUNT
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await update.message.reply_text("User not found. Please use /start to set up your wallet.")
        return ConversationHandler.END
    wallet_address = user[1]
    current_balance = await get_onchain_balance(wallet_address)
    if amount > current_balance:
        await update.message.reply_text(f"Insufficient funds. Your current balance is {current_balance:.4f} SOL. Please enter a lower amount:")
        return WITHDRAW_AMOUNT
    tx_hash = await transfer_sol(user[2], context.user_data.get("withdraw_address"), amount)
    if tx_hash:
        await update.message.reply_text(f"Withdrawal successful!\nTransaction hash: {tx_hash}")
    else:
        await update.message.reply_text("Withdrawal failed. Please try again later.")
    await show_main_menu(update, context)
    return ConversationHandler.END

# === TICKET PURCHASE HANDLERS ===
async def cancel_buy_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if "pending_purchase" in context.user_data:
        context.user_data.pop("pending_purchase")
    keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
    await query.edit_message_text(text=style_message("Ticket purchase cancelled.", update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await show_main_menu(update, context)

async def buy_ticket_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("daily")
    ticket_price = lottery_info[1] if lottery_info else TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "daily", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_daily")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("daily").isoformat()
        add_ticket(telegram_id, "daily", draw_dt, 1, ticket_price)
        update_lottery_pot("daily", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

async def buy_ticket_weekly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("weekly")
    ticket_price = lottery_info[1] if lottery_info else WEEKLY_TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "weekly", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_weekly")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_weekly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("weekly").isoformat()
        add_ticket(telegram_id, "weekly", draw_dt, 1, ticket_price)
        update_lottery_pot("weekly", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

# === HOURLY LOTTERY HANDLERS ===
async def hourly_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Hourly Lottery\n\n" + get_lottery_info_text("hourly")
    keyboard = [
        [InlineKeyboardButton("Buy Hourly Ticket", callback_data="buy_ticket_hourly")],
        [InlineKeyboardButton("Back to Lottery Menu", callback_data="lotteries_menu")],
        [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=style_message(text, update.effective_user.id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def buy_ticket_hourly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user:
        await query.edit_message_text(text=style_message("User not found. Please use /start to set up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    lottery_info = get_lottery_info("hourly")
    ticket_price = lottery_info[1] if lottery_info else TICKET_PRICE_SOL
    balance = await get_onchain_balance(user[1])
    user_keypair = Keypair.from_private_key(user[2])
    dummy_tx = Transaction(
        instructions=[transfer(
            from_public_key=user_keypair.public_key,
            to_public_key=PublicKey(OPERATOR_WALLET_ADDRESS.strip()),
            lamports=int(ticket_price * 1_000_000_000)
        )],
        signers=[user_keypair]
    )
    blockhash_info = await solana_client.get_latest_blockhash()
    actual_blockhash = blockhash_info["result"]["value"]["blockhash"]
    dummy_tx.recent_blockhash = actual_blockhash
    compiled_msg = dummy_tx.compile_transaction()
    fee_response = await solana_client.get_fee_for_message(compiled_msg)
    if isinstance(fee_response, dict):
        result = fee_response.get("result", {})
        fee_lamports = result.get("fee", 0) if isinstance(result, dict) else result or 0
    else:
        fee_lamports = fee_response
    estimated_fee_sol = fee_lamports / 1_000_000_000 if fee_lamports else 0.0
    total_cost = ticket_price + estimated_fee_sol
    if balance < total_cost:
        await query.edit_message_text(text=style_message(f"Insufficient funds.\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\nPlease top up your wallet.", telegram_id),
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]),
                                      parse_mode="HTML")
        return
    context.user_data["pending_purchase"] = {"lottery_type": "hourly", "ticket_price": ticket_price, "fee": estimated_fee_sol, "total_cost": total_cost}
    await query.edit_message_text(text=style_message(f"Ticket Purchase Details:\nTicket Price: {ticket_price} SOL\nEstimated Fee: {estimated_fee_sol:.6f} SOL\nTotal Cost: {total_cost:.6f} SOL\nYour Balance: {balance:.6f} SOL\n\nDo you want to confirm this purchase?", telegram_id),
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("Confirm Purchase", callback_data="confirm_buy_ticket_hourly")],
                                      [InlineKeyboardButton("Cancel", callback_data="cancel_buy_ticket")],
                                      [InlineKeyboardButton("Main Menu", callback_data="main_menu")]
                                  ]),
                                  parse_mode="HTML")

async def confirm_buy_ticket_hourly_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if not user or "pending_purchase" not in context.user_data:
        await query.edit_message_text(text=style_message("No pending purchase found. Please try again.", telegram_id), parse_mode="HTML")
        return
    purchase = context.user_data.pop("pending_purchase")
    ticket_price = purchase["ticket_price"]
    tx_hash = await transfer_sol(user[2], str(OPERATOR_WALLET_ADDRESS), ticket_price)
    if tx_hash:
        draw_dt = get_next_draw_datetime("hourly").isoformat()
        add_ticket(telegram_id, "hourly", draw_dt, 1, ticket_price)
        update_lottery_pot("hourly", ticket_price)
        new_balance = await get_onchain_balance(user[1])
        keyboard = [[InlineKeyboardButton("Main Menu", callback_data="main_menu")]]
        text = f"Ticket Purchased!\nTicket Price: {ticket_price} SOL\nTX: {tx_hash}\nNew Balance: {new_balance:.4f} SOL"
        await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await query.edit_message_text(text=style_message("Failed to transfer SOL: Transfer failed", telegram_id), parse_mode="HTML")
    await show_main_menu(update, context)

# === MAIN TELEGRAM BOT HANDLERS ===
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user = get_user(telegram_id)
    if user is None:
        help_text = (
            "Welcome to the Solana Lottery Bot!\n\n"
            "• Create or import a wallet to begin.\n"
            "• After setting up your wallet, set your username and preferences (language, notifications).\n"
            "• Use the main menu to view lotteries, check live status, view transaction history, and more.\n"
            "• For additional help, use the Help/About section in Settings."
        )
        keyboard = [
            [InlineKeyboardButton("Create Wallet", callback_data="create_wallet")],
            [InlineKeyboardButton("Import Wallet", callback_data="import_wallet")],
            [InlineKeyboardButton("Help/About", callback_data="help")]
        ]
        await update.message.reply_text(text=style_message(help_text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await show_main_menu(update, context)

async def create_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = query.from_user.id
    if get_user(telegram_id) is not None:
        await query.edit_message_text(text=style_message("You already have a wallet.", telegram_id), parse_mode="HTML")
        return
    wallet_address, private_key_base58 = create_new_wallet()
    create_user(telegram_id, wallet_address, private_key_base58)
    text = (f"Wallet Created Successfully!\n\nAddress: {wallet_address}\n\nPrivate Key (Base58 Encoded):\n{private_key_base58}\n\nPlease set a short username for yourself.")
    keyboard = [[InlineKeyboardButton("Set Username", callback_data="set_username_start")]]
    await query.edit_message_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def import_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text=style_message("Please send your Solana wallet private key (Base58 encoded) to import:", update.effective_user.id), parse_mode="HTML")
    return 1

async def receive_import_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    private_key_input = update.message.text.strip()
    wallet_address, private_key_base58 = import_wallet_from_secret(private_key_input)
    if not wallet_address:
        await update.message.reply_text("Failed to import wallet. Please ensure the private key is valid (Base58 encoded).")
        return ConversationHandler.END
    create_user(telegram_id, wallet_address, private_key_base58)
    text = f"Wallet imported successfully!\n\nAddress: {wallet_address}\n\nPlease set a short username for yourself."
    keyboard = [[InlineKeyboardButton("Set Username", callback_data="set_username_start")]]
    await update.message.reply_text(text=style_message(text, telegram_id), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ConversationHandler.END

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# === SCHEDULED PERIODIC NOTIFICATIONS ===
# Daily notifications at 09:00 UTC; weekly notifications also at 09:00 UTC (but only send on Sunday)
# (Ensure to import dt_time from datetime)
# Already defined daily_notification_job and weekly_notification_job above.

# === MAIN FUNCTION ===
def main():
    print("Available methods on solana_client:", dir(solana_client))
    init_wallet_db()
    update_wallet_db_schema()
    init_lottery_db()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    job_queue = application.job_queue
    schedule_draw("hourly", job_queue)
    schedule_draw("daily", job_queue)
    schedule_draw("weekly", job_queue)
    job_queue.run_daily(daily_notification_job, dt_time(9, 0, tzinfo=timezone.utc))
    job_queue.run_daily(weekly_notification_job, dt_time(9, 0, tzinfo=timezone.utc))
    
    conv_import = ConversationHandler(
        entry_points=[CallbackQueryHandler(import_wallet_callback, pattern="^import_wallet$")],
        states={1: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_import_wallet)]},
        fallbacks=[],
    )
    conv_withdraw = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start_callback, pattern="^withdraw_funds$")],
        states={
            WITHDRAW_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_withdraw_address)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_withdraw_amount)]
        },
        fallbacks=[],
    )
    conv_set_username = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_username_start_callback, pattern="^set_username_start$")],
        states={SET_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username_callback)]},
        fallbacks=[],
    )
    conv_set_language = ConversationHandler(
        entry_points=[CallbackQueryHandler(language_menu_callback, pattern="^set_language_start$")],
        states={},  # No further text input; selection is handled by set_language_callback below.
        fallbacks=[],
    )
    conv_set_notification = ConversationHandler(
        entry_points=[CallbackQueryHandler(notification_menu_callback, pattern="^set_notification_start$")],
        states={},  # Handled by set_notification_callback below.
        fallbacks=[],
    )
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", live_status_callback))
    application.add_handler(conv_import)
    application.add_handler(conv_withdraw)
    application.add_handler(conv_set_username)
    application.add_handler(conv_set_language)
    application.add_handler(conv_set_notification)
    application.add_handler(CallbackQueryHandler(help_callback, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(transaction_history_callback, pattern="^history"))
    
    application.add_handler(CallbackQueryHandler(create_wallet_callback, pattern="^create_wallet$"))
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(lotteries_menu_callback, pattern="^lotteries_menu$"))
    application.add_handler(CallbackQueryHandler(hourly_lottery_callback, pattern="^hourly_lottery$"))
    application.add_handler(CallbackQueryHandler(daily_lottery_callback, pattern="^daily_lottery$"))
    application.add_handler(CallbackQueryHandler(weekly_lottery_callback, pattern="^weekly_lottery$"))
    application.add_handler(CallbackQueryHandler(buy_ticket_daily_callback, pattern="^buy_ticket_daily$"))
    application.add_handler(CallbackQueryHandler(confirm_buy_ticket_daily_callback, pattern="^confirm_buy_ticket_daily$"))
    application.add_handler(CallbackQueryHandler(buy_ticket_weekly_callback, pattern="^buy_ticket_weekly$"))
    application.add_handler(CallbackQueryHandler(confirm_buy_ticket_weekly_callback, pattern="^confirm_buy_ticket_weekly$"))
    application.add_handler(CallbackQueryHandler(buy_ticket_hourly_callback, pattern="^buy_ticket_hourly$"))
    application.add_handler(CallbackQueryHandler(confirm_buy_ticket_hourly_callback, pattern="^confirm_buy_ticket_hourly$"))
    application.add_handler(CallbackQueryHandler(cancel_buy_ticket_callback, pattern="^cancel_buy_ticket$"))
    application.add_handler(CallbackQueryHandler(wallet_info_callback, pattern="^wallet_info$"))
    application.add_handler(CallbackQueryHandler(view_tickets_callback, pattern="^view_tickets$"))
    application.add_handler(CallbackQueryHandler(stats_menu_callback, pattern="^stats_menu$"))
    application.add_handler(CallbackQueryHandler(settings_menu_callback, pattern="^settings_menu$"))
    application.add_handler(CallbackQueryHandler(wallet_settings_callback, pattern="^wallet_settings$"))
    application.add_handler(CallbackQueryHandler(view_private_key_callback, pattern="^view_private_key$"))
    application.add_handler(CallbackQueryHandler(delete_wallet_callback, pattern="^delete_wallet$"))
    application.add_handler(CallbackQueryHandler(confirm_delete_wallet_callback, pattern="^confirm_delete_wallet$"))
    application.add_handler(CallbackQueryHandler(live_status_callback, pattern="^live_status$"))
    application.add_handler(CallbackQueryHandler(copy_wallet_callback, pattern="^copy_wallet$"))
    application.add_handler(CallbackQueryHandler(copy_private_key_callback, pattern="^copy_private_key$"))
    application.add_handler(CallbackQueryHandler(change_style_callback, pattern="^change_style$"))
    application.add_handler(CallbackQueryHandler(set_style_monospace_callback, pattern="^set_style_monospace$"))
    application.add_handler(CallbackQueryHandler(set_style_fancy_callback, pattern="^set_style_fancy$"))
    application.add_handler(CallbackQueryHandler(set_style_bold_callback, pattern="^set_style_bold$"))
    application.add_handler(CallbackQueryHandler(set_style_italic_callback, pattern="^set_style_italic$"))
    application.add_handler(CallbackQueryHandler(set_language_callback, pattern="^lang_"))
    application.add_handler(CallbackQueryHandler(set_notification_callback, pattern="^notif_"))
    application.add_handler(CallbackQueryHandler(lambda update, context: update.callback_query.edit_message_text("An error occurred. Returning to Main Menu.")))
    
    application.run_polling()

if __name__ == "__main__":
    main()