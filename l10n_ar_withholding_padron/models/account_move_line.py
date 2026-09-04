# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMoveLine(models.Model):
    """Enganche de la percepción de padrón en el motor de impuestos por
    default de la línea de factura.

    ``_get_computed_taxes()`` es el hook nativo (Odoo 19) que calcula los
    impuestos por defecto de una línea (producto + posición fiscal, etc.)
    antes de que se asignen a ``tax_ids``. Interceptamos ahí: por cada
    impuesto por defecto que sea una percepción de padrón
    (``account.tax._l10n_ar_is_padron_perception_tax``), lo resolvemos a la
    alícuota vigente del partner **a la fecha del comprobante** (no la
    fecha de hoy) antes de que el motor de impuestos calcule nada. Así el
    monto de la percepción se computa 100% nativo (``amount_type='percent'``),
    sin necesidad de tocar el motor de cómputo en sí.
    """

    _inherit = "account.move.line"

    def _get_computed_taxes(self):
        taxes = super()._get_computed_taxes()
        if not self.move_id.is_sale_document(include_receipts=True):
            return taxes

        padron_taxes = taxes.filtered(lambda t: t._l10n_ar_is_padron_perception_tax())
        if not padron_taxes:
            return taxes

        date = (
            self.move_id.invoice_date
            or (self.move_id.reversed_entry_id.invoice_date if self.move_id.reversed_entry_id else False)
            or fields.Date.context_today(self)
        )
        partner = self.partner_id or self.move_id.partner_id
        resolved = taxes - padron_taxes
        for tax in padron_taxes:
            resolved |= tax._l10n_ar_resolve_padron_tax(partner, date)
        return resolved
