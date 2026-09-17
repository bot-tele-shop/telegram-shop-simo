/* Owner console — Supabase Auth for sign-in, Worker /admin/api for everything else. */
"use strict";

const SUPABASE_URL = `https://${CONFIG.projectRef}.supabase.co`;
const API = `${CONFIG.workerUrl}/admin/api`;
const TOKEN_KEY = "ds_session";

let session = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null");
let productsCache = [];
let pendingConfirm = null;

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

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 4000);
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
  document.getElementById("shell").hidden = true;
  document.getElementById("login-screen").hidden = false;
}

function showView(name) {
  for (const s of document.querySelectorAll("main section")) s.hidden = true;
  document.getElementById(`view-${name}`).hidden = false;
  for (const b of document.querySelectorAll("#nav button[data-view]"))
    b.classList.toggle("active", b.dataset.view === name);
  loadView(name);
}

async function loadView(name) {
  try {
    if (name === "overview") await loadOverview();
    if (name === "products") await loadProducts();
    if (name === "stock") await loadStock();
    if (name === "orders") await loadOrders();
    if (name === "settings") await loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function confirmAction(title, body, okLabel = "Confirm") {
  return new Promise((resolve) => {
    pendingConfirm = resolve;
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-body").textContent = body;
    document.getElementById("confirm-ok").textContent = okLabel;
    document.getElementById("confirm-dialog").showModal();
  });
}

/* ---- Overview ---- */
async function loadOverview() {
  const o = await api("overview");
  document.getElementById("shop-status").textContent = o.checkout_paused ? "checkout paused" : "checkout open";
  const cards = [
    ["Active products", `${o.products.active} / ${o.products.total}`],
    ["Available stock", o.stock.available],
    ["Sold", o.stock.sold],
    ["Quarantined", o.stock.quarantined],
    ["Paid orders", o.orders.paid],
    ["Pending delivery", o.orders.pending_delivery],
    ["Refund queue", o.orders.needs_refund],
    ["Checkout", o.checkout_paused ? "paused" : "open"],
  ];
  document.getElementById("overview-cards").innerHTML = cards
    .map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`)
    .join("");

  const banner = document.getElementById("setup-banner");
  if (!o.products.total) {
    banner.hidden = false;
    banner.innerHTML = `<strong>The shop is empty.</strong> Buyers will see no catalog until you finish this:
      <ol>
        <li>Create a product — it stays hidden</li>
        <li>Upload license codes or download URLs</li>
        <li>Activate — it appears in Telegram immediately</li>
      </ol>`;
  } else if (!o.products.active) {
    banner.hidden = false;
    banner.innerHTML = `<strong>Products exist, none are live.</strong> Upload stock if needed, then activate a product to show it in @velmorabazaar_bot.`;
  } else {
    banner.hidden = true;
  }

  const alerts = [];
  if (o.review_alerts) alerts.push(`${o.review_alerts} payment event(s) need review`);
  if (o.failed_updates) alerts.push(`${o.failed_updates} webhook update(s) failed permanently`);
  if (o.orders.needs_refund) alerts.push(`${o.orders.needs_refund} order(s) are owed a refund`);
  if (o.orders.pending_delivery) alerts.push(`${o.orders.pending_delivery} paid order(s) awaiting delivery`);
  document.getElementById("overview-alerts").innerHTML = alerts.length
    ? `<ul>${alerts.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`
    : `<p class="hint">Nothing waiting. Create a product when you are ready to sell.</p>`;
}

/* ---- Products ---- */
async function loadProducts() {
  productsCache = await api("products");
  const el = document.getElementById("products-list");
  if (!productsCache.length) {
    el.innerHTML = `<div class="panel empty">No products yet. Create one — it will stay inactive until stock is loaded.</div>`;
    return;
  }
  el.innerHTML = productsCache.map((p) => `
    <div class="item">
      <div>
        <b>${esc(p.title)}</b> <code>${esc(p.sku)}</code>
        <span class="badge ${p.active ? "ok" : "warn"}">${p.active ? "live in Telegram" : "hidden"}</span>
        <div class="meta">${esc(p.price_stars)} Stars · ${esc(p.available)} available · ${esc(p.sold)} sold · ${esc(p.quarantined)} quarantined</div>
        <div class="meta">${esc(p.description || "")}</div>
      </div>
      <div class="actions">
        <button data-act="stock" data-sku="${esc(p.sku)}">Add stock</button>
        <button data-act="edit" data-sku="${esc(p.sku)}">Edit</button>
        <button data-act="toggle" data-sku="${esc(p.sku)}" data-active="${p.active}" class="${p.active ? "" : "primary"}">
          ${p.active ? "Hide" : "Activate"}</button>
      </div>
    </div>`).join("");
}

function openProductDialog(product) {
  const editing = Boolean(product);
  document.getElementById("p-mode").value = editing ? "edit" : "create";
  document.getElementById("product-dialog-title").textContent = editing ? "Edit product" : "New product";
  document.getElementById("product-dialog-hint").textContent = editing
    ? "Price and title changes apply to new orders only. Existing orders keep their snapshot."
    : "Created inactive. Upload stock, then activate.";
  document.getElementById("p-sku").value = product ? product.sku : "";
  document.getElementById("p-sku").disabled = editing;
  document.getElementById("p-title").value = product ? product.title : "";
  document.getElementById("p-price").value = product ? product.price_stars : "";
  document.getElementById("p-category").value = product ? product.category : "general";
  document.getElementById("p-description").value = product ? product.description || "" : "";
  document.getElementById("product-save").textContent = editing ? "Save" : "Create";
  document.getElementById("product-error").hidden = true;
  document.getElementById("product-dialog").showModal();
}

document.getElementById("new-product").addEventListener("click", () => openProductDialog(null));
document.getElementById("product-cancel").addEventListener("click", () => {
  document.getElementById("product-dialog").close();
});

document.getElementById("product-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = document.getElementById("product-error");
  errEl.hidden = true;
  const editing = document.getElementById("p-mode").value === "edit";
  const body = {
    sku: document.getElementById("p-sku").value.trim(),
    title: document.getElementById("p-title").value.trim(),
    description: document.getElementById("p-description").value.trim(),
    category: document.getElementById("p-category").value.trim() || "general",
    price_stars: parseInt(document.getElementById("p-price").value, 10),
  };
  try {
    if (editing) await api("products/update", { method: "POST", body });
    else await api("products/create", { method: "POST", body });
    document.getElementById("product-dialog").close();
    toast(editing ? "Product saved" : "Product created (hidden until you activate it)");
    if (!editing) {
      document.getElementById("s-sku").dataset.prefer = body.sku;
      showView("stock");
    } else {
      loadProducts();
    }
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

document.getElementById("products-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const sku = btn.dataset.sku;
  const product = productsCache.find((p) => p.sku === sku);
  try {
    if (btn.dataset.act === "edit") {
      openProductDialog(product);
      return;
    }
    if (btn.dataset.act === "stock") {
      document.getElementById("s-sku").dataset.prefer = sku;
      showView("stock");
      return;
    }
    if (btn.dataset.act === "toggle") {
      const makingLive = btn.dataset.active !== "true";
      if (makingLive && product && product.available < 1) {
        toast("Upload stock before activating — buyers would see an empty listing.", true);
        document.getElementById("s-sku").dataset.prefer = sku;
        showView("stock");
        return;
      }
      await api("products/update", {
        method: "POST",
        body: { sku, active: makingLive },
      });
      toast(makingLive ? "Live in Telegram" : "Hidden from buyers");
      loadProducts();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---- Stock ---- */
async function loadStock() {
  productsCache = await api("products");
  const stockProducts = productsCache.filter((p) => p.source === "stock");
  const select = document.getElementById("s-sku");
  const prefer = select.dataset.prefer;
  select.innerHTML = stockProducts
    .map((p) => `<option value="${esc(p.sku)}">${esc(p.title)} (${esc(p.sku)}) — ${esc(p.available)} available</option>`)
    .join("") || `<option value="">Create a product first</option>`;
  if (prefer && stockProducts.some((p) => p.sku === prefer)) select.value = prefer;
  delete select.dataset.prefer;
  await loadStockList();
}

async function loadStockList() {
  const sku = document.getElementById("s-sku").value;
  const el = document.getElementById("stock-list");
  if (!sku) {
    el.innerHTML = `<p class="empty">Create a product first.</p>`;
    return;
  }
  const rows = await api(`stock?sku=${encodeURIComponent(sku)}`);
  if (!rows.length) {
    el.innerHTML = `<p class="empty">No inventory for this SKU yet.</p>`;
    return;
  }
  el.innerHTML = `<table>
    <thead><tr><th>Fingerprint</th><th>State</th><th>Added</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td><code>${esc(r.fingerprint)}</code></td>
      <td><span class="badge ${r.state === "available" ? "ok" : r.state === "quarantined" ? "bad" : ""}">${esc(r.state)}</span></td>
      <td>${esc(new Date(r.created_at).toLocaleString())}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}

document.getElementById("s-sku").addEventListener("change", () => {
  loadStockList().catch((err) => toast(err.message, true));
});

document.getElementById("s-file").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  document.getElementById("s-lines").value = await file.text();
});

const drop = document.getElementById("dropzone");
drop.addEventListener("dragover", (e) => { e.preventDefault(); });
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  document.getElementById("s-lines").value = await file.text();
});

document.getElementById("stock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sku = document.getElementById("s-sku").value;
  const lines = document.getElementById("s-lines").value.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!sku) return toast("Create a product first", true);
  if (!lines.length) return toast("Paste or drop at least one code", true);
  try {
    const r = await api("stock/upload", { method: "POST", body: { sku, lines } });
    document.getElementById("stock-result").textContent =
      `Accepted ${r.accepted} · duplicates ${r.duplicates} · rejected ${r.rejected}`;
    document.getElementById("s-lines").value = "";
    document.getElementById("s-file").value = "";
    toast("Stock encrypted and stored");
    await loadStock();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---- Orders ---- */
async function loadOrders() {
  const params = new URLSearchParams();
  const state = document.getElementById("o-state").value;
  const q = document.getElementById("o-q").value.trim();
  if (state) params.set("state", state);
  if (q) params.set("q", q);
  const orders = await api(`orders?${params}`);
  const el = document.getElementById("orders-list");
  if (!orders.length) {
    el.innerHTML = `<div class="panel empty">No orders yet. They appear here after a Stars payment.</div>`;
    return;
  }
  el.innerHTML = orders.map((o) => `
    <div class="item">
      <div>
        <code>${esc(o.id)}</code> <b>${esc(o.title)}</b>
        <span class="badge ${o.state === "delivered" ? "ok" : /fail|refund/.test(o.state) ? "bad" : "warn"}">${esc(o.state)}</span>
        <div class="meta">${esc(o.price_stars)} Stars · buyer ${esc(o.user_id)} · ${esc(new Date(o.created_at).toLocaleString())}${
          o.error_code ? ` · ${esc(o.error_code)}` : ""
        }</div>
      </div>
      <div class="actions">
        ${["delivering", "delivery_failed", "delivered"].includes(o.state)
          ? `<button data-act="resend" data-id="${esc(o.id)}">Resend same code</button>` : ""}
        ${o.charge_id && o.state !== "refunded"
          ? `<button data-act="refund" data-id="${esc(o.id)}" class="danger">Refund</button>` : ""}
      </div>
    </div>`).join("");
}

document.getElementById("o-search").addEventListener("click", () => {
  loadOrders().catch((err) => toast(err.message, true));
});
document.getElementById("orders-list").addEventListener("click", async (e) => {
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

document.getElementById("confirm-cancel").addEventListener("click", () => {
  document.getElementById("confirm-dialog").close();
  if (pendingConfirm) pendingConfirm(false);
  pendingConfirm = null;
});
document.getElementById("confirm-form").addEventListener("submit", (e) => {
  e.preventDefault();
  document.getElementById("confirm-dialog").close();
  if (pendingConfirm) pendingConfirm(true);
  pendingConfirm = null;
});
document.getElementById("confirm-dialog").addEventListener("close", () => {
  if (pendingConfirm) pendingConfirm(false);
  pendingConfirm = null;
});

/* ---- Settings ---- */
const SETTING_KEYS = ["shop_name", "support_contact", "terms_text", "privacy_text", "checkout_paused"];

async function loadSettings() {
  const s = await api("settings");
  for (const key of SETTING_KEYS) {
    const el = document.getElementById(`set-${key}`);
    if (el.type === "checkbox") el.checked = s[key] === "true";
    else el.value = s[key] || "";
  }
}

document.getElementById("set-terms_text").addEventListener("input", () => {
  document.getElementById("terms-warning").hidden = false;
});
document.getElementById("set-privacy_text").addEventListener("input", () => {
  document.getElementById("terms-warning").hidden = false;
});
document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const key of SETTING_KEYS) {
    const el = document.getElementById(`set-${key}`);
    body[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  try {
    const r = await api("settings/update", { method: "POST", body });
    document.getElementById("settings-result").textContent = r.terms_changed ? `Saved. ${r.note}` : "Saved.";
    document.getElementById("terms-warning").hidden = true;
    document.getElementById("shop-status").textContent = body.checkout_paused ? "checkout paused" : "checkout open";
    toast("Settings saved");
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---- wiring ---- */
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = document.getElementById("login-error");
  errEl.hidden = true;
  try {
    await signIn(document.getElementById("login-email").value, document.getElementById("login-password").value);
    document.getElementById("login-screen").hidden = true;
    document.getElementById("shell").hidden = false;
    showView("overview");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});
document.getElementById("logout").addEventListener("click", signOut);
for (const b of document.querySelectorAll("#nav button[data-view]"))
  b.addEventListener("click", () => showView(b.dataset.view));

if (token()) {
  document.getElementById("login-screen").hidden = true;
  document.getElementById("shell").hidden = false;
  showView("overview");
}
