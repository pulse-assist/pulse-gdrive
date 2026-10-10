"""Аккаунты, вход, подключения и хранилище @gdrive.

Аккаунт — вход в браузере с вставкой адреса (oauth.py), токены продлеваются сами. Подключение — папка верхнего уровня
в @gdrive: «Мой диск», «Доступные мне», общий диск или папка в них (из аккаунта) либо ссылка на файл или папку
(открывается от имени выбранного аккаунта). Drive работает с id, поэтому путь переводится в id по спискам папок:
одинаковые имена в одной папке различаются суффиксом «(2)», документы Google видны как .docx/.xlsx/.pptx/.png
(экспорт, только чтение), ярлыки — как то, на что они ведут.
"""

import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pulse_plugin.fs import Exists, FileProvider, FsError, NotEmpty, NotFound, ReadOnly, entry

from .drive import FOLDER, GOOGLE, NATIVE, SHORTCUT, AuthExpired, Drive
from .oauth import App, GoogleOAuth, OAuthError, auth_url, parse_callback, pkce_pair
from .store import Store

REFRESH_BEFORE_SEC = 300
LOGIN_TTL_SEC = 15 * 60
LIST_TTL_SEC = 15.0
_BAD_NAME = re.compile(r'[/\\<>:"|?*\x00]')
_LINK_ID = [re.compile(r"/(?:file|document|spreadsheets|presentation|drawings|forms)/d/([\w-]{10,})"),
            re.compile(r"/folders/([\w-]{10,})"), re.compile(r"[?&]id=([\w-]{10,})")]
ROOT_TITLES = {"my": "Мой диск", "shared": "Доступные мне"}
# офисные файлы, которые Google открывает в своих редакторах (режим совместимости с Office)
OFFICE_EDITORS = {"docx": "document", "doc": "document", "odt": "document", "rtf": "document",
                  "xlsx": "spreadsheets", "xls": "spreadsheets", "ods": "spreadsheets", "csv": "spreadsheets",
                  "pptx": "presentation", "ppt": "presentation", "odp": "presentation"}
EDITOR_TITLES = {"document": "Google Документы", "spreadsheets": "Google Таблицы", "presentation": "Google Презентации",
                 "drawings": "Google Рисунки", "preview": "просмотр Google Диска"}


class UserError(Exception):
    """Ошибка, понятная владельцу и агенту (400)."""


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or name.startswith(".") or _BAD_NAME.search(name) or len(name) > 100:
        raise UserError("Имя папки — до 100 символов, без / \\ < > : \" | ? * и не с точки")
    return name


def is_folder(item: dict) -> bool:
    return item.get("mimeType") == FOLDER


def link_id(url: str) -> tuple[str, str | None]:
    """Ссылка Google Drive → (id файла или папки, resourcekey)."""
    url = (url or "").strip()
    if not url.startswith(("https://drive.google.com/", "https://docs.google.com/")):
        raise UserError("Это не ссылка Google Drive (https://drive.google.com/… или https://docs.google.com/…)")
    for pattern in _LINK_ID:
        if match := pattern.search(url):
            key = (parse_qs(urlsplit(url).query).get("resourcekey") or [None])[0]
            return match.group(1), key
    raise UserError("В ссылке не нашёлся id файла или папки — скопируйте ссылку «Поделиться» целиком")


def visible(items: list[dict]) -> dict[str, dict]:
    """Список Drive → имя в @gdrive → элемент. Ярлыки — их цель, документы Google — с расширением экспорта,
    неподдерживаемые (Формы, Сайты, Карты) скрыты, одинаковые имена — с «(2)»."""
    out: dict[str, dict] = {}
    taken: set[str] = set()
    for raw in items:
        item = dict(raw)
        if item.get("mimeType") == SHORTCUT:
            target = item.get("shortcutDetails") or {}
            item.update(id=target.get("targetId"), mimeType=target.get("targetMimeType"),
                        resourceKey=target.get("targetResourceKey"), size=None, shortcut=True)
            if not item["id"]:
                continue
        mime = item.get("mimeType") or ""
        if mime.startswith(GOOGLE) and mime != FOLDER and mime not in NATIVE:
            continue
        name = (item.get("name") or "без имени").replace("/", "∕").strip() or "без имени"
        if mime in NATIVE and not name.lower().endswith("." + NATIVE[mime][0]):
            name = f"{name}.{NATIVE[mime][0]}"
        stem, dot, ext = name.rpartition(".") if "." in name.strip(".") and not is_folder(item) else (name, "", "")
        candidate, n = name, 2
        while candidate.casefold() in taken:
            candidate, n = f"{stem} ({n}){dot}{ext}", n + 1
        taken.add(candidate.casefold())
        out[candidate] = item
    return out


def to_entry(name: str, item: dict, readonly_mount: bool = False) -> dict:
    caps = item.get("capabilities") or {}
    native = NATIVE.get(item.get("mimeType", ""))
    folder = is_folder(item)
    readonly = readonly_mount or bool(native) or caps.get("canEdit") is False
    if folder:
        readonly = readonly_mount or caps.get("canAddChildren") is False
    size = item.get("size")
    return entry(name, "dir" if folder else "file", int(size) if size not in (None, "") else None,
                 item.get("modifiedTime"), readonly, None if folder else (native[1] if native else item.get("mimeType")))


class GDrive:
    def __init__(self, store: Store, http: httpx.Client, app: Callable[[], App | None],
                 on_expired: Callable[[dict], None] = lambda account: None, clock=time.time):
        self.store = store
        self.http = http
        self.app = app
        self.on_expired = on_expired
        self.clock = clock
        self._refresh_lock = threading.Lock()
        self._lists: dict[tuple, tuple[float, dict[str, dict]]] = {}
        self._lists_lock = threading.Lock()

    def _oauth(self) -> GoogleOAuth:
        app = self.app()
        if app is None:
            raise UserError("Вход не настроен: укажите ClientID и Client secret приложения Google Cloud в настройках "
                            "плагина «Google Drive» (их выдаёт администратор вашей установки Пульса)")
        return GoogleOAuth(app, self.http)

    # --- вход ---------------------------------------------------------------------------------------------

    def login_state(self) -> dict | None:
        login = self.store.load().get("login")
        if not login:
            return None
        if login["status"] == "pending" and self.clock() - login["created_at"] > LOGIN_TTL_SEC:
            login = {**login, "status": "expired", "error": "Вход не завершён за 15 минут — начните заново"}
        return {k: login.get(k) for k in ("status", "url", "error", "account")}

    def start_login(self, login_hint: str | None = None) -> dict:
        app = self._oauth().app
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        login = {"status": "pending", "state": state, "verifier": verifier, "created_at": self.clock(),
                 "url": auth_url(app, state, challenge, login_hint), "error": None, "account": None}
        self.store.change(lambda data: data.__setitem__("login", login))
        return self.login_state()

    def cancel_login(self) -> None:
        self.store.change(lambda data: data.pop("login", None))

    def finish_login(self, url: str) -> dict:
        login = self.store.load().get("login") or {}
        if login.get("status") != "pending":
            raise UserError("Сначала нажмите «Открыть вход Google»")
        if self.clock() - login["created_at"] > LOGIN_TTL_SEC:
            raise UserError("Вход не завершён за 15 минут — нажмите «Открыть вход Google» ещё раз")
        try:
            code = parse_callback(url, login["state"])
            tokens = self._oauth().exchange(code, login["verifier"])
        except OAuthError as exc:
            raise UserError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise UserError(f"Google не ответил ({exc.__class__.__name__}) — нажмите «Проверить подключение» в "
                            "настройках плагина") from None
        account = self._add_account(tokens)

        def done(data: dict) -> None:
            data["login"] = {"status": "done", "account": account["name"], "error": None, "url": None,
                             "created_at": self.clock()}
        self.store.change(done)
        return self.login_state()

    def _add_account(self, tokens: dict) -> dict:
        info = Drive(self.http, lambda: tokens["access_token"]).about()
        user = info.get("user") or {}
        uid = str(user.get("permissionId") or user.get("emailAddress"))
        account = {"uid": uid, "login": user.get("emailAddress") or uid, "name": user.get("displayName") or user.get("emailAddress"),
                   **{k: v for k, v in tokens.items() if v}, "expired": False}

        def apply(data: dict) -> dict:
            accounts = data.setdefault("accounts", {})
            known = uid in accounts
            accounts[uid] = {**accounts.get(uid, {}), **account}
            mounts = data.setdefault("mounts", [])
            if not known and not any(m.get("account") == uid for m in mounts):
                wanted = "Мой диск" if not any(m["name"] == "Мой диск" for m in mounts) else f"Мой диск — {account['login']}"
                mounts.append({"name": self._free_name(mounts, wanted), "kind": "account", "account": uid, "root": "my"})
            data["expired_signal"] = [u for u in data.get("expired_signal", []) if u != uid]
            return accounts[uid]
        return self.store.change(apply)

    # --- аккаунты -----------------------------------------------------------------------------------------

    def _account(self, ref: str) -> dict:
        accounts = self.store.load().get("accounts") or {}
        ref = (ref or "").strip()
        if not ref and len(accounts) == 1:
            return next(iter(accounts.values()))
        for account in accounts.values():
            if ref.casefold() in (account["uid"].casefold(), account["login"].casefold(),
                                  account["login"].split("@")[0].casefold(), (account.get("name") or "").casefold()):
                return account
        names = ", ".join(a["login"] for a in accounts.values()) or "нет ни одного — сначала войдите"
        raise UserError(f"Аккаунт «{ref}» не найден; аккаунты: {names}")

    def token(self, uid: str) -> str:
        account = (self.store.load().get("accounts") or {}).get(uid)
        if account is None:
            raise AuthExpired("аккаунт отключён")
        if account.get("expired"):
            raise AuthExpired(f"вход в Google ({account['login']}) истёк — войдите снова на странице плагина")
        if account["expires_at"] - self.clock() > REFRESH_BEFORE_SEC:
            return account["access_token"]
        with self._refresh_lock:
            account = self.store.load()["accounts"][uid]
            if account["expires_at"] - self.clock() > REFRESH_BEFORE_SEC:
                return account["access_token"]
            try:
                if not account.get("refresh_token"):
                    raise OAuthError("нет refresh token")
                tokens = self._oauth().refresh(account["refresh_token"])
            except (OAuthError, UserError, httpx.HTTPError):
                self._expire(uid)
                raise AuthExpired(f"вход в Google ({account['login']}) истёк — войдите снова на странице плагина")

            def apply(data: dict) -> None:
                data["accounts"][uid].update({k: v for k, v in tokens.items() if v})
            self.store.change(apply)
            return tokens["access_token"]

    def _expire(self, uid: str) -> None:
        def apply(data: dict) -> dict | None:
            account = (data.get("accounts") or {}).get(uid)
            if account is None:
                return None
            account["expired"] = True
            signalled = data.setdefault("expired_signal", [])
            if uid in signalled:
                return None
            signalled.append(uid)
            return account
        account = self.store.change(apply)
        if account is not None:
            self.on_expired(account)

    def drive(self, uid: str) -> Drive:
        return Drive(self.http, lambda: self.token(uid))

    def accounts(self, space: bool = False) -> list[dict]:
        out = []
        for account in (self.store.load().get("accounts") or {}).values():
            row = {"uid": account["uid"], "login": account["login"], "name": account.get("name"),
                   "expired": bool(account.get("expired"))}
            if space and not row["expired"]:
                try:
                    quota = self.drive(account["uid"]).about().get("storageQuota") or {}
                    row.update(used_space=int(quota["usage"]) if quota.get("usage") else None,
                               total_space=int(quota["limit"]) if quota.get("limit") else None)
                except FsError as exc:
                    row["error"] = str(exc)
                    row["expired"] = isinstance(exc, AuthExpired)
            out.append(row)
        return out

    def logout(self, ref: str) -> dict:
        account = self._account(ref)

        def apply(data: dict) -> None:
            data.get("accounts", {}).pop(account["uid"], None)
            data["mounts"] = [m for m in data.get("mounts", []) if m.get("account") != account["uid"]]
        self.store.change(apply)
        return {"logged_out": account["login"]}

    # --- подключения --------------------------------------------------------------------------------------

    @staticmethod
    def _free_name(mounts: list[dict], wanted: str) -> str:
        base = clean_name(wanted)
        taken = {m["name"].casefold() for m in mounts}
        name, n = base, 2
        while name.casefold() in taken:
            name, n = f"{base} ({n})", n + 1
        return name

    def mounts(self) -> list[dict]:
        data = self.store.load()
        accounts = data.get("accounts") or {}
        out = []
        for mount in data.get("mounts") or []:
            row = {k: mount.get(k) for k in ("name", "kind", "root", "drive_id", "drive_name", "folder_id", "folder_path",
                                             "url", "title", "target_type")}
            row["files_path"] = f"@gdrive/{mount['name']}"
            row["readonly"] = bool(mount.get("readonly"))
            account = accounts.get(mount.get("account"))
            row["account"] = account and {"uid": account["uid"], "login": account["login"], "name": account.get("name"),
                                          "expired": bool(account.get("expired"))}
            out.append(row)
        return out

    def mount(self, name: str) -> dict:
        for mount in self.store.load().get("mounts") or []:
            if mount["name"] == name:
                return mount
        raise NotFound(f"нет подключения «{name}»")

    def _add_mount(self, mount: dict, wanted: str | None, default: str) -> dict:
        def apply(data: dict) -> str:
            mounts = data.setdefault("mounts", [])
            if wanted:
                name = clean_name(wanted)
                if any(m["name"].casefold() == name.casefold() for m in mounts):
                    raise UserError(f"Подключение «{name}» уже есть — выберите другое имя")
            else:
                name = self._free_name(mounts, default)
            mounts.append({**mount, "name": name})
            return name
        name = self.store.change(apply)
        return next(m for m in self.mounts() if m["name"] == name)

    def roots(self, account_ref: str) -> dict:
        """Что можно подключить из аккаунта: «Мой диск», «Доступные мне», общие диски."""
        account = self._account(account_ref)
        drives = [{"id": d["id"], "name": d["name"]} for d in self.drive(account["uid"]).drives()]
        return {"account": account["login"], "roots": [{"root": "my", "title": "Мой диск"},
                                                       {"root": "shared", "title": "Доступные мне"}],
                "drives": drives}

    def browse(self, account_ref: str, root: str = "my", folder_id: str | None = None, drive_id: str | None = None) -> dict:
        """Папки внутри корня или папки — для выбора подключения."""
        account = self._account(account_ref)
        drive = self.drive(account["uid"])
        if folder_id:
            items = drive.children(folder_id, drive_id)
        elif root == "shared":
            items = drive.shared_with_me()
        elif root == "drive":
            if not drive_id:
                raise UserError("Укажите общий диск")
            items = drive.children(drive_id, drive_id)
        else:
            items = drive.children("root")
        folders = [{"id": item["id"], "name": name} for name, item in visible(items).items() if is_folder(item)]
        return {"account": account["login"], "root": root, "drive_id": drive_id, "folder_id": folder_id, "folders": folders}

    def add_root(self, account_ref: str, root: str = "my", drive_id: str | None = None, folder_id: str | None = None,
                 folder_path: str | None = None, name: str | None = None) -> dict:
        account = self._account(account_ref)
        if root not in ("my", "shared", "drive"):
            raise UserError("Корень — my (Мой диск), shared (Доступные мне) или drive (общий диск)")
        mount = {"kind": "account", "account": account["uid"], "root": root}
        default = ROOT_TITLES.get(root, "")
        if root == "drive":
            drives = {d["id"]: d["name"] for d in self.drive(account["uid"]).drives()}
            if not drive_id:
                raise UserError("Укажите общий диск: pulse gdrive drives — список")
            if drive_id not in drives:
                match = [i for i, n in drives.items() if n.casefold() == drive_id.casefold()]
                if not match:
                    raise UserError(f"Общий диск «{drive_id}» не найден; есть: {', '.join(drives.values()) or 'нет'}")
                drive_id = match[0]
            mount.update(drive_id=drive_id, drive_name=drives[drive_id])
            default = drives[drive_id]
        if folder_id:
            item = self.drive(account["uid"]).get(folder_id)
            if not is_folder(item):
                raise UserError("Подключить можно папку, а не файл — файл открывается из папки")
            mount.update(folder_id=folder_id, folder_path=folder_path or item.get("name"))
            default = item.get("name") or default
        return self._add_mount(mount, name, default)

    def preview_link(self, url: str, account_ref: str = "") -> dict:
        file_id, key = link_id(url)
        account = self._account(account_ref)
        try:
            item = self.drive(account["uid"]).get(file_id, key)
        except NotFound:
            raise UserError(f"По ссылке ничего не открылось от имени {account['login']}: нет доступа или ссылку "
                            "закрыли") from None
        owners = ", ".join(o.get("emailAddress", "") for o in item.get("owners") or [])
        return {"url": url, "id": file_id, "name": item.get("name"), "type": "dir" if is_folder(item) else "file",
                "owner": owners or None, "can_edit": bool((item.get("capabilities") or {}).get("canEdit")),
                "account": account["login"]}

    def add_link(self, url: str, account_ref: str = "", name: str | None = None) -> dict:
        preview = self.preview_link(url, account_ref)
        account = self._account(account_ref)
        _, key = link_id(url)
        mount = {"kind": "link", "account": account["uid"], "url": url, "file_id": preview["id"], "resource_key": key,
                 "title": preview["name"], "target_type": preview["type"], "readonly": not preview["can_edit"]}
        return self._add_mount(mount, name, preview["name"] or "Ссылка")

    def rename(self, name: str, new: str) -> dict:
        new = clean_name(new)

        def apply(data: dict) -> None:
            mounts = data.get("mounts") or []
            if not any(m["name"] == name for m in mounts):
                raise UserError(f"Нет подключения «{name}»")
            if new.casefold() != name.casefold() and any(m["name"].casefold() == new.casefold() for m in mounts):
                raise UserError(f"Подключение «{new}» уже есть")
            for m in mounts:
                if m["name"] == name:
                    m["name"] = new
        self.store.change(apply)
        return next(m for m in self.mounts() if m["name"] == new)

    def unmount(self, name: str) -> dict:
        def apply(data: dict) -> None:
            mounts = data.get("mounts") or []
            if not any(m["name"] == name for m in mounts):
                raise UserError(f"Нет подключения «{name}»")
            data["mounts"] = [m for m in mounts if m["name"] != name]
        self.store.change(apply)
        return {"unmounted": name}

    # --- пути -> id -----------------------------------------------------------------------------------------

    def invalidate(self, key: tuple | None = None) -> None:
        with self._lists_lock:
            if key is None:
                self._lists.clear()
            else:
                self._lists.pop(key, None)

    def root_item(self, mount: dict) -> dict:
        if mount["kind"] == "link":
            item = self.drive(mount["account"]).get(mount["file_id"], mount.get("resource_key"))
            return {**item, "resourceKey": item.get("resourceKey") or mount.get("resource_key")}
        if mount.get("folder_id"):
            return {"id": mount["folder_id"], "mimeType": FOLDER, "driveId": mount.get("drive_id")}
        if mount.get("root") == "shared":
            return {"id": None, "mimeType": FOLDER, "virtual": "shared", "capabilities": {"canAddChildren": False}}
        if mount.get("root") == "drive":
            return {"id": mount["drive_id"], "mimeType": FOLDER, "driveId": mount["drive_id"]}
        return {"id": "root", "mimeType": FOLDER}

    def children(self, mount: dict, folder: dict) -> dict[str, dict]:
        key = (mount["account"], folder.get("id") or folder.get("virtual"))
        with self._lists_lock:
            cached = self._lists.get(key)
            if cached and self.clock() - cached[0] < LIST_TTL_SEC:
                return cached[1]
        drive = self.drive(mount["account"])
        if folder.get("virtual") == "shared":
            items = drive.shared_with_me()
        elif not is_folder(folder):                      # ссылка на один файл — папка с этим файлом
            items = [folder]
        else:
            items = drive.children(folder["id"], folder.get("driveId"))
        names = visible(items)
        with self._lists_lock:
            self._lists[key] = (self.clock(), names)
        return names

    def walk(self, path: str) -> tuple[dict, dict | None, dict | None, str]:
        """Путь @gdrive → (подключение, папка-родитель, элемент или None, имя последней части)."""
        name, _, rest = path.partition("/")
        mount = self.mount(name)
        node = self.root_item(mount)
        if not rest:
            return mount, None, node, name
        parts = rest.split("/")
        parent = node
        for i, part in enumerate(parts):
            if not is_folder(parent) and not (i == 0 and mount["kind"] == "link"):
                raise NotFound(path)
            child = self.children(mount, parent).get(part)
            if child is None:
                if i == len(parts) - 1:
                    return mount, parent, None, part
                raise NotFound(path)
            if i < len(parts) - 1:
                parent = child
            else:
                return mount, parent, child, part
        raise NotFound(path)

    def editor(self, path: str) -> dict:
        """Где открыть файл в Google: редактор документа Google, редактор Office-файла или просмотр Диска."""
        mount, _, item, name = self.walk(path)
        if item is None or is_folder(item):
            raise NotFound(path)
        account = (self.store.load().get("accounts") or {}).get(mount["account"]) or {}
        native = NATIVE.get(item.get("mimeType", ""))
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        kind = native[2] if native else OFFICE_EDITORS.get(ext)
        params = {"authuser": account.get("login") or ""}
        if item.get("resourceKey"):
            params["resourcekey"] = item["resourceKey"]
        if kind:
            if not native:
                params["rtpof"] = "true"                 # Office-файл в редакторе Google
            url = f"https://docs.google.com/{kind}/d/{item['id']}/edit?{urlencode(params)}"
        else:
            kind = "preview"
            url = f"https://drive.google.com/file/d/{item['id']}/preview?{urlencode(params)}"
        open_url = url.replace("/preview?", "/view?")
        return {"url": url, "open_url": open_url, "title": EDITOR_TITLES[kind], "account": account.get("login"),
                "name": name, "native": bool(native)}


class GDriveFiles(FileProvider):
    """@gdrive/<подключение>/… → Google Drive аккаунта."""

    def __init__(self, gdrive: GDrive):
        self.g = gdrive

    def list(self, path: str) -> list[dict]:
        if not path:
            return [entry(m["name"], "dir", readonly=bool(m.get("readonly"))) for m in self.g.store.load().get("mounts") or []]
        mount, _, item, _ = self.g.walk(path)
        if item is None:
            raise NotFound(path)
        if not is_folder(item) and not (mount["kind"] == "link" and "/" not in path):
            raise FsError("это файл, а не папка")
        return [to_entry(name, child, bool(mount.get("readonly"))) for name, child in self.g.children(mount, item).items()]

    def stat(self, path: str) -> dict:
        if not path:
            return entry("", "dir")
        mount, _, item, name = self.g.walk(path)
        if item is None:
            raise NotFound(path)
        if "/" not in path:
            return entry(mount["name"], "dir", readonly=bool(mount.get("readonly")))
        return to_entry(name, item, bool(mount.get("readonly")))

    def read(self, path: str, offset: int = 0, length: int | None = None):
        mount, _, item, _ = self.g.walk(path)
        if item is None:
            raise NotFound(path)
        if is_folder(item) or "/" not in path:
            raise FsError("это папка")
        return self.g.drive(mount["account"]).read(item, offset, length)

    def _writable_parent(self, path: str, what: str) -> tuple[dict, dict, dict | None, str]:
        if "/" not in path:
            raise ReadOnly(f"{what}: подключения меняются на странице плагина «Google Drive»")
        mount, parent, item, name = self.g.walk(path)
        if mount.get("readonly"):
            raise ReadOnly("ссылка открыта только на чтение")
        if parent is None or parent.get("virtual") == "shared":
            raise ReadOnly("в «Доступные мне» нельзя положить файл — выберите папку внутри")
        return mount, parent, item, name

    def write(self, path: str, stream, overwrite: bool = True) -> dict:
        mount, parent, item, name = self._writable_parent(path, "файл в корне @gdrive")
        drive = self.g.drive(mount["account"])
        if item is not None:
            if is_folder(item):
                raise Exists(path)
            if item.get("mimeType", "") in NATIVE:
                raise ReadOnly("документ Google меняется в Google — откройте его в «Файлах» (редактор Google)")
            if not overwrite:
                raise Exists(path)
            saved = drive.update_content(item["id"], stream)
        else:
            saved = drive.upload_new(parent["id"], name, stream)
        self.g.invalidate((mount["account"], parent["id"]))
        return to_entry(name, saved)

    def mkdir(self, path: str) -> dict:
        mount, parent, item, name = self._writable_parent(path, "новая папка в корне @gdrive")
        if item is not None:
            raise Exists(path)
        created = self.g.drive(mount["account"]).create_folder(parent["id"], name)
        self.g.invalidate((mount["account"], parent["id"]))
        return to_entry(name, created)

    def move(self, source: str, target: str) -> dict:
        if "/" not in source and "/" not in target:              # переименование подключения
            self.g.rename(source, target)
            return entry(target, "dir")
        src_mount, src_parent, item, _ = self._writable_parent(source, "перенос подключения")
        dst_mount, dst_parent, existing, new_name = self._writable_parent(target, "перенос в корень @gdrive")
        if item is None:
            raise NotFound(source)
        if existing is not None:
            raise Exists(target)
        if src_mount["account"] == dst_mount["account"]:
            native = NATIVE.get(item.get("mimeType", ""))
            if native and new_name.lower().endswith("." + native[0]):
                new_name = new_name[: -len(native[0]) - 1]         # у документа Google в Drive нет расширения
            moved = self.g.drive(src_mount["account"]).update(
                item["id"], name=new_name if new_name != item.get("name") else None,
                add_parent=dst_parent["id"] if dst_parent["id"] != src_parent["id"] else None,
                remove_parent=src_parent["id"] if dst_parent["id"] != src_parent["id"] else None)
            self.g.invalidate((src_mount["account"], src_parent["id"]))
            self.g.invalidate((dst_mount["account"], dst_parent["id"]))
            return to_entry(target.rsplit("/", 1)[-1], moved)
        if is_folder(item):
            raise FsError("папки между аккаунтами переносите по файлам")
        if item.get("mimeType", "") in NATIVE:
            raise FsError("документ Google между аккаунтами не переносится — скачайте экспорт и загрузите")
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            for chunk in self.read(source):
                spool.write(chunk)
            spool.seek(0)
            result = self.write(target, spool, overwrite=False)
        self.delete(source)
        return result

    def delete(self, path: str, recursive: bool = False) -> None:
        mount, parent, item, _ = self._writable_parent(path, "отключить подключение")
        if item is None:
            raise NotFound(path)
        if is_folder(item) and not recursive and self.g.children(mount, item):
            raise NotEmpty("папка не пуста")
        self.g.drive(mount["account"]).trash(item["id"])
        self.g.invalidate((mount["account"], parent["id"]))
