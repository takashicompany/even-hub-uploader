"""ehup — Even Hub 開発者ポータル操作 CLI。

削除系の操作（アプリ削除・バージョン削除・テスター削除・審査取り下げ）は
意図的に実装していない。
"""

from __future__ import annotations

import contextlib
import json as _json
import sys
from pathlib import Path

import click

from . import __version__, credentials

BACKEND_LABEL = {
    "keyring": "OS の資格情報ストア",
    "file": "暗号化ファイル",
    "env": "環境変数",
}


def _fail(message: str) -> None:
    click.secho(message, fg="red", err=True)
    sys.exit(1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version")
def main() -> None:
    """Even Hub 開発者ポータルへのプラグイン公開作業を CLI から行う。"""


# --------------------------------------------------------------------------
# 認証
# --------------------------------------------------------------------------


@main.command()
@click.option("--email", "-e", help="メールアドレス（省略時は対話入力）")
def login(email: str | None) -> None:
    """メールアドレスとパスワードを保存する。"""
    if not email:
        email = click.prompt("メールアドレス").strip()
    password = click.prompt("パスワード", hide_input=True)

    if not email or not password:
        _fail("メールアドレスとパスワードの両方が必要です。")

    try:
        backend = credentials.save(email, password)
    except credentials.CredentialError as exc:
        _fail(str(exc))

    click.secho(f"保存しました: {email}", fg="green")
    click.echo(f"保存先: {BACKEND_LABEL[backend]}")
    if backend == "file":
        click.secho(
            "OS の資格情報ストアが使えないため、暗号化ファイルへ保存しました。\n"
            "このファイルは他のマシンへ持ち出しても復号できません。",
            fg="yellow",
        )


@main.command()
@click.option("--json", "as_json", is_flag=True, help="機械可読で出力する")
def whoami(as_json: bool) -> None:
    """資格情報の保存状態を表示する（パスワードは表示しない）。"""
    info = credentials.status()

    if as_json:
        click.echo(_json.dumps(info, indent=2, ensure_ascii=False))
        return

    if not info["stored"]:
        click.secho("資格情報は保存されていません。", fg="yellow")
        click.echo("`ehup login` を実行してください。")
    else:
        click.secho(f"メールアドレス: {info['email']}", fg="green")
        click.echo(f"取得元: {BACKEND_LABEL[info['source']]}")

    click.echo()
    click.echo(f"OS の資格情報ストア: {'利用可' if info['keyring_available'] else '利用不可'}")
    click.echo(f"設定ディレクトリ: {info['config_dir']}")


@main.command()
@click.confirmation_option(prompt="保存済みの資格情報を削除しますか？")
def logout() -> None:
    """保存済みの資格情報を削除する。"""
    removed = credentials.delete()
    if removed:
        click.secho(
            "削除しました: " + ", ".join(BACKEND_LABEL[r] for r in removed), fg="green"
        )
    else:
        click.echo("削除対象はありませんでした。")

    if credentials.status()["env_configured"]:
        click.secho(
            "環境変数に資格情報が残っています。必要なら手動で解除してください。",
            fg="yellow",
        )


# --------------------------------------------------------------------------
# プロジェクト
# --------------------------------------------------------------------------

_browser_options = [
    click.option("--headed", is_flag=True, help="ブラウザを表示して動作を目視する"),
    click.option("--json", "as_json", is_flag=True, help="機械可読で出力する"),
]


def browser_options(f):
    for opt in reversed(_browser_options):
        f = opt(f)
    return f


@contextlib.contextmanager
def _portal(headed: bool):
    from . import browser

    try:
        with browser.portal(headless=not headed) as p:
            yield p
    except (browser.PortalError, credentials.CredentialError) as exc:
        _fail(str(exc))


@main.command("apps")
@browser_options
def apps_cmd(headed: bool, as_json: bool) -> None:
    """プロジェクト一覧を表示する。"""
    from . import actions

    with _portal(headed) as p:
        items = actions.list_apps(p)

    if as_json:
        click.echo(_json.dumps(items, indent=2, ensure_ascii=False))
        return

    if not items:
        click.echo("プロジェクトがありません。")
        return

    width = max(len(i["package_id"] or "") for i in items)
    for i in items:
        click.echo(
            f"{(i['package_id'] or '?'):{width}s}  {i['name']}  "
            f"{i['version']} {i['status']}".rstrip()
        )


@main.group("app")
def app_group() -> None:
    """プロジェクトを操作する。"""


@app_group.command("create")
@click.option(
    "--ehpk",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="アップロードする .ehpk",
)
@click.option("--tagline", required=True, help="一言説明（50文字以内）")
@click.option("--name", help="プラグイン名（20文字以内。省略時は .ehpk の値）")
@click.option(
    "--icon",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="アイコン（24x24 モノクロ PNG）",
)
@click.option(
    "--tester",
    "testers",
    multiple=True,
    help="ベータテスターに追加するメールアドレス（複数可）",
)
@click.option(
    "--beta/--no-beta",
    default=True,
    show_default=True,
    help="作成後にビルドをBeta化し、ログイン中のメールアドレスをテスターに追加する",
)
@click.option("--dry-run", is_flag=True, help="確定せずに、入力内容だけ確認する")
@browser_options
def app_create(
    ehpk: Path,
    tagline: str,
    name: str | None,
    icon: Path | None,
    testers: tuple[str, ...],
    beta: bool,
    dry_run: bool,
    headed: bool,
    as_json: bool,
) -> None:
    """.ehpk から新しいプロジェクトを作成する。

    既定では作成後にビルドをBeta化し、ログイン中のメールアドレスを
    ベータテスターに追加する（--no-beta で止められる）。
    """
    from . import actions
    from .browser import PortalError

    with _portal(headed) as p:
        try:
            result = actions.create_app(
                p, ehpk=ehpk, tagline=tagline, name=name, icon=icon, dry_run=dry_run
            )

            if beta and not dry_run:
                package_id = result["package_id"]
                version = actions.latest_version(p, package_id)
                result["beta_version"] = version
                result["beta"] = actions.set_build_state(
                    p, package_id, version=version, state="Beta"
                )

                emails = [credentials.load().email, *testers]
                seen: list[str] = []
                for e in emails:
                    if e.lower() not in [s.lower() for s in seen]:
                        seen.append(e)
                result["testers"] = actions.add_testers(p, package_id, seen)
        except PortalError as exc:
            _fail(str(exc))

    if as_json:
        click.echo(_json.dumps(result, indent=2, ensure_ascii=False))
        return

    if dry_run:
        click.secho("確定していません（--dry-run）", fg="yellow")
        click.echo(f"  .ehpk   : {result['ehpk']}")
        click.echo(f"  名前     : {result['name']}")
        click.echo(f"  一言説明 : {result['tagline']}")
        click.echo(f"  アイコン : {result['icon'] or '(なし)'}")
        if beta:
            click.echo("  作成後   : Beta化 → テスター追加（ログイン中のアドレス"
                       + (f" + {len(testers)}件" if testers else "") + "）")
        return

    click.secho(f"作成しました: {result['package_id']}", fg="green")
    if beta:
        click.secho(f"  {result['beta_version']} を Beta にしました", fg="green")
        for t in result.get("testers", []):
            color = "green" if t["sent"] else "yellow"
            click.secho(f"  テスター {t['email']}: {t['status']}", fg=color)
    click.echo(actions.app_url(result["package_id"]))


@app_group.command("icon")
@click.option("--app", "package_id", required=True, help="パッケージID")
@click.option(
    "--icon",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="アイコン（24x24 モノクロ PNG）",
)
@click.option(
    "--allow-draft",
    is_flag=True,
    help="公開中の内容が変わらず下書きに留まる場合でも保存する",
)
@click.option(
    "--via-editor",
    is_flag=True,
    help="PNG を送らず、内蔵の作成ツールを 2x2 ペンで操作して描く（描いた形をそのまま通せる）",
)
@click.option("--dry-run", is_flag=True, help="確定せずに、入力内容だけ確認する")
@browser_options
def app_icon(
    package_id: str,
    icon: Path,
    allow_draft: bool,
    via_editor: bool,
    dry_run: bool,
    headed: bool,
    as_json: bool,
) -> None:
    """既存プロジェクトのアイコンを変更する（Store listing の Basic info）。

    審査を通ったビルドがあるプロジェクトでは、ポータルは Store listing の変更を
    下書きに溜め、Submit for review を通すまで公開中の内容を書き換えない。
    その場合は既定で確定せずに止まる（--allow-draft で下書き保存まで行う）。

    PNG をそのまま送る経路はポータル側の検査が厳しく、絵によっては
    描き足しが要る。--via-editor はポータル内蔵の作成ツールを操作するため、
    検査が緩く、多くの場合は描いた形を1画素も変えずに通せる。
    """
    from . import actions
    from .browser import PortalError

    with _portal(headed) as p:
        try:
            result = actions.set_icon(
                p,
                package_id,
                icon=icon,
                dry_run=dry_run,
                allow_draft=allow_draft,
                via_editor=via_editor,
            )
        except PortalError as exc:
            _fail(str(exc))

    if as_json:
        click.echo(_json.dumps(result, indent=2, ensure_ascii=False))
        return

    def _pixels() -> None:
        click.echo(f"  点灯する画素 : {result['lit_pixels']} 個")
        if result.get("stamps") is not None:
            click.echo(f"  ペンを置く回数: {result['stamps']} 回（作成ツールを操作）")
        if not result["added_pixels"] and not result["removed_pixels"]:
            click.secho("  描いた形をそのまま使えます（変更なし）", fg="green")
        if result["added_pixels"]:
            click.secho(
                f"  2x2 のペンで描ける形にするため {result['added_pixels']} 画素を描き足しました。",
                fg="yellow",
            )
        if result["removed_pixels"]:
            click.secho(
                f"  端に収まらない {result['removed_pixels']} 画素を落としました。", fg="yellow"
            )

    if dry_run:
        click.secho("確定していません（--dry-run）", fg="yellow")
        click.echo(f"  プロジェクト : {result['package_id']}（{result['name']}）")
        click.echo(f"  アイコン     : {result['icon']}")
        click.echo(f"  現在のアイコン: {result['current_icon'] or '(なし)'}")
        _pixels()
        if result["draft_mode"]:
            click.secho("\n" + actions.DRAFT_NOTICE, fg="yellow")
        return

    _pixels()
    if result.get("saved_as_draft"):
        click.secho("下書きとして保存しました（公開中のアイコンは未変更）", fg="yellow")
        click.echo("  変更のある項目: " + ", ".join(result["changed_sections"]))
        click.secho("反映にはポータルでの Submit for review が必要です。", fg="yellow")
    else:
        click.secho(f"アイコンを変更しました: {result['package_id']}", fg="green")
    click.echo(actions.app_url(result["package_id"]) + "/store-listing")


# --------------------------------------------------------------------------
# ビルド
# --------------------------------------------------------------------------


def _read_notes(value: str | None) -> str:
    """--notes は本文そのもの、または @ファイル名 を受け取る。"""
    if not value:
        return ""
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8").strip()
    return value


@main.command("versions")
@click.option("--app", "package_id", required=True, help="パッケージID")
@browser_options
def versions_cmd(package_id: str, headed: bool, as_json: bool) -> None:
    """ビルド一覧を表示する。"""
    from . import actions

    with _portal(headed) as p:
        items = actions.list_versions(p, package_id)

    if as_json:
        click.echo(_json.dumps(items, indent=2, ensure_ascii=False))
        return

    section = None
    for i in items:
        if i["section"] != section:
            section = i["section"]
            click.secho(f"\n[{section}]", fg="cyan")
        click.echo(f"  {i['version']:12s} {i['state']:10s} {i['when']}")


@main.command("upload")
@click.option("--app", "package_id", required=True, help="パッケージID")
@click.option(
    "--ehpk",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="アップロードする .ehpk",
)
@click.option("--notes", help="アップデートノート（500文字以内。@ファイル名 で読み込み）")
@click.option("--dry-run", is_flag=True, help="確定せずに、入力内容だけ確認する")
@browser_options
def upload_cmd(
    package_id: str,
    ehpk: Path,
    notes: str | None,
    dry_run: bool,
    headed: bool,
    as_json: bool,
) -> None:
    """.ehpk を新しいビルドとしてアップロードする。"""
    from . import actions
    from .browser import PortalError

    with _portal(headed) as p:
        try:
            result = actions.upload_build(
                p, package_id, ehpk=ehpk, notes=_read_notes(notes), dry_run=dry_run
            )
        except PortalError as exc:
            _fail(str(exc))

    if as_json:
        click.echo(_json.dumps(result, indent=2, ensure_ascii=False))
        return

    if dry_run:
        click.secho("確定していません（--dry-run）", fg="yellow")
    else:
        click.secho(f"アップロードしました: {result['version']}", fg="green")
    click.echo(f"  バージョン       : {result['version']}")
    click.echo(f"  アップデートノート: {result['notes'] or '(なし)'}")


# --------------------------------------------------------------------------
# ベータ
# --------------------------------------------------------------------------


@main.group("beta")
def beta_group() -> None:
    """ベータ配布を操作する。"""


@beta_group.command("push")
@click.option("--app", "package_id", required=True, help="パッケージID")
@click.option("--version", required=True, help="対象バージョン（例: 0.2.0）")
@click.option("--dry-run", is_flag=True, help="変更せずに、現在の状態だけ確認する")
@browser_options
def beta_push(
    package_id: str, version: str, dry_run: bool, headed: bool, as_json: bool
) -> None:
    """ビルドをベータ状態にする。"""
    _set_state(package_id, version, "Beta", dry_run, headed, as_json)


@main.command("promote")
@click.option("--app", "package_id", required=True, help="パッケージID")
@click.option("--version", required=True, help="対象バージョン（例: 0.2.0）")
@click.option(
    "--to",
    "state",
    required=True,
    type=click.Choice(["Private", "Beta", "Public"], case_sensitive=False),
    help="変更後の状態",
)
@click.option("--dry-run", is_flag=True, help="変更せずに、現在の状態だけ確認する")
@browser_options
def promote_cmd(
    package_id: str, version: str, state: str, dry_run: bool, headed: bool, as_json: bool
) -> None:
    """ビルドの公開状態を変更する。"""
    _set_state(package_id, version, state.capitalize(), dry_run, headed, as_json)


def _set_state(
    package_id: str, version: str, state: str, dry_run: bool, headed: bool, as_json: bool
) -> None:
    from . import actions
    from .browser import PortalError

    with _portal(headed) as p:
        try:
            result = actions.set_build_state(
                p, package_id, version=version, state=state, dry_run=dry_run
            )
        except PortalError as exc:
            _fail(str(exc))

    if as_json:
        click.echo(_json.dumps(result, indent=2, ensure_ascii=False))
        return

    if result["current"] == result["requested"]:
        click.echo(f"{result['version']} は既に {state} です。")
    elif dry_run:
        click.secho("変更していません（--dry-run）", fg="yellow")
        click.echo(f"  {result['version']}: {result['current']} → {state}")
    else:
        click.secho(f"{result['version']}: {result['current']} → {state}", fg="green")


# --------------------------------------------------------------------------
# テスター
# --------------------------------------------------------------------------


@beta_group.command("testers")
@click.option("--app", "package_id", required=True, help="パッケージID")
@browser_options
def testers_cmd(package_id: str, headed: bool, as_json: bool) -> None:
    """テスター一覧を表示する。"""
    from . import actions

    with _portal(headed) as p:
        items = actions.list_testers(p, package_id)

    if as_json:
        click.echo(_json.dumps(items, indent=2, ensure_ascii=False))
        return

    for i in items:
        click.echo(f"{i['email']:40s} {i['status']}")


@beta_group.command("add-testers")
@click.option("--app", "package_id", required=True, help="パッケージID")
@click.option("--email", "emails", multiple=True, help="追加するメールアドレス（複数可）")
@click.option(
    "--from",
    "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="メールアドレスを1行ずつ書いたファイル",
)
@click.option("--dry-run", is_flag=True, help="送信せずに、対象だけ確認する")
@browser_options
def add_testers_cmd(
    package_id: str,
    emails: tuple[str, ...],
    from_file: Path | None,
    dry_run: bool,
    headed: bool,
    as_json: bool,
) -> None:
    """テスターのメールアドレスを追加する。"""
    from . import actions
    from .browser import PortalError

    targets = list(emails)
    if from_file:
        targets += [
            ln.strip()
            for ln in from_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    if not targets:
        _fail("--email か --from でメールアドレスを指定してください。")

    with _portal(headed) as p:
        try:
            results = actions.add_testers(p, package_id, targets, dry_run=dry_run)
        except PortalError as exc:
            _fail(str(exc))

    if as_json:
        click.echo(_json.dumps(results, indent=2, ensure_ascii=False))
        return

    for r in results:
        color = "green" if r["sent"] else "yellow"
        click.secho(f"{r['email']:40s} {r['status']}", fg=color)


if __name__ == "__main__":
    main()
