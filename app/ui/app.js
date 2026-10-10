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
const PANELS = ["conn", "access", "keys", "llm", "jev", "retr", "fmt", "web", "summary"];
const RECENT_KB_KEY = "rag.console.kbs.v1";
const VIEWS = ["kbs", "chat", "eval", "settings"];
// Versi bentuk opsi uji di localStorage. Versi lama memaksa reranker MATI di setiap pertanyaan konsol,
// sehingga hasil uji berbeda dari yang diterima aplikasi lewat API; opsi lama itu dibuang.
const OPTS_VERSION = 2;
const HISTORY_TURNS = 6;
const EVAL_KEY = "rag.console.eval.v1";
// Preset kualitas jawaban (setelan layanan).
const PRESETS = {
  accurate: { min_relevance: 0.3, relevance_threshold: 0.35, final_top_k: 12, context_neighbor_chunks: 1,
    context_full_document_tokens: 3000, context_max_tokens: 24000, max_chunks_per_document: 8,
    context_expand_max_documents: 3, context_expand_documents: true, strict_grounding: true },
  balanced: { min_relevance: 0.2, relevance_threshold: 0.25, final_top_k: 15, context_neighbor_chunks: 1,
    context_full_document_tokens: 3000, context_max_tokens: 24000, max_chunks_per_document: 8,
    context_expand_max_documents: 3, context_expand_documents: true, strict_grounding: true },
  complete: { min_relevance: 0.25, relevance_threshold: 0.3, final_top_k: 20, context_neighbor_chunks: 2,
    context_full_document_tokens: 8000, context_max_tokens: 48000, max_chunks_per_document: 12,
    context_expand_max_documents: 4, context_expand_documents: true, strict_grounding: true },
};

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
  // null = ikut setelan layanan (bawaan): konsol menguji perilaku yang sama dengan API.
  opts: { top_k: null, threshold: null, strict: null, hybrid: null, reranker: null, route: "", memory: true },
  history: [],
  evalMode: "search",
  kbs: [],
  kbSort: "recent",
  view: "",
  evalStop: false,
  limits: { max_mb: null, allowed: [], allowed_ext: [] },
  catalog: [],
  models: [],
  panel: "conn",
  keyContext: null,
  // True bila layanan berjalan tanpa gerbang kode akses (cukup kunci API).
  apiKeyOnly: false,
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
    if (stored.optsVersion === OPTS_VERSION) state.opts = Object.assign(state.opts, stored.opts || {});
  }
}

function savePrefs() {
  try {
    localStorage.setItem(KEY, JSON.stringify({ key: state.key, kb: state.kb, mode: state.mode, opts: state.opts, optsVersion: OPTS_VERSION }));
  } catch (err) {
    /* penyimpanan browser tidak tersedia: pilihan berlaku untuk tab ini saja */
  }
}

function restore() {
  state.base = location.origin + "/api/v1";
  $("set-base").value = state.base;
  loadPrefs();
  loadSession();
  $("set-kb").value = state.kb;
  $("set-key").value = state.key;
  renderBrowserOptions();
  setMode(state.mode);
  try {
    $("eval-cases").value = localStorage.getItem(EVAL_KEY) || "";
  } catch (err) {
    /* tanpa localStorage: daftar uji tidak diingat */
  }
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
    if (ok) route();
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

/* Rute halaman (hash): #/kbs, #/kb/<id>, #/eval, #/settings/<panel>. */
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const head = parts[0] || "";
  if (head === "settings") return { view: "settings", arg: parts[1] || "" };
  if (head === "eval") return { view: "eval", arg: "" };
  if (head === "kb") return { view: "chat", arg: parts.slice(1).join("/") };
  if (head === "chat") return { view: "chat", arg: "" };
  return { view: "kbs", arg: "" };
}

function currentView() {
  return parseRoute().view;
}

function showView(name, arg) {
  const target = name === "settings"
    ? "#/settings" + (arg ? "/" + arg : "")
    : name === "chat"
      ? "#/kb/" + encodeURIComponent(arg || state.kb)
      : name === "eval" ? "#/eval" : "#/kbs";
  if (location.hash !== target) location.hash = target;
  else route();
}

function route() {
  const { view, arg } = parseRoute();
  const previous = state.view;
  state.view = view;
  VIEWS.forEach((name) => { $("view-" + name).hidden = name !== view; });
  document.querySelectorAll(".rail-nav [data-route]").forEach((link) => {
    if (link.dataset.route === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("nav-chat").setAttribute("href", "#/kb/" + encodeURIComponent(state.kb));
  if (view === "chat") {
    openKnowledgeBase(arg || state.kb);
  } else if (view === "kbs") {
    loadKbs();
  } else if (view === "eval") {
    renderEvalKbs();
  } else if (view === "settings") {
    selectPanel(arg || state.panel, true);
    if (previous !== "settings") {
      loadSettings();
      loadApiKeys();
      loadGate();
    }
  }
  if (view !== "chat") window.scrollTo(0, 0);
}

function selectPanel(name, fromRoute) {
  state.panel = PANELS.indexOf(name) === -1 ? "conn" : name;
  // Panel Akses & Sesi hanya relevan bila konsol memang memakai kode akses. Pada mode
  // "cukup API key" panelnya disembunyikan supaya tidak ada setelan mati yang membingungkan.
  if (state.apiKeyOnly && state.panel === "access") state.panel = "conn";
  document.querySelectorAll("#settings-nav [data-panel]").forEach((button) => {
    const on = button.dataset.panel === state.panel;
    button.setAttribute("aria-selected", on ? "true" : "false");
    // Di layar sempit menu pengaturan jadi baris yang digeser: pastikan menu aktif terlihat.
    if (on && button.offsetParent) button.scrollIntoView({ block: "nearest", inline: "center" });
  });
  document.querySelectorAll("#settings-panels > [data-panel]").forEach((section) => {
    section.hidden = section.dataset.panel !== state.panel;
  });
  if (!fromRoute) history.replaceState(null, "", "#/settings/" + state.panel);
  if (state.panel === "access") loadAccess();
}

// Sembunyikan/tampilkan bagian yang hanya berguna saat gerbang kode akses dipakai.
function applyConsoleMode() {
  const apiKeyOnly = !!state.apiKeyOnly;
  document.querySelectorAll("#settings-nav [data-panel='access']").forEach((node) => {
    node.hidden = apiKeyOnly;
  });
  const accessPanel = document.querySelector("#settings-panels > [data-panel='access']");
  if (accessPanel) accessPanel.hidden = apiKeyOnly || state.panel !== "access";
  const logout = $("btn-logout");
  if (logout) logout.hidden = apiKeyOnly || !state.session;
}

/* --------------------------------------------------------- knowledge base */

function cleanKbId(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_.:-]/g, "").slice(0, 120);
}

function recentKbs() {
  try {
    const stored = JSON.parse(localStorage.getItem(RECENT_KB_KEY) || "[]");
    return Array.isArray(stored) ? stored.filter((item) => typeof item === "string") : [];
  } catch (err) {
    return [];
  }
}

function rememberKb(id) {
  if (!id) return;
  const list = [id].concat(recentKbs().filter((item) => item !== id)).slice(0, 30);
  try {
    localStorage.setItem(RECENT_KB_KEY, JSON.stringify(list));
  } catch (err) {
    /* tidak bisa diingat: tetap bisa dibuka */
  }
}

function openKnowledgeBase(id) {
  const kb = cleanKbId(id) || DEFAULT_KB;
  if (kb !== state.kb) {
    state.kb = kb;
    state.picked.clear();
    state.docs = [];
    newConversation();
    renderDocs([]);
  }
  $("set-kb").value = state.kb;
  $("kb-current").textContent = state.kb;
  $("kb-current").title = state.kb;
  document.title = state.kb + " - RAG Console";
  $("nav-chat").setAttribute("href", "#/kb/" + encodeURIComponent(state.kb));
  rememberKb(state.kb);
  savePrefs();
  if (credential()) loadDocs();
}

function relativeTime(value) {
  if (!value) return "belum ada aktivitas";
  const at = new Date(value);
  if (isNaN(at.getTime())) return String(value).slice(0, 16);
  const minutes = Math.round((Date.now() - at.getTime()) / 60000);
  if (minutes < 1) return "baru saja";
  if (minutes < 60) return minutes + " menit lalu";
  const hours = Math.round(minutes / 60);
  if (hours < 24) return hours + " jam lalu";
  const days = Math.round(hours / 24);
  if (days < 30) return days + " hari lalu";
  return shortTime(value).slice(0, 10);
}

async function loadKbs() {
  if (!credential()) {
    $("kb-grid").innerHTML = "";
    note("kbs-status", "warn", "Masuk dengan kode akses, atau tempel <strong>API key</strong> di <a href=\"#/settings/conn\">Pengaturan &rsaquo; Koneksi</a>.");
    return;
  }
  note("kbs-status", "info", "Memuat knowledge base...");
  try {
    const data = await api("GET", "/knowledge-bases");
    state.kbs = data.knowledge_bases || [];
    if (state.flash) {
      note("kbs-status", "ok", state.flash);
      state.flash = "";
    } else {
      clearNote("kbs-status");
    }
  } catch (err) {
    state.kbs = [];
    note("kbs-status", "err", escapeHtml(err.message || "gagal memuat") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
  }
  renderKbs();
}

function renderKbs() {
  const known = new Set(state.kbs.map((item) => item.knowledge_base_id));
  // KB yang baru dibuat dari browser ini (belum ada dokumen) tetap terlihat.
  const drafts = recentKbs().filter((id) => !known.has(id)).map((id) => ({
    knowledge_base_id: id, documents: 0, chunks: 0, tokens: 0, completed: 0, processing: 0, failed: 0, updated_at: "", draft: true,
  }));
  const query = ($("kb-search").value || "").trim().toLowerCase();
  let items = state.kbs.concat(drafts).filter((item) => !query || item.knowledge_base_id.toLowerCase().indexOf(query) !== -1);
  if (state.kbSort === "name") items.sort((a, b) => a.knowledge_base_id.localeCompare(b.knowledge_base_id));
  else if (state.kbSort === "size") items.sort((a, b) => (b.chunks - a.chunks) || (b.documents - a.documents));
  else items.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));

  const totals = state.kbs.reduce((acc, item) => {
    acc.docs += item.documents; acc.chunks += item.chunks; acc.busy += item.processing; acc.failed += item.failed;
    return acc;
  }, { docs: 0, chunks: 0, busy: 0, failed: 0 });
  $("kb-stats").innerHTML = [
    ["Knowledge base", state.kbs.length],
    ["Dokumen", totals.docs],
    ["Potongan terindeks", totals.chunks],
    ["Sedang diproses", totals.busy],
  ].map((pair) => '<div class="stat"><span class="stat-v">' + escapeHtml(pair[1].toLocaleString("id-ID")) +
    '</span><span class="stat-k">' + escapeHtml(pair[0]) + "</span></div>").join("");

  $("kbs-empty").hidden = items.length > 0;
  $("kb-grid").innerHTML = items.map((item) => {
    const active = item.knowledge_base_id === state.kb ? " active" : "";
    const badges = [];
    if (item.draft) badges.push('<span class="tag">kosong</span>');
    if (item.processing) badges.push('<span class="tag warn">' + item.processing + " diproses</span>");
    if (item.failed) badges.push('<span class="tag off">' + item.failed + " gagal</span>");
    if (active) badges.push('<span class="tag ok">aktif</span>');
    return '<article class="kb-card' + active + '" data-kb="' + escapeHtml(item.knowledge_base_id) + '" tabindex="0">' +
      '<div class="kb-card-head"><span class="kb-icon"><svg class="ico"><use href="#i-db"/></svg></span>' +
      '<h3 class="mono" title="' + escapeHtml(item.knowledge_base_id) + '">' + escapeHtml(item.knowledge_base_id) + "</h3></div>" +
      '<div class="kb-card-meta"><span><b>' + item.documents.toLocaleString("id-ID") + "</b> dokumen</span>" +
      "<span><b>" + item.chunks.toLocaleString("id-ID") + "</b> potongan</span></div>" +
      '<div class="kb-card-foot"><span class="help">' + escapeHtml(relativeTime(item.updated_at)) + "</span>" +
      '<span class="kb-badges">' + badges.join("") + "</span></div>" +
      '<div class="kb-card-actions"><button class="btn sm primary" type="button" data-open="' + escapeHtml(item.knowledge_base_id) +
      '"><svg class="ico"><use href="#i-arrow"/></svg><span>Buka</span></button>' +
      '<button class="btn sm" type="button" data-eval="' + escapeHtml(item.knowledge_base_id) +
      '"><svg class="ico"><use href="#i-target"/></svg><span>Uji</span></button>' +
      '<button class="btn sm icon danger-ghost" type="button" data-delete-kb="' + escapeHtml(item.knowledge_base_id) +
      '" title="Hapus knowledge base" aria-label="Hapus knowledge base ' + escapeHtml(item.knowledge_base_id) +
      '"><svg class="ico"><use href="#i-trash"/></svg></button></div></article>';
  }).join("");
  $("kb-grid").querySelectorAll(".kb-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-delete-kb]");
      if (deleteButton) {
        openDeleteKb(deleteButton.dataset.deleteKb);
        return;
      }
      const evalButton = event.target.closest("[data-eval]");
      if (evalButton) {
        openKnowledgeBase(evalButton.dataset.eval);
        showView("eval");
        return;
      }
      showView("chat", card.dataset.kb);
    });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter") showView("chat", card.dataset.kb);
    });
  });
}

function forgetKb(id) {
  try {
    localStorage.setItem(RECENT_KB_KEY, JSON.stringify(recentKbs().filter((item) => item !== id)));
  } catch (err) {
    /* tidak bisa disimpan: daftar lokal tetap seperti semula */
  }
}

function openDeleteKb(id) {
  const item = state.kbs.find((entry) => entry.knowledge_base_id === id) ||
    { knowledge_base_id: id, documents: id === state.kb ? state.docs.length : 0, chunks: 0, tables: 0, draft: true };
  state.deleting = item;
  $("delkb-name").textContent = id;
  $("delkb-confirm").value = "";
  $("delkb-confirm").placeholder = id;
  $("delkb-ok").disabled = true;
  clearNote("delkb-status");
  $("delkb-stats").innerHTML = [["Dokumen", item.documents || 0], ["Potongan", item.chunks || 0], ["Sedang diproses", item.processing || 0]]
    .map((pair) => '<div class="stat"><span class="stat-v">' + escapeHtml(Number(pair[1]).toLocaleString("id-ID")) +
      '</span><span class="stat-k">' + escapeHtml(pair[0]) + "</span></div>").join("");
  $("dlg-delkb").showModal();
  $("delkb-confirm").focus();
}

async function confirmDeleteKb() {
  const item = state.deleting;
  if (!item || $("delkb-confirm").value.trim() !== item.knowledge_base_id) return;
  const id = item.knowledge_base_id;
  $("delkb-ok").disabled = true;
  note("delkb-status", "info", "Menghapus...");
  let summary = "tidak ada dokumen";
  const onServer = state.kbs.some((entry) => entry.knowledge_base_id === id);
  try {
    if (onServer || !item.draft) {
      const data = await api("DELETE", "/knowledge-bases/" + encodeURIComponent(id) + "?confirm=" + encodeURIComponent(id));
      summary = data.deleted_documents + " dokumen beserta seluruh isinya" +
        (data.deleted_tables ? " dan " + data.deleted_tables + " tabel" : "");
    }
  } catch (err) {
    // KB yang baru dibuat di browser ini (belum ada dokumen) memang tidak ada di server.
    if (err.code !== "DOCUMENT_NOT_FOUND") {
      note("delkb-status", "err", escapeHtml(err.message || "gagal menghapus") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>");
      $("delkb-ok").disabled = false;
      return;
    }
  }
  forgetKb(id);
  state.kbs = state.kbs.filter((entry) => entry.knowledge_base_id !== id);
  if (state.kb === id) {
    state.picked.clear();
    state.docs = [];
    renderDocs([]);
    newConversation();
  }
  $("dlg-delkb").close();
  state.deleting = null;
  state.flash = "Knowledge base <span class=\"mono\">" + escapeHtml(id) + "</span> dihapus (" + escapeHtml(summary) + ").";
  if (currentView() !== "kbs") showView("kbs");
  else loadKbs();
}

function newKbDialog() {
  $("newkb-name").value = "";
  $("newkb-preview").textContent = "Huruf kecil, angka, garis bawah, dan tanda hubung.";
  $("dlg-newkb").showModal();
  $("newkb-name").focus();
}

function renderEvalKbs() {
  const ids = Array.from(new Set([state.kb].concat(state.kbs.map((item) => item.knowledge_base_id), recentKbs())));
  $("eval-kb-select").innerHTML = ids.map((id) =>
    '<option value="' + escapeHtml(id) + '"' + (id === state.kb ? " selected" : "") + ">" + escapeHtml(id) + "</option>").join("");
  $("eval-kb").textContent = state.kb;
  if (!state.kbs.length && credential()) {
    api("GET", "/knowledge-bases").then((data) => {
      state.kbs = data.knowledge_bases || [];
      if (state.view === "eval") renderEvalKbs();
    }).catch(() => {});
  }
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
      const number = source.index || index + 1;
      const cited = source.cited ? " cited" : "";
      const title = source.cited ? "dikutip di jawaban" : "dibaca model, tidak dikutip";
      return '<span class="cite' + cited + '" title="' + title + '"><b>[' + number + "]</b>" + escapeHtml(label) + page + link + " " + score + "</span>";
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
  if (!data.computed && usage.best_score != null) {
    chips.push(["skor terbaik", fmtScore(usage.best_score) + (usage.relevance_gate ? " (" + (GATE_LABELS[usage.relevance_gate] || usage.relevance_gate) + ")" : "")]);
  }
  if (usage.search_query) chips.push(["dicari bersama pertanyaan sebelumnya", "ya"]);
  chips.push(["retrieval_ms", usage.retrieval_ms != null ? usage.retrieval_ms : "-"]);
  chips.push(["rendering_ms", usage.generation_ms != null ? Math.round(usage.generation_ms) : "-"]);
  if (usage.computed_rows) chips.push(["baris dihitung", usage.computed_rows]);
  if (data.no_answer_reason) chips.push(["alasan", NO_ANSWER_REASONS[data.no_answer_reason] || data.no_answer_reason]);

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
  const verdict = data.relevant
    ? '<span class="tag ok">cukup relevan untuk dijawab</span>'
    : '<span class="tag warn">akan dijawab "tidak ditemukan"</span>';
  bubble.innerHTML = "Pencarian saja (tanpa model): <b>" + results.length + "</b> hasil. Skor terbaik <b>" +
    escapeHtml(fmtScore(data.best_score)) + "</b> " + (results.length ? verdict : "");
  if (results.length) {
    const wrap = document.createElement("div");
    wrap.className = "hit-wrap";
    const rows = results.map((hit, index) => {
      const label = hit.document_name || hit.document_id;
      const where = [hit.section, hit.page ? "hal. " + hit.page : ""].filter(Boolean).join(" · ");
      const preview = String(hit.content || "").replace(/\s+/g, " ").slice(0, 220);
      return "<tr><td>" + (index + 1) + "</td><td><b>" + escapeHtml(label) + "</b>" +
        (where ? '<div class="help">' + escapeHtml(where) + "</div>" : "") +
        '<div class="hit-preview">' + escapeHtml(preview) + (String(hit.content || "").length > 220 ? "…" : "") + "</div></td>" +
        '<td class="mono">' + fmtScore(hit.rerank_score != null ? hit.rerank_score : hit.score) + "</td>" +
        '<td class="mono">' + fmtScore(hit.sparse_score) + "</td>" +
        '<td class="mono">' + fmtScore(hit.dense_score) + "</td></tr>";
    }).join("");
    wrap.innerHTML = '<table class="key-table hit-table"><thead><tr><th>#</th><th>Potongan</th>' +
      '<th title="Skor akhir reranker (0-1)">Skor</th><th title="Skor kata kunci BM25 (relatif)">Kata kunci</th>' +
      '<th title="Kemiripan vektor">Vektor</th></tr></thead><tbody>' + rows + "</tbody></table>";
    node.appendChild(wrap);
  }
  const meta = document.createElement("div");
  meta.className = "meta";
  const chips = [
    ["retrieval_ms", data.retrieval_ms != null ? data.retrieval_ms : "-"],
    ["reranker", data.reranker || "-"],
    ["kata kunci / vektor", (data.sparse_hits || 0) + " / " + (data.dense_hits || 0)],
    ["bobot vektor", data.dense_weight != null ? data.dense_weight : "-"],
    ["gerbang", GATE_LABELS[data.relevance_gate] || data.relevance_gate || "-"],
  ];
  meta.innerHTML = chips.map((pair) =>
    "<span><span class=\"k\">" + escapeHtml(pair[0]) + ":</span><span class=\"v\">" + escapeHtml(pair[1]) + "</span></span>").join("");
  node.appendChild(meta);
  if (!data.relevant) node.classList.add("no-answer");
}

// Opsi per permintaan: hanya yang DIUBAH di browser ini yang dikirim; sisanya ikut setelan layanan.
function queryOptions() {
  const options = { include_sources: true };
  const opts = state.opts;
  if (opts.top_k) options.top_k = Number(opts.top_k);
  if (opts.threshold !== null && opts.threshold !== "" && opts.threshold !== undefined) options.threshold = Number(opts.threshold);
  if (opts.strict !== null && opts.strict !== undefined) options.strict_grounding = !!opts.strict;
  if (opts.hybrid !== null && opts.hybrid !== undefined) options.use_hybrid = !!opts.hybrid;
  if (opts.reranker !== null && opts.reranker !== undefined) options.use_reranker = !!opts.reranker;
  if (opts.route) options.route = opts.route;
  if (state.picked.size) options.document_ids = Array.from(state.picked);
  return options;
}

function searchOptions() {
  const options = queryOptions();
  delete options.include_sources;
  delete options.strict_grounding;
  return options;
}

async function ask(query) {
  addMessage("user", { html: escapeHtml(query) });
  const node = addMessage("assistant", { html: '<span class="pending">Menyusun jawaban</span>' });
  const searching = state.mode === "search";
  const body = { query: query, knowledge_base_id: state.kb };
  if (searching) {
    body.top_k = Number(state.opts.top_k) || 10;
    body.options = searchOptions();
  } else {
    body.options = queryOptions();
    if (state.opts.memory && state.history.length) body.history = state.history.slice(-HISTORY_TURNS);
  }
  try {
    const data = await api("POST", searching ? "/search" : "/query", body);
    if (searching) {
      renderHits(node, data);
    } else {
      renderAnswer(node, data);
      if (state.opts.memory) {
        state.history.push({ role: "user", content: query.slice(0, 2000) });
        if (data.grounded && data.answer) state.history.push({ role: "assistant", content: String(data.answer).slice(0, 2000) });
        state.history = state.history.slice(-HISTORY_TURNS * 2);
      }
    }
  } catch (err) {
    node.querySelector(".bubble").textContent = "Gagal: " + (err.message || err.code || "tidak diketahui");
    node.classList.add("no-answer");
  }
}

function newConversation() {
  state.history = [];
  $("thread-inner").querySelectorAll(".msg").forEach((item) => item.remove());
  $("thread-empty").hidden = false;
}

const NO_ANSWER_REASONS = {
  below_threshold: "skor relevansi terbaik di bawah ambang minimum",
  no_candidates: "tidak ada potongan yang memuat kata dari pertanyaan",
  strict_grounding: "model menyatakan konteks tidak cukup",
  context_empty: "konteks kosong",
  answer_truncated: "jawaban terpotong batas token",
  table_plan_invalid: "perhitungan tabel tidak bisa dilakukan",
};

const GATE_LABELS = {
  lexical: "cocok kata kunci",
  semantic: "cocok makna (vektor)",
  reranker_off: "reranker mati, tanpa gerbang",
  gate_off: "gerbang dimatikan",
  below_min_relevance: "di bawah ambang",
  no_candidates: "tanpa kandidat",
};

function fmtScore(value) {
  return typeof value === "number" ? value.toFixed(2) : "-";
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
    $("llm-temperature").value = llm.temperature != null ? llm.temperature : 0.1;
    $("llm-top-p").value = llm.top_p != null ? llm.top_p : 0.9;
    $("llm-frequency-penalty").value = llm.frequency_penalty != null ? llm.frequency_penalty : 0;
    $("llm-frequency-help").classList.toggle("warn-text", Number(llm.frequency_penalty) > 0);
    $("llm-repair-attempts").value = llm.repair_attempts != null ? llm.repair_attempts : 1;
    $("llm-strip-foreign").checked = llm.strip_foreign_scripts !== false;
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
    loadEngineNote();
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
    state.kbs = [];
  }
}

async function saveLlm() {
  const payload = {
    provider: $("llm-provider").value,
    base_url: $("llm-base").value.trim(),
    model: $("llm-model").value.trim(),
    max_tokens: Number($("llm-max-tokens").value) || 8192,
    temperature: Number($("llm-temperature").value),
    top_p: Number($("llm-top-p").value),
    frequency_penalty: Number($("llm-frequency-penalty").value),
    repair_attempts: Number($("llm-repair-attempts").value),
    strip_foreign_scripts: $("llm-strip-foreign").checked,
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

function triState(value) {
  return value === "on" ? true : value === "off" ? false : null;
}

function triValue(value) {
  return value === true ? "on" : value === false ? "off" : "";
}

function renderBrowserOptions() {
  $("r-topk").value = state.opts.top_k || "";
  $("r-threshold").value = state.opts.threshold === null || state.opts.threshold === undefined ? "" : state.opts.threshold;
  $("r-route").value = state.opts.route || "";
  $("r-strict").value = triValue(state.opts.strict);
  $("r-hybrid").value = triValue(state.opts.hybrid);
  $("r-reranker").value = triValue(state.opts.reranker);
  $("r-memory").checked = state.opts.memory !== false;
}

function saveRetrieval() {
  const threshold = $("r-threshold").value.trim();
  state.opts = {
    top_k: Number($("r-topk").value) || null,
    threshold: threshold === "" ? null : Number(threshold),
    strict: triState($("r-strict").value),
    hybrid: triState($("r-hybrid").value),
    reranker: triState($("r-reranker").value),
    route: $("r-route").value,
    memory: $("r-memory").checked,
  };
  if (!state.opts.memory) state.history = [];
  savePrefs();
  note("retr-status", "ok", "Pilihan disimpan untuk browser ini.");
}

function resetBrowserOptions() {
  state.opts = { top_k: null, threshold: null, strict: null, hybrid: null, reranker: null, route: "", memory: true };
  renderBrowserOptions();
  savePrefs();
  note("retr-status", "ok", "Semua pilihan kembali <b>ikut setelan layanan</b>.");
}

function syncRangeLabels() {
  [["s-min-relevance", "s-min-relevance-val"], ["s-rel-threshold", "s-rel-threshold-val"], ["s-hash-weight", "s-hash-weight-val"]]
    .forEach((pair) => {
      const value = Number($(pair[0]).value);
      $(pair[1]).textContent = pair[0] === "s-min-relevance" && value === 0 ? "mati" : value.toFixed(2);
    });
}

function setNumber(id, value) {
  if (value !== null && value !== undefined) $(id).value = value;
}

function renderRetrievalService(retrieval) {
  setNumber("s-context-tokens", retrieval.context_max_tokens);
  setNumber("s-topk", retrieval.final_top_k);
  setNumber("s-maxchunks", retrieval.max_chunks_per_document);
  setNumber("s-min-relevance", retrieval.min_relevance);
  setNumber("s-rel-threshold", retrieval.relevance_threshold);
  setNumber("s-neighbor", retrieval.context_neighbor_chunks);
  setNumber("s-fulldoc", retrieval.context_full_document_tokens);
  setNumber("s-expand-docs", retrieval.context_expand_max_documents);
  setNumber("s-hash-weight", retrieval.hash_dense_weight);
  $("s-expand").checked = retrieval.context_expand_documents !== false;
  $("s-reranker").checked = retrieval.reranker_enabled !== false;
  $("s-strict").checked = retrieval.strict_grounding !== false;
  if (retrieval.reranker_provider) $("s-reranker-provider").value = retrieval.reranker_provider;
  syncRangeLabels();
  markPreset();
}

function collectRetrieval() {
  return {
    context_max_tokens: Number($("s-context-tokens").value) || 24000,
    final_top_k: Number($("s-topk").value) || 12,
    max_chunks_per_document: Number($("s-maxchunks").value) || 8,
    min_relevance: Number($("s-min-relevance").value),
    relevance_threshold: Number($("s-rel-threshold").value),
    context_neighbor_chunks: Number($("s-neighbor").value),
    context_full_document_tokens: Number($("s-fulldoc").value),
    context_expand_max_documents: Number($("s-expand-docs").value) || 3,
    hash_dense_weight: Number($("s-hash-weight").value),
    context_expand_documents: $("s-expand").checked,
    reranker_enabled: $("s-reranker").checked,
    reranker_provider: $("s-reranker-provider").value,
    strict_grounding: $("s-strict").checked,
  };
}

function applyPreset(name) {
  const preset = PRESETS[name];
  if (!preset) return;
  renderRetrievalService(Object.assign(collectRetrieval(), preset, { reranker_enabled: true }));
  note("retr-svc-status", "info", "Preset <b>" + escapeHtml(name === "accurate" ? "Akurat" : name === "balanced" ? "Seimbang" : "Dokumen lengkap") +
    "</b> diisikan. Klik <b>Simpan kualitas jawaban</b> untuk menerapkan.");
}

function markPreset() {
  const current = collectRetrieval();
  document.querySelectorAll("[data-preset]").forEach((button) => {
    const preset = PRESETS[button.dataset.preset];
    const same = Object.keys(preset).every((key) => String(preset[key]) === String(current[key]));
    button.setAttribute("aria-pressed", same ? "true" : "false");
  });
}

async function loadEngineNote() {
  try {
    const ready = await api("GET", "/ready");
    const detail = ready.detail || {};
    const provider = detail.embedding_provider || "";
    if (!provider) return;
    if (provider === "hash") {
      note("retr-engine-note", "warn", "Pencarian makna: <span class=\"mono\">hash</span> &mdash; layanan mencari berdasarkan <b>kata</b> (dengan bentuk dasar kata Indonesia), belum memahami sinonim seperti <i>karyawan/pegawai</i>. Pertanyaan yang memakai istilah lain dari dokumen bisa tidak ditemukan; pasang embedder semantik untuk mengatasinya.");
    } else {
      note("retr-engine-note", "ok", "Pencarian makna aktif: <span class=\"mono\">" + escapeHtml(detail.embedding_model || provider) + "</span>.");
    }
  } catch (err) {
    clearNote("retr-engine-note");
  }
}

async function saveRetrievalService() {
  const payload = { retrieval: collectRetrieval() };
  note("retr-svc-status", "info", "Menyimpan...");
  try {
    const data = await api("PUT", "/settings", payload);
    note("retr-svc-status", "ok", "Tersimpan dan langsung berlaku.");
    if (data.sections && data.sections.retrieval) renderRetrievalService(data.sections.retrieval);
  } catch (err) {
    const forbidden = err.code === "AUTH_FORBIDDEN";
    const message = forbidden
      ? "Setelan layanan hanya bisa diubah dengan kunci berizin <strong>admin</strong> atau kunci organisasi operator."
      : escapeHtml(err.message || "gagal menyimpan") + " <span class=\"mono\">(" + escapeHtml(err.code || "") + ")</span>";
    note("retr-svc-status", forbidden ? "warn" : "err", message);
  }
}

/* ------------------------------------------------------------ uji akurasi */

function parseEvalCases(text) {
  return String(text || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split("=>");
    const query = parts[0].trim();
    const expected = parts.length > 1 ? parts.slice(1).join("=>").trim() : null;
    return { query: query, expected: expected };
  }).filter((item) => item.query);
}

function docMatches(hit, expected) {
  const wanted = expected.toLowerCase();
  return String(hit.document_name || "").toLowerCase().indexOf(wanted) !== -1 ||
    String(hit.document_id || "").toLowerCase() === wanted;
}

function setEvalMode(mode) {
  state.evalMode = mode;
  $("btn-eval-mode-search").setAttribute("aria-pressed", mode === "search" ? "true" : "false");
  $("btn-eval-mode-answer").setAttribute("aria-pressed", mode === "answer" ? "true" : "false");
}

async function evalOne(item) {
  if (state.evalMode === "answer") {
    const data = await api("POST", "/query", { query: item.query, knowledge_base_id: state.kb, options: queryOptions() });
    const docs = [];
    (data.sources || []).filter((s) => s.cited).concat(data.sources || []).forEach((s) => {
      if (!docs.some((d) => d.document_id === s.document_id)) docs.push(s);
    });
    return { docs: docs, relevant: !!data.grounded, best: (data.usage || {}).best_score, answer: data.answer || "" };
  }
  const data = await api("POST", "/search", { query: item.query, knowledge_base_id: state.kb, top_k: 10, options: searchOptions() });
  const docs = [];
  (data.results || []).forEach((hit) => {
    if (!docs.some((d) => d.document_id === hit.document_id)) docs.push(hit);
  });
  return { docs: docs, relevant: !!data.relevant, best: data.best_score, answer: "" };
}

async function runEval() {
  const cases = parseEvalCases($("eval-cases").value);
  try {
    localStorage.setItem(EVAL_KEY, $("eval-cases").value);
  } catch (err) {
    /* tidak bisa diingat: tetap dijalankan */
  }
  if (!cases.length) {
    note("eval-status", "err", "Tulis minimal satu pertanyaan uji.");
    return;
  }
  state.evalStop = false;
  $("btn-eval-run").disabled = true;
  $("btn-eval-stop").hidden = false;
  $("eval-table").hidden = false;
  $("eval-rows").innerHTML = "";
  clearNote("eval-summary");
  const tally = { expected: 0, hit1: 0, hit3: 0, refuse_ok: 0, refuse_total: 0, false_refusal: 0, done: 0 };
  for (let index = 0; index < cases.length; index += 1) {
    if (state.evalStop) break;
    const item = cases[index];
    note("eval-status", "info", "Menguji " + (index + 1) + " dari " + cases.length + "...");
    const row = document.createElement("tr");
    $("eval-rows").appendChild(row);
    let status;
    let tone;
    let top = "-";
    let expectedCell = item.expected === null ? '<span class="help">tidak ditentukan</span>' : escapeHtml(item.expected === "-" ? "harus ditolak" : item.expected);
    let best = "-";
    try {
      const result = await evalOne(item);
      top = result.relevant && result.docs[0] ? escapeHtml(result.docs[0].document_name || result.docs[0].document_id) : "-";
      best = fmtScore(result.best);
      if (item.expected === "-") {
        tally.refuse_total += 1;
        if (!result.relevant) { tally.refuse_ok += 1; status = "Ditolak (benar)"; tone = "ok"; }
        else { status = "Tidak ditolak"; tone = "warn"; }
      } else if (item.expected) {
        tally.expected += 1;
        const rank = result.relevant ? result.docs.findIndex((doc) => docMatches(doc, item.expected)) + 1 : 0;
        if (!result.relevant) { tally.false_refusal += 1; status = "Ditolak (seharusnya dijawab)"; tone = "off"; }
        else if (rank === 1) { tally.hit1 += 1; tally.hit3 += 1; status = "Lulus"; tone = "ok"; }
        else if (rank > 1 && rank <= 3) { tally.hit3 += 1; status = "Peringkat " + rank; tone = "warn"; }
        else if (rank > 3) { status = "Peringkat " + rank; tone = "warn"; }
        else { status = "Dokumen tidak ketemu"; tone = "off"; }
        if (rank) expectedCell += ' <span class="help">(peringkat ' + rank + ")</span>";
      } else {
        status = result.relevant ? "Dijawab" : "Ditolak";
        tone = result.relevant ? "ok" : "warn";
      }
      if (result.answer) top += '<div class="hit-preview">' + escapeHtml(String(result.answer).slice(0, 180)) + "</div>";
    } catch (err) {
      status = "Gagal: " + (err.code || "error");
      tone = "off";
    }
    tally.done += 1;
    row.innerHTML = "<td>" + (index + 1) + "</td><td>" + escapeHtml(item.query) + "</td><td>" + top + "</td><td>" + expectedCell +
      '</td><td class="mono">' + best + '</td><td><span class="tag ' + tone + '">' + escapeHtml(status) + "</span></td>";
  }
  const parts = [];
  if (tally.expected) {
    parts.push("Dokumen benar di peringkat 1: <b>" + tally.hit1 + "/" + tally.expected + "</b> (" + Math.round(100 * tally.hit1 / tally.expected) + "%)");
    parts.push("di 3 teratas: <b>" + tally.hit3 + "/" + tally.expected + "</b>");
    parts.push("salah ditolak: <b>" + tally.false_refusal + "</b>");
  }
  if (tally.refuse_total) parts.push("pertanyaan di luar knowledge yang ditolak: <b>" + tally.refuse_ok + "/" + tally.refuse_total + "</b>");
  note("eval-summary", tally.false_refusal || (tally.expected && tally.hit1 < tally.expected) ? "warn" : "ok",
    parts.join(" &middot; ") || "Selesai " + tally.done + " pertanyaan.");
  note("eval-status", "ok", state.evalStop ? "Dihentikan." : "Selesai " + tally.done + " pertanyaan.");
  $("btn-eval-run").disabled = false;
  $("btn-eval-stop").hidden = true;
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
  const kbs = (entry.knowledge_base_ids || []).map((kb) => '<span class="tag">' + escapeHtml(kb) + "</span>").join(" ") ||
    '<span class="help">semua</span>';
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
    "<td>" + kbs + "</td>" +
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
  const kbs = $("key-kbs").value.split(",").map((item) => item.trim()).filter(Boolean);
  if (kbs.length) payload.knowledge_base_ids = kbs;
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
    $("key-kbs").value = "";
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
  $("btn-kbs-refresh").addEventListener("click", loadKbs);
  $("btn-kb-new").addEventListener("click", newKbDialog);
  $("btn-kb-delete").addEventListener("click", () => openDeleteKb(state.kb));
  $("delkb-confirm").addEventListener("input", () => {
    $("delkb-ok").disabled = !state.deleting || $("delkb-confirm").value.trim() !== state.deleting.knowledge_base_id;
  });
  $("delkb-confirm").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmDeleteKb();
    }
  });
  $("delkb-ok").addEventListener("click", confirmDeleteKb);
  $("kb-search").addEventListener("input", renderKbs);
  document.querySelectorAll("[data-kb-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      state.kbSort = button.dataset.kbSort;
      document.querySelectorAll("[data-kb-sort]").forEach((other) => {
        other.setAttribute("aria-pressed", other === button ? "true" : "false");
      });
      renderKbs();
    });
  });
  $("newkb-name").addEventListener("input", () => {
    const id = cleanKbId($("newkb-name").value);
    $("newkb-preview").innerHTML = id ? "Akan dibuat sebagai <span class=\"mono\">" + escapeHtml(id) + "</span>" : "Huruf kecil, angka, garis bawah, dan tanda hubung.";
  });
  $("dlg-newkb").addEventListener("close", () => {
    if ($("dlg-newkb").returnValue !== "ok") return;
    const id = cleanKbId($("newkb-name").value);
    if (!id) return;
    rememberKb(id);
    showView("chat", id);
  });
  $("eval-kb-select").addEventListener("change", () => {
    openKnowledgeBase($("eval-kb-select").value);
    renderEvalKbs();
  });
  document.querySelectorAll("#settings-nav [data-panel]").forEach((button) => {
    button.addEventListener("click", () => selectPanel(button.dataset.panel));
  });
  window.addEventListener("hashchange", route);

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
  $("btn-reset-retr").addEventListener("click", resetBrowserOptions);
  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => applyPreset(button.dataset.preset));
  });
  ["s-min-relevance", "s-rel-threshold", "s-hash-weight"].forEach((id) => $(id).addEventListener("input", () => { syncRangeLabels(); markPreset(); }));
  ["s-topk", "s-neighbor", "s-fulldoc", "s-context-tokens", "s-maxchunks", "s-expand-docs", "s-expand", "s-strict"]
    .forEach((id) => $(id).addEventListener("change", markPreset));
  $("btn-new-chat").addEventListener("click", newConversation);
  $("btn-eval-run").addEventListener("click", runEval);
  $("btn-eval-stop").addEventListener("click", () => { state.evalStop = true; });
  $("btn-eval-mode-search").addEventListener("click", () => setEvalMode("search"));
  $("btn-eval-mode-answer").addEventListener("click", () => setEvalMode("answer"));
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
  route();
  // Tombol Keluar hanya bermakna bila ada sesi (bukan pada mode "cukup API key");
  // applyConsoleMode() di bawah menetapkan keadaan akhirnya setelah mode diketahui.
  try {
    const gate = await loadGate();
    if (gate && gate.api_key_only) {
      // Konsol tanpa gerbang kode akses: cukup kunci API. Arahkan operator ke panel Koneksi,
      // dan jangan tampilkan layar kode akses yang tidak akan pernah menerima apa pun.
      state.apiKeyOnly = true;
      applyConsoleMode();
      if (state.key) {
        const connected = await checkConnection();
        if (connected) route();
      } else {
        setConnection("warn", "tempel kunci API");
        note("conn-status", "warn", "Konsol ini tidak memakai kode akses. Tempel <strong>kunci API</strong> sekali di panel <strong>Koneksi</strong> - kunci itu langsung membuka semua fitur, termasuk Pengaturan.");
        showView("settings", "conn");
      }
      return;
    }
    applyConsoleMode();
    if (gate && gate.enabled && !state.session) {
      showGate("Konsol ini memakai kode akses. Masukkan kodenya untuk mulai.");
      return;
    }
    if (state.session || state.key) {
      const ok = await checkConnection();
      if (ok) route();
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
