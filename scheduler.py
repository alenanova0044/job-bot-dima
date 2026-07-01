import schedule
import time
from bot import run

# Запускаем каждый час
schedule.every(1).hours.do(run)

print("Планировщик запущен. Первый запуск через минуту...")
run()  # Первый запуск сразу

while True:
    schedule.run_pending()
    time.sleep(60)
