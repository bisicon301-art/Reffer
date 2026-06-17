import os
import sys
import threading
import psycopg2
from psycopg2.extras import DictCursor
from flask import Flask, request, jsonify, render_template_string
import telebot
from telebot import types

# --- CONFIGURATION ---
BOT_TOKEN = "8962945474:AAE5XCJkWXeFTpFupOPtfG6oXyhYZ1iQ6zc"
ADMIN_ID = 7503462902
DB_URI = "postgresql://neondb_owner:npg_wVS1nkipHl2J@ep-green-hall-ahi1p53g.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# --- DATABASE SETUP ---
def get_db_connection():
    return psycopg2.connect(DB_URI)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Create tables
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username VARCHAR(255),
        balance NUMERIC DEFAULT 0,
        referred_by BIGINT,
        ip_address VARCHAR(100),
        is_ip_verified BOOLEAN DEFAULT FALSE,
        is_channels_verified BOOLEAN DEFAULT FALSE,
        referral_reward_given BOOLEAN DEFAULT FALSE,
        is_banned BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        channel_id VARCHAR(255) PRIMARY KEY
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        amount NUMERIC,
        wallet_address VARCHAR(255),
        status VARCHAR(50) DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key VARCHAR(100) PRIMARY KEY,
        value VARCHAR(255)
    );
    """)
    # Default parameters
    cur.execute("INSERT INTO settings (key, value) VALUES ('referral_bonus', '1.0') ON CONFLICT (key) DO NOTHING;")
    cur.execute("INSERT INTO settings (key, value) VALUES ('min_withdrawal', '10.0') ON CONFLICT (key) DO NOTHING;")
    conn.commit()
    cur.close()
    conn.close()

# --- HELPER FUNCTIONS ---
def get_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM users WHERE user_id = %s;", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def update_user(user_id, **kwargs):
    conn = get_db_connection()
    cur = conn.cursor()
    fields = ", ".join([f"{k} = %s" for k in kwargs.keys()])
    values = list(kwargs.values()) + [user_id]
    cur.execute(f"UPDATE users SET {fields} WHERE user_id = %s;", values)
    conn.commit()
    cur.close()
    conn.close()

def get_mandatory_channels():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT channel_id FROM channels;")
    channels = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return channels

def is_user_in_channel(user_id, channel_id):
    try:
        member = bot.get_chat_member(channel_id, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
    except Exception as e:
        print(f"Error checking channel {channel_id}: {e}")
    return False

def check_user_membership(user_id):
    channels = get_mandatory_channels()
    if not channels:
        return True
    for ch in channels:
        if not is_user_in_channel(user_id, ch):
            return False
    return True

def process_referral_reward(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM users WHERE user_id = %s;", (user_id,))
    user = cur.fetchone()
    
    if user and user['is_ip_verified'] and user['is_channels_verified'] and not user['referral_reward_given']:
        # Mark reward as issued
        cur.execute("UPDATE users SET referral_reward_given = TRUE WHERE user_id = %s;", (user_id,))
        
        if user['referred_by']:
            referrer_id = user['referred_by']
            cur.execute("SELECT value FROM settings WHERE key = 'referral_bonus';")
            bonus_row = cur.fetchone()
            bonus = float(bonus_row['value']) if bonus_row else 1.0
            
            cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s;", (bonus, referrer_id))
            conn.commit()
            
            try:
                ref_msg = (
                    f"🎉 *New Active Referral!*\n\n"
                    f"User @{user['username'] or 'User'} (ID: {user_id}) has completed all verifications.\n"
                    f"💰 You earned: *{bonus:.2f} USDT*"
                )
                bot.send_message(referrer_id, ref_msg, parse_mode='Markdown')
            except Exception as e:
                print(f"Failed to send notification to referrer: {e}")
        else:
            conn.commit()
    cur.close()
    conn.close()

def is_user_fully_verified(user_id, username):
    user = get_user(user_id)
    if not user:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s);", (user_id, username))
        conn.commit()
        cur.close()
        conn.close()
        user = get_user(user_id)
        
    if user['is_banned']:
        bot.send_message(user_id, "❌ You have been banned for violating our Terms (Multi-accounting / duplicate IP detected).")
        return False
        
    if not user['is_ip_verified']:
        # Point to Render WebApp URL
        render_url = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:5000")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Verify IP Address", web_app=types.WebAppInfo(url=f"{render_url}/verify-web")))
        
        bot.send_message(
            user_id, 
            "⚠️ *IP Verification Required*\n\n"
            "To prevent duplicate accounts, you must verify your IP address.\n"
            "Please click the button below to secure your entry:", 
            parse_mode='Markdown',
            reply_markup=markup
        )
        return False
        
    joined_all = check_user_membership(user_id)
    if not joined_all:
        if user['is_channels_verified']:
            update_user(user_id, is_channels_verified=False)
            
        channels = get_mandatory_channels()
        markup = types.InlineKeyboardMarkup()
        for idx, ch in enumerate(channels, 1):
            ch_url = f"https://t.me/{ch[1:]}" if ch.startswith('@') else f"https://t.me/{ch}"
            markup.add(types.InlineKeyboardButton(f"Join Channel #{idx}", url=ch_url))
        markup.add(types.InlineKeyboardButton("🔄 Check Membership", callback_data="check_membership"))
        
        bot.send_message(
            user_id, 
            "⚠️ *Mandatory Channels Required*\n\n"
            "You must remain subscribed to our mandatory channels to use the bot and earn rewards.", 
            parse_mode='Markdown',
            reply_markup=markup
        )
        return False
        
    if not user['is_channels_verified']:
        update_user(user_id, is_channels_verified=True)
        process_referral_reward(user_id)
        
    return True

def send_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("💼 Wallet", "👥 Referral")
    bot.send_message(
        user_id,
        "🎉 *Welcome to USDT Earn Bot!*\n\n"
        "Use the buttons below to manage your balance, retrieve your referral link, and track earnings.",
        parse_mode='Markdown',
        reply_markup=markup
    )

# --- FLASK WEB SERVER & WEBAPP ---
@app.route('/')
def home():
    return "Service is online."

@app.route('/verify-web')
def verify_web():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <title>IP Verification</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: #17212b;
                color: #f5f5f5;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                padding: 20px;
                box-sizing: border-box;
            }
            .card {
                background-color: #24303f;
                border-radius: 12px;
                padding: 30px;
                text-align: center;
                box-shadow: 0 4px 15px rgba(0,0,0,0.3);
                max-width: 400px;
                width: 100%;
            }
            h2 { margin-top: 0; color: #64b5f6; }
            p { font-size: 14px; line-height: 1.5; color: #b2bec3; }
            .btn {
                background: #2481cc;
                color: white;
                border: none;
                padding: 12px 24px;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
                cursor: pointer;
                margin-top: 20px;
                width: 100%;
                transition: background 0.2s;
            }
            .btn:active { background: #1a65a4; }
            .status { margin-top: 15px; font-weight: bold; font-size: 14px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>IP Check</h2>
            <p>Verification is needed to prevent multi-accounts and spam.</p>
            <button class="btn" id="verify-btn">Verify Now</button>
            <div class="status" id="status-text"></div>
        </div>
        <script>
            window.Telegram.WebApp.ready();
            window.Telegram.WebApp.expand();
            
            document.getElementById('verify-btn').onclick = function() {
                const statusText = document.getElementById('status-text');
                statusText.innerText = "⏳ Processing...";
                statusText.style.color = "#ffeaa7";
                
                const user = window.Telegram.WebApp.initDataUnsafe.user;
                if (!user) {
                    statusText.innerText = "❌ Please open this inside Telegram!";
                    statusText.style.color = "#ff7675";
                    return;
                }
                
                fetch('/api/verify-ip', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: user.id, username: user.username })
                })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        statusText.innerText = "✅ " + data.message;
                        statusText.style.color = "#55efc4";
                        setTimeout(() => {
                            window.Telegram.WebApp.close();
                        }, 2000);
                    } else {
                        statusText.innerText = "❌ " + data.message;
                        statusText.style.color = "#ff7675";
                    }
                })
                .catch(err => {
                    statusText.innerText = "❌ Request Error: " + err;
                    statusText.style.color = "#ff7675";
                });
            };
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/api/verify-ip', methods=['POST'])
def verify_ip_endpoint():
    data = request.json or {}
    user_id = data.get('user_id')
    username = data.get('username') or ''
    
    if not user_id:
        return jsonify({'success': False, 'message': 'User parameters missing.'}), 400
        
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ',' in ip:
        ip = ip.split(',')[0].strip()
        
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM users WHERE user_id = %s;", (user_id,))
    user = cur.fetchone()
    
    if not user:
        cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s);", (user_id, username))
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = %s;", (user_id,))
        user = cur.fetchone()
        
    if user['is_banned']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'You have been banned.'})
        
    if user['is_ip_verified']:
        cur.close()
        conn.close()
        return jsonify({'success': True, 'message': 'Already verified!'})
        
    # Check duplicate IP
    cur.execute("SELECT user_id FROM users WHERE ip_address = %s AND user_id != %s AND is_ip_verified = TRUE;", (ip, user_id))
    duplicate = cur.fetchone()
    
    if duplicate:
        cur.execute("UPDATE users SET is_banned = TRUE WHERE user_id = %s;", (user_id,))
        conn.commit()
        cur.close()
        conn.close()
        try:
            bot.send_message(user_id, "❌ *Suspicious Activity*\n\nYour IP matches an existing database entry. You have been banned for multi-accounting.", parse_mode='Markdown')
        except Exception:
            pass
        return jsonify({'success': False, 'message': 'IP duplicate. You are now banned.'})
        
    # Mark verified
    cur.execute("UPDATE users SET ip_address = %s, is_ip_verified = TRUE WHERE user_id = %s;", (ip, user_id))
    conn.commit()
    cur.close()
    conn.close()
    
    try:
        msg = "✅ *IP Verified!*\n\n"
        if check_user_membership(user_id):
            update_user(user_id, is_channels_verified=True)
            process_referral_reward(user_id)
            msg += "You joined all mandatory channels! Launching bot..."
        else:
            msg += "Now join the mandatory channels and select check membership."
        bot.send_message(user_id, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Failed to alert verified user: {e}")
        
    return jsonify({'success': True, 'message': 'Successfully Verified.'})

# --- TELEGRAM BOT LOGIC ---
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    
    user = get_user(user_id)
    if not user:
        referred_by = None
        args = message.text.split()
        if len(args) > 1:
            try:
                ref_id = int(args[1])
                if ref_id != user_id:
                    referred_by = ref_id
            except ValueError:
                pass
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (user_id, username, referred_by) VALUES (%s, %s, %s);", (user_id, username, referred_by))
        conn.commit()
        cur.close()
        conn.close()
        
    if is_user_fully_verified(user_id, username):
        send_main_menu(user_id)

@bot.message_handler(func=lambda msg: msg.text == "💼 Wallet")
def wallet_menu(message):
    user_id = message.from_user.id
    if not is_user_fully_verified(user_id, message.from_user.username):
        return
        
    user = get_user(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'min_withdrawal';")
    min_w = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💸 Withdraw USDT", callback_data="request_withdraw"))
    
    msg_text = (
        f"💼 *Your Wallet*\n\n"
        f"💵 Balance: *{user['balance']:.2f} USDT*\n"
        f"📌 Minimum: *{min_w:.2f} USDT*\n"
        f"⚡ Network: *USDT BEP20*\n\n"
        f"All payments are structured within 48 hours."
    )
    bot.send_message(user_id, msg_text, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "request_withdraw")
def start_withdrawal(call):
    user_id = call.from_user.id
    bot.delete_message(call.message.chat.id, call.message.message_id)
    
    if not is_user_fully_verified(user_id, call.from_user.username):
        return
        
    user = get_user(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'min_withdrawal';")
    min_w = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    
    if user['balance'] < min_w:
        bot.send_message(user_id, f"❌ Insufficient balance. Minimum withdrawal is *{min_w:.2f} USDT*.", parse_mode='Markdown')
        return
        
    msg = bot.send_message(user_id, "👉 Send your *USDT BEP20* wallet address:", parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_wallet_address)

def process_wallet_address(message):
    user_id = message.from_user.id
    address = message.text.strip()
    
    if not (address.startswith("0x") and len(address) == 42):
        bot.send_message(user_id, "❌ Invalid USDT BEP20 format. Must start with '0x' and be 42 characters. Canceled.")
        return
        
    msg = bot.send_message(user_id, "👉 Send the withdrawal amount in USDT:", parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_withdrawal_amount, address)

def process_withdrawal_amount(message, address):
    user_id = message.from_user.id
    amount_str = message.text.strip()
    
    try:
        amount = float(amount_str)
    except ValueError:
        bot.send_message(user_id, "❌ Invalid amount. Canceled.")
        return
        
    user = get_user(user_id)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'min_withdrawal';")
    min_w = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    
    if amount < min_w:
        bot.send_message(user_id, f"❌ Minimum withdrawal is *{min_w:.2f} USDT*.", parse_mode='Markdown')
        return
        
    if amount > user['balance']:
        bot.send_message(user_id, "❌ Balance is lower than this request.", parse_mode='Markdown')
        return
        
    # Deduct and log pending record
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance - %s WHERE user_id = %s;", (amount, user_id))
    cur.execute("INSERT INTO withdrawals (user_id, amount, wallet_address) VALUES (%s, %s, %s);", (user_id, amount, address))
    conn.commit()
    cur.close()
    conn.close()
    
    bot.send_message(
        user_id, 
        f"✅ *Request Submitted!*\n\n"
        f"💵 Amount: *{amount:.2f} USDT*\n"
        f"📌 Address: `{address}`\n\n"
        f"Processing will complete within 48 hours.",
        parse_mode='Markdown'
    )
    
    try:
        admin_msg = (
            f"🚨 *New Withdrawal Request*\n\n"
            f"👤 User: @{user['username'] or 'User'} (ID: {user_id})\n"
            f"💵 Amount: `{amount:.2f}`\n"
            f"📌 Address: `{address}`\n\n"
            f"Check pending list in /adminpanel"
        )
        bot.send_message(ADMIN_ID, admin_msg, parse_mode='Markdown')
    except Exception:
        pass

@bot.message_handler(func=lambda msg: msg.text == "👥 Referral")
def referral_menu(message):
    user_id = message.from_user.id
    if not is_user_fully_verified(user_id, message.from_user.username):
        return
        
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE referred_by = %s AND is_ip_verified = TRUE AND is_channels_verified = TRUE;", (user_id,))
    active_count = cur.fetchone()[0]
    cur.execute("SELECT value FROM settings WHERE key = 'referral_bonus';")
    bonus = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    msg_text = (
        f"👥 *Referral Program*\n\n"
        f"🔗 Referral link:\n{ref_link}\n\n"
        f"💰 Bonus/Referral: *{bonus:.2f} USDT*\n"
        f"✅ Active Referrals: *{active_count}*\n\n"
        f"⚠️ *Note*: Referrals must finish IP verification and join mandatory channels for the reward to credit."
    )
    bot.send_message(user_id, msg_text, parse_mode='Markdown')

# --- ADMIN FUNCTIONS ---
@bot.message_handler(commands=['adminpanel'])
def admin_panel_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    send_admin_menu()

def send_admin_menu():
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("📢 Channels", callback_data="adm_channels"),
        types.InlineKeyboardButton("💰 Ref Bonus", callback_data="adm_ref_bonus")
    )
    markup.row(
        types.InlineKeyboardButton("💸 Min Withdraw", callback_data="adm_min_withdraw"),
        types.InlineKeyboardButton("✉️ Broadcast", callback_data="adm_broadcast")
    )
    markup.row(
        types.InlineKeyboardButton("⏳ Pending", callback_data="adm_pending"),
        types.InlineKeyboardButton("📊 Performance", callback_data="adm_performance")
    )
    bot.send_message(ADMIN_ID, "⚙️ *Admin Management System*", parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_") or call.data == "adm_back")
def admin_callbacks(call):
    if call.from_user.id != ADMIN_ID:
        return
    
    bot.delete_message(call.message.chat.id, call.message.message_id)
    action = call.data
    
    if action == "adm_back":
        send_admin_menu()
        
    elif action == "adm_channels":
        channels = get_mandatory_channels()
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("➕ Add Channel", callback_data="add_ch"),
            types.InlineKeyboardButton("➖ Remove Channel", callback_data="remove_ch")
        )
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back"))
        ch_list = "\n".join([f"- {ch}" for ch in channels]) if channels else "None"
        bot.send_message(ADMIN_ID, f"📢 *Mandatory Channels List:*\n\n{ch_list}", parse_mode='Markdown', reply_markup=markup)
        
    elif action == "adm_ref_bonus":
        msg = bot.send_message(ADMIN_ID, "👉 Send the new referral reward value:")
        bot.register_next_step_handler(msg, update_ref_bonus)
        
    elif action == "adm_min_withdraw":
        msg = bot.send_message(ADMIN_ID, "👉 Send the new minimum withdrawal value:")
        bot.register_next_step_handler(msg, update_min_withdraw)
        
    elif action == "adm_broadcast":
        msg = bot.send_message(ADMIN_ID, "👉 Send the message to broadcast:")
        bot.register_next_step_handler(msg, start_broadcast)
        
    elif action == "adm_pending":
        show_pending_withdrawals()
        
    elif action == "adm_performance":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users;")
        tot_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE is_ip_verified = TRUE AND is_channels_verified = TRUE;")
        tot_active = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE status = 'approved';")
        tot_paid = cur.fetchone()[0]
        cur.close()
        conn.close()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back"))
        
        perf_text = (
            f"📊 *Bot Statistics*\n\n"
            f"👥 Registered: *{tot_users}*\n"
            f"✅ Verified Active: *{tot_active}*\n"
            f"💰 Settled Volume: *{tot_paid:.2f} USDT*"
        )
        bot.send_message(ADMIN_ID, perf_text, parse_mode='Markdown', reply_markup=markup)

# Sub handlers for settings updates
def update_ref_bonus(message):
    try:
        val = float(message.text.strip())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE settings SET value = %s WHERE key = 'referral_bonus';", (str(val),))
        conn.commit()
        cur.close()
        conn.close()
        bot.send_message(ADMIN_ID, f"✅ Referral bonus set to *{val:.2f} USDT*.", parse_mode='Markdown')
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Failed to parse. Process cancelled.")
    send_admin_menu()

def update_min_withdraw(message):
    try:
        val = float(message.text.strip())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE settings SET value = %s WHERE key = 'min_withdrawal';", (str(val),))
        conn.commit()
        cur.close()
        conn.close()
        bot.send_message(ADMIN_ID, f"✅ Minimum withdrawal configured to *{val:.2f} USDT*.", parse_mode='Markdown')
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Failed to parse. Process cancelled.")
    send_admin_menu()

def start_broadcast(message):
    text = message.text
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users;")
    all_users = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    success = 0
    bot.send_message(ADMIN_ID, f"⏳ Broadcasting to {len(all_users)} users...")
    for uid in all_users:
        try:
            bot.send_message(uid, text)
            success += 1
        except Exception:
            pass
    bot.send_message(ADMIN_ID, f"✅ Broadcast finished. Delivered to {success}/{len(all_users)}.", parse_mode='Markdown')
    send_admin_menu()

@bot.callback_query_handler(func=lambda call: call.data in ["add_ch", "remove_ch"])
def modify_channels(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if call.data == "add_ch":
        msg = bot.send_message(ADMIN_ID, "👉 Send the channel link (formatted with @, e.g., `@mychannel`):")
        bot.register_next_step_handler(msg, register_channel_step)
    elif call.data == "remove_ch":
        channels = get_mandatory_channels()
        if not channels:
            bot.send_message(ADMIN_ID, "There are no channels registered.")
            send_admin_menu()
            return
        markup = types.InlineKeyboardMarkup()
        for ch in channels:
            markup.add(types.InlineKeyboardButton(f"Remove {ch}", callback_data=f"delch_{ch}"))
        markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back"))
        bot.send_message(ADMIN_ID, "Select channel to delete:", reply_markup=markup)

def register_channel_step(message):
    ch = message.text.strip()
    if not ch.startswith("@"):
        bot.send_message(ADMIN_ID, "❌ Format invalid. Must start with @.")
        send_admin_menu()
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO channels (channel_id) VALUES (%s) ON CONFLICT DO NOTHING;", (ch,))
    conn.commit()
    cur.close()
    conn.close()
    bot.send_message(ADMIN_ID, f"✅ Channel {ch} registered.")
    send_admin_menu()

@bot.callback_query_handler(func=lambda call: call.data.startswith("delch_"))
def remove_channel_step(call):
    ch = call.data.replace("delch_", "")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE channel_id = %s;", (ch,))
    conn.commit()
    cur.close()
    conn.close()
    bot.answer_callback_query(call.id, f"Removed {ch}")
    send_admin_menu()

def show_pending_withdrawals():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT w.*, u.username FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending' ORDER BY w.created_at ASC LIMIT 1;")
    req = cur.fetchone()
    cur.close()
    conn.close()
    
    if not req:
        bot.send_message(ADMIN_ID, "🎉 No pending withdrawals.")
        send_admin_menu()
        return
        
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Approve", callback_data=f"w_app_{req['id']}"),
        types.InlineKeyboardButton("❌ Reject", callback_data=f"w_rej_{req['id']}")
    )
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="adm_back"))
    
    msg_txt = (
        f"⏳ *Pending Withdrawal*\n\n"
        f"👤 User: @{req['username'] or 'User'} (ID: {req['user_id']})\n"
        f"💵 Amount:\n`{req['amount']:.2f}`\n"
        f"📌 BEP20 Address:\n`{req['wallet_address']}`\n\n"
        f"Tap the amount/address to copy directly."
    )
    bot.send_message(ADMIN_ID, msg_txt, parse_mode='Markdown', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("w_app_") or call.data.startswith("w_rej_"))
def process_payout(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.delete_message(call.message.chat.id, call.message.message_id)
    
    action = "approve" if "w_app_" in call.data else "reject"
    w_id = int(call.data.replace("w_app_", "").replace("w_rej_", ""))
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM withdrawals WHERE id = %s;", (w_id,))
    req = cur.fetchone()
    
    if not req or req['status'] != 'pending':
        cur.close()
        conn.close()
        bot.answer_callback_query(call.id, "Already processed.")
        show_pending_withdrawals()
        return
        
    if action == "approve":
        cur.execute("UPDATE withdrawals SET status = 'approved' WHERE id = %s;", (w_id,))
        conn.commit()
        try:
            bot.send_message(
                req['user_id'],
                f"✅ *Withdrawal Completed!*\n\n"
                f"💵 Amount: *{req['amount']:.2f} USDT*\n"
                f"⚡ Status: *Paid (BEP20 Network)*\n\n"
                f"The transaction has been fully processed and sent to your address.",
                parse_mode='Markdown'
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Approved.")
    else:
        # Refund and Mark Reject
        cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s;", (req['amount'], req['user_id']))
        cur.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = %s;", (w_id,))
        conn.commit()
        try:
            bot.send_message(
                req['user_id'],
                f"❌ *Withdrawal Rejected*\n\n"
                f"💵 Amount: *{req['amount']:.2f} USDT*\n"
                f"⚠️ Status: *Rejected & Refunded*\n\n"
                f"The funds have been returned to your wallet balance.",
                parse_mode='Markdown'
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Rejected and refunded.")
        
    cur.close()
    conn.close()
    show_pending_withdrawals()

@bot.callback_query_handler(func=lambda call: call.data == "check_membership")
def user_check_membership_button(call):
    uid = call.from_user.id
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if is_user_fully_verified(uid, call.from_user.username):
        send_main_menu(uid)
    else:
        bot.answer_callback_query(call.id, "❌ Verification not complete yet!")

# --- FALLBACK TEXT INTERACTION ---
@bot.message_handler(func=lambda msg: True)
def default_message_router(message):
    uid = message.from_user.id
    username = message.from_user.username or ""
    if uid == ADMIN_ID:
        return
    if is_user_fully_verified(uid, username):
        send_main_menu(uid)

# --- THREADING AND LAUNCH ---
def run_bot_polling():
    print("Starting bot listener...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Polling crash detected: {e}")
            import time
            time.sleep(5)

if __name__ == '__main__':
    init_db()
    # Execute Telegram Client on a secondary background thread
    threading.Thread(target=run_bot_polling, daemon=True).start()
    # Execute Flask Web Server on Render binding port
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
