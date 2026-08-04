"""Even Hub の資格情報の保存と取り出し。

保存先は以下の優先順位で決まる。

1. 環境変数 (EVENHUB_EMAIL / EVENHUB_PASSWORD) ... CI やサーバ向け
2. OS の資格情報ストア ......................... 通常利用の既定
3. 暗号化ファイル .............................. 2 が使えない環境のフォールバック

3 はマシン固有情報から鍵を導出する。ファイルを他のマシンへ持ち出しても
復号できないが、同一マシン上のプログラムからは復元できる。2 ほど強くない。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICE = "even-hub-uploader"

ENV_EMAIL = "EVENHUB_EMAIL"
ENV_PASSWORD = "EVENHUB_PASSWORD"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    source: str  # "env" | "keyring" | "file"

    def __repr__(self) -> str:  # パスワードを絶対に文字列化しない
        return f"Credentials(email={self.email!r}, source={self.source!r}, password=***)"

    __str__ = __repr__


# --------------------------------------------------------------------------
# 保存場所
# --------------------------------------------------------------------------


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / SERVICE


def _config_path() -> Path:
    return config_dir() / "config.json"


def _vault_path() -> Path:
    return config_dir() / "vault.bin"


def _ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(d, 0o700)
    return d


def _read_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_config(data: dict) -> None:
    _ensure_config_dir()
    p = _config_path()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(p, 0o600)


# --------------------------------------------------------------------------
# OS の資格情報ストア
# --------------------------------------------------------------------------


def _keyring():
    """使える keyring バックエンドを返す。使えなければ None。"""
    try:
        import keyring
        from keyring.backends import fail as _fail
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, _fail.Keyring):
        return None
    # chainer が中身空、という構成もあるので念のため名前で弾く
    if backend.__class__.__name__ in {"Keyring", "ChainerBackend"} and getattr(
        backend, "priority", 0
    ) <= 0:
        return None
    return keyring


def keyring_available() -> bool:
    return _keyring() is not None


# --------------------------------------------------------------------------
# 暗号化ファイル (フォールバック)
# --------------------------------------------------------------------------


def _machine_id() -> str:
    """マシン固有と見なせる文字列。取得できない要素は黙って飛ばす。"""
    parts: list[str] = [platform.node(), str(Path.home())]

    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    parts.append(line.split('"')[-2])
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux"):
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                parts.append(Path(candidate).read_text(encoding="utf-8").strip())
                break
            except OSError:
                continue
    elif sys.platform == "win32":
        try:
            out = subprocess.run(
                [
                    "reg",
                    "query",
                    r"HKLM\SOFTWARE\Microsoft\Cryptography",
                    "/v",
                    "MachineGuid",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
            parts.append(out.strip().split()[-1])
        except (OSError, subprocess.SubprocessError, IndexError):
            pass

    return "\x00".join(parts)


def _derive_key(salt: bytes) -> bytes:
    raw = hashlib.scrypt(
        _machine_id().encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def _fernet(salt: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise CredentialError(
            "OS の資格情報ストアが使えないため暗号化ファイルへ保存しますが、"
            "cryptography パッケージがありません。`pip install cryptography` を実行してください。"
        ) from exc
    return Fernet(_derive_key(salt))


def _vault_save(email: str, password: str) -> None:
    salt = os.urandom(16)
    blob = _fernet(salt).encrypt(
        json.dumps({"email": email, "password": password}).encode("utf-8")
    )
    _ensure_config_dir()
    p = _vault_path()
    p.write_bytes(b"EHUP1" + salt + blob)
    if sys.platform != "win32":
        os.chmod(p, 0o600)


def _vault_load() -> tuple[str, str] | None:
    p = _vault_path()
    if not p.exists():
        return None
    data = p.read_bytes()
    if not data.startswith(b"EHUP1"):
        raise CredentialError(f"保存ファイルの形式が不正です: {p}")
    salt, blob = data[5:21], data[21:]
    try:
        payload = json.loads(_fernet(salt).decrypt(blob))
    except Exception as exc:
        raise CredentialError(
            f"保存ファイルを復号できません。別のマシンで作成された可能性があります: {p}\n"
            "`ehup login` で保存し直してください。"
        ) from exc
    return payload["email"], payload["password"]


def _vault_delete() -> bool:
    p = _vault_path()
    if p.exists():
        p.unlink()
        return True
    return False


# --------------------------------------------------------------------------
# 公開 API
# --------------------------------------------------------------------------


def save(email: str, password: str) -> str:
    """資格情報を保存し、使った保存先の名前を返す。"""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, email, password)
        except Exception as exc:
            raise CredentialError(f"OS の資格情報ストアへ保存できませんでした: {exc}") from exc
        cfg = _read_config()
        cfg["email"] = email
        cfg["backend"] = "keyring"
        _write_config(cfg)
        _vault_delete()  # 古いフォールバックが残っていたら消す
        return "keyring"

    _vault_save(email, password)
    cfg = _read_config()
    cfg["email"] = email
    cfg["backend"] = "file"
    _write_config(cfg)
    return "file"


def load() -> Credentials:
    """資格情報を取り出す。見つからなければ CredentialError。"""
    env_email = os.environ.get(ENV_EMAIL)
    env_password = os.environ.get(ENV_PASSWORD)
    if env_email and env_password:
        return Credentials(env_email, env_password, "env")

    cfg = _read_config()
    email = cfg.get("email")

    if email and cfg.get("backend") == "keyring":
        kr = _keyring()
        if kr is not None:
            password = kr.get_password(SERVICE, email)
            if password:
                return Credentials(email, password, "keyring")

    stored = _vault_load()
    if stored is not None:
        return Credentials(stored[0], stored[1], "file")

    raise CredentialError(
        "資格情報が保存されていません。`ehup login` を実行してください。"
    )


def status() -> dict:
    """保存状態の要約。パスワードそのものは含めない。"""
    info = {
        "keyring_available": keyring_available(),
        "config_dir": str(config_dir()),
        "env_configured": bool(os.environ.get(ENV_EMAIL) and os.environ.get(ENV_PASSWORD)),
        "stored": False,
        "email": None,
        "source": None,
    }
    try:
        creds = load()
    except CredentialError:
        return info
    info["stored"] = True
    info["email"] = creds.email
    info["source"] = creds.source
    return info


def delete() -> list[str]:
    """保存済みの資格情報を消し、消した先の一覧を返す。"""
    removed: list[str] = []

    cfg = _read_config()
    email = cfg.get("email")
    kr = _keyring()
    if email and kr is not None:
        try:
            if kr.get_password(SERVICE, email) is not None:
                kr.delete_password(SERVICE, email)
                removed.append("keyring")
        except Exception:
            pass

    if _vault_delete():
        removed.append("file")

    p = _config_path()
    if p.exists():
        p.unlink()

    return removed
