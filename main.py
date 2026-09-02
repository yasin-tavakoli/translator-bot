import os, time, logging, sqlite3, speech_recognition as sr, json
from datetime import datetime, timedelta
from collections import defaultdict
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from deep_translator import GoogleTranslator, LingueeTranslator
from gtts import gTTS
import PyPDF2
from docx import Document
from PIL import Image
import pytesseract
from pydub import AudioSegment
import requests

# ==========================================================
# ⚙️ تنظیمات حرفه‌ای
# ==========================================================
class Config:
    BOT_TOKEN = "8791676273:AAEIw5JaJmZk9f7YqOdO1Xq1Fm0KBkvteTQ"
    ADMIN_IDS = [5138190544]
    MAX_FILE_SIZE = 20971520  # 20MB
    RATE_LIMIT = 50  # افزایش محدودیت
    MAX_TEXT_LENGTH = 5000
    ALLOWED_FILE_TYPES = ['pdf', 'docx', 'txt']
    RENDER_URL = "https://translator-bot-z4wh.onrender.com"
    SUPPORTED_LANGUAGES = {
        'fa': 'فارسی', 'en': 'English', 'ar': 'العربية', 
        'fr': 'Français', 'de': 'Deutsch', 'es': 'Español',
        'tr': 'Türkçe', 'ru': 'Русский', 'it': 'Italiano',
        'pt': 'Português', 'nl': 'Nederlands', 'zh': '中文'
    }

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==========================================================
# ️ دیتابیس پیشرفته با امنیت بالا
# ==========================================================
class SecureDatabase:
    def __init__(self):
        self.conn = sqlite3.connect('bot_data.db', check_same_thread=False)
        self.init_tables()
        self.master_key = Fernet.generate_key()  # کلید اصلی برای رمزنگاری
    
    def init_tables(self):
        c = self.conn.cursor()
        
        # جدول کاربران با اطلاعات کامل
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            secret_key TEXT,
            device_info TEXT,
            consent_given INTEGER DEFAULT 0,
            chat_consent INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_active DATETIME
        )''')
        
        # جدول ترجمه‌ها با رمزنگاری پیشرفته
        c.execute('''CREATE TABLE IF NOT EXISTS translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            encrypted_source TEXT,
            encrypted_target TEXT,
            source_lang TEXT,
            target_lang TEXT,
            translation_type TEXT DEFAULT 'text',
            is_favorite INTEGER DEFAULT 0,
            word_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )''')
        
        # جدول آمار پیشرفته
        c.execute('''CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            total_translations INTEGER DEFAULT 0,
            total_words INTEGER DEFAULT 0,
            total_chars INTEGER DEFAULT 0,
            favorite_count INTEGER DEFAULT 0,
            last_activity DATETIME,
            theme TEXT DEFAULT 'light',
            default_source_lang TEXT DEFAULT 'auto',
            default_target_lang TEXT DEFAULT 'fa',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )''')
        
        # جدول علاقه‌مندی‌ها
        c.execute('''CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            translation_id INTEGER,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (translation_id) REFERENCES translations(id)
        )''')
        
        # جدول لاگ‌های ادمین
        c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # جدول چت ادمین
        c.execute('''CREATE TABLE IF NOT EXISTS admin_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            sender TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # جدول بازی و یادگیری
        c.execute('''CREATE TABLE IF NOT EXISTS game_progress (
            user_id INTEGER,
            word_id INTEGER,
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            last_played DATETIME,
            PRIMARY KEY (user_id, word_id)
        )''')
        
        self.conn.commit()
        logger.info("✅ دیتابیس با موفقیت ایجاد شد")
    
    def generate_secret_key(self):
        return Fernet.generate_key().decode()
    
    def get_or_create_user(self, user_id, username=None, first_name=None):
        c = self.conn.cursor()
        c.execute('SELECT secret_key FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        if r:
            # آپدیت آخرین فعالیت
            c.execute('UPDATE users SET last_active = ? WHERE user_id = ?', (datetime.now(), user_id))
            self.conn.commit()
            return r[0]
        
        # ساخت کاربر جدید
        new_key = self.generate_secret_key()
        c.execute('''INSERT INTO users (user_id, username, first_name, secret_key, last_active) 
                     VALUES (?, ?, ?, ?, ?)''', 
                  (user_id, username, first_name, new_key, datetime.now()))
        c.execute('INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)', (user_id,))
        self.conn.commit()
        return new_key
    
    def encrypt(self, text, key):
        if not text: return ""
        try:
            return Fernet(key.encode()).encrypt(text.encode()).decode()
        except:
            return ""
    
    def decrypt(self, encrypted_text, key):
        if not encrypted_text: return ""
        try:
            return Fernet(key.encode()).decrypt(encrypted_text.encode()).decode()
        except:
            return "[خطا در رمزگشایی]"
    
    def save_translation(self, user_id, source_text, translated_text, source_lang, target_lang, trans_type='text'):
        key = self.get_or_create_user(user_id)
        c = self.conn.cursor()
        
        encrypted_source = self.encrypt(source_text, key)
        encrypted_target = self.encrypt(translated_text, key)
        word_count = len(source_text.split())
        char_count = len(source_text)
        
        c.execute('''INSERT INTO translations 
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, translation_type, word_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, trans_type, word_count))
        
        trans_id = c.lastrowid
        
        # آپدیت آمار
        c.execute('SELECT total_translations, total_words, total_chars FROM user_stats WHERE user_id = ?', (user_id,))
        stats = c.fetchone()
        if stats:
            c.execute('''UPDATE user_stats SET 
                total_translations = ?, total_words = ?, total_chars = ?, last_activity = ?
                WHERE user_id = ?''',
                (stats[0] + 1, stats[1] + word_count, stats[2] + char_count, datetime.now(), user_id))
        
        self.conn.commit()
        return trans_id
    
    def add_to_favorites(self, user_id, translation_id):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO favorites (user_id, translation_id) VALUES (?, ?)', (user_id, translation_id))
        c.execute('UPDATE user_stats SET favorite_count = favorite_count + 1 WHERE user_id = ?', (user_id,))
        c.execute('UPDATE translations SET is_favorite = 1 WHERE id = ?', (translation_id,))
        self.conn.commit()
    
    def get_favorites(self, user_id, key, limit=10):
        c = self.conn.cursor()
        c.execute('''SELECT t.encrypted_source, t.encrypted_target, t.source_lang, t.target_lang, t.timestamp
                     FROM translations t
                     JOIN favorites f ON t.id = f.translation_id
                     WHERE f.user_id = ? ORDER BY f.added_at DESC LIMIT ?''', (user_id, limit))
        results = c.fetchall()
        return [(self.decrypt(r[0], key), self.decrypt(r[1], key), r[2], r[3], r[4]) for r in results]
    
    def get_user_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT total_translations, total_words, total_chars, favorite_count, last_activity FROM user_stats WHERE user_id = ?', (user_id,))
        return c.fetchone()
    
    def get_top_languages(self, user_id, limit=5):
        c = self.conn.cursor()
        c.execute('''SELECT target_lang, COUNT(*) as count FROM translations 
                     WHERE user_id = ? GROUP BY target_lang ORDER BY count DESC LIMIT ?''', (user_id, limit))
        return c.fetchall()
    
    def get_user_history(self, user_id, key, limit=10):
        c = self.conn.cursor()
        c.execute('''SELECT encrypted_source, encrypted_target, source_lang, target_lang, timestamp
                     FROM translations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?''', (user_id, limit))
        return [(self.decrypt(r[0], key), self.decrypt(r[1], key), r[2], r[3], r[4]) for r in c.fetchall()]
    
    def get_admin_dashboard(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM users'); total_users = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM users WHERE last_active > ?', (datetime.now() - timedelta(days=7),)); active_users = c.fetchone()[0]
        c.execute('SELECT SUM(total_translations) FROM user_stats'); total_trans = c.fetchone()[0] or 0
        c.execute('SELECT SUM(total_words) FROM user_stats'); total_words = c.fetchone()[0] or 0
        c.execute('SELECT COUNT(*) FROM translations'); total_translations_today = c.fetchone()[0]
        
        return {
            'total_users': total_users,
            'active_users': active_users,
            'total_translations': total_trans,
            'total_words': total_words,
            'translations_today': total_translations_today
        }
    
    def log_admin_action(self, admin_id, action, details=""):
        c = self.conn.cursor()
        c.execute('INSERT INTO admin_logs (admin_id, action, details) VALUES (?, ?, ?)', (admin_id, action, details))
        self.conn.commit()
    
    def get_all_user_ids(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row[0] for row in c.fetchall()]
    
    # بقیه متدها مشابه قبل...
    def has_consent(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT consent_given FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        return r and r[0] == 1
    
    def save_device_info(self, user_id, device_info):
        c = self.conn.cursor()
        c.execute('UPDATE users SET device_info = ?, consent_given = 1 WHERE user_id = ?', (device_info, user_id))
        self.conn.commit()
    
    def set_chat_consent(self, user_id, consent):
        c = self.conn.cursor()
        c.execute('UPDATE users SET chat_consent = ? WHERE user_id = ?', (1 if consent else 0, user_id))
        self.conn.commit()
    
    def has_chat_consent(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT chat_consent FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        return r and r[0] == 1
    
    def save_chat_message(self, user_id, message, sender):
        c = self.conn.cursor()
        c.execute('INSERT INTO admin_chat (user_id, message, sender) VALUES (?, ?, ?)', (user_id, message, sender))
        self.conn.commit()
    
    def get_chat_history(self, user_id, limit=20):
        c = self.conn.cursor()
        c.execute('SELECT message, sender, timestamp FROM admin_chat WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?', (user_id, limit))
        return c.fetchall()[::-1]
    
    def get_users_with_chat_consent(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id, username, first_name FROM users WHERE chat_consent = 1')
        return c.fetchall()
    
    def reset_user(self, user_id):
        c = self.conn.cursor()
        for table in ['favorites', 'translations', 'user_stats', 'game_progress', 'admin_chat']:
            c.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
        c.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        self.conn.commit()

db = SecureDatabase()
logger.info("✅ ربات با موفقیت راه‌اندازی شد")

# ==========================================================
# 🤖 هندلرهای پیشرفته
# ==========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    secret_key = db.get_or_create_user(user_id, user.username, user.first_name)
    
    if 'secret_shown' not in context.user_data:
        await update.message.reply_text(
            f"🔐 **امانت امنیتی شما**\n\n"
            f"کلید منحصر به فرد شما:\n\n`{secret_key}`\n\n"
            f"⚠️ این کلید را در جای امنی ذخیره کنید. بدون آن به ترجمه‌هایتان دسترسی نخواهید داشت.",
            parse_mode='Markdown'
        )
        context.user_data['secret_shown'] = True
    
    keyboard = [
        [InlineKeyboardButton("🌐 ترجمه متن", callback_data='translate_text'),
         InlineKeyboardButton("🖼️ ترجمه عکس", callback_data='translate_photo')],
        [InlineKeyboardButton("🎤 ترجمه صوتی", callback_data='translate_voice'),
         InlineKeyboardButton("📄 ترجمه فایل", callback_data='translate_file')],
        [InlineKeyboardButton("📖 دیکشنری", callback_data='dictionary'),
         InlineKeyboardButton("⭐ علاقه‌مندی‌ها", callback_data='favorites')],
        [InlineKeyboardButton("📊 آمار من", callback_data='my_stats'),
         InlineKeyboardButton("🎮 بازی", callback_data='start_game')],
        [InlineKeyboardButton("💬 چت با ادمین", callback_data='admin_chat')],
        [InlineKeyboardButton("🔑 کلید امنیتی", callback_data='show_key')]
    ]
    
    # دکمه‌های ادمین
    if user_id in Config.ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🛡️ پنل ادمین", callback_data='admin_panel')])
    
    await update.message.reply_text(
        f"👋 سلام {user.first_name}!\nبه ربات مترجم حرفه‌ای خوش آمدید.\n\nیک گزینه انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==========================================================
# 🛡️ پنل مدیریت پیشرفته
# ==========================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    stats = db.get_admin_dashboard()
    
    keyboard = [
        [InlineKeyboardButton(" آمار کامل", callback_data='admin_full_stats')],
        [InlineKeyboardButton("👥 لیست کاربران", callback_data='admin_user_list')],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data='admin_broadcast')],
        [InlineKeyboardButton("📝 لاگ‌ها", callback_data='admin_logs')],
        [InlineKeyboardButton("💬 چت با کاربران", callback_data='admin_chat_list')],
        [InlineKeyboardButton("🔄 به‌روزرسانی ربات", callback_data='admin_update')]
    ]
    
    msg = (
        f"🛡️ **پنل مدیریت پیشرفته**\n\n"
        f"📊 **آمار لحظه‌ای:**\n"
        f"👥 کل کاربران: {stats['total_users']}\n"
        f"✅ کاربران فعال (۷ روز): {stats['active_users']}\n"
        f" کل ترجمه‌ها: {stats['total_translations']:,}\n"
        f"📚 کل کلمات ترجمه شده: {stats['total_words']:,}\n"
        f"📅 ترجمه‌های امروز: {stats['translations_today']}\n"
    )
    
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    db.log_admin_action(user_id, "access_admin_panel")

async def admin_full_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    c = db.conn.cursor()
    c.execute('''SELECT DATE(timestamp), COUNT(*) FROM translations 
                 WHERE timestamp > datetime('now', '-7 days') 
                 GROUP BY DATE(timestamp) ORDER BY DATE(timestamp)''')
    weekly_stats = c.fetchall()
    
    msg = "📊 **آمار ۷ روز اخیر:**\n\n"
    for date, count in weekly_stats:
        msg += f"📅 {date}: {count} ترجمه\n"
    
    await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
    db.log_admin_action(user_id, "view_full_stats")

async def admin_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    c = db.conn.cursor()
    c.execute('''SELECT user_id, username, first_name, created_at, last_active 
                 FROM users ORDER BY created_at DESC LIMIT 20''')
    users = c.fetchall()
    
    msg = "👥 **۰ کاربر اخیر:**\n\n"
    for u in users:
        uid, username, fname, created, last = u
        msg += f"👤 {fname or 'Unknown'} (@{username or 'N/A'})\n"
        msg += f"   ID: `{uid}`\n"
        msg += f"   عضویت: {created[:10]}\n"
        msg += f"   آخرین فعالیت: {last[:16] if last else 'N/A'}\n\n"
    
    await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
    db.log_admin_action(user_id, "view_user_list")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    await update.callback_query.edit_message_text(
        " **پیام همگانی**\n\n"
        "متن پیام خود را ارسال کنید (یا /cancel برای انصراف):"
    )
    context.user_data['admin_action'] = 'broadcast_wait_message'

async def admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    c = db.conn.cursor()
    c.execute('''SELECT admin_id, action, details, timestamp FROM admin_logs 
                 ORDER BY timestamp DESC LIMIT 10''')
    logs = c.fetchall()
    
    msg = "📝 **۰ لاگ اخیر ادمین:**\n\n"
    for log in logs:
        aid, action, details, ts = log
        msg += f" {ts[:16]}\n"
        msg += f"👤 ادمین {aid}\n"
        msg += f" {action}\n"
        if details:
            msg += f" {details}\n"
        msg += "\n"
    
    await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
    db.log_admin_action(user_id, "view_logs")

# ==========================================================
# ▶️ اجرای اصلی
# ==========================================================

def main():
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    # دستورات اصلی
    app.add_handler(CommandHandler("start", start))
    
    # دستورات ادمین
    app.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    app.add_handler(CallbackQueryHandler(admin_full_stats, pattern='^admin_full_stats$'))
    app.add_handler(CallbackQueryHandler(admin_user_list, pattern='^admin_user_list$'))
    app.add_handler(CallbackQueryHandler(admin_broadcast, pattern='^admin_broadcast$'))
    app.add_handler(CallbackQueryHandler(admin_logs, pattern='^admin_logs$'))
    
    # هندلرهای دیگر
    app.add_handler(CallbackQueryHandler(lambda u, c: c.callback_query.answer("در حال توسعه..."), pattern='^(translate_|dictionary|favorites|my_stats|start_game|admin_chat|show_key)'))
    
    webhook_url = f"{Config.RENDER_URL}/{Config.BOT_TOKEN}"
    
    logger.info("✅ ربات با Webhook فعال شد!")
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 8080)),
        url_path=Config.BOT_TOKEN,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == '__main__':
    main()
