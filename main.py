import asyncio
import aiosqlite
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from config import *

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_PATH = "shop.db"

class AgreementState(StatesGroup):
    waiting_accept = State()

class ReviewState(StatesGroup):
    waiting_for_text = State()

# --- БАЗА ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            agreed INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            completed_orders INTEGER DEFAULT 16
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            text TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_version TEXT,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("INSERT OR IGNORE INTO stats (id, completed_orders) VALUES (1, 16)")
        await db.commit()

async def save_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                         (user_id, username, first_name))
        await db.commit()

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

async def has_purchased(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM purchases WHERE user_id = ? LIMIT 1", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def save_purchase(user_id: int, version: str = "1.0"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases (user_id, product_version) VALUES (?, ?)", (user_id, version))
        await db.commit()

# --- ВЫДАЧА ФАЙЛОВ ---
async def send_product_files(message: Message):
    script_path = "files/script.zip"
    video_path = "files/instruction.mp4"
    
    if os.path.exists(script_path):
        await message.answer_document(
            FSInputFile(script_path),
            caption="📦 <b>Твой скрипт</b>\n\nСкачай и распакуй. Инструкция ниже."
        )
    else:
        await message.answer("⚠️ Файл скрипта не найден. Обратись в поддержку.")
    
    if os.path.exists(video_path):
        await message.answer_video(
            FSInputFile(video_path),
            caption="🎬 <b>Видеоинструкция</b>\n\nПосмотри перед использованием."
        )
    else:
        await message.answer("⚠️ Видеоинструкция не найдена. Обратись в поддержку.")

# --- КЛАВИАТУРЫ ---
def agreement_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_offer")],
        [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        [InlineKeyboardButton(text="📄 Правила пользования", url=PRIVACY_URL)],
    ])

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить скрипт", callback_data="buy_menu")],
        [InlineKeyboardButton(text="📊 Информация", callback_data="info")],
        [InlineKeyboardButton(text="❓ FAQ", callback_data="faq")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="📢 Наш канал", url=TG_CHANNEL_URL)],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton(text="📄 Правила пользования", callback_data="rules")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])

async def send_main_menu(message: Message, edit=False):
    text = "🏠 <b>Добро пожаловать в Scriptonit Shop</b>\n\nВыбери действие:"
    if os.path.exists(MAIN_IMAGE):
        if edit:
            await message.delete()
        await message.answer_photo(photo=FSInputFile(MAIN_IMAGE), caption=text, reply_markup=main_menu())
    else:
        if edit:
            await message.edit_text(text, reply_markup=main_menu())
        else:
            await message.answer(text, reply_markup=main_menu())

# --- СТАРТ ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await init_db()
    await save_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    if await is_agreed(message.from_user.id):
        await send_main_menu(message)
    else:
        await state.set_state(AgreementState.waiting_accept)
        await message.answer(
            "👋 <b>Приветствуем тебя в Scriptonit Shop!</b>\n\n"
            "Перед использованием бота, пожалуйста, ознакомься с офертой и правилами пользования, затем прими условия.",
            reply_markup=agreement_kb()
        )

@dp.callback_query(F.data == "accept_offer")
async def accept_offer(call: CallbackQuery, state: FSMContext):
    await set_agreed(call.from_user.id)
    await state.clear()
    await call.message.delete()
    await send_main_menu(call.message, edit=False)
    await call.answer("Условия приняты")

# --- ИНФО ---
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

# --- FAQ ---
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

# --- ПРАВИЛА ---
@dp.callback_query(F.data == "rules")
async def rules(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        [InlineKeyboardButton(text="📄 Политика конфиденциальности", url=PRIVACY_URL)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])
    await call.message.delete()
    await call.message.answer("📄 <b>Scriptonit Shop — Правила пользования</b>", reply_markup=kb)
    await call.answer()

# --- ОТЗЫВЫ ---
@dp.callback_query(F.data.startswith("reviews"))
async def reviews(call: CallbackQuery):
    page = 0
    if "_page_" in call.data:
        page = int(call.data.split("_")[-1])
    
    limit = 10
    offset = page * limit
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM reviews WHERE status = 'approved'") as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT text, created_at FROM reviews WHERE status = 'approved' ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        text = "⭐ <b>Scriptonit Shop — Отзывы</b>\n\nПока отзывов нет. Стань первым!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
        ])
    else:
        text = "⭐ <b>Scriptonit Shop — Отзывы</b>\n\n"
        for review_text, created_at in rows:
            try:
                dt = datetime.strptime(created_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                msk = dt + timedelta(hours=3)
                formatted = msk.strftime("%d.%m.%Y %H:%M")
            except Exception:
                formatted = created_at
            text += f"👤 Покупатель\n📅 {formatted} (МСК)\n💬 {review_text}\n\n"
        
        buttons = [[InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")]]
        if offset + limit < total:
            buttons.append([InlineKeyboardButton(text="➡️ Следующие 10", callback_data=f"reviews_page_{page + 1}")])
        if page > 0:
            buttons.append([InlineKeyboardButton(text="⬅️ Предыдущие 10", callback_data=f"reviews_page_{page - 1}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await call.message.delete()
    await call.message.answer(text, reply_markup=kb)
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
    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком короткий отзыв. Напиши хотя бы пару слов.")
        return
    if await has_review(message.from_user.id):
        await state.clear()
        await message.answer("❌ Ты уже оставлял отзыв.", reply_markup=main_menu())
        return
    review_id = await save_review(message.from_user.id, message.from_user.username or "", text)
    await state.clear()
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{review_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{review_id}")],
    ])
    await bot.send_message(ADMIN_ID, f"📝 <b>Новый отзыв</b>\n\n👤 @{message.from_user.username or 'без_ника'}\n🆔 <code>{message.from_user.id}</code>\n\n{text}", reply_markup=admin_kb)
    await message.answer("✅ Спасибо! Отзыв отправлен на проверку.", reply_markup=main_menu())

@dp.callback_query(F.data.startswith("approve_"))
async def approve_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    review_id = int(call.data.split("_")[1])
    await set_review_status(review_id, "approved")
    await call.message.edit_text(call.message.text + "\n\n✅ Одобрено")
    await call.answer("Одобрено")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    review_id = int(call.data.split("_")[1])
    await set_review_status(review_id, "rejected")
    await call.message.edit_text(call.message.text + "\n\n❌ Отклонено")
    await call.answer("Отклонено")

# --- ПОКУПКА ---
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", callback_data="pay_stub")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])
    await call.message.delete()
    if os.path.exists(BUY_IMAGE):
        await call.message.answer_photo(photo=FSInputFile(BUY_IMAGE), caption=text, reply_markup=kb)
    else:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "pay_stub")
async def pay_stub(call: CallbackQuery):
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
    
    await save_purchase(user_id, version="1.0")
    await increment_orders()
    
    await call.message.delete()
    await call.message.answer(
        "✅ <b>Оплата прошла!</b>\n\n"
        "Спасибо за покупку в <b>Scriptonit Shop</b>! Вот твои файлы! Обязательно сохрани их, "
        "после обновления скрипта их нельзя будет получить повторно!"
    )
    await send_product_files(call.message)
    await call.message.answer("Главное меню:", reply_markup=main_menu())
    await call.answer()

# --- НАЗАД (ПОСЛЕДНИМ) ---
@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await send_main_menu(call.message, edit=False)
    await call.answer()

# --- ЗАПУСК ---
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())