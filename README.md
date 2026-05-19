## Автовход в old.bankrot.fedresurs.ru через Chromium (Playwright)

Скрипт `auto_login_fedresurs.py` открывает страницу входа в реальном Chromium через Playwright, подставляет логин/пароль и нажимает кнопку входа.

### Установка

```bash
pip install playwright
playwright install chromium
```

### Запуск

```bash
python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
```

### Параметры

- `--url` — адрес страницы входа (по умолчанию нужный URL)
- `--delay-ms` — задержка перед автозаполнением после загрузки страницы
- `--user-agent` — User-Agent для браузера Chromium
- `--headless` — запуск без интерфейса
- `--browser-path` — путь к вашему установленному Google Chrome (если не указать, скрипт попытается найти его автоматически)

### Важно

Если даже в системном Google Chrome видите 403, это обычно признак серверной блокировки по IP/anti-bot, а не проблема только User-Agent.
