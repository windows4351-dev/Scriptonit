import asyncio
import aiosqlite
import os
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import *
from keys import init_keys_table, issue_key_for_user

# --- ИМПОРТ SDK ROLLYPAY ---
from rollypay import RollyPayClient
rolly_client = RollyPayClient(api_key=ROLLY_API_KEY)
print("RollyPay SDK УСПЕШНО ЗАГРУЖЕН!")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_PATH = "shop.db"

class AgreementState(StatesGroup):
    waiting_accept = State()

class ReviewState(StatesGroup):
    waiting_for_text = State()

class WithdrawState(StatesGroup):
    waiting_card = State()

# --- БАЗА ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            agreed INTEGER DEFAULT 0, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1), completed_orders INTEGER DEFAULT 16
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE,
            username TEXT, text TEXT, status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            product_version TEXT, purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            payment_id TEXT UNIQUE, status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            card TEXT, status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # --- миграция для реферальной системы ---
        for col in ("referrer_id INTEGER", "balance INTEGER DEFAULT 0", "ref_rewarded INTEGER DEFAULT 0"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        await db.execute("INSERT OR IGNORE INTO stats (id, completed_orders) VALUES (1, 16)")
        await db.commit()
    await init_keys_table()

async def save_user(user_id, username, first_name, ref_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                                  (user_id, username, first_name))
        # реферер привязывается только у нового пользователя и не может быть им самим
        if cursor.rowcount and ref_id and ref_id != user_id:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (ref_id,)) as c:
                if await c.fetchone():
                    await db.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (ref_id, user_id))
        await db.commit()

# --- РЕФЕРАЛКА ---
async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return (row[0] or 0) if row else 0

async def get_ref_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(ref_rewarded), 0) FROM users WHERE referrer_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0], row[1]

async def reward_referrer(user_id: int):
    """Начисляет REF_REWARD пригласившему — один раз за покупку приглашённого."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE users SET ref_rewarded = 1 WHERE user_id = ? AND ref_rewarded = 0 AND referrer_id IS NOT NULL", (user_id,))
        if not cursor.rowcount:
            return
        async with db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)) as c:
            referrer_id = (await c.fetchone())[0]
        await db.execute("UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE user_id = ?", (REF_REWARD, referrer_id))
        await db.commit()
    try:
        await bot.send_message(referrer_id, f"🎉 <b>Ваш реферал совершил покупку!</b>\n\n💰 На баланс начислено: <b>+{REF_REWARD} ₽</b>\n💼 Баланс: <b>{await get_balance(referrer_id)} ₽</b>")
    except Exception as e:
        print(f"Не удалось уведомить реферера {referrer_id}: {e}")

async def is_agreed(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT agreed FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row and row[0] == 1

async def set_agreed(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET agreed = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_orders_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT completed_orders FROM stats WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 16

async def increment_orders():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE stats SET completed_orders = completed_orders + 1 WHERE id = 1")
        await db.commit()

async def has_review(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM reviews WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def save_review(user_id, username, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("INSERT INTO reviews (user_id, username, text) VALUES (?, ?, ?)",
                                  (user_id, username, text))
        await db.commit()
        return cursor.lastrowid

async def set_review_status(review_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
        await db.commit()

async def get_review_text(review_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT text FROM reviews WHERE id = ?", (review_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def has_purchased(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM purchases WHERE user_id = ? LIMIT 1", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def save_purchase(user_id: int, version: str = "1.0"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases (user_id, product_version) VALUES (?, ?)", (user_id, version))
        await db.commit()

async def save_payment(user_id, payment_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO payments (user_id, payment_id, status) VALUES (?, ?, 'pending')",
                         (user_id, payment_id))
        await db.commit()

async def set_payment_status(payment_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
        await db.commit()

# --- ВЫДАЧА КЛЮЧА И ФАЙЛОВ ---
async def send_product_files(message: Message, user_id: int):
    """Выдаёт ключ активации и ВСЕ файлы из папки files/ (любых типов), независимо от имени."""
    key = await issue_key_for_user(user_id)
    if key:
        await message.answer(
            f"🔑 <b>Твой ключ активации:</b>\n<code>{key}</code>\n\n"
            f"Вставь его в приложение при первом запуске. Ключ одноразовый и привязывается "
            f"к одному устройству — сохрани его, он больше нигде не повторится."
        )
    else:
        await message.answer("⚠️ Свободные ключи закончились. Напиши в поддержку — выдадим вручную.")
        await bot.send_message(ADMIN_ID, f"⚠️ Закончились свободные ключи активации! Покупатель: {user_id}")

    if os.path.isdir("files"):
        sent = False
        for name in sorted(os.listdir("files")):
            path = os.path.join("files", name)
            if not os.path.isfile(path):
                continue
            sent = True
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            try:
                if ext in ("mp4", "mov", "mkv"):
                    await message.answer_video(FSInputFile(path), caption=f"🎬 <b>{name}</b>")
                elif ext in ("jpg", "jpeg", "png", "webp"):
                    await message.answer_photo(FSInputFile(path), caption=name)
                else:
                    await message.answer_document(FSInputFile(path), caption=f"📦 <b>{name}</b>")
            except Exception as e:
                print(f"Не удалось отправить файл {path}: {e}")
        if not sent:
            await message.answer("⚠️ В папке files/ нет файлов для выдачи.")
    else:
        await message.answer("⚠️ Папка files/ не найдена.")

    if not await has_review(user_id):
        await message.answer(
            "💬 <b>Как тебе покупка?</b>\n\nОставь отзыв — это займёт минуту и очень поможет нам! ⭐",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Написать отзыв", callback_data="leave_review")]
            ])
        )

# --- КЛАВИАТУРЫ ---
def agreement_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_offer")],
        [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        [InlineKeyboardButton(text="📄 Правила", url=PRIVACY_URL)],
    ])

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить скрипт", callback_data="buy_menu")],
        [InlineKeyboardButton(text="📊 Информация", callback_data="info")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton(text="⭐ Отзывы", url=REVIEWS_CHANNEL_URL)],
        [InlineKeyboardButton(text="💰 Заработок", callback_data="earn")],
        [InlineKeyboardButton(text="📢 Наш канал", url=TG_CHANNEL_URL)],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton(text="📄 Правила", callback_data="rules")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back")]])

async def send_main_menu(message: Message, edit=False):
    text = "🏠 <b>Добро пожаловать в Scriptonit Shop</b>\n\nВыбери действие:"
    if os.path.exists(MAIN_IMAGE):
        if edit: await message.delete()
        await message.answer_photo(photo=FSInputFile(MAIN_IMAGE), caption=text, reply_markup=main_menu())
    else:
        if edit: await message.edit_text(text, reply_markup=main_menu())
        else: await message.answer(text, reply_markup=main_menu())

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject):
    await init_db()
    ref_id = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref_id = int(command.args[4:])
    await save_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "", ref_id)
    if await is_agreed(message.from_user.id):
        await send_main_menu(message)
    else:
        await state.set_state(AgreementState.waiting_accept)
        await message.answer("👋 <b>Приветствуем в Scriptonit Shop!</b>\n\nПрими условия:", reply_markup=agreement_kb())

@dp.callback_query(F.data == "accept_offer")
async def accept_offer(call: CallbackQuery, state: FSMContext):
    await set_agreed(call.from_user.id)
    await state.clear()
    await call.message.delete()
    await send_main_menu(call.message)
    await call.answer("Условия приняты")

@dp.callback_query(F.data == "info")
async def info(call: CallbackQuery):
    count = await get_orders_count()
    text = (
        f"📊 <b>Scriptonit Shop — Информация</b>\n\n"
        f"✅ Выполненных заказов: <b>{count}</b>\n"
        f"💰 Цена: <b>{PRODUCT_PRICE} ₽</b>\n\n"
        f"ℹ️ <b>Важно знать:</b>\n"
        f"• Скрипт автоматизирует нажатия на рынке.\n"
        f"• Мы не несём ответственности за бан вашего аккаунта — это редкий случай, но он возможен.\n"
        f"• Рекомендуем после получения скина сразу передавать его на другой аккаунт — так вы не потеряете ничего.\n"
        f"• Использование скрипта — на ваше усмотрение."
    )
    await call.message.delete()
    if os.path.exists(INFO_IMAGE):
        await call.message.answer_photo(photo=FSInputFile(INFO_IMAGE), caption=text, reply_markup=back_kb())
    else:
        await call.message.answer(text, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "faq")
async def faq(call: CallbackQuery):
    text = (
        "❓ <b>Scriptonit Shop — FAQ</b>\n\n"
        "<b>1. Как работает скрипт?</b>\n"
        "Он автоматически нажимает кнопки на рынке, чтобы поймать скин за 0.03. Это не чит, он не лезет в память игры.\n\n"
        "<b>2. Меня забанят?</b>\n"
        "Шанс маленький, но не нулевой. Мы не несём ответственности за бан, но рекомендуем ловить на одном аккаунте и сразу переводить скины на другой — так вы не потеряете ничего.\n\n"
        "<b>3. Как получить товар?</b>\n"
        "После оплаты бот выдаст скрипт и инструкцию автоматически."
    )
    await call.message.delete()
    await call.message.answer(text, reply_markup=back_kb())
    await call.answer()

@dp.callback_query(F.data == "rules")
async def rules(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
                                               [InlineKeyboardButton(text="📄 Политика", url=PRIVACY_URL)],
                                               [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]])
    await call.message.delete()
    await call.message.answer("📄 <b>Правила</b>", reply_markup=kb)
    await call.answer()

# --- ЗАРАБОТОК ---
@dp.callback_query(F.data == "earn")
async def earn(call: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    balance = await get_balance(call.from_user.id)
    invited, bought = await get_ref_stats(call.from_user.id)
    text = (
        f"💰 <b>Заработок</b>\n\n"
        f"Приглашай друзей и получай <b>{REF_REWARD} ₽</b> за каждую их покупку! 🤑\n\n"
        f"💼 Твой баланс: <b>{balance} ₽</b>\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"🛍 Из них купили: <b>{bought}</b>\n\n"
        f"🔗 <b>Твоя реферальная ссылка</b> (нажми, чтобы скопировать):\n<code>{link}</code>\n\n"
        f"💸 Вывод средств — от <b>{MIN_WITHDRAW} ₽</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Вывести средства", callback_data="withdraw")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])
    await call.message.delete()
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "withdraw")
async def withdraw(call: CallbackQuery, state: FSMContext):
    balance = await get_balance(call.from_user.id)
    if balance < MIN_WITHDRAW:
        await call.answer(f"😔 У вас меньше {MIN_WITHDRAW} ₽ на балансе\n\n💼 Сейчас: {balance} ₽", show_alert=True)
        return
    await call.message.delete()
    await call.message.answer(
        f"💳 <b>Вывод средств</b>\n\n"
        f"💼 К выводу: <b>{balance} ₽</b>\n\n"
        f"Введите номер карты, на которую выплатить деньги, и мы оформим заявку 👇",
        reply_markup=back_kb()
    )
    await state.set_state(WithdrawState.waiting_card)
    await call.answer()

@dp.message(WithdrawState.waiting_card)
async def process_withdraw(message: Message, state: FSMContext):
    card = re.sub(r"\D", "", message.text or "")
    if not 16 <= len(card) <= 19:
        await message.answer("❌ Неверный номер карты. Введите от 16 до 19 цифр.", reply_markup=back_kb())
        return
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        # списываем весь баланс сразу, чтобы нельзя было подать заявку дважды
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
        amount = (row[0] or 0) if row else 0
        if amount < MIN_WITHDRAW:
            await state.clear()
            await message.answer(f"😔 У вас меньше {MIN_WITHDRAW} ₽ на балансе", reply_markup=main_menu())
            return
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        cursor = await db.execute("INSERT INTO withdrawals (user_id, amount, card) VALUES (?, ?, ?)", (user_id, amount, card))
        w_id = cursor.lastrowid
        await db.commit()
    await state.clear()
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить выплату", callback_data=f"wd_ok_{w_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_no_{w_id}")],
    ])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на вывод #{w_id}</b>\n\n👤 @{message.from_user.username or 'без_ника'}\n🆔 <code>{user_id}</code>\n"
        f"💰 Сумма: <b>{amount} ₽</b>\n💳 Карта: <code>{card}</code>",
        reply_markup=admin_kb
    )
    await message.answer(
        f"✅ <b>Заявка на вывод оставлена!</b>\n\n💰 Сумма: <b>{amount} ₽</b>\n⏳ Ожидайте подтверждения выплаты.",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data.startswith("wd_ok_"))
async def withdraw_confirm(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    w_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE withdrawals SET status = 'paid' WHERE id = ? AND status = 'pending'", (w_id,))
        await db.commit()
        if not cursor.rowcount:
            await call.answer("Заявка уже обработана", show_alert=True); return
        async with db.execute("SELECT user_id FROM withdrawals WHERE id = ?", (w_id,)) as c:
            user_id = (await c.fetchone())[0]
    try:
        await bot.send_message(user_id, "🎉 <b>Ваши средства успешно выведены!</b>\n\n💳 Деньги придут в течение 24 часов. Спасибо, что с нами! 💚")
    except Exception as e:
        print(f"Не удалось уведомить {user_id}: {e}")
    await call.message.edit_text(call.message.text + "\n\n✅ Выплата подтверждена")
    await call.answer("Выплата подтверждена")

@dp.callback_query(F.data.startswith("wd_no_"))
async def withdraw_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    w_id = int(call.data.split("_")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ? AND status = 'pending'", (w_id,))
        if not cursor.rowcount:
            await call.answer("Заявка уже обработана", show_alert=True); return
        async with db.execute("SELECT user_id, amount FROM withdrawals WHERE id = ?", (w_id,)) as c:
            user_id, amount = await c.fetchone()
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
    try:
        await bot.send_message(user_id, f"❌ <b>Заявка на вывод отклонена.</b>\n\n💰 {amount} ₽ возвращены на баланс. Если есть вопросы — напишите в поддержку.")
    except Exception as e:
        print(f"Не удалось уведомить {user_id}: {e}")
    await call.message.edit_text(call.message.text + "\n\n❌ Отклонено, деньги возвращены на баланс")
    await call.answer("Отклонено")

@dp.callback_query(F.data.startswith("reviews"))
async def reviews(call: CallbackQuery):
    page = int(call.data.split("_")[-1]) if "_page_" in call.data else 0
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM reviews WHERE status = 'approved'") as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute("SELECT text, created_at FROM reviews WHERE status = 'approved' ORDER BY id DESC LIMIT 10 OFFSET ?", (page*10,)) as cursor:
            rows = await cursor.fetchall()
    text = "⭐ <b>Scriptonit Shop — Отзывы</b>\n\n"
    if not rows: text += "Пока нет отзывов. Стань первым!"
    else:
        for r_text, c_at in rows:
            try:
                dt = datetime.strptime(c_at.split(".")[0], "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
                fmt = dt.strftime("%d.%m.%Y %H:%M")
            except: fmt = c_at
            text += f"👤 Покупатель\n📅 {fmt} (МСК)\n💬 {r_text}\n\n"
    buttons = [[InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")]]
    if page*10+10 < total: buttons.append([InlineKeyboardButton(text="➡️ Следующие 10", callback_data=f"reviews_page_{page+1}")])
    if page > 0: buttons.append([InlineKeyboardButton(text="⬅️ Предыдущие 10", callback_data=f"reviews_page_{page-1}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    await call.message.delete()
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@dp.callback_query(F.data == "leave_review")
async def leave_review(call: CallbackQuery, state: FSMContext):
    if await has_review(call.from_user.id):
        await call.message.delete()
        await call.message.answer("❌ Ты уже оставлял отзыв. Один человек — один отзыв.", reply_markup=back_kb())
        await call.answer()
        return
    await call.message.delete()
    await call.message.answer("✍️ Напиши свой отзыв одним сообщением. Он уйдёт на проверку администратору Scriptonit Shop.", reply_markup=back_kb())
    await state.set_state(ReviewState.waiting_for_text)
    await call.answer()

@dp.message(ReviewState.waiting_for_text)
async def process_review(message: Message, state: FSMContext):
    if len(message.text.strip()) < 3:
        await message.answer("Слишком короткий отзыв. Напиши хотя бы пару слов.")
        return
    if await has_review(message.from_user.id):
        await state.clear()
        await message.answer("❌ Ты уже оставлял отзыв.", reply_markup=main_menu())
        return
    r_id = await save_review(message.from_user.id, message.from_user.username or "", message.text.strip())
    await state.clear()
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Скинуть в канал", callback_data=f"approve_{r_id}")],
                                                     [InlineKeyboardButton(text="🚫 Не скидывать", callback_data=f"reject_{r_id}")]])
    await bot.send_message(ADMIN_ID, f"📝 <b>Новый отзыв</b>\n\n👤 @{message.from_user.username or 'без_ника'}\n🆔 <code>{message.from_user.id}</code>\n\n{message.text.strip()}", reply_markup=admin_kb)
    await message.answer("✅ Спасибо! Отзыв отправлен на проверку.", reply_markup=main_menu())

@dp.callback_query(F.data.startswith("approve_"))
async def approve_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    r_id = int(call.data.split("_")[1])
    text = await get_review_text(r_id)
    try:
        await bot.send_message(REVIEWS_CHANNEL, f"⭐ <b>Новый отзыв</b>\n\n💬 {text}\n\n👤 Покупатель Scriptonit")
    except Exception as e:
        await call.answer(f"Не удалось отправить в канал: {e}", show_alert=True)
        return
    await set_review_status(r_id, "approved")
    await call.message.edit_text(call.message.text + "\n\n📢 Отправлено в канал")
    await call.answer("Отправлено в канал")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    await set_review_status(int(call.data.split("_")[1]), "rejected")
    await call.message.edit_text(call.message.text + "\n\n🚫 В канал не отправлено")
    await call.answer("Не отправлено")

@dp.callback_query(F.data == "buy_menu")
async def buy_menu(call: CallbackQuery):
    user_id = call.from_user.id
    if await has_purchased(user_id):
        await call.message.delete()
        await call.message.answer(
            "✅ <b>Вы уже купили этот товар в Scriptonit Shop!</b>\n\n"
            "Файлы можно получить только один раз. Если вы потеряли файлы — обратитесь в поддержку.",
            reply_markup=main_menu()
        )
        await call.answer()
        return
    text = (
        f"🛒 <b>Scriptonit Shop — Покупка скрипта</b>\n\n"
        f"💰 Цена: <b>{PRODUCT_PRICE} ₽</b>\n\n"
        f"ℹ️ <b>Что важно знать:</b>\n"
        f"• Скрипт автоматизирует нажатия, это не чит.\n"
        f"• Бан возможен, но шанс маленький.\n"
        f"• После получения скина желательно сразу передать его на другой аккаунт — так вы не потеряете ничего.\n"
        f"• Мы не несём ответственности за бан вашего аккаунта.\n\n"
        f"Если всё устраивает — нажми «Оплатить»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", callback_data="pay_start")],
                                               [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]])
    await call.message.delete()
    if os.path.exists(BUY_IMAGE):
        await call.message.answer_photo(photo=FSInputFile(BUY_IMAGE), caption=text, reply_markup=kb)
    else:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

# --- ОПЛАТА (С ФОНОВОЙ ПРОВЕРКОЙ) ---
@dp.callback_query(F.data == "pay_start")
async def pay_start(call: CallbackQuery):
    user_id = call.from_user.id
    
    if await has_purchased(user_id):
        await call.message.delete()
        await call.message.answer("✅ Вы уже купили этот товар в Scriptonit Shop!", reply_markup=main_menu())
        await call.answer()
        return

    try:
        payment = rolly_client.payments.create(
            amount=str(PRODUCT_PRICE),
            order_id=f"order_{user_id}_{int(asyncio.get_event_loop().time())}",
            payment_method="sbp",
            description="Скрипт для ловли скинов",
            customer_id=str(user_id)
        )
        pay_url = payment.get('pay_url')
        payment_id = payment.get('payment_id')

        await save_payment(user_id, payment_id)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{payment_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
        ])
        await call.message.delete()
        await call.message.answer(
            f"💳 <b>Оплата</b>\n\nСумма: <b>{PRODUCT_PRICE} ₽</b>\n\n"
            f"После оплаты бот выдаст файлы автоматически (в течение 5-10 секунд) или нажми «Я оплатил».",
            reply_markup=kb
        )

        asyncio.create_task(check_payment_background(bot, user_id, payment_id))

    except Exception as e:
        print(f"Ошибка RollyPay: {e}")
        await call.message.delete()
        await call.message.answer(f"❌ Ошибка создания платежа: {e}", reply_markup=back_kb())
    await call.answer()

# --- ФОНОВАЯ ПРОВЕРКА ОПЛАТЫ ---
async def check_payment_background(bot: Bot, user_id: int, payment_id: str):
    for _ in range(180):
        await asyncio.sleep(5)
        
        if await has_purchased(user_id):
            return
            
        try:
            payment = rolly_client.payments.get(payment_id)
            status = payment.get('status')
            
            if status in ['succeeded', 'paid']:
                await save_purchase(user_id)
                await reward_referrer(user_id)
                await increment_orders()
                await set_payment_status(payment_id, "succeeded")
                
                await bot.send_message(
                    user_id,
                    "✅ <b>Оплата прошла!</b>\n\n"
                    "Спасибо за покупку в <b>Scriptonit Shop</b>! Вот твои файлы! Обязательно сохрани их, "
                    "после обновления скрипта их нельзя будет получить повторно!"
                )
                
                class _Dest:
                    async def answer(self_, *a, **kw): return await bot.send_message(user_id, *a, **kw)
                    async def answer_document(self_, *a, **kw): return await bot.send_document(user_id, *a, **kw)
                    async def answer_video(self_, *a, **kw): return await bot.send_video(user_id, *a, **kw)
                    async def answer_photo(self_, *a, **kw): return await bot.send_photo(user_id, *a, **kw)

                await send_product_files(_Dest(), user_id)
                return
                
        except Exception as e:
            print(f"Ошибка фоновой проверки {payment_id}: {e}")

# --- РУЧНАЯ ПРОВЕРКА ---
@dp.callback_query(F.data.startswith("check_"))
async def check_payment(call: CallbackQuery):
    payment_id, user_id = call.data.split("_")[1], call.from_user.id
    try:
        payment = rolly_client.payments.get(payment_id)
        status = payment.get('status')
        
        if status in ['succeeded', 'paid']:
            if await has_purchased(user_id): 
                await call.answer("Вы уже получили товар", show_alert=True)
                return
            
            await save_purchase(user_id)
            await reward_referrer(user_id)
            await increment_orders()
            await set_payment_status(payment_id, "succeeded")
            
            await call.message.delete()
            await call.message.answer(
                "✅ <b>Оплата прошла!</b>\n\n"
                "Спасибо за покупку в <b>Scriptonit Shop</b>! Вот твои файлы! Обязательно сохрани их, "
                "после обновления скрипта их нельзя будет получить повторно!"
            )
            await send_product_files(call.message, user_id)
            
        else:
            await call.answer("❌ Вы не оплатили. Если вы уверены, что деньги списались, подождите 1-2 минуты. Бот выдаст файлы автоматически.", show_alert=True)
            
    except Exception as e:
        await call.answer(f"❌ Ошибка проверки: {e}", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await send_main_menu(call.message)
    await call.answer()

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())