# -*- coding: utf-8 -*-
"""
FazMash — Телеграм-бот для конкурсов с перебиванием.

Правила доступа:
  1. Добавить бота в группу может ТОЛЬКО ВЛАДЕЛЕЦ бота.
     Если бота добавит любой другой пользователь — бот немедленно выходит из группы (leave_chat).
  2. В группе, куда бота добавил владелец, управлять конкурсом могут:
     - Сам ВЛАДЕЛЕЦ бота
     - СОЗДАТЕЛЬ (Владелец) этой группы
     - Все АДМИНИСТРАТОРЫ этой группы
  3. Бот работает ТОЛЬКО в группах и супергруппах.
  4. Обычные участники сразу участвуют в перебивании своими сообщениями.
"""

import asyncio
import json
import logging
import os
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "8660717839:AAFkZoGlBWVd6hT-uVpO-VEcCc7UINivOT0"

OWNER_FILE = pathlib.Path(__file__).parent / "owner.json"

DEFAULT_TIMEOUT = 30
TIME_OPTIONS = [30, 60, 120, 180, 240]

games: dict = {}


def load_owner() -> int:
    if OWNER_FILE.exists():
        try:
            data = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
            return int(data.get("owner_id", 0))
        except Exception as e:
            logger.error("Error reading owner.json: %s", e)
    return 0


def save_owner(owner_id: int):
    try:
        OWNER_FILE.write_text(json.dumps({"owner_id": owner_id}, ensure_ascii=False), encoding="utf-8")
        logger.info("Owner saved: %s", owner_id)
    except Exception as e:
        logger.error("Error saving owner: %s", e)


OWNER_ID = load_owner()


def is_owner(user_id: int) -> bool:
    global OWNER_ID
    if OWNER_ID == 0:
        OWNER_ID = user_id
        save_owner(user_id)
        return True
    return user_id == OWNER_ID


async def can_manage_contest(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Управлять конкурсом может владелец бота, создатель группы ИЛИ администраторы группы."""
    if is_owner(user_id):
        return True

    # 1. Проверяем через get_chat_member
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status_str = str(member.status).lower()
        if member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR} or status_str in {"creator", "administrator"}:
            return True
    except Exception as e:
        logger.warning("get_chat_member check failed for user %s in chat %s: %s", user_id, chat_id, e)

    # 2. Надёжный запасной вариант через список администраторов группы
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.user.id == user_id:
                return True
    except Exception as e:
        logger.warning("get_chat_administrators check failed: %s", e)

    return False


def user_link(user) -> str:
    if user is None:
        return "Никто"
    if user.username:
        return f"@{user.username}"
    name = user.first_name or "Участник"
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def format_time(sec: int) -> str:
    if sec < 60:
        return f"{sec} сек"
    m = sec // 60
    s = sec % 60
    if s == 0:
        return f"{m} мин"
    return f"{m} мин {s} сек"


def get_game(chat_id: int) -> dict:
    if chat_id not in games:
        games[chat_id] = {
            "phase": "idle",
            "participants": {},
            "last_user": None,
            "last_time": 0,
            "timer_task": None,
            "timeout": DEFAULT_TIMEOUT,
        }
    return games[chat_id]


def cancel_timer(game: dict):
    t = game.get("timer_task")
    if t and not t.done():
        t.cancel()
    game["timer_task"] = None


def is_group(chat) -> bool:
    return chat is not None and chat.type in {"group", "supergroup"}


# ==================== КЛАВИАТУРЫ ====================

def kb_main(timeout: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Запустить перебивание", callback_data="start_battle")],
        [InlineKeyboardButton(f"⏱ Время: {format_time(timeout)}", callback_data="timer_settings")],
    ])


def kb_battle():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Остановить перебивание", callback_data="stop_battle")],
    ])


def kb_timer(current_timeout: int):
    buttons = []
    row = []
    for sec in TIME_OPTIONS:
        label = format_time(sec)
        if sec == current_timeout:
            label = "✅ " + label
        row.append(InlineKeyboardButton(label, callback_data=f"set_time_{sec}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


# ==================== СТРОГАЯ ЗАЩИТА: ДОБАВЛЕНИЕ ТОЛЬКО ВЛАДЕЛЬЦЕМ ====================

async def check_group_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Только владелец бота имеет право приглашать бота в группу."""
    result = update.my_chat_member
    if result is None:
        return

    chat = result.chat
    inviter = result.from_user

    was_member = result.old_chat_member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, "member", "administrator"}
    is_member = result.new_chat_member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, "member", "administrator"}

    if not was_member and is_member:
        if inviter is None or not is_owner(inviter.id):
            logger.warning("Blocked unauthorized addition in chat %s by user %s", chat.id, getattr(inviter, "id", None))
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "⛔ <b>ДОСТУП ЗАПРЕЩЁН</b>\n\n"
                        "Этого бота разрешено добавлять в группы <b>ТОЛЬКО его владельцу</b>!\n"
                        "Поскольку вы не являетесь владельцем бота, бот немедленно покидает эту группу."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

            try:
                await context.bot.leave_chat(chat.id)
            except Exception as e:
                logger.error("Error leaving chat: %s", e)


async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.new_chat_members:
        return

    chat = update.effective_chat
    inviter = message.from_user
    bot_id = context.bot.id

    for member in message.new_chat_members:
        if member.id == bot_id:
            if inviter is None or not is_owner(inviter.id):
                try:
                    await message.reply_html(
                        "⛔ <b>ДОСТУП ЗАПРЕЩЁН</b>\n\n"
                        "Этого бота разрешено добавлять в группы <b>ТОЛЬКО его владельцу</b>!\n"
                        "Бот немедленно покидает этот чат."
                    )
                except Exception:
                    pass

                try:
                    await context.bot.leave_chat(chat.id)
                except Exception as e:
                    logger.error("Error leaving chat: %s", e)
            break


# ==================== КОМАНДЫ ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if user is None or chat is None:
        return

    if OWNER_ID == 0:
        is_owner(user.id)

    # 1. Если написали в личные сообщения
    if not is_group(chat):
        if is_owner(user.id):
            await update.message.reply_html(
                f"👋 <b>Привет, Владелец!</b> (Ваш ID: <code>{user.id}</code>)\n\n"
                "🔒 <b>Настройки доступа:</b>\n"
                "• Добавлять бота в группы можете <b>только вы</b>.\n"
                "• В вашей группе запускать конкурс сможете <b>вы, создатель группы и её администраторы</b>.\n"
                "• Бот работает <b>исключительно в группах</b>.\n\n"
                "👉 Добавьте меня в вашу группу и напишите /start там!"
            )
        else:
            await update.message.reply_html(
                "⛔ <b>Этот бот приватный.</b>\n"
                "Добавлять и настраивать его может только владелец."
            )
        return

    # 2. В группе: проверяем, что это ВЛАДЕЛЕЦ бота, СОЗДАТЕЛЬ или АДМИНИСТРАТОР группы
    if not await can_manage_contest(context, chat.id, user.id):
        await update.message.reply_html("⛔ <b>Управлять конкурсом могут только администраторы чата и владелец бота.</b>")
        return

    game = get_game(chat.id)

    if game["phase"] == "battle":
        await update.message.reply_html(
            "⚡ <b>Перебивание уже активно в этом чате!</b>\n"
            f"👑 Текущий лидер: {user_link(game['last_user'])}\n"
            f"⏱ Таймер: <b>{format_time(game['timeout'])}</b>",
            reply_markup=kb_battle(),
        )
        return

    cancel_timer(game)
    game["phase"] = "idle"
    game["participants"] = {}
    game["last_user"] = None

    await update.message.reply_html(
        "✨✨✨✨✨✨✨✨✨✨✨✨✨✨\n"
        "\n"
        "  🎯 <b>КОНКУРС: ПЕРЕБИВАНИЕ</b>\n"
        "\n"
        "✨✨✨✨✨✨✨✨✨✨✨✨✨✨\n\n"
        "👑 <b>Панель управления (Владелец / Администраторы)</b>\n\n"
        f"⏱ Текущий таймер: <b>{format_time(game['timeout'])}</b>\n\n"
        "💡 <i>После запуска любой участник группы сможет сразу писать в чат и перебивать других!</i>",
        reply_markup=kb_main(game["timeout"]),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if user is None or chat is None:
        return

    if not is_group(chat):
        return

    if not await can_manage_contest(context, chat.id, user.id):
        await update.message.reply_html("⛔ <b>Остановить конкурс могут только администраторы чата и владелец бота.</b>")
        return

    game = get_game(chat.id)
    if game["phase"] != "battle":
        await update.message.reply_html("ℹ️ Сейчас нет активного перебивания.")
        return

    cancel_timer(game)
    game["phase"] = "idle"
    game["last_user"] = None
    game["last_time"] = 0

    await update.message.reply_html(
        "🛑 <b>Перебивание остановлено.</b>\n\n"
        "Напишите /start для открытия панели конкурса."
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        status = " ✅ Владелец бота" if is_owner(user.id) else ""
        await update.message.reply_html(f"Ваш Telegram ID: <code>{user.id}</code>{status}")


# ==================== ОБРАБОТЧИК КНОПОК ====================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    chat = update.effective_chat
    user = query.from_user
    if chat is None or user is None:
        return

    if not is_group(chat):
        await query.answer("Бот работает только в группах!", show_alert=True)
        return

    # Нажимать кнопки могут ВЛАДЕЛЕЦ бота, СОЗДАТЕЛЬ или АДМИНИСТРАТОРЫ группы
    if not await can_manage_contest(context, chat.id, user.id):
        await query.answer("⛔ Только администраторы группы и владелец бота могут управлять конкурсом!", show_alert=True)
        return

    await query.answer()

    game = get_game(chat.id)
    data = query.data

    # ---- ЗАПУСТИТЬ ПЕРЕБИВАНИЕ ----
    if data == "start_battle":
        cancel_timer(game)
        game["phase"] = "battle"
        game["participants"] = {}
        game["last_user"] = None
        game["last_time"] = time.time()

        await query.message.edit_text(
            "⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡\n"
            "\n"
            "🔥 <b>ПЕРЕБИВАНИЕ НАЧАЛОСЬ!</b>\n"
            "\n"
            "⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡\n\n"
            "💬 <b>Пишите сообщения в чат прямо сейчас!</b>\n"
            "Каждое новое сообщение перебивает предыдущего лидера.\n\n"
            f"⏰ <b>Таймер:</b> {format_time(game['timeout'])} без ответов = победа!\n\n"
            "👑 <i>Кто напишет первым и станет лидером?</i>",
            parse_mode="HTML",
            reply_markup=kb_battle(),
        )

        game["timer_task"] = asyncio.create_task(battle_timer(chat.id, context))
        return

    # ---- ОСТАНОВИТЬ ПЕРЕБИВАНИЕ ----
    if data == "stop_battle":
        cancel_timer(game)
        game["phase"] = "idle"
        game["last_user"] = None
        game["last_time"] = 0

        await query.message.edit_text(
            "🛑 <b>Перебивание остановлено.</b>\n\n"
            "Нажмите /start чтобы открыть меню заново.",
            parse_mode="HTML",
            reply_markup=kb_main(game["timeout"]),
        )
        return

    # ---- НАСТРОЙКИ ТАЙМЕРА ----
    if data == "timer_settings":
        await query.message.edit_text(
            "───────────────────────\n"
            "⏱ <b>НАСТРОЙКА ВРЕМЕНИ ПЕРЕБИВАНИЯ</b>\n"
            "───────────────────────\n\n"
            "Выберите время ожидания:\n"
            "Если после сообщения никто не ответит за это время — "
            "последний написавший побеждает!\n\n"
            f"Текущий таймер: <b>{format_time(game['timeout'])}</b>",
            parse_mode="HTML",
            reply_markup=kb_timer(game["timeout"]),
        )
        return

    # ---- ВЫБОР ВРЕМЕНИ ----
    if data.startswith("set_time_"):
        sec = int(data.replace("set_time_", ""))
        if sec in TIME_OPTIONS:
            game["timeout"] = sec

        await query.message.edit_text(
            "───────────────────────\n"
            "⏱ <b>НАСТРОЙКА ВРЕМЕНИ ПЕРЕБИВАНИЯ</b>\n"
            "───────────────────────\n\n"
            f"✅ Установлено время: <b>{format_time(game['timeout'])}</b>\n\n"
            "Можете выбрать другое время или вернуться в меню:",
            parse_mode="HTML",
            reply_markup=kb_timer(game["timeout"]),
        )
        return

    # ---- НАЗАД В ГЛАВНОЕ МЕНЮ ----
    if data == "back_main":
        await query.message.edit_text(
            "✨✨✨✨✨✨✨✨✨✨✨✨✨✨\n"
            "\n"
            "  🎯 <b>КОНКУРС: ПЕРЕБИВАНИЕ</b>\n"
            "\n"
            "✨✨✨✨✨✨✨✨✨✨✨✨✨✨\n\n"
            "👑 <b>Панель управления (Владелец / Администраторы)</b>\n\n"
            f"⏱ Текущий таймер: <b>{format_time(game['timeout'])}</b>\n\n"
            "💡 <i>После запуска любой участник группы сможет сразу писать в чат и перебивать других!</i>",
            parse_mode="HTML",
            reply_markup=kb_main(game["timeout"]),
        )
        return


# ==================== ТАЙМЕР ПЕРЕБИВАНИЯ ====================

async def battle_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(chat_id)
    if not game:
        return

    while True:
        await asyncio.sleep(1)

        if not game or game["phase"] != "battle":
            return

        if game["last_time"] == 0:
            continue

        elapsed = time.time() - game["last_time"]
        if elapsed >= game["timeout"]:
            break

    winner = game["last_user"]
    total_participants = len(game["participants"])
    game["phase"] = "idle"
    game["timer_task"] = None

    if winner is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "───────────────────────\n"
                "⏹ <b>КОНКУРС ОКОНЧЕН</b>\n"
                "───────────────────────\n\n"
                "😔 Никто не написал ни одного сообщения...\n\n"
                "/start — начать заново"
            ),
            parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "\n"
                "🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆\n"
                "\n"
                "  👑 <b>П О Б Е Д И Т Е Л Ь</b>\n"
                "\n"
                "🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆🏆\n\n"
                f"  🌟 <b>{user_link(winner)}</b> 🌟\n\n"
                f"  🎉 <b>Поздравляем! Ты победил в перебивании!</b> 🎉\n"
                f"  👥 Всего участников: <b>{total_participants}</b>\n\n"
                "───────────────────────\n"
                "/start — начать новый раунд"
            ),
            parse_mode="HTML",
        )


# ==================== ПЕРЕБИВАНИЕ СООБЩЕНИЯМИ ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return

    chat = update.effective_chat
    if not is_group(chat):
        return

    game = games.get(chat.id)
    if not game or game["phase"] != "battle":
        return

    user = message.from_user
    if user is None or user.is_bot:
        return

    # Засчитываем участника
    game["participants"][user.id] = user

    old_user = game["last_user"]
    game["last_user"] = user
    game["last_time"] = time.time()

    # Первое сообщение
    if old_user is None:
        await message.reply_html(
            f"🔥 {user_link(user)} <b>стал первым лидером!</b>\n\n"
            f"Кто перебьёт? Таймер: <b>{format_time(game['timeout'])}</b>"
        )
        return

    # Тот же человек
    if old_user.id == user.id:
        await message.reply_html(
            f"💪 {user_link(user)} удерживает позицию!\n"
            f"Таймер обновлён: <b>{format_time(game['timeout'])}</b>"
        )
        return

    # ПЕРЕБИЛИ!
    await message.reply_html(
        "⚡⚡⚡ <b>ПЕРЕБИЛИ!</b> ⚡⚡⚡\n\n"
        f"❌ {user_link(old_user)}, <b>вас перебили!</b>\n\n"
        "───────────────────────\n\n"
        f"👑 Новый лидер: <b>{user_link(user)}</b>\n\n"
        f"⏰ {format_time(game['timeout'])} без ответа = победа!"
    )


# ==================== HTTP СЕРВЕР ДЛЯ RENDER (HEALTH CHECK) ====================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        # Отключаем постоянный вывод регулярных пингов от Render в лог
        pass


def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Health check HTTP-сервер запущен на порту %s", port)
    server.serve_forever()


def main() -> None:
    # Запуск фонового HTTP-сервера для Render
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .pool_timeout(30.0)
        .build()
    )

    app.add_handler(ChatMemberHandler(check_group_addition, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started. Registered owner: %s", OWNER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
