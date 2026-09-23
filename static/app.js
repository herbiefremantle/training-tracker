"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const KM_PER_MI = 1.609344;

function readUnits() {
  try { return localStorage.getItem("units") === "mi" ? "mi" : "km"; } catch (_) { return "km"; }
}

const state = {
  status: null, dashboard: null,
  weekData: null, weekStart: null,          // weekStart null = the current week
  calData: null, calMonth: null,            // calMonth null = the current month
  exploreData: null, explore: { scope: "year", anchor: null, sport: null },
  planSessions: null, admin: null,
  units: readUnits(),                       // "km" | "mi" - distances are stored in km, converted for display
  paceMode: null,                           // "pace" | "speed"; null = default for the selected sport
  charts: {}, flash: null,
};

// ---------- formatting -------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
  if (res.status === 401) {              // session expired or never started: go and log in
    location.assign("/login");
    throw new Error("Login required");
  }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : d ? "Invalid request" : `Request failed (${res.status})`);
  }
  return data;
}

const utc = (iso) => Date.parse(iso + "T00:00:00Z");
const fmtDay = (iso) => new Date(utc(iso)).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const fmtShort = (iso) => new Date(utc(iso)).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const fmtWeekdayNum = (iso) => new Date(utc(iso)).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", timeZone: "UTC" });
const dayNum = (iso) => Math.floor(utc(iso) / 864e5);
const isoAdd = (iso, n) => new Date(utc(iso) + n * 864e5).toISOString().slice(0, 10);
const isoFromDayNum = (n) => new Date(n * 864e5).toISOString().slice(0, 10);

const dist = (km) => (state.units === "mi" ? km / KM_PER_MI : km);
const fmtDist = (km) => (!km ? "–" : `${+dist(km).toFixed(1)} ${state.units}`);
const fmtMetres = (m) => (!m ? "–" : `${Math.round(m).toLocaleString("en-GB")} m`);
function fmtMins(m) {
  if (m == null) return "–";
  const t = Math.round(m), h = Math.floor(t / 60), mm = t % 60;
  return h ? `${h}h ${String(mm).padStart(2, "0")}` : `${mm} min`;
}
const fmtHours = (h) => (h ? fmtMins(h * 60) : "–");
const dot = (parts) => parts.filter(Boolean).join(" · ");
function fmtPace(min) {
  let m = Math.floor(min), s = Math.round((min - m) * 60);
  if (s === 60) { m += 1; s = 0; }
  return `${m}:${String(s).padStart(2, "0")}`;
}
// speed in m/s -> pace (min per km|mi) or speed (km/h | mph)
function toUnit(ms, mode) {
  if (!ms) return null;
  if (mode === "pace") return (state.units === "mi" ? 1609.344 : 1000) / 60 / ms;
  return ms * (state.units === "mi" ? 2.236936 : 3.6);
}
const speedLabel = () => (state.units === "mi" ? "mph" : "km/h");
const fmtSpeed = (v, mode) => (v == null ? "–" : mode === "pace" ? `${fmtPace(v)} /${state.units}` : `${v.toFixed(1)} ${speedLabel()}`);
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const STATUS = {
  done: ["✓", "Done"], over: ["▲", "Over"], under: ["▼", "Under"], missed: ["✕", "Missed"], extra: ["+", "Extra"],
  pending: ["◷", "Today"], upcoming: ["○", "Upcoming"], rest: ["–", "Rest"],
};
const STATUS_TIP = {
  over: "Longer than planned by more than the leeway (overtrained)",
  under: "Shorter than planned by more than the leeway (undertrained)",
};
function pill(st, diff) {
  let text = STATUS[st][1];
  if ((st === "over" || st === "under") && diff != null) text += ` ${diff > 0 ? "+" : "−"}${Math.abs(diff)} min`;
  const tip = STATUS_TIP[st] ? ` title="${STATUS_TIP[st]}"` : "";
  return `<span class="pill ${st}"${tip}><i aria-hidden="true">${STATUS[st][0]}</i>${esc(text)}</span>`;
}

// ---------- status bar / banner ----------------------------------------------------------------

async function refreshStatus() {
  state.status = await api("/api/status");
  const s = state.status;
  $("#sync-meta").textContent = s.connected
    ? (s.last_sync ? "Last sync " + new Date(s.last_sync).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "Never synced")
    : "";
  $("#logout-form").hidden = !s.auth_enabled;
  $("#whoami").hidden = !s.username;
  $("#whoami").textContent = s.username ? `Logged in as ${s.display_name || s.username}` : "";
  $("#admin-link").hidden = !s.is_admin;
  $("#sync-btn").disabled = !(s.configured && s.connected);
  $("#sync-btn").title = !s.configured ? "Add your Strava credentials to .env first" : !s.connected ? "Connect Strava first" : "";
}

function setFlash(kind, html) { state.flash = { kind, html }; renderBanner(); }

function renderBanner() {
  const s = state.status, out = [];
  if (state.flash) out.push(`<div class="banner ${state.flash.kind}" role="status">${state.flash.html}</div>`);
  if (s && !s.configured) {
    out.push(`<div class="banner info"><h3>Set up Strava</h3>
      <ol>
        <li>Create an API app at <a href="https://www.strava.com/settings/api" target="_blank" rel="noopener">strava.com/settings/api</a>
            and set <b>Authorization Callback Domain</b> to <code>localhost</code>.</li>
        <li>Copy <code>.env.example</code> to <code>.env</code> and fill in <code>STRAVA_CLIENT_ID</code> and <code>STRAVA_CLIENT_SECRET</code>.</li>
        <li>Restart the server, then come back here and click <b>Connect with Strava</b>.</li>
      </ol></div>`);
  } else if (s && !s.connected) {
    out.push(`<div class="banner info"><h3>Connect your Strava account</h3>
      <p>Log in once so the app can read your activities. Your tokens stay in the local database.</p>
      <p><a class="btn primary" style="text-decoration:none;display:inline-block" href="/auth/login">Connect with Strava</a></p></div>`);
  } else if (s && s.connected && s.activity_count === 0 && !state.flash) {
    out.push(`<div class="banner info"><p>Connected${s.athlete ? " as <b>" + esc(s.athlete) + "</b>" : ""}. Click <b>Sync Strava</b> to pull your activities.</p></div>`);
  }
  $("#banner").innerHTML = out.join("");
}

// ---------- router -----------------------------------------------------------------------------

function route() {
  const r = location.hash.startsWith("#/plan") ? "plan" : location.hash.startsWith("#/admin") ? "admin" : "dashboard";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === r));
  $("#view-dashboard").hidden = r !== "dashboard";
  $("#view-plan").hidden = r !== "plan";
  $("#view-admin").hidden = r !== "admin";
  if (r === "plan") return loadPlan();
  if (r === "admin") return loadAdmin();
  return loadDashboard();
}

// ---------- sync -------------------------------------------------------------------------------

async function doSync() {
  const btn = $("#sync-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Syncing…';
  let flash;
  try {
    const r = await api("/api/sync", { method: "POST" });
    let msg = `Synced ${r.fetched} ${r.fetched === 1 ? "activity" : "activities"} (${r.new} new${r.removed ? `, ${r.removed} removed` : ""}).`;
    if (r.full_history) msg = "First sync pulled your full history. " + msg;
    if (r.suffer_lookups) msg += ` Looked up Strava suffer score for ${r.suffer_lookups}.`;
    if (r.suffer_lookups_remaining) msg += ` ${r.suffer_lookups_remaining} more to look up - sync again to continue (rate-limit friendly).`;
    flash = ["ok", esc(msg)];
  } catch (e) {
    flash = ["error", "<b>Sync failed.</b> " + esc(e.message)];
  }
  btn.textContent = "Sync Strava";
  await refreshStatus();
  await route();
  setFlash(...flash);
}

// ---------- dashboard: loading -----------------------------------------------------------------

const DASHBOARD_SKELETON = `<div class="stack">
  <div class="kpis" id="kpis"></div>
  <div class="card" id="week-card"></div>
  <div class="card" id="cal-card"></div>
  <div class="card" id="upcoming-card"></div>
  <div class="card" id="load-card"></div>
  <div class="stack" id="explore"></div>
</div>`;

const exploreUrl = () => {
  const x = state.explore;
  return `/api/explore?scope=${x.scope}${x.anchor ? "&anchor=" + x.anchor : ""}&sport=${encodeURIComponent(x.sport || "all")}`;
};

async function loadDashboard() {
  const el = $("#view-dashboard");
  try {
    const d = await api("/api/dashboard");
    state.dashboard = d;
    if (!state.explore.sport) state.explore.sport = d.default_sport;
    if (!$("#kpis")) el.innerHTML = DASHBOARD_SKELETON;
    // keep the user's place in the week / calendar / drill-down when the page refreshes (e.g. after a sync)
    const [week, cal, ex] = await Promise.all([
      state.weekStart ? api("/api/week?start=" + state.weekStart) : Promise.resolve(d.week),
      api("/api/calendar" + (state.calMonth ? "?month=" + state.calMonth : "")),
      api(exploreUrl()),
    ]);
    state.weekData = week; state.calData = cal; state.exploreData = ex;
    renderAll();
  } catch (e) {
    el.innerHTML = `<div class="banner error">Couldn't load the dashboard: ${esc(e.message)}</div>`;
  }
}

async function loadWeek(start) {
  state.weekData = await api("/api/week" + (start ? "?start=" + start : ""));
  state.weekStart = state.weekData.is_current ? null : state.weekData.start;
  renderWeek();
}

async function loadCalendar(month) {
  state.calData = await api("/api/calendar" + (month ? "?month=" + month : ""));
  state.calMonth = state.calData.is_current ? null : state.calData.month;
  renderCalendar();
}

async function loadExplore() {
  try {
    state.exploreData = await api(exploreUrl());
    renderExplore();
  } catch (e) {
    $("#explore").innerHTML = `<div class="banner error">Couldn't load trends: ${esc(e.message)}</div>`;
  }
}

function renderAll() {
  renderKpis(); renderWeek(); renderCalendar(); renderUpcoming(); renderLoad(); renderExplore();
}

// ---------- dashboard: KPIs, week, calendar, upcoming, load ------------------------------------

function renderKpis() {
  const d = state.dashboard, L = d.load, c = d.week.counts, T = d.week.totals, ratio = L.ratio;
  const flag = { high: ["✕", "High load risk"], low: ["▼", "Low load"], ok: ["✓", "In range"] }[L.flag];
  const parts = [c.done && `${c.done} on target`, c.over && `${c.over} over`, c.under && `${c.under} under`, c.missed && `${c.missed} missed`, c.extra && `${c.extra} extra`];
  const weekFoot = c.planned || c.extra ? dot(parts) || "Nothing done yet" : "No plan this week";
  $("#kpis").innerHTML = `
    <div class="card kpi hero"><div class="label">Load ratio</div>
      <div class="value">${ratio == null ? "–" : ratio.toFixed(2)}</div>
      <div class="foot">${flag ? `<span class="pill ${L.flag}"><i aria-hidden="true">${flag[0]}</i>${flag[1]}</span>` : "Not enough data yet"}</div>
      <div class="foot">7-day ÷ 28-day average. High above ${d.thresholds.high}, low below ${d.thresholds.low}</div></div>
    <div class="card kpi"><div class="label">Distance this week</div>
      <div class="value">${+dist(T.distance_km).toFixed(1)}<span class="unit"> ${state.units}</span></div>
      <div class="foot">${esc(T.planned_distance_km ? `Planned this week: ${fmtDist(T.planned_distance_km)}` : c.planned ? "No distance planned this week" : "No plan this week")}</div></div>
    <div class="card kpi"><div class="label">Time this week</div>
      <div class="value">${fmtMins(T.minutes)}</div>
      <div class="foot">${esc(T.planned_minutes ? `Planned this week: ${fmtMins(T.planned_minutes)}` : c.planned ? "No duration planned this week" : "No plan this week")}</div></div>
    <div class="card kpi"><div class="label">This week</div><div class="value">${c.completed}<span class="muted"> / ${c.planned}</span></div>
      <div class="foot">${esc(weekFoot)}</div></div>`;
}

function sessionItem(s) {
  const plan = dot([s.planned_distance_km && fmtDist(s.planned_distance_km), s.planned_duration_min && fmtMins(s.planned_duration_min)]);
  let meta = plan ? `Plan: ${plan}` : "";
  if (s.activity) {
    const a = s.activity;
    const actual = dot([a.distance_km && fmtDist(a.distance_km), a.duration_min && fmtMins(a.duration_min), a.elevation_m ? `${Math.round(a.elevation_m)} m ↑` : ""]);
    meta += `${meta ? "<br>" : ""}Strava: ${esc(a.name)}${actual ? " — " + actual : ""}${s.completion_pct != null ? ` (${s.completion_pct}% of plan)` : ""}`;
  }
  if (s.notes) meta += `${meta ? "<br>" : ""}<i>${esc(s.notes)}</i>`;
  const sameLabel = (s.session_type || "").toLowerCase() === s.sport_label.toLowerCase();
  return `<div class="item ${s.status}">
    <div class="top"><span class="title">${esc(s.session_type || s.sport_label)}${sameLabel ? "" : ` <span class="muted">· ${esc(s.sport_label)}</span>`}</span>${pill(s.status, s.duration_diff_min)}</div>
    ${meta ? `<div class="meta">${meta}</div>` : ""}</div>`;
}

function extraItem(a) {
  const meta = dot([a.sport_label, a.distance_km && fmtDist(a.distance_km), a.duration_min && fmtMins(a.duration_min), a.elevation_m ? `${Math.round(a.elevation_m)} m ↑` : ""]);
  return `<div class="item extra"><div class="top"><span class="title">${esc(a.name)}</span>${pill("extra")}</div>
    <div class="meta">${esc(meta)}</div></div>`;
}

function countChips(c) {
  const todo = (c.pending || 0) + (c.upcoming || 0);
  const chips = [["done", c.done, "on target"], ["over", c.over, "over"], ["under", c.under, "under"], ["missed", c.missed, "missed"],
    ["upcoming", todo, "still to do"], ["extra", c.extra, "extra"]];
  return chips.filter(([, n]) => n).map(([k, n, l]) => `<span class="pill ${k}"><i aria-hidden="true">${STATUS[k][0]}</i>${n} ${l}</span>`).join("");
}

function renderWeek() {
  const w = state.weekData, c = w.counts;
  const rows = w.days.map((day) => `
    <div class="day-row ${day.is_today ? "today" : ""}">
      <div class="day-name">${day.weekday}<small>${fmtShort(day.date)}${day.is_today ? " · today" : ""}</small></div>
      <div class="items">${day.items.length
        ? day.items.map((i) => (i.kind === "planned" ? sessionItem(i) : extraItem(i))).join("")
        : '<span class="none-day">Nothing planned or logged</span>'}</div>
    </div>`).join("");
  const chips = countChips(c);
  $("#week-card").innerHTML = `
    <div class="card-head"><h2>Week: planned vs actual</h2>
      <div class="nav">
        <button class="btn icon" type="button" data-act="weekNav" data-to="${w.prev}" aria-label="Previous week">‹</button>
        <span class="nav-label">${fmtShort(w.start)} – ${fmtShort(w.end)}${w.is_current ? " · this week" : ""}</span>
        <button class="btn icon" type="button" data-act="weekNav" data-to="${w.next}" aria-label="Next week">›</button>
        ${w.is_current ? "" : '<button class="btn small" type="button" data-act="weekNav" data-to="">This week</button>'}
      </div></div>
    ${chips ? `<div class="summary-line">${chips}</div>` : ""}
    ${rows}
    <div class="caption">Done = within ±${w.tolerance_min} min of the planned duration. ▲ Over / ▼ Under = more than ${w.tolerance_min} min longer / shorter than planned (the session still counts as completed).</div>`;
}

const CAL_TIPS = {
  done: "on target", over: "over plan", under: "under plan", missed: "missed", pending: "today, not done yet", upcoming: "upcoming",
};

function renderCalendar() {
  const c = state.calData;
  const heads = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="cal-head">${d}</div>`).join("");
  const cells = c.days.map((d) => {
    const dots = d.sessions.map((s) => `<span class="dot ${s.status}" aria-hidden="true">${["done", "over", "under", "missed"].includes(s.status) ? STATUS[s.status][0] : ""}</span>`)
      .concat(d.extras.map(() => `<span class="dot extra" aria-hidden="true">+</span>`)).join("");
    const desc = d.sessions.map((s) => `${s.label} (${s.sport}): ${CAL_TIPS[s.status]}${s.diff && (s.status === "over" || s.status === "under") ? ` ${s.diff > 0 ? "+" : "−"}${Math.abs(s.diff)} min` : ""}`)
      .concat(d.extras.map((e) => `Extra: ${e.label}`)).join("; ");
    const label = `${fmtDay(d.date)}${desc ? ". " + desc : ". Nothing planned or logged"}`;
    return `<button type="button" class="cal-day ${d.in_month ? "" : "out"} ${d.is_today ? "today" : ""}" data-act="calDay" data-date="${d.date}" title="${esc(label)}" aria-label="${esc(label)}">
      <span class="num">${d.day}</span><span class="dots">${dots}</span></button>`;
  }).join("");
  const chips = countChips({ ...c.counts, upcoming: 0, pending: 0 });
  $("#cal-card").innerHTML = `
    <div class="card-head"><h2>Calendar</h2>
      <div class="nav">
        <button class="btn icon" type="button" data-act="calNav" data-to="${c.prev}" aria-label="Previous month">‹</button>
        <span class="nav-label">${esc(c.label)}</span>
        <button class="btn icon" type="button" data-act="calNav" data-to="${c.next}" aria-label="Next month">›</button>
        ${c.is_current ? "" : '<button class="btn small" type="button" data-act="calNav" data-to="">This month</button>'}
      </div></div>
    ${chips ? `<div class="summary-line">${chips}</div>` : ""}
    <div class="cal">${heads}${cells}</div>
    <div class="legend" aria-label="Legend">
      <span><i class="dot done" aria-hidden="true">✓</i>On target</span>
      <span><i class="dot over" aria-hidden="true">▲</i>Over</span>
      <span><i class="dot under" aria-hidden="true">▼</i>Under</span>
      <span><i class="dot missed" aria-hidden="true">✕</i>Missed</span>
      <span><i class="dot extra" aria-hidden="true">+</i>Extra (unplanned)</span>
      <span><i class="dot upcoming" aria-hidden="true"></i>Still to do</span>
      <span class="muted">Click a day to open its week</span>
    </div>`;
}

function renderUpcoming() {
  const up = state.dashboard.upcoming;
  $("#upcoming-card").innerHTML = `<div class="card-head"><h2>Next 7 days</h2><span class="sub">Planned sessions from today</span></div>` + (up.length
    ? `<div class="table-wrap"><table>
    <thead><tr><th>Date</th><th>Sport</th><th>Session</th><th class="num">Distance</th><th class="num">Duration</th><th>Notes</th><th>Status</th></tr></thead>
    <tbody>${up.map((s) => `<tr><td>${fmtDay(s.date)}</td><td>${esc(s.sport_label)}</td><td>${esc(s.session_type)}</td>
      <td class="num">${fmtDist(s.planned_distance_km)}</td><td class="num">${fmtMins(s.planned_duration_min)}</td>
      <td>${esc(s.notes)}</td><td>${pill(s.status, s.duration_diff_min)}</td></tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">No planned sessions in the next 7 days. <a href="#/plan">Upload or paste a plan</a>.</div>');
}

function dataTable(headers, rows, numCols = []) {
  const th = headers.map((h, i) => `<th class="${numCols.includes(i) ? "num" : ""}">${esc(h)}</th>`).join("");
  const body = rows.map((r) => `<tr>${r.map((c, i) => `<td class="${numCols.includes(i) ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("");
  return `<details class="data"><summary>Show data as table</summary><div class="table-wrap short"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div></details>`;
}

function renderLoad() {
  const d = state.dashboard, L = d.load, src = L.sources_28d;
  const srcNote = (src.suffer_score + src.hr_fallback + src.none) === 0 ? "" :
    `Load inputs, last 28 days: ${src.suffer_score} from Strava suffer score, ${src.hr_fallback} estimated from heart rate` +
    (src.none ? `, ${src.none} with neither (counted as 0)` : "") + "." +
    (src.suffer_score && src.hr_fallback ? " The two aren't on the same scale, so treat the ratio as approximate." : "");
  $("#load-card").innerHTML = `<div class="card-head"><h2>Training load: 7-day vs 28-day</h2><span class="sub">Average daily load (Strava suffer score, else duration × avg HR ÷ 100)</span></div>` +
    (d.has_activities ? `<div class="chart-box"><canvas id="c-load" role="img" aria-label="Line chart of 7-day and 28-day average daily training load"></canvas></div>
      ${dataTable(["Date", "7-day avg", "28-day avg"], L.series.slice().reverse().map((p) => [p.date, p.avg7, p.avg28]), [1, 2])}
      ${srcNote ? `<div class="risk-note">${esc(srcNote)}</div>` : ""}` : '<div class="empty">No activities yet - sync from Strava.</div>');
  if (d.has_activities) renderLoadChart(theme());
}

// ---------- charts: shared ---------------------------------------------------------------------

function theme() {
  return { ink: cssVar("--ink"), ink2: cssVar("--ink-2"), ink3: cssVar("--ink-3"), grid: cssVar("--grid"), card: cssVar("--card"), s1: cssVar("--series-1"), s2: cssVar("--series-2") };
}

function mkChart(id, cfg) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart(canvas, cfg);
}

const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip && chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
    if (!active.length) return;
    const { ctx, chartArea: { top, bottom } } = chart, x = active[0].element.x;
    ctx.save(); ctx.strokeStyle = chart.options.plugins.crosshair.color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  },
};

function baseOptions(t, { legend = false, yTitle = "", mode = "index" } = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 200 },
    interaction: { mode, intersect: mode !== "index" },
    plugins: {
      crosshair: { color: t.ink3 },
      legend: { display: legend, labels: { color: t.ink2, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
      tooltip: { backgroundColor: t.card, titleColor: t.ink, bodyColor: t.ink2, borderColor: t.grid, borderWidth: 1, padding: 10, boxPadding: 4 },
    },
    scales: {
      x: { grid: { display: false }, border: { color: t.grid }, ticks: { color: t.ink2, maxRotation: 0, autoSkip: true } },
      y: { beginAtZero: true, grid: { color: t.grid, lineWidth: 1 }, border: { display: false }, ticks: { color: t.ink2 },
           title: { display: !!yTitle, text: yTitle, color: t.ink2 } },
    },
  };
}

function renderLoadChart(t) {
  const s = state.dashboard.load.series;
  const line = (label, key, color) => ({ label, data: s.map((p) => p[key]), borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: t.card, tension: 0 });
  const opts = baseOptions(t, { legend: true, yTitle: "Avg daily load" });
  opts.scales.x.ticks.maxTicksLimit = 8;
  opts.scales.x.ticks.callback = function (v) { return fmtShort(this.getLabelForValue(v)); };
  opts.plugins.tooltip.callbacks = { title: (items) => fmtDay(items[0].label) };
  mkChart("c-load", { type: "line", data: { labels: s.map((p) => p.date), datasets: [line("7-day average", "avg7", t.s1), line("28-day average", "avg28", t.s2)] }, options: opts, plugins: [crosshair] });
}

// ---------- drill-down: year > month > week ----------------------------------------------------

const SCOPE_NAMES = { year: "Year", month: "Month", week: "Week" };
const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function bucketLabel(scope, b) {
  return scope === "year" ? fmtShort(b.start) : scope === "month" ? String(+b.start.slice(8)) : fmtWeekdayNum(b.start);
}
function bucketTitle(scope, b) {
  if (scope === "year") return (b.start === b.end ? fmtDay(b.start) : `${fmtShort(b.start)} – ${fmtShort(b.end)}`) + (b.partial ? " (in progress)" : "");
  return fmtDay(b.start) + (b.partial ? " (today)" : "");
}

function crumbsHTML(e) {
  const thursday = e.scope === "week" ? isoAdd(e.start, 3) : e.start;   // the week's month/year is the one holding most of its days
  const year = thursday.slice(0, 4);
  const crumb = (scope, anchor, text) => `<button type="button" class="linkbtn" data-act="exDrill" data-scope="${scope}" data-anchor="${anchor}">${esc(text)}</button>`;
  const parts = [e.scope === "year" ? `<b>${year}</b>` : crumb("year", `${year}-01-01`, year)];
  if (e.scope !== "year") {
    const monthName = new Date(utc(thursday)).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
    parts.push(e.scope === "month" ? `<b>${esc(monthName)}</b>` : crumb("month", thursday.slice(0, 8) + "01", monthName));
  }
  if (e.scope === "week") parts.push(`<b>${esc(e.label)}</b>`);
  return parts.join('<span class="sep">›</span>');
}

function monthChips(e) {
  const year = e.start.slice(0, 4), today = state.dashboard.today;
  return `<div class="chips" aria-label="Jump to a month">` + MONTH_ABBR.map((m, i) => {
    const first = `${year}-${String(i + 1).padStart(2, "0")}-01`;
    return `<button type="button" class="chip" data-act="exDrill" data-scope="month" data-anchor="${first}" ${first > today ? "disabled" : ""}>${m}</button>`;
  }).join("") + `</div>`;
}

function renderExplore() {
  const e = state.exploreData, x = state.explore, today = state.dashboard.today;
  const inPeriod = e.start <= today && today <= e.end;
  const opts = state.dashboard.sport_options.map((o) => `<option value="${esc(o.value)}" ${o.value === x.sport ? "selected" : ""}>${esc(o.label)}</option>`).join("");
  const T = e.totals, none = T.count === 0;
  const totals = none ? "" : `<div class="summary-line"><span class="muted small">Total: ${esc(dot([fmtDist(T.distance_km), T.elevation_m ? `${T.elevation_m.toLocaleString("en-GB")} m climbed` : "", fmtHours(T.hours), `${T.count} ${T.count === 1 ? "activity" : "activities"}`]))}</span></div>`;
  const hint = e.scope === "year" ? "Each bar is a week - click one to see its seven days."
    : e.scope === "month" ? "Each bar is a day - click one to open its week." : "";
  $("#explore").innerHTML = `
    <div class="section-head"><h2>Trends &amp; drill-down</h2>
      <label class="muted small">Sport <select id="sport-select">${opts}</select></label></div>
    <div class="card">
      <div class="card-head" style="margin-bottom:0">
        <div class="seg" role="group" aria-label="Period">${Object.keys(SCOPE_NAMES).map((k) => `<button type="button" data-act="exScope" data-scope="${k}" aria-pressed="${e.scope === k}">${SCOPE_NAMES[k]}</button>`).join("")}</div>
        <div class="nav">
          <button class="btn icon" type="button" data-act="exNav" data-to="${e.prev}" aria-label="Previous ${e.scope}">‹</button>
          <span class="nav-label">${esc(e.label)}</span>
          <button class="btn icon" type="button" data-act="exNav" data-to="${e.next}" aria-label="Next ${e.scope}" ${e.has_next ? "" : "disabled"}>›</button>
          ${inPeriod ? "" : `<button class="btn small" type="button" data-act="exNow">Today</button>`}
        </div>
      </div>
      <div class="crumbs">${crumbsHTML(e)}</div>
      ${e.scope === "year" ? monthChips(e) : ""}
      ${hint ? `<div class="hint">${hint}</div>` : ""}
    </div>
    ${none ? `<div class="card"><div class="empty">No ${esc((state.dashboard.sport_options.find((o) => o.value === x.sport) || { label: "" }).label.toLowerCase())} activities in this ${e.scope}.</div></div>` : `
    <div class="grid-2">
      <div class="card"><div class="card-head"><h2>Distance</h2><span class="sub">${state.units}, ${e.scope === "year" ? "per week" : "per day"}</span></div>
        <div class="chart-box short"><canvas id="c-dist" role="img" aria-label="Bar chart of distance"></canvas></div></div>
      <div class="card"><div class="card-head"><h2>Elevation gain</h2><span class="sub">metres climbed, ${e.scope === "year" ? "per week" : "per day"}</span></div>
        <div class="chart-box short"><canvas id="c-elev" role="img" aria-label="Bar chart of elevation gain"></canvas></div></div>
    </div>
    <div class="card" id="pace-card"></div>`}
    <div class="card" id="detail-card"></div>`;

  if (!none) {
    const t = theme();
    barChart("c-dist", e, e.buckets.map((b) => +dist(b.distance_km).toFixed(1)), t.s1, state.units,
      (v) => `${v} ${state.units}`, (b) => `${b.count} session${b.count === 1 ? "" : "s"} · ${fmtHours(b.hours)}`, t);
    barChart("c-elev", e, e.buckets.map((b) => b.elevation_m), t.s2, "m",
      (v) => `${v.toLocaleString("en-GB")} m`, (b) => (b.climb_per_km ? `${Math.round(b.elevation_m / dist(b.distance_km))} m climbed per ${state.units}` : ""), t);
    renderPace(e, t);
  }
  renderDetail(e);
}

function barChart(id, e, values, color, yTitle, fmtVal, extra, t) {
  const drill = e.scope !== "week";
  const opts = baseOptions(t, { yTitle });
  opts.scales.x.ticks.maxTicksLimit = e.scope === "year" ? 13 : 16;
  opts.scales.x.ticks.callback = (v) => (e.buckets[v] ? bucketLabel(e.scope, e.buckets[v]) : "");
  opts.plugins.tooltip.callbacks = {
    title: (items) => bucketTitle(e.scope, e.buckets[items[0].dataIndex]),
    label: (item) => fmtVal(values[item.dataIndex]),
    afterLabel: (item) => extra(e.buckets[item.dataIndex]),
  };
  opts.onHover = (evt, els) => { evt.native.target.style.cursor = drill && els.length ? "pointer" : "default"; };
  opts.onClick = (_evt, els) => { if (drill && els.length) drillTo("week", e.buckets[els[0].index].start); };
  mkChart(id, {
    type: "bar",
    data: { labels: e.buckets.map((b) => b.start), datasets: [{
      data: values,
      backgroundColor: e.buckets.map((b) => (b.partial ? color + "88" : color)),
      borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 24,
    }] },
    options: opts, plugins: [crosshair],
  });
}

// pace / speed ------------------------------------------------------------------------------------

const defaultMode = (sport) => (["run", "hike", "foot"].includes(sport) ? "pace" : "speed");

function renderPace(e, t) {
  const mode = state.paceMode || defaultMode(state.explore.sport);
  const card = $("#pace-card");
  const pts = e.activities.filter((a) => a.average_speed);
  const toggle = `<div class="seg" role="group" aria-label="Pace or speed">
      <button type="button" data-act="paceMode" data-mode="pace" aria-pressed="${mode === "pace"}">Pace (min/${state.units})</button>
      <button type="button" data-act="paceMode" data-mode="speed" aria-pressed="${mode === "speed"}">Speed (${speedLabel()})</button></div>`;
  if (!pts.length) {
    card.innerHTML = `<div class="card-head"><h2>Pace / speed</h2></div><div class="empty">No activities with speed data in this ${e.scope}.</div>`;
    return;
  }
  const word = mode === "pace" ? "pace" : "speed";
  card.innerHTML = `
    <div class="card-head"><h2>Pace / speed trend</h2>${toggle}</div>
    <div class="grid-2">
      <div><h3>Average ${word}</h3><div class="chart-box short"><canvas id="c-avg" role="img" aria-label="Average ${word} per activity"></canvas></div></div>
      <div><h3>Max ${word}</h3><div class="chart-box short"><canvas id="c-max" role="img" aria-label="Maximum ${word} per activity"></canvas></div></div>
    </div>
    <div class="risk-note">${mode === "pace" ? "Faster is higher on the axis. " : ""}Max speed comes from GPS samples and spikes easily - read it as a rough ceiling, not a target. Each dot is one activity; the line is the ${e.scope === "year" ? "weekly" : "daily"} average (total distance ÷ total time).</div>`;

  const x0 = dayNum(e.start), lastBucket = e.buckets[e.buckets.length - 1];
  const x1 = dayNum(e.scope === "year" && lastBucket ? lastBucket.end : e.end);
  const pad = e.scope === "week" ? 0.5 : e.scope === "month" ? 0.5 : 2;
  const xy = (a, k) => ({ x: dayNum(a.date), y: toUnit(a[k], mode), a });
  const unitTitle = mode === "pace" ? `min/${state.units}` : speedLabel();
  const tickDays = () => {
    if (e.scope === "year") { const v = []; for (let d = dayNum(e.start); d <= x1; ) { v.push(d); const dt = new Date(d * 864e5); d = Math.floor(Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth() + 1, 1) / 864e5); } return v; }
    const step = e.scope === "month" ? 7 : 1, v = [];
    for (let d = x0; d <= x1; d += step) v.push(d);
    return v;
  };
  const tickText = (v) => { const iso = isoFromDayNum(v); return e.scope === "week" ? fmtWeekdayNum(iso) : e.scope === "year" ? MONTH_ABBR[+iso.slice(5, 7) - 1] : fmtShort(iso); };
  const scatterOpts = (legend) => {
    const o = baseOptions(t, { legend, mode: "nearest" });
    o.scales.x = { type: "linear", min: x0 - pad, max: x1 + pad, grid: { display: false }, border: { color: t.grid },
      ticks: { color: t.ink2, maxRotation: 0, callback: tickText },
      afterBuildTicks: (scale) => { scale.ticks = tickDays().map((value) => ({ value })); } };
    o.scales.y = { ...o.scales.y, beginAtZero: false, reverse: mode === "pace", title: { display: true, text: unitTitle, color: t.ink2 },
      ticks: { color: t.ink2, callback: (v) => (mode === "pace" ? fmtPace(v) : v) } };
    return o;
  };
  const dotStyle = (color) => ({ pointRadius: 4.5, pointHoverRadius: 7, pointBackgroundColor: color, pointBorderColor: t.card, pointBorderWidth: 2, hitRadius: 10 });
  const tipTitle = (items) => { const r = items[0].raw; return r.a ? `${fmtDay(r.a.date)} · ${r.a.name}` : bucketTitle(e.scope, r.b); };
  const tipLabel = (item) => `${item.dataset.label}: ${fmtSpeed(item.raw.y, mode)}`;

  const line = e.buckets.filter((b) => b.avg_speed).map((b) => ({ x: (dayNum(b.start) + dayNum(b.end)) / 2, y: toUnit(b.avg_speed, mode), b }));
  const avgOpts = scatterOpts(true);
  avgOpts.plugins.tooltip.callbacks = { title: tipTitle, label: tipLabel };
  mkChart("c-avg", { type: "scatter", options: avgOpts, data: { datasets: [
    { label: "Activity", data: pts.map((a) => xy(a, "average_speed")), showLine: false, ...dotStyle(t.s1 + "aa") },
    { label: e.scope === "year" ? "Weekly average" : "Daily average", data: line, showLine: true, borderColor: t.s1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, backgroundColor: t.s1, tension: 0 },
  ] } });

  const maxOpts = scatterOpts(false);
  maxOpts.plugins.tooltip.callbacks = { title: tipTitle, label: tipLabel };
  mkChart("c-max", { type: "scatter", options: maxOpts, data: { datasets: [
    { label: "Max", data: pts.filter((a) => a.max_speed).map((a) => xy(a, "max_speed")), showLine: false, ...dotStyle(t.s2) },
  ] } });
}

// detail table ------------------------------------------------------------------------------------

function renderDetail(e) {
  const card = $("#detail-card");
  const T = e.totals, mode = state.paceMode || defaultMode(state.explore.sport);
  if (e.scope === "year") {
    const rows = e.buckets.map((b) => `<tr class="${b.count ? "" : "empty-day"}">
      <td class="day-cell"><button type="button" class="linkbtn" data-act="exDrill" data-scope="week" data-anchor="${b.start}">${esc(bucketTitle("year", b).replace(" (in progress)", ""))}</button>${b.partial ? ' <span class="badge">in progress</span>' : ""}</td>
      <td class="num">${b.count || "–"}</td><td class="num">${fmtDist(b.distance_km)}</td><td class="num">${fmtMetres(b.elevation_m)}</td>
      <td class="num">${fmtHours(b.hours)}</td><td class="num">${b.climb_per_km ? `${Math.round(b.elevation_m / dist(b.distance_km))} m/${state.units}` : "–"}</td>
      <td class="num">${fmtSpeed(toUnit(b.avg_speed, mode), mode)}</td></tr>`).join("");
    card.innerHTML = `<div class="card-head"><h2>Week by week</h2><span class="sub">${esc(e.label)} · click a week to see its days</span></div>
      <div class="table-wrap tall"><table><thead><tr><th>Week</th><th class="num">Activities</th><th class="num">Distance</th><th class="num">Elevation</th><th class="num">Time</th><th class="num">Climb rate</th><th class="num">Avg ${mode === "pace" ? "pace" : "speed"}</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td>Total</td><td class="num">${T.count}</td><td class="num">${fmtDist(T.distance_km)}</td><td class="num">${fmtMetres(T.elevation_m)}</td><td class="num">${fmtHours(T.hours)}</td><td></td><td></td></tr></tfoot></table></div>`;
    return;
  }
  const byDay = {};
  e.activities.forEach((a) => (byDay[a.date] = byDay[a.date] || []).push(a));
  const rows = e.buckets.map((b) => {
    const list = byDay[b.start] || [];
    const dayLabel = e.scope === "month"
      ? `<button type="button" class="linkbtn" data-act="exDrill" data-scope="week" data-anchor="${b.start}">${esc(fmtWeekdayNum(b.start))}</button>`
      : esc(fmtWeekdayNum(b.start));
    const flag = b.partial ? ' <span class="badge">today</span>' : "";
    if (!list.length) {
      return `<tr class="day-start"><td class="day-cell">${dayLabel}${flag}</td><td colspan="8">${b.future ? "" : "Nothing logged"}</td></tr>`;
    }
    return list.map((a, i) => `<tr class="${i === 0 ? "day-start" : ""}">
      ${i === 0 ? `<td class="day-cell" rowspan="${list.length}">${dayLabel}${flag}</td>` : ""}
      <td>${esc(a.name)}${a.workout ? `<span class="badge">${esc(a.workout)}</span>` : ""}</td><td>${esc(a.sport_label)}</td>
      <td class="num">${fmtDist(a.distance_km)}</td><td class="num">${fmtMins(a.duration_min)}</td><td class="num">${fmtMetres(a.elevation_m)}</td>
      <td class="num">${fmtSpeed(toUnit(a.average_speed, mode), mode)}</td><td class="num">${a.average_heartrate ? Math.round(a.average_heartrate) : "–"}</td>
      <td class="num" ${a.load_source === "hr_fallback" ? 'title="Estimated from heart rate (no Strava suffer score)"' : ""}>${a.load_source === "none" ? "–" : a.load + (a.load_source === "hr_fallback" ? "*" : "")}</td></tr>`).join("");
  }).join("");
  const weekLink = e.scope === "week"
    ? `<button type="button" class="linkbtn" data-act="openWeek" data-date="${e.start}">Show planned vs actual for this week</button>` : "";
  card.innerHTML = `<div class="card-head"><h2>${e.scope === "week" ? "Day by day" : "Every day"}</h2><span class="sub">${esc(e.label)}${e.scope === "month" ? " · click a day to open its week" : ""}</span></div>
    <div class="table-wrap tall"><table><thead><tr><th>Day</th><th>Activity</th><th>Sport</th><th class="num">Distance</th><th class="num">Time</th><th class="num">Elevation</th><th class="num">Avg ${mode === "pace" ? "pace" : "speed"}</th><th class="num">Avg HR</th><th class="num">Load</th></tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr><td>Total</td><td>${T.count} ${T.count === 1 ? "activity" : "activities"}</td><td></td><td class="num">${fmtDist(T.distance_km)}</td><td class="num">${fmtHours(T.hours)}</td><td class="num">${fmtMetres(T.elevation_m)}</td><td></td><td></td><td></td></tr></tfoot></table></div>
    <div class="caption">Load: Strava suffer score, or * estimated from heart rate.${weekLink ? " " + weekLink : ""}</div>`;
}

// ---------- plan page --------------------------------------------------------------------------

const PLAN_HELP = `date,session type,sport,planned distance,planned duration,notes
2026-09-22,Easy run,Run,8km,50,Flat and relaxed
2026-09-22,Strength,Gym,,45,Legs + core
2026-09-26,Long run,Run,25km,3:30,Hilly - practise walking climbs
2026-09-27,Recovery,Rest,,,`;

function planUnitNote() {
  return `Distances written without a unit are read as <b>${state.units === "mi" ? "miles" : "kilometres"}</b> (change with the km / mi switch at the top). A unit in the cell (<code>10km</code>, <code>6 mi</code>) or the column header always wins.`;
}

async function loadPlan() {
  const el = $("#view-plan");
  if (!$("#plan-text")) {
    el.innerHTML = `<div class="stack">
      <div class="card">
        <div class="card-head"><h2>Import a training plan</h2></div>
        <div class="form-grid">
          <p class="muted small" style="margin:0">Paste from a spreadsheet (tab-separated) or CSV, or choose a file. Columns:
            <b>date, session type, sport, planned distance, planned duration, notes</b>. A header row is optional, and columns can be in any order if you include one.
            <span id="plan-unit-note"></span>
            Duration takes <code>90</code> (minutes), <code>1:30</code> (h:mm), <code>1h30</code>. Use <i>Rest</i> as the sport for rest days.</p>
          <textarea id="plan-text" spellcheck="false" placeholder="${esc(PLAN_HELP)}" aria-label="Training plan"></textarea>
          <div class="row">
            <input type="file" id="plan-file" accept=".csv,.tsv,.txt,text/csv,text/plain" aria-label="Choose plan file">
            <label class="muted small">Dates <select id="plan-dayfirst"><option value="1">day first (21/09/2026)</option><option value="0">month first (09/21/2026)</option></select></label>
            <label class="muted small">On import <select id="plan-mode">
              <option value="replace_dates">replace sessions on the dates in this upload</option>
              <option value="replace_all">replace the whole plan</option></select></label>
          </div>
          <div class="row">
            <button class="btn" id="plan-preview" type="button">Preview</button>
            <button class="btn primary" id="plan-import" type="button">Import plan</button>
            <button class="btn danger" id="plan-clear" type="button" style="margin-left:auto">Clear entire plan</button>
          </div>
          <div id="plan-result"></div>
        </div>
      </div>
      <div class="card"><div class="card-head"><h2>Current plan</h2><span class="sub" id="plan-sub"></span></div><div id="plan-table"></div></div>
    </div>`;
    $("#plan-file").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      if (f) $("#plan-text").value = await f.text();
    });
    $("#plan-preview").addEventListener("click", () => submitPlan(true));
    $("#plan-import").addEventListener("click", () => submitPlan(false));
    $("#plan-clear").addEventListener("click", clearPlan);
  }
  $("#plan-unit-note").innerHTML = planUnitNote();
  await loadPlanTable();
}

async function submitPlan(dry) {
  const out = $("#plan-result");
  const text = $("#plan-text").value;
  if (!text.trim()) { out.innerHTML = '<div class="banner warn">Paste a plan or choose a file first.</div>'; return; }
  try {
    const r = await api("/api/plan/import", { method: "POST", body: JSON.stringify({
      text, dry_run: dry, day_first: $("#plan-dayfirst").value === "1", mode: $("#plan-mode").value, distance_unit: state.units }) });
    const list = (items) => (items.length ? `<ul class="errors">${items.slice(0, 30).map((i) => `<li>${esc(i)}</li>`).join("")}${items.length > 30 ? `<li>…and ${items.length - 30} more</li>` : ""}</ul>` : "");
    let html = "";
    if (dry) {
      html += `<div class="banner info"><b>Preview:</b> ${r.rows.length} session${r.rows.length === 1 ? "" : "s"} read${r.errors.length ? `, ${r.errors.length} row${r.errors.length === 1 ? "" : "s"} with problems (skipped on import)` : ""}. Nothing saved yet.</div>`;
      if (r.rows.length) html += `<div class="table-wrap short"><table><thead><tr><th>Date</th><th>Sport</th><th>Session</th><th class="num">Distance</th><th class="num">Duration</th><th>Notes</th></tr></thead><tbody>${
        r.rows.slice(0, 300).map((x) => `<tr><td>${fmtDay(x.date)} <span class="muted small">${x.date}</span></td><td>${esc(x.sport_group)}</td><td>${esc(x.session_type)}</td><td class="num">${fmtDist(x.planned_distance_km)}</td><td class="num">${fmtMins(x.planned_duration_min)}</td><td>${esc(x.notes)}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (r.saved) {
      html += `<div class="banner ok"><b>Imported ${r.saved} session${r.saved === 1 ? "" : "s"}.</b>${r.errors.length ? ` ${r.errors.length} row${r.errors.length === 1 ? " was" : "s were"} skipped:` : ""}</div>`;
    } else {
      html += `<div class="banner error"><b>Nothing imported.</b></div>`;
    }
    if (r.errors.length) html += `<div class="banner error"><b>Problems</b>${list(r.errors)}</div>`;
    if (r.warnings.length) html += `<div class="banner warn"><b>Check</b>${list(r.warnings)}</div>`;
    out.innerHTML = html;
    if (!dry && r.saved) { await refreshStatus(); await loadPlanTable(); }
  } catch (e) {
    out.innerHTML = `<div class="banner error">${esc(e.message)}</div>`;
  }
}

async function clearPlan() {
  if (!confirm("Delete every planned session? Your Strava activities are not affected.")) return;
  try { await api("/api/plan", { method: "DELETE" }); await refreshStatus(); await loadPlanTable(); $("#plan-result").innerHTML = '<div class="banner ok">Plan cleared.</div>'; }
  catch (e) { $("#plan-result").innerHTML = `<div class="banner error">${esc(e.message)}</div>`; }
}

async function loadPlanTable() {
  state.planSessions = (await api("/api/plan")).sessions;
  renderPlanTable();
}

function renderPlanTable() {
  const sessions = state.planSessions || [];
  if (!$("#plan-table")) return;
  $("#plan-sub").textContent = sessions.length ? `${sessions.length} sessions, ${fmtShort(sessions[0].date)} – ${fmtShort(sessions[sessions.length - 1].date)}` : "";
  $("#plan-table").innerHTML = !sessions.length ? '<div class="empty">No plan yet - paste one above.</div>' : `<div class="table-wrap tall"><table>
    <thead><tr><th>Date</th><th>Sport</th><th>Session</th><th class="num">Distance</th><th class="num">Duration</th><th>Notes</th><th>Status</th><th>Matched Strava activity</th></tr></thead>
    <tbody>${sessions.map((s) => `<tr><td>${fmtDay(s.date)}</td><td>${esc(s.sport_label)}</td><td>${esc(s.session_type)}</td>
      <td class="num">${fmtDist(s.planned_distance_km)}</td><td class="num">${fmtMins(s.planned_duration_min)}</td><td>${esc(s.notes)}</td>
      <td>${pill(s.status, s.duration_diff_min)}</td><td>${s.activity ? esc(dot([s.activity.name, s.activity.distance_km && fmtDist(s.activity.distance_km), s.activity.duration_min && fmtMins(s.activity.duration_min)])) : ""}</td></tr>`).join("")}</tbody></table></div>`;
}

// ---------- admin --------------------------------------------------------------------------------

function fmtDateTime(epochSeconds) {
  if (!epochSeconds) return "–";
  return new Date(epochSeconds * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadAdmin() {
  const el = $("#view-admin");
  el.innerHTML = `<div class="stack"><div class="card"><p class="muted">Loading…</p></div></div>`;
  try {
    state.admin = await api("/api/invites");
    renderAdmin();
  } catch (e) {
    el.innerHTML = `<div class="banner error">Couldn't load this page: ${esc(e.message)}</div>`;
  }
}

function renderAdmin() {
  const a = state.admin;
  const cell = (text) => (text ? esc(text) : '<span class="muted">—</span>');
  const rows = a.accounts.map((u) => `<tr><td>${cell(dot([u.first_name, u.last_name]))}</td>
      <td>${esc(u.username)}${u.is_admin ? '<span class="badge-admin">Admin</span>' : ""}</td>
      <td>${cell(u.email)}</td><td>${fmtDateTime(u.created_at)}</td><td>${fmtDateTime(u.last_login_at)}</td></tr>`).join("");
  const invites = a.pending_invites.length
    ? `<ul class="errors">${a.pending_invites.map((p) => `<li><code style="user-select:all">${esc(location.origin + p.url)}</code> <span class="muted small">(expires in ${p.expires_in_days} days)</span></li>`).join("")}</ul>`
    : '<p class="muted small" style="margin:6px 0 0">No pending invites.</p>';
  const atCap = a.accounts.length >= a.max_users;
  $("#view-admin").innerHTML = `<div class="stack">
    <div class="card">
      <div class="card-head"><h2>Invite a friend</h2><span class="sub">${a.accounts.length} of ${a.max_users} accounts used</span></div>
      <button class="btn primary" type="button" data-act="invite" ${atCap ? "disabled" : ""}>Create invite link</button>
      ${atCap ? '<p class="risk-note">At the account limit - raise MAX_USERS (and your Strava API app\'s athlete capacity) to invite more.</p>' : ""}
      <div id="invite-result"></div>
      <h3 style="margin-top:16px">Pending invites</h3>
      ${invites}
    </div>
    <div class="card">
      <div class="card-head"><h2>Accounts</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Username</th><th>Email</th><th>Signed up</th><th>Last login</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>
  </div>`;
}

// ---------- interaction ------------------------------------------------------------------------

function syncUnitsSeg() {
  document.querySelectorAll("#units-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.units === state.units)));
}

// change the drill-down period and reload it
async function drillTo(scope, anchor) {
  state.explore.scope = scope;
  state.explore.anchor = anchor || null;
  await loadExplore();
}

const actions = {
  units(d) {
    state.units = d.units === "mi" ? "mi" : "km";
    try { localStorage.setItem("units", state.units); } catch (_) { /* private mode: fine, just not remembered */ }
    syncUnitsSeg();
    if (state.dashboard && state.exploreData) renderAll();
    if ($("#plan-unit-note")) { $("#plan-unit-note").innerHTML = planUnitNote(); renderPlanTable(); }
  },
  weekNav: (d) => loadWeek(d.to || null),
  calNav: (d) => loadCalendar(d.to || null),
  async calDay(d) {
    await loadWeek(d.date);
    $("#week-card").scrollIntoView({ behavior: "smooth", block: "start" });
  },
  openWeek: async (d) => { await actions.calDay(d); },
  exScope(d) {
    const e = state.exploreData, today = state.dashboard.today;
    // keep the user's bearings: today if it falls in the current period, otherwise the period's start
    return drillTo(d.scope, e.start <= today && today <= e.end ? today : e.start);
  },
  exNav: (d) => drillTo(state.explore.scope, d.to),
  exNow: () => drillTo(state.explore.scope, null),
  exDrill: (d) => drillTo(d.scope, d.anchor),
  paceMode(d) { state.paceMode = d.mode; renderExplore(); },
  async invite() {
    const r = await api("/api/invites", { method: "POST" });
    const url = location.origin + r.url;
    await loadAdmin();   // refresh the account count and pending-invites list first...
    $("#invite-result").innerHTML = `<div class="banner ok"><b>Invite link created</b> (expires in ${r.expires_in_days} days):<br>
      <code style="user-select:all;display:inline-block;margin-top:4px">${esc(url)}</code><br>
      <button class="btn small" type="button" id="copy-invite" style="margin-top:8px">Copy link</button></div>`;
    document.getElementById("copy-invite")?.addEventListener("click", (e) => {   // ...then show the banner, so the refresh can't wipe it
      navigator.clipboard?.writeText(url);
      e.target.textContent = "Copied";
    });
  },
};

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const fn = actions[el.dataset.act];
  if (fn) Promise.resolve(fn(el.dataset)).catch((e) => setFlash("error", esc(e.message)));
});

document.addEventListener("change", (e) => {
  if (e.target.id === "sport-select") {
    state.explore.sport = e.target.value;
    state.paceMode = null;
    loadExplore();
  }
});

$("#sync-btn").addEventListener("click", doSync);
window.addEventListener("hashchange", () => { state.flash = null; renderBanner(); route(); });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.dashboard && state.exploreData && !$("#view-dashboard").hidden) renderAll();
});

// ---------- boot -------------------------------------------------------------------------------

(async function boot() {
  const q = new URLSearchParams(location.search);
  syncUnitsSeg();
  try {
    await refreshStatus();
  } catch (e) {
    $("#banner").innerHTML = `<div class="banner error">Can't reach the server: ${esc(e.message)}</div>`;
    return;
  }
  if (q.has("auth_error")) state.flash = { kind: "error", html: "<b>Strava login failed.</b> " + esc(q.get("auth_error")) };
  else if (q.has("connected")) state.flash = { kind: "ok", html: "<b>Connected to Strava.</b> Click <b>Sync Strava</b> to pull your activities." };
  if (q.has("auth_error") || q.has("connected")) history.replaceState(null, "", location.pathname + location.hash);
  renderBanner();
  route();
})();
