# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    l10n_ar_padron_aliquot_ids = fields.One2many(
        "l10n_ar.partner.padron.aliquot",
        "partner_id",
        string="Alícuotas IIBB (Padrón)",
    )
