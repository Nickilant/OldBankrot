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
import os
import platform
import socket
import subprocess
import sys
import time
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
    parser = argparse.ArgumentParser(description="Открытие страницы входа через установленный Google Chrome")
    parser.add_argument("--login", required=True, help="Логин")
    parser.add_argument("--password", required=True, help="Пароль")
    parser.add_argument("--url", default=TARGET_URL, help="URL страницы входа")
    parser.add_argument("--delay-ms", type=int, default=2500, help="Задержка перед автозаполнением (мс)")
    parser.add_argument("--user-agent", default=DEFAULT_CHROME_UA, help="User-Agent для вкладки")
    parser.add_argument("--browser-path", default=None, help="Путь к установленному Google Chrome")
    parser.add_argument("--profile-dir", default=None, help="Папка профиля Chrome (опционально)")
    parser.add_argument("--no-autofill", action="store_true", help="Только открыть URL в вашем Chrome без автозаполнения")

    args = parser.parse_args()

    chrome_path = args.browser_path or detect_system_chrome()
    if not chrome_path:
        print("Не найден установленный Google Chrome. Передайте --browser-path")
        return 1

    port = free_port()
    cmd = [chrome_path, f"--remote-debugging-port={port}", args.url]
    if args.profile_dir:
        cmd.append(f"--user-data-dir={args.profile_dir}")

    proc = subprocess.Popen(cmd)
    print(f"Запущен ВАШ Google Chrome: {chrome_path}")
    print(f"PID: {proc.pid}")

    if args.no_autofill:
        print("Автозаполнение отключено (--no-autofill).")
        return 0

    time.sleep(max(args.delay_ms, 0) / 1000)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context(user_agent=args.user_agent)
            page = context.pages[-1] if context.pages else context.new_page()

            if page.url == "about:blank":
                page.goto(args.url, wait_until="domcontentloaded", timeout=45000)

            result = fill_login_form(page, args.login, args.password)
            print(result)
        except PlaywrightError as exc:
            print(f"Не удалось подключиться к вкладке Chrome через CDP: {exc}")
            print("Но браузер открыт вашим chrome.exe — можно войти вручную.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
