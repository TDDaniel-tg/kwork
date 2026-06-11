"""
Kwork Фриланс-сканер бот
=========================
Мониторит биржу заказов Kwork в реальном времени.
Как только появляется подходящий заказ — сразу присылает в Telegram.
Можно сгенерировать отклик одной кнопкой.

Установка зависимостей:
    pip install python-telegram-bot google-generativeai aiohttp

Настройка:
    Заполни .env файл (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY)

Запуск:
    python freelance_scanner_bot.py
"""

import asyncio
import aiohttp
import google.generativeai as genai
import logging
import json
import re
import os
import time

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
#  CONFIG
# ─────────────────────────────────────────────

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")

# ─────────────────────────────────────────────
#  Твои данные
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
MIN_SCORE = 4

# Интервал проверки в секундах
CHECK_INTERVAL = 90

# ─────────────────────────────────────────────
#  Kwork: категории для мониторинга
# ─────────────────────────────────────────────

# Парсим несколько страниц для полного покрытия
KWORK_URLS = [
    # Все категории программирования
    "https://kwork.ru/projects?c=11",
    # Все категории (на случай если заказ в другой рубрике, но подходит по навыкам)
    "https://kwork.ru/projects?c=all",
]

KWORK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─────────────────────────────────────────────
#  Внутреннее состояние
# ─────────────────────────────────────────────

seen_ids: set = set()
pending_orders: dict = {}

# Счётчики для статистики
stats = {
    "total_checked": 0,
    "total_sent": 0,
    "total_skipped": 0,
    "last_check": None,
    "errors": 0,
}

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


async def ai_evaluate(title: str, description: str) -> tuple[int, str]:
    """Оценивает заказ по соответствию навыкам. Возвращает (оценка 1-10, причина)."""
    prompt = f"""Ты помощник фрилансера. Оцени, насколько заказ подходит исполнителю.

Навыки исполнителя:
{MY_SKILLS}

Заказ:
Название: {title}
Описание: {description[:1000]}

ВАЖНО: Оценивай щедро. Если заказ хоть как-то связан с программированием, ботами, 
веб-разработкой, AI, парсингом, автоматизацией — ставь 5+.
Если прямо по стеку — 7+.

Ответь СТРОГО в формате JSON без лишнего текста и без markdown-обёрток:
{{"score": <число от 1 до 10>, "reason": "<1 предложение почему>"}}
"""
    for attempt in range(4):
        try:
            response = await model.generate_content_async(prompt)
            raw = response.text.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            return int(data["score"]), data["reason"]
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "quota" in err_msg.lower() or "limit" in err_msg.lower():
                wait_time = 15 * (attempt + 1)
                log.warning(f"Квота Gemini API (429), повтор через {wait_time} сек... (Попытка {attempt+1}/4)")
                await asyncio.sleep(wait_time)
                continue
            log.warning(f"Ошибка оценки AI: {e}")
            return 0, "Ошибка оценки"
    return 0, "Ошибка: превышена квота запросов"


async def ai_generate_response(title: str, description: str) -> str:
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
    for attempt in range(3):
        try:
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "quota" in err_msg.lower() or "limit" in err_msg.lower():
                wait_time = 10 * (attempt + 1)
                log.warning(f"Квота Gemini при генерации отклика, повтор через {wait_time} сек...")
                await asyncio.sleep(wait_time)
                continue
            log.warning(f"Ошибка генерации отклика: {e}")
            return "Не удалось сгенерировать отклик. Попробуй ещё раз."
    return "Не удалось сгенерировать отклик из-за ограничений квоты."


# ─────────────────────────────────────────────
#  Kwork: парсинг заказов
# ─────────────────────────────────────────────

async def fetch_kwork_orders(session: aiohttp.ClientSession) -> list[dict]:
    """Парсит заказы с биржи Kwork через HTML (window.stateData)."""
    all_orders = []
    seen_in_batch = set()  # Чтобы не дублировать между категориями

    for url in KWORK_URLS:
        try:
            # Парсим первые 2 страницы каждой категории
            for page in range(1, 3):
                page_url = f"{url}&page={page}" if page > 1 else url

                async with session.get(page_url, headers=KWORK_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        log.warning(f"Kwork вернул статус {resp.status} для {page_url}")
                        continue

                    html = await resp.text()

                # Извлекаем JSON из window.stateData
                match = re.search(r'window\.stateData\s*=\s*(\{.*)', html, re.DOTALL)
                if not match:
                    log.warning(f"Не найден stateData на {page_url}")
                    continue

                try:
                    decoder = json.JSONDecoder()
                    data, _ = decoder.raw_decode(match.group(1))
                except json.JSONDecodeError as e:
                    log.warning(f"Ошибка парсинга JSON stateData: {e}")
                    continue

                wants = data.get("wants", [])
                if not wants:
                    log.debug(f"Нет заказов на {page_url}")
                    break  # Нет больше страниц

                for want in wants:
                    want_id = str(want.get("id", ""))
                    if not want_id or want_id in seen_in_batch:
                        continue
                    seen_in_batch.add(want_id)

                    # Чистим HTML из описания
                    description = want.get("description", "")
                    description = re.sub(r'<[^>]+>', ' ', description)
                    description = re.sub(r'\s+', ' ', description).strip()

                    name = want.get("name", "Без названия")
                    # Декодируем HTML-сущности
                    name = name.replace("&rarr;", "→").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

                    price = want.get("priceLimit", "")
                    price_str = f"{price} руб." if price else "Не указана"

                    # Формируем ссылку
                    link = f"https://kwork.ru/projects/{want_id}/view"

                    # Время
                    dates = want.get("wantDates", {})
                    date_active = dates.get("dateActive", want.get("date_active", ""))

                    all_orders.append({
                        "id": f"kwork_{want_id}",
                        "source": "Kwork",
                        "title": name,
                        "description": description,
                        "link": link,
                        "published": date_active,
                        "price": price_str,
                        "category_id": want.get("category_id", ""),
                        "time_left": want.get("timeLeft", ""),
                    })

                log.info(f"Kwork {page_url}: получено {len(wants)} заказов")

                # Пауза между страницами чтобы не банили
                await asyncio.sleep(2)

        except asyncio.TimeoutError:
            log.warning(f"Таймаут при загрузке {url}")
            stats["errors"] += 1
        except Exception as e:
            log.warning(f"Ошибка при парсинге Kwork ({url}): {e}")
            stats["errors"] += 1

    return all_orders


# ─────────────────────────────────────────────
#  Telegram: отправка карточки заказа
# ─────────────────────────────────────────────

async def send_order_notification(bot: Bot, order: dict, score: int, reason: str):
    title  = order["title"]
    link   = order["link"]
    desc   = order["description"][:300] + ("…" if len(order["description"]) > 300 else "")
    pub    = order.get("published", "")
    price  = order.get("price", "")
    time_left = order.get("time_left", "")

    score_emoji = "🔥" if score >= 8 else "✅" if score >= 6 else "📋" if score >= 4 else "⚠️"

    # Экранируем спецсимволы Markdown
    def esc(s): return s.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("[", "\\[")

    text = (
        f"{score_emoji} *Новый заказ — Kwork*\n\n"
        f"*{esc(title)}*\n\n"
        f"{esc(desc)}\n\n"
        f"💰 Бюджет: *{esc(price)}*\n"
        f"📊 Оценка: *{score}/10* — _{esc(reason)}_\n"
        f"🕐 {esc(pub)}"
    )

    if time_left:
        text += f" (осталось {esc(time_left)})"

    text += f"\n\n🔗 [Открыть заказ]({link})"

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
    response_text = await ai_generate_response(order["title"], order["description"])

    await loading_msg.edit_text(
        f"✍️ *Текст отклика:*\n\n{response_text}\n\n"
        f"🔗 [Перейти к заказу]({order['link']})",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────
#  Главный цикл мониторинга
# ─────────────────────────────────────────────

async def monitor_loop(bot: Bot):
    log.info("🚀 Сканер Kwork запущен. Интервал: %d сек.", CHECK_INTERVAL)

    async with aiohttp.ClientSession() as session:
        # Первый прогон — заполняем seen_ids, не отправляем старые
        log.info("Инициализация: загружаю текущие заказы с Kwork…")
        try:
            init_orders = await fetch_kwork_orders(session)
            for order in init_orders:
                seen_ids.add(order["id"])
            log.info(f"Загружено {len(init_orders)} существующих заказов в кэш (не отправляем)")
        except Exception as e:
            log.error(f"Ошибка при инициализации: {e}")

        # Отправляем приветственное сообщение
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    "✅ *Kwork-сканер запущен\\!*\n\n"
                    f"📌 Отслеживаю биржу заказов Kwork\n"
                    f"🎯 Порог оценки: {MIN_SCORE}/10\n"
                    f"⏱ Интервал проверки: {CHECK_INTERVAL} сек\n"
                    f"🤖 Модель: Gemini 2\\.0 Flash\n"
                    f"📦 Заказов в кэше: {len(seen_ids)}\n\n"
                    f"Буду присылать подходящие заказы сразу как появятся\\."
                ),
                parse_mode="MarkdownV2"
            )
        except Exception as e:
            log.warning(f"⚠️ Не удалось отправить приветствие: {e}")
            # Пробуем без форматирования
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"✅ Kwork-сканер запущен!\n\n"
                        f"📌 Отслеживаю биржу заказов Kwork\n"
                        f"🎯 Порог оценки: {MIN_SCORE}/10\n"
                        f"⏱ Интервал проверки: {CHECK_INTERVAL} сек\n"
                        f"📦 Заказов в кэше: {len(seen_ids)}\n\n"
                        f"Буду присылать подходящие заказы сразу как появятся."
                    ),
                )
            except Exception as e2:
                log.error(f"Не удалось отправить приветствие даже без форматирования: {e2}")

        check_count = 0

        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            check_count += 1

            log.info(f"── Проверка #{check_count} ──")

            try:
                all_orders = await fetch_kwork_orders(session)
            except Exception as e:
                log.error(f"Ошибка получения заказов: {e}")
                stats["errors"] += 1
                continue

            # Фильтруем только новые
            new_orders = [o for o in all_orders if o["id"] not in seen_ids]

            # Добавляем в seen_ids
            for order in all_orders:
                seen_ids.add(order["id"])

            stats["last_check"] = time.strftime("%H:%M:%S")

            if not new_orders:
                log.info(f"Новых заказов нет (всего в кэше: {len(seen_ids)})")
                # Каждые 20 проверок отправляем статус
                if check_count % 20 == 0:
                    try:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=(
                                f"📊 *Статус сканера*\n\n"
                                f"Проверок: {check_count}\n"
                                f"Отправлено: {stats['total_sent']}\n"
                                f"Проверено: {stats['total_checked']}\n"
                                f"Пропущено: {stats['total_skipped']}\n"
                                f"Ошибок: {stats['errors']}\n"
                                f"В кэше: {len(seen_ids)} заказов"
                            ),
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                continue

            log.info(f"🆕 Новых заказов: {len(new_orders)}")

            for order in new_orders:
                stats["total_checked"] += 1

                score, reason = await ai_evaluate(order["title"], order["description"])
                log.info(f"  [{score}/10] {order['title'][:60]} — {reason}")

                if score >= MIN_SCORE:
                    try:
                        await send_order_notification(bot, order, score, reason)
                        stats["total_sent"] += 1
                        log.info(f"  ✅ Отправлено в Telegram")
                        await asyncio.sleep(1)
                    except Exception as e:
                        log.warning(f"  ⚠️ Ошибка отправки: {e}")
                        stats["errors"] += 1
                else:
                    stats["total_skipped"] += 1
                    log.info(f"  ⏭️ Пропущено (оценка {score} < {MIN_SCORE})")

                # Пауза между вызовами Gemini API
                await asyncio.sleep(4)


# ─────────────────────────────────────────────
#  Веб-сервер для Health Check на Render
# ─────────────────────────────────────────────

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        status = json.dumps({
            "status": "ok",
            "checked": stats["total_checked"],
            "sent": stats["total_sent"],
            "skipped": stats["total_skipped"],
            "errors": stats["errors"],
            "cached": len(seen_ids),
            "last_check": stats["last_check"],
        })
        self.wfile.write(status.encode())
    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"Health check server started on port {port}")
    server.serve_forever()

# ─────────────────────────────────────────────
#  Self-ping Keep-Alive для Render
# ─────────────────────────────────────────────

async def self_ping_loop():
    ping_url = os.getenv("PING_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not ping_url:
        log.info("No PING_URL or RENDER_EXTERNAL_URL. Self-ping disabled.")
        return

    if not ping_url.startswith("http"):
        ping_url = "https://" + ping_url

    log.info(f"🏓 Self-ping запущен: {ping_url}")

    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    log.info(f"Self-ping: {response.status}")
        except Exception as e:
            log.warning(f"Self-ping ошибка: {e}")


# ─────────────────────────────────────────────
#  Точка входа
# ─────────────────────────────────────────────

async def main():
    log.info("=" * 50)
    log.info("  Kwork Freelance Scanner Bot")
    log.info("=" * 50)

    if not TELEGRAM_TOKEN:
        log.error("❌ TELEGRAM_TOKEN не задан!")
        return
    if not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_CHAT_ID не задан!")
        return
    if not GEMINI_API_KEY:
        log.error("❌ GEMINI_API_KEY не задан!")
        return

    # Health Check сервер
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(on_generate_response, pattern=r"^gen_"))

    async with app:
        await app.start()
        await asyncio.gather(
            monitor_loop(app.bot),
            app.updater.start_polling(),
            self_ping_loop(),
        )
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
