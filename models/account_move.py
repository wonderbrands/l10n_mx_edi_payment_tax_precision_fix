"""
Fix for a native Odoo 18 (l10n_mx_edi 18.0.0.3) bug in the Payment Complement
(CFDI Pagos 2.0) generation.

Root cause (verified against real production data):
    Odoo computes the proportional tax amount of a DoctoRelacionado
    (ImporteDR / BaseDR) by:
        1. prorating the INVOICE's own already-2-decimal-rounded tax amount
           by the percentage paid, then
        2. rounding the result (base and importe) to the currency precision
           (2 decimals for MXN/USD) via `currency.round(...)`.

    The SAT validates ImporteDR = BaseDR x TasaOCuotaDR with a tolerance of
    only +-0.0000005. Because the invoice's original tax amount is itself a
    2-decimal rounded value (e.g. 155628.03) that doesn't exactly equal
    base x rate (e.g. 972675.18 x 0.16 = 155628.0288), prorating it and then
    rounding both base/importe to 2 decimals independently breaks that
    consistency almost every time there's a partial payment, causing the PAC
    rejection:

        "El valor del atributo .../ImporteDR:X del DoctoRelacionado con
        IdDocumento:Y, es menor al limite inferior:A o es mayor al limite
        superior:B"

    Confirmed with real data from a failed document: the SAT's expected
    value (the reported "limite inferior") matches EXACTLY
    (base_prorated_unrounded x tasa_o_cuota), not the value Odoo computed
    from prorating the invoice's rounded tax total.

Fix:
    - BaseDR is prorated WITHOUT rounding to currency precision.
    - ImporteDR is recomputed as BaseDR x TasaOCuotaDR (when tipo_factor is
      'Tasa', i.e. a percentage-based tax) instead of being prorated from
      the invoice's rounded tax amount. This guarantees mathematical
      consistency with the SAT's own validation formula, by construction.
    - The 2-decimal `currency.round()` + `_get_post_fix_tax_amounts_map`
      step is skipped here; final formatting to 6 decimals is handled by
      the companion QWeb fix in this same module
      (data/payment20_tax_precision_fix.xml).

This is a full override (not a light extension) of
`_l10n_mx_edi_add_payment_cfdi_values` because the buggy block sits in the
middle of a single large native method with no smaller extension point.
Only the "Apply the percentage paid to the tax amounts" block differs from
Odoo 18.0.0.3's odoo/addons/l10n_mx_edi/models/account_move.py. Re-check
this override against the native method on every l10n_mx_edi upgrade.
"""
from collections import defaultdict
from datetime import datetime
import re

from odoo import models, fields, Command, _
from odoo.addons.l10n_mx_edi.models.l10n_mx_edi_document import CFDI_DATE_FORMAT
from odoo.tools import frozendict
from odoo.tools.float_utils import float_round


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _l10n_mx_edi_add_payment_cfdi_values(self, cfdi_values, pay_results):
        # OVERRIDES 'l10n_mx_edi' (see module docstring for why this is a full
        # override and what changed).
        self.ensure_one()
        Document = self.env['l10n_mx_edi.document']

        self._l10n_mx_edi_add_common_cfdi_values(cfdi_values)
        company = cfdi_values['company']
        company_curr = company.currency_id

        # Misc.
        cfdi_values['exportacion'] = '01'
        cfdi_values['forma_de_pago'] = (self.l10n_mx_edi_payment_method_id.code or '').replace('NA', '99')
        cfdi_values['moneda'] = self.currency_id.name
        cfdi_values['num_operacion'] = self.ref

        # Amounts.
        total_in_payment_curr = sum(x['payment_amount_currency'] for x in pay_results['invoice_results'])
        total_in_company_curr = sum(x['balance'] + x['payment_exchange_balance'] for x in pay_results['invoice_results'])
        if self.currency_id == company_curr:
            cfdi_values['monto'] = total_in_company_curr
        else:
            cfdi_values['monto'] = total_in_payment_curr

        # Exchange rate.
        cfdi_values['tipo_cambio_dp'] = 6
        if self.currency_id == company_curr:
            payment_rate = None
        else:
            raw_payment_rate = abs(total_in_company_curr / total_in_payment_curr) if total_in_payment_curr else 0.0
            payment_rate = float_round(raw_payment_rate, precision_digits=cfdi_values['tipo_cambio_dp'])
            total_in_company_curr = company_curr.round(total_in_payment_curr * payment_rate)

        cfdi_values.update({
            'tipo_cambio': payment_rate,
            'monto_total_pagos': total_in_company_curr,
            'mxn_digits': company_curr.decimal_places,
        })

        # === Create the list of invoice data ===
        invoice_values_list = []
        for invoice_values in pay_results['invoice_results']:
            invoice = invoice_values['invoice']

            inv_cfdi_values = Document._get_company_cfdi_values(invoice.company_id)
            Document._add_certificate_cfdi_values(inv_cfdi_values)
            invoice._l10n_mx_edi_add_invoice_cfdi_values(inv_cfdi_values)

            # Apply the percentage paid to the tax amounts.
            if invoice.amount_total:
                percentage_paid = abs(invoice_values['reconciled_amount'] / invoice.amount_total)
            else:
                percentage_paid = 0.0

            # --- FIX (l10n_mx_edi_payment_tax_precision_fix) ---
            # BaseDR is prorated without rounding to currency precision.
            # ImporteDR is recomputed as BaseDR x TasaOCuotaDR (for
            # percentage-based taxes) instead of being prorated from the
            # invoice's already-rounded tax total, to stay mathematically
            # consistent with the SAT's extremely tight validation
            # tolerance. See module docstring for the full explanation.
            for key in (
                'retenciones_list',
                'traslados_list',
                'local_traslados_list',
                'local_retenciones_list',
            ):
                for tax_values in inv_cfdi_values[key]:
                    if tax_values['base'] is not None:
                        tax_values['base'] = tax_values['base'] * percentage_paid
                    if tax_values['importe'] is not None:
                        if tax_values['tipo_factor'] == 'Tasa' and tax_values.get('tasa_o_cuota') and tax_values['base'] is not None:
                            tax_values['importe'] = tax_values['base'] * tax_values['tasa_o_cuota']
                        else:
                            tax_values['importe'] = tax_values['importe'] * percentage_paid
            # --- END FIX ---

            def calculate_rate(invoice_amount, payment_amount):
                if not payment_amount:
                    return 0.0
                return float_round(abs(invoice_amount / payment_amount), 10)

            if invoice.currency_id == self.currency_id:
                computed_rate = None
            elif invoice.currency_id == company_curr != self.currency_id:
                balance = invoice_values['balance'] + invoice_values['invoice_exchange_balance']
                computed_rate = calculate_rate(balance, invoice_values['payment_amount_currency'])
            elif self.currency_id == company_curr != invoice.currency_id:
                balance = invoice_values['balance'] + invoice_values['payment_exchange_balance']
                computed_rate = calculate_rate(invoice_values['invoice_amount_currency'], balance)
            else:
                computed_rate = calculate_rate(invoice_values['invoice_amount_currency'], invoice_values['payment_amount_currency'])

            invoice_values_list.append({
                **inv_cfdi_values,
                'id_documento': invoice.l10n_mx_edi_cfdi_uuid,
                'equivalencia': computed_rate,
                'num_parcialidad': invoice_values['number_of_payments'],
                'imp_pagado': invoice_values['reconciled_amount'],
                'imp_saldo_ant': invoice_values['amount_residual_before'],
                'imp_saldo_insoluto': invoice_values['amount_residual_after'],
            })
        cfdi_values['docto_relationado_list'] = invoice_values_list

        # Customer.
        rfcs = set(x['receptor']['rfc'] for x in invoice_values_list)
        if len(rfcs) > 1:
            cfdi_values['errors'] = [_("You can't register a payment for invoices having different RFCs.")]
            return

        customer_values = invoice_values_list[0]['receptor']
        customer = customer_values['customer']
        cfdi_values['receptor'] = customer_values
        cfdi_values['lugar_expedicion'] = cfdi_values['issued_address'].zip

        # Date.
        cfdi_date = datetime.combine(fields.Datetime.from_string(self.date), datetime.strptime('12:00:00', '%H:%M:%S').time())
        cfdi_values['fecha'] = Document._get_datetime_now_with_mx_timezone(cfdi_values, journal=self.journal_id).strftime(CFDI_DATE_FORMAT)
        cfdi_values['fecha_pago'] = cfdi_date.strftime(CFDI_DATE_FORMAT)

        # Bank information.
        payment_method_code = self.l10n_mx_edi_payment_method_id.code
        is_payment_code_emitter_ok = payment_method_code in ('02', '03', '04', '05', '06', '28', '29', '99')
        is_payment_code_receiver_ok = payment_method_code in ('02', '03', '04', '05', '28', '29', '99')
        is_payment_code_bank_ok = payment_method_code in ('02', '03', '04', '28', '29', '99')

        bank_account = customer.bank_ids.filtered(lambda x: x.company_id.id in (False, company.id))[:1]

        partner_bank = bank_account.bank_id
        if partner_bank.country and partner_bank.country.code != 'MX':
            partner_bank_vat = 'XEXX010101000'
        else:  # if no partner_bank (e.g. cash payment), partner_bank_vat is not set.
            partner_bank_vat = partner_bank.l10n_mx_edi_vat

        payment_account_ord = re.sub(r'\s+', '', bank_account.acc_number or '') or None
        payment_account_receiver = re.sub(r'\s+', '', self.journal_id.bank_account_id.acc_number or '') or None

        cfdi_values.update({
            'rfc_emisor_cta_ord': is_payment_code_emitter_ok and partner_bank_vat,
            'nom_banco_ord_ext': is_payment_code_bank_ok and partner_bank.name,
            'cta_ordenante': is_payment_code_emitter_ok and payment_account_ord,
            'rfc_emisor_cta_ben': is_payment_code_receiver_ok and self.journal_id.bank_account_id.bank_id.l10n_mx_edi_vat,
            'cta_beneficiario': is_payment_code_receiver_ok and payment_account_receiver,
        })

        # Taxes.
        def update_tax_amount(key, amount):
            if key not in cfdi_values:
                cfdi_values[key] = 0.0
            cfdi_values[key] += amount

        def check_transferred_tax_values(tax_values, tag, tax_class, amount):
            return (
                tax_values['impuesto'] == tag
                and tax_values['tipo_factor'] == tax_class
                and company_curr.compare_amounts(tax_values['tasa_o_cuota'] or 0.0, amount) == 0
            )

        withholding_values_map = defaultdict(lambda: {'importe': 0.0})
        transferred_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        local_retenciones_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        local_traslados_values_map = defaultdict(lambda: {'base': 0.0, 'importe': 0.0})
        pay_rate = cfdi_values['tipo_cambio'] or 1.0
        for cfdi_inv_values in invoice_values_list:
            inv_rate = cfdi_inv_values['equivalencia'] or 1.0
            to_mxn_rate = pay_rate / inv_rate
            for result_dict, key in (
                (withholding_values_map, 'retenciones_list'),
                (local_retenciones_values_map, 'local_retenciones_list'),
            ):
                for tax_values in cfdi_inv_values[key]:
                    tax_key = frozendict({
                        'impuesto': tax_values['impuesto'],
                        'tipo_factor': tax_values['tipo_factor'],
                        'tasa_o_cuota': tax_values['tasa_o_cuota'],
                        'local_tax_name': tax_values.get('local_tax_name'),
                    })
                    result_dict[tax_key]['importe'] += tax_values['importe'] / inv_rate

                    tax_amount_mxn = tax_values['importe'] * to_mxn_rate
                    if tax_values['impuesto'] == '001':
                        update_tax_amount('total_retenciones_isr', tax_amount_mxn)
                    elif tax_values['impuesto'] == '002':
                        update_tax_amount('total_retenciones_iva', tax_amount_mxn)
                    elif tax_values['impuesto'] == '003':
                        update_tax_amount('total_retenciones_ieps', tax_amount_mxn)

            for result_dict, key in (
                (transferred_values_map, 'traslados_list'),
                (local_traslados_values_map, 'local_traslados_list'),
            ):
                for tax_values in cfdi_inv_values[key]:
                    tax_key = frozendict({
                        'impuesto': tax_values['impuesto'],
                        'tipo_factor': tax_values['tipo_factor'],
                        'tasa_o_cuota': tax_values['tasa_o_cuota'],
                    })
                    tax_amount = tax_values['importe'] or 0.0
                    result_dict[tax_key]['base'] += tax_values['base'] / inv_rate
                    result_dict[tax_key]['importe'] += tax_amount / inv_rate

                    base_amount_mxn = tax_values['base'] * to_mxn_rate
                    tax_amount_mxn = tax_amount * to_mxn_rate
                    if check_transferred_tax_values(tax_values, '002', 'Tasa', 0.0):
                        update_tax_amount('total_traslados_base_iva0', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva0', tax_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Exento', 0.0):
                        update_tax_amount('total_traslados_base_iva_exento', base_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Tasa', 0.08):
                        update_tax_amount('total_traslados_base_iva8', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva8', tax_amount_mxn)
                    elif check_transferred_tax_values(tax_values, '002', 'Tasa', 0.16):
                        update_tax_amount('total_traslados_base_iva16', base_amount_mxn)
                        update_tax_amount('total_traslados_impuesto_iva16', tax_amount_mxn)

        # Rounding global tax amounts.
        for dictionary in (
            withholding_values_map,
            transferred_values_map,
            local_retenciones_values_map,
            local_traslados_values_map,
        ):
            for values in dictionary.values():
                if 'base' in values:
                    values['base'] = float_round(values['base'], 6)
                values['importe'] = float_round(values['importe'], 6)

        for key in (
            'total_traslados_base_iva0',
            'total_traslados_impuesto_iva0',
            'total_traslados_base_iva_exento',
            'total_traslados_base_iva8',
            'total_traslados_impuesto_iva8',
            'total_traslados_base_iva16',
            'total_traslados_impuesto_iva16',
            'total_retenciones_isr',
            'total_retenciones_iva',
            'total_retenciones_ieps',
        ):
            if key in cfdi_values:
                cfdi_values[key] = company_curr.round(cfdi_values[key])
            else:
                cfdi_values[key] = None

        for target_key, source_dict in (
            ('retenciones_list', withholding_values_map),
            ('traslados_list', transferred_values_map),
            ('local_retenciones_list', local_retenciones_values_map),
            ('local_traslados_list', local_traslados_values_map),
        ):
            cfdi_values[target_key] = [
                {**k, **v}
                for k, v in source_dict.items()
            ]

        # Cleanup attributes for Exento taxes.
        for key in (
            'traslados_list',
            'local_traslados_list',
        ):
            for tax_values in cfdi_values[key]:
                if tax_values['tipo_factor'] == 'Exento':
                    tax_values['importe'] = None
