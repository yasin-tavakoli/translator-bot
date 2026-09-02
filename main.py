import os, time, logging, sqlite3, speech_recognition as sr
from datetime import datetime
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

# ==========================================================
# ⚙️ تنظیمات
# ==========================================================
class Config:
    BOT_TOKEN = "8791676273:AAEIw5JaJmZk9f7YqOdO1Xq1Fm0KBkvteTQ"
    ADMIN_IDS = [5138190544]
    MAX_FILE_SIZE = 10485760
    RATE_LIMIT = 30
    MAX_TEXT_LENGTH = 4000
    ALLOWED_FILE_TYPES = ['pdf', 'docx', 'txt']

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==========================================================
# 🗄️ دیتابیس امن (رمزنگاری شده)
# ==========================================================
class SecureDatabase:
    def __init__(self):
        self.conn = sqlite3.connect('bot_data.db', check_same_thread=False)
        self.init_tables()
    
    def init_tables(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, secret_key TEXT, device_info TEXT, consent_given INTEGER DEFAULT 0, chat_consent INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS translations (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, encrypted_source TEXT, encrypted_target TEXT, source_lang TEXT, target_lang TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_stats (user_id INTEGER PRIMARY KEY, total_translations INTEGER DEFAULT 0, total_words INTEGER DEFAULT 0, last_activity DATETIME, theme TEXT DEFAULT 'light')''')
        c.execute('''CREATE TABLE IF NOT EXISTS language_usage (user_id INTEGER, lang_code TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (user_id, lang_code))''')
        c.execute('''CREATE TABLE IF NOT EXISTS game_progress (user_id INTEGER, word_id INTEGER, correct_answers INTEGER DEFAULT 0, wrong_answers INTEGER DEFAULT 0, last_played DATETIME, PRIMARY KEY (user_id, word_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS admin_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message TEXT, sender TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        self.conn.commit()
    
    def generate_secret_key(self): return Fernet.generate_key().decode()
    
    def get_or_create_user_key(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT secret_key FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        if r: return r[0]
        new_key = self.generate_secret_key()
        c.execute('INSERT INTO users (user_id, secret_key) VALUES (?, ?)', (user_id, new_key))
        self.conn.commit()
        return new_key
    
    def encrypt(self, text, key): return Fernet(key.encode()).encrypt(text.encode()).decode() if text else ""
    def decrypt(self, encrypted_text, key):
        try: return Fernet(key.encode()).decrypt(encrypted_text.encode()).decode() if encrypted_text else ""
        except: return "[خطا در رمزگشایی]"
    
    def save_translation(self, user_id, source_text, translated_text, source_lang, target_lang):
        key = self.get_or_create_user_key(user_id)
        c = self.conn.cursor()
        c.execute('INSERT INTO translations (user_id, encrypted_source, encrypted_target, source_lang, target_lang) VALUES (?, ?, ?, ?, ?)', 
                  (user_id, self.encrypt(source_text, key), self.encrypt(translated_text, key), source_lang, target_lang))
        word_count = len(source_text.split())
        c.execute('INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)', (user_id,))
        c.execute('UPDATE user_stats SET total_translations = total_translations + 1, total_words = total_words + ?, last_activity = ? WHERE user_id = ?', (word_count, datetime.now(), user_id))
        c.execute('INSERT OR IGNORE INTO language_usage (user_id, lang_code) VALUES (?, ?)', (user_id, target_lang))
        c.execute('UPDATE language_usage SET count = count + 1 WHERE user_id = ? AND lang_code = ?', (user_id, target_lang))
        self.conn.commit()
    
    def get_user_stats(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT total_translations, total_words, last_activity FROM user_stats WHERE user_id = ?', (user_id,))
        return c.fetchone()
    
    def get_top_languages(self, user_id, limit=3):
        c = self.conn.cursor()
        c.execute('SELECT lang_code, count FROM language_usage WHERE user_id = ? ORDER BY count DESC LIMIT ?', (user_id, limit))
        return c.fetchall()
    
    def get_user_history(self, user_id, key, limit=5):
        c = self.conn.cursor()
        c.execute('SELECT encrypted_source, encrypted_target, source_lang, target_lang, timestamp FROM translations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?', (user_id, limit))
        return [(self.decrypt(r[0], key), self.decrypt(r[1], key), r[2], r[3], r[4]) for r in c.fetchall()]
    
    def get_random_word_for_game(self, user_id, key):
        c = self.conn.cursor()
        c.execute('SELECT id, encrypted_source, encrypted_target, source_lang, target_lang FROM translations WHERE user_id = ? ORDER BY RANDOM() LIMIT 1', (user_id,))
        r = c.fetchone()
        return (r[0], self.decrypt(r[1], key), self.decrypt(r[2], key), r[3], r[4]) if r else None
    
    def update_game_progress(self, user_id, word_id, correct):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO game_progress (user_id, word_id) VALUES (?, ?)', (user_id, word_id))
        field = 'correct_answers' if correct else 'wrong_answers'
        c.execute(f'UPDATE game_progress SET {field} = {field} + 1, last_played = ? WHERE user_id = ? AND word_id = ?', (datetime.now(), user_id, word_id))
        self.conn.commit()
    
    def get_user_theme(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT theme FROM user_stats WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        return r[0] if r else 'light'
    
    def set_user_theme(self, user_id, theme):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)', (user_id,))
        c.execute('UPDATE user_stats SET theme = ? WHERE user_id = ?', (theme, user_id))
        self.conn.commit()
    
    def reset_user(self, user_id):
        c = self.conn.cursor()
        for table in ['translations', 'user_stats', 'language_usage', 'game_progress', 'admin_chat', 'users']:
            c.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
        self.conn.commit()
    
    def save_device_info(self, user_id, device_info):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, secret_key) VALUES (?, ?)', (user_id, self.generate_secret_key()))
        c.execute('UPDATE users SET device_info = ?, consent_given = 1 WHERE user_id = ?', (device_info, user_id))
        self.conn.commit()
    
    def has_consent(self, user_id):
        c = self.conn.cursor()
        c.execute('SELECT consent_given FROM users WHERE user_id = ?', (user_id,))
        r = c.fetchone()
        return r and r[0] == 1
    
    def set_chat_consent(self, user_id, consent):
        c = self.conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, secret_key) VALUES (?, ?)', (user_id, self.generate_secret_key()))
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
        c.execute('SELECT user_id, device_info FROM users WHERE chat_consent = 1')
        return c.fetchall()
    
    def get_all_user_ids(self):
        c = self.conn.cursor()
        c.execute('SELECT user_id FROM users')
        return [row[0] for row in c.fetchall()]
    
    def get_admin_dashboard(self):
        c = self.conn.cursor()
        c.execute('SELECT COUNT(*) FROM users'); total_users = c.fetchone()[0]
        c.execute('SELECT SUM(total_translations) FROM user_stats'); total_trans = c.fetchone()[0] or 0
        c.execute('SELECT SUM(total_words) FROM user_stats'); total_words = c.fetchone()[0] or 0
        c.execute('SELECT COUNT(*) FROM users WHERE chat_consent = 1'); chat_users = c.fetchone()[0]
        c.execute('SELECT lang_code, SUM(count) FROM language_usage GROUP BY lang_code ORDER BY SUM(count) DESC LIMIT 5'); top_langs = c.fetchall()
        return {'total_users': total_users, 'total_translations': total_trans, 'total_words': total_words, 'chat_consent_users': chat_users, 'top_languages': top_langs}

db = SecureDatabase()
LANGUAGES = {'fa': '🇮🇷 فارسی', 'en': '🇬🇧 انگلیسی', 'ar': '🇸🇦 عربی', 'fr': '🇫🇷 فرانسوی', 'de': '🇩🇪 آلمانی', 'es': '🇪🇸 اسپانیایی', 'tr': '🇹🇷 ترکی', 'ru': '🇷🇺 روسی'}

class RateLimiter:
    def __init__(self): self.user_requests = defaultdict(list)
    def is_allowed(self, user_id):
        now = time.time()
        self.user_requests[user_id] = [t for t in self.user_requests[user_id] if now - t < 60]
        if len(self.user_requests[user_id]) >= Config.RATE_LIMIT: return False
        self.user_requests[user_id].append(now)
        return True

rate_limiter = RateLimiter()
THEMES = {'light': {'header': '🌞', 'success': '✅', 'error': '❌'}, 'dark': {'header': '🌙', 'success': '✓', 'error': '✗'}}

# ==========================================================
# 🤖 هندلرهای ربات
# ==========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    secret_key = db.get_or_create_user_key(user_id)
    theme = THEMES[db.get_user_theme(user_id)]
    
    if 'secret_shown' not in context.user_data:
        await update.message.reply_text(f"🔐 **امانت امنیتی شما**\n\nکلید منحصر به فرد:\n\n`{secret_key}`\n\n⚠️ بدون این کلید، هیچ‌کس (حتی ادمین) به ترجمه‌های شما دسترسی ندارد.", parse_mode='Markdown')
        context.user_data['secret_shown'] = True
    
    if not db.has_consent(user_id):
        keyboard = [[InlineKeyboardButton("✅ موافقم", callback_data='consent_device')], [InlineKeyboardButton("❌ رد می‌کنم", callback_data='decline_device')]]
        await update.message.reply_text("📱 **حریم خصوصی**\nذخیره اطلاعات اولیه (نام، یوزرنیم) برای بهبود خدمات.\nآیا موافقت می‌کنید؟", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    keyboard = [
        [InlineKeyboardButton("🌐 ترجمه متن", callback_data='translate_text'), InlineKeyboardButton("🖼️ ترجمه عکس", callback_data='translate_photo_info')],
        [InlineKeyboardButton("🎤 ترجمه صوتی", callback_data='translate_voice_info'), InlineKeyboardButton("💬 مکالمه دوزبانه", callback_data='set_conv_mode')],
        [InlineKeyboardButton("📄 ترجمه فایل", callback_data='translate_file_info'), InlineKeyboardButton("🎮 بازی", callback_data='start_game')],
        [InlineKeyboardButton("📊 آمار", callback_data='my_stats'), InlineKeyboardButton("📜 تاریخچه", callback_data='my_history')],
        [InlineKeyboardButton("🔑 کلید امانت", callback_data='show_key'), InlineKeyboardButton("💬 چت ادمین", callback_data='admin_chat_menu')],
        [InlineKeyboardButton(f"{theme['header']} تغییر تم", callback_data='toggle_theme'), InlineKeyboardButton("⚠️ ریست", callback_data='confirm_reset')]
    ]
    await update.message.reply_text(f"{theme['header']} سلام! به ربات مترجم امن خوش اومدی.", reply_markup=InlineKeyboardMarkup(keyboard))

async def consent_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user = update.effective_user
    db.save_device_info(user.id, f"نام: {user.first_name} | @{user.username or 'ندارد'}")
    await update.callback_query.edit_message_text("✅ ذخیره شد.\n\n/start")

async def decline_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("❌ بدون ذخیره اطلاعات ادامه می‌دهیم.\n\n/start")

async def show_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(f"🔑 **کلید:**\n\n`{db.get_or_create_user_key(update.effective_user.id)}`", parse_mode='Markdown')

async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton("✅ بله", callback_data='do_reset')], [InlineKeyboardButton("❌ انصراف", callback_data='back_to_menu')]]
    await update.callback_query.edit_message_text("⚠️ همه چیز پاک و کلید جدید صادر می‌شود.", reply_markup=InlineKeyboardMarkup(kb))

async def do_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    db.reset_user(user_id)
    new_key = db.get_or_create_user_key(user_id)
    context.user_data['secret_shown'] = True
    await update.callback_query.edit_message_text(f"✅ پاک شد.\n\n🔐 **کلید جدید:**\n\n`{new_key}`", parse_mode='Markdown')

async def admin_chat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    if not db.has_chat_consent(user_id):
        kb = [[InlineKeyboardButton("✅ اجازه", callback_data='consent_chat')], [InlineKeyboardButton("❌ انصراف", callback_data='back_to_menu')]]
        await update.callback_query.edit_message_text("💬 اجازه چت با ادمین؟", reply_markup=InlineKeyboardMarkup(kb))
        return
    history = db.get_chat_history(user_id, 10)
    msg = "💬 **تاریخچه:**\n\n" + "\n".join([f"{'👤' if s=='user' else '🛡️'} {m}" for m, s, t in history]) if history else "💬 پیامی نیست. پیام بفرستید:"
    context.user_data['action'] = 'chatting_with_admin'
    await update.callback_query.edit_message_text(msg, parse_mode='Markdown')

async def consent_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    db.set_chat_consent(update.effective_user.id, True)
    await update.callback_query.edit_message_text("✅ اجازه داده شد.\n\n/start")

async def show_langs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    kb = [[InlineKeyboardButton(name, callback_data=f'lang_{code}')] for code, name in LANGUAGES.items()]
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_menu')])
    await update.callback_query.edit_message_text("🎯 زبان مقصد:", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['action'] = 'select_language'

async def handle_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['target_lang'] = update.callback_query.data.split('_')[1]
    context.user_data['action'] = 'waiting_for_text'
    await update.callback_query.edit_message_text(f"✅ {LANGUAGES[context.user_data['target_lang']]} انتخاب شد.\nمتن را بفرستید:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if context.user_data.get('action') == 'chatting_with_admin':
        if db.has_chat_consent(user_id):
            db.save_chat_message(user_id, update.message.text, "user")
            await update.message.reply_text("✅ به ادمین ارسال شد.")
        context.user_data['action'] = None
        return

    if context.user_data.get('conv_mode'):
        is_fa = any('\u0600' <= c <= '\u06FF' for c in update.message.text)
        target = 'en' if is_fa else 'fa'
        try:
            translated = GoogleTranslator(source='auto', target=target).translate(update.message.text)
            kb = [[InlineKeyboardButton("🔄 خروج", callback_data='exit_conv')]]
            await update.message.reply_text(f"🔄 {translated}", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
        return

    if context.user_data.get('action') == 'playing_game':
        await check_game_answer(update, context)
        return

    if context.user_data.get('action') != 'waiting_for_text':
        await update.message.reply_text("لطفاً اول یک گزینه از منو انتخاب کن! /start")
        return
    
    text = update.message.text.strip()
    if len(text) > Config.MAX_TEXT_LENGTH:
        await update.message.reply_text(f"⚠️ متن طولانی است!")
        return
    
    if not rate_limiter.is_allowed(user_id):
        await update.message.reply_text("⚠️ ۱ دقیقه صبر کن.")
        return

    target_lang = context.user_data.get('target_lang', 'fa')
    theme = THEMES[db.get_user_theme(user_id)]
    try:
        translator = GoogleTranslator(source='auto', target=target_lang)
        translated_text = translator.translate(text)
        db.save_translation(user_id, text, translated_text, translator._source, target_lang)
        kb = [[InlineKeyboardButton("🔊 تلفظ", callback_data=f'audio_{translated_text[:100]}')], [InlineKeyboardButton("🔄 جدید", callback_data='translate_text')]]
        await update.message.reply_text(f"{theme['success']} ترجمه شد.\n\n🔤 {translator._source} → {LANGUAGES.get(target_lang)}\n\n📝 {translated_text}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"{theme['error']} خطا: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    await update.message.reply_text("🖼️ در حال خواندن...")
    try:
        file = await photo.get_file()
        path = f"temp_img_{user_id}.jpg"
        await file.download_to_drive(path)
        text = pytesseract.image_to_string(Image.open(path), lang='fas+eng')
        os.remove(path)
        if not text.strip():
            await update.message.reply_text("⚠️ متنی پیدا نشد.")
            return
        target_lang = context.user_data.get('target_lang', 'fa')
        translated = GoogleTranslator(source='auto', target=target_lang).translate(text)
        db.save_translation(user_id, "Image", translated, "image", target_lang)
        await update.message.reply_text(f"✅ {translated}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("🎤 در حال پردازش...")
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
                await update.message.reply_text("⚠️ صدا تشخیص داده نشد.")
                os.remove(ogg_path); os.remove(wav_path)
                return
        os.remove(ogg_path); os.remove(wav_path)
        target_lang = context.user_data.get('target_lang', 'fa')
        translated = GoogleTranslator(source='auto', target=target_lang).translate(text)
        db.save_translation(user_id, "Voice", translated, "voice", target_lang)
        await update.message.reply_text(f"✅ {translated}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > Config.MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ حجم زیاد است!")
        return
    ext = doc.file_name.split('.')[-1].lower()
    if ext not in Config.ALLOWED_FILE_TYPES:
        await update.message.reply_text("⚠️ فقط PDF, DOCX, TXT.")
        return
    await update.message.reply_text("📄 در حال پردازش...")
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
        if len(text) > Config.MAX_TEXT_LENGTH: text = text[:Config.MAX_TEXT_LENGTH]
        target_lang = context.user_data.get('target_lang', 'fa')
        translated = GoogleTranslator(source='auto', target=target_lang).translate(text)
        db.save_translation(update.effective_user.id, "File", translated, "file", target_lang)
        with open("translated.txt", "w", encoding="utf-8") as f: f.write(translated)
        await update.message.reply_document(document=open("translated.txt", "rb"), caption="✅ ترجمه شد!")
        os.remove(path); os.remove("translated.txt")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    word = db.get_random_word_for_game(user_id, db.get_or_create_user_key(user_id))
    if not word:
        await update.callback_query.edit_message_text("🎮 اول ترجمه کن!")
        return
    context.user_data['game_word'] = word
    context.user_data['action'] = 'playing_game'
    await update.callback_query.edit_message_text(f"🎮 ترجمه کن:\n\n📝 {word[1]}")

async def check_game_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = context.user_data.get('game_word')
    user_ans = update.message.text.strip().lower()
    correct_ans = word[2].strip().lower()
    if user_ans == correct_ans or user_ans in correct_ans or correct_ans in user_ans:
        db.update_game_progress(update.effective_user.id, word[0], True)
        await update.message.reply_text(f"🎉 درسته:\n{word[2]}")
    else:
        db.update_game_progress(update.effective_user.id, word[0], False)
        await update.message.reply_text(f"❌ جواب: {word[2]}")
    context.user_data['action'] = None

async def set_conv_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['conv_mode'] = True
    await update.callback_query.edit_message_text("💬 حالت دوزبانه فعال شد.\nبرای خروج /start بزنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 خروج", callback_data='exit_conv')]]))

async def exit_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['conv_mode'] = False
    await update.callback_query.edit_message_text("✅ خارج شدید.\n\n/start")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    stats = db.get_user_stats(update.effective_user.id)
    top_langs = db.get_top_languages(update.effective_user.id, 3)
    msg = f"📊 آمار:\n🔢 ترجمه‌ها: {stats[0] if stats else 0}\n📝 کلمات: {stats[1] if stats else 0}\n"
    if top_langs: msg += "\n🏆 زبان‌ها:\n" + "\n".join([f"• {LANGUAGES.get(l[0], l[0])}: {l[1]}" for l in top_langs])
    await update.callback_query.edit_message_text(msg)

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    history = db.get_user_history(user_id, db.get_or_create_user_key(user_id), 5)
    if history:
        msg = "📜 ۵ آخر:\n\n" + "\n".join([f"{i}. {h[0][:20]}... → {h[1][:20]}..." for i, h in enumerate(history, 1)])
        await update.callback_query.edit_message_text(msg)
    else:
        await update.callback_query.edit_message_text("تاریخچه‌ای نداری!")

async def toggle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    user_id = update.effective_user.id
    new_theme = 'dark' if db.get_user_theme(user_id) == 'light' else 'light'
    db.set_user_theme(user_id, new_theme)
    await update.callback_query.edit_message_text(f"تم به {new_theme} تغییر کرد! /start")

async def generate_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    text = update.callback_query.data.split('_', 1)[1]
    try:
        path = f"audio_{update.effective_user.id}.mp3"
        gTTS(text=text, lang=context.user_data.get('target_lang', 'fa')).save(path)
        await update.callback_query.message.reply_audio(audio=open(path, 'rb'))
        os.remove(path)
    except Exception as e:
        await update.callback_query.message.reply_text(f"❌ خطا: {e}")

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['action'] = None
    context.user_data['conv_mode'] = False
    await start(update, context)

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("⛔ غیرمجاز!")
        return
    d = db.get_admin_dashboard()
    msg = f"🛡️ **داشبورد**\n\n👥 کاربران: {d['total_users']}\n📝 ترجمه‌ها: {d['total_translations']}\n📚 کلمات: {d['total_words']}\n💬 چت: {d['chat_consent_users']}\n\n"
    if d['top_languages']: msg += "🏆 زبان‌ها:\n" + "\n".join([f"• {LANGUAGES.get(l[0], l[0])}: {l[1]}" for l in d['top_languages']])
    await update.message.reply_text(msg, parse_mode='Markdown')

async def admin_list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("⛔ غیرمجاز!")
        return
    users = db.get_users_with_chat_consent()
    if not users:
        await update.message.reply_text("هیچ کاربری اجازه نداده.")
        return
    msg = "💬 کاربران:\n\n"
    kb = [[InlineKeyboardButton(f"💬 چت با {uid}", callback_data=f'admin_chat_{uid}')] for uid, _ in users[:10]]
    await update.message.reply_text(msg + "\n".join([f"• کاربر {uid}" for uid, _ in users[:10]]), reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_chat_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    target_user_id = int(update.callback_query.data.split('_')[2])
    context.user_data['admin_chat_target'] = target_user_id
    context.user_data['action'] = 'admin_chatting'
    history = db.get_chat_history(target_user_id, 10)
    msg = f"💬 چت با {target_user_id}:\n\n" + "\n".join([f"{'👤' if s=='user' else '🛡️'} {m}" for m, s, t in history]) if history else f"💬 چت با {target_user_id}:\n\nپیام بفرستید:"
    await update.callback_query.edit_message_text(msg)

async def admin_send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS: return
    if context.user_data.get('action') == 'admin_chatting':
        target = context.user_data.get('admin_chat_target')
        if target:
            db.save_chat_message(target, update.message.text, "admin")
            await update.message.reply_text(f"✅ به کاربر {target} ارسال شد.")
            context.user_data['action'] = None

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in Config.ADMIN_IDS:
        await update.message.reply_text("⛔ غیرمجاز!")
        return
    text = update.message.text.replace('/broadcast', '').strip()
    if not text:
        await update.message.reply_text("⚠️ مثال: `/broadcast سلام`", parse_mode='Markdown')
        return
    await update.message.reply_text("⏳ در حال ارسال...")
    users = db.get_all_user_ids()
    success, fail = 0, 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            success += 1
        except Exception:
            fail += 1
        time.sleep(0.05)
    await update.message.reply_text(f"✅ کل: {len(users)} | موفق: {success} | ناموفق: {fail}")

# ==========================================================
# ▶️ اجرای اصلی (فقط Polling - بدون Webhook)
# ==========================================================
def main():
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CommandHandler("list_chats", admin_list_chats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    
    app.add_handler(CallbackQueryHandler(show_langs, pattern='^translate_text$'))
    app.add_handler(CallbackQueryHandler(handle_lang, pattern='^lang_'))
    app.add_handler(CallbackQueryHandler(start_game, pattern='^start_game$'))
    app.add_handler(CallbackQueryHandler(show_stats, pattern='^my_stats$'))
    app.add_handler(CallbackQueryHandler(show_history, pattern='^my_history$'))
    app.add_handler(CallbackQueryHandler(toggle_theme, pattern='^toggle_theme$'))
    app.add_handler(CallbackQueryHandler(generate_audio, pattern='^audio_'))
    app.add_handler(CallbackQueryHandler(show_key, pattern='^show_key$'))
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern='^confirm_reset$'))
    app.add_handler(CallbackQueryHandler(do_reset, pattern='^do_reset$'))
    app.add_handler(CallbackQueryHandler(consent_device, pattern='^consent_device$'))
    app.add_handler(CallbackQueryHandler(decline_device, pattern='^decline_device$'))
    app.add_handler(CallbackQueryHandler(admin_chat_menu, pattern='^admin_chat_menu$'))
    app.add_handler(CallbackQueryHandler(consent_chat, pattern='^consent_chat$'))
    app.add_handler(CallbackQueryHandler(admin_chat_user, pattern='^admin_chat_'))
    app.add_handler(CallbackQueryHandler(set_conv_mode, pattern='^set_conv_mode$'))
    app.add_handler(CallbackQueryHandler(exit_conv, pattern='^exit_conv$'))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.edit_message_text("🖼️ عکس بفرستید."), pattern='^translate_photo_info$'))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.edit_message_text("🎤 ویس بفرستید."), pattern='^translate_voice_info$'))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.edit_message_text("📄 فایل بفرستید."), pattern='^translate_file_info$'))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    print("✅ ربات با Polling فعال شد! (در حال گوش دادن به پیام‌ها...)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()