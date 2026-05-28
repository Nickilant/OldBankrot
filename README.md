## Автоматизация old.bankrot.fedresurs.ru: CLI + HTTP API

Скрипт `auto_login_fedresurs.py` теперь умеет:
- запускаться как **CLI** для одиночного прогона;
- запускаться как **веб-сервер (FastAPI)**, чтобы инициировать выполнение через API.

Для каждого запроса запускается отдельное окно Chrome с отдельным временным профилем.

### Установка

```bash
pip install playwright fastapi uvicorn
```

---

## 1) Одиночный запуск (CLI)

```bash
python auto_login_fedresurs.py run \
  --login "user" \
  --password "pass" \
  --inn "051100482760" \
  --message-text "Текст для поля"
```

Опционально можно передать `--browser-path`.

---

## 2) Запуск API-сервера

```bash
python auto_login_fedresurs.py serve --host 0.0.0.0 --port 8080 --workers 2
```

Параметры:
- `--workers` — максимум параллельных задач (по умолчанию 2).
- `--queue` — если указан, лишние запросы **ждут в очереди**. Если не указан — при перегрузке вернется `429`.

### Health-check

```bash
curl http://127.0.0.1:8080/health
```

### Запуск задачи через API

```bash
curl -X POST http://127.0.0.1:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "login": "user",
    "password": "pass",
    "inn": "051100482760",
    "message_text": "Текст для поля"
  }'
```

Успешный ответ:

```json
{
  "ok": true,
  "pid": 12345,
  "elapsed_sec": 18.42
}
```

---

## Многопоточность / параллельность

- Предпочтительный режим: `--workers 2` (или больше) — одновременно выполняются несколько задач, каждая в своем окне Chrome.
- Альтернативный режим очереди: `--queue` — если все воркеры заняты, запрос ждет освобождения слота и выполняется позже.
- Без `--queue` при перегрузке API сразу отвечает `429 Too Many Requests`.
