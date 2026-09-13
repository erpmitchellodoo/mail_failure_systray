from odoo import api, fields, models, _


class MailMail(models.Model):
    _inherit = 'mail.mail'

    def _failure_center_sync(self):
        """Private lifecycle hook. No native state, recipient or cleanup changes."""
        Ledger = self.env['mail.failure.email'].sudo()
        mails = self.sudo().with_context(prefetch_fields=False)
        metadata_fields = ['author_id', 'mail_message_id', 'record_company_id', 'model', 'res_id',
                           'subject', 'email_to', 'email_cc', 'recipient_ids', 'state',
                           'create_date', 'scheduled_date', 'failure_type']
        if 'mailing_id' in self._fields:
            metadata_fields.append('mailing_id')
        mails.fetch(metadata_fields)
        mails.recipient_ids.fetch(['email'])
        existing = {row.mail_id: row for row in Ledger.search([('mail_id', 'in', self.ids)])}
        partners = mails.mapped('author_id')
        users = self.env['res.users'].sudo().search([
            ('partner_id', 'in', partners.ids), ('share', '=', False), ('active', '=', True),
        ])
        by_partner = {}
        for user in users:
            by_partner.setdefault(user.partner_id.id, []).append(user)
        new_values = []
        for mail in mails:
            row = existing.get(mail.id)
            if 'mailing_id' in mail._fields and mail.mailing_id:
                if row:
                    row.unlink()
                continue
            authors = by_partner.get(mail.author_id.id, [])
            owner = authors[0] if len(authors) == 1 else False
            # A named external/ambiguous author is never assigned to a queue worker.
            if not mail.author_id:
                creator = mail.mail_message_id.create_uid
                owner = creator if creator.active and not creator.share else False
            if not owner:
                if row:
                    row.unlink()
                continue
            company = mail.record_company_id
            if not company and mail.model in self.env and mail.res_id:
                record = self.env[mail.model].sudo().browse(mail.res_id).exists()
                if record and 'company_id' in record._fields:
                    company = record.company_id
            company = company or (row.company_id if row else self.env.company)
            # Only human-readable native failure categories are exposed. Raw SMTP
            # exceptions may contain credentials, server configuration or payload.
            reason = dict(mail._fields['failure_type'].selection).get(mail.failure_type)
            values = {
                'owner_id': owner.id, 'company_id': company.id,
                'mail_id': mail.id, 'message_id': mail.mail_message_id.id,
                'subject': (mail.subject or _('No subject'))[:512],
                'recipients': ', '.join(filter(None, [mail.email_to, mail.email_cc, *mail.recipient_ids.mapped('email')]))[:2000],
                'model': mail.model, 'res_id': mail.res_id,
                'state': mail.state, 'created_date': mail.create_date,
                'scheduled_date': mail.scheduled_date, 'queue_available': True,
                'failure_reason': (reason or _('Delivery failed. Please contact your administrator for details.')) if mail.state == 'exception' else False,
            }
            if mail.state == 'sent' and not (row and row.sent_date):
                values['sent_date'] = fields.Datetime.now()
            if row:
                row.write(values)
            else:
                new_values.append(values)
        if new_values:
            Ledger.create(new_values)

    @api.model_create_multi
    def create(self, vals_list):
        mails = super().create(vals_list)
        mails._failure_center_sync()
        return mails

    def write(self, vals):
        result = super().write(vals)
        if set(vals) & {'state', 'failure_type', 'failure_reason', 'scheduled_date', 'email_to', 'email_cc', 'recipient_ids', 'subject', 'author_id', 'model', 'res_id', 'record_company_id', 'mailing_id'}:
            self._failure_center_sync()
        return result

    def _postprocess_sent_message(self, success_pids, failure_reason=False, failure_type=None):
        # Odoo 18 signature is (success_pids, failure_reason=False, failure_type=None).
        # Capture metadata before native auto_delete removes queue/message records.
        self._failure_center_sync()
        if failure_type:
            reason = dict(self._fields['failure_type'].selection).get(failure_type, _('Delivery failed.'))
            self.env['mail.failure.email'].sudo().search([
                ('mail_id', 'in', self.ids), ('state', '=', 'exception'),
            ]).write({'failure_reason': reason})
        return super()._postprocess_sent_message(success_pids, failure_reason, failure_type)

    def unlink(self):
        rows = self.env['mail.failure.email'].sudo().search([('mail_id', 'in', self.ids)])
        rows.write({'queue_available': False})
        rows.filtered(lambda row: row.state == 'outgoing').write({'state': 'cancel'})
        return super().unlink()
