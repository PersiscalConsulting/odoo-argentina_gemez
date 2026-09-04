# -*- coding: utf-8 -*-
from odoo import fields, models


class L10nArPartnerPadronAliquot(models.Model):
    """Alícuota de IIBB (percepción / retención) vigente para un partner en
    una jurisdicción y período determinados.

    Se completa de dos formas:

    * Manualmente por el usuario (alta de partner con alícuota manual): con
      prioridad sobre el padrón.
    * Automáticamente, la primera vez que se resuelve la alícuota de un
      partner contra el padrón cargado (ver ``account.tax._l10n_ar_get_padron_rate``),
      para no tener que releer el archivo de padrón en cada línea de factura
      del mismo período.

    Es el equivalente, renombrado y adaptado a Odoo 19, del modelo
    ``res.partner.arba_alicuot`` del módulo ADHOC v17.0
    ``l10n_ar_account_withholding``.
    """

    _name = "l10n_ar.partner.padron.aliquot"
    _description = "Alícuota IIBB de Padrón por Partner"
    _order = "to_date desc, from_date desc"

    # Evita filas de caché duplicadas (mismo partner/jurisdicción/período)
    # ante creación concurrente (dos facturas del mismo partner resueltas
    # casi al mismo tiempo, ninguna encuentra caché todavía). NULL no es
    # igual a NULL en Postgres, así que esto no restringe alícuotas
    # manuales con `from_date`/`to_date` vacíos (vigencia abierta); esas
    # siguen sin protección de unicidad, aceptado como alcance de esta
    # constraint. Ver `account.tax._l10n_ar_resolve_rate_from_padron` para
    # cómo se maneja la violación de esta constraint en el `create()`
    # automático.
    _partner_state_period_uniq = models.Constraint(
        'unique(partner_id, company_id, state_id, from_date, to_date)',
        'Ya existe una alícuota cargada para este partner, jurisdicción y período.',
    )

    partner_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    state_id = fields.Many2one(
        "res.country.state",
        string="Jurisdicción",
        domain="[('country_id.code', '=', 'AR')]",
        required=True,
    )
    from_date = fields.Date("Vigente desde")
    to_date = fields.Date("Vigente hasta")
    numero_comprobante = fields.Char(
        "Origen / Nro. Comprobante",
        help="Referencia de dónde se obtuvo la alícuota: número de "
             "comprobante del padrón, o 'Alícuota no inscripto' si el CUIT "
             "no figuraba en el padrón al momento de resolverla.",
    )
    alicuota_percepcion = fields.Float("Alícuota Percepción (%)")
    alicuota_retencion = fields.Float("Alícuota Retención (%)")
    is_manual = fields.Boolean(
        "Cargada manualmente",
        default=True,
        help="Si está marcado, esta alícuota fue cargada a mano y tiene "
             "prioridad sobre la que figure en el padrón cargado en el "
             "sistema. Se desmarca automáticamente en los registros que el "
             "sistema crea al resolver la alícuota contra el padrón.",
    )
