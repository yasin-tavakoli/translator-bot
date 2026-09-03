import os, time, logging, speech_recognition as sr, json, hashlib
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
import re

# ==========================================================
# ⚙️ تنظیمات حرفه‌ای با Environment Variables
# ==========================================================
class Config:
    BOT_TOKEN = os.getenv('BOT_TOKEN', '8791676273:AAEIw5JaJmZk9f7YqOdO1Xq1Fm0KBkvteTQ')
    ADMIN_IDS = [5138190544]
    MAX_FILE_SIZE = 20971520  # 20MB
    RATE_LIMIT = 30  # تعداد درخواست در دقیقه
    MAX_TEXT_LENGTH = 5000
    ALLOWED_FILE_TYPES = ['pdf', 'docx', 'txt']
    RENDER_URL = os.getenv('RENDER_URL', 'https://translator-bot-z4wh.onrender.com')
    DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://postgres:YasinBot2026%21Secure@db.qwhkjpljsaledhqbgkbp.supabase.co:5432/postgres?sslmode=require')
    
    # تنظیمات امنیتی
    ENABLE_RATE_LIMIT = True
    ENABLE_LOGGING = True
    MAX_CONCURRENT_REQUESTS = 5
    SESSION_TIMEOUT = 3600  # 1 ساعت
    
    # کش
    CACHE_EXPIRY = 300  # 5 دقیقه

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==========================================================
# 🔐 سیستم Rate Limiting پیشرفته
# ==========================================================
class AdvancedRateLimiter:
    def __init__(self):
        self.user_requests = defaultdict(list)
        self.user_sessions = {}
    
    def is_allowed(self, user_id):
        if not Config.ENABLE_RATE_LIMIT:
            return True
        
        now = time.time()
        
        # بررسی session
        if user_id in self.user_sessions:
            if now - self.user_sessions[user_id] > Config.SESSION_TIMEOUT:
                self.user_sessions[user_id] = now
                self.user_requests[user_id] = []
        else:
            self.user_sessions[user_id] = now
        
        # پاک‌سازی درخواست‌های قدیمی
        self.user_requests[user_id] = [t for t in self.user_requests[user_id] if now - t < 60]
        
        if len(self.user_requests[user_id]) >= Config.RATE_LIMIT:
            logger.warning(f"️ Rate limit exceeded for user {user_id}")
            return False
        
        self.user_requests[user_id].append(now)
        return True
    
    def get_remaining_requests(self, user_id):
        now = time.time()
        self.user_requests[user_id] = [t for t in self.user_requests[user_id] if now - t < 60]
        return max(0, Config.RATE_LIMIT - len(self.user_requests[user_id]))

rate_limiter = AdvancedRateLimiter()

# ==========================================================
# 💾 سیستم کش هوشمند
# ==========================================================
class SmartCache:
    def __init__(self):
        self.cache = {}
    
    def get(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < Config.CACHE_EXPIRY:
                return data
            else:
                del self.cache[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = (value, time.time())
    
    def clear(self):
        self.cache.clear()

smart_cache = SmartCache()

# ==========================================================
# 🗄️ دیتابیس Supabase با امنیت بالا
# ==========================================================
class SecureDatabase:
    def __init__(self):
        try:
            self.conn = psycopg2.connect(Config.DATABASE_URL)
            logger.info("✅ اتصال به دیتابیس ابری Supabase برقرار شد")
        except Exception as e:
            logger.error(f"❌ خطا در اتصال به دیتابیس: {e}")
            raise
    
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
            return "[خطا]"
    
    def save_translation(self, user_id, source_text, translated_text, source_lang, target_lang, trans_type='text'):
        key = self.get_or_create_user(user_id)
        c = self.conn.cursor()
        
        # اعتبارسنجی ورودی
        if not source_text or len(source_text) > Config.MAX_TEXT_LENGTH:
            raise ValueError("متن نامعتبر است")
        
        encrypted_source = self.encrypt(source_text, key)
        encrypted_target = self.encrypt(translated_text, key)
        word_count = len(source_text.split())
        char_count = len(source_text)
        
        c.execute('''INSERT INTO translations 
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, translation_type, word_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (user_id, encrypted_source, encrypted_target, source_lang, target_lang, trans_type, word_count))
        
        trans_id = c.fetchone()[0]
        
        c.execute('''UPDATE user_stats SET 
            total_translations = total_translations + 1, 
            total_words = total_words + %s, 
            total_chars = COALESCE(total_chars, 0) + %s,
            last_activity = NOW()
            WHERE user_id = %s''', (word_count, char_count, user_id))
        
        self.conn.commit()
        return trans_id

    def get_user_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT total_translations, total_words, total_chars, last_activity FROM user_stats WHERE user_id = %s', (user_id,))
        return c.fetchone()
    
    def get_all_user_ids(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row[0] for row in c.fetchall()]
    
    def get_admin_dashboard(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM users'); total = c.fetchone()[0]
        c.execute('SELECT SUM(total_translations) FROM user_stats'); trans = c.fetchone()[0] or 0
        c.execute('SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL \'7 days\''); active = c.fetchone()[0]
        return {'total_users': total, 'total_translations': trans, 'active_users': active}

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
    
    def log_security_event(self, user_id, event_type, details):
        """ثبت رویدادهای امنیتی"""
        if Config.ENABLE_LOGGING:
            c = self.conn.cursor()
            c.execute('''INSERT INTO admin_logs (admin_id, action, details) 
                         VALUES (%s, %s, %s)''', (user_id, f"SECURITY_{event_type}", details))
            self.conn.commit()
            logger.warning(f" Security Event: {event_type} - User: {user_id} - {details}")

db = SecureDatabase()

# ==========================================================
# 🔍 سیستم اعتبارسنجی و امنیت
# ==========================================================
class SecurityValidator:
    @staticmethod
    def validate_text(text):
        """بررسی متن برای جلوگیری از Injection"""
        if not text:
            return False, "متن خالی است"
        if len(text) > Config.MAX_TEXT_LENGTH:
            return False, f"متن بیش از حد طولانی است (حداکثر {Config.MAX_TEXT_LENGTH} کاراکتر)"
        
        # بررسی کاراکترهای مشکوک
        suspicious_patterns = [
            r'<script>',
            r'javascript:',
            r'onerror=',
            r'onclick='
        ]
        for pattern in suspicious_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return False, "متن حاوی محتوای مشکوک است"
        
        return True, "OK"
    
    @staticmethod
    def validate_file(filename, file_size):
        """بررسی فایل از نظر امنیتی"""
        if file_size > Config.MAX_FILE_SIZE:
            return False, f"حجم فایل بیش از حد است (حداکثر {Config.MAX_FILE_SIZE // 1024 // 1024}MB)"
        
        ext = filename.split('.')[-1].lower() if '.' in filename else ''
        if ext not in Config.ALLOWED_FILE_TYPES:
            return False, f"نوع فایل مجاز نیست. انواع مجاز: {', '.join(Config.ALLOWED_FILE_TYPES)}"
        
        return True, "OK"

# ==========================================================
# 🤖 هندلرهای پیشرفته
# ==========================================================
def get_main_keyboard(user_id):
    kb = [
        [InlineKeyboardButton("🌐 ترجمه متن", callback_data='translate_text'), 
         InlineKeyboardButton("🖼️ ترجمه عکس", callback_data='translate_photo')],
        [InlineKeyboardButton("🎤 ترجمه صوتی", callback_data='translate_voice'), 
         InlineKeyboardButton("📄 ترجمه فایل", callback_data='translate_file')],
        [InlineKeyboardButton("🤖 دستیار هوشمند", callback_data='smart_assistant'), 
         InlineKeyboardButton("📊 آمار من", callback_data='my_stats')],
        [InlineKeyboardButton("🔑 کلید امنیتی", callback_data='show_key')]
    ]
    if user_id in Config.ADMIN_IDS:
        kb.append([InlineKeyboardButton("🛡️ پنل مدیریت", callback_data='admin_panel')])
    return InlineKeyboardMarkup(kb)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    # بررسی Rate Limit
    if not rate_limiter.is_allowed(user_id):
        remaining = rate_limiter.get_remaining_requests(user_id)
        await update.message.reply_text(f"⚠️ تعداد درخواست‌های شما بیش از حد است.\nلطفاً {60} ثانیه صبر کنید.\nدرخواست‌های باقیمانده: {remaining}")
        db.log_security_event(user_id, "RATE_LIMIT", f"User exceeded rate limit")
        return
    
    key = db.get_or_create_user(user_id, user.username, user.first_name)
    
    if 'secret_shown' not in context.user_data:
        await update.message.reply_text(
            f"🔐 **امانت امنیتی شما**\n\n"
            f"کلید منحصر به فرد شما:\n\n`{key}`\n\n"
            f"⚠️ این کلید را در جای امنی ذخیره کنید. بدون آن به ترجمه‌هایتان دسترسی نخواهید داشت.",
            parse_mode='Markdown'
        )
        context.user_data['secret_shown'] = True

    if not db.has_consent(user_id):
        kb = [[InlineKeyboardButton("✅ موافقم", callback_data='consent_device')], 
              [InlineKeyboardButton("❌ رد می‌کنم", callback_data='decline_device')]]
        await update.message.reply_text(
            "📱 **حریم خصوصی**\n\n"
            "برای بهبود خدمات، اطلاعات زیر ذخیره می‌شوند:\n"
            "• نام و نام کاربری\n"
            "• زمان آخرین فعالیت\n"
            "• آمار استفاده از ربات\n\n"
            "آیا موافقید؟",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return
    
    context.user_data['chat_mode'] = False
    context.user_data['action'] = None
    await update.message.reply_text(
        f"👋 سلام {user.first_name}!\n"
        f"به ربات مترجم حرفه‌ای و امن خوش آمدید.\n\n"
        f"یک گزینه انتخاب کنید:",
        reply_markup=get_main_keyboard(user_id)
    )

async def consent_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = update.effective_user
    db.save_device_info(user.id, f"{user.first_name} (@{user.username or 'N/A'})")
    logger.info(f"✅ User {user.id} consented to data collection")
    await update.callback_query.edit_message_text(
        "✅ ممنون! اطلاعات با موفقیت ذخیره شد.\n\n"
        "اکنون می‌توانید از تمام امکانات ربات استفاده کنید.",
        reply_markup=get_main_keyboard(user.id)
    )

async def decline_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    logger.info(f"❌ User {update.effective_user.id} declined data collection")
    await update.callback_query.edit_message_text(
        " بدون ذخیره اطلاعات ادامه می‌دهیم.\n"
        "برخی امکانات ممکن است محدود باشند.",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['chat_mode'] = False
    context.user_data['action'] = None
    await update.callback_query.edit_message_text(
        "🏠 به منوی اصلی بازگشتید.",
        reply_markup=get_main_keyboard(update.effective_user.id)
    )

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
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # بررسی Rate Limit
    if not rate_limiter.is_allowed(user_id):
        await update.message.reply_text("⚠️ لطفاً کمی صبر کنید و سپس دوباره تلاش کنید.")
        return

    # اعتبارسنجی متن
    is_valid, msg = SecurityValidator.validate_text(text)
    if not is_valid:
        await update.message.reply_text(f"❌ خطا: {msg}")
        db.log_security_event(user_id, "INVALID_INPUT", msg)
        return

    if context.user_data.get('chat_mode'):
        text_lower = text.lower()
        if "سلام" in text_lower or "درود" in text_lower:
            response = "سلام! 👋 چطور می‌تونم کمکت کنم؟ می‌تونی متنت رو برای ترجمه بفرستی."
        elif "کاربرد" in text_lower or "چیکار" in text_lower:
            response = "من می‌تونم متن، عکس، فایل (PDF/Word) و حتی پیام صوتی شما را با امنیت کامل ترجمه کنم! 🌍"
        elif "ادمین" in text_lower or "سازنده" in text_lower:
            response = "سازنده این ربات یک توسعه‌دهنده حرفه‌ای است که برای راحتی شما این ابزار را ساخته است. 💻"
        else:
            # بررسی کش
            cache_key = f"translate_{text}_{hashlib.md5(text.encode()).hexdigest()}"
            cached = smart_cache.get(cache_key)
            if cached:
                response = f"🔄 **ترجمه (از کش):**\n\n{cached}"
            else:
                try:
                    translated = GoogleTranslator(source='auto', target='fa').translate(text)
                    smart_cache.set(cache_key, translated)
                    response = f"🔄 **ترجمه:**\n\n{translated}"
                except Exception as e:
                    response = "متوجه نشدم. لطفاً سوالتان را واضح‌تر بپرسید یا متن را برای ترجمه بفرستید."
                    logger.error(f"Translation error: {e}")
        
        kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_text(response, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return

    if context.user_data.get('action') == 'waiting_for_text':
        target_lang = context.user_data.get('target_lang', 'fa')
        try:
            translator = GoogleTranslator(source='auto', target=target_lang)
            translated_text = translator.translate(text)
            db.save_translation(user_id, text, translated_text, translator._source, target_lang)
            kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
            await update.message.reply_text(
                f"✅ ترجمه شد:\n\n"
                f"🔤 زبان مبدأ: {translator._source}\n"
                f"🎯 زبان مقصد: {target_lang}\n\n"
                f"📝 {translated_text}",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"Translation error: {e}")
            await update.message.reply_text(f"❌ خطا در ترجمه: {str(e)}")
        context.user_data['action'] = None
        return

    await update.message.reply_text(
        "لطفاً ابتدا یک گزینه را از منوی اصلی انتخاب کنید.\n"
        "یا از دستور /start استفاده کنید.",
        reply_markup=get_main_keyboard(user_id)
    )

async def show_langs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    langs = {'fa': '🇷 فارسی', 'en': '🇬🇧 انگلیسی', 'ar': '🇸🇦 عربی', 'fr': '🇫🇷 فرانسوی', 'de': '🇩 آلمانی', 'tr': '🇷 ترکی'}
    kb = [[InlineKeyboardButton(name, callback_data=f'lang_{code}')] for code, name in langs.items()]
    kb.append([InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')])
    await update.callback_query.edit_message_text(
        "🎯 **زبان مقصد را انتخاب کنید:**",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    context.user_data['action'] = 'select_language'

async def handle_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    code = update.callback_query.data.split('_')[1]
    langs = {'fa': 'فارسی', 'en': 'انگلیسی', 'ar': 'عربی', 'fr': 'فرانسوی', 'de': 'آلمانی', 'tr': 'ترکی'}
    context.user_data['target_lang'] = code
    context.user_data['action'] = 'waiting_for_text'
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(
        f"✅ زبان {langs[code]} انتخاب شد.\n\n"
        f" متن خود را بفرستید:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def show_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    key = db.get_or_create_user(update.effective_user.id)
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(
        f"🔑 **کلید امنیتی شما:**\n\n"
        f"`{key}`\n\n"
        f"⚠️ این کلید را در جای امنی ذخیره کنید!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(
        "🚧 این قابلیت در حال توسعه است و به زودی اضافه می‌شود!",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        db.log_security_event(update.effective_user.id, "UNAUTHORIZED_ACCESS", "Attempted to access admin panel")
        return
    
    stats = db.get_admin_dashboard()
    kb = [
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data='admin_broadcast')],
        [InlineKeyboardButton("📊 آمار کامل", callback_data='admin_full_stats')],
        [InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]
    ]
    msg = (
        f"🛡️ **پنل مدیریت پیشرفته**\n\n"
        f"📊 **آمار لحظه‌ای:**\n"
        f"👥 کل کاربران: {stats['total_users']}\n"
        f"✅ کاربران فعال ( روز): {stats['active_users']}\n"
        f"📝 کل ترجمه‌ها: {stats['total_translations']:,}\n"
    )
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        " **ارسال پیام همگانی**\n\n"
        "متن پیام خود را ارسال کنید (یا /cancel برای انصراف):"
    )
    context.user_data['admin_action'] = 'wait_broadcast_msg'

async def handle_admin_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS: 
        return
    if context.user_data.get('admin_action') != 'wait_broadcast_msg': 
        return
    
    msg = update.message.text
    users = db.get_all_user_ids()
    success = 0
    failed = 0
    
    await update.message.reply_text(f" در حال ارسال به {len(users)} کاربر...")
    
    for uid in users:
        try:
            await update.message.bot.send_message(
                chat_id=uid, 
                text=f"📢 **پیام از طرف ادمین:**\n\n{msg}",
                parse_mode='Markdown'
            )
            success += 1
            time.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.error(f"Failed to send to user {uid}: {e}")
            
    await update.message.reply_text(
        f"✅ **ارسال تکمیل شد!**\n\n"
        f"📊 آمار:\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}\n"
        f"👥 کل: {len(users)}"
    )
    context.user_data['admin_action'] = None

# ==========================================================
# 📢 اطلاع‌رسانی خودکار آپدیت
# ==========================================================
def send_update_notification(app):
    users = db.get_all_user_ids()
    if not users:
        logger.info("کاربری برای ارسال پیام آپدیت وجود ندارد.")
        return
        
    msg = (
        "🎉 **ربات به‌روزرسانی شد!**\n\n"
        "✨ **ویژگی‌های جدید:**\n"
        "✅ دیتابیس ابری Supabase (داده‌ها دیگر پاک نمی‌شوند!)\n"
        "✅ سیستم Rate Limiting (جلوگیری از اسپم)\n"
        "✅ کش هوشمند (سرعت بالاتر)\n"
        "✅ اعتبارسنجی پیشرفته ورودی‌ها\n"
        "✅ دکمه بازگشت به منو در تمام صفحات\n"
        "✅ دستیار هوشمند برای پاسخ به سوالات\n\n"
        "🔐 **بهبودهای امنیتی:**\n"
        "• رمزنگاری قوی‌تر داده‌ها\n"
        "• لاگ‌گیری پیشرفته\n"
        "• محافظت در برابر حملات\n\n"
        "ممنون که از ربات ما استفاده می‌کنید! ❤️\n"
        "برای شروع: /start"
    )
    
    success = 0
    logger.info(f" شروع ارسال پیام همگانی آپدیت به {len(users)} کاربر...")
    for uid in users:
        try:
            app.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
            success += 1
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to notify user {uid}: {e}")
            
    logger.info(f"✅ پیام آپدیت به {success} کاربر از {len(users)} کاربر ارسال شد.")

# ==========================================================
# ▶️ اجرای اصلی
# ==========================================================
def main():
    logger.info("🚀 شروع راه‌اندازی ربات...")
    
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    # دستورات اصلی
    app.add_handler(CommandHandler("start", start))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(consent_device, pattern='^consent_device$'))
    app.add_handler(CallbackQueryHandler(decline_device, pattern='^decline_device$'))
    app.add_handler(CallbackQueryHandler(smart_assistant_menu, pattern='^smart_assistant$'))
    app.add_handler(CallbackQueryHandler(show_langs, pattern='^translate_text$'))
    app.add_handler(CallbackQueryHandler(handle_lang, pattern='^lang_'))
    app.add_handler(CallbackQueryHandler(show_key, pattern='^show_key$'))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    app.add_handler(CallbackQueryHandler(admin_broadcast, pattern='^admin_broadcast$'))
    app.add_handler(CallbackQueryHandler(placeholder, pattern='^(translate_photo|translate_voice|translate_file|my_stats)$'))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^.*$'), handle_admin_broadcast_msg))
    
    webhook_url = f"{Config.RENDER_URL}/{Config.BOT_TOKEN}"
    
    logger.info("✅ ربات با موفقیت راه‌اندازی شد!")
    logger.info("🔗 Webhook URL: " + webhook_url)
    
    # ارسال پیام آپدیت
    send_update_notification(app)
    
    # شروع Webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get('PORT', 8080)),
        url_path=Config.BOT_TOKEN,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == '__main__':
    main()
