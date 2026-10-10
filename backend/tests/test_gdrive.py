"""Плагин без сети: Google OAuth и Drive API подменены (httpx.MockTransport). Запуск: cd backend && pytest"""

import json
import os
import re
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="gdrive-test-"))
os.environ.setdefault("PULSE_PLUGIN_SETTINGS", json.dumps({"client_id": "test-app"}))
os.environ.setdefault("PULSE_PLUGIN_SECRETS", json.dumps({"client_secret": "test-secret"}))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gdrive_plugin import app as module  # noqa: E402
from gdrive_plugin.oauth import App, app_from  # noqa: E402
from gdrive_plugin.service import GDrive, link_id  # noqa: E402
from gdrive_plugin.store import Store  # noqa: E402

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SLIDES = "application/vnd.google-apps.presentation"


class FakeGoogle:
    """OAuth и Drive v3 одного-двух аккаунтов в памяти."""

    def __init__(self):
        self.tokens = {}
        self.next_user = "u1"
        self.refresh_ok = True
        self.issued = 0
        self.users = {"u1": {"displayName": "Artem D.", "emailAddress": "artem@gmail.com", "permissionId": "u1"},
                      "u2": {"displayName": "Work", "emailAddress": "work@corp.com", "permissionId": "u2"}}
        self.files = {}
        self.seq = 0
        self.ranges = []
        self.uploads = {}
        for fid, name, mime, parent, content, extra in [
            ("f-docs", "Документы", FOLDER, "root", None, {}),
            ("f-plan", "план.txt", "text/plain", "f-docs", "план".encode(), {}),
            ("f-plan2", "план.txt", "text/plain", "f-docs", b"second", {}),        # то же имя в той же папке
            ("f-gdoc", "Отчёт", DOC, "f-docs", None, {}),
            ("f-deck", "Питч.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "root",
             bytes(range(200)), {}),
            ("f-form", "Анкета", "application/vnd.google-apps.form", "root", None, {}),
            ("f-short", "Ярлык на отчёт", "application/vnd.google-apps.shortcut", "root", None,
             {"shortcutDetails": {"targetId": "f-gdoc", "targetMimeType": DOC}}),
            ("f-shared", "Проект", FOLDER, "other-root", None, {"sharedWithMe": True}),
            ("f-shared-file", "ТЗ.pdf", "application/pdf", "f-shared", b"%PDF-1", {}),
            ("d-mkt", "Маркетинг", FOLDER, None, None, {"driveId": "d-mkt"}),
            ("f-mkt", "бриф.md", "text/markdown", "d-mkt", b"# brief", {"driveId": "d-mkt"}),
            ("f-pub", "Публичный", FOLDER, "stranger", None, {"canEdit": False}),
            ("f-pub-file", "readme.txt", "text/plain", "f-pub", b"hello", {"canEdit": False}),
        ]:
            self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "parents": [parent] if parent else [],
                               "content": content, "modifiedTime": "2026-10-01T10:00:00.000Z", "trashed": False,
                               "canEdit": extra.get("canEdit", True), **{k: v for k, v in extra.items() if k != "canEdit"}}

    def meta(self, f: dict) -> dict:
        out = {k: f[k] for k in ("id", "name", "mimeType", "modifiedTime")}
        if f["content"] is not None and not f["mimeType"].startswith("application/vnd.google-apps."):
            out["size"] = str(len(f["content"]))
        if f.get("shortcutDetails"):
            out["shortcutDetails"] = f["shortcutDetails"]
        if f.get("driveId"):
            out["driveId"] = f["driveId"]
        out["capabilities"] = {"canEdit": f["canEdit"], "canAddChildren": f["canEdit"], "canDelete": True}
        out["parents"] = f["parents"]
        out["owners"] = [{"emailAddress": "owner@example.com"}]
        return out

    def _user(self, request) -> str | None:
        return self.tokens.get(request.headers.get("authorization", "").removeprefix("Bearer "))

    def _issue(self, uid: str) -> dict:
        self.issued += 1
        token = f"tok-{uid}-{self.issued}"
        self.tokens[token] = uid
        return {"access_token": token, "refresh_token": f"ref-{uid}", "expires_in": 3599, "token_type": "Bearer"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        q = parse_qs(url.query)
        if url.netloc == "oauth2.googleapis.com":
            if request.method == "GET":
                return httpx.Response(405)
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["test-app"] and form["client_secret"] == ["test-secret"]
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"][0] and form["redirect_uri"] == ["http://127.0.0.1:1/"]
                if form["code"] != ["good-code"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue(self.next_user))
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            data = self._issue(form["refresh_token"][0].removeprefix("ref-"))
            data.pop("refresh_token")                    # Google не присылает новый refresh token
            return httpx.Response(200, json=data)
        if url.netloc == "upload.test":
            body = request.read()
            session = self.uploads.pop(url.path)
            if session.get("id"):
                f = self.files[session["id"]]
                f["content"] = body
            else:
                self.seq += 1
                fid = f"new-{self.seq}"
                f = self.files[fid] = {"id": fid, "name": session["name"], "mimeType": "application/octet-stream",
                                       "parents": session["parents"], "content": body, "trashed": False, "canEdit": True,
                                       "modifiedTime": "2026-10-10T10:00:00.000Z"}
            return httpx.Response(200, json=self.meta(f))
        uid = self._user(request)
        if uid is None:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        path = url.path
        if path == "/drive/v3/about":
            return httpx.Response(200, json={"user": self.users[uid], "storageQuota": {"limit": "1000", "usage": "540"}})
        if path == "/drive/v3/drives":
            return httpx.Response(200, json={"drives": [{"id": "d-mkt", "name": "Маркетинг"}]})
        if path == "/drive/v3/files" and request.method == "GET":
            query = q["q"][0]
            if "sharedWithMe" in query:
                found = [f for f in self.files.values() if f.get("sharedWithMe") and not f["trashed"]]
            else:
                parent = re.match(r"'([^']+)' in parents", query).group(1)
                found = [f for f in self.files.values() if parent in f["parents"] and not f["trashed"]]
            return httpx.Response(200, json={"files": [self.meta(f) for f in found]})
        if path == "/drive/v3/files" and request.method == "POST":
            body = json.loads(request.content)
            self.seq += 1
            fid = f"dir-{self.seq}"
            self.files[fid] = {"id": fid, "name": body["name"], "mimeType": body["mimeType"], "parents": body["parents"],
                               "content": None, "trashed": False, "canEdit": True, "modifiedTime": "2026-10-10T10:00:00.000Z"}
            return httpx.Response(200, json=self.meta(self.files[fid]))
        if path.startswith("/upload/drive/v3/files"):
            body = json.loads(request.content or b"{}")
            self.seq += 1
            session = f"/s{self.seq}"
            fid = path.removeprefix("/upload/drive/v3/files").strip("/")
            parents = body.get("parents") or []
            if parents and not self.files.get(parents[0], {}).get("canEdit", True):
                return httpx.Response(403, json={"error": {"code": 403, "message": "Insufficient permissions",
                                                           "errors": [{"reason": "insufficientFilePermissions"}]}})
            self.uploads[session] = {"id": fid or None, "name": body.get("name"), "parents": parents}
            return httpx.Response(200, headers={"Location": f"https://upload.test{session}"})
        m = re.match(r"^/drive/v3/files/([^/]+)(/export|/permissions)?$", path)
        if not m or m.group(1) not in self.files:
            return httpx.Response(404, json={"error": {"code": 404, "message": "File not found"}})
        f = self.files[m.group(1)]
        if m.group(2) == "/export":
            return httpx.Response(200, content=f"export of {f['name']} as {q['mimeType'][0]}".encode())
        if m.group(2) == "/permissions":
            return httpx.Response(200, json={"id": "anyone"})
        if request.method == "PATCH":
            body = json.loads(request.content or b"{}")
            if body.get("trashed"):
                f["trashed"] = True
            if body.get("name"):
                f["name"] = body["name"]
            if q.get("addParents"):
                f["parents"] = [p for p in f["parents"] if p not in q.get("removeParents", [])] + q["addParents"]
            return httpx.Response(200, json=self.meta(f))
        if q.get("alt") == ["media"]:
            rng = request.headers.get("range")
            if rng:
                self.ranges.append(rng)
                start, _, end = rng.removeprefix("bytes=").partition("-")
                return httpx.Response(206, content=f["content"][int(start):(int(end) + 1 if end else None)])
            return httpx.Response(200, content=f["content"])
        if q.get("fields") == ["webViewLink"]:
            return httpx.Response(200, json={"webViewLink": f"https://drive.google.com/file/d/{f['id']}/view"})
        return httpx.Response(200, json=self.meta(f))


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = FakeGoogle()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    signals = []
    service = GDrive(Store(tmp_path), http, lambda: App("test-app", "test-secret"), on_expired=signals.append)
    monkeypatch.setattr(module, "gdrive", service)
    monkeypatch.setattr(module.files, "g", service)
    fake.service, fake.signals = service, signals
    return fake


@pytest.fixture
def client(fake):
    return TestClient(module.app)


def login(client, fake, user="u1"):
    fake.next_user = user
    started = client.post("/login", json={}).json()
    assert started["status"] == "pending" and started["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    done = client.post("/login/finish", json={
        "url": f"http://127.0.0.1:1/?state={state}&code=good-code&scope=https://www.googleapis.com/auth/drive"})
    assert done.status_code == 200 and done.json()["status"] == "done", done.text


# --- вход ------------------------------------------------------------------------------------------------

def test_app_only_from_settings():
    assert app_from({}, {}) is None and app_from({"client_id": "x"}, {}) is None
    assert app_from({"client_id": " x "}, {"client_secret": "s"}).client_id == "x"


def test_login_by_pasted_address(client, fake):
    started = client.post("/login", json={}).json()
    query = parse_qs(urlsplit(started["url"]).query)
    assert query["redirect_uri"] == ["http://127.0.0.1:1/"] and query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"] and "drive" in query["scope"][0]
    state = query["state"][0]
    wrong = client.post("/login/finish", json={"url": "http://127.0.0.1:1/?state=other&code=good-code"})
    assert wrong.status_code == 400 and "другого входа" in wrong.json()["detail"]
    denied = client.post("/login/finish", json={"url": f"http://127.0.0.1:1/?state={state}&error=access_denied"})
    assert denied.status_code == 400 and "отменён" in denied.json()["detail"]
    no_scope = client.post("/login/finish", json={"url": f"http://127.0.0.1:1/?state={state}&code=good-code&scope=email"})
    assert no_scope.status_code == 400 and "Доступ к Диску" in no_scope.json()["detail"]
    login(client, fake)
    state_ = client.get("/state").json()
    assert state_["accounts"][0]["login"] == "artem@gmail.com" and state_["accounts"][0]["used_space"] == 540
    assert [m["name"] for m in state_["mounts"]] == ["Мой диск"]
    login(client, fake, "u2")
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["Мой диск", "Мой диск — work@corp.com"]


def test_refresh_keeps_refresh_token_and_signals_once(client, fake):
    login(client, fake)
    fake.service.store.change(lambda d: d["accounts"]["u1"].update(expires_at=0))
    assert client.get("/fs/list", params={"path": "Мой диск"}).status_code == 200
    account = fake.service.store.load()["accounts"]["u1"]
    assert account["refresh_token"] == "ref-u1" and account["access_token"] == "tok-u1-2"
    fake.refresh_ok = False
    fake.service.store.change(lambda d: d["accounts"]["u1"].update(expires_at=0))
    fake.service.invalidate()                            # списки папок кэшируются на 15 с
    assert client.get("/fs/list", params={"path": "Мой диск"}).status_code == 401
    client.get("/fs/list", params={"path": "Мой диск"})
    assert [a["login"] for a in fake.signals] == ["artem@gmail.com"]


# --- хранилище @gdrive -----------------------------------------------------------------------------------

def test_my_drive_names_natives_shortcuts_and_duplicates(client, fake):
    login(client, fake)
    top = {i["name"]: i for i in client.get("/fs/list", params={"path": "Мой диск"}).json()["items"]}
    assert set(top) == {"Документы", "Питч.pptx", "Ярлык на отчёт.docx"}            # анкета скрыта, ярлык — как цель
    docs = {i["name"]: i for i in client.get("/fs/list", params={"path": "Мой диск/Документы"}).json()["items"]}
    assert set(docs) == {"план.txt", "план (2).txt", "Отчёт.docx"}
    assert docs["Отчёт.docx"]["readonly"] is True and docs["план.txt"]["readonly"] is False
    assert client.get("/fs/read", params={"path": "Мой диск/Документы/план (2).txt"}).content == b"second"
    exported = client.get("/fs/read", params={"path": "Мой диск/Документы/Отчёт.docx"}).content
    assert exported.startswith(b"export of") and b"wordprocessingml" in exported
    part = client.get("/fs/read", params={"path": "Мой диск/Питч.pptx"}, headers={"Range": "bytes=5-9"})
    assert part.status_code == 206 and part.content == bytes([5, 6, 7, 8, 9]) and fake.ranges[-1] == "bytes=5-9"


def test_write_mkdir_move_delete(client, fake):
    login(client, fake)
    put = client.put("/fs/write", params={"path": "Мой диск/Документы/итоги.md"}, content="итоги".encode())
    assert put.status_code == 200
    created = next(f for f in fake.files.values() if f["name"] == "итоги.md")
    assert created["parents"] == ["f-docs"] and created["content"] == "итоги".encode()
    assert client.put("/fs/write", params={"path": "Мой диск/Документы/итоги.md"}, content=b"v2").status_code == 200
    assert created["content"] == b"v2"
    assert client.put("/fs/write", params={"path": "Мой диск/Документы/итоги.md", "overwrite": "false"},
                      content=b"x").status_code == 409
    native = client.put("/fs/write", params={"path": "Мой диск/Документы/Отчёт.docx"}, content=b"x")
    assert native.status_code == 403 and "в Google" in native.json()["detail"]
    assert client.post("/fs/mkdir", json={"path": "Мой диск/Архив"}).status_code == 200
    assert client.post("/fs/move", json={"source": "Мой диск/Документы/Отчёт.docx",
                                         "target": "Мой диск/Архив/Итоги года.docx"}).status_code == 200
    report = fake.files["f-gdoc"]
    assert report["name"] == "Итоги года" and report["parents"][0].startswith("dir-")   # без .docx у документа Google
    assert client.delete("/fs/delete", params={"path": "Мой диск/Документы"}).status_code == 409   # не пуста
    assert client.delete("/fs/delete", params={"path": "Мой диск/Документы", "recursive": "true"}).status_code == 200
    assert fake.files["f-docs"]["trashed"] is True
    assert client.post("/fs/mkdir", json={"path": "Своя"}).status_code == 403
    assert client.post("/fs/move", json={"source": "Мой диск", "target": "Личный"}).status_code == 200
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["Личный"]


def test_shared_with_me_shared_drive_and_links(client, fake):
    login(client, fake)
    roots = client.get("/roots", params={"account": "artem"}).json()
    assert roots["drives"] == [{"id": "d-mkt", "name": "Маркетинг"}]
    shared = client.post("/mounts", json={"kind": "account", "account": "artem", "root": "shared"}).json()
    assert shared["name"] == "Доступные мне"
    assert [i["name"] for i in client.get("/fs/list", params={"path": "Доступные мне"}).json()["items"]] == ["Проект"]
    assert client.get("/fs/read", params={"path": "Доступные мне/Проект/ТЗ.pdf"}).content == b"%PDF-1"
    assert client.put("/fs/write", params={"path": "Доступные мне/x.txt"}, content=b"x").status_code == 403

    drive = client.post("/mounts", json={"kind": "account", "account": "artem", "root": "drive", "drive_id": "Маркетинг"}).json()
    assert drive["name"] == "Маркетинг" and drive["drive_id"] == "d-mkt"
    assert [i["name"] for i in client.get("/fs/list", params={"path": "Маркетинг"}).json()["items"]] == ["бриф.md"]

    folders = client.get("/browse", params={"account": "artem", "root": "my"}).json()["folders"]
    assert folders == [{"id": "f-docs", "name": "Документы"}]
    sub = client.post("/mounts", json={"kind": "account", "account": "artem", "root": "my", "folder_id": "f-docs"}).json()
    assert sub["name"] == "Документы"

    assert link_id("https://drive.google.com/drive/folders/f-pub1234567?usp=sharing&resourcekey=0-abc") == \
        ("f-pub1234567", "0-abc")
    with pytest.raises(Exception):
        link_id("https://example.com/x")
    fake.files["f-pub1234567"] = {**fake.files["f-pub"], "id": "f-pub1234567"}
    fake.files["f-pub-file"]["parents"] = ["f-pub1234567"]
    preview = client.get("/link", params={"url": "https://drive.google.com/drive/folders/f-pub1234567"}).json()
    assert preview["type"] == "dir" and preview["can_edit"] is False
    link = client.post("/mounts", json={"kind": "link", "url": "https://drive.google.com/drive/folders/f-pub1234567"}).json()
    assert link["readonly"] is True
    assert client.get("/fs/read", params={"path": "Публичный/readme.txt"}).content == b"hello"
    assert client.put("/fs/write", params={"path": "Публичный/x.txt"}, content=b"x").status_code == 403
    # ссылка на один файл — папка с этим файлом
    fake.files["f-deck-1234567"] = {**fake.files["f-deck"], "id": "f-deck-1234567"}
    one = client.post("/mounts", json={"kind": "link", "url": "https://drive.google.com/file/d/f-deck-1234567/view"}).json()
    items = client.get("/fs/list", params={"path": one["name"]}).json()["items"]
    assert [i["name"] for i in items] == ["Питч.pptx"]
    assert len(client.get("/fs/read", params={"path": f"{one['name']}/Питч.pptx"}).content) == 200


# --- редактор Google в «Файлах» ------------------------------------------------------------------------------

def test_editor_urls_and_page(client, fake, monkeypatch):
    login(client, fake)
    native = fake.service.editor("Мой диск/Документы/Отчёт.docx")
    assert native["url"].startswith("https://docs.google.com/document/d/f-gdoc/edit?") and "authuser=artem%40gmail.com" in native["url"]
    assert native["native"] is True and native["title"] == "Google Документы"
    office = fake.service.editor("Мой диск/Питч.pptx")
    assert office["url"].startswith("https://docs.google.com/presentation/d/f-deck/edit?") and "rtpof=true" in office["url"]
    client.post("/mounts", json={"kind": "account", "account": "artem", "root": "shared"})
    pdf = fake.service.editor("Доступные мне/Проект/ТЗ.pdf")
    assert pdf["url"].startswith("https://drive.google.com/file/d/f-shared-file/preview?") and pdf["title"] == "просмотр Google Диска"

    class Ticket:
        def __init__(self, path):
            self.path = path

        def meta(self):
            return {"path": self.path}
    monkeypatch.setattr(module.plugin, "file_ticket", lambda ticket: Ticket(
        "@gdrive/Мой диск/Документы/Отчёт.docx" if ticket == "t1" else "@gdrive/Мой диск/нет.docx"))
    page = client.get("/editor", params={"ticket": "t1"}).text
    assert 'src="https://docs.google.com/document/d/f-gdoc/edit?' in page and "Открыть в Google" in page
    assert '"pulse:file-status"' in page and 'const status = "ready"' in page
    missing = client.get("/editor", params={"ticket": "t2"}).text
    assert 'const status = "error"' in missing and "Не удалось открыть" in missing and "</script>" not in missing.split("const message")[1].split(";")[0]


# --- проверка и команды ---------------------------------------------------------------------------------------

def test_check_and_commands(client, fake):
    login(client, fake)
    result = client.post("/commands/check", json={}).json()
    assert result["ok"] is True, result
    assert {c["title"] for c in result["checks"]} >= {"Приложение Google Cloud", "Вход: oauth2.googleapis.com",
                                                      "Диск: www.googleapis.com", "Аккаунт artem@gmail.com"}
    assert client.post("/commands/drives", json={}).json()["drives"][0]["name"] == "Маркетинг"
    mounted = client.post("/commands/mount", json={"root": "shared"}).json()
    assert mounted["files_path"] == "@gdrive/Доступные мне"
    shared = client.post("/commands/share", json={"path": "@gdrive/Мой диск/Питч.pptx"}).json()
    assert shared["url"] == "https://drive.google.com/file/d/f-deck/view"
    assert client.post("/commands/login/finish", json={}).status_code == 400
    assert client.post("/commands/logout", json={}).json() == {"logged_out": "artem@gmail.com"}
    assert client.get("/state").json()["mounts"] == []
