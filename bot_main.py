import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Поддержка всех версий aiogram 3.x на BotHost
try:
    from aiogram.client.default import DefaultBotProperties
    HAS_DEFAULT_PROPS = True
except ImportError:
    HAS_DEFAULT_PROPS = False


# ==============================================================================
# 1. НАСТРОЙКИ БОТА (МОЖНО ЗАДАТЬ ЗДЕСЬ ИЛИ В ПЕРЕМЕННЫХ ОКРУЖЕНИЯ BOTHOST)
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ВАШ_ДОМЕН.bothost.ru")

# Укажите ваш числовой Telegram ID (узнать можно командой /myid в самом боте).
# Если оставить список пустым [], первым админом станет тот, кто первый напишет /admin
RAW_ADMIN_IDS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in RAW_ADMIN_IDS.split(",") if x.strip().isdigit()
}

# Путь к базе данных SQLite (создастся автоматически при запуске)
DB_PATH = Path(__file__).parent / "experiment.db"
INDEX_HTML_PATH = Path(__file__).parent / "index.html"
HTTP_PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("NeuroFeedBot")


# ==============================================================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLITE)
# ==============================================================================
def init_db() -> None:
    """Создаёт таблицы для хранения конспектов участников и прав администратора."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER,
                tg_username TEXT,
                participant_name TEXT NOT NULL,
                group_num INTEGER NOT NULL,
                actual_sec REAL DEFAULT 0,
                perceived_sec REAL DEFAULT 0,
                time_error_sec REAL DEFAULT 0,
                time_error_pct REAL DEFAULT 0,
                counting_answer TEXT DEFAULT '',
                free_recall_text TEXT DEFAULT '',
                hit_rate_pct INTEGER DEFAULT 0,
                viewed_count INTEGER DEFAULT 0,
                summary_text TEXT NOT NULL,
                watched_json TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        for uid in ADMIN_IDS:
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
        conn.commit()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь исследователем (админом)."""
    if user_id in ADMIN_IDS:
        return True
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def register_admin(user_id: int) -> None:
    """Добавляет Telegram ID в список исследователей."""
    ADMIN_IDS.add(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()


def get_all_admin_ids() -> list[int]:
    """Возвращает все Telegram ID администраторов для рассылки конспектов."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        ids = {r[0] for r in rows} | ADMIN_IDS
        return list(ids)


def save_session_record(
    tg_user_id: int | None,
    tg_username: str,
    payload: dict,
) -> int:
    """Сохраняет прохождение эксперимента и готовый конспект в SQLite."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    participant_name = str(payload.get("participant") or tg_username or "Участник")
    group_num = int(payload.get("group") or 1)
    actual_sec = float(payload.get("actualSec") or 0)
    perceived_sec = float(payload.get("perceivedSec") or 0)
    time_error_sec = float(
        payload.get("timeErrorSec") or round(perceived_sec - actual_sec, 1)
    )
    time_error_pct = float(payload.get("timeErrorPct") or 0)
    counting_answer = str(payload.get("countingAnswer") or "")
    free_recall_text = str(payload.get("freeRecallText") or "")
    hit_rate_pct = int(payload.get("hitRatePct") or 0)
    viewed_count = int(payload.get("viewedItemsCount") or 0)
    summary_text = str(payload.get("summaryText") or "").strip()
    watched_json = json.dumps(payload.get("watchedLog") or [], ensure_ascii=False)

    if not summary_text:
        summary_text = (
            f"📋 КОНСПЕКТ: {participant_name} — {group_num} группа\n"
            f"⏱ Факт: {actual_sec}с | Оценка: {perceived_sec}с ({time_error_pct}%)\n"
            f"🧠 Узнавание: {hit_rate_pct}% | Текст: {free_recall_text or 'нет'}"
        )

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO sessions (
                tg_user_id, tg_username, participant_name, group_num,
                actual_sec, perceived_sec, time_error_sec, time_error_pct,
                counting_answer, free_recall_text, hit_rate_pct, viewed_count,
                summary_text, watched_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tg_user_id,
                tg_username,
                participant_name,
                group_num,
                actual_sec,
                perceived_sec,
                time_error_sec,
                time_error_pct,
                counting_answer,
                free_recall_text,
                hit_rate_pct,
                viewed_count,
                summary_text,
                watched_json,
                now_str,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def fetch_recent_sessions(limit: int = 5) -> list[sqlite3.Row]:
    """Возвращает последние N конспектов."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def fetch_all_sessions() -> list[sqlite3.Row]:
    """Возвращает все записи для аналитики и выгрузки в CSV."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM sessions ORDER BY id ASC").fetchall()


def clear_sessions_db() -> None:
    """Очищает таблицу результатов."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sessions")
        conn.commit()


# ==============================================================================
# 3. ИНИЦИАЛИЗАЦИЯ AIOGRAM 3
# ==============================================================================
if HAS_DEFAULT_PROPS:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
else:
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)

dp = Dispatcher()


def build_main_keyboard(user_is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главная клавиатура: кнопка запуска Mini App + панель исследователя для админа."""
    rows = [
        [
            KeyboardButton(
                text="📱 Открыть ленту",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]
    ]
    if user_is_admin:
        rows.append(
            [
                KeyboardButton(text="📋 Конспекты"),
                KeyboardButton(text="📊 По группам"),
                KeyboardButton(text="📥 CSV"),
            ]
        )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_admin_inline_kb() -> InlineKeyboardMarkup:
    """Инлайн-кнопки управления результатами для исследователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Последние 5 конспектов", callback_data="adm_recent"
                ),
                InlineKeyboardButton(
                    text="📊 Сводка по 4 группам", callback_data="adm_groups"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📥 Скачать таблицу CSV", callback_data="adm_csv"
                ),
                InlineKeyboardButton(
                    text="🗑 Очистить базу", callback_data="adm_clear_confirm"
                ),
            ],
        ]
    )


# ==============================================================================
# 4. ОБРАБОТЧИКИ КОМАНД И MINI APP (WEB_APP_DATA)
# ==============================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    user = message.from_user
    admin_flag = is_admin(user.id) if user else False

    # Если админы ещё не заданы ни в коде, ни в БД — автоматически назначаем первого вошедшего
    if user and not get_all_admin_ids():
        register_admin(user.id)
        admin_flag = True

    text = (
        "Нажмите <b>«📱 Открыть ленту»</b> ниже, чтобы запустить эксперимент.\n\n"
        "После просмотра ленты и короткого опроса ответы сохранятся автоматически."
    )
    if admin_flag:
        text += "\n\n<i>Вы вошли как исследователь (доступны конспекты и выгрузка CSV).</i>"

    await message.answer(text, reply_markup=build_main_keyboard(admin_flag))


@dp.message(Command("myid"))
async def cmd_myid(message: types.Message) -> None:
    if not message.from_user:
        return
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        "Его можно указать в переменной <code>ADMIN_IDS</code> на BotHost."
    )


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if not get_all_admin_ids():
        register_admin(uid)

    if not is_admin(uid):
        await message.answer(
            f"Нет доступа. Ваш ID: <code>{uid}</code> (добавьте его в ADMIN_IDS)."
        )
        return

    rows = fetch_all_sessions()
    await message.answer(
        f"<b>Панель исследователя</b>\n"
        f"Всего записано участников: <b>{len(rows)}</b>\n"
        f"URL Mini App: <code>{WEBAPP_URL}</code>",
        reply_markup=build_admin_inline_kb(),
    )


@dp.message(F.web_app_data)
async def handle_webapp_data(message: types.Message) -> None:
    """
    Принимает данные из Telegram.WebApp.sendData() после завершения опроса,
    записывает их в SQLite и пересылает готовый конспект исследователю.
    """
    if not message.web_app_data:
        return

    raw_data = message.web_app_data.data
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        payload = {"summaryText": raw_data}

    user = message.from_user
    uid = user.id if user else None
    username = f"@{user.username}" if (user and user.username) else (user.full_name if user else "")

    record_id = save_session_record(uid, username, payload)
    summary_text = str(payload.get("summaryText") or "").strip()

    await message.answer(
        "Спасибо! Ваши ответы записаны. Можете закрыть бота или передать устройство следующему участнику."
    )

    # Отправляем готовый конспект всем администраторам
    admin_notification = (
        f"<b>Новая запись #{record_id}</b> ({username or 'без username'})\n\n"
        f"{summary_text}"
    )
    for admin_id in get_all_admin_ids():
        try:
            await bot.send_message(admin_id, admin_notification)
        except Exception as e:
            logger.warning("Не удалось отправить конспект админу %s: %s", admin_id, e)


# ==============================================================================
# 5. ПРОСМОТР КОНСПЕКТОВ, СТАТИСТИКИ ПО 4 ГРУППАМ И ЭКСПОРТ CSV
# ==============================================================================
async def send_recent_summaries(target_chat_id: int) -> None:
    rows = fetch_recent_sessions(limit=5)
    if not rows:
        await bot.send_message(target_chat_id, "Конспектов пока нет.")
        return

    for r in reversed(rows):
        header = f"<b>#{r['id']}</b> • {r['created_at']}\n"
        await bot.send_message(target_chat_id, header + r["summary_text"])


async def send_group_stats(target_chat_id: int) -> None:
    rows = fetch_all_sessions()
    if not rows:
        await bot.send_message(target_chat_id, "Пока нет данных для статистики.")
        return

    group_names = {
        1: "1 группа (часы видны)",
        2: "2 группа (время скрыто, считать нельзя)",
        3: "3 группа (время скрыто, считать можно)",
        4: "4 группа (ограниченная лента, считать нельзя)",
    }

    lines = [f"<b>Сводка по группам (всего: {len(rows)} чел.)</b>\n"]
    for g in (1, 2, 3, 4):
        g_rows = [r for r in rows if r["group_num"] == g]
        n = len(g_rows)
        if n == 0:
            lines.append(f"• <b>{group_names[g]}</b>: 0 чел.")
            continue

        avg_actual = sum(r["actual_sec"] for r in g_rows) / n
        avg_perceived = sum(r["perceived_sec"] for r in g_rows) / n
        avg_err_pct = sum(r["time_error_pct"] for r in g_rows) / n
        avg_hit = sum(r["hit_rate_pct"] for r in g_rows) / n

        lines.append(
            f"• <b>{group_names[g]}</b> — <b>{n} чел.</b>\n"
            f"  ⏱ Факт: <code>{avg_actual:.1f}с</code> | Оценка: <code>{avg_perceived:.1f}с</code> ({avg_err_pct:+.1f}%)\n"
            f"  🧠 Узнавание карточек: <code>{avg_hit:.0f}%</code>"
        )

    await bot.send_message(target_chat_id, "\n".join(lines))


async def send_csv_export(target_chat_id: int) -> None:
    rows = fetch_all_sessions()
    if not rows:
        await bot.send_message(target_chat_id, "База пуста — выгружать пока нечего.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        [
            "ID",
            "Дата",
            "Участник",
            "Username",
            "Группа",
            "Факт_сек",
            "Оценка_сек",
            "Ошибка_сек",
            "Ошибка_проц",
            "Счет_про_себя",
            "Просмотрено_шт",
            "Узнавание_проц",
            "Свободное_припоминание",
            "Полный_конспект",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["created_at"],
                r["participant_name"],
                r["tg_username"],
                r["group_num"],
                r["actual_sec"],
                r["perceived_sec"],
                r["time_error_sec"],
                r["time_error_pct"],
                r["counting_answer"],
                r["viewed_count"],
                r["hit_rate_pct"],
                r["free_recall_text"],
                r["summary_text"].replace("\n", " | "),
            ]
        )

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    doc = BufferedInputFile(csv_bytes, filename="neurofeed_experiment.csv")
    await bot.send_document(
        target_chat_id,
        document=doc,
        caption=f"Выгрузка эксперимента ({len(rows)} участников)",
    )


@dp.message(F.text == "📋 Конспекты")
async def btn_recent_summaries(message: types.Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_recent_summaries(message.chat.id)


@dp.message(F.text == "📊 По группам")
async def btn_group_stats(message: types.Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_group_stats(message.chat.id)


@dp.message(F.text == "📥 CSV")
async def btn_csv_export(message: types.Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_csv_export(message.chat.id)


@dp.callback_query(F.data == "adm_recent")
async def cb_adm_recent(callback: types.CallbackQuery) -> None:
    if callback.from_user and is_admin(callback.from_user.id) and callback.message:
        await send_recent_summaries(callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "adm_groups")
async def cb_adm_groups(callback: types.CallbackQuery) -> None:
    if callback.from_user and is_admin(callback.from_user.id) and callback.message:
        await send_group_stats(callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "adm_csv")
async def cb_adm_csv(callback: types.CallbackQuery) -> None:
    if callback.from_user and is_admin(callback.from_user.id) and callback.message:
        await send_csv_export(callback.message.chat.id)
    await callback.answer()


@dp.callback_query(F.data == "adm_clear_confirm")
async def cb_adm_clear_confirm(callback: types.CallbackQuery) -> None:
    if not (callback.from_user and is_admin(callback.from_user.id) and callback.message):
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить все записи", callback_data="adm_clear_do"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="adm_cancel"),
            ]
        ]
    )
    await callback.message.answer(
        "Точно удалить все записанные конспекты из базы?", reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_clear_do")
async def cb_adm_clear_do(callback: types.CallbackQuery) -> None:
    if callback.from_user and is_admin(callback.from_user.id) and callback.message:
        clear_sessions_db()
        await callback.message.answer("База конспектов очищена.")
    await callback.answer()


@dp.callback_query(F.data == "adm_cancel")
async def cb_adm_cancel(callback: types.CallbackQuery) -> None:
    if callback.message:
        await callback.message.delete()
    await callback.answer()


# ==============================================================================
# 6. ВСТРОЕННЫЙ HTTP-СЕРВЕР (РАЗДАЁТ INDEX.HTML, ЕСЛИ ОН ЛЕЖИТ РЯДОМ С MAIN.PY)
# ==============================================================================
async def http_serve_index(request: web.Request) -> web.Response:
    if INDEX_HTML_PATH.exists():
        return web.FileResponse(INDEX_HTML_PATH)
    return web.Response(
        text="NeuroFeedBot работает. Положите index.html рядом с main.py или укажите WEBAPP_URL.",
        content_type="text/plain",
    )


async def start_web_server() -> web.AppRunner | None:
    """Запускает лёгкий aiohttp-сервер параллельно с Telegram-ботом."""
    try:
        app = web.Application()
        app.router.add_get("/", http_serve_index)
        app.router.add_get("/index.html", http_serve_index)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        logger.info("HTTP сервер запущен на порту %s", HTTP_PORT)
        return runner
    except Exception as e:
        logger.warning("HTTP сервер не запущен (возможно порт занят): %s", e)
        return None


# ==============================================================================
# 7. ТОЧКА ВХОДА
# ==============================================================================
async def main() -> None:
    init_db()
    runner = await start_web_server()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен в режиме polling...")
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())