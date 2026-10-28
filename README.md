# l10n_mx_edi_payment_tax_precision_fix

Corrige un bug nativo de Odoo 18 (`l10n_mx_edi` 18.0.0.3) que provoca el
rechazo del PAC al timbrar el **Complemento de Pago (CFDI Pagos 2.0)** en
pagos parciales (PPD) con impuesto, con el error:

```
Código: CRP20261
Mensaje: El valor del campo ImporteDR o que corresponde a Traslado no se
encuentra entre el limite inferior y superior permitido.
Información adicional: El valor del atributo
Pagos/pago10:Pago/pago10:DoctoRelacionado/ImpuestosDR:TrasladosDR/TrasladoDR/
ImporteDR:X del DoctoRelacionado con IdDocumento:Y, es menor al limite
inferior:A o es mayor al limite superior:B
```

Este error se reproduce en **prácticamente cualquier pago parcial con
impuesto** cuyo prorrateo no sea exacto a 2 decimales — no depende de montos
raros ni de configuración del cliente.

## Causa raíz

El SAT valida en el Complemento de Pago que:

```
ImporteDR = BaseDR x TasaOCuotaDR
```

con una tolerancia de apenas **±0.0000005**.

Odoo nativo calculaba `BaseDR`/`ImporteDR` en dos pasos, cada uno con su
propio problema:

1. **Cálculo** (`account_move.py`,
   `_l10n_mx_edi_add_payment_cfdi_values`): en vez de derivar `ImporteDR` de
   `BaseDR x TasaOCuotaDR`, Odoo prorrateaba el **impuesto total de la
   factura ya redondeado a 2 decimales** por el porcentaje pagado. Como ese
   impuesto total casi nunca es exactamente `Base x Tasa` (por ejemplo,
   972,675.18 x 16% = 155,628.0288, pero la factura guarda 155,628.03), el
   desfase se arrastra al prorratear y excede la tolerancia del SAT.
2. **Formato** (plantilla QWeb `l10n_mx_edi.payment20`): `BaseDR`/
   `ImporteDR` se formateaban con la precisión de la moneda (2 decimales
   para MXN/USD) en lugar de 6, a diferencia de **todos** los campos
   equivalentes del mismo estándar (`TasaOCuotaDR`, `BaseP`/`ImporteP` del
   mismo complemento, y `Base`/`Importe` de la factura normal), que sí usan
   `precision=6` de forma nativa.

Verificado con datos reales de producción: el valor que el PAC reporta como
"límite inferior" coincide **exacto** con
`BaseDR_prorrateada_sin_redondear x TasaOCuotaDR`, confirmando ambas causas.

Se evaluó bajar la precisión a 4 o 2 decimales en lugar de 6 (para
"simplificar"); **no es viable**: con menos de 6 decimales se pierde
justamente la precisión que el SAT necesita para verificar la consistencia
`ImporteDR = BaseDR x TasaOCuotaDR` dentro de su tolerancia de
±0.0000005. El valor 6 no es arbitrario — es el estándar que el propio SAT
usa en el Anexo 20 para estos campos de impuesto, y el que ya usa el resto
de la plantilla nativa de Odoo.

## Qué hace este módulo

Dos cambios, ninguno modifica archivos del core:

### 1. Plantilla QWeb (`data/payment20_tax_precision_fix.xml`)

Hereda `l10n_mx_edi.payment20` y agrega `precision=6` a `BaseDR`/`ImporteDR`
en `RetencionDR` y `TrasladoDR`, igual que ya hace Odoo con `TasaOCuotaDR`
en la misma plantilla.

### 2. Lógica de cálculo (`models/account_move.py`)

Sobreescribe por completo `_l10n_mx_edi_add_payment_cfdi_values` en
`account.move` (no hay un punto de extensión más chico posible: la lógica a
corregir está en medio de un único método nativo de ~280 líneas). El único
bloque que cambia respecto al nativo es el de "Apply the percentage paid to
the tax amounts":

- `BaseDR` se prorratea **sin redondear** a la precisión de la moneda.
- `ImporteDR` se recalcula como `BaseDR x TasaOCuotaDR` (para impuestos tipo
  `'Tasa'`, es decir, porcentuales: IVA, ISR, IEPS porcentual) en lugar de
  prorratear el importe ya redondeado de la factura.
- Para impuestos tipo `'Cuota'` (cuota fija, ej. algunos IEPS) o `'Exento'`,
  se mantiene exactamente el comportamiento nativo anterior — no se tocaron.

El resto del método (tipo de cambio, totales agregados del complemento,
información bancaria, etc.) es una copia literal del nativo, sin cambios,
para mantener compatibilidad y facilitar comparar contra futuras versiones
de `l10n_mx_edi`.

## Qué NO toca

- La factura normal (CFDI de ingreso/egreso) y su plantilla `cfdi.xml` — no
  se modifican.
- Los totales agregados del complemento (`TotalRetenciones*`,
  `MontoTotalPagos`, `TotalTraslados*`) — siguen redondeándose a 2
  decimales, como exige el estándar.
- Factura global, ventas, compras, POS, inventario — el método modificado
  solo se ejecuta al generar el Complemento de Pago.
- Ningún otro módulo (core ni custom de esta instalación) sobreescribe
  `_l10n_mx_edi_add_payment_cfdi_values` ni la plantilla `payment20` en los
  mismos nodos — no hay conflicto de herencia.

## Dependencia relacionada (no incluida en este módulo)

Este fix por sí solo no resuelve el error si la causa real era la
precisión decimal "Payment Terms" (usada por
`account.payment.term.line.value_amount`), que en Odoo viene nativa en 6
dígitos. Si tu instancia la tiene en 6 y nunca necesitaste más de 2
decimales en los porcentajes de tus plazos de pago, bájala a 2 en
**Ajustes (modo desarrollador) > Técnico > Precisión Decimal > Payment
Terms**. Se verificó contra los datos de esta instalación que ningún plazo
de pago configurado depende de más de 2 decimales, por lo que el cambio es
seguro aquí — pero es un ajuste de configuración, no parte de este módulo,
y debe aplicarse (o confirmarse) por separado en producción.

## Instalación

```bash
python -m odoo shell -c odoo.conf -d <base_de_datos> -u l10n_mx_edi_payment_tax_precision_fix --no-http
```

o vía UI: modo desarrollador > Apps > Actualizar lista de aplicaciones >
buscar "MX EDI - Precisión de 6 decimales en impuestos del Complemento de
Pago" > Instalar/Actualizar.

## Validación recomendada antes/después de producción

1. Timbrar un complemento de pago de prueba con pago parcial y confirmar
   que ya no aparece el error `CRP20261`.
2. Revisar el XML generado: `BaseDR`/`ImporteDR` deben mostrar 6 decimales
   y cumplir `ImporteDR = BaseDR x TasaOCuotaDR`.
3. Confirmar que los totales del complemento (`MontoTotalPagos`,
   `TotalTraslados*`) siguen en 2 decimales y cuadran con el monto pagado.
4. Si hay documentos previamente fallidos (`l10n_mx_edi_document.state =
   'payment_sent_failed'`) con la misma firma de error, puede ser necesario
   volver a conciliar el pago contra la factura antes de reintentar el
   timbrado — Odoo revierte la conciliación cuando el timbrado síncrono
   falla.

## Desinstalación / rollback

Desinstalar el módulo normalmente desde Apps. Al no modificar archivos del
core, Odoo vuelve a usar la plantilla y el método nativos de `l10n_mx_edi`
sin dejar residuos.
