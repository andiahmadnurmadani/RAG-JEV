/* RAG console. No build step, no CDN: plain DOM, one state object per surface.
   Three surfaces live here: the gate (one access code -> a session), the chat (upload, list,
   prompt) and the settings dialog (connection, access & sessions, API keys, LLM, Jev, file
   formats, retrieval). Everything the browser can decide alone is in localStorage; everything
   that changes the service goes through /settings. Two values are shown exactly once by the
   service and never again: a new API key and a new session token. */

"use strict";

const KEY = "rag.console.v3";
const SESSION_KEY = "rag.session.v1";
const DEFAULT_KB = "kb_chat";
const PANELS = ["conn", "access", "keys", "llm", "jev", "fmt", "retr", "web", "summary"];

const state = {
  base: "",
  key: "",
  session: "",
  sessionExpiresAt: "",
  currentSessionId: "",
  gate: null,
  sessions: [],
  kb: DEFAULT_KB,
  mode: "answer",
  picked: new Set(),
  docs: [],
  opts: { top_k: 12, threshold: 0.35, strict: true, hybrid: true, reranker: false, route: "" },
  limits: { max_mb: null, allowed: [], allowed_ext: [] },
  catalog: [],
  models: [],
  panel: "conn",
  keyContext: null,
};

/* --------------------------------------------------------------- utilities */

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Salin teks Markdown ke papan klip. Clipboard API hanya ada di konteks aman (https/localhost);
// di akses http biasa tombolnya tetap harus bekerja, jadi ada jalur cadangan textarea.
async function copyMarkdown(button, text) {
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      copied = true;
    }
  } catch (err) {
    copied = false;
  }
  if (!copied) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      copied = document.execCommand("copy");
    } catch (err) {
      copied = false;
    }
    document.body.removeChild(area);
  }
  const original = button.textContent;
  button.textContent = copied ? "Tersalin" : "Gagal menyalin";
  window.setTimeout(() => { button.textContent = original; }, 1600);
}

function mb(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function shortUrl(url) {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname;
    const text = parsed.hostname + path + (parsed.search || "");
    return text.length > 58 ? text.slice(0, 55) + "..." : text;
  } catch (err) {
    return String(url || "").slice(0, 58);
  }
}

function uploadLimitBytes() {
  const max = state.limits.max_mb;
  return typeof max === "number" && max > 0 ? max * 1024 * 1024 : null;
}

function extensionSummary(extensions, limit) {
  const shown = (extensions || []).slice(0, 8).map((ext) => ext.slice(1).toUpperCase());
  if (!shown.length) return limit ? "maksimal " + limit + " MB" : "";
  const rest = (extensions || []).length - shown.length;
  return shown.join(", ") + (rest > 0 ? " +" + rest + " lain" : "") + (limit ? " - maksimal " + limit + " MB" : "");
}

function allowedExtensions() {
  return Array.isArray(state.limits.allowed_ext) ? state.limits.allowed_ext : [];
}

function note(target, tone, html) {
  const node = typeof target === "string" ? $(target) : target;
  if (!node) return;
  const icon = tone === "ok" ? "i-check" : tone === "err" || tone === "warn" ? "i-warn" : "";
  node.innerHTML = '<div class="note ' + tone + '">' +
    (icon ? '<svg><use href="#' + icon + '"/></svg>' : "") + "<div>" + html + "</div></div>";
}

function clearNote(target) {
  const node = typeof target === "string" ? $(target) : target;
  if (node) node.innerHTML = "";
}

function slug(name, fallback) {
  const base = String(name || fallback || "dokumen").toLowerCase()
    .replace(/\.[a-z0-9]+$/, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 48) || "dokumen";
  return base + "_" + Math.random().toString(36).slice(2, 7);
}

/* -------------------------------------------------------------- transport */

/* Kredensial yang dipakai sekarang: API key yang sedang tertulis di panel Koneksi, atau
   sesi hasil kode akses. Dibaca saat permintaan dikirim - bukan saat disimpan - supaya key
   yang baru ditempel langsung terpakai tanpa harus menekan Simpan dulu (penyebab keluhan
   "Missing credentials" saat membuat kunci). */
function credential() {
  const field = $("set-key");
  const typed = field ? String(field.value || "").trim() : "";
  return typed || state.session || "";
}

function usingSession() {
  const field = $("set-key");
  const typed = field ? String(field.value || "").trim() : "";
  return !typed && !!state.session;
}

async function api(method, path, body, extraHeaders) {
  readConnInputs();
  const headers = Object.assign({ Accept: "application/json" }, extraHeaders || {});
  const bearer = credential();
  if (bearer) headers.Authorization = "Bearer " + bearer;
  const init = { method: method, headers: headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(state.base + path, init);
  const requestId = response.headers.get("X-Request-Id") || "";
  $("req-id").textContent = requestId;
  let payload = null;
  try {
    payload = await response.json();
  } catch (err) {
    throw { code: "BAD_RESPONSE", message: "Respons bukan JSON (HTTP " + response.status + ")" };
  }
  if (!response.ok || payload.success === false) {
    const error = (payload && payload.error) || {};
    const code = error.code || "HTTP_" + response.status;
    // Sesi yang sudah tidak berlaku: kunci layar, jangan biarkan operator menebak-nebak.
    if (code === "AUTH_INVALID" && usingSession()) {
      clearSession();
      showGate("Sesi Anda sudah berakhir. Masukkan kode akses lagi.");
    }
    throw { code: code, message: error.message || "Permintaan gagal", details: error.details || {} };
  }
  return payload.data;
}

/* ------------------------------------------------------------------- prefs */

function loadPrefs() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(KEY) || "null");
  } catch (err) {
    stored = null;
  }
  if (stored && typeof stored === "object") {
    state.key = stored.key || "";
    state.kb = stored.kb || DEFAULT_KB;
    state.mode = stored.mode === "search" ? "search" : "answer";
    state.opts = Object.assign(state.opts, stored.opts || {});
  }
}

function savePrefs() {
  localStorage.setItem(KEY, JSON.stringify({ key: state.key, kb: state.kb, mode: state.mode, opts: state.opts }));
}

function restore() {
  state.base = location.origin + "/api/v1";
  $("set-base").value = state.base;
  loadPrefs();
  loadSession();
  $("set-kb").value = state.kb;
  $("set-key").value = state.key;
  $("r-topk").value = state.opts.top_k;
  $("r-threshold").value = state.opts.threshold;
  $("r-route").value = state.opts.route;
  $("r-strict").checked = !!state.opts.strict;
  $("r-hybrid").checked = !!state.opts.hybrid;
  $("r-reranker").checked = !!state.opts.reranker;
  setMode(state.mode);
}

function readConnInputs() {
  state.base = $("set-base").value.trim().replace(/\/$/, "") || location.origin + "/api/v1";
  state.key = $("set-key").value.trim();
  state.kb = $("set-kb").value.trim() || DEFAULT_KB;
}

/* ------------------------------------------------------------------- gate */

/* Sesi: hasil menukar kode akses. Disimpan di localStorage supaya "ingat saya" bertahan
   sampai masa berlakunya habis; server tetap penentu terakhir (token bisa dikeluarkan). */

function loadSession() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
  } catch (err) {
    stored = null;
  }
  if (!stored || typeof stored !== "object" || !stored.token) return false;
  const expires = stored.expires_at ? new Date(stored.expires_at).getTime() : 0;
  if (expires && expires <= Date.now()) {
    localStorage.removeItem(SESSION_KEY);
    return false;
  }
  state.session = String(stored.token);
  state.sessionExpiresAt = stored.expires_at || "";
  return true;
}

function saveSession(token, expiresAt, remember) {
  state.session = token;
  state.sessionExpiresAt = expiresAt || "";
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ token: token, expires_at: expiresAt, remember: !!remember }));
  } catch (err) {
    /* localStorage bisa diblokir (mode privat): sesi tetap hidup sampai tab ditutup. */
  }
  const button = $("btn-logout");
  if (button) button.hidden = false;
}

function clearSession() {
  state.session = "";
  state.sessionExpiresAt = "";
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch (err) {
    /* diabaikan */
  }
  const button = $("btn-logout");
  if (button) button.hidden = true;
}

function showGate(reason) {
  const gate = $("gate");
  const shell = $("app-shell");
  if (!gate || !shell) return;
  shell.hidden = true;
  gate.hidden = false;
  if (reason) $("gate-sub").textContent = reason;
  const code = $("gate-code");
  if (code) {
    code.value = "";
    window.setTimeout(() => code.focus(), 30);
  }
}

function hideGate() {
  const gate = $("gate");
  const shell = $("app-shell");
  if (gate) gate.hidden = true;
  if (shell) shell.hidden = false;
}

async function loadGate() {
  try {
    const data = await api("GET", "/auth/gate");
    state.gate = data;
    if (data.remember_lifetime) $("gate-remember-days").textContent = data.remember_lifetime.label;
    if (data.default_lifetime) $("access-life-default").textContent = data.default_lifetime.label;
    if (data.remember_lifetime) $("access-life-remember").textContent = data.remember_lifetime.label;
    return data;
  } catch (err) {
    state.gate = null;
    return null;
  }
}

async function login(event) {
  if (event) event.preventDefault();
  const code = $("gate-code").value;
  if (!code) {
    note("gate-status", "err", "Kode akses masih kosong.");
    return;
  }
  $("btn-gate-login").disabled = true;
  try {
    const data = await api("POST", "/auth/login", { code: code, remember: $("gate-remember").checked });
    saveSession(data.token, data.expires_at, data.remember);
    $("gate-code").value = "";
    note("gate-status", "ok", "Sesi dibuka sampai <strong>" + escapeHtml(shortTime(data.expires_at)) + "</strong>.");
    hideGate();
    const ok = await checkConnection();
    if (ok) loadDocs();
  } catch (err) {
    const left = err.details && typeof err.details.attempts_left === "number" ? err.details.attempts_left : null;
    note("gate-status", "err", escapeHtml(err.message || "gagal masuk") +
      (left !== null && err.code === "AUTH_INVALID" ? " Sisa percobaan: <strong>" + left + "</strong>." : "") +
      " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  } finally {
    $("btn-gate-login").disabled = false;
  }
}

async function logout() {
  try {
    await api("DELETE", "/auth/session");
  } catch (err) {
    /* Sesi mungkin sudah tidak berlaku di server; tetap keluar di sisi peramban. */
  }
  clearSession();
  showGate("Anda sudah keluar. Masukkan kode akses untuk masuk lagi.");
  clearNote("gate-status");
}

/* ------------------------------------------------------------------- views */

function showView(name) {
  const settings = name === "settings";
  const sheet = $("dlg-settings");
  if (settings) {
    if (!sheet.open) sheet.showModal();
    selectPanel(state.panel);
    loadSettings();
    loadApiKeys();
    loadGate();
  } else if (sheet.open) {
    sheet.close();
  }
  if (currentView() !== name) location.hash = settings ? "#/settings" : "#/";
}

function selectPanel(name) {
  state.panel = PANELS.indexOf(name) === -1 ? "conn" : name;
  document.querySelectorAll("#settings-nav [data-panel]").forEach((button) => {
    const on = button.dataset.panel === state.panel;
    button.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("#settings-panels > [data-panel]").forEach((section) => {
    section.hidden = section.dataset.panel !== state.panel;
  });
  if (state.panel === "access") loadAccess();
}

function currentView() {
  return location.hash.replace(/^#\/?/, "") === "settings" ? "settings" : "chat";
}

/* --------------------------------------------------------------- documents */

function setConnection(tone, text) {
  $("conn-dot").className = "dot " + tone;
  $("conn-text").textContent = text;
}

async function checkConnection() {
  readConnInputs();
  if (!credential()) {
    setConnection("warn", "butuh kredensial");
    note("conn-status", "warn", "Masukkan kode akses, atau isi API key di panel Koneksi ini.");
    return false;
  }
  setConnection("", "menghubungkan...");
  try {
    const ready = await api("GET", "/ready");
    const detail = ready.detail || {};
    const deps = ready.dependencies || {};
    state.limits.max_mb = typeof detail.max_upload_mb === "number" ? detail.max_upload_mb : null;
    state.limits.allowed = detail.allowed_mime || [];
    state.limits.allowed_ext = detail.allowed_extensions || [];
    const bad = Object.keys(deps).filter((k) => deps[k] === "error");
    setConnection(bad.length ? "warn" : "ok", bad.length ? "sebagian" : "siap");
    note("conn-status", bad.length ? "warn" : "ok",
      "Terhubung. Model: <strong>" + escapeHtml(detail.llm_model || "-") + "</strong> - Jev: " +
      escapeHtml(detail.jev_mode || "-") + " (" + escapeHtml(detail.jev_provider || "-") + ")" +
      (bad.length ? ". Gangguan: " + escapeHtml(bad.join(", ")) : ""));
    $("drop-hint").textContent = extensionSummary(allowedExtensions(), state.limits.max_mb);
    $("file-input").setAttribute("accept", allowedExtensions().join(","));
    savePrefs();
    return true;
  } catch (err) {
    setConnection("err", err.code === "AUTH_INVALID" ? "kunci ditolak" : "gagal");
    note("conn-status", "err", escapeHtml(err.message || String(err)) + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
    return false;
  }
}

async function loadDocs() {
  if (!credential()) return;
  try {
    const data = await api("GET", "/knowledge?limit=200&knowledge_base_id=" + encodeURIComponent(state.kb));
    state.docs = data.documents || [];
    renderDocs(state.docs);
  } catch (err) {
    if (err.code !== "AUTH_INVALID") note("upload-status", "err", escapeHtml(err.message || "gagal memuat dokumen"));
  }
}

function renderDocs(documents) {
  const list = $("docs-list");
  const alive = new Set(documents.map((doc) => doc.document_id));
  state.picked.forEach((id) => { if (!alive.has(id)) state.picked.delete(id); });

  $("docs-count").textContent = String(documents.length);
  $("docs-empty").hidden = documents.length > 0;
  $("docs-tools").hidden = documents.length === 0;

  list.innerHTML = documents.map((doc) => {
    const picked = state.picked.has(doc.document_id) ? " picked" : "";
    const when = (doc.updated_at || doc.created_at || "").replace("T", " ").slice(0, 16);
    const tone = doc.status === "completed" ? "" : doc.status === "failed" ? " style=\"color:var(--err)\"" : " style=\"color:var(--warn)\"";
    return '<li class="doc' + picked + '">' +
      '<input type="checkbox" data-pick="' + escapeHtml(doc.document_id) + '"' + (picked ? " checked" : "") + ' aria-label="Batasi ke dokumen ini">' +
      '<div class="doc-main"><div class="doc-name">' + escapeHtml(doc.document_name || doc.document_id) + "</div>" +
      '<div class="doc-meta"><span class="mono">' + escapeHtml(doc.document_id) + "</span>" +
      "<span" + tone + ">" + escapeHtml(doc.status) + "</span>" +
      "<span>" + escapeHtml(doc.chunks) + " chunk, " + escapeHtml(doc.tokens) + " token</span>" +
      (doc.tables ? "<span class=\"doc-tables\">" + escapeHtml(doc.tables) + " tabel</span>" : "") +
      (doc.web_pages ? "<span class=\"doc-tables\">" + escapeHtml(doc.web_pages) + " halaman web</span>" : "") +
      (doc.summary
        ? "<span class=\"doc-summary\" title=\"" + escapeHtml((doc.summary || "").slice(0, 600)) + "\">" +
          "<button type=\"button\" class=\"linkish\" data-summary=\"" + escapeHtml(doc.document_id) + "\">" +
          "ringkasan " + escapeHtml(doc.summary_tokens || 0) + " token</button></span>"
        : (doc.summary_error ? "<span class=\"doc-summary muted\" title=\"" + escapeHtml(doc.summary_error) + "\">tanpa ringkasan</span>" : "")) +
      (when ? "<span>" + escapeHtml(when) + "</span>" : "") +
      (doc.source_url
        ? "<span><a href=\"" + escapeHtml(doc.source_url) + "\" target=\"_blank\" rel=\"noopener noreferrer\">" +
          escapeHtml(shortUrl(doc.source_url)) + "</a></span>"
        : "") +
      "</div></div>" +
      '<div class="doc-actions"><button type="button" data-del="' + escapeHtml(doc.document_id) + '" title="Hapus" aria-label="Hapus"><svg><use href="#i-trash"/></svg></button></div>' +
      "</li>";
  }).join("");

  list.querySelectorAll("[data-pick]").forEach((box) => {
    box.addEventListener("change", () => {
      if (box.checked) state.picked.add(box.dataset.pick);
      else state.picked.delete(box.dataset.pick);
      renderScope();
      const row = box.closest(".doc");
      if (row) row.classList.toggle("picked", box.checked);
    });
  });
  list.querySelectorAll("[data-del]").forEach((button) => {
    button.addEventListener("click", () => removeDoc(button.dataset.del));
  });
  list.querySelectorAll("[data-summary]").forEach((button) => {
    button.addEventListener("click", () => showSummary(button.dataset.summary));
  });
  renderScope();
}

async function showSummary(documentId) {
  const doc = (state.docs || []).find((item) => item.document_id === documentId);
  const name = doc ? doc.document_name || doc.document_id : documentId;
  const body = doc && doc.summary ? doc.summary : "(ringkasan tidak tersedia)";
  const noteText = doc && doc.summary_error ? "\n\nCatatan: " + doc.summary_error : "";
  const box = $("dlg-summary");
  if (!box) {
    // Tanpa dialog (mis. halaman lama): tampilkan seadanya, jangan diam-diam gagal.
    note("upload-status", "info", escapeHtml(name) + ": " + escapeHtml(body.slice(0, 400)));
    return;
  }
  $("summary-title").textContent = "Ringkasan: " + name;
  // Ringkasan juga diminta berformat Markdown; render agar judul/daftar/tabel terbaca.
  const summaryBody = $("summary-body");
  const markdownText = body + noteText;
  if (window.Markdown && typeof window.Markdown.render === "function") {
    summaryBody.classList.add("markdown");
    summaryBody.innerHTML = window.Markdown.render(markdownText);
  } else {
    summaryBody.textContent = markdownText;
  }
  box.showModal();
}

function renderScope() {
  const count = state.picked.size;
  $("scope-chip").innerHTML = count
    ? "dibatasi ke <b>" + count + " dokumen</b>"
    : "semua dokumen";
  $("scope-chip").title = count ? Array.from(state.picked).join(", ") : "";
}

async function trackJob(documentId, label) {
  // Isi dokumen tersimpan LEBIH DULU daripada ringkasannya (stage "summarizing"), tetapi status
  // baru "completed" setelah ringkasan selesai. Jadi beri tahu pemakai begitu isinya siap
  // dipakai, dan tetap tunggu sampai pekerjaannya benar-benar tuntas.
  let toldContentReady = false;
  for (let attempt = 0; attempt < 150; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    let status = null;
    try {
      status = await api("GET", "/knowledge/" + encodeURIComponent(documentId));
    } catch (err) {
      return;
    }
    if (status.stage === "summarizing" && !toldContentReady) {
      toldContentReady = true;
      note("upload-status", "info", escapeHtml(label) + ": " + escapeHtml(status.chunks) + " chunk sudah masuk dan bisa dipakai. Ringkasan sedang dibuat di latar belakang - unggahan lain tidak ikut menunggu.");
      loadDocs();
    }
    if (status.status === "completed") {
      const summaryNote = status.summary
        ? " Ringkasan: " + escapeHtml(status.summary_tokens || 0) + " token" +
          (status.summary_error ? " (" + escapeHtml(status.summary_error.slice(0, 120)) + ")" : "") + "."
        : (status.summary_error ? " Tanpa ringkasan: " + escapeHtml(status.summary_error.slice(0, 140)) + "." : "");
      note("upload-status", "ok", escapeHtml(label) + " selesai: " + escapeHtml(status.chunks) + " chunk, " +
        escapeHtml(status.tokens) + " token." + summaryNote);
      loadDocs();
      return;
    }
    if (status.status === "failed" || status.status === "deleted") {
      note("upload-status", "err", escapeHtml(label) + " gagal: " + escapeHtml((status.error || "tidak diketahui").slice(0, 240)));
      loadDocs();
      return;
    }
    if (status.stage === "queued" && attempt > 4) {
      note("upload-status", "info", escapeHtml(label) + " masih menunggu di antrian (ada dokumen lain yang sedang diproses).");
    }
  }
  note("upload-status", "warn", escapeHtml(label) + " masih diproses; buka daftar dokumen beberapa saat lagi.");
  loadDocs();
}

async function submitIndex(payload, label) {
  try {
    await api("POST", "/knowledge/index", payload);
    note("upload-status", "info", escapeHtml(label) + " diterima, sedang diindeks...");
    trackJob(payload.document_id, label);
  } catch (err) {
    note("upload-status", "err", escapeHtml(err.message || "gagal mengirim") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("berkas tidak bisa dibaca"));
    reader.readAsDataURL(file);
  });
}

function suffixOf(name) {
  const match = String(name || "").toLowerCase().match(/(\.[a-z0-9]+)$/);
  return match ? match[1] : "";
}

async function indexFile(file) {
  const allowed = allowedExtensions();
  const suffix = suffixOf(file.name);
  if (allowed.length && suffix && allowed.indexOf(suffix) === -1) {
    note("upload-status", "err", escapeHtml(file.name) + " berjenis <span class=\"mono\">" +
      escapeHtml(suffix) + "</span> tidak termasuk format yang diizinkan (" + escapeHtml(allowed.join(", ")) +
      "). Tidak dikirim sama sekali.");
    return;
  }
  const limit = uploadLimitBytes();
  if (limit && file.size > limit) {
    note("upload-status", "err", escapeHtml(file.name) + " berukuran " + mb(file.size) + ", melebihi batas " +
      mb(limit) + ". Tidak dikirim sama sekali - perkecil berkas dulu.");
    return;
  }
  note("upload-status", "info", "membaca " + escapeHtml(file.name) + " (" + mb(file.size) + ")...");
  let content = "";
  try {
    content = await fileToBase64(file);
  } catch (err) {
    note("upload-status", "err", escapeHtml(String(err.message || err)));
    return;
  }
  await submitIndex({
    document_id: slug(file.name),
    knowledge_base_id: state.kb,
    document_name: file.name,
    content_base64: content,
  }, file.name);
}

async function indexText(name, text) {
  await submitIndex({
    document_id: slug(name, "catatan"),
    knowledge_base_id: state.kb,
    document_name: name,
    text: text,
  }, name || "teks tempel");
}

async function indexUrl(url, name, options) {
  const opts = options || {};
  const payload = {
    document_id: slug(name || url, "tautan"),
    knowledge_base_id: state.kb,
    document_name: name || url.split("/").pop() || url,
    file_url: url,
  };
  if (opts.crawl) {
    // Satu halaman/situs menjelajah jadi satu dokumen; setiap halaman menyimpan URL-nya
    // sendiri sehingga sitasi menunjuk halaman yang benar.
    payload.web_url = url;
    payload.file_url = "";
    payload.web_max_pages = opts.maxPages || 20;
    payload.web_max_depth = opts.maxDepth == null ? 2 : opts.maxDepth;
    payload.document_name = name || url;
  }
  await submitIndex(payload, name || url);
}

async function removeDoc(documentId) {
  try {
    const data = await api("DELETE", "/knowledge/" + encodeURIComponent(documentId));
    note("upload-status", "ok", "Dihapus: " + escapeHtml(documentId) + " (" + escapeHtml(data.deleted_chunks) + " chunk).");
    state.picked.delete(documentId);
    loadDocs();
  } catch (err) {
    note("upload-status", "err", escapeHtml(err.message || "gagal menghapus"));
  }
}

/* -------------------------------------------------------------------- chat */

function addMessage(role, options) {
  $("thread-empty").hidden = true;
  const wrap = document.createElement("div");
  wrap.className = "msg " + role + (options && options.className ? " " + options.className : "");
  wrap.innerHTML = '<span class="who">' + (role === "user" ? "Anda" : "RAG") + "</span>" +
    '<div class="bubble">' + ((options && options.html) || "") + "</div>";
  $("thread-inner").appendChild(wrap);
  $("thread").scrollTop = $("thread").scrollHeight;
  return wrap;
}

function computedBlock(computed) {
  const wrap = document.createElement("div");
  wrap.className = "computed";
  const head = document.createElement("div");
  head.className = "computed-head";
  const op = computed.operation === "top_n" ? "peringkat" : computed.operation;
  const target = computed.group_by ? computed.group_by : (computed.metric || "");
  head.textContent = "Dihitung dari data tabel: " + op + (target ? " " + target : "") +
    " (" + computed.rows_matched + " dari " + computed.rows_total + " baris)";
  wrap.appendChild(head);

  const rows = (computed.result || []).slice(0, 10);
  if (rows.length) {
    const table = document.createElement("table");
    table.className = "computed-table";
    const keys = Object.keys(rows[0]).filter((key) => key !== "teks");
    const headRow = document.createElement("tr");
    keys.forEach((key) => {
      const th = document.createElement("th");
      th.textContent = key;
      headRow.appendChild(th);
    });
    table.appendChild(headRow);
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      keys.forEach((key) => {
        const td = document.createElement("td");
        const value = row[key];
        td.textContent = typeof value === "number" && row.teks && keys.length === 2 && key === keys[1]
          ? row.teks
          : value;
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    wrap.appendChild(table);
  }
  const foot = document.createElement("div");
  foot.className = "computed-foot";
  foot.textContent = computed.explanation + (computed.rows_skipped ? " Nilai yang dilewati: " + computed.rows_skipped + "." : "");
  wrap.appendChild(foot);
  return wrap;
}

function renderAnswer(node, data) {
  const bubble = node.querySelector(".bubble");
  const answer = data.answer || "";
  // Jawaban diminta dalam Markdown; render supaya judul/daftar/tabel terbaca, bukan `##` mentah.
  // Bila renderer gagal dimuat (halaman lama), jatuh ke teks biasa - jangan tampilkan kosong.
  if (window.Markdown && typeof window.Markdown.render === "function") {
    bubble.classList.add("markdown");
    bubble.innerHTML = window.Markdown.render(answer);
  } else {
    bubble.textContent = answer;
  }

  // Salin sebagai Markdown: pemakai meminta hasilnya berformat .md (teks Markdown), bukan berkas.
  if (answer.trim()) {
    const tools = document.createElement("div");
    tools.className = "answer-tools";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "linkish";
    copy.textContent = "Salin .md";
    copy.title = "Salin jawaban sebagai teks Markdown";
    copy.addEventListener("click", () => copyMarkdown(copy, answer));
    tools.appendChild(copy);
    node.appendChild(tools);
  }

  const sources = data.sources || [];
  if (sources.length) {
    const cites = document.createElement("div");
    cites.className = "cites";
    cites.innerHTML = sources.map((source, index) => {
      const label = source.document_name || source.document_id;
      const page = source.page ? " hal. " + escapeHtml(source.page) : "";
      const score = typeof source.score === "number" ? '<span class="score">' + source.score.toFixed(2) + "</span>" : "";
      // Sumber dari web: tautkan ke halamannya supaya bisa dibuka langsung.
      const link = source.source_url
        ? ' <a class="cite-link" href="' + escapeHtml(source.source_url) + '" target="_blank" rel="noopener noreferrer" title="' +
          escapeHtml(source.source_url) + '">buka</a>'
        : "";
      return '<span class="cite"><b>[' + (index + 1) + "]</b>" + escapeHtml(label) + page + link + " " + score + "</span>";
    }).join("");
    node.appendChild(cites);
  }

  if (data.computed) node.appendChild(computedBlock(data.computed));
  if (data.table_note) {
    const note = document.createElement("div");
    note.className = "table-note";
    note.textContent = data.table_note;
    node.appendChild(note);
  }

  const usage = data.usage || {};
  const route = data.route || {};
  const chips = [
    ["keputusan", route.capability ? route.capability + " (" + route.source + ")" : "-"],
    ["model", data.model || usage.model || "-"],
  ];
  if (data.computed) {
    chips.push(["sumber data", "tabel (dihitung)"]);
  } else {
    chips.push(["reranker", usage.reranker || "-"]);
    chips.push(["context_tokens", usage.context_tokens != null ? usage.context_tokens : "-"]);
    if (usage.context_chunks) {
      const extra = usage.context_expanded_chunks ? " (+" + usage.context_expanded_chunks + " pelengkap)" : "";
      chips.push(["bagian di konteks", usage.context_chunks + extra]);
    }
    const coverage = usage.document_coverage || [];
    if (coverage.length) {
      const item = coverage[0];
      const total = item.total || item.included;
      chips.push([
        "dokumen",
        (item.document_name || item.document_id) + ": " + item.included + "/" + total + (item.complete ? " lengkap" : " sebagian"),
      ]);
    }
  }
  chips.push(["retrieval_ms", usage.retrieval_ms != null ? usage.retrieval_ms : "-"]);
  chips.push(["rendering_ms", usage.generation_ms != null ? Math.round(usage.generation_ms) : "-"]);
  if (usage.computed_rows) chips.push(["baris dihitung", usage.computed_rows]);
  if (data.no_answer_reason) chips.push(["alasan", data.no_answer_reason]);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = chips.map((pair) =>
    "<span><span class=\"k\">" + escapeHtml(pair[0]) + ":</span><span class=\"v\">" + escapeHtml(pair[1]) + "</span></span>").join("");
  node.appendChild(meta);
  if (data.grounded === false) node.classList.add("no-answer");
}

function renderHits(node, data) {
  const results = data.results || [];
  const bubble = node.querySelector(".bubble");
  bubble.textContent = "Retrieval saja" + (data.hybrid ? " (hybrid)" : "") + ": " + results.length + " hasil, tanpa memanggil model.";
  if (results.length) {
    const cites = document.createElement("div");
    cites.className = "cites";
    cites.innerHTML = results.map((hit, index) => {
      const label = hit.document_name || hit.document_id;
      const score = typeof hit.score === "number" ? '<span class="score">' + hit.score.toFixed(2) + "</span>" : "";
      return '<span class="cite"><b>[' + (index + 1) + "]</b>" + escapeHtml(label) + " " + score + "</span>";
    }).join("");
    node.appendChild(cites);
  }
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = "<span><span class=\"k\">retrieval_ms:</span><span class=\"v\">" + escapeHtml(data.retrieval_ms != null ? data.retrieval_ms : "-") +
    "</span></span><span><span class=\"k\">reranker:</span><span class=\"v\">" + escapeHtml(data.reranker || "-") + "</span></span>";
  node.appendChild(meta);
}

function queryOptions() {
  const options = {
    top_k: Number(state.opts.top_k) || 12,
    threshold: Number(state.opts.threshold) || 0,
    strict_grounding: !!state.opts.strict,
    include_sources: true,
    use_hybrid: !!state.opts.hybrid,
    use_reranker: !!state.opts.reranker,
  };
  if (state.opts.route) options.route = state.opts.route;
  if (state.picked.size) options.document_ids = Array.from(state.picked);
  return options;
}

async function ask(query) {
  addMessage("user", { html: escapeHtml(query) });
  const node = addMessage("assistant", { html: '<span class="pending">Menyusun jawaban</span>' });
  const path = state.mode === "search" ? "/search" : "/query";
  try {
    const data = await api("POST", path, { query: query, knowledge_base_id: state.kb, options: queryOptions() });
    if (state.mode === "search") renderHits(node, data);
    else renderAnswer(node, data);
  } catch (err) {
    node.querySelector(".bubble").textContent = "Gagal: " + (err.message || err.code || "tidak diketahui");
    node.classList.add("no-answer");
  }
}

/* ---------------------------------------------------------------- settings */

function modelOptionRow(model, current) {
  return '<li role="option" data-model="' + escapeHtml(model.id) + '" aria-selected="' + (model.id === current) + '">' +
    '<span class="id">' + escapeHtml(model.id) + "</span>" +
    '<span class="own">' + escapeHtml(model.owned_by || "") + "</span></li>";
}

async function loadSettings() {
  readConnInputs();
  clearNote("llm-note");
  if (!credential()) {
    note("llm-note", "warn", "Masuk dengan <strong>kode akses</strong>, atau isi <strong>API key layanan</strong> di panel Koneksi lalu uji koneksi; setelah itu setelan model bisa dibaca.");
    return;
  }
  try {
    const data = await api("GET", "/settings");
    const llm = data.sections.llm;
    const jev = data.sections.jev;
    $("llm-provider").value = llm.provider || "openai_compatible";
    $("llm-base").value = llm.base_url || "";
    $("llm-model").value = llm.model || "";
    $("llm-key-help").textContent = llm.api_key_set
      ? "Kunci tersimpan " + (llm.api_key_hint || "") + ". Biarkan kosong untuk memakainya."
      : "Belum ada kunci tersimpan. Isi bila endpoint memerlukannya.";
    $("llm-max-tokens").value = llm.max_tokens != null ? llm.max_tokens : 8192;
    $("jev-enabled").checked = !!jev.enabled;
    $("jev-provider").value = jev.provider || "systemone";
    $("jev-url").value = jev.systemone_url || "";
    $("jev-model").value = jev.model || "";
    $("jev-key-help").textContent = jev.api_key_set
      ? "Kunci tersimpan " + (jev.api_key_hint || "") + ". Biarkan kosong untuk memakainya."
      : "Belum ada kunci tersimpan.";
    syncJevFields();
    if (data.sections.uploads && data.catalog) {
      renderFormats(data.catalog, data.sections.uploads);
      clearNote("fmt-note");
    }
    if (data.sections.retrieval) renderRetrievalService(data.sections.retrieval);
    if (data.sections.web) renderWebService(data.sections.web);
    if (data.sections.summary) renderSummaryService(data.sections.summary);
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const message = forbidden
      ? "Kunci ini tidak punya izin <strong>admin</strong>, jadi konfigurasi model dan format berkas tidak bisa dibaca atau diubah. Chat tetap bisa dipakai."
      : escapeHtml(err.message || "gagal membaca pengaturan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>";
    note("llm-note", forbidden ? "warn" : "err", message);
    note("fmt-note", forbidden ? "warn" : "err", message);
  }
}

function syncJevFields() {
  const mcp = $("jev-provider").value === "mcp";
  $("jev-url-field").hidden = mcp;
}

async function saveConnection() {
  readConnInputs();
  savePrefs();
  const ok = await checkConnection();
  if (ok) {
    loadSettings();
    note("conn-status", "ok", "Pengaturan koneksi disimpan di browser ini.");
    $("set-key-help").textContent = "Disimpan di localStorage browser untuk origin ini.";
    loadDocs();
  }
}

async function saveLlm() {
  const payload = {
    provider: $("llm-provider").value,
    base_url: $("llm-base").value.trim(),
    model: $("llm-model").value.trim(),
    max_tokens: Number($("llm-max-tokens").value) || 8192,
  };
  const key = $("llm-key").value;
  if (key) payload.api_key = key;
  if (!payload.model) {
    note("llm-status", "err", "Model tidak boleh kosong.");
    return;
  }
  try {
    const data = await api("PUT", "/settings", { llm: payload });
    $("llm-key").value = "";
    note("llm-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    loadSettings();
    checkConnection();
  } catch (err) {
    note("llm-status", "err", escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function probeModels() {
  $("llm-picker-info").textContent = "memuat daftar model...";
  $("llm-picker").hidden = false;
  $("llm-models").innerHTML = "";
  note("llm-status", "info", "Menghubungi endpoint...");
  const payload = { base_url: $("llm-base").value.trim() };
  const key = $("llm-key").value;
  if (key) payload.api_key = key;
  try {
    const data = await api("POST", "/settings/llm/models", payload);
    state.models = data.models || [];
    $("llm-picker-info").textContent = state.models.length + " model dari " + data.base_url + " (" + data.latency_ms + " ms)";
    renderModels();
    $("llm-picker").hidden = false;
    clearNote("llm-status");
  } catch (err) {
    $("llm-picker").hidden = true;
    note("llm-status", "err", escapeHtml(err.message || "gagal memuat daftar model") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

function renderModels() {
  const needle = ($("llm-model-filter").value || "").toLowerCase();
  const current = $("llm-model").value.trim();
  const rows = state.models.filter((model) => !needle || model.id.toLowerCase().includes(needle));
  $("llm-models").innerHTML = rows.length
    ? rows.map((model) => modelOptionRow(model, current)).join("")
    : '<li><span class="own">tidak ada model yang cocok dengan saringan</span></li>';
  $("llm-models").querySelectorAll("[data-model]").forEach((row) => {
    row.addEventListener("click", () => {
      $("llm-model").value = row.dataset.model;
      renderModels();
      note("llm-status", "info", "Model dipilih: <span class=\"mono\">" + escapeHtml(row.dataset.model) + "</span>. Klik Simpan model untuk menerapkannya.");
    });
  });
}

/* Format berkas: katalog dari server, centang per grup. Server tetap penentu terakhir -
   daftar format ini hanya menyiapkan payload dan menolak berkas lebih awal di browser. */

function renderFormats(catalog, uploads) {
  state.catalog = catalog || [];
  const chosen = new Set(uploads.extensions || []);
  const groups = [];
  state.catalog.forEach((row) => {
    let bucket = groups.find((group) => group.name === row.group);
    if (!bucket) {
      bucket = { name: row.group, rows: [] };
      groups.push(bucket);
    }
    bucket.rows.push(row);
  });
  $("fmt-groups").innerHTML = groups.map((group) => {
    const usable = group.rows.filter((row) => row.available).length;
    const checked = group.rows.filter((row) => row.available && row.extensions.some((ext) => chosen.has(ext))).length;
    const items = group.rows.map((row) => {
      const on = row.available && row.extensions.some((ext) => chosen.has(ext));
      return '<li class="fmt-item' + (row.available ? "" : " off") + '">' +
        '<label><input type="checkbox" data-fmt="' + escapeHtml(row.key) + '"' + (on ? " checked" : "") +
        (row.available ? "" : " disabled") + ">" +
        '<span class="fmt-ext mono">' + escapeHtml(row.extensions.join(" ")) + "</span>" +
        '<span class="fmt-label">' + escapeHtml(row.label) + "</span></label>" +
        (row.available ? "" : '<span class="fmt-note">' + escapeHtml(row.note || "belum didukung di mesin ini") + "</span>") +
        "</li>";
    }).join("");
    return '<div class="fmt-group"><div class="fmt-group-head"><h3>' + escapeHtml(group.name) + "</h3>" +
      '<span class="fmt-count">' + checked + " dari " + usable + " dipilih</span></div>" +
      '<ul class="fmt-list">' + items + "</ul></div>";
  }).join("");
  $("fmt-max").value = uploads.max_upload_mb;
  $("fmt-max-help").textContent = "Berkas di atas batas ini ditolak sebelum diunggah. Format aktif: " +
    (chosen.size ? Array.from(chosen).sort().join(", ") : "tidak ada");
}

function collectFormats() {
  const picked = [];
  $("fmt-groups").querySelectorAll("[data-fmt]").forEach((box) => { if (box.checked) picked.push(box.dataset.fmt); });
  return picked;
}

async function saveFormats() {
  const keys = collectFormats();
  if (!keys.length) {
    note("fmt-status", "err", "Pilih minimal satu format yang tersedia.");
    return;
  }
  const extensions = [];
  state.catalog.forEach((row) => {
    if (keys.indexOf(row.key) !== -1) row.extensions.forEach((ext) => { if (extensions.indexOf(ext) === -1) extensions.push(ext); });
  });
  try {
    const data = await api("PUT", "/settings", {
      uploads: { extensions: extensions, max_upload_mb: Number($("fmt-max").value) || 1 },
    });
    note("fmt-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    renderFormats(data.catalog || state.catalog, data.sections.uploads);
    checkConnection();
  } catch (err) {
    note("fmt-status", "err", escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function saveJev() {
  const payload = {
    enabled: $("jev-enabled").checked,
    provider: $("jev-provider").value,
    model: $("jev-model").value.trim(),
  };
  if (payload.provider === "systemone") payload.systemone_url = $("jev-url").value.trim();
  const key = $("jev-key").value;
  if (key) payload.api_key = key;
  try {
    const data = await api("PUT", "/settings", { jev: payload });
    $("jev-key").value = "";
    note("jev-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    loadSettings();
    checkConnection();
  } catch (err) {
    note("jev-status", "err", escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function probeJev() {
  note("jev-status", "info", "Mengirim satu pertanyaan uji ke Jev...");
  const payload = { provider: $("jev-provider").value, model: $("jev-model").value.trim() };
  if (payload.provider === "systemone") payload.url = $("jev-url").value.trim();
  const key = $("jev-key").value;
  if (key) payload.api_key = key;
  try {
    const data = await api("POST", "/settings/jev/probe", payload);
    note("jev-status", "ok", "Jev menjawab dalam <strong>" + escapeHtml(data.latency_ms) + " ms</strong> dengan model <span class=\"mono\">" +
      escapeHtml(data.model) + "</span> (" + escapeHtml((data.answer && data.answer.type) || "noul") + ").");
  } catch (err) {
    note("jev-status", "err", escapeHtml(err.message || "Jev tidak menjawab") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

function saveRetrieval() {
  state.opts = {
    top_k: Number($("r-topk").value) || 12,
    threshold: Number($("r-threshold").value) || 0,
    strict: $("r-strict").checked,
    hybrid: $("r-hybrid").checked,
    reranker: $("r-reranker").checked,
    route: $("r-route").value,
  };
  savePrefs();
  note("retr-status", "ok", "Pilihan disimpan untuk browser ini.");
}

function renderRetrievalService(retrieval) {
  if (retrieval.context_max_tokens != null) $("s-context-tokens").value = retrieval.context_max_tokens;
  if (retrieval.final_top_k != null) $("s-topk").value = retrieval.final_top_k;
  if (retrieval.max_chunks_per_document != null) $("s-maxchunks").value = retrieval.max_chunks_per_document;
  $("s-expand").checked = retrieval.context_expand_documents !== false;
}

async function saveRetrievalService() {
  const payload = {
    retrieval: {
      context_max_tokens: Number($("s-context-tokens").value) || 0,
      final_top_k: Number($("s-topk").value) || 12,
      max_chunks_per_document: Number($("s-maxchunks").value) || 8,
      context_expand_documents: $("s-expand").checked,
    },
  };
  note("retr-svc-status", "info", "Menyimpan setelan konteks...");
  try {
    const data = await api("PUT", "/settings", payload);
    note("retr-svc-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    if (data.sections && data.sections.retrieval) renderRetrievalService(data.sections.retrieval);
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const message = forbidden
      ? "Setelan layanan hanya bisa diubah dengan kunci berizin <strong>admin</strong>."
      : escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>";
    note("retr-svc-status", forbidden ? "warn" : "err", message);
  }
}

function renderWebService(web) {
  if (!web) return;
  $("s-web-enabled").checked = web.enabled !== false;
  $("s-web-samehost").checked = web.same_host !== false;
  $("s-web-followfiles").checked = web.follow_files !== false;
  $("s-web-robots").checked = web.respect_robots !== false;
  $("s-web-private").checked = web.allow_private_urls === true;
  if (web.max_pages != null) $("s-web-pages").value = web.max_pages;
  if (web.max_depth != null) $("s-web-depth").value = web.max_depth;
}

async function saveWebService() {
  const payload = {
    web: {
      enabled: $("s-web-enabled").checked,
      same_host: $("s-web-samehost").checked,
      follow_files: $("s-web-followfiles").checked,
      respect_robots: $("s-web-robots").checked,
      allow_private_urls: $("s-web-private").checked,
      max_pages: Number($("s-web-pages").value) || 20,
      max_depth: Number($("s-web-depth").value),
    },
  };
  note("web-svc-status", "info", "Menyimpan setelan web...");
  try {
    const data = await api("PUT", "/settings", payload);
    note("web-svc-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    if (data.sections && data.sections.web) renderWebService(data.sections.web);
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const message = forbidden
      ? "Setelan layanan hanya bisa diubah dengan kunci berizin <strong>admin</strong>."
      : escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>";
    note("web-svc-status", forbidden ? "warn" : "err", message);
  }
}

function renderSummaryService(summary) {
  if (!summary) return;
  $("s-summary-enabled").checked = summary.enabled !== false;
  if (summary.window_tokens != null) $("s-summary-window").value = summary.window_tokens;
  if (summary.max_tokens != null) $("s-summary-maxtok").value = summary.max_tokens;
  if (summary.max_documents != null) $("s-summary-maxdocs").value = summary.max_documents;
}

async function saveSummaryService() {
  const payload = {
    summary: {
      enabled: $("s-summary-enabled").checked,
      window_tokens: Number($("s-summary-window").value) || 12000,
      max_tokens: Number($("s-summary-maxtok").value) || 2048,
      max_documents: Number($("s-summary-maxdocs").value) || 3,
    },
  };
  note("summary-svc-status", "info", "Menyimpan setelan ringkasan...");
  try {
    const data = await api("PUT", "/settings", payload);
    note("summary-svc-status", "ok", "Tersimpan: <span class=\"mono\">" + escapeHtml((data.applied || []).join(", ")) + "</span>");
    if (data.sections && data.sections.summary) renderSummaryService(data.sections.summary);
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const message = forbidden
      ? "Setelan layanan hanya bisa diubah dengan kunci berizin <strong>admin</strong>."
      : escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>";
    note("summary-svc-status", forbidden ? "warn" : "err", message);
  }
}

function setMode(mode) {
  state.mode = mode === "search" ? "search" : "answer";
  $("btn-mode-answer").setAttribute("aria-pressed", state.mode === "answer" ? "true" : "false");
  $("btn-mode-search").setAttribute("aria-pressed", state.mode === "search" ? "true" : "false");
  $("prompt").placeholder = state.mode === "search"
    ? "Cari di dokumen...  (retrieval saja, tanpa jawaban)"
    : "Tulis pertanyaan...  (Enter kirim, Shift+Enter baris baru)";
  savePrefs();
}

/* Kunci API: daftar, pembuatan, pencabutan. Nilai kunci hanya muncul di jawaban
   POST /settings/api-keys; setelah itu yang tersimpan hanyalah hash-nya. */

function shortTime(value) {
  if (!value) return "-";
  const at = new Date(value);
  if (isNaN(at.getTime())) return String(value).replace("T", " ").slice(0, 16);
  const pad = (number) => String(number).padStart(2, "0");
  return at.getFullYear() + "-" + pad(at.getMonth() + 1) + "-" + pad(at.getDate()) +
    " " + pad(at.getHours()) + ":" + pad(at.getMinutes());
}

function keyRow(entry) {
  const tenant = [entry.organization_id, entry.user_id, entry.application_id]
    .filter(Boolean).map(escapeHtml).join(" / ") || "-";
  const perms = (entry.permissions || []).map((perm) => '<span class="tag">' + escapeHtml(perm) + "</span>").join(" ") || "-";
  const label = entry.source === "env" ? "API_KEYS_JSON" : escapeHtml(entry.label);
  const state2 = entry.state === "active"
    ? '<span class="tag ok">aktif</span>'
    : entry.state === "revoked"
      ? '<span class="tag off">dicabut</span>'
      : '<span class="tag warn">kedaluwarsa</span>';
  const action = entry.source !== "registry"
    ? '<span class="help">dari env</span>'
    : entry.revocable
      ? '<button class="btn ghost sm" type="button" data-revoke="' + escapeHtml(entry.key_id) + '">Cabut</button>'
      : "";
  return "<tr" + (entry.state === "active" ? "" : ' class="off"') + ">" +
    "<td>" + label + (entry.expires_at ? '<span class="help"> berlaku sampai ' + escapeHtml(shortTime(entry.expires_at)) + "</span>" : "") + "</td>" +
    '<td class="mono">' + escapeHtml(entry.hint || "-") + "</td>" +
    "<td>" + tenant + "</td>" +
    "<td>" + perms + "</td>" +
    "<td>" + escapeHtml(shortTime(entry.created_at)) + "</td>" +
    "<td>" + escapeHtml(shortTime(entry.last_used_at)) + "</td>" +
    "<td>" + state2 + "</td>" +
    "<td>" + action + "</td></tr>";
}

function renderKeys(data) {
  const keys = data.keys || [];
  const context = data.context || {};
  state.keyContext = context;

  $("keys-rows").innerHTML = keys.map(keyRow).join("");
  $("keys-empty").hidden = keys.length > 0;
  $("keys-rows").querySelectorAll("[data-revoke]").forEach((button) => {
    button.addEventListener("click", () => revokeKey(button.dataset.revoke));
  });

  const canCross = (context.permissions || []).indexOf("*") !== -1;
  ["key-org", "key-user", "key-app"].forEach((id) => { $(id).disabled = !canCross; });
  $("key-tenant-help").textContent = canCross
    ? "Kosong = ikut konteks kunci Anda. Terisi = kunci baru memakai konteks itu."
    : "Kosong = ikut konteks kunci Anda (" + (context.organization_id || "-") + "). Mengisi field ini butuh izin '*'.";

  const who = (context.organization_id || "-") + " / " + (context.user_id || "-");
  note("keys-note", "", "Aktif <strong>" + (data.active || 0) + "</strong> dari batas " + (data.max_active_keys || 0) +
    ". Kunci yang Anda pakai: <span class=\"mono\">" + escapeHtml(who) + "</span>" +
    (context.key_id ? " (" + escapeHtml(context.key_id) + ")" : " (dari API_KEYS_JSON)"));

  // Kunci bootstrap adalah pintu masuk pertama; setelah kode akses dipasang, ia sebaiknya hilang.
  const bootstrap = keys.filter((entry) => entry.state === "active" && String(entry.label || "").indexOf("bootstrap") !== -1)[0];
  if (bootstrap) {
    note("keys-bootstrap", "warn", "Masih ada <strong>kunci bootstrap</strong> (" + escapeHtml(bootstrap.hint || bootstrap.key_id) +
      ") dari pemasangan pertama. Pasang kode akses di panel <strong>Akses &amp; Sesi</strong>, masuk dengan kode itu, lalu cabut kunci ini dan hapus berkasnya di server.");
  } else {
    clearNote("keys-bootstrap");
  }
}

async function loadApiKeys() {
  if (!credential()) {
    $("keys-rows").innerHTML = "";
    $("keys-empty").hidden = false;
    $("keys-empty").textContent = "Masuk dengan kode akses, atau tempel kunci API sekali di panel Koneksi.";
    return;
  }
  try {
    renderKeys(await api("GET", "/settings/api-keys"));
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const anonymous = err.code === "AUTH_INVALID";
    $("keys-rows").innerHTML = "";
    $("keys-empty").hidden = forbidden;
    note("keys-note", forbidden || anonymous ? "warn" : "err",
      anonymous
        ? "Butuh kredensial <strong>admin</strong> untuk melihat kunci: masuk dengan <strong>kode akses</strong>, atau tempel kunci API sekali di panel <strong>Koneksi</strong>."
        : forbidden
          ? "Kunci ini tidak berizin <strong>admin</strong>, jadi daftar kunci tidak bisa dibaca atau diubah."
          : escapeHtml(err.message || "gagal memuat daftar kunci") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function createKey() {
  const label = $("key-label").value.trim();
  if (!label) {
    note("keys-status", "err", "Label kunci wajib diisi supaya kunci ini bisa dikenali nanti.");
    return;
  }
  const permissions = ["read"];
  if ($("key-perm-write").checked) permissions.push("write");
  if ($("key-perm-admin").checked) permissions.push("admin");
  const payload = { label: label, permissions: permissions };
  if ($("key-expiry").value.trim()) payload.expires_in_days = Number($("key-expiry").value);
  const tenantFields = { "key-org": "organization_id", "key-user": "user_id", "key-app": "application_id" };
  Object.keys(tenantFields).forEach((id) => {
    const value = $(id).value.trim();
    if (value) payload[tenantFields[id]] = value;
  });

  $("btn-create-key").disabled = true;
  try {
    const data = await api("POST", "/settings/api-keys", payload);
    $("key-value").value = data.key;
    $("key-result").hidden = false;
    $("key-result-help").textContent = data.note || "Simpan sekarang; nilainya hanya tampil sekali.";
    $("key-label").value = "";
    $("key-expiry").value = "";
    note("keys-status", "ok", "Kunci untuk <span class=\"mono\">" + escapeHtml(data.entry.organization_id) +
      "</span> dibuat. Salin nilainya sebelum menutup layar.");
    loadApiKeys();
  } catch (err) {
    note("keys-status", "err", escapeHtml(err.message || "gagal membuat kunci") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  } finally {
    $("btn-create-key").disabled = false;
  }
}

async function revokeKey(keyId) {
  if (!keyId) return;
  if (!window.confirm("Cabut kunci ini? Klien yang memakainya akan langsung ditolak.")) return;
  try {
    const data = await api("DELETE", "/settings/api-keys/" + encodeURIComponent(keyId));
    note("keys-status", "ok", "Kunci <span class=\"mono\">" + escapeHtml(data.entry.label) + "</span> dicabut.");
    loadApiKeys();
  } catch (err) {
    note("keys-status", "err", escapeHtml(err.message || "gagal mencabut kunci") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function copyKey() {
  const field = $("key-value");
  if (!field.value) return;
  field.select();
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(field.value);
      copied = true;
    }
  } catch (err) {
    copied = false;
  }
  if (!copied) {
    try {
      copied = document.execCommand("copy");
    } catch (err) {
      copied = false;
    }
  }
  note("keys-status", copied ? "ok" : "warn", copied
    ? "Kunci disalin ke papan klip."
    : "Tidak bisa menyalin otomatis di sini; teks sudah dipilih, salin manual (Ctrl+C).");
}

/* ------------------------------------------------------------------ events */

function sessionRow(entry) {
  const device = escapeHtml(entry.label || entry.client || "peramban");
  const state2 = entry.stale_code
    ? '<span class="tag warn">kode sudah diganti</span>'
    : entry.state === "active"
      ? '<span class="tag ok">aktif</span>'
      : entry.state === "revoked"
        ? '<span class="tag off">keluar</span>'
        : '<span class="tag warn">kedaluwarsa</span>';
  const action = entry.state === "active" && !entry.stale_code
    ? '<button class="btn ghost sm" type="button" data-session="' + escapeHtml(entry.session_id) + '">Keluarkan</button>'
    : '<span class="help">tidak aktif</span>';
  return "<tr" + (entry.state === "active" && !entry.stale_code ? "" : ' class="off"') + ">" +
    "<td>" + device + '<span class="help mono">' + escapeHtml(entry.hint || "-") + "</span></td>" +
    "<td>" + escapeHtml(shortTime(entry.created_at)) + "</td>" +
    "<td>" + escapeHtml(shortTime(entry.expires_at)) + "</td>" +
    "<td>" + escapeHtml(shortTime(entry.last_used_at)) + "</td>" +
    "<td>" + (entry.remember ? '<span class="tag">1 minggu</span>' : '<span class="tag">12 jam</span>') + "</td>" +
    "<td>" + state2 + "</td>" +
    "<td>" + action + "</td></tr>";
}

function renderAccess(data) {
  const access = data.access || {};
  state.sessions = data.sessions || [];
  state.currentSessionId = ((data.context || {}).session_id) || "";
  $("access-life-default").textContent = (data.default_lifetime || {}).label || "-";
  $("access-life-remember").textContent = (data.remember_lifetime || {}).label || "-";
  $("sessions-rows").innerHTML = state.sessions.map(sessionRow).join("");
  $("sessions-empty").hidden = state.sessions.length > 0;
  $("sessions-count").textContent = (data.active_sessions || 0) + " / " + (data.max_active_sessions || 0);
  $("access-hint").innerHTML = access.enabled
    ? "Kode aktif (potongan " + escapeHtml(access.hint || "-") + "), diubah " + escapeHtml(shortTime(access.set_at)) +
      " oleh <span class=\"mono\">" + escapeHtml(access.updated_by || "-") + "</span>."
    : "Belum ada kode akses: konsol masih bisa dibuka dengan API key.";
  $("btn-clear-access").disabled = !access.enabled;
  $("access-current").disabled = !access.enabled;
  ["access-new", "access-repeat"].forEach((id) => { $(id).value = ""; });
  $("sessions-rows").querySelectorAll("[data-session]").forEach((button) => {
    button.addEventListener("click", () => revokeSession(button.dataset.session));
  });
  const source = (data.context || {}).source || "-";
  note("access-note", "", "Anda masuk sebagai <span class=\"mono\">" + escapeHtml(source) + "</span>" +
    (data.context && data.context.organization_id ? " untuk tenant <span class=\"mono\">" + escapeHtml(data.context.organization_id) + "</span>" : "") +
    ". Kode akses berlaku untuk seluruh layanan; sesi bisa dikeluarkan satu per satu.");
}

async function loadAccess() {
  try {
    renderAccess(await api("GET", "/settings/access"));
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const anonymous = err.code === "AUTH_INVALID";
    note("access-note", forbidden || anonymous ? "warn" : "err",
      anonymous
        ? "Panel ini butuh kredensial <strong>admin</strong>: masuk dengan <strong>kode akses</strong>, atau tempel kunci API sekali di panel <strong>Koneksi</strong>. Sesudah kode akses dipasang, kunci tidak perlu lagi."
        : forbidden
          ? "Kredensial ini tidak berizin <strong>admin</strong>, jadi kode akses dan daftar sesi tidak bisa dibaca."
          : escapeHtml(err.message || "gagal membaca status akses") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function saveAccess() {
  const code = $("access-new").value;
  const repeat = $("access-repeat").value;
  if (!code) {
    note("access-status", "err", "Kode akses baru masih kosong.");
    return;
  }
  if (code !== repeat) {
    note("access-status", "err", "Dua isian kode tidak sama; ulangi dengan kode yang sama.");
    return;
  }
  const min = (state.gate && state.gate.min_code_length) || 6;
  if (code.length < min) {
    note("access-status", "err", "Kode akses minimal <strong>" + min + "</strong> karakter.");
    return;
  }
  const payload = { code: code };
  if ($("access-current").value) payload.current_code = $("access-current").value;
  $("btn-save-access").disabled = true;
  try {
    const data = await api("PUT", "/settings/access", payload);
    renderAccess(data);
    $("access-current").value = "";
    const revoked = data.sessions_revoked || 0;
    note("access-status", "ok", "Kode akses tersimpan. " + (revoked
      ? "<strong>" + revoked + "</strong> sesi lain dikeluarkan karena kode berganti."
      : "Sesi Anda tetap berlaku."));
    loadGate();
  } catch (err) {
    note("access-status", "err", escapeHtml(err.message || "gagal menyimpan kode") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  } finally {
    $("btn-save-access").disabled = false;
  }
}

async function clearAccess() {
  if (!window.confirm("Matikan kode akses? Konsol hanya bisa dibuka dengan API key lagi, dan semua sesi dikeluarkan.")) return;
  try {
    const data = await api("DELETE", "/settings/access");
    renderAccess(data);
    note("access-status", "ok", "Kode akses dimatikan; " + (data.sessions_revoked || 0) + " sesi dikeluarkan.");
    loadGate();
  } catch (err) {
    note("access-status", "err", escapeHtml(err.message || "gagal mematikan kode") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function revokeAllSessions() {
  if (!window.confirm("Keluarkan semua sesi konsol? Perangkat yang sedang terbuka harus memasukkan kode akses lagi.")) return;
  try {
    const data = await api("DELETE", "/settings/access/sessions");
    renderAccess(data);
    note("access-status", "ok", (data.sessions_revoked || 0) + " sesi dikeluarkan.");
    loadGate();
  } catch (err) {
    note("access-status", "err", escapeHtml(err.message || "gagal mengeluarkan sesi") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

async function revokeSession(sessionId) {
  if (!sessionId) return;
  const mine = !!state.session && sessionId === state.currentSessionId;
  if (!window.confirm(mine
    ? "Ini sesi yang sedang Anda pakai. Keluarkan sekarang? Anda harus memasukkan kode akses lagi."
    : "Keluarkan sesi ini? Perangkat itu harus memasukkan kode akses lagi.")) return;
  try {
    const data = await api("DELETE", "/settings/access/sessions/" + encodeURIComponent(sessionId));
    renderAccess(data);
    note("access-status", "ok", "Sesi <span class=\"mono\">" + escapeHtml(sessionId) + "</span> dikeluarkan.");
    if (mine) {
      clearSession();
      showGate("Sesi Anda baru saja dikeluarkan. Masukkan kode akses untuk masuk lagi.");
    } else {
      loadGate();
    }
  } catch (err) {
    note("access-status", "err", escapeHtml(err.message || "gagal mengeluarkan sesi") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
}

function wire() {
  $("btn-settings").addEventListener("click", () => showView(currentView() === "settings" ? "chat" : "settings"));
  $("btn-settings-close").addEventListener("click", () => showView("chat"));
  $("dlg-settings").addEventListener("close", () => {
    if (currentView() === "settings") location.hash = "#/";
  });
  document.querySelectorAll("#settings-nav [data-panel]").forEach((button) => {
    button.addEventListener("click", () => selectPanel(button.dataset.panel));
  });
  window.addEventListener("hashchange", () => showView(currentView()));

  $("btn-create-key").addEventListener("click", createKey);
  $("btn-keys-refresh").addEventListener("click", loadApiKeys);
  $("btn-copy-key").addEventListener("click", copyKey);

  $("btn-test-conn").addEventListener("click", async () => {
    const ok = await checkConnection();
    if (ok && currentView() === "settings") loadSettings();
  });
  $("btn-save-conn").addEventListener("click", saveConnection);

  $("btn-browse").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (event) => {
    Array.from(event.target.files || []).forEach((file) => indexFile(file));
    event.target.value = "";
  });
  ["dragenter", "dragover"].forEach((name) => $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.add("over");
  }));
  ["dragleave", "drop"].forEach((name) => $("dropzone").addEventListener(name, () => $("dropzone").classList.remove("over")));
  $("dropzone").addEventListener("drop", (event) => {
    event.preventDefault();
    Array.from(event.dataTransfer.files || []).forEach((file) => indexFile(file));
  });

  $("btn-paste").addEventListener("click", () => $("dlg-paste").showModal());
  $("dlg-paste").addEventListener("close", () => {
    if ($("dlg-paste").returnValue !== "ok") return;
    const text = $("paste-text").value.trim();
    if (!text) {
      note("upload-status", "err", "Teks masih kosong.");
      return;
    }
    indexText($("paste-name").value.trim() || "catatan tanpa nama", text);
    $("paste-text").value = "";
    $("paste-name").value = "";
  });

  $("btn-url").addEventListener("click", () => {
    $("dlg-url").showModal();
  });
  $("url-crawl").addEventListener("change", () => {
    $("url-crawl-opts").hidden = !$("url-crawl").checked;
    $("url-confirm").textContent = $("url-crawl").checked ? "Jelajahi & indeks" : "Indeks";
  });
  $("dlg-url").addEventListener("close", () => {
    if ($("dlg-url").returnValue !== "ok") return;
    const url = $("url-value").value.trim();
    if (!url) {
      note("upload-status", "err", "URL masih kosong.");
      return;
    }
    const crawl = $("url-crawl").checked;
    indexUrl(url, $("url-name").value.trim(), {
      crawl: crawl,
      maxPages: Number($("url-max-pages").value) || 20,
      maxDepth: Number($("url-max-depth").value),
    });
    $("url-name").value = "";
    if (crawl) {
      note("upload-status", "info", "Menjelajahi " + escapeHtml(url) + " - halaman yang diambil akan muncul di daftar setelah selesai.");
    }
  });

  $("btn-select-none").addEventListener("click", () => {
    state.picked.clear();
    loadDocs();
  });

  $("btn-mode-answer").addEventListener("click", () => setMode("answer"));
  $("btn-mode-search").addEventListener("click", () => setMode("search"));

  $("form-ask").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = $("prompt").value.trim();
    if (!query) return;
    $("prompt").value = "";
    $("prompt").style.height = "auto";
    ask(query);
  });
  $("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("form-ask").requestSubmit();
    }
  });
  $("prompt").addEventListener("input", () => {
    $("prompt").style.height = "auto";
    $("prompt").style.height = Math.min($("prompt").scrollHeight, 160) + "px";
  });

  $("btn-save-llm").addEventListener("click", saveLlm);
  $("btn-llm-models").addEventListener("click", probeModels);
  $("llm-model-filter").addEventListener("input", renderModels);
  $("btn-llm-picker-close").addEventListener("click", () => { $("llm-picker").hidden = true; });
  $("btn-jev-probe").addEventListener("click", probeJev);
  $("btn-save-jev").addEventListener("click", saveJev);
  $("jev-provider").addEventListener("change", syncJevFields);
  $("btn-save-fmt").addEventListener("click", saveFormats);
  $("btn-fmt-all").addEventListener("click", () => {
    $("fmt-groups").querySelectorAll("[data-fmt]").forEach((box) => { box.checked = !box.disabled; });
    note("fmt-status", "info", "Semua format yang didukung mesin ini dipilih. Klik Simpan format untuk menerapkan.");
  });
  $("btn-save-retr").addEventListener("click", saveRetrieval);
  $("btn-save-retr-svc").addEventListener("click", saveRetrievalService);
  $("btn-save-web-svc").addEventListener("click", saveWebService);
  $("btn-save-summary-svc").addEventListener("click", saveSummaryService);
  $("gate-form").addEventListener("submit", login);
  $("btn-logout").addEventListener("click", logout);
  $("btn-save-access").addEventListener("click", saveAccess);
  $("btn-clear-access").addEventListener("click", clearAccess);
  $("btn-revoke-sessions").addEventListener("click", revokeAllSessions);
  $("access-new").addEventListener("input", () => clearNote("access-status"));
}

/* ------------------------------------------------------------------- boot */

/* Urutan penting: gerbang diperiksa lebih dulu. Kalau konsol tidak memakai kode akses
   (open access atau API key), gerbang tidak pernah muncul dan layar langsung terbuka. */
async function boot() {
  restore();
  wire();
  showView(currentView());
  if (state.session) $("btn-logout").hidden = false;
  try {
    const gate = await loadGate();
    if (gate && gate.enabled && !state.session) {
      showGate("Konsol ini memakai kode akses. Masukkan kodenya untuk mulai.");
      return;
    }
    if (state.session || state.key) {
      const ok = await checkConnection();
      if (ok) loadDocs();
    } else if (gate && gate.enabled) {
      setConnection("warn", "butuh kode akses");
    } else {
      setConnection("warn", "belum masuk");
      note("conn-status", "warn", "Layanan ini belum memakai kode akses. Tempel <strong>kunci API</strong> sekali di panel Koneksi (di server: kunci bootstrap pada berkas <span class=\"mono\">data/bootstrap_admin_key.json</span>), lalu pasang kode akses di panel <strong>Akses &amp; Sesi</strong> supaya tidak perlu menempel kunci lagi.");
    }
  } catch (err) {
    setConnection("err", "layanan tidak terjangkau");
  }
}

boot();
