{
    'name': 'MX EDI - Precisión de 6 decimales en impuestos del Complemento de Pago',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'Corrige BaseDR/ImporteDR del Complemento de Pago (CFDI 2.0) para que usen 6 decimales, igual que el resto de campos de impuesto del estándar SAT.',
    'description': """
Bug nativo de Odoo 18 (módulo l10n_mx_edi 18.0.0.3) en el cálculo y formato
de BaseDR/ImporteDR del Complemento de Pago (CFDI Pagos 2.0):

1. CÁLCULO (account_move.py, método _l10n_mx_edi_add_payment_cfdi_values):
   Odoo prorratea el IMPUESTO TOTAL de la factura (ya redondeado a 2
   decimales) por el porcentaje pagado, en vez de recalcular el impuesto
   como Base-prorrateada x Tasa. Como el impuesto original de la factura
   (ej. 155628.03) no es exactamente Base x Tasa (972675.18 x 0.16 =
   155628.0288), el prorrateo arrastra ese desfase y casi nunca cumple la
   tolerancia tan estrecha (+-0.0000005) que exige el SAT para validar
   ImporteDR = BaseDR x TasaOCuotaDR. Verificado con datos reales de
   producción: el valor que el PAC reporta como "limite inferior" coincide
   EXACTO con Base-prorrateada-sin-redondear x Tasa, confirmando esta causa.

2. FORMATO (plantilla QWeb payment20): BaseDR/ImporteDR se formateaban con
   la precisión de la moneda (2 decimales) en vez de 6, a diferencia de
   TODOS los campos equivalentes del mismo estándar (TasaOCuotaDR, BaseP,
   ImporteP, Base/Importe de la factura normal).

Este módulo corrige ambos puntos:
   - Hereda la plantilla QWeb nativa 'l10n_mx_edi.payment20' agregando
     precision=6 a BaseDR/ImporteDR (RetencionDR y TrasladoDR).
   - Sobreescribe _l10n_mx_edi_add_payment_cfdi_values en account.move para
     que ImporteDR se calcule como BaseDR x TasaOCuotaDR (impuestos tipo
     'Tasa'), sin redondear a 2 decimales antes del formateo final.

No se modifica ningún archivo del core.
""",
    'depends': ['l10n_mx_edi'],
    'data': [
        'data/payment20_tax_precision_fix.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}
