#!/usr/bin/env python3
"""
Автоматизация old.bankrot.fedresurs.ru как CLI и как HTTP API.

Установка зависимостей:
    pip install playwright fastapi uvicorn

Запуск API:
    python auto_login_fedresurs.py serve --host 0.0.0.0 --port 8080 --workers 2

Запуск CLI (обратная совместимость):
    python auto_login_fedresurs.py run --login USER --password PASS --inn 051100482760 --message-text "Текст"
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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


class JobRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)
    inn: str = Field(min_length=1)
    message_text: str = Field(min_length=1)


class FedresursAutomationService:
    def __init__(self, *, chrome_path: str | None, max_parallel_jobs: int, queue_if_busy: bool) -> None:
        self.chrome_path = chrome_path or detect_system_chrome()
        if not self.chrome_path:
            raise RuntimeError("Не найден установленный Google Chrome. Передайте --browser-path")
        self.max_parallel_jobs = max(1, max_parallel_jobs)
        self.queue_if_busy = queue_if_busy
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel_jobs)
        self.semaphore = threading.Semaphore(self.max_parallel_jobs)

    def submit(self, payload: JobRequest):
        return self.executor.submit(self._run_job, payload)

    def _run_job(self, payload: JobRequest) -> dict:
        if self.queue_if_busy:
            with self.semaphore:
                return self._run_once(payload)

        if not self.semaphore.acquire(blocking=False):
            raise RuntimeError("Сервис занят: достигнут лимит параллельных задач")
        try:
            return self._run_once(payload)
        finally:
            self.semaphore.release()

    def _run_once(self, payload: JobRequest) -> dict:
        started_at = time.time()
        result = run_automation(
            login=payload.login,
            password=payload.password,
            inn=payload.inn,
            message_text=payload.message_text,
            chrome_path=self.chrome_path,
            url=TARGET_URL,
            delay_ms=2500,
            cdp_timeout_sec=20.0,
        )
        elapsed = round(time.time() - started_at, 2)
        result["elapsed_sec"] = elapsed
        return result


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
        print(f"Не удалось удалить временный профиль Chrome: {temp_profile_dir.name}")


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
        const loginInput = document.querySelector('#ctl00_ctplhMain_Login1_UserName') || document.querySelector('input[type="text"]');
        const passwordInput = document.querySelector('#ctl00_ctplhMain_Login1_Password') || document.querySelector('input[type="password"]');
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

        const submitButton = document.querySelector('#ctl00_ctplhMain_Login1_LoginImageButton') || document.querySelector('input[type="submit"],input[type="image"],button');
        if (!submitButton) return 'Поля заполнены, но кнопка входа не найдена';
        submitButton.click();

        return agreementCheckbox
            ? 'OK: поля заполнены, галочка согласия установлена, кнопка входа нажата'
            : 'OK: поля заполнены, кнопка входа нажата (галочка согласия не найдена)';
    }
    """
    return page.evaluate(js, {"login": login, "password": password})


def open_new_message_form(page, timeout_ms: int = 45000) -> None:
    page.locator('img[alt="Создать новое сообщение"]').first.wait_for(state="visible", timeout=timeout_ms)
    page.locator('img[alt="Создать новое сообщение"]').first.click()
    page.locator('input#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_MessageTypeSelector_InsolventPicker_InsolventName').first.wait_for(state="visible", timeout=timeout_ms)


def search_individual_insolvent(page, inn: str, timeout_ms: int = 45000) -> None:
    page.wait_for_timeout(1200)
    tab = page.locator("a.rtsLink:has-text('Физ. лица')").first
    tab.wait_for(state="visible", timeout=timeout_ms)
    tab.click(timeout=timeout_ms)

    target_page = page
    target_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    inn_input = target_page.locator("#ctl00_cplhContent_InsolventList_EgripOrganizationCode_CodeTextBox")
    search_button = target_page.locator("#ctl00_cplhContent_InsolventList_btnSearchEgrip")
    result_row = target_page.locator("#resultTable tbody tr[onclick*='ReturnInsolvent']")
    inn_input.first.wait_for(state="visible", timeout=timeout_ms)
    inn_input.first.fill(inn)
    search_button.first.click()
    result_row.first.wait_for(state="visible", timeout=timeout_ms)
    result_row.first.click()


def fill_message_text(page, message_text: str, timeout_ms: int = 60000) -> None:
    message_textarea = page.locator(
        "#ctl00_ctl00_ctplhMain_CentralContentPlaceHolder_ucCreateMessage_messageListView_ctrl0_ObjectProxy_ctrl0_ReceivingCreditorDemand2Message_ObjectProxy_ctrl0_ObjectProxyView1_ctrl0_Message"
    )
    message_textarea.first.wait_for(state="visible", timeout=timeout_ms)
    message_textarea.first.fill(message_text)


def run_automation(*, login: str, password: str, inn: str, message_text: str, chrome_path: str, url: str, delay_ms: int, cdp_timeout_sec: float) -> dict:
    port = free_port()
    temp_profile_dir = tempfile.TemporaryDirectory(prefix=f"fedresurs-{uuid.uuid4().hex[:8]}-")
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
        url,
    ]
    proc = subprocess.Popen(cmd)

    try:
        if not wait_cdp_ready(port, timeout_sec=max(cdp_timeout_sec, 1.0)):
            return {"ok": False, "error": "CDP не поднялся вовремя", "pid": proc.pid}

        time.sleep(max(delay_ms, 0) / 1000)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            _context, page = pick_target_page(browser, url)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(max(delay_ms, 0))
            result = fill_login_form(page, login, password)
            if not result.startswith("OK:"):
                return {"ok": False, "error": result, "pid": proc.pid}
            open_new_message_form(page)
            search_individual_insolvent(page, inn=inn)
            fill_message_text(page, message_text=message_text)
        return {"ok": True, "pid": proc.pid}
    except PlaywrightError as exc:
        return {"ok": False, "error": str(exc), "pid": proc.pid}
    finally:
        safe_cleanup_temp_profile(temp_profile_dir)


def build_app(service: FedresursAutomationService) -> FastAPI:
    app = FastAPI(title="Fedresurs Automation API")

    @app.get("/health")
    def health():
        return {"status": "ok", "max_parallel_jobs": service.max_parallel_jobs, "queue_if_busy": service.queue_if_busy}

    @app.post("/run")
    def run_job(payload: JobRequest):
        future = service.submit(payload)
        try:
            result = future.result()
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result)
        return result

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Fedresurs automation")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    run_parser = subparsers.add_parser("run", help="Одиночный запуск")
    run_parser.add_argument("--login", required=True)
    run_parser.add_argument("--password", required=True)
    run_parser.add_argument("--inn", required=True)
    run_parser.add_argument("--message-text", required=True)
    run_parser.add_argument("--url", default=TARGET_URL)
    run_parser.add_argument("--delay-ms", type=int, default=2500)
    run_parser.add_argument("--browser-path", default=None)
    run_parser.add_argument("--cdp-timeout-sec", type=float, default=20.0)

    serve_parser = subparsers.add_parser("serve", help="HTTP API сервер")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)
    serve_parser.add_argument("--workers", type=int, default=2, help="Максимум параллельных задач")
    serve_parser.add_argument("--queue", action="store_true", help="Ставить задачи в очередь при перегрузке")
    serve_parser.add_argument("--browser-path", default=None)

    args = parser.parse_args()

    if args.mode == "run":
        chrome_path = args.browser_path or detect_system_chrome()
        if not chrome_path:
            print("Не найден установленный Google Chrome. Передайте --browser-path")
            return 1
        result = run_automation(
            login=args.login,
            password=args.password,
            inn=args.inn,
            message_text=args.message_text,
            chrome_path=chrome_path,
            url=args.url,
            delay_ms=args.delay_ms,
            cdp_timeout_sec=args.cdp_timeout_sec,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    service = FedresursAutomationService(
        chrome_path=args.browser_path,
        max_parallel_jobs=args.workers,
        queue_if_busy=args.queue,
    )
    app = build_app(service)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
