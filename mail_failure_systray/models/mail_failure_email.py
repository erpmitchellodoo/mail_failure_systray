from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError
from odoo.osv import expression


class MailFailureEmail(models.Model):
    """Read-only metadata ledger; never contains bodies, attachments or SMTP secrets."""

    _name = 'mail.failure.email'
    _description = 'My Email'
    _rec_name = 'subject'
    _order = 'created_date desc, id desc'

    owner_id = fields.Many2one('res.users', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, ondelete='cascade')
    mail_id = fields.Integer('Queue ID')
    message_id = fields.Integer('Message ID')
    subject = fields.Char()
    recipients = fields.Char()
    model = fields.Char('Related Model')
    res_id = fields.Integer('Document ID')
    document = fields.Char('Related Document', compute='_compute_document')
    state = fields.Selection([
        ('outgoing', 'Pending'), ('exception', 'Delivery Failed'),
        ('sent', 'Sent'), ('cancel', 'Cancelled'), ('received', 'Received'),
    ])
    failure_reason = fields.Char()
    created_date = fields.Datetime('Created')
    scheduled_date = fields.Datetime('Scheduled Send')
    sent_date = fields.Datetime('Sent')
    queue_available = fields.Boolean(default=True)

    _sql_constraints = [
        ('mail_unique', 'unique(mail_id)', 'An email can only be tracked once.'),
    ]

    def init(self):
        super().init()
        self.env.cr.execute(
            'CREATE INDEX IF NOT EXISTS mail_failure_email_owner_company_state_date_idx '
            'ON mail_failure_email (owner_id, company_id, state, created_date DESC)'
        )

    def _document_allowed_ids(self):
        """Batch document checks in the caller's environment, including archived records.

        Missing documents are hidden, not treated as private/documentless messages.
        This method must also be used for direct read/export, not only search.
        """
        rows = self.sudo().with_context(prefetch_fields=False)
        rows.fetch(['model', 'res_id'])
        groups = defaultdict(set)
        for row in rows:
            if row.model and row.res_id:
                groups[row.model].add(row.res_id)
        allowed = {}
        for model, ids in groups.items():
            records = self.env[model].with_context(active_test=False).browse(ids) if model in self.env else None
            allowed[model] = set(records.exists()._filtered_access('read').ids) if records is not None else set()
        return [row.id for row in rows if (
            not row.model and not row.res_id
            or row.res_id in allowed.get(row.model, set())
        )]

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        if self.env.su:
            return super()._search(domain, offset=offset, limit=limit, order=order)
        # Odoo 18 _search has no bypass_access keyword and uses classic list domains.
        # Search ownership first, then batch ACL checks, then paginate. Never filter
        # after pagination (which produces wrong counters and empty list pages).
        candidates = super()._search(domain, order=order)
        ids = [row[0] for row in self.env.execute_query(candidates.select())]
        allowed = self.browse(ids)._document_allowed_ids()
        safe_domain = expression.AND([domain, [('id', 'in', allowed)]])
        return super()._search(safe_domain, offset=offset, limit=limit, order=order)

    def _check_access(self, operation):
        result = super()._check_access(operation)
        if result or self.env.su or operation != 'read':
            return result
        forbidden = self - self.browse(self._document_allowed_ids())
        if forbidden:
            return forbidden, lambda: AccessError(_('This email is no longer accessible.'))
        return None

    def _compute_document(self):
        groups = defaultdict(set)
        for row in self:
            if row.model in self.env and row.res_id:
                groups[row.model].add(row.res_id)
        names = {}
        for model, ids in groups.items():
            records = self.env[model].with_context(active_test=False).browse(ids).exists()._filtered_access('read')
            names.update({(model, rec.id): rec.display_name for rec in records})
        for row in self:
            row.document = names.get((row.model, row.res_id), False)

    def action_open_document(self):
        self.ensure_one()
        return self.env['mail.failure.systray.service'].open_document(self.id)

    def action_retry(self):
        self.ensure_one()
        result = self.env['mail.failure.systray.service'].retry_email(self.mail_id)
        return self._notification_action(result)

    def action_send_now(self):
        self.ensure_one()
        result = self.env['mail.failure.systray.service'].send_now(self.mail_id)
        return self._notification_action(result)

    def _notification_action(self, result):
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'message': result['message'], 'type': result['type'],
            'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
        }}

    @api.autovacuum
    def _gc_recent_emails(self):
        self.sudo().search([
            ('state', 'not in', ['exception', 'outgoing']),
            ('write_date', '<', fields.Datetime.now() - timedelta(days=7)),
        ], limit=10000).unlink()
