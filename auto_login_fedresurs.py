#!/usr/bin/env python3
"""
Автоматизация входа на old.bankrot.fedresurs.ru через WebView (PyQt5).

Установка зависимостей:
    pip install PyQt5 PyQtWebEngine

Запуск:
    python auto_login_fedresurs.py --login Zakirov5 --password 3DqEdz
"""

import argparse
import html
import sys

from PyQt5.QtCore import QTimer, QUrl
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWebEngineWidgets import QWebEngineView

TARGET_URL = "https://old.bankrot.fedresurs.ru/BackOffice/ArbitrManager/Profile.aspx?storage=true"


def build_js(login: str, password: str) -> str:
    safe_login = html.escape(login, quote=True)
    safe_password = html.escape(password, quote=True)

    # Подставляем данные в первое найденное поле логина/пароля и нажимаем кнопку входа.
    return f"""
    (function() {{
        function findInputByHints(hints, typeName) {{
            const inputs = Array.from(document.querySelectorAll('input'));
            return inputs.find(i => {{
                const hay = [
                    i.id || '',
                    i.name || '',
                    i.placeholder || '',
                    i.className || '',
                    i.getAttribute('aria-label') || ''
                ].join(' ').toLowerCase();

                const byHint = hints.some(h => hay.includes(h));
                const byType = typeName ? (i.type || '').toLowerCase() === typeName : true;
                return byHint || byType;
            }}) || null;
        }}

        const loginInput =
            findInputByHints(['login', 'логин', 'username', 'user'], null) ||
            document.querySelector('input[type="text"]');

        const passwordInput =
            findInputByHints(['password', 'пароль', 'pass'], 'password') ||
            document.querySelector('input[type="password"]');

        if (!loginInput || !passwordInput) {{
            return 'Не удалось найти поля логина/пароля';
        }}

        loginInput.focus();
        loginInput.value = '{safe_login}';
        loginInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
        loginInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

        passwordInput.focus();
        passwordInput.value = '{safe_password}';
        passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
        passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

        const submitButton = Array.from(document.querySelectorAll('button, input[type="submit"], a'))
            .find(el => (el.textContent || el.value || '').toLowerCase().includes('вход') ||
                        (el.textContent || el.value || '').toLowerCase().includes('login') ||
                        (el.id || '').toLowerCase().includes('login') ||
                        (el.name || '').toLowerCase().includes('login'));

        if (!submitButton) {{
            return 'Поля заполнены, но кнопка входа не найдена';
        }}

        submitButton.click();
        return 'OK: поля заполнены, кнопка входа нажата';
    }})();
    """


def main() -> int:
    parser = argparse.ArgumentParser(description="Автологин на old.bankrot.fedresurs.ru через WebView")
    parser.add_argument("--login", required=True, help="Логин")
    parser.add_argument("--password", required=True, help="Пароль")
    parser.add_argument("--url", default=TARGET_URL, help="URL страницы входа")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=2500,
        help="Задержка перед автозаполнением после загрузки страницы (мс)",
    )

    args = parser.parse_args()

    app = QApplication(sys.argv)
    view = QWebEngineView()
    view.setWindowTitle("Fedresurs Auto Login")
    view.resize(1280, 900)

    def run_autofill():
        js = build_js(args.login, args.password)
        view.page().runJavaScript(js, lambda result: print(result))

    def on_load_finished(ok: bool):
        if not ok:
            print("Ошибка загрузки страницы")
            return
        QTimer.singleShot(max(args.delay_ms, 0), run_autofill)

    view.loadFinished.connect(on_load_finished)
    view.load(QUrl(args.url))
    view.show()

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
