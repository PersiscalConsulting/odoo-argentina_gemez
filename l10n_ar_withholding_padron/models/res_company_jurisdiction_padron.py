# -*- coding: utf-8 -*-
import base64
import logging
import re
from io import BytesIO, TextIOWrapper
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ResCompanyJurisdictionPadron(models.Model):
    """Padrón de alícuota de Ingresos Brutos (ARBA / AGIP) cargado por
    jurisdicción, compañía y período de vigencia.

    Porta la lógica de parseo del módulo ADHOC v17.0
    ``l10n_ar_account_withholding`` (``res_company_jurisdiction_padron.py``),
    con los fixes de negocio ya validados contra datos reales del cliente
    (2026-09-02):

    * Layout correcto por jurisdicción (ver ``_get_padron_layouts``): ARBA es
      un único archivo con percepción y retención en la misma línea; AGIP son
      dos archivos separados ("Per"/"Ret"). Antes estos layouts estaban
      cruzados entre jurisdicciones.
    * Normalización de CUIT (con/sin guiones) antes de comparar.
    * El ZIP se lee en streaming (``zipfile.ZipFile.open()``), nunca se hace
      ``extractall()`` a disco: el padrón de AGIP puede pesar ~450MB
      descomprimido y agotaba la cuota de ``/tmp`` del contenedor.
    * Distinción explícita entre "CUIT no encontrado en el padrón" (se debe
      usar la alícuota "no inscripto") y "alícuota real 0%" encontrada en el
      padrón (se debe aplicar 0%). Ver el contrato de retorno de
      ``_get_aliquot``.
    * (2026-09-04) ``find_file``: el patrón de búsqueda por período de
      AGIP no filtraba realmente por fecha (bug de precedencia del `|` en
      regex, ver docstring de ``find_file``).
    * (2026-09-04) ``_get_aliquot`` (rama AGIP, archivos "Per"/"Ret"
      separados): cada archivo lee ahora el índice de columna que le
      corresponde (``aliquot_per_idx``/``aliquot_ret_idx``) en lugar de
      usar siempre ``aliquot_ret_idx`` para ambos.
    """

    _name = "res.company.jurisdiction.padron"
    _description = "Padrón de Alícuota IIBB por Jurisdicción"
    _order = "l10n_ar_padron_to_date desc"

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
    file_padron = fields.Binary(
        "Archivo",
        required=True,
        attachment=True,
    )
    filename = fields.Char("Nombre de archivo")
    l10n_ar_padron_from_date = fields.Date(
        "Vigente desde",
        required=True,
    )
    l10n_ar_padron_to_date = fields.Date(
        "Vigente hasta",
        required=True,
    )

    def _get_padron_layouts(self):
        """Mapa de configuración de layout por jurisdicción (xmlid del
        ``res.country.state`` -> índices de columnas del archivo de padrón,
        0-based).

        Se centraliza acá para que agregar una jurisdicción nueva sea
        agregar una entrada al mapa, no tocar la lógica de parseo. Cada
        jurisdicción define:

        * ``cuit_idx``: índice del CUIT en el archivo.
        * ``nro_idx``: índice de un identificador de comprobante propio del
          padrón (``False`` si la jurisdicción no expone uno).
        * ``aliquot_ret_idx`` / ``aliquot_per_idx``: índices de las
          alícuotas de retención y percepción.
        * ``single_file``: ``True`` si ambas alícuotas vienen en la misma
          línea de un único archivo (ARBA); ``False`` si vienen en dos
          archivos separados "Per"/"Ret" con el mismo índice de alícuota
          (AGIP).
        """
        return {
            # ARBA (Buenos Aires): un único archivo (formato
            # "ARDJUMMAAAA.rar/.TXT"), cada línea trae percepción (índice 7)
            # y retención (índice 8). Confirmado contra archivo real
            # ARDJU008082026.rar. OJO: no invertir con el layout de AGIP -
            # antes este layout estaba mal asignado a AGIP, lo que hacía que
            # el CUIT se buscara en la columna incorrecta y la alícuota
            # nunca matcheara.
            'base.state_ar_b': {
                'single_file': True,
                'cuit_idx': 3,
                'nro_idx': False,
                'aliquot_ret_idx': 8,
                'aliquot_per_idx': 7,
            },
            # AGIP (CABA): dos archivos separados, "Per" y "Ret" (formato
            # "PadronRGSMMAAAA.zip"), mismo layout en ambos, la alícuota
            # relevante está en el índice 8. Confirmado contra archivo real
            # PadronRGS082026.zip. Es la misma estructura que antes estaba
            # (mal) asignada a ARBA.
            'base.state_ar_c': {
                'single_file': False,
                'cuit_idx': 4,
                'nro_idx': 3,
                'aliquot_ret_idx': 8,
                'aliquot_per_idx': 8,
            },
        }

    def _get_layout(self):
        """Layout de parseo (ver ``_get_padron_layouts``) correspondiente a
        ``self.state_id``, o ``False`` si la jurisdicción no tiene un parser
        implementado.
        """
        self.ensure_one()
        for xml_id, layout in self._get_padron_layouts().items():
            state = self.env.ref(xml_id, raise_if_not_found=False)
            if state and state == self.state_id:
                return layout
        return False

    @api.constrains('state_id')
    def _check_state_id(self):
        for rec in self:
            if not rec._get_layout():
                raise ValidationError(
                    _('El padrón para "%s" no está implementado.') % rec.state_id.name
                )

    def name_get(self):
        res = []
        for padron in self:
            name = "%s: %s" % (padron.company_id.name, padron.state_id.name)
            res.append((padron.id, name))
        return res

    def descompress_file(self, file_padron):
        """Abre el ZIP del padrón en memoria y devuelve el ``zipfile.ZipFile``
        listo para leer sus miembros en streaming, sin extraer nada a disco.

        El padrón real de AGIP es un único TXT de varios cientos de MB
        (~458 MB sin comprimir): ``extractall()`` volcaría ese contenido
        entero a ``/tmp`` de una sola vez, lo que agota la cuota de ``/tmp``
        del contenedor. Leer cada miembro vía ``ZipFile.open()`` desacopla
        el proceso del tamaño real del archivo descomprimido.
        """
        _logger.log(25, "Opening padron zip file in memory")
        file_bytes = base64.b64decode(file_padron)
        return zipfile.ZipFile(BytesIO(file_bytes))

    @staticmethod
    def _normalize_cuit(cuit):
        """Normaliza un CUIT a solo dígitos (sin guiones ni espacios).

        Tanto el archivo de padrón como ``partner.vat`` pueden traer el CUIT
        con o sin guiones. Sin normalizar ambos lados de la comparación, una
        porción real de los partners nunca matchea y el sistema cae en "no
        inscripto" en silencio.
        """
        return re.sub(r'\D', '', cuit or '')

    def find_aliquot(self, zip_file, name, cuit, cuit_idx, nro_idx, aliquot_idx):
        """Busca la alícuota y el número de comprobante de un CUIT en un
        miembro ``name`` del ``zip_file``, con un único valor de alícuota
        por línea (jurisdicciones con archivos separados por tipo, ej.
        AGIP). Lee el miembro en streaming, sin extraerlo a disco.

        Devuelve ``(nro, aliq) = (False, False)`` si el CUIT no aparece en
        el archivo, para que el llamador distinga "no encontrado" de
        "alícuota real en 0".
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
        """Busca en un miembro ``name`` del ``zip_file`` (único archivo de
        padrón, misma línea trae ambas alícuotas) la alícuota y el "número
        de comprobante" de un CUIT. Usado por jurisdicciones de archivo
        único (ARBA). Lee el miembro en streaming, sin extraerlo a disco.

        Devuelve ``(nro, aliq_ret, aliq_per) = (False, False, False)`` si el
        CUIT no aparece en el archivo — mismo contrato que ``find_aliquot``.
        """
        cuit_normalizado = self._normalize_cuit(cuit)
        max_idx = max(cuit_idx, aliquot_ret_idx, aliquot_per_idx)
        with zip_file.open(name) as raw, TextIOWrapper(raw, encoding="latin-1") as fp:
            for line in fp:
                values = line.split(";")
                if len(values) <= max_idx:
                    continue
                if self._normalize_cuit(values[cuit_idx]) == cuit_normalizado:
                    return cuit_normalizado, values[aliquot_ret_idx].strip(), values[aliquot_per_idx].strip()
        return False, False, False

    def find_file(self, zip_file, type_code=None):
        """Busca el nombre del miembro del padrón dentro del ``zip_file``
        abierto (sin extraer nada a disco).

        Si ``type_code`` se especifica (jurisdicciones con archivos
        separados por tipo, ej. AGIP "Per"/"Ret"), busca por nombre de
        archivo y fecha de vigencia. Si es ``None`` (archivo único, ej.
        ARBA), devuelve el primer ``.TXT`` encontrado.

        FIX (2026-09-04): el patrón original era
        ``"%s.{1}|.TXT\\Z" % type_code + date``, ej. ``"Per.{1}|.TXT\\Z82026"``.
        Por precedencia del operador ``|`` en regex (menor precedencia que
        la concatenación), eso se interpreta como DOS alternativas
        independientes: ``Per.{1}`` (matchea "Per" + 1 char, sin exigir la
        fecha) y ``.TXT\\Z82026`` (imposible de matchear: nada puede venir
        después de ``\\Z``, el ancla de fin de string). En la práctica el
        filtro de período no filtraba nada: cualquier archivo "Per"/"Ret"
        del zip matcheaba sin importar su mes/año, y se quedaba con el
        PRIMERO que apareciera en ``zip_file.namelist()`` — potencialmente
        el de un período distinto al configurado en este registro. Se
        corrige exigiendo ``type_code`` y la fecha como parte de la MISMA
        alternativa, ambos antes de la extensión ``.TXT`` final.
        """
        res = False
        names = zip_file.namelist()
        if type_code:
            date = str(self.l10n_ar_padron_from_date.month) + \
                str(self.l10n_ar_padron_from_date.year)
            pattern = r"%s.*%s.*\.TXT\Z" % (re.escape(type_code), re.escape(date))
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

    def _get_aliquot(self, partner):
        """Obtiene ``(is_in_padron, alicuota_retencion, alicuota_percepcion)``
        para ``partner`` desde el padrón cargado en este registro, según el
        layout de ``self.state_id``.

        ``is_in_padron`` es ``False`` si el CUIT no figura en el padrón (el
        llamador debe aplicar la alícuota "no inscripto") y ``True`` en
        caso contrario, incluso si la alícuota encontrada es 0.0 (alícuota
        real en cero, no equivale a "no inscripto").
        """
        self.ensure_one()
        layout = self._get_layout()
        if not layout:
            # No debería ocurrir: el constraint `_check_state_id` ya valida
            # esto al guardar el registro. Se deja explícito para evitar un
            # traceback poco claro si igual se llega acá.
            raise ValidationError(
                _('El padrón para "%s" no está implementado.') % self.state_id.name
            )

        # ZIP abierto en memoria, leído en streaming miembro a miembro. Nada
        # se extrae a disco, así que el proceso no depende del tamaño real
        # del padrón (el de AGIP puede rondar los 450+ MB sin comprimir).
        with self.descompress_file(self.file_padron) as zip_file:

            if layout['single_file']:
                name = self.find_file(zip_file)
                if not name:
                    return False, 0.0, 0.0
                nro, aliquot_ret, aliquot_per = self.find_aliquot_multi(
                    zip_file, name, partner.vat,
                    layout['cuit_idx'], layout['aliquot_ret_idx'], layout['aliquot_per_idx'],
                )
                if not nro:
                    return False, 0.0, 0.0
                aliquot_ret = float((aliquot_ret or '0').replace(",", "."))
                aliquot_per = float((aliquot_per or '0').replace(",", "."))
                return True, aliquot_ret, aliquot_per

            # Jurisdicciones con archivos separados de percepción y
            # retención (AGIP): comportamiento original, con guard contra
            # archivo faltante.
            padron_types = ["Per", "Ret"]
            found = False
            aliquot_ret = 0.0
            aliquot_per = 0.0
            for padron_type in padron_types:
                name = self.find_file(zip_file, padron_type)
                if not name:
                    # Archivo de este tipo no encontrado en el zip: se omite
                    # en lugar de romper.
                    continue
                # FIX (2026-09-04): cada archivo debe leerse con el índice
                # de columna que le corresponde ("Per" -> aliquot_per_idx,
                # "Ret" -> aliquot_ret_idx). Antes se usaba siempre
                # `aliquot_ret_idx`, incluso para el archivo "Per" — sin
                # efecto visible hoy porque el layout de AGIP define ambos
                # índices iguales (8), pero incorrecto para cualquier
                # jurisdicción futura que los tenga distintos.
                aliquot_idx = layout['aliquot_per_idx'] if padron_type == "Per" else layout['aliquot_ret_idx']
                nro_found, aliquot = self.find_aliquot(
                    zip_file, name, partner.vat,
                    layout['cuit_idx'], layout['nro_idx'], aliquot_idx,
                )
                if nro_found:
                    found = True
                    value = float((aliquot or '0').replace(",", "."))
                    if padron_type == "Per":
                        aliquot_per = value
                    else:
                        aliquot_ret = value
            return found, aliquot_ret, aliquot_per
