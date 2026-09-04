# -*- coding: utf-8 -*-
{
    'name': 'Argentina - Padrón de Alícuota IIBB (ARBA / AGIP)',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': (
        'Carga de padrón de alícuota de Ingresos Brutos por jurisdicción '
        '(ARBA / AGIP) y cálculo automático de la percepción en la factura '
        'según la alícuota vigente del partner'
    ),
    'description': """
Padrón de Alícuota IIBB (ARBA / AGIP)
======================================

Extiende el módulo nativo ``l10n_ar_withholding`` para soportar:

* Carga del archivo de padrón de alícuota de Ingresos Brutos publicado por
  cada jurisdicción (ARBA, AGIP), asociado a la jurisdicción (``res.country.state``),
  compañía y período de vigencia.
* Búsqueda del CUIT del partner en el padrón cargado, normalizando el CUIT
  (con o sin guiones) de ambos lados de la comparación.
* Cálculo automático de la percepción de IIBB en la factura de venta, según
  la alícuota vigente del partner **a la fecha del comprobante** (no la
  fecha de hoy), con la siguiente prioridad:

  1. Alícuota cargada manualmente en el partner (``l10n_ar.partner.padron.aliquot``).
  2. Alícuota encontrada en el padrón cargado en el sistema para esa
     jurisdicción y período.
  3. Alícuota "por defecto" configurada en el propio impuesto (usada cuando
     el CUIT no figura en el padrón, es decir "no inscripto").

* Distinción explícita entre "CUIT no encontrado en el padrón" (se aplica la
  alícuota por defecto del impuesto) y "alícuota real 0%" encontrada en el
  padrón (se aplica 0%, sin caer en el fallback de "no inscripto").

Fuera de alcance de este módulo: retención de Ganancias por tabla de
escalas AFIP (ya cubierta por ``l10n_ar_withholding`` nativo) y consulta en
vivo a los webservices de ARBA / Rentas Córdoba.

Este módulo porta la lógica de negocio (y los fixes ya validados contra
datos reales del cliente) del módulo ADHOC v17.0 ``l10n_ar_account_withholding``
(``models/res_company_jurisdiction_padron.py``), adaptada al nuevo motor de
impuestos de Odoo 19 (ver README.rst para el detalle de las decisiones de
diseño).
""",
    'author': 'Persiscal Consulting',
    'website': 'https://www.persiscalconsulting.com',
    'license': 'AGPL-3',
    'countries': ['ar'],
    'depends': [
        'l10n_ar_withholding',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_company_jurisdiction_padron_views.xml',
        'views/res_partner_views.xml',
        'views/account_tax_views.xml',
    ],
    'installable': True,
    'application': False,
}
