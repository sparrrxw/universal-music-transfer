"""Запуск: python -m app  (откроет http://127.0.0.1:8890 в браузере по умолчанию)"""
import threading
import webbrowser

import uvicorn

from . import db

PORT = int(db.settings().get("port", 8890))
URL = f"http://127.0.0.1:{PORT}"

if __name__ == "__main__":
    print(f"\n  Сервис запущен: {URL}\n  Откройте этот адрес в Brave. Для остановки закройте окно или нажмите Ctrl+C.\n")
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
