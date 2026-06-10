"""
Фриланс-сканер бот (Gemini Edition)
=====================================
Мониторит fl.ru в реальном времени.
Как только появляется подходящий заказ — сразу присылает в Telegram.
Можно сгенерировать отклик одной кнопкой.

Установка зависимостей:
    pip install python-telegram-bot feedparser google-generativeai aiohttp

Получить бесплатный Gemini API ключ:
    https://aistudio.google.com/apikey

Настройка:
    Отредактируй блок CONFIG ниже.

Запуск:
    python freelance_scanner_bot.py
"""

import asyncio
import feedparser
import google.generativeai as genai
import logging
import json
import hashlib
import os

# Load local .env file if it exists (for local development)
if os.path.exists(".env"):
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
                except ValueError:
                    pass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# ─────────────────────────────────────────────
#  CONFIG — заполни эти три строки или используй ENV
# ─────────────────────────────────────────────

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")  # от @BotFather
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")             # узнать: @userinfobot
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")       # aistudio.google.com/apikey (бесплатно)

# ─────────────────────────────────────────────
#  Твои данные (уже заполнено по твоему сайту)
# ─────────────────────────────────────────────

MY_SKILLS = """
Даниэль Ташматов — Full-Stack разработчик и AI-инженер, 17 лет, 3+ года опыта.

Frontend: React.js, Next.js, TypeScript, Tailwind CSS, GSAP, Three.js, React Native
Backend: Python, Django, FastAPI, Node.js, REST API, WebSockets, Celery
AI & ML: GPT-4 / OpenAI API, LangChain, RAG-системы, Prompt Engineering, Computer Vision, TensorFlow, Hugging Face
Базы данных: PostgreSQL, MongoDB, Redis, SQLite, Prisma, SQLAlchemy
Инструменты: Docker, Linux/SSH, Vercel, Nginx, CI/CD, Git

Реализованные проекты:
- E-Commerce платформа (Django + React + PostgreSQL)
- Telegram-бот с компьютерным зрением для замера потолков по фото (GPT-4 + CV)
- WB/Ozon Telegram-бот для продавцов (заказы, отзывы, остатки)
- Chrome-расширение для аналитики Wildberries
- Медицинская система голосового ввода Speech-to-Text
- AI Beauty-ассистент (React Native + анализ кожи)
- Telegram Mini Apps

Опыт: тимлид в NeuroImpuls, выпускал AI-продукты в продакшен.
"""

MY_PORTFOLIO_HIGHLIGHTS = """
- Бот с компьютерным зрением, измеряющий площадь потолка по одному фото
- Telegram-боты для маркетплейсов WB и Ozon — обрабатывают тысячи запросов
- Chrome-расширения для парсинга и аналитики
- Медицинская система голосового ввода для врачей
- E-commerce платформы под ключ (Django + React)
- AI-мобильные приложения на React Native
"""

# Минимальный порог оценки (1-10), при котором бот присылает заказ
MIN_SCORE = 6

# Интервал проверки RSS в секундах
CHECK_INTERVAL = 60

# ─────────────────────────────────────────────
#  RSS-ленты бирж
# ─────────────────────────────────────────────

RSS_FEEDS = [
    {"name": "fl.ru",      "url": "https://www.fl.ru/rss/all.xml"},
    {"name": "fl.ru (IT)", "url": "https://www.fl.ru/rss/it.xml"},
    # Добавляй другие биржи сюда при наличии RSS
]

# ─────────────────────────────────────────────
#  Внутреннее состояние
# ─────────────────────────────────────────────

seen_ids: set = set()
pending_orders: dict = {}

# ─────────────────────────────────────────────
#  Логирование
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Gemini: инициализация
# ─────────────────────────────────────────────

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")


def ai_evaluate(title: str, description: str) -> tuple[int, str]:
    """Оценивает заказ по соответствию навыкам. Возвращает (оценка 1-10, причина)."""
    prompt = f"""Ты помощник фрилансера. Оцени, насколько заказ подходит исполнителю.

Навыки исполнителя:
{MY_SKILLS}

Заказ:
Название: {title}
Описание: {description[:800]}

Ответь СТРОГО в формате JSON без лишнего текста и без markdown-обёрток:
{{"score": <число от 1 до 10>, "reason": "<1 предложение почему>"}}
"""
    try:
        response = model.generate_content(prompt)
        raw = response.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return int(data["score"]), data["reason"]
    except Exception as e:
        log.warning(f"Ошибка оценки AI: {e}")
        return 0, "Ошибка оценки"


def ai_generate_response(title: str, description: str) -> str:
    """Генерирует персонализированный текст отклика на заказ."""
    prompt = f"""Ты фрилансер Даниэль Ташматов — Full-Stack и AI разработчик, 17 лет, 3+ года опыта.
Стек: React, Next.js, Django, FastAPI, Python, GPT-4, LangChain, Computer Vision, React Native.

Твои реализованные проекты (используй релевантные):
{MY_PORTFOLIO_HIGHLIGHTS}

Заказ:
Название: {title}
Описание: {description[:800]}

Напиши короткий убедительный отклик:
- 3-5 предложений, не больше
- Конкретно покажи что понял задачу
- Упомяни 1 похожий проект из портфолио если есть
- Без шаблонных фраз типа "готов помочь с вашим проектом"
- Живой деловой тон
- В конце предложи обсудить детали

Отвечай ТОЛЬКО текстом отклика, без пояснений и заголовков.
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        log.warning(f"Ошибка генерации отклика: {e}")
        return "Не удалось сгенерировать отклик. Попробуй ещё раз."


# ─────────────────────────────────────────────
#  Telegram: отправка карточки заказа
# ─────────────────────────────────────────────

async def send_order_notification(bot: Bot, order: dict, score: int, reason: str):
    source = order["source"]
    title  = order["title"]
    link   = order["link"]
    desc   = order["description"][:300] + ("…" if len(order["description"]) > 300 else "")
    pub    = order.get("published", "")

    score_emoji = "🔥" if score >= 8 else "✅" if score >= 6 else "⚠️"

    # Экранируем спецсимволы Markdown в title и reason
    def esc(s): return s.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")

    text = (
        f"{score_emoji} *Новый заказ — {source}*\n\n"
        f"*{esc(title)}*\n\n"
        f"{esc(desc)}\n\n"
        f"📊 Оценка: *{score}/10* — _{esc(reason)}_\n"
        f"🕐 {pub}\n\n"
        f"🔗 [Открыть заказ]({link})"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Сгенерировать отклик", callback_data=f"gen_{order['id']}")
    ]])

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    pending_orders[order["id"]] = {
        "title": title,
        "description": order["description"],
        "link": link,
    }


# ─────────────────────────────────────────────
#  Обработчик кнопки "Сгенерировать отклик"
# ─────────────────────────────────────────────

async def on_generate_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.replace("gen_", "")
    order = pending_orders.get(order_id)

    if not order:
        await query.message.reply_text("⚠️ Данные заказа не найдены (бот был перезапущен?)")
        return

    loading_msg = await query.message.reply_text("⏳ Генерирую отклик через Gemini…")
    response_text = ai_generate_response(order["title"], order["description"])

    await loading_msg.edit_text(
        f"✍️ *Текст отклика:*\n\n{response_text}\n\n"
        f"🔗 [Перейти к заказу]({order['link']})",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────
#  Парсинг RSS
# ─────────────────────────────────────────────

def fetch_new_orders() -> list[dict]:
    new_orders = []
    for feed_cfg in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries:
                uid = hashlib.md5(
                    entry.get("link", entry.get("id", entry.get("title", ""))).encode()
                ).hexdigest()

                if uid in seen_ids:
                    continue
                seen_ids.add(uid)

                new_orders.append({
                    "id":          uid,
                    "source":      feed_cfg["name"],
                    "title":       entry.get("title", "Без названия"),
                    "description": entry.get("summary", entry.get("description", "")),
                    "link":        entry.get("link", ""),
                    "published":   entry.get("published", ""),
                })
        except Exception as e:
            log.warning(f"Ошибка чтения {feed_cfg['name']}: {e}")
    return new_orders


# ─────────────────────────────────────────────
#  Главный цикл мониторинга
# ─────────────────────────────────────────────

async def monitor_loop(bot: Bot):
    log.info("🚀 Сканер запущен. Интервал: %d сек.", CHECK_INTERVAL)

    # Первый прогон — только заполняем seen_ids, не отправляем старые заказы
    log.info("Инициализация: загружаю текущие заказы…")
    fetch_new_orders()
    log.info("Готово. Слежу за новыми заказами…")

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "✅ *Сканер запущен!*\n"
                "Буду присылать подходящие заказы сразу как только они появятся.\n"
                f"Порог оценки: {MIN_SCORE}/10 • Модель: Gemini 2.0 Flash"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        log.warning("⚠️ Не удалось отправить приветственное сообщение: %s. Пожалуйста, напишите /start вашему боту в Telegram.", e)

    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        new_orders = fetch_new_orders()
        if not new_orders:
            continue

        log.info("Новых заказов: %d", len(new_orders))

        for order in new_orders:
            score, reason = ai_evaluate(order["title"], order["description"])
            log.info("[%d/10] %s", score, order["title"])

            if score >= MIN_SCORE:
                try:
                    await send_order_notification(bot, order, score, reason)
                    await asyncio.sleep(1)  # небольшая пауза между сообщениями
                except Exception as e:
                    log.warning("⚠️ Не удалось отправить уведомление о заказе %s: %s", order["id"], e)


# ─────────────────────────────────────────────
#  Веб-сервер для Health Check на Render (Free Tier)
# ─────────────────────────────────────────────

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        # Отключаем логирование запросов, чтобы не забивать логи
        return

def run_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"Health check server started on port {port}")
    server.serve_forever()

# ─────────────────────────────────────────────
#  Точка входа
# ─────────────────────────────────────────────

async def main():
    # Запускаем Health Check веб-сервер в фоновом потоке
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(on_generate_response, pattern=r"^gen_"))

    async with app:
        await app.start()
        await asyncio.gather(
            monitor_loop(app.bot),
            app.updater.start_polling(),
        )
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
