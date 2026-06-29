#!/usr/bin/env python3
"""
Автоматизация old.bankrot.fedresurs.ru как CLI и как HTTP API.

Установка зависимостей:
    pip install playwright fastapi uvicorn

Запуск API на Linux-сервере (headed под Xvfb — WAF fedresurs блокирует headless!):
    xvfb-run -a --server-args="-screen 0 1920x1080x24" \
        python auto_login_fedresurs_v2.py serve --host 0.0.0.0 --port 8080 --workers 1 --queue

Запуск API на Windows (headed по умолчанию, Xvfb не нужен):
    python auto_login_fedresurs_v2.py serve --host 0.0.0.0 --port 8080 --workers 1 --queue

На сервере держите --workers 1 --queue: общий постоянный профиль Chrome
не переносит параллельных запусков.

Эндпоинты:
    POST /run                — создать новое сообщение (старый флоу, без изменений)
    POST /fetch-last-message — найти должника по ИНН, вернуть текст последнего сообщения

Запуск CLI:
    python auto_login_fedresurs_v2.py run --login USER --password PASS --inn 051100482760 --message-text "Текст"
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

TARGET_URL = "https://old.bankrot.fedresurs.ru/BackOffice/ArbitrManager/Profile.aspx?storage=true"
DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Pydantic-модели запросов
# ---------------------------------------------------------------------------

# Тип сообщения, требующий дополнительных полей
MESSAGE_TYPE_CREDITOR_DEMAND = "Уведомление о получении требований кредитора"


class JobRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)
    inn: str = Field(min_length=1)
    message_text: str = Field(min_length=1)
    message_type_text: str = Field(min_length=1)

    # Поля только для MESSAGE_TYPE_CREDITOR_DEMAND — необязательны для остальных типов
    corr_address: str | None = Field(default=None, description="Адрес для корреспонденции")
    email: str | None = Field(default=None, description="Электронная почта")
    demand_date: str | None = Field(default=None, description="Дата получения требования ДД.ММ.ГГГГ")
    creditor_name: str | None = Field(default=None, description="Наименование кредитора (поиск по названию)")
    creditor_inn: str | None = Field(default=None, description="ИНН кредитора (поиск по ИНН; приоритетнее названия)")
    demand_sum: str | None = Field(default=None, description="Сумма требования")
    occurrence_reason: str | None = Field(default=None, description="Основание возникновения требования")


class FetchLastMessageRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)
    inn: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Сервис с пулом потоков
# ---------------------------------------------------------------------------

class FedresursAutomationService:
    def __init__(self, *, chrome_path: str | None, max_parallel_jobs: int, queue_if_busy: bool) -> None:
        self.chrome_path = chrome_path or detect_system_chrome()
        if not self.chrome_path:
            raise RuntimeError("Не найден установленный Google Chrome. Передайте --browser-path")
        self.max_parallel_jobs = max(1, max_parallel_jobs)
        self.queue_if_busy = queue_if_busy
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel_jobs)
        self.semaphore = threading.Semaphore(self.max_parallel_jobs)

    def submit(self, fn, *args):
        return self.executor.submit(self._wrap, fn, *args)

    def _wrap(self, fn, *args):
        if self.queue_if_busy:
            with self.semaphore:
                return fn(*args)
        if not self.semaphore.acquire(blocking=False):
            raise RuntimeError("Сервис занят: достигнут лимит параллельных задач")
        try:
            return fn(*args)
        finally:
            self.semaphore.release()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def detect_system_chrome() -> str | None:
    system = platform.system().lower()
    candidates: list[str] = []
    if system == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        candidates = [
            str(Path(local_app_data) / "Google/Chrome/Application/chrome.exe") if local_app_data else "",
            str(Path(program_files) / "Google/Chrome/Application/chrome.exe") if program_files else "",
            str(Path(program_files_x86) / "Google/Chrome/Application/chrome.exe") if program_files_x86 else "",
        ]
    elif system == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/snap/bin/chromium"]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_cdp_ready(port: int, timeout_sec: float = 15.0) -> bool:
    deadline = time.time() + timeout_sec
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
                if data.get("webSocketDebuggerUrl"):
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _cleanup_singleton_locks() -> None:
    """Удаляет файлы-локи постоянного профиля, оставшиеся от упавшего Chrome."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (PERSISTENT_PROFILE_DIR / name).unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Не удалось удалить {name}: {e}")


def kill_chrome(proc: subprocess.Popen | None) -> None:
    """
    Завершает Chrome ВМЕСТЕ со всеми дочерними процессами и чистит локи профиля.

    proc.terminate() гасит только головной процесс — рендереры, zygote, gpu
    остаются жить и держат лок профиля, из-за чего следующий запуск стартует
    в сломанном/занятом профиле. На Linux Chrome запускается в отдельной
    сессии (start_new_session=True), поэтому бьём всю группу процессов.
    """
    if proc is None:
        return

    is_linux = platform.system().lower() == "linux"
    try:
        if is_linux:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    except Exception as e:
        print(f"Ошибка при завершении Chrome: {e}")

    _cleanup_singleton_locks()


def safe_cleanup_temp_profile(temp_profile_dir: tempfile.TemporaryDirectory | None) -> None:
    if temp_profile_dir is None:
        return
    try:
        temp_profile_dir.cleanup()
    except PermissionError:
        print(f"Не удалось удалить временный профиль Chrome: {temp_profile_dir.name}")


PERSISTENT_PROFILE_DIR = Path.home() / ".fedresurs_chrome_profile"


def launch_chrome(chrome_path: str, url: str, delay_ms: int, cdp_timeout_sec: float):
    """
    Запускает Chrome и возвращает (proc, port, None).
    Использует постоянный профиль чтобы сайт не показывал капчу.

    На Linux процесс стартует в отдельной сессии (start_new_session=True),
    чтобы kill_chrome мог убить всю группу процессов целиком.
    """
    # Удаляем локи если остались от предыдущего упавшего процесса
    _cleanup_singleton_locks()

    port = free_port()
    PERSISTENT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    is_linux = platform.system().lower() == "linux"
    cmd = [
        chrome_path,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
        # Антидетект — скрываем признаки автоматизации
        "--disable-blink-features=AutomationControlled",
        "--exclude-switches=enable-automation",
        "--disable-infobars",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        # Постоянный профиль — сайт запоминает браузер между сессиями
        f"--user-data-dir={PERSISTENT_PROFILE_DIR}",
    ]
    if is_linux:
        # WAF fedresurs режет именно headless-Chrome (403 Forbidden), хотя curl
        # с того же IP проходит. Поэтому по умолчанию запускаемся HEADED —
        # для этого нужен дисплей (на сервере без монитора — через Xvfb):
        #   xvfb-run -a --server-args="-screen 0 1920x1080x24" python ... serve ...
        # Headless можно вернуть переменной окружения FEDRESURS_HEADLESS=1
        # (например для других сайтов, где антибота нет).
        linux_headless = os.environ.get("FEDRESURS_HEADLESS", "0") == "1"
        cmd += [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-gpu",
        ]
        if linux_headless:
            cmd += ["--headless=new"]
    else:
        cmd += ["--new-window"]
    cmd.append(url)

    if is_linux:
        # Отдельная сессия → отдельная группа процессов → killpg убьёт всех детей
        proc = subprocess.Popen(cmd, start_new_session=True)
    else:
        proc = subprocess.Popen(cmd)

    # Возвращаем None вместо TemporaryDirectory — профиль постоянный, удалять не нужно
    return proc, port, None


def pick_target_page(browser, target_url: str):
    target_host = urllib.parse.urlparse(target_url).netloc.lower()
    for context in browser.contexts:
        for page in reversed(context.pages):
            page_host = urllib.parse.urlparse(page.url).netloc.lower()
            if target_host and target_host in page_host:
                return context, page
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return context, page


# ---------------------------------------------------------------------------
# Общие шаги браузерной автоматизации
# ---------------------------------------------------------------------------

def fill_login_form(page, login: str, password: str, timeout_ms: int = 60000) -> str:
    # Ждём конкретно поле логина по ID — без широких фоллбэков чтобы не поймать rwDialogInput
    page.locator('#ctl00_ctplhMain_Login1_UserName').wait_for(
        state="visible", timeout=timeout_ms
    )
    js = """
    (payload) => {
        const { login, password } = payload;
        const loginInput = document.querySelector('#ctl00_ctplhMain_Login1_UserName');
        const passwordInput = document.querySelector('#ctl00_ctplhMain_Login1_Password');
        if (!loginInput || !passwordInput) return 'Не удалось найти поля логина/пароля';

        loginInput.focus();
        loginInput.value = login;
        loginInput.dispatchEvent(new Event('input', { bubbles: true }));
        loginInput.dispatchEvent(new Event('change', { bubbles: true }));

        passwordInput.focus();
        passwordInput.value = password;
        passwordInput.dispatchEvent(new Event('input', { bubbles: true }));
        passwordInput.dispatchEvent(new Event('change', { bubbles: true }));

        const agreementCheckbox =
            document.querySelector('#ctl00_ctplhMain_agreement') ||
            document.querySelector('input[name="ctl00$ctplhMain$agreement"]') ||
            document.querySelector('input[type="checkbox"][data-item="agreementCheckbox"]');

        if (agreementCheckbox) {
            if (!agreementCheckbox.checked && typeof agreementCheckbox.click === 'function') {
                agreementCheckbox.click();
            }
            agreementCheckbox.checked = true;
            agreementCheckbox.dispatchEvent(new Event('input', { bubbles: true }));
            agreementCheckbox.dispatchEvent(new Event('change', { bubbles: true }));
        }

        const submitButton = document.querySelector('#ctl00_ctplhMain_Login1_LoginImageButton');
        if (!submitButton) return 'Поля заполнены, но кнопка входа не найдена';
        submitButton.click();

        return agreementCheckbox
            ? 'OK: поля заполнены, галочка согласия установлена, кнопка входа нажата'
            : 'OK: поля заполнены, кнопка входа нажата (галочка согласия не найдена)';
    }
    """
    return page.evaluate(js, {"login": login, "password": password})


def ensure_logged_in(page, login: str, password: str, timeout_ms: int = 60000) -> str:
    """
    Идемпотентный вход.

    Постоянный профиль может остаться залогиненным с прошлого запуска — тогда
    Profile.aspx сразу отдаёт кабинет, формы входа на странице нет, и старое
    ожидание поля логина висело бы до таймаута. Поэтому ждём ЛИБО форму входа,
    ЛИБО маркер меню кабинета:
      - видим меню  → уже авторизованы, вход пропускаем;
      - видим форму → заполняем и логинимся;
      - не дождались ничего → снимаем скриншот + HTML-дамп для диагностики
        (капча? чужая страница?) и бросаем понятную ошибку.
    """
    login_field = page.locator('#ctl00_ctplhMain_Login1_UserName')
    menu_marker = page.locator(
        '#ctl00_ctl00_ctplhMenu_hlLegalCaseList, #ctl00_ctl00_ctplhMenu_HyperLink11'
    )

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if menu_marker.count() > 0 and menu_marker.first.is_visible():
                return "OK: уже авторизованы, вход пропущен"
        except PlaywrightError:
            pass
        try:
            if login_field.count() > 0 and login_field.first.is_visible():
                return fill_login_form(page, login, password, timeout_ms=timeout_ms)
        except PlaywrightError:
            pass
        page.wait_for_timeout(250)

    # Ни форма, ни кабинет не появились — сохраняем доказательства и падаем понятно
    ts = time.strftime("%Y%m%d_%H%M%S")
    dump_png = f"/tmp/login_fail_{ts}.png"
    dump_html = f"/tmp/login_fail_{ts}.html"
    try:
        page.screenshot(path=dump_png, full_page=True)
        Path(dump_html).write_text(page.content(), encoding="utf-8")
        print(f"[DIAG] url={page.url} dump={dump_png}")
    except Exception as e:
        print(f"[DIAG] не удалось сохранить дамп: {e}")

    raise PlaywrightError(
        f"Ни форма логина, ни меню кабинета не появились за {timeout_ms} мс. "
        f"url={page.url}, дамп: {dump_png}"
    )


# ---------------------------------------------------------------------------
# Шаги флоу "Судебные дела → последнее сообщение"
# ---------------------------------------------------------------------------

def dismiss_dialogs(page, timeout_ms: int = 5000) -> None:
    """
    Закрывает Telerik RadWindow диалоги (предупреждения, уведомления)
    которые могут появиться после входа или навигации.
    """
    try:
        ok_btn = page.locator(
            '.rwDialogButton, .rwCloseButton, '
            'button:has-text("OK"), button:has-text("Закрыть"), '
            'input[type="button"][value="OK"]'
        )
        if ok_btn.first.is_visible(timeout=timeout_ms):
            ok_btn.first.click()
            page.wait_for_timeout(300)
    except Exception:
        pass  # Диалога нет — продолжаем


def navigate_to_legal_cases(page, timeout_ms: int = 45000) -> None:
    link = page.locator('#ctl00_ctl00_ctplhMenu_hlLegalCaseList')
    link.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    link.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


def open_insolvent_picker_on_legal_cases(page, timeout_ms: int = 45000) -> None:
    picker = page.locator('input[data-item="stbBankrupt"]')
    picker.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    picker.first.click()


def search_insolvent_in_modal(page, inn: str, timeout_ms: int = 45000) -> None:
    """Общий хелпер: найти вкладку Физ.лица, ввести ИНН, выбрать первого."""
    page.wait_for_timeout(1200)

    def find_tab_container():
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
        for frame in frames:
            tab = frame.locator(
                "a.rtsLink:has-text('Физ. лица'), a[href*='InsolventListWindow.aspx'][href*='filterBy=egrip']"
            )
            if tab.count() > 0:
                return frame, tab
        return None, None

    frame, persons_tab = find_tab_container()
    if frame is None or persons_tab is None:
        frame_urls = ", ".join([f.url for f in page.frames])
        raise PlaywrightError(f"Вкладка 'Физ. лица' не найдена. Frames: {frame_urls}")

    context = page.context
    old_pages = list(context.pages)

    try:
        page.wait_for_timeout(150)
        persons_tab.first.click(timeout=timeout_ms)
    except PlaywrightError:
        frame.evaluate(
            """
            () => {
                const tab = document.querySelector("a.rtsLink .rtsTxt");
                if (!tab || !tab.textContent || !tab.textContent.includes("Физ. лица")) {
                    throw new Error("Вкладка Физ. лица не найдена");
                }
                const link = tab.closest("a");
                link.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
                link.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
                link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                if (typeof link.click === "function") link.click();
            }
            """
        )

    target_page = page
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        new_pages = [p for p in context.pages if p not in old_pages]
        if new_pages:
            target_page = new_pages[-1]
            break
        if page.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox").count() > 0:
            target_page = page
            break
        if any(
            frm.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox").count() > 0
            for frm in target_page.frames
        ):
            break
        if "InsolventListWindow.aspx" in page.url:
            target_page = page
            break
        time.sleep(0.2)

    target_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    inn_input = target_page.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox")
    search_button = target_page.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
    result_row = target_page.locator("#resultTable tbody tr[onclick*='ReturnInsolvent']")

    if inn_input.count() == 0:
        dynamic_frame = target_page.frame_locator("iframe[src*='InsolventListWindow.aspx']")
        inn_input = dynamic_frame.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox")
        search_button = dynamic_frame.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
        result_row = dynamic_frame.locator("#resultTable tbody tr[onclick*='ReturnInsolvent']")

    inn_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    inn_input.first.fill(inn)
    search_button.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    search_button.first.click()

    result_row.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    result_row.first.click()


def click_search_on_legal_cases(page, timeout_ms: int = 45000) -> None:
    search_btn = page.locator('input[data-item="SearchButton"]')
    search_btn.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(300)
    search_btn.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


def fetch_last_message_text(page, timeout_ms: int = 45000) -> str:
    """
    В таблице результатов кликнуть ссылку 'Сообщение №...',
    дождаться нового окна (window.open), извлечь текст из div.msg, закрыть окно.
    """
    result_table = page.locator("table.GridView_General.ResultView")
    result_table.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(300)

    message_link = page.locator("table.GridView_General.ResultView a.CursorHand").first
    message_link.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)

    with page.context.expect_page(timeout=20000) as popup_info:
        message_link.click()
    new_page = popup_info.value

    new_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(500)

    msg_text = new_page.evaluate(
        """
        () => {
            const div = document.querySelector('div.msg');
            if (!div) return null;
            const clone = div.cloneNode(true);
            const bold = clone.querySelector('b');
            if (bold) {
                const next = bold.nextSibling;
                if (next && next.nodeName === 'BR') next.remove();
                bold.remove();
            }
            return (clone.textContent || '').trim();
        }
        """
    )

    new_page.close()

    if not msg_text:
        raise PlaywrightError("Не удалось извлечь текст из div.msg в окне просмотра сообщения")

    return msg_text


# ---------------------------------------------------------------------------
# Шаги флоу "Создать новое сообщение"
# ---------------------------------------------------------------------------

def navigate_to_messages(page, timeout_ms: int = 45000) -> None:
    link = page.locator('#ctl00_ctl00_ctplhMenu_HyperLink11')
    link.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    link.first.click()
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)


def open_new_message_form(page, timeout_ms: int = 45000) -> None:
    create_button = page.locator('img[alt="Создать новое сообщение"]')
    create_button.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    create_button.first.click()

    insolvent_input = page.locator(
        'input#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_InsolventPicker_InsolventName'
    )
    insolvent_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    insolvent_input.first.click()


def select_creditor_claims_message_type(page, message_type_text: str, timeout_ms: int = 45000) -> None:
    message_type_input = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_MessageTypeTextBox"
    )
    message_type_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    message_type_input.first.click()
    page.wait_for_timeout(500)

    tree_root_selector = "#ctl00_cplhContent_MessageTypeTree"
    tree_scope = page
    tree_found = page.locator(tree_root_selector).count() > 0

    if not tree_found:
        for frm in page.frames:
            if frm.locator(tree_root_selector).count() > 0:
                tree_scope = frm
                tree_found = True
                break

    if not tree_found:
        frame_urls = ", ".join([f.url for f in page.frames])
        raise PlaywrightError(f"Дерево типов сообщений не найдено. Frames: {frame_urls}")

    tree_scope.locator(tree_root_selector).first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)

    expand_deadline = time.time() + timeout_ms / 1000
    while time.time() < expand_deadline:
        plus_icons = tree_scope.locator(f"{tree_root_selector} span.rtPlus:visible")
        if plus_icons.count() == 0:
            break
        plus_icons.first.click()
        page.wait_for_timeout(120)

    message_type_items = tree_scope.locator(f"{tree_root_selector} span.rtIn")
    message_type_item = None
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline and message_type_item is None:
        total_items = message_type_items.count()
        for i in range(total_items):
            item = message_type_items.nth(i)
            item_text = (item.inner_text(timeout=1000) or "").strip()
            if item_text == message_type_text:
                message_type_item = item
                break
        if message_type_item is None:
            time.sleep(0.15)

    if message_type_item is None:
        raise PlaywrightError(f"Тип сообщения '{message_type_text}' не найден в дереве")

    message_type_item.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    message_type_item.click()


def select_legal_case_and_continue(page, timeout_ms: int = 45000) -> None:
    legal_case_select = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_InsolventPicker_LegalCasesDropDownList"
    )
    legal_case_select.first.wait_for(state="visible", timeout=timeout_ms)

    selected_value = legal_case_select.first.evaluate(
        """
        (el) => {
            const options = Array.from(el.options || []);
            const target = options.find((o) => (o.value || '').trim() && (o.textContent || '').trim());
            if (!target) return null;
            el.value = target.value;
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return target.value;
        }
        """
    )
    if not selected_value:
        raise PlaywrightError("Не найден непустой вариант с номером дела в списке LegalCasesDropDownList")

    next_button = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_SelectImageButton"
    )
    next_button.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    next_button.first.click()


def _search_and_select_creditor(scope, *, creditor_inn: str | None,
                                creditor_name: str | None, timeout_ms: int) -> None:
    """
    Внутри открытой модалки выбора кредитора (scope — это Page нового окна
    ИЛИ Frame iframe-а) выполняет поиск и выбирает первую строку результата.

    Приоритет: если задан creditor_inn — ищем по полю кода фирмы (ИНН),
    иначе по полю наименования. Кнопка поиска одна и та же — btnSearchFirm.
    """
    if creditor_inn:
        # Поиск по ИНН — поле кода фирмы
        inn_input = scope.locator('#ctl00_cplhContent_ucFirmCode_CodeTextBox')
        inn_input.first.wait_for(state="visible", timeout=timeout_ms)
        inn_input.first.fill(creditor_inn)
    else:
        # Поиск по наименованию
        name_input = scope.locator('#ctl00_cplhContent_txtFirmName')
        name_input.first.wait_for(state="visible", timeout=timeout_ms)
        name_input.first.fill(creditor_name)

    search_btn = scope.locator('#ctl00_cplhContent_btnSearchFirm')
    search_btn.first.wait_for(state="visible", timeout=timeout_ms)
    search_btn.first.click()

    result_row = scope.locator('#resultTable tbody tr[onclick*="SelectCreditor"]')
    result_row.first.wait_for(state="visible", timeout=timeout_ms)
    result_row.first.click()


def fill_creditor_demand_fields(
    page,
    corr_address: str,
    email: str,
    demand_date: str,
    demand_sum: str,
    occurrence_reason: str,
    creditor_name: str | None = None,
    creditor_inn: str | None = None,
    timeout_ms: int = 60000,
) -> None:
    """
    Заполняет дополнительные поля формы для типа
    'Уведомление о получении требований кредитора'.
    Вызывать ПОСЛЕ select_legal_case_and_continue и ДО fill_message_text.

    Поиск кредитора: если задан creditor_inn — ищем по ИНН (приоритет),
    иначе по creditor_name. После заполнения основания возникновения
    жмём кнопку "Добавить требование" (btnAddCreditorDemand).
    """
    # --- Блок Публикатор ---

    # Адрес для корреспонденции
    addr_input = page.locator('input[data-element="corrAddress"]')
    addr_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    addr_input.first.fill(corr_address)

    # Электронная почта
    email_input = page.locator(
        '#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_messageListView_ctrl0_PublisherControl_tbEmail'
    )
    email_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    email_input.first.fill(email)

    # --- Блок Сообщение ---

    # Дата получения требования
    date_input = page.locator('input[data-item="dateInput"]')
    date_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    date_input.first.fill(demand_date)
    # Триггерим change чтобы датпикер принял значение
    date_input.first.dispatch_event("change")
    page.wait_for_timeout(150)

    # Кредитор — клик открывает модалку поиска (может быть window.open или Telerik RadWindow/iframe)
    creditor_input = page.locator('input[data-element="inpCreditorName"]')
    creditor_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)

    old_pages = list(page.context.pages)
    creditor_input.first.click()
    page.wait_for_timeout(2000)  # даём время на открытие

    new_pages = [p for p in page.context.pages if p not in old_pages]
    print(f"[DEBUG creditor] новых страниц после клика: {len(new_pages)}")
    print(f"[DEBUG creditor] фреймов на странице: {len(page.frames)}")
    for f in page.frames:
        print(f"[DEBUG creditor]   frame url: {f.url}")

    if new_pages:
        # Открылось новое окно (window.open)
        creditor_modal = new_pages[-1]
        creditor_modal.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(300)

        _search_and_select_creditor(
            creditor_modal,
            creditor_inn=creditor_inn,
            creditor_name=creditor_name,
            timeout_ms=timeout_ms,
        )
        creditor_modal.wait_for_event("close", timeout=10000)

    else:
        # Модалка внутри страницы — ищем iframe с txtFirmName
        creditor_frame = None
        for frm in page.frames:
            if frm.locator('#ctl00_cplhContent_txtFirmName').count() > 0:
                creditor_frame = frm
                break

        if creditor_frame is None:
            # Ждём ещё немного и повторяем поиск
            page.wait_for_timeout(3000)
            for frm in page.frames:
                print(f"[DEBUG creditor retry] frame url: {frm.url}")
                if frm.locator('#ctl00_cplhContent_txtFirmName').count() > 0:
                    creditor_frame = frm
                    break

        if creditor_frame is None:
            raise PlaywrightError(
                f"Модалка выбора кредитора не найдена. "
                f"Фреймы: {[f.url for f in page.frames]}, "
                f"Страницы: {[p.url for p in page.context.pages]}"
            )

        firm_name_input = creditor_frame.locator('#ctl00_cplhContent_txtFirmName')
        firm_name_input.wait_for(state="visible", timeout=timeout_ms)

        _search_and_select_creditor(
            creditor_frame,
            creditor_inn=creditor_inn,
            creditor_name=creditor_name,
            timeout_ms=timeout_ms,
        )

    page.wait_for_timeout(800)

    # Сумма требования
    sum_input = page.locator(
        '#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_messageListView_ctrl0_ObjectProxy_ctrl0_ReceivingCreditorDemand2Message_ObjectProxy_ctrl0_ObjectProxyView1_ctrl0_DemandList_txtSum'
    )
    sum_input.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    sum_input.first.fill(demand_sum)

    # Основание возникновения требования
    reason_textarea = page.locator(
        '#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_messageListView_ctrl0_ObjectProxy_ctrl0_ReceivingCreditorDemand2Message_ObjectProxy_ctrl0_ObjectProxyView1_ctrl0_DemandList_txtOccurenceReason'
    )
    reason_textarea.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    reason_textarea.first.fill(occurrence_reason)

    # Кнопка "Добавить требование" — жмём после заполнения основания.
    # У кнопки нет id, выбираем по data-element. onclick="return false;"
    # отменяет дефолт, но навешанный JS-обработчик при click() отработает.
    add_demand_btn = page.locator('input[data-element="btnAddCreditorDemand"]')
    add_demand_btn.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    add_demand_btn.first.click()
    page.wait_for_timeout(800)


def fill_message_text(page, message_text: str, timeout_ms: int = 60000) -> None:
    message_textarea = page.locator('textarea[data-item="message-text"]')
    message_textarea.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    message_textarea.first.fill(message_text)


def click_save_button(page, timeout_ms: int = 60000) -> None:
    save_btn = page.locator('#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_btnTempSaveClick')
    save_btn.first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(150)
    save_btn.first.click()


# ---------------------------------------------------------------------------
# Флоу 1: создать новое сообщение (старое API /run)
# ---------------------------------------------------------------------------

def run_automation(
    *,
    login: str,
    password: str,
    inn: str,
    message_text: str,
    message_type_text: str,
    chrome_path: str,
    url: str,
    delay_ms: int,
    cdp_timeout_sec: float,
    # Дополнительные поля — только для MESSAGE_TYPE_CREDITOR_DEMAND
    corr_address: str | None = None,
    email: str | None = None,
    demand_date: str | None = None,
    creditor_name: str | None = None,
    creditor_inn: str | None = None,
    demand_sum: str | None = None,
    occurrence_reason: str | None = None,
) -> dict:
    # Валидация: для типа кредиторского уведомления доп. поля обязательны
    if message_type_text == MESSAGE_TYPE_CREDITOR_DEMAND:
        missing = [
            name for name, val in [
                ("corr_address", corr_address),
                ("email", email),
                ("demand_date", demand_date),
                ("demand_sum", demand_sum),
                ("occurrence_reason", occurrence_reason),
            ] if not val
        ]
        if missing:
            return {
                "ok": False,
                "error": f"Для типа '{MESSAGE_TYPE_CREDITOR_DEMAND}' обязательны поля: {', '.join(missing)}",
            }
        # Кредитор задаётся ИНН-ом или названием; нужно хотя бы одно
        if not creditor_inn and not creditor_name:
            return {
                "ok": False,
                "error": f"Для типа '{MESSAGE_TYPE_CREDITOR_DEMAND}' укажите creditor_inn или creditor_name",
            }

    proc, port, temp_profile_dir = launch_chrome(chrome_path, url, delay_ms, cdp_timeout_sec)
    try:
        if not wait_cdp_ready(port, timeout_sec=max(cdp_timeout_sec, 1.0)):
            return {"ok": False, "error": "CDP не поднялся вовремя", "pid": proc.pid}

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            _context, page = pick_target_page(browser, url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            result = ensure_logged_in(page, login, password)  # форма ИЛИ уже-залогинен
            if not result.startswith("OK:"):
                return {"ok": False, "error": result, "pid": proc.pid}
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(500)
            dismiss_dialogs(page)
            page.wait_for_timeout(150)

            open_new_message_form(page)
            page.wait_for_timeout(150)
            search_insolvent_in_modal(page, inn=inn)
            page.wait_for_timeout(150)
            select_creditor_claims_message_type(page, message_type_text=message_type_text)
            page.wait_for_timeout(150)
            select_legal_case_and_continue(page)
            page.wait_for_timeout(150)

            # Доп. поля только для типа кредиторского уведомления
            if message_type_text == MESSAGE_TYPE_CREDITOR_DEMAND:
                fill_creditor_demand_fields(
                    page,
                    corr_address=corr_address,
                    email=email,
                    demand_date=demand_date,
                    creditor_name=creditor_name,
                    creditor_inn=creditor_inn,
                    demand_sum=demand_sum,
                    occurrence_reason=occurrence_reason,
                )
                page.wait_for_timeout(150)

            fill_message_text(page, message_text=message_text)
            page.wait_for_timeout(150)
            click_save_button(page)
            page.wait_for_timeout(3000)

        return {"ok": True, "pid": proc.pid}
    except PlaywrightError as exc:
        return {"ok": False, "error": str(exc), "pid": proc.pid}
    finally:
        kill_chrome(proc)


# ---------------------------------------------------------------------------
# Флоу 2: найти последнее сообщение по ИНН и вернуть его текст (новое API)
# ---------------------------------------------------------------------------

def fetch_last_message_automation(*, login: str, password: str, inn: str,
                                   chrome_path: str, url: str, delay_ms: int,
                                   cdp_timeout_sec: float) -> dict:
    proc, port, temp_profile_dir = launch_chrome(chrome_path, url, delay_ms, cdp_timeout_sec)
    try:
        if not wait_cdp_ready(port, timeout_sec=max(cdp_timeout_sec, 1.0)):
            return {"ok": False, "error": "CDP не поднялся вовремя", "pid": proc.pid}

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            _context, page = pick_target_page(browser, url)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 1. Авторизация (идемпотентная)
            result = ensure_logged_in(page, login, password)
            if not result.startswith("OK:"):
                return {"ok": False, "error": result, "pid": proc.pid}
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(500)
            dismiss_dialogs(page)
            page.wait_for_timeout(150)

            # 2. Судебные дела → поиск должника → поиск → текст сообщения
            navigate_to_legal_cases(page)
            page.wait_for_timeout(300)
            open_insolvent_picker_on_legal_cases(page)
            page.wait_for_timeout(300)
            search_insolvent_in_modal(page, inn=inn)
            page.wait_for_timeout(300)
            click_search_on_legal_cases(page)
            page.wait_for_timeout(300)
            msg_text = fetch_last_message_text(page)

        return {"ok": True, "message_text": msg_text, "pid": proc.pid}
    except PlaywrightError as exc:
        return {"ok": False, "error": str(exc), "pid": proc.pid}
    finally:
        kill_chrome(proc)


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------

def build_app(service: FedresursAutomationService, chrome_path: str) -> FastAPI:
    app = FastAPI(title="Fedresurs Automation API")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "max_parallel_jobs": service.max_parallel_jobs,
            "queue_if_busy": service.queue_if_busy,
        }

    @app.post("/run")
    def run_job(payload: JobRequest):
        """Создать новое сообщение на fedresurs."""
        def job():
            started_at = time.time()
            result = run_automation(
                login=payload.login,
                password=payload.password,
                inn=payload.inn,
                message_text=payload.message_text,
                message_type_text=payload.message_type_text,
                chrome_path=chrome_path,
                url=TARGET_URL,
                delay_ms=2500,
                cdp_timeout_sec=20.0,
                corr_address=payload.corr_address,
                email=payload.email,
                demand_date=payload.demand_date,
                creditor_name=payload.creditor_name,
                creditor_inn=payload.creditor_inn,
                demand_sum=payload.demand_sum,
                occurrence_reason=payload.occurrence_reason,
            )
            result["elapsed_sec"] = round(time.time() - started_at, 2)
            return result

        future = service.submit(job)
        try:
            result = future.result()
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result)
        return result

    @app.post("/fetch-last-message")
    def fetch_last_message(payload: FetchLastMessageRequest):
        """
        Найти должника по ИНН, открыть последнее сообщение из судебных дел,
        вернуть его текст.

        Ответ: { "ok": true, "message_text": "...", "elapsed_sec": 12.3 }
        """
        def job():
            started_at = time.time()
            result = fetch_last_message_automation(
                login=payload.login,
                password=payload.password,
                inn=payload.inn,
                chrome_path=chrome_path,
                url=TARGET_URL,
                delay_ms=2500,
                cdp_timeout_sec=20.0,
            )
            result["elapsed_sec"] = round(time.time() - started_at, 2)
            return result

        future = service.submit(job)
        try:
            result = future.result()
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result)
        return result

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Fedresurs automation")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    run_parser = subparsers.add_parser("run", help="Создать новое сообщение (CLI)")
    run_parser.add_argument("--login", required=True)
    run_parser.add_argument("--password", required=True)
    run_parser.add_argument("--inn", required=True)
    run_parser.add_argument("--message-text", required=True)
    run_parser.add_argument("--message-type-text",
                            default="Уведомление о получении требований кредитора")
    run_parser.add_argument("--url", default=TARGET_URL)
    run_parser.add_argument("--delay-ms", type=int, default=2500)
    run_parser.add_argument("--browser-path", default=None)
    run_parser.add_argument("--cdp-timeout-sec", type=float, default=20.0)

    fetch_parser = subparsers.add_parser("fetch", help="Получить текст последнего сообщения (CLI)")
    fetch_parser.add_argument("--login", required=True)
    fetch_parser.add_argument("--password", required=True)
    fetch_parser.add_argument("--inn", required=True)
    fetch_parser.add_argument("--url", default=TARGET_URL)
    fetch_parser.add_argument("--delay-ms", type=int, default=2500)
    fetch_parser.add_argument("--browser-path", default=None)
    fetch_parser.add_argument("--cdp-timeout-sec", type=float, default=20.0)

    serve_parser = subparsers.add_parser("serve", help="HTTP API сервер")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8080)
    serve_parser.add_argument("--workers", type=int, default=1,
                              help="ВАЖНО: профиль общий → ставьте 1, иначе два Chrome подерутся за профиль")
    serve_parser.add_argument("--queue", action="store_true",
                              help="Ставить задачи в очередь вместо отказа 429 при занятости")
    serve_parser.add_argument("--browser-path", default=None)

    args = parser.parse_args()

    if args.mode == "run":
        chrome_path = args.browser_path or detect_system_chrome()
        if not chrome_path:
            print("Не найден Google Chrome. Передайте --browser-path")
            return 1
        result = run_automation(
            login=args.login,
            password=args.password,
            inn=args.inn,
            message_text=args.message_text,
            message_type_text=args.message_type_text,
            chrome_path=chrome_path,
            url=args.url,
            delay_ms=args.delay_ms,
            cdp_timeout_sec=args.cdp_timeout_sec,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.mode == "fetch":
        chrome_path = args.browser_path or detect_system_chrome()
        if not chrome_path:
            print("Не найден Google Chrome. Передайте --browser-path")
            return 1
        result = fetch_last_message_automation(
            login=args.login,
            password=args.password,
            inn=args.inn,
            chrome_path=chrome_path,
            url=args.url,
            delay_ms=args.delay_ms,
            cdp_timeout_sec=args.cdp_timeout_sec,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    # serve
    chrome_path = args.browser_path or detect_system_chrome()
    if not chrome_path:
        print("Не найден Google Chrome. Передайте --browser-path")
        return 1

    service = FedresursAutomationService(
        chrome_path=chrome_path,
        max_parallel_jobs=args.workers,
        queue_if_busy=args.queue,
    )
    app = build_app(service, chrome_path)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())