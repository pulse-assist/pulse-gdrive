"""Google Drive API v3: файлы и папки аккаунта, «Доступные мне», общие диски, ссылки.

Drive работает с id, а не путями: путь в @gdrive переводит в id service.py. Документы Google (Docs, Sheets, Slides,
Drawings) — не файлы: читаются экспортом в .docx/.xlsx/.pptx/.png, записать в них нельзя. Ошибки Drive переводятся в
ошибки хранилища pulse_plugin.fs: 404 — NotFound, 409 — Exists, 403 — ReadOnly (или нет места/лимит), 401 — вход истёк.
"""

from collections.abc import Callable, Iterator
from typing import IO

import httpx
from pulse_plugin.fs import Exists, FsError, NoSpace, NotFound, ReadOnly

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE = "application/vnd.google-apps."
# документ Google → (расширение, формат экспорта, редактор Google)
NATIVE = {
    GOOGLE + "document": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document"),
    GOOGLE + "spreadsheet": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "spreadsheets"),
    GOOGLE + "presentation": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                              "presentation"),
    GOOGLE + "drawing": ("png", "image/png", "drawings"),
}
FIELDS = ("id,name,mimeType,size,modifiedTime,driveId,resourceKey,ownedByMe,"
          "capabilities(canEdit,canAddChildren,canDelete,canRename,canTrash),"
          "shortcutDetails(targetId,targetMimeType,targetResourceKey),owners(emailAddress)")
CHUNK = 256 * 1024
PAGE = 1000


class AuthExpired(FsError):
    """Вход в Google истёк"""
    status = 401


def _reason(response: httpx.Response) -> tuple[str, str]:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return "", response.text[:200]
    if isinstance(error, str):
        return error, error
    reasons = [e.get("reason", "") for e in error.get("errors") or []]
    return (reasons[0] if reasons else ""), str(error.get("message") or "")


def check(response: httpx.Response, what: str = "") -> httpx.Response:
    if response.status_code < 400:
        return response
    reason, message = _reason(response)
    code = response.status_code
    if code == 401:
        raise AuthExpired("вход в Google истёк — войдите снова на странице плагина")
    if code == 404:
        raise NotFound(f"не найдено{': ' + what if what else ''} (или нет доступа)")
    if code == 403 and reason in ("storageQuotaExceeded", "teamDriveFileLimitExceeded"):
        raise NoSpace("на Google Диске нет места")
    if code in (403, 429) and "rateLimit" in reason:
        raise FsError("Google Диск просит подождать: слишком много запросов")
    if code == 403 and reason in ("exportSizeLimitExceeded",):
        raise FsError("документ Google слишком большой для экспорта (больше 10 МБ) — откройте его в Google")
    if code == 403:
        raise ReadOnly(message or "нет прав")
    if code == 409:
        raise Exists(message or what)
    raise FsError(f"Google Диск: {message or code}")


def stream_url(http: httpx.Client, url: str, headers: dict, offset: int = 0, length: int | None = None,
               params: dict | None = None) -> Iterator[bytes]:
    """Содержимое (часть — Range); не понял Range — режем сами."""
    headers = dict(headers)
    if offset or length is not None:
        headers["Range"] = f"bytes={offset}-{'' if length is None else offset + length - 1}"
    with http.stream("GET", url, headers=headers, params=params, follow_redirects=True) as response:
        if response.status_code >= 400:
            response.read()
            check(response)
        skip = offset if response.status_code == 200 and offset else 0
        left = length
        for chunk in response.iter_bytes(CHUNK):
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk, skip = chunk[skip:], 0
            if left is not None:
                chunk = chunk[:left]
                left -= len(chunk)
            if chunk:
                yield chunk
            if left == 0:
                break


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    while chunk := stream.read(CHUNK):
        yield chunk


class Drive:
    """Диск одного аккаунта. token() — действующий токен (продление — забота вызывающего)."""

    def __init__(self, http: httpx.Client, token: Callable[[], str]):
        self.http = http
        self.token = token

    def _headers(self, resource_key: str | None = None, file_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}"}
        if resource_key and file_id:
            headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
        return headers

    def _request(self, method: str, url: str, what: str = "", resource_key: str | None = None,
                 file_id: str | None = None, **kwargs) -> httpx.Response:
        headers = {**self._headers(resource_key, file_id), **kwargs.pop("headers", {})}
        try:
            response = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise FsError(f"Google Диск не ответил: {exc.__class__.__name__}") from None
        return check(response, what)

    def about(self) -> dict:
        return self._request("GET", f"{API}/about", params={
            "fields": "user(displayName,emailAddress,permissionId),storageQuota(limit,usage)"}).json()

    def drives(self) -> list[dict]:
        items, token = [], None
        while True:
            params = {"pageSize": 100, "fields": "nextPageToken,drives(id,name)"}
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/drives", params=params).json()
            items += data.get("drives") or []
            token = data.get("nextPageToken")
            if not token:
                return items

    def _files(self, q: str, drive_id: str | None = None) -> list[dict]:
        items, token = [], None
        while True:
            params = {"q": q, "pageSize": PAGE, "fields": f"nextPageToken,files({FIELDS})", "orderBy": "folder,name",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if drive_id:
                params.update(corpora="drive", driveId=drive_id)
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/files", params=params).json()
            items += data.get("files") or []
            token = data.get("nextPageToken")
            if not token:
                return items

    def children(self, folder_id: str, drive_id: str | None = None) -> list[dict]:
        return self._files(f"'{folder_id}' in parents and trashed = false", drive_id)

    def shared_with_me(self) -> list[dict]:
        return self._files("sharedWithMe = true and trashed = false")

    def get(self, file_id: str, resource_key: str | None = None) -> dict:
        return self._request("GET", f"{API}/files/{file_id}", file_id, resource_key, file_id,
                             params={"fields": FIELDS + ",parents", "supportsAllDrives": "true"}).json()

    def read(self, item: dict, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        """Файл — содержимым (Range), документ Google — экспортом."""
        headers = self._headers(item.get("resourceKey"), item["id"])
        native = NATIVE.get(item.get("mimeType", ""))
        if native:
            data = b"".join(stream_url(self.http, f"{API}/files/{item['id']}/export", headers,
                                       params={"mimeType": native[1]}))
            data = data[offset:] if length is None else data[offset:offset + length]
            return iter([data])
        return stream_url(self.http, f"{API}/files/{item['id']}", headers, offset, length,
                          params={"alt": "media", "supportsAllDrives": "true"})

    def _upload(self, method: str, url: str, metadata: dict, stream: IO[bytes]) -> dict:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        start = self._request(method, url, metadata.get("name", ""), json=metadata,
                              params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FIELDS},
                              headers={"X-Upload-Content-Length": str(size)})
        location = start.headers.get("location")
        if not location:
            raise FsError("Google Диск не начал загрузку")
        try:
            response = self.http.put(location, content=_chunks(stream), headers={"Content-Length": str(size)},
                                     timeout=httpx.Timeout(None, connect=10))
        except httpx.HTTPError as exc:
            raise FsError(f"Google Диск не принял файл: {exc.__class__.__name__}") from None
        return check(response, metadata.get("name", "")).json()

    def upload_new(self, parent_id: str, name: str, stream: IO[bytes]) -> dict:
        return self._upload("POST", f"{UPLOAD}/files", {"name": name, "parents": [parent_id]}, stream)

    def update_content(self, file_id: str, stream: IO[bytes]) -> dict:
        return self._upload("PATCH", f"{UPLOAD}/files/{file_id}", {}, stream)

    def create_folder(self, parent_id: str, name: str) -> dict:
        return self._request("POST", f"{API}/files", name, json={"name": name, "mimeType": FOLDER, "parents": [parent_id]},
                             params={"supportsAllDrives": "true", "fields": FIELDS}).json()

    def update(self, file_id: str, name: str | None = None, add_parent: str | None = None,
               remove_parent: str | None = None) -> dict:
        params = {"supportsAllDrives": "true", "fields": FIELDS}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parent:
            params["removeParents"] = remove_parent
        body = {"name": name} if name else {}
        return self._request("PATCH", f"{API}/files/{file_id}", name or file_id, json=body, params=params).json()

    def trash(self, file_id: str) -> None:
        """В корзину Google Диска, не навсегда."""
        self._request("PATCH", f"{API}/files/{file_id}", file_id, json={"trashed": True},
                      params={"supportsAllDrives": "true"})

    def share_link(self, file_id: str) -> str:
        """Доступ «всем, у кого есть ссылка» (чтение) и ссылка."""
        self._request("POST", f"{API}/files/{file_id}/permissions", file_id, json={"role": "reader", "type": "anyone"},
                      params={"supportsAllDrives": "true"})
        return self._request("GET", f"{API}/files/{file_id}", file_id,
                             params={"fields": "webViewLink", "supportsAllDrives": "true"}).json()["webViewLink"]

