// RoadSense officer page: this week's plan, road card, name-a-road mode, report triage, streetlights.
// Talks only to the local API. Every number on screen comes from it; plain-English wording is added here.
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (x, d = 3) => (x === null || x === undefined ? "n/a" : Number(x).toFixed(d));
const gbp = (x) => "£" + Math.round(Number(x)).toLocaleString("en-GB");
const BAND_COLOR = { critical: "#B3261E", high: "#D9661F", prevention: "#FFC72C", monitor: "#9DB4A4" };
// Open-Meteo's terms require attribution wherever its weather data is shown
const OPEN_METEO = `<a class="credit" href="https://open-meteo.com/" target="_blank" rel="noopener">Weather data by Open-Meteo.com</a>`;

const state = { city: "", homeCity: "", ward: "", scenario: "", meta: null, plan: null, list: [], index: {}, filter: "all",
  gj: null, map: null, mapReady: false, lastFms: null, ctxVerdict: {}, view: "plan", current: null };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v)).toString();
const otherCity = () => state.city && state.city !== state.homeCity;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------- plain English ----------
// The model's reason strings are precise but technical; this rewrites the common ones and flags stand-in history.
const REASONS = [
  [/^open FixMyStreet report.*/i, () => "A resident reported a pothole here and it's still open"],
  [/^long segment \((\d+) m\)/i, (m) => `A long stretch of road (${m[1]} m), so more surface to wear`],
  [/^road dug up (\d+)x in the last (\d+) years.*/i, (m) => `Dug up ${m[1]} times in ${m[2]} years for pipes and cables`],
  [/^(\d+) utility or highway digs here last year.*/i, (m) => `Dug up ${m[1]} times last year for pipes and cables`],
  [/^(\d+) bus routes use this road/i, (m) => `${plural(+m[1], "bus route uses", "bus routes use")} this road`],
  [/^HGV and bus flow ([\d,]+) a day/i, (m) => `About ${m[1]} lorries and buses a day`],
  [/^heavy axle load.*/i, () => "Heavy vehicles pound this surface"],
  [/^busy junction/i, () => "A busy junction, where braking wears the road"],
  [/^potholes on neighbouring segments last year/i, () => "Potholes on the next stretches of road last year"],
  [/^potholes repaired here in earlier years/i, () => "Potholes fixed here in earlier years"],
  [/^potholes repaired here last year/i, () => "Potholes fixed here last year"],
  [/^high pothole density last year/i, () => "Lots of potholes here last year"],
];
function plain(reason) {
  const raw = String(reason || "").trim();
  const standIn = /synthetic|simulated/i.test(raw);
  const base = raw.replace(/\s*\((synthetic|simulated)[^)]*\)/i, "");
  for (const [re, f] of REASONS) { const m = base.match(re); if (m) return { text: f(m), standIn }; }
  return { text: cap(base), standIn };
}
const reasonList = (r) => (Array.isArray(r) ? r : String(r || "").split(" · ")).filter(Boolean).map(plain);

// One verdict per road: fix now / wait / recently fixed / watch. Driven by the API's own value badge.
const RATIO = (b) => { const m = String(b || "").match(/×([\d.]+)/); return m ? +m[1] : null; };
function verdictFor(r) {
  const b = String(r.value_badge || "");
  if (r.recently_treated) return { kind: "done", label: "Recently fixed", line: `The council worked on it recently (${r.recently_treated.label || "scheme list"}). Check before sending a crew.` };
  if (b.startsWith("Confirmed") || b.startsWith("Simulated")) return { kind: "fix", label: "Fix now",
    line: "A confirmed pothole. A planned repair now costs less than an emergency repair later.", sim: b.startsWith("Simulated") };
  if (b.startsWith("Pays back")) return { kind: "fix", label: "Fix now", line: `Every £1 spent now saves about £${RATIO(b).toFixed(2)} in emergency repairs.` };
  if (b.startsWith("Loss maker")) return { kind: "wait", label: "Wait", line: `Risky, but treating it now would cost more than it saves (about £${(RATIO(b) || 0).toFixed(2)} back per £1).` };
  return null;
}
function verdictNotListed(s) {
  if (s.recently_treated) return verdictFor(s);
  if (s.band === "critical" || s.band === "high") return { kind: "wait", label: "Watch", line: "High risk, but not on this week's list. RoadSense re-checks it every day." };
  return { kind: "watch", label: "No action", line: "Low risk right now. RoadSense re-checks it every day." };
}
function verdictCity(r) {
  if (r.open_report) return { kind: "fix", label: "Check now", line: "A resident reported a problem here and it's still open." };
  if (r.band === "critical" || r.band === "high") return { kind: "wait", label: "Inspect", line: "High predicted risk. Worth an inspection." };
  return { kind: "watch", label: "Watch", line: "On the watch list by predicted risk." };
}

// ---------- views ----------
function show(view) {
  state.view = view;
  ["plan", "road", "triage", "lights"].forEach((v) => $("#view-" + v).classList.toggle("hidden", v !== view));
  $("#panel").scrollTop = 0;
  if (view === "plan" && state.mapReady) state.map.setFilter("segments-hl", ["==", ["get", "segment_id"], ""]);
}
$$("[data-back]").forEach((b) => b.onclick = () => show("plan"));
$("#to-triage").onclick = () => { show("triage"); $("#panel").focus(); };
$("#to-lights").onclick = () => { show("lights"); loadLights(); };

// ---------- meta / header ----------
async function loadMeta() {
  const m = await api("/meta?" + qs({ forecast_as_of: new URLSearchParams(location.search).get("forecast_as_of") }));
  state.meta = m;
  state.homeCity = state.homeCity || m.city;
  state.city = state.city || m.city;
  const t = $("#city-toggle");
  if ((m.cities || []).length > 1 && !t.children.length) {
    t.innerHTML = m.cities.map((c) => `<button type="button" data-city="${esc(c)}" aria-pressed="${c === state.city}">${esc(c)}${c === m.city ? "" : " · real data only"}</button>`).join("");
    t.classList.remove("hidden");
    t.querySelectorAll("button").forEach((b) => b.onclick = () => setCity(b.dataset.city));
  }
  const sel = $("#ward");
  if (sel.options.length === 1) m.wards.forEach((w) => sel.add(new Option(w, w)));
  const b = $("#stale");
  const msg = m.freshness.provisional || m.badges.mock_inputs || m.freshness.stale;
  b.innerHTML = esc(m.freshness.message + (m.badges.mock_inputs ? " " + m.badges.note : "")) + " · " + OPEN_METEO;
  b.classList.toggle("hidden", !msg);
  $("#official-link").href = m.official_report_url;
  const du = m.data_updated || {};
  const when = du.timestamp ? new Date(du.timestamp).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "unknown";
  $("#updated").textContent = `Data updated ${when}` + (du.stale ? " · out of date" : "");
}

// ---------- 1. this week's plan ----------
async function loadPlan() {
  if (otherCity()) return loadInspection(state.city);
  const p = await api("/plan/prevention?" + qs({ ward: state.ward, scenario: state.scenario,
    forecast_as_of: new URLSearchParams(location.search).get("forecast_as_of") }));
  state.plan = p;
  const seen = new Set();
  state.list = [...p.worth.rows, ...p.rows].filter((r) => !seen.has(r.segment_id) && seen.add(r.segment_id))
    .map((r, i) => ({ ...r, rank: i + 1, verdict: verdictFor(r), why: reasonList(r.reasons) }));
  state.index = Object.fromEntries(state.list.map((r) => [r.segment_id, r]));
  const wk = p.as_of ? new Date(p.as_of + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }) : "";
  $("#plan-title").textContent = "This week's plan";
  $("#plan-sub").textContent = `Week of ${wk}${state.ward ? " · " + state.ward : ""}${state.scenario ? " · what if: " + state.scenario.replace(/_/g, " ") : ""} · tap a road to see why`;
  renderFilters();
  renderList();
}

function renderFilters() {
  const counts = { fix: 0, wait: 0, done: 0, watch: 0 };
  state.list.forEach((r) => r.verdict && counts[r.verdict.kind]++);
  const tabs = otherCity()
    ? [["all", "All", state.list.length], ["fix", "Check now", counts.fix], ["wait", "Inspect", counts.wait], ["watch", "Watch", counts.watch]]
    : [["all", "All", state.list.length], ["fix", "Fix now", counts.fix], ["wait", "Wait", counts.wait], ["done", "Recently fixed", counts.done]];
  $("#filters").innerHTML = tabs.filter(([id, , n]) => id === "all" || n).map(([id, label, n]) =>
    `<button role="tab" type="button" data-f="${id}" aria-selected="${state.filter === id}">${label}<span class="n">${n}</span></button>`).join("");
  $$("#filters button").forEach((b) => b.onclick = () => { state.filter = b.dataset.f; renderFilters(); renderList(); });
}

function roadItem(r) {
  const v = r.verdict || { kind: "watch", label: "Watch" };
  const why = r.why[0] || { text: "" };
  const tags = [];
  if (why.standIn || r.labels_synthetic) tags.push(`<span class="tag standin">Stand-in history</span>`);
  if (v.sim) tags.push(`<span class="tag standin">Simulated crew report</span>`);
  if (r.context && r.context.construction) tags.push(`<span class="tag">Building site nearby</span>`);
  if (r.open_report) tags.push(`<span class="tag real">Open report</span>`);
  return `<li><button type="button" class="road" data-id="${esc(r.segment_id)}">
    <span class="rank">${r.rank}</span>
    <span class="road-main"><span class="road-name">${esc(r.name)} <span class="area">· ${esc(r.ward)}</span></span>
      <span class="road-why">${esc(why.text)}</span>${tags.length ? `<span class="tags">${tags.join("")}</span>` : ""}</span>
    <span class="pill ${v.kind}">${esc(v.label)}</span></button></li>`;
}

function renderList() {
  const rows = state.list.filter((r) => state.filter === "all" || (r.verdict && r.verdict.kind === state.filter));
  $("#plan-list").innerHTML = rows.map(roadItem).join("") || `<li class="empty-list">No roads here this week.</li>`;
  $$("#plan-list .road").forEach((b) => b.onclick = () => showSegment(b.dataset.id, true));
}

// second city: an inspection list from real reports (no costs, no work orders)
async function loadInspection(c) {
  const p = await api("/plan/inspection?" + qs({ city: c }));
  state.list = p.rows.map((r) => ({ ...r, verdict: verdictCity(r), why: reasonList(r.reasons) }));
  state.index = Object.fromEntries(state.list.map((r) => [r.segment_id, r]));
  $("#plan-title").textContent = `${c}: what to inspect`;
  $("#plan-sub").textContent = `100% real data · ${p.open_report_rows} open resident reports first, then the highest predicted risk`;
  renderFilters();
  renderList();
}

// ---------- 2. road card ----------
function focusSegment(id, fly) {
  if (!state.mapReady || !state.map.getLayer("segments-hl")) return false;
  state.map.setFilter("segments-hl", ["==", ["get", "segment_id"], id]);
  const f = (state.gj ? state.gj.features : []).find((x) => x.properties.segment_id === id);
  if (!f || !fly) return !!f;
  const b = new maplibregl.LngLatBounds();
  (f.geometry.type === "MultiLineString" ? f.geometry.coordinates.flat() : f.geometry.coordinates).forEach((c) => b.extend(c));
  const c = state.map.getContainer(), pad = Math.min(80, c.clientWidth / 6, c.clientHeight / 4);
  state.map.fitBounds(b, { padding: pad, maxZoom: 17 });
  return true;
}

const ICON = {
  fix: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"/></svg>`,
  wait: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>`,
  done: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16M6 16l4-8 4 4 4-6"/></svg>`,
  watch: `<svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`,
};
const verdictBox = (v) => `<div class="verdict ${v.kind}"><span class="vi">${ICON[v.kind]}</span><span><b>${esc(v.label)}${v.kind === "fix" && !otherCity() ? ". It's cheaper than waiting." : ""}</b><span>${esc(v.line)}</span></span></div>`;
const whyBox = (why) => `<section class="box" aria-labelledby="why-h"><h2 id="why-h">Why it's on the list</h2>
  ${why.slice(0, 3).map((w, i) => `<div class="reason"><span class="num">${i + 1}</span><div>${esc(w.text)}${w.standIn ? ` <span class="tag standin">stand-in history</span>` : ""}</div></div>`).join("") || `<p class="muted">No strong signals: low risk.</p>`}
  </section>`;

function ctxRows(c) {
  if (!c) return "";
  let h = "";
  if (c.construction) h += `<div>Building sites</div><div>${c.construction.sites.map((x) => esc(x.address || x.reference)).join("<br>")}
    <div class="small muted">Within 150 m; site lorries add heavy traffic.</div></div>`;
  if (c.works) h += `<div>Street works</div><div>${plural(c.works.count, "dig", "digs")} in the last ${c.works.months} months · ${esc(c.works.promoters.join(", "))}</div>`;
  return h;
}

async function showSegment(id, fly) {
  const found = focusSegment(id, fly);
  if (otherCity()) return showCitySegment(id);
  let s;
  try { s = await api(`/segments/${encodeURIComponent(id)}?` + qs({ scenario: state.scenario })); }
  catch (e) { return roadCard(`<h1>Road not available</h1><p class="muted">${esc(e.message)}</p>`); }
  if (otherCity()) return;
  if (fly && !found && state.mapReady) state.map.flyTo({ center: [s.lon, s.lat], zoom: 16 });
  state.current = s;
  if (s.status !== "scored") return roadCard(`<div class="road-head"><h1>${esc(s.name)}</h1></div>
    <div class="verdict watch"><span class="vi">${ICON.watch}</span><span><b>Not a council road</b><span>${esc(s.status)}</span></span></div>`);
  const row = state.index[id];
  const v = row ? row.verdict : verdictNotListed(s);
  const why = reasonList(s.reasons);
  const costs = state.plan ? state.plan.costs : { planned_repair: 56.94, reactive_repair: 87.8 };
  const standIn = why.some((w) => w.standIn) || s.wear_synthetic || s.labels_synthetic;
  const dd = s.data_dates;
  roadCard(`<div class="road-head">
      ${row ? `<span class="badge-top">#${row.rank} THIS WEEK</span>` : ""}
      <h1>${esc(s.name)}</h1><p class="sub">${esc(s.ward)}, ${esc(state.city)}${s.ref ? " · " + esc(s.ref) : ""}</p></div>
    ${verdictBox(v)}
    ${whyBox(why)}
    <section class="box" aria-labelledby="cost-h"><h2 id="cost-h">Fix now or wait?</h2>
      <div class="costs">
        <div class="cost now"><span>Fix now</span><b>${gbp(costs.planned_repair)}</b><small>per pothole, planned</small></div>
        <div class="cost later"><span>Wait until it breaks</span><b>${gbp(costs.reactive_repair)}</b><small>per pothole, emergency</small></div>
      </div>
      <p><b>Suggested job:</b> ${esc(s.treatment_label)}, about ${gbp(s.treatment_cost_gbp)}. It should avoid about ${gbp(s.reactive_cost_avoided_gbp)} of emergency repairs.</p>
      ${standIn ? `<div class="unknown"><b>What we don't know</b>Southampton's repair records aren't public, so some history on this road is a labelled stand-in. Reasons marked "stand-in history" come from it.</div>` : ""}
    </section>
    <section class="box" aria-labelledby="crew-h"><h2 id="crew-h">After the crew visits</h2>
      <p class="small muted">Tell RoadSense what they found and it re-scores the road straight away.</p>
      <div class="actions">
        <button class="btn" type="button" data-out="pothole">Found a pothole</button>
        <button class="btn" type="button" data-out="cracking">Found cracking</button>
        <button class="btn" type="button" data-out="none">Nothing found</button>
      </div>
      <div id="outcome-msg"></div>
      ${s.outcomes.map((o) => `<div class="small muted">Recorded: ${esc(o.outcome)} on ${esc(o.inspection_date)} (${esc(o.source)})</div>`).join("")}
    </section>
    <details class="tech"><summary>Technical details</summary><div class="kv">
      <div>Risk score</div><div>${fmt(s.urgency, 0)} / 100 (${esc(s.band)})</div>
      <div>Chance of a report</div><div>${fmt(s.p7 * 100, 0)}% in 7 days · ${fmt(s.p30 * 100, 0)}% in 30 days${s.labels_synthetic ? " (stand-in labels)" : ""}</div>
      <div>Next 4 weeks</div><div>${s.risk_weeks.map((w) => `${fmt((w.p30 || 0) * 100, 0)}%${w.weather_basis === "climatology_10y" ? "<sup>c</sup>" : w.uses_forecast ? "<sup>f</sup>" : ""}`).join(" → ")}
        <div class="small muted">f = forecast, c = 10-year average weather · ${OPEN_METEO}</div></div>
      <div>Wear</div><div>${fmt(s.wear_pct, 0)}%${s.wear_synthetic ? " (stand-in)" : ""}</div>
      <div>Traffic</div><div>${fmt(s.esal_day, 0)} heavy-axle loads a day · ${esc(s.traffic_note)}</div>
      <div>Who it affects</div><div>${esc(s.consequence_text)} (×${fmt(s.consequence, 2)})</div>
      <div>Model</div><div>${esc(s.model_version)} · ${esc(state.meta ? state.meta.badges.model_label : s.trained_on)}</div>
      ${ctxRows(s.context)}
      <div>Data dates</div><div class="small">Risk as of ${esc(dd.risk_as_of)} · ${esc(dd.forecast.message)}</div>
    </div></details>`);
  $$("#view-road [data-out]").forEach((b) => b.onclick = () => outcome(s.segment_id, b.dataset.out));
}

async function showCitySegment(id) {
  const c = state.city;
  let s;
  try { s = await api(`/segments/${encodeURIComponent(id)}?` + qs({ city: c })); }
  catch (e) { if (c === state.city) roadCard(`<h1>Road not available</h1><p class="muted">${esc(e.message)}</p>`); return; }
  if (c !== state.city) return;  // the city changed while this was loading
  state.current = s;
  const row = state.index[id];
  const v = row ? row.verdict : verdictCity(s);
  roadCard(`<div class="road-head">${row ? `<span class="badge-top">#${row.rank} TO INSPECT</span>` : ""}
      <h1>${esc(s.name)}</h1><p class="sub">${esc(s.ward)}, ${esc(s.city)} · <span class="tag real">${esc(s.badge)}</span></p></div>
    ${verdictBox(v)}
    ${whyBox(reasonList(s.reasons))}
    ${row && row.open_reports ? `<section class="box"><h2>What residents reported</h2>${row.open_reports.map((o) => `<p class="wo-said">${esc(o.title)}</p><p class="small muted">${esc(o.reported_at)}</p>`).join("")}</section>` : ""}
    <details class="tech"><summary>Technical details</summary><div class="kv">
      <div>Risk score</div><div>${fmt(s.urgency, 0)} / 100 (${esc(s.band)})</div>
      <div>Chance of a report</div><div>${fmt(s.p7 * 100, 0)}% in 7 days · ${fmt(s.p30 * 100, 0)}% in 30 days</div>
      <div>Who it affects</div><div>${esc(s.consequence_text)} (×${fmt(s.consequence, 2)})</div>
      <div>Model</div><div>${esc(s.model_version)} · trained on ${esc(s.trained_on)} reports</div>
      ${ctxRows(s.context)}
    </div><p class="small muted">${esc(s.note)}</p></details>`);
}
window.showCitySegment = showCitySegment;
window.showSegment = showSegment;

function roadCard(html) {
  $("#view-road").innerHTML = `<button class="back" type="button" data-back>← ${otherCity() ? "Inspection list" : "This week's plan"}</button>` + html;
  $("#view-road [data-back]").onclick = () => show("plan");
  show("road");
}

async function outcome(sid, o) {
  const r = await post("/outcomes", { segment_id: sid, outcome: o });
  await Promise.all([loadPlan(), state.mapReady ? loadMap() : null]);
  await showSegment(sid);
  $("#outcome-msg").innerHTML = `<p class="note">Re-scored: risk ${fmt(r.before.urgency, 0)} → <b>${fmt(r.after.urgency, 0)}</b>. Suggested job is now: ${esc(r.after.treatment)}.</p>`;
}

// ---------- search (header) and 3. name-a-road mode ----------
function matchRoads(q, n = 8) {
  q = q.trim().toLowerCase();
  if (!q || !state.gj) return [];
  const best = {};
  for (const f of state.gj.features) {
    const p = f.properties;
    if (!p.name || !p.name.toLowerCase().includes(q)) continue;
    const k = p.name + "|" + p.ward;
    const listed = state.index[p.segment_id];
    const score = (listed ? 1000 - listed.rank : 0) + (p.urgency || 0);
    if (!best[k] || score > best[k].score) best[k] = { ...p, score, starts: p.name.toLowerCase().startsWith(q) };
  }
  return Object.values(best).sort((a, b) => (b.starts - a.starts) || (b.score - a.score)).slice(0, n);
}

const sInput = $("#search"), sRes = $("#search-results");
sInput.oninput = () => {
  const m = matchRoads(sInput.value);
  sRes.innerHTML = m.map((r) => `<li><button type="button" role="option" data-id="${esc(r.segment_id)}">${esc(r.name)} <span class="area">· ${esc(r.ward)}</span></button></li>`).join("")
    || (sInput.value.trim() ? `<li class="empty">No road by that name in ${esc(state.city)}.</li>` : "");
  sRes.classList.toggle("hidden", !sRes.innerHTML);
  sInput.setAttribute("aria-expanded", String(!sRes.classList.contains("hidden")));
  sRes.querySelectorAll("button").forEach((b) => b.onclick = () => { sRes.classList.add("hidden"); sInput.value = ""; showSegment(b.dataset.id, true); });
};
sInput.onkeydown = (e) => { if (e.key === "Enter") { const b = sRes.querySelector("button"); if (b) b.click(); } if (e.key === "Escape") sRes.classList.add("hidden"); };
document.addEventListener("click", (e) => { if (!e.target.closest(".search")) sRes.classList.add("hidden"); });

const demo = { open() { $("#demo").classList.remove("hidden"); $("#demo-city").textContent = state.city; $("#demo-input").focus(); },
  close() { $("#demo").classList.add("hidden"); $("#demo-open").focus(); } };
$("#demo-open").onclick = demo.open;
$(".demo-close").onclick = demo.close;
$("#demo").addEventListener("keydown", (e) => { if (e.key === "Escape") demo.close(); });
$("#demo-input").oninput = () => renderDemoMatches();
$("#demo-input").onkeydown = (e) => { if (e.key === "Enter") { const b = $("#demo-matches button"); if (b) b.click(); } };

function renderDemoMatches(picked) {
  const m = matchRoads($("#demo-input").value, 6);
  $("#demo-matches").innerHTML = m.map((r) => `<li><button type="button" data-id="${esc(r.segment_id)}" aria-pressed="${r.segment_id === picked}">${esc(r.name)} · ${esc(r.ward)}</button></li>`).join("");
  $$("#demo-matches button").forEach((b) => b.onclick = () => answer(b.dataset.id));
  if (!m.length) $("#demo-answer").classList.add("hidden");
}

async function answer(id) {
  renderDemoMatches(id);
  const c = state.city;
  const s = await api(`/segments/${encodeURIComponent(id)}?` + qs(otherCity() ? { city: c } : { scenario: state.scenario }));
  if (c !== state.city) return;
  const row = state.index[id];
  const v = row ? row.verdict : otherCity() ? verdictCity(s) : (s.status === "scored" ? verdictNotListed(s) : { kind: "watch", label: "Not a council road", line: s.status });
  const why = reasonList(s.reasons || []);
  const onList = row ? `Yes, #${row.rank} ${otherCity() ? "to inspect" : "this week"}` : v.kind === "done" ? "Recently fixed" : "Not this week";
  const first = why[0];
  $("#demo-answer").innerHTML = `
    <div class="q"><span>1 · On this week's list?</span><span class="big">${esc(onList)}</span></div>
    <div class="q"><span>2 · Why?</span><span class="why">${esc(first ? first.text : "No strong risk signals on this road right now.")}</span></div>
    <div class="q"><span>3 · ${otherCity() ? "What to do" : "Fix now or wait?"}</span><span class="pill ${v.kind}">${esc(v.label)}</span><span class="small">${esc(v.line)}</span></div>
    ${first && first.standIn ? `<div class="unknown wide">This reason comes from stand-in history, because Southampton's repair records aren't public. Treat it with care.</div>` : ""}
    <button type="button" class="primary yellow wide open">Open the full road card →</button>`;
  $("#demo-answer").classList.remove("hidden");
  $("#demo-answer .open").onclick = () => { demo.close(); showSegment(id, true); };
}

// ---------- 4. triage ----------
async function loadFms() {
  const recent = await api("/reports/recent?n=40");
  $("#fms-pick").innerHTML = recent.map((r) => `<option value="${r.id}">${esc(r.reported_at)} · ${esc(r.title).slice(0, 60)}</option>`).join("");
}

const STATUS = { pending_approval: ["Nothing is sent yet", "The AI only drafts. Nothing reaches a crew until you approve it."],
  approved: ["Sent to the crew ✓", "Added to this week's work."], rejected: ["Not a road job", "No job created. The report stays on record."],
  merged: ["Merged as a duplicate", "Same problem as an earlier report, so no second job."] };

function woCard(w) {
  const st = STATUS[w.status] || [cap(String(w.status).replace(/_/g, " ")), ""];
  const where = w.segment_id ? `<a href="#" data-seg="${esc(w.segment_id)}">${esc(w.location.name)}</a>` : esc(w.location.message || "Not on a council road");
  return `<article class="wo ${w.status === "merged" ? "merged" : ""}">
    <span class="step">1 · What the resident sent · ${esc(String(w.reported_at).slice(0, 10))}</span>
    <p class="wo-said">${esc(w.title)}</p>
    <span class="step">2 · What the AI understood${w.method && !String(w.method).startsWith("gemini") ? " (keyword fallback)" : ""}</span>
    <dl class="wo-fields">
      <div><dt>Problem</dt><dd>${esc(cap(String(w.category || "unknown").replace(/_/g, " ")))}</dd></div>
      <div><dt>Where</dt><dd>${where}</dd></div>
      <div><dt>How urgent</dt><dd>${w.urgency !== undefined && w.urgency !== null ? fmt(w.urgency, 0) + " / 100" : "n/a"}</dd></div>
      <div><dt>Suggested job</dt><dd>${esc(w.recommended_action || "Inspect")}</dd></div>
    </dl>
    ${w.hazard ? `<p class="note"><b>Possible danger.</b> Also use the <a href="${esc(w.official_report_url)}" target="_blank" rel="noopener">council reporting page</a>.</p>` : ""}
    ${w.merged_into ? `<p class="small muted">Merged into ${esc(w.merged_into)}: same problem within 15 m and 14 days.</p>` : ""}
    ${(w.merged_reports || []).length ? `<p class="small muted">${plural(w.merged_reports.length, "duplicate report", "duplicate reports")} merged into this one.</p>` : ""}
    <div class="wo-decide"><span class="step" style="color:#C9CED2">3 · You decide</span><b class="${w.status === "approved" ? "status-ok" : ""}">${esc(st[0])}</b><p>${esc(st[1])}</p>
      ${w.status === "pending_approval" ? `<div class="row"><button type="button" class="primary yellow" data-approve="${esc(w.report_id)}">Approve and send</button>
        <button type="button" class="ghost" data-reject="${esc(w.report_id)}">Not a road problem</button></div>` : ""}</div>
    <details><summary class="small">Draft work order (JSON)</summary><pre>${esc(JSON.stringify({ report_id: w.report_id, category: w.category,
      segment_id: w.segment_id, lamp_id: w.lamp_id, urgency: w.urgency, summary: w.summary, recommended_action: w.recommended_action,
      duplicate_of: w.merged_into }, null, 1))}</pre></details>
  </article>`;
}

async function loadQueue() {
  const q = await api("/triage/queue");
  const pending = q.filter((x) => x.status === "pending_approval").length;
  $("#qcount").textContent = pending || "";
  $("#queue").innerHTML = q.slice().reverse().map(woCard).join("") || `<p class="muted">No reports read yet. Pick one above.</p>`;
  $$("#queue [data-approve]").forEach((b) => b.onclick = async () => {
    const r = await post(`/workorders/${encodeURIComponent(b.dataset.approve)}/approve`, { officer: "officer" });
    await Promise.all([loadQueue(), loadPlan()]);
    if (r.segment) focusSegment(r.segment.segment_id, true);
  });
  $$("#queue [data-reject]").forEach((b) => b.onclick = async () => { await post(`/workorders/${encodeURIComponent(b.dataset.reject)}/reject`); loadQueue(); });
  $$("#queue [data-seg]").forEach((a) => a.onclick = (e) => { e.preventDefault(); showSegment(a.dataset.seg, true); });
}

async function triage(fmsId) {
  const btns = $$("#triage-form button"); btns.forEach((b) => b.disabled = true);
  $("#triage-err").classList.add("hidden");
  try {
    const body = { fms_id: Number(fmsId) };
    const f = $("#photo").files[0];
    if (f) { body.image_mime = f.type || "image/jpeg";
      body.image_b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); }); }
    await post("/reports", body); state.lastFms = fmsId; $("#photo").value = ""; await loadQueue();
  } catch (e) { $("#triage-err").textContent = "Couldn't read that report: " + e.message; $("#triage-err").classList.remove("hidden"); }
  finally { btns.forEach((b) => b.disabled = false); }
}
$("#triage-form").onsubmit = (e) => { e.preventDefault(); triage($("#fms-pick").value); };
$("#dup-btn").onclick = () => triage(state.lastFms || $("#fms-pick").value);

async function loadCityTriage(c) {
  let t;
  try { t = await api("/triage/city?" + qs({ city: c })); }
  catch (e) { $("#city-triage").innerHTML = `<p class="muted">No reports read for ${esc(c)} yet.</p>`; return; }
  $("#city-triage").innerHTML = `<p class="sub">${esc(c)} is read-only: ${plural(t.count, "open report", "open reports")} from the last ${t.window_days} days, read by AI. No work orders.</p>`
    + (t.reports.map((w) => `<article class="wo ${w.status === "merged" ? "merged" : ""}"><p class="wo-said">${esc(w.title)}</p>
      <dl class="wo-fields"><div><dt>Problem</dt><dd>${esc(cap(String(w.category || "").replace(/_/g, " ")))}</dd></div>
      <div><dt>Where</dt><dd>${w.segment_id ? `<a href="#" data-cseg="${esc(w.segment_id)}">${esc(w.segment_name)}</a>` : "Not on a council road"}</dd></div></dl>
      ${w.merged_into ? `<p class="small muted">Duplicate of ${esc(w.merged_into)}</p>` : ""}</article>`).join("") || `<p class="muted">No open reports.</p>`);
  $$("#city-triage [data-cseg]").forEach((a) => a.onclick = (e) => { e.preventDefault(); showCitySegment(a.dataset.cseg); });
}

// ---------- streetlights ----------
async function loadLights() {
  const l = await api("/lights?" + qs({ ward: state.ward, due: !$("#dark-only").checked, dark: $("#dark-only").checked, limit: 200 }));
  $("#lights-head").innerHTML = `${l.count_due} lamps due for replacement within ${l.due_days} days, ${l.count_currently_dark} dark now.
    <span class="tag standin">${l.kind === "mock" ? "Mock" : "Stand-in"} lamp register</span>
    Report a faulty light on the <a href="${esc(l.official_report_url)}" target="_blank" rel="noopener">council's streetlight page</a>.`;
  $("#light-list").innerHTML = l.lamps.map((x) => `<li><button type="button" class="road" data-lat="${x.lat}" data-lon="${x.lon}">
    <span class="road-main"><span class="road-name">${esc(x.name)} <span class="area">· ${esc(x.ward)}</span></span>
    <span class="road-why">Lamp ${esc(x.lamp_id)} · ${fmt(x.p_fail_90d * 100, 0)}% chance of failing in 90 days · replace by ${esc(x.replace_by)}</span></span>
    <span class="pill ${x.currently_dark ? "fix" : x.replacement_overdue ? "wait" : "watch"}">${x.currently_dark ? "Dark now" : x.replacement_overdue ? "Overdue" : "Due"}</span></button></li>`).join("");
  $$("#light-list .road").forEach((b) => b.onclick = () => state.mapReady && state.map.flyTo({ center: [+b.dataset.lon, +b.dataset.lat], zoom: 17 }));
}
$("#dark-only").onchange = loadLights;

// ---------- sources ----------
async function loadSources() {
  const s = await api("/sources");
  $("#sources").innerHTML = s.map((x) => `<span><span title="${esc(x.used_for)}">${esc(x.name)}</span>: ${esc(x.label)}${x.is_synthetic ? ` <span class="tag standin">${x.kind === "mock" ? "mock" : "stand-in"}</span>` : ""} <span title="${esc(x.licence)}">ⓘ</span></span>`).join("")
    + (state.meta ? `<span><b>Risk model:</b> ${esc(state.meta.badges.model_label)}</span>` : "")
    + `<span>Map © OpenStreetMap contributors</span><span>${OPEN_METEO}</span>`;
}

// ---------- map ----------
async function loadSegments() {
  const tok = state.segTok = (state.segTok || 0) + 1;
  const gj = await api("/segments?" + qs(otherCity() ? { city: state.city } : { ward: state.ward, scenario: state.scenario }));
  if (tok !== state.segTok) return null;
  state.gj = gj;
  return gj;
}

async function loadMap() {
  const tok = state.mapTok = (state.mapTok || 0) + 1;  // a later loadMap (city switch) makes this one stale
  const gj = await loadSegments();
  if (!gj || tok !== state.mapTok) return null;
  const m = state.map;
  if (m.getSource("segments")) m.getSource("segments").setData(gj); else {
    m.addSource("segments", { type: "geojson", data: gj });
    m.addLayer({ id: "segments", type: "line", source: "segments", paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.5, 16, 6],
      "line-color": ["case", ["==", ["get", "status"], "National Highways road, not scored"], "#B8BDC2",
        ["match", ["get", "band"], "critical", BAND_COLOR.critical, "high", BAND_COLOR.high, "prevention", BAND_COLOR.prevention, BAND_COLOR.monitor]],
      "line-opacity": 0.9 } });
    m.addLayer({ id: "segments-hl", type: "line", source: "segments", filter: ["==", ["get", "segment_id"], ""],
      paint: { "line-color": "#1F2326", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 7, 16, 16], "line-opacity": 0.85 } }, "segments");
    m.on("click", "segments", (e) => { e.preventDefault(); showSegment(e.features[0].properties.segment_id); });
    m.on("mouseenter", "segments", () => m.getCanvas().style.cursor = "pointer");
    m.on("mouseleave", "segments", () => m.getCanvas().style.cursor = "");
    m.on("click", (e) => { if (!e.defaultPrevented) locate(e.lngLat.lat, e.lngLat.lng); });
    fit(gj);
  }
  const open = await api("/reports/open" + (otherCity() ? "?" + qs({ city: state.city }) : ""));
  if (tok !== state.mapTok) return null;
  const fc = { type: "FeatureCollection", features: open.reports.map((r) => ({ type: "Feature",
    properties: { title: r.title, date: r.reported_at }, geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) };
  if (m.getSource("open")) m.getSource("open").setData(fc); else {
    m.addSource("open", { type: "geojson", data: fc });
    m.addLayer({ id: "open", type: "circle", source: "open", paint: { "circle-radius": 6, "circle-color": "#1F2326", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    m.on("click", "open", (e) => { e.preventDefault(); const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<b>Resident report</b><br>${esc(p.title)}<br><span class="small muted">Reported ${esc(p.date)}</span>`).addTo(m); });
  }
  const sites = await api("/context/sites?" + qs({ city: state.city })).catch(() => ({ sites: [] }));
  if (tok !== state.mapTok) return null;
  const sc = { type: "FeatureCollection", features: sites.sites.map((x) => ({ type: "Feature", properties: x, geometry: { type: "Point", coordinates: [x.lon, x.lat] } })) };
  if (m.getSource("sites")) m.getSource("sites").setData(sc); else {
    m.addSource("sites", { type: "geojson", data: sc });
    m.addLayer({ id: "sites", type: "circle", source: "sites", paint: { "circle-radius": 6, "circle-color": "#ffffff", "circle-stroke-color": "#2650b8", "circle-stroke-width": 3 } }, "open");
    m.on("click", "sites", (e) => { e.preventDefault(); const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<b>Building site</b><br>${esc(p.address || p.reference)}`).addTo(m); });
  }
  return gj;
}

function fit(gj) {
  const b = new maplibregl.LngLatBounds();
  gj.features.forEach((f) => f.geometry.coordinates.forEach((c) => b.extend(c)));
  if (!b.isEmpty()) state.map.fitBounds(b, { padding: 20, duration: 0 });
}

async function locate(lat, lon) {
  if (otherCity()) return;  // /locate covers the home city only
  const r = await api(`/locate?lat=${lat}&lon=${lon}`);
  if (otherCity()) return;
  if (r.status === "out_of_coverage") return roadCard(`<h1>Outside the city</h1><p class="sub">RoadSense covers Southampton City Council roads only.</p>`);
  if (r.status === "national_highways") return roadCard(`<h1>${esc(r.name)}</h1><p class="sub">A National Highways road (motorway), not a council road, so it isn't scored.</p>`);
  if (!r.segment_id) return roadCard(`<h1>No council road here</h1><p class="sub">${esc(r.message)}</p>`);
  showSegment(r.segment_id);
}

const DARK = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
function initMap() {
  state.map = new maplibregl.Map({ container: "map", center: [-1.40, 50.91], zoom: 12, style: {
    version: 8, sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
      attribution: "© OpenStreetMap contributors" } }, layers: [{ id: "osm", type: "raster", source: "osm",
      paint: DARK ? { "raster-saturation": -0.85, "raster-brightness-max": 0.62 } : { "raster-saturation": -0.75 } }] } });
  return new Promise((res) => state.map.on("load", res));
}

// ---------- city switch ----------
const HOME_TITLE = document.title;
async function setCity(c) {
  state.city = c;
  state.filter = "all";
  const other = otherCity();
  document.title = other ? `RoadSense · ${c}` : HOME_TITLE;
  $$("#city-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.city === c)));
  ["#triage-form", "#queue", "#to-lights", ".more"].forEach((s) => $(s).classList.toggle("hidden", other));
  $("#city-triage").classList.toggle("hidden", !other);
  $("#to-triage .cta-text b").firstChild.textContent = other ? "Resident reports (read-only) " : "New resident reports ";
  show("plan");
  $("#plan-list").innerHTML = `<li class="sk"></li><li class="sk"></li><li class="sk"></li>`;
  state.gj = null;
  if (state.mapReady) ["segments", "open", "sites"].forEach((id) => state.map.getSource(id) && state.map.getSource(id).setData({ type: "FeatureCollection", features: [] }));
  if (other) loadCityTriage(c);
  await loadPlan();
  const gj = state.mapReady ? await loadMap() : await loadSegments();
  if (gj && state.mapReady) fit(gj);
}

$("#ward").onchange = (e) => { state.ward = e.target.value; loadPlan(); if (state.mapReady) loadMap(); else loadSegments(); };
$("#scenario").onchange = async (e) => { state.scenario = e.target.value; $("#plan-sub").textContent = "Re-scoring…";
  await Promise.all([loadPlan(), state.mapReady ? loadMap() : loadSegments()]); };

(async () => {
  await loadMeta();
  if (window.maplibregl) initMap().then(async () => { state.mapReady = true; await loadMap(); state.map.resize(); }).catch(() => {});
  else $("#map").innerHTML = `<p class="muted" style="padding:20px">The map couldn't load (no connection to the map library). Everything else works.</p>`;
  api("/context/evaluation").then((v) => { state.ctxVerdict = v || {}; }).catch(() => {});
  await Promise.all([loadPlan(), loadQueue(), loadFms(), loadSources(), state.gj ? null : loadSegments()]);
})().catch((e) => { $("#stale").textContent = "Could not reach the RoadSense API: " + e.message; $("#stale").classList.remove("hidden"); });
