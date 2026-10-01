"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const nfCompact = new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 });
const pf = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1, signDisplay: "exceptZero" });
const AGG = { sum: "сумма", last: "остаток на начало", end: "на будущий период" };
const TABLE_TABS = ["pivot", "rc", "days", "trend"];
const RC_ROLES = { stock: "Остаток", order: "Заказ", output: "Выпуск", consumption: "Потребление" };

const state = {
  tab: "pivot",
  mode: "week",       // сводная: неделя, день или произвольный период
  date: null,         // якорная дата (ISO), неделя = неделя этой даты
  range: null,        // сводная, режим «Период»: { from, to } (ISO, включительно)
  category: "",
  q: "",
  sourceId: null,     // для «По дням» и «Динамики»
  trendCount: 8,
  meta: { sources: [], weeks: [], days: [] },
  data: {},
  sort: { pivot: {}, rc: {}, days: {}, trend: {} },
  detail: null,
  detailSource: null,
};

// ---------- утилиты ----------

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(body?.error || `HTTP ${res.status}`);
  return body;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}

const isNil = (v) => v === null || v === undefined;
const fmt = (v) => (isNil(v) ? "—" : nf.format(v));
const sum = (vals) => {
  const xs = vals.filter((v) => !isNil(v));
  return xs.length ? xs.reduce((a, b) => a + b, 0) : null;
};
const delta = (cur, prev) => (isNil(cur) || isNil(prev) || prev === 0 ? null : (cur - prev) / Math.abs(prev));

function monday(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

const ddmm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;

function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function setCaption(...parts) {
  $("#caption").replaceChildren(...parts.filter((x) => !isNil(x) && x !== false));
}

function deltaNode(d, prev) {
  if (isNil(d)) return null;
  const arrow = d > 0 ? el("span", { class: "up" }, "▲ ") : d < 0 ? el("span", { class: "down" }, "▼ ") : null;
  return el("span", { class: "delta", title: `пред.: ${fmt(prev)}` }, arrow, pf.format(d));
}

function savePrefs() {
  try {
    localStorage.setItem("instrument.prefs", JSON.stringify({ tab: state.tab, mode: state.mode, range: state.range, trendCount: state.trendCount }));
  } catch { /* приватный режим — не критично */ }
}

function loadPrefs() {
  try { Object.assign(state, JSON.parse(localStorage.getItem("instrument.prefs") || "{}")); } catch { /* нет доступа */ }
}

// ---------- периоды ----------

function periodOptions() {
  const byWeek = state.tab !== "pivot" || state.mode === "week";
  return byWeek
    ? state.meta.weeks.map((w) => ({ value: w.start, label: w.label }))
    : state.meta.days.map((d) => ({ value: d.date, label: d.label }));
}

function currentPeriodValue() {
  const byWeek = state.tab !== "pivot" || state.mode === "week";
  return byWeek ? monday(state.date) : state.date;
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// По умолчанию — последняя «закрытая» неделя: если в самой свежей неделе
// меньше 7 дней данных, а есть предыдущая, берём предыдущую.
// «Самая свежая» — не позже сегодня: заказы, план и остатки 1С выгружает и на будущие
// даты (остатки — до конца месяца), иначе сайт открывался бы на пустой будущей неделе.
function defaultDate() {
  const { days } = state.meta;
  if (!days.length) return null;
  const today = todayIso();
  const latest = days.find((d) => d.date <= today)?.date ?? days.at(-1).date;  // days — от новых к старым
  const wk = monday(latest);
  const inWeek = days.filter((d) => monday(d.date) === wk).length;
  const prev = state.meta.weeks.find((w) => w.start < wk);
  if (inWeek < 7 && prev) return lastDayInWeek(prev.start);
  return latest;
}

// Последний день с данными внутри недели — чтобы переключение в «День» не упиралось в пустой понедельник.
// Будущие дни (план, заказы, остатки вперёд) пропускаем, если в неделе есть прошедшие.
function lastDayInWeek(start) {
  const inWeek = state.meta.days.filter((x) => monday(x.date) === start);
  const d = inWeek.find((x) => x.date <= todayIso()) ?? inWeek[0];
  return d ? d.date : start;
}

const isRange = () => state.tab === "pivot" && state.mode === "range";
const pivotPeriod = () => (isRange() ? { date: state.range.from, date_to: state.range.to } : { date: state.date });

// Период по умолчанию — неделя, на которой стоял пользователь.
function ensureRange() {
  if (state.range?.from && state.range?.to) return;
  const from = monday(state.date || todayIso());
  state.range = { from, to: addDays(from, 6) };
}

// Быстрый выбор считаем от последнего дня с данными (не позже сегодня), как и неделю по умолчанию.
function presetRange(kind) {
  const today = todayIso();
  const anchor = state.meta.days.find((d) => d.date <= today)?.date ?? today;
  const month = `${anchor.slice(0, 7)}-01`;
  if (kind === "month") return { from: month, to: anchor };
  if (kind === "prev-month") {
    const to = addDays(month, -1);
    return { from: `${to.slice(0, 7)}-01`, to };
  }
  if (kind === "28d") return { from: addDays(anchor, -27), to: anchor };
  if (kind === "ytd") return { from: `${anchor.slice(0, 4)}-01-01`, to: anchor };
  return null;
}

function renderRangeForm() {
  $("#range-from").value = state.range.from;
  $("#range-to").value = state.range.to;
  $("#range-preset").value = "";
}

function renderPeriodSelect() {
  const sel = $("#period-select");
  const opts = periodOptions();
  const cur = currentPeriodValue();
  sel.replaceChildren(...opts.map((o) => el("option", { value: o.value }, o.label)));
  if (!opts.some((o) => o.value === cur)) sel.prepend(el("option", { value: cur }, cur));
  sel.value = cur;
  const idx = opts.findIndex((o) => o.value === cur);
  $("#period-prev").disabled = idx < 0 || idx >= opts.length - 1;
  $("#period-next").disabled = idx <= 0;
}

function stepPeriod(dir) {
  const opts = periodOptions();
  const idx = opts.findIndex((o) => o.value === currentPeriodValue());
  const next = opts[idx - dir];  // список отсортирован от новых к старым
  if (!next) return;
  state.date = state.tab === "pivot" && state.mode === "day" ? next.value : lastDayInWeek(next.value);
  load();
}

// ---------- фильтры ----------

function renderFilters() {
  const tableTab = TABLE_TABS.includes(state.tab);
  $("#filters").hidden = !tableTab;
  $("#caption").hidden = !tableTab;
  if (!tableTab) return;
  $("#mode-toggle").hidden = state.tab !== "pivot";
  for (const b of $$("#mode-toggle button")) b.setAttribute("aria-pressed", String(b.dataset.mode === state.mode));
  $("#source-select").hidden = state.tab === "pivot" || state.tab === "rc";
  $("#count-wrap").hidden = state.tab !== "trend";
  $("#trend-count").value = String(state.trendCount);

  const srcSel = $("#source-select");
  srcSel.replaceChildren(...state.meta.sources.map((s) => el("option", { value: s.id }, s.name)));
  if (state.sourceId) srcSel.value = String(state.sourceId);

  const mode = state.tab === "pivot" ? state.mode : "week";
  const range = isRange();
  if (range) ensureRange();
  $("#export").href = state.tab === "rc"
    ? `api/export-rc.xlsx${qs({ date: state.date, category: state.category, q: state.q })}`
    : `api/export.xlsx${qs({ mode, ...pivotPeriod(), category: state.category, q: state.q })}`;
  $("#export").hidden = state.tab === "trend";
  $("#period-nav").hidden = range;
  $("#range-form").hidden = !range;
  if (range) renderRangeForm();
  else renderPeriodSelect();
}

function renderCategories(rows) {
  const cats = [...new Set(rows.map((r) => r.category))].sort((a, b) => a.localeCompare(b, "ru"));
  if (state.category && !cats.includes(state.category)) cats.unshift(state.category);
  const sel = $("#category-select");
  sel.replaceChildren(el("option", { value: "" }, "Все категории"), ...cats.map((c) => el("option", { value: c }, c)));
  sel.value = state.category;
}

function filterRows(rows) {
  const q = state.q.trim().toLowerCase();
  return rows.filter((r) =>
    (!state.category || r.category === state.category)
    && (!q || r.name.toLowerCase().includes(q) || r.code.toLowerCase().includes(q)));
}

// ---------- универсальная сортируемая таблица ----------

function nextSort(cur, key, numeric) {
  const first = numeric ? "desc" : "asc";
  if (cur.key !== key) return { key, dir: first };
  if (cur.dir === first) return { key, dir: first === "desc" ? "asc" : "desc" };
  return {};  // третий клик — вернуть группировку по категориям
}

function sortRows(rows, sort, columns) {
  const col = columns.find((c) => c.key === sort.key);
  if (!col) return null;
  return [...rows].sort((a, b) => {
    const va = col.get(a);
    const vb = col.get(b);
    if (isNil(va) && isNil(vb)) return 0;
    if (isNil(va)) return 1;  // пустые — всегда в конце
    if (isNil(vb)) return -1;
    const cmp = typeof va === "string" ? va.localeCompare(vb, "ru") : va - vb;
    return sort.dir === "asc" ? cmp : -cmp;
  });
}

function grid(table, { columns, rows, view, onRowClick }) {
  const sort = state.sort[view];
  const head = el("tr", {}, ...columns.map((c) => {
    const active = sort.key === c.key;
    const th = el("th", {
      class: [c.sortable !== false ? "sortable" : "", c.sep ? "sep" : ""].join(" "),
      "aria-sort": active ? (sort.dir === "asc" ? "ascending" : "descending") : null,
      title: c.sortable !== false ? "Сортировать" : null,
      onclick: c.sortable !== false ? () => {
        state.sort[view] = nextSort(sort, c.key, c.num);
        renderCurrent();
      } : null,
    }, c.label, active ? el("span", { class: "arrow" }, sort.dir === "asc" ? "▲" : "▼") : null,
    c.sub ? el("span", { class: `thsub ${c.subWarn ? "warn" : ""}` }, c.sub) : null);
    return th;
  }));

  const body = el("tbody");
  const addRow = (r) => body.append(el("tr", {
    class: `item ${state.detail === r.code ? "active" : ""}`,
    "data-code": r.code,
    onclick: () => onRowClick(r),
  }, ...columns.map((c) => el("td", { class: [c.num ? "num" : "", c.cls || "", c.sep ? "sep" : ""].join(" ") }, c.render(r)))));

  const sorted = sortRows(rows, sort, columns);
  if (sorted) {
    sorted.forEach(addRow);
  } else {
    let cat = null;
    for (const r of rows) {  // сервер отдаёт строки по категории и названию
      if (r.category !== cat) {
        cat = r.category;
        const n = rows.filter((x) => x.category === cat).length;
        body.append(el("tr", { class: "group" }, el("td", { colspan: columns.length }, `${cat} · ${n}`)));
      }
      addRow(r);
    }
  }
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: columns.length, class: "nil" }, "Нет данных под фильтр")));

  // Итоги по единицам измерения: кг отдельно от штук, «шт» — последней (липкой) строкой.
  const units = [...new Set(rows.map((r) => r.unit))].sort((a, b) => (a === "шт") - (b === "шт") || a.localeCompare(b));
  const foot = el("tfoot", {}, ...units.map((u) => {
    const part = rows.filter((r) => r.unit === u);
    return el("tr", {}, ...columns.map((c, i) => el("td", { class: [c.num ? "num" : "", c.sep ? "sep" : ""].join(" ") },
      i === 0 ? `Итого, ${u} · ${part.length} поз.` : c.total ? c.total(part) : "")));
  }));
  table.replaceChildren(el("thead", {}, head), body, foot);
}

function valueCell(v, prev, d) {
  return [fmt(v), deltaNode(d, prev)];
}

function totalCell(rows, cur, prev) {
  const c = sum(rows.map(cur));
  const p = sum(rows.map(prev));
  return valueCell(c, p, delta(c, p));
}

const nameCol = {
  key: "name", label: "Номенклатура", get: (r) => r.name,
  render: (r) => [r.name, el("span", { class: "code" }, r.code)],
};
const unitCol = { key: "unit", label: "Ед.", get: (r) => r.unit, render: (r) => r.unit, num: false };

// ---------- сводная: товары × источники ----------

function renderPivot() {
  const data = state.data.pivot;
  if (!data) return;
  renderCategories(data.rows);
  // Колонки: «сумма» за дни периода или срез на дату (остаток на начало / на конец, значение на конец периода).
  const partial = data.columns.filter((c) => c.covered < c.of);
  const gap = (c) => (c.date ? `${c.name} — нет среза на ${ddmm(c.date)}` : `${c.name} — ${c.covered} из ${c.of} дн.`);
  setCaption(
    `${data.period.label}. Значения в базовых единицах (шт, кг). Остатки — срез на начало периода и на начало следующего. Δ — ${data.period.compare}. `,
    partial.length ? el("span", { class: "warn" }, `⚠ Неполные данные: ${partial.map(gap).join("; ")}`) : null,
  );
  const columns = [nameCol, unitCol, ...data.columns.map((s, i) => ({
    key: `s${s.key}`,
    label: s.name,
    sub: s.date
      ? `на ${ddmm(s.date)}${s.covered ? "" : " · нет данных"}`
      : AGG[s.agg] + (s.covered < s.of ? ` · ${s.covered}/${s.of} дн.` : ""),
    subWarn: s.covered < s.of,
    num: true,
    sep: true,
    get: (r) => r.values[i].cur,
    render: (r) => valueCell(r.values[i].cur, r.values[i].prev, r.values[i].delta),
    total: (rows) => totalCell(rows, (r) => r.values[i].cur, (r) => r.values[i].prev),
  }))];
  grid($("#pivot-table"), { columns, rows: filterRows(data.rows), view: "pivot", onRowClick: (r) => openDetail(r.code) });
}

// ---------- товародвиженец РЦ: остаток ср, заказ и выпуск Ср–Вс, остаток пн ----------

function renderRcRoles(data) {
  const pick = (role) => el("label", { class: "inline" }, `${RC_ROLES[role]}:`,
    el("select", {
      "aria-label": `Источник: ${RC_ROLES[role].toLowerCase()}`,
      onchange: async (e) => {
        await api("api/rc-settings", {
          method: "PUT", headers: { "content-type": "application/json" },
          body: JSON.stringify({ [role]: e.target.value ? Number(e.target.value) : null }),
        });
        load();
      },
    },
    el("option", { value: "", selected: !data.roles[role] }, "— не выбран —"),
    ...state.meta.sources.map((s) => el("option", { value: s.id, selected: data.roles[role]?.id === s.id }, s.name))));
  $("#rc-roles").replaceChildren(...Object.keys(RC_ROLES).map(pick),
    el("span", { class: "muted" }, "Выбор источников общий для всех."));
}

function renderRc() {
  const data = state.data.rc;
  if (!data) return;
  renderCategories(data.rows);
  renderRcRoles(data);
  const missing = data.columns.filter((c) => c.role !== "calc" && data.roles[c.role] && c.covered < c.of);
  const gap = (c) => `${c.label.toLowerCase()} ${c.sub}${c.of > 1 ? ` (${c.covered}/${c.of} дн.)` : ""}`;
  const unset = Object.keys(RC_ROLES).filter((r) => !data.roles[r]).map((r) => RC_ROLES[r].toLowerCase());
  setCaption(
    `${data.period.label}. Остаток ср — из 1С, остаток пн — расчёт: остаток ср − заказ Ср–Вс + выпуск Ср–Вс. Потребление — за 3 прошлые недели. В базовых единицах (шт, кг). `,
    unset.length ? el("span", { class: "warn" }, `⚠ Не выбран источник: ${unset.join(", ")}. `) : null,
    missing.length ? el("span", { class: "warn" }, `⚠ Нет данных: ${missing.map(gap).join(", ")}`) : null,
  );
  const columns = [nameCol, unitCol, ...data.columns.map((c, i) => ({
    key: c.key,
    label: c.label,
    sub: c.sub + (c.covered >= c.of ? "" : c.of > 1 ? ` · ${c.covered}/${c.of} дн.` : " · нет данных"),
    subWarn: c.covered < c.of,
    num: true,
    // разделитель — на границе групп: остаток | заказ | выпуск | остаток
    sep: i === 0 || c.role !== data.columns[i - 1].role,
    cls: c.role === "stock" || c.role === "calc" ? "strong" : "",
    get: (r) => r.values[i],
    render: (r) => fmt(r.values[i]),
    total: (rows) => fmt(sum(rows.map((r) => r.values[i]))),
  }))];
  grid($("#rc-table"), { columns, rows: filterRows(data.rows), view: "rc", onRowClick: (r) => openDetail(r.code) });
}

// ---------- по дням: один источник, товары × дни недели ----------

function renderDays() {
  const data = state.data.days;
  if (!data) return;
  renderCategories(data.rows);
  const cov = data.covered < 7 ? el("span", { class: "warn" }, ` ⚠ данные за ${data.covered} из 7 дн.`) : null;
  setCaption(
    `${data.source.name} · ${data.period.label}. «Итого нед.» — ${AGG[data.source.agg]}. Δ — к пред. неделе.`, cov);
  const columns = [nameCol, unitCol,
    ...data.days.map((d, i) => ({
      key: `d${i}`, label: d.short, num: true,
      get: (r) => r.days[i], render: (r) => fmt(r.days[i]), total: (rows) => fmt(sum(rows.map((r) => r.days[i]))),
    })),
    { key: "total", label: "Итого нед.", sub: AGG[data.source.agg], num: true, sep: true, cls: "strong",
      get: (r) => r.total, render: (r) => fmt(r.total), total: (rows) => fmt(sum(rows.map((r) => r.total))) },
    { key: "prev", label: "Пред. нед.", num: true, get: (r) => r.prev, render: (r) => fmt(r.prev),
      total: (rows) => fmt(sum(rows.map((r) => r.prev))) },
    { key: "delta", label: "Δ", num: true, get: (r) => r.delta, render: (r) => deltaNode(r.delta, r.prev) || "—",
      total: (rows) => {
        const c = sum(rows.map((r) => r.total));
        const p = sum(rows.map((r) => r.prev));
        return deltaNode(delta(c, p), p) || "—";
      } },
  ];
  grid($("#days-table"), { columns, rows: filterRows(data.rows), view: "days", onRowClick: (r) => openDetail(r.code) });
}

// ---------- динамика: один источник, товары × недели ----------

function renderTrend() {
  const data = state.data.trend;
  if (!data) return;
  renderCategories(data.rows);
  // Недели до первой загрузки пустые у всех — не тратим на них ширину.
  const first = Math.max(0, Math.min(data.weeks.length - 2,
    ...data.rows.map((r) => r.values.findIndex((v) => !isNil(v))).filter((i) => i >= 0)));
  const w = data.weeks.slice(first);
  const rows = data.rows.map((r) => ({ ...r, values: r.values.slice(first) }));
  setCaption(
    `${data.source.name} · ${AGG[data.source.agg]} по неделям, ${w[0].label} — ${w.at(-1).label}.`);
  const columns = [nameCol, unitCol,
    ...w.map((wk, i) => ({
      key: `w${i}`, label: wk.iso.replace(/^\d+-/, ""), sub: `${wk.start.slice(8, 10)}.${wk.start.slice(5, 7)}`,
      num: true, cls: i === w.length - 1 ? "strong" : "",
      get: (r) => r.values[i], render: (r) => fmt(r.values[i]), total: (rows) => fmt(sum(rows.map((r) => r.values[i]))),
    })),
    { key: "delta", label: "Δ посл. нед.", num: true, sep: true, get: (r) => r.delta,
      render: (r) => deltaNode(r.delta, r.values.at(-2)) || "—",
      total: (rows) => {
        const c = sum(rows.map((r) => r.values.at(-1)));
        const p = sum(rows.map((r) => r.values.at(-2)));
        return deltaNode(delta(c, p), p) || "—";
      } },
  ];
  grid($("#trend-table"), { columns, rows: filterRows(rows), view: "trend", onRowClick: (r) => openDetail(r.code) });
}

function renderCurrent() {
  ({ pivot: renderPivot, rc: renderRc, days: renderDays, trend: renderTrend })[state.tab]?.();
}

// ---------- загрузка данных вкладок ----------

async function load() {
  renderFilters();
  if (!TABLE_TABS.includes(state.tab)) return;
  if (!state.meta.days.length) {
    $("#caption").textContent = "Данных пока нет — загрузи xlsx на вкладке «Загрузка».";
    for (const t of ["#pivot-table", "#rc-table", "#days-table", "#trend-table"]) $(t).replaceChildren();
    return;
  }
  if (!state.sourceId && state.meta.sources.length) state.sourceId = state.meta.sources[0].id;
  try {
    if (state.tab === "pivot") {
      state.data.pivot = await api(`api/pivot${qs({ mode: state.mode, ...pivotPeriod() })}`);
    } else if (state.tab === "rc") {
      state.data.rc = await api(`api/rc${qs({ date: state.date })}`);
    } else if (state.tab === "days") {
      state.data.days = await api(`api/by-days${qs({ source_id: state.sourceId, date: state.date })}`);
    } else {
      state.data.trend = await api(`api/trend${qs({ source_id: state.sourceId, end: state.date, count: state.trendCount })}`);
    }
    renderCurrent();
    if (state.detail) loadDetail();
  } catch (err) {
    $("#caption").textContent = `Ошибка: ${err.message}`;
  }
}

async function loadMeta() {
  state.meta = await api("api/meta");
  if (!state.date || !state.meta.days.some((d) => d.date === state.date)) state.date = defaultDate();
  if (state.sourceId && !state.meta.sources.some((s) => s.id === state.sourceId)) state.sourceId = null;
}

// ---------- карточка товара ----------

function openDetail(code) {
  state.detail = code;
  for (const tr of $$("tr.item")) tr.classList.toggle("active", tr.dataset.code === code);
  loadDetail(true);
}

async function loadDetail(scroll = false) {
  // В режиме «Период» карточка — по неделе последнего дня периода.
  const date = isRange() ? state.range.to : state.date;
  const data = await api(`api/item/${encodeURIComponent(state.detail)}${qs({ date })}`);
  const panel = $("#detail");
  panel.hidden = false;
  $("#detail-cat").textContent = `${data.item.category} · код ${data.item.code}${data.item.pack ? ` · упак ${data.item.pack} ${data.item.unit}` : ""}`;
  $("#detail-title").textContent = data.item.name;
  $("#detail-week").textContent = `${data.period.label}, ${data.item.unit}`;

  const head = el("tr", {}, el("th", {}, "Источник"), ...data.days.map((d) => el("th", {}, d.short)), el("th", { class: "sep" }, "Итого нед."));
  const body = el("tbody", {}, ...data.sources.map((s) => el("tr", {},
    el("td", {}, s.name, el("span", { class: "code" }, (s.label || AGG[s.agg]) + (s.as_of ? ` · пн ${ddmm(s.as_of)}` : ""))),
    ...s.days.map((v) => el("td", { class: `num ${isNil(v) ? "nil" : ""}` }, fmt(v))),
    el("td", { class: "num strong sep" }, fmt(s.total)),
  )));
  $("#detail-table").replaceChildren(el("thead", {}, head), body);

  const withData = data.sources.filter((s) => s.weeks.some((w) => !isNil(w.value)));
  const sel = $("#detail-source");
  sel.replaceChildren(...withData.map((s) => el("option", { value: s.id }, s.name)));
  if (!withData.some((s) => s.id === state.detailSource)) state.detailSource = withData[0]?.id ?? null;
  sel.value = String(state.detailSource ?? "");
  const src = withData.find((s) => s.id === state.detailSource);
  if (src) {
    const firstIdx = Math.min(...withData.map((s) => s.weeks.findIndex((w) => !isNil(w.value))));
    drawColumns($("#detail-chart"), src.weeks.slice(Math.min(firstIdx, src.weeks.length - 4)).map((w) => ({
      key: w.iso.replace(/^\d+-/, ""), value: w.value, tip: w.label, current: w.start === monday(date),
    })));
  } else {
    $("#detail-chart").replaceChildren(el("p", { class: "hint" }, "Нет данных за 12 недель"));
  }
  if (scroll) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- график: столбцы по неделям (одна серия) ----------

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function niceScale(values) {
  const vals = values.filter((v) => !isNil(v));
  let lo = Math.min(0, ...vals);
  let hi = Math.max(0, ...vals);
  if (lo === hi) hi = lo + 1;
  const raw = (hi - lo) / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let t = lo; t <= hi + step / 2; t += step) ticks.push(Number(t.toPrecision(12)));
  return { lo, hi, ticks };
}

function drawColumns(container, points) {
  const W = Math.max(container.clientWidth || 600, 320);
  const H = 220;
  const M = { top: 18, right: 12, bottom: 24, left: 52 };
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Столбцы по неделям" });
  const { lo, hi, ticks } = niceScale(points.map((p) => p.value));
  const y = (v) => M.top + (H - M.top - M.bottom) * (1 - (v - lo) / (hi - lo));
  for (const t of ticks) {
    root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: M.left, x2: W - M.right, y1: y(t), y2: y(t) }));
    const lbl = svg("text", { class: "tick", x: M.left - 8, y: y(t) + 4, "text-anchor": "end" });
    lbl.textContent = nfCompact.format(t);
    root.append(lbl);
  }
  const band = (W - M.left - M.right) / points.length;
  const bw = Math.min(24, band * 0.6);
  const xOf = (i) => M.left + band * (i + 0.5);
  const base = y(Math.max(0, lo));
  const hasCurrent = points.some((p) => p.current);
  const lastIdx = points.map((p) => !isNil(p.value)).lastIndexOf(true);
  points.forEach((p, i) => {
    const hl = hasCurrent ? p.current : i === lastIdx;
    if (!isNil(p.value)) {
      const top = y(p.value);
      const r = Math.min(4, Math.abs(base - top), bw / 2);
      const x = xOf(i) - bw / 2;
      const s = p.value >= 0 ? 1 : -1;  // скругляем только «данные»-конец
      const d = `M${x},${base} V${top + s * r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + s * r} V${base} Z`;
      root.append(svg("path", { class: `bar ${hl ? "" : "dim"}`, d }));
      if (hl) {
        const t = svg("text", { class: "label", x: xOf(i), y: p.value >= 0 ? top - 6 : top + 14, "text-anchor": "middle" });
        t.textContent = nfCompact.format(p.value);
        root.append(t);
      }
    }
    const hit = svg("rect", { class: "hit", x: xOf(i) - band / 2, y: M.top, width: band, height: H - M.top - M.bottom });
    hit.addEventListener("mousemove", (e) => tip(e, p.tip, p.value));
    hit.addEventListener("mouseleave", hideTip);
    root.append(hit);
    const t = svg("text", { class: "tick", x: xOf(i), y: H - 6, "text-anchor": "middle" });
    t.textContent = p.key;
    root.append(t);
  });
  container.replaceChildren(root);
}

function tip(e, label, value) {
  const t = $("#tooltip");
  t.replaceChildren(el("div", { class: "muted" }, label), el("b", {}, fmt(value)));
  t.hidden = false;
  t.style.left = `${Math.min(e.clientX + 12, window.innerWidth - t.offsetWidth - 8)}px`;
  t.style.top = `${e.clientY + 12}px`;
}
function hideTip() { $("#tooltip").hidden = true; }

// ---------- загрузка файлов ----------

let pending = [];

async function addFiles(files) {
  const list = [...files].filter((f) => /\.xls[xm]$/i.test(f.name));
  if (!list.length) return;
  $("#sources-list").replaceChildren(...state.meta.sources.map((s) => el("option", { value: s.name })));
  for (const file of list) {
    const { source } = await api(`api/source-name${qs({ filename: file.name })}`);
    pending.push({ file, source });
  }
  renderPending();
}

function renderPending() {
  $("#pending-table tbody").replaceChildren(...pending.map((p, i) => el("tr", {},
    el("td", {}, p.file.name),
    el("td", {}, el("input", {
      type: "text", value: p.source, list: "sources-list",
      oninput: (e) => { pending[i].source = e.target.value; },
    })),
    el("td", {}, el("button", {
      class: "btn icon", type: "button", "aria-label": "Убрать файл",
      onclick: () => { pending.splice(i, 1); renderPending(); },
    }, "✕")),
  )));
  $("#upload-form").hidden = pending.length === 0;
}

async function submitUpload(e) {
  e.preventDefault();
  const btn = $("#upload-form button[type=submit]");
  btn.disabled = true;
  btn.textContent = "Загружаю…";
  const form = new FormData();
  for (const p of pending) { form.append("files", p.file); form.append("sources", p.source); }
  const out = $("#upload-results");
  try {
    const { results } = await api("api/upload", { method: "POST", body: form });
    out.replaceChildren(...results.map((r) => (r.ok
      ? el("div", { class: "result" },
          el("b", {}, r.file), ` → «${r.source}»: ${r.rows} строк, ${r.items} товаров, ${r.date_from} — ${r.date_to}`,
          r.replaced ? ` (заменено ${r.replaced} старых записей)` : "",
          ...r.warnings.map((w) => el("div", { class: "warn" }, w)))
      : el("div", { class: "result err" }, el("b", {}, r.file), `: ошибка — ${r.error}`))));
    pending = [];
    renderPending();
    if (results.some((r) => r.ok)) state.date = null;  // перейти на свежую неделю
    await loadMeta();
    loadUploads();
  } catch (err) {
    out.replaceChildren(el("div", { class: "result err" }, `Ошибка загрузки: ${err.message}`));
  } finally {
    btn.disabled = false;
    btn.textContent = "Загрузить";
  }
}

// ---------- автозагрузка с почты ----------

const ddmmyyyy = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;

function when(iso) {
  const d = new Date(iso);
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString("ru-RU")} ${time}`;
}

function ago(iso) {
  const min = Math.round((Date.now() - new Date(iso)) / 60000);
  return min < 1 ? "только что" : min < 60 ? `${min} мин назад` : `${Math.floor(min / 60)} ч ${min % 60} мин назад`;
}

function mailEvent(e) {
  if (e.kind === "skip") {
    return el("li", { class: "warn" }, `${when(e.at)} ⚠ Письмо от ${e.sender} «${e.subject}» пропущено: ${e.reason}`);
  }
  if (!e.ok) return el("li", { class: "err" }, `${when(e.at)} ✕ ${e.file}: ${e.error}`);
  return el("li", {}, `${when(e.at)} ✓ `, el("b", {}, e.file), ` → «${e.source}», `,
    `${ddmmyyyy(e.date_from)}–${ddmmyyyy(e.date_to)}, ${fmt(e.rows)} строк`);
}

async function loadMail() {
  const box = $("#mail-status");
  let m;
  try { m = await api("api/mail"); } catch { box.hidden = true; return; }
  box.hidden = false;
  if (!m.enabled) {
    box.replaceChildren(el("h2", {}, "Почта"), el("p", { class: m.error ? "err" : "muted" },
      m.error ? `Автозагрузка выключена: ${m.error}` : "Автозагрузка с почты не настроена (MAIL_HOST в .env)."));
    return;
  }
  const every = Math.round(m.interval / 60);
  const next = m.checked_at ? new Date(new Date(m.checked_at).getTime() + m.interval * 1000).toISOString() : null;
  box.replaceChildren(...[
    el("h2", {}, "Почта"),
    el("p", {}, `Проверка ящика каждые ${every} мин. `,
      m.checked_at ? `Последняя: ${when(m.checked_at)} (${ago(m.checked_at)}), следующая ≈ ${when(next)}.` : "Первая проверка идёт…"),
    m.error ? el("p", { class: "err" }, `Последняя проверка не удалась: ${m.error}`) : null,
    m.events.length
      ? el("ul", { class: "events" }, ...m.events.map(mailEvent))
      : el("p", { class: "muted" }, "С момента запуска сервиса писем с файлами не было — полная история ниже."),
  ].filter(Boolean));
}

async function loadUploads() {
  const rows = await api("api/uploads");
  const head = el("tr", {}, ...["Файл", "Источник", "Откуда", "Период", "Строк", "Загружен", ""].map((h) => el("th", {}, h)));
  const body = el("tbody", {}, ...rows.map((u) => el("tr", {},
    el("td", {}, u.filename),
    el("td", {}, u.source),
    el("td", {}, u.via === "mail" ? "почта" : "вручную"),
    el("td", {}, `${u.date_from} — ${u.date_to}`),
    el("td", { class: "num" }, fmt(u.rows)),
    el("td", {}, new Date(u.uploaded_at).toLocaleString("ru-RU")),
    el("td", {}, el("button", {
      class: "btn small", onclick: async () => {
        if (!confirm(`Удалить загрузку «${u.filename}» и её данные?`)) return;
        await api(`api/uploads/${u.id}`, { method: "DELETE" });
        await loadMeta();
        loadUploads();
      },
    }, "Удалить")),
  )));
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 7, class: "nil" }, "Пока ничего не загружено")));
  $("#uploads-table").replaceChildren(el("thead", {}, head), body);
}

// ---------- источники ----------

async function patchSource(id, body) {
  try {
    await api(`api/sources/${id}`, {
      method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (err) {
    alert(err.message);
  }
  await loadMeta();
  renderSources();
}

function renderSources() {
  const list = state.meta.sources;
  const head = el("tr", {}, ...["Порядок", "Название (колонка)", "Как считать период", "Колонка «на конец»", "Скрыть", "Данные", "Загрузок", ""]
    .map((h) => el("th", {}, h)));
  const body = el("tbody", {}, ...list.map((s, i) => el("tr", {},
    el("td", {},
      el("button", { class: "btn small", disabled: i === 0, "aria-label": "Выше", onclick: () => swap(i, i - 1) }, "↑"), " ",
      el("button", { class: "btn small", disabled: i === list.length - 1, "aria-label": "Ниже", onclick: () => swap(i, i + 1) }, "↓")),
    el("td", {}, el("input", {
      type: "text", value: s.name,
      title: s.aliases.length ? `Файлы с прежними именами тоже идут сюда: ${s.aliases.join(", ")}` : null,
      onchange: (e) => patchSource(s.id, { name: e.target.value }),
    })),
    el("td", {}, el("select", { onchange: (e) => patchSource(s.id, { agg: e.target.value }) },
      el("option", { value: "sum", selected: s.agg === "sum" }, "Сумма за период"),
      el("option", { value: "last", selected: s.agg === "last" }, "Остаток: на начало и на конец периода"),
      el("option", { value: "end", selected: s.agg === "end" }, "Заказ: на текущий и на будущий период (срезы на первый день этого и следующего)"))),
    el("td", {}, s.agg === "last"
      ? el("input", {
          type: "text", value: s.close_name || "", placeholder: s.close_label,
          onchange: (e) => patchSource(s.id, { close_name: e.target.value }),
        })
      : el("span", { class: "muted" }, "—")),
    el("td", {}, el("input", { type: "checkbox", checked: s.hidden, onchange: (e) => patchSource(s.id, { hidden: e.target.checked }) })),
    el("td", {}, s.date_from ? `${s.date_from} — ${s.date_to}` : "—"),
    el("td", { class: "num" }, s.uploads),
    el("td", {}, el("button", {
      class: "btn small", onclick: async () => {
        if (!confirm(`Удалить источник «${s.name}» со всеми загрузками?`)) return;
        await api(`api/sources/${s.id}`, { method: "DELETE" });
        await loadMeta();
        renderSources();
      },
    }, "Удалить")),
  )));
  if (!list.length) body.append(el("tr", {}, el("td", { colspan: 8, class: "nil" }, "Источники появятся после первой загрузки")));
  $("#sources-table").replaceChildren(el("thead", {}, head), body);

  async function swap(a, b) {
    const [x, y] = [list[a], list[b]];
    // Позиции могут совпадать — раздаём по индексам, чтобы обмен точно сработал.
    await Promise.all([
      api(`api/sources/${x.id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ position: b }) }),
      api(`api/sources/${y.id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ position: a }) }),
      ...list.filter((_, i) => i !== a && i !== b).map((s) => api(`api/sources/${s.id}`, {
        method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ position: list.indexOf(s) }),
      })),
    ]);
    await loadMeta();
    renderSources();
  }
}

// ---------- вкладки и старт ----------

// Данные приходят и в фоне (автозагрузка с почты), поэтому периоды и источники
// перечитываем при каждом переходе на вкладку и при возврате на страницу.
async function refreshMeta() {
  const latest = state.meta.days[0]?.date;
  const onDefault = state.date === defaultDate();
  try { await loadMeta(); } catch { return; }  // сеть моргнула — покажем, что было
  // Пользователь стоял на периоде по умолчанию — переезжает на новый, иначе его выбор не трогаем.
  if (onDefault && state.meta.days[0]?.date !== latest) state.date = defaultDate();
}

async function showTab(tab) {
  state.tab = tab;
  savePrefs();
  await refreshMeta();
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const s of $$(".tab")) s.hidden = s.id !== `tab-${tab}`;
  $("#detail").hidden = !state.detail || !TABLE_TABS.includes(tab);
  if (tab === "upload") { renderFilters(); loadMail(); loadUploads(); return; }
  if (tab === "sources") { renderFilters(); renderSources(); return; }
  load();
}

function bind() {
  for (const b of $$(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  for (const b of $$("#mode-toggle button")) {
    b.addEventListener("click", () => {
      if (state.mode === b.dataset.mode) return;
      state.mode = b.dataset.mode;
      if (state.mode === "day") state.date = lastDayInWeek(monday(state.date));
      savePrefs();
      load();
    });
  }
  $("#period-select").addEventListener("change", (e) => {
    state.date = state.tab === "pivot" && state.mode === "day" ? e.target.value : lastDayInWeek(e.target.value);
    load();
  });
  $("#range-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const [from, to] = [$("#range-from").value, $("#range-to").value].sort();  // ISO сортируется как даты
    state.range = { from, to };
    savePrefs();
    load();
  });
  $("#range-preset").addEventListener("change", (e) => {
    const r = presetRange(e.target.value);
    if (!r) return;
    state.range = r;
    savePrefs();
    load();
  });
  $("#period-prev").addEventListener("click", () => stepPeriod(-1));
  $("#period-next").addEventListener("click", () => stepPeriod(1));
  $("#source-select").addEventListener("change", (e) => { state.sourceId = Number(e.target.value); load(); });
  $("#trend-count").addEventListener("change", (e) => { state.trendCount = Number(e.target.value); savePrefs(); load(); });
  $("#category-select").addEventListener("change", (e) => { state.category = e.target.value; renderFilters(); renderCurrent(); });
  let t;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => { state.q = e.target.value; renderFilters(); renderCurrent(); }, 150);
  });
  $("#detail-close").addEventListener("click", () => {
    state.detail = null;
    $("#detail").hidden = true;
    for (const tr of $$("tr.item.active")) tr.classList.remove("active");
  });
  $("#detail-source").addEventListener("change", (e) => { state.detailSource = Number(e.target.value); loadDetail(); });

  const dz = $("#dropzone");
  $("#file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); addFiles(e.dataTransfer.files); });
  $("#upload-form").addEventListener("submit", submitUpload);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) showTab(state.tab); });
}

(async function init() {
  loadPrefs();
  bind();
  await loadMeta();
  if (!state.meta.days.length) state.tab = "upload";
  showTab(state.tab);
})().catch((err) => {
  $("main").prepend(el("div", { class: "result err" }, `Не удалось загрузить данные: ${err.message}`));
});
