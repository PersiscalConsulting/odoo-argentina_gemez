from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from io import BytesIO, TextIOWrapper
import zipfile
import re
import logging
import base64
_logger = logging.getLogger(__name__)


class ResCompanyJurisdictionPadron(models.Model):
    _name = "res.company.jurisdiction.padron"
    _description = "res.company.jurisdiction.padron"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    jurisdiction_id = fields.Many2one(
        "account.account.tag",
        domain="[('applicability', '=', 'taxes'),('jurisdiction_code', '!=', False)]",
        required=True,
    )

    file_padron = fields.Binary(
        "File",
        required=True,
    )
    l10n_ar_padron_from_date = fields.Date(
        "From Date",
        required=True,
    )
    l10n_ar_padron_to_date = fields.Date(
        "To Date",
        required=True,
    )

    def _get_padron_layouts(self):
        """Mapa de configuración de layout por jurisdicción (xmlid del tag de
        jurisdicción -> índices de columnas del archivo de padrón, 0-based).

        Se centraliza acá para que agregar una jurisdicción nueva sea
        agregar una entrada al mapa, no tocar la lógica de parseo. Cada
        jurisdicción define:
          - cuit_idx: índice del CUIT en el archivo.
          - nro_idx: índice de un identificador de comprobante propio del
            padrón (False si la jurisdicción no expone uno).
          - aliquot_ret_idx / aliquot_per_idx: índices de las alícuotas de
            retención y percepción.
          - single_file: True si ambas alícuotas vienen en la misma línea de
            un único archivo (AGIP); False si vienen en dos archivos
            separados "Per"/"Ret" con el mismo índice de alícuota (ARBA).
        """
        return {
            # ARBA (Buenos Aires): CORREGIDO 2026-09-11 — dos archivos
            # separados, "Per" y "Ret" (formato "PadronRGSMMAAAA.zip", con
            # "PadronRGSPer.../PadronRGSRet..." adentro), mismo layout en
            # ambos, la alícuota relevante está en el índice 8. Confirmado
            # directamente por el cliente (dueño del dato) el 2026-09-11,
            # tras un commit intermedio (627dcef9, 2026-09-02) que había
            # invertido este mapeo basándose en una confirmación posterior
            # contradicha por el propio cliente el mismo día que se hizo
            # esta corrección — ver memoria de proyecto para la cronología
            # completa de idas y vueltas sobre esta identidad.
            'l10n_ar_ux.tag_tax_jurisdiccion_902': {
                'single_file': False,
                'cuit_idx': 4,
                'nro_idx': 3,
                'aliquot_ret_idx': 8,
                'aliquot_per_idx': 8,
            },
            # AGIP (CABA): CORREGIDO 2026-09-11 — un único archivo (formato
            # "ARDJUMMAAAA.rar/.TXT"), cada línea trae percepción (índice 7)
            # y retención (índice 8). Confirmado directamente por el cliente
            # el 2026-09-11 — ver nota arriba.
            'l10n_ar_ux.tag_tax_jurisdiccion_901': {
                'single_file': True,
                'cuit_idx': 3,
                # No expone un número de comprobante propio; se usa el
                # CUIT normalizado como identificador de "match".
                'nro_idx': False,
                'aliquot_ret_idx': 8,
                'aliquot_per_idx': 7,
            },
        }

    def _get_layout(self):
        """Devuelve el layout de parseo (ver `_get_padron_layouts`)
        correspondiente a `self.jurisdiction_id`, o `False` si la
        jurisdicción configurada no tiene un parser implementado.
        """
        self.ensure_one()
        for xml_id, layout in self._get_padron_layouts().items():
            tag = self.env.ref(xml_id, raise_if_not_found=False)
            if tag and tag == self.jurisdiction_id:
                return layout
        return False

    @api.constrains('jurisdiction_id')
    def check_jurisdiction_id(self):
        for rec in self:
            if not rec._get_layout():
                raise ValidationError(
                    _("El padron para (%s) no está implementado.") % rec.jurisdiction_id.name
                )

    @api.depends('company_id', 'jurisdiction_id')
    def name_get(self):
        res = []
        for padron in self:
            name = "%s: %s" % (padron.company_id.name,
                               padron.jurisdiction_id.name)
            res += [(padron.id, name)]
        return res

    def descompress_file(self, file_padron):
        """Abre el ZIP del padrón en memoria y devuelve el `zipfile.ZipFile`
        listo para leer sus miembros en streaming, sin extraer nada a disco.

        El padrón real de AGIP es esencialmente un único TXT de varios
        cientos de MB (~458 MB sin comprimir) — `extractall()` volcaba ese
        contenido entero a `/tmp` de una sola vez, lo que agota la cuota de
        `/tmp` del contenedor (`OSError: No space left on device`) aunque
        ya no leakeara entre llamadas. Leer cada miembro vía `ZipFile.open()`
        desacopla el proceso del tamaño real del archivo.
        """
        _logger.log(25, "Opening padron zip file in memory")
        try:
            file_bytes = base64.b64decode(file_padron)
        except Exception:
            file_bytes = base64.decodestring(file_padron)
        return zipfile.ZipFile(BytesIO(file_bytes))

    @staticmethod
    def _normalize_cuit(cuit):
        """Normaliza un CUIT a solo dígitos (sin guiones ni espacios).

        Tanto el archivo de padrón como `partner.vat` pueden traer el CUIT
        con o sin guiones (confirmado en `db_prod`: de 13.875 partners con
        vat, 11.924 tienen guiones y 1.951 no). Sin normalizar ambos lados
        de la comparación, una porción real de los partners nunca matchea
        y el sistema cae en "no inscripto" en silencio, sin que se note el
        error. Se aplica tanto en ARBA (bug preexistente) como en AGIP.
        """
        return re.sub(r'\D', '', cuit or '')

    def find_aliquot(self, zip_file, name, cuit, cuit_idx, nro_idx, aliquot_idx):
        """Busca la alícuota y el número de comprobante de un CUIT en un
        miembro `name` del `zip_file`, con un único valor de alícuota por
        línea (usado por jurisdicciones con archivos separados por tipo,
        ej. ARBA). Lee el miembro en streaming directo del ZIP, sin
        extraerlo a disco.

        Devuelve (nro, aliq) = (False, False) si el CUIT no aparece en el
        archivo, para que el llamador pueda distinguir "no encontrado" de
        "alícuota real en 0" y aplique la alícuota de "no inscripto" en
        lugar de fallar en silencio.
        """
        cuit_normalizado = self._normalize_cuit(cuit)
        max_idx = max(cuit_idx, nro_idx or 0, aliquot_idx)
        with zip_file.open(name) as raw, TextIOWrapper(raw, encoding="latin-1") as fp:
            for line in fp:
                values = line.split(";")
                if len(values) <= max_idx:
                    continue
                if self._normalize_cuit(values[cuit_idx]) == cuit_normalizado:
                    nro = values[nro_idx].strip() if nro_idx is not False and nro_idx is not None else cuit_normalizado
                    return nro, values[aliquot_idx].strip()
        return False, False

    def find_aliquot_multi(self, zip_file, name, cuit, cuit_idx, aliquot_ret_idx, aliquot_per_idx):
        """Busca en un miembro `name` del `zip_file` (único archivo de padrón,
        una misma línea trae ambas alícuotas, percepción y retención) la
        alícuota y el "número de comprobante" de un CUIT. Usado por
        jurisdicciones de archivo único (AGIP), cuyo padrón no separa
        percepción y retención en archivos distintos como ARBA. Lee el
        miembro en streaming directo del ZIP, sin extraerlo a disco.

        Devuelve (nro, aliq_ret, aliq_per) = (False, False, False) si el
        CUIT no aparece en el archivo — mismo contrato que `find_aliquot`,
        nunca "0.00" indistinguible de una alícuota real en cero.
        """
        cuit_normalizado = self._normalize_cuit(cuit)
        max_idx = max(cuit_idx, aliquot_ret_idx, aliquot_per_idx)
        with zip_file.open(name) as raw, TextIOWrapper(raw, encoding="latin-1") as fp:
            for line in fp:
                values = line.split(";")
                if len(values) <= max_idx:
                    continue
                if self._normalize_cuit(values[cuit_idx]) == cuit_normalizado:
                    # AGIP no expone un número de comprobante propio; se usa
                    # el CUIT normalizado como identificador de que hubo match.
                    return cuit_normalizado, values[aliquot_ret_idx].strip(), values[aliquot_per_idx].strip()
        return False, False, False

    def find_file(self, zip_file, type_code=None):
        """Busca el nombre del miembro del padrón dentro del `zip_file`
        abierto (sin extraer nada a disco).

        Si `type_code` se especifica (jurisdicciones con archivos separados
        por tipo, ej. ARBA "Per"/"Ret"), mantiene el comportamiento original
        basado en el nombre del archivo y la fecha de vigencia.

        Si `type_code` es None (jurisdicciones de archivo único, ej. AGIP),
        devuelve el primer archivo .TXT encontrado: el naming interno del
        TXT de AGIP dentro del zip no está estandarizado/confirmado con
        Gemez (solo se confirmó el naming del .zip externo), así que
        buscar por contenido de extensión es más robusto que asumir un
        patrón de nombre que podría no matchear.
        """
        res = False
        names = zip_file.namelist()
        if type_code:
            date = str(self.l10n_ar_padron_from_date.month) + \
                str(self.l10n_ar_padron_from_date.year)
            pattern = "%s.{1}|.TXT\Z" % type_code + date
            for f in names:
                if re.search(pattern, f):
                    res = f
                    break
        else:
            for f in names:
                if f.upper().endswith(".TXT"):
                    res = f
                    break
        return res

    def _get_aliquit(self, partner):
        """Obtiene (numero_comprobante, alicuota_retencion,
        alicuota_percepcion) para `partner` desde el padrón cargado en este
        registro, según el layout de `self.jurisdiction_id`
        (`_get_padron_layouts`). Rama por `single_file` para soportar tanto
        el layout de archivo único (AGIP) como el de archivos separados
        Per/Ret (ARBA).
        """
        self.ensure_one()
        layout = self._get_layout()
        if not layout:
            # No debería ocurrir: el constraint `check_jurisdiction_id` ya
            # valida esto al guardar el registro. Se deja explícito para
            # evitar un traceback poco claro si igual se llega acá.
            raise ValidationError(
                _("El padron para (%s) no está implementado.") % self.jurisdiction_id.name
            )

        # ZIP abierto en memoria, leído en streaming miembro a miembro — ver
        # docstring de descompress_file. Nada se extrae a disco, así que el
        # proceso no depende del tamaño real del padrón (el de AGIP puede
        # rondar los 450+ MB sin comprimir).
        with self.descompress_file(self.file_padron) as zip_file:

            if layout['single_file']:
                name = self.find_file(zip_file)
                if not name:
                    return False, 0.0, 0.0
                nro, aliquot_ret, aliquot_per = self.find_aliquot_multi(
                    zip_file, name, partner.vat,
                    layout['cuit_idx'], layout['aliquot_ret_idx'], layout['aliquot_per_idx'],
                )
                aliquot_ret = aliquot_ret and aliquot_ret.replace(",", ".")
                aliquot_per = aliquot_per and aliquot_per.replace(",", ".")
                return nro, aliquot_ret, aliquot_per

            # Jurisdicciones con archivos separados de percepción y retención
            # (ARBA): comportamiento original, con guard contra archivo faltante.
            padron_types = ["Per", "Ret"]
            nro = False
            aliquot_ret = 0.0
            aliquot_per = 0.0
            for padron_type in padron_types:
                name = self.find_file(zip_file, padron_type)
                if not name:
                    # Archivo de este tipo no encontrado en el zip: se omite en
                    # lugar de romper con TypeError concatenando el path con False.
                    continue
                nro_found, aliquot = self.find_aliquot(
                    zip_file, name, partner.vat,
                    layout['cuit_idx'], layout['nro_idx'], layout['aliquot_ret_idx'],
                )
                nro = nro_found
                if padron_type == "Per":
                    aliquot_per = aliquot and aliquot.replace(",", ".")
                else:
                    aliquot_ret = aliquot and aliquot.replace(",", ".")
            return nro, aliquot_ret, aliquot_per
