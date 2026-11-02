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
        await db.execute("INSERT OR IGNORE INTO stats (id, completed_orders) VALUES (1, 16)")
        await db.commit()
    await init_keys_table()

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
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
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
async def start(message: Message, state: FSMContext):
    await init_db()
    await save_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
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
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{r_id}")],
                                                     [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{r_id}")]])
    await bot.send_message(ADMIN_ID, f"📝 <b>Новый отзыв</b>\n\n👤 @{message.from_user.username or 'без_ника'}\n🆔 <code>{message.from_user.id}</code>\n\n{message.text.strip()}", reply_markup=admin_kb)
    await message.answer("✅ Спасибо! Отзыв отправлен на проверку.", reply_markup=main_menu())

@dp.callback_query(F.data.startswith("approve_"))
async def approve_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    await set_review_status(int(call.data.split("_")[1]), "approved")
    await call.message.edit_text(call.message.text + "\n\n✅ Одобрено")
    await call.answer("Одобрено")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_review(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: await call.answer("Нет доступа", show_alert=True); return
    await set_review_status(int(call.data.split("_")[1]), "rejected")
    await call.message.edit_text(call.message.text + "\n\n❌ Отклонено")
    await call.answer("Отклонено")

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