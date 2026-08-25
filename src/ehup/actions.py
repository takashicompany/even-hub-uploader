"""ポータル画面に対する操作。

破壊的な操作（プロジェクト削除・ビルド削除・テスター削除・審査取り下げ）は
意図的に実装していない。該当する画面要素にも触れない。
"""

from __future__ import annotations

import base64
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


def _open_basic_info(page):
    """Store listing の Basic info の Edit を開き、そのダイアログを返す。"""
    heading = page.get_by_text("Basic info", exact=True).first
    row = heading.locator("xpath=ancestor::*[.//*[normalize-space(text())='Edit']][1]")
    row.get_by_text("Edit", exact=True).first.click()
    page.wait_for_timeout(2000)

    dialog = page.locator('[role="dialog"]:visible').filter(
        has=page.locator('input[name="name"]')
    ).first
    dialog.locator('input[name="name"]').first.wait_for(state="visible", timeout=15_000)
    return dialog


# ポータルが受け付けるアイコンの条件（画面上の説明には出ておらず、
# 外れると保存時に「invalid icon pixel: x, y」で弾かれる）:
#
#   * 24x24 の PNG
#   * 点灯画素は #F4F4F4 で不透明、消灯画素は完全に透明
#   * 2x2 のペンで、行優先に塗っていける形であること
#
# 3つ目はポータルの検査を実測して割り出した条件で、次の走査と同じ:
#
#   左上から行優先に見ていき、まだ塗られていない点灯画素に出会ったら、
#   その画素を左上とする 2x2 が全点灯していなければならない。
#   全点灯ならその4画素を「塗った」ことにして先へ進む。
#
# 升目に整列している必要はない（1画素ずらした 2x2 も通る）が、
# 3x3 の塊や十字は通らない。
#
# なお、ポータル内蔵の作成ツール（Create with a tool）が持つ検査は
# これより緩く、「各点灯画素が、全点灯 2x2 のどれかに属すること」しか
# 見ていない（警告文も "Every pixel must be part of at least one
# 2x2 filled block"）。3x3 の塊はツールの検査は通るが、PNG を
# アップロードする経路では上の走査で弾かれる。ehup は後者を通す。
ICON_ON_RGB = (244, 244, 244)
ICON_SIZE = 24

# 指定された PNG をブラウザ上で上記の形式へ揃え、ファイル入力に流し込む。
# 透明を含む画像は不透明部分を、含まない画像は暗い部分を点灯画素とみなす。
_NORMALIZE_ICON_JS = """
async (el, arg) => {
    const { b64, on: ON, mode } = arg;
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    const w = bitmap.width;
    const h = bitmap.height;

    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);

    const image = ctx.getImageData(0, 0, w, h);
    const px = image.data;

    let hasAlpha = false;
    for (let i = 3; i < px.length; i += 4) {
        if (px[i] < 128) { hasAlpha = true; break; }
    }

    const lit = new Uint8Array(w * h);
    for (let i = 0, n = 0; i < px.length; i += 4, n += 1) {
        const lum = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
        lit[n] = (hasAlpha ? px[i + 3] >= 128 : lum < 128) ? 1 : 0;
    }

    const full = (x, y) => {
        if (x < 0 || y < 0 || x + 1 >= w || y + 1 >= h) return false;
        const n = y * w + x;
        return lit[n] && lit[n + 1] && lit[n + w] && lit[n + w + 1];
    };

    // アップロード経路の検査（行優先に 2x2 を置いていけるか）
    const firstBadUpload = () => {
        const covered = new Uint8Array(w * h);
        for (let y = 0; y < h; y += 1) {
            for (let x = 0; x < w; x += 1) {
                const n = y * w + x;
                if (!lit[n] || covered[n]) continue;
                if (!full(x, y)) return [x, y];
                for (const i of [n, n + 1, n + w, n + w + 1]) covered[i] = 1;
            }
        }
        return null;
    };

    // 作成ツールの検査（各点灯画素が全点灯 2x2 のどれかに属するか）
    const firstBadEditor = () => {
        const covered = new Uint8Array(w * h);
        for (let y = 0; y + 1 < h; y += 1) {
            for (let x = 0; x + 1 < w; x += 1) {
                if (!full(x, y)) continue;
                const n = y * w + x;
                for (const i of [n, n + 1, n + w, n + w + 1]) covered[i] = 1;
            }
        }
        for (let y = 0; y < h; y += 1) {
            for (let x = 0; x < w; x += 1) {
                if (lit[y * w + x] && !covered[y * w + x]) return [x, y];
            }
        }
        return null;
    };

    const firstBad = mode === "editor" ? firstBadEditor : firstBadUpload;

    // 通らない画素は、2x2 を埋める方向（描き足す方向）で直す。
    // 元の絵から画素を削らないので、描いた形がそのまま残る。
    let added = 0;
    let removed = 0;
    for (let guard = 0; guard < w * h * 4; guard += 1) {
        const bad = firstBad();
        if (!bad) break;
        const [x, y] = bad;
        const n = y * w + x;
        if (x + 1 >= w || y + 1 >= h) {
            lit[n] = 0;
            removed += 1;
            continue;
        }
        // アップロード経路は行優先に見ていくので、その画素を左上とする
        // 2x2 を埋めるほかない。ツール経路はどの向きの 2x2 でもよいので、
        // 描き足しが最も少ないものを選ぶ。
        let blk = [n, n + 1, n + w, n + w + 1];
        if (mode === "editor") {
            let best = null;
            for (const [ox, oy] of [[x, y], [x - 1, y], [x, y - 1], [x - 1, y - 1]]) {
                if (ox < 0 || oy < 0 || ox + 1 >= w || oy + 1 >= h) continue;
                const m = oy * w + ox;
                const cand = [m, m + 1, m + w, m + w + 1];
                const cost = cand.filter((i) => !lit[i]).length;
                if (cost > 0 && (best === null || cost < best.cost)) best = { cand, cost };
            }
            if (!best) { lit[n] = 0; removed += 1; continue; }
            blk = best.cand;
        }
        for (const i of blk) {
            if (!lit[i]) { lit[i] = 1; added += 1; }
        }
    }

    // 作成ツール経路では、置くべき 2x2 スタンプの位置を返す。
    // 検査を通った形は「全点灯 2x2 の和集合」と一致するので、
    // その位置すべてに 2x2 ペンを置けば元の形がそのまま再現できる。
    if (mode === "editor") {
        const stamps = [];
        let on = 0;
        for (let y = 0; y + 1 < h; y += 1) {
            for (let x = 0; x + 1 < w; x += 1) {
                if (full(x, y)) stamps.push([x, y]);
            }
        }
        for (let n = 0; n < lit.length; n += 1) if (lit[n]) on += 1;
        return { width: w, height: h, lit: on, added, removed, stamps };
    }

    let on = 0;
    for (let i = 0, n = 0; i < px.length; i += 4, n += 1) {
        if (lit[n]) {
            px[i] = ON[0]; px[i + 1] = ON[1]; px[i + 2] = ON[2]; px[i + 3] = 255;
            on += 1;
        } else {
            px[i] = 0; px[i + 1] = 0; px[i + 2] = 0; px[i + 3] = 0;
        }
    }
    ctx.putImageData(image, 0, 0);

    const blob = await new Promise((res) => canvas.toBlob(res, "image/png"));
    const file = new File([blob], "icon.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    el.files = dt.files;
    el.dispatchEvent(new Event("change", { bubbles: true }));

    return {
        width: w,
        height: h,
        lit: on,
        added: added,
        removed: removed,
        source_has_alpha: hasAlpha,
    };
}
"""


def _plan_icon(dialog, icon: Path, mode: str) -> dict:
    """アイコンを読み、指定の経路が通る形に整える。

    mode="upload" のときは、そのままファイル入力へ流し込む。
    mode="editor" のときは、作成ツールで置くスタンプの位置を返す。
    """
    field = dialog.locator('input[type="file"][accept="image/png"]:not([multiple])').first
    info = field.evaluate(
        _NORMALIZE_ICON_JS,
        {
            "b64": base64.b64encode(icon.read_bytes()).decode("ascii"),
            "on": list(ICON_ON_RGB),
            "mode": mode,
        },
    )

    if not info["lit"]:
        raise PortalError(
            f"点灯する画素がありません: {icon}\n"
            "透明を含む PNG は不透明な部分、含まない PNG は暗い部分を点灯画素として扱います。"
        )
    return info


def _draw_with_tool(page, dialog, stamps: list) -> None:
    """作成ツール（Create with a tool）を開き、2x2 ペンで描く。

    ツールのペンは「カーソルのいる升目を左上とする 2x2」を置く。
    stamps はその左上の位置。
    """
    dialog.get_by_role("button", name="Create with a tool").first.click()
    page.wait_for_timeout(2500)

    tool = page.locator('[role="dialog"]:visible').first
    canvas = tool.locator("canvas").first
    canvas.wait_for(state="visible", timeout=15_000)

    # 読み込まれている絵を消してから描く
    tool.get_by_role("button", name="Clear").first.click()
    page.wait_for_timeout(600)

    box = canvas.bounding_box()
    if not box:
        raise PortalError("作成ツールのキャンバスが見つかりませんでした。")
    cell_w = box["width"] / ICON_SIZE
    cell_h = box["height"] / ICON_SIZE

    for cx, cy in stamps:
        page.mouse.move(box["x"] + (cx + 0.5) * cell_w, box["y"] + (cy + 0.5) * cell_h)
        page.wait_for_timeout(30)
        page.mouse.down()
        page.wait_for_timeout(30)
        page.mouse.up()

    # プレビューの枠がキャンバスに残らないよう、外へ出してから確定する
    page.mouse.move(box["x"] - 40, box["y"] - 40)
    page.wait_for_timeout(500)
    tool.get_by_role("button", name="Confirm").first.click()
    page.wait_for_timeout(2500)

    warnings = [
        el.inner_text().strip()
        for el in page.locator('[role="alert"], [role="status"]').all()
        if el.is_visible() and el.inner_text().strip()
    ]
    if warnings:
        raise PortalError("作成ツールが受け付けませんでした: " + " / ".join(warnings)[:200])


def _save_error(page, saved: dict) -> str:
    """保存に失敗した理由を短くまとめる。"""
    body = saved.get("body") or {}
    message = body.get("message") or ""
    if message:
        detail = f"ポータルに拒否されました: {message}"
        if body.get("code"):
            detail += f"（code {body['code']}）"
        if "icon pixel" in message:
            detail += (
                "\nアイコンの画素がポータルの想定と違います。"
                "24x24 で、点灯させたい形だけが描かれた PNG を指定してください。"
            )
        return detail

    for el in page.locator('[role="dialog"]').all():
        if el.is_visible():
            return "画面に出ているメッセージ: " + " / ".join(
                ln.strip() for ln in el.inner_text().splitlines() if ln.strip()
            )[:200]
    return "ポータルからの応答を確認できませんでした。--headed で画面を見てください。"


def set_icon(
    p: Portal,
    package_id: str,
    icon: Path,
    dry_run: bool = False,
    allow_draft: bool = False,
    via_editor: bool = False,
) -> dict:
    """Store listing の Basic info からアイコンを差し替える。

    via_editor のときは PNG を直接送らず、内蔵の作成ツールを 2x2 ペンで
    操作して描く。ツール側の検査の方が緩いため、描いた形をそのまま通せる。

    dry_run のときは確定ボタンを押す直前で止める。
    下書きにしかならないプロジェクトでは、allow_draft を付けない限り確定しない。
    """
    if not icon.exists():
        raise PortalError(f"アイコンが見つかりません: {icon}")

    width, height = _png_size(icon)
    if (width, height) != (ICON_SIZE, ICON_SIZE):
        raise PortalError(
            f"アイコンは {ICON_SIZE}x{ICON_SIZE} の PNG です"
            f"（指定されたものは {width}x{height}）。"
        )

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

    mode = "editor" if via_editor else "upload"
    dialog = _open_basic_info(page)
    info = _plan_icon(dialog, icon, mode)
    result["via_editor"] = via_editor
    result["lit_pixels"] = info["lit"]
    result["added_pixels"] = info["added"]
    result["removed_pixels"] = info["removed"]
    if via_editor:
        result["stamps"] = len(info["stamps"])
    page.wait_for_timeout(1000)

    if dry_run:
        result["dry_run"] = True
        dialog.get_by_role("button", name="Cancel").first.click()
        return result

    if state["draft_mode"] and not allow_draft:
        dialog.get_by_role("button", name="Cancel").first.click()
        raise PortalError(DRAFT_NOTICE)

    if via_editor:
        _draw_with_tool(page, dialog, info["stamps"])

    # 保存の結果はポータルの応答にしか出ないことがあるので拾っておく
    saved: dict = {}

    def on_response(r) -> None:
        if "/api/v1/apps/" not in r.url or r.request.method not in ("POST", "PUT", "PATCH"):
            return
        with contextlib.suppress(Exception):
            saved["body"] = r.json()

    page.on("response", on_response)
    try:
        dialog.get_by_role("button", name="Confirm").first.click()
        page.wait_for_timeout(5000)
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("response", on_response)

    body = saved.get("body") or {}
    if body.get("code"):
        raise PortalError("アイコンを変更できませんでした。\n" + _save_error(page, saved))

    after = listing_state(p, package_id)
    result["changed_sections"] = after["changed_sections"]
    result["saved_as_draft"] = after["draft_mode"]
    if after["draft_mode"]:
        sections = {s.replace("-", "_") for s in after["changed_sections"]}
        result["changed"] = "basic_info" in sections
    else:
        result["new_icon"] = after["icon"]
        result["changed"] = bool(after["icon"]) and after["icon"] != state["icon"]

    if not result["changed"]:
        raise PortalError("アイコンを変更できませんでした。\n" + _save_error(page, saved))
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
