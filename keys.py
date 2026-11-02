"""
Модуль ключей активации Scriptonit Shop.
Хранится в той же базе, что и бот (shop.db), в отдельной таблице keys.

Жизненный цикл ключа:
  free    -> ещё не продан
  issued  -> выдан покупателю после оплаты, но ни разу не активирован в приложении
  active  -> активирован в приложении, привязан к конкретному device_id навсегда
"""
import secrets
import string
import aiosqlite

DB_PATH = "shop.db"
ALPHABET = string.ascii_uppercase + string.digits  # без похожих друг на друга символов ниже
ALPHABET = ALPHABET.translate({ord(c): None for c in "O0I1"})


def _make_key() -> str:
    part = lambda: "".join(secrets.choice(ALPHABET) for _ in range(4))
    return f"SCRPT-{part()}-{part()}-{part()}"


async def init_keys_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'free',
            user_id INTEGER,
            device_id TEXT,
            issued_at TIMESTAMP,
            activated_at TIMESTAMP
        )""")
        await db.commit()


async def generate_keys(n: int) -> int:
    """Досыпает n новых уникальных ключей со статусом free. Возвращает, сколько реально добавлено."""
    added = 0
    async with aiosqlite.connect(DB_PATH) as db:
        while added < n:
            batch = [(_make_key(),) for _ in range(min(1000, n - added))]
            try:
                await db.executemany("INSERT OR IGNORE INTO keys (key) VALUES (?)", batch)
                await db.commit()
                added += len(batch)
            except Exception:
                await db.rollback()
                raise
    return added


async def free_keys_left() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM keys WHERE status='free'") as cur:
            return (await cur.fetchone())[0]


async def issue_key_for_user(user_id: int) -> str | None:
    """Берёт один свободный ключ, привязывает к покупателю (ещё не к устройству) и возвращает его."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key FROM keys WHERE status='free' LIMIT 1") as cur:
            row = await cur.fetchone()
        if not row:
            return None
        key = row[0]
        await db.execute(
            "UPDATE keys SET status='issued', user_id=?, issued_at=CURRENT_TIMESTAMP WHERE key=? AND status='free'",
            (user_id, key),
        )
        await db.commit()
        return key


async def key_info(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT key, status, user_id, device_id FROM keys WHERE key=?", (key,)
        ) as cur:
            return await cur.fetchone()


async def activate(key: str, device_id: str) -> tuple[bool, str]:
    """
    Пытается активировать ключ на устройстве device_id.
    Правила: ключ должен существовать и быть issued/active;
    если уже active — device_id должен совпадать с тем, что сохранён (иначе отказ: "уже использован").
    """
    row = await key_info(key)
    if row is None:
        return False, "Ключ не найден"
    _, status, user_id, bound_device = row
    if status == "free":
        return False, "Ключ ещё не оплачен"
    if status == "issued":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE keys SET status='active', device_id=?, activated_at=CURRENT_TIMESTAMP "
                "WHERE key=? AND status='issued'",
                (device_id, key),
            )
            await db.commit()
        return True, "Активировано"
    if status == "active":
        if bound_device == device_id:
            return True, "Уже активно на этом устройстве"
        return False, "Ключ уже использован на другом устройстве"
    return False, "Неизвестный статус ключа"


async def verify(key: str, device_id: str) -> bool:
    row = await key_info(key)
    if row is None:
        return False
    _, status, _, bound_device = row
    return status == "active" and bound_device == device_id
