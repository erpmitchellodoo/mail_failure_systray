import smtplib
from unittest.mock import patch

from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user


@tagged('post_install', '-at_install')
class TestMailFailureBrowser(HttpCase):
    def test_internal_user_email_center(self):
        user = new_test_user(self.env, login='email_browser_user', groups='base.group_user,base.group_partner_manager')
        document = self.env['res.partner'].create({'name': 'Email Center Source'})
        self.env['mail.mail'].create({
            'subject': 'Browser failed email', 'email_to': 'customer@example.test',
            'email_from': 'sender@example.test', 'author_id': user.partner_id.id,
            'state': 'exception', 'failure_type': 'mail_smtp',
            'model': 'res.partner', 'res_id': document.id, 'auto_delete': False,
        })
        with patch.object(type(self.env['ir.mail_server']), '_connect__', side_effect=smtplib.SMTPAuthenticationError(535, b'Test rejection')):
            self.browser_js('/odoo', r"""
                (async () => {
                    const waitFor = async (test) => {
                        for (let i = 0; i < 200; i++) {
                            const value = test();
                            if (value) return value;
                            await new Promise(resolve => setTimeout(resolve, 100));
                        }
                        throw new Error('Email Center UI wait timed out: ' + document.body.innerText);
                    };
                    const button = (label) => [...document.querySelectorAll('.o_mail_failure_menu button')].find(el => el.textContent.trim() === label);
                    const toggle = await waitFor(() => document.querySelector('button[aria-label="My Email Issues"]'));
                    await waitFor(() => toggle.textContent.includes('1'));
                    toggle.click();
                    await waitFor(() => button('Retry'));
                    if (!document.querySelector('.o_mail_failure_menu').textContent.includes('Browser failed email')) throw new Error('Missing failed email');
                    button('Retry').click();
                    await waitFor(() => !button('Retry') && document.body.textContent.includes('Email queued for retry.'));
                    button('Pending').click();
                    await waitFor(() => button('Send Now'));
                    button('Send Now').click();
                    await waitFor(() => document.body.textContent.includes('Delivery failed. The email issue has been updated.'));
                    button('Failed').click();
                    await waitFor(() => button('Retry'));
                    button('Open Document').click();
                    await waitFor(() => document.querySelector('.o_form_view'));
                    document.querySelector('button[aria-label="My Email Issues"]').click();
                    await waitFor(() => button('View All My Emails'));
                    button('View All My Emails').click();
                    await waitFor(() => document.querySelector('.o_list_view')?.textContent.includes('Browser failed email'));
                    console.log('test successful');
                })().catch(error => console.error(error));
            """, login=user.login, timeout=100)
