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
| `ehup app icon` | 既存プロジェクトのアイコンを変更する |
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
- 公開審査への提出（Submit for review）と取り下げ
- ストアリスティング下書きの破棄（Revert）

これらが必要な場合はポータルで手動操作すること。

## 動作環境

macOS / Windows / Linux。Linux はヘッドレスでも動作する。

- Python 3.9 以上
- Node.js（`.ehpk` の作成に公式 CLI `@evenrealities/evenhub-cli` を使う場合）

## インストール

```sh
git clone git@github.com:takashicompany/even-hub-uploader.git
cd even-hub-uploader

python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

SSH 鍵を使わない場合は HTTPS でも取得できる。

```sh
gh repo clone takashicompany/even-hub-uploader
# または
git clone https://github.com/takashicompany/even-hub-uploader.git
```

プライベートリポジトリなので、取得にはアクセス権のある GitHub アカウントの認証が要る。

### PATH から使えるようにする

```sh
ln -sf "$(pwd)/.venv/bin/ehup" ~/.local/bin/ehup
```

`~/.local/bin` が PATH に入っていれば、以後どこからでも `ehup` で実行できる。

### ヘッドレス Linux の場合

ブラウザが起動しないときは、依存ライブラリを入れる。

```sh
.venv/bin/playwright install --with-deps chromium   # root 権限が必要
```

### 更新する

```sh
git pull
```

依存関係が変わったときのみ `.venv/bin/pip install -e .` を再実行する。

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

**既定では、作成に続けて次まで行う。**

1. アップロードしたビルドを Beta にする
2. ログイン中のメールアドレスをベータテスターに追加し、招待を送る

`--tester` で追加のアドレスを指定できる（複数可）。`--no-beta` で 1・2 を止め、プロジェクト作成だけにできる。

```sh
ehup app create --ehpk myapp.ehpk --tagline "..." --tester a@example.com
ehup app create --ehpk myapp.ehpk --tagline "..." --no-beta
```

`--name` 省略時は `.ehpk` 内の名前を使う。`--icon` で 24x24 モノクロ PNG を指定できる。

### 4. アイコンを変更する

```sh
ehup app icon --app com.example.myapp --icon icon.png
ehup app icon --app com.example.myapp --icon icon.png --dry-run
```

ポータルの Store listing → Basic info を操作する。アイコンは 24x24 のモノクロ PNG。
寸法が違う場合は、ポータルに触れる前に止まる。

ポータルはアイコンの中身にも条件を課している（画面上には説明が無く、
外れると `invalid icon pixel: x, y` で保存が失敗する）。

| 条件 | 内容 |
|---|---|
| 色 | 点灯画素は `#F4F4F4` で不透明、消灯画素は完全に透明 |
| 升目 | 2x2 単位（実質 12x12）で描かれていること |

**この変換は ehup が自動で行う。** 白黒で描いた PNG をそのまま渡してよい。

- 透明を含む PNG は不透明な部分を、含まない PNG は暗い部分を点灯画素とみなす
- 2x2 に揃っていない画素は多数決で塗り直し、塗り直した数を表示する

**審査を通ったビルドがあるプロジェクトでは、既定で確定せずに止まる。**
ポータルはそうしたプロジェクトの Store listing の変更を「下書き」に溜め、
Submit for review（再審査）を通すまで公開中の内容を書き換えないため、
アイコンだけを差し替えることができない。

下書きとして保存するところまでで良ければ `--allow-draft` を付ける。

```sh
ehup app icon --app com.example.myapp --icon icon.png --allow-draft
```

この場合も審査には出さない。下書きの提出・破棄はポータルで行うこと。

### 5. ビルドをアップロードする

```sh
ehup upload --app com.example.myapp --ehpk myapp.ehpk --notes "修正内容"
ehup upload --app com.example.myapp --ehpk myapp.ehpk --notes @CHANGELOG.txt
```

アップデートノートは 500 文字以内。`@ファイル名` でファイルから読める。

### 6. ベータに上げる

```sh
ehup beta push --app com.example.myapp --version 1.2.0
```

### 7. テスターを追加する

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
