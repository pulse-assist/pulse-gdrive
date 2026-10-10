// Страница «Google Drive»: аккаунты (вход в браузере и вставка адреса) и подключения — папки @gdrive/… в «Файлах».
// Всё — через мост Пульса: pulse.plugin(...) к серверу плагина, pulse.navigate(...) — в разделы Пульса.
// В песочнице нет alert/confirm — подтверждения прямо на странице.

const app = document.getElementById("app");
const dialog = document.getElementById("dialog");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gb = (bytes) => {
  if (bytes == null) return "";
  const g = bytes / 1024 ** 3;
  return g >= 100 ? `${Math.round(g).toLocaleString("ru")} ГБ` : `${g.toFixed(1).replace(".", ",")} ГБ`;
};
const filesLink = (path) => `/files?path=${encodeURIComponent(path)}`;
const ROOTS = { my: "Мой диск", shared: "Доступные мне" };

let data = null;
let error = "";
let openMenu = null;      // имя подключения с открытым меню «⋯»
let renaming = null;      // имя подключения, которое переименовывают
let confirm = null;       // {kind: "logout"|"unmount", id}
let busy = "";            // что сейчас делается: запросы к Google бывают долгими — показываем, кнопки выключаем
let loginError = "";

async function load() {
  try {
    data = await pulse.plugin("/state");
    error = "";
  } catch (e) {
    if (!data) { app.innerHTML = `<div class="pl-error">Плагин не отвечает: ${esc(e.message)}</div>`; return; }
    error = e.message;
  }
  render();
}

async function act(fn, label) {
  busy = label || "Выполняю…";
  openMenu = null;
  render();
  try {
    await fn();
    error = "";
  } catch (e) {
    error = e.message;
  }
  openMenu = renaming = confirm = null;
  await load();
  busy = "";
  render();
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// --- отрисовка ----------------------------------------------------------------------------------------------

function render() {
  const { accounts, mounts, app: oauth } = data;
  app.classList.toggle("busy", !!busy);
  app.innerHTML = `
    ${busy ? `<div class="working" role="status"><span class="spinner"></span>${esc(busy)}</div>` : ""}
    <div class="status">
      <span>${accounts.length ? `${accounts.length} ${plural(accounts.length, "аккаунт", "аккаунта", "аккаунтов")}` : "аккаунтов нет"}
        · ${mounts.length} ${plural(mounts.length, "подключение", "подключения", "подключений")}</span>
      <span class="grow"></span>
      <button class="btn" data-go="${esc(filesLink("@gdrive"))}">Открыть @gdrive в «Файлах»</button>
    </div>
    ${!oauth.configured ? `<div class="pl-error">Вход в аккаунты не настроен: нужны ClientID и Client secret приложения
      Google Cloud — их выдаёт администратор вашей установки Пульса. Укажите их в
      <button class="btn-link" data-go="/settings/plugins/gdrive">настройках плагина</button>.</div>` : ""}
    ${error ? `<div class="pl-error">${esc(error)}</div>` : ""}
    <div class="cols">
      <section class="box">
        <div class="box-head"><h2>Аккаунты</h2><span class="hint">вход через браузер, плагин продлевает его сам</span></div>
        ${accounts.map(renderAccount).join("")}
        ${renderLogin()}
      </section>
      <section class="box">
        <div class="box-head"><h2>Подключения</h2><span class="hint">папки в «Файлах» → @gdrive/…, их же видят агенты</span>
          <span class="grow"></span>
          <button class="btn" data-add="link" ${accounts.length ? "" : "disabled title=\"Сначала войдите в аккаунт\""}>+ Ссылка</button>
          <button class="btn btn-primary" data-add="account" ${accounts.length ? "" : "disabled title=\"Сначала войдите в аккаунт\""}>+ Подключить</button></div>
        ${mounts.length ? `<table class="mounts"><thead><tr><th>Папка в «Файлах»</th><th>Откуда</th><th>Доступ</th><th></th></tr></thead>
          <tbody>${mounts.map(renderMount).join("")}</tbody></table>`
          : `<div class="pl-empty">Подключений нет. Войдите в аккаунт — «Мой диск» подключится сам.</div>`}
        <div class="box-foot hint">Документы, таблицы и презентации Google видны как .docx, .xlsx и .pptx и открываются в
          «Файлах» редактором Google (правки сохраняет Google); в PulseFiles+ — «⋯ → Открыть в…», только чтение.
          Одинаковые имена в одной папке различаются суффиксом «(2)».</div>
      </section>
    </div>`;
}

function renderAccount(a) {
  const share = a.total_space ? Math.min(100, Math.round((a.used_space / a.total_space) * 100)) : 0;
  const asking = confirm?.kind === "logout" && confirm.id === a.uid;
  return `<div class="account">
    <span class="avatar">${esc((a.name || a.login || "?").slice(0, 1).toUpperCase())}</span>
    <div class="account-body">
      <div class="account-name">${esc(a.name || a.login)} <span>· ${esc(a.login)}</span>
        ${a.expired ? `<span class="badge badge-warn">вход истёк — войдите снова</span>` : ""}</div>
      ${a.total_space ? `<div class="space"><div class="space-bar"><div style="width:${share}%"></div></div>
        <span>${gb(a.used_space)} из ${gb(a.total_space)}</span></div>` : a.error ? `<div class="hint">${esc(a.error)}</div>` : ""}
    </div>
    ${asking
      ? `<span class="inline-form"><span class="hint">Выйти и убрать подключения?</span>
          <button class="btn btn-sm btn-danger" data-logout-yes="${esc(a.uid)}">Выйти</button>
          <button class="btn btn-sm" data-cancel>Нет</button></span>`
      : `<button class="btn btn-sm" data-logout="${esc(a.uid)}">Выйти</button>`}
  </div>`;
}

function renderLogin() {
  const login = data.login;
  if (!data.app.configured) return "";
  if (busy.startsWith("Готовлю вход")) {
    return `<div class="login"><h3>Вход в аккаунт Google</h3><div class="working"><span class="spinner"></span>Готовлю вход…</div></div>`;
  }
  if (login?.status === "pending") {
    return `<div class="login">
      <h3>Вход в ${data.accounts.length ? "ещё один " : ""}аккаунт Google</h3>
      <div class="code-row"><span class="step-n">1</span>
        <div><a class="btn btn-primary" style="display:inline-flex;align-items:center;text-decoration:none" href="${esc(login.url)}"
                target="_blank" rel="noopener">Открыть вход Google ↗</a>
          <div class="hint">Войдите и разрешите доступ к Диску. «Приложение не проверено» — это ваше приложение:
            «Дополнительно» → «Перейти».</div></div></div>
      <div class="code-row"><span class="step-n">2</span>
        <form class="paste" id="paste" style="flex-grow:1">
          <div class="hint" style="margin-bottom:6px">Браузер откроет страницу, которая не загрузится, — так задумано.
            Скопируйте её адрес целиком и вставьте сюда:</div>
          <div class="inline-form"><input id="callback" class="mono" style="flex-grow:1;padding:7px 10px" placeholder="http://127.0.0.1:1/?state=…&code=…" autocomplete="off">
            <button class="btn" type="submit">Войти</button></div>
          ${loginError ? `<div class="error">${esc(loginError)}</div>` : ""}
          <div class="hint">Пароль вы вводите на сайте Google — плагин его не видит.</div>
        </form></div>
      <div style="text-align:right"><button class="btn-link" data-login-cancel>Отмена</button></div>
    </div>`;
  }
  const done = login?.status === "done" ? `<div class="hint">Вход выполнен: ${esc(login.account)}.</div>` : "";
  const failed = login?.status === "expired" ? `<div class="error">${esc(login.error)}</div>` : "";
  return `<div class="login-start">${failed}${done}
    <button class="btn ${data.accounts.length ? "" : "btn-primary"}" data-login>+ Войти в аккаунт Google</button></div>`;
}

function renderMount(m) {
  const where = m.kind === "link"
    ? `ссылка${m.target_type === "file" ? " на файл" : ""}<div class="sub mono">${esc((m.url || "").replace(/^https?:\/\//, "").slice(0, 60))}</div>`
    : `${m.root === "drive" ? `общий диск «${esc(m.drive_name)}»` : esc(ROOTS[m.root] || m.root)}${m.folder_path ? ` → «${esc(m.folder_path)}»` : ""}`;
  const who = m.account ? ` · ${esc(m.account.login)}${m.account.expired ? ` <span class="badge badge-warn">вход истёк</span>` : ""}`
    : ` <span class="badge badge-warn">аккаунт отключён</span>`;
  const badge = m.kind === "link" ? `<span class="badge">ссылка</span>` : m.root === "drive" ? `<span class="badge">общий диск</span>`
    : m.root === "shared" ? `<span class="badge">чужие файлы</span>` : "";
  const name = renaming === m.name
    ? `<form class="inline-form" data-rename-form="${esc(m.name)}"><input name="to" value="${esc(m.name)}" aria-label="Новое имя">
        <button class="btn btn-sm btn-primary">Сохранить</button><button type="button" class="btn btn-sm" data-cancel>Отмена</button></form>`
    : `<b>${esc(m.name)}</b> ${badge}<div class="sub mono">${esc(m.files_path)}</div>`;
  const asking = confirm?.kind === "unmount" && confirm.id === m.name;
  const actions = asking
    ? `<span class="inline-form"><span class="hint">Отключить?</span>
        <button class="btn btn-sm btn-danger" data-unmount-yes="${esc(m.name)}">Отключить</button><button class="btn btn-sm" data-cancel>Нет</button></span>`
    : `<button class="btn-link" data-go="${esc(filesLink(m.files_path))}">Открыть</button>
       <span class="menu"><button class="btn btn-sm" data-menu="${esc(m.name)}" aria-label="Действия: ${esc(m.name)}">⋯</button>
       ${openMenu === m.name ? `<div class="menu-list">
          <button data-rename="${esc(m.name)}">Переименовать</button>
          <button class="btn-danger" data-unmount="${esc(m.name)}">Отключить</button></div>` : ""}</span>`;
  const access = m.readonly ? `<span class="badge badge-ro">только чтение</span>`
    : m.root === "shared" ? "по правам владельца файла" : "чтение и запись";
  return `<tr><td class="name">${name}</td><td>${where}${who}</td><td>${access}</td><td class="actions">${actions}</td></tr>`;
}

// --- действия ------------------------------------------------------------------------------------------------

app.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a");
  if (!t) { if (openMenu) { openMenu = null; render(); } return; }
  const d = t.dataset;
  if (d.go) pulse.navigate(d.go);
  else if (busy) return;
  else if (d.login !== undefined) { loginError = ""; await act(() => pulse.plugin("/login", { method: "POST", body: {} }), "Готовлю вход…"); }
  else if (d.loginCancel !== undefined) await act(() => pulse.plugin("/login", { method: "DELETE" }), "Отменяю вход…");
  else if (d.logout) { confirm = { kind: "logout", id: d.logout }; render(); }
  else if (d.logoutYes) await act(() => pulse.plugin(`/accounts/${encodeURIComponent(d.logoutYes)}`, { method: "DELETE" }), "Выхожу из аккаунта…");
  else if (d.unmount) { confirm = { kind: "unmount", id: d.unmount }; openMenu = null; render(); }
  else if (d.unmountYes) await act(() => pulse.plugin(`/mounts/${encodeURIComponent(d.unmountYes)}`, { method: "DELETE" }), "Отключаю…");
  else if (d.menu) { openMenu = openMenu === d.menu ? null : d.menu; render(); }
  else if (d.rename) { renaming = d.rename; openMenu = null; render(); app.querySelector("[data-rename-form] input")?.select(); }
  else if (d.cancel !== undefined) { confirm = renaming = null; render(); }
  else if (d.add) openDialog(d.add);
});

app.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (e.target.id === "paste") {
    const url = document.getElementById("callback").value.trim();
    if (!url) return;
    busy = "Завершаю вход…";
    render();
    try {
      await pulse.plugin("/login/finish", { method: "POST", body: { url } });
      loginError = "";
    } catch (err) {
      loginError = err.message;
    }
    busy = "";
    await load();
    return;
  }
  const form = e.target.closest("[data-rename-form]");
  if (!form) return;
  const to = form.elements.to.value.trim();
  await act(() => pulse.plugin(`/mounts/${encodeURIComponent(form.dataset.renameForm)}`, { method: "PATCH", body: { name: to } }),
            "Переименовываю…");
});

// --- окно «Подключить» ----------------------------------------------------------------------------------------

const form = { tab: "account", account: "", roots: null, target: null, trail: [], folders: null, name: "", url: "",
               preview: null, error: "", busy: false, checking: false };

function openDialog(tab) {
  Object.assign(form, { tab, account: data.accounts[0]?.uid || "", roots: null, target: null, trail: [], folders: null,
                        name: "", url: "", preview: null, error: "", busy: false, checking: false });
  renderDialog();
  if (tab === "account") loadRoots();
}

function closeDialog() { dialog.innerHTML = ""; }

async function loadRoots() {
  form.roots = null;
  form.target = null;
  form.trail = [];
  form.folders = null;
  renderDialog();
  try {
    form.roots = await pulse.plugin("/roots", { params: { account: form.account } });
  } catch (e) {
    form.roots = { roots: [], drives: [] };
    form.error = e.message;
  }
  renderDialog();
}

// target: {root, drive_id?, drive_name?, title}; trail: [{id, name}] — папки внутри корня
async function browseInto(target, trail) {
  form.target = target;
  form.trail = trail;
  form.folders = null;
  form.name = "";
  renderDialog();
  const params = { account: form.account, root: target.root };
  if (target.drive_id) params.drive = target.drive_id;
  if (trail.length) params.folder = trail[trail.length - 1].id;
  try {
    form.folders = (await pulse.plugin("/browse", { params })).folders;
  } catch (e) {
    form.folders = [];
    form.error = e.message;
  }
  renderDialog();
}

async function checkUrl() {
  form.preview = null;
  form.error = "";
  if (!form.url.trim()) { renderDialog(); return; }
  form.checking = true;
  renderDialog();
  try {
    form.preview = await pulse.plugin("/link", { params: { url: form.url.trim(), account: form.account } });
  } catch (e) {
    form.error = e.message;
  }
  form.checking = false;
  renderDialog();
}

function selectedTitle() {
  if (form.tab === "link") return form.preview?.name || "";
  if (!form.target) return "";
  return form.trail.length ? form.trail[form.trail.length - 1].name : form.target.title;
}

function renderAccountTab() {
  const accountSelect = `<label class="field">Аккаунт<select id="f-account">${data.accounts.map((a) =>
    `<option value="${esc(a.uid)}" ${a.uid === form.account ? "selected" : ""}>${esc(a.name || a.login)} · ${esc(a.login)}</option>`).join("")}</select></label>`;
  if (!form.target) {
    const roots = form.roots;
    return `${accountSelect}
      <div class="field">Что подключить
        <div class="browser">
          ${roots === null ? `<div class="pl-empty">Загружаю…</div>` : `
            <div class="crumbs">Корни аккаунта</div>
            ${roots.roots.map((r) => `<button class="folder" data-root="${esc(r.root)}">${esc(r.title)}</button>`).join("")}
            <div class="crumbs">Общие диски</div>
            ${roots.drives.length ? roots.drives.map((d) => `<button class="folder" data-drive="${esc(d.id)}" data-drive-name="${esc(d.name)}">${esc(d.name)}</button>`).join("")
              : `<div class="pl-empty">Общих дисков нет</div>`}`}
        </div>
        <span class="hint">Выберите корень — подключится целиком; внутри можно выбрать папку.</span></div>`;
  }
  const crumbs = [{ label: form.target.title, trail: [] },
                  ...form.trail.map((f, i) => ({ label: f.name, trail: form.trail.slice(0, i + 1) }))];
  return `${accountSelect}
    <div class="field">Папка
      <div class="browser">
        <div class="crumbs"><button data-back>Корни</button> / ${crumbs.map((c, i) =>
          `<button data-crumb="${i}">${esc(c.label)}</button>`).join(" / ")}</div>
        <div class="folder-list">${form.folders === null ? `<div class="pl-empty">Загружаю…</div>`
          : form.folders.length ? form.folders.map((f) => `<button class="folder" data-folder="${esc(f.id)}" data-folder-name="${esc(f.name)}">${esc(f.name)}</button>`).join("")
          : `<div class="pl-empty">Вложенных папок нет</div>`}</div>
      </div>
      <span class="hint">Будет подключено: <b>${esc(crumbs.map((c) => c.label).join(" → "))}</b></span></div>`;
}

function renderLinkTab() {
  return `
    <label class="field">Ссылка на файл или папку Google Drive<input id="f-url" class="mono" value="${esc(form.url)}" placeholder="https://drive.google.com/…" autocomplete="off"></label>
    <label class="field">Открывать от имени<select id="f-account">${data.accounts.map((a) =>
      `<option value="${esc(a.uid)}" ${a.uid === form.account ? "selected" : ""}>${esc(a.name || a.login)} · ${esc(a.login)}</option>`).join("")}</select></label>
    ${form.checking ? `<div class="working"><span class="spinner"></span>Проверяю ссылку…</div>` : ""}
    ${form.preview ? `<div class="preview"><b>${esc(form.preview.name)}</b><span class="hint">${form.preview.type === "dir" ? "папка" : "файл"}
      ${form.preview.owner ? ` · владелец: ${esc(form.preview.owner)}` : ""} · ${form.preview.can_edit ? "можно править" : "только чтение"}</span></div>` : ""}
    <span class="hint">Ссылка «доступно всем, у кого есть ссылка» или файл, которым поделились с вами, открывается от имени
      выбранного аккаунта. Если по ссылке можно править — подключение с записью, иначе только чтение.</span>`;
}

function renderDialog() {
  const ready = form.tab === "link" ? !!form.preview : !!form.target;
  const defaultName = selectedTitle();
  dialog.innerHTML = `<div class="overlay" data-close-overlay><div class="modal" role="dialog" aria-label="Подключить">
    <div class="modal-head"><h2>Подключить</h2><button class="btn-link" data-close aria-label="Закрыть">✕</button></div>
    <div class="tabs"><button class="${form.tab === "account" ? "on" : ""}" data-tab="account">Из аккаунта</button>
      <button class="${form.tab === "link" ? "on" : ""}" data-tab="link">Ссылка</button></div>
    <div class="modal-body">${form.tab === "account" ? renderAccountTab() : renderLinkTab()}
      <label class="field">Имя папки в «Файлах»<input id="f-name" value="${esc(form.name)}" placeholder="${esc(defaultName)}" autocomplete="off">
        <span class="hint mono">@gdrive/${esc(form.name || defaultName)}</span></label>
      ${form.error ? `<div class="error">${esc(form.error)}</div>` : ""}
      ${form.busy ? `<div class="working"><span class="spinner"></span>Подключаю…</div>` : ""}
    </div>
    <div class="modal-foot"><button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" data-submit ${form.busy || !ready ? "disabled" : ""}>Подключить</button></div>
  </div></div>`;
}

dialog.addEventListener("click", async (e) => {
  if (e.target.dataset.closeOverlay !== undefined) { closeDialog(); return; }
  const t = e.target.closest("button");
  if (!t) return;
  const d = t.dataset;
  form.error = "";
  if (d.close !== undefined) closeDialog();
  else if (d.tab) { form.tab = d.tab; renderDialog(); if (d.tab === "account" && !form.roots) loadRoots(); }
  else if (d.root) browseInto({ root: d.root, title: ROOTS[d.root] }, []);
  else if (d.drive) browseInto({ root: "drive", drive_id: d.drive, drive_name: d.driveName, title: d.driveName }, []);
  else if (d.back !== undefined) { form.target = null; form.trail = []; renderDialog(); }
  else if (d.crumb) browseInto(form.target, form.trail.slice(0, Number(d.crumb)));
  else if (d.folder) browseInto(form.target, [...form.trail, { id: d.folder, name: d.folderName }]);
  else if (d.submit !== undefined) {
    form.busy = true;
    renderDialog();
    const body = form.tab === "link"
      ? { kind: "link", url: form.url.trim(), account: form.account, name: form.name || null }
      : { kind: "account", account: form.account, root: form.target.root, drive_id: form.target.drive_id || null,
          folder_id: form.trail.length ? form.trail[form.trail.length - 1].id : null,
          folder_path: form.trail.length ? form.trail.map((f) => f.name).join("/") : null, name: form.name || null };
    try {
      await pulse.plugin("/mounts", { method: "POST", body });
      closeDialog();
      await load();
    } catch (err) {
      form.busy = false;
      form.error = err.message;
      renderDialog();
    }
  }
});

dialog.addEventListener("input", (e) => {
  if (e.target.id === "f-name") {
    form.name = e.target.value;
    const hint = e.target.parentElement.querySelector(".hint");
    if (hint) hint.textContent = `@gdrive/${form.name || e.target.placeholder}`;
  } else if (e.target.id === "f-url") form.url = e.target.value;
});

dialog.addEventListener("change", (e) => {
  if (e.target.id === "f-account") {
    form.account = e.target.value;
    if (form.tab === "account") loadRoots(); else checkUrl();
  } else if (e.target.id === "f-url") checkUrl();
});

load();
