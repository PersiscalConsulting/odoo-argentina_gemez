# -*- coding: utf-8 -*-
import base64
import io
import zipfile
from datetime import date
from unittest.mock import patch

from odoo import Command
from odoo.tests import tagged

from odoo.addons.l10n_ar.tests.common import TestArCommon


def _make_padron_zip(files):
    """files: dict {filename: content_str} -> base64 str de un zip en memoria."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("latin-1"))
    return base64.b64encode(buffer.getvalue())


# CUIT de res_partner_adhoc (fixture de TestArCommon), sin guiones.
ADHOC_CUIT = "30714295698"
ADHOC_CUIT_DASHED = "30-71429569-8"

# CUIT que nunca aparece en los padrones armados en los tests.
OTHER_CUIT = "20111111112"


def _arba_line(cuit, per, ret):
    """Layout ARBA (archivo único): col3=CUIT, col7=percepción, col8=retención."""
    return "h0;h1;h2;%s;h4;h5;h6;%s;%s\n" % (cuit, per, ret)


def _agip_line(cuit, nro, aliquot):
    """Layout AGIP (archivos separados Per/Ret): col3=nro, col4=CUIT, col8=alícuota."""
    return "h0;h1;h2;%s;%s;h5;h6;h7;%s\n" % (nro, cuit, aliquot)


@tagged('post_install', '-at_install')
class TestPadronAliquot(TestArCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.state_arba = cls.env.ref('base.state_ar_b')
        cls.state_agip = cls.env.ref('base.state_ar_c')
        # La localización argentina exige un impuesto de IVA por línea al
        # confirmar el comprobante: lo sumamos a los productos de test junto
        # con la percepción, igual que en un caso de uso real.
        cls.tax_vat_21 = cls.env.ref('account.%s_ri_tax_vat_21_ventas' % cls.company_ri.id)

        cls.tax_group_iibb = cls.env['account.tax.group'].create({
            'name': 'IIBB Percepción (test)',
        })

        # Impuesto "plantilla": su `amount` es la alícuota que se aplica
        # cuando el CUIT no figura en el padrón ("no inscripto").
        cls.tax_perc_arba = cls.env['account.tax'].create({
            'name': 'Percepción IIBB ARBA',
            'type_tax_use': 'sale',
            'amount_type': 'percent',
            'amount': 3.0,
            'company_id': cls.company_ri.id,
            'tax_group_id': cls.tax_group_iibb.id,
            'l10n_ar_state_id': cls.state_arba.id,
        })
        cls.tax_perc_agip = cls.env['account.tax'].create({
            'name': 'Percepción IIBB AGIP',
            'type_tax_use': 'sale',
            'amount_type': 'percent',
            'amount': 2.0,
            'company_id': cls.company_ri.id,
            'tax_group_id': cls.tax_group_iibb.id,
            'l10n_ar_state_id': cls.state_agip.id,
        })

    def _create_padron(self, state, files, from_date, to_date):
        return self.env['res.company.jurisdiction.padron'].create({
            'company_id': self.company_ri.id,
            'state_id': state.id,
            'file_padron': _make_padron_zip(files),
            'filename': 'padron.zip',
            'l10n_ar_padron_from_date': from_date,
            'l10n_ar_padron_to_date': to_date,
        })

    def _create_invoice(self, tax, invoice_date, price_unit=1000.0, user=None):
        """No pasamos `tax_ids` explícito en la línea: así se dispara el
        cómputo por defecto (`_get_computed_taxes`, el hook que estamos
        extendiendo) a partir del impuesto de venta configurado en el
        producto, igual que en el flujo real de usuario (crear factura,
        elegir producto).

        `user`: si se pasa, la factura se crea con los permisos de ese
        usuario (no del admin de test), para poder reproducir escenarios de
        permisos como el de un facturador sin grupo Contable.
        """
        self.product_a.taxes_id = [Command.set((tax + self.tax_vat_21).ids)]
        Move = self.env['account.move'].with_user(user) if user else self.env['account.move']
        move = Move.create({
            'move_type': 'out_invoice',
            'partner_id': self.res_partner_adhoc.id,
            'invoice_date': invoice_date,
            'date': invoice_date,
            'invoice_line_ids': [Command.create({
                'product_id': self.product_a.id,
                'price_unit': price_unit,
            })],
        })
        return move

    # ---------------------------------------------------------------
    # Parseo de padrón (unitario, sin pasar por factura)
    # ---------------------------------------------------------------

    def test_arba_layout_single_file_found(self):
        """ARBA: un solo archivo, percepción en índice 7, retención en 8."""
        content = _arba_line(ADHOC_CUIT, "3,87", "1,50") + _arba_line(OTHER_CUIT, "5,00", "2,00")
        padron = self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': content},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 3.87)
        self.assertEqual(aliquot_ret, 1.50)

    def test_arba_cuit_normalization_with_dashes(self):
        """El padrón trae el CUIT con guiones; partner.vat sin guiones (o viceversa): debe matchear igual."""
        content = _arba_line(ADHOC_CUIT_DASHED, "4,25", "0,00")
        padron = self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': content},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 4.25)

    def test_agip_layout_two_files_found(self):
        """AGIP: dos archivos (Per/Ret) dentro del zip, alícuota en índice 8."""
        per_content = _agip_line(ADHOC_CUIT, "31082026", "3,50")
        ret_content = _agip_line(ADHOC_CUIT, "31082026", "1,25")
        padron = self._create_padron(
            self.state_agip,
            {'PadronRGSPer082026.TXT': per_content, 'PadronRGSRet082026.TXT': ret_content},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 3.50)
        self.assertEqual(aliquot_ret, 1.25)

    def test_agip_find_file_filters_by_period_not_first_match(self):
        """Regresión: `find_file` debe exigir que el nombre del archivo
        matchee el mes/año de vigencia del registro (``l10n_ar_padron_from_date``),
        no devolver el primer archivo "Per"/"Ret" que aparezca en el zip sin
        importar su período.

        Antes del fix, el patrón `"%s.{1}|.TXT\\Z" % type_code + date` se
        partía (por precedencia de `|`) en dos alternativas: `Per.{1}` (sin
        exigir fecha) y `.TXT\\Z<fecha>` (imposible de matchear). En la
        práctica cualquier archivo "Per"/"Ret" matcheaba y se devolvía el
        primero de `zip_file.namelist()`, sin filtrar por período. Este
        test arma un zip con un archivo de un período equivocado (más
        antiguo en la lista) y el correcto (más adelante en la lista): si el
        bug estuviera presente, ganaría el equivocado.
        """
        wrong_period_files = {
            # Período equivocado (setiembre/2025), aparece PRIMERO en el zip.
            'PadronRGSPer092025.TXT': _agip_line(ADHOC_CUIT, "30092025", "9,99"),
            'PadronRGSRet092025.TXT': _agip_line(ADHOC_CUIT, "30092025", "8,88"),
        }
        correct_period_files = {
            # Período correcto (agosto/2026), la vigencia configurada abajo.
            'PadronRGSPer082026.TXT': _agip_line(ADHOC_CUIT, "31082026", "4,75"),
            'PadronRGSRet082026.TXT': _agip_line(ADHOC_CUIT, "31082026", "1,00"),
        }
        padron = self._create_padron(
            self.state_agip,
            {**wrong_period_files, **correct_period_files},
            date(2026, 8, 1), date(2026, 8, 31),
        )

        with padron.descompress_file(padron.file_padron) as zip_file:
            self.assertEqual(padron.find_file(zip_file, "Per"), "PadronRGSPer082026.TXT")
            self.assertEqual(padron.find_file(zip_file, "Ret"), "PadronRGSRet082026.TXT")

        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 4.75)
        self.assertEqual(aliquot_ret, 1.00)

    def test_agip_per_and_ret_use_their_own_aliquot_column(self):
        """Regresión: la resolución de AGIP ("Per"/"Ret" en archivos
        separados) debe leer la columna de alícuota correspondiente a cada
        tipo de archivo (`aliquot_per_idx` para "Per", `aliquot_ret_idx`
        para "Ret"), no siempre `aliquot_ret_idx` para ambos.

        Con el layout real de AGIP (`aliquot_ret_idx == aliquot_per_idx == 8`)
        este bug no tiene efecto visible, así que se usa acá un layout
        ficticio con índices distintos para poder detectarlo: si el bug
        estuviera presente, `find_aliquot` leería la columna de retención
        también para el archivo "Per".
        """
        cuit_idx, nro_idx, per_idx, ret_idx = 4, 3, 8, 9
        fake_layout = {
            'base.state_ar_c': {
                'single_file': False,
                'cuit_idx': cuit_idx,
                'nro_idx': nro_idx,
                'aliquot_per_idx': per_idx,
                'aliquot_ret_idx': ret_idx,
            },
        }

        def _fake_line(cuit, nro, per_value, ret_value):
            values = ["h0", "h1", "h2", nro, cuit, "h5", "h6", "h7", per_value, ret_value]
            return ";".join(values) + "\n"

        per_content = _fake_line(ADHOC_CUIT, "31082026", "4,75", "99,99")
        ret_content = _fake_line(ADHOC_CUIT, "31082026", "99,99", "1,00")
        padron = self._create_padron(
            self.state_agip,
            {'PadronRGSPer082026.TXT': per_content, 'PadronRGSRet082026.TXT': ret_content},
            date(2026, 8, 1), date(2026, 8, 31),
        )

        Padron = type(padron)
        with patch.object(Padron, '_get_padron_layouts', return_value=fake_layout):
            is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)

        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 4.75, "Debe leer el índice de percepción del archivo 'Per', no el de retención.")
        self.assertEqual(aliquot_ret, 1.00, "Debe leer el índice de retención del archivo 'Ret', no siempre el mismo índice.")

    def test_cuit_not_found_is_distinguished_from_zero(self):
        """CUIT ausente del padrón: is_in_padron=False (distinto de 'alícuota real 0%')."""
        content = _arba_line(OTHER_CUIT, "5,00", "2,00")
        padron = self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': content},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertFalse(is_in_padron)

    def test_real_zero_aliquot_is_not_treated_as_not_inscripto(self):
        """CUIT presente con alícuota real 0,00: is_in_padron=True y aliquot=0.0."""
        content = _arba_line(ADHOC_CUIT, "0,00", "0,00")
        padron = self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': content},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        is_in_padron, aliquot_ret, aliquot_per = padron._get_aliquot(self.res_partner_adhoc)
        self.assertTrue(is_in_padron)
        self.assertEqual(aliquot_per, 0.0)
        self.assertEqual(aliquot_ret, 0.0)

    def test_padron_wrong_jurisdiction_layout_raises(self):
        """No se permite cargar un padrón para una jurisdicción sin parser implementado."""
        other_state = self.env.ref('base.state_ar_x')
        with self.assertRaises(Exception):
            self._create_padron(
                other_state, {'foo.TXT': 'x'}, date(2026, 8, 1), date(2026, 8, 31),
            )

    # ---------------------------------------------------------------
    # Resolución de alícuota (account.tax) y prioridades
    # ---------------------------------------------------------------

    def test_manual_partner_aliquot_has_priority(self):
        """Alta de partner con alícuota manual: tiene prioridad sobre el padrón."""
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(ADHOC_CUIT, "3,87", "1,50")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        self.env['l10n_ar.partner.padron.aliquot'].create({
            'partner_id': self.res_partner_adhoc.id,
            'company_id': self.company_ri.id,
            'state_id': self.state_arba.id,
            'from_date': date(2026, 8, 1),
            'to_date': date(2026, 8, 31),
            'alicuota_percepcion': 9.99,
            'is_manual': True,
        })
        rate = self.tax_perc_arba._l10n_ar_get_padron_rate(
            self.res_partner_adhoc, date(2026, 8, 15),
        )
        self.assertEqual(rate, 9.99)

    def test_no_padron_uses_tax_default_amount(self):
        """Sin padrón cargado para el período y sin alícuota manual: usa el % del impuesto (no inscripto)."""
        rate = self.tax_perc_arba._l10n_ar_get_padron_rate(
            self.res_partner_adhoc, date(2026, 8, 15),
        )
        self.assertEqual(rate, self.tax_perc_arba.amount)

    def test_cuit_not_found_uses_tax_default_amount(self):
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(OTHER_CUIT, "5,00", "2,00")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        rate = self.tax_perc_arba._l10n_ar_get_padron_rate(
            self.res_partner_adhoc, date(2026, 8, 15),
        )
        self.assertEqual(rate, self.tax_perc_arba.amount)

    def test_ratio_scales_resolved_rate(self):
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(ADHOC_CUIT, "10,0", "0,00")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        self.tax_perc_arba.ratio = 50.0
        rate = self.tax_perc_arba._l10n_ar_get_padron_rate(
            self.res_partner_adhoc, date(2026, 8, 15),
        )
        self.assertEqual(rate, 5.0)

    # ---------------------------------------------------------------
    # Cálculo automático de la percepción en la factura
    # ---------------------------------------------------------------

    @staticmethod
    def _perc_tax(move):
        """La percepción IIBB resuelta en la línea (distinta del IVA, que
        también está presente en `tax_ids` de la línea de test)."""
        return move.invoice_line_ids.tax_ids.filtered(lambda t: t.l10n_ar_state_id)

    def test_invoice_percepcion_computed_from_arba_padron(self):
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(ADHOC_CUIT, "3,87", "1,50")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        move = self._create_invoice(self.tax_perc_arba, '2026-08-15', price_unit=1000.0)
        applied_tax = self._perc_tax(move)
        self.assertEqual(len(applied_tax), 1)
        self.assertAlmostEqual(applied_tax.amount, 3.87)
        move.action_post()
        tax_line = move.line_ids.filtered(lambda l: l.tax_line_id == applied_tax)
        self.assertAlmostEqual(sum(tax_line.mapped('balance')), -38.7, places=2)

    def test_invoice_percepcion_computed_from_agip_padron_two_files(self):
        self._create_padron(
            self.state_agip,
            {
                'PadronRGSPer082026.TXT': _agip_line(ADHOC_CUIT, "31082026", "4,75"),
                'PadronRGSRet082026.TXT': _agip_line(ADHOC_CUIT, "31082026", "1,00"),
            },
            date(2026, 8, 1), date(2026, 8, 31),
        )
        move = self._create_invoice(self.tax_perc_agip, '2026-08-15', price_unit=1000.0)
        applied_tax = self._perc_tax(move)
        self.assertAlmostEqual(applied_tax.amount, 4.75)

    def test_invoice_percepcion_cuit_not_found_uses_default(self):
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(OTHER_CUIT, "5,00", "2,00")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        move = self._create_invoice(self.tax_perc_arba, '2026-08-15', price_unit=1000.0)
        applied_tax = self._perc_tax(move)
        self.assertEqual(applied_tax, self.tax_perc_arba)
        self.assertAlmostEqual(applied_tax.amount, 3.0)

    def test_invoice_percepcion_vigencia_by_invoice_date_not_today(self):
        """La alícuota debe resolverse según la fecha del comprobante, no la de hoy."""
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(ADHOC_CUIT, "3,87", "1,50")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        self._create_padron(
            self.state_arba, {'ARDJU009092026.TXT': _arba_line(ADHOC_CUIT, "6,00", "1,50")},
            date(2026, 9, 1), date(2026, 9, 30),
        )
        move_august = self._create_invoice(self.tax_perc_arba, '2026-08-20', price_unit=1000.0)
        self.assertAlmostEqual(self._perc_tax(move_august).amount, 3.87)

        move_september = self._create_invoice(self.tax_perc_arba, '2026-09-05', price_unit=1000.0)
        self.assertAlmostEqual(self._perc_tax(move_september).amount, 6.00)

    def test_invoice_percepcion_manual_partner_aliquot(self):
        """Alta de partner con alícuota manual: se refleja en la factura sin necesidad de padrón."""
        self.env['l10n_ar.partner.padron.aliquot'].create({
            'partner_id': self.res_partner_adhoc.id,
            'company_id': self.company_ri.id,
            'state_id': self.state_arba.id,
            'from_date': False,
            'to_date': False,
            'alicuota_percepcion': 7.5,
            'is_manual': True,
        })
        move = self._create_invoice(self.tax_perc_arba, '2026-08-15', price_unit=1000.0)
        self.assertAlmostEqual(self._perc_tax(move).amount, 7.5)

    # ---------------------------------------------------------------
    # Regresión: usuario con SOLO el grupo de Facturación (sin Contable)
    # ---------------------------------------------------------------

    def test_invoice_percepcion_computed_by_billing_only_user(self):
        """Reproduce el bug reportado por QA: un usuario con el perfil
        estándar de Facturación (`account.group_account_invoice`, SIN
        `account.group_account_user`) debe poder facturar y que la
        percepción se calcule sola, incluso en la primera factura del mes
        para un partner/jurisdicción sin caché (`l10n_ar.partner.padron.
        aliquot`) ni impuesto de esa alícuota exacta todavía creados.

        Antes del fix: `AccessError` al leer `res.company.jurisdiction.
        padron` (búsqueda sin sudo) y, de haber llegado más lejos, otro al
        crear el `account.tax` con la alícuota resuelta (solo Asesor/
        Contable puede crear impuestos nativamente).
        """
        self._create_padron(
            self.state_arba, {'ARDJU008082026.TXT': _arba_line(ADHOC_CUIT, "3,87", "1,50")},
            date(2026, 8, 1), date(2026, 8, 31),
        )
        billing_user = self.env['res.users'].create({
            'name': 'Facturador (solo Facturación)',
            'login': 'facturador_test_qa@example.com',
            'email': 'facturador_test_qa@example.com',
            'company_id': self.company_ri.id,
            'company_ids': [Command.set([self.company_ri.id])],
            'group_ids': [Command.set([
                self.env.ref('base.group_user').id,
                self.env.ref('account.group_account_invoice').id,
            ])],
        })
        # Precondición del escenario: el usuario NO tiene el grupo Contable
        # (si lo tuviera, el bug reportado no sería reproducible).
        self.assertFalse(billing_user.has_group('account.group_account_user'))

        move = self._create_invoice(
            self.tax_perc_arba, '2026-08-15', price_unit=1000.0, user=billing_user,
        )
        # No debe haber levantado AccessError, y la alícuota resuelta debe
        # ser la del padrón (no la de "no inscripto" del impuesto plantilla).
        self.assertAlmostEqual(self._perc_tax(move).amount, 3.87)

        # También debe poder confirmarla (ejercita el resto del flujo de
        # facturación con el impuesto recién resuelto/creado).
        move.with_user(billing_user).action_post()
        self.assertEqual(move.state, 'posted')
