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

### Что изменено для обхода 403

По умолчанию скрипт теперь запускает страницу с Chromium User-Agent. Для сайтов, которые блокируют дефолтный WebEngine UA, это часто решает `403 Forbidden`.

### Параметры

- `--url` — адрес страницы входа (по умолчанию нужный URL)
- `--delay-ms` — задержка перед автозаполнением после загрузки страницы
- `--user-agent` — User-Agent браузера (по умолчанию современный Chromium)

### Пример со своим User-Agent

```bash
python auto_login_fedresurs.py \
  --login Zakirov5 \
  --password 3DqEdz \
  --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
```

### Важно

Если структура страницы изменится, селекторы могут перестать находить поля/кнопку — тогда нужно подправить JS-логику в `build_js`.
