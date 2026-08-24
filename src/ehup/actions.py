"""ポータル画面に対する操作。

破壊的な操作（プロジェクト削除・ビルド削除・テスター削除・審査取り下げ）は
意図的に実装していない。該当する画面要素にも触れない。
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from .browser import PORTAL_BASE, Portal, PortalError

CARD_SELECTOR = ".group\\/er-grid-item"

# カバー画像の URL に package_id が入っている
_COVER_PKG = re.compile(r"/prod/([A-Za-z0-9_.-]+)/covers/")


def _card_package_id(card) -> str | None:
    """カード内のカバー画像 URL から package_id を拾う。無ければ None。"""
    for img in card.locator("img[src]").all():
        m = _COVER_PKG.search(img.get_attribute("src") or "")
        if m:
            return m.group(1)
    return None


def list_apps(p: Portal) -> list[dict]:
    """プロジェクト一覧を返す。"""
    p.goto("/hub")
    p.page.wait_for_timeout(1500)

    cards = p.page.locator(CARD_SELECTOR)
    count = cards.count()

    apps: list[dict] = []
    unresolved: list[int] = []

    for i in range(count):
        card = cards.nth(i)
        lines = [ln.strip() for ln in card.inner_text().splitlines() if ln.strip()]
        entry = {
            "package_id": _card_package_id(card),
            "name": lines[0] if lines else "",
            "version": lines[1] if len(lines) > 1 else "",
            "status": lines[2] if len(lines) > 2 else "",
        }
        apps.append(entry)
        if not entry["package_id"]:
            unresolved.append(i)

    # カバー画像が無いものは開いて URL から拾う
    for i in unresolved:
        p.goto("/hub")
        p.page.wait_for_timeout(1000)
        p.page.locator(CARD_SELECTOR).nth(i).click()
        p.page.wait_for_url(re.compile(r"/hub/[^/]+"), timeout=15_000)
        apps[i]["package_id"] = p.page.url.rsplit("/hub/", 1)[-1].split("/")[0]

    return apps


def create_app(
    p: Portal,
    ehpk: Path,
    tagline: str,
    name: str | None = None,
    icon: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """.ehpk から新しいプロジェクトを作る。

    dry_run のときは確定ボタンを押す直前で止め、入力内容だけ返す。
    """
    if not ehpk.exists():
        raise PortalError(f".ehpk が見つかりません: {ehpk}")

    page = p.page
    p.goto("/hub")
    page.wait_for_timeout(1500)

    page.get_by_text("Upload package", exact=True).first.click()
    page.wait_for_timeout(1000)

    page.locator('input[type="file"][accept=".ehpk"], input[type="file"]').first.set_input_files(
        str(ehpk)
    )

    name_box = page.locator('input[name="name"]')
    name_box.wait_for(state="visible", timeout=30_000)
    tagline_box = page.locator('input[name="tagline"]')

    if name:
        name_box.fill(name)
    tagline_box.fill(tagline)

    if icon:
        if not icon.exists():
            raise PortalError(f"アイコンが見つかりません: {icon}")
        page.locator('input[type="file"][accept="image/png"]').first.set_input_files(str(icon))
        page.wait_for_timeout(1000)

    planned = {
        "ehpk": str(ehpk),
        "name": name_box.input_value(),
        "tagline": tagline_box.input_value(),
        "icon": str(icon) if icon else None,
        "created": False,
    }

    if dry_run:
        planned["dry_run"] = True
        return planned

    created_name = planned["name"]
    page.get_by_role("button", name="Create project").click()
    page.wait_for_timeout(4000)

    # 詳細画面へ飛ぶ場合と、一覧に戻る場合がある
    if "/hub/" in page.url:
        planned["package_id"] = page.url.rsplit("/hub/", 1)[-1].split("/")[0]
        planned["created"] = True
        return planned

    package_id = _find_package_id_by_name(p, created_name)
    if package_id:
        planned["package_id"] = package_id
        planned["created"] = True
        return planned

    raise PortalError(
        "プロジェクトを作成できませんでした。画面に残っているメッセージを確認してください:\n"
        + _visible_dialog_text(page)
    )


def latest_version(p: Portal, package_id: str) -> str:
    """直近にアップロードされたビルドのバージョンを返す。

    アップロード直後のビルドは Private builds の先頭に並ぶ。
    Private が無い場合のみ、画面上の先頭を使う。
    """
    versions = list_versions(p, package_id)
    if not versions:
        raise PortalError(f"{package_id} にビルドがありません。")
    for v in versions:
        if v["section"] == "Private builds":
            return v["version"]
    return versions[0]["version"]


def _find_package_id_by_name(p: Portal, name: str) -> str | None:
    """一覧から名前でプロジェクトを探し、package_id を返す。"""
    p.goto("/hub")
    p.page.wait_for_timeout(1500)

    cards = p.page.locator(CARD_SELECTOR)
    for i in range(cards.count()):
        lines = [ln.strip() for ln in cards.nth(i).inner_text().splitlines() if ln.strip()]
        if not lines or lines[0] != name:
            continue
        pkg = _card_package_id(cards.nth(i))
        if pkg:
            return pkg
        cards.nth(i).click()
        p.page.wait_for_url(re.compile(r"/hub/[^/]+"), timeout=15_000)
        return p.page.url.rsplit("/hub/", 1)[-1].split("/")[0]
    return None


def _visible_dialog_text(page) -> str:
    for el in page.locator('[role="dialog"]').all():
        if el.is_visible():
            return el.inner_text()[:500]
    return page.locator("body").inner_text()[:500]


def app_url(package_id: str) -> str:
    return f"{PORTAL_BASE}/hub/{package_id}"


# --------------------------------------------------------------------------
# ストアリスティング（アイコン）
# --------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int]:
    """PNG の幅・高さを IHDR から読む。PNG でなければ PortalError。"""
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != _PNG_MAGIC or head[12:16] != b"IHDR":
        raise PortalError(f"PNG ではありません: {path}")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def listing_state(p: Portal, package_id: str) -> dict:
    """Store listing 画面を開き、下書きの扱いに関わる状態を読む。

    審査を通ったビルドがあるプロジェクトでは、ポータルは Store listing の変更を
    「下書き」に溜め、Submit for review を通すまで公開中の内容を書き換えない。
    その判定に使う。
    """
    captured: dict = {}

    def on_response(r) -> None:
        if "/api/v1/apps/store-listing-summary" in r.url:
            key = "summary"
        elif "/api/v1/apps/listing-draft" in r.url:
            key = "draft"
        elif "/api/v1/apps/get" in r.url:
            key = "app"
        else:
            return
        with contextlib.suppress(Exception):
            captured[key] = r.json()

    page = p.page
    page.on("response", on_response)
    try:
        p.goto(f"/hub/{package_id}/store-listing")
        page.wait_for_timeout(4000)
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("response", on_response)

    summary = (captured.get("summary") or {}).get("data") or {}
    draft = (captured.get("draft") or {}).get("data") or {}
    app = (captured.get("app") or {}).get("data") or {}

    return {
        "draft_mode": bool(summary.get("has_approved_or_published_version")),
        "has_draft": draft.get("draft") is not None,
        "changed_sections": draft.get("changed_sections") or [],
        "icon": app.get("icon") or "",
        "name": app.get("name") or "",
        "tagline": app.get("tagline") or "",
    }


DRAFT_NOTICE = (
    "審査を通ったビルドがあるプロジェクトのため、Store listing の変更は下書きとして\n"
    "保存されるだけで、公開中のアイコンは変わりません。\n"
    "反映にはポータルでの Submit for review（再審査）が必要です。\n"
    "ehup は審査への提出を行いません。下書きとして保存するだけでよければ\n"
    "--allow-draft を付けてください（下書きはポータルの Store listing から破棄できます）。"
)


def _open_basic_info(page) -> None:
    """Store listing の Basic info の Edit を開く。"""
    heading = page.get_by_text("Basic info", exact=True).first
    row = heading.locator("xpath=ancestor::*[.//*[normalize-space(text())='Edit']][1]")
    row.get_by_text("Edit", exact=True).first.click()
    page.wait_for_timeout(2000)
    page.locator('[role="dialog"] input[name="name"]').first.wait_for(
        state="visible", timeout=15_000
    )


def set_icon(
    p: Portal,
    package_id: str,
    icon: Path,
    dry_run: bool = False,
    allow_draft: bool = False,
) -> dict:
    """Store listing の Basic info からアイコンを差し替える。

    dry_run のときは確定ボタンを押す直前で止める。
    下書きにしかならないプロジェクトでは、allow_draft を付けない限り確定しない。
    """
    if not icon.exists():
        raise PortalError(f"アイコンが見つかりません: {icon}")

    width, height = _png_size(icon)
    if (width, height) != (24, 24):
        raise PortalError(f"アイコンは 24x24 の PNG です（指定されたものは {width}x{height}）。")

    state = listing_state(p, package_id)
    page = p.page

    result = {
        "package_id": package_id,
        "icon": str(icon),
        "name": state["name"],
        "tagline": state["tagline"],
        "current_icon": state["icon"],
        "draft_mode": state["draft_mode"],
        "changed_sections": state["changed_sections"],
        "changed": False,
    }

    _open_basic_info(page)
    page.locator('[role="dialog"] input[type="file"][accept="image/png"]').first.set_input_files(
        str(icon)
    )
    page.wait_for_timeout(1500)

    if dry_run:
        result["dry_run"] = True
        page.get_by_role("button", name="Cancel").first.click()
        return result

    if state["draft_mode"] and not allow_draft:
        page.get_by_role("button", name="Cancel").first.click()
        raise PortalError(DRAFT_NOTICE)

    page.get_by_role("button", name="Confirm").first.click()
    page.wait_for_timeout(4000)

    after = listing_state(p, package_id)
    result["changed_sections"] = after["changed_sections"]
    result["saved_as_draft"] = after["draft_mode"]
    if after["draft_mode"]:
        result["changed"] = "basic_info" in after["changed_sections"]
    else:
        result["new_icon"] = after["icon"]
        result["changed"] = bool(after["icon"]) and after["icon"] != state["icon"]

    if not result["changed"]:
        raise PortalError(
            "アイコンを変更できませんでした。画面に残っているメッセージを確認してください:\n"
            + _visible_dialog_text(page)
        )
    return result


# --------------------------------------------------------------------------
# ビルド
# --------------------------------------------------------------------------

SECTION_TITLES = ("Public build", "Beta build", "Private builds")


def list_versions(p: Portal, package_id: str) -> list[dict]:
    """ビルド一覧を返す。"""
    p.goto(f"/hub/{package_id}")
    p.page.wait_for_timeout(2000)

    versions: list[dict] = []
    section = ""
    for line in p.page.locator("body").inner_text().splitlines():
        line = line.strip()
        if line in SECTION_TITLES:
            section = line
            continue
        if not section:
            continue
        if re.fullmatch(r"v\d+\.\d+\.\d+", line):
            versions.append({"section": section, "version": line, "state": "", "when": ""})
        elif versions and not versions[-1]["when"] and line.startswith(("Uploaded", "Published")):
            versions[-1]["when"] = line
        elif versions and not versions[-1]["state"] and line in (
            "Public",
            "Beta",
            "Private",
            "Approved",
            "Rejected",
            "In review",
        ):
            versions[-1]["state"] = line
    return versions


def upload_build(
    p: Portal,
    package_id: str,
    ehpk: Path,
    notes: str = "",
    dry_run: bool = False,
) -> dict:
    """.ehpk を新しいビルドとしてアップロードする。

    dry_run のときは確定ボタンを押す直前で止める。
    """
    if not ehpk.exists():
        raise PortalError(f".ehpk が見つかりません: {ehpk}")
    if len(notes) > 500:
        raise PortalError(f"アップデートノートは500文字以内です（現在 {len(notes)} 文字）。")

    page = p.page
    p.goto(f"/hub/{package_id}")
    page.wait_for_timeout(2000)

    page.get_by_text("Upload a build", exact=True).first.click()
    page.wait_for_timeout(1200)
    page.locator('input[type="file"]').first.set_input_files(str(ehpk))

    changelog = page.locator('textarea[name="changelog"]')
    changelog.wait_for(state="visible", timeout=60_000)
    if notes:
        changelog.fill(notes)

    dialog = _visible_dialog_text(page)
    version = ""
    m = re.search(r"v\d+\.\d+\.\d+", dialog)
    if m:
        version = m.group(0)

    result = {
        "package_id": package_id,
        "ehpk": str(ehpk),
        "version": version,
        "notes": changelog.input_value(),
        "added": False,
    }

    if dry_run:
        result["dry_run"] = True
        page.get_by_role("button", name="Cancel").click()
        return result

    page.get_by_role("button", name="Add build").click()
    page.wait_for_timeout(4000)
    result["added"] = True
    return result


# --------------------------------------------------------------------------
# ベータ
# --------------------------------------------------------------------------

STATE_TAGS = {"Public": "tag-public", "Beta": "tag-beta", "Private": "tag-private"}


def set_build_state(
    p: Portal,
    package_id: str,
    version: str,
    state: str,
    dry_run: bool = False,
) -> dict:
    """ビルドの公開状態を変更する（Private / Beta / Public）。"""
    if state not in STATE_TAGS:
        raise PortalError(f"状態は {', '.join(STATE_TAGS)} のいずれかです: {state}")

    version = version if version.startswith("v") else f"v{version}"

    page = p.page
    p.goto(f"/hub/{package_id}")
    page.wait_for_timeout(2000)

    row = page.get_by_text(version, exact=True).first
    if row.count() == 0:
        raise PortalError(f"{version} が見つかりません。`ehup versions` で確認してください。")

    # 行の中の状態タグ（クリックでメニューが開く）
    tag = row.locator("xpath=ancestor::div[contains(@class,'justify-between')][1]").locator(
        "span.tag"
    ).first
    current = tag.inner_text().strip()

    result = {
        "package_id": package_id,
        "version": version,
        "current": current,
        "requested": state,
        "changed": False,
    }

    if current == state:
        return result

    if dry_run:
        result["dry_run"] = True
        return result

    tag.click()
    page.wait_for_timeout(1200)

    option = page.locator(f'[role="dialog"] div.{STATE_TAGS[state]}').first
    option.wait_for(state="visible", timeout=15_000)
    if "cursor-not-allowed" in (option.get_attribute("class") or ""):
        raise PortalError(f"{version} は {state} に変更できません（画面上で選択不可）。")
    option.click()
    page.wait_for_timeout(1500)

    # 「Promote this build to X?」の確認ダイアログが挟まる
    confirm = page.get_by_role("button", name=re.compile(rf"(Promote|Publish).*{state}", re.I))
    if confirm.count() == 0:
        confirm = page.get_by_role("button", name=re.compile(rf"^{state}$", re.I))
    if confirm.count() == 0:
        raise PortalError(
            f"{state} への変更を確定するボタンが見つかりませんでした:\n"
            + _visible_dialog_text(page)
        )
    confirm.first.click()
    page.wait_for_timeout(4000)

    # 実際に変わったか確認する
    actual = _build_state(p, package_id, version)
    result["actual"] = actual
    result["changed"] = actual == state
    if not result["changed"]:
        raise PortalError(
            f"{version} の状態を {state} に変更できませんでした（現在: {actual or '不明'}）。"
        )
    return result


def _build_state(p: Portal, package_id: str, version: str) -> str:
    """ビルドの現在の状態を読み直す。"""
    for v in list_versions(p, package_id):
        if v["version"] == version:
            return v["state"]
    return ""


# --------------------------------------------------------------------------
# テスター
# --------------------------------------------------------------------------


def list_testers(p: Portal, package_id: str) -> list[dict]:
    """テスター一覧を返す。"""
    page = p.page
    p.goto(f"/hub/{package_id}/testing-group")

    # 読み込み中はスケルトン行（td[data-slot=empty]）が出る
    rows = page.locator('table tbody tr:not(:has(td[data-slot="empty"]))')
    try:
        page.wait_for_function(
            """() => {
                const tb = document.querySelector('table tbody');
                if (!tb) return false;
                if (tb.querySelector('td[data-slot="empty"] [aria-busy="true"]')) return false;
                return true;
            }""",
            timeout=20_000,
        )
    except Exception:
        pass
    page.wait_for_timeout(500)

    testers: list[dict] = []
    for i in range(rows.count()):
        cells = [c.inner_text().strip() for c in rows.nth(i).locator("td").all()]
        if len(cells) >= 2 and "@" in cells[0]:
            testers.append({"email": cells[0], "status": cells[1]})
    return testers


def add_testers(
    p: Portal,
    package_id: str,
    emails: list[str],
    dry_run: bool = False,
) -> list[dict]:
    """テスターを追加する（招待を送る）。"""
    page = p.page
    results: list[dict] = []

    existing = {t["email"].lower() for t in list_testers(p, package_id)}

    for email in emails:
        if email.lower() in existing:
            results.append({"email": email, "status": "既に登録済み", "sent": False})
            continue
        if dry_run:
            results.append({"email": email, "status": "未送信（--dry-run）", "sent": False})
            continue

        p.goto(f"/hub/{package_id}/testing-group")
        page.wait_for_timeout(1500)
        page.get_by_text("Add user", exact=True).first.click()
        page.wait_for_timeout(1200)

        box = page.locator('input[placeholder="user@email.com"]')
        box.wait_for(state="visible", timeout=15_000)
        box.fill(email)
        page.get_by_role("button", name="Send invite").click()
        page.wait_for_timeout(3000)

        results.append({"email": email, "status": "招待を送信", "sent": True})

    return results
