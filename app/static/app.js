"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const AGG = { sum: "сумма", avg: "среднее", last: "последнее" };
const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const nfCompact = new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 });
const pf = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1, signDisplay: "exceptZero" });

const state = {
  tab: "week",
  week: null,          // понедельник выбранной недели, ISO-строка
  weeks: [],
  dimKey: "",
  dimValue: "",
  dims: {},
  trendCount: 8,
  chartMetric: null,
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

function dimParams() {
  return state.dimKey && state.dimValue ? { dim_key: state.dimKey, dim_value: state.dimValue } : {};
}

function fmt(v) {
  return v === null || v === undefined ? "—" : nf.format(v);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c instanceof Node ? c : String(c));
  return node;
}

function numCell(v, extra = "") {
  return el("td", { class: `num ${extra} ${v === null || v === undefined ? "nil" : ""}` }, fmt(v));
}

function deltaCell(d) {
  if (d === null || d === undefined) return el("td", { class: "num nil" }, "—");
  const dir = d > 0 ? "up" : d < 0 ? "down" : "";
  const arrow = d > 0 ? "▲ " : d < 0 ? "▼ " : "";
  return el("td", { class: "num" }, el("span", { class: `delta ${dir}` }, arrow + pf.format(d)));
}

function metricCell(row) {
  return el("td", {}, row.name, el("span", { class: "agg" }, AGG[row.agg]));
}

// ---------- вкладки ----------

function showTab(tab) {
  state.tab = tab;
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const s of $$(".tab")) s.hidden = s.id !== `tab-${tab}`;
  $("#chart-panel").hidden = !state.chartMetric || !["week", "trend"].includes(tab);
  try { localStorage.setItem("instrument.tab", tab); } catch { /* приватный режим */ }
  ({ week: loadWeek, trend: loadTrend, upload: loadUploads, metrics: loadMetrics })[tab]();
}

// ---------- фильтр по разрезу ----------

async function loadDims() {
  state.dims = await api("/api/dims");
  for (const box of $$(".dimfilter")) {
    const keySel = $(".dim-key", box);
    keySel.replaceChildren(
      el("option", { value: "" }, "Все данные"),
      ...Object.keys(state.dims).map((k) => el("option", { value: k }, k)),
    );
    keySel.value = state.dimKey;
    box.hidden = Object.keys(state.dims).length === 0;
    fillDimValues(box);
  }
}

function fillDimValues(box) {
  const valSel = $(".dim-value", box);
  const vals = state.dims[state.dimKey] || [];
  valSel.hidden = !state.dimKey;
  valSel.replaceChildren(...vals.map((v) => el("option", { value: v }, v)));
  if (state.dimKey && !vals.includes(state.dimValue)) state.dimValue = vals[0] || "";
  valSel.value = state.dimValue;
}

function bindDimFilters() {
  for (const box of $$(".dimfilter")) {
    $(".dim-key", box).addEventListener("change", (e) => {
      state.dimKey = e.target.value;
      state.dimValue = "";
      for (const b of $$(".dimfilter")) { $(".dim-key", b).value = state.dimKey; fillDimValues(b); }
      refresh();
    });
    $(".dim-value", box).addEventListener("change", (e) => {
      state.dimValue = e.target.value;
      for (const b of $$(".dimfilter")) $(".dim-value", b).value = state.dimValue;
      refresh();
    });
  }
}

function refresh() {
  if (state.tab === "week") loadWeek();
  if (state.tab === "trend") loadTrend();
  if (state.chartMetric) loadChart(state.chartMetric);
}

// ---------- неделя ----------

async function loadWeeks() {
  state.weeks = await api("/api/weeks");
  const sel = $("#week-select");
  sel.replaceChildren(...state.weeks.map((w) => el("option", { value: w.start }, w.label)));
  if (!state.week && state.weeks.length) state.week = state.weeks[0].start;
}

async function loadWeek() {
  const empty = state.weeks.length === 0;
  $("#week-empty").hidden = !empty;
  $("#week-table").closest(".table-wrap").hidden = empty;
  if (empty) return;

  const data = await api(`/api/week${qs({ start: state.week, ...dimParams() })}`);
  state.week = data.week.start;
  const sel = $("#week-select");
  if (![...sel.options].some((o) => o.value === data.week.start)) {
    sel.append(el("option", { value: data.week.start }, data.week.label));
  }
  sel.value = data.week.start;
  $("#week-prev").disabled = !data.prev_week;
  $("#week-next").disabled = !data.next_week;
  $("#week-prev").dataset.target = data.prev_week || "";
  $("#week-next").dataset.target = data.next_week || "";
  $("#week-export").href = `/api/export/week.xlsx${qs({ start: state.week, ...dimParams() })}`;

  const head = el("thead", {}, el("tr", {},
    el("th", {}, "Метрика"),
    ...data.days.map((d) => el("th", {}, d.label)),
    el("th", {}, "Итого нед."),
    el("th", {}, "Пред. нед."),
    el("th", {}, "Δ"),
  ));
  const body = el("tbody");
  const cols = data.days.length + 4;
  for (const g of data.groups) {
    body.append(el("tr", { class: "group" }, el("td", { colspan: cols }, g.source)));
    for (const r of g.rows) {
      body.append(el("tr", {
        class: `metric ${state.chartMetric === r.metric_id ? "active" : ""}`,
        "data-metric": r.metric_id,
        onclick: () => openChart(r.metric_id),
      },
        metricCell(r),
        ...r.days.map((v) => numCell(v)),
        numCell(r.total, "total"),
        numCell(r.prev),
        deltaCell(r.delta),
      ));
    }
  }
  if (!data.groups.length) {
    body.append(el("tr", {}, el("td", { colspan: cols, class: "nil" }, "За эту неделю данных нет")));
  }
  $("#week-table").replaceChildren(head, body);
}

// ---------- динамика ----------

async function loadTrend() {
  const data = await api(`/api/trend${qs({ count: state.trendCount, end: state.week, ...dimParams() })}`);
  const head = el("thead", {}, el("tr", {},
    el("th", {}, "Метрика"),
    ...data.weeks.map((w) => el("th", { title: `${w.start} — ${w.end}` }, w.iso.replace(/^\d+-/, ""))),
    el("th", {}, "Δ посл."),
  ));
  const body = el("tbody");
  const cols = data.weeks.length + 2;
  for (const g of data.groups) {
    body.append(el("tr", { class: "group" }, el("td", { colspan: cols }, g.source)));
    for (const r of g.rows) {
      body.append(el("tr", {
        class: `metric ${state.chartMetric === r.metric_id ? "active" : ""}`,
        "data-metric": r.metric_id,
        onclick: () => openChart(r.metric_id),
      },
        metricCell(r),
        ...r.values.map((v, i) => numCell(v, i === r.values.length - 1 ? "total" : "")),
        deltaCell(r.delta),
      ));
    }
  }
  if (!data.groups.length) body.append(el("tr", {}, el("td", { colspan: cols, class: "nil" }, "Нет данных")));
  $("#trend-table").replaceChildren(head, body);
}

// ---------- график ----------

function openChart(metricId) {
  state.chartMetric = metricId;
  for (const tr of $$("tr.metric")) tr.classList.toggle("active", Number(tr.dataset.metric) === metricId);
  loadChart(metricId);
}

async function loadChart(metricId) {
  const end = state.week || new Date().toISOString().slice(0, 10);
  const from = new Date(end);
  from.setDate(from.getDate() - 7 * 11);
  const data = await api(`/api/series/${metricId}${qs({
    date_from: from.toISOString().slice(0, 10), date_to: end, ...dimParams(),
  })}`);
  $("#chart-source").textContent = data.metric.source;
  $("#chart-title").textContent = `${data.metric.name} · ${AGG[data.metric.agg]}`;
  const panel = $("#chart-panel");
  panel.hidden = false;
  drawColumns($("#chart-weeks"), data.weeks.map((w) => ({
    key: w.iso.replace(/^\d+-/, ""), value: w.value, tip: w.label, current: w.start === state.week,
  })));
  // Дни — только последние 4 недели, иначе точки сливаются.
  drawLine($("#chart-days"), data.days.slice(-28).map((d) => ({
    key: d.date.slice(8, 10) + "." + d.date.slice(5, 7), value: d.value,
    tip: new Date(d.date).toLocaleDateString("ru-RU", { weekday: "short", day: "2-digit", month: "2-digit" }),
  })));
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function niceScale(values) {
  const vals = values.filter((v) => v !== null && v !== undefined);
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

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function frame(container, points, height = 220) {
  const W = Math.max(container.clientWidth || 600, 320);
  const M = { top: 16, right: 12, bottom: 24, left: 52 };
  const root = svg("svg", { viewBox: `0 0 ${W} ${height}`, role: "img" });
  const { lo, hi, ticks } = niceScale(points.map((p) => p.value));
  const y = (v) => M.top + (height - M.top - M.bottom) * (1 - (v - lo) / (hi - lo));
  for (const t of ticks) {
    root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: M.left, x2: W - M.right, y1: y(t), y2: y(t) }));
    const lbl = svg("text", { class: "tick", x: M.left - 8, y: y(t) + 4, "text-anchor": "end" });
    lbl.textContent = nfCompact.format(t);
    root.append(lbl);
  }
  container.replaceChildren(root);
  return { root, W, H: height, M, y, lo };
}

function xLabels(f, points, xOf) {
  const every = Math.ceil(points.length / Math.max(1, Math.floor((f.W - f.M.left) / 48)));
  points.forEach((p, i) => {
    if (i % every !== 0 && i !== points.length - 1) return;
    const t = svg("text", { class: "tick", x: xOf(i), y: f.H - 6, "text-anchor": "middle" });
    t.textContent = p.key;
    f.root.append(t);
  });
}

function drawColumns(container, points) {
  const f = frame(container, points);
  const band = (f.W - f.M.left - f.M.right) / points.length;
  const bw = Math.min(24, band * 0.6);
  const xOf = (i) => f.M.left + band * (i + 0.5);
  const base = f.y(Math.max(0, f.lo));
  const lastWithValue = points.map((p) => p.value !== null).lastIndexOf(true);
  points.forEach((p, i) => {
    if (p.value === null) return;
    const top = f.y(p.value);
    const h = Math.abs(base - top);
    const x = xOf(i) - bw / 2;
    const r = Math.min(4, h, bw / 2);
    // Скруглён только «данные»-конец, у базовой линии — прямой угол.
    const d = p.value >= 0
      ? `M${x},${base} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${base} Z`
      : `M${x},${base} V${top - r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top - r} V${base} Z`;
    f.root.append(svg("path", { class: `bar ${p.current || i === lastWithValue ? "" : "dim"}`, d }));
    const hit = svg("rect", { class: "hit", x: xOf(i) - band / 2, y: f.M.top, width: band, height: f.H - f.M.top - f.M.bottom });
    hit.addEventListener("mousemove", (e) => tip(e, p.tip, p.value));
    hit.addEventListener("mouseleave", hideTip);
    f.root.append(hit);
    if (p.current || (i === lastWithValue && !points.some((q) => q.current))) {
      const t = svg("text", { class: "label", x: xOf(i), y: (p.value >= 0 ? top - 6 : top + 14), "text-anchor": "middle" });
      t.textContent = nfCompact.format(p.value);
      f.root.append(t);
    }
  });
  xLabels(f, points, xOf);
}

function drawLine(container, points) {
  const f = frame(container, points, 200);
  const step = (f.W - f.M.left - f.M.right) / Math.max(1, points.length - 1);
  const xOf = (i) => f.M.left + step * i;
  // Разрывы в данных — разрывы линии, без интерполяции через пустые дни.
  let seg = [];
  const segs = [];
  points.forEach((p, i) => {
    if (p.value === null) { if (seg.length) segs.push(seg); seg = []; return; }
    seg.push([xOf(i), f.y(p.value)]);
  });
  if (seg.length) segs.push(seg);
  const base = f.y(Math.max(0, f.lo));
  for (const s of segs) {
    const line = s.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");
    f.root.append(svg("path", { class: "area", d: `${line} L${s.at(-1)[0]},${base} L${s[0][0]},${base} Z` }));
    f.root.append(svg("path", { class: "line", d: line }));
    if (s.length === 1) f.root.append(svg("circle", { class: "dot", cx: s[0][0], cy: s[0][1], r: 4 }));
  }
  const lastIdx = points.map((p) => p.value !== null).lastIndexOf(true);
  if (lastIdx >= 0) {
    const [x, y] = [xOf(lastIdx), f.y(points[lastIdx].value)];
    f.root.append(svg("circle", { class: "dot", cx: x, cy: y, r: 4 }));
    const t = svg("text", { class: "label", x: x - 6, y: y - 10, "text-anchor": "end" });
    t.textContent = nfCompact.format(points[lastIdx].value);
    f.root.append(t);
  }
  xLabels(f, points, xOf);

  // Перекрестие + подсказка по ближайшему дню.
  const cross = svg("line", { class: "cross", y1: f.M.top, y2: f.H - f.M.bottom, visibility: "hidden" });
  const dot = svg("circle", { class: "dot", r: 4, visibility: "hidden" });
  const hit = svg("rect", { class: "hit", x: f.M.left, y: 0, width: f.W - f.M.left - f.M.right, height: f.H });
  f.root.append(cross, dot, hit);
  hit.addEventListener("mousemove", (e) => {
    const box = f.root.getBoundingClientRect();
    const sx = (e.clientX - box.left) * (f.W / box.width);
    const i = Math.max(0, Math.min(points.length - 1, Math.round((sx - f.M.left) / step)));
    const p = points[i];
    cross.setAttribute("x1", xOf(i)); cross.setAttribute("x2", xOf(i));
    cross.setAttribute("visibility", "visible");
    if (p.value !== null) {
      dot.setAttribute("cx", xOf(i)); dot.setAttribute("cy", f.y(p.value));
      dot.setAttribute("visibility", "visible");
    } else dot.setAttribute("visibility", "hidden");
    tip(e, p.tip, p.value);
  });
  hit.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip();
  });
}

function tip(e, label, value) {
  const t = $("#tooltip");
  t.replaceChildren(el("div", { class: "muted" }, label), el("b", {}, fmt(value)));
  t.hidden = false;
  const x = Math.min(e.clientX + 12, window.innerWidth - t.offsetWidth - 8);
  t.style.left = `${x}px`;
  t.style.top = `${e.clientY + 12}px`;
}
function hideTip() { $("#tooltip").hidden = true; }

// ---------- загрузка ----------

let pending = [];

async function addFiles(files) {
  const list = [...files].filter((f) => /\.xls[xm]$/i.test(f.name));
  if (!list.length) return;
  const sources = await api("/api/sources");
  $("#sources-list").replaceChildren(...sources.map((s) => el("option", { value: s })));
  for (const file of list) {
    const { source } = await api(`/api/source-name${qs({ filename: file.name })}`);
    pending.push({ file, source });
  }
  renderPending();
}

function renderPending() {
  const tbody = $("#pending-table tbody");
  tbody.replaceChildren(...pending.map((p, i) => el("tr", {},
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
    const { results } = await api("/api/upload", { method: "POST", body: form });
    out.replaceChildren(...results.map((r) => r.ok
      ? el("div", { class: "result" },
          el("b", {}, r.file), ` → «${r.source}»: ${r.metrics} метрик, ${r.date_from} — ${r.date_to}`,
          r.replaced ? ` (заменено ${r.replaced} старых значений)` : "",
          ...r.warnings.map((w) => el("div", { class: "warn" }, w)))
      : el("div", { class: "result err" }, el("b", {}, r.file), `: ошибка — ${r.error}`)));
    pending = [];
    renderPending();
    if (results.some((r) => r.ok)) state.week = null;  // после загрузки — на самую свежую неделю
    await Promise.all([loadWeeks(), loadDims()]);
    loadUploads();
  } catch (err) {
    out.replaceChildren(el("div", { class: "result err" }, `Ошибка загрузки: ${err.message}`));
  } finally {
    btn.disabled = false;
    btn.textContent = "Загрузить";
  }
}

async function loadUploads() {
  const rows = await api("/api/uploads");
  const head = el("thead", {}, el("tr", {},
    el("th", {}, "Файл"), el("th", {}, "Источник"), el("th", {}, "Период"),
    el("th", {}, "Значений"), el("th", {}, "Загружен"), el("th", {}, ""),
  ));
  const body = el("tbody", {}, ...rows.map((u) => el("tr", {},
    el("td", {}, u.filename),
    el("td", {}, u.source),
    el("td", {}, `${u.date_from} — ${u.date_to}`),
    numCell(u.facts),
    el("td", {}, new Date(u.uploaded_at).toLocaleString("ru-RU")),
    el("td", {}, el("button", {
      class: "btn", onclick: async () => {
        if (!confirm(`Удалить загрузку «${u.filename}» и все её данные?`)) return;
        await api(`/api/uploads/${u.id}`, { method: "DELETE" });
        state.week = null;
        await Promise.all([loadWeeks(), loadDims()]);
        loadUploads();
      },
    }, "Удалить")),
  )));
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "nil" }, "Пока ничего не загружено")));
  $("#uploads-table").replaceChildren(head, body);
}

// ---------- метрики ----------

async function loadMetrics() {
  const rows = await api("/api/metrics");
  const patch = (id, body) => api(`/api/metrics/${id}`, {
    method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const head = el("thead", {}, el("tr", {},
    el("th", {}, "Источник"), el("th", {}, "Метрика в файле"), el("th", {}, "Отображаемое имя"),
    el("th", {}, "Агрегация"), el("th", {}, "Скрыть"), el("th", {}, "Последний день"),
  ));
  const body = el("tbody", {}, ...rows.map((m) => {
    const agg = el("select", { onchange: (e) => patch(m.id, { agg: e.target.value }) },
      ...Object.entries(AGG).map(([k, v]) => el("option", { value: k, selected: m.agg === k }, v)));
    return el("tr", {},
      el("td", {}, m.source),
      el("td", {}, m.name),
      el("td", {}, el("input", {
        type: "text", value: m.label || "", placeholder: m.name,
        onchange: (e) => patch(m.id, { label: e.target.value }),
      })),
      el("td", {}, agg),
      el("td", {}, el("input", {
        type: "checkbox", checked: m.hidden,
        onchange: (e) => patch(m.id, { hidden: e.target.checked }),
      })),
      el("td", {}, m.last_day || "—"),
    );
  }));
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "nil" }, "Метрик пока нет")));
  $("#metrics-table").replaceChildren(head, body);
}

// ---------- старт ----------

function bind() {
  for (const b of $$(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  $("#week-select").addEventListener("change", (e) => { state.week = e.target.value; refresh(); });
  for (const id of ["#week-prev", "#week-next"]) {
    $(id).addEventListener("click", (e) => {
      if (e.currentTarget.dataset.target) { state.week = e.currentTarget.dataset.target; refresh(); }
    });
  }
  $("#trend-count").addEventListener("change", (e) => { state.trendCount = Number(e.target.value); loadTrend(); });
  $("#chart-close").addEventListener("click", () => {
    state.chartMetric = null;
    $("#chart-panel").hidden = true;
    for (const tr of $$("tr.metric.active")) tr.classList.remove("active");
  });

  const dz = $("#dropzone");
  $("#file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); addFiles(e.dataTransfer.files); });
  $("#upload-form").addEventListener("submit", submitUpload);
  bindDimFilters();
  window.addEventListener("resize", () => { if (state.chartMetric && !$("#chart-panel").hidden) loadChart(state.chartMetric); });
}

(async function init() {
  bind();
  await Promise.all([loadWeeks(), loadDims()]);
  let tab = "week";
  try { tab = localStorage.getItem("instrument.tab") || "week"; } catch { /* нет доступа */ }
  if (!state.weeks.length) tab = "upload";
  showTab(tab);
})().catch((err) => {
  document.querySelector("main").prepend(el("div", { class: "result err" }, `Не удалось загрузить данные: ${err.message}`));
});
