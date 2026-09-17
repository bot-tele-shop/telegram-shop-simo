# Admin Experience Specification

Status: Phase 3 product and interaction specification. This document defines the owner experience to implement against the canonical API in `TARGET_ARCHITECTURE.md`.

## Task contract

**Goal:** let a motivated non-technical owner operate the store safely without understanding database tables, payment callbacks, encryption, worker jobs, or Telegram internals.

**In scope:** navigation, pages, daily workflows, forms, tables, statuses, empty/error states, permissions, confirmations, and operational safeguards.

**Out of scope:** visual branding, final CSS, backend implementation, provider onboarding, advanced accounting, and multi-store management.

**Activation event:** the owner creates a product, loads or configures its fulfillment, previews it, and makes it available in Telegram.

**Stop condition:** every required owner action has one discoverable location, plain-language states, safe confirmation behavior, and a defined permission.

## Design principles

1. Show what needs action before showing analytics.
2. Use customer and business language, not internal state or table names.
3. Keep dangerous actions reversible where possible and explicit where not.
4. Never expose unused inventory secrets in lists, search, exports, logs, or dashboards.
5. Put common actions on the page; reserve overflow menus for uncommon actions.
6. Preserve entered form data after validation or network errors.
7. Show a final outcome for every action. A request being accepted is not presented as a completed refund or delivery.
8. Empty states explain the next useful action without fake sales or sample data.

## Navigation

The desktop sidebar contains seven task-oriented destinations:

| Destination | Purpose | Sub-navigation |
|---|---|---|
| **Home** | Current store status and work requiring attention | none |
| **Catalog** | What the store sells and how it is fulfilled | Products, Inventory, Categories |
| **Sales** | Orders and money movement | Orders, Payments, Refunds |
| **Customers** | Customer lookup, balances and history | Customers, Balance activity |
| **Marketing** | Promotions, referrals and messages | Promotions, Referrals, Broadcasts |
| **Support** | Customer requests and operator replies | Open, Waiting, Closed |
| **Settings** | Store behavior and access | Store, Checkout, Notifications, Legal, Team, Integrations, Activity log |

The mobile layout uses the same hierarchy in a navigation drawer. It does not create a different set of destinations. Breadcrumbs appear only inside real object hierarchy, such as `Catalog / Products / Product name`.

A persistent store-status control in the sidebar shows **Checkout open** or **Checkout paused**. Pausing checkout requires confirmation and explains that paid orders will still be delivered.

## Home

Home answers three questions in order:

1. Is the store accepting orders?
2. Is anything waiting for me?
3. How is the store performing?

### Attention queue

This is the first content when any actionable issue exists. Each row has a plain-language reason, age, affected order/product, and direct action.

Priority order:

1. Paid orders that cannot be fulfilled
2. Refunds requiring confirmation or reconciliation
3. Failed or exhausted delivery attempts
4. Payment events requiring review
5. Manual fulfillment tasks nearing their promised deadline
6. Products out of stock or below their threshold
7. Failed broadcasts or disconnected integrations

Internal job names, stack traces, provider payloads, and webhook identifiers are not shown. Technical details may appear in a permissioned advanced section with sanitized values.

### Operational summary

Show compact decision-relevant metrics:

- Sales today and selected comparison period
- Paid orders
- Orders awaiting delivery
- Refund amount and count
- Products low or out of stock

Metrics link to the corresponding filtered list. Do not show decorative metrics that cannot lead to a decision.

### Quick actions

- Add product
- Add inventory
- Find order
- Message customers

### First-run state

Until the first product is live, Home replaces metrics with a dismiss-free setup path:

1. Add store details
2. Create a product
3. Configure delivery or add inventory
4. Preview in Telegram
5. Make product available

Completed steps collapse. The setup path disappears after activation and can later be reached from Help rather than occupying permanent dashboard space.

## Catalog

### Products list

Use a table because owners compare repeated products.

Default columns:

- Product
- Category
- Price
- Availability
- Delivery method
- Store status
- Sales in selected period
- Actions

Search covers product name and SKU. Filters cover live/draft/paused/archived, category, low stock, and delivery method. Default sorting puts attention-needed products first, then active products.

Primary row actions are **Edit**, **Add inventory** when relevant, and **Pause/Make available**. **Archive** is in the overflow menu. A product referenced by an order is archived, never physically deleted.

Statuses use:

- **Draft** — not visible to customers
- **Ready** — configured but not yet available
- **Available** — visible and purchasable
- **Paused** — visible only if configured, but cannot be purchased
- **Out of stock** — automatically unavailable until stock is added
- **Archived** — retained for order history

### Product creation

Use a guided four-step form. Progress is visible, and a draft may be saved after the required identity fields exist.

#### 1. Product details

- Product name
- Description
- Category
- Optional image
- Internal SKU under **Advanced**; generate a sensible default

#### 2. Price

- Amount
- Currency/payment method supported by the store
- Optional promotion eligibility

The form explains that later price changes affect new purchases only.

#### 3. Customer delivery

The owner chooses a plain-language option:

- **A different code for every customer** → `unique_code`
- **A different link for every customer** → `unique_url`
- **A downloadable file** → `download_file`
- **The same content for every customer** → `reusable_content`
- **I will fulfill this order myself** → `manual`
- **Time-limited access or subscription** → `subscription_access`

Only fields needed by the selected method appear. Implementation terms such as inventory policy or fulfillment strategy are never required knowledge.

#### 4. Review and availability

Show the exact customer-facing title, description, price, availability, and delivery promise. The final actions are:

- **Save as draft**
- **Preview in Telegram**
- **Make available**, enabled only when required content or stock exists

Validation occurs on submit and when leaving a step, not on the first keystroke. Errors appear beside the field, preserve input, and explain how to fix the problem.

### Product detail

Tabs:

- Overview
- Inventory or Content, depending on delivery method
- Orders
- Activity

Overview shows the customer-facing content, price, availability, low-stock threshold, and recent sales. Activity shows relevant audited changes without exposing global system events.

### Inventory

Inventory starts with product selection/search and summary counts:

- Available
- Reserved for checkout
- Sold
- Quarantined
- Retired

The default list shows masked fingerprint, state, added date, and related order when assigned. It never shows ciphertext or plaintext. Searching by a known full code is not supported because it would require unnecessarily transmitting secrets.

#### Bulk upload

The owner may paste lines or upload a UTF-8 text/CSV file. The flow has two stages:

1. **Check inventory** validates format locally and server-side and shows counts for new, duplicate, blank, and invalid rows.
2. **Add inventory** commits all accepted rows atomically or adds none.

The result reports added, duplicate, and rejected counts with a downloadable error report that contains line numbers and reasons but does not echo accepted secrets. The original paste is cleared after success.

Allowed actions on unused stock are **Retire selected** and **Export fingerprints**. Revealing unused stock is not a normal admin feature. Sold or quarantined items cannot be returned to available through the UI.

Low-stock thresholds are configured per finite product with a store default. Alerts clear automatically when stock is replenished.

### Categories

Categories support name, description, image, display order, active state, and optional parent. Reordering is a direct ordered-list action. A category containing products can be hidden but not deleted without moving those products.

## Sales

### Orders list

Default columns:

- Order reference
- Customer
- Product
- Total
- Payment
- Delivery
- Ordered at
- Primary action

Search accepts order reference, customer username/id, product, and provider charge reference. Filters cover date, payment status, delivery status, product, refund state, and attention required. Saved URL query parameters preserve filtered views from Home alerts.

Customer-facing labels replace internal states:

| Internal condition | Admin label |
|---|---|
| unpaid/pending | Awaiting payment |
| paid + queued | Paid — delivery queued |
| paid + processing | Delivering |
| delivered | Delivered |
| manual action | Waiting for manual fulfillment |
| failed/retry available | Delivery needs attention |
| refund pending | Refund in progress |
| anomalous/review required | Payment needs review |
| refunded | Refunded |

Status is communicated by text and icon, not color alone.

### Order detail

The header shows order reference, customer, amount, current outcome, and the single most useful next action. Sections:

- **Summary:** product and price snapshot, purchase time, customer
- **Payment:** expected/received amount, method, confirmed time, refund history
- **Delivery:** delivery method, assigned-item fingerprint, attempts and customer-visible result
- **Timeline:** chronological audited events in plain language
- **Support:** linked tickets and operator notes

Provider ids and sanitized technical fields live behind **Advanced details**.

Actions are shown only when valid:

- **Retry delivery** sends the already assigned content and never allocates replacement stock.
- **Mark manually fulfilled** requires a note and is available only for manual products.
- **Start refund** creates a refund request; it does not claim success immediately.
- **Cancel unpaid order** releases eligible reservations.
- **Add internal note** is non-customer-visible and audited.

### Refund interaction

The refund dialog shows customer, order, original amount, refundable amount, payment method, and effect on delivered inventory or entitlement. It requires a reason and a final confirmation whose button includes the amount, for example **Refund 250 Stars**.

After submission, show **Refund in progress**. Only a confirmed provider result changes it to **Refunded**. Unknown results say **We could not confirm the refund. Do not retry yet; this order is in review.**

### Payments

Payments are read-only except for review/resolution actions. Columns: payment reference, order, customer, method, amount, status, received time. The default page hides successful routine payments behind normal filters and places anomalies first.

An anomaly view explains the mismatch in owner language: wrong amount, unknown order, duplicate provider event, wrong customer, or unconfirmed provider result. Resolution choices are constrained by the backend state machine and require a note.

### Refunds

Refunds show Requested, Processing, Confirmed, Failed, or Needs review. A retry action is available only when the backend proves the provider did not refund. Unknown outcomes allow **Check status** or **Open review**, never blind retry.

## Customers

The customer table shows customer, Telegram identifier, first/last activity, paid orders, total spent, wallet balance, referral state, and account status.

Customer detail contains:

- Order history
- Payment/refund history
- Wallet activity
- Referral relationship and rewards
- Support tickets
- Terms acceptance versions
- Internal notes and account restrictions

### Balance adjustment

The balance is never directly editable. **Adjust balance** opens a form with:

- Credit or debit
- Amount and currency
- Required reason
- Optional external reference
- Preview of current and resulting balance

Debits that would make the balance negative are rejected. The submission creates one immutable ledger entry and audit event. The result links to both records.

## Marketing

### Promotions

The list shows code, discount, validity, use count/limit, eligibility, and status. Creation is a guided form for code, discount, eligible products/categories, date window, total limit, per-customer limit, and minimum spend. The review step gives examples of qualifying and non-qualifying purchases.

Used promotions are paused or ended, not destructively changed in ways that rewrite order history.

### Referrals

The overview shows referred customers, qualifying purchases, pending rewards, available rewards, and held/rejected rewards. Fraud holds state the owner action required. Owners may approve or reject a held reward with a reason but cannot rewrite referral attribution after a qualifying purchase.

### Broadcasts

Creation steps:

1. Choose audience using understandable filters
2. Write message and optional button
3. Preview exactly as it will appear in Telegram
4. Show recipient estimate and exclusions
5. Send test to owner
6. Schedule or send

The final confirmation states the recipient count. Progress shows queued, sent, blocked, failed, and skipped. Broadcasts are delivered through durable jobs with rate limiting and may be paused. The UI never promises instant delivery.

## Support

Support uses an inbox layout with Open, Waiting for customer, Waiting for owner, and Closed states. A ticket includes customer context, linked orders, conversation, internal notes, and response composer. Commerce actions link to the order page rather than being duplicated inside support.

## Settings

Settings are grouped by owner intent:

- **Store:** name, description, support contact, locale and timezone
- **Checkout:** open/paused, accepted payment methods, reservation duration and customer-facing delivery expectations
- **Notifications:** low-stock, failed delivery, refund, payment-review and daily summary preferences
- **Legal:** current terms and privacy text with version/effective-date preview
- **Team:** administrators, roles, invitations and recent access
- **Integrations:** Telegram, object storage, and approved suppliers shown as Connected, Needs attention, or Not configured
- **Activity log:** searchable privileged actions and their outcomes

Secrets are entered through write-only controls. Existing values display **Configured**, never the secret. Saving legal text clearly states that customers will be asked to accept a new version. Unsaved-change warnings protect multi-field forms.

## Roles and permissions

Initial built-in roles:

| Role | Intended access |
|---|---|
| **Owner** | All actions, team access, settings, refunds, balance adjustments and audit export |
| **Manager** | Catalog, orders, inventory, customers, promotions, broadcasts and support; no team or secret configuration |
| **Fulfillment** | Orders, delivery retries, manual fulfillment, inventory upload and support notes; no refunds or balances |
| **Support** | Customer/order read access, ticket replies and internal notes; no secrets, refunds, stock mutation or balances |
| **Analyst** | Read-only sales, customer and marketing reporting with sensitive fields minimized |

The server enforces permissions; hiding a button is not authorization. Every sensitive action records actor, target, reason, correlation id, result, and sanitized before/after values.

## Common interaction states

Every page defines:

- Loading skeleton appropriate to its table or form
- First-run empty state with one primary next action
- Filtered empty state with **Clear filters**
- Network/server error stating what failed and offering a safe retry
- Partial-data warning when analytics are delayed but operations remain available
- Permission-denied state that explains which role is required
- Disconnected integration state with a link to its setup or owner contact

Buttons that submit disable while in flight. Repeated clicks use the same request id. Closing a completed dialog does not repeat the action. Toasts supplement but never replace persistent status on consequential operations.

## Responsive behavior

- Desktop is optimized for frequent operations and comparison.
- Tablet retains tables with fewer columns and a row-detail drawer.
- Mobile prioritizes Home alerts, order lookup, support replies, pause checkout, and safe retry actions.
- Dense inventory, payment, audit, and analytics tables may scroll horizontally rather than hiding critical meaning.
- Bulk inventory import and complex product configuration remain usable on mobile but may recommend desktop for large files.

## Existing dashboard disposition

### Keep

- Supabase owner sign-in with server-side authorization
- Checkout status visibility
- Hidden-by-default product creation
- Product price snapshots for existing orders
- Paste/file stock import
- Fingerprints instead of inventory values
- Same-assigned-item resend language
- Explicit checkout pause semantics

### Replace

- Five flat single-page sections with the task-oriented hierarchy above
- Product/source fields with guided fulfillment choices
- Raw order-state filters with customer/business language
- Immediate refund-success messaging with durable progress and reconciliation states
- Card-only overview with an actionable attention queue
- Free-text categories with managed category records
- Stock row-by-row upload with preview and atomic import

### Remove

- Any UI path that directly edits database concepts
- Any normal endpoint or screen capable of listing unused secrets
- Claims such as **Refunded** or **Delivered** before the provider/backend confirms them
- Technical failure strings shown directly to the owner

## Phase 3 acceptance criteria

- A first-time owner can create and publish each supported product type without knowing fulfillment or inventory terminology.
- Every requested owner capability has exactly one primary home in navigation.
- Home exposes paid-but-unfulfilled, refund-review, delivery-failure and low-stock work before analytics.
- Product activation is impossible until required delivery content or inventory is ready.
- Bulk inventory upload previews validation and commits atomically without echoing accepted secrets.
- Orders expose payment and fulfillment as separate understandable statuses.
- Resend always means resend the assigned artifact, never allocate another item.
- Refund UI distinguishes requested, uncertain, failed and confirmed outcomes.
- Balance changes require an amount, reason, preview, immutable ledger entry and audit event.
- Promotions, referrals and broadcasts show eligibility or recipient impact before activation.
- Permissions are enforced server-side and sensitive actions are audited.
- Empty, loading, error, filtered-empty, permission and degraded states are specified.

