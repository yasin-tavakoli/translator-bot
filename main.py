import os, time, logging, speech_recognition as sr
from datetime import datetime
from collections import defaultdict
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from deep_translator import GoogleTranslator, MyMemoryTranslator
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
    BOT_TOKEN = os.getenv('BOT_TOKEN', '8791676273:AAEIw5JaJmZk9f7YqOdO1Xq1Fm0KBkvteTQ')
    ADMIN_IDS = [5138190544]
    MAX_FILE_SIZE = 20971520
    RATE_LIMIT = 30
    MAX_TEXT_LENGTH = 5000
    ALLOWED_FILE_TYPES = ['pdf', 'docx', 'txt']
    RENDER_URL = os.getenv('RENDER_URL', 'https://translator-bot-z4wh.onrender.com')
    DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://postgres.qwhkjpljsaledhqbgkbp:YasinBot2026%21Secure@aws-0-us-west-2.pooler.supabase.com:6543/postgres')

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================================
# 🔐 Rate Limiting
# ==========================================================
class AdvancedRateLimiter:
    def __init__(self):
        self.user_requests = defaultdict(list)
    def is_allowed(self, user_id):
        now = time.time()
        self.user_requests[user_id] = [t for t in self.user_requests[user_id] if now - t < 60]
        if len(self.user_requests[user_id]) >= Config.RATE_LIMIT:
            return False
        self.user_requests[user_id].append(now)
        return True

rate_limiter = AdvancedRateLimiter()

# ==========================================================
# ️ دیتابیس
# ==========================================================
class SecureDatabase:
    def __init__(self):
        try:
            self.conn = psycopg2.connect(Config.DATABASE_URL)
            logger.info("✅ اتصال به دیتابیس Supabase برقرار شد")
        except Exception as e:
            logger.error(f"❌ خطا در اتصال به دیتابیس: {e}")
            raise
    
    def generate_secret_key(self): return Fernet.generate_key().decode()
    
    def get_or_create_user(self, user_id, username=None, first_name=None):
        c = self.conn.cursor(cursor_factory=RealDictCursor)
        c.execute('SELECT secret_key FROM users WHERE user_id = %s', (user_id,))
        r = c.fetchone()
        if r:
            c.execute('UPDATE users SET last_active = NOW() WHERE user_id = %s', (user_id,))
            self.conn.commit()
            return r['secret_key']
        new_key = self.generate_secret_key()
        c.execute('''INSERT INTO users (user_id, username, first_name, secret_key, last_active) VALUES (%s, %s, %s, %s, NOW())''', (user_id, username, first_name, new_key))
        c.execute('INSERT INTO user_stats (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING', (user_id,))
        self.conn.commit()
        return new_key
    
    def encrypt(self, text, key): 
        return Fernet(key.encode()).encrypt(text.encode()).decode() if text else ""
    
    def save_translation(self, user_id, source_text, translated_text, source_lang, target_lang, trans_type='text'):
        key = self.get_or_create_user(user_id)
        c = self.conn.cursor()
        encrypted_source = self.encrypt(source_text, key)
        encrypted_target = self.encrypt(translated_text, key)
        word_count = len(source_text.split())
        c.execute('''INSERT INTO translations (user_id, encrypted_source, encrypted_target, source_lang, target_lang, translation_type, word_count) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                  (user_id, encrypted_source, encrypted_target, source_lang, target_lang, trans_type, word_count))
        c.execute('''UPDATE user_stats SET total_translations = total_translations + 1, total_words = total_words + %s, last_activity = NOW() WHERE user_id = %s''', (word_count, user_id))
        self.conn.commit()

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
        c.execute('SELECT COALESCE(SUM(total_translations), 0) FROM user_stats'); trans = c.fetchone()[0]
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

db = SecureDatabase()

# ==========================================================
# 🌐 سیستم ترجمه هوشمند با Retry و Fallback
# ==========================================================
def smart_translate(text, target_lang='fa', source_lang='auto', max_retries=3):
    """ترجمه با چندین تلاش و چندین موتور"""
    if not text or not text.strip():
        return "", "none"
    
    last_error = None
    
    for attempt in range(max_retries):
        # تاخیر بین تلاش‌ها برای جلوگیری از بلاک شدن
        if attempt > 0:
            time.sleep(2 * attempt)
        
        # تلاش ۱: Google Translate
        try:
            translator = GoogleTranslator(source=source_lang, target=target_lang)
            result = translator.translate(text)
            if result and result.strip() and result != text:
                logger.info(f"✅ Translation success via Google (attempt {attempt+1})")
                return result, 'google'
        except Exception as e:
            last_error = e
            logger.warning(f"Google attempt {attempt+1} failed: {type(e).__name__}")
        
        # تلاش ۲: MyMemory (رایگان و بدون محدودیت سخت)
        try:
            lang_map = {'fa': 'persian', 'en': 'english', 'ar': 'arabic', 'fr': 'french', 'de': 'german', 'tr': 'turkish'}
            target_en = lang_map.get(target_lang, target_lang)
            translator = MyMemoryTranslator(source=source_lang, target=target_en)
            result = translator.translate(text)
            if result and result.strip():
                logger.info(f"✅ Translation success via MyMemory (attempt {attempt+1})")
                return result, 'mymemory'
        except Exception as e:
            last_error = e
            logger.warning(f"MyMemory attempt {attempt+1} failed: {type(e).__name__}")
    
    raise Exception(f"ترجمه انجام نشد. لطفاً ۱ دقیقه بعد دوباره تلاش کنید.")

# ==========================================================
# 🤖 هندلرها
# ==========================================================
def get_main_keyboard(user_id):
    kb = [
        [InlineKeyboardButton("🌐 ترجمه متن", callback_data='translate_text'), InlineKeyboardButton("🖼️ ترجمه عکس", callback_data='translate_photo')],
        [InlineKeyboardButton("🎤 ترجمه صوتی", callback_data='translate_voice'), InlineKeyboardButton("📄 ترجمه فایل", callback_data='translate_file')],
        [InlineKeyboardButton(" دستیار هوشمند", callback_data='smart_assistant'), InlineKeyboardButton("📊 آمار من", callback_data='my_stats')],
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

async def consent_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = update.effective_user
    db.save_device_info(user.id, f"{user.first_name} (@{user.username or 'N/A'})")
    await update.callback_query.edit_message_text("✅ ممنون! اطلاعات ذخیره شد.", reply_markup=get_main_keyboard(user.id))

async def decline_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(" بدون ذخیره اطلاعات ادامه می‌دهیم.", reply_markup=get_main_keyboard(update.effective_user.id))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['chat_mode'] = False
    context.user_data['action'] = None
    await update.callback_query.edit_message_text("🏠 به منوی اصلی بازگشتید.", reply_markup=get_main_keyboard(update.effective_user.id))

async def smart_assistant_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['chat_mode'] = True
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("🤖 **دستیار هوشمند فعال شد!**\n\nمتن خود را بفرستید.", reply_markup=InlineKeyboardMarkup(kb))

async def show_langs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    langs = {'fa': '🇮🇷 فارسی', 'en': '🇬 انگلیسی', 'ar': '🇸🇦 عربی', 'fr': '🇷 فرانسوی', 'de': '🇩🇪 آلمانی', 'tr': '🇹🇷 ترکی'}
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
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(f"✅ زبان {langs[code]} انتخاب شد.\nمتن خود را بفرستید:", reply_markup=InlineKeyboardMarkup(kb))

async def show_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    key = db.get_or_create_user(update.effective_user.id)
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text(f"🔑 **کلید امنیتی شما:**\n\n`{key}`", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    stats = db.get_user_stats(update.effective_user.id)
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    if stats and stats[0] > 0:
        last_act = str(stats[2])[:16] if stats[2] else 'ندارد'
        msg = f"📊 **آمار شما:**\n\n🔢 تعداد ترجمه‌ها: {stats[0]}\n📝 مجموع کلمات: {stats[1]}\n🕒 آخرین فعالیت: {last_act}"
    else:
        msg = "📊 **آمار شما:**\n\nهنوز ترجمه‌ای انجام نداده‌اید!"
    await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

# ==========================================================
# 🖼️🎤📄 Handlerهای دکمه‌های عکس/صوت/فایل (که قبلاً نبودند!)
# ==========================================================
async def translate_photo_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['action'] = 'waiting_for_photo'
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("🖼️ لطفاً عکس مورد نظر را بفرستید:", reply_markup=InlineKeyboardMarkup(kb))

async def translate_voice_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['action'] = 'waiting_for_voice'
    kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("🎤 لطفاً پیام صوتی خود را بفرستید:", reply_markup=InlineKeyboardMarkup(kb))

async def translate_file_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['action'] = 'waiting_for_file'
    kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("📄 لطفاً فایل خود را بفرستید (PDF, DOCX, TXT):", reply_markup=InlineKeyboardMarkup(kb))

# ==========================================================
# ⚙️ Handlerهای عملیاتی
# ==========================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not rate_limiter.is_allowed(user_id):
        await update.message.reply_text("⚠️ تعداد درخواست‌ها زیاد است. لطفاً ۱ دقیقه صبر کنید.")
        return

    if context.user_data.get('chat_mode'):
        try:
            translated, engine = smart_translate(text, target_lang='fa')
            response = f"🔄 **ترجمه ({engine}):**\n\n{translated}"
        except Exception as e:
            response = f"❌ {str(e)}"
        kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_text(response, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return

    if context.user_data.get('action') == 'waiting_for_text':
        target_lang = context.user_data.get('target_lang', 'fa')
        try:
            translated_text, engine = smart_translate(text, target_lang=target_lang)
            db.save_translation(user_id, text, translated_text, 'auto', target_lang)
            kb = [[InlineKeyboardButton(" بازگشت به منو", callback_data='back_to_menu')]]
            await update.message.reply_text(f"✅ ترجمه شد ({engine}):\n\n📝 {translated_text}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await update.message.reply_text(f"❌ {str(e)}")
        context.user_data['action'] = None
        return

    await update.message.reply_text("لطفاً ابتدا یک گزینه را از منوی اصلی انتخاب کنید. /start", reply_markup=get_main_keyboard(user_id))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not rate_limiter.is_allowed(user_id):
        await update.message.reply_text("⚠️ لطفاً ۱ دقیقه صبر کنید.")
        return
        
    await update.message.reply_text("🖼️ در حال خواندن متن از تصویر...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        path = f"temp_img_{user_id}.jpg"
        await file.download_to_drive(path)
        
        text = pytesseract.image_to_string(Image.open(path), lang='fas+eng')
        os.remove(path)
        
        if not text.strip():
            await update.message.reply_text("⚠️ متنی در تصویر پیدا نشد. لطفاً تصویر واضح‌تری بفرستید.", reply_markup=get_main_keyboard(user_id))
            return
            
        target_lang = context.user_data.get('target_lang', 'fa')
        translated, engine = smart_translate(text, target_lang=target_lang)
        db.save_translation(user_id, "Image", translated, "image", target_lang)
        
        kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_text(f"✅ متن استخراج و ترجمه شد ({engine}):\n\n📝 {translated}", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text("❌ خطا در پردازش تصویر.", reply_markup=get_main_keyboard(user_id))

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not rate_limiter.is_allowed(user_id):
        await update.message.reply_text("⚠️ لطفاً ۱ دقیقه صبر کنید.")
        return

    await update.message.reply_text("🎤 در حال تبدیل صوت به متن...")
    try:
        voice = update.message.voice
        file = await voice.get_file()
        ogg_path = f"temp_voice_{user_id}.ogg"
        wav_path = f"temp_voice_{user_id}.wav"
        await file.download_to_drive(ogg_path)
        
        sound = AudioSegment.from_file(ogg_path, format="ogg")
        sound.export(wav_path, format="wav")
        
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            
        try:
            text = recognizer.recognize_google(audio, language='fa-IR')
        except sr.UnknownValueError:
            try:
                text = recognizer.recognize_google(audio, language='en-US')
            except sr.UnknownValueError:
                await update.message.reply_text("⚠️ صدا تشخیص داده نشد.", reply_markup=get_main_keyboard(user_id))
                os.remove(ogg_path); os.remove(wav_path)
                return
                
        os.remove(ogg_path); os.remove(wav_path)
        
        translated, engine = smart_translate(text, target_lang='fa')
        db.save_translation(user_id, "Voice", translated, "voice", 'fa')
        
        kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_text(f"✅ صوت ترجمه شد ({engine}):\n\n📝 {translated}", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ خطا در پردازش صوت.", reply_markup=get_main_keyboard(user_id))

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document
    
    if doc.file_size > Config.MAX_FILE_SIZE:
        await update.message.reply_text(f"⚠️ حجم فایل زیاد است (حداکثر {Config.MAX_FILE_SIZE // 1024 // 1024} مگابایت).", reply_markup=get_main_keyboard(user_id))
        return
        
    ext = doc.file_name.split('.')[-1].lower() if '.' in doc.file_name else ''
    if ext not in Config.ALLOWED_FILE_TYPES:
        await update.message.reply_text(f"⚠️ فرمت فایل پشتیبانی نمی‌شود. فقط: {', '.join(Config.ALLOWED_FILE_TYPES)}", reply_markup=get_main_keyboard(user_id))
        return

    await update.message.reply_text("📄 در حال استخراج و ترجمه متن فایل...")
    try:
        file = await doc.get_file()
        path = f"temp_file.{ext}"
        await file.download_to_drive(path)
        
        text = ""
        if ext == 'txt':
            with open(path, 'r', encoding='utf-8') as f: text = f.read()
        elif ext == 'pdf':
            for page in PyPDF2.PdfReader(path).pages: text += page.extract_text() + '\n'
        elif ext == 'docx':
            for p in Document(path).paragraphs: text += p.text + '\n'
            
        if len(text) > Config.MAX_TEXT_LENGTH:
            text = text[:Config.MAX_TEXT_LENGTH] + "\n\n(متن طولانی بود و خلاصه شد)"
            
        translated, engine = smart_translate(text, target_lang='fa')
        db.save_translation(user_id, "File", translated, "file", 'fa')
        
        with open("translated.txt", "w", encoding="utf-8") as f: f.write(translated)
        
        kb = [[InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]]
        await update.message.reply_document(document=open("translated.txt", "rb"), caption=f"✅ فایل ترجمه شد ({engine})!", reply_markup=InlineKeyboardMarkup(kb))
        os.remove(path); os.remove("translated.txt")
    except Exception as e:
        logger.error(f"Document error: {e}")
        await update.message.reply_text("❌ خطا در خواندن فایل.", reply_markup=get_main_keyboard(user_id))

# ==========================================================
# 🛡️ پنل مدیریت کامل
# ==========================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ دسترسی غیرمجاز!", show_alert=True)
        return
    
    stats = db.get_admin_dashboard()
    kb = [
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data='admin_broadcast')],
        [InlineKeyboardButton("🔙 بازگشت به منو", callback_data='back_to_menu')]
    ]
    msg = (f"🛡️ **پنل مدیریت**\n\n"
           f"👥 کل کاربران: {stats['total_users']}\n"
           f"✅ کاربران فعال (۷ روز): {stats['active_users']}\n"
           f"📝 کل ترجمه‌ها: {stats['total_translations']:,}")
    await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.callback_query.answer("⛔ غیرمجاز!", show_alert=True)
        return
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("متن پیام همگانی را بفرستید (یا /cancel برای انصراف):")
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

# ==========================================================
# ▶️ اجرای اصلی
# ==========================================================
def main():
    logger.info("🚀 شروع راه‌اندازی ربات...")
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    # دستورات
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", lambda u, c: (c.user_data.clear(), u.message.reply_text("❌ انصراف داده شد."))[1]))
    
    # Callback handlers - **همه دکمه‌ها**
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(consent_device, pattern='^consent_device$'))
    app.add_handler(CallbackQueryHandler(decline_device, pattern='^decline_device$'))
    app.add_handler(CallbackQueryHandler(smart_assistant_menu, pattern='^smart_assistant$'))
    app.add_handler(CallbackQueryHandler(show_langs, pattern='^translate_text$'))
    app.add_handler(CallbackQueryHandler(handle_lang, pattern='^lang_'))
    app.add_handler(CallbackQueryHandler(show_key, pattern='^show_key$'))
    app.add_handler(CallbackQueryHandler(show_stats, pattern='^my_stats$'))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    app.add_handler(CallbackQueryHandler(admin_broadcast, pattern='^admin_broadcast$'))
    
    # ✅ Handlerهای جدید برای دکمه‌های عکس/صوت/فایل
    app.add_handler(CallbackQueryHandler(translate_photo_info, pattern='^translate_photo$'))
    app.add_handler(CallbackQueryHandler(translate_voice_info, pattern='^translate_voice$'))
    app.add_handler(CallbackQueryHandler(translate_file_info, pattern='^translate_file$'))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^.*$'), handle_admin_broadcast_msg))
    
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
