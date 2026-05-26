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
    persons_tab = page.locator("a.rtsLink:has-text('Физ. лица')")
    persons_tab.first.wait_for(state="visible", timeout=timeout_ms)

    context = page.context
    old_pages = list(context.pages)
    persons_tab.first.click()

    target_page = page
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        new_pages = [p for p in context.pages if p not in old_pages]
        if new_pages:
            target_page = new_pages[-1]
            break
        if "InsolventListWindow.aspx" in page.url:
            target_page = page
            break
        time.sleep(0.2)

    target_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    last_name_input = target_page.locator("#ctl00_cplhContent_InsolventList_tbLastNameEgrip")
    last_name_input.first.wait_for(state="visible", timeout=timeout_ms)
    last_name_input.first.fill(query)

    search_button = target_page.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
    search_button.first.wait_for(state="visible", timeout=timeout_ms)
    search_button.first.click()

    return f"OK: открыта вкладка физ. лиц, введено '{query}', выполнен поиск"

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
        except PlaywrightError as exc:
            print(f"Не удалось подключиться к вкладке Chrome через CDP: {exc}")
            print("Но браузер открыт вашим chrome.exe — можно войти вручную.")
            return 1
        finally:
            safe_cleanup_temp_profile(temp_profile_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
