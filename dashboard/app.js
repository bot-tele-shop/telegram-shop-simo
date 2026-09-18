/* Digital Shelf owner console — Supabase Auth for sign-in, Worker /admin/api for data. */
"use strict";

const SUPABASE_URL = `https://${CONFIG.projectRef}.supabase.co`;
const API = `${CONFIG.workerUrl}/admin/api`;
const TOKEN_KEY = "ds_session";

let session = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null");
let productsCache = [];
let stockCache = [];
let stockStateFilter = "";
let orderStateFilter = "";
let pendingConfirm = null;

const $ = (id) => document.getElementById(id);

/* ---------- session ---------- */
function token() {
  return session && session.access_token;
}

function saveSession(data) {
  session = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Date.now() + (Number(data.expires_in || 3600) - 30) * 1000,
  };
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(session));
}

async function refreshSession() {
  if (!session || !session.refresh_token) return false;
  const resp = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=refresh_token`, {
    method: "POST",
    headers: { apikey: CONFIG.anonKey, "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: session.refresh_token }),
  });
  const data = await resp.json();
  if (!resp.ok) return false;
  saveSession(data);
  return true;
}

async function api(path, { method = "GET", body } = {}, retried = false) {
  if (session && session.expires_at && Date.now() > session.expires_at) {
    await refreshSession();
  }
  const resp = await fetch(`${API}/${path}`, {
    method,
    headers: {
      authorization: `Bearer ${token()}`,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if ((resp.status === 401 || resp.status === 403) && !retried && (await refreshSession())) {
    return api(path, { method, body }, true);
  }
  if (resp.status === 401 || resp.status === 403) {
    signOut();
    throw new Error(data.error || "not authorized");
  }
  if (!resp.ok || data.ok === false) throw new Error(data.error || `HTTP ${resp.status}`);
  return data.data;
}

async function signIn(email, password) {
  const resp = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: CONFIG.anonKey, "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error_description || data.msg || "sign-in failed");
  saveSession(data);
}

function signOut() {
  session = null;
  sessionStorage.removeItem(TOKEN_KEY);
  $("shell").hidden = true;
  $("login-screen").hidden = false;
}

/* ---------- helpers ---------- */
function toast(msg, isError = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 4200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function timeAgo(iso) {
  if (!iso) return "—";
  const seconds = Math.max(1, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.floor(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  const days = hours / 24;
  if (days < 30) return `${Math.floor(days)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function confirmAction(title, body, okLabel = "Confirm") {
  return new Promise((resolve) => {
    pendingConfirm = resolve;
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm-ok").textContent = okLabel;
    $("confirm-dialog").showModal();
  });
}

function skeletons(target, count, height) {
  target.innerHTML = Array.from(
    { length: count },
    () => `<div class="skeleton" style="height:${height}px"></div>`,
  ).join("");
}

const ORDER_BADGE = {
  delivered: "ok",
  delivering: "info",
  paid: "info",
  invoice: "dim",
  checkout: "dim",
  expired: "dim",
  cancelled: "dim",
  refunded: "dim",
  delivery_failed: "warn",
  needs_refund: "bad",
  refund_pending: "bad",
};

const STOCK_BADGE = { available: "ok", reserved: "info", sold: "dim", quarantined: "bad" };

function stockBadge(state) {
  return `<span class="badge ${STOCK_BADGE[state] || "dim"}">${esc(state)}</span>`;
}

function orderBadge(state) {
  return `<span class="badge ${ORDER_BADGE[state] || "dim"}">${esc(state.replace(/_/g, " "))}</span>`;
}

/* ---------- navigation ---------- */
function showView(name, { deferLoad = false } = {}) {
  for (const s of document.querySelectorAll("main section")) s.hidden = true;
  $(`view-${name}`).hidden = false;
  for (const b of document.querySelectorAll("#nav button[data-view]"))
    b.classList.toggle("active", b.dataset.view === name);
  if (!deferLoad) loadView(name);
}

async function loadView(name) {
  try {
    if (name === "overview") await loadOverview();
    if (name === "earnings") await loadEarnings();
    if (name === "products") await loadProducts();
    if (name === "stock") await loadStock();
    if (name === "orders") await loadOrders();
    if (name === "activity") await loadActivity();
    if (name === "settings") await loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- overview ---------- */
async function loadOverview() {
  skeletons($("overview-cards"), 4, 84);
  // health/failed endpoints degrade gracefully on an older Worker deploy.
  const optional = (p) => p.catch(() => null);
  const [o, products, health, failed] = await Promise.all([
    api("overview"),
    api("products"),
    optional(api("health")),
    optional(api("updates/failed")),
  ]);
  productsCache = products;

  const pill = $("shop-status");
  pill.textContent = o.checkout_paused ? "checkout paused" : "checkout open";
  pill.classList.toggle("paused", !!o.checkout_paused);

  const banner = $("setup-banner");
  if (o.products.active === 0) {
    banner.textContent = "No products are visible to buyers yet. Create a product, upload stock, then activate it.";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }

  const cards = [
    { label: "active products", value: `${o.products.active}/${o.products.total}` },
    { label: "codes in stock", value: o.stock.available, accent: true },
    { label: "paid orders", value: o.orders.paid },
    { label: "awaiting delivery", value: o.orders.pending_delivery, alert: o.orders.pending_delivery > 0 },
    { label: "need refund", value: o.orders.needs_refund, alert: o.orders.needs_refund > 0 },
    { label: "payment reviews", value: o.review_alerts, alert: o.review_alerts > 0 },
    { label: "failed updates", value: o.failed_updates, alert: o.failed_updates > 0 },
    { label: "open invoices", value: o.orders.open_invoices },
  ];
  $("overview-cards").innerHTML = cards
    .map(
      (c, i) => `
      <div class="stat-card${c.accent ? " accent" : ""}${c.alert ? " alert" : ""}" style="animation-delay:${i * 40}ms">
        <span class="stat-value">${esc(c.value)}</span>
        <span class="stat-label">${esc(c.label)}</span>
      </div>`,
    )
    .join("");

  const alerts = [];
  if (o.orders.needs_refund > 0)
    alerts.push({ text: "orders are waiting on a refund decision", count: o.orders.needs_refund, view: "orders" });
  if (o.orders.pending_delivery > 0)
    alerts.push({ text: "paid orders are stuck or failed delivery", count: o.orders.pending_delivery, view: "orders" });
  if (o.review_alerts > 0)
    alerts.push({ text: "payment events need review (wrong amount, unknown order, replay)", count: o.review_alerts, view: "orders" });
  if (o.failed_updates > 0)
    alerts.push({ text: "Telegram updates failed permanently", count: o.failed_updates });
  $("overview-alerts").innerHTML = alerts.length
    ? alerts
        .map(
          (a) => `
        <div class="alert-row">
          <span><b class="count">${esc(a.count)}</b> ${esc(a.text)}</span>
          ${a.view ? `<button class="btn small ghost" data-goto="${a.view}">View</button>` : ""}
        </div>`,
        )
        .join("")
    : `<div class="empty-state">All clear — nothing needs your attention.</div>`;

  const low = products.filter((p) => p.active && p.source === "stock" && p.available <= 3);
  $("overview-lowstock").innerHTML = low.length
    ? low
        .map(
          (p) => `
        <div class="lowstock-row">
          <span>${esc(p.title)} <span class="mono">${esc(p.sku)}</span></span>
          <span>
            <b class="mono" style="color:var(--red)">${esc(p.available)}</b> left
            <button class="btn small" data-addstock="${esc(p.sku)}">Add stock</button>
          </span>
        </div>`,
        )
        .join("")
    : `<div class="empty-state">Stock levels look healthy.</div>`;

  renderSystem(health);
  renderFailedUpdates(failed);
}

/* ---------- system health ---------- */
function renderSystem(h) {
  const el = $("overview-system");
  if (!h) {
    el.innerHTML = `<div class="empty-state">System status needs the latest Worker deploy.</div>`;
    return;
  }
  const rows = [];
  if (!h.webhook) {
    rows.push({ k: "Telegram webhook", v: "unreachable — bot token or Telegram API problem", bad: true });
  } else if (!h.webhook.url) {
    rows.push({ k: "Telegram webhook", v: "not registered — run the registration workflow", bad: true });
  } else {
    rows.push({ k: "Telegram webhook", v: `registered · ${h.webhook.pending_updates} pending` });
    if (h.webhook.last_error)
      rows.push({
        k: "last webhook error",
        v: `${h.webhook.last_error} (${timeAgo((h.webhook.last_error_at || 0) * 1000)})`,
        bad: true,
      });
  }
  rows.push({
    k: "stuck processing",
    v: `${h.stuck_updates.length} update${h.stuck_updates.length === 1 ? "" : "s"}`,
    bad: h.stuck_updates.length > 0,
  });
  rows.push({
    k: "stuck deliveries",
    v: `${h.stuck_deliveries.length} order${h.stuck_deliveries.length === 1 ? "" : "s"}`,
    bad: h.stuck_deliveries.length > 0,
  });
  el.innerHTML = rows
    .map(
      (r) => `
      <div class="kv-row">
        <span>${esc(r.k)}</span>
        <b${r.bad ? ' style="color:var(--red)"' : ""}>${esc(r.v)}</b>
      </div>`,
    )
    .join("");
}

/* ---------- failed updates ---------- */
function renderFailedUpdates(rows) {
  const el = $("overview-failed");
  if (!rows) {
    el.innerHTML = `<div class="empty-state">Failed-update inspection needs the latest Worker deploy.</div>`;
    return;
  }
  el.innerHTML = rows.length
    ? rows
        .map(
          (u) => `
        <div class="alert-row">
          <span>
            <b class="mono">#${esc(u.update_id)}</b> ${esc(u.kind)} ·
            ${esc(u.attempts)} attempt${u.attempts === 1 ? "" : "s"} · ${esc(timeAgo(u.updated_at))}
            <br><span class="hint">${esc(u.last_error || "unknown error")}</span>
          </span>
          <button class="btn small" data-retry="${esc(u.update_id)}">Retry</button>
        </div>`,
        )
        .join("")
    : `<div class="empty-state">No failed updates.</div>`;
}

/* ---------- activity ---------- */
async function loadActivity() {
  const el = $("activity-list");
  el.innerHTML = `<div class="empty-state">Loading…</div>`;
  const rows = await api("audit");
  el.innerHTML = rows.length
    ? `<div class="activity-table">
        <div class="activity-row head">
          <span>When</span><span>Who</span><span>Action</span><span>Target</span>
        </div>
        ${rows
          .map(
            (r) => `
          <div class="activity-row">
            <span class="hint">${esc(timeAgo(r.created_at))}</span>
            <span>${esc(r.actor)}</span>
            <span><b>${esc(r.action)}</b></span>
            <span class="mono">${esc(r.target || "—")}</span>
          </div>`,
          )
          .join("")}
      </div>`
    : `<div class="empty-state">No owner actions recorded yet.</div>`;
}

/* ---------- earnings ---------- */
async function loadEarnings() {
  skeletons($("earnings-cards"), 4, 84);
  $("earnings-error").hidden = true;
  let a;
  try {
    a = await api("analytics");
  } catch (err) {
    $("earnings-cards").innerHTML = "";
    $("earnings-chart").innerHTML = "";
    $("earnings-products").innerHTML = "";
    const banner = $("earnings-error");
    banner.textContent = /admin_analytics|404|not found|function/i.test(err.message)
      ? "Analytics need one extra database function. Run supabase/migrations/0005_admin_analytics.sql in your Supabase SQL editor, then refresh."
      : err.message;
    banner.hidden = false;
    return;
  }

  const r = a.revenue;
  const kept = r.kept_stars + r.pending_refund_stars;
  const lifetime = kept + r.refunded_stars;
  const avg = r.payments_count ? Math.round(lifetime / r.payments_count) : 0;
  const cards = [
    { label: "kept after refunds", value: `★ ${kept}`, accent: true },
    { label: "lifetime earned", value: `★ ${lifetime}` },
    { label: "refunded", value: `★ ${r.refunded_stars}`, alert: r.refunded_stars > 0 },
    { label: "payments", value: r.payments_count },
    { label: "avg payment", value: `★ ${avg}` },
  ];
  if (r.review_stars > 0)
    cards.push({ label: "flagged for review", value: `★ ${r.review_stars}`, alert: true });
  $("earnings-cards").innerHTML = cards
    .map(
      (c, i) => `
    <div class="stat-card${c.accent ? " accent" : ""}${c.alert ? " alert" : ""}" style="animation-delay:${i * 40}ms">
      <span class="stat-value">${esc(c.value)}</span>
      <span class="stat-label">${esc(c.label)}</span>
    </div>`,
    )
    .join("");

  const days = a.by_day || [];
  const max = Math.max(1, ...days.map((d) => d.earned_stars));
  $("earnings-chart").innerHTML = days.length
    ? `<div class="bars">${days
        .map((d) => {
          const h = Math.max(2, Math.round((d.earned_stars / max) * 100));
          const label = new Date(d.day).toLocaleDateString(undefined, { day: "numeric", month: "short" });
          return `<div class="bar" title="${label}: ★ ${d.earned_stars}${d.refunded_stars ? ` (−${d.refunded_stars} refunded)` : ""}">
            <em>★ ${d.earned_stars || ""}</em>
            <i style="height:${h}%"></i>
            <span>${label}</span>
          </div>`;
        })
        .join("")}</div>`
    : `<div class="empty-state">No payments in the last 14 days.</div>`;

  const rows = a.by_product || [];
  $("earnings-products").innerHTML = rows.length
    ? `
    <table class="stock-table">
      <thead><tr><th>product</th><th>sales</th><th>earned</th><th>refunds</th><th>refunded</th></tr></thead>
      <tbody>${rows
        .map(
          (p) => `<tr>
            <td>${esc(p.title)} <span class="mono faint">${esc(p.sku)}</span></td>
            <td class="mono">${esc(p.sales)}</td>
            <td class="mono gold">★ ${esc(p.earned_stars)}</td>
            <td class="mono">${esc(p.refunds)}</td>
            <td class="mono${p.refunded_stars ? " red" : ""}">${p.refunded_stars ? `−★ ${esc(p.refunded_stars)}` : "—"}</td>
          </tr>`,
        )
        .join("")}</tbody>
    </table>`
    : `<div class="empty-state">No sales yet — paid orders show up here.</div>`;
}

/* ---------- products ---------- */
async function loadProducts() {
  const el = $("products-list");
  skeletons(el, 3, 150);
  const products = await api("products");
  productsCache = products;
  if (!products.length) {
    el.innerHTML = `<div class="panel empty-state">No products yet. Hit “New product” to create your first one.</div>`;
    return;
  }
  el.innerHTML = products
    .map((p, i) => {
      const status = p.active
        ? `<span class="pc-status live"><i></i>live in shop</span>`
        : `<span class="pc-status off"><i></i>hidden</span>`;
      const stats =
        p.source === "stock"
          ? `<div class="pc-stat${p.available <= 3 && p.active ? " low" : ""}"><b>${esc(p.available)}</b><span>available</span></div>
             <div class="pc-stat"><b>${esc(p.sold)}</b><span>sold</span></div>
             <div class="pc-stat${p.quarantined > 0 ? " warn" : ""}"><b>${esc(p.quarantined)}</b><span>quarantined</span></div>`
          : `<div class="pc-stat span3"><span>supplier-fed inventory</span></div>`;
      return `
      <div class="product-card" style="animation-delay:${i * 35}ms">
        <div class="pc-head">
          <div class="pc-id">
            <h4>${esc(p.title)}</h4>
            <code class="pc-sku">${esc(p.sku)}</code>
          </div>
          <span class="pc-price">★ ${esc(p.price_stars)}</span>
        </div>
        <p class="pc-desc">${esc(p.description || "No description.")}</p>
        <div class="pc-stats">${stats}</div>
        <div class="pc-foot">
          <div class="pc-tags">
            <span class="pc-cat">${esc(p.category)}</span>
            ${status}
          </div>
          <div class="pc-actions">
            <button class="btn small" data-edit="${esc(p.sku)}">Edit</button>
            ${p.source === "stock" ? `<button class="btn small" data-addstock="${esc(p.sku)}">Add stock</button>` : ""}
            <button class="btn small ${p.active ? "ghost" : "primary"}" data-toggle="${esc(p.sku)}">
              ${p.active ? "Hide" : "Activate"}
            </button>
          </div>
        </div>
      </div>`;
    })
    .join("");
}

function openProductDialog(product) {
  const isEdit = !!product;
  $("p-mode").value = isEdit ? "edit" : "create";
  $("product-dialog-title").textContent = isEdit ? `Edit ${product.title}` : "New product";
  $("product-dialog-hint").textContent = isEdit
    ? "Price, title, category and description."
    : "Created hidden. Upload stock, then activate.";
  $("p-sku-field").style.display = isEdit ? "none" : "";
  $("p-sku").required = !isEdit;
  $("p-sku").value = product ? product.sku : "";
  $("p-title").value = product ? product.title : "";
  $("p-price").value = product ? product.price_stars : "";
  $("p-category").value = product ? product.category : "";
  $("p-description").value = product ? product.description || "" : "";
  $("product-save").textContent = isEdit ? "Save changes" : "Create";
  $("product-error").hidden = true;
  $("product-dialog").showModal();
}

/* ---------- stock ---------- */
async function loadStock(prefer) {
  if (!productsCache.length) productsCache = await api("products");
  const select = $("s-sku");
  const stockProducts = productsCache.filter((p) => p.source === "stock");
  select.innerHTML = stockProducts.length
    ? stockProducts
        .map(
          (p) =>
            `<option value="${esc(p.sku)}">${esc(p.title)} (${esc(p.sku)}) — ${esc(p.available)} available</option>`,
        )
        .join("")
    : `<option value="">create a product first</option>`;
  if (prefer && stockProducts.some((p) => p.sku === prefer)) select.value = prefer;
  renderStockStateChips();
  await loadStockList();
}

function renderStockStateChips() {
  const states = ["", "available", "reserved", "sold", "quarantined"];
  $("stock-state-filter").innerHTML = states
    .map(
      (s) =>
        `<button class="chip${stockStateFilter === s ? " active" : ""}" data-state="${s}">${s || "all"}</button>`,
    )
    .join("");
}

async function loadStockList() {
  const el = $("stock-list");
  const sku = $("s-sku").value;
  if (!sku) {
    el.innerHTML = "";
    return;
  }
  skeletons(el, 4, 34);
  stockCache = await api(`stock?sku=${encodeURIComponent(sku)}`);
  renderStockList();
}

function renderStockList() {
  const el = $("stock-list");
  const rows = stockStateFilter
    ? stockCache.filter((r) => r.state === stockStateFilter)
    : stockCache;
  if (!rows.length) {
    el.innerHTML = `<div class="empty-state">Nothing here yet — upload some codes.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="stock-table">
      <thead><tr><th>fingerprint</th><th>state</th><th>uploaded</th><th>assigned</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td class="mono">${esc(r.fingerprint)}…</td>
            <td>${stockBadge(r.state)}</td>
            <td class="mono">${esc(timeAgo(r.created_at))}</td>
            <td>${r.assigned ? "✓" : "—"}</td>
          </tr>`,
        )
        .join("")}</tbody>
    </table>`;
}

function updateLineCount() {
  const lines = $("s-lines").value.split("\n").map((l) => l.trim()).filter(Boolean);
  $("s-count").textContent = lines.length ? `${lines.length} line${lines.length === 1 ? "" : "s"}` : "";
}

/* ---------- orders ---------- */
const ORDER_STATES = ["", "invoice", "checkout", "expired", "delivering", "delivered", "delivery_failed", "needs_refund", "refunded"];

function renderOrderChips() {
  $("o-state-chips").innerHTML = ORDER_STATES.map(
    (s) =>
      `<button class="chip${orderStateFilter === s ? " active" : ""}" data-state="${s}">${s ? s.replace(/_/g, " ") : "all"}</button>`,
  ).join("");
}

async function loadOrders() {
  const el = $("orders-list");
  skeletons(el, 4, 66);
  renderOrderChips();
  const params = new URLSearchParams();
  if (orderStateFilter) params.set("state", orderStateFilter);
  const q = $("o-q").value.trim();
  if (q) params.set("q", q);
  const orders = await api(`orders?${params}`);
  if (!orders.length) {
    el.innerHTML = `<div class="panel empty-state">No orders match. They appear here after a Stars payment.</div>`;
    return;
  }
  el.innerHTML = orders
    .map(
      (o, i) => `
    <div class="order-card" style="animation-delay:${i * 25}ms">
      <div class="oc-main">
        <span class="oc-title">${esc(o.title)}</span>${orderBadge(o.state)}
        <div class="oc-meta">
          <code>${esc(o.id)}</code> · ★ ${esc(o.price_stars)} · buyer <span class="mono">${esc(o.user_id)}</span> · ${esc(timeAgo(o.created_at))}
          ${o.delivery_attempts > 1 ? ` · ${esc(o.delivery_attempts)} delivery attempts` : ""}
          ${o.error_code ? ` · <span style="color:var(--red)">${esc(o.error_code)}</span>` : ""}
          ${o.payment_status === "review" ? ` · <span class="badge bad">payment review</span>` : ""}
        </div>
      </div>
      <div class="oc-actions">
        ${["delivering", "delivery_failed", "delivered"].includes(o.state)
          ? `<button class="btn small" data-act="resend" data-id="${esc(o.id)}">Resend same code</button>` : ""}
        ${o.charge_id && o.state !== "refunded"
          ? `<button class="btn small danger" data-act="refund" data-id="${esc(o.id)}">Refund</button>` : ""}
      </div>
    </div>`,
    )
    .join("");
}

/* ---------- settings ---------- */
const SETTING_KEYS = ["shop_name", "support_contact", "terms_text", "privacy_text", "checkout_paused"];

async function loadSettings() {
  const s = await api("settings");
  for (const key of SETTING_KEYS) {
    const el = $(`set-${key}`);
    if (el.type === "checkbox") el.checked = s[key] === "true";
    else el.value = s[key] || "";
  }
  updatePauseLabel();
}

function updatePauseLabel() {
  const paused = $("set-checkout_paused").checked;
  $("pause-label").textContent = paused ? "Paused" : "Open";
  $("pause-label").style.color = paused ? "var(--gold)" : "var(--green)";
}

async function saveSettings() {
  const body = {};
  for (const key of SETTING_KEYS) {
    const el = $(`set-${key}`);
    body[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  const r = await api("settings/update", { method: "POST", body });
  $("settings-result").textContent = r.terms_changed ? `Saved. ${r.note}` : "Saved.";
  $("terms-warning").hidden = true;
  const pill = $("shop-status");
  pill.textContent = body.checkout_paused ? "checkout paused" : "checkout open";
  pill.classList.toggle("paused", !!body.checkout_paused);
  toast("Settings saved");
}

/* ---------- events ---------- */
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = $("login-error");
  errEl.hidden = true;
  try {
    await signIn($("login-email").value, $("login-password").value);
    $("login-screen").hidden = true;
    $("shell").hidden = false;
    showView("overview");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

$("logout").addEventListener("click", signOut);
for (const b of document.querySelectorAll("#nav button[data-view]"))
  b.addEventListener("click", () => showView(b.dataset.view));

$("overview-refresh").addEventListener("click", () => loadView("overview"));
$("earnings-refresh").addEventListener("click", () => loadView("earnings"));
document.querySelector("#view-overview").addEventListener("click", async (e) => {
  const goto = e.target.closest("[data-goto]");
  if (goto) showView(goto.dataset.goto);
  const addstock = e.target.closest("[data-addstock]");
  if (addstock) {
    showView("stock", { deferLoad: true });
    loadStock(addstock.dataset.addstock).catch((err) => toast(err.message, true));
  }
  const retry = e.target.closest("[data-retry]");
  if (retry) {
    retry.disabled = true;
    try {
      const result = await api("updates/retry", {
        method: "POST",
        body: { update_id: Number(retry.dataset.retry) },
      });
      toast(result.state === "done" ? "Update processed." : `Retry finished: ${result.state}`,
        result.state === "failed");
      await loadOverview();
    } catch (err) {
      retry.disabled = false;
      toast(err.message, true);
    }
  }
});
$("activity-refresh").addEventListener("click", () => loadView("activity"));

$("new-product").addEventListener("click", () => openProductDialog(null));
$("product-cancel").addEventListener("click", () => $("product-dialog").close());
$("product-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = $("product-error");
  errEl.hidden = true;
  const mode = $("p-mode").value;
  const price = Number($("p-price").value);
  const body = {
    sku: $("p-sku").value.trim(),
    title: $("p-title").value.trim(),
    description: $("p-description").value.trim(),
    category: $("p-category").value.trim() || "general",
    price_stars: Number.isInteger(price) ? price : 0,
  };
  try {
    if (mode === "edit") {
      const original = productsCache.find((p) => p.sku === body.sku);
      const changes = { sku: body.sku };
      if (original) {
        for (const key of ["title", "description", "category", "price_stars"])
          if (body[key] !== original[key]) changes[key] = body[key];
      }
      await api("products/update", { method: "POST", body: changes });
      toast("Product updated");
    } else {
      await api("products/create", { method: "POST", body });
      toast("Product created — hidden until you add stock and activate it");
    }
    $("product-dialog").close();
    await loadProducts();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

$("products-list").addEventListener("click", async (e) => {
  const edit = e.target.closest("[data-edit]");
  const addstock = e.target.closest("[data-addstock]");
  const toggle = e.target.closest("[data-toggle]");
  try {
    if (edit) {
      const product = productsCache.find((p) => p.sku === edit.dataset.edit);
      if (product) openProductDialog(product);
    } else if (addstock) {
      showView("stock", { deferLoad: true });
      await loadStock(addstock.dataset.addstock);
    } else if (toggle) {
      const product = productsCache.find((p) => p.sku === toggle.dataset.toggle);
      if (!product) return;
      if (!product.active) {
        const ok = await confirmAction(
          `Activate “${product.title}”?`,
          product.available > 0
            ? `It becomes visible in the shop with ${product.available} codes in stock.`
            : "It becomes visible in the shop.",
          "Activate",
        );
        if (!ok) return;
      }
      await api("products/update", {
        method: "POST",
        body: { sku: product.sku, active: !product.active },
      });
      toast(product.active ? "Product hidden from buyers" : "Product is live");
      await loadProducts();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

$("s-sku").addEventListener("change", () => {
  stockStateFilter = "";
  renderStockStateChips();
  loadStockList().catch((err) => toast(err.message, true));
});
$("stock-state-filter").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  stockStateFilter = chip.dataset.state;
  renderStockStateChips();
  renderStockList();
});
$("s-lines").addEventListener("input", updateLineCount);
$("s-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) {
    $("s-lines").value = await file.text();
    updateLineCount();
  }
  e.target.value = "";
});
const drop = $("dropzone");
drop.addEventListener("dragover", (e) => {
  e.preventDefault();
  drop.classList.add("over");
});
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  const file = e.dataTransfer.files[0];
  if (file) {
    $("s-lines").value = await file.text();
    updateLineCount();
  }
});
$("stock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sku = $("s-sku").value;
  const lines = $("s-lines").value.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) {
    toast("Paste or drop at least one line", true);
    return;
  }
  try {
    const r = await api("stock/upload", { method: "POST", body: { sku, lines } });
    $("stock-result").textContent =
      `Added ${r.accepted}, skipped ${r.duplicates} duplicates` +
      (r.rejected ? `, rejected ${r.rejected}` : "") + ".";
    $("s-lines").value = "";
    updateLineCount();
    toast(`Stock uploaded: ${r.accepted} new codes`);
    productsCache = await api("products");
    await loadStock(sku);
  } catch (err) {
    toast(err.message, true);
  }
});

$("o-search").addEventListener("click", () => loadOrders().catch((err) => toast(err.message, true)));
$("o-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadOrders().catch((err) => toast(err.message, true));
});
$("o-state-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  orderStateFilter = chip.dataset.state;
  loadOrders().catch((err) => toast(err.message, true));
});

$("orders-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  try {
    if (btn.dataset.act === "resend") {
      const ok = await confirmAction(
        "Resend delivery",
        "This sends the already-assigned code again. It will not allocate a new one.",
        "Resend",
      );
      if (!ok) return;
      await api("orders/resend", { method: "POST", body: { order_id: btn.dataset.id } });
      toast("Same code resent");
    } else if (btn.dataset.act === "refund") {
      const ok = await confirmAction(
        "Refund this order?",
        "Telegram Stars will be refunded in full. The delivered code is quarantined and never resold.",
        "Refund now",
      );
      if (!ok) return;
      await api("orders/refund", { method: "POST", body: { order_id: btn.dataset.id, confirm: true } });
      toast("Refunded and code quarantined");
    }
    loadOrders();
  } catch (err) {
    toast(err.message, true);
  }
});

$("confirm-cancel").addEventListener("click", () => {
  $("confirm-dialog").close();
  if (pendingConfirm) pendingConfirm(false);
  pendingConfirm = null;
});
$("confirm-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("confirm-dialog").close();
  if (pendingConfirm) pendingConfirm(true);
  pendingConfirm = null;
});
$("confirm-dialog").addEventListener("close", () => {
  if (pendingConfirm) pendingConfirm(false);
  pendingConfirm = null;
});

$("set-checkout_paused").addEventListener("change", updatePauseLabel);
$("set-terms_text").addEventListener("input", () => ($("terms-warning").hidden = false));
$("set-privacy_text").addEventListener("input", () => ($("terms-warning").hidden = false));
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await saveSettings();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---------- boot ---------- */
if (token()) {
  $("login-screen").hidden = true;
  $("shell").hidden = false;
  showView("overview");
}
