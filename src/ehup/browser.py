"""Even Hub 開発者ポータルのブラウザセッション。

保存済みの資格情報でログインし、セッション（Cookie 等）を再利用する。
セッションが切れていたら自動で入り直す。

パスワードはログ・例外メッセージ・スクリーンショットに出さない。
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import credentials

PORTAL_BASE = os.environ.get("EVENHUB_BASE_URL", "https://hub.evenrealities.com")
LOGIN_URL = f"{PORTAL_BASE}/login"
HUB_URL = f"{PORTAL_BASE}/hub"

DEFAULT_TIMEOUT_MS = 30_000


class PortalError(Exception):
    pass


def state_path() -> Path:
    return credentials.config_dir() / "session.json"


@dataclass
class Portal:
    """ログイン済みのポータル画面を扱うためのハンドル。"""

    page: object  # playwright.sync_api.Page
    context: object  # playwright.sync_api.BrowserContext

    def goto(self, path: str) -> None:
        url = path if path.startswith("http") else f"{PORTAL_BASE}{path}"
        self.page.goto(url, wait_until="networkidle")

    def save_session(self) -> None:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(p))
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)


def _is_logged_in(page) -> bool:
    """ログイン画面へ飛ばされていなければログイン済みとみなす。"""
    return "/login" not in page.url


def _perform_login(page, creds: credentials.Credentials) -> None:
    """ポータルのログイン画面を操作する。

    メールアドレスを入れて Continue を押すとパスワード欄が現れ、
    もう一度 Continue で確定する2段構成。
    """
    page.goto(LOGIN_URL, wait_until="networkidle")

    submit = page.get_by_role("button", name="Continue")

    page.locator('input[name="email"]').fill(creds.email)
    submit.click()

    password_box = page.locator('input[name="password"]')
    password_box.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    # fill() の引数は Playwright のログに残らない
    password_box.fill(creds.password)
    submit.click()

    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=DEFAULT_TIMEOUT_MS)
    except Exception as exc:
        raise PortalError(
            "ログインに失敗しました。メールアドレスかパスワードが違う可能性があります。\n"
            "`ehup login` で保存し直してください。"
        ) from exc

    page.wait_for_load_state("networkidle")


@contextlib.contextmanager
def portal(headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """ログイン済みのポータルを開く。

    1. 保存済みセッションがあれば再利用する
    2. 切れていれば保存済みの資格情報で入り直す
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise PortalError(
            "playwright がありません。`pip install playwright` の後に "
            "`playwright install chromium` を実行してください。"
        ) from exc

    creds = credentials.load()
    state = state_path()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(state) if state.exists() else None,
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(timeout_ms)
        page = context.new_page()

        page.goto(HUB_URL, wait_until="networkidle")
        if not _is_logged_in(page):
            _perform_login(page, creds)
            page.goto(HUB_URL, wait_until="networkidle")

        handle = Portal(page=page, context=context)
        try:
            handle.save_session()
            yield handle
        finally:
            with contextlib.suppress(Exception):
                handle.save_session()
            context.close()
            browser.close()
