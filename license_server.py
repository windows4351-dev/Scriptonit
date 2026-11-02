"""
Сервер активации ключей Scriptonit Shop. Отдельный процесс, работает рядом с ботом
и использует ту же базу (shop.db). Приложение на телефоне обращается сюда, чтобы
активировать ключ (один раз, привязка к устройству) и затем периодически проверять,
что ключ всё ещё действителен.

Запуск:
    pip install aiohttp aiosqlite
    python license_server.py            # слушает 0.0.0.0:8787

Это голый HTTP — в продакшене обязательно поставь перед ним reverse-proxy с HTTPS
(Caddy — проще всего, сам получает сертификат):

    scriptonit.example.com {
        reverse_proxy 127.0.0.1:8787
    }

Приложению нужен адрес ВИДА https://scriptonit.example.com — без своего домена
активация по интернету невозможна (IP и порт на телефоне не настроишь на HTTPS).
"""
from aiohttp import web
import keys as K


async def activate(request: web.Request) -> web.Response:
    data = await request.json()
    key = str(data.get("key", "")).strip().upper()
    device_id = str(data.get("device_id", "")).strip()
    if not key or not device_id:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    ok, msg = await K.activate(key, device_id)
    return web.json_response({"ok": ok, "message": msg})


async def verify(request: web.Request) -> web.Response:
    data = await request.json()
    key = str(data.get("key", "")).strip().upper()
    device_id = str(data.get("device_id", "")).strip()
    ok = await K.verify(key, device_id) if key and device_id else False
    return web.json_response({"ok": ok})


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.post("/activate", activate),
        web.post("/verify", verify),
    ])
    return app


async def on_startup(app):
    await K.init_keys_table()


if __name__ == "__main__":
    app = build_app()
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=8787)
