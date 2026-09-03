import os, time, logging, speech_recognition as sr
from datetime import datetime, timedelta
from collections import defaultdict
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from deep_translator import GoogleTranslator
from gtts import gTTS
import PyPDF2
from docx import Document
from PIL import Image
import pytesseract
from pydub import AudioSegment
import psycopg2
from psycopg2.extras import RealDictCursor

# ==========================================================
# ⚙️ تنظیمات
# ==========================================================
class Config:
    BOT_TOKEN = "8791676273:AAEIw5JaJmZk9f7YqOdO1Xq1Fm0KBkvteTQ"
    ADMIN_IDS = [5138190544]
    MAX_FILE_SIZE = 20971520
    RATE_LIMIT = 50
    MAX_TEXT_LENGTH = 5000
    ALLOWED_FILE_TYPES = ['pdf', 'docx', 'txt']
    RENDER_URL = "https://translator-bot-z4wh.onrender.com"
    # لینک اتصال Supabase
    DATABASE_URL = "postgresql://postgres:YasinBot2026%21Secure@db.qwhkjpljsaledhqbgkbp.supabase.co:5432/postgres"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================================
# ️ دیتابیس Supabase (پایدار و ابری)
# ==========================================================
class SecureDatabase:
    def __init__(self):
        self.conn = psycopg2.connect(Config.DATABASE_URL)
        logger.info("✅ اتصال به دیتابیس ابری Supabase برقرار شد")
    
    def generate_secret_key(self): 
        return Fernet.generate_key().decode()
    
    def get_or_create_user(self, user_id, username=None, first_name=None):
        c = self.conn.cursor(cursor_factory=RealDictCursor)
        c.execute('SELECT secret_key FROM users WHERE user_id = %s', (user_id,))
        r = c.fetchone()
        if r:
            c.execute('UPDATE users SET last_active = NOW() WHERE user_id = %s', (user_id,))
            self.conn.commit()
            return r['secret_key']
        
        new_key = self.generate_secret_key()
        c.execute('''INSERT INTO users (user_id, username, first_name, secret_key, last_active) 
                     VALUES (%s, %s, %s, %s, NOW())''', 
                  (user_id, username, first_name, new_key))
        c.execute('INSERT INTO user_stats (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING', (user_id,))
        self.conn.commit()
        return new_key
    
    def encrypt(self, text, key): 
        return Fernet(key.encode()).encrypt(text.encode()).decode() if text else ""
    
    def decrypt(self, encrypted_text, key):
        try: 
            return Fernet(key.encode()).decrypt(encrypted_text.encode()).decode() if encrypted_text else ""
        except: 
            return "[خطا]"
    
    def save_translation(self, user_id, source_text, translated_text, source_lang, target_lang, trans_type='text'):
        key = self.get_or_create_user(user_id)
        c = self.conn.cursor()
        
        encrypted_source = self.encrypt(source_text, key)
        encrypted_target = self.encrypt(translated_text, key)
        word_count = len(source_text.split())
        
        c.execute('''INSERT INTO translations 
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, translation_type, word_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, trans_type, word_count))
        
        trans_id = c.fetchone()[0]
        
        c.execute('''UPDATE user_stats SET 
            total_translations = total_translations + 1, 
            total_words = total_words + %s, 
            last_activity = NOW()
            WHERE user_id = %s''', (word_count, user_id))
        
        self.conn.commit()
        return trans_id

    def get_user_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT total_translations, total_words, last_activity FROM user_stats WHERE user_id = %s', (user_id,))
        return c.fetchone()
    
    def get_all_user_ids(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row[0] for row in c.fetchall()]
    
    def get_admin_dashboard(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM users'); total = c.fetchone()[0]
        c.execute('SELECT SUM(total_translations) FROM user_stats'); trans = c.fetchone()[0] or 0
        return {'total_users': total, 'total_translations': trans}

    def has_consent(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT consent_given FROM users WHERE user_id = %s', (user_id,))
        r = c.fetchone()
        return r and r[0]

    def save_device_info(self, user_id, info):
        c = self.conn.cursor()
        c.execute('UPDATE users SET device_info = %s, consent_given = TRUE WHERE user_id = %s', (info, user_id))
        self.conn.commit()
    
    def save_chat_message(self, user_id, message, sender):
        c = self.conn.cursor()
        c.execute('INSERT INTO admin_chat (user_id, message, sender) VALUES (%s, %s, %s)', (user_id, message, sender))
        self.conn.commit()
    
    def get_chat_history(self, user_id, limit=20):
        c = self.conn.cursor()
        c.execute('SELECT message, sender, timestamp FROM admin_chat WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s', (user_id, limit))
        return c.fetchall()[::-1]

db = SecureDatabase()

# ==========================================================
# 🤖 هندلرها
# ==========================================================
def get_main_keyboard(user_id):
    kb = [
        [InlineKeyboardButton("🌐 ترجمه متن", callback_data='translate_text'), InlineKeyboardButton("🖼️ ترجمه عکس", callback_data='translate_photo')],
        [InlineKeyboardButton("🎤 ترجمه صوتی", callback_data='translate_voice'), InlineKeyboardButton("📄 ترجمه فایل", callback_data='translate_file')],
        [InlineKeyboardButton("🤖 دستیار هوشمند", callback_data='smart_assistant'), InlineKeyboardButton("📊 آمار من", callback_data='my_stats')],
        [InlineKeyboardButton("🔑 کلید امنیتی", callback_data='show_key')]
    ]
    if user_id in Config.ADMIN_IDS:
        kb.append([InlineKeyboardButton("🛡️ پنل مدیریت", callback_data='admin_panel')])
    return InlineKeyboardMarkup(kb)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    key = db.get_or_create_user(user.id, user.username, user.first_name)
    
    if 'secret_shown' not in context.user_data:
        await update.message.reply_text(f" **امانت امنیتی شما**\n\nکلید: `{key}`\n\n⚠️ این کلید را ذخیره کنید.", parse_mode='Markdown')
        context.user_data['secret_shown'] = True

    if not db.has_consent(user.id):
        kb = [[InlineKeyboardButton("✅ موافقم", callback_data='consent_device')], [InlineKeyboardButton("❌ رد می‌کنم", callback_data='decline_device')]]
        await update.message.reply_text("📱 ذخیره نام و یوزرنیم برای بهبود خدمات. موافقید؟", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    context.user_data['chat_mode'] = False
    context.user_data['action'] = None
    await update.message.reply_text(f"👋 سلام {user.first_name}! به ربات مترجم حرفه‌ای خوش آمدید.", reply_markup=get_main_keyboard(user.id))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['chat_mode'] = False
    context.user_data['action'] = None
    await update.callback_query.edit_message_text(" به منوی اصلی بازگشتید.", reply_markup=get_main_keyboard(update.effective_user.id))

async def smart_assistant_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['chat_mode'] = True
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(
        "🤖 **دستیار هوشمند فعال شد!**\n\n"
        "حالا می‌توانید:\n"
        "1. سوالات خود را درباره ربات بپرسید (مثلاً: 'کاربرد تو چیه؟')\n"
        "2. یا مستقیماً متنی که می‌خواهید ترجمه شود را بفرستید.\n\n"
        "من در خدمتم!",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if context.user_data.get('chat_mode'):
        text_lower = text.lower()
        if "سلام" in text_lower or "درود" in text_lower:
            response = "سلام! 👋 چطور می‌تونم کمکت کنم؟ می‌تونی متنت رو برای ترجمه بفرستی."
        elif "کاربرد" in text_lower or "چیکار" in text_lower:
            response = "من می‌تونم متن، عکس، فایل (PDF/Word) و حتی پیام صوتی شما را با امنیت کامل ترجمه کنم! 🌍"
        elif "ادمین" in text_lower or "سازنده" in text_lower:
            response = "سازنده این ربات یک توسعه‌دهنده حرفه‌ای است که برای راحتی شما این ابزار را ساخته است. "
        else:
            try:
                translated = GoogleTranslator(source='auto', target='fa').translate(text)
                response = f"🔄 **ترجمه:**\n\n{translated}"
            except:
                response = "متوجه نشدم. لطفاً سوالتان را واضح‌تر بپرسید یا متن را برای ترجمه بفرستید."
        
        kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_text(response, reply_markup=InlineKeyboardMarkup(kb))
        return

    if context.user_data.get('action') == 'waiting_for_text':
        target_lang = context.user_data.get('target_lang', 'fa')
        try:
            translator = GoogleTranslator(source='auto', target=target_lang)
            translated_text = translator.translate(text)
            db.save_translation(user_id, text, translated_text, translator._source, target_lang)
            kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
            await update.message.reply_text(f"✅ ترجمه شد:\n\n📝 {translated_text}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
        context.user_data['action'] = None
        return

    await update.message.reply_text("لطفاً ابتدا یک گزینه را از منوی اصلی انتخاب کنید. /start", reply_markup=get_main_keyboard(user_id))

async def show_langs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    langs = {'fa': '🇮 فارسی', 'en': '🇬 انگلیسی', 'ar': '🇦 عربی', 'fr': '🇷 فرانسوی', 'de': '🇩🇪 آلمانی', 'tr': '🇹🇷 ترکی'}
    kb = [[InlineKeyboardButton(name, callback_data=f'lang_{code}')] for code, name in langs.items()]
    kb.append([InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')])
    await update.callback_query.edit_message_text("🎯 زبان مقصد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['action'] = 'select_language'

async def handle_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    code = update.callback_query.data.split('_')[1]
    langs = {'fa': 'فارسی', 'en': 'انگلیسی', 'ar': 'عربی', 'fr': 'فرانسوی', 'de': 'آلمانی', 'tr': 'ترکی'}
    context.user_data['target_lang'] = code
    context.user_data['action'] = 'waiting_for_text'
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(f"✅ زبان {langs[code]} انتخاب شد.\nمتن خود را بفرستید:", reply_markup=InlineKeyboardMarkup(kb))

async def show_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    key = db.get_or_create_user(update.effective_user.id)
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(f"🔑 **کلید امنیتی شما:**\n\n`{key}`", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def consent_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = update.effective_user
    db.save_device_info(user.id, f"{user.first_name} (@{user.username or 'N/A'})")
    await update.callback_query.edit_message_text("✅ ممنون! اطلاعات ذخیره شد.", reply_markup=get_main_keyboard(user.id))

async def decline_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("❌ بدون ذخیره اطلاعات ادامه می‌دهیم.", reply_markup=get_main_keyboard(update.effective_user.id))

async def placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("این قابلیت به زودی اضافه می‌شود! 🚀", reply_markup=InlineKeyboardMarkup(kb))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.callback_query.answer(" غیرمجاز!", show_alert=True)
        return
    
    stats = db.get_admin_dashboard()
    kb = [
        [InlineKeyboardButton("📢 ارسال پیام همگانی دستی", callback_data='admin_broadcast_manual')],
        [InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]
    ]
    msg = (f"🛡️ **پنل مدیریت**\n\n"
           f"👥 کل کاربران: {stats['total_users']}\n"
           f"📝 کل ترجمه‌ها: {stats['total_translations']:,}")
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_broadcast_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("متن پیام همگانی خود را بفرستید (یا /cancel برای انصراف):")
    context.user_data['admin_action'] = 'wait_broadcast_msg'

async def handle_admin_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS: return
    if context.user_data.get('admin_action') != 'wait_broadcast_msg': return
    
    msg = update.message.text
    users = db.get_all_user_ids()
    success = 0
    
    await update.message.reply_text(f"⏳ در حال ارسال به {len(users)} کاربر...")
    
    for uid in users:
        try:
            await update.message.bot.send_message(chat_id=uid, text=f"📢 **پیام ادمین:**\n\n{msg}", parse_mode='Markdown')
            success += 1
            time.sleep(0.05)
        except Exception:
            pass
            
    await update.message.reply_text(f"✅ ارسال تکمیل شد!\nموفق: {success} | ناموفق: {len(users) - success}")
    context.user_data['admin_action'] = None

def send_update_notification(app):
    users = db.get_all_user_ids()
    if not users:
        logger.info("کاربری برای ارسال پیام آپدیت وجود ندارد.")
        return
        
    msg = (
        "🎉 **ربات به‌روزرسانی شد!**\n\n"
        "✅ دیتابیس ابری Supabase (داده‌ها دیگر پاک نمی‌شوند!)\n"
        "✅ افزودن دکمه بازگشت به منو در تمام صفحات\n"
        "✅ اضافه شدن دستیار هوشمند برای پاسخ به سوالات شما\n"
        "✅ بهبود سرعت و امنیت سرور\n\n"
        "ممنون که از ربات ما استفاده می‌کنید! ❤️\n"
        "برای شروع: /start"
    )
    
    success = 0
    logger.info(f"شروع ارسال پیام همگانی آپدیت به {len(users)} کاربر...")
    for uid in users:
        try:
            app.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
            success += 1
            time.sleep(0.05)
        except Exception as e:
            pass
            
    logger.info(f"✅ پیام آپدیت با موفقیت به {success} کاربر از {len(users)} کاربر ارسال شد.")

def main():
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(smart_assistant_menu, pattern='^smart_assistant$'))
    app.add_handler(CallbackQueryHandler(show_langs, pattern='^translate_text$'))
    app.add_handler(CallbackQueryHandler(handle_lang, pattern='^lang_'))
    app.add_handler(CallbackQueryHandler(show_key, pattern='^show_key$'))
    app.add_handler(CallbackQueryHandler(consent_device, pattern='^consent_device$'))
    app.add_handler(CallbackQueryHandler(decline_device, pattern='^decline_device$'))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    app.add_handler(CallbackQueryHandler(admin_broadcast_manual, pattern='^admin_broadcast_manual$'))
    
    app.add_handler(CallbackQueryHandler(placeholder, pattern='^(translate_photo|translate_voice|translate_file|my_stats)$'))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^.*$'), handle_admin_broadcast_msg))
    
    webhook_url = f"{Config.RENDER_URL}/{Config.BOT_TOKEN}"
    
    logger.info("✅ ربات با Webhook فعال شد! در حال تنظیم اطلاع‌رسانی آپدیت...")
    
    send_update_notification(app)
    
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 8080)),
        url_path=Config.BOT_TOKEN,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == '__main__':
    main()
