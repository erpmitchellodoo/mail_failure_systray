# My Unsent Emails — Odoo 19 V1

Technical module: `mail_failure_systray`. Dependencies: Community `mail` and `web`.

Internal users get an envelope systray button with a **failed-only** badge. The
dropdown opens on Failed, with Pending and Recent tabs, native Retry/Send Now,
document links, and a standard read-only My Emails list. Refresh happens on mount,
opening, actions, manual refresh, and every 60 seconds while the page is visible.
Recent shows at most ten successful emails from the last seven days.

## Source audit (this Odoo 19 checkout)

| Concern | Native source and result |
| --- | --- |
| Outgoing queue | `addons/mail/models/mail_mail.py`, model `mail.mail` |
| States | `outgoing`, `sent`, `received`, `exception`, `cancel` |
| Failure details | `mail.mail.failure_type` and `failure_reason`; notification equivalents also exist |
| Retry | `mail.mail.action_retry()` filters exception mails and calls `mark_outgoing()`; this queues, it does not deliver |
| Immediate delivery | `mail.mail.send(auto_commit=False, raise_exception=False)`; delegates to `_send()`, native server selection, throttling, recipient preparation and postprocessing |
| Cron | `process_email_queue()` selects outgoing mails with absent/due `scheduled_date`; native `mail.ir_cron_mail_scheduler_action` |
| SMTP | `odoo/addons/base/models/ir_mail_server.py`: `_connect__()`, `_build_email__()`, `send_email()`; credentials remain in the native server model |
| Message | `mail.mail` delegates to `mail.message` through required, indexed `mail_message_id` (`_inherits`) |
| Notifications | `mail.notification` has required indexed `mail_message_id`, optional indexed `mail_mail_id`, recipient partner/address, `notification_status`, failure type/reason; one message can have many queue items and recipient notifications |
| Notification states | `ready`, `process`, `pending`, `sent`, `bounce`, `exception`, `canceled`; these are different from queue states |
| Recipients | Queue `email_to`, `email_cc`, `recipient_ids`; notifications `res_partner_id`, `mail_email_address` |
| Source document | Inherited `mail.message.model` and `res_id`; `record_name` is computed. `record_company_id` is available on the message |
| Author | Indexed `mail.message.author_id` is a partner, not a user |
| Creator | `mail.mail.create_uid` and `mail.message.create_uid` are audit users, not a universal human initiator |
| Sent timestamp | No dedicated native queue sent timestamp. The ledger records when a native transition to sent is observed; `mail.message.date` is message date, not delivery confirmation |
| Cleanup | `_postprocess_sent_message()` may unlink auto-delete sent mails and invalid/missing-recipient failures. Notification mail unlink preserves the parent message; standalone queue unlink can delete it |

Flow inspection:

- Chatter `mail.thread.message_post()` creates a message; `_notify_thread_by_email()`
  creates mails and notifications using `sudo()` and cleaned context. It can call
  `send_after_commit()`, whose callback uses the superuser. This does not make the
  worker responsible for the message.
- `mail.compose.message._action_send_mail_comment()` uses `message_post()` or
  `message_notify()`. `_action_send_mail_mass_mail()` creates queue records with
  elevated access after native document checks.
- `mail.template.send_mail()` / `send_mail_batch()` check document access, generate
  values, then create mails with elevated access. Templates can supply sender
  identities independently of the human launching them.
- Sale and purchase Send by Email actions open the mail composer. Sale order
  notifications also use `message_post_with_source()`; deferred sale confirmation
  stores `pending_email_template_id`, not a universal initiating-user field.
- `account.move.send._send_mail()` calls `move.message_post()`. Invoice send data
  carries author partner information, but `_send_mails()` may recompute the author
  from a template's sender. This is why author and initiating user are distinct.
- `stock.picking._send_confirmation_email()` uses `message_post_with_source()`.
- Automated actions and scheduled workflows execute under configured users and
  can override authors. No common native field reliably records a human initiator
  across all these flows.
- `mail.scheduled.message` delays posting and later posts as its creator.
  `mail.message.schedule` delays notifications. Neither is an outgoing queue item
  until native processing creates `mail.mail`; they are outside the Pending tab.
- `mass_mailing` adds `mailing_id` and mailing traces and overrides outgoing
  formatting/postprocessing. Campaign queue items are excluded from V1, preserving
  native unsubscribe, trace and campaign behavior.

Frontend references inspected:

- `addons/mail/static/src/core/web/activity_menu.js` and `.xml`:
  OWL `Component`, static template/components/props, `Dropdown`,
  `useDropdownState`, named content slot, `beforeOpen`, badge and action service.
- `addons/mail/static/src/core/public_web/messaging_menu.js`:
  OWL state, services, dropdown and systray integration.
- `addons/web/static/src/webclient/navbar/navbar.js`: systray registry rendering.
- V1 registers with `registry.category("systray").add(...)`, internal-user
  `isDisplayed`, `web.assets_backend`, ORM/action/notification services and standard
  Bootstrap/Odoo styling. No legacy widgets, jQuery, `web.ajax` or SMTP JS.

## Meaning of “My Emails”

V1 deliberately defines **responsibility by the native message author**, matching
Odoo's author-based failure notifications. At queue creation, the author partner
must resolve to exactly one active internal user. If an author is absent, an
active internal message creator is the conservative fallback. External or
ambiguous authors are excluded; a worker is never substituted for them. Document
salesperson, assigned employee, recipient, follower and raw sender email address
do not grant ownership.

The smallest extension is a separate metadata ledger. It holds the resolved
responsible user and company without adding fields to the native queue or
rewriting business sending flows. It does **not** claim universal human initiator
tracking. An automated email authored by an internal service account belongs to
that account. Sending on behalf of another internal author attributes responsibility
to that author. Queue lifecycle synchronization updates metadata when native queue
fields change.

Company attribution uses the native message company, then the source record's
company, then the creation environment's company (retained for later lifecycle
updates). Missing/removed source records are hidden rather than reclassified as
documentless private emails. Current source access is always required.

## Security and concurrency

- No new ACL or record rule on `mail.mail`, `mail.message` or `mail.notification`.
- Internal users have read-only access to `mail.failure.email`. A global ownership
  and allowed-company rule applies even if a list filter is removed.
- `_search()` batches document read-ACL checks before pagination, counts and
  grouping. `_check_access()` repeats them for direct reads/exports. Document
  labels and opening use the caller's environment without `sudo()`.
- The abstract service rejects non-internal users, invalid IDs/categories/limits,
  unauthorized companies and arbitrary queue IDs.
- Elevated queue access is confined to private lifecycle hooks and a single
  validated action. Action context is rebuilt from validated companies/language.
  Ownership and source access are checked again after acquiring a parameterized
  `FOR UPDATE SKIP LOCKED` queue-row lock and refreshing state.
- Retry only permits exception; Send Now only permits outgoing. Already sent,
  removed, locked or cron-processed items return a warning instead of resending.
- Native delivery handles most SMTP failures. A disconnected session returns an
  uncertainty warning; database errors still propagate for transaction rollback.
  SMTP cannot provide general exactly-once delivery after an ambiguous network
  disconnect or a transaction failure following acceptance.
- Bodies and attachments never enter the ledger or service response. SMTP raw
  exception text is not exposed because it can contain credentials or payload.
  The interface displays native failure category labels; administrators retain
  the full native error. No new body/attachment opening action is provided.

## Performance and retention

The systray queries the metadata ledger, never scans the native queue. An index on
`(owner_id, company_id, state, created_date DESC)` supports ownership/category
lookups. The unique mail ID constraint supplies its own index. Native author,
queue-message and notification indexes are reused; no speculative native queue
indexes are added. Source checks are batched by model with explicit metadata
fetching, not body/attachment prefetching.

Exact access-aware counts require checking the requesting user's matching source
records, so costs grow with that user's outstanding queue, not the whole database.
This is not a constant-time counter for users with extremely large personal queues.
An installation-only hook imports existing pending/failed records in batches of
500. Existing sent history is not imported because native sent timestamps are
unavailable. A private autovacuum hook removes completed metadata older than
seven days; outstanding issues remain. Native queue deletion is unchanged.

## Files and extension points

All implementation files are new, under `custom/mail_failure_systray/`:

- `__init__.py`, `__manifest__.py`: registration, assets, initial attention-queue import.
- `models/__init__.py`, `models/mail_mail.py`: native lifecycle metadata capture.
- `models/mail_failure_email.py`: read-only ledger, dynamic source access, list actions, retention.
- `models/mail_failure_service.py`: summary, limited category metadata, retry/send/open service.
- `security/security.xml`, `security/ir.model.access.csv`: global ownership/company rule and read ACL.
- `views/mail_failure_views.xml`: list/search/action and My Emails menu.
- `static/src/components/mail_failure_systray/mail_failure_systray.{js,xml,scss}`: OWL component.
- `tests/__init__.py`, `tests/test_mail_failure_systray.py`, `tests/test_mail_failure_browser.py`.
- This report: `README.md`.

No pre-existing project file was changed by this implementation. Existing changes
in `.gitignore`, `addons/stock_account/views/stock_move_views.xml` and
`odoo/service/server.py` were left intact.

## Validation

Installed and upgraded in isolated database `mail_failure_systray_v1_test`, using
the configured PyCharm Python 3.12.9 SDK and Community addon paths. Final suite:
22 tests, zero failures/errors, including the real headless Chrome HttpCase.
Native SMTP transport is mocked, so no external test email is delivered.
The final clean installation in `mail_failure_systray_v1_clean` also passed all
22 tests. Logs: `/tmp/mail_failure_upgrade.log` and `/tmp/mail_failure_clean.log`.

Coverage includes ownership and private isolation; pending/recent; native retry
and send delegation; native successful delivery state and auto-delete; SMTP
authentication rejection; invalid recipients; arbitrary/invalid/missing IDs;
already-sent rejection; deleted/missing source; access revocation; company
isolation/context spoofing; unavailable lock response; removed queue item;
chatter/template ownership; and lack of queue/create ACL grants.

The browser test logs in as a normal internal user and verifies badge, default
Failed dropdown, Retry, Pending, Send Now with native SMTP failure, document form,
and metadata list. Browser console errors fail the test. Compiled JS/XML/SCSS
assets loaded successfully.

The first run exposed two invalid test fixtures (contact posting permission and
creating against a nonexistent model); these were corrected. Extended native
send tests explicitly enable the native test sending path while mocking transport.
The browser runner initially skipped because `websocket-client` was absent; it
was installed under `/tmp/mail_failure_test_deps` only, then Chrome passed.

The host PostgreSQL is 12.22. This Odoo checkout warns that its minimum is 13.
Tests passed, but the environment warning is pre-existing and remains; this
module does not upgrade a database server. Enterprise compatibility is by use of
Community APIs; a full Enterprise business-flow matrix has not been executed.

Example validation command (substitute your configured interpreter and config):

```sh
PYTHONPATH=/tmp/mail_failure_test_deps /home/vishnu/odoo/odoo19/odoo19/bin/python odoo-bin \
  -c odoo.conf -d mail_failure_systray_v1_test --addons-path=addons,custom \
  -u mail_failure_systray --test-enable --test-tags=/mail_failure_systray \
  --stop-after-init --http-port=8119 --max-cron-threads=0
```

Installation into an existing working database awaits its name. No working server
was restarted and no existing business queue was sent during validation.

## V1 limits and possible Phase 2

- Retry queues the existing message; it does not regenerate template bodies or
  static `email_to` addresses. Native partner recipients use current partner email
  at delivery. Correcting a static address requires sending a new email from the
  document.
- When native cleanup has removed an invalid-recipient failure, V1 shows the issue
  with an Open Document/new-email explanation and disables Retry. It does not
  recreate a deleted queue item. Deleted-document issues are hidden.
- A successful queue state means SMTP submission, not proof of recipient inbox
  delivery. Later bounces and partial recipient notification failures on a queue
  item marked sent are not a separate notification-level issue center in V1.
- No universal initiating-human inference for automated/deferred sender overrides;
  no campaign, team or manager view; no pre-queue scheduled-message controls.
- Raw SMTP details are intentionally replaced by safe categories. Recent history
  is limited and begins with module tracking.
- Phase 2 candidates, not implemented: explicit initiator propagation through
  deferred business workflows; per-recipient bounce/partial-delivery handling;
  safe native notification resend after queue cleanup; issue dismissal; bus-driven
  refresh; optional team access with its own security design.
