import os
import logging
import sqlite3
import random
import json
import asyncio
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    filters, ConversationHandler, CallbackQueryHandler, PicklePersistence, JobQueue
)

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8114895932:AAHlwDcEi0dFMvMgGa3nVsGdcZWr2GfDoTY" 
GOOGLE_API_KEY = "AIzaSyDiD0wnoHjxpydqsuqPrYg8nM9iUEsg5s4"
ADMIN_ID = 7572645545
TIMEZONE = "Europe/Moscow"
DB_FILE = "relationships.db"
PERSISTENCE_FILE = "bot_persistence.pickle"
MAIN_MENU_IMAGE_URL = "https://i.pinimg.com/originals/e7/e4/26/e7e426315c1e935478438198953579e0.jpg"
USE_AI_ANALYSIS = True

# --- ИНИЦИАЛИЗАЦИЯ ---
try:
    if GOOGLE_API_KEY and GOOGLE_API_KEY != "YOUR_GOOGLE_AI_API_KEY":
        genai.configure(api_key=GOOGLE_API_KEY)
        ai_model = genai.GenerativeModel('gemini-1.5-flash')
    else:
        ai_model = None
except Exception as e:
    ai_model = None
    logging.error(f"Не удалось инициализировать Google AI, возможно, неверный ключ: {e}")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO, filename="bot.log")
logger = logging.getLogger(__name__)

# --- СОСТОЯНИЯ ДЛЯ ДИАЛОГОВ ---
(
    STATE_WAITING_CODE, STATE_ANSWERING_DEEP_QUESTION,
    STATE_CHOOSE_TEST, STATE_TAKING_TEST, STATE_BREAKUP_CONFIRMATION,
    STATE_ADMIN_MENU, STATE_ADMIN_BROADCAST_MESSAGE, 
    STATE_COACH_AWAITING_PROBLEM, STATE_ADMIN_VIEW_COACH
) = range(9)

# --- ДАННЫЕ (Вопросы, Советы, Статьи) ---
QUESTIONS = ["Что для тебя значит 'идеальные отношения'?", "Как ты справляешься с конфликтами в отношениях?", "Что тебя больше всего пугает в близости?"]
TIPS_OF_THE_DAY = ["Лучший способ сохранить любовь — не переставать её дарить.", "Счастье в отношениях — это не когда на вас смотрят, а когда вы оба смотрите в одном направлении."]
ARTICLES = [
    {"title": "Искусство слушать", "text": "Часто в споре мы не слушаем, чтобы понять, а слушаем, чтобы ответить..."},
    {"title": "Пять языков любви", "text": "Знаете ли вы, на каком 'языке любви' говорите вы и ваш партнер? ..."}
]

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS relationships (id TEXT PRIMARY KEY, user1_id INTEGER, user2_id INTEGER, status TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_seen TIMESTAMP)")
    cursor.execute("CREATE TABLE IF NOT EXISTS answers (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, question TEXT, answer TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS tests (id INTEGER PRIMARY KEY AUTOINCREMENT, test_name TEXT NOT NULL, description TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS test_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER, question_text TEXT NOT NULL, options_json TEXT NOT NULL, FOREIGN KEY (test_id) REFERENCES tests (id))")
    cursor.execute("CREATE TABLE IF NOT EXISTS user_test_answers (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, partner_id INTEGER NOT NULL, test_id INTEGER NOT NULL, question_id INTEGER NOT NULL, chosen_option_key TEXT NOT NULL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS coach_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, problem_text TEXT, advice_text TEXT)")
    conn.commit()
    conn.close()
    logger.info("База данных успешно инициализирована.")

def populate_tests_if_empty():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM tests")
    if cursor.fetchone()[0] == 0:
        logger.info("Таблица тестов пуста. Заполняем данными...")
        # Сюда можно вставить ваш большой список all_tests
    conn.commit()
    conn.close()

def create_relationship_in_db(user_id):
    conn = get_db_connection()
    rel_id = f"{user_id}{datetime.now().strftime('%f')}"[-10:]
    conn.execute("INSERT INTO relationships (id, user1_id, status) VALUES (?, ?, 'pending')", (rel_id, user_id))
    conn.commit()
    conn.close()
    return rel_id

def join_relationship_in_db(rel_id, user2_id):
    conn = get_db_connection()
    result = conn.execute("SELECT user1_id FROM relationships WHERE id = ? AND status = 'pending' AND user2_id IS NULL", (rel_id,)).fetchone()
    if result and result['user1_id'] != user2_id:
        conn.execute("UPDATE relationships SET user2_id = ?, status = 'active' WHERE id = ?", (user2_id, rel_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_partner_id(user_id):
    conn = get_db_connection()
    res = conn.execute("SELECT user2_id FROM relationships WHERE user1_id = ? AND status = 'active'", (user_id,)).fetchone()
    if res: conn.close(); return res['user2_id']
    res = conn.execute("SELECT user1_id FROM relationships WHERE user2_id = ? AND status = 'active'", (user_id,)).fetchone()
    conn.close()
    return res['user1_id'] if res else None

def get_creator_id_by_rel_id(rel_id: str):
    conn = get_db_connection()
    result = conn.execute("SELECT user1_id FROM relationships WHERE id = ?", (rel_id,)).fetchone()
    conn.close()
    return result['user1_id'] if result else None

def delete_relationship(user_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM relationships WHERE user1_id = ? OR user2_id = ?", (user_id, user_id))
    conn.commit()
    conn.close()
    logger.info(f"Отношения для пользователя {user_id} разорваны.")

def update_user_activity(user: Update.effective_user):
    conn = get_db_connection()
    conn.execute("INSERT INTO users (user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen", (user.id, user.username, user.first_name, datetime.now()))
    conn.commit()
    conn.close()

# --- ФУНКЦИИ НАПОМИНАНИЙ ---
def remove_all_reminders_for_user(user_id: int, job_queue: JobQueue):
    job_names = [f"reminder_11_{user_id}", f"reminder_15_{user_id}", f"reminder_22_{user_id}"]
    for name in job_names:
        current_jobs = job_queue.get_jobs_by_name(name)
        for job in current_jobs:
            job.schedule_removal()
    logger.info(f"Все напоминания для {user_id} удалены.")

async def reminder_callback_morning(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=context.job.chat_id, text="Доброе утро! ☀️ Найдите минутку, чтобы написать что-то приятное вашей второй половинке и пожелайте хорошего дня!")

async def reminder_callback_afternoon(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=context.job.chat_id, text="День в самом разгаре! Узнайте, как дела у вашего партнера, и отправьте ему/ей немного своей поддержки. ❤️")

async def reminder_callback_evening(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=context.job.chat_id, text="Скоро ночь... не забудьте пожелать сладких снов вашей второй половинке! 🌙")

def schedule_all_reminders_for_user(job_queue: JobQueue, user_id: int):
    remove_all_reminders_for_user(user_id, job_queue)
    job_queue.run_daily(reminder_callback_morning, time=dt_time(hour=11, minute=0, tzinfo=ZoneInfo(TIMEZONE)), chat_id=user_id, name=f"reminder_11_{user_id}")
    job_queue.run_daily(reminder_callback_afternoon, time=dt_time(hour=15, minute=0, tzinfo=ZoneInfo(TIMEZONE)), chat_id=user_id, name=f"reminder_15_{user_id}")
    job_queue.run_daily(reminder_callback_evening, time=dt_time(hour=22, minute=0, tzinfo=ZoneInfo(TIMEZONE)), chat_id=user_id, name=f"reminder_22_{user_id}")
    logger.info(f"Все 3 напоминания запланированы для {user_id}")

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_activity(user)
    args = context.args
    
    is_callback = update.callback_query is not None
    if is_callback:
        await update.callback_query.answer()
        message = update.callback_query.message
    else:
        message = update.message
        
    if args and args[0].startswith("join_"):
        rel_id = args[0].replace("join_", "")
        creator_id = get_creator_id_by_rel_id(rel_id)
        if not creator_id: await message.reply_text("❌ Ссылка-приглашение недействительна."); return
        if creator_id == user.id: await message.reply_text("Вы не можете присоединиться к паре, которую сами создали.")
        elif get_partner_id(user.id): await message.reply_text("Вы уже состоите в паре. Чтобы присоединиться к новой, сначала нужно разорвать текущую связь.")
        else:
            if join_relationship_in_db(rel_id, user.id):
                schedule_all_reminders_for_user(context.application.job_queue, user.id)
                schedule_all_reminders_for_user(context.application.job_queue, creator_id)
                await message.reply_text("🎉 Вы успешно присоединились к паре по ссылке!\nНажмите /start, чтобы начать.")
                await context.bot.send_message(chat_id=creator_id, text=f"🎉 Ваш партнер ({user.first_name}) присоединился по ссылке!")
            else: await message.reply_text("❌ К сожалению, эта ссылка уже недействительна или пара заполнена.")
        return

    partner_id = get_partner_id(user.id)
    caption_text, keyboard = "", None
    
    if partner_id:
        tip = random.choice(TIPS_OF_THE_DAY)
        caption_text = (f"<b>С возвращением, {user.first_name}!</b> ✨\n\n<i><b>Совет дня:</b> {tip}</i>\n\nГотовы исследовать вашу связь глубже сегодня?")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Задать глубокий вопрос 🤔", callback_data='ask_deep_question')],
            [InlineKeyboardButton("Пройти тест на совместимость 🧪", callback_data='start_testing_flow')],
            [InlineKeyboardButton("📝 Статья для обсуждения", callback_data='get_article')],
            [InlineKeyboardButton("💬 Персональный коуч", callback_data='personal_coach_start')],
            [InlineKeyboardButton("Разорвать связь 💔", callback_data='break_up_confirm')]
        ])
    else:
        caption_text = ("<b>Добро пожаловать в LoveBot!</b>\n\nЯ ваш личный помощник и дневник для отношений. Давайте начнем это путешествие к более глубокому пониманию друг друга.")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Создать пару 💍", callback_data='create_pair')], [InlineKeyboardButton("Присоединиться 🔗", callback_data='join_pair')]])

    try:
        if is_callback: await message.delete()
        await context.bot.send_photo(chat_id=user.id, photo=MAIN_MENU_IMAGE_URL, caption=caption_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Не удалось отправить фото-меню: {e}. Отправляю текстовую версию.")
        reply_func = message.edit_text if is_callback and message.text else message.reply_text
        await reply_func(caption_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def article_for_couple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    partner_id = get_partner_id(user_id)
    if not partner_id: await query.answer("Эта функция доступна только для пар.", show_alert=True); return
    article = random.choice(ARTICLES)
    article_text = f"<b>📝 Статья для совместного обсуждения</b>\n\n<b>{article['title']}</b>\n\n{article['text']}\n\n<i>Прочитайте и обсудите это вместе. Какие мысли у вас возникли?</i>"
    await context.bot.send_message(chat_id=user_id, text=article_text, parse_mode=ParseMode.HTML)
    await context.bot.send_message(chat_id=partner_id, text=article_text, parse_mode=ParseMode.HTML)

async def reminders_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner_id = get_partner_id(user_id)
    if partner_id:
        schedule_all_reminders_for_user(context.application.job_queue, user_id)
        schedule_all_reminders_for_user(context.application.job_queue, partner_id)
        await update.message.reply_text("Новое расписание напоминаний (11:00, 15:00, 22:00) успешно включено для вашей пары! ⏰")
    else:
        await update.message.reply_text("Эта команда доступна только для пользователей, состоящих в паре.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Действие отменено. Чтобы начать заново, введите /start.")
    return ConversationHandler.END

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

# --- ДИАЛОГИ ---
async def create_relationship_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    rel_id = create_relationship_in_db(query.from_user.id)
    bot_username = context.bot.username
    invite_link = f"https://t.me/{bot_username}?start=join_{rel_id}"
    message_text = (f"Пара создана! 💍\n\nВаш код для присоединения: `{rel_id}`\n\nИли просто **отправьте вашему партнеру эту ссылку**:\n{invite_link}")
    keyboard = [[InlineKeyboardButton("« Отмена", callback_data='cancel_pairing')]]
    await query.message.reply_text(message_text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_WAITING_CODE

async def join_relationship_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    await query.message.reply_text("Введите 10-значный код, который дал вам партнер. Для отмены введите /cancel.")
    return STATE_WAITING_CODE

async def cancel_pairing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    delete_relationship(query.from_user.id)
    await start(update, context)
    return ConversationHandler.END

async def handle_join_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    code = update.message.text.strip()
    creator_id = get_creator_id_by_rel_id(code)
    if creator_id and creator_id != user_id and not get_partner_id(user_id):
        if join_relationship_in_db(code, user_id):
            schedule_all_reminders_for_user(context.application.job_queue, user_id)
            schedule_all_reminders_for_user(context.application.job_queue, creator_id)
            await update.message.reply_text("🎉 Вы успешно присоединились! Я включил для вас ежедневные напоминания.\n\nЧтобы начать, введите /start.")
            await context.bot.send_message(chat_id=creator_id, text=f"🎉 Ваш партнер ({update.effective_user.first_name}) присоединился! Я также включил для вас напоминания.\n\nЧтобы начать, введите /start.")
            return ConversationHandler.END
    await update.message.reply_text("❌ Код неверный, пара уже создана, или вы уже в других отношениях. Попробуйте снова.")
    return STATE_WAITING_CODE

async def ask_deep_question_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    context.user_data['current_question'] = random.choice(QUESTIONS)
    await query.message.reply_text(f"Ваш вопрос:\n\n*{context.user_data['current_question']}*\n\nНапишите свой ответ. Для отмены введите /cancel.", parse_mode="Markdown")
    return STATE_ANSWERING_DEEP_QUESTION

async def handle_deep_Youtube(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer, question = update.message.text, context.user_data.get('current_question', "Неизвестный вопрос")
    conn = get_db_connection()
    conn.execute("INSERT INTO answers (user_id, question, answer) VALUES (?, ?, ?)", (update.effective_user.id, question, answer)); conn.commit(); conn.close()
    partner_id = get_partner_id(update.effective_user.id)
    await update.message.reply_text("✅ Ответ принят и отправлен партнеру! Чтобы продолжить, нажмите /start.")
    if partner_id:
        await context.bot.send_message(chat_id=partner_id, text=f"💌 Ваш партнер ответил на вопрос:\n\n*_{question}_*\n\nОтвет:\n_{answer}_", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def choose_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    conn = get_db_connection()
    tests = conn.execute("SELECT id, test_name FROM tests").fetchall()
    conn.close()
    if not tests:
        await query.message.reply_text("Пока нет доступных тестов. Чтобы вернуться, нажмите /start.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(test['test_name'], callback_data=f"start_test_{test['id']}")] for test in tests]
    keyboard.append([InlineKeyboardButton("« Назад в главное меню", callback_data='back_to_main_menu')])
    await query.message.reply_text("Выберите тест, который вы хотите пройти вместе:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_CHOOSE_TEST

async def start_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    test_id = int(query.data.split("_")[2])
    conn = get_db_connection()
    questions = conn.execute("SELECT id, question_text, options_json FROM test_questions WHERE test_id = ?", (test_id,)).fetchall()
    conn.close()
    context.user_data.update({'test_id': test_id, 'questions': questions, 'current_q_index': 0, 'user_answers': []})
    await send_next_test_question(update, context)
    return STATE_TAKING_TEST

async def send_next_test_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q_index, questions, question_data = context.user_data['current_q_index'], context.user_data['questions'], context.user_data['questions'][context.user_data['current_q_index']]
    options = json.loads(question_data['options_json'])
    message_text = f"Вопрос {q_index + 1}/{len(questions)}:\n\n*{question_data['question_text']}*\n"
    for key, text in options.items(): message_text += f"\n**{key})** {text}"
    answer_buttons = [InlineKeyboardButton(key, callback_data=f"answer_{question_data['id']}_{key}") for key in options.keys()]
    back_button = InlineKeyboardButton("↩️ Назад к тестам", callback_data="back_to_test_selection")
    keyboard = [answer_buttons, [back_button]]
    await update.callback_query.message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_test_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    _, q_id_str, chosen_key = query.data.split("_")
    q_id = int(q_id_str)
    context.user_data['user_answers'].append({'q_id': q_id, 'key': chosen_key})
    context.user_data['current_q_index'] += 1
    if context.user_data['current_q_index'] < len(context.user_data['questions']):
        await send_next_test_question(update, context)
        return STATE_TAKING_TEST
    else:
        user_id, partner_id, test_id = query.from_user.id, get_partner_id(query.from_user.id), context.user_data['test_id']
        conn = get_db_connection()
        conn.execute("DELETE FROM user_test_answers WHERE test_id = ? AND user_id = ?", (test_id, user_id))
        conn.commit()
        for ans in context.user_data['user_answers']:
            conn.execute("INSERT INTO user_test_answers (user_id, partner_id, test_id, question_id, chosen_option_key) VALUES (?, ?, ?, ?, ?)", (user_id, partner_id, test_id, ans['q_id'], ans['key']))
        conn.commit()
        await query.message.edit_text("Тест завершен! 👍\n\nЯ сообщу вам результаты, как только ваш партнер тоже его пройдет.\nЧтобы вернуться в меню, нажмите /start.")
        completed_users = conn.execute("SELECT DISTINCT user_id FROM user_test_answers WHERE test_id = ? AND (user_id = ? OR user_id = ?)", (test_id, user_id, partner_id)).fetchall()
        conn.close()
        if len(completed_users) == 2:
            asyncio.create_task(calculate_and_send_ai_analysis(test_id, user_id, partner_id, context))
        context.user_data.clear()
        return ConversationHandler.END

async def back_to_test_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await choose_test(update, context)

async def break_up_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    keyboard = [[InlineKeyboardButton("Да, разорвать", callback_data='confirm_break_up_yes')], [InlineKeyboardButton("Нет, я передумал(а)", callback_data='confirm_break_up_no')]]
    await query.message.reply_text(text="Вы уверены, что хотите разорвать связь? Подумайте, может, это ваша судьба...", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_BREAKUP_CONFIRMATION

async def perform_break_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    user_id = query.from_user.id
    partner_id = get_partner_id(user_id)
    if partner_id:
        delete_relationship(user_id)
        remove_all_reminders_for_user(user_id, context.application.job_queue)
        remove_all_reminders_for_user(partner_id, context.application.job_queue)
        await query.message.edit_text("Связь в боте разорвана. Напоминания отключены.\n\nЧтобы начать все с чистого листа, введите /start.")
        await context.bot.send_message(chat_id=partner_id, text="Ваш партнер разорвал связь в боте. Чтобы начать заново, введите /start.")
    else:
        await query.message.edit_text("Вы не состоите в паре. Нажмите /start.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_break_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await start(update, context)
    return ConversationHandler.END

async def personal_coach_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    await query.message.reply_text("Вы обратились к Персональному коучу. Опишите вашу проблему или вопрос как можно подробнее.\n\nДля отмены введите /cancel.")
    return STATE_COACH_AWAITING_PROBLEM

async def handle_coach_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_problem, user_id = update.message.text, update.effective_user.id
    await update.message.reply_text("Спасибо. Анализирую ваш запрос... 🧠 Это может занять до минуты.")
    prompt = (f"Выступи в роли мудрого, опытного и очень эмпатичного психолога по отношениям. Пользователь обратился к тебе с личной проблемой. Вот его запрос: '{user_problem}'.\n\nТвоя задача — дать развернутый, поддерживающий и конструктивный совет. Структурируй свой ответ:\n1. Прояви сочувствие и покажи, что ты понял суть проблемы.\n2. Проанализируй возможные причины ситуации, не обвиняя пользователя или его партнера.\n3. Предложи 2-3 конкретных, практических шага или вопроса для саморефлексии.\n4. Заверши ответ на ободряющей и позитивной ноте.\nОбращайся на 'вы'. Твой тон должен быть максимально человечным и теплым.")
    try:
        if not ai_model: raise Exception("Модель ИИ не инициализирована.")
        response = await ai_model.generate_content_async(prompt)
        ai_advice = response.text
    except Exception as e:
        logger.error(f"Ошибка при обращении к Google AI для коуча: {e}")
        ai_advice = "К сожалению, при обработке вашего запроса произошла ошибка. Пожалуйста, попробуйте позже."
    conn = get_db_connection()
    conn.execute("INSERT INTO coach_requests (user_id, problem_text, advice_text) VALUES (?, ?, ?)", (user_id, user_problem, ai_advice)); conn.commit(); conn.close()
    await update.message.reply_text(ai_advice)
    await update.message.reply_text("Надеюсь, это было полезно. Чтобы вернуться в главное меню, нажмите /start.")
    context.user_data.clear()
    return ConversationHandler.END
    
async def calculate_and_send_ai_analysis(test_id: int, user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE):
    if not USE_AI_ANALYSIS or not ai_model: await context.bot.send_message(chat_id=user1_id, text="Анализ ИИ отключен или не настроен."); return
    await context.bot.send_message(chat_id=user1_id, text="Анализирую ваши ответы... 🧠 Это может занять до минуты.")
    await context.bot.send_message(chat_id=user2_id, text="Анализирую ваши ответы... 🧠 Это может занять до минуты.")
    conn = get_db_connection()
    test_info, questions_data = conn.execute("SELECT test_name FROM tests WHERE id = ?", (test_id,)).fetchone(), conn.execute("SELECT id, question_text, options_json FROM test_questions WHERE test_id = ?", (test_id,)).fetchall()
    user1_answers, user2_answers = {row['question_id']: row['chosen_option_key'] for row in conn.execute("SELECT question_id, chosen_option_key FROM user_test_answers WHERE test_id = ? AND user_id = ?", (test_id, user1_id)).fetchall()}, {row['question_id']: row['chosen_option_key'] for row in conn.execute("SELECT question_id, chosen_option_key FROM user_test_answers WHERE test_id = ? AND user_id = ?", (test_id, user2_id)).fetchall()}
    conn.close()
    prompt = f"Выступи в роли мудрого психолога по отношениям. Пара прошла тест '{test_info['test_name']}'.\n\nИх ответы:\n"
    for q_data in questions_data:
        q_id, options = q_data['id'], json.loads(q_data['options_json'])
        ans1_key, ans2_key = user1_answers.get(q_id), user2_answers.get(q_id)
        if ans1_key and ans2_key: prompt += f"\n- Вопрос: {q_data['question_text']}\n  - Партнер 1: \"{options.get(ans1_key, '-')}\"\n  - Партнер 2: \"{options.get(ans2_key, '-')}\"\n"
    prompt += "\nПроанализируй эти ответы. Твоя задача:\n1. Кратко опиши общую картину.\n2. Выдели 1-2 сильные стороны.\n3. Выдели 1-2 точки роста.\n4. Дай 1-2 конкретных совета.\n5. Заверши анализ позитивным сообщением.\nОбращайся к паре на 'вы'. Тон поддерживающий."
    try:
        response = await ai_model.generate_content_async(prompt)
        ai_result_text = response.text
    except Exception as e:
        logger.error(f"Ошибка при обращении к Google AI: {e}"); ai_result_text = "К сожалению, при анализе произошла ошибка. ❤️"
    header = f"📊 Глубинный анализ ваших ответов по тесту «{test_info['test_name']}»"
    final_message = f"{header}\n\n{ai_result_text}"
    await context.bot.send_message(chat_id=user1_id, text=final_message, parse_mode="Markdown")
    await context.bot.send_message(chat_id=user2_id, text=final_message, parse_mode="Markdown")

# --- АДМИН-ПАНЕЛЬ ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message or update.callback_query.message
    if message.from_user.id != ADMIN_ID:
        await message.reply_text("У вас нет доступа к этой команде.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton("📊 Статистика", callback_data='admin_stats')], [InlineKeyboardButton("📢 Сделать рассылку", callback_data='admin_broadcast_start')], [InlineKeyboardButton("❓ Запросы к коучу", callback_data='admin_coach_req_0')]]
    if update.callback_query:
        await update.callback_query.message.edit_text("Добро пожаловать в админ-панель!", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text("Добро пожаловать в админ-панель!", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_ADMIN_MENU

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    conn = get_db_connection()
    total_users, active_pairs, total_coach_requests = conn.execute("SELECT COUNT(user_id) FROM users").fetchone()[0], conn.execute("SELECT COUNT(*) FROM relationships WHERE status = 'active'").fetchone()[0], conn.execute("SELECT COUNT(*) FROM coach_requests").fetchone()[0]
    conn.close()
    stats_text = (f"<b>Статистика бота:</b>\n\n" f"Всего уникальных пользователей: {total_users}\n" f"Активных пар: {active_pairs}\n" f"Запросов к коучу: {total_coach_requests}")
    keyboard = [[InlineKeyboardButton("« Назад в админку", callback_data='admin_back_to_menu')]]
    await query.message.edit_text(stats_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_ADMIN_MENU

async def broadcast_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.message.delete()
    await query.message.reply_text("Введите сообщение для рассылки. Для отмены введите /cancel.")
    return STATE_ADMIN_BROADCAST_MESSAGE

async def broadcast_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_text = update.message.text
    await update.message.reply_text("Начинаю рассылку...")
    conn = get_db_connection()
    all_user_ids = {row['user_id'] for row in conn.execute("SELECT user_id FROM users").fetchall()}
    conn.close()
    sent_count, failed_count = 0, 0
    for user_id in all_user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
            sent_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            failed_count += 1
    await update.message.reply_text(f"✅ Рассылка завершена!\nОтправлено: {sent_count}\nНе удалось: {failed_count}")
    await admin_menu(update, context)
    return ConversationHandler.END

async def admin_view_coach_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    offset = int(query.data.split('_')[-1])
    limit, conn = 3, get_db_connection()
    requests = conn.execute("SELECT user_id, timestamp, problem_text FROM coach_requests ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    total_requests = conn.execute("SELECT COUNT(*) FROM coach_requests").fetchone()[0]
    conn.close()
    if not requests:
        await query.message.edit_text("Пока не было запросов к Персональному коучу.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад в админку", callback_data='admin_back_to_menu')]]))
        return STATE_ADMIN_VIEW_COACH
    response_text = "<b>Последние запросы к коучу:</b>\n\n"
    for req in requests:
        response_text += (f"<b>Дата:</b> {req['timestamp']}\n<b>User ID:</b> {req['user_id']}\n<b>Проблема:</b> {req['problem_text'][:150]}...\n-------------------\n")
    nav_buttons = []
    if offset > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"admin_coach_req_{offset - limit}"))
    if offset + limit < total_requests: nav_buttons.append(InlineKeyboardButton("След. ➡️", callback_data=f"admin_coach_req_{offset + limit}"))
    keyboard = [nav_buttons, [InlineKeyboardButton("« Назад в админку", callback_data='admin_back_to_menu')]]
    await query.message.edit_text(response_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_ADMIN_VIEW_COACH
    
async def admin_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await admin_menu(update.callback_query, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла внутренняя ошибка. Я уже сообщил о ней администратору. Пожалуйста, попробуйте позже или перезапустите диалог командой /start.")
        except BadRequest:
            logger.error("Не удалось отправить сообщение об ошибке пользователю.")

# --- ЗАПУСК БОТА ---
def main() -> None:
    init_database()
    populate_tests_if_empty()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    
    # Диалоги
    pairing_handler = ConversationHandler(entry_points=[CallbackQueryHandler(create_relationship_handler, pattern="^create_pair$"), CallbackQueryHandler(join_relationship_handler, pattern="^join_pair$")], states={STATE_WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_join_code), CallbackQueryHandler(cancel_pairing, pattern="^cancel_pairing$")]}, fallbacks=[CommandHandler("cancel", cancel)], name="pairing_conv", persistent=True)
    deep_question_handler = ConversationHandler(entry_points=[CallbackQueryHandler(ask_deep_question_entry, pattern="^ask_deep_question$")], states={STATE_ANSWERING_DEEP_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deep_Youtube)]}, fallbacks=[CommandHandler("cancel", cancel)], name="deep_question_conv", persistent=True)
    testing_handler = ConversationHandler(entry_points=[CallbackQueryHandler(choose_test, pattern="^start_testing_flow$")], states={STATE_CHOOSE_TEST: [CallbackQueryHandler(start_test, pattern=r"^start_test_\d+$"), CallbackQueryHandler(back_to_main_menu, pattern="^back_to_main_menu$")], STATE_TAKING_TEST: [CallbackQueryHandler(handle_test_answer, pattern=r"^answer_\d+_\w$"), CallbackQueryHandler(back_to_test_selection, pattern="^back_to_test_selection$")]}, fallbacks=[CommandHandler("cancel", cancel)], name="testing_conv", persistent=True)
    breakup_handler = ConversationHandler(entry_points=[CallbackQueryHandler(break_up_confirmation, pattern="^break_up_confirm$")], states={STATE_BREAKUP_CONFIRMATION: [CallbackQueryHandler(perform_break_up, pattern="^confirm_break_up_yes$"), CallbackQueryHandler(cancel_break_up, pattern="^confirm_break_up_no$")]}, fallbacks=[CommandHandler("cancel", cancel)], name="breakup_conv", persistent=True)
    coach_handler = ConversationHandler(entry_points=[CallbackQueryHandler(personal_coach_start, pattern="^personal_coach_start$")], states={STATE_COACH_AWAITING_PROBLEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coach_problem)]}, fallbacks=[CommandHandler("cancel", cancel)], name="coach_conv", persistent=True)
    admin_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_menu)],
        states={
            STATE_ADMIN_MENU: [CallbackQueryHandler(admin_stats, pattern="^admin_stats$"), CallbackQueryHandler(broadcast_start_handler, pattern="^admin_broadcast_start$"), CallbackQueryHandler(admin_view_coach_requests, pattern=r"^admin_coach_req_\d+$"), CallbackQueryHandler(admin_back_to_menu, pattern="^admin_back_to_menu$")],
            STATE_ADMIN_VIEW_COACH: [CallbackQueryHandler(admin_back_to_menu, pattern="^admin_back_to_menu$"), CallbackQueryHandler(admin_view_coach_requests, pattern=r"^admin_coach_req_\d+$")],
            STATE_ADMIN_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel)], name="admin_conv", persistent=True
    )
    
    application.add_error_handler(error_handler)
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reminders_on", reminders_on))
    application.add_handler(CommandHandler("article", article_for_couple))
    # Диалоги
    application.add_handler(pairing_handler)
    application.add_handler(deep_question_handler)
    application.add_handler(testing_handler)
    application.add_handler(breakup_handler)
    application.add_handler(coach_handler)
    application.add_handler(admin_conv_handler)
    # Отдельные кнопки, не входящие в диалоги
    application.add_handler(CallbackQueryHandler(article_for_couple, pattern="^get_article$"))

    print("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()