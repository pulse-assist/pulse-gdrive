# Разработка плагина «Google Drive» — инструкция для агента

Плагин уже сделан (см. README.md): `backend/gdrive_plugin/` — `oauth.py` (вход «Desktop app» с PKCE и вставкой
адреса, продление), `drive.py` (Drive API v3: файлы, экспорт документов Google, загрузка, общие диски), `service.py`
(аккаунты, подключения, перевод путей @gdrive в id Drive с кэшем, хранилище `GDriveFiles`, адрес редактора Google),
`net.py` (прокси, проверка сети), `store.py` (state.json), `app.py` (маршруты страницы, встроенная страница
`/editor` для «Файлов», команды агентов); страница — `ui/`; тесты — `backend/tests` (Google подменён).

Правила этого плагина:
- Пароль Google плагин не принимает — только вход в браузере владельца и токены. ClientID и секрет — только из
  настроек плагина, в код и репозиторий не попадают.
- Токены не попадают в журнал, ответы страницы и команд, сигналы; state.json — 0600.
- Хранилище — протокол `pulse_plugin.fs` (CONTRACT.md, «Поставщик файлов»). Drive — по id: одинаковые имена
  различаются суффиксом, документы Google — экспорт только для чтения, ярлыки — их цель.
- Обработчик «Файлов» (`files.handlers`, `scope: own`) — только для файлов @gdrive: редактор Google во фрейме.
- Удаление — в корзину (`trashed: true`), не навсегда.
- В песочнице страницы нет `alert`/`confirm`/`prompt` — подтверждения делай на самой странице.

Ниже — общая инструкция по плагинам «Пульса» (из шаблона pulse-plugin-template).

---

# Разработка плагина «Пульса» — инструкция для агента

Этот репозиторий — плагин для «Пульса», личной агентной платформы (FastAPI + MongoDB + React). Плагин
добавляет к Пульсу свои страницы в меню «Плагины ▾», свой серверный процесс, команды для агентов
(`pulse <id> …`), правила для агентов (скиллы), настройки и секреты. Ставит его агент Пульса по ссылке на
репозиторий или из каталога, а дальше идёт по `INSTALL.md`.

Доступ к коду Пульса не нужен. Всё о платформе, на что опирается плагин, — в **справочнике контракта**
[CONTRACT.md](https://github.com/pulse-assist/pulse-plugin-sdk/blob/main/CONTRACT.md) пакета
`pulse-plugin` (pulse-assist/pulse-plugin-sdk): манифест, права и какие API они открывают, окружение
процесса, мост страниц `window.pulse`, токены и классы дизайна, формат каталога и выпуск. Прочитай его перед
работой; если справочник и этот файл расходятся — прав справочник.

Общение с владельцем, тексты интерфейса, описания команд и настроек — **по-русски**.

## Как начать новый плагин из шаблона

```bash
pip install -r backend/requirements-dev.txt     # pulse-plugin (SDK и утилита) + pytest
pulse-plugin new weather --name "Погода"         # копия шаблона в pulse-weather/: id, название, пакет weather_plugin
```

Без утилиты — вручную: `id` (латиница в нижнем регистре, цифры, «-») и название в `pulse-plugin.json`, пакет
`backend/example_plugin` → `backend/<id>_plugin` (подчёркивания вместо «-») и `runtime.entry`. Дальше:

1. `permissions` — только нужные; меню, настройки, описание.
2. Замени пример кода, страницы, скилл, агента, `commands.json` своими.
3. Заполни `INSTALL.md` под свой плагин (что спросить у владельца, что настроить, как проверить).
4. `pulse-plugin check` и тесты зелёные, версия поднята, тег — см. «Релиз».

## Структура

```
pulse-plugin.json     манифест — единственный обязательный файл
backend/              серверная часть на Python (runtime.path), пакет <id>_plugin, тесты
  requirements.txt    свои зависимости сверх Пульса; только комментарии — отдельного venv не будет
ui/                   страницы (ui.dist): index.html + js/css; сборка необязательна
skills/*.md           правила для агентов Пульса (становятся скиллами библиотеки)
agents/*.md           готовые агенты (необязательно)
commands.json         команды для агентов — справка `pulse <id> --help`
INSTALL.md            инструкция развёртывания для агента-установщика
Dockerfile            тот же плагин контейнером (runtime.kind = docker)
```

## Манифест `pulse-plugin.json`

| Поле | Что это |
|---|---|
| `id` | `^[a-z][a-z0-9-]{1,39}$`; префикс команд (`pulse <id> …`), адресов (`/p/<id>/…`), базы |
| `name`, `description` | название и одна строка о том, что умеет (видят владелец и агенты) |
| `version` | `1.2.0`; новая версия = новый тег `v1.2.0` в репозитории |
| `pulse` | совместимость с контрактом плагинов: `">=1.0"` |
| `permissions` | что можно плагину (ниже); всё прочее Пульс отклоняет с 403 |
| `runtime` | серверная часть: `kind: "python"`, `entry: "<пакет>.app:app"` (ASGI), `path: "backend"`, `requirements`, `health: "/health"`. Нет блока — плагин без процесса (только страницы и скиллы) |
| `ui` | `dist` — папка со страницами; `build` — команда сборки при установке (`"npm ci && npm run build"`), если dist не лежит в репозитории |
| `menu` | пункты меню: `id`, `title` (коротко), `icon` (Bootstrap Icons, `bi-…`), `page` (`index.html#состояние`) |
| `settings` | настройки по схеме: `type` (string, number, integer, boolean), `title`, `description`, `enum`, `default`, `secret: true` — для токенов и паролей |
| `skills`, `agents`, `commands`, `install` | пути к файлам внутри плагина |
| `jobs` | фоновая работа: `[{name, cron, command, args, tz, description, timeout_sec}]` — команда плагина по расписанию |

Права (`permissions`):

| Право | Что даёт |
|---|---|
| `cards:read` | читать карточки, теги, ленту (`GET /api/cards…`, `/api/tags`, `/api/signals/feed`) |
| `cards:write` | создавать и менять карточки (включает чтение) |
| `signals:create` | заводить сигналы (`POST /api/cards` с `type: signal`) — без права менять остальное |
| `files` | читать свою папку `Plugins/<Название>/` через API; писать в неё — напрямую на диск (`plugin.files_dir`) |
| `jobs` | задачи шедулера (`/api/jobs…`) |
| `chat` | чаты (`/api/chats…`) |
| `notify` | уведомления владельцу (`POST /api/notify`) |
| `network` | ходить в интернет — заявление для владельца (банки, внешние API) |

Проси минимум: права видны владельцу в «Настройки → Плагины → Права».

## Серверная часть — пакет `pulse_plugin`

Пульс запускает `uvicorn <runtime.entry>` в папке `runtime.path` (она же в `PYTHONPATH`) и передаёт
окружение. Работай через `pulse_plugin.Plugin`:

```python
from pulse_plugin import CommandError, Plugin

plugin = Plugin()
app = plugin.app                                   # FastAPI; /health и POST /commands/{имя} уже есть

plugin.settings / plugin.setting("bank", "tinkoff")  # настройки (со значениями по умолчанию из манифеста)
plugin.secret("token")                             # секрет (расшифрован Пульсом)
plugin.api.get("/api/cards", type="task", limit=20)  # API Пульса с токеном плагина (только заявленные права)
plugin.signal("Превышен бюджет на еду", priority=80, tags=["финансы"])   # сигнал в ленту (signals:create)
plugin.db["operations"].insert_one({...})          # своя база MongoDB (pymongo)
plugin.data_dir                                    # служебные файлы плагина (не видны владельцу)
plugin.files_dir                                   # папка Plugins/<Название>/ в «Файлах» (видна владельцу и агентам)

@plugin.command("transactions list", "Операции за месяц")
def transactions(args: dict):                      # args — {"month": "2026-10"} из pulse finance transactions list --month 2026-10
    if "month" not in args:
        raise CommandError("нужен --month ГГГГ-ММ")   # агент увидит этот текст
    return {"items": [...]}                        # ответ — JSON

@app.get("/summary")                               # свой API для страниц: pulse.plugin("/summary")
def summary():
    return {...}
```

Окружение процесса: `PULSE_API_URL`, `PULSE_PLUGIN_TOKEN`, `PULSE_PLUGIN_ID`, `PULSE_PLUGIN_NAME`,
`PULSE_PLUGIN_VERSION`, `PULSE_PLUGIN_PORT`, `PULSE_PLUGIN_DATA`, `PULSE_PLUGIN_FILES`, `PULSE_PLUGIN_FOLDER`,
`PULSE_PLUGIN_MONGO_URL`, `PULSE_PLUGIN_MONGO_DB`, `PULSE_PLUGIN_SETTINGS` (JSON), `PULSE_PLUGIN_SECRETS` (JSON).
Секретов Пульса (его токена, ключей) у плагина нет и быть не должно.

Правила:
- Процесс должен подниматься быстро и отвечать на `/health` (до 30 с); тяжёлую работу — в фоне.
- Настройки меняются — Пульс перезапускает процесс: читай их при старте, кешировать между запусками не нужно.
- Ничего не пиши в репозиторий плагина во время работы: данные — в `plugin.db`, `plugin.data_dir`, `plugin.files_dir`.
- Секреты не логируй и не возвращай в ответах команд.
- Сеть — только туда, что заявлено в описании плагина; таймауты на все внешние запросы.

## Команды для агентов — `commands.json`

```json
[{ "name": "transactions list", "description": "Операции за месяц",
   "args": [{ "name": "month", "description": "ГГГГ-ММ", "required": true }] }]
```

- Имя — слова латиницей через пробел; агент вызывает `pulse <id> transactions list --month 2026-10`
  (флаг без значения — `true`). Обработчик — `@plugin.command("transactions list")` с тем же именем.
- Ответ — компактный JSON: агент пересказывает его владельцу. Списки — с ограничением (`limit`).
- `commands.json` и обработчики держи одинаковыми: справку агент читает из `commands.json`.

## Фоновая работа — `jobs`

```json
"jobs": [{ "name": "Выписка из банка", "cron": "0 * * * *", "command": "sync", "args": { "days": 1 } }]
```

Каждая задача — системная задача шедулера Пульса «<плагин> — <задача>» (папка `Jobs/Плагины/…`, журнал
запусков там же): по расписанию Пульс вызывает команду плагина с `args`. Команда должна быть в
`commands.json` и в коде, иначе установка не пройдёт. `cron` — в поясе владельца (свой — `tz`), таймаут —
`timeout_sec` (по умолчанию 600). Выключенный плагин — выключенные задачи. Нашли важное — заведи сигнал
(`plugin.signal(...)`): он попадёт в ленту и, по порогу, в Telegram.

## Запуск в Docker — `runtime.kind = "docker"`

Для плагина не на Python или с тяжёлыми системными зависимостями. Пульс при установке собирает образ из
`runtime.dockerfile` (по умолчанию `Dockerfile`), предварительно положив пакет `pulse_plugin` в контекст
сборки — `.pulse_plugin_sdk/` (смотри Dockerfile шаблона). Контейнер получает то же окружение, что процесс
(файлом, не аргументами), слушает `PULSE_PLUGIN_PORT`, данные — `/plugin/data`, папка в «Файлах» —
`/plugin/files`. Сервер должен иметь Docker; без него установка ответит понятной ошибкой.

## Страницы — `ui/`

Страница открывается во фрейме Пульса **без доступа к cookie** (sandbox): обычный `fetch` к API Пульса
не сработает. Всё — через мост `window.pulse` (Пульс подмешивает его в каждый `.html`):

```js
await pulse.plugin("/summary", { params: { month: "2026-10" } })   // свой сервер плагина
await pulse.plugin("/budget", { method: "POST", body: { food: 15000 } })
await pulse.api("/api/cards", { params: { type: "task", limit: 20 } })  // API Пульса в пределах прав
await pulse.command("transactions list", { month: "2026-10" })
await pulse.settings()                  // настройки плагина без секретов
pulse.openCard(id)                      // открыть карточку в окне Пульса
pulse.chat("Разбери мои траты")         // чат с агентом о плагине
pulse.setState("ledger?month=2026-10")  // своё состояние в адресе — ссылкой можно поделиться
pulse.state                             // состояние, с которым страницу открыли
```

Дизайн — как у Пульса (его дизайн-система: docs/design-system.md). Токены `--pulse-*` и классы уже на
странице: `pl-stack`, `pl-grid`, `pl-card`, `pl-section`, `pl-h`, `pl-kpi-label`, `pl-kpi-value`,
`pl-kpi-delta`, `pl-table`, `pl-list`, `pl-badge`, `pl-tag`, `pl-dot`, `pl-muted`, `pl-small`, `pl-num`,
`pl-key`, `pl-empty`, `pl-error`, `pl-ok`, `pl-important`, `pl-urgent`, `pl-type-task|signal|knowledge`.
Красный — только для срочного и ошибок; у каждой страницы три состояния: загрузка, пусто, ошибка.
Модули и `fetch` своих файлов работают (Пульс отдаёт их с CORS). Внешние CDN — только если без них никак.

## Скиллы и агенты

`skills/*.md` — правила для агентов Пульса. YAML-заголовок: `title`, `description`, `default: true|false`
(true — скилл крепится ко всем агентам). Текст — конкретные правила: когда и какой командой пользоваться,
что ответить владельцу, ссылки `[название](/cards?card=ID)`. Обновляется вместе с плагином, если владелец
текст не правил.

`agents/*.md` — готовый агент: заголовок `name`, `description` (по нему диспетчер решает, поручать ли ему
запрос), `model` (необязательно); текст — инструкции.

## INSTALL.md — инструкция развёртывания

Агент Пульса после `pulse plugins install` читает `INSTALL.md` и проходит его целиком. Пиши его как
пошаговый сценарий для агента: какие настройки обязательны и что спросить у владельца (по-русски, понятно:
«какой у вас банк»), команды настройки (`pulse plugins settings <id> set key=value`,
`pulse plugins secret <id> key --value …`), проверка (`pulse <id> …` с ожидаемым результатом), что
рассказать владельцу в конце и что делать, если проверка не прошла.

## Проверка и тесты

```bash
pip install -r backend/requirements-dev.txt
pulse-plugin check                              # манифест, пути, команды задач, страницы меню — как проверит Пульс
cd backend && python -m pytest                  # тесты без Пульса: pulse_plugin.testing.fake_api (tests/test_app.py)
PULSE_PLUGIN_SETTINGS='{"greeting":"Привет"}' uvicorn example_plugin.app:app --port 8711   # запустить локально
```

`pulse_plugin.testing.fake_api(plugin, monkeypatch)` подменяет API Пульса: `api.on("GET", "/api/cards", …)`
задаёт ответ, `api.signals` — заведённые сигналы, `api.calls` — все запросы; путь без ответа — ошибка, как у
Пульса на путь без прав.

**Проверка в живом Пульсе** — на своём (или демо-) Пульсе с токеном владельца:

```bash
pulse-plugin dev --pulse https://<ваш Пульс> --token <токен>      # собрать и поставить поверх, журнал установки
pulse-plugin dev --pulse … --token … --watch                     # переставлять при каждой правке
pulse-plugin dev … --grant notify                                # версия просит новое право — вы владелец
```

Затем в Пульсе: `pulse plugins logs <id>`, `pulse <id> --help`, страница в меню «Плагины ▾».

## Релиз и каталог

1. Подними `version` в `pulse-plugin.json` (semver: исправление — 1.2.1, новое — 1.3.0, несовместимое — 2.0.0;
   предрелиз — `1.3.0-beta.1`, он уходит в канал beta).
2. `pulse-plugin check` и тесты зелёные, `INSTALL.md` и `commands.json` соответствуют коду.
3. Коммит и тег `v<версия>`: `git tag v1.3.0 && git push --tags`. Workflow `release.yml` соберёт
   `<id>-<версия>.tar.gz` (`pulse-plugin pack`: воспроизводимая сборка, sha256) и выложит релиз с ней и с
   записью версии для каталога `<id>-<версия>.json`.
4. В каталог: `pulse-plugin publish <клон каталога> --changes "что нового — для владельца"` добавит версию в
   `plugins/<id>.json`; коммит и MR в каталог (публичный — pulse-assist/pulse-plugins) или в свой репозиторий
   плагинов. После слияния Пульсы увидят «доступна 1.3.0».
5. **Новые права.** Если версия просит право, которого не было, Пульс не поставит её без «да» владельца —
   объясни право в README и в `changes`.
6. **Данные.** Перед обновлением Пульс делает снимок данных плагина и при сбое запуска откатывает и версию, и
   данные. Свои миграции делай при старте по `PULSE_PLUGIN_PREVIOUS_VERSION` и делай их повторяемыми.

## Чего не делать

- Не трогать Пульс: его код, базу (кроме своей), чужие папки. Всё — через API в пределах прав.
- Не просить права «на всякий случай».
- Не класть секреты в репозиторий, в `settings` (только `secret: true`) и в ответы команд.
- Не менять `id` у опубликованного плагина: для Пульса это другой плагин.
- Не править опубликованную сборку: новая сборка — новая версия (каталог хранит sha256).
