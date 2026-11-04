/* Ledger Admin — settings, sources, syncing, security
 *
 * Sections: sources (cards + add forms + Google connect), Claude API, Company, Google setup, Sync log,
 * Data (demo/backup/clear), Users & security (user accounts + the built-in admin password). All calls go to /api/admin/* in
 * app/main.py and require the admin role; a 401/403 brings the login box back.
 */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const label = (s) => s ? String(s).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "—";
  const when = (iso) => iso ? new Date(iso).toLocaleString("en-IE", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "never";

  const state = { overview: null, timer: null, wasSyncing: false, emailRequired: false };

  function toast(msg, ms = 3000) {
    const t = $("#toast"); t.textContent = msg; t.style.display = "block";
    clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if ((res.status === 401 || res.status === 403) && !/\/api\/admin\/(login|setup)$/.test(path)) { showAuth(false); throw new Error("Please log in as admin"); }
    if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) { /* ignore */ } throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
    return res.json();
  }

  // ------------------------------------------------------------ auth
  function showAuth(needsSetup) {
    $("#adminLayout").style.display = "none"; $("#authWrap").style.display = "grid"; $("#logoutBtn").style.display = "none";
    $("#authTitle").textContent = needsSetup ? "Create the admin password" : "Admin login";
    $("#authHint").textContent = needsSetup ? "This is the first time the admin area is opened. Choose an admin password (at least 10 characters)."
      : state.emailRequired ? "Sign in with the admin email and password, or with an account that has the admin role."
      : "Sign in with an admin account, or leave the email empty and use the admin password.";
    $("#authEmail").style.display = needsSetup ? "none" : "block";
    $("#authEmail").placeholder = state.emailRequired ? "Email" : "Email (leave empty for the admin password)";
    $("#authEmail").required = state.emailRequired && !needsSetup;
    $("#authPassword2").style.display = needsSetup ? "block" : "none";
    $("#authSubmit").textContent = needsSetup ? "Create password" : "Log in";
    $("#authForm").dataset.mode = needsSetup ? "setup" : "login";
    (needsSetup ? $("#authPassword") : $("#authEmail")).focus();
  }
  $("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault(); $("#authError").textContent = "";
    const pw = $("#authPassword").value, mode = e.target.dataset.mode;
    if (mode === "setup" && pw !== $("#authPassword2").value) { $("#authError").textContent = "Passwords do not match"; return; }
    const body = mode === "setup" ? { password: pw } : { email: $("#authEmail").value.trim(), password: pw };
    try { await api(`/api/admin/${mode}`, { method: "POST", body: JSON.stringify(body) }); $("#authPassword").value = ""; $("#authPassword2").value = ""; await boot(); }
    catch (err) { $("#authError").textContent = err.message; }
  });
  $("#logoutBtn").addEventListener("click", async () => { await api("/api/admin/logout", { method: "POST" }); showAuth(false); });

  // ------------------------------------------------------------ overview
  async function load() {
    state.overview = await api("/api/admin/overview");
    renderAll();
  }
  function renderAll() {
    const o = state.overview;
    renderStatus(o.sync, o.settings);
    renderSources(o.sources, o.settings);
    renderClaude(o.settings, o.models);
    renderCompany(o.settings);
    renderGoogle(o.settings);
    renderMicrosoft(o.settings);
    renderSecurity(o.settings, o.users || []);
    renderLog(o.sync);
    $("#demoLoad").style.display = o.demo_loaded ? "none" : ""; $("#demoClear").style.display = o.demo_loaded ? "" : "none";
    renderPreviews(o.previews || { enabled: false, files: 0, bytes: 0 });
    $("#redirectUri").textContent = `${location.origin}/admin/oauth/callback`;
    $("#msRedirectUri").textContent = `${location.origin}/admin/oauth/ms-callback`;
  }

  /** Human label + chip colour per source kind. */
  const KIND = { drive: ["Google Drive", "in"], m365: ["Microsoft 365", "in"], local: ["Local folder", "pipe"] };
  const kindLabel = (k) => (KIND[k] || [k])[0];
  const isCloud = (src) => src.kind === "drive" || src.kind === "m365";
  /** Where a source reads from, as shown under its name. */
  function sourceWhere(src) {
    if (src.kind === "local") return `<code>${esc(src.local_folder)}</code>`;
    const who = src.account_email ? `Signed in as <b>${esc(src.account_email)}</b>` : "Not signed in yet";
    if (src.kind === "drive") return who + (src.drive_folder_id ? ` · folder <code>${esc(src.drive_folder_id)}</code>` : " · whole Drive");
    let loc = "whole OneDrive";
    try { const l = src.ms_location ? JSON.parse(src.ms_location) : null; if (l && l.label) loc = l.label; } catch (_) { /* ignore */ }
    if (!src.ms_location && src.ms_link) loc = src.ms_link === "shared" ? "everything shared with the account" : "folder link (resolved at sign-in)";
    return `${who} · ${esc(loc)}${src.ms_tenant ? ` · organisation <code>${esc(src.ms_tenant)}</code>` : ""}`;
  }

  function renderStatus(st, s) {
    const total = st.total || 0, done = (st.processed || 0) + (st.skipped || 0) + (st.errors || 0);
    const pct = total ? Math.round(done / total * 100) : 0;
    const warn = [];
    if (!s.has_api_key) warn.push(`<a href="#claude" data-go="claude">Add a Claude API key</a> before syncing.`);
    if (!s.has_google_client && !s.has_ms_client) warn.push(`Set up <a href="#microsoft" data-go="microsoft">Microsoft 365</a> (SharePoint / OneDrive) or <a href="#google" data-go="google">Google Drive</a> to connect cloud folders.`);
    if (state.overview.production && !s.secrets_encrypted) warn.push(`<b>APP_SECRET_KEY is not set</b>: the API key and Google tokens are stored unencrypted. See docs/DEPLOYMENT.md.`);
    if (!state.overview.production) warn.push(`Running in local mode (no PUBLIC_BASE_URL). Fine on this PC; set it before exposing the app on a server.`);
    const c = st.counts || {};
    $("#statusStrip").innerHTML = `
      <div class="strip-row">
        <div><b>${st.running ? esc(st.message || "Syncing…") : "Sync idle"}</b>${st.running && st.current_file ? ` <span class="muted">— ${esc(st.current_file)}</span>` : ""}${!st.running && st.last_finished_at ? ` <span class="muted">— last finished ${when(st.last_finished_at)}</span>` : ""}</div>
        <div class="muted small">${Object.entries(c).map(([k, v]) => `${label(k)}: ${v}`).join(" · ") || "no documents yet"}</div>
        <div class="actions"><button class="btn primary sm" id="syncAllBtn" ${st.running || !s.has_api_key ? "disabled" : ""}>Sync all sources</button> <button class="btn sm" id="watchNowBtn" ${st.running || !s.has_api_key ? "disabled" : ""} title="Ask SharePoint / Google Drive for changes right now">Check for changes</button>${st.running ? ` <button class="btn sm" id="stopSyncBtn" ${st.stop_requested ? "disabled" : ""} title="Finish the files being read now, then stop. Nothing already read is lost.">${st.stop_requested ? "Stopping…" : "Stop"}</button>` : ""}</div>
      </div>
      ${st.running ? `<div class="progress"><i style="width:${pct}%"></i></div><div class="muted small">${done} of ${total} files · read ${st.processed} · unchanged/skipped ${st.skipped} · errors ${st.errors}${st.queue && st.queue.length ? ` · waiting: ${st.queue.map(esc).join(", ")}` : ""}</div>` : ""}
      ${warn.length ? `<ul class="warnlist">${warn.map((w) => `<li>${w}</li>`).join("")}</ul>` : ""}`;
    $("#syncAllBtn").addEventListener("click", () => run(api("/api/admin/sync-all", { method: "POST" }), "Sync started"));
    $("#watchNowBtn").addEventListener("click", () => run(api("/api/admin/watch-now", { method: "POST" }).then((r) => toast(r.changed_sources.length ? `Changes found in ${r.changed_sources.length} source(s), sync started` : "No changes since the last check")), null));
    const stopBtn = $("#stopSyncBtn");
    if (stopBtn) stopBtn.addEventListener("click", () => run(api("/api/admin/sync-stop", { method: "POST" }), "Stopping after the files being read now"));
    $$("#statusStrip [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection(a.dataset.go); }));
    $("#adminSub").textContent = st.running ? `Syncing ${pct}%` : whoAmI();
  }

  function statusBadge(src) {
    const map = { ok: ["good", "OK"], errors: ["serious", "Finished with errors"], failed: ["critical", "Failed"], syncing: ["accent", "Syncing"], stopped: ["warning", "Stopped"],
      connected: ["good", "Connected"], not_connected: ["warning", "Not connected"], ready: ["good", "Ready"] };
    const [cls, text] = map[src.last_status] || ["muted", label(src.last_status || "new")];
    return `<span class="badge ${cls}">${text}</span>`;
  }

  function renderSources(list, s) {
    // "Add" buttons stay disabled until the matching app registration exists (also when there are no sources yet).
    $("#addDriveBtn").disabled = !s.has_google_client;
    $("#addDriveNote").innerHTML = s.has_google_client ? "" : `Upload the Google OAuth client under <a href="#google" data-go="google">Google setup</a> first.`;
    $$("#addDriveNote [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("google"); }));
    $("#addMsBtn").disabled = !s.has_ms_client;
    $("#addMsNote").innerHTML = s.has_ms_client ? "" : `Fill in the app registration under <a href="#microsoft" data-go="microsoft">Microsoft setup</a> first.`;
    $$("#addMsNote [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("microsoft"); }));
    if (!list.length) { $("#sourceList").innerHTML = `<div class="card empty">No sources yet. Add a SharePoint / OneDrive folder, a Google Drive or a local folder below.</div>`; return; }
    $("#sourceList").innerHTML = list.map((src) => `
      <div class="card source ${src.enabled ? "" : "disabled"}" data-id="${src.id}">
        <div class="source-head">
          <div>
            <div class="source-title"><span class="chip ${(KIND[src.kind] || ["", "pipe"])[1]}">${kindLabel(src.kind)}</span> <b>${esc(src.name)}</b> ${statusBadge(src)} ${src.enabled ? "" : `<span class="badge muted">Disabled</span>`}</div>
            <div class="muted small">${sourceWhere(src)}</div>
            <div class="muted small">${src.doc_count} files known · ${src.done_count} read${src.error_count ? ` · <span class="neg">${src.error_count} errors</span>` : ""} · last sync ${when(src.last_sync_at)}${isCloud(src) && src.connected ? ` · watch ${when(src.last_change_check)}` : ""}${src.last_error ? ` · <span class="neg">${esc(src.last_error)}</span>` : ""}</div>
            ${src.connected ? `<div class="small">${src.can_upload ? `<span class="ok">Uploads from the dashboard enabled</span>` : `<span class="missing">Uploads disabled — ${src.kind === "m365" && src.ms_link === "shared" ? "add a source for a specific folder to upload into it" : src.kind === "m365" && !src.ms_upload ? "turn on uploads (Edit) and reconnect" : "reconnect to grant the “create files” permission"}</span>`}</div>` : ""}
          </div>
          <label class="switch" title="Include in syncs"><input type="checkbox" data-act="toggle" ${src.enabled ? "checked" : ""}><span></span></label>
        </div>
        <div class="actions">
          ${isCloud(src) ? `<a class="btn ${src.connected ? "" : "primary"} sm" href="/admin/sources/${src.id}/connect">${src.connected ? "Reconnect / change account" : src.kind === "m365" ? "Connect with Microsoft" : "Connect with Google"}</a>` : ""}
          <button class="btn sm ${isCloud(src) && src.connected ? "primary" : ""}" data-act="sync" ${!s.has_api_key || !src.connected ? "disabled" : ""}>Sync now</button>
          <button class="btn sm" data-act="force" ${!s.has_api_key || !src.connected ? "disabled" : ""} title="Re-read every file, even ones already processed">Full re-read</button>
          <button class="btn sm" data-act="edit">Edit</button>
          ${isCloud(src) && src.connected ? `<button class="btn sm" data-act="disconnect">Disconnect</button>` : ""}
          <button class="btn sm danger-btn" data-act="remove">Remove</button>
          <a class="btn sm" href="/#documents" title="Open the owner dashboard filtered to this source">View documents</a>
        </div>
      </div>`).join("");
    $$("#sourceList .source").forEach((card) => {
      const id = card.dataset.id, src = list.find((x) => String(x.id) === id);
      card.querySelector('[data-act="toggle"]').addEventListener("change", (e) => run(api(`/api/admin/sources/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: e.target.checked }) }), e.target.checked ? "Enabled" : "Disabled"));
      card.querySelector('[data-act="sync"]').addEventListener("click", () => run(api(`/api/admin/sources/${id}/sync`, { method: "POST" }), "Sync started"));
      card.querySelector('[data-act="force"]').addEventListener("click", () => { if (confirm(`Re-read every file in "${src.name}"? This costs API usage for each document.`)) run(api(`/api/admin/sources/${id}/sync?force=1`, { method: "POST" }), "Full re-read started"); });
      card.querySelector('[data-act="remove"]').addEventListener("click", () => { if (confirm(`Remove "${src.name}" and delete its ${src.doc_count} documents from the ledger?`)) run(api(`/api/admin/sources/${id}`, { method: "DELETE" }), "Source removed"); });
      const dis = card.querySelector('[data-act="disconnect"]'); if (dis) dis.addEventListener("click", () => run(api(`/api/admin/sources/${id}/disconnect`, { method: "POST" }), "Disconnected"));
      card.querySelector('[data-act="edit"]').addEventListener("click", () => {
        const name = prompt("Name", src.name); if (name === null) return;
        const patch = { name };
        if (src.kind === "drive") { const f = prompt("Only this folder (link or id, blank = whole Drive)", src.drive_folder_id || ""); if (f === null) return; patch.drive_folder_id = f; }
        else if (src.kind === "m365") {
          const f = prompt("Folder link (blank = whole OneDrive, 'shared' = everything shared with the account)", src.ms_link || ""); if (f === null) return; patch.ms_link = f;
          const t = prompt("Organisation (tenant) when signing in as a guest, blank for your own", src.ms_tenant || ""); if (t === null) return; patch.ms_tenant = t;
          patch.ms_upload = confirm("Allow uploads from the dashboard into this folder? (OK = yes, Cancel = read-only; takes effect at the next Connect)");
        } else { const f = prompt("Folder path", src.local_folder || ""); if (f === null) return; patch.local_folder = f; }
        run(api(`/api/admin/sources/${id}`, { method: "PATCH", body: JSON.stringify(patch) }), "Saved");
      });
    });
  }

  $("#addMsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const src = await api("/api/admin/sources", { method: "POST", body: JSON.stringify({ kind: "m365", name: fd.get("name"), ms_link: fd.get("ms_link"), ms_tenant: fd.get("ms_tenant"), ms_upload: fd.get("ms_upload") === "on" }) });
      location.href = `/admin/sources/${src.id}/connect`;
    } catch (err) { toast(err.message, 6000); }
  });

  $("#addDriveForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const src = await api("/api/admin/sources", { method: "POST", body: JSON.stringify({ kind: "drive", name: fd.get("name"), drive_folder_id: fd.get("drive_folder_id") }) });
      location.href = `/admin/sources/${src.id}/connect`;
    } catch (err) { toast(err.message, 6000); }
  });
  $("#addLocalForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    await run(api("/api/admin/sources", { method: "POST", body: JSON.stringify({ kind: "local", name: fd.get("name"), local_folder: fd.get("local_folder") }) }), "Folder added");
    e.target.reset();
  });

  // ------------------------------------------------------------ claude / company / google
  function fillForm(form, s) { $$("[name]", form).forEach((el) => { if (el.type === "password" || el.type === "file") return; if (s[el.name] !== undefined) el.value = s[el.name]; }); }
  function renderClaude(s, models) {
    const sel = $('#claudeForm [name="extract_model"]'), ask = $('#claudeForm [name="ask_model"]');
    sel.innerHTML = models.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
    ask.innerHTML = `<option value="">Same as reading model</option>` + models.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
    fillForm($("#claudeForm"), s);
    $('#claudeForm [name="anthropic_api_key"]').placeholder = s.has_api_key ? `Saved: ${s.anthropic_api_key_masked} — paste a new key to replace` : "sk-ant-…";
    $("#keyStatus").innerHTML = s.has_api_key ? `<span class="ok">Key saved (${esc(s.anthropic_api_key_masked)})</span>` : `<span class="missing">No key saved — syncing and Ask are disabled</span>`;
  }
  $("#showKeyBtn").addEventListener("click", () => { const i = $('#claudeForm [name="anthropic_api_key"]'); i.type = i.type === "password" ? "text" : "password"; $("#showKeyBtn").textContent = i.type === "password" ? "Show" : "Hide"; });
  $("#testKeyBtn").addEventListener("click", async () => {
    const btn = $("#testKeyBtn"); btn.disabled = true; btn.textContent = "Testing…";
    try {
      const r = await api("/api/admin/test-key", { method: "POST", body: JSON.stringify({ key: $('#claudeForm [name="anthropic_api_key"]').value, model: $('#claudeForm [name="extract_model"]').value }) });
      $("#keyStatus").innerHTML = `<span class="${r.ok ? "ok" : "missing"}">${esc(r.message)}</span>`;
    } catch (err) { $("#keyStatus").innerHTML = `<span class="missing">${esc(err.message)}</span>`; }
    btn.disabled = false; btn.textContent = "Test key";
  });
  $("#claudeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target); const body = {};
    for (const [k, v] of fd.entries()) { if (k === "anthropic_api_key" && !v.trim()) continue; body[k] = ["concurrency", "auto_sync_minutes", "watch_interval_seconds"].includes(k) ? parseInt(v, 10) : k === "max_file_mb" ? parseFloat(v) : v; }
    try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); $('#claudeForm [name="anthropic_api_key"]').value = ""; $("#claudeSaved").textContent = "Saved"; await load(); setTimeout(() => ($("#claudeSaved").textContent = ""), 2500); }
    catch (err) { toast(err.message, 6000); }
  });
  function renderCompany(s) { fillForm($("#companyForm"), s); }
  $("#companyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); $("#companySaved").textContent = "Saved"; await load(); setTimeout(() => ($("#companySaved").textContent = ""), 2500); }
    catch (err) { toast(err.message, 6000); }
  });
  function renderGoogle(s) {
    $("#googleStatus").innerHTML = s.has_google_client
      ? `<div class="ok">Google OAuth client saved (${esc(s.google_client_type)} app, client id <code>${esc(s.google_client_id)}</code>). You can add Google Drives under <a href="#sources" data-go="sources">Sources</a>.</div>`
      : `<div class="missing">No Google OAuth client yet. Follow the steps below once; after that every Drive only needs a Google sign-in.</div>`;
    $$("#googleStatus [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("sources"); }));
  }
  $("#googleFile").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $('#googleForm [name="json_text"]').value = await f.text();
  });
  $("#googleForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const txt = $('#googleForm [name="json_text"]').value.trim();
    if (!txt) { toast("Choose the JSON file or paste its contents first"); return; }
    await run(api("/api/admin/google-client", { method: "POST", body: JSON.stringify({ json_text: txt }) }), "Google client saved");
    $('#googleForm [name="json_text"]').value = ""; $("#googleFile").value = "";
  });
  $("#googleRemove").addEventListener("click", () => { if (confirm("Remove the Google OAuth client? Connected Drives keep working until their tokens expire.")) run(api("/api/admin/google-client", { method: "DELETE" }), "Removed"); });

  /** Microsoft setup: shows the saved registration (never the secret) and fills the form. */
  function renderMicrosoft(s) {
    $("#msStatus").innerHTML = s.has_ms_client
      ? `<div class="ok">Microsoft app registration saved (client id <code>${esc(s.ms_client_id)}</code>, tenant <code>${esc(s.ms_tenant)}</code>${s.ms_has_secret ? ", secret saved" : ", <b>no client secret</b> — fine for a public client, otherwise add one"}). You can add SharePoint / OneDrive folders under <a href="#sources" data-go="sources">Sources</a>.</div>`
      : `<div class="missing">No Microsoft app registration yet. Follow the steps below once; after that every SharePoint or OneDrive folder only needs a Microsoft sign-in.</div>`;
    $$("#msStatus [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("sources"); }));
    if (s.has_ms_client) { $('#msForm [name="client_id"]').value = s.ms_client_id || ""; $('#msForm [name="tenant"]').value = s.ms_tenant || ""; }
  }
  $("#msForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    await run(api("/api/admin/ms-client", { method: "POST", body: JSON.stringify({ client_id: fd.get("client_id"), client_secret: fd.get("client_secret"), tenant: fd.get("tenant") }) }), "Microsoft registration saved");
    $('#msForm [name="client_secret"]').value = "";
  });
  $("#msRemove").addEventListener("click", () => { if (confirm("Remove the Microsoft app registration? Connected SharePoint/OneDrive sources stop working when their tokens expire.")) run(api("/api/admin/ms-client", { method: "DELETE" }), "Removed"); });

  // ------------------------------------------------------------ log / data / security
  function renderLog(st) {
    const runs = st.runs || [];
    $("#runsTable").innerHTML = runs.length ? `<table><thead><tr><th>Started</th><th>Source</th><th class="num">Files</th><th class="num">Read</th><th class="num">Skipped</th><th class="num">Errors</th><th>Result</th></tr></thead><tbody>${runs.map((r) => `<tr><td>${when(r.started_at)}</td><td>${esc(r.source_name || r.source)}${r.force ? ' <span class="chip">full</span>' : ""}</td><td class="num">${r.total}</td><td class="num">${r.processed}</td><td class="num">${r.skipped}</td><td class="num">${r.errors}</td><td>${esc(label(r.status))}${r.message ? ` <span class="neg small">${esc(r.message)}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No syncs yet</div>`;
    const errs = st.recent_errors || [];
    $("#errorsTable").innerHTML = `<h3>Recent file errors</h3>` + (errs.length ? `<ul class="small">${errs.map((e) => `<li><b>${esc(e.file)}</b>: ${esc(e.error)}</li>`).join("")}</ul>` : `<div class="muted small">None in the current / last run. Files with a stored error are listed on the owner dashboard under Documents → Error.</div>`);
  }
  /** Data → document files kept on this server (off by default: nothing but the figures is stored). */
  function renderPreviews(p) {
    const mb = (p.bytes / 1048576).toFixed(1);
    $("#previewsStatus").textContent = p.files ? `${p.files} file cop${p.files === 1 ? "y" : "ies"} stored (${mb} MB).` : "No document files are stored on this server.";
    $("#cachePreviews").checked = !!p.enabled;
    $("#clearPreviews").style.display = p.files ? "" : "none";
  }
  $("#cachePreviews").addEventListener("change", (e) => run(api("/api/admin/settings", { method: "PUT", body: JSON.stringify({ cache_previews: e.target.checked }) }), e.target.checked ? "Copies will be kept from now on" : "Copies disabled and deleted"));
  $("#clearPreviews").addEventListener("click", () => run(api("/api/admin/previews/clear", { method: "POST" }), "Stored copies deleted"));
  $("#demoLoad").addEventListener("click", () => run(api("/api/admin/demo/load", { method: "POST" }), "Demo data loaded"));
  $("#demoClear").addEventListener("click", () => run(api("/api/admin/demo/clear", { method: "POST" }), "Demo data removed"));
  $("#clearDocs").addEventListener("click", () => { if (confirm("Delete ALL extracted documents and sync history? This cannot be undone.")) run(api("/api/admin/clear-documents", { method: "POST" }), "All documents deleted"); });
  function renderSecurity(s, users) {
    const active = users.filter((u) => u.active).length;
    $("#securityStatus").innerHTML = `<ul class="setup-steps" style="list-style:none;padding:0">
      <li class="${s.has_admin_password ? "ok" : "missing"}">Admin password set${s.admin_from_env ? ` — from <code>ADMIN_PASSWORD</code> in the server environment${s.admin_email ? `, sign-in name <code>${esc(s.admin_email)}</code>` : ""}` : ""}</li>
      <li class="${active ? "ok" : "missing"}">${active ? `${active} user account${active === 1 ? "" : "s"} can sign in` : "No user accounts yet — everyone would need the admin password"}</li>
      <li class="${s.secrets_encrypted ? "ok" : "missing"}">Secrets encrypted at rest (<code>APP_SECRET_KEY</code>) ${s.secrets_encrypted ? "" : "— set it in the server environment"}</li>
      <li class="${s.public_base_url ? "ok" : "missing"}">Public URL pinned (<code>PUBLIC_BASE_URL</code>) ${s.public_base_url ? esc(s.public_base_url) : "— required when deployed on a server"}</li>
    </ul>`;
    // the built-in password cannot be changed here while the environment defines it
    $$("#passwordForm input, #passwordForm button").forEach((el) => { el.disabled = !!s.admin_from_env; });
    $("#passwordEnvNote").style.display = s.admin_from_env ? "" : "none";
    renderUsers(users);
  }
  /** User accounts table with per-row actions (reset password, role, enable/disable, remove). */
  function renderUsers(users) {
    const me = state.me || {};
    if (!users.length) { $("#usersTable").innerHTML = `<div class="empty">No accounts yet. Add the owner and anyone else who should see the ledger.</div>`; return; }
    $("#usersTable").innerHTML = `<table class="users-table"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead><tbody>${users.map((u) => `
      <tr data-id="${u.id}" class="${u.active ? "" : "muted"}">
        <td>${esc(u.name || "—")}${me.uid === u.id ? ' <span class="chip">you</span>' : ""}</td>
        <td>${esc(u.email)}</td>
        <td><span class="badge ${u.role === "admin" ? "accent" : "muted"}">${u.role === "admin" ? "Admin" : "User"}</span></td>
        <td>${u.active ? "Active" : "Disabled"}</td>
        <td>${when(u.last_login_at)}</td>
        <td><div class="actions">
          <button class="btn sm" data-act="rename">Rename</button>
          <button class="btn sm" data-act="reset">Reset password</button>
          <button class="btn sm" data-act="role">${u.role === "admin" ? "Make user" : "Make admin"}</button>
          <button class="btn sm" data-act="toggle">${u.active ? "Disable" : "Enable"}</button>
          <button class="btn sm" data-act="delete">Remove</button>
        </div></td>
      </tr>`).join("")}</tbody></table>`;
    $$("#usersTable button[data-act]").forEach((b) => b.addEventListener("click", () => userAction(b.dataset.act, users.find((u) => u.id === +b.closest("tr").dataset.id))));
  }
  async function userAction(act, u) {
    const url = `/api/admin/users/${u.id}`;
    if (act === "rename") {
      const name = prompt(`Name for ${u.email}:`, u.name || ""); if (name === null) return;
      return run(api(url, { method: "PATCH", body: JSON.stringify({ name }) }), "Renamed");
    }
    if (act === "reset") {
      const pw = prompt(`New password for ${u.email} (10+ characters). They will be signed out everywhere.`); if (!pw) return;
      return run(api(`${url}/password`, { method: "POST", body: JSON.stringify({ password: pw }) }), "Password reset");
    }
    if (act === "role") {
      const role = u.role === "admin" ? "user" : "admin";
      if (!confirm(role === "admin" ? `Give ${u.email} access to this admin area?` : `Limit ${u.email} to the dashboard?`)) return;
      return run(api(url, { method: "PATCH", body: JSON.stringify({ role }) }), "Role changed");
    }
    if (act === "toggle") {
      if (u.active && !confirm(`Disable ${u.email}? They are signed out immediately and cannot sign in until re-enabled.`)) return;
      return run(api(url, { method: "PATCH", body: JSON.stringify({ active: !u.active }) }), u.active ? "Account disabled" : "Account enabled");
    }
    if (act === "delete") {
      if (!confirm(`Remove the account ${u.email}? This cannot be undone.`)) return;
      return run(api(url, { method: "DELETE" }), "Account removed");
    }
  }
  $("#addUserForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    try { await api("/api/admin/users", { method: "POST", body: JSON.stringify(body) }); toast(`Account created for ${body.email}`); e.target.reset(); await load(); }
    catch (err) { toast(err.message, 7000); }
  });
  $("#passwordForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    try { await api("/api/admin/password", { method: "POST", body: JSON.stringify(body) }); toast("Password changed"); e.target.reset(); }
    catch (err) { toast(err.message, 6000); }
  });

  // ------------------------------------------------------------ plumbing
  /** Await an API call, toast the outcome, reload the overview. */
  async function run(promise, okMsg) {
    try { await promise; if (okMsg) toast(okMsg); await load(); poll(); }
    catch (err) { toast(err.message, 7000); }
  }
  async function poll() {
    clearTimeout(state.timer);
    try {
      const st = await api("/api/sync/status");
      if (state.overview) { state.overview.sync = st; renderStatus(st, state.overview.settings); renderLog(st); }
      if (st.running) { state.wasSyncing = true; state.timer = setTimeout(poll, 2000); }
      else if (state.wasSyncing) { state.wasSyncing = false; await load(); }
    } catch (_) { /* logged out */ }
  }
  function showSection(name) {
    $$(".sec").forEach((s) => s.classList.toggle("active", s.id === `sec-${name}`));
    $$("#sideNav button").forEach((b) => b.classList.toggle("active", b.dataset.sec === name));
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  }
  $$("#sideNav button").forEach((b) => b.addEventListener("click", () => showSection(b.dataset.sec)));
  window.addEventListener("hashchange", () => { const s = location.hash.slice(1); if (s && $(`#sec-${s}`)) showSection(s); });

  function applyTheme(t) { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); try { if (t) localStorage.setItem("theme", t); else localStorage.removeItem("theme"); } catch (_) { /* ignore */ } }
  $("#themeBtn").addEventListener("click", () => { const cur = document.documentElement.getAttribute("data-theme"); const isDark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches; applyTheme(isDark ? "light" : "dark"); });
  try { const t = localStorage.getItem("theme"); if (t) applyTheme(t); } catch (_) { /* ignore */ }

  /** Header subtitle: which admin is signed in (an account, or the built-in admin password). */
  function whoAmI() {
    const me = state.me || {};
    if (me.uid) return `Signed in as ${me.name || me.email}`;
    return me.email ? `Signed in as ${me.email} (built-in admin)` : "Signed in with the admin password";
  }
  async function boot() {
    const me = await api("/api/admin/me");
    state.emailRequired = !!me.email_required;  // ADMIN_EMAIL set on the server: no "leave the email empty" login
    if (!me.authenticated) { showAuth(me.needs_setup); return; }
    state.me = me.user || {};
    $("#adminSub").textContent = whoAmI();
    $("#authWrap").style.display = "none"; $("#adminLayout").style.display = ""; $("#logoutBtn").style.display = "";
    await load();
    const sec = location.hash.slice(1); if (sec && $(`#sec-${sec}`)) showSection(sec);
    const p = new URLSearchParams(location.search);
    if (p.get("connected")) { toast(p.get("connected"), 6000); history.replaceState(null, "", "/admin#sources"); }
    if (p.get("error")) { toast(p.get("error"), 9000); history.replaceState(null, "", "/admin#sources"); }
    poll();
  }
  boot().catch((err) => toast("Failed to load: " + err.message, 8000));
})();
