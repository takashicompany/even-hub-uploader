# ehup — Even Hub Uploader

Even G2 用プラグインの公開作業を CLI から行うツール。

Even Hub 開発者ポータル（https://hub.evenrealities.com ）の画面をブラウザ経由で操作する。
公開 API が提供されていないため、人がポータルで行う操作をそのまま自動化している。

## できること

| コマンド | 内容 |
|---|---|
| `ehup login` | メールアドレスとパスワードを保存する |
| `ehup whoami` | 保存状態を確認する |
| `ehup logout` | 保存済み資格情報を削除する |
| `ehup apps` | プロジェクト一覧 |
| `ehup app create` | `.ehpk` から新規プロジェクトを作成 |
| `ehup versions` | ビルド一覧 |
| `ehup upload` | `.ehpk` をアップロード（アップデートノート付き） |
| `ehup beta push` | ビルドをベータ状態にする |
| `ehup promote` | ビルドの公開状態を変更する（Private / Beta / Public） |
| `ehup beta testers` | テスター一覧 |
| `ehup beta add-testers` | テスターのメールアドレスを追加する |

## やらないこと

**破壊的な操作は実装していない。** 以下に対応するコードを持たない。

- プロジェクトの削除
- ビルドの削除
- テスターの削除
- 公開審査の取り下げ

これらが必要な場合はポータルで手動操作すること。

## 動作環境

macOS / Windows / Linux。Linux はヘッドレスでも動作する。

- Python 3.9 以上
- Node.js（`.ehpk` の作成に公式 CLI `@evenrealities/evenhub-cli` を使う場合）

## インストール

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

ヘッドレス Linux では初回にブラウザの依存ライブラリが要る。

```sh
.venv/bin/playwright install --with-deps chromium   # root 権限が必要
```

## 使い方

### 1. ログイン

```sh
ehup login
```

メールアドレスとパスワードを保存する。保存先は環境に応じて自動で決まる。

| 優先 | 保存先 | 対象 |
|---|---|---|
| 1 | 環境変数 `EVENHUB_EMAIL` / `EVENHUB_PASSWORD` | CI・自動実行 |
| 2 | OS の資格情報ストア（macOS キーチェーン / Windows 資格情報マネージャー / Linux 鍵束） | デスクトップ環境 |
| 3 | 暗号化ファイル `~/.config/even-hub-uploader/vault.bin` | ヘッドレス Linux など |

3 の暗号化ファイルは、復号鍵をマシン固有情報から毎回導出する。
**ファイルを他のマシンへ持ち出しても復号できない。**
ただし同一マシン・同一ユーザー権限のプログラムからは復元できる。

ログインセッションは再利用され、切れていれば保存済みの資格情報で自動的に入り直す。

### 2. `.ehpk` を作る

公式 CLI を使う。

```sh
npx @evenrealities/evenhub-cli pack app.json ./dist -o myapp.ehpk
```

### 3. 新規プロジェクトを作る

```sh
ehup app create --ehpk myapp.ehpk --tagline "A short description"
```

`--name` 省略時は `.ehpk` 内の名前を使う。`--icon` で 24x24 モノクロ PNG を指定できる。

### 4. ビルドをアップロードする

```sh
ehup upload --app com.example.myapp --ehpk myapp.ehpk --notes "修正内容"
ehup upload --app com.example.myapp --ehpk myapp.ehpk --notes @CHANGELOG.txt
```

アップデートノートは 500 文字以内。`@ファイル名` でファイルから読める。

### 5. ベータに上げる

```sh
ehup beta push --app com.example.myapp --version 1.2.0
```

### 6. テスターを追加する

```sh
ehup beta add-testers --app com.example.myapp --email a@example.com --email b@example.com
ehup beta add-testers --app com.example.myapp --from testers.txt
```

既に登録済みのアドレスは飛ばす。

## 共通オプション

| オプション | 内容 |
|---|---|
| `--dry-run` | 確定操作の直前で止める。何が行われるかを確認できる |
| `--headed` | ブラウザを表示して動作を目視する |
| `--json` | 結果を機械可読で出力する |

## 注意

ポータルの画面構造に依存しているため、ポータルが改修されると動かなくなることがある。
その場合は `--headed` で実際の画面を見ながら修正する。

パスワードはログ・エラーメッセージ・例外の表示に出力しない。
