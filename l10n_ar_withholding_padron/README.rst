====================================================
Argentina - Padrón de Alícuota IIBB (ARBA / AGIP)
====================================================

Extiende el módulo nativo ``l10n_ar_withholding`` (Odoo 19 Community) con:

* Carga del archivo de padrón de alícuota de IIBB por jurisdicción (ARBA,
  AGIP), compañía y período de vigencia (``res.company.jurisdiction.padron``,
  menú *Contabilidad > Configuración > Padrón de Alícuotas IIBB*).
* Alícuota manual por partner (``l10n_ar.partner.padron.aliquot``, pestaña
  *Alícuotas IIBB (Padrón)* en el formulario de contacto), con prioridad
  sobre el padrón.
* Cálculo automático de la percepción en la factura de venta, según la
  alícuota vigente del partner **a la fecha del comprobante**.

Decisiones de diseño (por qué no es una migración literal del v17)
====================================================================

El módulo v17 ``l10n_ar_account_withholding`` resolvía esto agregando
``amount_type = 'partner_tax'`` a ``account.tax`` y sobreescribiendo
``_compute_amount()``. En Odoo 19 el motor de impuestos fue rediseñado
(``account.tax._get_tax_details`` / ``_eval_tax_amount_*``): calcula en
batch, está espejado 1:1 en JavaScript para el preview del formulario de
factura, y **no recibe el partner de la línea** en ningún punto de
extensión razonable. Confirmado además contra el código real de ADHOC para
v19 (``ingadhoc/odoo-argentina`` rama ``19.0``, módulo ``l10n_ar_tax``, que
reemplaza a la cadena v17 completa): tampoco usa un ``amount_type``
dinámico.

En cambio, resolvemos la alícuota **antes** de que el motor de impuestos
calcule nada, en el hook nativo ``account.move.line._get_computed_taxes()``:
por cada impuesto "plantilla" que sea una percepción de IIBB
(``type_tax_use='sale'`` + ``l10n_ar_state_id`` seteado), se determina la
alícuota vigente del partner (manual > padrón > valor por defecto del
propio impuesto = "no inscripto") y se reemplaza por un impuesto **100%
nativo** (``amount_type='percent'``) con ese porcentaje exacto (se busca uno
existente con esa alícuota antes de crear uno nuevo, para no duplicar
impuestos). Así el cálculo, el reporting y el preview JS del formulario
funcionan igual que con cualquier otro impuesto porcentual de Odoo, sin
tocar el motor de cómputo.

No se depende de ``l10n_ar_tax`` (la continuación real de ADHOC para v19)
porque ese módulo trae consigo una dependencia a ``account_payment_pro`` y
una capa completa de posiciones fiscales / webservices en vivo que excede
el alcance pedido (padrón + percepción). Si en el futuro se necesita
también retención de IIBB por padrón en el pago, o soporte de más
jurisdicciones, considerar evaluar migrar a ese módulo en lugar de seguir
extendiendo este.

Fuera de alcance
=================

* Retención de Ganancias por tabla de escalas AFIP (ya cubierta por
  ``l10n_ar_withholding`` nativo, sin cambios de este módulo).
* Consulta en vivo a los webservices de ARBA / Rentas Córdoba.
* Retención de IIBB por padrón al momento del pago (la alícuota de
  retención se parsea y se guarda en ``l10n_ar.partner.padron.aliquot``
  para uso futuro, pero no se engancha al asistente de registro de pago en
  esta versión).

Fixes de negocio portados desde v17 (validados contra datos reales)
======================================================================

Ver docstrings en ``models/res_company_jurisdiction_padron.py``:

* Layout correcto por jurisdicción: ARBA es un único archivo con
  percepción (índice 7) y retención (índice 8) en la misma línea; AGIP son
  dos archivos separados "Per"/"Ret" con la alícuota en el índice 8.
* Normalización de CUIT (con/sin guiones) antes de comparar.
* El ZIP se lee en streaming (``zipfile.ZipFile.open()``), nunca
  ``extractall()`` a disco.
* Distinción explícita entre "CUIT no encontrado" (aplica el % por defecto
  del impuesto) y "alícuota real 0%" (aplica 0%).
