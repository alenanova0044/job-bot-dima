import feedparser
import requests
import json
import time
import os
import re
import html
import socket
from datetime import datetime

# Жёсткий лимит на сетевые операции feedparser (чтобы медленный RSS не вешал весь цикл)
socket.setdefaulttimeout(20)

# ── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003890109420")
# Постоянное хранилище: Railway Volume монтируется в /data.
# Если тома нет — падаем обратно на локальный файл (не критично, но сбрасывается при деплое).
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
SEEN_FILE = os.path.join(DATA_DIR, "seen_jobs.json")

# Живая бесплатная модель. flash-lite: свой дневной лимит (1500/сут) + 30 запросов/мин
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Порог отправки в группу: карточка уходит, если оценка >= этого числа
MIN_SCORE = 5

# Как часто запускать сбор (в часах). Бот работает постоянно и повторяет цикл.
INTERVAL_HOURS = 1

# Пауза между запросами к Gemini (сек). Бесплатный лимит ~10 запросов/мин.
GEMINI_PAUSE = 4

# Предохранитель: если подряд столько запросов к Gemini провалились (лимит/перегрузка) —
# считаем, что квота исчерпана, и прерываем прогон до следующего часа (не молотим впустую).
CONSEC_FAIL_LIMIT = 8
_consec_fail = 0

class QuotaExhausted(Exception):
    pass

RSS_FEEDS = [
    "https://remotive.com/remote-jobs/feed/",
    "https://weworkremotely.com/remote-jobs.rss",
    "https://himalayas.app/jobs/rss",
    "https://www.workatastartup.com/jobs.rss",
    "https://jobicy.com/?feed=job_feed",
    "https://remote.co/remote-jobs/feed/",
    "https://remoteok.com/remote-jobs.rss",
]

# Публичные Telegram-каналы (username без @). Читаются через веб-страницу t.me/s/<name>.
TG_CHANNELS = [
    "forproducer",
    "rabotavserbii",
    "digitalclubjobs",
    "normremote",
    "remotejobss",
    "evacuatejobs",
    "relocateme",
    "young_relocate",
    "geekjobs",
    "bbe_jobs",
    "it_vakansii_jobs",
    "youritjob",
    "agile_jobs",
    "projects_jobs_feed",
    "careerspace",
    "remote_jobs_relocate",
    "products_jobs_projects",
    "remotegeekjob",
    "workshopjobs",
    "femtechforce",
    "job_for_relocation",
    "marketing_jobs",
]

PROMPT = """Ты рекрутер. Оцени, насколько вакансия подходит кандидату, и дай балл от 1 до 10.

ПРОФИЛЬ КАНДИДАТА (Дима):
- Уровень: Middle+/Senior, 7+ лет в управлении digital/web-проектами.
- Кто он (по резюме): Head of digital/web projects. Ведёт портфель из 3–6 проектов
  одновременно (бюджеты до ~$90K, суммарный оборот до ~$330K/год) для enterprise-клиентов
  в fintech, digital production и корпоративных коммуникациях.
- Сильные стороны: delivery-менеджмент и экономика проектов (маржа, бюджет, ресурсы),
  stakeholder management, кризис-менеджмент и стабилизация проблемных проектов,
  presale и оценка, запуск сложных digital-продуктов, долгие отношения с enterprise.
- Подтверждённые кейсы: держал маржу 20–30% на банковском проекте (~$330K) 3 года;
  спас сервисный проект >$100K при нехватке ресурсов; запустил промо-сайт банка с CMS/API
  за <1 мес (конверсия 5.7%); продакшн 360-коммуникаций для корп. фестиваля банка за 7 дней;
  digital-продакшн онлайн-конференции на 1000+ участников; редизайн карточки товара для
  фармы (конверсия 3.8%→6.5%).
- Инструменты: Jira, Confluence, ActiveCollab, Notion, Figma, Miro, Bitrix24, Google Workspace.
- Подходящие роли (широко): Digital Producer, Producer (в т.ч. маркетинг/медиа),
  Project Manager, Delivery Manager, Program Manager, Product Manager (не технический),
  Account Manager, Client Success / Client Engagement / Client Partner,
  Project Coordinator (senior), Operations в digital/креативе.
- Целевые индустрии: creative / branding agency, digital agency, fintech, SaaS,
  media, product-компании с дизайн-командами.
- Формат: подходит FULL REMOTE из любой точки. Офис/гибрид засчитывай ТОЛЬКО если
  локация — Сербия или страна ЕС, ЛИБО в вакансии явно есть релокация/relocation
  в ЕС/Сербию. Офис без релокации вне ЕС (напр. Москва-офис) — это минус к баллу.
- ВАЖНО про язык: вакансии бывают на русском и английском. Считай синонимами:
  "удалённо" / "удаленка" / "удаленно" / "из любой точки" / "remote" / "anywhere" = REMOTE (это плюс);
  "релокация" / "релокейт" / "переезд" / "relocation" / "relocate" = РЕЛОКАЦИЯ (плюс, если в ЕС/Сербию);
  "офис" / "гибрид" / "гибридный" / "hybrid" / "on-site" = ОФИСНЫЙ ФОРМАТ (оценивай по локации);
  "только Москва" / "офис в РФ" / "офис в СНГ" без релокации = минус к баллу.
- English B2 (FCE), может собеседоваться голосом. Если жёстко требуется C1/native English —
  это риск: понижай балл, но не считай стопом.

КАК СТАВИТЬ БАЛЛ:
- 8-10: роль прямо в цель (продюсер/PM/деливери/клиентская) + remote или ЕС/релокация.
- 5-7: смежная подходящая роль ИЛИ хорошая роль, но формат/локация под вопросом.
- 1-4: не его специализация (напр. чистая разработка кода, чистые холодные продажи,
  junior/intern) ИЛИ жёсткий офис вне ЕС без релокации.
- ЖЁСТКИЙ СТОП (ставь 1): gambling, casino, беттинг.
- ЭТО НЕ ВАКАНСИЯ (ставь 1): вебинар, курс, обучение, мастер-класс, интенсив,
  "набор на поток", реклама услуг, подборка/дайджест каналов, пост "как искать работу",
  новости, анонсы, résumé-разбор, менторство, промо. Вакансия — это конкретная
  открытая позиция в конкретной компании с обязанностями. Если это не так — балл 1.
Не блокируй вакансию только из-за слов "sales" или "engineer" в тексте —
смотри на СУТЬ роли. Если роль подходящая, но с продажами/техникой по краю — это не стоп.

ВАКАНСИЯ:
Название: {title}
Компания: {company}
Описание: {description}

Ответь СТРОГО в этом формате, без лишнего текста:
SCORE: [число от 1 до 10]
CARD:
🎯 Релевантность: [X]/10
💼 {title} — {company}
🌍 [формат/локация]
💰 [зарплата или «не указана»]
📋 [2-3 строки — суть роли под профиль Димы]
⚠️ Стоп-факторы: [перечисли или «нет»]
📌 [Откликаться / Рассмотреть / Пропустить]"""

# ── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"    TG HTTP {r.status_code}: {r.text[:150]}")
        return r.ok
    except Exception as e:
        print(f"    TG error: {e}")
        return False

def analyze_with_gemini(title, company, description, link):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    desc_clean = strip_html(description)[:2000]
    prompt = PROMPT.format(title=title, company=company, description=desc_clean)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=40)
            if r.ok:
                data = r.json()
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            if r.status_code == 429:
                print(f"    Gemini лимит (429), жду 20с и пробую снова...")
                time.sleep(20)
                continue
            if r.status_code in (500, 503):
                # временная перегрузка модели на стороне Google — ждём и повторяем
                wait = 10 * (attempt + 1)
                print(f"    Gemini перегружен ({r.status_code}), жду {wait}с и пробую снова...")
                time.sleep(wait)
                continue
            # прочие не-успехи (404, 400 и т.д.) — печатаем и выходим
            print(f"    Gemini HTTP {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"    Gemini error: {e}")
            time.sleep(5)
            continue
    print(f"    Gemini: не удалось после нескольких попыток, пропускаю")
    return None

def parse_score(response):
    match = re.search(r'SCORE:\s*(\d+)', response)
    if match:
        return int(match.group(1))
    return 0

def parse_card(response):
    match = re.search(r'CARD:\s*\n?(.*)', response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return response.strip()

# ── ИСТОЧНИКИ ────────────────────────────────────────────────────────────────

def fetch_rss(feed_url):
    entries = []
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"  Ошибка фида: {e}")
        return entries
    for entry in feed.entries[:5]:
        entries.append({
            "link":        entry.get("link", ""),
            "title":       entry.get("title", "Без названия"),
            "company":     entry.get("author", entry.get("source", {}).get("title", "Неизвестно")),
            "description": entry.get("summary", entry.get("description", "")),
        })
    return entries

def fetch_tg_channel(channel):
    """Читает t.me/s/<channel>. Разбивает страницу на блоки-посты,
    чтобы ссылка (t.me/канал/номер) точно соответствовала тексту вакансии."""
    url = f"https://t.me/s/{channel}"
    entries = []
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok:
            print(f"  TG @{channel}: HTTP {r.status_code}")
            return entries
        page = r.text

        # Режем страницу на отдельные сообщения по началу блока сообщения
        blocks = re.split(r'(?=<div class="tgme_widget_message[ "])', page)

        for block in blocks[-12:]:  # последние ~12 сообщений
            post_m = re.search(r'data-post="([^"]+)"', block)
            text_m = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                block, re.DOTALL
            )
            if not text_m:
                continue
            text = strip_html(text_m.group(1).replace("<br/>", "\n").replace("<br>", "\n"))
            if not text:
                continue
            post = post_m.group(1) if post_m else ""          # вида "channel/1234"
            link = f"https://t.me/{post}" if post else url
            title = text.split("\n")[0][:100]
            entries.append({
                "link":        link,
                "title":       title,
                "company":     f"@{channel}",
                "description": text,
            })
    except Exception as e:
        print(f"  Ошибка TG @{channel}: {e}")
    return entries

# ── ОБРАБОТКА ────────────────────────────────────────────────────────────────

# Явный не-вакансионный мусор — отсекаем до Gemini (экономит лимит API)
JUNK_MARKERS = [
    "вебинар", "webinar", "курс ", "курса", "курсы", "обучени", "мастер-класс",
    "мастеркласс", "интенсив", "набор на поток", "старт потока", "поток стартует",
    "менторств", "разбор резюме", "подборка каналов", "дайджест", "розыгрыш",
    "как искать работу", "как найти работу", "реклама",
]

def looks_like_junk(title, description):
    text = f"{title} {description}".lower()
    return any(m in text for m in JUNK_MARKERS)

def process_entries(entries, seen, source_label):
    global _consec_fail
    new_count = 0
    sent_count = 0
    for entry in entries:
        link = entry.get("link", "")
        if not link or link in seen:
            continue
        title   = entry.get("title", "Без названия")
        company = entry.get("company", "Неизвестно")
        desc    = entry.get("description", "")

        new_count += 1
        print(f"    Новая: {title[:60]}")

        # реклама/не-вакансия — решение окончательное: помечаем и сохраняем
        if looks_like_junk(title, desc):
            print(f"    ⏭ Пропуск (не вакансия / реклама)")
            seen.add(link)
            save_seen(seen)
            continue

        response = analyze_with_gemini(title, company, desc, link)
        if not response:
            # не смогли оценить (лимит/перегрузка) — НЕ помечаем seen,
            # чтобы повторить позже, когда сервис оживёт
            _consec_fail += 1
            print(f"    ↩ отложено (повторю позже)")
            if _consec_fail >= CONSEC_FAIL_LIMIT:
                raise QuotaExhausted(f"{_consec_fail} сбоев подряд")
            time.sleep(GEMINI_PAUSE)
            continue

        _consec_fail = 0  # успех — сбрасываем счётчик
        score = parse_score(response)
        card  = parse_card(response)
        print(f"    Оценка: {score}/10")

        if score >= MIN_SCORE:
            card_with_source = f"{card}\n🔗 {link}\n📌 Источник: {source_label}"
            if send_telegram(card_with_source):
                sent_count += 1
                print(f"    ✅ Отправлено в TG")
            time.sleep(1)

        seen.add(link)
        save_seen(seen)          # сохраняем прогресс сразу (переживёт перезапуск/деплой)
        time.sleep(GEMINI_PAUSE)
    return new_count, sent_count

# ── ОДИН ПРОГОН ──────────────────────────────────────────────────────────────

def run():
    global _consec_fail
    _consec_fail = 0
    seen = load_seen()
    new_count = 0
    sent_count = 0

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Запуск. Модель: {GEMINI_MODEL}, порог: {MIN_SCORE}+. Память: {SEEN_FILE}. Уже видели: {len(seen)}")

    for channel in TG_CHANNELS:
        print(f"  Читаю TG-канал: @{channel}")
        n, s = process_entries(fetch_tg_channel(channel), seen, f"@{channel}")
        new_count += n
        sent_count += s

    for feed_url in RSS_FEEDS:
        print(f"  Читаю RSS: {feed_url}")
        n, s = process_entries(fetch_rss(feed_url), seen, feed_url.split('/')[2])
        new_count += n
        sent_count += s

    save_seen(seen)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Готово. Новых: {new_count}, отправлено: {sent_count}")

# ── ЦИКЛ (запуск раз в INTERVAL_HOURS часов) ─────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            run()
        except QuotaExhausted as e:
            print(f"Похоже, лимит Gemini исчерпан ({e}). Пауза до следующего цикла.")
        except Exception as e:
            print(f"Ошибка цикла: {e}")
        print(f"Сплю {INTERVAL_HOURS} ч до следующего прогона...")
        time.sleep(INTERVAL_HOURS * 3600)
