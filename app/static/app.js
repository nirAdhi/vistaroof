/* Roofing Ledger — owner dashboard
 *
 * Structure of this file
 *   1. helpers: DOM shortcuts, escaping, money/date formatting, toast, api() (fetch wrapper that
 *      shows the login box on 401)
 *   2. tooltip + hand-drawn SVG charts (grouped columns, line, horizontal bars) with table twins
 *   3. renderAll(): fills every tab from /api/summary (tiles, charts, tables)
 *   4. documents table, document details drawer (edit / re-read)
 *   5. preview viewer (file + facts), company window, companies directory
 *   6. upload window (app -> Drive), sync panel, Ask tab
 *   7. navigation (tabs via URL hash), period presets, theme, login, live refresh, boot
 *
 * All data comes from the JSON API in app/main.py; nothing is computed server-side for the UI except
 * the aggregates in app/analytics.py. Every number in a chart is also reachable in a table view.
 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    config: null, meta: null, range: { from: null, to: null, preset: "last12" },
    summary: null, docs: [], chartModes: {}, syncTimer: null, emailRequired: false,
  };

  // ------------------------------------------------------------ formatting
  let moneyFmt, moneyFmt0;
  function setupFormats(currency) {
    moneyFmt = new Intl.NumberFormat("en-IE", { style: "currency", currency, maximumFractionDigits: 2 });
    moneyFmt0 = new Intl.NumberFormat("en-IE", { style: "currency", currency, maximumFractionDigits: 0 });
  }
  const money = (v, dec = true) => (v == null || isNaN(v)) ? "—" : (dec ? moneyFmt : moneyFmt0).format(v);
  function compact(v) {
    const a = Math.abs(v);
    const sym = (state.config && state.config.currency === "EUR") ? "€" : (state.config && state.config.currency === "GBP") ? "£" : "";
    const sign = v < 0 ? "-" : "";
    if (a >= 1e6) return `${sign}${sym}${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
    if (a >= 1e3) return `${sign}${sym}${(a / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K`;
    return `${sign}${sym}${Math.round(a)}`;
  }
  const label = (s) => s ? String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "—";
  /** Source kinds: Google Drive, Microsoft 365 (SharePoint/OneDrive) or a local folder. */
  const kindLabel = (k) => ({ drive: "Google Drive", m365: "SharePoint / OneDrive", local: "folder", demo: "demo" })[k] || k;
  /** Text for the link that opens the original file where it lives. */
  const openWhere = (d) => d.source === "m365" ? "Open in SharePoint" : d.source === "drive" ? "Open in Drive" : "Open original";
  const monthLabel = (m) => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleString("en-IE", { month: "short", year: "2-digit" }); };
  const fmtDate = (d) => d ? new Date(d + "T00:00:00").toLocaleDateString("en-IE", { day: "2-digit", month: "short", year: "numeric" }) : "—";
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  function toast(msg, ms = 2600) {
    const t = $("#toast"); t.textContent = msg; t.style.display = "block";
    clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
  }

  /** fetch() wrapper: JSON in/out, surfaces the API's `detail` message, shows the login box on 401. */
  async function api(path, opts = {}) {
    const isForm = opts.body instanceof FormData;
    const res = await fetch(path, { headers: isForm ? {} : { "Content-Type": "application/json" }, ...opts });
    if (res.status === 401 && path !== "/api/auth/login") { showLogin(); throw new Error("Please sign in"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  // ------------------------------------------------------------ login
  /** Signed-out state: nothing but the brand and the sign-in box (body.signed-out hides the rest, see styles.css). */
  function showLogin() {
    closeAccount();
    document.body.classList.add("signed-out");
    $("#authWrap").style.display = "grid";
    $("main").style.display = "none"; $(".filterbar").style.display = "none";
    $("#sourceLine").textContent = "Sign in to continue";
    $("#authEmail").required = state.emailRequired;  // ADMIN_EMAIL set on the server: the admin signs in with it, never with an empty email
    $("#authAdminHint").textContent = state.emailRequired ? "Administrator? Sign in with the admin email and password." : "Administrator? Enter the admin email (or leave it empty) and the admin password.";
    ($("#authEmail").value ? $("#authPassword") : $("#authEmail")).focus();
  }
  function hideLogin() {
    document.body.classList.remove("signed-out");
    $("#authWrap").style.display = "none"; $("main").style.display = ""; $(".filterbar").style.display = "";
  }
  $("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault(); $("#authError").textContent = "";
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify({ email: $("#authEmail").value.trim(), password: $("#authPassword").value }) });
      $("#authPassword").value = ""; hideLogin(); await init();
    } catch (err) { $("#authError").textContent = err.message; }
  });

  // ------------------------------------------------------------ account window (who am I, change password, log out)
  const accountModal = $("#accountModal"), accountBackdrop = $("#accountBackdrop");
  function closeAccount() { accountModal.classList.remove("open"); accountBackdrop.classList.remove("open"); }
  /** Reflect the signed-in person in the header and the account window. */
  function renderUser(u) {
    const me = u || {};
    $("#accountBtn").textContent = me.name || "Account";
    $("#accountName").textContent = me.name || "Account";
    $("#accountMeta").textContent = [me.email, me.role === "admin" ? "administrator" : "dashboard user"].filter(Boolean).join(" · ");
    $("#adminLink").classList.toggle("hidden", me.role !== "admin");
    const builtin = !me.uid;  // uid 0 = signed in with the admin password, which has no account row
    $("#accountPasswordForm").style.display = builtin ? "none" : "";
    $("#accountBuiltin").style.display = builtin ? "" : "none";
  }
  $("#accountBtn").addEventListener("click", () => { accountModal.classList.add("open"); accountBackdrop.classList.add("open"); });
  $("#accountClose").addEventListener("click", closeAccount);
  accountBackdrop.addEventListener("click", closeAccount);
  $("#accountPasswordForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    try { await api("/api/auth/password", { method: "POST", body: JSON.stringify(body) }); toast("Password changed"); e.target.reset(); closeAccount(); }
    catch (err) { toast(err.message, 6000); }
  });
  $("#logoutBtn").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); showLogin(); });

  // ------------------------------------------------------------ tooltip
  const tip = $("#tooltip");
  function showTip(html, ev) { tip.innerHTML = html; tip.style.display = "block"; moveTip(ev); }
  function moveTip(ev) {
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = ev.clientY - h - pad;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const hideTip = () => (tip.style.display = "none");
  function bindTips(root) {
    $$("[data-tip]", root).forEach((el) => {
      el.addEventListener("mouseenter", (e) => showTip(el.dataset.tip, e));
      el.addEventListener("mousemove", moveTip);
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("focus", (e) => { const r = el.getBoundingClientRect(); showTip(el.dataset.tip, { clientX: r.left + r.width / 2, clientY: r.top }); });
      el.addEventListener("blur", hideTip);
    });
  }

  // ------------------------------------------------------------ charts
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
    return m * p;
  }

  /** Grouped columns: rows = [{month, ...}], series = [{key,label,color}] */
  function groupedColumns(el, rows, series, opts = {}) {
    if (!rows.length) { el.innerHTML = `<div class="empty">No data for this period</div>`; return; }
    const W = 900, H = 260, padL = 48, padR = 12, padT = 12, padB = 30;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const vals = rows.flatMap((r) => series.map((s) => r[s.key] || 0));
    const maxV = niceMax(Math.max(...vals, 0));
    const minV = Math.min(0, ...vals);
    const minN = minV < 0 ? -niceMax(-minV) : 0;
    const y = (v) => padT + plotH * (1 - (v - minN) / (maxV - minN));
    const band = plotW / rows.length;
    const gap = 2;
    const barW = Math.min(24, (band * 0.7 - gap * (series.length - 1)) / series.length);
    const groupW = barW * series.length + gap * (series.length - 1);
    const ticks = 4;
    let g = "";
    for (let i = 0; i <= ticks; i++) {
      const v = minN + (maxV - minN) * i / ticks;
      g += `<line class="gridline" x1="${padL}" x2="${W - padR}" y1="${y(v)}" y2="${y(v)}"/>`;
      g += `<text x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`;
    }
    const zeroY = y(0);
    let bars = "";
    rows.forEach((r, i) => {
      const x0 = padL + band * i + (band - groupW) / 2;
      const tipHtml = `<b>${esc(opts.rowLabel ? opts.rowLabel(r) : r.month)}</b><br>` + series.map((s) => `${esc(s.label)}: ${money(r[s.key] || 0)}`).join("<br>") + (opts.extra ? "<br>" + opts.extra(r) : "");
      bars += `<g tabindex="0" data-tip="${esc(tipHtml)}"><rect class="hit" x="${padL + band * i}" y="${padT}" width="${band}" height="${plotH}"/>`;
      series.forEach((s, j) => {
        const v = r[s.key] || 0;
        const x = x0 + j * (barW + gap);
        const top = Math.min(y(v), zeroY), h = Math.abs(zeroY - y(v));
        const rr = Math.min(4, h / 2);
        const path = v >= 0
          ? `M${x},${zeroY} V${top + rr} Q${x},${top} ${x + rr},${top} H${x + barW - rr} Q${x + barW},${top} ${x + barW},${top + rr} V${zeroY} Z`
          : `M${x},${zeroY} V${zeroY + h - rr} Q${x},${zeroY + h} ${x + rr},${zeroY + h} H${x + barW - rr} Q${x + barW},${zeroY + h} ${x + barW},${zeroY + h - rr} V${zeroY} Z`;
        bars += `<path class="mark" d="${path}" fill="${s.color}"/>`;
      });
      const every = rows.length > 18 ? 3 : rows.length > 9 ? 2 : 1;
      if (i % every === 0) bars += `<text x="${padL + band * i + band / 2}" y="${H - 8}" text-anchor="middle">${esc(opts.xLabel ? opts.xLabel(r) : monthLabel(r.month))}</text>`;
      bars += `</g>`;
    });
    el.innerHTML = `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.aria || "column chart")}">${g}<line class="baseline" x1="${padL}" x2="${W - padR}" y1="${zeroY}" y2="${zeroY}"/>${bars}</svg>
      <div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div></div>`;
    bindTips(el);
  }

  function lineChart(el, rows, key, color, opts = {}) {
    if (!rows.length) { el.innerHTML = `<div class="empty">No data for this period</div>`; return; }
    const W = 600, H = 240, padL = 50, padR = 50, padT = 14, padB = 30;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const vals = rows.map((r) => r[key] || 0);
    const maxV = niceMax(Math.max(...vals, 0)), minRaw = Math.min(0, ...vals);
    const minV = minRaw < 0 ? -niceMax(-minRaw) : 0;
    const y = (v) => padT + plotH * (1 - (v - minV) / (maxV - minV));
    const x = (i) => padL + (rows.length === 1 ? plotW / 2 : plotW * i / (rows.length - 1));
    let g = "";
    for (let i = 0; i <= 4; i++) {
      const v = minV + (maxV - minV) * i / 4;
      g += `<line class="gridline" x1="${padL}" x2="${W - padR}" y1="${y(v)}" y2="${y(v)}"/><text x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`;
    }
    const pts = rows.map((r, i) => `${x(i)},${y(r[key] || 0)}`);
    const area = `M${x(0)},${y(0)} L${pts.join(" L")} L${x(rows.length - 1)},${y(0)} Z`;
    let dots = "";
    rows.forEach((r, i) => {
      const t = `<b>${esc(monthLabel(r.month))}</b><br>${esc(opts.label || label(key))}: ${money(r[key] || 0)}`;
      dots += `<g tabindex="0" data-tip="${esc(t)}"><rect class="hit" x="${x(i) - (plotW / rows.length) / 2}" y="${padT}" width="${plotW / rows.length}" height="${plotH}"/><circle cx="${x(i)}" cy="${y(r[key] || 0)}" r="6" fill="var(--surface)"/><circle class="mark" cx="${x(i)}" cy="${y(r[key] || 0)}" r="4" fill="${color}"/></g>`;
    });
    const every = rows.length > 12 ? 3 : rows.length > 6 ? 2 : 1;
    const xl = rows.map((r, i) => i % every === 0 ? `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(monthLabel(r.month))}</text>` : "").join("");
    const last = rows[rows.length - 1];
    const endLabel = `<text class="ink" x="${x(rows.length - 1) + 8}" y="${y(last[key] || 0) + 4}" font-weight="600">${compact(last[key] || 0)}</text>`;
    el.innerHTML = `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.aria || "line chart")}">${g}<line class="baseline" x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}"/>
      <path d="${area}" fill="${color}" opacity="0.1"/><path d="M${pts.join(" L")}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>${dots}${endLabel}${xl}</svg></div>`;
    bindTips(el);
  }

  /** Horizontal bars for a ranked list: rows = [{name, amount, count}] */
  function hbars(el, rows, color, opts = {}) {
    rows = rows.filter((r) => r.amount !== 0).slice(0, opts.limit || 10);
    if (!rows.length) { el.innerHTML = `<div class="empty">Nothing here for this period</div>`; return; }
    const maxV = Math.max(...rows.map((r) => Math.abs(r.amount)));
    const rowH = 30, labelW = 170, W = 600, padR = 70;
    const H = rows.length * rowH + 4;
    const plotW = W - labelW - padR;
    let b = "";
    rows.forEach((r, i) => {
      const w = Math.max(2, plotW * Math.abs(r.amount) / maxV);
      const yy = i * rowH + 4, h = 18, rr = 4;
      const x0 = labelW;
      const path = `M${x0},${yy} H${x0 + w - rr} Q${x0 + w},${yy} ${x0 + w},${yy + rr} V${yy + h - rr} Q${x0 + w},${yy + h} ${x0 + w - rr},${yy + h} H${x0} Z`;
      const name = r.name.length > 26 ? r.name.slice(0, 25) + "…" : r.name;
      const t = `<b>${esc(r.name)}</b><br>${money(r.amount)}${r.count != null ? `<br>${r.count} document${r.count === 1 ? "" : "s"}` : ""}`;
      b += `<g tabindex="0" data-tip="${esc(t)}" ${opts.onClick ? `data-name="${esc(r.name)}" style="cursor:pointer"` : ""}><rect class="hit" x="0" y="${yy - 4}" width="${W}" height="${rowH}"/>
        <text class="ink" x="${labelW - 10}" y="${yy + 13}" text-anchor="end">${esc(opts.labelFn ? opts.labelFn(r.name) : name)}</text>
        <path class="mark" d="${path}" fill="${r.amount < 0 ? "var(--muted)" : color}"/>
        <text class="ink" x="${x0 + w + 8}" y="${yy + 13}">${compact(r.amount)}</text></g>`;
    });
    el.innerHTML = `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.aria || "bar chart")}">${b}</svg></div>`;
    bindTips(el);
    if (opts.onClick) $$("g[data-name]", el).forEach((g) => g.addEventListener("click", () => opts.onClick(g.dataset.name)));
  }

  function tableHtml(cols, rows, opts = {}) {
    if (!rows.length) return `<div class="empty">${esc(opts.empty || "Nothing here for this period")}</div>`;
    const th = cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
    const body = rows.map((r) => `<tr ${opts.rowAttr ? opts.rowAttr(r) : ""}>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("")}</tr>`).join("");
    return `<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
  }

  /** Chart card with a chart/table toggle. renderChart(el) and tableRows() supplied by caller. */
  function chartCard(id, renderChart, tableCols, tableRows) {
    const el = $(`#chart-${id}`);
    const mode = state.chartModes[id] || "chart";
    if (mode === "chart") renderChart(el);
    else el.innerHTML = `<div class="table-scroll" style="max-height:320px">${tableHtml(tableCols, tableRows())}</div>`;
    const toggle = $(`.view-toggle[data-chart="${id}"]`);
    if (toggle) $$("button", toggle).forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  }
  $$(".view-toggle").forEach((t) => $$("button", t).forEach((b) => b.addEventListener("click", () => {
    state.chartModes[t.dataset.chart] = b.dataset.mode; renderAll();
  })));

  // ------------------------------------------------------------ tiles
  function tile({ label: l, value, sub, cls = "", color, pct, onClick }) {
    const bar = pct != null ? `<div class="bar"><i style="width:${Math.max(0, Math.min(100, pct))}%;background:${color || "var(--accent)"}"></i></div>` : "";
    return `<div class="card tile ${cls} ${onClick ? "clickable" : ""}" ${onClick ? `data-go="${onClick}"` : ""}><div class="label">${esc(l)}</div><div class="value ${value < 0 ? "neg" : ""}">${typeof value === "number" ? money(value, false) : esc(value)}</div>${sub ? `<div class="delta">${sub}</div>` : ""}${bar}</div>`;
  }

  function statusChip(s) { return s ? `<span class="status ${esc(s)}">${label(s)}</span>` : "—"; }
  function treatmentChip(t) {
    const cls = t === "revenue" || t === "payment" ? "in" : t === "expense" ? "out" : (t === "pipeline" || t === "commitment") ? "pipe" : "";
    return `<span class="chip ${cls}">${label(t)}</span>`;
  }
  const docLink = (d) => `<a href="#" data-doc="${d.id}">${esc(d.name)}</a>`;
  const previewBtn = (d) => `<button class="eye" data-preview="${d.id}" title="See the actual document">👁 Preview</button>`;
  const companyLink = (name) => name ? `<button class="link-btn" data-company="${esc(name)}">${esc(name)}</button>` : "—";
  function bindPreviews(root, list) { $$("[data-preview]", root).forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openPreview(+b.dataset.preview, list); })); }
  function bindCompanies(root) { $$("[data-company]", root).forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openCompany(b.dataset.company); })); }

  // ------------------------------------------------------------ preview viewer (the actual file)
  const viewer = $("#viewerModal"), viewerBackdrop = $("#viewerBackdrop");
  function closeViewer() { viewer.classList.remove("open"); viewerBackdrop.classList.remove("open"); viewer.innerHTML = ""; }
  viewerBackdrop.addEventListener("click", closeViewer);
  /** What was read from the document, laid out next to the file. */
  function factsHtml(d) {
    const open = new Set(["unpaid", "partial", "overdue", "unknown"]);
    const outstanding = (d.doc_type === "sales_invoice" || d.doc_type === "supplier_invoice") && open.has(d.payment_status) ? Math.max((d.total || d.net_amount || 0) - (d.amount_paid || 0), 0) : null;
    const row = (l, v) => v == null || v === "" ? "" : `<dt>${l}</dt><dd>${v}</dd>`;
    if (d.status !== "done") return `<div class="note"><b>${label(d.status)}</b>${d.error ? ` — ${esc(d.error)}` : d.status === "pending" ? " — not read yet. Run a sync." : ""}</div>`;
    const items = (d.line_items || []).length ? `<h4>Line items</h4>${tableHtml([
      { key: "description", label: "Description" }, { key: "quantity", label: "Qty", num: true, render: (r) => r.quantity ?? "" },
      { key: "unit_price", label: "Unit", num: true, render: (r) => r.unit_price != null ? money(r.unit_price) : "" },
      { key: "amount", label: "Amount", num: true, render: (r) => r.amount != null ? money(r.amount) : "" }], d.line_items)}` : "";
    return `
      <div class="facts-head">${treatmentChip(d.treatment)} <b>${esc(label(d.doc_type))}</b>${d.direction === "money_in" ? ' <span class="chip in">money in</span>' : d.direction === "money_out" ? ' <span class="chip out">money out</span>' : ""}</div>
      ${d.summary ? `<p class="facts-summary">${esc(d.summary)}</p>` : ""}
      <dl class="facts-grid">
        ${row(d.counterparty_role === "customer" ? "Customer" : d.counterparty_role === "supplier" ? "Supplier" : "Who", d.counterparty ? companyLink(d.counterparty) : null)}
        ${row("Number", esc(d.document_number))}
        ${row("Date", d.issue_date ? fmtDate(d.issue_date) : null)}
        ${row("Due", d.due_date ? fmtDate(d.due_date) : null)}
        ${row("Job / site", esc(d.project))}
        ${row("Category", d.category ? esc(label(d.category)) : null)}
        ${row("Net (ex VAT)", d.subtotal != null ? money(d.subtotal) : null)}
        ${row("VAT", d.vat_amount != null ? money(d.vat_amount) : null)}
        ${row("Total", d.total != null ? `<b>${money(d.total)}</b>` : null)}
        ${row("Paid", d.payment_status && d.payment_status !== "not_applicable" ? statusChip(d.payment_status) + (d.amount_paid ? ` · ${money(d.amount_paid)} received` : "") : null)}
        ${row("Outstanding", outstanding ? `<b class="neg">${money(outstanding)}</b>` : null)}
        ${row("Confidence", d.confidence != null ? `${(d.confidence * 100).toFixed(0)}%` + ((d.flags || []).length ? " · " + d.flags.map((f) => `<span class="flag">${esc(label(f))}</span>`).join(" ") : "") : null)}
        ${row("File", `${esc(d.name)}<div class="muted small">${esc(d.source_name || "")}${d.folder ? " · " + esc(d.folder) : ""}</div>`)}
      </dl>
      ${items}
      ${d.overridden ? `<p class="muted small">Edited by hand — these values override what was read.</p>` : ""}`;
  }

  function fileFrame(d) {
    return `<iframe src="/api/documents/${d.id}/preview?embed=1" title="Preview of ${esc(d.name)}"></iframe>${d.source === "demo" ? `<div class="sample-tag">Sample rendering of demo data — real documents show the actual scan, photo or PDF</div>` : ""}`;
  }

  async function fullDoc(docOrId) {
    if (typeof docOrId === "object" && docOrId.line_items !== undefined && docOrId.raw !== undefined) return docOrId;
    return api(`/api/documents/${typeof docOrId === "object" ? docOrId.id : docOrId}`);
  }

  /** Preview window: the file on the left, what was read on the right, prev/next through `list`. */
  async function openPreview(docOrId, list) {
    const d = await fullDoc(docOrId);
    if (list) { state.viewerList = list; state.viewerIndex = list.findIndex((x) => x.id === d.id); }
    else if (!state.viewerList || !state.viewerList.some((x) => x.id === d.id)) { state.viewerList = [d]; state.viewerIndex = 0; }
    else state.viewerIndex = state.viewerList.findIndex((x) => x.id === d.id);
    const n = state.viewerList.length, i = state.viewerIndex;
    viewer.innerHTML = `
      <div class="modal-head">
        <div><h2>${esc(d.name)}</h2><div class="muted small">${esc(d.source_name || "")}${d.counterparty ? ` · ${esc(d.counterparty)}` : ""}${d.total != null ? ` · ${money(d.total)}` : ""}${n > 1 ? ` · ${i + 1} of ${n}` : ""}</div></div>
        <div class="actions" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          ${n > 1 ? `<button class="btn sm" id="viewerPrev" ${i <= 0 ? "disabled" : ""}>‹ Previous</button><button class="btn sm" id="viewerNext" ${i >= n - 1 ? "disabled" : ""}>Next ›</button>` : ""}
          <a class="btn sm" href="/api/documents/${d.id}/preview" target="_blank" rel="noopener">Open full size</a>
          ${d.web_link ? `<a class="btn sm" href="${esc(d.web_link)}" target="_blank" rel="noopener">${openWhere(d)}</a>` : ""}
          <button class="btn sm" id="viewerDetails">Edit details</button>
          <button class="icon-btn" id="viewerClose" aria-label="Close">✕</button>
        </div>
      </div>
      <div class="modal-body split">
        <div class="pane-file">${fileFrame(d)}</div>
        <div class="facts">${factsHtml(d)}</div>
      </div>`;
    $("#viewerClose").addEventListener("click", closeViewer);
    $("#viewerDetails").addEventListener("click", () => { closeViewer(); openDoc(d.id); });
    const pv = $("#viewerPrev"), nx = $("#viewerNext");
    if (pv) pv.addEventListener("click", () => openPreview(state.viewerList[i - 1]));
    if (nx) nx.addEventListener("click", () => openPreview(state.viewerList[i + 1]));
    bindCompanies(viewer);
    viewer.classList.add("open"); viewerBackdrop.classList.add("open");
  }
  document.addEventListener("keydown", (e) => {
    if (!viewer.classList.contains("open")) return;
    if (e.key === "ArrowLeft" && $("#viewerPrev") && !$("#viewerPrev").disabled) $("#viewerPrev").click();
    if (e.key === "ArrowRight" && $("#viewerNext") && !$("#viewerNext").disabled) $("#viewerNext").click();
  });

  // ------------------------------------------------------------ company view
  const companyModal = $("#companyModal"), companyBackdrop = $("#companyBackdrop");
  function closeCompany() { companyModal.classList.remove("open"); companyBackdrop.classList.remove("open"); }
  companyBackdrop.addEventListener("click", closeCompany);
  /** Company window: all-time totals computed client-side from that counterparty's documents, a document
   *  list on the left and the selected document's file + facts on the right. */
  async function openCompany(name) {
    const docs = await api(`/api/documents?counterparty=${encodeURIComponent(name)}&limit=5000`);
    const done = docs.filter((d) => d.status === "done");
    const open = new Set(["unpaid", "partial", "overdue", "unknown"]);
    const sum = (f) => done.reduce((a, d) => a + (f(d) || 0), 0);
    const revenue = sum((d) => d.treatment === "revenue" ? d.net_amount * d.sign : 0);
    const expenses = sum((d) => d.treatment === "expense" ? d.net_amount * d.sign : 0);
    const gross = sum((d) => (d.treatment === "revenue" || d.treatment === "expense") ? (d.total || 0) * d.sign : 0);
    const outstanding = sum((d) => (d.doc_type === "sales_invoice" || d.doc_type === "supplier_invoice") && open.has(d.payment_status) && d.payment_status !== "paid" ? Math.max((d.total || d.net_amount || 0) - (d.amount_paid || 0), 0) : 0);
    const pipeline = sum((d) => (d.treatment === "pipeline" || d.treatment === "commitment") ? d.net_amount * d.sign : 0);
    const roles = {}; done.forEach((d) => { roles[d.counterparty_role || "other"] = (roles[d.counterparty_role || "other"] || 0) + 1; });
    const role = Object.keys(roles).sort((a, b) => roles[b] - roles[a])[0] || "other";
    const dates = done.map((d) => d.issue_date).filter(Boolean).sort();
    const isCustomer = revenue >= expenses;
    const stat = (l, v, cls = "") => `<div class="stat"><div class="label">${l}</div><div class="value ${cls}">${typeof v === "number" ? money(v, false) : v}</div></div>`;
    const typeCounts = {}; docs.forEach((d) => { if (d.doc_type) typeCounts[d.doc_type] = (typeCounts[d.doc_type] || 0) + 1; });
    const mix = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]).map(([t, c]) => `${c} ${label(t).toLowerCase()}${c > 1 ? "s" : ""}`).join(", ");
    companyModal.innerHTML = `
      <div class="modal-head">
        <div><h2>${esc(name)}</h2><div class="muted small">${esc(label(role))} · ${docs.length} document${docs.length === 1 ? "" : "s"}${mix ? ` (${esc(mix)})` : ""}${dates.length ? ` · ${fmtDate(dates[0])} to ${fmtDate(dates[dates.length - 1])}` : ""} · all time</div></div>
        <button class="icon-btn" id="companyClose" aria-label="Close">✕</button>
      </div>
      <div class="modal-body">
        <div class="stats">
          ${isCustomer ? stat("Invoiced to them (net)", revenue) : stat("Spent with them (net)", expenses)}
          ${stat("Gross incl. VAT", gross)}
          ${stat(isCustomer ? "They still owe" : "We still owe", outstanding, outstanding ? "neg" : "")}
          ${pipeline ? stat(isCustomer ? "Quoted, not yet invoiced" : "On order, not yet billed", pipeline) : ""}
          ${isCustomer && expenses ? stat("Also spent with them", expenses) : !isCustomer && revenue ? stat("Also invoiced to them", revenue) : ""}
        </div>
        <div class="split company-split">
          <div class="table-scroll doc-list">${tableHtml([
            { key: "issue_date", label: "Date", render: (r) => fmtDate(r.issue_date) },
            { key: "doc_type", label: "Type", render: (r) => r.status === "done" ? esc(label(r.doc_type)) : `<span class="chip">${esc(label(r.status))}</span>` },
            { key: "document_number", label: "Number", render: (r) => esc(r.document_number || "") },
            { key: "payment_status", label: "Paid?", render: (r) => r.payment_status && r.payment_status !== "not_applicable" ? statusChip(r.payment_status) : "" },
            { key: "total", label: "Total", num: true, render: (r) => r.total != null ? money(r.total) : "" },
          ], docs, { empty: "No documents", rowAttr: (r) => `class="row-link" data-sel="${r.id}"` })}</div>
          <div class="pane" id="companyPane"><div class="empty">Select a document to see it</div></div>
        </div>
      </div>`;
    $("#companyClose").addEventListener("click", closeCompany);
    const select = async (id) => {
      $$("#companyModal tr[data-sel]").forEach((tr) => tr.classList.toggle("selected", +tr.dataset.sel === id));
      const d = await fullDoc(id);
      $("#companyPane").innerHTML = `
        <div class="pane-file">${fileFrame(d)}</div>
        <div class="pane-actions"><a class="btn sm" href="/api/documents/${d.id}/preview" target="_blank" rel="noopener">Open full size</a>${d.web_link ? ` <a class="btn sm" href="${esc(d.web_link)}" target="_blank" rel="noopener">${openWhere(d)}</a>` : ""} <button class="btn sm" id="paneBig">Large preview</button> <button class="btn sm" id="paneEdit">Edit details</button></div>
        <div class="facts">${factsHtml(d)}</div>`;
      $("#paneBig").addEventListener("click", () => openPreview(d, docs));
      $("#paneEdit").addEventListener("click", () => openDoc(d.id));
      bindCompanies($("#companyPane"));
    };
    $$("#companyModal tr[data-sel]").forEach((tr) => tr.addEventListener("click", () => select(+tr.dataset.sel)));
    companyModal.classList.add("open"); companyBackdrop.classList.add("open");
    if (docs.length) select(docs[0].id);
  }

  // ------------------------------------------------------------ companies directory
  let companiesCache = null;
  async function loadCompanies() {
    companiesCache = await api("/api/companies");
    renderCompanies();
  }
  function renderCompanies() {
    const q = $("#companySearch").value.trim().toLowerCase(), role = $("#companyRole").value;
    const rows = (companiesCache || []).filter((c) => (!q || c.name.toLowerCase().includes(q)) && (!role || c.role === role));
    $("#companyCount").textContent = `${rows.length} compan${rows.length === 1 ? "y" : "ies"}`;
    $("#companyTable").innerHTML = tableHtml([
      { key: "name", label: "Company", render: (r) => companyLink(r.name) },
      { key: "role", label: "Role", render: (r) => esc(label(r.role)) },
      { key: "docs", label: "Docs", num: true },
      { key: "revenue", label: "Invoiced to them", num: true, render: (r) => r.revenue ? money(r.revenue) : "" },
      { key: "expenses", label: "Spent with them", num: true, render: (r) => r.expenses ? money(r.expenses) : "" },
      { key: "outstanding", label: "Outstanding", num: true, render: (r) => r.outstanding ? `<b class="neg">${money(r.outstanding)}</b>` : "" },
      { key: "pipeline", label: "Quoted / on order", num: true, render: (r) => r.pipeline ? money(r.pipeline) : "" },
      { key: "last_date", label: "Last document", render: (r) => fmtDate(r.last_date) },
    ], rows, { empty: "No companies yet" });
    bindCompanies($("#companyTable"));
  }
  $("#companySearch").addEventListener("input", renderCompanies);
  $("#companyRole").addEventListener("change", renderCompanies);

  // ------------------------------------------------------------ render views
  /** Re-draw every tab from state.summary (the /api/summary payload for the selected period). */
  function renderAll() {
    const s = state.summary; if (!s) return;
    const t = s.totals;
    $("#periodNote").textContent = `${t.documents} documents in period` + (t.undated_docs ? ` · ${t.undated_docs} without a date (placed by file date)` : "");

    // ---- Overview tiles
    const margin = t.margin_pct != null ? `${t.margin_pct.toFixed(1)}% margin` : "";
    $("#tiles").innerHTML = [
      tile({ label: "Net profit (revenue − costs)", value: t.profit, sub: margin, cls: "hero" }),
      tile({ label: "Revenue (net of VAT)", value: t.revenue, sub: `${t.revenue_docs} sales documents · gross ${money(t.revenue_gross, false)}`, onClick: "money-in" }),
      tile({ label: "Costs (net of VAT)", value: t.expenses, sub: `${t.expense_docs} cost documents · gross ${money(t.expenses_gross, false)}`, onClick: "money-out" }),
      tile({ label: "Owed to us (unpaid invoices)", value: t.receivables, sub: `${t.receivables_count} invoices · <span class="${t.overdue_receivables ? "neg" : ""}">${money(t.overdue_receivables, false)} overdue</span>`, onClick: "money-in" }),
      tile({ label: "We owe (unpaid bills)", value: t.payables, sub: `${t.payables_count} bills · <span class="${t.overdue_payables ? "neg" : ""}">${money(t.overdue_payables, false)} overdue</span>`, onClick: "money-out" }),
      tile({ label: "VAT position", value: t.vat_position, sub: `charged ${money(t.vat_on_sales, false)} · paid ${money(t.vat_on_purchases, false)}` }),
      tile({ label: "Pipeline (quotes & proformas)", value: t.pipeline, sub: `${t.pipeline_count} documents`, onClick: "money-in" }),
    ].join("");
    $$("#tiles [data-go]").forEach((el) => el.addEventListener("click", () => showView(el.dataset.go)));

    const series = [{ key: "revenue", label: "Revenue", color: "var(--revenue)" }, { key: "expenses", label: "Costs", color: "var(--expense)" }];
    const monthCols = [
      { key: "month", label: "Month", render: (r) => esc(monthLabel(r.month)) },
      { key: "revenue", label: "Revenue", num: true, render: (r) => money(r.revenue) },
      { key: "expenses", label: "Costs", num: true, render: (r) => money(r.expenses) },
      { key: "profit", label: "Profit", num: true, render: (r) => `<span class="${r.profit < 0 ? "neg" : ""}">${money(r.profit)}</span>` },
      { key: "cumulative_profit", label: "Cumulative", num: true, render: (r) => money(r.cumulative_profit) },
    ];
    chartCard("monthly", (el) => groupedColumns(el, s.monthly, series, { aria: "Revenue and costs by month", extra: (r) => `Profit: ${money(r.profit)}` }), monthCols, () => s.monthly);
    chartCard("cumulative", (el) => lineChart(el, s.monthly, "cumulative_profit", "var(--profit)", { label: "Cumulative profit", aria: "Cumulative profit" }), monthCols, () => s.monthly);
    const catCols = [{ key: "name", label: "Category", render: (r) => esc(label(r.name)) }, { key: "count", label: "Docs", num: true }, { key: "amount", label: "Amount", num: true, render: (r) => money(r.amount) }];
    chartCard("expcat", (el) => hbars(el, s.expenses_by_category, "var(--expense)", { labelFn: label, aria: "Costs by category" }), catCols, () => s.expenses_by_category);

    // ---- Attention
    const att = s.attention;
    const overdueR = s.open_receivables.filter((d) => d.due_date && d.due_date < s.period.today);
    const overdueP = s.open_payables.filter((d) => d.due_date && d.due_date < s.period.today);
    const items = [];
    if (overdueR.length) items.push(`<li><b class="neg">${overdueR.length} overdue customer invoice${overdueR.length > 1 ? "s" : ""}</b> worth ${money(t.overdue_receivables, false)} — ${overdueR.slice(0, 3).map((d) => `${esc(d.counterparty || d.name)} (${money(d.outstanding, false)})`).join(", ")}${overdueR.length > 3 ? "…" : ""}</li>`);
    if (overdueP.length) items.push(`<li><b class="neg">${overdueP.length} overdue supplier bill${overdueP.length > 1 ? "s" : ""}</b> worth ${money(t.overdue_payables, false)} — ${overdueP.slice(0, 3).map((d) => `${esc(d.counterparty || d.name)} (${money(d.outstanding, false)})`).join(", ")}${overdueP.length > 3 ? "…" : ""}</li>`);
    if (att.other_currencies.length) items.push(`<li>Documents in other currencies (${att.other_currencies.join(", ")}) are added without conversion.</li>`);
    if (att.low_confidence.length) items.push(`<li>${att.low_confidence.length} document${att.low_confidence.length > 1 ? "s" : ""} read with low confidence — worth a quick check: ${att.low_confidence.slice(0, 3).map(docLink).join(", ")}${att.low_confidence.length > 3 ? "…" : ""}</li>`);
    if (att.flagged.length) items.push(`<li>${att.flagged.length} document${att.flagged.length > 1 ? "s" : ""} flagged by the reader (poor scans, mismatched totals, etc.).</li>`);
    const counts = state.config.counts || {};
    if (counts.error) items.push(`<li class="neg">${counts.error} file${counts.error > 1 ? "s" : ""} failed to process — see Documents › Error.</li>`);
    if (counts.pending) items.push(`<li>${counts.pending} file${counts.pending > 1 ? "s" : ""} still pending — run a sync.</li>`);
    $("#attention").innerHTML = items.length ? `<ul style="margin:0;padding-left:18px;line-height:1.7">${items.join("")}</ul>` : `<div class="empty">Nothing needs attention</div>`;
    bindDocLinks($("#attention"));

    // ---- Doc mix
    $("#docmix").innerHTML = tableHtml([
      { key: "doc_type", label: "Type", render: (r) => esc(label(r.doc_type)) },
      { key: "count", label: "Count", num: true },
      { key: "total", label: "Gross total", num: true, render: (r) => r.total ? money(r.total, false) : "—" },
    ], s.by_doc_type, { rowAttr: (r) => `class="row-link" data-filter-type="${esc(r.doc_type)}"` });
    $$("#docmix [data-filter-type]").forEach((tr) => tr.addEventListener("click", () => { $("#docType").value = tr.dataset.filterType; showView("documents"); loadDocs(); }));

    // ---- Money in
    $("#tiles-in").innerHTML = [
      tile({ label: "Revenue (net of VAT)", value: t.revenue, sub: `${t.revenue_docs} documents`, cls: "hero" }),
      tile({ label: "Revenue incl. VAT", value: t.revenue_gross, sub: `VAT charged ${money(t.vat_on_sales, false)}` }),
      tile({ label: "Owed to us", value: t.receivables, sub: `${t.receivables_count} unpaid invoices` }),
      tile({ label: "Overdue", value: t.overdue_receivables, sub: `${t.overdue_receivables_count} invoices past due date`, color: "var(--critical)", pct: t.receivables ? t.overdue_receivables / t.receivables * 100 : 0 }),
      tile({ label: "Pipeline", value: t.pipeline, sub: `${t.pipeline_count} quotes / proformas` }),
      tile({ label: "Payments recorded", value: t.payments_recorded, sub: `${t.payments_count} receipts / remittances` }),
    ].join("");
    const cpCols = [{ key: "name", label: "Name" }, { key: "count", label: "Docs", num: true }, { key: "amount", label: "Amount", num: true, render: (r) => money(r.amount) }];
    const goDocsFor = (name) => openCompany(name);
    chartCard("customers", (el) => hbars(el, s.top_customers, "var(--revenue)", { aria: "Revenue by customer", onClick: goDocsFor }), cpCols, () => s.top_customers);
    chartCard("revcat", (el) => hbars(el, s.revenue_by_category, "var(--revenue)", { labelFn: label, aria: "Revenue by category" }), catCols, () => s.revenue_by_category);
    const openCols = [
      { key: "counterparty", label: "Who", render: (r) => companyLink(r.counterparty) },
      { key: "document_number", label: "Number", render: (r) => esc(r.document_number || "—") },
      { key: "name", label: "File", render: (r) => `${docLink(r)} ${previewBtn(r)}` },
      { key: "issue_date", label: "Issued", render: (r) => fmtDate(r.issue_date) },
      { key: "due_date", label: "Due", render: (r) => `<span class="${r.due_date && r.due_date < s.period.today ? "neg" : ""}">${fmtDate(r.due_date)}</span>` },
      { key: "payment_status", label: "Status", render: (r) => statusChip(r.payment_status) },
      { key: "total", label: "Total", num: true, render: (r) => money(r.total) },
      { key: "outstanding", label: "Outstanding", num: true, render: (r) => `<b>${money(r.outstanding)}</b>` },
    ];
    $("#receivables").innerHTML = `<div class="table-scroll">${tableHtml(openCols, s.open_receivables, { empty: "No unpaid sales invoices in this period" })}</div>`;
    bindDocLinks($("#receivables")); bindPreviews($("#receivables"), s.open_receivables); bindCompanies($("#receivables"));
    $("#pipeline").innerHTML = `<div class="table-scroll">${tableHtml([
      { key: "counterparty", label: "Customer", render: (r) => companyLink(r.counterparty) },
      { key: "doc_type", label: "Type", render: (r) => esc(label(r.doc_type)) },
      { key: "name", label: "File", render: (r) => `${docLink(r)} ${previewBtn(r)}` },
      { key: "project", label: "Job", render: (r) => esc(r.project || "—") },
      { key: "issue_date", label: "Date", render: (r) => fmtDate(r.issue_date) },
      { key: "total", label: "Value", num: true, render: (r) => money(r.total) },
    ], s.pipeline_docs, { empty: "No quotes or proformas in this period" })}</div>`;
    bindDocLinks($("#pipeline")); bindPreviews($("#pipeline"), s.pipeline_docs); bindCompanies($("#pipeline"));

    // ---- Money out
    $("#tiles-out").innerHTML = [
      tile({ label: "Costs (net of VAT)", value: t.expenses, sub: `${t.expense_docs} documents`, cls: "hero" }),
      tile({ label: "Costs incl. VAT", value: t.expenses_gross, sub: `VAT paid ${money(t.vat_on_purchases, false)}` }),
      tile({ label: "We owe", value: t.payables, sub: `${t.payables_count} unpaid bills` }),
      tile({ label: "Overdue", value: t.overdue_payables, sub: `${t.overdue_payables_count} bills past due date`, color: "var(--critical)", pct: t.payables ? t.overdue_payables / t.payables * 100 : 0 }),
      tile({ label: "Committed (purchase orders)", value: t.commitments, sub: `${t.commitments_count} POs not yet billed` }),
    ].join("");
    chartCard("suppliers", (el) => hbars(el, s.top_suppliers, "var(--expense)", { aria: "Spend by supplier", onClick: goDocsFor }), cpCols, () => s.top_suppliers);
    chartCard("expcat2", (el) => hbars(el, s.expenses_by_category, "var(--expense)", { labelFn: label, aria: "Spend by category", limit: 20 }), catCols, () => s.expenses_by_category);
    $("#payables").innerHTML = `<div class="table-scroll">${tableHtml(openCols, s.open_payables, { empty: "No unpaid supplier bills in this period" })}</div>`;
    bindDocLinks($("#payables")); bindPreviews($("#payables"), s.open_payables); bindCompanies($("#payables"));

    // ---- Projects
    $("#projects").innerHTML = `<div class="table-scroll">${tableHtml([
      { key: "name", label: "Job / site" },
      { key: "docs", label: "Docs", num: true },
      { key: "revenue", label: "Revenue", num: true, render: (r) => money(r.revenue) },
      { key: "expenses", label: "Costs", num: true, render: (r) => money(r.expenses) },
      { key: "profit", label: "Profit", num: true, render: (r) => `<b class="${r.profit < 0 ? "neg" : ""}">${money(r.profit)}</b>` },
      { key: "margin", label: "Margin", num: true, render: (r) => r.margin == null ? "—" : `${r.margin.toFixed(0)}%` },
      { key: "pipeline", label: "Quoted", num: true, render: (r) => r.pipeline ? money(r.pipeline) : "—" },
    ], s.projects, { rowAttr: (r) => `class="row-link" data-project="${esc(r.name)}"` })}</div>
    <p class="muted small" style="margin:10px 0 0">Costs only land on a job when the supplier document names it. Materials bought without a job reference show under “(no project stated)”.</p>`;
    $$("#projects [data-project]").forEach((tr) => tr.addEventListener("click", () => { $("#docSearch").value = tr.dataset.project === "(no project stated)" ? "" : tr.dataset.project; showView("documents"); loadDocs(); }));
  }

  function bindDocLinks(root) { $$("a[data-doc]", root).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openDoc(+a.dataset.doc); })); }

  // ------------------------------------------------------------ upload (app -> Google Drive / folder)
  const uploadModal = $("#uploadModal"), uploadBackdrop = $("#uploadBackdrop");
  let pendingFiles = [];
  function closeUpload() { uploadModal.classList.remove("open"); uploadBackdrop.classList.remove("open"); }
  uploadBackdrop.addEventListener("click", closeUpload);
  $("#uploadClose").addEventListener("click", closeUpload);
  async function openUpload() {
    const targets = await api("/api/upload/targets");
    const sel = $("#uploadTarget");
    sel.innerHTML = targets.length
      ? targets.map((t) => `<option value="${t.id}" ${t.can_upload ? "" : "disabled"}>${esc(t.name)} (${kindLabel(t.kind)}${t.folder ? ": " + esc(t.folder) : ""})${t.can_upload ? "" : " — uploads not enabled (see Admin → Sources)"}</option>`).join("")
      : `<option value="">No connected source — add one in Admin</option>`;
    pendingFiles = []; $("#uploadFiles").value = ""; $("#uploadList").textContent = ""; $("#uploadResults").innerHTML = ""; $("#uploadStatus").textContent = "";
    $("#uploadGo").disabled = true;
    uploadModal.classList.add("open"); uploadBackdrop.classList.add("open");
  }
  $("#uploadBtn").addEventListener("click", openUpload);
  function setFiles(list) {
    pendingFiles = Array.from(list).slice(0, 20);
    $("#uploadList").textContent = pendingFiles.length ? pendingFiles.map((f) => `${f.name} (${(f.size / 1024).toFixed(0)} KB)`).join(" · ") : "";
    $("#uploadGo").disabled = !pendingFiles.length || !$("#uploadTarget").value;
  }
  $("#uploadFiles").addEventListener("change", (e) => setFiles(e.target.files));
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => setFiles(e.dataTransfer.files));
  $("#uploadGo").addEventListener("click", async () => {
    const fd = new FormData();
    fd.append("source_id", $("#uploadTarget").value);
    pendingFiles.forEach((f) => fd.append("files", f, f.name));
    $("#uploadGo").disabled = true; $("#uploadStatus").textContent = `Uploading and reading ${pendingFiles.length} file${pendingFiles.length > 1 ? "s" : ""}… this can take a minute.`;
    try {
      const r = await api("/api/upload", { method: "POST", body: fd });
      $("#uploadStatus").textContent = "Done.";
      $("#uploadResults").innerHTML = r.results.map((x) => `<div class="upload-row">${x.ok ? (x.status === "done" ? "✅" : x.status === "pending" ? "⏳" : "⚠️") : "❌"} <b>${esc(x.name)}</b>
        <span class="muted">${x.ok ? (x.status === "done" ? `${esc(label(x.doc_type))}${x.counterparty ? " · " + esc(x.counterparty) : ""}${x.total != null ? " · " + money(x.total) : ""}` : x.status === "pending" ? "saved; will be read when a Claude key is set" : esc(x.error || label(x.status))) : esc(x.error)}</span>
        <span class="spacer"></span>${x.ok && x.doc_id ? `<button class="eye" data-preview="${x.doc_id}">👁 Preview</button> ${x.web_link ? `<a class="eye" href="${esc(x.web_link)}" target="_blank" rel="noopener">Open original</a>` : ""}` : ""}</div>`).join("");
      bindPreviews($("#uploadResults"));
      pendingFiles = []; $("#uploadFiles").value = ""; $("#uploadList").textContent = "";
      await refresh();
    } catch (err) { $("#uploadStatus").textContent = ""; toast("Upload failed: " + err.message, 7000); $("#uploadGo").disabled = false; }
  });

  // ------------------------------------------------------------ document reader badge (Ollama / Claude, reachable or not)
  const AI_CLASS = { connected: "good", ready: "good", model_missing: "warning", unreachable: "critical", no_key: "critical", key_invalid: "critical" };
  const AI_SHORT = { connected: "connected", ready: "ready", model_missing: "model missing", unreachable: "UNREACHABLE", no_key: "no key", key_invalid: "key rejected", unknown: "checking…" };
  let lastAiState = null;
  const aiTime = (iso) => iso ? new Date(iso).toLocaleTimeString("en-IE", { hour: "2-digit", minute: "2-digit" }) : "";
  /** Header badge + a toast whenever the reader's state changes (e.g. the Ollama PC goes to sleep or comes back). */
  function renderAi(l) {
    if (!l || !l.state) return;
    const cls = AI_CLASS[l.state] || "", who = l.provider === "ollama" ? "Ollama" : l.provider === "claude" ? "Claude" : "AI";
    const b = $("#aiBadge");
    b.className = `ai-badge ${cls}`;
    b.textContent = `AI: ${who} · ${AI_SHORT[l.state] || l.state}`;
    b.title = `${l.message || ""}${l.model ? ` · model ${l.model}` : ""}${l.checked_at ? ` · checked ${aiTime(l.checked_at)}` : ""}`;
    if (lastAiState && lastAiState !== l.state) {
      toast(l.ok ? `${who} is ${AI_SHORT[l.state]} again — documents can be read.` : `${l.label}: ${l.message}`, l.ok ? 5000 : 12000);
    }
    lastAiState = l.state;
  }

  // ------------------------------------------------------------ live refresh (new documents arriving from Drive)
  let lastVersion = null;
  async function pollVersion() {
    try {
      const v = await api("/api/version");
      renderAi(v.llm);
      if (lastVersion && v.version !== lastVersion && !v.sync_running) { toast("New documents arrived — refreshing"); await refresh(); }
      lastVersion = v.version;
    } catch (_) { /* logged out or offline */ }
  }
  setInterval(pollVersion, 20000);

  // ------------------------------------------------------------ documents
  /** Fetch /api/documents with the current period + toolbar filters and render the ledger table. */
  async function loadDocs() {
    const p = new URLSearchParams();
    if (state.range.from) p.set("from", state.range.from);
    if (state.range.to) p.set("to", state.range.to);
    const q = $("#docSearch").value.trim(); if (q) p.set("q", q);
    for (const [id, key] of [["docType", "doc_type"], ["docTreatment", "treatment"], ["docPay", "payment_status"], ["docStatus", "status"]]) {
      const v = $("#" + id).value; if (v) p.set(key, v);
    }
    state.docs = await api(`/api/documents?${p}`);
    $("#docCount").textContent = `${state.docs.length} documents`;
    $("#docTable").innerHTML = tableHtml([
      { key: "issue_date", label: "Date", render: (r) => fmtDate(r.issue_date) },
      { key: "name", label: "File", render: (r) => `<div>${esc(r.name)}</div><div class="muted small">${esc(r.folder || r.source_name || "")}</div>` },
      { key: "doc_type", label: "Type", render: (r) => r.status === "done" ? esc(label(r.doc_type)) : `<span class="chip">${esc(label(r.status))}</span>` },
      { key: "treatment", label: "Counted as", render: (r) => r.treatment ? treatmentChip(r.treatment) : "" },
      { key: "counterparty", label: "Who", render: (r) => r.counterparty ? companyLink(r.counterparty) : "" },
      { key: "document_number", label: "Number", render: (r) => esc(r.document_number || "") },
      { key: "project", label: "Job", render: (r) => esc(r.project || "") },
      { key: "payment_status", label: "Paid?", render: (r) => r.payment_status && r.payment_status !== "not_applicable" ? statusChip(r.payment_status) : "" },
      { key: "net_amount", label: "Net", num: true, render: (r) => r.net_amount != null ? money(r.net_amount) : "" },
      { key: "total", label: "Total", num: true, render: (r) => r.total != null ? money(r.total) : "" },
      { key: "confidence", label: "", render: (r) => (r.overridden ? `<span class="chip" title="Edited by hand">edited</span> ` : "") + (r.flags && r.flags.length ? `<span class="flag" title="${esc(r.flags.join(", "))}">⚑</span> ` : "") + previewBtn(r) },
    ], state.docs, { rowAttr: (r) => `class="row-link" data-id="${r.id}"`, empty: "No documents match" });
    $$("#docTable tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openDoc(+tr.dataset.id)));
    bindPreviews($("#docTable"), state.docs); bindCompanies($("#docTable"));
  }
  let searchTimer;
  $("#docSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadDocs, 250); });
  ["docType", "docTreatment", "docPay", "docStatus"].forEach((id) => $("#" + id).addEventListener("change", loadDocs));
  $("#exportCsv").addEventListener("click", () => {
    const cols = ["id", "issue_date", "due_date", "doc_type", "treatment", "direction", "counterparty", "document_number", "project", "category", "currency", "subtotal", "vat_amount", "total", "amount_paid", "payment_status", "name", "folder", "web_link", "summary"];
    const csv = [cols.join(",")].concat(state.docs.map((d) => cols.map((c) => { const v = d[c]; const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; }).join(","))).join("\n");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "ledger.csv"; a.click();
  });

  // ------------------------------------------------------------ drawer
  const drawer = $("#drawer"), backdrop = $("#drawerBackdrop");
  function closeDrawer() { drawer.classList.remove("open"); backdrop.classList.remove("open"); }
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (viewer.classList.contains("open")) closeViewer(); else if (drawer.classList.contains("open")) closeDrawer(); else if (accountModal.classList.contains("open")) closeAccount(); else closeCompany(); } });

  /** Details drawer: inline preview + editable extracted fields (PATCH) + re-read (POST reprocess). */
  async function openDoc(id) {
    const d = await api(`/api/documents/${id}`);
    const m = state.meta;
    const opt = (list, cur, allowNull = true) => (allowNull ? `<option value="">—</option>` : "") + list.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label(v))}</option>`).join("");
    const field = (name, type = "text") => `<input name="${name}" type="${type}" value="${esc(d[name] ?? "")}" ${type === "number" ? 'step="0.01"' : ""}>`;
    const items = (d.line_items || []).length ? `<h3 style="font-size:13px;margin:14px 0 6px">Line items</h3>${tableHtml([
      { key: "description", label: "Description" }, { key: "quantity", label: "Qty", num: true, render: (r) => r.quantity ?? "" },
      { key: "unit_price", label: "Unit", num: true, render: (r) => r.unit_price != null ? money(r.unit_price) : "" },
      { key: "amount", label: "Amount", num: true, render: (r) => r.amount != null ? money(r.amount) : "" }], d.line_items)}` : "";
    drawer.innerHTML = `
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">
        <div><h2>${esc(d.name)}</h2><div class="muted small">${esc(d.folder || "")} · ${d.size ? (d.size / 1024).toFixed(0) + " KB" : ""} · modified ${esc((d.modified_at || "").slice(0, 10))}</div></div>
        <button class="icon-btn" id="drawerClose" aria-label="Close">✕</button>
      </div>
      ${d.status !== "done" ? `<div class="note" style="margin-top:12px"><b>${label(d.status)}</b>${d.error ? ` — ${esc(d.error)}` : ""}</div>` : ""}
      <iframe class="preview-frame" src="/api/documents/${d.id}/preview?embed=1" title="Preview of ${esc(d.name)}" loading="lazy"></iframe>
      <div class="small"><button class="link-btn" id="drawerPreview">Open large preview</button>${d.web_link ? ` · <a href="${esc(d.web_link)}" target="_blank" rel="noopener">${openWhere(d)}</a>` : ""}${d.counterparty ? ` · <button class="link-btn" data-company="${esc(d.counterparty)}">All documents from ${esc(d.counterparty)}</button>` : ""}${d.source === "demo" ? ` · <span class="muted">sample rendering of demo data</span>` : ""}</div>
      ${d.summary ? `<p style="margin:12px 0 4px">${esc(d.summary)}</p>` : ""}
      <div class="flagline">${d.treatment ? treatmentChip(d.treatment) : ""}${d.confidence != null ? `<span class="chip">confidence ${(d.confidence * 100).toFixed(0)}%</span>` : ""}${(d.flags || []).map((f) => `<span class="flag">${esc(label(f))}</span>`).join("")}${d.overridden ? `<span class="chip">edited by hand</span>` : ""}</div>
      <form id="docForm">
      <dl class="kv">
        <dt>Type</dt><dd><select name="doc_type">${opt(m.doc_types, d.doc_type, false)}</select></dd>
        <dt>Direction</dt><dd><select name="direction">${opt(m.directions, d.direction, false)}</select></dd>
        <dt>Counterparty</dt><dd>${field("counterparty")}</dd>
        <dt>Role</dt><dd><select name="counterparty_role">${opt(["customer", "supplier", "employee", "authority", "bank", "other"], d.counterparty_role)}</select></dd>
        <dt>Number</dt><dd>${field("document_number")}</dd>
        <dt>Issue date</dt><dd>${field("issue_date", "date")}</dd>
        <dt>Due date</dt><dd>${field("due_date", "date")}</dd>
        <dt>Currency</dt><dd>${field("currency")}</dd>
        <dt>Net (ex VAT)</dt><dd>${field("subtotal", "number")}</dd>
        <dt>VAT</dt><dd>${field("vat_amount", "number")}</dd>
        <dt>Total</dt><dd>${field("total", "number")}</dd>
        <dt>Amount paid</dt><dd>${field("amount_paid", "number")}</dd>
        <dt>Payment status</dt><dd><select name="payment_status">${opt(m.payment_statuses, d.payment_status, false)}</select></dd>
        <dt>Category</dt><dd><select name="category"><option value="">—</option><optgroup label="Income">${m.income_categories.map((v) => `<option value="${v}" ${v === d.category ? "selected" : ""}>${label(v)}</option>`).join("")}</optgroup><optgroup label="Costs">${m.expense_categories.map((v) => `<option value="${v}" ${v === d.category ? "selected" : ""}>${label(v)}</option>`).join("")}</optgroup></select></dd>
        <dt>Job / site</dt><dd>${field("project")}</dd>
        <dt>Summary</dt><dd><textarea name="summary">${esc(d.summary || "")}</textarea></dd>
      </dl>
      <div class="actions">
        <button class="btn primary" type="submit">Save changes</button>
        ${d.web_link ? `<a class="btn" href="${esc(d.web_link)}" target="_blank" rel="noopener">${openWhere(d)}</a>` : ""}
        ${d.source !== "demo" ? `<button class="btn" type="button" id="reprocessBtn">Re-read with Claude</button>` : ""}
      </div>
      </form>
      ${items}
      <p class="muted small" style="margin-top:16px">Saved changes are kept even when the file is re-synced. Use “Re-read with Claude” to discard them and extract again.</p>`;
    $("#drawerClose").addEventListener("click", closeDrawer);
    const dp = $("#drawerPreview"); if (dp) dp.addEventListener("click", () => openPreview(d));
    bindCompanies(drawer);
    $("#docForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target); const patch = {};
      for (const [k, v] of fd.entries()) {
        if (["subtotal", "vat_amount", "total", "amount_paid"].includes(k)) patch[k] = v === "" ? null : Number(v);
        else patch[k] = v === "" ? null : v;
      }
      try { await api(`/api/documents/${id}`, { method: "PATCH", body: JSON.stringify(patch) }); toast("Saved"); closeDrawer(); await refresh(); }
      catch (err) { toast("Save failed: " + err.message, 5000); }
    });
    const rp = $("#reprocessBtn");
    if (rp) rp.addEventListener("click", async () => {
      rp.disabled = true; rp.textContent = "Reading…";
      try { await api(`/api/documents/${id}/reprocess`, { method: "POST" }); toast("Document re-read"); await refresh(); openDoc(id); }
      catch (err) { toast("Failed: " + err.message, 6000); rp.disabled = false; rp.textContent = "Re-read with Claude"; }
    });
    drawer.classList.add("open"); backdrop.classList.add("open"); drawer.scrollTop = 0;
  }

  // ------------------------------------------------------------ sync & setup
  /** Sync tab: progress of the running sync (polled every 2 s while running), watcher status, run history. */
  function renderSync(st) {
    const pct = st.total ? Math.round((st.processed + st.skipped + st.errors) / st.total * 100) : 0;
    const c = st.counts || {};
    const runs = (st.runs || []).map((r) => `<tr><td>${esc((r.started_at || "").replace("T", " ").slice(0, 16))}</td><td>${esc(r.source_name || r.source)}</td><td class="num">${r.total}</td><td class="num">${r.processed}</td><td class="num">${r.skipped}</td><td class="num">${r.errors}</td><td>${esc(label(r.status))}</td></tr>`).join("");
    const enabled = (state.config.sources || []).filter((s) => s.enabled && s.connected).length;
    const watch = state.config.watch_interval_seconds;
    renderAi(st.llm);
    const l = st.llm || {}, lcls = AI_CLASS[l.state] || "", readBy = Object.entries(st.read_by || {});
    const reader = l.provider === "ollama" ? `Ollama · model <b>${esc(l.model)}</b> at <b>${esc(l.url)}</b> (your own server — documents never leave the network)`
      : l.provider === "claude" ? `Claude API · model <b>${esc(l.model || state.config.model)}</b>` : "checking…";
    $("#syncPanel").innerHTML = `
      <div class="card" style="margin:0 0 14px;padding:12px 14px">
        <div class="sync-status">Document reader: ${reader}</div>
        <div class="sync-status" style="margin-top:4px"><span class="ai-badge ${lcls}">${esc(l.label || "Checking…")}</span>
          <span class="muted small" style="margin-left:8px">${esc(l.message || "")}${l.checked_at ? ` · checked ${aiTime(l.checked_at)}` : ""}${l.fallback ? " · Claude fallback on" : ""}</span></div>
        <div class="small" style="margin-top:8px">Figures in this ledger were read by: ${readBy.length ? readBy.map(([m, n]) => `<span class="chip ${m.startsWith("ollama/") ? "in" : "pipe"}">${esc(m.startsWith("ollama/") ? "Ollama " + m.slice(7) : m)}: ${n} document${n === 1 ? "" : "s"}</span>`).join(" ") : "nothing read yet"}</div>
      </div>
      <div class="sync-status"><b>${esc(st.message || (st.running ? "Working…" : "Idle"))}</b>${st.current_source ? ` — ${esc(st.current_source)}` : ""}${st.current_file ? ` · ${esc(st.current_file)}` : ""}</div>
      <div class="progress"><i style="width:${pct}%"></i></div>
      <div class="sync-status">${st.running ? `${st.processed + st.skipped + st.errors} of ${st.total}` : ""} · read ${st.processed} · unchanged/skipped ${st.skipped} · errors ${st.errors}</div>
      <div style="margin:12px 0" class="small">In database: ${Object.entries(c).map(([k, v]) => `<span class="chip">${esc(label(k))}: ${v}</span>`).join(" ") || "nothing yet"}</div>
      <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" id="syncNow" ${st.running || !enabled || !state.config.llm_ready ? "disabled" : ""}>Sync new &amp; changed files</button>
        <button class="btn" id="syncForce" ${st.running || !enabled || !state.config.llm_ready ? "disabled" : ""} title="Re-read every file, even ones already processed">Full re-read</button>
      </div>
      ${state.config.llm_ready && enabled ? `<p class="muted small"><span class="live-dot"></span>${watch > 0 ? `Watching SharePoint / Drive for changes every ${watch >= 60 ? Math.round(watch / 60) + " min" : watch + " s"}${st.last_watch_at ? ` · last check ${new Date(st.last_watch_at).toLocaleTimeString("en-IE", { hour: "2-digit", minute: "2-digit" })}` : ""}. New files appear here automatically.` : "Automatic watching is off (Admin → Claude API)."}</p>` : ""}
      ${!state.config.llm_ready ? `<p class="muted small">No document reader yet — add a Claude API key in <a href="/admin#claude">Admin → Claude API</a>, or set LLM_PROVIDER=ollama in .env.</p>` : ""}
      ${!enabled ? `<p class="muted small">No connected sources yet — add a SharePoint / OneDrive folder, Google Drive or local folder in <a href="/admin#sources">Admin → Sources</a>.</p>` : ""}
      ${st.recent_errors && st.recent_errors.length ? `<h3 style="font-size:13px;margin:14px 0 6px">Recent errors</h3><ul class="small" style="padding-left:18px">${st.recent_errors.map((e) => `<li><b>${esc(e.file)}</b>: ${esc(e.error)}</li>`).join("")}</ul>` : ""}
      ${runs ? `<h3 style="font-size:13px;margin:14px 0 6px">Previous runs</h3><table><thead><tr><th>Started</th><th>Source</th><th class="num">Files</th><th class="num">Read</th><th class="num">Skipped</th><th class="num">Errors</th><th>Status</th></tr></thead><tbody>${runs}</tbody></table>` : ""}`;
    $("#syncNow").addEventListener("click", () => startSync(false));
    $("#syncForce").addEventListener("click", () => { if (confirm(state.config.llm_provider === "ollama" ? "Re-read every file with the local model? This takes a while." : "Re-read every file with Claude? This costs API usage for every document.")) startSync(true); });
    $("#syncBtn").disabled = !!st.running;
    $("#syncBtn").textContent = st.running ? `Syncing ${pct}%` : "Sync now";
  }

  function renderSetup() {
    const c = state.config;
    const srcs = c.sources || [];
    const when = (iso) => iso ? new Date(iso).toLocaleString("en-IE", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "never";
    $("#setupPanel").innerHTML = (srcs.length ? `<div class="sources-mini">${srcs.map((s) => `<div class="row">
        <span class="chip ${s.kind === "local" ? "pipe" : "in"}">${kindLabel(s.kind)}</span><b>${esc(s.name)}</b>
        <span class="muted small">${s.kind === "local" ? esc(s.local_folder) : esc(s.account_email || "not signed in")}</span>
        <span class="spacer"></span>
        <span class="muted small">${s.done_count} read${s.error_count ? ` · <span class="neg">${s.error_count} errors</span>` : ""} · last sync ${when(s.last_sync_at)}</span>
        <span class="status ${s.last_status === "ok" || s.last_status === "connected" || s.last_status === "ready" ? "paid" : s.last_status === "failed" ? "overdue" : "unknown"}">${label(s.enabled ? s.last_status : "disabled")}</span>
      </div>`).join("")}</div>` : `<div class="empty">No sources connected yet</div>`) +
      `<p class="muted small" style="margin:12px 0 0">${c.llm_provider === "ollama" ? `Reader: <b>Ollama</b> · model <b>${esc(c.llm_model)}</b>${c.llm_fallback ? " · Claude fallback on" : ""}` : `Claude key: <b class="${c.has_api_key ? "pos" : "neg"}">${c.has_api_key ? "saved" : "missing"}</b> · model <b>${esc(c.model)}</b>`} · currency <b>${esc(c.currency)}</b>${c.auto_sync_minutes ? ` · auto-sync every ${c.auto_sync_minutes} min` : ""}</p>`;
    const tr = state.meta.treatments;
    $("#rulesPanel").innerHTML = `<table><thead><tr><th>Counted as</th><th>Meaning</th><th>Which documents</th></tr></thead><tbody>
      <tr><td>${treatmentChip("revenue")}</td><td>${esc(tr.revenue)}</td><td>Sales invoices the company issued (credit notes issued are subtracted)</td></tr>
      <tr><td>${treatmentChip("expense")}</td><td>${esc(tr.expense)}</td><td>Supplier bills, purchase receipts, payslips, expense claims (credit notes received are subtracted)</td></tr>
      <tr><td>${treatmentChip("pipeline")}</td><td>${esc(tr.pipeline)}</td><td>Quotes, estimates and proforma invoices sent to customers</td></tr>
      <tr><td>${treatmentChip("commitment")}</td><td>${esc(tr.commitment)}</td><td>Purchase orders raised, supplier quotes / proformas received</td></tr>
      <tr><td>${treatmentChip("payment")}</td><td>${esc(tr.payment)}</td><td>Receipts issued to customers, remittance advices</td></tr>
      <tr><td>${treatmentChip("none")}</td><td>${esc(tr.none)}</td><td>Bank statements, contracts, insurance schedules, certificates, tax forms, delivery notes</td></tr>
    </tbody></table>
    <p class="muted small" style="margin:10px 0 0">Figures are on an invoice basis (by document date), net of VAT where the document shows VAT. Open any document to correct a reading — your correction wins over the automatic one.</p>`;
  }

  async function startSync(force) {
    try {
      const r = await api(`/api/sync?force=${force ? 1 : 0}`, { method: "POST" });
      if (!r.started) toast("A sync is already running");
      showView("sync"); pollSync();
    } catch (err) { toast(err.message, 6000); showView("sync"); }
  }
  async function pollSync() {
    clearTimeout(state.syncTimer);
    const st = await api("/api/sync/status");
    renderSync(st);
    if (st.running) { state.wasSyncing = true; state.syncTimer = setTimeout(pollSync, 2000); }
    else if (state.wasSyncing) { state.wasSyncing = false; await refresh(); renderSync(await api("/api/sync/status")); }
  }
  $("#syncBtn").addEventListener("click", () => startSync(false));

  // ------------------------------------------------------------ ask
  async function askQuestion() {
    const q = $("#askInput").value.trim(); if (!q) return;
    const btn = $("#askBtn"); btn.disabled = true; btn.textContent = "Thinking…";
    $("#answer").innerHTML = `<span class="muted">Reading the ledger…</span>`;
    try {
      const r = await api("/api/ask", { method: "POST", body: JSON.stringify({ question: q, date_from: state.range.from, date_to: state.range.to }) });
      $("#answer").textContent = r.answer;
    } catch (err) { $("#answer").innerHTML = `<span class="neg">${esc(err.message)}</span>`; }
    btn.disabled = false; btn.textContent = "Ask";
  }
  $("#askBtn").addEventListener("click", askQuestion);
  $("#askInput").addEventListener("keydown", (e) => { if (e.key === "Enter") askQuestion(); });
  $$(".suggestions button").forEach((b) => b.addEventListener("click", () => { $("#askInput").value = b.textContent; askQuestion(); }));

  // ------------------------------------------------------------ navigation, period, theme
  const VIEWS = ["overview", "money-in", "money-out", "projects", "companies", "documents", "ask", "sync"];
  function showView(name) {
    if (!VIEWS.includes(name)) name = "overview";
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    if (name === "documents") loadDocs();
    if (name === "companies") loadCompanies();
    if (name === "sync") pollSync();
    window.scrollTo({ top: 0 });
  }
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));

  function applyPreset(p) {
    const now = new Date(); let from = null, to = null;
    if (p === "ytd") { from = `${now.getFullYear()}-01-01`; to = iso(now); }
    else if (p === "last12") { const d = new Date(now.getFullYear(), now.getMonth() - 11, 1); from = iso(d); to = iso(now); }
    else if (p === "lastyear") { from = `${now.getFullYear() - 1}-01-01`; to = `${now.getFullYear() - 1}-12-31`; }
    else if (p === "q") { const qm = Math.floor(now.getMonth() / 3) * 3; from = iso(new Date(now.getFullYear(), qm, 1)); to = iso(now); }
    state.range = { from, to, preset: p };
    $("#fromDate").value = from || ""; $("#toDate").value = to || "";
    $$("#presets button").forEach((b) => b.classList.toggle("active", b.dataset.range === p));
  }
  $$("#presets button").forEach((b) => b.addEventListener("click", () => { applyPreset(b.dataset.range); refresh(); }));
  ["fromDate", "toDate"].forEach((id) => $("#" + id).addEventListener("change", () => {
    state.range = { from: $("#fromDate").value || null, to: $("#toDate").value || null, preset: null };
    $$("#presets button").forEach((b) => b.classList.remove("active")); refresh();
  }));

  function applyTheme(t) {
    if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
    try { if (t) localStorage.setItem("theme", t); else localStorage.removeItem("theme"); } catch (_) { /* ignore */ }
  }
  $("#themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const osDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const isDark = cur ? cur === "dark" : osDark;
    applyTheme(isDark ? "light" : "dark");
  });
  try { const t = localStorage.getItem("theme"); if (t) applyTheme(t); } catch (_) { /* ignore */ }

  // ------------------------------------------------------------ boot
  async function refresh() {
    state.config = await api("/api/config");
    const p = new URLSearchParams();
    if (state.range.from) p.set("from", state.range.from);
    if (state.range.to) p.set("to", state.range.to);
    state.summary = await api(`/api/summary?${p}`);
    renderAll();
    renderSetup();
    if ($("#view-documents").classList.contains("active")) loadDocs();
  }

  async function init() {
    const me = await api("/api/auth/me");
    state.emailRequired = !!me.email_required;
    if (me.needs_setup) { location.href = "/admin"; return; }
    if (!me.role) { showLogin(); return; }
    hideLogin();
    renderUser(me.user);
    state.config = await api("/api/config");
    lastVersion = state.config.version;
    state.meta = await api("/api/meta");
    setupFormats(state.config.currency || "EUR");
    $("#companyName").textContent = state.config.company;
    renderAi(state.config.llm_status);
    const srcs = state.config.sources || [];
    const nDrive = srcs.filter((s) => s.kind === "drive").length, nMs = srcs.filter((s) => s.kind === "m365").length, nLocal = srcs.length - nDrive - nMs;
    $("#sourceLine").textContent = srcs.length
      ? [nMs ? `${nMs} SharePoint/OneDrive` : "", nDrive ? `${nDrive} Google Drive${nDrive > 1 ? "s" : ""}` : "", nLocal ? `${nLocal} folder${nLocal > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ")
      : (state.config.demo_loaded ? "Demo data" : "No sources connected");
    const fill = (id, list, allowBlank = true) => { const sel = $("#" + id); const first = sel.options[0].outerHTML; sel.innerHTML = first + list.map((v) => `<option value="${esc(v)}">${esc(label(v))}</option>`).join(""); };
    fill("docType", state.meta.doc_types); fill("docTreatment", Object.keys(state.meta.treatments)); fill("docPay", state.meta.payment_statuses);
    if (!state.range.from && !state.range.to && state.range.preset) applyPreset(state.range.preset);
    await refresh();
    renderSetup();
    const total = Object.values(state.config.counts || {}).reduce((a, b) => a + b, 0);
    const wanted = location.hash.slice(1);
    if (wanted && VIEWS.includes(wanted)) showView(wanted);
    else if (!total) showView("sync");
    pollSync();
  }
  init().catch((err) => { console.error(err); if (!/sign in/i.test(err.message)) toast("Failed to load: " + err.message, 8000); });
})();
