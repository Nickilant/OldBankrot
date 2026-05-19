## Автовход в old.bankrot.fedresurs.ru через **ваш установленный Google Chrome**

Скрипт `auto_login_fedresurs.py` запускает именно ваш локальный `chrome.exe` (или Chrome на macOS/Linux), открывает URL и при необходимости подключается к этой же вкладке через CDP для автозаполнения.

### Установка

```bash
pip install playwright
```

### Запуск

```bash
python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
```

### Варианты запуска

```bash
# Явно указать ваш chrome.exe
python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz --browser-path "C:\Program Files\Google\Chrome\Application\chrome.exe"

# Только открыть страницу в вашем Chrome без автозаполнения
python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz --no-autofill
```

### Параметры

- `--url` — адрес страницы входа
- `--delay-ms` — задержка перед автозаполнением
- `--user-agent` — User-Agent для вкладки при CDP-автозаполнении
- `--browser-path` — путь к вашему Google Chrome
- `--profile-dir` — отдельная папка профиля Chrome (опционально)
- `--no-autofill` — просто открыть страницу в вашем Chrome

### Важно

Если в обычном Chrome тоже 403, это почти всегда серверная блокировка по IP/anti-bot/WAF.
