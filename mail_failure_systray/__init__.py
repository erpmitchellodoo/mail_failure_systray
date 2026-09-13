from . import models


def post_init_hook(env):
    """Import the existing attention queue once, in bounded batches."""
    last_id = 0
    while mails := env['mail.mail'].sudo().search([
        ('id', '>', last_id), ('state', 'in', ['exception', 'outgoing']),
    ], order='id', limit=500):
        mails._failure_center_sync()
        last_id = mails[-1].id
