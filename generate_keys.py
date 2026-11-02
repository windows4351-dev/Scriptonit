"""
Разовый скрипт: добавляет в базу новые ключи активации.
Запускать на сервере, где лежит shop.db, рядом с ботом:

    python generate_keys.py 100000

Повторный запуск безопасен — ключи уникальны, дубликаты не создаются.
"""
import asyncio
import sys
from keys import init_keys_table, generate_keys, free_keys_left


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    await init_keys_table()
    added = await generate_keys(n)
    left = await free_keys_left()
    print(f"Добавлено ключей: {added}")
    print(f"Свободных ключей сейчас: {left}")


if __name__ == "__main__":
    asyncio.run(main())
