"""Сеть до Google и проверка подключения.

Если Google с сервера Пульса недоступен или медленный (например, сервер в России), в настройках задаётся прокси:
по умолчанию через него идёт только вход (accounts.google.com, oauth2.googleapis.com), флажок proxy_all — все
запросы к Google (если не отвечает и Диск). Прокси — http://… или socks5://… (с логином
и паролем в адресе); хранится секретом.
"""

from urllib.parse import urlsplit

import httpx

OAUTH_HOSTS = ("accounts.google.com", "oauth2.googleapis.com")
TIMEOUT = httpx.Timeout(60, connect=10)
CHECK_TIMEOUT = httpx.Timeout(12, connect=10)


class ProxyError(ValueError):
    pass


def proxy_of(settings: dict, secrets: dict) -> str | None:
    url = (secrets.get("proxy") or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "socks5", "socks5h") or not parts.hostname or not parts.port:
        raise ProxyError("Прокси — адрес вида http://логин:пароль@хост:порт или socks5://хост:порт")
    return url


def proxy_label(url: str | None) -> str:
    """Прокси без логина и пароля — для страницы, проверки и журнала."""
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}"


def build_http(settings: dict, secrets: dict, timeout=TIMEOUT) -> httpx.Client:
    """Клиент для Google: без прокси, с прокси только для входа или для всего."""
    headers = {"User-Agent": "Pulse-gdrive"}
    try:
        proxy = proxy_of(settings, secrets)
    except ProxyError:
        proxy = None                                     # ошибку покажет проверка подключения
    if not proxy:
        return httpx.Client(timeout=timeout, headers=headers)
    try:
        if settings.get("proxy_all"):
            return httpx.Client(timeout=timeout, headers=headers, proxy=proxy)
        mounts = {f"all://{host}": httpx.HTTPTransport(proxy=proxy) for host in OAUTH_HOSTS}
        return httpx.Client(timeout=timeout, headers=headers, mounts=mounts)
    except ImportError:                                  # socks5 без socksio — работаем напрямую, проверка подскажет
        return httpx.Client(timeout=timeout, headers=headers)


def reach(http: httpx.Client, url: str) -> tuple[bool, str]:
    """Отвечает ли адрес (любой HTTP-ответ — значит, сеть есть)."""
    try:
        response = http.get(url, timeout=CHECK_TIMEOUT)
    except httpx.ConnectTimeout:
        return False, "нет соединения за 10 с"
    except httpx.TimeoutException:
        return False, "соединение есть, но ответа нет (зависает TLS) — похоже на блокировку по стране"
    except httpx.ProxyError as exc:
        return False, f"прокси не пускает: {exc}"
    except httpx.HTTPError as exc:
        return False, f"ошибка сети: {exc.__class__.__name__}"
    except ImportError:
        return False, "для socks5-прокси нужен пакет socksio — переустановите плагин"
    return True, f"отвечает (HTTP {response.status_code})"
