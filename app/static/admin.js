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
  /** Why a sync run started (sync_runs.trigger). A run nobody pressed a button for is the watcher reacting to a change. */
  const TRIGGER = { manual: "Sync button", watch: "Change in SharePoint / Drive", queued: "Queued behind the previous run", schedule: "Auto-sync timer", oauth: "After sign-in", upload: "Upload" };

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
    renderSources(o.sources, o.settings, !!(o.sync && o.sync.running));
    renderClaude(o.settings, o.models);
    renderCompany(o.settings);
    renderGoogle(o.settings);
    renderMicrosoft(o.settings);
    renderSecurity(o.settings, o.users || []);
    renderAlerts(o.settings, o.alerts || {});
    renderLog(o.sync);
    fillForm($("#backupForm"), o.settings);
    loadBackups();
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
    if (!s.llm_ready) warn.push(`<a href="#claude" data-go="claude">Add a Claude API key</a> before syncing (or set <code>LLM_PROVIDER=ollama</code> in .env).`);
    if (!s.has_google_client && !s.has_ms_client) warn.push(`Set up <a href="#microsoft" data-go="microsoft">Microsoft 365</a> (SharePoint / OneDrive) or <a href="#google" data-go="google">Google Drive</a> to connect cloud folders.`);
    if (state.overview.production && !s.secrets_encrypted) warn.push(`<b>APP_SECRET_KEY is not set</b>: the API key and Google tokens are stored unencrypted. See docs/DEPLOYMENT.md.`);
    if (!state.overview.production) warn.push(`Running in local mode (no PUBLIC_BASE_URL). Fine on this PC; set it before exposing the app on a server.`);
    const c = st.counts || {};
    const l = st.llm || {};
    const lcls = { connected: "good", ready: "good", model_missing: "warning", unreachable: "critical", no_key: "critical", key_invalid: "critical" }[l.state] || "muted";
    if (l.state && l.ok === false) warn.push(`<b>${esc(l.label)}</b> — ${esc(l.message)} <a href="#claude" data-go="claude">Document reader settings</a>`);
    $("#statusStrip").innerHTML = `
      <div class="strip-row">
        <div><b>${st.running ? esc(st.message || "Syncing…") : "Sync idle"}</b> <span class="badge ${lcls}" title="${esc(l.message || "")}">${esc(l.label || "AI: checking…")}</span>${st.running && st.current_file ? ` <span class="muted">— ${esc(st.current_file)}</span>` : ""}${!st.running && st.last_finished_at ? ` <span class="muted">— last finished ${when(st.last_finished_at)}</span>` : ""}</div>
        <div class="muted small">${Object.entries(c).map(([k, v]) => `${k === "split" ? "Workbooks read sheet by sheet" : label(k)}: ${v}`).join(" · ") || "no documents yet"}</div>
        <div class="actions"><button class="btn primary sm" id="syncAllBtn" ${st.running || !s.llm_ready ? "disabled" : ""}>Sync all sources</button> <button class="btn sm" id="watchNowBtn" ${st.running || !s.llm_ready ? "disabled" : ""} title="Ask SharePoint / Google Drive for changes right now">Check for changes</button>${st.running ? ` <button class="btn sm" id="stopSyncBtn" ${st.stop_requested ? "disabled" : ""} title="Finish the files being read now, then stop. Nothing already read is lost.">${st.stop_requested ? "Stopping…" : "Stop"}</button>` : ""}</div>
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

  const pct = (a, b) => b ? Math.min(100, Math.round(a / b * 100)) : 0;
  const nf = (n) => (n || 0).toLocaleString("en-IE");
  const fileList = (names, lead) => names && names.length ? `<div class="muted small">${lead}: ${names.map(esc).join(", ")}</div>` : "";
  /** One source's own progress: live while its part of the run goes (files checked, new / changed ones being read, the last
   *  few read), and after a run a "100% complete" line with the last run's figures — so the owner can tell a big folder is
   *  fully done, and when the watcher picks up a couple of new uploads, that only those are being read. */
  function sourceProgress(src, st) {
    const live = st && st.running && st.sources && st.sources[src.id];
    if (live && (live.phase === "listing" || live.phase === "processing")) {
      const why = TRIGGER[live.trigger] ? ` · ${TRIGGER[live.trigger].toLowerCase()}` : "";
      if (live.phase === "listing") return `<div class="src-progress live"><div class="progress"><i style="width:2%"></i></div><div class="small"><b>Listing the files in this folder…</b>${why}</div></div>`;
      const p = pct(live.checked, live.total);
      const changed = live.to_read ? `<b>${nf(live.to_read)} new or changed</b> (${nf(live.read)} read${live.skipped ? `, ${nf(live.skipped)} skipped` : ""}${live.errors ? `, <span class="neg">${nf(live.errors)} failed</span>` : ""})` : "no new or changed files so far";
      return `<div class="src-progress live"><div class="progress"><i style="width:${p}%"></i></div>
        <div class="small"><b>Syncing ${p}%</b> — ${nf(live.checked)} of ${nf(live.total)} files checked · ${changed}${why}</div>
        ${fileList(live.reading, "Reading now")}${fileList(live.recent.map((r) => r.file).reverse(), "Just read")}</div>`;
    }
    if (live && live.phase === "queued") return `<div class="src-progress"><div class="small muted">Waiting its turn — syncs after ${esc(st.current_source || "the current source")}.</div></div>`;
    const r = src.last_run;
    if (!r) return "";
    const why = TRIGGER[r.trigger] ? ` · started by: ${TRIGGER[r.trigger].toLowerCase()}` : "";
    if (r.status === "done") {
      return `<div class="src-progress complete"><div class="progress"><i style="width:100%"></i></div>
        <div class="small"><b>✓ 100% complete</b> — all ${nf(r.total)} files checked · ${nf(r.processed)} new or changed read · ${nf(r.skipped)} unchanged or skipped${r.errors ? ` · <span class="neg">${nf(r.errors)} failed</span>` : ""}${r.removed ? ` · ${nf(r.removed)} removed` : ""} · finished ${when(r.finished_at)}${why}</div>
        ${fileList(r.files.slice().reverse(), r.processed ? "Last files read" : "")}</div>`;
    }
    const done = (r.processed || 0) + (r.skipped || 0) + (r.errors || 0), p = pct(done, r.total);
    if (r.status === "stopped") return `<div class="src-progress stopped"><div class="progress"><i style="width:${p}%"></i></div><div class="small"><b>Stopped at ${p}%</b>${r.message && r.message !== "stopped by the admin" ? ` <span class="neg">(${esc(r.message)})</span>` : ""} — ${nf(done)} of ${nf(r.total)} files checked, ${nf(r.processed)} read · ${when(r.finished_at)}${why}. The next sync carries on from here.</div></div>`;
    if (r.status === "failed") return `<div class="src-progress failed"><div class="progress"><i style="width:${p}%"></i></div><div class="small"><b class="neg">Failed at ${p}%</b> — ${esc(r.message || "")} · ${when(r.finished_at)}${why}</div></div>`;
    return "";
  }
  /** Every 2 s while a sync runs: refresh only the progress blocks (the cards themselves stay greyed out, nothing re-binds). */
  function updateSourceProgress(st) {
    (state.overview.sources || []).forEach((src) => { const el = document.querySelector(`#sourceList .source[data-id="${src.id}"] .src-slot`); if (el) el.innerHTML = sourceProgress(src, st); });
  }

  function renderSources(list, s, running = false) {
    // While a sync runs every card control is greyed out: a second Sync would only queue behind it, and Edit / Remove /
    // Disconnect would pull the folder from under the run. Everything comes back when the run finishes (poll → load()).
    const busy = running ? "disabled" : "";
    const busyTitle = running ? ` title="Wait for the running sync to finish (or press Stop above)"` : "";
    // "Add" buttons stay disabled until the matching app registration exists (also when there are no sources yet).
    $("#addDriveBtn").disabled = !s.has_google_client;
    $("#addDriveNote").innerHTML = s.has_google_client ? "" : `Upload the Google OAuth client under <a href="#google" data-go="google">Google setup</a> first.`;
    $$("#addDriveNote [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("google"); }));
    $("#addMsBtn").disabled = !s.has_ms_client;
    $("#addMsNote").innerHTML = s.has_ms_client ? "" : `Fill in the app registration under <a href="#microsoft" data-go="microsoft">Microsoft setup</a> first.`;
    $$("#addMsNote [data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showSection("microsoft"); }));
    if (!list.length) { $("#sourceList").innerHTML = `<div class="card empty">No sources yet. Add a SharePoint / OneDrive folder, a Google Drive or a local folder below.</div>`; return; }
    $("#sourceList").innerHTML = (list.length > 1 ? `<p class="muted small" style="margin:0 0 8px">Cards are synced top to bottom: use ▲ ▼ to put the folder that matters most first.</p>` : "") + list.map((src, i) => `
      <div class="card source ${src.enabled ? "" : "disabled"}" data-id="${src.id}">
        <div class="source-head">
          <div class="order-btns" title="${running ? "Wait for the running sync to finish" : "Sync order: cards are synced top to bottom"}">
            <button class="icon-btn" data-act="up" aria-label="Move up (synced earlier)" ${i === 0 || running ? "disabled" : ""}>▲</button>
            <button class="icon-btn" data-act="down" aria-label="Move down (synced later)" ${i === list.length - 1 || running ? "disabled" : ""}>▼</button>
          </div>
          <div style="flex:1">
            <div class="source-title"><span class="chip ${(KIND[src.kind] || ["", "pipe"])[1]}">${kindLabel(src.kind)}</span> <b>${esc(src.name)}</b> ${statusBadge(src)} ${src.enabled ? "" : `<span class="badge muted">Disabled</span>`}</div>
            <div class="muted small">${sourceWhere(src)}</div>
            <div class="muted small">${src.doc_count} files known · <a href="/?source=${src.id}&status=done#documents">${src.done_count} read</a>${src.skipped_count ? ` · <a href="/?source=${src.id}&status=skipped#documents">${src.skipped_count} skipped</a>` : ""}${src.error_count ? ` · <a class="neg" href="/?source=${src.id}&status=error#documents">${src.error_count} errors</a>` : ""}${src.waiting_count ? ` · ${src.waiting_count} waiting` : ""} · last sync ${when(src.last_sync_at)}${isCloud(src) && src.connected ? ` · <span title="The watcher asks ${src.kind === "m365" ? "SharePoint / OneDrive" : "Google Drive"} about this folder every watch interval and syncs only this card when something changed">checked for changes ${when(src.last_change_check)} · last change found ${when(src.last_change_found)}</span>` : ""}${src.last_error ? ` · <span class="neg">${esc(src.last_error)}</span>` : ""}</div>
            ${src.connected ? `<div class="small">${src.can_upload ? `<span class="ok">Uploads from the dashboard enabled</span>` : `<span class="missing">Uploads disabled — ${src.kind === "m365" && src.ms_link === "shared" ? "add a source for a specific folder to upload into it" : src.kind === "m365" && !src.ms_upload ? "turn on uploads (Edit) and reconnect" : "reconnect to grant the “create files” permission"}</span>`}</div>` : ""}
          </div>
          <label class="switch" title="${running ? "Wait for the running sync to finish" : "Include in syncs"}"><input type="checkbox" data-act="toggle" ${src.enabled ? "checked" : ""} ${busy}><span></span></label>
        </div>
        <div class="src-slot">${sourceProgress(src, state.overview && state.overview.sync)}</div>
        <div class="actions"${running ? ' aria-busy="true"' : ""}>
          ${isCloud(src) ? `<a class="btn ${src.connected ? "" : "primary"} sm ${running ? "is-disabled" : ""}" href="/admin/sources/${src.id}/connect"${running ? ' aria-disabled="true" tabindex="-1"' : ""}${busyTitle}>${src.connected ? "Reconnect / change account" : src.kind === "m365" ? "Connect with Microsoft" : "Connect with Google"}</a>` : ""}
          <button class="btn sm ${isCloud(src) && src.connected ? "primary" : ""}" data-act="sync" ${!s.llm_ready || !src.connected ? "disabled" : busy}${busyTitle}>Sync now</button>
          <button class="btn sm" data-act="force" ${!s.llm_ready || !src.connected ? "disabled" : busy} title="${running ? "Wait for the running sync to finish" : "Re-read every file, even ones already processed"}">Full re-read</button>
          <button class="btn sm" data-act="edit" ${busy}${busyTitle}>Edit</button>
          ${isCloud(src) && src.connected ? `<button class="btn sm" data-act="disconnect" ${busy}${busyTitle}>Disconnect</button>` : ""}
          <button class="btn sm danger-btn" data-act="remove" ${busy}${busyTitle}>Remove</button>
          <a class="btn sm" href="/?source=${src.id}#documents" title="Open the owner dashboard's Documents tab showing only this source's files">View documents</a>
        </div>
      </div>`).join("");
    $$("#sourceList .source").forEach((card) => {
      const id = card.dataset.id, src = list.find((x) => String(x.id) === id);
      if (running) { card.querySelectorAll("a.is-disabled").forEach((a) => a.addEventListener("click", (e) => e.preventDefault())); return; }  // nothing is wired while busy
      card.querySelector('[data-act="toggle"]').addEventListener("change", (e) => run(api(`/api/admin/sources/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: e.target.checked }) }), e.target.checked ? "Enabled" : "Disabled"));
      card.querySelector('[data-act="sync"]').addEventListener("click", () => run(api(`/api/admin/sources/${id}/sync`, { method: "POST" }), "Sync started"));
      card.querySelector('[data-act="up"]').addEventListener("click", () => run(api(`/api/admin/sources/${id}/move?direction=up`, { method: "POST" }), null));
      card.querySelector('[data-act="down"]').addEventListener("click", () => run(api(`/api/admin/sources/${id}/move?direction=down`, { method: "POST" }), null));
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
    fillForm($("#geminiForm"), s);
    $("#geminiModels").innerHTML = (state.overview.gemini_models || []).map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
    const ollama = s.llm_provider === "ollama";
    const keyLine = (saved, masked, prov, label) => {
      const inUse = s.llm_provider === prov, isFallback = s.llm_fallback === prov;
      if (saved) return `<span class="ok">Key saved (${esc(masked)})${inUse ? "" : isFallback ? " — used as the fallback reader" : " — not in use until chosen above"}</span>`;
      return inUse || isFallback ? `<span class="missing">No key saved — ${label} is ${inUse ? "the reader" : "the fallback"} but cannot work without it</span>`
        : `<span class="muted">No key saved — only needed if ${label} is chosen as the reader or the fallback</span>`;
    };
    $('#claudeForm [name="anthropic_api_key"]').placeholder = s.has_api_key ? `Saved: ${s.anthropic_api_key_masked} — paste a new key to replace` : "sk-ant-…";
    $("#keyStatus").innerHTML = keyLine(s.has_api_key, s.anthropic_api_key_masked, "claude", "Claude");
    $('#geminiForm [name="gemini_api_key"]').placeholder = s.has_gemini_key ? `Saved: ${s.gemini_api_key_masked} — paste a new key to replace` : "AIza…";
    $("#geminiKeyStatus").innerHTML = keyLine(s.has_gemini_key, s.gemini_api_key_masked, "gemini", "Gemini");
    const l = (state.overview && state.overview.sync && state.overview.sync.llm) || {};
    const lcls = { connected: "good", ready: "good", model_missing: "warning", unreachable: "critical", no_key: "critical", key_invalid: "critical" }[l.state] || "muted";
    const readBy = Object.entries((state.overview && state.overview.sync && state.overview.sync.read_by) || {});
    const statusLine = (r) => `<span class="badge ${{ connected: "good", ready: "good", model_missing: "warning", unreachable: "critical", no_key: "critical", key_invalid: "critical" }[r.state] || "muted"}">${esc(r.label || "Checking…")}</span>
      <span class="${r.ok ? "ok" : r.ok === false ? "missing" : "muted"}">${esc(r.message || "")}</span>${r.checked_at ? ` <span class="muted">· checked ${when(r.checked_at)}</span>` : ""}
      ${r.models ? `<br><span class="muted">Installed models on the Ollama PC: ${esc(r.models.join(", ") || "none")}</span>` : ""}${r.probe ? ` <span class="muted">· probe answered “${esc(r.probe)}”</span>` : ""}`;
    const NAMES = { ollama: "Ollama", claude: "Claude", gemini: "Gemini" };
    const DESC = { ollama: "Ollama — the internal AI on your network (private, no API cost; text documents only)",
      claude: "Claude API — Anthropic (also reads photos and scanned PDFs; uses API credit)",
      gemini: "Gemini API — Google (also reads photos and scanned PDFs; uses API quota)" };
    const provs = s.providers || [];
    const who = ollama ? `Ollama, your own AI server: model <code>${esc(s.llm_model)}</code> at <code>${esc(s.llm_url)}</code>` : `${NAMES[s.llm_provider] || s.llm_provider} API: model <code>${esc(s.llm_model)}</code>`;
    $("#llmCard").innerHTML = `
      <div class="strip-row"><div><b>Document reader</b> — ${who}${s.llm_fallback ? ` · fallback <b>${esc(NAMES[s.llm_fallback] || s.llm_fallback)}</b>${s.llm_fallback_usable ? "" : ' <span class="missing">(no key saved for it)</span>'}` : ""}</div>
        <div><button class="btn sm" id="llmCheckBtn">Check now</button></div></div>
      <div class="small" id="llmStatus" style="margin:6px 0 10px">${statusLine(l)}</div>
      <div class="small" style="margin:0 0 12px">Figures in the ledger were read by: ${readBy.length ? readBy.map(([m, n]) => `<span class="chip">${esc(m.startsWith("ollama/") ? "Ollama " + m.slice(7) : m.startsWith("gemini/") ? "Gemini " + m.slice(7) : m === "manual" ? "Entered by hand" : m === "jobsheet" ? "Job summary sheets" : m)}: ${n}</span>`).join(" ") : "nothing read yet"}</div>
      <form id="llmForm" class="form">
        <div class="grid two">
          <label>Read documents with
            <select name="llm_provider">${provs.map((p) => `<option value="${p.id}">${esc(DESC[p.id] || p.label)}${p.ready ? "" : " — key missing"}</option>`).join("")}</select></label>
          <label>Second reader (fallback) when the first is unreachable, out of credit, or given a photo / scanned PDF it cannot read
            <select name="llm_fallback"><option value="">None</option>${provs.map((p) => `<option value="${p.id}">${esc(p.label)}${p.ready ? "" : " — key missing"}</option>`).join("")}</select></label>
        </div>
        <div class="actions"><button class="btn primary" type="submit">Save reader</button><span class="muted small" id="llmSaved"></span>
          <span class="muted small">Default comes from <code>LLM_PROVIDER</code> / <code>LLM_FALLBACK</code> in .env; the choice saved here wins.</span></div>
      </form>`;
    $('#llmForm [name="llm_provider"]').value = s.llm_provider;
    $('#llmForm [name="llm_fallback"]').value = s.llm_fallback || "";
    $("#llmForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { llm_provider: $('#llmForm [name="llm_provider"]').value, llm_fallback: $('#llmForm [name="llm_fallback"]').value };
      if (body.llm_fallback === body.llm_provider) body.llm_fallback = "";
      try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); toast(`Documents are now read with ${NAMES[body.llm_provider] || body.llm_provider}${body.llm_fallback ? `, falling back to ${NAMES[body.llm_fallback]}` : ""}`); setTimeout(load, 1500); }
      catch (err) { toast(err.message, 6000); }
    });
    $("#llmCheckBtn").addEventListener("click", async () => {
      const btn = $("#llmCheckBtn"); btn.disabled = true; btn.textContent = "Checking…";
      try {
        const r = await api("/api/admin/llm-check", { method: "POST" });
        $("#llmStatus").innerHTML = statusLine(r);
        if (state.overview && state.overview.sync) { state.overview.sync.llm = r; renderStatus(state.overview.sync, state.overview.settings); }
      } catch (err) { $("#llmStatus").innerHTML = `<span class="missing">${esc(err.message)}</span>`; }
      btn.disabled = false; btn.textContent = "Check now";
    });
  }
  $("#showKeyBtn").addEventListener("click", () => { const i = $('#claudeForm [name="anthropic_api_key"]'); i.type = i.type === "password" ? "text" : "password"; $("#showKeyBtn").textContent = i.type === "password" ? "Show" : "Hide"; });
  $("#showGeminiKeyBtn").addEventListener("click", () => { const i = $('#geminiForm [name="gemini_api_key"]'); i.type = i.type === "password" ? "text" : "password"; $("#showGeminiKeyBtn").textContent = i.type === "password" ? "Show" : "Hide"; });
  $("#testGeminiKeyBtn").addEventListener("click", async () => {
    const btn = $("#testGeminiKeyBtn"); btn.disabled = true; btn.textContent = "Testing…";
    try {
      const r = await api("/api/admin/test-key", { method: "POST", body: JSON.stringify({ provider: "gemini", key: $('#geminiForm [name="gemini_api_key"]').value, model: $('#geminiForm [name="gemini_model"]').value || "auto" }) });
      $("#geminiKeyStatus").innerHTML = `<span class="${r.ok ? "ok" : "missing"}">${esc(r.message)}</span>${r.models ? `<br><span class="muted">Models this key can use: ${esc(r.models.join(", "))}</span>` : ""}`;
      if (r.models) $("#geminiModels").innerHTML = `<option value="auto">auto — newest Flash model for this key</option>` + r.models.map((m) => `<option value="${esc(m)}">`).join("");
    } catch (err) { $("#geminiKeyStatus").innerHTML = `<span class="missing">${esc(err.message)}</span>`; }
    btn.disabled = false; btn.textContent = "Test key";
  });
  $("#geminiForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { gemini_model: $('#geminiForm [name="gemini_model"]').value.trim() || "auto" };
    const key = $('#geminiForm [name="gemini_api_key"]').value.trim();
    if (key) body.gemini_api_key = key;
    try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); $('#geminiForm [name="gemini_api_key"]').value = ""; $("#geminiSaved").textContent = "Saved"; await load(); setTimeout(() => ($("#geminiSaved").textContent = ""), 2500); }
    catch (err) { toast(err.message, 6000); }
  });
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

  // ------------------------------------------------------------ alerts (email)
  function renderAlerts(s, a) {
    fillForm($("#alertsForm"), s);
    $('#alertsForm [name="alerts_enabled"]').checked = String(s.alerts_enabled) !== "0";
    $('#alertsForm [name="smtp_password"]').placeholder = s.has_smtp_password ? "Saved — type a new one to replace" : "app password";
    $("#smtpPasswordStatus").textContent = s.has_smtp_password ? "A password is saved (encrypted)." : "";
    const on = String(s.alerts_enabled) !== "0";
    $("#alertsStatus").innerHTML = `<ul class="setup-steps" style="list-style:none;padding:0">
      <li class="${a.configured ? "ok" : "missing"}">${a.configured ? `Alerts go to ${esc((a.recipients || []).join(", "))}` : "Not configured yet — fill in the mail server and a recipient below"}</li>
      <li class="${on ? "ok" : "missing"}">${on ? "Alerts are switched on" : "Alerts are switched off"}</li>
      <li class="${a.last_error ? "missing" : "ok"}">${a.last_error ? `Last attempt failed: ${esc(a.last_error)}` : a.last_sent_at ? `Last sent ${when(a.last_sent_at)}: ${esc(a.last_subject || "")} (${a.sent_count} so far)` : "Nothing sent yet"}</li></ul>`;
  }
  $("#showSmtpBtn").addEventListener("click", () => { const i = $('#alertsForm [name="smtp_password"]'); i.type = i.type === "password" ? "text" : "password"; $("#showSmtpBtn").textContent = i.type === "password" ? "Show" : "Hide"; });
  $("#alertsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target); const body = {};
    for (const [k, v] of fd.entries()) { if (k === "smtp_password" && !v) continue; body[k] = k === "smtp_port" ? parseInt(v || "587", 10) : v; }
    body.alerts_enabled = $('#alertsForm [name="alerts_enabled"]').checked;
    try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); $('#alertsForm [name="smtp_password"]').value = ""; $("#alertsSaved").textContent = "Saved"; await load(); setTimeout(() => ($("#alertsSaved").textContent = ""), 2500); }
    catch (err) { toast(err.message, 7000); }
  });
  $("#alertTestBtn").addEventListener("click", async () => {
    const btn = $("#alertTestBtn"); btn.disabled = true; btn.textContent = "Sending…";
    try { const r = await api("/api/admin/alerts/test", { method: "POST" }); toast(r.message, 6000); await load(); }
    catch (err) { toast(err.message, 9000); }
    btn.disabled = false; btn.textContent = "Send test email";
  });
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
    $("#runsTable").innerHTML = runs.length ? `<table><thead><tr><th>Started</th><th>Source</th><th>Started by</th><th class="num">Files</th><th class="num">Read</th><th class="num">Skipped</th><th class="num">Errors</th><th>Result</th></tr></thead><tbody>${runs.map((r) => `<tr><td>${when(r.started_at)}</td><td>${esc(r.source_name || r.source)}${r.force ? ' <span class="chip">full</span>' : ""}</td><td class="muted small">${esc(TRIGGER[r.trigger] || label(r.trigger))}</td><td class="num">${r.total}</td><td class="num">${r.processed}</td><td class="num">${r.skipped}</td><td class="num">${r.errors}</td><td>${esc(label(r.status))}${r.message ? ` <span class="neg small">${esc(r.message)}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No syncs yet</div>`;
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
  $("#repairDates").addEventListener("click", async () => {
    const btn = $("#repairDates"); btn.disabled = true;
    try {
      const r = await api("/api/admin/repair-dates", { method: "POST" });
      $("#repairDatesResult").textContent = r.checked ? `${r.updated} of ${r.checked} undated document${r.checked === 1 ? "" : "s"} now carry a date` : "Every document already has a date";
    } catch (err) { toast(err.message, 6000); }
    btn.disabled = false;
  });
  $("#clearDocs").addEventListener("click", () => { if (confirm("Delete ALL extracted documents and sync history? This cannot be undone.")) run(api("/api/admin/clear-documents", { method: "POST" }), "All documents deleted"); });

  // ------------------------------------------------------------ backups (Data section)
  const mb = (b) => b == null ? "" : b >= 1073741824 ? `${(b / 1073741824).toFixed(2)} GB` : `${(b / 1048576).toFixed(1)} MB`;
  let backupTimer = null;
  function renderBackups(d) {
    const dir = d.dir || {}, job = d.job || {}, items = d.items || [];
    $("#backupDirStatus").innerHTML = dir.writable
      ? `<span class="ok">Writing to <code>${esc(dir.path)}</code>${dir.default ? " (default, on this server's data disk)" : ""}${dir.free_bytes != null ? ` · ${esc(mb(dir.free_bytes))} free` : ""}</span>`
      : `<span class="missing">${esc(dir.path)}: ${esc(dir.problem || "not writable")}</span>`;
    $("#backupStopBtn").style.display = job.running ? "" : "none";
    $("#backupFullBtn").disabled = !!job.running; $("#backupAppBtn").disabled = !!job.running;
    const pct = job.total ? Math.round(job.done / job.total * 100) : 0;
    $("#backupJob").innerHTML = job.running
      ? `<div class="progress"><i style="width:${pct}%"></i></div><div class="muted">${esc(job.message)} · ${job.done} of ${job.total} files · ${esc(mb(job.bytes))}${job.error_count ? ` · <span class="neg">${job.error_count} could not be fetched</span>` : ""}</div>`
      : job.phase === "done" || job.phase === "stopped" || job.phase === "failed"
        ? `<span class="${job.phase === "done" ? "ok" : "missing"}">${esc(job.message)}</span>${job.errors && job.errors.length ? `<ul class="small">${job.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}`
        : d.schedule && d.schedule.every_hours ? `<span class="muted">Automatic application backup every ${d.schedule.every_hours} h, keeping ${d.schedule.keep}.</span>` : `<span class="muted">No automatic backups (set the hours above).</span>`;
    $("#backupList").innerHTML = items.length ? `<table><thead><tr><th>Backup</th><th>Kind</th><th class="num">Size</th><th>Created</th><th></th></tr></thead><tbody>${items.map((b) => `<tr>
        <td><code>${esc(b.name)}</code>${b.complete ? "" : ' <span class="badge warning">incomplete</span>'}</td><td>${b.kind === "app" ? "Application data" : "Everything incl. files"}</td><td class="num">${esc(mb(b.size))}</td><td>${when(b.created)}</td>
        <td class="actions"><a class="btn sm" href="/api/admin/backups/download?name=${encodeURIComponent(b.name)}">Download zip</a> <button class="btn sm" data-del-backup="${esc(b.name)}">Delete</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="muted small">No backups in this folder yet.</div>`;
    $$("#backupList [data-del-backup]").forEach((btn) => btn.addEventListener("click", async () => {
      if (!confirm(`Delete backup ${btn.dataset.delBackup}? The backup is removed from the folder; the ledger itself is not touched.`)) return;
      try { await api(`/api/admin/backups/${encodeURIComponent(btn.dataset.delBackup)}`, { method: "DELETE" }); toast("Backup deleted"); await loadBackups(); } catch (err) { toast(err.message, 6000); }
    }));
    clearTimeout(backupTimer);
    if (job.running) backupTimer = setTimeout(loadBackups, 2000);
  }
  async function loadBackups() {
    try { renderBackups(await api("/api/admin/backups")); } catch (_) { /* logged out */ }
  }
  $("#backupForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = { backup_dir: fd.get("backup_dir").trim(), backup_every_hours: parseInt(fd.get("backup_every_hours") || "0", 10), backup_keep: parseInt(fd.get("backup_keep") || "10", 10) };
    try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }); toast("Backup settings saved"); await loadBackups(); } catch (err) { toast(err.message, 7000); }
  });
  $("#backupAppBtn").addEventListener("click", async () => {
    const btn = $("#backupAppBtn"); btn.disabled = true; btn.textContent = "Backing up…";
    try { const r = await api("/api/admin/backups/app", { method: "POST" }); toast(`Application backup written: ${r.item.name} (${mb(r.item.size)})`, 6000); await loadBackups(); }
    catch (err) { toast(err.message, 8000); }
    btn.disabled = false; btn.textContent = "Back up application data now";
  });
  $("#backupFullBtn").addEventListener("click", async () => {
    if (!confirm("Fetch every document file from the sources into the backup folder? Nothing on the sources is changed; it can take a while and needs free space.")) return;
    try { await api("/api/admin/backups/full", { method: "POST" }); toast("Full backup started"); await loadBackups(); } catch (err) { toast(err.message, 8000); }
  });
  $("#backupStopBtn").addEventListener("click", async () => { try { await api("/api/admin/backups/stop", { method: "POST" }); await loadBackups(); } catch (err) { toast(err.message, 6000); } });
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
      if (state.overview) {
        const wasRunning = !!(state.overview.sync && state.overview.sync.running);
        state.overview.sync = st; renderStatus(st, state.overview.settings); renderLog(st);
        if (!!st.running !== wasRunning) renderSources(state.overview.sources, state.overview.settings, !!st.running);  // grey the cards out / back
        else if (st.running) updateSourceProgress(st);
      }
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
