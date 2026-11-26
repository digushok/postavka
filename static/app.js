"use strict";
/* Поставки Ozon — интерфейс. Без внешних библиотек. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => { const n = parseInt(String(v).replace(/\s/g, ""), 10); return isNaN(n) ? 0 : n; };
const fmt = (n) => Number(n || 0).toLocaleString("ru-RU");

let APP = { profile: null, profiles: [], clusters: null };
let D = null;            // черновик новой поставки
let PLAN = null;         // последний расчёт с сервера
let openBatch = null;    // id открытой партии
let pollTimer = null;
let selSlot = null;

/* ---------------- служебное ---------------- */
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let js = {};
  try { js = await r.json(); } catch (e) { /* пусто */ }
  if (!r.ok) throw new Error(js.error || `Ошибка ${r.status}`);
  return js;
}
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3000);
}
function confirmBox(html, yes = "Да", okOnly = false) {
  return new Promise((res) => {
    $("#modalBody").innerHTML = html; $("#modalYes").textContent = yes;
    $("#modalNo").classList.toggle("hidden", okOnly);
    $("#modal").classList.remove("hidden");
    const done = (v) => { $("#modal").classList.add("hidden"); $("#modalYes").onclick = $("#modalNo").onclick = null; res(v); };
    $("#modalYes").onclick = () => done(true); $("#modalNo").onclick = () => done(false);
  });
}
// текст из поля ввода в модальном окне запоминаем на лету
const promptBox = { _v: "" };
document.addEventListener("input", (e) => { if (e.target.id === "promptIn") promptBox._v = e.target.value; });
function lsGet(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* нет хранилища */ } }
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const addDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return ymd(d); };
const dmy = (d) => (d ? String(d).split("-").reverse().join(".") : "");
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ---------------- вкладки ---------------- */
function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
  lsSet("tab", name);
  if (name === "batches") { if (openBatch) showBatch(openBatch); else loadBatches(); }
  if (name === "settings") renderSettings();
}
$$(".tabs button").forEach((b) => (b.onclick = () => { if (b.dataset.tab === "batches") closeBatch(); showTab(b.dataset.tab); }));

/* ---------------- загрузка ---------------- */
async function loadState() {
  const s = await api("/api/state");
  APP.profile = s.profile; APP.profiles = s.profiles; APP.dataDir = s.data_dir; APP.server = !!s.server;
  document.body.classList.toggle("server", APP.server);
  APP.ver = { version: s.version, date: s.release_date, repo: s.repo, changes: s.changes || [] };
  $("#verFoot").textContent = `Поставки Ozon · версия ${s.version} от ${dmy(s.release_date)}`;
  document.title = `Поставки Ozon ${s.version}`;
  if (s.import_note) setTimeout(() => toast(`Настройки и ключи перенесены из прошлой версии: ${s.import_note}`), 600);
  $("#shopSel").innerHTML = s.profiles.map((p) => `<option value="${p.id}" ${p.id === s.active ? "selected" : ""}>${esc(p.name)}${p.demo ? " (демо)" : ""}</option>`).join("");
  $("#demoNote").classList.toggle("hidden", !APP.profile.demo);
  $("#keyNote").classList.toggle("hidden", APP.profile.demo || (APP.profile.client_id && APP.profile.api_key_set));
}
$("#shopSel").onchange = async (e) => {
  await api("/api/profile/select", { id: e.target.value });
  APP.clusters = null; openBatch = null;
  await init();
};

async function loadClusters() {
  const P = APP.profile;
  if (!P.demo && !(P.client_id && P.api_key_set)) { APP.clusters = []; $("#clusterNote").textContent = "Кластеры загрузятся после ввода ключа API."; return; }
  try {
    const r = await api("/api/clusters");
    APP.clusters = r.clusters;
    $("#clusterNote").textContent = "";
    // привязываем города шаблонов к кластерам по ключевому слову
    let changed = false;
    for (const t of P.templates) for (const c of t.cities) {
      if (!c.cluster_id) {
        const m = (c.match || c.name || "").toLowerCase();
        const hit = findCluster(m);
        if (hit) { c.cluster_id = hit.id; c.cluster_name = hit.name; changed = true; }
      }
    }
    if (changed) await saveProfile({ templates: P.templates });
    if (D) bindRows();
  } catch (e) {
    APP.clusters = [];
    $("#clusterNote").innerHTML = `<span class="err-text">Не удалось загрузить кластеры: ${esc(e.message)}</span>`;
  }
}
function findCluster(m) {
  const n = (s) => String(s || "").toLowerCase().replace(/ё/g, "е").trim();
  const q = n(m); if (!q) return null;
  const L = APP.clusters || [];
  return L.find((x) => n(x.name) === q) || L.find((x) => n(x.name).startsWith(q)) || L.find((x) => n(x.name).includes(q)) || null;
}
async function saveProfile(part) {
  const r = await api("/api/profile/save", { profile: part });
  APP.profile = r.profile;
}

/* ================= НОВАЯ ПОСТАВКА ================= */
function draftKey() { return "draft:" + APP.profile.id; }
function newDraft() {
  const P = APP.profile;
  const tpl = P.templates.find((t) => t.name === P.template) || P.templates[0];
  const ts = P.timeslot || {};
  return {
    products: [], template: tpl ? tpl.name : "",
    rows: rowsFromTemplate(tpl), mode: "share",
    method: P.method || "remainder_city", remCity: P.remainder_city || "largest",
    tsmode: ts.mode || "auto",
    ts: { d1: addDays(ts.days_from ?? 2), t1: ts.time_from || "10:00" },
    passOn: !!(P.pass && P.pass.enabled), dropoff: P.dropoff || null, multi: false, cv: 2,
  };
}
function saveDraft() { lsSet(draftKey(), D); }

function renderNew() {
  renderProducts(); renderDistControls(); renderShipping();
  replan();
}

/* ---- товары ---- */
function renderProducts() {
  const saved = APP.profile.products || {};
  $("#prodTable tbody").innerHTML = D.products.map((p, i) => {
    const s = saved[p.sku] || {};
    const name = p.name || s.name || "";
    return `<tr data-i="${i}">
      <td><input class="p-sku" value="${esc(p.sku)}" placeholder="SKU" inputmode="numeric"></td>
      <td><div class="pname" title="${esc(name)}">${name ? esc(name) : (p.sku ? '<span class="muted">—</span>' : "")}${p.lookupErr ? ` <span class="err-text">${esc(p.lookupErr)}</span>` : ""}</div></td>
      <td><input class="p-short" value="${esc(p.short || "")}" placeholder="${esc(shortGuess(name))}"></td>
      <td><input class="p-box" type="number" min="0" value="${p.box_size || ""}" placeholder="—"></td>
      <td class="c"><input class="p-mult" type="checkbox" ${p.multiple ? "checked" : ""} ${p.box_size ? "" : "disabled"}></td>
      <td><input class="p-qty" type="number" min="0" value="${p.qty || ""}"></td>
      <td><button class="x" title="Убрать">×</button></td></tr>`;
  }).join("") || `<tr class="empty"><td colspan="7">Пока пусто. Нажмите «+ Товар» или вставьте список из Excel.</td></tr>`;
}
function shortGuess(name) {
  const m = String(name || "").match(/(\d+\s?(ГБ|ТБ|GB|TB|Гб|Тб))/i);
  return m ? m[1].replace(/\s/, " ") : "";
}
function prodLabel(p) { const s = APP.profile.products[p.sku] || {}; return p.short || shortGuess(p.name || s.name) || p.name || s.name || p.sku; }

$("#addProd").onclick = () => { D.products.push({ sku: "", qty: 0 }); saveDraft(); renderProducts(); $$("#prodTable .p-sku").pop()?.focus(); };
$("#prodTable").addEventListener("click", (e) => {
  if (!e.target.classList.contains("x")) return;
  const i = +e.target.closest("tr").dataset.i;
  D.products.splice(i, 1); saveDraft(); renderProducts(); replan();
});
$("#prodTable").addEventListener("change", async (e) => {
  const tr = e.target.closest("tr"); if (!tr || tr.dataset.i === undefined) return;
  const p = D.products[+tr.dataset.i];
  const t = e.target;
  if (t.classList.contains("p-sku")) {
    p.sku = t.value.trim(); p.name = ""; p.lookupErr = "";
    const s = APP.profile.products[p.sku];
    if (s) { p.short = p.short || s.short || ""; p.box_size = s.box_size || p.box_size; p.multiple = s.multiple ?? p.multiple; p.name = s.name || ""; }
    renderProducts(); await lookup([p]);
  } else if (t.classList.contains("p-short")) { p.short = t.value.trim(); rememberProduct(p); }
  else if (t.classList.contains("p-box")) { p.box_size = num(t.value) || null; if (!p.box_size) p.multiple = false; else if (p.multiple === undefined) p.multiple = true; rememberProduct(p); renderProducts(); }
  else if (t.classList.contains("p-mult")) { p.multiple = t.checked; rememberProduct(p); }
  else if (t.classList.contains("p-qty")) { p.qty = num(t.value); }
  saveDraft(); replan();
});
const rememberProduct = debounce(async (p) => {
  if (!p.sku) return;
  const prods = APP.profile.products || {};
  prods[p.sku] = Object.assign(prods[p.sku] || {}, { short: p.short || "", box_size: p.box_size || null, multiple: !!p.multiple });
  try { await saveProfile({ products: prods }); } catch (e) { /* не критично */ }
}, 400);

async function lookup(list) {
  const skus = list.filter((p) => /^\d+$/.test(p.sku)).map((p) => p.sku);
  if (!skus.length) return;
  try {
    const r = await api("/api/products", { skus });
    const st = await api("/api/state"); APP.profile = st.profile;
    for (const p of list) {
      const f = r.found[p.sku];
      if (f) { p.name = f.name; p.lookupErr = ""; } else if (p.sku) p.lookupErr = "не найден в магазине";
    }
  } catch (e) {
    for (const p of list) p.lookupErr = "";
    toast("Не удалось получить данные товаров: " + e.message, true);
  }
  saveDraft(); renderProducts(); replan();
}

/* ---- импорт готовой таблицы «кластеры × SKU» из Excel ---- */
$("#mxBtn").onclick = () => $("#mxBox").classList.toggle("hidden");
$("#mxClose").onclick = () => $("#mxBox").classList.add("hidden");
$("#mxTpl").onclick = async () => {
  try {
    if (!APP.clusters) await loadClusters();
    const d = {
      products: prodsOf().map((p) => ({ sku: p.sku, name: p.name, short: p.short, box_size: p.box_size })),
      clusters: D.rows.map((r) => rowName(r) || r.match).filter(Boolean),
      all_clusters: (APP.clusters || []).map((c) => c.name).sort((a, b) => a.localeCompare(b, "ru")),
    };
    const a = document.createElement("a");
    a.href = "/matrix-template.xlsx?d=" + encodeURIComponent(JSON.stringify(d));
    document.body.appendChild(a); a.click(); a.remove();
    toast("Шаблон сохранён в «Загрузки»: в нём ваши товары и кластеры");
  } catch (e) { toast(e.message, true); }
};
$("#mxPick").onclick = () => $("#mxFile").click();
$("#mxFile").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  try {
    const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
    const t = await api("/api/matrix/import", { name: f.name, data });
    if (!APP.clusters) await loadClusters();
    const miss = t.rows.filter((r) => !findCluster(r.name)).map((r) => r.name);
    const units = t.products.reduce((a, p) => a + p.qty, 0);
    const ok = await confirmBox(`<h3>Заполнить из таблицы?</h3><p>Товаров: <b>${t.products.length}</b>, кластеров: <b>${t.rows.length}</b>, всего <b>${fmt(units)}</b> шт. Текущие товары и распределение в форме будут заменены.</p>`
      + (miss.length ? `<p class="warn-text">Не найдены в Озоне: ${miss.map(esc).join(", ")}. Эти строки останутся в таблице с пометкой — поправьте название в файле или выберите кластер вручную.</p>` : "")
      + (t.skipped.length ? `<p class="muted small">Пропущено: ${t.skipped.map(esc).join("; ")}</p>` : ""), "Заполнить");
    if (!ok) return;
    const saved = APP.profile.products || {};
    D.products = t.products.map((p) => { const s = saved[p.sku] || {}; return { sku: p.sku, qty: p.qty, name: s.name || "", short: s.short || "", box_size: p.box_size || s.box_size || null, multiple: !!s.multiple }; });
    D.rows = t.rows.map((r) => {
      const hit = findCluster(r.name);
      const total = Object.values(r.cells).reduce((a, v) => a + v, 0);
      return { cluster_id: hit ? hit.id : null, cluster_name: hit ? hit.name : null, match: r.name, fixed: { ...r.cells },
               share: units ? Math.round(total / units * 1000) / 10 : 0 };
    });
    // доли округлены до десятых — остаток округления отдаём самому крупному кластеру, чтобы сумма была 100%
    const top = [...D.rows].sort((x, y) => y.share - x.share)[0];
    if (top) top.share = Math.round((top.share + 100 - D.rows.reduce((a, r) => a + r.share, 0)) * 10) / 10;
    D.mode = "share";
    saveDraft(); $("#mxBox").classList.add("hidden"); renderNew(); await lookup(D.products);
    toast(miss.length ? `Таблица загружена. Не найдено кластеров: ${miss.length}` : "Таблица загружена", !!miss.length);
  } catch (err) { toast(err.message, true); }
};

$("#pasteBtn").onclick = () => { $("#pasteBox").classList.toggle("hidden"); $("#pasteText").focus(); };
$("#pasteCancel").onclick = () => $("#pasteBox").classList.add("hidden");
$("#pasteApply").onclick = async () => {
  const added = [];
  for (const line of $("#pasteText").value.split(/\r?\n/)) {
    const parts = line.trim().split(/[\t;,]+|\s{2,}|\s(?=\d)/).map((x) => x.trim()).filter(Boolean);
    if (parts.length < 1 || !/^\d{3,}$/.test(parts[0])) continue;
    const sku = parts[0], qty = num(parts[parts.length - 1] !== sku ? parts[parts.length - 1] : 0);
    const s = APP.profile.products[sku] || {};
    let p = D.products.find((x) => x.sku === sku);
    if (p) p.qty = qty; else { p = { sku, qty, short: s.short || "", box_size: s.box_size || null, multiple: !!s.multiple, name: s.name || "" }; D.products.push(p); }
    added.push(p);
  }
  D.products = D.products.filter((p) => p.sku);
  $("#pasteText").value = ""; $("#pasteBox").classList.add("hidden");
  saveDraft(); renderProducts(); await lookup(added);
};

/* ---- распределение по кластерам: одна таблица ---- */
// D.rows: [{cluster_id, cluster_name, share, fixed}] — fixed: {sku: qty}, если количество в строке правили вручную
const prodsOf = () => D.products.filter((p) => p.sku);
const totalOf = (row) => Object.values(row || {}).reduce((a, v) => a + (+v || 0), 0);
const purchaseTotal = () => prodsOf().reduce((a, p) => a + (p.qty || 0), 0);
const pct = (v) => (Math.round(v * 10) / 10).toLocaleString("ru-RU");
const rowName = (r) => r.cluster_name || ((APP.clusters || []).find((c) => String(c.id) === String(r.cluster_id)) || {}).name || r.cluster_id;

function migrateDraft(d) {
  // старая форма: города + отдельный кластер + матрица с правками по ячейкам
  if (!d.rows) {
    d.rows = (d.cities || []).map((c) => {
      const row = { cluster_id: c.cluster_id || null, cluster_name: c.cluster_name || null, share: +c.share || 0, match: c.match || c.cluster_name || c.name, fixed: null };
      const key = c.cluster_id ? String(c.cluster_id) : "?" + c.name;
      if (d.edited && Object.keys(d.edited).some((k) => k.startsWith(key + "|")) && d.matrix && d.matrix[key]) row.fixed = { ...d.matrix[key] };
      return row;
    });
    delete d.cities; delete d.edited; delete d.matrix;
  }
  // до 1.11.1 правка количества меняла долю строки — возвращаем исходную, чтобы не сдвигались другие строки
  for (const r of d.rows) if (r.fixed && r.share_before !== undefined) r.share = r.share_before;
  // режим «Наборами» убран (1.13.2): наборы черновика превращаем в количества по ячейкам
  if (d.mode === "kits") {
    const defs = (d.kits && d.kits.defs) || [], alloc = (d.kits && d.kits.alloc) || {};
    for (const r of d.rows) {
      const a = alloc[r.cluster_id ? String(r.cluster_id) : "?" + (r.match || r.cluster_name || "")] || {}, cells = {};
      for (const k of defs) for (const [sku, q] of Object.entries(k.items || {})) cells[sku] = (cells[sku] || 0) + (+a[k.id] || 0) * (+q || 0);
      r.fixed = cells;
    }
  }
  d.mode = "share";
  delete d.kits;
  return d;
}
function rowsFromTemplate(t) {
  return (t ? t.cities : []).map((c) => ({ cluster_id: c.cluster_id || null, cluster_name: c.cluster_name || null, share: +c.share || 0, match: c.match || c.cluster_name || c.name, fixed: null }));
}
/* ---- порядок кластеров в таблице ----
   По умолчанию — как ниже. Если строки перетаскивали, порядок (ID кластеров) хранится в магазине
   (profile.cluster_order) и применяется везде: шаблоны, импорт, новые кластеры, порядок заявок. */
const DEFAULT_ORDER = ["москва", "екатеринбург", ["петербург", "спб"], "ростов", "казань", "воронеж", "уфа", "краснодар",
  "саратов", "невинномысск", "ярославль", "тюмень", "оренбург", "пермь", "тверь", "калининград"];
function defaultRank(name) {
  const n = String(name || "").toLowerCase().replace(/ё/g, "е");
  const i = DEFAULT_ORDER.findIndex((k) => [].concat(k).some((w) => n.includes(w)));
  return i < 0 ? DEFAULT_ORDER.length : i;
}
function clusterOrder() {
  // все кластеры Озона: сначала сохранённый порядок, остальные — по умолчанию (неизвестные — по алфавиту)
  const all = (APP.clusters || []).map((c) => ({ id: String(c.id), name: c.name }));
  const nameOf = new Map(all.map((c) => [c.id, c.name]));
  const saved = (APP.profile.cluster_order || []).map(String).filter((id) => nameOf.has(id));
  const inSaved = new Set(saved);
  const rest = all.filter((c) => !inSaved.has(c.id))
    .sort((a, b) => defaultRank(a.name) - defaultRank(b.name) || a.name.localeCompare(b.name, "ru")).map((c) => c.id);
  if (!saved.length) return rest;
  // кластер, которого нет в сохранённом порядке, встаёт после последнего кластера, что по умолчанию идёт раньше него
  const out = [...saved];
  for (const id of rest) {
    const r = defaultRank(nameOf.get(id));
    let at = 0;
    for (let i = out.length - 1; i >= 0; i--) if (defaultRank(nameOf.get(out[i])) <= r) { at = i + 1; break; }
    out.splice(at, 0, id);
  }
  return out;
}
function sortRows() {
  const pos = new Map(clusterOrder().map((id, i) => [id, i]));
  const rank = (r) => (r.cluster_id && pos.has(String(r.cluster_id)) ? pos.get(String(r.cluster_id)) : 1e6 + defaultRank(r.cluster_name || r.match));
  D.rows = D.rows.map((r, i) => [r, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map((x) => x[0]);
}
async function moveRow(key, toIndex) {
  // строка key встаёт на место toIndex; новый порядок запоминается в магазине
  const from = D.rows.findIndex((r) => rowKey(r) === key);
  if (from < 0 || toIndex < 0 || toIndex >= D.rows.length || toIndex === from) return;
  const [r] = D.rows.splice(from, 1); D.rows.splice(toIndex, 0, r);
  const order = clusterOrder();
  const ids = D.rows.filter((x) => x.cluster_id && order.includes(String(x.cluster_id))).map((x) => String(x.cluster_id));
  const slots = ids.map((id) => order.indexOf(id)).sort((a, b) => a - b);
  ids.forEach((id, i) => { order[slots[i]] = id; });
  saveDraft(); renderDist();
  try { await saveProfile({ cluster_order: order }); } catch (e) { toast(e.message, true); }
  renderDistControls();
}

function bindRows() {
  // кластеры без ID (из старых шаблонов) — находим по названию; дубли убираем
  const seen = new Set();
  D.rows = D.rows.filter((r) => {
    if (!r.cluster_id && APP.clusters?.length) { const hit = findCluster(r.match || r.cluster_name || ""); if (hit) { r.cluster_id = hit.id; r.cluster_name = hit.name; } }
    if (r.cluster_id && !r.cluster_name) r.cluster_name = rowName(r);
    const k = String(r.cluster_id || "?" + (r.match || ""));
    if (seen.has(k)) return false; seen.add(k); return true;
  });
  sortRows();
}
const rowKey = (r) => (r.cluster_id ? String(r.cluster_id) : "?" + (r.match || r.cluster_name || ""));

function renderDistControls() {
  const P = APP.profile;
  $("#tplSel").innerHTML = P.templates.map((t) => `<option ${t.name === D.template ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  $("#method").value = D.method;
  $("#remCityWrap").classList.toggle("hidden", D.method === "floor");
  $("#remCity").innerHTML = `<option value="largest">с наибольшей долей</option>` +
    D.rows.filter((r) => r.cluster_id).map((r) => `<option value="${esc(r.cluster_id)}" ${String(D.remCity) === String(r.cluster_id) ? "selected" : ""}>${esc(rowName(r))}</option>`).join("");
  if (![...$("#remCity").options].some((o) => o.selected)) $("#remCity").value = "largest";
  const used = new Set(D.rows.map((r) => String(r.cluster_id)));
  const pos = new Map(clusterOrder().map((id, i) => [id, i]));
  const free = (APP.clusters || []).filter((c) => !used.has(String(c.id))).sort((a, b) => (pos.get(String(a.id)) ?? 1e6) - (pos.get(String(b.id)) ?? 1e6));
  $("#addCluster").innerHTML = free.length ? `<option value="">Добавить кластер…</option>` + free.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("") : `<option value="">Все кластеры уже в таблице</option>`;
  $("#distSub").textContent = "Меняйте доли или количество прямо в таблице — остальное пересчитается. Порядок кластеров — перетаскиванием за ⠿";
  $("#orderReset").classList.toggle("hidden", !(P.cluster_order || []).length);
}

function renderDist() {
  renderDistControls();
  const prods = prodsOf();
  const M = PLAN?.matrix || {};
  const buy = Object.fromEntries(prods.map((p) => [p.sku, p.qty || 0]));
  const tot = purchaseTotal();
  if (!prods.length || !D.rows.length) {
    $("#dist").innerHTML = `<tr class="empty"><td>Добавьте товары в шаге 1 и кластеры кнопкой «+ Кластер».</td></tr>`;
    $("#distWarn").innerHTML = ""; renderSummary(); return;
  }
  let h = `<thead><tr><th>Кластер Озона</th><th>Доля, %</th>${prods.map((p) => `<th title="${esc(p.name || "")}">${esc(prodLabel(p))}</th>`).join("")}<th>Всего</th><th>Коробок</th><th></th></tr></thead><tbody>`;
  const col = {}; let offCells = 0;
  for (const r of D.rows) {
    const k = rowKey(r); const row = M[k] || {};
    const rt = totalOf(row);
    const actual = tot ? rt / tot * 100 : 0;
    const shareShown = r.fixed ? actual : +r.share || 0;
    const i = D.rows.indexOf(r);
    h += `<tr data-k="${esc(k)}"><td class="cl" title="${esc(rowName(r))}"><span class="mv"><span class="grip" draggable="true" title="Перетащите, чтобы поменять порядок">⠿</span><span class="ud"><button data-up="${esc(k)}" ${i ? "" : "disabled"} title="Выше">▲</button><button data-dn="${esc(k)}" ${i < D.rows.length - 1 ? "" : "disabled"} title="Ниже">▼</button></span></span>${esc(rowName(r))}${r.cluster_id ? "" : ' <span class="err-text">не найден в Озоне</span>'}${r.fixed ? `<span class="manual" data-reset="${esc(k)}" title="Количество задано вручную. Нажмите, чтобы снова считать от доли">вручную ↺</span>` : ""}</td>
      <td><input class="share" type="number" min="0" step="0.5" value="${+shareShown.toFixed(1)}" data-share="${esc(k)}"></td>`;
    for (const p of prods) {
      const v = row[p.sku] ?? 0;
      col[p.sku] = (col[p.sku] || 0) + v;
      const skuShare = buy[p.sku] ? v / buy[p.sku] * 100 : 0;
      const off = rt > 0 && buy[p.sku] && Math.abs(skuShare - actual) > 0.5;
      if (off) offCells++;
      h += `<td><input type="number" min="0" value="${v}" data-c="${esc(k)}" data-s="${esc(p.sku)}" class="${off ? "off" : ""}" title="${off ? `Доля по этому SKU ${pct(skuShare)}%, а по кластеру ${pct(actual)}%` : ""}"></td>`;
    }
    h += `<td class="tot">${fmt(rt)}</td><td class="boxes">${fmt((PLAN?.boxes || {})[k] || 0)}</td><td><button class="x" data-del="${esc(k)}" title="Убрать кластер">×</button></td></tr>`;
  }
  const allTot = prods.reduce((a, p) => a + (col[p.sku] || 0), 0);
  const shareSum = D.rows.reduce((a, r) => a + (+r.share || 0), 0);
  h += `</tbody><tfoot>
    <tr><td class="lbl">Распределено</td><td class="${Math.abs(shareSum - 100) < 0.05 ? "ok-mark" : "bad-mark"}" title="Сумма долей">${pct(shareSum)}%</td>${prods.map((p) => `<td>${fmt(col[p.sku] || 0)}</td>`).join("")}<td>${fmt(allTot)}</td><td>${fmt(Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0))}</td><td></td></tr>
    <tr><td class="lbl">Закуплено</td><td></td>${prods.map((p) => `<td><input type="number" min="0" value="${p.qty || 0}" data-buy="${esc(p.sku)}"></td>`).join("")}<td class="sub">${fmt(tot)}</td><td></td><td></td></tr>
    <tr><td class="lbl">Проверка</td><td></td>${prods.map((p) => { const d = (col[p.sku] || 0) - (p.qty || 0); return `<td class="${d ? "bad-mark" : "ok-mark"}">${d ? (d > 0 ? "+" : "") + fmt(d) : "✓"}</td>`; }).join("")}<td class="${allTot - tot ? "bad-mark" : "ok-mark"}">${allTot - tot ? (allTot - tot > 0 ? "+" : "") + fmt(allTot - tot) : "✓"}</td><td></td><td></td></tr>
  </tfoot>`;
  $("#dist").innerHTML = h;
  const diffs = prods.some((p) => (col[p.sku] || 0) !== (p.qty || 0));
  $("#restBtn").classList.toggle("hidden", !diffs);
  const manualRows = D.rows.filter((r) => r.fixed).length;
  $("#editedNote").classList.toggle("hidden", !manualRows);
  $("#editedNote").textContent = `вручную: ${manualRows} кластер.`;
  let warn = "";
  if (offCells) warn += `<div class="banner warn" style="margin-top:10px">Доля кластера уже не одинакова по всем SKU — такие ячейки подсвечены (наведите, чтобы увидеть долю по SKU).</div>`;
  if (Math.abs(shareSum - 100) >= 0.05) warn += `<div class="banner warn" style="margin-top:10px">Сумма долей ${pct(shareSum)}%, а не 100%.</div>`;
  $("#distWarn").innerHTML = warn;
  renderSummary();
}

// изменения в таблице
$("#dist").addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.share !== undefined) {                       // доля → строка считается от доли
    const r = D.rows.find((x) => rowKey(x) === t.dataset.share);
    r.share = parseFloat(String(t.value).replace(",", ".")) || 0; r.fixed = null;
  } else if (t.dataset.c !== undefined) {                    // количество → строка «вручную», доля из факта
    const r = D.rows.find((x) => rowKey(x) === t.dataset.c);
    if (!r.fixed) r.share_before = r.share;                    // для «↺» — вернуть исходную долю
    r.fixed = { ...((PLAN?.matrix || {})[t.dataset.c] || {}) }; r.fixed[t.dataset.s] = num(t.value);
    // доля строки (r.share) не меняется: от неё считаются остальные строки, и правка одной ячейки
    // не должна сдвигать количества в других кластерах. В строке показывается фактическая доля.
  } else if (t.dataset.buy !== undefined) {                  // закупка
    const p = D.products.find((x) => x.sku === t.dataset.buy); p.qty = num(t.value);
    renderProducts();
  } else return;
  saveDraft(); replan();
});
$("#dist").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.dataset.del !== undefined) {
    D.rows = D.rows.filter((r) => rowKey(r) !== t.dataset.del);
    saveDraft(); replan();
  } else if (t.dataset.up !== undefined || t.dataset.dn !== undefined) {
    const up = t.dataset.up !== undefined, k = up ? t.dataset.up : t.dataset.dn;
    const i = D.rows.findIndex((r) => rowKey(r) === k);
    await moveRow(k, up ? i - 1 : i + 1);
    const b = $(`#dist button[data-${up ? "up" : "dn"}="${CSS.escape(k)}"]`); if (b && !b.disabled) b.focus();
  } else if (t.dataset.reset !== undefined) {
    const r = D.rows.find((x) => rowKey(x) === t.dataset.reset);
    r.fixed = null; if (r.share_before !== undefined) { r.share = r.share_before; delete r.share_before; }
    saveDraft(); replan();
  }
});
// перетаскивание строк за ⠿
let dragKey = null;
$("#dist").addEventListener("dragstart", (e) => {
  const tr = e.target.closest && e.target.closest("tr[data-k]");
  if (!tr || !e.target.classList.contains("grip")) return;
  dragKey = tr.dataset.k; tr.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragKey);
  e.dataTransfer.setDragImage(tr, 20, 16);
});
const dropMarks = () => $$("#dist tr.drop-before, #dist tr.drop-after").forEach((x) => x.classList.remove("drop-before", "drop-after"));
$("#dist").addEventListener("dragover", (e) => {
  if (!dragKey) return;
  const tr = e.target.closest("tbody tr[data-k]"); if (!tr) return;
  e.preventDefault(); dropMarks();
  const r = tr.getBoundingClientRect();
  tr.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
});
$("#dist").addEventListener("drop", async (e) => {
  if (!dragKey) return;
  const tr = e.target.closest("tbody tr[data-k]"); if (!tr) return;
  e.preventDefault();
  const after = tr.classList.contains("drop-after");
  const key = dragKey; dragKey = null; dropMarks();
  const from = D.rows.findIndex((r) => rowKey(r) === key);
  let to = D.rows.findIndex((r) => rowKey(r) === tr.dataset.k) + (after ? 1 : 0);
  if (from < to) to--;
  await moveRow(key, to);
});
$("#dist").addEventListener("dragend", () => { dragKey = null; dropMarks(); $$("#dist tr.dragging").forEach((x) => x.classList.remove("dragging")); });
$("#orderReset").onclick = async () => {
  try { await saveProfile({ cluster_order: [] }); } catch (e) { return toast(e.message, true); }
  sortRows(); saveDraft(); renderDistControls(); renderDist(); toast("Порядок кластеров — по умолчанию");
};

$("#dist").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
  e.preventDefault();
  const ins = $$("#dist tbody input"); const i = ins.indexOf(e.target); const step = $$("#dist tbody tr")[0].querySelectorAll("input").length;
  const next = ins[i + step] || ins[0]; next.focus(); next.select();
});
$("#addClusterBtn").onclick = async () => {
  const id = $("#addCluster").value; if (!id) return toast("Выберите кластер в списке");
  const c = (APP.clusters || []).find((x) => String(x.id) === id);
  D.rows.push({ cluster_id: id, cluster_name: c ? c.name : id, share: 0, fixed: null });
  saveDraft(); renderDistControls(); replan();
  toast(`«${c ? c.name : id}» добавлен — укажите долю`);
};
$("#restBtn").onclick = () => {
  const target = D.remCity !== "largest" ? D.rows.find((r) => String(r.cluster_id) === String(D.remCity))
    : [...D.rows].sort((a, b) => (+b.share || 0) - (+a.share || 0))[0];
  if (!target) return;
  const k = rowKey(target); const M = PLAN?.matrix || {};
  const col = {}; for (const r of D.rows) for (const [s, v] of Object.entries(M[rowKey(r)] || {})) col[s] = (col[s] || 0) + v;
  if (!target.fixed) target.share_before = target.share;
  target.fixed = { ...(M[k] || {}) };
  for (const p of prodsOf()) target.fixed[p.sku] = Math.max(0, (target.fixed[p.sku] || 0) + (p.qty || 0) - (col[p.sku] || 0));
  saveDraft(); replan(); toast(`Остаток добавлен в «${rowName(target)}»`);
};
$("#method").onchange = (e) => { D.method = e.target.value; saveDraft(); replan(); };
$("#remCity").onchange = (e) => { D.remCity = e.target.value; saveDraft(); replan(); };
$("#recalc").onclick = async () => { for (const r of D.rows) { if (r.fixed && r.share_before !== undefined) r.share = r.share_before; r.fixed = null; delete r.share_before; } saveDraft(); replan(); };
$("#tplSel").onchange = async (e) => {
  const t = APP.profile.templates.find((x) => x.name === e.target.value);
  if (!t) return;
  D.template = t.name; D.rows = rowsFromTemplate(t); bindRows();
  await saveProfile({ template: t.name });
  saveDraft(); replan();
};
async function saveTemplate(name) {
  const tpls = APP.profile.templates.filter((t) => t.name !== name);
  const cities = D.rows.filter((r) => r.cluster_id).map((r) => ({ name: rowName(r), cluster_id: r.cluster_id, cluster_name: rowName(r), share: +r.share || 0, match: r.match || rowName(r) }));
  const idx = APP.profile.templates.findIndex((t) => t.name === name);
  if (idx >= 0) tpls.splice(idx, 0, { name, cities }); else tpls.push({ name, cities });
  await saveProfile({ templates: tpls, template: name, method: D.method, remainder_city: D.remCity });
  D.template = name; saveDraft(); renderDistControls(); toast(`Шаблон «${name}» сохранён`);
}
$("#tplSave").onclick = () => saveTemplate(D.template || "Основной");
$("#tplSaveAs").onclick = async () => {
  const ok = await confirmBox(`<h3>Новый шаблон кластеров</h3><input id="promptIn" placeholder="Название, например «Без Москвы»">`, "Сохранить");
  const name = ok ? (promptBox._v || "").trim() : "";
  promptBox._v = "";
  if (name) saveTemplate(name);
};

/* ---- расчёт ---- */
const replan = debounce(async () => {
  bindRows();
  const body = {
    products: prodsOf().map((p) => ({ ...p, short: prodLabel(p) })), mode: D.mode, method: D.method, remainder_city: D.remCity,
    rows: D.rows.map((r) => ({ key: rowKey(r), share: r.share, name: rowName(r), fixed: r.fixed })),
  };
  try {
    PLAN = await api("/api/plan", body);
    renderDist(); renderGroups();
  } catch (e) { toast(e.message, true); }
}, 150);

function renderGroups() {
  const g = PLAN?.groups || [];
  const total = g.reduce((a, x) => a + x.total, 0);
  $("#boxTotal").textContent = total ? `${fmt(total)} коробок · ${g.length} вариантов состава — столько же файлов этикеток` : "Одинаковые коробки — один файл этикеток";
  $("#groupList").innerHTML = g.length ? g.map((x) => `<div class="gbox ${x.total > 2 ? "big" : ""}">
      <div class="gh"><span class="gname">${esc(x.name)}</span><span class="gqty">× ${fmt(x.qty)} шт</span><span class="gcount">${x.total} кор</span></div>
      <div class="gcities">${x.cities.map((c) => esc(c.name) + (c.count > 1 ? ` ×${c.count}` : "")).join(", ")}</div></div>`).join("")
    : `<div class="empty-state">Здесь появятся группы одинаковых коробок.</div>`;
}

/* ---- отгрузка ---- */
function renderShipping() {
  const favs = [...(APP.profile.dropoffs || [])];
  if (D.dropoff && !favs.some((f) => f.warehouse_id === D.dropoff.warehouse_id)) favs.push(D.dropoff);
  $("#dropoffChips").innerHTML = favs.length ? favs.map((f, i) => `<button class="dchip ${D.dropoff && D.dropoff.warehouse_id === f.warehouse_id ? "sel" : ""}" data-i="${i}"><b>${esc(f.name)}</b><span>${esc(f.address || "")}</span></button>`).join("")
    : `<span class="warn-text">Точка не выбрана — найдите её ниже или добавьте в настройках</span>`;
  $$("#dropoffChips .dchip").forEach((el) => (el.onclick = () => { D.dropoff = favs[+el.dataset.i]; saveDraft(); renderShipping(); }));
  $$("input[name=tsmode]").forEach((r) => (r.checked = r.value === D.tsmode));
  $$("input[name=multi]").forEach((r) => (r.checked = r.value === (D.multi ? "1" : "0")));
  if (!D.ts.d1 || D.ts.d1 < ymd(new Date())) D.ts.d1 = addDays((APP.profile.timeslot || {}).days_from ?? 2);
  $("#tsD1").value = D.ts.d1; $("#tsT1").value = D.ts.t1 || "10:00";
  $("#tsAuto").classList.toggle("hidden", D.tsmode !== "auto");
  $("#tsManualHint").classList.toggle("hidden", D.tsmode !== "manual");
  $("#tsNoneHint").classList.toggle("hidden", D.tsmode !== "none");
  const ps = APP.profile.pass || {};
  $("#passOn").checked = D.passOn;
  $("#passInfo").textContent = ps.driver_name ? `${ps.driver_name}, ${ps.driver_phone || ""}, ${ps.vehicle_model || ""} ${ps.vehicle_number || ""}` : "Данные водителя не заполнены — укажите их в настройках.";
  $("#passOn").disabled = !ps.driver_name;
}
$$("input[name=tsmode]").forEach((r) => (r.onchange = () => { D.tsmode = r.value; saveDraft(); renderShipping(); renderSummary(); }));
$$("input[name=multi]").forEach((r) => (r.onchange = () => { D.multi = r.value === "1"; saveDraft(); renderShipping(); renderSummary(); }));
[["#tsD1", "d1"], ["#tsT1", "t1"]].forEach(([s, k]) => ($(s).onchange = (e) => { D.ts[k] = e.target.value; saveDraft(); }));
$("#passOn").onchange = (e) => { D.passOn = e.target.checked; saveDraft(); };
$("#dropoffChange").onclick = () => { $("#dropoffSearch").classList.toggle("hidden"); $("#dropoffQ").focus(); };
async function searchDropoff(q, resEl, onPick) {
  resEl.innerHTML = `<div class="muted">Ищу…</div>`;
  try {
    const r = await api("/api/dropoff", { q });
    resEl.innerHTML = r.items.length ? r.items.map((x, i) => `<div class="it" data-i="${i}">${esc(x.name)}<div class="muted">${esc(x.address || "")} · ${esc((x.warehouse_type || "").replace("WAREHOUSE_TYPE_", ""))}</div></div>`).join("") : `<div class="muted">Ничего не найдено. Пункт выдачи (ПВЗ) Озон находит только по полному названию, как в кабинете, например «МОСКВА_10704». По адресу ПВЗ не ищутся.</div>`;
    $$(".it", resEl).forEach((el) => (el.onclick = () => onPick(r.items[+el.dataset.i])));
  } catch (e) { resEl.innerHTML = `<div class="err-text">${esc(e.message)}</div>`; }
}
$("#dropoffFind").onclick = () => searchDropoff($("#dropoffQ").value, $("#dropoffRes"), (x) => { D.dropoff = x; saveDraft(); $("#dropoffSearch").classList.add("hidden"); $("#dropoffRes").innerHTML = ""; renderShipping(); });
$("#dropoffQ").onkeydown = (e) => { if (e.key === "Enter") $("#dropoffFind").click(); };

function renderSummary() {
  const n = D.rows.filter((r) => totalOf((PLAN?.matrix || {})[rowKey(r)]) > 0).length;
  const boxes = Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0);
  const units = D.products.reduce((a, p) => a + (p.qty || 0), 0);
  const min = D.multi ? 2 : Math.ceil(n * 0.55) * 2;
  $("#summary").innerHTML = n ? `<div class="k"><b>${D.multi ? 1 : n}</b><span>${D.multi ? `заявка на ${n} кластеров` : "заявок"}</span></div><div class="k"><b>${fmt(boxes)}</b><span>коробок</span></div><div class="k"><b>${fmt(units)}</b><span>штук</span></div><div class="k"><b>~${min}–${min + 5} мин</b><span>займёт создание</span></div>`
    : `<span class="muted">Добавьте товары, чтобы увидеть итог</span>`;
}

$("#createBtn").onclick = async () => {
  const prods = D.products.filter((p) => p.sku);
  const errs = [];
  if (!prods.length) errs.push("нет товаров");
  if (prods.some((p) => !/^\d+$/.test(p.sku))) errs.push("SKU должен состоять из цифр");
  if (prods.some((p) => p.lookupErr)) errs.push("есть товары, не найденные в магазине");
  const M = PLAN?.matrix || {};
  const used = D.rows.filter((r) => totalOf(M[rowKey(r)]) > 0);
  if (used.some((r) => !r.cluster_id)) errs.push("есть кластеры, не найденные в Озоне");
  if (!D.dropoff) errs.push("не выбрана точка отгрузки");
  if (D.tsmode === "auto" && !D.ts.d1) errs.push("не указан желаемый день поставки");

  if (errs.length) return toast("Нельзя создать: " + errs.join("; "), true);

  const sumCol = {}; for (const r of used) for (const [s, v] of Object.entries(M[rowKey(r)] || {})) sumCol[s] = (sumCol[s] || 0) + v;
  const diff = prods.filter((p) => (sumCol[p.sku] || 0) !== (p.qty || 0));
  const boxes = Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0);
  if (D.multi && used.length > 20) return toast("Одной заявкой Озон принимает не больше 20 кластеров", true);
  const ok = await confirmBox(`<h3>${D.multi ? `Создать одну заявку на ${used.length} кластеров?` : `Создать ${used.length} заявок?`}</h3>
    <p>${fmt(boxes)} коробок через «${esc(D.dropoff.name)}».<br>
    Таймслот: ${D.tsmode === "auto" ? `желаемо ${esc(dmy(D.ts.d1))} в ${esc(D.ts.t1 || "10:00")} — каждому кластеру ближайший свободный` : D.tsmode === "none" ? "без таймслота — выберете потом в кабинете Озона" : "выберете из доступных после создания черновиков"}.<br>
    ${D.passOn ? "Пропуск будет оформлен." : "Без пропуска."}</p>
    ${diff.length ? `<p class="warn-text">Распределено не столько, сколько закуплено: ${diff.map((p) => esc(prodLabel(p))).join(", ")}.</p>` : ""}
    ${APP.profile.demo ? '<p class="muted">Демо-режим: в настоящий Озон ничего не отправится.</p>' : ""}`, "Создать");
  if (!ok) return;
  $("#createBtn").disabled = true;
  try {
    const body = {
      products: prods.map((p) => ({ sku: p.sku, qty: p.qty, name: p.name, short: prodLabel(p), box_size: p.box_size || null })),
      cities: used.map((r) => ({ cluster_id: String(r.cluster_id), name: rowName(r), cluster_name: rowName(r) })),
      matrix: Object.fromEntries(used.map((r) => [String(r.cluster_id), M[rowKey(r)]])),
      mode: D.tsmode, slot_pref: { date: D.ts.d1, time: D.ts.t1 || "10:00" },
      dropoff: D.dropoff, pass: Object.assign({}, APP.profile.pass || {}, { enabled: D.passOn }), multi: !!D.multi,
      source: JSON.parse(JSON.stringify(D)),
    };
    const r = await api("/api/batch/create", body);
    openBatch = r.id; showTab("batches");
  } catch (e) { toast(e.message, true); }
  finally { $("#createBtn").disabled = false; }
};

/* ================= ПАРТИИ ================= */
const PHASE = { new: ["ожидает", ""], drafts: ["создаются черновики", "run"], await_slot: ["выберите таймслот", "warn"], supplies: ["создаются заявки", "run"], cargo: ["грузоместа и этикетки", "run"], done: ["готово", "ok"], partial: ["есть ошибки", "err"], error: ["ошибка", "err"], stopped: ["остановлено", "warn"], cancelling: ["отменяются заявки…", "run"], cancelled: ["отменена", ""] };
const SST = { new: ["ожидает", ""], draft: ["черновик…", "run"], draft_ok: ["черновик готов", ""], supply: ["заявка…", "run"], cargo: ["грузоместа…", "run"], labels: ["этикетки…", "run"], pass: ["пропуск…", "run"], done: ["готово", "ok"], error: ["ошибка", "err"], cancelled: ["отменена", ""], cancelling: ["отменяется…", "run"], merged: ["объединена", ""] };
const tag = (m, k) => { const [t, c] = m[k] || [k, ""]; return `<span class="chip ${c}">${esc(t)}</span>`; };

async function loadBatches() {
  $("#batchView").classList.add("hidden"); $("#batchList").classList.remove("hidden");
  try {
    const r = await api("/api/batches");
    $("#batchList").innerHTML = `<div class="page-h"><h1>Партии поставок</h1></div>` + (r.batches.length ? `<div class="blist">` +
      r.batches.map((b) => `<div class="bitem" data-id="${esc(b.id)}">
        <div><div class="t">${esc(b.title || b.created)}</div><div class="m">${b.demo ? "демо · " : ""}${b.title ? esc(b.created) : `партия ${esc(b.id)}`}</div></div>
        <div>${b.phase === "cancelling" || b.phase === "cancelled" ? tag(PHASE, b.phase) : b.running ? '<span class="chip run">выполняется</span>' : tag(PHASE, b.phase)}</div>
        <div><div class="t">${b.done}/${b.cities}</div><div class="m">поставок готово</div></div>
        <div><div class="t">${fmt(b.boxes)}</div><div class="m">коробок</div></div>
        <div><div class="t">${esc(b.slot || "—")}</div><div class="m">таймслот</div></div>
        <div>${b.labels ? `<div class="t ${b.labels_done === b.labels ? "good" : ""}">${b.labels_done === b.labels ? "✓ " : ""}${b.labels_done}/${b.labels}</div><div class="m">этикетки наклеены</div>` : ""}</div>
        <div class="arrow">›</div></div>`).join("") + "</div>"
      : `<div class="card"><div class="empty-state">Пока нет созданных партий. Создайте первую во вкладке «Новая поставка».</div></div>`);
    $$("#batchList [data-id]").forEach((a) => (a.onclick = () => showBatch(a.dataset.id)));
  } catch (e) { $("#batchList").innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}
function closeBatch() { openBatch = null; clearTimeout(pollTimer); selSlot = null; }

async function showBatch(id) {
  openBatch = id;
  $("#batchList").classList.add("hidden"); $("#batchView").classList.remove("hidden");
  clearTimeout(pollTimer);
  let b;
  try { b = (await api("/api/batch", { id })).batch; } catch (e) { toast(e.message, true); return; }
  if (openBatch !== id) return;
  renderBatch(b);
  if (b.cancel_result && !b.cancel_result.shown && b.phase !== "cancelling") await showCancelResult(b);
  if (b.running || ["drafts", "supplies", "cargo", "cancelling"].includes(b.phase)) pollTimer = setTimeout(() => showBatch(id), 1500);
  else if (b.phase === "await_slot") pollTimer = setTimeout(() => showBatch(id), 15000);
}

function renderBatch(b) {
  const ships = b.shipments;
  const busy = b.running || b.phase === "cancelling";
  const active = ships.filter((s) => !s.cancelled);
  const units = (s) => s.boxes.reduce((a, x) => a + x.qty, 0);
  const done = active.filter((s) => s.status === "done").length;
  const pct = active.length ? Math.round(done / active.length * 100) : 0;
  const pref = b.slot_pref || {};
  const slotText = b.chosen_slot_human || (b.mode === "auto" ? `желаемо ${dmy(pref.date || pref.date_from)} ${pref.time || pref.time_from || ""}` : b.mode === "none" ? "без таймслота" : "выбор из списка");
  let h = `<a class="back" id="backList">← Все партии</a>
    <div class="card"><div class="bhead"><h2>${b.title ? `${esc(b.title)} <span class="muted small bdate">от ${esc(b.created)}</span>` : `Партия от ${esc(b.created)}`}</h2><button class="icon-sm-plain" id="bRename" title="Переименовать партию" aria-label="Переименовать партию"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></button>${busy && b.phase !== "cancelling" ? '<span class="chip run">выполняется</span>' : tag(PHASE, b.phase)}${b.demo ? '<span class="chip plain">демо</span>' : ""}
    <div class="actions">
      ${busy && b.phase !== "cancelling" ? '<button class="btn" id="bStop">Остановить</button>' : ""}
      ${!busy && ["error", "partial", "stopped", "new"].includes(b.phase) ? '<button class="btn primary" id="bResume">Продолжить</button>' : ""}
      ${!busy && b.phase !== "cancelled" && ships.some((s) => s.order_id && !s.cancelled) ? '<button class="btn soft" id="bSync">Сверить с Озоном</button>' : ""}
      ${!["cancelling", "cancelled"].includes(b.phase) ? '<button class="btn soft" id="bAddSup" title="Добавить в партию поставку, уже созданную в Озоне: её грузоместа, этикетки и штрихкоды войдут в партию">+ Поставка по номеру</button>' : ""}
      ${!busy && (b.phase === "cancelled" || b.phase === "error") ? '<button class="btn primary" id="bAgain">Создать заново</button>' : ""}
      <button class="btn soft" id="bFolder">Открыть папку</button>
      ${b.phase !== "cancelling" && ships.some((s) => s.order_id && !s.cancelled) ? `<button class="btn danger" id="bCancel">${busy ? "Остановить и отменить заявки" : "Отменить заявки"}</button>` : ""}
      ${!busy && !ships.some((s) => s.order_id && !s.cancelled) ? '<button class="btn ghost" id="bDelete">Удалить из истории</button>' : ""}
    </div></div>
    <div class="meta">
      <div class="k"><span>Заявки</span><b>${b.multi ? `одна на все кластеры${b.order?.order_id ? " · № " + esc(ships.find((s) => s.order_number)?.order_number || b.order.order_id) : ""}` : "отдельно на каждый кластер"}</b></div>
      <div class="k"><span>Точка отгрузки</span><b>${esc(b.dropoff?.name || "—")}</b></div>
      <div class="k"><span>Таймслот</span><b>${esc(slotText)}</b></div>
      <div class="k"><span>Пропуск</span><b>${b.pass?.enabled ? "оформляется" : "нет"}</b></div>
      <div class="k"><span>${b.phase === "cancelled" ? "Отменено" : "Готово"}</span><b>${b.phase === "cancelled" ? `${ships.length} из ${ships.length}` : `${done} из ${active.length}`}</b></div>
    </div>
    <div class="progress ${pct === 100 ? "ok" : ""}"><i style="width:${pct}%"></i></div>
    ${b.phase !== "cancelling" && ships.some(canReplace) ? `<div class="banner warn" style="margin:14px 0 0"><b>Озон не принял поставку в: ${ships.filter(canReplace).map((s) => esc(s.name)).join(", ")}.</b> Нажмите ↻ в строке, чтобы попробовать ещё раз, или «Заменить кластер» — можно выбрать любой кластер, в том числе уже выбранный в этой партии.</div>` : ""}
    ${b.phase === "cancelling" ? `<div class="banner" style="margin:14px 0 0">Отменяю заявки в Озоне — это займёт до пары минут. Можно не ждать: итог появится здесь.</div>` : ""}
    ${b.phase === "cancelled" ? `<div class="banner" style="margin:14px 0 0">Все заявки этой партии отменены${b.cancel_result?.at ? " " + esc(b.cancel_result.at) : ""}. Чтобы отправить те же товары снова, нажмите «Создать заново».</div>` : ""}
    ${b.error && b.phase !== "cancelled" ? `<div class="banner err" style="margin:14px 0 0">${esc(b.error)}</div>` : ""}
    ${ships.some((s) => s.mismatch && !s.cancelled) ? `<div class="banner err" style="margin:14px 0 0"><b>Есть поставки, которые едут не в тот кластер</b> — они отмечены красным. Отмените их кнопкой «Отменить» в строке и создайте эти города заново новой партией.</div>` : ""}</div>`;

  if (b.phase === "await_slot" && !b.running) h += slotPicker(b);

  h += `<div class="card"><div class="card-h"><div><h2>Поставки</h2></div></div><div class="scroll"><table class="grid"><thead><tr><th>Кластер</th><th>По данным Озона</th><th>Статус</th><th>Заявка</th><th class="r">Коробок</th><th class="r">Шт</th><th>Таймслот</th><th>Примечания</th><th></th></tr></thead><tbody>` +
    ships.map((s) => { const cl = s.actual_cluster || s.ozon_cluster_name || s.cluster_name || "—"; return `<tr class="${s.mismatch && !s.cancelled ? "mismatch" : ""} ${s.cancelled ? "dim" : ""}"><td><b>${esc(s.name)}</b>${s.imported ? ' <span class="chip plain" title="Поставка создана не этой партией — добавлена по номеру">добавлена</span>' : ""}</td><td class="small">${esc(cl)}${s.mismatch ? ' <span class="err-text">не тот кластер!</span>' : ""}</td><td>${tag(SST, s.status)}</td><td>${esc(s.order_number || s.order_id || "—")}</td><td class="r">${s.boxes.length}</td><td class="r">${fmt(units(s))}</td><td class="small">${s.slot ? esc(humanSlot(s.slot)) : s.no_slot && s.order_id ? '<span class="warn-text">без таймслота</span>' : "—"}</td>
      <td>${typeof s.verified === "number" ? `<div class="good small">✓ состав подтверждён Озоном</div>` : ""}${s.error ? `<div class="err-text">${esc(s.error)}</div>` : ""}${(s.warn || []).map((w) => `<div class="warn-text">${esc(w)}</div>`).join("")}</td>
      <td class="r">${!busy && (!b.multi || s.imported) && s.order_id && !s.cancelled ? `<button class="btn danger sm" data-cancel="${esc(skey(s))}">Отменить</button>` : ""}${canReplace(s) && b.phase !== "cancelling" ? `<span class="row-acts"><button class="btn soft sm icon-sm" data-retry="${esc(skey(s))}" title="Повторить попытку в этот же кластер" aria-label="Повторить попытку"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg></button><button class="btn soft sm" data-repl="${esc(skey(s))}">Заменить кластер</button></span>` : ""}</td></tr>`; }).join("") + `</tbody></table></div>${b.synced_at ? `<div class="muted small" style="margin-top:8px">Сверено с Озоном ${esc(b.synced_at)}</div>` : ""}</div>`;

  h += barcodesCard(b);
  const groups = b.groups || [];
  const files = b.files;
  h += `<div class="card"><div class="card-h"><div><h2>Этикетки</h2><p class="sub">${files ? "Один файл — одинаковые коробки. Печатайте и клейте на любую коробку этого состава." : "Появятся, когда поставки будут созданы"}</p></div><div class="actions">${files?.all ? `<a class="btn soft" target="_blank" href="${fileUrl(b, files.all)}">Все этикетки</a>` : ""}${files?.packing ? `<a class="btn soft" target="_blank" href="${fileUrl(b, files.packing)}">Лист сборки</a>` : ""}${files && !busy ? '<button class="btn ghost" id="bCheckLabels">Проверить этикетки</button>' : ""}${files ? '<button class="btn ghost" id="bLabelsFolder">Папка</button>' : ""}</div></div>`;
  const chk = b.labels_check;
  if (chk && files) h += `<div class="banner ${chk.ok ? "" : "err"}" style="margin:0 0 12px">${chk.ok ? "✓ " : ""}<b>Проверка этикеток:</b> ${esc(chk.summary)}${chk.at ? ` <span class="muted small">(${esc(chk.at)})</span>` : ""}
      ${!chk.ok && (chk.problems || []).length ? `<ul style="margin:8px 0 0">${chk.problems.slice(0, 12).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
      ${!chk.ok && !busy ? `<div class="row"><button class="btn danger sm" id="bRelabel">Перезапросить этикетки</button><span class="muted small">По одной на каждое грузоместо — так ошибка исключена. Старые файлы этикеток не печатайте.</span></div>` : ""}</div>`;
  const lDone = b.labels_done || {};
  const nDone = files ? groups.filter((g) => lDone[g.id]).length : 0;
  if (files && groups.length) h += `<div class="done-sum ${nDone === groups.length ? "all" : ""}">${nDone === groups.length ? "✓ Все этикетки наклеены" : `Готово: ${nDone} из ${groups.length}`}</div>`;
  h += `<div class="files-grid">` + groups.map((g, i) => {
    const f = (files?.groups || []).find((x) => x.id === g.id);
    const dn = lDone[g.id];
    const mark = files ? `<label class="done-tg" title="Отметьте, когда этикетки этого состава напечатаны и наклеены"><input type="checkbox" data-done="${esc(g.id)}" ${dn ? "checked" : ""}> ${dn ? `Готово · ${esc(dn.at)}` : "Готово"}</label>` : "";
    const inner = `<div class="pdf">PDF</div><div><div class="fn">${esc(g.name)} × ${fmt(g.qty)} шт</div><div class="fm">${g.total} кор · ${g.cities.map((c) => esc(c.name) + (c.count > 1 ? ` ×${c.count}` : "")).join(", ")}${f?.missing ? `<br><span class="warn-text">не готово для ${f.missing} пост.</span>` : ""}</div></div><div class="num">${String(i + 1).padStart(2, "0")}</div>`;
    const card = f?.file ? `<a class="fcard" target="_blank" href="${fileUrl(b, f.file)}">${inner}</a>` : `<div class="fcard off">${inner}</div>`;
    return `<div class="fwrap ${dn ? "done" : ""}">${card}${mark}</div>`;
  }).join("") + `</div></div>`;

  h += `<details class="card" ${b.phase === "error" || b.phase === "partial" || b.running ? "open" : ""}><summary>Журнал</summary><div class="log" id="logBox">${(b.log || []).map((l) => `<div class="${l.lvl}"><span class="t">${esc(l.t)}</span> ${l.city ? "<b>" + esc(l.city) + "</b> · " : ""}${esc(l.msg)}</div>`).join("") || '<span class="muted">пусто</span>'}</div><div class="muted small" style="margin-top:8px">Подробный журнал запросов к Ozon — файл api.log в папке партии.</div></details>`;
  const keepOpen = $("#batchView details")?.open;
  $("#batchView").innerHTML = h;
  if (keepOpen !== undefined) $("#batchView details").open = keepOpen;
  const lb = $("#logBox"); lb.scrollTop = lb.scrollHeight;

  $("#backList").onclick = () => { closeBatch(); loadBatches(); };
  $("#bStop") && ($("#bStop").onclick = async () => { await api("/api/batch/stop", { id: b.id }); toast("Останавливаю после текущего запроса…"); });
  $("#bResume") && ($("#bResume").onclick = async () => { await api("/api/batch/resume", { id: b.id }); showBatch(b.id); });
  $("#bFolder").onclick = () => api("/api/open", { id: b.id }).catch((e) => toast(e.message, true));
  $("#bSync") && ($("#bSync").onclick = async () => {
    $("#bSync").disabled = true; $("#bSync").textContent = "Сверяю…";
    try { const r = await api("/api/batch/sync", { id: b.id }); toast(r.mismatch ? `Не в тот кластер: ${r.mismatch} пост. — отмечены красным` : "Все поставки едут в свои кластеры", !!r.mismatch); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  });
  $$("[data-repl]").forEach((el) => (el.onclick = () => askReplace(b, ships.find((x) => skey(x) === el.dataset.repl))));
  $("#bAddSup") && ($("#bAddSup").onclick = async () => {
    promptBox._v = "";
    const ok = await confirmBox(`<h3>Добавить поставку по номеру</h3>
      <input id="promptIn" placeholder="Например: 2000075570670" autocomplete="off">
      <p class="muted small">Номер заявки из кабинета Озона или с этикетки грузоместа (пробелы не важны), можно ID заявки или номер поставки.
      Несколько — через запятую. Программа возьмёт из Озона состав и грузоместа (если их нет — создаст сама),
      скачает этикетки, и поставка войдёт в «Этикетки», «По товарам» и лист сборки.</p>`, "Добавить");
    const numbers = (promptBox._v || "").trim(); promptBox._v = "";
    if (!ok || !numbers) return;
    toast("Ищу у Озона…");
    try {
      const r = await api("/api/batch/add_supply", { id: b.id, numbers });
      toast(`Добавлено: ${r.added.join(", ")}` + (r.errors.length ? `. Не добавлено: ${r.errors.join("; ")}` : ""), !!r.errors.length);
    } catch (e) { toast(e.message, true); }
    showBatch(b.id);
  });
  $("#bRename").onclick = async () => {
    promptBox._v = b.title || "";
    const ok = await confirmBox(`<h3>Название партии</h3><input id="promptIn" maxlength="120" placeholder="Например: Карты памяти, неделя 48" value="${esc(b.title || "")}"><p class="muted small">Пустое название — снова «Партия от ${esc(b.created)}».</p>`, "Сохранить");
    const title = (promptBox._v || "").trim(); promptBox._v = "";
    if (!ok) return;
    try { await api("/api/batch/rename", { id: b.id, title }); } catch (e) { toast(e.message, true); }
    showBatch(b.id);
  };
  $$("[data-retry]").forEach((el) => (el.onclick = async () => {
    el.disabled = true; el.classList.add("spin");
    try { await api("/api/batch/retry", { id: b.id, key: el.dataset.retry }); toast("Пробую ещё раз…"); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  }));
  const ask = ships.find((s) => canReplace(s) && !PROMPTED.has(`${b.id}|${skey(s)}|${s.error}`));
  if (ask && b.phase !== "cancelling" && $("#modal").classList.contains("hidden")) {
    PROMPTED.add(`${b.id}|${skey(ask)}|${ask.error}`);
    setTimeout(() => askReplace(b, ask), 300);
  }
  $$("[data-cancel]").forEach((el) => (el.onclick = async () => {
    const s = ships.find((x) => skey(x) === el.dataset.cancel);
    if (!(await confirmBox(`<h3>Отменить заявку «${esc(s.name)}» в Озоне?</h3><p>Заявка ${esc(s.order_number || s.order_id)} будет отменена. Это действие нельзя отменить.</p>`, "Отменить заявку"))) return;
    try { await api("/api/batch/cancel", { id: b.id, cluster_ids: [skey(s)] }); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  }));
  $$("[data-bcup]").forEach((el) => (el.onclick = () => { const f = $("#bcFile"); f.dataset.sku = el.dataset.bcup; f.click(); }));
  $("#bcFile") && ($("#bcFile").onchange = async (e) => {
    const f = e.target.files[0], sku = e.target.dataset.sku; e.target.value = "";
    if (!f) return;
    try {
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
      const r = await api("/api/product_label/upload", { sku, data });
      toast(`Этикетка загружена (${r.size[0]}×${r.size[1]} мм)`);
    } catch (err) { toast(err.message, true); }
    showBatch(b.id);
  });
  $$("[data-done]").forEach((el) => (el.onchange = async () => {
    try { await api("/api/batch/label_done", { id: b.id, group: el.dataset.done, done: el.checked }); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  }));
  $("#bLabelsFolder") && ($("#bLabelsFolder").onclick = () => api("/api/open", { id: b.id, sub: "Этикетки" }).catch((e) => toast(e.message, true)));
  $("#bCancel") && ($("#bCancel").onclick = async () => {
    const n = new Set(ships.filter((s) => s.order_id && !s.cancelled).map((s) => s.order_id)).size;
    if (!(await confirmBox(`<h3>Отменить ${n === 1 ? "заявку" : `${n} заявок`} этой партии в Озоне?</h3><p>${busy ? "Создание поставок будет остановлено. " : ""}Это действие нельзя отменить.</p>`, "Отменить заявки"))) return;
    $("#bCancel").disabled = true; $("#bCancel").textContent = busy ? "Останавливаю…" : "Отменяю…";
    try { await api("/api/batch/cancel", { id: b.id }); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  });
  $("#bAgain") && ($("#bAgain").onclick = () => recreate(b));
  $("#bCheckLabels") && ($("#bCheckLabels").onclick = async () => {
    $("#bCheckLabels").disabled = true; $("#bCheckLabels").textContent = "Проверяю…";
    try { const r = await api("/api/batch/check_labels", { id: b.id }); toast(r.check.ok ? "Этикетки в порядке" : "Найдены проблемы с этикетками", !r.check.ok); }
    catch (e) { toast(e.message, true); }
    showBatch(b.id);
  });
  $("#bRelabel") && ($("#bRelabel").onclick = async () => {
    const keys = (b.labels_check || {}).bad_ships || [];
    const ok = await confirmBox(`<h3>Перезапросить этикетки?</h3><p>${keys.length ? `Для поставок с проблемой (${keys.length})` : "Для всех поставок партии"} этикетки будут получены заново — по одной на каждое грузоместо. Файлы этикеток обновятся, распечатанные раньше лучше не использовать.</p>`, "Перезапросить");
    if (!ok) return;
    try { await api("/api/batch/relabel", { id: b.id, keys }); } catch (e) { toast(e.message, true); }
    showBatch(b.id);
  });
  $("#bDelete") && ($("#bDelete").onclick = async () => {
    if (!(await confirmBox(`<h3>Удалить партию из истории?</h3><p>Партия и её файлы этикеток будут удалены с компьютера. В Озоне ничего не изменится.</p>`, "Удалить"))) return;
    try { await api("/api/batch/delete", { id: b.id }); toast("Партия удалена"); closeBatch(); loadBatches(); }
    catch (e) { toast(e.message, true); }
  });
  $$(".slot").forEach((el) => (el.onclick = () => { selSlot = JSON.parse(el.dataset.slot); $$(".slot").forEach((x) => x.classList.toggle("sel", x === el)); $("#slotGo").disabled = false; $("#slotSel").innerHTML = `Выбран: <b>${esc(el.dataset.human)}</b>`; }));
  if (selSlot) { const el = $$(".slot").find((x) => x.dataset.slot === JSON.stringify(selSlot)); if (el) el.click(); }
  $("#slotGo") && ($("#slotGo").onclick = async () => {
    if (!selSlot) return;
    await api("/api/batch/slot", { id: b.id, slot: selSlot }); selSlot = null; showBatch(b.id);
  });
  $("#slotRefresh") && ($("#slotRefresh").onclick = async () => { await api("/api/batch/resume", { id: b.id, reset_slot: true }); showBatch(b.id); });
}
const skey = (s) => s.key || String(s.cluster_id);
const canReplace = (s) => !s.cancelled && !s.order_id && (s.status === "error" || s.excluded);
const PROMPTED = new Set();
async function askReplace(b, s) {
  if (!s) return;
  if (!(APP.clusters || []).length) await loadClusters();
  const others = b.shipments.filter((x) => !x.cancelled && x !== s);
  const inBatch = new Set(others.map((x) => String(x.cluster_id)));
  const all = (APP.clusters || []).filter((c) => String(c.id) !== String(s.cluster_id));
  const opt = (c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`;
  const ok = await confirmBox(`<h3>Озон не принимает поставку в «${esc(s.name)}»</h3>
    <p class="err-text">${esc(s.error || "Озон отказал")}</p>
    <p>Куда отправить эти товары (${s.boxes.length} кор., ${fmt(s.boxes.reduce((a, x) => a + x.qty, 0))} шт)?</p>
    <select id="replSel" style="width:100%">
      <optgroup label="Уже в этой партии — добавить к ней или отдельной заявкой">${all.filter((c) => inBatch.has(String(c.id))).map(opt).join("")}</optgroup>
      <optgroup label="Другие кластеры">${all.filter((c) => !inBatch.has(String(c.id))).map(opt).join("")}</optgroup>
    </select>
    <p class="muted small">${b.running ? "Поставки ещё создаются — замена войдёт в этот же запуск." : "Программа сразу создаст поставку в выбранный кластер."}</p>`, "Заменить");
  const sel = $("#replSel");
  if (!ok || !sel || !sel.value) return;
  const c = all.find((x) => String(x.id) === sel.value);
  const dsts = others.filter((x) => String(x.cluster_id) === String(c.id));
  if (dsts.length) {                                  // кластер уже в партии — добавить к нему?
    const d = dsts.find((x) => x.order_id) || dsts[0];
    const units = (x) => x.boxes.reduce((a, y) => a + y.qty, 0);
    const pick = await choiceBox(`<h3>«${esc(c.name)}» уже есть в партии</h3>
      <p>Добавить товары «${esc(s.name)}» (${s.boxes.length} кор., ${fmt(units(s))} шт) в поставку «${esc(d.name)}» (${d.boxes.length} кор., ${fmt(units(d))} шт)${d.order_id ? ` — заявка ${esc(d.order_number || d.order_id)} уже создана, программа изменит её состав в Озоне` : ""}?</p>
      <div class="choice">
        <button class="btn soft" data-choice="keep"><b>Грузоместа как есть</b><span>коробки «${esc(s.name)}» добавляются к «${esc(d.name)}» без изменений — допечатать только их этикетки</span></button>
        <button class="btn soft" data-choice="sum"><b>Сложить одинаковые товары</b><span>одинаковый товар из двух кластеров — в одно новое грузоместо (32 ГБ: 10 + 5 = 15 шт в одной коробке); этикетки «${esc(d.name)}» печатаются заново</span></button>
        <button class="btn ghost" data-choice="separate"><b>Отдельной заявкой</b><span>в «${esc(c.name)}» уйдёт ещё одна заявка, как раньше</span></button>
      </div>
      ${d.order_id ? `<p class="muted small">Если Озон не даст изменить состав заявки, программа отменит заявку «${esc(d.name)}» и создаст новую с общим составом — будет новый номер, таймслот и все этикетки «${esc(d.name)}» заново. Об этом будет предупреждение.</p>` : ""}`);
    if (!pick) return;
    if (pick !== "separate") {
      try {
        const r = await api("/api/batch/merge", { id: b.id, key: skey(s), to: skey(d), mode: pick });
        toast(`«${s.name}» → в «${d.name}»` + (r.queued ? ": войдёт в текущий запуск" : ""));
      } catch (e) { toast(e.message, true); }
      return showBatch(b.id);
    }
  }
  try {
    const r = await api("/api/batch/replace", { id: b.id, key: skey(s), cluster_id: c.id, cluster_name: c.name });
    toast(r.queued ? `«${s.name}» → «${c.name}»: замена войдёт в текущий запуск` : `«${s.name}» → «${c.name}»: создаю поставку`);
  } catch (e) { toast(e.message, true); }
  showBatch(b.id);
}
// окно с несколькими вариантами: кнопки с data-choice, возвращает выбранный или null
function choiceBox(html) {
  return new Promise((res) => {
    const done = (v) => { $("#modal").classList.add("hidden"); $("#modalBody").removeEventListener("click", on); $("#modalNo").onclick = null; res(v); };
    const on = (e) => { const t = e.target.closest("[data-choice]"); if (t) done(t.dataset.choice); };
    $("#modalBody").innerHTML = html;
    $("#modalYes").classList.add("hidden"); $("#modalNo").classList.remove("hidden"); $("#modalNo").textContent = "Отмена";
    $("#modalBody").addEventListener("click", on);
    $("#modalNo").onclick = () => done(null);
    $("#modal").classList.remove("hidden");
  }).finally(() => $("#modalYes").classList.remove("hidden"));
}
async function showCancelResult(b) {
  const r = b.cancel_result;
  const ok = r.ok || [], bad = r.failed || [], skip = r.skipped || [];
  const title = !bad.length ? "Заявки на поставку отменены" : ok.length ? "Отменены не все заявки" : "Заявки не отменены";
  await confirmBox(`<h3>${title}</h3>
    ${ok.length ? `<p>Отменено заявок в Озоне: <b>${ok.length}</b> — ${ok.map(esc).join(", ")}.</p>` : ""}
    ${skip.length ? `<p class="muted">Ещё не были созданы и сняты с очереди: ${skip.length} — ${skip.map(esc).join(", ")}.</p>` : ""}
    ${bad.length ? `<p class="err-text">Не удалось отменить: ${bad.map((x) => `<br>• ${esc(x.name)} — ${esc(x.error)}`).join("")}</p><p class="muted small">Можно нажать «Отменить заявки» ещё раз или отменить их в личном кабинете Озона.</p>` : ""}`, "ОК", true);
  await api("/api/batch/ack", { id: b.id });
  if (b.phase === "cancelled") { closeBatch(); loadBatches(); }
}
function recreate(b) {
  const src = b.source;
  if (src && (src.rows || src.cities)) {
    D = migrateDraft(Object.assign(newDraft(), JSON.parse(JSON.stringify(src)), { cv: 2 }));
  } else {                                   // партии из старых версий — собираем из поставок
    D = newDraft();
    D.products = b.products.map((p) => ({ sku: p.sku, qty: 0, name: p.name, short: p.short, box_size: p.box_size, multiple: !!(APP.profile.products[p.sku] || {}).multiple }));
    D.rows = b.shipments.map((s) => ({ cluster_id: s.cluster_id, cluster_name: s.cluster_name || s.name, share: 0, fixed: {} }));
    for (const s of b.shipments) {
      const r = D.rows.find((x) => x.cluster_id === s.cluster_id);
      for (const p of b.products) {
        const q = s.boxes.filter((x) => x.sku === p.sku).reduce((a, x) => a + x.qty, 0);
        r.fixed[p.sku] = q; D.products.find((x) => x.sku === p.sku).qty += q;
      }
    }
    const tot = D.products.reduce((a, p) => a + p.qty, 0) || 1;
    for (const r of D.rows) r.share = Math.round(totalOf(r.fixed) / tot * 1000) / 10;
    D.dropoff = b.dropoff || D.dropoff; D.multi = !!b.multi;
  }
  saveDraft(); closeBatch(); showTab("new"); renderNew();
  toast("Форма заполнена данными партии — проверьте и нажмите «Создать поставки»");
}
function barcodesCard(b) {
  const list = b.barcodes || [];
  if (!list.length) return "";
  const done = b.labels_done || {};
  const nDone = list.filter((x) => done["sku:" + x.sku]).length;
  return `<div class="card"><div class="card-h"><div><h2>По товарам</h2><p class="sub">Штрихкоды и этикетки грузомест одного товара. Страницы в обратном порядке и перевёрнуты: в начале файла — город с самым большим количеством.</p></div><div class="actions"><span class="muted small nowrap">${nDone === list.length ? '<span class="good">✓ Все наклеены</span>' : `Готово: ${nDone} из ${list.length}`}</span></div></div>
    <div class="bc-list">${list.map((x) => {
      const dn = done["sku:" + x.sku];
      return `<div class="bc-row ${dn ? "done" : ""}">
        <div class="bc-n"><b>${esc(x.name)}</b> <span class="muted small">SKU ${esc(x.sku)}${x.offer_id ? " · " + esc(x.offer_id) : ""}</span></div>
        <div class="muted small bc-m">${fmt(x.units)} шт · ${x.cities} ${x.cities % 10 === 1 && x.cities % 100 !== 11 ? "город" : [2, 3, 4].includes(x.cities % 10) && ![12, 13, 14].includes(x.cities % 100) ? "города" : "городов"}${(x.failed || []).length ? `<div class="warn-text" title="Пока заявки в Озоне нет, город в штрихкоды и грузоместа не входит. Как только заявка создастся (или кластер заменят), файлы сами станут полными">пока без: ${x.failed.map(esc).join(", ")} — заявка не создана</div>` : ""}${dn && dn.sig !== undefined && dn.sig !== x.sig ? `<div class="err-text" title="Отметка «Готово» стояла на прежнем составе">состав изменился после отметки — скачайте и допечатайте штрихкоды</div>` : ""}</div>
        <div class="bc-a">${x.has_label && !x.cities
          ? `<button class="btn soft sm" disabled title="Ни одна заявка с этим товаром не создана">Штрихкоды</button>`
          : x.has_label
          ? `<span class="bc-pair"><a class="btn soft sm" target="_blank" href="/barcodes/${encodeURIComponent(b.id)}/${encodeURIComponent(x.sku)}">Штрихкоды</a><a class="link small" data-bcup="${esc(x.sku)}" title="Загрузить другую этикетку товара">заменить</a></span>`
          : `<button class="btn primary sm" data-bcup="${esc(x.sku)}" title="Скачайте этикетку товара в кабинете Озона и загрузите сюда — один раз для этого товара">Загрузить этикетку</button>`}
        ${x.cargo
          ? `<a class="btn soft sm" target="_blank" href="/cargo/${encodeURIComponent(b.id)}/${encodeURIComponent(x.sku)}" title="Этикетки грузомест этого товара из Озона, на каждой — сколько в ней штук">Грузоместа</a>${x.cargo_missing ? `<span class="warn-text small" title="Этикетки ещё не получены для ${x.cargo_missing} пост.">не все</span>` : ""}`
          : `<button class="btn soft sm" disabled title="Этикетки грузомест появятся, когда поставки будут созданы">Грузоместа</button>`}
        <label class="done-tg" title="Штрихкоды этого товара наклеены"><input type="checkbox" data-done="sku:${esc(x.sku)}" ${dn ? "checked" : ""}>${dn ? esc(dn.at) : ""}</label></div></div>`;
    }).join("")}</div><input type="file" id="bcFile" accept=".pdf,application/pdf" class="hidden"></div>`;
}
function fileUrl(b, rel) { return `/files/${encodeURIComponent(b.id)}/${rel.split("/").map(encodeURIComponent).join("/")}`; }
function humanSlot(s) {
  const a = new Date(s[0].slice(0, 16)), z = new Date(s[1].slice(0, 16));
  const d = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"][a.getDay()];
  const p = (n) => String(n).padStart(2, "0");
  return `${p(a.getDate())}.${p(a.getMonth() + 1)} ${d} ${p(a.getHours())}:${p(a.getMinutes())}–${p(z.getHours())}:${p(z.getMinutes())}`;
}
function slotPicker(b) {
  const opts = b.slot_options || [];
  const total = b.slot_total || 0;
  const byDay = {};
  for (const o of opts) (byDay[o.slot[0].slice(0, 10)] = byDay[o.slot[0].slice(0, 10)] || []).push(o);
  const left = b.slot_deadline ? Math.max(0, Math.round((b.slot_deadline * 1000 - Date.now()) / 60000)) : null;
  return `<div class="card"><div class="card-h"><div><h2>${b.multi ? "Выберите таймслот заявки" : "Выберите таймслот — один для всех поставок"}</h2><p class="sub">${b.multi ? "Слоты общей заявки на все города" : `Бледные — доступны не всем (${total} поставок). Если выбрать такой, остальным возьмётся ближайший.`}</p></div><div class="actions muted small">${left !== null ? `Черновики Озона живут 30 минут: осталось ~${left} мин. Потом программа пересоздаст их сама.` : ""}</div></div>
    <div class="slots">${Object.entries(byDay).map(([d, list]) => `<div class="slotday"><div class="d">${esc(humanSlot(list[0].slot).slice(0, 5))} <span>${esc(humanSlot(list[0].slot).slice(6, 8))}</span></div>${list.map((o) => `<button class="slot ${o.count < total ? "part" : ""}" data-slot='${esc(JSON.stringify(o.slot))}' data-human="${esc(o.human)}"><span>${esc(o.human.slice(9))}</span><span class="n">${b.multi ? "" : o.count + "/" + total}</span></button>`).join("")}</div>`).join("") || '<div class="muted">Свободных таймслотов нет.</div>'}</div>
    <div class="row" style="margin-top:16px"><span class="muted" id="slotSel">Таймслот не выбран</span><span style="flex:1"></span><button class="btn ghost" id="slotRefresh">↻ Обновить слоты</button><button class="btn primary lg" id="slotGo" disabled>Создать поставки →</button></div></div>`;
}

/* ================= НАСТРОЙКИ ================= */
function renderSettings() {
  const P = APP.profile;
  $("#sName").value = P.name || ""; $("#sClient").value = P.client_id || "";
  $("#sKey").value = ""; $("#sKey").placeholder = P.api_key_set ? "сохранён — введите новый, чтобы заменить" : "";
  $("#sDemo").checked = !!P.demo;
  const favs = P.dropoffs || [];
  $("#sDropoffs").innerHTML = favs.length ? favs.map((f, i) => { const def = P.dropoff && P.dropoff.warehouse_id === f.warehouse_id; return `<div class="shop-row"><div><b>${esc(f.name)}</b><div class="muted small">${esc(f.address || "")}</div></div>${def ? '<span class="chip ok">основная</span>' : ""}<div class="acts">${def ? "" : `<a class="link" data-def="${i}">Сделать основной</a>`}<a class="link" style="color:var(--err)" data-rm="${i}">Убрать</a></div></div>`; }).join("")
    : `<div class="muted">Пока нет избранных точек.</div>`;
  $$("#sDropoffs [data-def]").forEach((a) => (a.onclick = () => favAction({ action: "default", item: favs[+a.dataset.def] })));
  $$("#sDropoffs [data-rm]").forEach((a) => (a.onclick = () => favAction({ action: "remove", item: favs[+a.dataset.rm] })));
  renderPending();
  $("#dataDir").textContent = APP.dataDir || "";
  const V = APP.ver || {};
  $("#aboutSub").textContent = `Версия ${V.version} от ${dmy(V.date)} · исходный код: github.com/${V.repo}`;
  $("#changeLog").innerHTML = (V.changes || []).map((c) => `<div class="ver"><b>${esc(c.version)}</b><span>${esc(dmy(c.date))}</span><ul>${c.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`).join("");
  const ps = P.pass || {};
  $("#pOn").checked = !!ps.enabled; $("#pName").value = ps.driver_name || ""; $("#pPhone").value = ps.driver_phone || "";
  $("#pModel").value = ps.vehicle_model || ""; $("#pNum").value = ps.vehicle_number || "";
  const ts = P.timeslot || {};
  $("#tMode").value = ts.mode || "auto"; $("#tDays1").value = ts.days_from ?? 2; $("#tT1").value = ts.time_from || "10:00";
  const prods = Object.entries(P.products || {});
  $("#sProducts").innerHTML = prods.length ? `<thead><tr><th>SKU</th><th>Артикул</th><th>Название</th><th>Коротко</th><th class="r">В коробке</th><th class="c">Кратно</th><th></th></tr></thead><tbody>` +
    prods.map(([sku, p]) => `<tr data-sku="${esc(sku)}"><td>${esc(sku)}</td><td class="small">${esc(p.offer_id || "—")}</td><td class="pname">${esc(p.name || "")}</td><td><input class="sp-short" value="${esc(p.short || "")}"></td><td><input class="sp-box" type="number" min="0" value="${p.box_size || ""}" style="width:80px"></td><td class="c"><input type="checkbox" class="sp-mult" ${p.multiple ? "checked" : ""}></td><td><button class="x">×</button></td></tr>`).join("") + "</tbody>"
    : `<tr class="empty"><td>Товары появятся здесь после первого ввода SKU.</td></tr>`;
  $("#sShops").innerHTML = APP.profiles.map((p) => `<div class="shop-row"><b>${esc(p.name)}</b>${p.demo ? ' <span class="chip plain">демо</span>' : ""}${p.id === P.id ? ' <span class="chip ok">текущий</span>' : ""}<div class="acts">${p.id !== P.id ? `<a class="link" data-sel="${p.id}">Выбрать</a><a class="link" style="color:var(--err)" data-del="${p.id}">Удалить</a>` : ""}</div></div>`).join("");
  $$("#sShops [data-sel]").forEach((a) => (a.onclick = async () => { await api("/api/profile/select", { id: a.dataset.sel }); APP.clusters = null; await init(); showTab("settings"); }));
  $$("#sShops [data-del]").forEach((a) => (a.onclick = async () => {
    if (!(await confirmBox("<h3>Удалить магазин из программы?</h3><p>Настройки этого магазина будут удалены. Партии останутся в папке data.</p>", "Удалить"))) return;
    await api("/api/profile/delete", { id: a.dataset.del }); await init(); showTab("settings");
  }));
}
$("#sSave").onclick = async () => {
  try {
    const part = { name: $("#sName").value.trim() || "Магазин", client_id: $("#sClient").value.trim(), demo: $("#sDemo").checked };
    if ($("#sKey").value.trim()) part.api_key = $("#sKey").value.trim();
    await saveProfile(part); await loadState(); renderSettings();
    $("#sMsg").textContent = "Сохранено"; APP.clusters = null; loadClusters().then(() => { if (D) { bindRows(); renderDistControls(); replan(); } }); resolveDropoffs();
  } catch (e) { toast(e.message, true); }
};
$("#sCheck").onclick = async () => {
  $("#sMsg").textContent = "Проверяю…";
  try {
    const r = await api("/api/check");
    $("#sMsg").innerHTML = `<span class="good" style="color:var(--ok)">Подключение работает.</span> Роли: ${esc(r.roles.join(", ") || "—")}` + (r.missing.length ? `<br><span class="warn-text">Ключу не хватает методов: ${esc(r.missing.join(", "))}</span>` : "");
  } catch (e) { $("#sMsg").innerHTML = `<span class="err-text">${esc(e.message)}</span>`; }
};
$("#sDropFind").onclick = () => searchDropoff($("#sDropQ").value, $("#sDropRes"), async (x) => {
  await favAction({ action: "add", item: x, make_default: !APP.profile.dropoff }); $("#sDropRes").innerHTML = ""; $("#sDropQ").value = "";
  toast(`«${x.name}» добавлена в избранные точки`);
});
async function favAction(body) {
  try {
    const r = await api("/api/dropoff/fav", body); APP.profile = r.profile;
    if (D && !D.dropoff && APP.profile.dropoff) { D.dropoff = APP.profile.dropoff; saveDraft(); }
    renderSettings(); if (D) renderShipping();
  } catch (e) { toast(e.message, true); }
}
let PENDING = [];
function renderPending() {
  $("#sPending").innerHTML = PENDING.map((p, i) => `<div class="pend"><div class="ph">Не удалось однозначно найти «${esc(p.seed)}»${p.error ? ` — ${esc(p.error)}` : p.candidates.length ? " — выберите нужную:" : " — ничего не найдено, поищите вручную ниже"}<a class="link" data-skip="${i}">Не искать</a></div>
    ${p.candidates.length ? `<div class="list">${p.candidates.map((c, j) => `<div class="it" data-p="${i}" data-c="${j}">${esc(c.name)}<div class="muted">${esc(c.address || "")} · ${esc((c.warehouse_type || "").replace("WAREHOUSE_TYPE_", ""))}</div></div>`).join("")}</div>` : ""}</div>`).join("");
  $$("#sPending [data-c]").forEach((el) => (el.onclick = async () => { const p = PENDING[+el.dataset.p]; PENDING.splice(+el.dataset.p, 1); await favAction({ action: "add", item: p.candidates[+el.dataset.c], seed: p.seed }); }));
  $$("#sPending [data-skip]").forEach((el) => (el.onclick = async () => { const p = PENDING[+el.dataset.skip]; PENDING.splice(+el.dataset.skip, 1); await favAction({ action: "none", seed: p.seed }); }));
}
async function resolveDropoffs() {
  const P = APP.profile;
  if (!(P.dropoff_seeds || []).length && (P.dropoffs || []).length) return;
  if (!P.demo && !(P.client_id && P.api_key_set)) return;
  try {
    const r = await api("/api/dropoff/resolve"); APP.profile = r.profile; PENDING = r.pending;
    if (D && !D.dropoff && APP.profile.dropoff) { D.dropoff = APP.profile.dropoff; saveDraft(); }
    if (D) renderShipping();
    if (!$("#tab-settings").classList.contains("hidden")) renderSettings();
  } catch (e) { /* не критично */ }
}
$("#sDropQ").onkeydown = (e) => { if (e.key === "Enter") $("#sDropFind").click(); };
$("#pSave").onclick = async () => {
  await saveProfile({ pass: { enabled: $("#pOn").checked, driver_name: $("#pName").value.trim(), driver_phone: $("#pPhone").value.trim(), vehicle_model: $("#pModel").value.trim(), vehicle_number: $("#pNum").value.trim().toUpperCase() } });
  $("#pMsg").textContent = "Сохранено"; if (D) { D.passOn = APP.profile.pass.enabled; saveDraft(); renderShipping(); }
};
$("#tSave").onclick = async () => {
  await saveProfile({ timeslot: { mode: $("#tMode").value, days_from: num($("#tDays1").value), time_from: $("#tT1").value } });
  $("#tMsg").textContent = "Сохранено";
};
$("#sProducts").addEventListener("change", async (e) => {
  const tr = e.target.closest("tr"); if (!tr) return;
  const prods = APP.profile.products; const p = prods[tr.dataset.sku];
  if (e.target.classList.contains("sp-short")) p.short = e.target.value.trim();
  if (e.target.classList.contains("sp-box")) p.box_size = num(e.target.value) || null;
  if (e.target.classList.contains("sp-mult")) p.multiple = e.target.checked;
  await saveProfile({ products: prods });
  if (D) for (const d of D.products) if (d.sku === tr.dataset.sku) { d.short = p.short; d.box_size = p.box_size; d.multiple = p.multiple; }
});
$("#sProducts").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("x")) return;
  const prods = APP.profile.products; delete prods[e.target.closest("tr").dataset.sku];
  await saveProfile({ products: prods }); renderSettings();
});
async function checkUpdate(force, quiet) {
  if (!quiet) $("#updRes").innerHTML = '<div class="upd">Проверяю…</div>';
  try {
    const r = await api("/api/update", { force: !!force });
    const box = r.ok ? (r.newer ? (APP.server
      ? `<div class="upd new"><b>Доступна версия ${esc(r.latest)}</b><span class="muted">обновляется на сервере</span><a class="link" href="${esc(r.page)}" target="_blank" rel="noopener">Что нового</a></div>`
        + (r.busy ? `<div class="upd warn-text">Сейчас создаются поставки — обновляйте после того, как партия закончится</div>` : "")
      : `<div class="upd new"><b>Доступна версия ${esc(r.latest)}</b><a class="btn primary sm" href="${esc(r.url)}" target="_blank" rel="noopener">Скачать</a><a class="link" href="${esc(r.page)}" target="_blank" rel="noopener">Что нового</a></div>`)
      : `<div class="upd">У вас последняя версия (${esc(APP.ver.version)})</div>`)
      : `<div class="upd">Не удалось проверить обновления: ${esc(r.error)}</div>`;
    if (!quiet || r.newer) $("#updRes").innerHTML = box;
    const f = $("#verUpd");
    if (r.ok && r.newer) {
      f.textContent = (APP.server ? `доступна ${r.latest}` : `доступна ${r.latest} — скачать`) + (r.busy ? " · не обновляйте, пока создаются поставки" : "");
      f.classList.toggle("warn-text", !!r.busy); f.href = APP.server ? r.page : r.url;
    } else { f.textContent = ""; f.removeAttribute("href"); }
  } catch (e) { if (!quiet) $("#updRes").innerHTML = `<div class="upd">${esc(e.message)}</div>`; }
}
$("#updCheck").onclick = () => checkUpdate(true);
$("#dExport").onclick = async () => {
  const a = document.createElement("a");
  a.href = "/export"; document.body.appendChild(a); a.click(); a.remove();
  toast("Файл с настройками сохранён в «Загрузки»");
};
$("#dImport").onclick = () => $("#dFile").click();
$("#dFile").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    if (!(await confirmBox(`<h3>Загрузить настройки из файла?</h3><p>Магазинов в файле: ${(data.profiles || []).length}. Магазины с теми же названиями-ID будут заменены, остальные сохранятся.</p>`, "Загрузить"))) return;
    const r = await api("/api/import", { data });
    toast(`Загружено магазинов: ${r.imported}`); APP.clusters = null; await init(); showTab("settings");
  } catch (err) { toast(err instanceof SyntaxError ? "Это не файл настроек" : err.message, true); }
};
$("#dOpen").onclick = () => api("/api/open", { id: "__data__" }).catch((e) => toast(e.message, true));
$("#dFind").onclick = async () => {
  $("#dFound").innerHTML = '<div class="muted">Ищу на компьютере… это может занять до 20 секунд</div>';
  try {
    const r = await api("/api/legacy/find");
    $("#dFound").innerHTML = r.found.length ? r.found.map((x, i) => `<div class="it" data-i="${i}">${esc(x.path)}<div class="muted">изменено ${esc(x.date)} · нажмите, чтобы перенести</div></div>`).join("")
      : '<div class="muted">Старых настроек с API-ключом не найдено.</div>';
    $$("#dFound .it").forEach((el) => (el.onclick = async () => {
      const x = r.found[+el.dataset.i];
      if (!(await confirmBox(`<h3>Перенести настройки?</h3><p>Из папки ${esc(x.path)} будут скопированы магазины, ключи, товары и партии.</p>`, "Перенести"))) return;
      try { await api("/api/legacy/import", { path: x.path }); toast("Настройки перенесены"); APP.clusters = null; await init(); showTab("settings"); }
      catch (e) { toast(e.message, true); }
    }));
  } catch (e) { $("#dFound").innerHTML = `<div class="err-text">${esc(e.message)}</div>`; }
};
$("#shopNew").onclick = async () => {
  const ok = await confirmBox(`<h3>Новый магазин</h3><input id="promptIn" placeholder="Название магазина">`, "Создать");
  const name = ok ? (promptBox._v || "").trim() : ""; promptBox._v = "";
  if (!name) return;
  await api("/api/profile/new", { name }); APP.clusters = null; await init(); showTab("settings");
};
$("#shopDemo").onclick = async () => {
  await api("/api/profile/new", { name: "Демо-магазин", demo: true }); APP.clusters = null; await init(); showTab("new");
};

$("#quitBtn").onclick = async () => {
  if (!(await confirmBox("<h3>Закрыть программу?</h3><p>Созданные партии и настройки сохранятся. Чтобы открыть снова, запустите «Поставки Ozon».</p>", "Закрыть"))) return;
  try { await api("/api/quit"); document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#7b8092;font:15px system-ui">Программа закрыта. Эту вкладку можно закрыть.</div>'; }
  catch (e) { toast(e.message, true); }
};

/* ================= старт ================= */
async function init() {
  await loadState();
  const fresh = !lsGet(draftKey());
  D = lsGet(draftKey()) || newDraft();
  if (D.ts && ("d2" in D.ts || "t2" in D.ts)) {        // старая форма с диапазоном
    D.ts = { d1: D.ts.d1, t1: D.ts.t1 || "10:00" }; D.tsmode = "auto"; saveDraft();
  }
  if (D.cv !== 2) {
    for (const c of D.cities || []) { c.match = c.match || c.cluster_name || c.name; c.cluster_id = null; c.cluster_name = null; }
    D.matrix = null; D.edited = {}; D.remCity = "largest"; D.cv = 2;
  }
  migrateDraft(D); saveDraft();
  if (APP.profile.demo && !D.products.length && fresh) {
    D.products = [["1000000032", 300, "32 ГБ"], ["1000000064", 600, "64 ГБ"], ["1000000128", 1000, "128 ГБ"], ["1000000256", 600, "256 ГБ"], ["1000000512", 300, "512 ГБ"], ["1000001024", 300, "1 ТБ"]]
      .map(([sku, qty, short]) => ({ sku, qty, short }));
  }
  renderNew();
  await resolveDropoffs();
  await loadClusters();
  bindRows(); renderDistControls(); replan();
  if (APP.profile.demo && D.products.some((p) => !p.name)) lookup(D.products.filter((p) => !p.name));
  const tab = lsGet("tab") || "new";
  showTab(tab === "batches" && !openBatch ? "batches" : tab);
}
init().then(() => checkUpdate(false, true)).catch((e) => toast(e.message, true));
