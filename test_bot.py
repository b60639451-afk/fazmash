import asyncio
import inspect
import unittest

import bot


class TestHelpers(unittest.TestCase):
    """Тесты вспомогательных функций."""

    def test_normalize_star_message_counts_stars(self):
        self.assertEqual(bot.normalize_star_message("*****"), 5)
        self.assertEqual(bot.normalize_star_message("**"), 0)
        self.assertEqual(bot.normalize_star_message("hello"), 0)
        self.assertEqual(bot.normalize_star_message("супер ******"), 6)
        self.assertEqual(bot.normalize_star_message("***"), 3)

    def test_normalize_star_message_edge_cases(self):
        self.assertEqual(bot.normalize_star_message(None), 0)
        self.assertEqual(bot.normalize_star_message(""), 0)
        self.assertEqual(bot.normalize_star_message("   "), 0)

    def test_format_time(self):
        self.assertEqual(bot.format_time(0), "00:00")
        self.assertEqual(bot.format_time(65), "01:05")
        self.assertEqual(bot.format_time(300), "05:00")
        self.assertEqual(bot.format_time(-5), "00:00")

    def test_progress_bar(self):
        bar = bot.progress_bar(5, 10, 10)
        self.assertEqual(len(bar), 10)
        self.assertIn("▰", bar)
        self.assertIn("▱", bar)

        empty = bot.progress_bar(0, 10, 10)
        self.assertEqual(empty, "▱" * 10)

        full = bot.progress_bar(10, 10, 10)
        self.assertEqual(full, "▰" * 10)

    def test_progress_bar_zero_total(self):
        self.assertEqual(bot.progress_bar(5, 0, 10), "▱" * 10)

    def test_stars_display(self):
        self.assertEqual(bot.stars_display(3), "⭐⭐⭐")
        self.assertEqual(bot.stars_display(5), "⭐⭐⭐⭐⭐")
        self.assertEqual(bot.stars_display(8), "⭐×8")

    def test_rank_emoji(self):
        self.assertEqual(bot.rank_emoji(1), "🥇")
        self.assertEqual(bot.rank_emoji(2), "🥈")
        self.assertEqual(bot.rank_emoji(3), "🥉")


class TestRaffleDataModel(unittest.TestCase):
    """Тесты модели данных Raffle."""

    def test_raffle_defaults(self):
        raffle = bot.Raffle()
        self.assertFalse(raffle.active)
        self.assertIsNone(raffle.leader)
        self.assertEqual(raffle.time_left, 0)
        self.assertEqual(len(raffle.participants), 0)

    def test_raffle_reset(self):
        raffle = bot.Raffle()
        raffle.active = True
        raffle.leader_id = 42
        raffle.participants[42] = bot.Participant(
            user=type("User", (), {"id": 42, "username": "test", "first_name": "Test"})(),
            stars=5,
            message_id=1,
        )
        raffle.reset()
        self.assertFalse(raffle.active)
        self.assertIsNone(raffle.leader_id)
        self.assertEqual(len(raffle.participants), 0)

    def test_leaderboard_sorted(self):
        raffle = bot.Raffle()

        user1 = type("User", (), {"id": 1, "username": "a", "first_name": "A"})()
        user2 = type("User", (), {"id": 2, "username": "b", "first_name": "B"})()
        user3 = type("User", (), {"id": 3, "username": "c", "first_name": "C"})()

        raffle.participants[1] = bot.Participant(user=user1, stars=3, message_id=10)
        raffle.participants[2] = bot.Participant(user=user2, stars=7, message_id=11)
        raffle.participants[3] = bot.Participant(user=user3, stars=5, message_id=12)

        lb = raffle.leaderboard
        self.assertEqual(lb[0].user.id, 2)
        self.assertEqual(lb[1].user.id, 3)
        self.assertEqual(lb[2].user.id, 1)


class TestKeyboards(unittest.TestCase):
    """Тесты клавиатур."""

    def test_main_keyboard_structure(self):
        kb = bot.build_main_keyboard()
        self.assertIsNotNone(kb)
        rows = kb.inline_keyboard
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0].text, "🚀 Запустить")
        self.assertEqual(rows[0][1].text, "🏁 Завершить")
        self.assertEqual(rows[1][0].text, "📊 Статус")
        self.assertEqual(rows[1][1].text, "🏆 Таблица")
        self.assertEqual(rows[2][0].text, "⚙️ Настройки")
        self.assertEqual(rows[2][1].text, "❓ Помощь")

    def test_settings_keyboard(self):
        raffle = bot.Raffle()
        raffle.duration = 300
        kb = bot.build_settings_keyboard(raffle)
        rows = kb.inline_keyboard
        self.assertEqual(len(rows), 3)
        self.assertIn("5 мин", rows[0][0].text)


class TestMessages(unittest.TestCase):
    """Тесты формирования сообщений."""

    def test_welcome_contains_key_info(self):
        msg = bot.msg_welcome()
        self.assertIn("FazMash", msg)
        self.assertIn("Как играть", msg)

    def test_help_contains_commands(self):
        msg = bot.msg_help()
        self.assertIn("/start", msg)
        self.assertIn("/begin", msg)
        self.assertIn("/stop", msg)
        self.assertIn("/status", msg)
        self.assertIn("/help", msg)

    def test_raffle_started_msg(self):
        msg = bot.msg_raffle_started(300)
        self.assertIn("РОЗЫГРЫШ ЗАПУЩЕН", msg)
        self.assertIn("05:00", msg)

    def test_status_msg_no_active(self):
        raffle = bot.Raffle()
        msg = bot.msg_raffle_status(raffle)
        self.assertIn("НЕТ АКТИВНОГО РОЗЫГРЫША", msg)


class TestStarHandler(unittest.TestCase):
    """Тесты обработки звёздных сообщений."""

    def setUp(self):
        # Очистить состояние
        bot.raffles.clear()

    def test_star_message_sets_leader(self):
        chat_id = 100
        raffle = bot.raffles[chat_id]
        raffle.active = True
        raffle.started_at = 1000

        user = type("User", (), {"id": 7, "username": "star_user", "first_name": "Star", "full_name": "Star User"})()

        async def reply_html(*args, **kwargs):
            return None

        message = type(
            "Message", (),
            {"text": "******", "from_user": user, "message_id": 88, "reply_html": reply_html},
        )()
        chat = type("Chat", (), {"type": "private", "id": chat_id})()
        update = type("Update", (), {
            "message": message,
            "effective_chat": chat,
            "callback_query": None,
        })()

        async def send_message(*args, **kwargs):
            return None

        ctx = type("Context", (), {
            "bot": type("Bot", (), {"send_message": send_message})()
        })()

        asyncio.run(bot.handle_star_message(update, ctx))

        self.assertEqual(raffle.leader_id, 7)
        self.assertEqual(raffle.participants[7].stars, 6)

    def test_leader_replaced_by_higher_score(self):
        chat_id = 200
        raffle = bot.raffles[chat_id]
        raffle.active = True
        raffle.started_at = 1000

        user1 = type("User", (), {"id": 1, "username": "first", "first_name": "First", "full_name": "First"})()
        user2 = type("User", (), {"id": 2, "username": "second", "first_name": "Second", "full_name": "Second"})()

        async def reply_html(*args, **kwargs):
            return None

        msg1 = type("Message", (), {
            "text": "***", "from_user": user1, "message_id": 10, "reply_html": reply_html,
        })()
        msg2 = type("Message", (), {
            "text": "********", "from_user": user2, "message_id": 11, "reply_html": reply_html,
        })()

        chat = type("Chat", (), {"type": "group", "id": chat_id})()

        update1 = type("Update", (), {"message": msg1, "effective_chat": chat, "callback_query": None})()
        update2 = type("Update", (), {"message": msg2, "effective_chat": chat, "callback_query": None})()

        async def send_message(*args, **kwargs):
            return None

        ctx = type("Context", (), {
            "bot": type("Bot", (), {"send_message": send_message})()
        })()

        asyncio.run(bot.handle_star_message(update1, ctx))
        asyncio.run(bot.handle_star_message(update2, ctx))

        self.assertEqual(raffle.leader_id, 2)
        self.assertEqual(raffle.participants[2].stars, 8)
        self.assertEqual(len(raffle.participants), 2)

    def test_inactive_raffle_ignores_stars(self):
        chat_id = 300
        raffle = bot.raffles[chat_id]
        raffle.active = False

        user = type("User", (), {"id": 5, "username": "u", "first_name": "U", "full_name": "U"})()
        message = type("Message", (), {
            "text": "*****", "from_user": user, "message_id": 1, "reply_html": lambda *a, **k: None,
        })()
        chat = type("Chat", (), {"type": "group", "id": chat_id})()
        update = type("Update", (), {"message": message, "effective_chat": chat, "callback_query": None})()
        ctx = type("Context", (), {"bot": type("Bot", (), {"send_message": lambda *a, **k: None})()})()

        asyncio.run(bot.handle_star_message(update, ctx))
        self.assertEqual(len(raffle.participants), 0)


class TestMainFunction(unittest.TestCase):
    """Тесты основной функции."""

    def test_main_is_not_async(self):
        self.assertFalse(inspect.iscoroutinefunction(bot.main))


if __name__ == "__main__":
    unittest.main()
