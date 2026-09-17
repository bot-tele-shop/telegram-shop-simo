/* Digital Shelf owner dashboard — talks only to Supabase Auth (sign-in) and
   the Worker's /admin/api (everything else). No build step. */
"use strict";

const SUPABASE_URL = `https://${CONFIG.projectRef}.supabase.co`;
const API = `${CONFIG.workerUrl}/admin/api`;

let token = sessionStorage.getItem("ds_token") || null;

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 4000);
}

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`${API}/${path}`, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
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
  token = data.access_token;
  sessionStorage.setItem("ds_token", token);
}

function signOut() {
  token = null;
  sessionStorage.removeItem("ds_token");
  showView("login");
}

function showView(name) {
  for (const s of document.querySelectorAll("main section")) s.hidden = true;
  document.getElementById(`view-${name}`).hidden = false;
  document.getElementById("nav").hidden = !token;
  for (const b of document.querySelectorAll("#nav button[data-view]"))
    b.classList.toggle("active", b.dataset.view === name);
  if (token) loadView(name);
}

async function loadView(name) {
  try {
    if (name === "overview") await loadOverview();
    if (name === "products") await loadProducts();
    if (name === "stock") await loadStockForm();
    if (name === "orders") await loadOrders();
    if (name === "settings") await loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---- Overview ---- */
async function loadOverview() {
  const o = await api("overview");
  const cards = [
    ["Active products", `${o.products.active} / ${o.products.total}`],
    ["Available stock", o.stock.available],
    ["Sold", o.stock.sold],
    ["Quarantined", o.stock.quarantined],
    ["Paid orders", o.orders.paid],
    ["Pending deliveries", o.orders.pending_delivery],
    ["Refund queue", o.orders.needs_refund],
    ["Checkout", o.checkout_paused ? "PAUSED" : "open"],
  ];
  document.getElementById("overview-cards").innerHTML = cards
    .map(([k, v]) => `<div class="card stat"><b>${v}</b><span>${k}</span></div>`)
    .join("");
  const alerts = [];
  if (o.review_alerts) alerts.push(`${o.review_alerts} payment event(s) need review`);
  if (o.failed_updates) alerts.push(`${o.failed_updates} webhook update(s) failed permanently`);
  if (o.orders.needs_refund) alerts.push(`${o.orders.needs_refund} order(s) are owed a refund`);
  if (o.orders.pending_delivery) alerts.push(`${o.orders.pending_delivery} paid order(s) awaiting delivery`);
  document.getElementById("overview-alerts").innerHTML =
    alerts.length ? `<ul>${alerts.map((a) => `<li>⚠ ${a}</li>`).join("")}</ul>` : "All clear.";
}

/* ---- Products ---- */
async function loadProducts() {
  const products = await api("products");
  const el = document.getElementById("products-list");
  el.innerHTML = products
    .map(
      (p) => `
    <div class="card row spread">
      <div>
        <b>${esc(p.title)}</b> <code>${esc(p.sku)}</code>
        <span class="badge ${p.active ? "ok" : ""}">${p.active ? "active" : "inactive"}</span>
        <span class="badge">${p.source}</span><br>
        <span class="muted">${p.price_stars} Stars · ${p.available} in stock · ${p.sold} sold · ${p.quarantined} quarantined</span><br>
        <span class="muted">${esc(p.description || "")}</span>
      </div>
      <div>
        <button data-act="toggle" data-sku="${esc(p.sku)}" data-active="${p.active}">
          ${p.active ? "Deactivate" : "Activate"}</button>
        <button data-act="edit" data-sku="${esc(p.sku)}">Edit</button>
      </div>
    </div>`
    )
    .join("") || '<div class="card muted">No products yet.</div>';
}

document.getElementById("products-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  try {
    if (btn.dataset.act === "toggle") {
      await api("products/update", { body: { sku: btn.dataset.sku, active: btn.dataset.active !== "true" } });
      toast("Product updated");
    } else if (btn.dataset.act === "edit") {
      const title = prompt("New title (leave empty to keep):");
      const price = prompt("New Stars price (whole number, empty to keep):");
      const body = { sku: btn.dataset.sku };
      if (title) body.title = title;
      if (price) body.price_stars = parseInt(price, 10);
      await api("products/update", { body });
      toast("Product updated");
    }
    loadProducts();
  } catch (err) {
    toast(err.message, true);
  }
});

document.getElementById("product-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("products/create", {
      body: {
        sku: document.getElementById("p-sku").value.trim(),
        title: document.getElementById("p-title").value.trim(),
        description: document.getElementById("p-description").value.trim(),
        category: document.getElementById("p-category").value.trim() || "general",
        price_stars: parseInt(document.getElementById("p-price").value, 10),
      },
    });
    toast("Product created (inactive — activate after uploading stock)");
    e.target.reset();
    loadProducts();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---- Stock ---- */
async function loadStockForm() {
  const products = await api("products");
  document.getElementById("s-sku").innerHTML = products
    .filter((p) => p.source === "stock")
    .map((p) => `<option>${esc(p.sku)}</option>`)
    .join("");
}

document.getElementById("stock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const lines = document.getElementById("s-lines").value.split("\n").map((l) => l.trim()).filter(Boolean);
  try {
    const r = await api("stock/upload", {
      body: { sku: document.getElementById("s-sku").value, lines },
    });
    document.getElementById("stock-result").textContent =
      `Accepted: ${r.accepted} · Duplicates skipped: ${r.duplicates} · Rejected: ${r.rejected}`;
    document.getElementById("s-lines").value = "";
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
  document.getElementById("orders-list").innerHTML = orders
    .map(
      (o) => `
    <div class="card row spread">
      <div>
        <code>${esc(o.id)}</code> <b>${esc(o.title)}</b> (${o.price_stars} Stars)
        <span class="badge ${o.state === "delivered" ? "ok" : o.state.includes("fail") || o.state.includes("refund") ? "bad" : ""}">${o.state}</span>
        <span class="badge">${o.payment_status || "no payment"}</span><br>
        <span class="muted">buyer ${o.user_id} · ${new Date(o.created_at).toLocaleString()}${
          o.error_code ? ` · error: ${esc(o.error_code)}` : ""
        }</span>
      </div>
      <div>
        ${["delivering", "delivery_failed", "delivered"].includes(o.state)
          ? `<button data-act="resend" data-id="${esc(o.id)}">Resend code</button>` : ""}
        ${o.charge_id && !["refunded"].includes(o.state)
          ? `<button data-act="refund" data-id="${esc(o.id)}" class="danger">Refund</button>` : ""}
      </div>
    </div>`
    )
    .join("") || '<div class="card muted">No orders match.</div>';
}

document.getElementById("o-search").addEventListener("click", loadOrders);
document.getElementById("orders-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  try {
    if (btn.dataset.act === "resend") {
      await api("orders/resend", { body: { order_id: btn.dataset.id } });
      toast("Code resent (same code, no new allocation)");
    } else if (btn.dataset.act === "refund") {
      if (!confirm(`Refund ${btn.dataset.id} in full via Telegram Stars? The delivered code will be quarantined and never resold.`)) return;
      if (!confirm("Please confirm again: issue the refund now?")) return;
      await api("orders/refund", { body: { order_id: btn.dataset.id, confirm: true } });
      toast("Refunded and code quarantined");
    }
    loadOrders();
  } catch (err) {
    toast(err.message, true);
  }
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

document.getElementById("set-terms_text").addEventListener("input", showTermsWarning);
document.getElementById("set-privacy_text").addEventListener("input", showTermsWarning);
function showTermsWarning() {
  document.getElementById("terms-warning").hidden = false;
}

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const key of SETTING_KEYS) {
    const el = document.getElementById(`set-${key}`);
    body[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  try {
    const r = await api("settings/update", { body });
    document.getElementById("settings-result").textContent =
      r.terms_changed ? `Saved. ${r.note}` : "Saved.";
    document.getElementById("terms-warning").hidden = true;
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---- wiring ---- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = document.getElementById("login-error");
  errEl.hidden = true;
  try {
    await signIn(document.getElementById("login-email").value, document.getElementById("login-password").value);
    showView("overview");
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

document.getElementById("logout").addEventListener("click", signOut);
for (const b of document.querySelectorAll("#nav button[data-view]"))
  b.addEventListener("click", () => showView(b.dataset.view));

showView(token ? "overview" : "login");
