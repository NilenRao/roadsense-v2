// RoadSense officer page: plan, map, triage queue, streetlights. Talks only to the local API.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (x, d = 3) => (x === null || x === undefined ? "n/a" : Number(x).toFixed(d));
const BAND_COLOR = { critical: "#c62828", high: "#ef6c00", prevention: "#f9a825", monitor: "#7cb342" };
const state = { city: "", homeCity: "", ward: "", scenario: "", meta: null, map: null, lastFms: null, planView: "all", plan: null,
  ctxVerdict: {} };
const vbClass = (t) => (String(t).startsWith("Pays back") ? "pay" : String(t).startsWith("Confirmed") ? "must"
  : String(t).startsWith("Simulated") ? "sim" : "loss");

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v)).toString();
const wx = (s) => String(s || "").split(",").map((p) => p.split(":").pop()).filter(Boolean)
  .map((v, i) => (i === 0 ? "this week " : `wk ${i} `) + v.replace("climatology_10y", "climatology").replace("observed", "observed")).join(", ");
const gbp = (x) => "£" + Number(x).toFixed(2);
// Open-Meteo's terms require attribution wherever its weather data is shown
const OPEN_METEO = `<a class="credit" href="https://open-meteo.com/" target="_blank" rel="noopener">Weather data by Open-Meteo.com</a>`;
const badge = (t, cls = "") => `<span class="badge ${cls}">${t}</span>`;

async function loadMeta() {
  const m = await api("/meta?" + qs({ forecast_as_of: new URLSearchParams(location.search).get("forecast_as_of") }));
  state.meta = m;
  state.homeCity = state.homeCity || m.city;
  state.city = state.city || m.city;
  const cs = $("#city");
  if ((m.cities || []).length > 1 && cs.options.length === 0) {
    m.cities.forEach((c) => cs.add(new Option(c, c)));
    $("#city-wrap").classList.remove("hidden");
  }
  const sel = $("#ward");
  if (sel.options.length === 1) m.wards.forEach((w) => sel.add(new Option(w, w)));
  $("#badges").innerHTML = `<span class="home-only">` + badge("SYNTHETIC", "") + (m.badges.mock_inputs ? badge("MOCK INPUTS", "mock") : "")
    + (m.freshness.stale ? badge("STALE", "stale") : "") + (m.freshness.provisional ? badge("PROVISIONAL", "prov") : "") + `</span>`
    + (m.gemini ? "" : ` <span class="muted small">Gemini off: keyword triage</span>`);
  const b = $("#stale");
  b.innerHTML = esc(m.freshness.message + (m.badges.mock_inputs ? " " + m.badges.note : "")) + " · " + OPEN_METEO;
  b.classList.toggle("hidden", !m.freshness.provisional && !m.badges.mock_inputs);
  $("#official-link").href = m.official_report_url;
  const du = m.data_updated || {};
  const when = du.timestamp ? new Date(du.timestamp).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "unknown";
  $("#data-updated").innerHTML = `Data updated: ${esc(when)}` + (du.source === "snapshot" ? " (committed snapshot)" : "")
    + (du.stale ? " " + badge(`STALE: over ${du.stale_after_h} h old`, "amber") : "");
}

async function loadPlan() {
  const p = await api("/plan/prevention?" + qs({ ward: state.ward, scenario: state.scenario,
    forecast_as_of: new URLSearchParams(location.search).get("forecast_as_of") }));
  const asOf = p.as_of ? new Date(p.as_of + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }) : "unknown";
  $("#plan-head").innerHTML = `<span class="lead">${p.count} segments to treat${state.ward ? " in " + esc(state.ward) : ""}, week of ${esc(asOf)}</span> (as_of ${esc(p.as_of)}).`
    + (p.provisional ? " " + badge("PROVISIONAL", "prov") : "")
    + (p.scenario ? ` What-if <b>${esc(p.scenario)}</b> (${esc(p.scenario_method)}).` : "")
    + (p.model_label ? ` Model ${esc(p.model_version || "")}: <b>${esc(p.model_label)}</b>.` : "")
    + ` Ranked by reactive repair cost avoided over each treatment's life per £ of treatment (planned ${gbp(p.costs.planned_repair)} vs reactive ${gbp(p.costs.reactive_repair)}, ${esc(p.costs.source)}).`;
  $("#movers").innerHTML = p.movers.length ? `<div class="card"><b>Biggest risk rises under ${esc(p.scenario)}</b> <span class="small muted">urgency at the 4-week forecast peak, before → after</span>
    <table class="rows"><tbody>${p.movers.map((m) => `<tr data-id="${esc(m.segment_id)}"><td>${esc(m.name)} <span class="small muted">${esc(m.highway)}${m.bus_stop ? " · bus stop" : ""} · ${fmt(m.esal_day, 0)} ESAL/day</span></td>
      <td>${fmt(m.urgency_base, 0)} → <span class="u ${m.band_scn}">${fmt(m.urgency_scn, 0)}</span></td><td class="up">+${fmt(m.p30_peak_change)} P30</td></tr>`).join("")}</tbody></table></div>` : "";
  document.querySelectorAll("#movers tr[data-id]").forEach((tr) => tr.onclick = () => showSegment(tr.dataset.id, true));
  state.plan = p;
  renderPlanRows();
}

// council scheme lists: carriageway work done in the last 2 years (display only, not used in the ranking)
const treatedTag = (t, long) => !t ? "" : `<div class="small"><span class="vb treated" title="${esc(t.work + ": " + t.scheme + " (" + t.source + (t.match === "manual" ? ", manual match" : "") + ")")}">${esc(t.label)}</span>${long ? ` ${esc(t.work)}. Check before treating again.` : ""}</div>`;

// building sites near the road and utility digs on it (brownfield registers, OpenStreetMap, DfT Street Manager).
// Both feed the risk model (earlier years' sites and digs); ctxNote says what the held-out check found.
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const ctxTags = (c) => {
  if (!c) return "";
  const t = [];
  if (c.construction) t.push(`<span class="vb site" title="${esc(c.construction.sites.map((x) => x.address || x.reference).join("; "))}">${
    c.construction.count > 1 ? plural(c.construction.count, "building site", "building sites") : "Building site"} nearby</span>`);
  if (c.works) t.push(`<span class="vb dig" title="${esc(c.works.promoters.join(", "))}">Dug up ${c.works.count}× in ${c.works.months} months</span>`);
  return t.length ? `<div class="small ctx">${t.join(" ")}</div>` : "";
};
const ctxNote = (k) => {
  const v = state.ctxVerdict[k];
  if (!v) return "";
  if (!v.used_in_ranking) return "Context only: not an input to the risk model.";
  return v.clear_gain ? "Used in the risk model; clear gain on held-out years." : "Used in the risk model; on held-out years it has not improved the ranking.";
};
function ctxRows(c) {
  if (!c) return "";
  let h = "";
  if (c.construction) {
    const sites = c.construction.sites.map((x) => `<div>${esc(x.address || x.reference)} <span class="small muted">${[
      x.dwellings ? plural(x.dwellings, "home", "homes") : "", x.permission_date ? "permission " + esc(x.permission_date) : "",
      x.source === "osm_construction" ? "OpenStreetMap construction site" + (x.kind ? " (" + esc(x.kind) + ")" : "") : "brownfield register",
      x.public_land ? "public land" : "", fmt(x.dist_m, 0) + " m away"].filter(Boolean).join(" · ")}</span></div>`).join("");
    h += `<div>Building sites</div><div>${sites}${c.construction.count > 3 ? `<div class="small muted">and ${c.construction.count - 3} more</div>` : ""}
      <div class="small muted">Within 150 m; site lorries and new homes add heavy traffic. ${ctxNote("construction")}</div></div>`;
  }
  if (c.works) {
    const w = c.works;
    h += `<div>Street works</div><div>${plural(w.count, "dig", "digs")} completed in the last ${w.months} months${w.carriageway ? `, ${w.carriageway} in the carriageway` : ", footway or verge only"}; last ${esc(w.last)}
      <div class="small muted">${esc(w.promoters.join(", "))} · ${esc(w.activities.join(", "))}</div>
      <div class="small muted">DfT Street Manager. Trench reinstatements can fail early. ${ctxNote("utility_works")}</div></div>`;
  }
  return h;
}

function renderPlanRows() {
  const p = state.plan;
  if (!p) return;
  const worth = state.planView === "worth";
  const rows = worth ? p.worth.rows : p.rows;
  $("#worth-count").textContent = p.worth.rows.length || "";
  $("#worth-head").textContent = p.worth.header;
  $("#worth-head").classList.toggle("hidden", !worth);
  document.querySelectorAll(".subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.planView));
  $("#plan-rows").innerHTML = rows.map((r) => `<tr data-id="${esc(r.segment_id)}">
      <td>${r.rank}</td>
      <td><b>${esc(r.name)}</b> <span class="muted small">${esc(r.highway)} · ${esc(r.ward)}</span>
        <div class="reasons" title="${esc(r.reasons)}">${esc(String(r.reasons || "").split(" · ")[0])}${r.labels_synthetic ? " " + badge("SYNTHETIC") : r.history_source ? " " + badge("HISTORY SYNTHETIC") : ""}</div></td>
      <td><span class="u ${r.band}">${fmt(r.urgency, 0)}</span><div class="small muted">P30 ${fmt(r.p30_w0)} → ${fmt(r.p30_peak)}</div></td>
      <td>${esc(r.treatment_label)}<div class="small muted">£${fmt(r.cost_planned_gbp, 0)}</div>${treatedTag(r.recently_treated)}${ctxTags(r.context)}</td>
      <td><span class="vb ${vbClass(r.value_badge)}" title="${esc(p.horizon)}">${esc(r.value_badge)}</span>
        ${r.confirmed_pothole ? `<div class="small muted">${r.confirmed_by === "crew" ? "crew " : "FixMyStreet "}${esc(r.confirmed_source_id)} · ${esc(r.confirmed_by)}${r.confirmed_is_synthetic ? " " + badge("SYNTHETIC") : ""}</div>` : ""}
        ${r.confirmed_pothole && r.preventive_treatment ? `<div class="small muted">Also suggested: ${esc(r.preventive_treatment_label)} £${fmt(r.preventive_treatment_cost_gbp, 0)} (×${fmt(r.preventive_avoided_per_gbp, 2)})</div>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">${worth ? "No road pays back and no confirmed pothole yet." : "No segments in the prevention window are forecast to rise."}</td></tr>`;
  document.querySelectorAll("#plan-rows tr[data-id]").forEach((tr) => tr.onclick = () => showSegment(tr.dataset.id, true));
}

const otherCity = () => state.city && state.city !== state.homeCity;

async function loadMap() {
  const tok = state.mapTok = (state.mapTok || 0) + 1;  // a later loadMap (city switch) makes this one stale
  const gj = await api("/segments?" + qs(otherCity() ? { city: state.city } : { ward: state.ward, scenario: state.scenario }));
  if (tok !== state.mapTok) return null;
  const src = state.map.getSource("segments");
  if (src) src.setData(gj); else {
    state.map.addSource("segments", { type: "geojson", data: gj });
    state.map.addLayer({ id: "segments", type: "line", source: "segments", paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.5, 16, 6],
      "line-color": ["case", ["==", ["get", "status"], "National Highways road, not scored"], "#9e9e9e",
        ["match", ["get", "band"], "critical", BAND_COLOR.critical, "high", BAND_COLOR.high, "prevention", BAND_COLOR.prevention, BAND_COLOR.monitor]],
      "line-opacity": ["case", ["==", ["get", "status"], "National Highways road, not scored"], 0.5, 0.9] } });
    state.map.addLayer({ id: "segments-hl", type: "line", source: "segments", filter: ["==", ["get", "segment_id"], ""],
      paint: { "line-color": "#0d47a1", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 7, 16, 18], "line-opacity": 0.85 } }, "segments");
    state.map.on("click", "segments", (e) => { e.preventDefault(); showSegment(e.features[0].properties.segment_id); });
    state.map.on("mouseenter", "segments", () => state.map.getCanvas().style.cursor = "pointer");
    state.map.on("mouseleave", "segments", () => state.map.getCanvas().style.cursor = "");
    state.map.on("click", (e) => { if (!e.defaultPrevented) locate(e.lngLat.lat, e.lngLat.lng); });
    const b = new maplibregl.LngLatBounds();
    gj.features.forEach((f) => f.geometry.coordinates.forEach((c) => b.extend(c)));
    if (!b.isEmpty()) state.map.fitBounds(b, { padding: 20, duration: 0 });
  }
  const open = await api("/reports/open" + (otherCity() ? "?" + qs({ city: state.city }) : ""));
  if (tok !== state.mapTok) return null;
  const fc = { type: "FeatureCollection", features: open.reports.map((r) => ({ type: "Feature",
    properties: { title: r.title, date: r.reported_at, id: r.id }, geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) };
  if (state.map.getSource("open")) state.map.getSource("open").setData(fc); else {
    state.map.addSource("open", { type: "geojson", data: fc });
    state.map.addLayer({ id: "open", type: "circle", source: "open", paint: { "circle-radius": 5, "circle-color": "#000",
      "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    state.map.on("click", "open", (e) => { e.preventDefault(); const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<b>Open FixMyStreet report</b><br>${esc(p.title)}<br>Reported ${esc(p.date)}${otherCity() ? "" : " · urgency 100"}`).addTo(state.map); });
  }
  state.map.setLayoutProperty("open", "visibility", "visible");  // the selected city's open reports (same open window)
  const sites = await api("/context/sites?" + qs({ city: state.city })).catch(() => ({ sites: [] }));
  if (tok !== state.mapTok) return null;
  const sc = { type: "FeatureCollection", features: sites.sites.map((x) => ({ type: "Feature", properties: x,
    geometry: { type: "Point", coordinates: [x.lon, x.lat] } })) };
  if (state.map.getSource("sites")) state.map.getSource("sites").setData(sc); else {
    state.map.addSource("sites", { type: "geojson", data: sc });
    state.map.addLayer({ id: "sites", type: "circle", source: "sites", paint: { "circle-radius": 6, "circle-color": "#ffffff",
      "circle-stroke-color": "#2650b8", "circle-stroke-width": 3 } }, "open");
    state.map.on("click", "sites", (e) => { e.preventDefault(); const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setHTML(`<b>Building site</b><br>${esc(p.address || p.reference)}<br><span class="small muted">${
        p.source === "osm_construction" ? "OpenStreetMap construction site" : "Brownfield register" + (p.permission_date ? ", permission " + esc(p.permission_date) : "")
      }${p.dwellings ? " · " + esc(p.dwellings) + " homes" : ""}${p.public_land === true || p.public_land === "true" ? " · public land" : ""}</span>`).addTo(state.map); });
    state.map.on("mouseenter", "sites", () => state.map.getCanvas().style.cursor = "pointer");
    state.map.on("mouseleave", "sites", () => state.map.getCanvas().style.cursor = "");
  }
  state.gj = gj;
  return gj;
}

function detail(html) { const d = $("#detail"); d.innerHTML = `<button class="close" aria-label="Close" onclick="this.parentNode.classList.add('hidden')">×</button>` + html; d.classList.remove("hidden"); }

async function locate(lat, lon) {
  if (otherCity()) return;  // /locate covers the home city only
  const r = await api(`/locate?lat=${lat}&lon=${lon}`);
  if (otherCity()) return;
  if (r.status === "out_of_coverage") return detail(`<h3>Outside coverage</h3><p>RoadSense covers Southampton City Council roads only (E06000045).</p>`);
  if (r.status === "national_highways") return detail(`<h3>${esc(r.name)}</h3><p><b>National Highways road, not scored.</b> Motorways (M271, M27) are not council roads.</p>`);
  if (!r.segment_id) return detail(`<h3>No council road here</h3><p>${esc(r.message)}</p>`);
  showSegment(r.segment_id);
}

async function showCitySegment(id) {
  const c = state.city;
  let s;
  try { s = await api(`/segments/${encodeURIComponent(id)}?` + qs({ city: c })); }
  catch (e) { if (c === state.city) detail(`<h3>Road not available</h3><p class="small muted">${esc(e.message)}</p>`); return; }
  if (c !== state.city) return;  // the city changed while this was loading
  detail(`<h3>${esc(s.name)}</h3><div class="muted small">${esc(s.segment_id)} · ${esc(s.highway)} · ${esc(s.ward)} · ${esc(s.city)}</div>
    <p><span class="u ${s.band}">${fmt(s.urgency, 0)}</span> ${esc(s.band)} ${badge(s.badge, s.badge.startsWith("100%") ? "real" : "")}</p>
    <div class="kv">
      <div>Wear</div><div class="small muted">No resurfacing history for ${esc(s.city)}, so no wear term: urgency is P30 × consequence</div>
      <div>Risk P7 / P30</div><div>${fmt(s.p7)} / ${fmt(s.p30)}</div>
      <div>Consequence</div><div>×${fmt(s.consequence, 2)} ${esc(s.consequence_text)}</div>
      <div>Why</div><div>${s.reasons.map(esc).join("<br>") || "n/a"}</div>
      <div>Model</div><div class="small">${esc(s.model_version)} · trained on ${esc(s.trained_on)} reports · history ${esc(s.history_source)}</div>
      ${ctxRows(s.context)}</div>
    <p class="small muted">${esc(s.note)}</p>`);
}

// highlight a segment on the map and, with fly, fit the map to it (rows in the plan / inspection list)
function focusSegment(id, fly) {
  if (!state.mapReady || !state.map.getLayer("segments-hl")) return false;
  state.map.setFilter("segments-hl", ["==", ["get", "segment_id"], id]);
  const f = (state.gj ? state.gj.features : []).find((x) => x.properties.segment_id === id);
  if (!f || !fly) return !!f;
  const b = new maplibregl.LngLatBounds();
  (f.geometry.type === "MultiLineString" ? f.geometry.coordinates.flat() : f.geometry.coordinates).forEach((c) => b.extend(c));
  const c = state.map.getContainer(), p = Math.min(80, c.clientWidth / 6, c.clientHeight / 4);  // padding must fit inside the map
  state.map.fitBounds(b, { padding: { top: p, bottom: p, left: p, right: Math.min(c.clientWidth / 2, p + 390) }, maxZoom: 17 });  // clear of the road card
  return true;
}

async function showSegment(id, fly) {
  const found = focusSegment(id, fly);
  if (otherCity()) return showCitySegment(id);
  let s;
  try { s = await api(`/segments/${encodeURIComponent(id)}?` + qs({ scenario: state.scenario })); }
  catch (e) { if (!otherCity()) detail(`<h3>Road not available</h3><p class="small muted">${esc(e.message)}</p>`); return; }
  if (otherCity()) return;
  if (fly && !found && state.mapReady) state.map.flyTo({ center: [s.lon, s.lat], zoom: 16 });
  if (s.status !== "scored") return detail(`<h3>${esc(s.name)}</h3><p><b>${esc(s.status)}</b></p>`);
  const dd = s.data_dates;
  detail(`<h3>${esc(s.name)}</h3><div class="muted small">${esc(s.segment_id)} · ${esc(s.highway)} · ${esc(s.ward)}</div>
    <p><span class="u ${s.band}">${fmt(s.urgency, 0)}</span> ${esc(s.band)}${s.confirmed_open ? " · <b>confirmed open pothole</b>" : ""}</p>
    <div class="kv">
      <div>Wear</div><div>${fmt(s.wear_pct, 0)}% ${s.wear_synthetic ? badge("SYNTHETIC") : ""}</div>
      <div>Risk P7 / P30</div><div>${fmt(s.p7)} / ${fmt(s.p30)} ${s.labels_synthetic ? badge("SYNTHETIC LABELS") : ""}</div>
      <div>P30 next 4 wks</div><div>${s.risk_weeks.map((w) => `<span title="week of ${esc(w.week_start)}: ${esc(w.weather_basis || "")}${w.forecast_retrieved_at ? ", forecast retrieved " + esc(w.forecast_retrieved_at) : ""}">${fmt(w.p30)}${w.weather_basis === "climatology_10y" ? "<sup>c</sup>" : w.uses_forecast ? "<sup>f</sup>" : ""}</span>`).join(" → ")}
        <div class="small muted">f = Open-Meteo forecast, c = 10-year climatology (beyond the 16-day forecast) · ${OPEN_METEO}</div></div>
      <div>Model</div><div class="small">${esc(s.model_version)} · ${esc(state.meta ? state.meta.badges.model_label : s.trained_on)}${s.history_source ? " " + badge("HISTORY SYNTHETIC") : ""}</div>
      <div>Consequence</div><div>×${fmt(s.consequence, 2)} ${esc(s.consequence_text)}</div>
      <div>Load</div><div>${fmt(s.esal_day, 0)} ESAL/day · ${esc(s.traffic_note)}${s.load.count_year ? " (DfT count year " + s.load.count_year + ")" : ""}</div>
      <div>Treatment</div><div><b>${esc(s.treatment_label)}</b> £${fmt(s.treatment_cost_gbp, 0)}${treatedTag(s.recently_treated, true)}</div>
      ${s.ref ? `<div>Road</div><div>${esc(s.ref)}${s.bus_routes ? " · " + s.bus_routes + " bus routes" : ""}</div>` : ""}
      <div>Why</div><div>${s.reasons.map(esc).join("<br>") || "n/a"}</div>
      ${ctxRows(s.context)}
      <div>Data dates</div><div class="small">risk as of ${esc(dd.risk_as_of)}<br>${esc(dd.forecast.message)}<br>FixMyStreet retrieved ${esc((dd.fixmystreet_retrieved_at || "").slice(0, 10))}</div>
    </div>
    <div><b>Record crew outcome</b><br>
      <button onclick="outcome('${esc(s.segment_id)}','pothole')">Pothole</button>
      <button onclick="outcome('${esc(s.segment_id)}','cracking')">Cracking</button>
      <button onclick="outcome('${esc(s.segment_id)}','none')">Nothing found</button></div>
    ${s.outcomes.map((o) => `<div class="small muted">Outcome ${esc(o.outcome)} on ${esc(o.inspection_date)} (${esc(o.source)})</div>`).join("")}`);
}

window.outcome = async (sid, o) => {
  const r = await post("/outcomes", { segment_id: sid, outcome: o });
  await refresh();
  await showSegment(sid);
  $("#detail").insertAdjacentHTML("beforeend", `<p class="card">Re-scored: urgency ${fmt(r.before.urgency, 0)} (${esc(r.before.band)}) → <b>${fmt(r.after.urgency, 0)} (${esc(r.after.band)})</b>, treatment ${esc(r.after.treatment)}.</p>`);
};

async function loadFms() {
  const recent = await api("/reports/recent?n=40");
  $("#fms-pick").innerHTML = recent.map((r) => `<option value="${r.id}">${esc(r.reported_at)} · ${esc(r.title).slice(0, 60)}</option>`).join("");
}

async function loadQueue() {
  const q = await api("/triage/queue");
  $("#qcount").textContent = q.filter((x) => x.status === "pending_approval").length || "";
  $("#queue").innerHTML = q.slice().reverse().map((w) => `<div class="card ${w.status === "merged" ? "merged" : ""}">
    <div><span class="status">${esc(w.status.replace("_", " "))}</span> · <b>${esc(w.category)}</b> (${esc(w.method)}${w.tokens ? `, ${w.tokens.total_tokens} tokens` : ""})</div>
    <div>${esc(w.title)} <span class="muted small">reported ${esc(String(w.reported_at).slice(0, 10))} · ${esc(w.report_id)}</span></div>
    <div class="small">${w.segment_id ? `Segment <a href="#" onclick="showSegment('${esc(w.segment_id)}',true);return false">${esc(w.location.name)}</a> (${fmt(w.location.segment_distance_m, 0)} m)` : esc(w.location.message || "")}
      ${w.urgency !== undefined ? ` · urgency ${fmt(w.urgency, 0)} (risk model ${fmt(w.risk_urgency, 0)})` : ""}</div>
    ${w.merged_into ? `<div class="small">Merged into ${esc(w.merged_into)}: same defect within 15 m and 14 days</div>` : ""}
    ${(w.merged_reports || []).length ? `<div class="small">${w.merged_reports.length} duplicate report(s) merged in</div>` : ""}
    ${w.hazard ? `<div class="small"><b>Possible danger:</b> <a href="${esc(w.official_report_url)}" target="_blank" rel="noopener">council reporting page</a></div>` : ""}
    <details><summary class="small">Draft work order (JSON)</summary><pre>${esc(JSON.stringify({ report_id: w.report_id, category: w.category,
      segment_id: w.segment_id, lamp_id: w.lamp_id, urgency: w.urgency, summary: w.summary, recommended_action: w.recommended_action,
      duplicate_of: w.merged_into }, null, 1))}</pre></details>
    ${w.status === "pending_approval" ? `<button class="primary" onclick="approve('${esc(w.report_id)}')">Approve work order</button>
      <button onclick="reject('${esc(w.report_id)}')">Reject</button>` : ""}</div>`).join("") || `<p class="empty">No reports triaged yet.</p>`;
}

window.approve = async (id) => { const r = await post(`/workorders/${encodeURIComponent(id)}/approve`, { officer: "officer" });
  await refresh(); if (r.segment) showSegment(r.segment.segment_id, true); };
window.reject = async (id) => { await post(`/workorders/${encodeURIComponent(id)}/reject`); loadQueue(); };
window.showSegment = showSegment;

async function triage(fmsId) {
  const btns = document.querySelectorAll("#triage-form button"); btns.forEach((b) => b.disabled = true);
  $("#triage-err").classList.add("hidden");
  try {
    const body = { fms_id: Number(fmsId) };
    const f = $("#photo").files[0];
    if (f) { body.image_mime = f.type || "image/jpeg";
      body.image_b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); }); }
    await post("/reports", body); state.lastFms = fmsId; $("#photo").value = ""; await loadQueue();
  }
  catch (e) { $("#triage-err").textContent = "Triage failed: " + e.message; $("#triage-err").classList.remove("hidden"); } finally { btns.forEach((b) => b.disabled = false); }
}

async function loadLights() {
  const l = await api("/lights?" + qs({ ward: state.ward, due: !$("#dark-only").checked, dark: $("#dark-only").checked, limit: 200 }));
  $("#lights-head").innerHTML = `<span class="lead">${l.count_due} lamps due for replacement within ${l.due_days} days, ${l.count_currently_dark} currently dark,</span> as of ${esc(l.as_of)} ${badge(l.kind === "mock" ? "MOCK" : "SYNTHETIC", l.kind === "mock" ? "mock" : "")}. `
    + `Report a faulty light on the <a href="${esc(l.official_report_url)}" target="_blank" rel="noopener">council's streetlight page</a>.`;
  $("#light-rows").innerHTML = l.lamps.map((x) => `<tr data-lat="${x.lat}" data-lon="${x.lon}"><td>${esc(x.lamp_id)}<div class="small muted">${esc(x.lamp_type)}</div></td>
    <td>${esc(x.name)}<div class="small muted">${esc(x.ward)}</div></td><td>${fmt(x.p_fail_90d)}</td>
    <td>${esc(x.replace_by)}</td><td>${x.currently_dark ? "<b>dark</b> since " + esc(x.dark_since || "?") + " (" + esc(x.dark_source || "") + ")" : x.replacement_overdue ? "overdue" : ""}</td></tr>`).join("");
  document.querySelectorAll("#light-rows tr").forEach((tr) => tr.onclick = () => state.map.flyTo({ center: [+tr.dataset.lon, +tr.dataset.lat], zoom: 17 }));
}

async function loadSources() {
  const s = await api("/sources");
  $("#sources").innerHTML = "<b>Data sources:</b> " + s.map((x) => `<span class="${x.is_synthetic || x.name === "repair_costs" ? "home-only" : ""}"><span title="${esc(x.used_for)}">${esc(x.name)}</span>: ${esc(x.label)}${x.is_synthetic ? " " + badge(x.kind === "mock" ? "MOCK" : "SYNTHETIC", x.kind === "mock" ? "mock" : "") : ""} <span title="${esc(x.licence)}">ⓘ</span></span>`).join("")
    + (state.meta ? `<span class="home-only"><b>Risk model:</b> ${esc(state.meta.badges.model_label)}</span>` : "")
    + `<span>Map © OpenStreetMap contributors</span><span>${OPEN_METEO}</span>`;
}

// the plan never waits on the map: map layers load whenever the base map is ready
async function refresh() {
  await Promise.all([loadMeta(), loadPlan(), loadQueue(), state.mapReady ? loadMap() : null]);
  if (state.mapReady) state.map.resize();
}

// dim the base map in dark mode so the risk colours still read; the colours themselves never change
const DARK = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
function initMap() {
  state.map = new maplibregl.Map({ container: "map", center: [-1.40, 50.91], zoom: 12, style: {
    version: 8, sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
      attribution: "© OpenStreetMap contributors" } }, layers: [{ id: "osm", type: "raster", source: "osm", paint: DARK ? { "raster-saturation": -0.85, "raster-brightness-max": 0.62, "raster-contrast": 0.1 }
      : { "raster-saturation": -0.6 } }] } });
  return new Promise((res) => state.map.on("load", res));
}

document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "lights") loadLights();
});
async function loadCityTriage(c) {
  let t;
  try { t = await api("/triage/city?" + qs({ city: c })); }
  catch (e) { $("#city-triage").innerHTML = `<p class="muted">No cached triage for ${esc(c)} yet.</p>`; return; }
  $("#city-triage").innerHTML = `<p class="muted small"><b>${esc(c)}: read-only triage</b> of ${t.count} open FixMyStreet report(s) from the last
    ${t.window_days} days (method: ${esc(t.methods.join(", ") || "none")}, cached ${esc(String(t.generated_at).slice(0, 16).replace("T", " "))}).
    No work orders: ${esc(c)} is map only.</p>` + (t.reports.map((w) => `<div class="card ${w.status === "merged" ? "merged" : ""}">
    <div><span class="status">${esc(w.status)}</span> · <b>${esc(w.category)}</b> (${esc(w.method)}${w.model ? ", " + esc(w.model) : ""})</div>
    <div>${esc(w.title)} <span class="muted small">reported ${esc(w.reported_at)} · ${esc(w.report_id)}</span></div>
    <div class="small">${w.segment_id ? `Segment <a href="#" onclick="showCitySegment('${esc(w.segment_id)}');return false">${esc(w.segment_name)}</a> (${fmt(w.snap_m, 0)} m)${w.urgency != null ? ` · urgency <span class="u ${esc(w.risk_band)}">${fmt(w.urgency, 0)}</span>` : ""}` : "Not within 30 m of a council road"}</div>
    ${w.merged_into ? `<div class="small">Merged into ${esc(w.merged_into)}: same defect within 15 m and 14 days</div>` : ""}</div>`).join("")
    || `<p class="muted">No open reports in the window.</p>`);
}
window.showCitySegment = showCitySegment;

async function loadInspection(c) {
  const p = await api("/plan/inspection?" + qs({ city: c }));
  $("#inspect-head").innerHTML = `<b>${esc(p.header)}</b> ${badge(p.badge, p.badge.startsWith("100%") ? "real" : "")}<br>
    <span class="small">${p.count} segments: ${p.open_report_rows} with an open FixMyStreet report in the last ${p.window_days} days (first),
    then the top ${p.top_n} by urgency in ${esc(c)}.</span>`;
  const row = (r) => `<tr data-id="${esc(r.segment_id)}">
      <td>${r.rank}</td>
      <td><b>${esc(r.name)}</b> <span class="muted small">${esc(r.highway)} · ${esc(r.ward)}</span>
        ${r.open_report ? ` <span class="vb must">open report</span><div class="reasons">${r.open_reports.map((o) => `${esc(o.reported_at)}: ${esc(o.title)}`).join(" · ")}</div>` : ""}
        <div class="reasons" title="${esc(r.reasons.join(" · "))}">${esc(r.reasons[0] || "")}</div>${ctxTags(r.context)}</td>
      <td><span class="u ${r.band}">${fmt(r.urgency, 0)}</span><div class="small muted">P30 ${fmt(r.p30)}</div></td>
      <td>${esc(r.action)}</td></tr>`;
  $("#inspect-rows").innerHTML = p.sections.filter((s) => s.count).map((s) =>
    `<tr class="section"><th colspan="4">${esc(s.label)} <span class="muted small">${s.count} · ${esc(s.note)}</span></th></tr>`
    + p.rows.filter((r) => r.section === s.key).map(row).join("")).join("") || `<tr><td colspan="4" class="muted">No segments.</td></tr>`;
  document.querySelectorAll("#inspect-rows tr[data-id]").forEach((tr) => tr.onclick = () => showSegment(tr.dataset.id, true));
}

// page title per city; the home city keeps the original text
const CITY_TITLE = { Brighton: ["Brighton & Hove highways · inspection list", "RoadSense · Brighton & Hove"] };
const HOME_TITLE = [document.querySelector("#brand-sub").textContent, document.title];

async function setCity(c) {
  state.city = c;
  const other = otherCity();
  const [sub, title] = other ? (CITY_TITLE[c] || [`${c} highways · inspection list`, `RoadSense · ${c}`]) : HOME_TITLE;
  $("#brand-sub").textContent = sub;
  document.title = title;
  document.body.classList.toggle("other-city", other);
  ["#ward-wrap", "#scenario-wrap", "#triage-form", "#queue", "#triage-note"].forEach((s) => $(s).classList.toggle("hidden", other));
  $("#city-triage").classList.toggle("hidden", !other);
  [".subtabs", "#plan-head", "#movers", "#plan-table"].forEach((s) => $(s).classList.toggle("hidden", other));
  $("#worth-head").classList.toggle("hidden", other || state.planView !== "worth");
  $("#inspect").classList.toggle("hidden", !other);
  if (!other) renderPlanRows();
  if (other) { loadCityTriage(c); loadInspection(c); }
  const note = $("#city-note");
  note.classList.toggle("hidden", !other);
  $("#detail").classList.add("hidden");
  if (state.mapReady) {  // drop the old city's roads and reports at once so they can't be clicked while the new city loads
    const empty = { type: "FeatureCollection", features: [] };
    ["segments", "open", "sites"].forEach((id) => state.map.getSource(id) && state.map.getSource(id).setData(empty));
    state.gj = null;
  }
  if (other) {
    const gjMeta = (await api("/segments?" + qs({ city: c }))).meta;
    note.innerHTML = `<b>${esc(c)}: inspection list (real data)</b> ${badge(gjMeta.badge, gjMeta.badge.startsWith("100%") ? "real" : "")}<br>
      <span class="small">Roads coloured by urgency on Southampton's bands, from P30 and consequence (no wear model for ${esc(c)}; model ${esc(gjMeta.model_version)}, trained on ${esc(gjMeta.trained_on)} reports).
      The Prevention plan tab shows an inspection list from real reports; open FixMyStreet reports are on the map and in
      Report triage (read-only). Streetlights are <b>Southampton only</b>.</span>`;
  }
  if (state.mapReady) {
    const gj = await loadMap();
    if (!gj) return;  // superseded by a later city switch
    const b = new maplibregl.LngLatBounds();
    gj.features.forEach((f) => f.geometry.coordinates.forEach((x) => b.extend(x)));
    if (!b.isEmpty()) state.map.fitBounds(b, { padding: 20, duration: 0 });
  }
}
// side panel width: drag the handle (or arrow keys on it), double-click to reset; remembered in this browser
(function panelResizer() {
  const main = document.querySelector("main"), bar = $("#resizer"), KEY = "roadsense.panelWidth";
  const MIN = 320, mapMin = () => Math.min(360, main.clientWidth * 0.3);
  const clamp = (w) => Math.round(Math.max(MIN, Math.min(w, main.clientWidth - mapMin())));
  const apply = (w) => { main.style.setProperty("--panel-w", clamp(w) + "px"); bar.setAttribute("aria-valuenow", String(clamp(w)));
    if (state.mapReady) state.map.resize(); };
  const save = (w) => { try { localStorage.setItem(KEY, String(clamp(w))); } catch (e) { /* storage blocked: width lasts this visit only */ } };
  const reset = () => { main.style.removeProperty("--panel-w"); try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ }
    if (state.mapReady) state.map.resize(); };
  try { const w = +localStorage.getItem(KEY); if (w) apply(w); } catch (e) { /* ignore */ }
  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault(); bar.setPointerCapture(e.pointerId); document.body.classList.add("resizing");
    const left = main.getBoundingClientRect().left;
    const move = (ev) => apply(ev.clientX - left);
    const up = (ev) => { bar.removeEventListener("pointermove", move); bar.removeEventListener("pointerup", up);
      document.body.classList.remove("resizing"); save(ev.clientX - left); };
    bar.addEventListener("pointermove", move); bar.addEventListener("pointerup", up);
  });
  bar.addEventListener("dblclick", reset);
  bar.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const w = $("#panel").getBoundingClientRect().width + (e.key === "ArrowLeft" ? -40 : 40);
    apply(w); save(w);
  });
  window.addEventListener("resize", () => { const v = main.style.getPropertyValue("--panel-w"); if (v) apply(parseFloat(v)); });
})();
$("#city").onchange = (e) => setCity(e.target.value);
$("#ward").onchange = (e) => { state.ward = e.target.value; refresh(); if (!$("#tab-lights").classList.contains("hidden")) loadLights(); };
$("#scenario").onchange = async (e) => { state.scenario = e.target.value; $("#plan-head").textContent = "Re-scoring…"; await Promise.all([loadPlan(), state.mapReady ? loadMap() : null]); };
$("#dark-only").onchange = loadLights;
document.querySelectorAll(".subtabs button").forEach((b) => b.onclick = () => { state.planView = b.dataset.view; renderPlanRows(); });
$("#triage-form").onsubmit = (e) => { e.preventDefault(); triage($("#fms-pick").value); };
$("#dup-btn").onclick = () => triage(state.lastFms || $("#fms-pick").value);

(async () => {
  await loadMeta();
  initMap().then(async () => { state.mapReady = true; await loadMap(); state.map.resize(); });
  api("/context/evaluation").then((v) => { state.ctxVerdict = v || {}; }).catch(() => {});
  await Promise.all([refresh(), loadFms(), loadSources()]);
})().catch((e) => { $("#stale").textContent = "Could not reach the RoadSense API: " + e.message; $("#stale").classList.remove("hidden"); });
