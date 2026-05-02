import os
import logging
import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
import google.generativeai as genai

# ─── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Ключи ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
WEATHER_KEY    = os.environ.get("OPENWEATHER_API_KEY", "")
NEWS_KEY       = os.environ.get("NEWS_API_KEY", "")

# ─── Gemini ──────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=(
        "Ты полезный ассистент в Telegram. "
        "Отвечай кратко и по делу. Используй эмодзи там, где уместно. "
        "Если пользователь пишет на русском — отвечай на русском."
    )
)

# ─── Хранилище ───────────────────────────────────────────────────────────────
user_chats: dict = {}
user_notes: dict = {}

# ─── Меню ────────────────────────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🌤 Погода",     "📰 Новости"],
        ["📝 Заметки",    "🌍 Перевод"],
        ["❓ Спросить AI", "ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)

# ════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════════════════

def get_chat(user_id: int):
    if user_id not in user_chats:
        user_chats[user_id] = gemini_model.start_chat(history=[])
    return user_chats[user_id]


async def ask_gemini(user_id: int, text: str) -> str:
    chat = get_chat(user_id)
    response = chat.send_message(text)
    return response.text


async def get_weather(city: str) -> str:
    if not WEATHER_KEY:
        return (
            "⚠️ API-ключ погоды не настроен.\n"
            "Добавь OPENWEATHER_API_KEY в переменные окружения.\n"
            "Бесплатный ключ: https://openweathermap.org/api"
        )
    url = (
        f"https://api.openweathermap.org/data/2.5/weather"
        f"?q={city}&appid={WEATHER_KEY}&units=metric&lang=ru"
    )
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
    if r.status_code != 200:
        return f"❌ Город «{city}» не найден. Попробуй по-английски."
    d = r.json()
    return (
        f"🌤 Погода в {d['name']}\n\n"
        f"🌡 Температура: {round(d['main']['temp'])}°C "
        f"(ощущается {round(d['main']['feels_like'])}°C)\n"
        f"☁️ {d['weather'][0]['description'].capitalize()}\n"
        f"💧 Влажность: {d['main']['humidity']}%\n"
        f"💨 Ветер: {round(d['wind']['speed'])} м/с"
    )


async def get_news(topic: str = "") -> str:
    if not NEWS_KEY:
        return (
            "⚠️ API-ключ новостей не настроен.\n"
            "Добавь NEWS_API_KEY в переменные окружения.\n"
            "Бесплатный ключ: https://newsapi.org"
        )
    query = topic if topic else "Россия"
    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={query}&language=ru&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}"
    )
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
    articles = r.json().get("articles", [])
    if not articles:
        return "📰 Новости не найдены."
    lines = [f"📰 Новости по теме «{query}»:\n"]
    for i, a in enumerate(articles[:5], 1):
        lines.append(f"{i}. {a['title']}\n   🔗 {a['url']}\n")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Обработчики
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Я твой AI-ассистент на базе Gemini.\n"
        "Выбери действие или просто напиши мне!",
        reply_markup=MAIN_MENU,
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Что я умею:\n\n"
        "🌤 *Погода* — «погода Москва»\n"
        "📰 *Новости* — «новости» или «новости спорт»\n"
        "🌍 *Перевод* — «переведи Hello на русский»\n"
        "📝 *Заметки* — «заметка купить молоко» / «мои заметки» / «удали заметки»\n"
        "💬 *Общение* — просто напиши что угодно!\n\n"
        "/start — перезапустить\n"
        "/clear — очистить историю\n"
        "/notes — мои заметки",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def clear_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_chats.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑 История очищена!", reply_markup=MAIN_MENU)


async def show_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    notes = user_notes.get(uid, [])
    if not notes:
        await update.message.reply_text("📝 Заметок пока нет.", reply_markup=MAIN_MENU)
    else:
        text = "📝 Твои заметки:\n\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(notes, 1))
        await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    text  = update.message.text.strip()
    lower = text.lower()

    if text == "ℹ️ Помощь":
        await help_cmd(update, ctx); return
    if text == "📝 Заметки":
        await show_notes(update, ctx); return
    if text == "❓ Спросить AI":
        await update.message.reply_text("💬 Напиши свой вопрос!", reply_markup=MAIN_MENU); return

    if text == "🌤 Погода" or lower.startswith("погода"):
        city = text.replace("🌤 Погода", "").replace("погода", "").strip()
        if not city:
            await update.message.reply_text("🏙 Напиши город: «погода Москва»"); return
        await update.message.reply_text("⏳ Получаю данные...")
        await update.message.reply_text(await get_weather(city), reply_markup=MAIN_MENU); return

    if text == "📰 Новости" or lower.startswith("новости"):
        topic = text.replace("📰 Новости", "").replace("новости", "").strip()
        await update.message.reply_text("⏳ Ищу новости...")
        await update.message.reply_text(await get_news(topic), reply_markup=MAIN_MENU); return

    if text == "🌍 Перевод":
        await update.message.reply_text("✍️ Напиши: «переведи Hello на русский»"); return

    if lower.startswith("заметка "):
        note = text[8:].strip()
        user_notes.setdefault(uid, []).append(note)
        await update.message.reply_text(f"✅ Сохранено: «{note}»", reply_markup=MAIN_MENU); return
    if lower in ("мои заметки", "заметки"):
        await show_notes(update, ctx); return
    if lower in ("удали заметки", "удалить заметки", "очисти заметки"):
        user_notes[uid] = []
        await update.message.reply_text("🗑 Заметки удалены!", reply_markup=MAIN_MENU); return

    # Gemini
    await update.message.reply_text("⏳ Думаю...")
    try:
        reply = await ask_gemini(uid, text)
        await update.message.reply_text(reply, reply_markup=MAIN_MENU)
    except Exception as e:
        logger.error("Gemini error: %s", e)
        await update.message.reply_text("❌ Ошибка. Попробуй ещё раз.", reply_markup=MAIN_MENU)


# ════════════════════════════════════════════════════════════════════════════
# Запуск
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("notes", show_notes))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started with Gemini!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
