"""Вход в Google: браузер владельца и вставка адреса (OAuth 2.0 для приложений «Desktop app», PKCE).

Вход по коду с устройства у Google даёт только право drive.file (файлы, созданные самим приложением) — Диск так не
подключить. Поэтому: плагин даёт ссылку на вход Google, владелец входит и разрешает доступ, Google отправляет браузер
на http://127.0.0.1:1/?code=… — страница не открывается (так задумано), владелец копирует её адрес на страницу
плагина, плагин меняет код на токены. Ни внешний адрес Пульса, ни регистрация адресов возврата не нужны.

Приложение Google Cloud (тип «Desktop app», API Google Drive) — только из настроек плагина: client_id, client_secret.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REDIRECT = "http://127.0.0.1:1/"
SCOPE = "https://www.googleapis.com/auth/drive"


class OAuthError(Exception):
    pass


class App:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret


def app_from(settings: dict, secrets_: dict) -> App | None:
    """Приложение из настроек плагина; не задано — входить нельзя (None)."""
    client_id = (settings.get("client_id") or "").strip()
    client_secret = (secrets_.get("client_secret") or "").strip()
    return App(client_id, client_secret) if client_id and client_secret else None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(app: App, state: str, challenge: str, login_hint: str | None = None) -> str:
    params = {"client_id": app.client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPE,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
              "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true"}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH}?{urlencode(params)}"


def parse_callback(url: str, state: str) -> str:
    """Адрес из браузера после входа → код. Ошибки — понятным текстом."""
    url = (url or "").strip()
    query = parse_qs(urlsplit(url).query) if "?" in url else parse_qs(url)
    if "error" in query:
        error = query["error"][0]
        if error == "access_denied":
            raise OAuthError("Вход отменён: доступ к Диску не разрешён — начните вход заново и нажмите «Разрешить»")
        raise OAuthError(f"Google вернул ошибку: {error}")
    if "code" not in query:
        raise OAuthError("В адресе нет кода входа — скопируйте адрес страницы целиком, он начинается с "
                         "http://127.0.0.1:1/?")
    if query.get("state", [""])[0] != state:
        raise OAuthError("Этот адрес — от другого входа: нажмите «Открыть вход Google» ещё раз и вставьте новый адрес")
    granted = query.get("scope", [SCOPE])[0]
    if SCOPE not in granted.split():
        raise OAuthError("Доступ к Диску не разрешён: при входе отметьте «Просмотр, изменение, создание и удаление "
                         "всех ваших файлов на Google Диске»")
    return query["code"][0]


def _error(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(data.get("error_description") or data.get("error") or response.status_code)
    except ValueError:
        return response.text[:200]


class GoogleOAuth:
    def __init__(self, app: App, http: httpx.Client):
        self.app = app
        self.http = http

    def _token(self, form: dict) -> dict:
        response = self.http.post(TOKEN, data={**form, "client_id": self.app.client_id,
                                               "client_secret": self.app.client_secret})
        if response.status_code != 200:
            text = _error(response)
            if "invalid_client" in text or "unauthorized_client" in text:
                raise OAuthError("Google не принял приложение — проверьте ClientID и Client secret в настройках плагина")
            if "invalid_grant" in text:
                raise OAuthError("Код входа истёк или уже использован — начните вход заново")
            raise OAuthError(f"Google отказал во входе: {text}")
        data = response.json()
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                "expires_at": time.time() + int(data.get("expires_in") or 3600)}

    def exchange(self, code: str, verifier: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                            "code_verifier": verifier})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
