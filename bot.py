import os
import logging
import httpx
import asyncio
from datetime import datetime, date, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
import google.generativeai as genai

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
ALERTS_TOKEN   = os.environ.get("ALERTS_TOKEN", "")
WEATHER_KEY    = os.environ.get("OPENWEATHER_API_KEY", "")
NEWS_KEY       = os.environ.get("NEWS_API_KEY", "")
ACCESS_GROUP   = int(os.environ.get("ACCESS_GROUP_ID", "-5252439690"))  # ID закрытой группы

ODESSA_REGION_ID = 16

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
all_users: set = set()
last_alert_status: dict = {"active": None}
user_settings: dict = {}  # {uid: {"alert": True, "events": True}}

async def check_access(bot, uid: int) -> bool:
    """Проверить есть ли пользователь в закрытой группе."""
    if ACCESS_GROUP == 0:
        return True  # Если группа не настроена — пускать всех
    try:
        member = await bot.get_chat_member(chat_id=ACCESS_GROUP, user_id=uid)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def get_settings(uid: int) -> dict:
    if uid not in user_settings:
        user_settings[uid] = {"alert": True, "events": True}
    return user_settings[uid]

# Фиксированные сабантуи
FIXED_EVENTS = [
    (1, 1,  "🎆 Новый год"),
    (7, 1,  "🎄 Рождество"),
    (8, 3,  "💐 8 Марта"),
]
custom_events: list = []

# ─── Меню ────────────────────────────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🌤 Погода",        "🚨 Тревога Одесса"],
        ["🎉 График сабантуев"],
        ["🔔 Напоминания"],
        ["❓ Спросить AI"],
        ["⚙️ Настройки",    "📖 Инструкция"],
    ],
    resize_keyboard=True,
)

# Подменю напоминаний
REMINDERS_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Мои напоминания"],
        ["➕ Создать напоминание"],
        ["🔙 Назад"],
    ],
    resize_keyboard=True,
)

def settings_keyboard(uid: int) -> InlineKeyboardMarkup:
    s = get_settings(uid)
    alert_icon  = "🔔" if s["alert"]  else "🔕"
    events_icon = "🔔" if s["events"] else "🔕"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{alert_icon} Тревога Одесса", callback_data="toggle_alert")],
        [InlineKeyboardButton(f"{events_icon} Сабантуи",      callback_data="toggle_events")],
    ])

# ════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════════════════

def get_chat(uid: int):
    if uid not in user_chats:
        user_chats[uid] = gemini_model.start_chat(history=[])
    return user_chats[uid]

async def ask_gemini(uid: int, text: str) -> str:
    chat = get_chat(uid)
    response = chat.send_message(text)
    return response.text

async def ask_gemini_with_file(prompt: str, file_data: bytes, mime_type: str) -> str:
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content([{"mime_type": mime_type, "data": file_data}, prompt])
    return response.text

async def download_file(file_url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        r = await client.get(file_url, timeout=30)
    return r.content

async def get_weather(city: str) -> str:
    if not WEATHER_KEY:
        return "⚠️ OPENWEATHER_API_KEY не настроен.\nhttps://openweathermap.org/api"
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_KEY}&units=metric&lang=ru"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
    if r.status_code != 200:
        return f"❌ Город «{city}» не найден."
    d = r.json()
    return (
        f"🌤 Погода в {d['name']}\n\n"
        f"🌡 {round(d['main']['temp'])}°C (ощущается {round(d['main']['feels_like'])}°C)\n"
        f"☁️ {d['weather'][0]['description'].capitalize()}\n"
        f"💧 Влажность: {d['main']['humidity']}%\n"
        f"💨 Ветер: {round(d['wind']['speed'])} м/с"
    )

async def get_news(topic: str = "") -> str:
    if not NEWS_KEY:
        return "⚠️ NEWS_API_KEY не настроен.\nhttps://newsapi.org"
    query = topic if topic else "Украина"
    url = f"https://newsapi.org/v2/everything?q={query}&language=ru&sortBy=publishedAt&pageSize=5&apiKey={NEWS_KEY}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
    articles = r.json().get("articles", [])
    if not articles:
        return "📰 Новости не найдены."
    lines = [f"📰 Новости «{query}»:\n"]
    for i, a in enumerate(articles[:5], 1):
        lines.append(f"{i}. {a['title']}\n   🔗 {a['url']}\n")
    return "\n".join(lines)

# ────────────────────────────────────────────────────────────────────────────
# Тревога
# ────────────────────────────────────────────────────────────────────────────

async def check_alert() -> tuple[bool, str]:
    if not ALERTS_TOKEN:
        return False, "⚠️ ALERTS_TOKEN не настроен.\nПолучи на https://alerts.in.ua"
    try:
        url = f"https://alerts.in.ua/api/alerts/{ODESSA_REGION_ID}.json"
        headers = {"Authorization": f"Bearer {ALERTS_TOKEN}"}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10, headers=headers)
        data = r.json()
        active = data.get("alert", {}).get("status") == "A"
        if active:
            return True, "🚨 УВАГА! Повітряна тривога в Одесі!\nПрямуйте до укриття! 🏃"
        else:
            return False, "✅ Тривоги немає. Одеса спокійна."
    except Exception as e:
        logger.error("Alert check error: %s", e)
        return False, "❌ Не вдалося перевірити тривогу."

async def alert_monitor(app: Application) -> None:
    await asyncio.sleep(10)
    while True:
        try:
            active, _ = await check_alert()
            prev = last_alert_status["active"]
            if prev is not None and active != prev:
                if active:
                    msg = "🚨 УВАГА! Повітряна тривога в Одесі!\nПрямуйте до укриття! 🏃"
                else:
                    msg = "✅ Відбій тривоги в Одесі. Можна виходити. 😌"
                # Отправляем только подписанным пользователям
                for uid in list(all_users):
                    if get_settings(uid)["alert"]:
                        try:
                            await app.bot.send_message(chat_id=uid, text=msg)
                        except Exception:
                            pass
            last_alert_status["active"] = active
        except Exception as e:
            logger.error("Alert monitor error: %s", e)
        await asyncio.sleep(30)

# ────────────────────────────────────────────────────────────────────────────
# Сабантуи
# ────────────────────────────────────────────────────────────────────────────

def get_upcoming_events(days_ahead: int = 60) -> list[dict]:
    today = date.today()
    year = today.year
    events = []
    for day, month, name in FIXED_EVENTS:
        for y in [year, year + 1]:
            try:
                d = date(y, month, day)
                delta = (d - today).days
                if 0 <= delta <= days_ahead:
                    events.append({"name": name, "date": d, "days_left": delta})
            except ValueError:
                pass
    for ev in custom_events:
        d = ev["date"]
        if d < today:
            try:
                d = date(d.year + 1, d.month, d.day)
            except ValueError:
                continue
        delta = (d - today).days
        if 0 <= delta <= days_ahead:
            events.append({"name": ev["name"], "date": d, "days_left": delta})
    events.sort(key=lambda x: x["date"])
    return events

def format_events_list() -> str:
    events = get_upcoming_events(180)
    if not events:
        return "🎉 Ближайших событий нет."
    lines = ["🎉 Ближайшие сабантуи:\n"]
    for e in events[:10]:
        dl = e["days_left"]
        if dl == 0:
            when = "🔥 СЕГОДНЯ!"
        elif dl == 1:
            when = "завтра"
        else:
            when = f"через {dl} дн."
        lines.append(f"{e['name']}\n   📅 {e['date'].strftime('%d.%m.%Y')} ({when})\n")
    return "\n".join(lines)

async def send_to_all(app, msg: str, only_events_subscribers: bool = False) -> None:
    for uid in list(all_users):
        if only_events_subscribers and not get_settings(uid)["events"]:
            continue
        try:
            await app.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            pass

async def check_and_send(app: Application, is_morning: bool) -> None:
    events = get_upcoming_events(7)
    for ev in events:
        dl = ev["days_left"]
        if is_morning and dl == 0:
            await send_to_all(app, f"🎉 СЕГОДНЯ — {ev['name']}!\nПоздравляем! 🥳", only_events_subscribers=True)
        elif not is_morning and dl in (3, 7):
            if dl == 3:
                await send_to_all(app, f"⏰ Через 3 дня — {ev['name']}!\n📅 {ev['date'].strftime('%d.%m.%Y')}", only_events_subscribers=True)
            else:
                await send_to_all(app, f"📅 Через неделю — {ev['name']}!\n📅 {ev['date'].strftime('%d.%m.%Y')}", only_events_subscribers=True)

async def sabantuy_notifier(app: Application) -> None:
    now = datetime.now()
    WINDOW = 2 * 60 * 60

    t_0900_today = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    t_1430_today = now.replace(hour=14, minute=30, second=0, microsecond=0)

    if 0 < (now - t_0900_today).total_seconds() < WINDOW:
        await check_and_send(app, is_morning=True)
    if 0 < (now - t_1430_today).total_seconds() < WINDOW:
        await check_and_send(app, is_morning=False)

    while True:
        now = datetime.now()
        t_0900 = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        if now >= t_0900:
            t_0900 += timedelta(days=1)
        t_1430 = now.replace(hour=14, minute=30, second=0, microsecond=0)
        if now >= t_1430:
            t_1430 += timedelta(days=1)
        next_run = min(t_0900, t_1430)
        await asyncio.sleep((next_run - now).total_seconds())
        now2 = datetime.now()
        await check_and_send(app, is_morning=(now2.hour == 9))

# ════════════════════════════════════════════════════════════════════════════
# Обработчики команд
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# СИСТЕМА НАПОМИНАНИЙ
# ════════════════════════════════════════════════════════════════════════════

# user_reminders[uid] = [{"name": ..., "dt": datetime, "always": bool, "sent": bool}]
user_reminders: dict = {}

# Состояние создания напоминания
# reminder_state[uid] = {"step": ..., "name": ..., "year": ..., ...}
reminder_state: dict = {}

def get_reminders(uid: int) -> list:
    if uid not in user_reminders:
        user_reminders[uid] = []
    return user_reminders[uid]

# ─── Клавиатуры для создания напоминания ────────────────────────────────────

def kb_years() -> InlineKeyboardMarkup:
    now = datetime.now()
    years = [now.year + i for i in range(6)]
    rows = [[InlineKeyboardButton(str(y), callback_data=f"ry_{y}") for y in years]]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_months() -> InlineKeyboardMarkup:
    names = ["Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"]
    rows = []
    row = []
    for i, n in enumerate(names, 1):
        row.append(InlineKeyboardButton(n, callback_data=f"rm_{i}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_days(year: int, month: int) -> InlineKeyboardMarkup:
    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    rows = []
    row = []
    for d in range(1, days_in_month + 1):
        row.append(InlineKeyboardButton(str(d), callback_data=f"rd_{d}"))
        if len(row) == 7:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_hours() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for h in range(24):
        row.append(InlineKeyboardButton(f"{h:02d}", callback_data=f"rh_{h}"))
        if len(row) == 6:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_minutes() -> InlineKeyboardMarkup:
    minutes = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
    rows = []
    row = []
    for m in minutes:
        row.append(InlineKeyboardButton(f":{m:02d}", callback_data=f"rmin_{m}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_missed() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ Прислать в течение 2ч (если бот заработает)", callback_data="rmiss_2h")],
        [InlineKeyboardButton("📨 Прислать когда бот заработает", callback_data="rmiss_always")],
        [InlineKeyboardButton("❌ Отмена", callback_data="r_cancel")],
    ])

def kb_reminders_list(uid: int) -> InlineKeyboardMarkup:
    reminders = get_reminders(uid)
    rows = []
    for i, r in enumerate(reminders):
        dt_str = r["dt"].strftime("%d.%m.%Y %H:%M")
        rows.append([InlineKeyboardButton(
            f"🗑 {r['name']} ({dt_str})", callback_data=f"rdel_{i}"
        )])
    rows.append([InlineKeyboardButton("➕ Добавить напоминание", callback_data="r_add")])
    rows.append([InlineKeyboardButton("🔙 Закрыть", callback_data="r_close")])
    return InlineKeyboardMarkup(rows)

# ─── Монитор напоминаний ─────────────────────────────────────────────────────

async def reminder_monitor(app: Application) -> None:
    """Проверяет напоминания каждую минуту."""
    # Проверка пропущенных при старте
    now = datetime.now()
    for uid, reminders in user_reminders.items():
        for r in reminders:
            if r.get("sent"):
                continue
            delta = (now - r["dt"]).total_seconds()
            if delta > 0:
                if r.get("always") or delta < 7200:
                    try:
                        await app.bot.send_message(
                            chat_id=uid,
                            text=f"🔔 Напоминание!\n\n{r['name']}\n\n"
                                 f"📅 {r['dt'].strftime('%d.%m.%Y %H:%M')}"
                        )
                        r["sent"] = True
                    except Exception:
                        pass

    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        for uid, reminders in user_reminders.items():
            for r in reminders:
                if r.get("sent"):
                    continue
                delta = (now - r["dt"]).total_seconds()
                if 0 <= delta < 120:  # в пределах 2 минут от времени
                    try:
                        await app.bot.send_message(
                            chat_id=uid,
                            text=f"🔔 Напоминание!\n\n{r['name']}\n\n"
                                 f"📅 {r['dt'].strftime('%d.%m.%Y %H:%M')}"
                        )
                        r["sent"] = True
                    except Exception:
                        pass


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    name = update.effective_user.first_name

    if not await check_access(ctx.bot, uid):
        await update.message.reply_text(
            "🔒 Доступ запрещён.\n"
            "Ты не являешься участником группы."
        )
        return

    all_users.add(uid)
    get_settings(uid)
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Я твой AI-ассистент на базе Gemini.\n"
        "🚨 Уведомлю о тревоге в Одессе!\n"
        "🎉 Напомню о праздниках!\n\n"
        "Выбери действие в меню:",
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

# ════════════════════════════════════════════════════════════════════════════
# Обработчик кнопок настроек (inline)
# ════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    data = query.data
    await query.answer()

    # ── Настройки ──────────────────────────────────────────────────────────
    if data in ("toggle_alert", "toggle_events"):
        s = get_settings(uid)
        if data == "toggle_alert":
            s["alert"] = not s["alert"]
        else:
            s["events"] = not s["events"]
        s = get_settings(uid)
        alert_status  = "включены ✅" if s["alert"]  else "выключены ❌"
        events_status = "включены ✅" if s["events"] else "выключены ❌"
        await query.edit_message_text(
            f"⚙️ Настройки уведомлений:\n\n"
            f"🚨 Тревога Одесса: {alert_status}\n"
            f"🎉 Сабантуи: {events_status}\n\n"
            "Нажми кнопку чтобы включить/выключить:",
            reply_markup=settings_keyboard(uid),
        )
        return

    # ── Напоминания ────────────────────────────────────────────────────────
    if data == "r_close":
        await query.edit_message_text("✅ Закрыто.")
        return

    if data == "r_cancel":
        reminder_state.pop(uid, None)
        await query.edit_message_text("❌ Создание напоминания отменено.")
        return

    if data == "r_add":
        reminder_state[uid] = {"step": "name"}
        await query.edit_message_text(
            "📝 Напиши название напоминания\n(например: Страховка машины)"
        )
        return

    if data.startswith("rdel_"):
        idx = int(data.split("_")[1])
        reminders = get_reminders(uid)
        if 0 <= idx < len(reminders):
            name = reminders[idx]["name"]
            reminders.pop(idx)
            await query.edit_message_text(
                f"🗑 Удалено: {name}\n\nТвои напоминания:",
                reply_markup=kb_reminders_list(uid)
            )
        return

    if data.startswith("ry_"):
        reminder_state[uid]["year"] = int(data.split("_")[1])
        reminder_state[uid]["step"] = "month"
        await query.edit_message_text("📅 Выбери месяц:", reply_markup=kb_months())
        return

    if data.startswith("rm_"):
        reminder_state[uid]["month"] = int(data.split("_")[1])
        reminder_state[uid]["step"] = "day"
        y = reminder_state[uid]["year"]
        m = reminder_state[uid]["month"]
        await query.edit_message_text("📅 Выбери день:", reply_markup=kb_days(y, m))
        return

    if data.startswith("rd_"):
        reminder_state[uid]["day"] = int(data.split("_")[1])
        reminder_state[uid]["step"] = "hour"
        await query.edit_message_text("🕐 Выбери час:", reply_markup=kb_hours())
        return

    if data.startswith("rh_"):
        reminder_state[uid]["hour"] = int(data.split("_")[1])
        reminder_state[uid]["step"] = "minute"
        await query.edit_message_text("🕐 Выбери минуты:", reply_markup=kb_minutes())
        return

    if data.startswith("rmin_"):
        reminder_state[uid]["minute"] = int(data.split("_")[1])
        reminder_state[uid]["step"] = "missed"
        await query.edit_message_text(
            "⚙️ Если бот не работал и пропустил время — что делать?",
            reply_markup=kb_missed()
        )
        return

    if data in ("rmiss_2h", "rmiss_always"):
        state = reminder_state.get(uid, {})
        try:
            dt = datetime(
                state["year"], state["month"], state["day"],
                state["hour"], state["minute"]
            )
            always = data == "rmiss_always"
            get_reminders(uid).append({
                "name": state["name"],
                "dt": dt,
                "always": always,
                "sent": False
            })
            reminder_state.pop(uid, None)
            mode = "прислать когда бот заработает" if always else "прислать в течение 2ч после запуска"
            await query.edit_message_text(
                f"✅ Напоминание создано!\n\n"
                f"🏷 {state['name']}\n"
                f"📅 {dt.strftime('%d.%m.%Y %H:%M')}\n"
                f"⚙️ При пропуске: {mode}"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return

# ════════════════════════════════════════════════════════════════════════════
# Обработчики медиа
# ════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📸 Анализирую фото...")
    try:
        photo = update.message.photo[-1]
        tg_file = await ctx.bot.get_file(photo.file_id)
        file_bytes = await download_file(tg_file.file_path)
        caption = update.message.caption or "Опиши подробно что на этом фото. Отвечай на русском."
        reply = await ask_gemini_with_file(caption, file_bytes, "image/jpeg")
        await update.message.reply_text(reply, reply_markup=MAIN_MENU)
    except Exception as e:
        logger.error("Photo error: %s", e)
        await update.message.reply_text("❌ Не удалось обработать фото.", reply_markup=MAIN_MENU)

SUPPORTED_DOCS = {
    "application/pdf": "application/pdf",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/webp": "image/webp",
    "text/plain": "text/plain",
}

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    mime = doc.mime_type or ""
    if mime not in SUPPORTED_DOCS and not mime.startswith("text/"):
        await update.message.reply_text("⚠️ Поддерживаются: PDF, PNG, JPG, TXT", reply_markup=MAIN_MENU)
        return
    await update.message.reply_text(f"📄 Обрабатываю «{doc.file_name}»...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        file_bytes = await download_file(tg_file.file_path)
        caption = update.message.caption or "Проанализируй документ и кратко расскажи о содержимом. Отвечай на русском."
        reply = await ask_gemini_with_file(caption, file_bytes, SUPPORTED_DOCS.get(mime, "text/plain"))
        await update.message.reply_text(reply, reply_markup=MAIN_MENU)
    except Exception as e:
        logger.error("Document error: %s", e)
        await update.message.reply_text("❌ Не удалось обработать документ.", reply_markup=MAIN_MENU)

async def handle_sticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("😄 Классный стикер! Напиши мне что-нибудь словами.", reply_markup=MAIN_MENU)

# ════════════════════════════════════════════════════════════════════════════
# Главный обработчик текста
# ════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = update.effective_user.id
    text  = update.message.text.strip()
    lower = text.lower()

    # Проверка доступа
    if not await check_access(ctx.bot, uid):
        await update.message.reply_text(
            "🔒 Доступ запрещён.\n"
            "Ты не являешься участником группы."
        )
        return

    all_users.add(uid)

    # Инструкция
    if text == "📖 Инструкция":
        await update.message.reply_text(
            "📖 Как пользоваться ботом:\n\n"
            "🌤 *Погода* — нажми кнопку или «погода Одесса»\n"
            "🚨 *Тревога* — проверить тревогу в Одессе\n"
            "🎉 *График сабантуев* — ближайшие праздники\n"
            "❓ *Спросить AI* — любой вопрос Gemini\n"
            "⚙️ *Настройки* — включить/выключить уведомления\n\n"
            "Добавить событие:\n«сабантуй 25.06 День рождения»\n\n"
            "📰 Новости — напиши «новости»\n"
            "🌍 Перевод — «переведи Hello на русский»\n"
            "📝 Заметка — «заметка купить молоко»\n\n"
            "🔔 Бот сам уведомит о тревоге и праздниках!",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        ); return

    # Настройки
    if text == "⚙️ Настройки":
        s = get_settings(uid)
        alert_status  = "включены ✅" if s["alert"]  else "выключены ❌"
        events_status = "включены ✅" if s["events"] else "выключены ❌"
        await update.message.reply_text(
            f"⚙️ Настройки уведомлений:\n\n"
            f"🚨 Тревога Одесса: {alert_status}\n"
            f"🎉 Сабантуи: {events_status}\n\n"
            "Нажми кнопку чтобы включить/выключить:",
            reply_markup=settings_keyboard(uid),
        ); return

    # Напоминания — главная кнопка
    if text == "🔔 Напоминания":
        await update.message.reply_text(
            "🔔 Напоминания — выбери действие:",
            reply_markup=REMINDERS_MENU
        )
        return

    # Мои напоминания
    if text == "📋 Мои напоминания":
        reminders = get_reminders(uid)
        if not reminders:
            await update.message.reply_text(
                "📋 У тебя пока нет напоминаний.",
                reply_markup=REMINDERS_MENU
            )
        else:
            lines = ["📋 Твои напоминания:\n"]
            for r in reminders:
                status = "✅" if r.get("sent") else "⏳"
                lines.append(f"{status} {r['name']}\n   📅 {r['dt'].strftime('%d.%m.%Y %H:%M')}")
            await update.message.reply_text(
                "\n".join(lines) + "\n\nНажми на напоминание чтобы удалить:",
                reply_markup=kb_reminders_list(uid)
            )
        return

    # Создать напоминание
    if text == "➕ Создать напоминание":
        reminder_state[uid] = {"step": "name"}
        await update.message.reply_text(
            "📝 Напиши название напоминания\n(например: Страховка машины)",
            reply_markup=REMINDERS_MENU
        )
        return

    # Назад
    if text == "🔙 Назад":
        reminder_state.pop(uid, None)
        await update.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
        return

    # Ввод названия напоминания
    if uid in reminder_state and reminder_state[uid].get("step") == "name":
        reminder_state[uid]["name"] = text
        reminder_state[uid]["step"] = "year"
        await update.message.reply_text("📅 Выбери год:", reply_markup=kb_years())
        return

    if text == "❓ Спросить AI":
        await update.message.reply_text("💬 Напиши свой вопрос!", reply_markup=MAIN_MENU); return

    # Погода
    if text == "🌤 Погода" or lower.startswith("погода"):
        city = text.replace("🌤 Погода", "").replace("погода", "").strip() or "Одесса"
        await update.message.reply_text("⏳ Получаю данные...")
        await update.message.reply_text(await get_weather(city), reply_markup=MAIN_MENU); return

    # Новости
    if lower.startswith("новости"):
        topic = text.replace("новости", "").strip()
        await update.message.reply_text("⏳ Ищу новости...")
        await update.message.reply_text(await get_news(topic), reply_markup=MAIN_MENU); return

    # Тревога
    if text == "🚨 Тревога Одесса" or "тревога" in lower:
        await update.message.reply_text("⏳ Проверяю...")
        _, msg = await check_alert()
        await update.message.reply_text(msg, reply_markup=MAIN_MENU); return

    # Сабантуи
    if text == "🎉 График сабантуев":
        await update.message.reply_text(format_events_list(), reply_markup=MAIN_MENU); return

    # Добавить событие
    if lower.startswith("сабантуй "):
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            try:
                d = datetime.strptime(parts[1], "%d.%m").date().replace(year=date.today().year)
                name = parts[2]
                custom_events.append({"name": f"🎉 {name}", "date": d})
                await update.message.reply_text(
                    f"✅ Добавлено: {name} — {d.strftime('%d.%m.%Y')}\n"
                    "Уведомлю за 7 дней, 3 дня и в день события!",
                    reply_markup=MAIN_MENU
                )
            except ValueError:
                await update.message.reply_text(
                    "❌ Формат: «сабантуй 25.06 День рождения»",
                    reply_markup=MAIN_MENU
                )
        else:
            await update.message.reply_text("✍️ Формат: «сабантуй 25.06 День рождения»", reply_markup=MAIN_MENU)
        return



    # Заметки
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
        await update.message.reply_text("❌ Ошибка Gemini. Проверь API ключ.", reply_markup=MAIN_MENU)

# ════════════════════════════════════════════════════════════════════════════
# Запуск
# ════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application) -> None:
    asyncio.create_task(alert_monitor(app))
    asyncio.create_task(sabantuy_notifier(app))
    asyncio.create_task(reminder_monitor(app))

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("notes", show_notes))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.Sticker.ALL,  handle_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()