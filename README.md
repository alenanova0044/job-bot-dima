# Job Bot для Димы

Бот анализирует вакансии через Gemini AI и шлёт карточки в Telegram.

## Переменные окружения
- `GEMINI_KEY` — ключ от Google AI Studio
- `TG_TOKEN` — токен бота от @BotFather  
- `TG_CHAT_ID` — ID группы (уже прописан по умолчанию)

## Запуск локально
```bash
pip install -r requirements.txt
export GEMINI_KEY=...
export TG_TOKEN=...
python scheduler.py
```
