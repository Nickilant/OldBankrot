#!/usr/bin/env python3
"""
Открытие old.bankrot.fedresurs.ru в установленном Google Chrome пользователя
с опциональным автозаполнением через CDP.

Установка зависимостей:
    pip install playwright

Запуск:
    python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

TARGET_URL = "https://old.bankrot.fedresurs.ru/BackOffice/ArbitrManager/Profile.aspx?storage=true"
DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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


def safe_cleanup_temp_profile(temp_profile_dir: tempfile.TemporaryDirectory | None) -> None:
    if temp_profile_dir is None:
        return
    try:
        temp_profile_dir.cleanup()
    except PermissionError:
        print(
            "Не удалось удалить временный профиль Chrome (файлы заняты запущенным браузером). "
            f"Папка останется на диске: {temp_profile_dir.name}"
        )


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


def fill_login_form(page, login: str, password: str) -> str:
    js = """
    (payload) => {
        const { login, password } = payload;

        function findInputByHints(hints, typeName) {
            const inputs = Array.from(document.querySelectorAll('input'));
            return inputs.find((i) => {
                const hay = [
                    i.id || '',
                    i.name || '',
                    i.placeholder || '',
                    i.className || '',
                    i.getAttribute('aria-label') || '',
                ].join(' ').toLowerCase();

                const byHint = hints.some((h) => hay.includes(h));
                const byType = typeName ? (i.type || '').toLowerCase() === typeName : true;
                return byHint || byType;
            }) || null;
        }

        const loginInput =
            document.querySelector('#ctl00_ctplhMain_Login1_UserName') ||
            document.querySelector('input[name="ctl00$ctplhMain$Login1$UserName"]') ||
            findInputByHints(['login', 'логин', 'username', 'user'], null) ||
            document.querySelector('input[type="text"]');

        const passwordInput =
            document.querySelector('#ctl00_ctplhMain_Login1_Password') ||
            document.querySelector('input[name="ctl00$ctplhMain$Login1$Password"]') ||
            findInputByHints(['password', 'пароль', 'pass'], 'password') ||
            document.querySelector('input[type="password"]');

        if (!loginInput || !passwordInput) {
            return 'Не удалось найти поля логина/пароля';
        }

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
            if (!agreementCheckbox.checked && typeof agreementCheckbox.click === "function") {
                agreementCheckbox.click();
            }
            agreementCheckbox.checked = true;
            agreementCheckbox.dispatchEvent(new Event('input', { bubbles: true }));
            agreementCheckbox.dispatchEvent(new Event('change', { bubbles: true }));
        }

        const submitButton =
            document.querySelector('#ctl00_ctplhMain_Login1_LoginImageButton') ||
            document.querySelector('input[name="ctl00$ctplhMain$Login1$LoginImageButton"]') ||
            document.querySelector('input[type="image"][alt*="Войти" i]') ||
            Array.from(document.querySelectorAll('button, input[type="submit"], input[type="image"], a'))
                .find((el) => (el.textContent || el.value || el.alt || '').toLowerCase().includes('вход') ||
                              (el.textContent || el.value || el.alt || '').toLowerCase().includes('login') ||
                              (el.id || '').toLowerCase().includes('login') ||
                              (el.name || '').toLowerCase().includes('login'));

        if (!submitButton) {
            return 'Поля заполнены, но кнопка входа не найдена';
        }

        submitButton.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        submitButton.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
        submitButton.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        if (typeof submitButton.click === "function") {
            submitButton.click();
        }

        if (typeof window.WebForm_DoPostBackWithOptions === 'function' && submitButton.name) {
            window.WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions(submitButton.name, '', true, 'ctl00$ctplhMain$Login1', '', false, false));
        } else {
            const form = submitButton.form || submitButton.closest('form');
            if (form && typeof form.submit === 'function') {
                form.submit();
            }
        }

        return agreementCheckbox
            ? 'OK: поля заполнены, галочка согласия установлена, кнопка входа нажата'
            : 'OK: поля заполнены, кнопка входа нажата (галочка согласия не найдена)';
    }
    """
    return page.evaluate(js, {"login": login, "password": password})


def open_new_message_form(page, timeout_ms: int = 45000) -> str:
    create_button = page.locator('img[alt="Создать новое сообщение"]')
    create_button.first.wait_for(state="visible", timeout=timeout_ms)
    create_button.first.click()

    insolvent_input = page.locator(
        'input#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_InsolventPicker_InsolventName'
    )
    insolvent_input.first.wait_for(state="visible", timeout=timeout_ms)
    insolvent_input.first.click()
    return "OK: открыта форма нового сообщения и активировано поле выбора должника"




def search_individual_insolvent(page, query: str = "Абасс", timeout_ms: int = 45000) -> str:
    print("[DEBUG] Подготовка к переходу на вкладку 'Физ. лица'...")
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
        raise PlaywrightError(f"Вкладка 'Физ. лица' не найдена ни в одном фрейме. Frames: {frame_urls}")

    print(f"[DEBUG] Вкладка найдена. URL страницы: {page.url}; URL фрейма: {frame.url}")

    context = page.context
    old_pages = list(context.pages)
    try:
        persons_tab.first.click(timeout=timeout_ms)
        print("[DEBUG] Клик по вкладке выполнен через Playwright.")
    except PlaywrightError:
        print("[DEBUG] Обычный клик не сработал, пробуем JS-клик по вкладке.")
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
                if (typeof link.click === "function") {
                    link.click();
                }
            }
            """
        )

    target_page = page
    detection_mode = "not-detected"
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        new_pages = [p for p in context.pages if p not in old_pages]
        if new_pages:
            target_page = new_pages[-1]
            detection_mode = "new-page"
            break

        if page.locator("#ctl00_cplhContent_InsolventList_tbLastNameEgrip").count() > 0:
            target_page = page
            detection_mode = "same-page-dom"
            break

        frame_input_detected = False
        for frm in target_page.frames:
            if frm.locator("#ctl00_cplhContent_InsolventList_tbLastNameEgrip").count() > 0:
                detection_mode = "iframe-dom"
                frame_input_detected = True
                break
        if frame_input_detected:
            break

        if "InsolventListWindow.aspx" in page.url:
            target_page = page
            detection_mode = "same-page-url"
            break
        time.sleep(0.2)

    print(f"[DEBUG] Режим открытия вкладки: {detection_mode}. Текущий URL: {target_page.url}")
    target_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    inn_input = target_page.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox")
    search_button = target_page.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
    result_row = target_page.locator("#resultTable tbody tr[onclick*='ReturnInsolvent']")
    if inn_input.count() == 0:
        print("[DEBUG] Поле ИНН не найдено в основном DOM, пробуем iframe.")
        dynamic_frame = target_page.frame_locator("iframe[src*='InsolventListWindow.aspx']")
        inn_input = dynamic_frame.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox")
        search_button = dynamic_frame.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
        result_row = dynamic_frame.locator("#resultTable tbody tr[onclick*='ReturnInsolvent']")
    else:
        print("[DEBUG] Поле ИНН найдено в основном DOM страницы.")

    inn_input.first.wait_for(state="visible", timeout=timeout_ms)
    inn_input.first.fill("051100482760")
    print("[DEBUG] Введено значение ИНН: 051100482760")

    search_button.first.wait_for(state="visible", timeout=timeout_ms)
    search_button.first.click()
    print("[DEBUG] Кнопка поиска нажата.")

    result_row.first.wait_for(state="visible", timeout=timeout_ms)
    result_row.first.click()
    print("[DEBUG] Выбрана единственная строка результата, модальное окно выбора должника закрыто.")

    return "OK: открыта вкладка физ. лиц, введен ИНН, выполнен поиск и выбран найденный должник"


def select_creditor_claims_message_type(page, timeout_ms: int = 45000) -> str:
    print("[DEBUG] Открываем выбор типа сообщения...")
    message_type_input = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_MessageTypeTextBox"
    )
    message_type_input.first.wait_for(state="visible", timeout=timeout_ms)
    message_type_input.first.click()
    page.wait_for_timeout(700)

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

    print("[DEBUG] Дерево типов сообщений найдено, раскрываем 'Требования кредиторов'.")

    category_node = tree_scope.locator(
        f"{tree_root_selector} li:has(span.rtIn:has-text('Требования кредиторов'))"
    ).first
    category_node.wait_for(state="visible", timeout=timeout_ms)

    plus_icon = category_node.locator("span.rtPlus")
    if plus_icon.count() > 0:
        plus_icon.first.click()
        print("[DEBUG] Клик по '+' выполнен.")
    else:
        category_node.locator("span.rtIn:has-text('Требования кредиторов')").first.click()
        print("[DEBUG] '+' не найден, кликнули по тексту категории.")

    message_type_item = tree_scope.locator(
        f"{tree_root_selector} span.rtIn:has-text('Уведомление о получении требований кредитора')"
    ).first
    message_type_item.wait_for(state="visible", timeout=timeout_ms)
    message_type_item.click()
    print("[DEBUG] Выбран тип: 'Уведомление о получении требований кредитора'.")
    return "OK: выбран тип сообщения 'Уведомление о получении требований кредитора'"


def select_legal_case_and_continue(page, timeout_ms: int = 45000) -> str:
    print("[DEBUG] Выбираем номер дела из выпадающего списка...")
    legal_case_select = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_InsolventPicker_LegalCasesDropDownList"
    )
    legal_case_select.first.wait_for(state="visible", timeout=timeout_ms)

    selected_value = legal_case_select.first.evaluate(
        """
        (el) => {
            const options = Array.from(el.options || []);
            const target = options.find((o) => (o.value || '').trim() && (o.textContent || '').trim());
            if (!target) {
                return null;
            }
            el.value = target.value;
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return target.value;
        }
        """
    )
    if not selected_value:
        raise PlaywrightError("Не найден непустой вариант с номером дела в списке LegalCasesDropDownList")
    print(f"[DEBUG] Выбран номер дела, value={selected_value}")

    next_button = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_SelectImageButton"
    )
    next_button.first.wait_for(state="visible", timeout=timeout_ms)
    next_button.first.click()
    print("[DEBUG] Нажата кнопка 'Далее'.")
    return "OK: выбран номер дела и нажата кнопка 'Далее'"


def fill_message_text(page, timeout_ms: int = 60000) -> str:
    print("[DEBUG] Ожидание страницы создания сообщения...")
    message_textarea = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_messageListView_ctrl0_ObjectProxy_ctrl0_ReceivingCreditorDemand2Message_ObjectProxy_ctrl0_ObjectProxyView1_ctrl0_Message"
    )
    message_textarea.first.wait_for(state="visible", timeout=timeout_ms)
    message_textarea.first.fill("Это тестовый текст, все вроде супер работает")
    print("[DEBUG] Текст сообщения заполнен.")
    return "OK: текст сообщения заполнен"

def main() -> int:
    parser = argparse.ArgumentParser(description="Открытие страницы входа через установленный Google Chrome")
    parser.add_argument("--login", required=True, help="Логин")
    parser.add_argument("--password", required=True, help="Пароль")
    parser.add_argument("--url", default=TARGET_URL, help="URL страницы входа")
    parser.add_argument("--delay-ms", type=int, default=2500, help="Задержка перед автозаполнением (мс)")
    parser.add_argument("--user-agent", default=DEFAULT_CHROME_UA, help="User-Agent для вкладки")
    parser.add_argument("--browser-path", default=None, help="Путь к установленному Google Chrome")
    parser.add_argument("--profile-dir", default=None, help="Папка профиля Chrome (опционально)")
    parser.add_argument("--no-autofill", action="store_true", help="Только открыть URL в вашем Chrome без автозаполнения")
    parser.add_argument("--cdp-timeout-sec", type=float, default=20.0, help="Сколько ждать готовности CDP (сек)")

    args = parser.parse_args()

    chrome_path = args.browser_path or detect_system_chrome()
    if not chrome_path:
        print("Не найден установленный Google Chrome. Передайте --browser-path")
        return 1

    port = free_port()
    temp_profile_dir = None
    profile_dir = args.profile_dir
    if not profile_dir:
        temp_profile_dir = tempfile.TemporaryDirectory(prefix="fedresurs-chrome-")
        profile_dir = temp_profile_dir.name

    cmd = [
        chrome_path,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--new-window",
        args.url,
    ]

    proc = subprocess.Popen(cmd)
    print(f"Запущен ВАШ Google Chrome: {chrome_path}")
    print(f"PID: {proc.pid}")

    if args.no_autofill:
        print("Автозаполнение отключено (--no-autofill).")
        return 0

    if not wait_cdp_ready(port, timeout_sec=max(args.cdp_timeout_sec, 1.0)):
        print("CDP не поднялся вовремя. Проверьте, не блокирует ли антивирус/политики флаг remote-debugging.")
        print("Браузер открыт вашим chrome.exe — можно войти вручную.")
        safe_cleanup_temp_profile(temp_profile_dir)
        return 1

    time.sleep(max(args.delay_ms, 0) / 1000)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            _context, page = pick_target_page(browser, args.url)
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(max(args.delay_ms, 0))

            result = fill_login_form(page, args.login, args.password)
            print(result)
            if "Не удалось найти поля" in result:
                print(f"Текущий URL: {page.url}")
                print(f"Заголовок страницы: {page.title()}")
            elif result.startswith("OK:"):
                post_login_result = open_new_message_form(page)
                print(post_login_result)
                search_result = search_individual_insolvent(page)
                print(search_result)
                message_type_result = select_creditor_claims_message_type(page)
                print(message_type_result)
                legal_case_result = select_legal_case_and_continue(page)
                print(legal_case_result)
                fill_message_result = fill_message_text(page)
                print(fill_message_result)
        except PlaywrightError as exc:
            print(f"Не удалось подключиться к вкладке Chrome через CDP: {exc}")
            print("Но браузер открыт вашим chrome.exe — можно войти вручную.")
            return 1
        finally:
            safe_cleanup_temp_profile(temp_profile_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
