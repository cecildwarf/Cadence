/* Cadence — front end (vanilla JS, no framework).
   Mobile-first: every workflow works by tapping buttons; drag is
   an extra for mouse users, never the only way to reorder.
   Copy-to-clipboard uses navigator.clipboard with a textarea
   fallback, because iOS Safari needs it.
   Queue: polls /api/queue every 2s while anything is queued or
   building, so every row shows a live bar + % + stage. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let browsePath = "";
  let browseEntries = [];
  let currentJob = null;
  let pollTimer = null;
  let queueTimer = null;
  let logJobId = "app";
  let pickerTarget = null;
  let pickerPath = "/";
  let setupDismissed = false;

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { data = { ok: false, error: text }; }
    if (!res.ok && data && !data.error) data.error = "HTTP " + res.status;
    return { res, data };
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  /* --- Copy to clipboard: navigator.clipboard, textarea fallback for iOS --- */
  async function copyText(text, msgEl) {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext !== false) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (e) { ok = false; }
    if (!ok) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
    }
    if (msgEl) msgEl.textContent = ok ? "✓ Copied — paste it into chat." : "Copy failed — use the plain-text view and long-press > Select All > Copy.";
    return ok;
  }
  function flashCopied(btn) {
    const old = btn.textContent;
    btn.textContent = "✓ Copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 2000);
  }
  function progressBar(pct, stage) {
    pct = Math.max(0, Math.min(100, Math.round(pct || 0)));
    return "<div class='progress' role='progressbar' aria-valuenow='" + pct + "' aria-valuemin='0' aria-valuemax='100' aria-label='Progress: " + esc(stage || "") + "'>"
      + "<div class='fill' style='width:" + pct + "%'></div></div>"
      + "<p class='meta-line'><strong>" + pct + "%</strong> · " + esc(stage || "") + "</p>";
  }
  function stageText(job) {
    const map = { queued: "Queued", scanning: "Scanning", metadata: "Metadata",
      sorting: "Sorting", converting: "Converting", health: "Health check",
      placing: "Placing in Plex",
      done: "Done", failed: "Failed", cancelled: "Cancelled", created: "Needs review",
      needs_health_review: "Needs health review", rejected: "Rejected" };
    return map[job.stage] || map[job.status] || (job.status || "");
  }

  /* --- Tabs --- */
  document.querySelectorAll("nav.tabs button").forEach((b) => {
    b.addEventListener("click", () => showTab(b.dataset.tab));
  });
  function showTab(name) {
    ["browse", "queue", "review", "cleanup", "job", "jobs", "logs", "settings"].forEach((t) => {
      $("tab-" + t).classList.toggle("hidden", t !== name);
    });
    if (name === "queue") loadQueue();
    if (name === "review") loadReview();
    if (name === "cleanup") loadCleanup();
    if (name === "jobs") loadJobs();
    if (name === "logs") loadLogList();
    if (name === "settings") loadSettings();
  }

  /* --- Health / startup errors / last error / setup wizard --- */
  async function loadHealth() {
    const { data } = await api("/api/health");
    if (!data) return;
    $("inbox-path").textContent = (data.settings && data.settings.inbox_dir) || "(not set yet — finish setup)";
    const errs = data.startup_errors || [];
    const setup = data.setup_required;
    if (setup && !setupDismissed) {
      showSetup(data.setup_errors || []);
    } else if (setup) {
      $("startup-banner").innerHTML = '<div class="banner warn"><strong>⚠ Audiobook paths need attention:</strong><ul>'
        + (data.setup_errors || []).map((e) => "<li>" + esc(e.message) + "</li>").join("") + "</ul>"
        + '<button class="secondary" id="banner-setup-btn" type="button">Open setup</button></div>';
      const btn = $("banner-setup-btn");
      if (btn) btn.addEventListener("click", () => { setupDismissed = false; showSetup(data.setup_errors || []); });
    } else {
      $("startup-banner").innerHTML = errs.length
        ? '<div class="banner error"><strong>⚠ Startup problem — fix before building:</strong><ul>'
          + errs.map((e) => "<li>" + esc(e.message) + "</li>").join("") + "</ul></div>"
        : "";
    }
    const badge = $("queue-count-badge");
    const q = (data.queue || {});
    const active = (q.queued || 0) + (q.building || 0);
    if (active > 0) { badge.textContent = String(active); badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");
    const reviewCount = data.review_awaiting || 0;
    const rbadge = $("review-count-badge");
    if (reviewCount > 0) { rbadge.textContent = String(reviewCount); rbadge.classList.remove("hidden"); }
    else rbadge.classList.add("hidden");
    $("review-banner").innerHTML = reviewCount > 0
      ? '<div class="banner warn"><strong>🔍 ' + reviewCount + ' book(s) need a health review:</strong> '
        + 'a finished file did not match its metadata, so it is held in the Review folder — '
        + 'not in Plex — until you approve or reject it. '
        + '<button class="secondary" id="banner-review-btn" type="button">Open Review</button></div>'
      : "";
    const rbtn = $("banner-review-btn");
    if (rbtn) rbtn.addEventListener("click", () => showTab("review"));
    const { data: le } = await api("/api/logs/last-errors");
    const errors = (le && le.errors) || [];
    $("last-error-banner").innerHTML = errors.length
      ? '<div class="banner error"><strong>⛔ Last error:</strong> '
        + esc(errors[0].message) + ' <em>(job ' + esc(errors[0].job_id || "?")
        + ', ' + esc(errors[0].ts_local || "") + ")</em> — see Logs.</div>"
      : "";
  }

  /* --- First-run setup wizard --- */
  const TZ_CHIPS = ["UTC", "America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London", "Australia/Sydney"];
  function fillTzChips(el, inputEl) {
    el.innerHTML = "";
    TZ_CHIPS.forEach((tz) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip secondary"; b.textContent = tz;
      b.addEventListener("click", () => { inputEl.value = tz; });
      el.appendChild(b);
    });
  }
  async function fillSuggestions(el, inputEl) {
    const { data } = await api("/api/folders/suggestions");
    el.innerHTML = "";
    ((data && data.suggestions) || []).forEach((s) => {
      if (s.path === "/") return; // root is for browsing, not a one-tap pick
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip secondary";
      b.textContent = s.path + " — " + s.label;
      b.addEventListener("click", () => { inputEl.value = s.path; });
      el.appendChild(b);
    });
  }
  function showSetup(errors) {
    $("setup-wizard").classList.remove("hidden");
    const errBox = $("setup-errors");
    if (errors && errors.length) {
      errBox.classList.remove("hidden");
      errBox.innerHTML = "<strong>These paths need fixing:</strong><ul>"
        + errors.map((e) => "<li>" + esc(e.message) + "</li>").join("") + "</ul>";
    } else errBox.classList.add("hidden");
    fillSuggestions($("suggest-w-inbox"), $("w-inbox"));
    fillSuggestions($("suggest-w-output"), $("w-output"));
    fillSuggestions($("suggest-w-processed"), $("w-processed"));
    fillSuggestions($("suggest-w-review"), $("w-review"));
    fillTzChips($("suggest-w-tz"), $("w-tz"));
  }
  function hideSetup() { $("setup-wizard").classList.add("hidden"); }
  $("setup-skip").addEventListener("click", () => { setupDismissed = true; hideSetup(); loadHealth(); });
  $("setup-save").addEventListener("click", async () => {
    const btn = $("setup-save");
    btn.disabled = true;
    const { res, data } = await api("/api/settings", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ inbox_dir: $("w-inbox").value.trim(), output_dir: $("w-output").value.trim(),
        processed_dir: $("w-processed").value.trim(),
        review_dir: $("w-review").value.trim(),
        timezone: $("w-tz").value.trim() || "UTC", create_output: $("w-create-output").checked }) });
    btn.disabled = false;
    if (data && data.ok) {
      hideSetup(); setupDismissed = false;
      await loadHealth(); loadBrowse();
      alert("✓ Setup saved. Inbox: " + data.settings.inbox_dir + "\nOutput: " + data.settings.output_dir);
    } else {
      const errs = (data && data.errors) || [{ message: (data && data.error) || "Save failed." }];
      const errBox = $("setup-errors");
      errBox.classList.remove("hidden");
      errBox.innerHTML = "<strong>Could not save — fix these:</strong><ul>"
        + errs.map((e) => "<li>" + esc(e.message) + "</li>").join("") + "</ul>";
    }
  });

  /* --- Server folder picker (used by wizard + Settings) --- */
  document.querySelectorAll("[data-picker]").forEach((b) => {
    b.addEventListener("click", () => { pickerTarget = b.dataset.picker; openPicker("/"); });
  });
  function openPicker(path) {
    pickerPath = path || "/";
    $("folder-picker").classList.remove("hidden");
    loadPicker();
  }
  function closePicker() { $("folder-picker").classList.add("hidden"); pickerTarget = null; }
  async function loadPicker() {
    const { data } = await api("/api/folders?path=" + encodeURIComponent(pickerPath));
    const list = $("picker-list");
    if (!data || !data.ok) { list.innerHTML = "<li class='entry'>Cannot list folders: " + esc((data && data.error) || "unknown") + "</li>"; return; }
    pickerPath = data.path;
    $("picker-crumb").textContent = "Server folder: " + data.path;
    list.innerHTML = "";
    if (!data.entries.length) list.innerHTML = "<li class='entry'>No subfolders here — use this folder, or go up.</li>";
    data.entries.forEach((e) => {
      const li = document.createElement("li");
      li.className = "entry";
      li.innerHTML = "<span class='filename'>📁 " + esc(e.name) + "</span>"
        + "<div class='meta-line'>" + e.audio_files + " audio file(s) directly inside</div>"
        + "<div class='row'><button class='secondary'>Open</button>"
        + "<button class='secondary' data-use='1'>Use this folder</button></div>";
      li.querySelector("button.secondary").addEventListener("click", () => { pickerPath = e.path; loadPicker(); });
      li.querySelector("[data-use]").addEventListener("click", () => { pickFolder(e.path); });
      list.appendChild(li);
    });
  }
  function pickFolder(path) {
    if (pickerTarget) $(pickerTarget).value = path;
    closePicker();
  }
  $("picker-up").addEventListener("click", async () => {
    const { data } = await api("/api/folders?path=" + encodeURIComponent(pickerPath));
    if (data && data.ok) { pickerPath = data.parent; loadPicker(); }
  });
  $("picker-root").addEventListener("click", () => { pickerPath = "/"; loadPicker(); });
  $("picker-use").addEventListener("click", () => pickFolder(pickerPath));
  $("picker-cancel").addEventListener("click", closePicker);

  /* --- Browse + bulk queueing --- */
  async function loadBrowse() {
    const { data } = await api("/api/browse?path=" + encodeURIComponent(browsePath));
    const list = $("browse-list");
    $("select-all").checked = false;
    if (!data || !data.ok) {
      list.innerHTML = "<li class='entry'>Could not list inbox: " + esc((data && data.error) || "unknown error")
        + ((data && data.setup_required) ? " <button class='secondary' id='browse-setup-btn' type='button'>Open setup</button>" : "") + "</li>";
      const b = $("browse-setup-btn");
      if (b) b.addEventListener("click", () => { setupDismissed = false; showSetup([]); });
      return;
    }
    $("browse-crumb").textContent = "Inbox: " + data.inbox + " — " + (data.path || "/");
    browseEntries = data.entries || [];
    list.innerHTML = "";
    if (!browseEntries.length) list.innerHTML = "<li class='entry'>Empty folder — nothing here yet.</li>";
    browseEntries.forEach((e) => {
      const li = document.createElement("li");
      li.className = "entry";
      if (e.type === "folder") {
        const routing = e.audio_files === 1 ? "1 file → will auto-queue"
          : e.audio_files === 0 ? "No audio files here"
          : e.audio_files + " files → review order first";
        li.innerHTML = "<label class='checkrow'><input type='checkbox' class='pick' data-rel='" + esc(e.rel) + "' data-kind='folder'> "
          + "<span class='filename'>📁 " + esc(e.name) + "</span></label>"
          + "<div class='meta-line'>" + e.audio_files + " audio file(s) · " + esc(routing) + "</div>"
          + "<div class='row'><button class='secondary'>Open</button>"
          + "<button data-start='1'>▶ Start book from this folder</button></div>";
        li.querySelector("button.secondary").addEventListener("click", () => { browsePath = e.rel; loadBrowse(); });
        li.querySelector("[data-start]").addEventListener("click", () => startJob(e.rel));
      } else {
        li.innerHTML = "<label class='checkrow'><input type='checkbox' class='pick' data-rel='" + esc(e.rel) + "' data-kind='file'> "
          + "<span class='filename'>🎵 " + esc(e.name) + "</span></label>"
          + "<div class='meta-line'>" + (e.size / 1e6).toFixed(1) + " MB · 1 file → will auto-queue</div>"
          + "<div class='row'><button data-queue-file='1'>▶ Queue this file</button></div>";
        li.querySelector("[data-queue-file]").addEventListener("click", () => queueItems([{ file: e.rel }]));
      }
      list.appendChild(li);
    });
  }
  $("browse-up").addEventListener("click", () => {
    browsePath = browsePath.split("/").slice(0, -1).join("/");
    loadBrowse();
  });
  $("browse-refresh").addEventListener("click", loadBrowse);
  $("select-all").addEventListener("change", (e) => {
    document.querySelectorAll("#browse-list .pick").forEach((cb) => { cb.checked = e.target.checked; });
  });
  function selectedItems() {
    const items = [];
    document.querySelectorAll("#browse-list .pick:checked").forEach((cb) => {
      items.push(cb.dataset.kind === "file" ? { file: cb.dataset.rel } : { folder: cb.dataset.rel });
    });
    return items;
  }
  $("queue-selected").addEventListener("click", () => {
    const items = selectedItems();
    if (!items.length) { $("bulk-msg").textContent = "Tick at least one folder or file first."; return; }
    queueItems(items);
  });
  $("queue-all").addEventListener("click", () => {
    const items = browseEntries.map((e) => (e.type === "file" ? { file: e.rel } : { folder: e.rel }));
    if (!items.length) { $("bulk-msg").textContent = "Nothing in this folder to queue."; return; }
    queueItems(items);
  });
  async function queueItems(items) {
    $("bulk-msg").textContent = "Queueing " + items.length + " item(s)…";
    const { data } = await api("/api/queue", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ items }) });
    if (!data || !data.ok) { $("bulk-msg").textContent = "✕ Could not queue: " + ((data && data.error) || "unknown error"); return; }
    const lines = (data.results || []).map((r) =>
      "• " + (r.title || r.item) + " — " + (r.route === "auto_queue" ? "queued ✓" : r.route === "review" ? "needs order review" : "skipped: " + (r.error || r.route)));
    $("bulk-msg").textContent = "Queued " + data.queued + " · " + data.needs_review + " need order review.\n" + lines.join("\n");
    document.querySelectorAll("#browse-list .pick").forEach((cb) => { cb.checked = false; });
    $("select-all").checked = false;
    showTab("queue");
  }

  /* --- Job: create / render / reorder --- */
  async function startJob(folder) {
    const { data } = await api("/api/jobs", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ folder }) });
    if (!data || !data.ok) {
      alert("Could not start job: " + ((data && (data.error || (data.job && data.job.error))) || "unknown error"));
      return;
    }
    currentJob = data.job;
    if (data.route === "auto_queue") {
      alert("✓ Queued automatically — 1 file, no sorting needed. Watch it in the Queue tab.");
      showTab("queue");
    } else {
      showTab("job");
      renderJob();
    }
  }
  function fmtDur(s) { s = Math.round(s || 0); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }

  function renderJob() {
    if (!currentJob) { $("job-empty").classList.remove("hidden"); $("job-body").classList.add("hidden"); return; }
    $("job-empty").classList.add("hidden");
    $("job-body").classList.remove("hidden");
    const j = currentJob;
    $("job-title").textContent = (j.metadata && j.metadata.title) || j.folder;
    $("job-status").innerHTML = "Status: <span class='badge " + esc(j.status) + "'>" + esc(j.status.toUpperCase()) + "</span>"
      + (j.queue_position ? " · Queue #" + j.queue_position : "")
      + " · " + (j.files || []).length + " file(s)"
      + (j.output_path ? " · Output: " + esc(j.output_path) : "");
    const prog = $("job-progress-wrap");
    if (["queued", "building", "done", "failed", "cancelled", "needs_health_review", "rejected"].includes(j.status)) {
      prog.classList.remove("hidden");
      $("job-progress-fill").style.width = Math.max(0, Math.min(100, j.progress_pct || 0)) + "%";
      $("job-progress-pct").textContent = (j.progress_pct || 0) + "%";
      $("job-progress-stage").textContent = stageText(j);
    } else prog.classList.add("hidden");
    const err = $("job-error");
    if (j.status === "failed" && j.error) { err.classList.remove("hidden"); err.innerHTML = "<strong>⛔ FAILED:</strong> " + esc(j.error); }
    else if (j.status === "cancelled") { err.classList.remove("hidden"); err.innerHTML = "<strong>✕ CANCELLED:</strong> " + esc(j.cancelled_reason || "Cancelled by user."); }
    else { err.classList.add("hidden"); }
    const hb = $("job-health");
    if (j.status === "needs_health_review" && j.health) {
      hb.classList.remove("hidden");
      hb.innerHTML = "<div class='banner warn'><strong>🔍 HELD FOR HEALTH REVIEW:</strong> expected "
        + esc(j.health.expected_total || "?") + ", got " + esc(j.health.actual_total || "?") + ".<ul>"
        + (j.health.diagnoses || []).map((d) => "<li>" + esc(d) + "</li>").join("") + "</ul>"
        + "Open the 🔍 Review tab to approve it or reject it.</div>";
    } else hb.classList.add("hidden");
    $("cancel-job-btn").classList.toggle("hidden", !["queued", "building", "created"].includes(j.status));
    $("build-btn").textContent = j.status === "created" ? "🔨 Build .m4b (joins the queue)" : "🔨 Build .m4b (joins the queue)";
    const warns = j.warnings || [];
    $("sort-warnings").innerHTML = warns.length
      ? '<div class="banner warn"><strong>Auto-sort warnings — please check:</strong><ul>' + warns.map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul></div>" : "";
    const m = j.metadata || {};
    $("f-title").value = m.title || ""; $("f-author").value = m.author || "";
    $("f-narrator").value = m.narrator || ""; $("f-series").value = m.series || "";
    $("f-series-pos").value = m.series_position || ""; $("f-year").value = m.year || "";
    $("f-genre").value = m.genre || ""; $("f-asin").value = m.asin || "";
    $("f-desc").value = m.description || ""; $("asin").value = m.asin || "";
    const cover = $("cover-preview");
    if (m.cover_url) { cover.src = m.cover_url; cover.classList.remove("hidden"); $("cover-msg").textContent = "Cover from audnexus — will be embedded in the .m4b."; }
    else { cover.classList.add("hidden"); $("cover-msg").textContent = ""; }
    $("file-count").textContent = String((j.files || []).length);
    $("text-job-log").href = "/logs/" + j.id + "/text";
    renderFileList();
    if (j.status === "building" || j.status === "queued") startPolling(); else stopPolling();
  }

  function renderFileList() {
    const list = $("file-list");
    list.innerHTML = "";
    (currentJob.files || []).forEach((f, idx) => {
      const li = document.createElement("li");
      li.className = "filerow";
      li.draggable = true;
      li.dataset.idx = String(idx);
      li.innerHTML =
        "<div><span class='filename'>#" + (idx + 1) + " — " + esc(f.name) + "</span>"
        + "<div class='meta-line'>" + fmtDur(f.duration) + " · " + esc(f.codec || "")
        + " · " + (f.size ? (f.size / 1e6).toFixed(1) + " MB" : "") + "</div>"
        + "<div><span class='badge " + esc(f.confidence || "low") + "'>" + esc((f.confidence || "low").toUpperCase()) + "</span>"
        + "<span class='meta-line'>" + esc(f.reason || "") + "</span></div>"
        + (f.outcome && f.outcome.indexOf("FAILED") !== -1 ? "<div class='banner error'>" + esc(f.outcome) + "</div>" : "")
        + "<label>Chapter title <input class='chapter-title' value='" + esc(f.chapter_title || f.name.replace(/\.[^.]+$/, "")) + "'></label></div>"
        + "<div class='file-controls'>"
        + "<button class='secondary' data-act='up' aria-label='Move up'>↑</button>"
        + "<button class='secondary' data-act='down' aria-label='Move down'>↓</button>"
        + "<span class='move-to'><label>Move to #</label><input type='number' min='1' max='" + (currentJob.files.length) + "' value='" + (idx + 1) + "' aria-label='Move to position'><button class='secondary' data-act='goto'>Go</button></span>"
        + "<button class='danger' data-act='remove'>✕ Remove</button>"
        + "</div>";
      li.querySelector("[data-act='up']").addEventListener("click", () => moveFile(idx, idx - 1));
      li.querySelector("[data-act='down']").addEventListener("click", () => moveFile(idx, idx + 1));
      li.querySelector("[data-act='goto']").addEventListener("click", () => {
        const n = parseInt(li.querySelector(".move-to input").value, 10);
        if (n >= 1 && n <= currentJob.files.length) moveFile(idx, n - 1);
      });
      li.querySelector("[data-act='remove']").addEventListener("click", () => {
        if (confirm("Remove " + f.name + " from this book? (The file stays on the server.)")) {
          currentJob.files.splice(idx, 1); renderFileList();
        }
      });
      li.querySelector(".chapter-title").addEventListener("change", (e) => { f.chapter_title = e.target.value; });
      li.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", String(idx)); li.classList.add("dragging"); });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      li.addEventListener("dragover", (e) => e.preventDefault());
      li.addEventListener("drop", (e) => {
        e.preventDefault();
        const from = parseInt(e.dataTransfer.getData("text/plain"), 10);
        if (!isNaN(from)) moveFile(from, idx);
      });
      let touchY = null, touchFrom = null;
      li.addEventListener("touchstart", (e) => { touchY = e.touches[0].clientY; touchFrom = idx; }, { passive: true });
      li.addEventListener("touchmove", (e) => {
        if (touchY === null) return;
        const dy = e.touches[0].clientY - touchY;
        if (Math.abs(dy) > 64) {
          const to = touchFrom + (dy > 0 ? 1 : -1);
          touchY = e.touches[0].clientY; touchFrom = to;
          moveFile(idx, to);
        }
      }, { passive: true });
      li.addEventListener("touchend", () => { touchY = null; touchFrom = null; });
      list.appendChild(li);
    });
  }
  function moveFile(from, to) {
    const files = currentJob.files;
    if (from < 0 || from >= files.length || to < 0 || to >= files.length || from === to) return;
    const [item] = files.splice(from, 1);
    files.splice(to, 0, item);
    files.forEach((f, i) => { f.guessed_position = i + 1; });
    renderFileList();
  }
  function collectMetadata() {
    return {
      title: $("f-title").value.trim(), author: $("f-author").value.trim(),
      narrator: $("f-narrator").value.trim(), series: $("f-series").value.trim(),
      series_position: $("f-series-pos").value.trim(), year: $("f-year").value.trim(),
      genre: $("f-genre").value.trim(), asin: $("f-asin").value.trim() || $("asin").value.trim(),
      description: $("f-desc").value.trim(),
      cover_url: (currentJob.metadata && currentJob.metadata.cover_url) || "",
    };
  }

  $("lookup-btn").addEventListener("click", async () => {
    const asin = $("asin").value.trim();
    $("lookup-msg").textContent = "Looking up… (audnexus)";
    const { data } = await api("/api/jobs/" + currentJob.id + "/metadata", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ asin, title: $("f-title").value.trim(), author: $("f-author").value.trim() }),
    });
    if (data && data.ok) {
      currentJob.metadata = data.metadata;
      $("lookup-msg").textContent = "✓ Found: " + data.metadata.title + " — you can still edit every field below.";
      renderJob();
    } else {
      $("lookup-msg").textContent = "✕ Lookup failed: " + ((data && data.error) || "unknown error") + " — edit the fields manually and build anyway.";
    }
  });

  $("build-btn").addEventListener("click", async () => {
    document.querySelectorAll("#file-list .filerow").forEach((li, i) => {
      const input = li.querySelector(".chapter-title");
      if (input && currentJob.files[i]) currentJob.files[i].chapter_title = input.value;
    });
    // A queued job's fields can be saved from the queue row's Edit
    // first; here the Build button confirms a reviewed book.
    const { data } = await api("/api/jobs/" + currentJob.id + "/confirm", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ files: currentJob.files, metadata: collectMetadata() }),
    });
    if (data && data.ok) { currentJob = data.job; renderJob(); showTab("queue"); }
    else alert("Could not queue build: " + ((data && data.error) || "unknown error"));
  });
  $("cancel-job-btn").addEventListener("click", async () => {
    if (!currentJob) return;
    if (!confirm("Cancel this job? A running encode stops and any partial output is deleted. Source files are NOT touched.")) return;
    const { data } = await api("/api/queue/" + currentJob.id + "/cancel", { method: "POST" });
    if (data && data.ok) { currentJob = data.job; renderJob(); loadQueue(); }
    else alert("Could not cancel: " + ((data && data.message) || "unknown error"));
  });
  $("refresh-job-btn").addEventListener("click", async () => {
    if (!currentJob) return;
    const { data } = await api("/api/jobs/" + currentJob.id);
    if (data && data.ok) { currentJob = data.job; renderJob(); loadJobLog(); }
  });

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(async () => {
      if (!currentJob) return stopPolling();
      const { data } = await api("/api/jobs/" + currentJob.id);
      if (data && data.ok) {
        const was = currentJob.status;
        currentJob = data.job;
        renderJob();
        loadJobLog();
        if (["done", "failed", "cancelled"].includes(currentJob.status)) { stopPolling(); loadHealth(); }
        else if (was !== currentJob.status) loadHealth();
      }
    }, 2000);
  }
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  async function loadJobLog() {
    if (!currentJob) return;
    const res = await fetch("/api/jobs/" + currentJob.id + "/log?format=text");
    $("job-log").textContent = await res.text() || "No log lines yet.";
  }
  $("copy-job-log").addEventListener("click", async (e) => {
    if (!currentJob) return;
    const res = await fetch("/api/jobs/" + currentJob.id + "/log?format=pasteable");
    await copyText(await res.text(), $("copy-msg"));
    flashCopied(e.currentTarget);
  });
  $("copy-job-error").addEventListener("click", async (e) => {
    if (!currentJob) return;
    const res = await fetch("/api/jobs/" + currentJob.id + "/log?format=last-error");
    await copyText(await res.text(), $("copy-msg"));
    flashCopied(e.currentTarget);
  });
  $("download-job-log").addEventListener("click", () => {
    if (!currentJob) return;
    window.location = "/api/jobs/" + currentJob.id + "/log?download=1";
  });

  /* --- Queue tab --- */
  async function loadQueue() {
    const { data } = await api("/api/queue");
    const qlist = $("queue-list"), rlist = $("review-list");
    if (!data || !data.ok) { qlist.innerHTML = "<li class='jobrow'>Could not load queue.</li>"; return; }
    const queue = data.queue || [];
    const review = data.needs_review || [];
    qlist.innerHTML = "";
    if (!queue.length) qlist.innerHTML = "<li class='jobrow'>Queue is empty — pick folders in Browse to fill it.</li>";
    queue.forEach((j) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      const title = (j.metadata && j.metadata.title) || j.folder;
      li.innerHTML = "<span class='filename'>#" + (j.queue_position || "?") + " — " + esc(title) + "</span> "
        + "<span class='badge " + esc(j.status) + "'>" + esc(j.status.toUpperCase()) + "</span>"
        + progressBar(j.progress_pct, stageText(j))
        + "<div class='meta-line'>" + esc(j.created || "") + " · " + (j.files || []).length + " file(s)"
        + (j.output_path ? " · " + esc(j.output_path) : "") + "</div>"
        + (j.status === "failed" && j.error ? "<div class='banner error'><strong>⛔ FAILED:</strong> " + esc(j.error) + "</div>" : "")
        + (j.originals_warning ? "<div class='banner warn'><strong>⚠ Done with a warning:</strong> " + esc(j.originals_warning) + "</div>" : "")
        + "<div class='row'><button class='secondary' data-edit='1'>✏️ Edit</button>"
        + "<button class='danger' data-cancel='1'>" + (j.status === "building" ? "✕ Cancel" : "✕ Clear") + "</button></div>";
      li.querySelector("[data-edit]").addEventListener("click", () => { currentJob = j; showTab("job"); renderJob(); loadJobLog(); });
      li.querySelector("[data-cancel]").addEventListener("click", async () => {
        if (j.status === "building") {
          if (!confirm("Cancel the running encode for '" + title + "'? Partial output is deleted; source files are NOT touched.")) return;
          await api("/api/queue/" + j.id + "/cancel", { method: "POST" });
        } else {
          await api("/api/queue/" + j.id, { method: "DELETE" });
        }
        loadQueue(); loadHealth();
      });
      qlist.appendChild(li);
    });
    rlist.innerHTML = "";
    if (!review.length) rlist.innerHTML = "<li class='jobrow'>Nothing waiting for review.</li>";
    review.forEach((j) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      const title = (j.metadata && j.metadata.title) || j.folder;
      li.innerHTML = "<span class='filename'>" + esc(title) + "</span> "
        + "<span class='badge created'>NEEDS REVIEW</span>"
        + "<div class='meta-line'>" + (j.files || []).length + " file(s) — check the order, then confirm to queue it.</div>"
        + "<div class='row'><button class='secondary' data-open='1'>📖 Review &amp; confirm</button>"
        + "<button class='danger' data-clear='1'>✕ Clear</button></div>";
      li.querySelector("[data-open]").addEventListener("click", () => { currentJob = j; showTab("job"); renderJob(); });
      li.querySelector("[data-clear]").addEventListener("click", async () => {
        await api("/api/queue/" + j.id, { method: "DELETE" });
        loadQueue(); loadHealth();
      });
      rlist.appendChild(li);
    });
  }
  $("queue-refresh").addEventListener("click", loadQueue);
  $("clear-finished").addEventListener("click", async () => {
    const { data } = await api("/api/queue/clear", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ which: "finished" }) });
    $("queue-msg").textContent = data && data.ok ? "✓ Cleared " + data.removed + " finished job(s)." : "✕ " + ((data && data.error) || "failed");
    loadQueue(); loadJobs(); loadHealth();
  });
  $("clear-failed").addEventListener("click", async () => {
    const { data } = await api("/api/queue/clear", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ which: "failed" }) });
    $("queue-msg").textContent = data && data.ok ? "✓ Cleared " + data.removed + " failed job(s)." : "✕ " + ((data && data.error) || "failed");
    loadQueue(); loadJobs(); loadHealth();
  });
  function startQueuePolling() {
    if (queueTimer) return;
    queueTimer = setInterval(async () => {
      const { data } = await api("/api/queue");
      if (!data || !data.ok) return;
      const active = (data.queue || []).length > 0;
      if (!active) return;
      if (!$("tab-queue").classList.contains("hidden")) loadQueue();
      if (currentJob && ["queued", "building"].includes(currentJob.status)) {
        const updated = (data.queue || []).find((j) => j.id === currentJob.id);
        if (updated) { currentJob = updated; renderJob(); }
      }
      loadHealth();
    }, 2000);
  }

  /* --- Health Review tab --- */
  let reviewData = null;
  let reviewAction = null; // { jobId, action: "approve" | "reject" }
  function healthTable(h) {
    const rows = (h && h.chapters) || [];
    if (!rows.length) return "<p class='hint'>No chapter breakdown in this report.</p>";
    return "<div class='tablewrap'><table class='health'><thead><tr>"
      + "<th>#</th><th>Chapter</th><th>Expected</th><th>Actual</th><th>Starts at</th><th>Status</th></tr></thead><tbody>"
      + rows.map((c) => "<tr class='" + (c.status === "SHORT" ? "short" : c.status === "MISSING" ? "missing" : "") + "'>"
        + "<td>" + esc(c.number) + "</td>"
        + "<td>" + esc(c.title) + (c.expected_title && c.expected_title !== c.title ? "<br><span class='meta-line'>expected: " + esc(c.expected_title) + "</span>" : "") + "</td>"
        + "<td>" + (c.expected_sec ? fmtDur(c.expected_sec) : "—") + "</td>"
        + "<td>" + (c.actual_sec == null ? "—" : fmtDur(c.actual_sec)) + "</td>"
        + "<td>" + esc(c.start_timestamp || "") + "</td>"
        + "<td><strong>" + esc(c.status) + "</strong>" + (c.note ? "<br><span class='meta-line'>" + esc(c.note) + "</span>" : "") + "</td></tr>").join("")
      + "</tbody></table></div>";
  }
  async function loadReview() {
    const { data } = await api("/api/review");
    const list = $("review-list");
    if (!data || !data.ok) {
      $("review-msg").textContent = "✕ Could not load Review: " + ((data && data.error) || "unknown error");
      list.innerHTML = "";
      return;
    }
    reviewData = data.review || [];
    $("review-msg").textContent = reviewData.length
      ? reviewData.length + " book(s) waiting for a decision · Review folder: " + (data.review_dir || "(not set)")
      : "";
    list.innerHTML = "";
    if (!reviewData.length) list.innerHTML = "<li class='jobrow'>Nothing waiting for review — every finished file passed its health check.</li>";
    reviewData.forEach((j) => {
      const h = j.health_report || j.health || {};
      const title = (j.metadata && j.metadata.title) || j.folder;
      const verdict = (h.verdict || "unknown").toUpperCase();
      const li = document.createElement("li");
      li.className = "jobrow";
      li.innerHTML = "<span class='filename'>" + esc(title) + "</span> "
        + "<span class='badge needs_health_review'>" + esc(verdict) + "</span>"
        + "<div class='meta-line'>Expected <strong>" + esc(h.expected_total || "?")
        + "</strong> · got <strong>" + esc(h.actual_total || "?") + "</strong>"
        + (h.missing ? " · <strong>" + esc(h.missing) + " missing</strong>" : "")
        + (h.limited ? " · <em>limited check — no metadata runtime</em>" : "") + "</div>"
        + "<div class='meta-line'>Held file: " + esc(j.review_path || "(unknown)") + "</div>"
        + "<div class='meta-line'>Would go to: " + esc(j.output_path || "(unknown)") + "</div>"
        + "<div class='banner warn'><strong>What the health check thinks is wrong:</strong><ul>"
        + ((h.diagnoses || ["No details in this report."]).map((d) => "<li>" + esc(d) + "</li>").join("")) + "</ul></div>"
        + healthTable(h)
        + "<div class='row'><button data-approve='1'>✓ Looks OK — place in Plex</button>"
        + "<button class='danger' data-reject='1'>🗑 Reject — delete held file</button>"
        + "<button class='secondary' data-rerun='1'>↻ Re-run health check</button></div>"
        + "<p class='meta-line'>Originals stay in their source folder while the file is held — approving moves them to Processed; rejecting only deletes the held .m4b, never the sources.</p>";
      li.querySelector("[data-approve]").addEventListener("click", () => openReviewModal(j, "approve"));
      li.querySelector("[data-reject]").addEventListener("click", () => openReviewModal(j, "reject"));
      li.querySelector("[data-rerun]").addEventListener("click", async () => {
        const { data: rdata } = await api("/api/jobs/" + j.id + "/health/rerun", { method: "POST" });
        if (rdata && rdata.ok) { $("review-msg").textContent = "✓ Health check re-run: " + (rdata.health.verdict || "?").toUpperCase() + " — still held until you approve or reject it."; loadReview(); loadHealth(); }
        else $("review-msg").textContent = "✕ Re-run failed: " + ((rdata && rdata.error) || "unknown error");
      });
      list.appendChild(li);
    });
  }
  function openReviewModal(job, action) {
    reviewAction = { jobId: job.id, action };
    const h = job.health_report || job.health || {};
    const title = (job.metadata && job.metadata.title) || job.folder;
    const confirmBtn = $("review-modal-confirm");
    if (action === "approve") {
      $("review-modal-title").textContent = "✓ Approve — place in Plex?";
      $("review-modal-summary").innerHTML = "This moves <strong>" + esc(title) + "</strong> from the Review folder into Plex at:<br><code>"
        + esc(job.output_path || "(unknown)") + "</code><br>Your approval is recorded on the job, including what the health check flagged. Only now do the originals move to Processed.";
      confirmBtn.textContent = "✓ Yes, place it in Plex";
      confirmBtn.classList.remove("danger");
    } else {
      $("review-modal-title").textContent = "🗑 Reject — delete the held file?";
      $("review-modal-summary").innerHTML = "This deletes the held file <strong>" + esc(title) + "</strong>:<br><code>"
        + esc(job.review_path || "(unknown)") + "</code><br>The originals stay in their <strong>original source folder</strong> so you can re-process the book from there. This cannot be undone.";
      confirmBtn.textContent = "✓ Yes, delete the held file";
      confirmBtn.classList.add("danger");
    }
    $("review-modal-diagnoses").innerHTML = "<div class='banner warn'><strong>Health check said:</strong><ul>"
      + ((h.diagnoses || ["No details."]).map((d) => "<li>" + esc(d) + "</li>").join("")) + "</ul></div>";
    $("review-modal-note").value = "";
    $("review-modal").classList.remove("hidden");
  }
  $("review-modal-cancel").addEventListener("click", () => { $("review-modal").classList.add("hidden"); reviewAction = null; });
  $("review-modal-confirm").addEventListener("click", async () => {
    if (!reviewAction) return;
    const btn = $("review-modal-confirm");
    btn.disabled = true;
    const note = $("review-modal-note").value.trim();
    const { data } = await api("/api/jobs/" + reviewAction.jobId + "/" + reviewAction.action,
      { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ note }) });
    btn.disabled = false;
    $("review-modal").classList.add("hidden");
    if (data && data.ok) {
      $("review-msg").textContent = reviewAction.action === "approve"
        ? "✓ Placed in Plex. Rescan the Plex audiobook library to see it."
        : "✓ Held file deleted. The originals are still in their source folder if you want to re-process.";
    } else {
      $("review-msg").textContent = "✕ " + ((data && data.error) || "unknown error");
    }
    reviewAction = null;
    loadReview(); loadHealth(); loadJobs();
  });
  $("review-refresh").addEventListener("click", loadReview);

  /* --- Cleanup tab --- */
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
    return n + " B";
  }
  let cleanupData = null;
  async function loadCleanup() {
    const showKept = $("cleanup-show-kept").checked;
    const { data } = await api("/api/cleanup?show_kept=" + (showKept ? "1" : "0"));
    const safeList = $("cleanup-safe-list"), reviewList = $("cleanup-review-list"),
      keptList = $("cleanup-kept-list");
    if (!data || !data.ok) {
      $("cleanup-msg").textContent = "✕ Could not load cleanup: " + ((data && data.error) || "unknown error");
      return;
    }
    cleanupData = data;
    $("cleanup-total").textContent = fmtBytes(data.total_reclaimable_bytes)
      + " across " + data.total_reclaimable_count + " book(s)"
      + " · Processed folder: " + (data.processed_dir || "(not set)");
    $("cleanup-msg").textContent = "";
    safeList.innerHTML = "";
    if (!data.safe.length) safeList.innerHTML = "<li class='jobrow'>Nothing safe to delete right now.</li>";
    data.safe.forEach((item) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      li.innerHTML = "<span class='filename'>" + esc(item.title) + "</span>"
        + (item.author ? " <span class='meta-line'>by " + esc(item.author) + "</span>" : "")
        + " <span class='badge done'>SAFE</span>"
        + "<div class='meta-line'><strong>" + fmtBytes(item.processed_size) + "</strong> · "
        + (item.files || []).length + " source file(s) · "
        + "completed: " + (item.output_duration ? fmtDur(item.output_duration) : "?")
        + " (diff " + (item.diff_pct == null ? "?" : item.diff_pct + "%") + ") · "
        + "job " + esc(item.job_date || "") + "</div>"
        + "<div class='meta-line'>Processed: " + esc(item.processed_path) + "</div>"
        + "<div class='meta-line'>Finished: " + esc(item.output_path) + "</div>"
        + "<div class='row'><button class='secondary' data-keep='1'>✕ Keep — take off the delete list</button></div>";
      li.querySelector("[data-keep]").addEventListener("click", async () => {
        await api("/api/cleanup/keep", { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ processed_path: item.processed_path, keep: true }) });
        loadCleanup();
      });
      safeList.appendChild(li);
    });
    reviewList.innerHTML = "";
    if (!data.needs_review.length) reviewList.innerHTML = "<li class='jobrow'>Nothing waiting for review.</li>";
    data.needs_review.forEach((item) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      li.innerHTML = "<span class='filename'>" + esc(item.title) + "</span>"
        + (item.author ? " <span class='meta-line'>by " + esc(item.author) + "</span>" : "")
        + " <span class='badge low'>NOT SAFE</span>"
        + "<div class='meta-line'><strong>" + fmtBytes(item.processed_size) + "</strong> · "
        + (item.files || []).length + " source file(s)</div>"
        + "<div class='meta-line'>Processed: " + esc(item.processed_path) + "</div>"
        + "<div class='banner warn'><strong>Why not safe:</strong><ul>"
        + (item.reasons || ["Unknown reason"]).map((r) => "<li>" + esc(r) + "</li>").join("") + "</ul></div>";
      reviewList.appendChild(li);
    });
    keptList.innerHTML = "";
    const kept = data.kept || [];
    if (!showKept) keptList.innerHTML = "<li class='jobrow'>Tick “Show kept items” above to see them.</li>";
    else if (!kept.length) keptList.innerHTML = "<li class='jobrow'>No kept items.</li>";
    kept.forEach((item) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      li.innerHTML = "<span class='filename'>" + esc(item.title) + "</span>"
        + " <span class='meta-line'>" + fmtBytes(item.processed_size) + " · kept</span>"
        + "<div class='row'><button class='secondary' data-unkeep='1'>↩ Un-keep — back on the delete list if still safe</button></div>";
      li.querySelector("[data-unkeep]").addEventListener("click", async () => {
        await api("/api/cleanup/keep", { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ processed_path: item.processed_path, keep: false }) });
        loadCleanup();
      });
      keptList.appendChild(li);
    });
    const delBtn = $("cleanup-delete");
    delBtn.disabled = !data.safe.length;
    delBtn.textContent = data.safe.length
      ? "🗑 Delete " + data.safe.length + " safe item(s) (" + fmtBytes(data.total_reclaimable_bytes) + ")"
      : "🗑 Delete safe items";
  }
  $("cleanup-refresh").addEventListener("click", loadCleanup);
  $("cleanup-show-kept").addEventListener("change", loadCleanup);
  $("cleanup-delete").addEventListener("click", () => {
    if (!cleanupData || !cleanupData.safe.length) return;
    const total = fmtBytes(cleanupData.total_reclaimable_bytes);
    $("delete-summary").innerHTML = "This will delete <strong>" + cleanupData.safe.length
      + " book(s)</strong>, freeing <strong>" + total + "</strong>. "
      + "Only the processed source files listed below — the finished .m4b files stay in Plex.";
    const list = $("delete-list");
    list.innerHTML = "";
    cleanupData.safe.forEach((item) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      li.innerHTML = "<span class='filename'>" + esc(item.title) + "</span>"
        + " <span class='meta-line'>" + fmtBytes(item.processed_size) + " · " + esc(item.processed_path) + "</span>";
      list.appendChild(li);
    });
    $("delete-modal").classList.remove("hidden");
  });
  $("delete-cancel").addEventListener("click", () => $("delete-modal").classList.add("hidden"));
  $("delete-confirm").addEventListener("click", async () => {
    const paths = (cleanupData.safe || []).map((i) => i.processed_path);
    $("delete-confirm").disabled = true;
    const { data } = await api("/api/cleanup/delete", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ processed_paths: paths }) });
    $("delete-confirm").disabled = false;
    $("delete-modal").classList.add("hidden");
    if (data && data.ok) {
      $("cleanup-msg").textContent = "✓ Deleted " + (data.deleted || []).length
        + " item(s), freed " + fmtBytes(data.freed_bytes) + ".";
    } else {
      $("cleanup-msg").textContent = "✕ Delete stopped: " + ((data && data.error) || "unknown error")
        + ((data && data.deleted && data.deleted.length) ? " (" + data.deleted.length + " item(s) were already deleted before it stopped.)" : "");
    }
    loadCleanup();
  });

  /* --- Jobs list --- */
  async function loadJobs() {
    const { data } = await api("/api/jobs");
    const list = $("jobs-list");
    if (!data || !data.ok) { list.innerHTML = "<li class='jobrow'>Could not load jobs.</li>"; return; }
    list.innerHTML = "";
    if (!data.jobs.length) list.innerHTML = "<li class='jobrow'>No jobs yet.</li>";
    data.jobs.forEach((j) => {
      const li = document.createElement("li");
      li.className = "jobrow";
      const title = (j.metadata && j.metadata.title) || j.folder;
      li.innerHTML = "<span class='filename'>" + esc(title) + "</span> "
        + "<span class='badge " + esc(j.status) + "'>" + esc(j.status.toUpperCase()) + "</span>"
        + (["queued", "building", "done", "failed", "cancelled"].includes(j.status) ? progressBar(j.progress_pct, stageText(j)) : "")
        + "<div class='meta-line'>" + esc(j.created || "") + " · " + (j.files || []).length + " file(s)"
        + (j.output_path ? " · " + esc(j.output_path) : "") + "</div>"
        + (j.status === "failed" && j.error ? "<div class='banner error'><strong>⛔ FAILED:</strong> " + esc(j.error) + "</div>" : "")
        + (j.originals_warning ? "<div class='banner warn'><strong>⚠ Done with a warning:</strong> " + esc(j.originals_warning) + "</div>" : "")
        + (j.status === "cancelled" && j.cancelled_reason ? "<div class='meta-line'>✕ Cancelled: " + esc(j.cancelled_reason) + "</div>" : "")
        + (j.status === "needs_health_review" && j.health ? "<div class='banner warn'><strong>🔍 HELD FOR HEALTH REVIEW:</strong> expected "
          + esc(j.health.expected_total || "?") + ", got " + esc(j.health.actual_total || "?")
          + (j.health.missing ? " — " + esc(j.health.missing) + " missing" : "")
          + ". Open it in the Review tab to approve or reject it.</div>" : "")
        + (j.status === "rejected" ? "<div class='meta-line'>🗑 Rejected — held file deleted; originals stay in their source folder.</div>" : "")
        + "<div class='row'><button class='secondary'>Open</button>"
        + "<a href='/logs/" + j.id + "/text' target='_blank'><button class='secondary' type='button'>📄 Plain-text log</button></a>"
        + (j.status === "building"
            ? "<button class='danger' data-cancel='1'>✕ Cancel</button>"
            : j.status === "needs_health_review"
              ? "<button data-review='1'>🔍 Open in Review</button>"
              : "<button class='danger' data-clear='1'>✕ Clear</button>") + "</div>";
      li.querySelector("button.secondary").addEventListener("click", () => { currentJob = j; showTab("job"); renderJob(); loadJobLog(); });
      const reviewBtn = li.querySelector("[data-review]");
      if (reviewBtn) reviewBtn.addEventListener("click", () => showTab("review"));
      const cancelBtn = li.querySelector("[data-cancel]");
      if (cancelBtn) cancelBtn.addEventListener("click", async () => {
        await api("/api/queue/" + j.id + "/cancel", { method: "POST" });
        loadJobs(); loadQueue(); loadHealth();
      });
      const clearBtn = li.querySelector("[data-clear]");
      if (clearBtn) clearBtn.addEventListener("click", async () => {
        await api("/api/queue/" + j.id, { method: "DELETE" });
        loadJobs(); loadQueue(); loadHealth();
      });
      list.appendChild(li);
    });
  }
  $("jobs-refresh").addEventListener("click", loadJobs);
  $("jobs-clear-finished").addEventListener("click", async () => {
    await api("/api/queue/clear", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ which: "finished" }) });
    loadJobs(); loadQueue(); loadHealth();
  });

  /* --- Logs view --- */
  async function loadLogList() {
    const { data } = await api("/api/jobs");
    const sel = $("log-job-select");
    sel.innerHTML = "<option value='app'>🌐 Global log (app.log)</option>";
    (data && data.jobs || []).forEach((j) => {
      const opt = document.createElement("option");
      opt.value = j.id;
      opt.textContent = j.id + " — " + ((j.metadata && j.metadata.title) || j.folder) + " [" + j.status + "]";
      sel.appendChild(opt);
    });
  }
  async function loadLogView() {
    const level = $("log-level").value, q = $("log-search").value.trim();
    let url;
    if (logJobId === "app") url = "/api/logs/app?format=text";
    else url = "/api/jobs/" + logJobId + "/log?format=text";
    const res = await fetch(url);
    let text = await res.text();
    const lines = text.split("\n");
    const filtered = lines.filter((ln) => {
      if (level && ln.indexOf(" " + level + " ") === -1 && ln.indexOf("| " + level + " |") === -1 && ln.indexOf(level) === -1 && level !== "") {
        if (ln.indexOf("===") === 0 || ln.indexOf("Job id:") === 0) return true;
        return false;
      }
      if (q && ln.toLowerCase().indexOf(q.toLowerCase()) === -1) return false;
      return true;
    });
    $("log-view").textContent = filtered.join("\n") || "(no log lines match)";
    $("text-log-link").href = logJobId === "app" ? "/logs/app/text" : "/logs/" + logJobId + "/text";
  }
  $("log-load").addEventListener("click", () => { logJobId = $("log-job-select").value; loadLogView(); });
  $("log-level").addEventListener("change", loadLogView);
  $("log-search").addEventListener("input", loadLogView);
  $("copy-log").addEventListener("click", async (e) => {
    const url = logJobId === "app" ? "/api/logs/app?format=pasteable" : "/api/jobs/" + logJobId + "/log?format=pasteable";
    await copyText(await (await fetch(url)).text(), $("copy-msg"));
    flashCopied(e.currentTarget);
  });
  $("copy-last-error").addEventListener("click", async (e) => {
    const url = logJobId === "app" ? "/api/logs/app?format=last-error" : "/api/jobs/" + logJobId + "/log?format=last-error";
    await copyText(await (await fetch(url)).text(), $("copy-msg"));
    flashCopied(e.currentTarget);
  });
  $("download-log").addEventListener("click", () => {
    window.location = logJobId === "app" ? "/api/logs/app?download=1" : "/api/jobs/" + logJobId + "/log?download=1";
  });

  /* --- Settings --- */
  async function loadSettings() {
    const { data } = await api("/api/settings");
    if (!data) return;
    $("s-inbox").value = data.inbox_dir || ""; $("s-output").value = data.output_dir || "";
    $("s-processed").value = data.processed_dir || "";
    $("s-review").value = data.review_dir || "";
    $("s-temp").value = data.temp_dir || ""; $("s-bitrate").value = data.bitrate || "96k";
    $("s-audnexus").value = data.audnexus_url || ""; $("s-keep").value = data.keep_originals || "processed";
    $("s-tz").value = data.timezone || "UTC";
    $("cur-inbox").textContent = data.inbox_dir || "(not set)";
    $("cur-output").textContent = data.output_dir || "(not set)";
    fillSuggestions($("suggest-inbox"), $("s-inbox"));
    fillSuggestions($("suggest-output"), $("s-output"));
    fillSuggestions($("suggest-processed"), $("s-processed"));
    fillSuggestions($("suggest-review"), $("s-review"));
    fillTzChips($("suggest-tz"), $("s-tz"));
  }
  $("save-settings").addEventListener("click", async () => {
    const { data } = await api("/api/settings", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ inbox_dir: $("s-inbox").value, output_dir: $("s-output").value, processed_dir: $("s-processed").value, review_dir: $("s-review").value, temp_dir: $("s-temp").value, bitrate: $("s-bitrate").value, audnexus_url: $("s-audnexus").value, timezone: $("s-tz").value.trim() || "UTC", keep_originals: $("s-keep").value, create_output: $("s-create-output").checked }) });
    if (data && data.ok) {
      $("settings-msg").textContent = "✓ Saved." + ((data.startup_errors || []).length ? " But startup still reports errors — see the banner at the top." : "");
      loadSettings();
    } else {
      const errs = (data && data.errors) || [{ message: (data && data.error) || "unknown error" }];
      $("settings-msg").textContent = "✕ Not saved: " + errs.map((e) => e.message).join(" ");
    }
    loadHealth();
  });

  loadHealth(); loadBrowse(); startQueuePolling();
})();
