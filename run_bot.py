import os
import psycopg2
import threading
import telebot
from fastapi import FastAPI
from telebot import types
import uvicorn
from datetime import datetime
import pandas as pd

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8960832925:AAGGGnVKOJgQ6lFxy4xkcDnBTRpH7C99mVI"

# Список Telegram ID администраторов (только они могут запрашивать отчеты и очищать базу)
ADMIN_IDS = [5387945787] # Можете добавить через запятую другие ID администраторов

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

bot = telebot.TeleBot(BOT_TOKEN)

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Таблица пользователей для связи ID -> Фамилия Имя
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            status TEXT DEFAULT 'approved'
        )
    """)
    # Таблица смен (в PostgreSQL автоинкремент делается через SERIAL)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            full_name TEXT,
            action_type TEXT,
            time_str TEXT,
            date_str TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПОЛУЧЕНИЯ ИМЕНИ ИЗ БАЗЫ ---
def get_user_full_name(user_id, default_username=None, default_firstname=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT full_name FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0]:
        return row[0]
    
    # Если в базе нет, возвращаем ник или имя для уведомления админу
    return default_username or default_firstname or f"ID {user_id}"

# --- ОБРАБОТКА НАЖАТИЯ КНОПОК ВХОД / ВЫХОД В ЧАТЕ ---

@bot.callback_query_handler(func=lambda call: call.data.startswith("action_"))
def process_shift_action(call):
    user = call.from_user
    user_id = user.id
    
    # Получаем Фамилию и Имя из нашей базы привязок
    full_name = get_user_full_name(user_id, user.username, user.first_name)

    data_parts = call.data.split("_")
    action_type = "Вход" if data_parts[1] == "in" else "Выход"
    recorded_time = data_parts[2]
    current_date = datetime.now().strftime("%d/%m/%Y")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO shifts (user_id, full_name, action_type, time_str, date_str) VALUES (%s, %s, %s, %s, %s)",
                   (user_id, full_name, action_type, recorded_time, current_date))
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, f"Успешно сохранено: {action_type} на {recorded_time}")
    
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"👤 **{full_name}**\n✅ Зафиксировано: **{action_type}** на время **{recorded_time}** ({current_date})",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Не удалось обновить сообщение: {e}")

# --- КОМАНДА ОТЧЕТА (Сводная таблица с русскими заголовками) ---

@bot.message_handler(commands=["report", "отчет"])
def handle_report(message):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date_str, full_name, action_type, time_str FROM shifts ORDER BY date_str ASC, timestamp ASC")
    data = cursor.fetchall()
    conn.close()

    if not data:
        bot.reply_to(message, "📂 База данных смен пока пуста.")
        return

    records_dict = {}

    for row in data:
        date_str, full_name, action_type, time_str = row
        key = (date_str, full_name)
        
        if key not in records_dict:
            records_dict[key] = {"Вход": "Не указан", "Выход": "В работе..."}
        
        if action_type == "Вход":
            records_dict[key]["Вход"] = time_str
        elif action_type == "Выход":
            records_dict[key]["Выход"] = time_str

    formatted_data = []
    for idx, ((date_str, full_name), times) in enumerate(records_dict.items(), start=1):
        formatted_data.append({
            "No": idx,
            "Дата": date_str,
            "Фамилия Имя": full_name,
            "Вход": times["Вход"],
            "Выход": times["Выход"]
        })

    df = pd.DataFrame(formatted_data)
    file_path = "attendance_report.xlsx"

    with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Sheet1')
        worksheet = writer.sheets['Sheet1']
        
        for col in worksheet.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            worksheet.column_dimensions[col_letter].width = max(max_len + 4, 12)

    with open(file_path, "rb") as f:
        bot.send_document(message.chat.id, f, caption="📊 Сводный отчет по рабочему времени")

# --- КОМАНДА ОЧИСТКИ БАЗЫ ДАННЫХ ---

@bot.message_handler(commands=["clear", "очистить"])
def handle_clear(message):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM shifts")
    conn.commit()
    conn.close()

    bot.reply_to(message, "🗑 База данных успешно очищена!")

# --- КОНТРОЛЬ ВСЕХ СООБЩЕНИЙ ---

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    chat_type = message.chat.type

    # Обработка личных сообщений (привязка ID администратором)
    if chat_type == "private":
        if user_id in ADMIN_IDS:
            text = message.text.strip() if message.text else ""
            # Если админ отправляет текст вида: "123456789 Иван Иванов"
            if " " in text:
                parts = text.split(" ", 1)
                if parts[0].isdigit():
                    target_id = int(parts[0])
                    target_name = parts[1].strip()
                    
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO users (user_id, full_name, status) 
                        VALUES (%s, %s, 'approved')
                        ON CONFLICT (user_id) DO UPDATE SET full_name = %s
                    """, (target_id, target_name, target_name))
                    conn.commit()
                    conn.close()
                    
                    bot.reply_to(message, f"✅ Успешно! ID `{target_id}` теперь привязан как **{target_name}**.", parse_mode="Markdown")
                    return

            bot.reply_to(
                message, 
                "Приветствую, Администратор! 🛡\n\n"
                "• Чтобы привязать имя к ID, отправьте в ЛС в формате:\n`ID Фамилия Имя` (например: `5387945787 Александр Петрухин`)\n\n"
                "Команды:\n• `/report` — получить отчет\n• `/clear` — очистить базу", 
                parse_mode="Markdown"
            )
        else:
            try:
                bot.reply_to(message, "⛔️ Личные сообщения с ботом отключены.")
            except:
                pass
        return

    # Обработка рабочей группы
    if chat_type in ["group", "supergroup"]:
        if message.text and message.text.startswith("/"):
            return

        text = message.text.strip() if message.text else ""
        
        # Проверяем, есть ли пользователь в базе сотрудников
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT full_name FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        conn.close()

        # Если пользователя нет в базе — отправляем админам уведомление с его ID
        if not row:
            username_info = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
            for admin_id in ADMIN_IDS:
                try:
                    bot.send_message(
                        admin_id,
                        f"⚠️ **Новый пользователь в группе!**\n\n"
                        f"Имя в Telegram: {message.from_user.first_name} {message.from_user.last_name or ''}\n"
                        f"Юзернейм: {username_info}\n"
                        f"ID: `{user_id}`\n\n"
                        f"Отправьте мне в ЛС сообщение в формате:\n`{user_id} Фамилия Имя`",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Не удалось отправить уведомление админу: {e}")
            
            # Сохраняем временную запись со статусом "pending" (синтаксис ON CONFLICT для PostgreSQL)
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, full_name, username, status) 
                VALUES (%s, %s, %s, 'pending')
                ON CONFLICT (user_id) DO NOTHING
            """, (user_id, username_info, message.from_user.username))
            conn.commit()
            conn.close()

        full_name = get_user_full_name(user_id, message.from_user.username, message.from_user.first_name)

        is_time_format = False
        if ":" in text and len(text) <= 5:
            parts = text.split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                is_time_format = True

        if is_time_format:
            markup = types.InlineKeyboardMarkup()
            btn_in = types.InlineKeyboardButton("🟢 Вход", callback_data=f"action_in_{text}")
            btn_out = types.InlineKeyboardButton("🔴 Выход", callback_data=f"action_out_{text}")
            markup.add(btn_in, btn_out)

            bot.reply_to(
                message, 
                f"⏱ Сотрудник: **{full_name}**\nУказанное время: `{text}`\nВыберите действие:", 
                parse_mode="Markdown", 
                reply_markup=markup
            )
        else:
            try:
                bot.delete_message(message.chat.id, message.message_id)
                warning = bot.send_message(
                    message.chat.id, 
                    f"⚠️ @{message.from_user.username or message.from_user.first_name}, в этом чате разрешено отправлять **только время** (например, `12:00`).", 
                    parse_mode="Markdown"
                )
                threading.Timer(4.0, lambda: bot.delete_message(message.chat.id, warning.message_id)).start()
            except Exception as e:
                print(f"Не удалось удалить сообщение: {e}")

# --- ВЕБ-СЕРВЕР ---

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "Bot is running 24/7"}

def run_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=10000)

def run_bot():
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    print("Запуск веб-сервера...")
    web_thread = threading.Thread(target=run_fastapi)
    web_thread.daemon = True
    web_thread.start()

    print("Запуск Telegram-бота...")
    run_bot()
