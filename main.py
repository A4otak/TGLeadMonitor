"""TGLeadMonitor — точка входа.

1. Если .env не заполнен — интерактивный мастер настройки.
2. Если нет авторизованной сессии — выбор способа входа юзербота в консоли:
   [1] QR-код, [2] номер телефона. Сессия сохраняется в data/userbot.session.
3. Одновременный запуск Telethon (прослушка + очередь вступления) и aiogram (бот управления).
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import re
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
)

from bot.handlers import AdminMiddleware, router
from config import DATA_DIR, DB_PATH, ENV_PATH, SESSION_PATH, Config, load_config
from database.db import Database
from userbot.monitor import Monitor

log = logging.getLogger("tgleadmonitor")

QR_WAIT_SECONDS = 30      # сколько ждём сканирования одного QR-кода, затем он перевыпускается
QR_MAX_ATTEMPTS = 10      # максимум перевыпусков QR-кода за один заход (10 × 30 сек = 5 минут)
CODE_ATTEMPTS = 3         # попыток ввода кода из Telegram
PASSWORD_ATTEMPTS = 3     # попыток ввода облачного пароля (2FA)


def ask(prompt: str, pattern: str, error: str) -> str:
    while True:
        value = input(prompt).strip()
        if re.fullmatch(pattern, value):
            return value
        print(f"  ✖ {error}")


def setup_wizard() -> Config:
    print("=" * 60)
    print(" TGLeadMonitor — первичная настройка")
    print("=" * 60)
    print("Нужны: токен бота (@BotFather), ваш Telegram ID (@userinfobot)")
    print("и API ID / API Hash с https://my.telegram.org -> API development tools\n")
    token = ask("BOT_TOKEN: ", r"\d{6,12}:[A-Za-z0-9_-]{30,}", "Токен выглядит как 123456789:AAH... (проверьте, что скопировали полностью)")
    admin_id = ask("ADMIN_ID (ваш числовой ID): ", r"\d{3,15}", "ID — только цифры")
    api_id = ask("API_ID: ", r"\d{3,12}", "API_ID — только цифры")
    api_hash = ask("API_HASH: ", r"[0-9a-fA-F]{32}", "API_HASH — 32 символа (0-9, a-f)")
    ENV_PATH.write_text(
        f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\nAPI_ID={api_id}\nAPI_HASH={api_hash}\n",
        encoding="utf-8",
    )
    print(f"\n✔ Настройки сохранены в {ENV_PATH.name}\n")
    cfg = load_config()
    if cfg is None:
        sys.exit("Не удалось прочитать .env после сохранения")
    return cfg


# ---------------------------------------------------------------------------
# Авторизация юзербота (только если нет авторизованной сессии)
# ---------------------------------------------------------------------------
async def ainput(prompt: str) -> str:
    """input() в отдельном потоке, чтобы соединение Telethon не «замирало» на время ввода."""
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        sys.exit("Ввод недоступен (stdin закрыт). Запустите программу в интерактивной консоли.")


def render_qr(url: str) -> None:
    """Рисует QR-код в консоли пакетом qrcode; при неудаче печатает ссылку для входа."""
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except (ImportError, UnicodeEncodeError):
        print("Не удалось нарисовать QR-код в консоли (проверьте пакет qrcode и кодировку UTF-8).")
        print(f"Ссылка для входа (действует недолго): {url}")


async def ask_cloud_password(client: TelegramClient) -> bool:
    """Запрашивает облачный пароль (2FA) после SessionPasswordNeededError."""
    print("\nНа аккаунте включена двухфакторная защита.")
    for attempt in range(1, PASSWORD_ATTEMPTS + 1):
        password = await asyncio.to_thread(getpass.getpass, "Облачный пароль (2FA, ввод скрыт): ")
        try:
            await client.sign_in(password=password)
            return True
        except PasswordHashInvalidError:
            print(f"  ✖ Неверный пароль (попытка {attempt} из {PASSWORD_ATTEMPTS}).")
        except FloodWaitError as exc:
            print(f"  ✖ Слишком много попыток: подождите {exc.seconds} сек. и повторите вход.")
            return False
    return False


async def login_qr(client: TelegramClient) -> bool:
    """Вход по QR-коду: перевыпуск при истечении, запрос 2FA при необходимости."""
    print("\nОтсканируйте QR-код в Telegram на телефоне:")
    print("Настройки -> Устройства -> Подключить устройство\n")
    try:
        qr_login = await client.qr_login()
    except FloodWaitError as exc:
        print(f"  ✖ Telegram просит подождать {exc.seconds} сек. перед новой попыткой входа.")
        return False
    for attempt in range(1, QR_MAX_ATTEMPTS + 1):
        render_qr(qr_login.url)
        print(f"QR-код действует около {QR_WAIT_SECONDS} сек. (попытка {attempt} из {QR_MAX_ATTEMPTS})")
        try:
            await qr_login.wait(timeout=QR_WAIT_SECONDS)
            return True
        except SessionPasswordNeededError:
            return await ask_cloud_password(client)
        except (asyncio.TimeoutError, TimeoutError):
            if attempt == QR_MAX_ATTEMPTS:
                break
            print("\nQR-код истёк — выпускаю новый...\n")
            try:
                await qr_login.recreate()
            except FloodWaitError as exc:
                print(f"  ✖ Telegram просит подождать {exc.seconds} сек. перед новой попыткой входа.")
                return False
    print("QR-код так и не был отсканирован.")
    return False


async def login_phone(client: TelegramClient) -> bool:
    """Вход по номеру телефона: код из Telegram, затем 2FA при необходимости."""
    while True:
        raw = (await ainput("\nНомер телефона с кодом страны, например +380501234567 (пусто — назад): ")).strip()
        if not raw:
            return False
        phone = re.sub(r"[\s()\-]", "", raw)
        if not re.fullmatch(r"\+?\d{7,15}", phone):
            print("  ✖ Номер должен состоять из 7–15 цифр, допускается ведущий «+».")
            continue
        if not phone.startswith("+"):
            phone = "+" + phone
        try:
            sent = await client.send_code_request(phone)
            break
        except PhoneNumberInvalidError:
            print("  ✖ Telegram не принял этот номер. Проверьте его и повторите.")
        except PhoneNumberBannedError:
            print("  ✖ Этот номер заблокирован в Telegram.")
            return False
        except (FloodWaitError, PhoneNumberFloodError) as exc:
            seconds = getattr(exc, "seconds", None)
            wait = f"подождите {seconds} сек. и повторите" if seconds else "повторите позже"
            print(f"  ✖ Слишком много запросов кода: {wait}.")
            return False
        except ApiIdInvalidError:
            print("  ✖ Неверные API_ID / API_HASH. Удалите .env и введите данные заново.")
            return False

    print("Код отправлен в Telegram (или SMS). Если он не пришёл — проверьте приложение Telegram на другом устройстве.")
    for attempt in range(1, CODE_ATTEMPTS + 1):
        code = re.sub(r"\D", "", await ainput("Код из Telegram: "))
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            return True
        except SessionPasswordNeededError:
            return await ask_cloud_password(client)
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            print(f"  ✖ Неверный код (попытка {attempt} из {CODE_ATTEMPTS}).")
        except PhoneCodeExpiredError:
            print(f"  ✖ Код устарел (попытка {attempt} из {CODE_ATTEMPTS}). Запрашиваю новый...")
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                print(f"  ✖ Слишком много запросов кода: подождите {exc.seconds} сек. и повторите.")
                return False
        except PhoneNumberUnoccupiedError:
            print("  ✖ Этот номер не зарегистрирован в Telegram.")
            return False
        except FloodWaitError as exc:
            print(f"  ✖ Слишком много попыток: подождите {exc.seconds} сек. и повторите вход.")
            return False
    print("Исчерпаны попытки ввода кода.")
    return False


async def authorize(client: TelegramClient) -> None:
    """Подключается и, если сессия не авторизована, предлагает выбрать способ входа."""
    await client.connect()
    if await client.is_user_authorized():
        return
    print("\nЮзербот не авторизован. Выберите способ входа:")
    print("  [1] Вход по QR-коду")
    print("  [2] Вход по номеру телефона")
    while True:
        choice = (await ainput("Ваш выбор [1/2]: ")).strip()
        if choice == "1":
            success = await login_qr(client)
        elif choice == "2":
            success = await login_phone(client)
        else:
            print("  ✖ Введите 1 или 2.")
            continue
        if success and await client.is_user_authorized():
            print("\n✔ Авторизация выполнена, сессия сохранена в data/userbot.session\n")
            return
        print("\nВход не выполнен. Выберите способ входа ещё раз (Ctrl+C — выход).")


async def run(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = Database(DB_PATH)
    await db.init()

    # Параметры устройства/приложения не переопределяются — используются значения Telethon по умолчанию
    client = TelegramClient(str(SESSION_PATH), cfg.api_id, cfg.api_hash)
    await authorize(client)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    monitor = Monitor(client, db, bot, cfg.admin_id)
    await monitor.setup()

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AdminMiddleware(cfg.admin_id))
    dp.callback_query.outer_middleware(AdminMiddleware(cfg.admin_id))
    dp.include_router(router)
    dp["db"] = db
    dp["monitor"] = monitor

    print(f"✔ Юзербот: {monitor.me_name}")
    try:
        me = await bot.get_me()
        print(f"✔ Бот управления: @{me.username}")
        print("Откройте бота в Telegram и отправьте /start. Остановка — Ctrl+C.\n")
    except TelegramAPIError as exc:
        sys.exit(f"Неверный BOT_TOKEN или нет доступа к Telegram: {exc}")
    try:
        await bot.send_message(cfg.admin_id, "✅ TGLeadMonitor запущен. Нажмите /start, чтобы открыть меню.",
                               disable_notification=True)
    except TelegramAPIError:
        print("⚠ Не удалось написать администратору: откройте бота и нажмите /start вручную.")

    try:
        await asyncio.gather(
            client.run_until_disconnected(),
            dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types()),
            monitor.join_worker(),
        )
    finally:
        await bot.session.close()
        await client.disconnect()
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg = load_config() or setup_wizard()
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
