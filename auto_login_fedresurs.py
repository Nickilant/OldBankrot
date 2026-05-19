#!/usr/bin/env python3
"""
Автоматизация входа на old.bankrot.fedresurs.ru через Playwright Chromium.

Установка зависимостей:
    pip install playwright
    playwright install chromium

Запуск:
    python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
"""

import argparse
import os
import platform
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
            findInputByHints(['login', 'логин', 'username', 'user'], null) ||
            document.querySelector('input[type="text"]');

        const passwordInput =
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

        const submitButton = Array.from(document.querySelectorAll('button, input[type="submit"], a'))
            .find((el) => (el.textContent || el.value || '').toLowerCase().includes('вход') ||
                          (el.textContent || el.value || '').toLowerCase().includes('login') ||
                          (el.id || '').toLowerCase().includes('login') ||
                          (el.name || '').toLowerCase().includes('login'));

        if (!submitButton) {
            return 'Поля заполнены, но кнопка входа не найдена';
        }

        submitButton.click();
        return 'OK: поля заполнены, кнопка входа нажата';
    }
    """
    return page.evaluate(js, {"login": login, "password": password})


def main() -> int:
    parser = argparse.ArgumentParser(description="Автологин на old.bankrot.fedresurs.ru через Chromium")
    parser.add_argument("--login", required=True, help="Логин")
    parser.add_argument("--password", required=True, help="Пароль")
    parser.add_argument("--url", default=TARGET_URL, help="URL страницы входа")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=2500,
        help="Задержка перед автозаполнением после загрузки страницы (мс)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_CHROME_UA,
        help="User-Agent для Chromium",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Запуск без GUI (по умолчанию с окном браузера)",
    )
    parser.add_argument(
        "--browser-path",
        default=None,
        help="Путь к установленному Google Chrome (если не указан, пробуем найти автоматически)",
    )

    args = parser.parse_args()

    chrome_path = args.browser_path or detect_system_chrome()

    with sync_playwright() as p:
        launch_kwargs = {"headless": args.headless}
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
            print(f"Запуск через системный Chrome: {chrome_path}")
        else:
            print("Системный Chrome не найден, запускаем встроенный Chromium Playwright")

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(user_agent=args.user_agent)
        page = context.new_page()

        try:
            response = page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            if response is not None and response.status == 403:
                print("403 Forbidden даже в Chromium. Вероятно блок по IP/anti-bot на стороне сайта.")

            time.sleep(max(args.delay_ms, 0) / 1000)
            result = fill_login_form(page, args.login, args.password)
            print(result)

            if not args.headless:
                print("Браузер открыт. Закройте окно вручную после проверки.")
                page.wait_for_event("close", timeout=0)
        except PlaywrightTimeoutError:
            print("Таймаут загрузки страницы")
            return 1
        finally:
            context.close()
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
