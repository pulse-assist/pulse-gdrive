"""Плагин «Google Drive»: аккаунты (вход в браузере и вставка адреса), подключения («Мой диск», «Доступные мне»,
общие диски, папки, ссылки), хранилище @gdrive для «Файлов» и агентов и редактор Google в «Файлах».

Страница (pulse.plugin): GET /state, GET|POST|DELETE /login, POST /login/finish, DELETE /accounts/{uid}, GET /roots,
GET /browse, GET /link, POST /mounts, PATCH|DELETE /mounts/{name}, GET /check.
Встроенная страница «Файлов» (embed, /plugin-apps/gdrive/editor?ticket=…): документ в редакторе Google — Документы,
Таблицы, Презентации (и Office-файлы с Диска в них же), прочее — просмотр Google Диска.
Агенты: pulse gdrive accounts|login|login finish|logout|drives|browse|mounts|mount|mount link|rename|unmount|check|
share. Хранилище для ядра — /fs/… (pulse_plugin.fs).
"""

import html
import json

from fastapi import Body, HTTPException, Query
from fastapi.responses import HTMLResponse
from pulse_plugin import CommandError, Plugin, PulseApiError
from pulse_plugin.fs import FsError

from . import net
from .drive import API, AuthExpired
from .oauth import TOKEN, app_from
from .service import GDrive, GDriveFiles, UserError
from .store import Store

plugin = Plugin()
app = plugin.app
http = net.build_http(plugin.settings, plugin.secrets)


def expired_signal(account: dict) -> None:
    """Продлить вход не удалось — один сигнал на аккаунт, пока владелец не войдёт снова."""
    try:
        plugin.signal(f"Войдите в Google снова: {account['login']}",
                      body=f"Вход в аккаунт Google {account['login']} истёк — его подключения в @gdrive не открываются. "
                           "Откройте [Google Drive](/p/gdrive/main) и войдите ещё раз.", priority=70, tags=["google-drive"])
    except Exception:  # noqa: BLE001 — сигнал не обязателен: страница всё равно покажет, что вход истёк
        pass


gdrive = GDrive(Store(plugin.data_dir), http, lambda: app_from(plugin.settings, plugin.secrets), expired_signal)
files = GDriveFiles(gdrive)
plugin.provide_files(files)


def app_info() -> dict:
    return {"configured": gdrive.app() is not None}


def run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FsError as exc:
        raise HTTPException(exc.status if exc.status < 500 else 502, str(exc)) from exc


def command(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (UserError, FsError) as exc:
        raise CommandError(str(exc)) from exc


def mount_path(value: str) -> str:
    """«@gdrive/Мой диск/отчёт.pdf» или «Мой диск/отчёт.pdf» → путь в хранилище."""
    value = (value or "").strip().strip("/")
    return value[len("@gdrive/"):] if value.startswith("@gdrive/") else value


# --- страница -------------------------------------------------------------------------------------------

@app.get("/state")
def state() -> dict:
    return {"app": app_info(), "accounts": gdrive.accounts(space=True), "mounts": gdrive.mounts(),
            "login": gdrive.login_state()}


@app.get("/login")
def login_status() -> dict | None:
    return gdrive.login_state()


@app.post("/login")
def login(body: dict = Body(default={})) -> dict:
    return run(gdrive.start_login, (body or {}).get("login_hint"))


@app.post("/login/finish")
def login_finish(body: dict = Body(...)) -> dict:
    return run(gdrive.finish_login, body.get("url") or "")


@app.delete("/login")
def cancel_login() -> dict:
    gdrive.cancel_login()
    return {"login": None}


@app.delete("/accounts/{uid}")
def logout(uid: str) -> dict:
    return run(gdrive.logout, uid)


@app.get("/roots")
def roots(account: str = Query(...)) -> dict:
    return run(gdrive.roots, account)


@app.get("/browse")
def browse(account: str = Query(...), root: str = Query("my"), folder: str | None = Query(None),
           drive: str | None = Query(None)) -> dict:
    return run(gdrive.browse, account, root, folder, drive)


@app.get("/link")
def link_preview(url: str = Query(...), account: str = Query("")) -> dict:
    return run(gdrive.preview_link, url, account)


@app.post("/mounts")
def add_mount(body: dict = Body(...)) -> dict:
    if body.get("kind") == "link":
        return run(gdrive.add_link, body.get("url") or "", body.get("account") or "", body.get("name") or None)
    return run(gdrive.add_root, body.get("account") or "", body.get("root") or "my", body.get("drive_id") or None,
               body.get("folder_id") or None, body.get("folder_path") or None, body.get("name") or None)


@app.patch("/mounts/{name}")
def rename_mount(name: str, body: dict = Body(...)) -> dict:
    return run(gdrive.rename, name, body.get("name") or "")


@app.delete("/mounts/{name}")
def remove_mount(name: str) -> dict:
    return run(gdrive.unmount, name)


# --- редактор Google в «Файлах» (обработчик со scope own) -------------------------------------------------

EDITOR_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
html, body {{ margin: 0; height: 100%; font: 13px system-ui, -apple-system, "Segoe UI", sans-serif; color: #212529; }}
body {{ display: flex; flex-direction: column; }}
.bar {{ display: flex; gap: 10px; align-items: center; padding: 6px 10px; border-bottom: 1px solid #dee2e6; background: #f8f9fa; flex-wrap: wrap; }}
.bar .grow {{ flex-grow: 1; }}
.muted {{ color: #6c757d; }}
a.btn {{ border: 1px solid #ced4da; border-radius: 6px; padding: 3px 10px; color: #212529; text-decoration: none; background: #fff; }}
iframe {{ flex-grow: 1; border: 0; width: 100%; }}
.error {{ padding: 16px; color: #842029; }}
</style></head><body>
{body}
<script>
const status = {status_js};
const message = {message_js};
parent.postMessage({{ type: "pulse:file-status", status, message }}, location.origin);
</script>
</body></html>"""


def editor_html(name: str, body: str, status: str = "ready", message: str = "") -> HTMLResponse:
    def js(value: str) -> str:
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    return HTMLResponse(EDITOR_PAGE.format(name=html.escape(name), body=body, status_js=js(status), message_js=js(message)))


@app.get("/editor")
def editor(ticket: str = Query(...), mode: str = Query("view")) -> HTMLResponse:
    try:
        meta = plugin.file_ticket(ticket).meta()
        target = gdrive.editor(mount_path(meta["path"]))
    except (PulseApiError, FsError, UserError) as exc:
        text = f"Не удалось открыть в Google: {exc}"
        return editor_html("Google Drive", f'<div class="error">{html.escape(text)}</div>', "error", text)
    note = ("Документ Google: правки сохраняет Google." if target["native"]
            else "Правки сохраняет Google прямо в этот файл на Диске.")
    body = (f'<div class="bar"><b>{html.escape(target["title"])}</b>'
            f'<span class="muted">{html.escape(target["account"] or "")} · {note} Не грузится или просит войти — '
            'браузер закрыл Google доступ из встроенного окна: откройте в Google.</span><span class="grow"></span>'
            f'<a class="btn" href="{html.escape(target["open_url"])}" target="_blank" rel="noopener">Открыть в Google ↗</a></div>'
            f'<iframe src="{html.escape(target["url"])}" title="{html.escape(target["name"])}" '
            'allow="clipboard-read; clipboard-write; fullscreen"></iframe>')
    return editor_html(target["name"], body)


# --- проверка подключения --------------------------------------------------------------------------------

def connection_check() -> dict:
    checks = []

    def add(title: str, ok: bool, detail: str | None = None) -> None:
        checks.append({"title": title, "ok": ok, "detail": detail})

    configured = gdrive.app() is not None
    add("Приложение Google Cloud", configured,
        "ClientID и Client secret заданы" if configured else "не заданы ClientID и Client secret — войти нельзя")
    try:
        proxy = net.proxy_of(plugin.settings, plugin.secrets)
        if proxy and proxy.startswith("socks"):
            import socksio  # noqa: F401 — httpx нужен пакет socksio для socks5
        if proxy:
            add("Прокси", True, f"{net.proxy_label(proxy)} — " + ("все запросы к Google" if plugin.setting("proxy_all")
                                                                 else "только вход"))
    except net.ProxyError as exc:
        add("Прокси", False, str(exc))
    except ImportError:
        add("Прокси", False, "для socks5 нужен пакет socksio — переустановите плагин или используйте http-прокси")
    hint = "" if plugin.secret("proxy") else " Если Google с сервера недоступен, задайте прокси в настройках плагина."
    ok, detail = net.reach(gdrive.http, TOKEN)
    add("Вход: oauth2.googleapis.com", ok, detail + ("" if ok else "." + hint))
    ok, detail = net.reach(gdrive.http, f"{API}/about")
    add("Диск: www.googleapis.com", ok, detail + ("" if ok else "." + hint))
    for account in gdrive.accounts():
        title = f"Аккаунт {account['login']}"
        if account["expired"]:
            add(title, False, "вход истёк — войдите снова на странице плагина")
            continue
        try:
            gdrive.drive(account["uid"]).about()
            add(title, True, "вход действует, Диск отвечает")
        except (AuthExpired, FsError) as exc:
            add(title, False, str(exc))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@app.get("/check")
def check_page() -> dict:
    return connection_check()


# --- команды агентов --------------------------------------------------------------------------------------

@plugin.command("accounts", "Аккаунты Google: почта, место на Диске, не истёк ли вход")
def accounts_command(args: dict) -> dict:
    return {"app": app_info(), "accounts": gdrive.accounts(space=True)}


@plugin.command("login", "Начать вход в аккаунт Google: ссылка для владельца")
def login_command(args: dict) -> dict:
    state_ = command(gdrive.start_login, args.get("email") or None)
    return {**state_, "tell_owner": "Откройте [Google Drive](/p/gdrive/main) → «Войти в аккаунт Google»: войдите, "
                                    "разрешите доступ к Диску и вставьте на странице адрес, на который перекинет браузер "
                                    "(страница не откроется — так задумано). Или пришлите этот адрес в чат."}


@plugin.command("login finish", "Завершить вход: адрес страницы после входа (http://127.0.0.1:1/?…), его прислал владелец")
def login_finish_command(args: dict) -> dict:
    if not args.get("url"):
        raise CommandError("нужен --url — адрес страницы после входа, целиком")
    return command(gdrive.finish_login, args["url"])


@plugin.command("login status", "Чем закончился вход: pending, done, expired")
def login_status_command(args: dict) -> dict:
    return gdrive.login_state() or {"status": "none"}


@plugin.command("logout", "Выйти из аккаунта и убрать его подключения")
def logout_command(args: dict) -> dict:
    return command(gdrive.logout, args.get("account") or "")


@plugin.command("drives", "Что можно подключить: «Мой диск», «Доступные мне» и общие диски аккаунта")
def drives_command(args: dict) -> dict:
    return command(gdrive.roots, args.get("account") or "")


@plugin.command("browse", "Папки внутри корня или папки — чтобы выбрать, что подключить")
def browse_command(args: dict) -> dict:
    return command(gdrive.browse, args.get("account") or "", args.get("root") or "my", args.get("folder") or None,
                   args.get("drive") or None)


@plugin.command("mounts", "Подключения: папки в @gdrive и откуда они")
def mounts_command(args: dict) -> dict:
    return {"mounts": gdrive.mounts()}


@plugin.command("mount", "Подключить «Мой диск», «Доступные мне», общий диск или папку в них")
def mount_command(args: dict) -> dict:
    return command(gdrive.add_root, args.get("account") or "", args.get("root") or "my", args.get("drive") or None,
                   args.get("folder") or None, None, args.get("name") or None)


@plugin.command("mount link", "Подключить файл или папку по ссылке Google Drive (от имени аккаунта)")
def mount_link_command(args: dict) -> dict:
    if not args.get("url"):
        raise CommandError("нужен --url https://drive.google.com/…")
    return command(gdrive.add_link, args["url"], args.get("account") or "", args.get("name") or None)


@plugin.command("rename", "Переименовать подключение")
def rename_command(args: dict) -> dict:
    if not args.get("name") or not args.get("to"):
        raise CommandError("нужны --name и --to")
    return command(gdrive.rename, args["name"], args["to"])


@plugin.command("unmount", "Отключить подключение (файлы на Диске не трогаются)")
def unmount_command(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    return command(gdrive.unmount, args["name"])


@plugin.command("check", "Проверить подключение: приложение, прокси, доступ ко входу и к Диску, аккаунты")
def check_command(args: dict) -> dict:
    return connection_check()


@plugin.command("share", "Открыть доступ по ссылке (чтение) к файлу или папке из @gdrive и вернуть ссылку")
def share_command(args: dict) -> dict:
    path = mount_path(args.get("path") or "")
    if "/" not in path:
        raise CommandError("нужен --path файла или папки внутри подключения: @gdrive/<подключение>/…")
    mount, _, item, _ = command(gdrive.walk, path)
    if item is None:
        raise CommandError(f"не найдено: @gdrive/{path}")
    return {"path": f"@gdrive/{path}", "url": command(gdrive.drive(mount["account"]).share_link, item["id"])}
