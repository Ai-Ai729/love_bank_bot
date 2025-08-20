# love_bank.py
# Мини-бот Love Bank:
# - Приветствие, баланс, меню призов
# - Принимает фото игрушечных 100€ и считает КОЛИЧЕСТВО купюр через OpenAI Vision
# - 1 купюра = 100€, зачисляет баланс и даёт обменять на призы
# - Уведомления владельцу при пополнении/покупке (если задан OWNER_CHAT_ID)
# - Поддержка альбомов (media_group_id)
# - NEW: подтверждение обмена приза
# - NEW: защита от повторной загрузки идентичного фото (анти-дубли)

import os, re, sqlite3, base64, asyncio, hashlib, time, secrets
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# -------- Настройки окружения --------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OWNER_CHAT_ID  = os.getenv("OWNER_CHAT_ID")   # опционально: твой chat id для уведомлений
DENOM_VALUE    = 100     # номинал одной игрушечной купюры
JACKPOT        = 5000    # порог суперприза
DB_PATH        = "love_bank.db"

if not TELEGRAM_TOKEN:
    raise SystemExit("Нужно выставить переменную TELEGRAM_TOKEN")
if not OPENAI_API_KEY:
    raise SystemExit("Нужно выставить переменную OPENAI_API_KEY")

# -------- OpenAI (Responses API) --------
from openai import OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

async def owner_notify(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Асинхронно шлём уведомление владельцу, если указан OWNER_CHAT_ID."""
    if not OWNER_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
    except Exception:
        pass

def count_banknotes_with_openai(image_bytes: bytes) -> int:
    """Просим модель посчитать СКОЛЬКО отдельных игрушечных купюр 100€ на фото."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "You are counting TOY euro banknotes. "
        "These are fake 100 EURO notes. "
        "Count HOW MANY SEPARATE banknotes are visible on the photo. "
        "Important: multiple '100' texts on the SAME note still count as ONE. "
        "Answer ONLY with an integer number, nothing else."
    )
    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
            ],
        }],
    )
    text = resp.output_text.strip()
    m = re.search(r"\d+", text)
    if m:
        return int(m.group())
    return 0

# -------- Меню призов --------
MENU = [
    ("Поцелуй 💋 ",          100,   "kiss"),
    ("Обнимашки 🤗 (с бонусом)", 200, "hug"),
    ("Массаж спины 💆",    300,   "massage"),
    ("Домашняя выпечка 🍰", 400,  "coffee"),
    ("Доброе утро (ты спишь, Нику отвожу в сад я)",  500, "breakfast"),
    ("Перевод 100€ на счёт 💰 (с квитанцией)", 1000, "cashout100"),
    ("СЕКРЕТНЫЙ СУПЕРПРИЗ 💃 (готовься…)",     5000,  "jackpot"),
]

def menu_keyboard(balance: int) -> InlineKeyboardMarkup:
    rows = []
    for title, cost, code in MENU:
        label = f"{title} — {cost}€"
        if balance >= cost:
            cb = f"redeem|{code}|{cost}"
        else:
            label = "🔒 " + label
            cb = f"lock|{code}|{cost}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    return InlineKeyboardMarkup(rows)

# -------- База (SQLite) --------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        balance INTEGER DEFAULT 0
    )""")
    # NEW: таблица учтённых изображений (анти-дубликаты)
    cur.execute("""CREATE TABLE IF NOT EXISTS images(
        user_id INTEGER,
        hash TEXT,
        created_at INTEGER,
        PRIMARY KEY(user_id, hash)
    )""")
    # NEW: отложенные обмены (подтверждение)
    cur.execute("""CREATE TABLE IF NOT EXISTS pending(
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        code TEXT,
        cost INTEGER,
        created_at INTEGER
    )""")
    con.commit(); con.close()

def with_db(fn):
    def wrapper(*args, **kwargs):
        con = sqlite3.connect(DB_PATH)
        try:
            res = fn(con, *args, **kwargs)
            con.commit()
            return res
        finally:
            con.close()
    return wrapper

@with_db
def ensure_user(con, u):
    cur = con.cursor()
    cur.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,))
    if not cur.fetchone():
        cur.execute("INSERT INTO users(user_id, first_name, username, balance) VALUES (?,?,?,0)",
                    (u.id, u.first_name or "", u.username or ""))

@with_db
def get_balance(con, user_id: int) -> int:
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else 0

@with_db
def add_balance(con, user_id: int, delta: int) -> int:
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("user not found")
    new_bal = row[0] + delta
    if new_bal < 0:
        raise ValueError("Недостаточно средств")
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
    return new_bal

# --- Анти-дубли изображений ---
def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

@with_db
def try_add_image_hash(con, user_id: int, h: str) -> bool:
    """True, если хэш записан (новое фото). False, если уже было."""
    cur = con.cursor()
    try:
        cur.execute("INSERT INTO images(user_id, hash, created_at) VALUES (?,?,?)",
                    (user_id, h, int(time.time())))
        return True
    except sqlite3.IntegrityError:
        return False

# --- Pending подтверждения обмена ---
@with_db
def pending_put(con, token: str, user_id: int, code: str, cost: int):
    cur = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO pending(token, user_id, code, cost, created_at) VALUES (?,?,?,?,?)",
        (token, user_id, code, cost, int(time.time()))
    )

@with_db
def pending_get(con, token: str):
    cur = con.cursor()
    cur.execute("SELECT user_id, code, cost FROM pending WHERE token=?", (token,))
    return cur.fetchone()  # (user_id, code, cost) | None

@with_db
def pending_del(con, token: str):
    cur = con.cursor()
    cur.execute("DELETE FROM pending WHERE token=?", (token,))

# -------- Команды --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    name = user.first_name or "мой клиент"
    text = (
        f"Привет, {name}! Я твой <b>Love Bank</b> 💘\n\n"
        "Правила простые:\n"
        "• Присылай фото с купюрами 100€. Я посчитаю и пополню твой баланс.\n"
        "• Открой /love_menu и меняй их на радости.\n\n"
        "<i>P.S. Даже если деньги фальшивые, наша любовь настоящая 🧡.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Правила простые:\n"
        "• Присылай фото с купюрами 100€. Я посчитаю и пополню твой баланс.\n"
        "• Открой /love_menu и меняй их на радости.\n\n"
        "Советы:\n"
        "• Клади купюры на светлый фон, не перекрывай.\n"
        "• Лучше одним слоем.\n"
        "• Можно снимать сразу пачку.\n\n"
    )

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance(update.effective_user.id)
    await update.message.reply_text(f"Твой баланс: {bal}€\nИ помни, ты всегда прав, потому что ты лев :)")

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance(update.effective_user.id)
    await update.message.reply_text(
        f"Обменный курс. Твой баланс: {bal}€\nВыбирай приз, красавчик:",
        reply_markup=menu_keyboard(bal)
    )

# -------- Фото обработка --------
album_buffers = defaultdict(list)  # gid -> [bytes]
album_tasks: dict[str, asyncio.Task] = {}  # gid -> asyncio.Task

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    photo = update.message.photo[-1]
    file = await photo.get_file()
    b = await file.download_as_bytearray()

    # 1) считаем купюры
    try:
        count = count_banknotes_with_openai(b)
    except Exception:
        await update.message.reply_text("Упс, не смог распознать фото 🙈")
        return

    if count <= 0:
        await update.message.reply_text("Я не вижу купюры 😔")
        return

    # 2) фиксируем хэш как использованный (после удачного распознавания)
    h = sha256_hex(b)
    if not try_add_image_hash(user.id, h):
        await update.message.reply_text("Ага! Попался! Это фото уже использовалось для пополнения 🤚\nПришли другое фото.")
        return

    # 3) начисляем
    amount = count * DENOM_VALUE
    new_bal = add_balance(user.id, amount)

    await owner_notify(
        context,
        f"💶 Пополнение: {user.first_name or user.username or user.id} "
        f"+{amount}€ (купюр: {count}). Баланс: {new_bal}€."
    )

    msg = (
        f"✅ Купюр найдено: {count} × {DENOM_VALUE}€ = +{amount}€\n"
        f"Текущий баланс: {new_bal}€"
    )
    if new_bal >= JACKPOT and (new_bal - amount) < JACKPOT:
        msg += "\n\n🎉 Ты собрал суперприз! Открой /love_menu."
    await update.message.reply_text(msg)
    await update.message.reply_text("Хочешь обменять прямо сейчас?", reply_markup=menu_keyboard(new_bal))

# -------- Album handler: одно сообщение на весь альбом --------
async def album_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Собираем фото альбома и планируем единичную обработку через asyncio."""
    user = update.effective_user
    ensure_user(user)

    gid = update.message.media_group_id
    photo = update.message.photo[-1]
    file = await photo.get_file()
    b = await file.download_as_bytearray()
    # Кладём все фото; фильтровать будем после распознавания
    album_buffers[gid].append(b)

    # Планируем обработку ОДИН раз на весь альбом (если ещё не запланирована)
    if gid not in album_tasks:
        chat_id = update.effective_chat.id
        first_name = user.first_name or user.username or str(user.id)

        async def finalize_after_delay():
            # ждём, пока Telegram догрузит остальные фото альбома
            await asyncio.sleep(1.2)
            images = album_buffers.pop(gid, [])
            album_tasks.pop(gid, None)  # снять «замок» задачи

            if not images:
                return

            total_count = 0
            accepted_photos = 0

            # Для каждого изображения: считаем, затем пытаемся пометить хэш (анти-дубль).
            for img in images:
                try:
                    c = count_banknotes_with_openai(img)
                except Exception:
                    continue
                if c <= 0:
                    continue
                h = sha256_hex(img)
                if not try_add_image_hash(user.id, h):
                    # дубль — пропускаем
                    continue
                total_count += c
                accepted_photos += 1

            if total_count <= 0:
                await context.bot.send_message(
                    chat_id,
                    "Ага! Попался! В альбоме нет новых фото для пополнения 🤷‍♀️"
                )
                return

            amount = total_count * DENOM_VALUE
            new_bal = add_balance(user.id, amount)

            # оповещение владельцу (в ЛС)
            await owner_notify(
                context,
                f"💶 Пополнение (альбом): {first_name} +{amount}€ "
                f"(купюр: {total_count}, учтённых фото: {accepted_photos}/{len(images)}). Баланс: {new_bal}€."
            )

            # Одно сообщение на весь альбом: сводка + меню
            text = (
                "📸 Фотосессия для денег завершена!\n"
                f"✅ В альбоме зачтено {accepted_photos} фото. "
                f"Найдено {total_count} купюр × {DENOM_VALUE}€ = +{amount}€\n"
                f"Текущий баланс: {new_bal}€\n\n"
                "Хочешь обменять прямо сейчас?"
            )
            await context.bot.send_message(chat_id, text, reply_markup=menu_keyboard(new_bal))

        album_tasks[gid] = asyncio.create_task(finalize_after_delay())

# -------- Router --------
async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.media_group_id:
        await album_handler(update, context)
    else:
        await photo_handler(update, context)

# -------- Callback кнопок (включая подтверждение) --------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    ensure_user(user)

    data = (q.data or "").split("|")
    # Поддерживаем как форматы из меню (len==3), так и confirm/cancel (len==2)
    action = data[0]
    if action == "lock":
        await q.edit_message_text("Недостаточно средств. Пришли ещё фото 😉")
        return

    if action == "redeem":
        if len(data) != 3:
            return
        _, code, cost_s = data
        cost = int(cost_s)

        bal = get_balance(user.id)
        item = next((t for (t, c, k) in MENU if k == code and c == cost), None)
        if not item:
            await q.edit_message_text("Позиция не найдена.")
            return
        if bal < cost:
            await q.edit_message_text("Недостаточно средств. Копи еще 😉")
            return

        # NEW: подтверждение обмена
        token = secrets.token_hex(8)
        pending_put(token, user.id, code, cost)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить обмен", callback_data=f"confirm|{token}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{token}")]
        ])
        await q.edit_message_text(
            f"Ты выбираешь: {item} за {cost}€.\nПодтверди обмен?",
            reply_markup=kb
        )
        return

    if action == "confirm":
        if len(data) != 2:
            return
        token = data[1]
        row = pending_get(token)
        if not row:
            await q.edit_message_text("Запрос подтверждения не найден или уже обработан.")
            return
        p_user_id, p_code, p_cost = row
        if p_user_id != user.id:
            await q.edit_message_text("Этот запрос подтверждения не принадлежит тебе.")
            return

        bal = get_balance(user.id)
        item = next((t for (t, c, k) in MENU if k == p_code and c == p_cost), None)
        if not item:
            pending_del(token)
            await q.edit_message_text("Позиция не найдена.")
            return
        if bal < p_cost:
            pending_del(token)
            await q.edit_message_text("Недостаточно средств. Копи еще 😉")
            return

        new_bal = add_balance(user.id, -p_cost)
        pending_del(token)

        extra = ""
        if p_code == "cashout100":
            extra = "\n🧾 Жди подтверждения."
        if p_code == "jackpot":
            extra = "\n💃 Суперприз активирован! (Жди сообщение - сюрприз 😉)"

        await owner_notify(
            context,
            f"🛒 Покупка: {user.first_name or user.username or user.id} "
            f"обменял {p_cost}€ → {item}. Баланс: {new_bal}€."
        )

        await q.edit_message_text(
            f"✅ Обмен подтверждён: {item} за {p_cost}€{extra}\n"
            f"Текущий баланс: {new_bal}€"
        )
        await q.message.reply_text("Хочешь обменять что-то ещё?", reply_markup=menu_keyboard(new_bal))
        return

    if action == "cancel":
        if len(data) != 2:
            return
        token = data[1]
        pending_del(token)
        await q.edit_message_text("Обмен отменён. Выбери другой приз из меню ❤️")
        return

# -------- Main --------
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("love_menu", menu_cmd))

    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))

    print("Love Bank (OpenAI Vision) запущен. Ctrl+C для выхода.")
    app.run_polling()

if __name__ == "__main__":
    main()
