import feedparser
import requests
import json
import time
import os
import re
from datetime import datetime

# ── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003890109420")
SEEN_FILE  = "seen_jobs.json"

RSS_FEEDS = [
    "https://remotive.com/remote-jobs/feed/",
    "https://weworkremotely.com/remote-jobs.rss",
    "https://himalayas.app/jobs/rss",
    "https://www.workatastartup.com/jobs.rss",
    "https://jobicy.com/?feed=job_feed",
    "https://remote.co/remote-jobs/feed/",
]

PROMPT = """Ты рекрутер. Проанализируй вакансию по профилю кандидата.

ПРОФИЛЬ КАНДИДАТА:
- Роль: Digital Producer / Creative Project Manager / Account Manager
- Опыт: 7+ лет, уровень Middle+/Senior
- Формат: ТОЛЬКО remote или hybrid в Сербии
- Целевые индустрии: creative agency, branding, fintech, digital agency, SaaS
- Стоп-факторы: gambling, casino, junior, intern, pure sales, software developer
- Зарплата цель: от 1800 EUR net

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
    return re.sub(r'<[^>]+>', ' ', text or '').strip()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print(f"TG error: {e}")
        return False

def analyze_with_gemini(title, company, description, link):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
    desc_clean = strip_html(description)[:2000]
    prompt = PROMPT.format(
        title=title,
        company=company,
        description=desc_clean
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.ok:
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"Gemini error: {e}")
    return None

def parse_score(response):
    match = re.search(r'SCORE:\s*(\d+)', response)
    if match:
        return int(match.group(1))
    return 0

def parse_card(response):
    match = re.search(r'CARD:\n(.*)', response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return response.strip()

# ── ОСНОВНОЙ ЦИКЛ ────────────────────────────────────────────────────────────

def run():
    seen = load_seen()
    new_count = 0
    sent_count = 0

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Запуск. Уже видели: {len(seen)} вакансий")

    for feed_url in RSS_FEEDS:
        print(f"  Читаю: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            entries = feed.entries[:10]  # берём последние 10
        except Exception as e:
            print(f"  Ошибка фида: {e}")
            continue

        for entry in entries:
            link = entry.get("link", "")
            if not link or link in seen:
                continue

            title   = entry.get("title", "Без названия")
            company = entry.get("author", entry.get("source", {}).get("title", "Неизвестно"))
            desc    = entry.get("summary", entry.get("description", ""))

            new_count += 1
            print(f"    Новая: {title[:60]}")

            # Анализируем через Gemini
            response = analyze_with_gemini(title, company, desc, link)
            if not response:
                seen.add(link)
                continue

            score = parse_score(response)
            card  = parse_card(response)

            print(f"    Оценка: {score}/10")

            # Шлём только релевантные (6+)
            if score >= 6:
                card_with_source = f"{card}\n📌 Источник: {feed_url.split('/')[2]}"
                if send_telegram(card_with_source):
                    sent_count += 1
                    print(f"    ✅ Отправлено в TG")
                time.sleep(1)

            seen.add(link)
            time.sleep(2)  # пауза между запросами к Gemini

    save_seen(seen)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Готово. Новых: {new_count}, отправлено: {sent_count}")

if __name__ == "__main__":
    run()
