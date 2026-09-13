import smtplib
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


class MailFailureService(models.AbstractModel):
    _name = 'mail.failure.systray.service'
    _description = 'My Email Center Service'

    def _emails(self):
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(_('The Email Center is available to internal users only.'))
        # Validate allowed_company_ids before any elevated operation.
        self.env.companies
        return self.env['mail.failure.email'].sudo(False)

    def _category_domain(self, category):
        if category not in ('failed', 'pending', 'recent'):
            raise UserError(_('Invalid email category.'))
        domain = [('state', '=', {'failed': 'exception', 'pending': 'outgoing', 'recent': 'sent'}[category])]
        if category == 'recent':
            domain.append(('sent_date', '>=', fields.Datetime.now() - timedelta(days=7)))
        return domain

    @api.model
    def get_systray_summary(self):
        emails = self._emails()
        return {category + '_count': emails.search_count(self._category_domain(category), limit=10 if category == 'recent' else None)
                for category in ('failed', 'pending', 'recent')}

    @api.model
    def get_systray_emails(self, category='failed', limit=10):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise UserError(_('Invalid email limit.'))
        rows = self._emails().search(self._category_domain(category), limit=min(limit, 10) if category == 'recent' else limit,
                                     order='sent_date desc, id desc' if category == 'recent' else None)
        return rows.read(['mail_id', 'subject', 'recipients', 'document', 'model', 'state',
                          'created_date', 'scheduled_date', 'sent_date', 'failure_reason', 'queue_available'])

    def _validate_id(self, value):
        if type(value) is not int or value <= 0 or value > 2147483647:
            raise UserError(_('Invalid email ID.'))

    def _owned_email(self, mail_id):
        self._validate_id(mail_id)
        row = self._emails().search([('mail_id', '=', mail_id)], limit=1)
        if not row:
            raise AccessError(_('This email is unavailable or you no longer have access.'))
        row.check_access('read')
        return row

    @api.model
    def open_document(self, email_id):
        self._validate_id(email_id)
        row = self._emails().search([('id', '=', email_id)], limit=1)
        if not row or not row.model or not row.res_id or row.model not in self.env:
            raise UserError(_('The source document is unavailable.'))
        record = self.env[row.model].browse(row.res_id).exists()
        record.check_access('read')
        if not record:
            raise UserError(_('The source document is unavailable.'))
        return {'type': 'ir.actions.act_window', 'res_model': row.model, 'res_id': row.res_id,
                'views': [(False, 'form')], 'target': 'current'}

    @api.model
    def retry_email(self, mail_id):
        return self._process(mail_id, retry=True)

    @api.model
    def send_now(self, mail_id):
        return self._process(mail_id, retry=False)

    def _process(self, mail_id, *, retry):
        row = self._owned_email(mail_id)
        # Parameterized row lock prevents two requests from sending the same mail.
        # Native _send writes exception before SMTP and participates in this lock.
        self.env.cr.execute('SELECT id FROM mail_mail WHERE id = %s FOR UPDATE SKIP LOCKED', [mail_id])
        if not self.env.cr.fetchone():
            return {'type': 'warning', 'message': _('Email is being processed or has been removed. Refresh the list.')}
        mail = self.env['mail.mail'].with_context({
            'allowed_company_ids': self.env.companies.ids, 'lang': self.env.lang,
        }).sudo().browse(mail_id)
        mail.invalidate_recordset()
        # Refresh metadata and recheck ownership, company and document after lock.
        mail._failure_center_sync()
        row.invalidate_recordset()
        self._owned_email(mail_id)
        expected = 'exception' if retry else 'outgoing'
        if mail.state != expected:
            return {'type': 'warning', 'message': _('Email status has changed. Refresh the list.')}
        if retry:
            mail.action_retry()
            return {'type': 'success', 'message': _('Email queued for retry.')}
        try:
            mail.send(auto_commit=False, raise_exception=False)
        except smtplib.SMTPServerDisconnected:
            # Native deliberately propagates disconnected sessions. Do not claim
            # delivery or resend automatically: SMTP acceptance may be uncertain.
            if mail.exists():
                mail.write({'state': 'exception', 'failure_type': 'mail_smtp'})
            return {'type': 'warning', 'message': _('SMTP connection lost. Delivery could not be confirmed. Check before retrying.')}
        row.invalidate_recordset()
        if row.state == 'sent':
            return {'type': 'success', 'message': _('Email sent successfully.')}
        if row.state == 'outgoing':
            return {'type': 'info', 'message': _('Email remains queued under the native sending limits.')}
        return {'type': 'warning', 'message': _('Delivery failed. The email issue has been updated.')}
