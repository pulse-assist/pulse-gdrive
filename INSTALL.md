# Установка плагина «Google Drive»

Инструкция для агента Пульса после `pulse plugins install …`. Пройди её целиком.

## 1. Проверь, что плагин запустился

```bash
pulse plugins list            # у gdrive — status: running
pulse gdrive accounts         # app.configured — задано ли приложение Google Cloud; accounts: [] до входа
```

Если status: error — `pulse plugins logs gdrive --lines 80`, расскажи владельцу, что в журнале, и остановись.
Нужен Пульс с контрактом плагинов 1.5 (хранилища @<плагин>, обработчики своего хранилища, проверка подключения).

## 2. Приложение Google Cloud

Вход работает только с приложением из настроек плагина — значений по умолчанию нет. Если `app.configured: false`,
спроси владельца, есть ли у него ClientID и Client secret: для доверенных установок их выдаёт администратор Пульса.
Есть — пусть задаст их (пункт 4 ниже). Нет — пусть запросит у администратора или зарегистрирует своё приложение:

1. console.cloud.google.com → новый проект → «APIs & Services» → включить **Google Drive API**;
2. «OAuth consent screen»: тип External, область `.../auth/drive`; **Publishing status — In production** (в статусе
   Testing Google отзывает вход через 7 дней). Проверку Google проходить не нужно: при входе будет предупреждение
   «Приложение не проверено» — «Дополнительно» → «Перейти»;
3. «Credentials» → «Create credentials» → OAuth client ID → тип **Desktop app**;
4. ClientID и Client secret — в Настройки → Плагины → Google Drive или командами:

```bash
pulse plugins settings gdrive set client_id=<ClientID>
printf '%s' "$CLIENT_SECRET" | pulse plugins secret gdrive client_secret    # через stdin, не --value; в ответах не повторяй
```

## 3. Проверь доступ к Google — обязательно

```bash
pulse gdrive check            # по шагам: приложение, прокси, «Вход: oauth2.googleapis.com», «Диск: www.googleapis.com», аккаунты
```

То же — кнопка «Проверить подключение» в настройках плагина. Не проходят «Вход» или «Диск» — Google с сервера Пульса
недоступен (например, сервер в России): нужен прокси за пределами блокировки. Спроси владельца, есть ли у него
прокси или сервер (описание может лежать в его карточках и файлах: `pulse cards list --q прокси`,
`pulse files find прокси`). Адрес прокси — секрет: `printf '%s' "$PROXY_URL" | pulse plugins secret gdrive proxy`
(`http://логин:пароль@хост:порт` или `socks5://…`); не отвечает и Диск — `pulse plugins settings gdrive set
proxy_all=true`. Повтори `pulse gdrive check`, пока все шаги не станут ok (кроме аккаунтов, если их ещё нет).

## 4. Закрепи страницу

```bash
pulse plugins pin gdrive main
```

## 5. Вход — делает владелец сам

Пароль Google вводит только владелец, на сайте Google. Отправь его на страницу [Google Drive](/p/gdrive/main) →
«Войти в аккаунт Google»: войти, разрешить доступ к Диску, скопировать адрес страницы, на которую перекинет браузер
(она не откроется — так задумано), и вставить его на странице плагина. Если владелец пришлёт этот адрес в чат —
`pulse gdrive login finish --url "<адрес>"` (сначала `pulse gdrive login`, если вход ещё не начат). Каждый вход —
ещё один аккаунт; «Мой диск» подключается сам.

## 6. Подключения (если владелец просит)

```bash
pulse gdrive drives --account <почта>                    # «Мой диск», «Доступные мне», общие диски
pulse gdrive mount --account <почта> --root shared       # «Доступные мне»
pulse gdrive mount --account <почта> --root drive --drive "Маркетинг"
pulse gdrive browse --account <почта> --root my          # папки — подключить одну: mount --folder <id>
pulse gdrive mount link --url https://drive.google.com/… # файл или папка по ссылке
pulse gdrive mounts
```

## 7. Проверь и расскажи владельцу

```bash
pulse gdrive check
pulse files ls @gdrive
```

Что установлен «Google Drive» (версия), страница закреплена, Диск виден в «Файлах» папкой @gdrive и доступен агентам;
документы Google открываются в «Файлах» редактором Google (правки сохраняет Google; если браузер не пускает Google во
встроенное окно — кнопка «Открыть в Google»), в PulseFiles+ — через «⋯ → Открыть в…», только чтение. Вход
продлевается сам; истечёт — придёт сигнал «Войдите в Google снова».
