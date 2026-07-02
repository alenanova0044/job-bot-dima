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
SEEN_FILE  = "seen_jobs.json"

# Живая бесплатная модель (gemini-1.5-flash отключена Google — возвращала 404)
GEMINI_MODEL = "gemini-2.5-flash"

# Порог отправки в группу: карточка уходит, если оценка >= этого числа
MIN_SCORE = 5

# Как часто запускать сбор (в часах). Бот работает постоянно и повторяет цикл.
INTERVAL_HOURS = 1

# Пауза между запросами к Gemini (сек). Бесплатный лимит ~10 запросов/мин.
GEMINI_PAUSE = 6

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
- Бэкграунд: 7+ лет в управлении digital/web-проектами, уровень Middle+/Senior.
- Подходящие роли (широко): Digital Producer, Producer (в т.ч. в маркетинге/медиа),
  Project Manager, Delivery Manager, Program Manager, Product Manager (не тех.),
  Account Manager, Client Success / Client Engagement, Client Partner,
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
- English B2 (может собеседоваться голосом).

КАК СТАВИТЬ БАЛЛ:
- 8-10: роль прямо в цель (продюсер/PM/деливери/клиентская) + remote или ЕС/релокация.
- 5-7: смежная подходящая роль ИЛИ хорошая роль, но формат/локация под вопросом.
- 1-4: не его специализация (напр. чистая разработка кода, чистые холодные продажи,
  junior/intern) ИЛИ жёсткий офис вне ЕС без релокации.
- ЖЁСТКИЙ СТОП (ставь 1): gambling, casino, беттинг.
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
📌 [Откликаться / Рассмотреть / Пропустить]
🔗 [ссылка на вакансию]"""

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

    for attempt in range(2):
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
            # любой другой не-успех — печатаем, чтобы было видно (404, 400 и т.д.)
            print(f"    Gemini HTTP {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"    Gemini error: {e}")
            return None
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
    for entry in feed.entries[:8]:
        entries.append({
            "link":        entry.get("link", ""),
            "title":       entry.get("title", "Без названия"),
            "company":     entry.get("author", entry.get("source", {}).get("title", "Неизвестно")),
            "description": entry.get("summary", entry.get("description", "")),
        })
    return entries

def fetch_tg_channel(channel):
    url = f"https://t.me/s/{channel}"
    entries = []
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok:
            print(f"  TG @{channel}: HTTP {r.status_code}")
            return entries
        page = r.text
        posts = re.findall(r'data-post="([^"]+)"', page)
        texts = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            page, re.DOTALL
        )
        for i, raw in enumerate(texts[-10:]):
            text = strip_html(raw.replace("<br/>", "\n").replace("<br>", "\n"))
            if not text:
                continue
            post = posts[i] if i < len(posts) else ""
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

def process_entries(entries, seen, source_label):
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

        response = analyze_with_gemini(title, company, desc, link)
        if not response:
            seen.add(link)
            time.sleep(GEMINI_PAUSE)
            continue

        score = parse_score(response)
        card  = parse_card(response)
        print(f"    Оценка: {score}/10")

        if score >= MIN_SCORE:
            card_with_source = f"{card}\n📌 Источник: {source_label}"
            if send_telegram(card_with_source):
                sent_count += 1
                print(f"    ✅ Отправлено в TG")
            time.sleep(1)

        seen.add(link)
        time.sleep(GEMINI_PAUSE)
    return new_count, sent_count

# ── ОДИН ПРОГОН ──────────────────────────────────────────────────────────────

def run():
    seen = load_seen()
    new_count = 0
    sent_count = 0

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Запуск. Модель: {GEMINI_MODEL}, порог: {MIN_SCORE}+. Уже видели: {len(seen)}")

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
        except Exception as e:
            print(f"Ошибка цикла: {e}")
        print(f"Сплю {INTERVAL_HOURS} ч до следующего прогона...")
        time.sleep(INTERVAL_HOURS * 3600)
