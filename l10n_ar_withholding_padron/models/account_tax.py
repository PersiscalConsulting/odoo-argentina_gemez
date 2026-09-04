# -*- coding: utf-8 -*-
import logging
import re
from datetime import timedelta

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)


class AccountTax(models.Model):
    """Percepción de IIBB "según alícuota del padrón/partner" en Odoo 19.

    Decisión de diseño (ver README.rst del módulo para el detalle): el motor
    de impuestos de Odoo 19 (``account.tax._get_tax_details`` /
    ``_eval_tax_amount_*``) fue rediseñado para computar en batch y está
    espejado 1:1 en JavaScript (``account_tax.js``) para el preview en el
    formulario de factura; no recibe el partner de la línea ni expone un
    punto de extensión razonable para una alícuota variable por partner
    dentro de un mismo ``amount_type``. Confirmado además contra el propio
    código v19 real de ADHOC (``ingadhoc/odoo-argentina`` rama ``19.0``,
    módulo ``l10n_ar_tax``, que reemplaza a la cadena v17
    ``l10n_ar_account_withholding`` + ``l10n_ar_withholding_ux``): ese
    módulo tampoco usa un ``amount_type`` dinámico, resuelve qué impuesto
    (100% nativo, ``amount_type='percent'``) aplicar ANTES de que el motor
    calcule, en ``account.move.line._get_computed_taxes()``.

    Replicamos ese mismo patrón, simplificado al alcance pedido (percepción
    de venta vía padrón/partner, sin la capa completa de posiciones
    fiscales ni de retenciones por webservice de ADHOC): un impuesto
    "plantilla" (``type_tax_use='sale'``, ``l10n_ar_state_id`` seteado,
    ``amount_type='percent'``) se resuelve, según la alícuota vigente del
    partner, a un impuesto concreto (el mismo, o una copia con el
    porcentaje exacto) que es 100% nativo y por lo tanto se computa y
    muestra correctamente tanto en Python como en el preview JS del
    formulario.
    """

    _inherit = "account.tax"

    ratio = fields.Float(
        default=100.0,
        string="Ratio (%)",
        help="Porcentaje de la base imponible sobre el que efectivamente "
             "se aplica este impuesto. Migrado del módulo ADHOC v17.0 "
             "'l10n_ar_account_withholding_ratio' (en v19 ADHOC lo "
             "fusionó directamente al impuesto en su módulo 'l10n_ar_tax'; "
             "acá se replica el mismo campo de forma standalone).",
    )

    @api.constrains('ratio')
    def _check_ratio(self):
        for tax in self:
            if not tax.ratio or tax.ratio <= 0.0 or tax.ratio > 100.0:
                raise ValidationError(
                    _('El ratio (%s) debe ser mayor a 0 y menor o igual a 100.') % tax.ratio
                )

    def _l10n_ar_is_padron_perception_tax(self):
        """Un impuesto es candidato a resolución de alícuota por padrón si
        es una percepción de venta (``type_tax_use='sale'``) ligada a una
        jurisdicción IIBB (``l10n_ar_state_id``) y calculada como
        porcentaje simple. No hace falta un flag booleano nuevo: reusamos
        campos ya nativos de ``l10n_ar_withholding``.
        """
        self.ensure_one()
        return bool(
            self.country_code == 'AR'
            and self.type_tax_use == 'sale'
            and self.l10n_ar_state_id
            and self.amount_type == 'percent'
        )

    def _l10n_ar_get_padron_rate(self, partner, date):
        """Resuelve la alícuota de percepción (en %, ya multiplicada por
        `ratio`) vigente para `partner` en la jurisdicción de `self`, a la
        fecha `date` (fecha del comprobante, no la de hoy).

        Orden de prioridad:

        1. Alícuota cargada manualmente en el partner
           (``l10n_ar.partner.padron.aliquot`` con ``is_manual=True``).
        2. Alícuota cacheada previamente para ese partner/jurisdicción/
           período (evita releer el padrón en cada línea).
        3. Alícuota encontrada en el padrón cargado en el sistema para esa
           jurisdicción y período (y se cachea en el partner para el punto
           2). Distingue "no encontrado" (aplica el % por defecto del
           impuesto) de "alícuota real 0%" (aplica 0%).
        4. Si no hay padrón cargado para el período: el % configurado en el
           propio impuesto (comportamiento "no inscripto" / valor por
           defecto).

        Devuelve el % final (float), ya escalado por `ratio`.
        """
        self.ensure_one()
        commercial_partner = partner.commercial_partner_id
        company = self.company_id or self.env.company

        existing = commercial_partner.sudo().l10n_ar_padron_aliquot_ids.filtered(
            lambda a: (
                a.state_id == self.l10n_ar_state_id
                and a.company_id == company
                and (not a.from_date or a.from_date <= date)
                and (not a.to_date or a.to_date >= date)
            )
        ).sorted(key=lambda a: not a.is_manual)[:1]

        if existing:
            rate = existing.alicuota_percepcion
        else:
            rate = self._l10n_ar_resolve_rate_from_padron(commercial_partner, company, date)

        return rate * (self.ratio / 100.0)

    def _l10n_ar_resolve_rate_from_padron(self, commercial_partner, company, date):
        """Busca el padrón vigente, resuelve la alícuota de `commercial_partner`
        y cachea el resultado en `l10n_ar.partner.padron.aliquot` para no
        releer el padrón en cada línea del mismo período. Devuelve el % de
        percepción (sin escalar por `ratio`, eso lo hace el llamador).

        Este método se dispara como efecto secundario de un flujo normal de
        facturación de venta (``account.move.line._get_computed_taxes()``),
        no de una acción explícita de un contador. Un usuario con el perfil
        estándar de Facturación (``account.group_account_invoice``, sin
        ``account.group_account_user``) NO tiene por qué tener acceso
        nativo de lectura a ``res.company.jurisdiction.padron`` (es
        configuración contable), pero sí necesita poder facturar y que la
        percepción se calcule sola. Por eso se usa `sudo()` para la
        búsqueda/lectura del padrón: el dominio queda pinneado a
        `company.id` (la compañía de `self`, el impuesto plantilla que se
        está resolviendo — un valor de configuración, no un dato que
        dependa del usuario ni del partner), así que el `sudo()` no permite
        leer padrones de ninguna otra compañía.
        """
        self.ensure_one()
        padron = self.env['res.company.jurisdiction.padron'].sudo().search([
            ('state_id', '=', self.l10n_ar_state_id.id),
            ('company_id', '=', company.id),
            ('l10n_ar_padron_from_date', '<=', date),
            ('l10n_ar_padron_to_date', '>=', date),
        ], limit=1)

        # Ventana de vigencia con la que se cachea el resultado: el mes
        # calendario del comprobante (igual criterio que el v17 original).
        from_date = date.replace(day=1)
        if date.month == 12:
            next_month_first = date.replace(year=date.year + 1, month=1, day=1)
        else:
            next_month_first = date.replace(month=date.month + 1, day=1)
        to_date = next_month_first - timedelta(days=1)

        if not padron:
            # Sin padrón cargado para el período: se usa el % del propio
            # impuesto (fallback "no inscripto" / valor por defecto), sin
            # crear un registro cacheado (no hay de dónde confirmar que ese
            # valor siga vigente todo el período).
            return self.amount

        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(commercial_partner)
        if is_in_padron:
            rate = aliquot_per
            ref = _('Alícuota encontrada en padrón %s') % padron.display_name
        else:
            rate = self.amount
            ref = _('Alícuota no inscripto (CUIT no encontrado en padrón %s)') % padron.display_name

        # `sudo()`: crear la fila de caché es un efecto secundario técnico
        # del cómputo de impuestos, no una acción contable explícita del
        # usuario; solo Contable/Asesor tienen `perm_create` nativo sobre
        # este modelo. Protegido con savepoint: si otra factura del mismo
        # partner/jurisdicción/período se resolvió en paralelo y ya insertó
        # la misma fila, la constraint única de
        # `l10n_ar.partner.padron.aliquot` haría fallar este INSERT; se
        # descarta el duplicado en lugar de romper la factura que se está
        # creando (la próxima resolución va a encontrar la fila ya
        # cacheada por el otro proceso).
        try:
            with self.env.cr.savepoint():
                self.env['l10n_ar.partner.padron.aliquot'].sudo().create({
                    'partner_id': commercial_partner.id,
                    'company_id': company.id,
                    'state_id': self.l10n_ar_state_id.id,
                    'from_date': from_date,
                    'to_date': to_date,
                    'alicuota_percepcion': rate,
                    'alicuota_retencion': aliquot_ret,
                    'numero_comprobante': ref,
                    'is_manual': False,
                })
        except UniqueViolation:
            _logger.info(
                "Alícuota de padrón para %s/%s/%s-%s ya cacheada por otro proceso, se descarta el duplicado.",
                commercial_partner.display_name, self.l10n_ar_state_id.name, from_date, to_date,
            )
        return rate

    def _l10n_ar_resolve_padron_tax(self, partner, date):
        """Punto de entrada: devuelve el impuesto concreto (100% nativo,
        ``amount_type='percent'``) que corresponde aplicar en la línea de
        factura de `partner` a la fecha `date`, según la alícuota vigente.

        Si `self` no es una percepción de padrón (``_l10n_ar_is_padron_perception_tax``
        es False) devuelve `self` sin cambios: este método es seguro de
        llamar sobre cualquier impuesto.
        """
        self.ensure_one()
        if not self._l10n_ar_is_padron_perception_tax():
            return self
        rate = self._l10n_ar_get_padron_rate(partner, date)
        return self._l10n_ar_get_or_create_rate_tax(rate)

    def _l10n_ar_get_or_create_rate_tax(self, rate):
        """Busca (o crea, copiando `self`) un impuesto hermano con el mismo
        grupo/jurisdicción/tipo pero con `amount` == `rate`. Evita
        multiplicar impuestos duplicados: reusa uno existente si ya fue
        creado para otro partner con la misma alícuota.

        `sudo()`: el ACL nativo de `account` (``access_account_tax_invoice``)
        solo da a Facturación (``account.group_account_invoice``) lectura
        sobre ``account.tax`` — crear o activar un impuesto está reservado a
        Contable/Asesor. Como este método se dispara como efecto colateral
        de facturar (no de una acción explícita de "gestionar impuestos"),
        se resuelve en `sudo()`. El dominio de búsqueda y el `copy()` quedan
        pinneados a `self.company_id`/`self.tax_group_id` (datos del
        impuesto plantilla que ya se estaba resolviendo, no del usuario ni
        del partner), así que como máximo se reutiliza o crea un impuesto
        en la MISMA compañía y grupo del impuesto plantilla: no hay fuga
        multi-compañía. El impuesto encontrado/creado se devuelve
        re-vinculado al entorno original (no sudo) del llamador: alcanza
        con permiso de lectura nativo (ya lo tiene Facturación) para que el
        resto del cómputo de impuestos lo use con normalidad.
        """
        self.ensure_one()
        rate = float_round(rate, precision_digits=2)
        if float_compare(self.amount, rate, precision_digits=2) == 0:
            return self

        domain = [
            ('company_id', '=', self.company_id.id),
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('l10n_ar_state_id', '=', self.l10n_ar_state_id.id),
            ('tax_group_id', '=', self.tax_group_id.id),
            ('amount', '=', rate),
        ]
        AccountTaxSudo = self.env['account.tax'].sudo()
        tax_sudo = AccountTaxSudo.with_context(active_test=False).search(domain, limit=1)
        if tax_sudo:
            if not tax_sudo.active:
                tax_sudo.active = True
            return self.browse(tax_sudo.id)

        if re.search(r'\d+([.,]\d+)?\s*%', self.name):
            name = re.sub(r'\d+([.,]\d+)?\s*%', '%s%%' % rate, self.name)
        else:
            name = "%s %s%%" % (self.name, rate)

        new_tax_sudo = self.sudo().copy(default={
            'name': name,
            'amount': rate,
            'active': True,
            'ratio': 100.0,
        })
        return self.browse(new_tax_sudo.id)
