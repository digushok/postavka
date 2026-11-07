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
  APP.profile = s.profile; APP.profiles = s.profiles; APP.dataDir = s.data_dir;
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
    if (D) for (const c of D.cities) {
      if (!c.cluster_id) {
        const m = (c.match || c.name || "").toLowerCase();
        const hit = findCluster(m);
        if (hit) { c.cluster_id = hit.id; c.cluster_name = hit.name; }
      }
    }
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
    cities: tpl ? JSON.parse(JSON.stringify(tpl.cities)) : [],
    method: P.method || "remainder_city", remCity: P.remainder_city || "largest",
    matrix: null, edited: {},
    tsmode: ts.mode || "auto",
    ts: { d1: addDays(ts.days_from ?? 2), t1: ts.time_from || "10:00" },
    passOn: !!(P.pass && P.pass.enabled), dropoff: P.dropoff || null, multi: false, cv: 2,
  };
}
function saveDraft() { lsSet(draftKey(), D); }
const cityKey = (c) => (c.cluster_id ? String(c.cluster_id) : "?" + c.name);

function renderNew() {
  renderProducts(); renderCities(); renderShipping();
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

/* ---- города ---- */
function renderCities() {
  const P = APP.profile;
  $("#tplSel").innerHTML = P.templates.map((t) => `<option ${t.name === D.template ? "selected" : ""}>${esc(t.name)}</option>`).join("");
  const opts = (sel) => (APP.clusters || []).map((c) => `<option value="${esc(c.id)}" ${String(sel) === String(c.id) ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  $("#cityTable tbody").innerHTML = D.cities.map((c, i) => `<tr data-i="${i}">
      <td><input class="c-name" value="${esc(c.name)}"></td>
      <td><select class="c-cl ${c.cluster_id ? "" : "bad"}"><option value="">${c.cluster_id ? "" : "— выберите кластер —"}</option>${opts(c.cluster_id)}${c.cluster_id && !(APP.clusters || []).some((x) => String(x.id) === String(c.cluster_id)) ? `<option value="${esc(c.cluster_id)}" selected>${esc(c.cluster_name || c.cluster_id)}</option>` : ""}</select></td>
      <td><input class="c-share" type="number" min="0" step="0.5" value="${c.share}"></td>
      <td><button class="x" title="Убрать">×</button></td></tr>`).join("");
  const sum = D.cities.reduce((a, c) => a + (parseFloat(c.share) || 0), 0);
  $("#shareSum").innerHTML = `<span class="${Math.abs(sum - 100) < 0.001 ? "good" : "warn-text"}">${+sum.toFixed(2)}%</span>`;
  $("#method").value = D.method;
  $("#remCityWrap").classList.toggle("hidden", D.method !== "remainder_city" && D.method !== "fair");
  $("#remCityWrap").firstChild.textContent = D.method === "fair" ? "Город для рассыпных штук" : "Город для остатка";
  $("#remCity").innerHTML = `<option value="largest">С наибольшей долей</option>` +
    D.cities.map((c) => `<option value="${esc(cityKey(c))}" ${D.remCity === cityKey(c) ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  if (![...$("#remCity").options].some((o) => o.selected)) $("#remCity").value = "largest";
}
$("#cityTable").addEventListener("change", (e) => {
  const tr = e.target.closest("tr"); if (!tr) return;
  const c = D.cities[+tr.dataset.i]; const t = e.target;
  const oldKey = cityKey(c);
  if (t.classList.contains("c-name")) c.name = t.value.trim();
  if (t.classList.contains("c-cl")) { c.cluster_id = t.value || null; c.cluster_name = t.selectedOptions[0]?.textContent || ""; }
  if (t.classList.contains("c-share")) c.share = parseFloat(t.value) || 0;
  const nk = cityKey(c);
  if (nk !== oldKey) moveCityKey(oldKey, nk);
  saveDraft(); renderCities(); replan();
});
$("#cityTable").addEventListener("click", (e) => {
  if (!e.target.classList.contains("x")) return;
  D.cities.splice(+e.target.closest("tr").dataset.i, 1); saveDraft(); renderCities(); replan();
});
function moveCityKey(a, b) {
  if (D.matrix && D.matrix[a]) { D.matrix[b] = D.matrix[a]; delete D.matrix[a]; }
  for (const k of Object.keys(D.edited)) if (k.startsWith(a + "|")) { D.edited[b + k.slice(a.length)] = 1; delete D.edited[k]; }
  if (D.remCity === a) D.remCity = b;
}
$("#addCity").onclick = () => { D.cities.push({ name: "Новый город", cluster_id: null, share: 5 }); saveDraft(); renderCities(); replan(); };
$("#method").onchange = (e) => { D.method = e.target.value; saveDraft(); renderCities(); replan(); };
$("#remCity").onchange = (e) => { D.remCity = e.target.value; saveDraft(); replan(); };
$("#tplSel").onchange = async (e) => {
  const t = APP.profile.templates.find((x) => x.name === e.target.value);
  if (!t) return;
  D.template = t.name; D.cities = JSON.parse(JSON.stringify(t.cities)); D.edited = {}; D.matrix = null;
  await saveProfile({ template: t.name });
  saveDraft(); renderCities(); replan();
};
async function saveTemplate(name) {
  const tpls = APP.profile.templates.filter((t) => t.name !== name);
  const cities = D.cities.map(({ name, cluster_id, cluster_name, share, match }) => ({ name, cluster_id, cluster_name, share, match }));
  const idx = APP.profile.templates.findIndex((t) => t.name === name);
  if (idx >= 0) tpls.splice(idx, 0, { name, cities }); else tpls.push({ name, cities });
  await saveProfile({ templates: tpls, template: name, method: D.method, remainder_city: D.remCity });
  D.template = name; saveDraft(); renderCities(); toast(`Шаблон «${name}» сохранён`);
}
$("#tplSave").onclick = () => saveTemplate(D.template || "Основной");
$("#tplSaveAs").onclick = async () => {
  const ok = await confirmBox(`<h3>Новый шаблон городов</h3><input id="promptIn" placeholder="Название, например «Без Москвы»">`, "Сохранить");
  const name = ok ? (promptBox._v || "").trim() : "";
  promptBox._v = "";
  if (name) saveTemplate(name);
};

/* ---- расчёт ---- */
const replan = debounce(async () => {
  const body = {
    products: D.products.filter((p) => p.sku), method: D.method, remainder_city: D.remCity,
    cities: D.cities.map((c) => ({ key: cityKey(c), share: c.share, name: c.name })),
    matrix: Object.keys(D.edited).length ? D.matrix : null, edited: D.edited,
  };
  body.products = body.products.map((p) => ({ ...p, short: prodLabel(p) }));
  try {
    PLAN = await api("/api/plan", body);
    D.matrix = PLAN.matrix; saveDraft();
    renderMatrix(); renderGroups();
  } catch (e) { toast(e.message, true); }
}, 150);

function renderMatrix() {
  const prods = D.products.filter((p) => p.sku);
  const M = D.matrix || {};
  if (!prods.length || !D.cities.length) { $("#matrix").innerHTML = `<tr class="empty"><td>Здесь появится таблица, когда добавите товары и города.</td></tr>`; $("#editedNote").classList.add("hidden"); return; }
  let h = `<thead><tr><th>Город</th><th>Доля</th>${prods.map((p) => `<th title="${esc(p.name || "")}">${esc(prodLabel(p))}</th>`).join("")}<th>Всего шт</th><th>Коробок</th></tr></thead><tbody>`;
  const colSum = {};
  for (const c of D.cities) {
    const k = cityKey(c); let rowSum = 0;
    h += `<tr><td class="city">${esc(c.name)}${c.cluster_id ? "" : ' <span class="err-text">нет кластера</span>'}</td><td class="share">${+(+c.share).toFixed(2)}%</td>`;
    for (const p of prods) {
      const v = (M[k] || {})[p.sku] ?? 0;
      rowSum += v; colSum[p.sku] = (colSum[p.sku] || 0) + v;
      const ed = D.edited[k + "|" + p.sku];
      h += `<td><input type="number" min="0" data-c="${esc(k)}" data-s="${esc(p.sku)}" value="${v}" class="${ed ? "edited" : ""}"></td>`;
    }
    h += `<td class="tot">${fmt(rowSum)}</td><td class="boxes">${fmt((PLAN?.boxes || {})[k] || 0)}</td></tr>`;
  }
  const tot = prods.reduce((a, p) => a + (colSum[p.sku] || 0), 0);
  const bought = prods.reduce((a, p) => a + (p.qty || 0), 0);
  h += `</tbody><tfoot><tr><td class="lbl">Распределено</td><td></td>${prods.map((p) => `<td>${fmt(colSum[p.sku] || 0)}</td>`).join("")}<td>${fmt(tot)}</td><td>${fmt(Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0))}</td></tr>`;
  h += `<tr><td class="lbl">Закуплено</td><td></td>${prods.map((p) => `<td class="sub">${fmt(p.qty)}</td>`).join("")}<td class="sub">${fmt(bought)}</td><td></td></tr>`;
  h += `<tr><td class="lbl">Проверка</td><td></td>${prods.map((p) => { const d = (colSum[p.sku] || 0) - (p.qty || 0); return `<td class="${d ? "bad-mark" : "ok-mark"}">${d ? (d > 0 ? "+" : "") + fmt(d) : "✓"}</td>`; }).join("")}<td class="${tot - bought ? "bad-mark" : "ok-mark"}">${tot - bought ? (tot - bought > 0 ? "+" : "") + fmt(tot - bought) : "✓"}</td><td></td></tr></tfoot>`;
  $("#matrix").innerHTML = h;
  $("#editedNote").classList.toggle("hidden", !Object.keys(D.edited).length);
  renderSummary();
}
$("#matrix").addEventListener("change", (e) => {
  const t = e.target; if (!t.dataset.c) return;
  const k = t.dataset.c, s = t.dataset.s;
  D.matrix[k] = D.matrix[k] || {}; D.matrix[k][s] = num(t.value);
  const calc = ((PLAN?.calc || {})[k] || {})[s];
  if (calc === num(t.value)) delete D.edited[k + "|" + s]; else D.edited[k + "|" + s] = 1;
  saveDraft(); replan();
});
$("#matrix").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target.dataset.c) return;
  e.preventDefault();
  const ins = $$("#matrix tbody input"); const i = ins.indexOf(e.target); const cols = D.products.filter((p) => p.sku).length;
  (ins[i + cols] || ins[0]).focus(); (ins[i + cols] || ins[0]).select();
});
$("#recalc").onclick = () => { D.edited = {}; saveDraft(); replan(); };

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
  $("#tsManualHint").classList.toggle("hidden", D.tsmode === "auto");
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
    resEl.innerHTML = r.items.length ? r.items.map((x, i) => `<div class="it" data-i="${i}">${esc(x.name)}<div class="muted">${esc(x.address || "")} · ${esc((x.warehouse_type || "").replace("WAREHOUSE_TYPE_", ""))}</div></div>`).join("") : `<div class="muted">Ничего не найдено</div>`;
    $$(".it", resEl).forEach((el) => (el.onclick = () => onPick(r.items[+el.dataset.i])));
  } catch (e) { resEl.innerHTML = `<div class="err-text">${esc(e.message)}</div>`; }
}
$("#dropoffFind").onclick = () => searchDropoff($("#dropoffQ").value, $("#dropoffRes"), (x) => { D.dropoff = x; saveDraft(); $("#dropoffSearch").classList.add("hidden"); $("#dropoffRes").innerHTML = ""; renderShipping(); });
$("#dropoffQ").onkeydown = (e) => { if (e.key === "Enter") $("#dropoffFind").click(); };

function renderSummary() {
  const n = D.cities.filter((c) => Object.values((D.matrix || {})[cityKey(c)] || {}).some((v) => v > 0)).length;
  const boxes = Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0);
  const units = D.products.reduce((a, p) => a + (p.qty || 0), 0);
  const min = D.multi ? 2 : Math.ceil(n * 0.55) * 2;
  $("#summary").innerHTML = n ? `<div class="k"><b>${D.multi ? 1 : n}</b><span>${D.multi ? `заявка на ${n} городов` : "заявок"}</span></div><div class="k"><b>${fmt(boxes)}</b><span>коробок</span></div><div class="k"><b>${fmt(units)}</b><span>штук</span></div><div class="k"><b>~${min}–${min + 5} мин</b><span>займёт создание</span></div>`
    : `<span class="muted">Добавьте товары, чтобы увидеть итог</span>`;
}

$("#createBtn").onclick = async () => {
  const prods = D.products.filter((p) => p.sku);
  const errs = [];
  if (!prods.length) errs.push("нет товаров");
  if (prods.some((p) => !/^\d+$/.test(p.sku))) errs.push("SKU должен состоять из цифр");
  if (prods.some((p) => p.lookupErr)) errs.push("есть товары, не найденные в магазине");
  const used = D.cities.filter((c) => Object.values((D.matrix || {})[cityKey(c)] || {}).some((v) => v > 0));
  if (used.some((c) => !c.cluster_id)) errs.push("не у всех городов выбран кластер Ozon");
  if (!D.dropoff) errs.push("не выбрана точка отгрузки");
  if (D.tsmode === "auto" && !D.ts.d1) errs.push("не указан желаемый день поставки");
  const keys = used.map(cityKey); if (new Set(keys).size !== keys.length) errs.push("один кластер выбран для двух городов");
  if (errs.length) return toast("Нельзя создать: " + errs.join("; "), true);

  const sumCol = {}; for (const c of used) for (const [s, v] of Object.entries(D.matrix[cityKey(c)] || {})) sumCol[s] = (sumCol[s] || 0) + v;
  const diff = prods.filter((p) => (sumCol[p.sku] || 0) !== (p.qty || 0));
  const boxes = Object.values(PLAN?.boxes || {}).reduce((a, b) => a + b, 0);
  if (D.multi && used.length > 20) return toast("Одной заявкой Озон принимает не больше 20 городов", true);
  const ok = await confirmBox(`<h3>${D.multi ? `Создать одну заявку на ${used.length} городов?` : `Создать ${used.length} заявок?`}</h3>
    <p>${fmt(boxes)} коробок через «${esc(D.dropoff.name)}».<br>
    Таймслот: ${D.tsmode === "auto" ? `желаемо ${esc(dmy(D.ts.d1))} в ${esc(D.ts.t1 || "10:00")} — каждому городу ближайший свободный` : "выберете из доступных после создания черновиков"}.<br>
    ${D.passOn ? "Пропуск будет оформлен." : "Без пропуска."}</p>
    ${diff.length ? `<p class="warn-text">Распределено не столько, сколько закуплено: ${diff.map((p) => esc(prodLabel(p))).join(", ")}.</p>` : ""}
    ${APP.profile.demo ? '<p class="muted">Демо-режим: в настоящий Озон ничего не отправится.</p>' : ""}`, "Создать");
  if (!ok) return;
  $("#createBtn").disabled = true;
  try {
    const body = {
      products: prods.map((p) => ({ sku: p.sku, qty: p.qty, name: p.name, short: prodLabel(p), box_size: p.box_size || null })),
      cities: used.map((c) => ({ cluster_id: String(c.cluster_id), name: c.name, cluster_name: c.cluster_name })),
      matrix: Object.fromEntries(used.map((c) => [String(c.cluster_id), D.matrix[cityKey(c)]])),
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
const SST = { new: ["ожидает", ""], draft: ["черновик…", "run"], draft_ok: ["черновик готов", ""], supply: ["заявка…", "run"], cargo: ["грузоместа…", "run"], labels: ["этикетки…", "run"], pass: ["пропуск…", "run"], done: ["готово", "ok"], error: ["ошибка", "err"], cancelled: ["отменена", ""], cancelling: ["отменяется…", "run"] };
const tag = (m, k) => { const [t, c] = m[k] || [k, ""]; return `<span class="chip ${c}">${esc(t)}</span>`; };

async function loadBatches() {
  $("#batchView").classList.add("hidden"); $("#batchList").classList.remove("hidden");
  try {
    const r = await api("/api/batches");
    $("#batchList").innerHTML = `<div class="page-h"><h1>Партии поставок</h1></div>` + (r.batches.length ? `<div class="blist">` +
      r.batches.map((b) => `<div class="bitem" data-id="${esc(b.id)}">
        <div><div class="t">${esc(b.created)}</div><div class="m">${b.demo ? "демо · " : ""}партия ${esc(b.id)}</div></div>
        <div>${b.phase === "cancelling" || b.phase === "cancelled" ? tag(PHASE, b.phase) : b.running ? '<span class="chip run">выполняется</span>' : tag(PHASE, b.phase)}</div>
        <div><div class="t">${b.done}/${b.cities}</div><div class="m">поставок готово</div></div>
        <div><div class="t">${fmt(b.boxes)}</div><div class="m">коробок</div></div>
        <div><div class="t">${esc(b.slot || "—")}</div><div class="m">таймслот</div></div>
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
  const slotText = b.chosen_slot_human || (b.mode === "auto" ? `желаемо ${dmy(pref.date || pref.date_from)} ${pref.time || pref.time_from || ""}` : "выбор из списка");
  let h = `<a class="back" id="backList">← Все партии</a>
    <div class="card"><div class="bhead"><h2>Партия от ${esc(b.created)}</h2>${busy && b.phase !== "cancelling" ? '<span class="chip run">выполняется</span>' : tag(PHASE, b.phase)}${b.demo ? '<span class="chip plain">демо</span>' : ""}
    <div class="actions">
      ${busy && b.phase !== "cancelling" ? '<button class="btn" id="bStop">Остановить</button>' : ""}
      ${!busy && ["error", "partial", "stopped", "new"].includes(b.phase) ? '<button class="btn primary" id="bResume">Продолжить</button>' : ""}
      ${!busy && b.phase !== "cancelled" && ships.some((s) => s.order_id && !s.cancelled) ? '<button class="btn soft" id="bSync">Сверить с Озоном</button>' : ""}
      ${!busy && (b.phase === "cancelled" || b.phase === "error") ? '<button class="btn primary" id="bAgain">Создать заново</button>' : ""}
      <button class="btn soft" id="bFolder">Открыть папку</button>
      ${b.phase !== "cancelling" && ships.some((s) => s.order_id && !s.cancelled) ? `<button class="btn danger" id="bCancel">${busy ? "Остановить и отменить заявки" : "Отменить заявки"}</button>` : ""}
      ${!busy && !ships.some((s) => s.order_id && !s.cancelled) ? '<button class="btn ghost" id="bDelete">Удалить из истории</button>' : ""}
    </div></div>
    <div class="meta">
      <div class="k"><span>Заявки</span><b>${b.multi ? `одна на все города${b.order?.order_id ? " · № " + esc(ships.find((s) => s.order_number)?.order_number || b.order.order_id) : ""}` : "отдельно на каждый город"}</b></div>
      <div class="k"><span>Точка отгрузки</span><b>${esc(b.dropoff?.name || "—")}</b></div>
      <div class="k"><span>Таймслот</span><b>${esc(slotText)}</b></div>
      <div class="k"><span>Пропуск</span><b>${b.pass?.enabled ? "оформляется" : "нет"}</b></div>
      <div class="k"><span>${b.phase === "cancelled" ? "Отменено" : "Готово"}</span><b>${b.phase === "cancelled" ? `${ships.length} из ${ships.length}` : `${done} из ${active.length}`}</b></div>
    </div>
    <div class="progress ${pct === 100 ? "ok" : ""}"><i style="width:${pct}%"></i></div>
    ${b.phase === "cancelling" ? `<div class="banner" style="margin:14px 0 0">Отменяю заявки в Озоне — это займёт до пары минут. Можно не ждать: итог появится здесь.</div>` : ""}
    ${b.phase === "cancelled" ? `<div class="banner" style="margin:14px 0 0">Все заявки этой партии отменены${b.cancel_result?.at ? " " + esc(b.cancel_result.at) : ""}. Чтобы отправить те же товары снова, нажмите «Создать заново».</div>` : ""}
    ${b.error && b.phase !== "cancelled" ? `<div class="banner err" style="margin:14px 0 0">${esc(b.error)}</div>` : ""}
    ${ships.some((s) => s.mismatch && !s.cancelled) ? `<div class="banner err" style="margin:14px 0 0"><b>Есть поставки, которые едут не в тот кластер</b> — они отмечены красным. Отмените их кнопкой «Отменить» в строке и создайте эти города заново новой партией.</div>` : ""}</div>`;

  if (b.phase === "await_slot" && !b.running) h += slotPicker(b);

  h += `<div class="card"><div class="card-h"><div><h2>Поставки</h2></div></div><div class="scroll"><table class="grid"><thead><tr><th>Город</th><th>Кластер в Озоне</th><th>Статус</th><th>Заявка</th><th class="r">Коробок</th><th class="r">Шт</th><th>Таймслот</th><th>Примечания</th><th></th></tr></thead><tbody>` +
    ships.map((s) => { const cl = s.actual_cluster || s.ozon_cluster_name || s.cluster_name || "—"; return `<tr class="${s.mismatch && !s.cancelled ? "mismatch" : ""} ${s.cancelled ? "dim" : ""}"><td><b>${esc(s.name)}</b></td><td class="small">${esc(cl)}${s.mismatch ? ' <span class="err-text">не тот кластер!</span>' : ""}</td><td>${tag(SST, s.status)}</td><td>${esc(s.order_number || s.order_id || "—")}</td><td class="r">${s.boxes.length}</td><td class="r">${fmt(units(s))}</td><td class="small">${s.slot ? esc(humanSlot(s.slot)) : "—"}</td>
      <td>${typeof s.verified === "number" ? `<div class="good small">✓ состав подтверждён Озоном</div>` : ""}${s.error ? `<div class="err-text">${esc(s.error)}</div>` : ""}${(s.warn || []).map((w) => `<div class="warn-text">${esc(w)}</div>`).join("")}</td>
      <td class="r">${!busy && !b.multi && s.order_id && !s.cancelled ? `<button class="btn danger sm" data-cancel="${esc(s.cluster_id)}">Отменить</button>` : ""}</td></tr>`; }).join("") + `</tbody></table></div>${b.synced_at ? `<div class="muted small" style="margin-top:8px">Сверено с Озоном ${esc(b.synced_at)}</div>` : ""}</div>`;

  const groups = b.groups || [];
  const files = b.files;
  h += `<div class="card"><div class="card-h"><div><h2>Этикетки</h2><p class="sub">${files ? "Один файл — одинаковые коробки. Печатайте и клейте на любую коробку этого состава." : "Появятся, когда поставки будут созданы"}</p></div><div class="actions">${files?.all ? `<a class="btn soft" target="_blank" href="${fileUrl(b, files.all)}">Все этикетки</a>` : ""}${files?.packing ? `<a class="btn soft" target="_blank" href="${fileUrl(b, files.packing)}">Лист сборки</a>` : ""}${files ? '<button class="btn ghost" id="bLabelsFolder">Папка</button>' : ""}</div></div>`;
  h += `<div class="files-grid">` + groups.map((g, i) => {
    const f = (files?.groups || []).find((x) => x.id === g.id);
    const inner = `<div class="pdf">PDF</div><div><div class="fn">${esc(g.name)} × ${fmt(g.qty)} шт</div><div class="fm">${g.total} кор · ${g.cities.map((c) => esc(c.name) + (c.count > 1 ? ` ×${c.count}` : "")).join(", ")}${f?.missing ? `<br><span class="warn-text">не готово для ${f.missing} пост.</span>` : ""}</div></div><div class="num">${String(i + 1).padStart(2, "0")}</div>`;
    return f?.file ? `<a class="fcard" target="_blank" href="${fileUrl(b, f.file)}">${inner}</a>` : `<div class="fcard off">${inner}</div>`;
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
  $$("[data-cancel]").forEach((el) => (el.onclick = async () => {
    const s = ships.find((x) => x.cluster_id === el.dataset.cancel);
    if (!(await confirmBox(`<h3>Отменить заявку «${esc(s.name)}» в Озоне?</h3><p>Заявка ${esc(s.order_number || s.order_id)} будет отменена. Это действие нельзя отменить.</p>`, "Отменить заявку"))) return;
    try { await api("/api/batch/cancel", { id: b.id, cluster_ids: [s.cluster_id] }); }
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
  if (src && src.cities) {
    D = Object.assign(newDraft(), JSON.parse(JSON.stringify(src)), { cv: 2 });
  } else {                                   // партии из старых версий — собираем из поставок
    D = newDraft();
    D.products = b.products.map((p) => ({ sku: p.sku, qty: 0, name: p.name, short: p.short, box_size: p.box_size, multiple: !!(APP.profile.products[p.sku] || {}).multiple }));
    D.cities = b.shipments.map((s) => ({ name: s.name, cluster_id: s.cluster_id, cluster_name: s.cluster_name, share: 0 }));
    D.matrix = {}; D.edited = {};
    for (const s of b.shipments) {
      D.matrix[s.cluster_id] = {};
      for (const p of b.products) {
        const q = s.boxes.filter((x) => x.sku === p.sku).reduce((a, x) => a + x.qty, 0);
        D.matrix[s.cluster_id][p.sku] = q; D.edited[s.cluster_id + "|" + p.sku] = 1;
        D.products.find((x) => x.sku === p.sku).qty += q;
      }
    }
    const tot = D.products.reduce((a, p) => a + p.qty, 0) || 1;
    for (const c of D.cities) c.share = Math.round(Object.values(D.matrix[c.cluster_id]).reduce((a, v) => a + v, 0) / tot * 1000) / 10;
    D.dropoff = b.dropoff || D.dropoff; D.multi = !!b.multi;
  }
  saveDraft(); closeBatch(); showTab("new"); renderNew();
  toast("Форма заполнена данными партии — проверьте и нажмите «Создать поставки»");
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
    $("#sMsg").textContent = "Сохранено"; APP.clusters = null; loadClusters().then(() => D && renderCities()); resolveDropoffs();
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
    const box = r.ok ? (r.newer
      ? `<div class="upd new"><b>Доступна версия ${esc(r.latest)}</b><a class="btn primary sm" href="${esc(r.url)}" target="_blank" rel="noopener">Скачать</a><a class="link" href="${esc(r.page)}" target="_blank" rel="noopener">Что нового</a></div>`
      : `<div class="upd">У вас последняя версия (${esc(APP.ver.version)})</div>`)
      : `<div class="upd">Не удалось проверить обновления: ${esc(r.error)}</div>`;
    if (!quiet || r.newer) $("#updRes").innerHTML = box;
    const f = $("#verUpd");
    if (r.ok && r.newer) { f.textContent = `доступна ${r.latest} — скачать`; f.href = r.url; } else { f.textContent = ""; f.removeAttribute("href"); }
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
  D = lsGet(draftKey()) || newDraft();
  if (D.ts && ("d2" in D.ts || "t2" in D.ts)) {        // старая форма с диапазоном
    D.ts = { d1: D.ts.d1, t1: D.ts.t1 || "10:00" }; D.tsmode = "auto"; saveDraft();
  }
  if (D.cv !== 2) {
    for (const c of D.cities) { c.match = c.match || c.cluster_name || c.name; c.cluster_id = null; c.cluster_name = null; }
    D.matrix = null; D.edited = {}; D.remCity = "largest"; D.cv = 2; saveDraft();
  }
  if (APP.profile.demo && !D.products.length && !lsGet(draftKey())) {
    D.products = [["1000000032", 300, "32 ГБ"], ["1000000064", 600, "64 ГБ"], ["1000000128", 1000, "128 ГБ"], ["1000000256", 600, "256 ГБ"], ["1000000512", 300, "512 ГБ"], ["1000001024", 300, "1 ТБ"]]
      .map(([sku, qty, short]) => ({ sku, qty, short }));
  }
  renderNew();
  await resolveDropoffs();
  await loadClusters();
  renderCities(); replan();
  if (APP.profile.demo && D.products.some((p) => !p.name)) lookup(D.products.filter((p) => !p.name));
  const tab = lsGet("tab") || "new";
  showTab(tab === "batches" && !openBatch ? "batches" : tab);
}
init().then(() => checkUpdate(false, true)).catch((e) => toast(e.message, true));
