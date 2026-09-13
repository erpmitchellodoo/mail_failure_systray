import smtplib
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestMailFailureSystray(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company_b = cls.env['res.company'].create({'name': 'Email Other Company'})
        cls.user_a = new_test_user(cls.env, login='email_center_a', groups='base.group_user',
                                   company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.user_b = new_test_user(cls.env, login='email_center_b', groups='base.group_user',
                                   company_id=cls.company.id, company_ids=[Command.set([cls.company.id])])
        cls.service = cls.env['mail.failure.systray.service'].with_user(cls.user_a)

    def mail(self, user=None, **values):
        return self.env['mail.mail'].sudo().create({
            'subject': 'Quotation test', 'email_from': 'sender@example.test',
            'email_to': 'customer@example.test', 'author_id': (user or self.user_a).partner_id.id,
            'state': 'exception', 'auto_delete': False, 'record_company_id': self.company.id,
            **values,
        })

    def test_failed_owner_and_private_isolation(self):
        own = self.mail()
        other = self.mail(self.user_b)
        rows = self.service.get_systray_emails('failed')
        self.assertIn(own.id, [r['mail_id'] for r in rows])
        self.assertNotIn(other.id, [r['mail_id'] for r in rows])
        self.assertNotIn('body', rows[0])
        self.assertEqual(self.service.get_systray_summary()['failed_count'], 1)
        record = self.env['mail.failure.email'].search([('mail_id', '=', other.id)])
        with self.assertRaises(AccessError):
            record.with_user(self.user_a).read(['subject'])

    def test_pending_and_recent(self):
        pending = self.mail(state='outgoing')
        sent = self.mail(state='sent')
        self.assertEqual(self.service.get_systray_emails('pending')[0]['mail_id'], pending.id)
        self.assertEqual(self.service.get_systray_emails('recent')[0]['mail_id'], sent.id)

    def test_retry_uses_native_method_and_only_queues(self):
        mail = self.mail()
        Mail = type(self.env['mail.mail'])
        native = Mail.action_retry
        with patch.object(Mail, 'action_retry', autospec=True, side_effect=native) as retry:
            with patch.object(Mail, 'send') as send:
                result = self.service.retry_email(mail.id)
        retry.assert_called_once()
        send.assert_not_called()
        self.assertEqual(mail.state, 'outgoing')
        self.assertEqual(result['message'], 'Email queued for retry.')

    def test_send_now_uses_native_method(self):
        mail = self.mail(state='outgoing')
        def mark_sent(records, **kwargs):
            records.write({'state': 'sent'})
        with patch.object(type(mail), 'send', autospec=True, side_effect=mark_sent) as send:
            result = self.service.send_now(mail.id)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs, {'auto_commit': False, 'raise_exception': False})
        self.assertEqual(result['type'], 'success')

    def test_arbitrary_mail_rejected(self):
        other = self.mail(self.user_b)
        with self.assertRaises(AccessError):
            self.service.retry_email(other.id)
        with self.assertRaises(AccessError):
            self.service.send_now(other.id)

    def test_deleted_document_hidden_and_direct_read_denied(self):
        document = self.env['res.partner'].create({'name': 'Source document'})
        mail = self.mail(model='res.partner', res_id=document.id)
        row = self.env['mail.failure.email'].search([('mail_id', '=', mail.id)])
        self.assertTrue(self.service.get_systray_emails('failed'))
        document.unlink()
        self.assertFalse(self.service.get_systray_emails('failed'))
        with self.assertRaises(AccessError):
            row.with_user(self.user_a).read(['subject'])
        with self.assertRaises(UserError):
            self.service.open_document(row.id)

    def test_native_smtp_failure_does_not_crash(self):
        mail = self.mail(state='outgoing')
        with patch.object(type(self.env['ir.mail_server']), 'connect', side_effect=smtplib.SMTPAuthenticationError(535, b'Invalid password')):
            result = self.service.send_now(mail.id)
        self.assertEqual(result['type'], 'warning')
        self.assertEqual(mail.state, 'exception')
        self.assertNotIn('password', self.service.get_systray_emails('failed')[0]['failure_reason'])

    def test_cross_company_isolation(self):
        mail = self.mail(record_company_id=self.company_b.id)
        self.assertFalse(self.service.get_systray_emails('failed'))
        with self.assertRaises(AccessError):
            self.service.retry_email(mail.id)
        with self.assertRaises(AccessError):
            self.service.with_context(allowed_company_ids=[self.company_b.id]).get_systray_summary()

    def test_invalid_id_and_missing_id(self):
        for value in (None, True, '1', -1, 0, 2**40, [1]):
            with self.assertRaises(UserError):
                self.service.retry_email(value)
        with self.assertRaises(AccessError):
            self.service.retry_email(2147483647)

    def test_sent_cannot_retry_or_send(self):
        mail = self.mail(state='sent')
        with patch.object(type(mail), 'send') as send:
            self.assertEqual(self.service.retry_email(mail.id)['type'], 'warning')
            self.assertEqual(self.service.send_now(mail.id)['type'], 'warning')
        send.assert_not_called()

    def test_sent_metadata_survives_native_cleanup(self):
        mail = self.mail(state='sent', auto_delete=True)
        mail._postprocess_sent_message([])
        self.assertFalse(mail.exists())
        recent = self.service.get_systray_emails('recent')
        self.assertEqual(len(recent), 1)
        self.assertFalse(recent[0]['queue_available'])

    def test_external_author_not_assigned_to_creator(self):
        external = self.env['res.partner'].create({'name': 'External author'})
        self.mail(author_id=external.id)
        self.assertFalse(self.service.get_systray_emails('failed'))

    def test_document_access_revocation(self):
        doc = self.env['res.partner'].create({'name': 'Confidential'})
        mail = self.mail(model='res.partner', res_id=doc.id)
        self.assertTrue(self.service.get_systray_emails('failed'))
        self.env['ir.rule'].create({
            'name': 'Hide confidential partner', 'model_id': self.env['ir.model']._get_id('res.partner'),
            'domain_force': "[('id', '!=', %s)]" % doc.id,
        })
        self.assertFalse(self.service.get_systray_emails('failed'))
        with self.assertRaises(AccessError):
            self.service.retry_email(mail.id)

    def test_no_queue_acl_granted(self):
        self.assertFalse(self.env['mail.mail'].with_user(self.user_a).has_access('read'))
        self.assertFalse(self.env['mail.failure.email'].with_user(self.user_a).has_access('create'))

    def test_missing_model_does_not_crash(self):
        mail = self.mail()
        self.env['mail.failure.email'].search([('mail_id', '=', mail.id)]).write({
            'model': 'removed.business.model', 'res_id': 123,
        })
        self.assertFalse(self.service.get_systray_emails('failed'))

    def test_chatter_author_ownership(self):
        self.user_a.group_ids += self.env.ref('base.group_partner_manager')
        document = self.env['res.partner'].create({'name': 'Chatter source'})
        recipient = self.env['res.partner'].create({'name': 'Recipient', 'email': 'recipient@example.test'})
        document.with_user(self.user_a).with_context(mail_notify_force_send=False).message_post(
            body='Private test body', subject='Chatter email', message_type='comment',
            subtype_xmlid='mail.mt_comment', partner_ids=recipient.ids,
        )
        self.assertTrue(any(row['subject'] == 'Chatter email' for row in self.service.get_systray_emails('pending')))

    def test_locked_mail_is_not_sent(self):
        mail = self.mail(state='outgoing')
        # Simulate the empty result from SKIP LOCKED without committing fixtures.
        with patch.object(self.env.cr, 'fetchone', return_value=None):
            with patch.object(type(mail), 'send') as send:
                result = self.service.send_now(mail.id)
        self.assertEqual(result['type'], 'warning')
        send.assert_not_called()

    def test_removed_queue_item_is_safe(self):
        mail = self.mail()
        mail_id = mail.id
        mail.unlink()
        result = self.service.retry_email(mail_id)
        self.assertEqual(result['type'], 'warning')

    def test_native_success_and_recent_with_auto_delete(self):
        from unittest.mock import MagicMock
        mail = self.mail(state='outgoing', auto_delete=True)
        Server = type(self.env['ir.mail_server'])
        with patch.object(Server, '_disable_send', return_value=False), patch.object(Server, '_connect__', return_value=MagicMock()):
            with patch.object(Server, 'send_email', return_value='<test@example.test>') as smtp:
                result = self.service.send_now(mail.id)
        smtp.assert_called_once()
        self.assertEqual(result['type'], 'success')
        self.assertFalse(mail.exists())
        self.assertEqual(self.service.get_systray_emails('recent')[0]['state'], 'sent')

    def test_native_invalid_recipient_cleanup_retains_issue(self):
        from unittest.mock import MagicMock
        mail = self.mail(state='outgoing', auto_delete=True, email_to=False)
        Server = type(self.env['ir.mail_server'])
        with patch.object(Server, '_disable_send', return_value=False), patch.object(Server, '_connect__', return_value=MagicMock()):
            result = self.service.send_now(mail.id)
        self.assertEqual(result['type'], 'warning')
        self.assertEqual(self.service.get_systray_emails('failed')[0]['state'], 'exception')

    def test_template_ownership(self):
        self.user_a.group_ids += self.env.ref('base.group_partner_manager')
        doc = self.env['res.partner'].create({'name': 'Template Source'})
        template = self.env['mail.template'].create({
            'name': 'Center test', 'model_id': self.env['ir.model']._get_id('res.partner'),
            'subject': 'Template Email', 'email_to': 'recipient@example.test',
            'body_html': '<p>Test</p>',
        })
        mail_id = template.with_user(self.user_a).send_mail(doc.id, force_send=False)
        self.assertIn(mail_id, [row['mail_id'] for row in self.service.get_systray_emails('pending')])
