## Автовход в old.bankrot.fedresurs.ru через Python WebView

Скрипт `auto_login_fedresurs.py` открывает страницу входа во встроенном браузере (PyQt5 WebEngine), подставляет логин/пароль и нажимает кнопку входа.

### Установка

```bash
pip install PyQt5 PyQtWebEngine
```

### Запуск

```bash
python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
```

### Параметры

- `--url` — адрес страницы входа (по умолчанию нужный URL)
- `--delay-ms` — задержка перед автозаполнением после загрузки страницы
- `--user-agent` — User-Agent для браузера (по умолчанию Chrome-подобный; помогает обходить 403 при блокировке WebView UA)

### Важно

Если структура страницы изменится, селекторы могут перестать находить поля/кнопку — тогда нужно немного подправить JS-логику в `build_js`.
