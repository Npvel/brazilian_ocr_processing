#!/usr/bin/env python3.13
# -*- coding: utf-8 -*-
"""
build_sbs_mapping.py — Generate sbs_label_mapping.json

Joins:
  * labels_OLD.yaml          — old `primary` codes + their Portuguese `aliases`
  * SBS_fields_mapping_with_INDIAN_labels.xlsx — new SBS `Field Name` (camelCase)
                                                  + `Indian Labels` (the join key)

The "Indian Labels" column IS (mostly) the old `primary` slug, so it is the
bridge between the two files. For each SBS Field Name we attach the aliases of
the matching old primary.

Output (sbs_label_mapping.json):
  fields[<sbsFieldName>] = {category, indian_label, source_old_primary,
                            match_type, needs_review, note?, aliases:[raw]}
  alias_index[<normalized_alias>]   = <sbsFieldName>   # unambiguous only
  alias_conflicts[<normalized_alias>] = [<sbsFieldName>, ...]  # need page ctx
  unmapped_old_anchors[<OLD_PRIMARY>] = {section, aliases:[raw]}

reverse-index keys are produced with anchors_lib.normalize() — the SAME
function the runtime classifier uses — so a normalized OCR `label` looks up
its SBS field directly.
"""

import json
import sys
import openpyxl
import yaml

from anchors_lib import normalize

YAML_PATH = "labels_OLD.yaml"
XLSX_PATH = "SBS_fields_mapping_with_INDIAN_labels.xlsx"
OUT_PATH = "sbs_label_mapping.json"

# --------------------------------------------------------------------------- #
# The hand-validated join table.
#   sbs_field -> (category, indian_label, old_primary | None, match_type,
#                 needs_review, note)
# match_type:
#   exact         — Indian label == old primary slug (1:1)
#   semantic      — different slug, same accounting meaning
#   field_override— took aliases by Field Name meaning, NOT the Indian label
#   new           — no old primary exists; aliases stay empty
# --------------------------------------------------------------------------- #
JOIN = [
    # ---- Metadata (old aliases are empty for all of these) -----------------
    ("typeCode",     "Metadata", "ACCOUNT_TYPE",  "ACCOUNT_TYPE",  "exact",    False, None),
    ("closingDate",  "Metadata", "CLOSING_DATE",  "CLOSING_DATE",  "exact",    False, None),
    ("currencyCode", "Metadata", "CURRENCY",      "CURRENCY",      "exact",    False, None),
    ("amountBase",   "Metadata", "UNIT",          "UNIT",          "exact",    False, None),
    ("durationCode", "Metadata", "DURATION",      "DURATION",      "exact",    False, None),

    # ---- Balance Sheet -----------------------------------------------------
    ("intangibleAssetsAmount", "Balance Sheet", "INTANGIBLE_ASSETS", "INTANGIBLE_ASSETS", "exact", False, None),
    ("financialAssetsAmount",  "Balance Sheet", "FINANCIAL_ASSETS",  None, "new", False,
     "No old anchor for financial (non-current) assets; add aliases as needed."),
    ("fixedAssetsAmount",      "Balance Sheet", "TOTAL_FIXED_ASSETS", "FIXED_ASSETS", "semantic", False,
     "PP&E (imobilizado). Indian label says TOTAL_FIXED_ASSETS but the Field "
     "Name is line-level fixed assets, not the non-current total."),
    ("goodwillAmount",         "Balance Sheet", "GOODWILL", None, "new", False,
     "No old anchor for goodwill (agio)."),
    ("totalFixedAssetsAmount", "Balance Sheet", "TOTAL_NON_CURRENT_ASSETS", "NON_CURRENT_ASSETS", "semantic", True,
     "MISLEADING NAME: 'totalFixed' = total NON-CURRENT assets (ativo nao "
     "circulante), NOT the imobilizado total. Verify before trusting."),
    ("stockAmount",            "Balance Sheet", "INVENTORY", "INVENTORIES", "semantic", False, "estoques."),
    ("tradeDebtorsAmount",     "Balance Sheet", "TRADE_RECEIVABLES_CURRENT", "RECEIVABLES", "semantic", False,
     "clientes / contas a receber."),
    ("cashAndSecuritiesAmount","Balance Sheet", "CASH_AND_SECURITIES", "CASH_AND_SECURITIES", "exact", False,
     "Old SHORT_TERM_INVESTMENTS aliases merged in (field name covers securities)."),
    ("totalCurrentAssetsAmount","Balance Sheet","TOTAL_CURRENT_ASSETS", "CURRENT_ASSETS", "semantic", False,
     "ativo circulante (total)."),
    ("pensionFundsAndOtherStaffCommitmentsAmount", "Balance Sheet", "No direct match", None, "new", False,
     "No old anchor."),
    ("provisions",             "Balance Sheet", "SHORT_TERM_PROVISIONS / LONG_TERM_PROVISIONS", None, "new", False,
     "Balance-sheet provisions. NOTE: old PROVISION anchor is PECLD (cash-flow "
     "doubtful-account allowance) — a DIFFERENT concept — so it is NOT reused here. "
     "User decision 2026-11-05: IAS 37 liabilities only (contingencies / lawsuits, decommissioning, "
     "warranties, onerous contracts), current + non-current summed; balance liabilities only."),
    ("shareholdersFundsAmount","Balance Sheet", "SHAREHOLDERS_FUNDS", "TOTAL_EQUITY", "semantic", False,
     "patrimonio liquido."),
    # USER MAPPING 2026-11-04 (pinned by the business owner, overrides the xlsx Description column):
    #   financialLiabilitiesLongTerm / ShortTerm = the TOTAL non-current / current liabilities,
    #   longTermLiabilitiesAmount / shortTermLiabilitiesAmount = long- / short-term CREDITS AND LOANS
    #   (empréstimos e financiamentos, debêntures ...). The xlsx text reads the other way round.
    # The rows keep their alias-set order (totals, loans, totals, loans) so the matcher sees the
    # same variants in the same order as before the swap — only the field names moved.
    ("financialLiabilitiesLongTerm","Balance Sheet","LONG_TERM_BORROWINGS", "NON_CURRENT_LIABILITIES", "field_override", False,
     "passivo nao circulante (total) — user mapping 2026-11-04: financialLiabilitiesLongTerm = total non-current liabilities."),
    ("longTermLiabilitiesAmount","Balance Sheet","NON_CURRENT_LIABILITIES", "LONG_TERM_DEBT", "field_override", False,
     "emprestimos e financiamentos (nao circulante) — user mapping 2026-11-04: longTermLiabilitiesAmount = long-term credits and loans."),
    ("financialLiabilitiesShortTerm","Balance Sheet","SHORT_TERM_BORROWINGS", "CURRENT_LIABILITIES", "field_override", False,
     "passivo circulante (total) — user mapping 2026-11-04: financialLiabilitiesShortTerm = total current liabilities."),
    ("shortTermLiabilitiesAmount","Balance Sheet","TOTAL_CURRENT_LIABILITIES", "SHORT_TERM_DEBT", "field_override", False,
     "emprestimos e financiamentos (circulante) — user mapping 2026-11-04: shortTermLiabilitiesAmount = short-term credits and loans."),
    ("tradeCreditorsAmount",   "Balance Sheet", "TRADE_CREDITORS_CURRENT", "PAYABLES", "semantic", False,
     "fornecedores."),
    ("totalLiabilitiesAmount", "Balance Sheet", "TOTAL_LIABILITIES", None, "new", False,
     "No old anchor for total liabilities (old YAML splits current/non-current only)."),
    ("totalAssetsAmount",      "Balance Sheet", "TOTAL_ASSETS", "TOTAL_ASSETS", "exact", False, None),

    # ---- Profit and Loss ---------------------------------------------------
    ("turnoverAmount",         "Profit and Loss", "TURNOVER", "TURNOVER", "exact", False, None),
    ("nonTradingIncomeAmount", "Profit and Loss", "OTHER_INCOME", "OTHER_OPER_INC", "semantic", True,
     "Old anchor is OTHER OPERATING income (out rec oper); non-trading income "
     "may also cover non-operating items. Narrow alias set — verify."),
    ("rawMaterialsAndConsumablesAmount", "Profit and Loss", "COST_OF_GOODS_SOLD / COGS", "COGS", "semantic", True,
     "Indian label = COGS. Field Name (raw materials & consumables) is a COGS "
     "sub-component; reusing full COGS aliases — verify scope."),
    ("grossProfitAmount",      "Profit and Loss", "GROSS_PROFIT", "GROSS_PROFIT", "exact", False, None),
    ("totalRemunWagesAmount",  "Profit and Loss", "EMPLOYEE_BENEFITS_EXPENSES", None, "new", False,
     "No old anchor for personnel expense (despesas com pessoal)."),
    ("exploitProvisionAmount", "Profit and Loss", "No direct match", None, "new", False, "No old anchor."),
    ("operatingProfitAmount",  "Profit and Loss", "OPERATING_PROFIT", "OPERATING_PROFIT", "exact", False, None),
    ("ebitdaAmount",           "Profit and Loss", "EBITDA", None, "new", False, "No old anchor for EBITDA."),
    ("interestPaidAmount",     "Profit and Loss", "INTEREST_PAID", "FINANCIAL_EXPENSES", "semantic", True,
     "Old anchor is despesas financeiras (broader than interest paid). Verify."),
    ("interestIncomeAndSimilarAmount", "Profit and Loss", "No direct match", None, "new", False, "No old anchor."),
    ("pretaxProfitAmount",     "Profit and Loss", "PROFIT_BEFORE_TAX", "EBT", "semantic", False,
     "EBT = earnings before tax = lucro antes dos impostos."),
    ("incomeTaxExpenseRefundAmount", "Profit and Loss", "TAXES / CURRENT_TAX", "TAXES", "exact", False, None),
    ("dividendAmount",         "Profit and Loss", "DIVIDEND", "DIVIDENDS", "semantic", False, "dividendos."),
    ("posttaxProfitAmount",    "Profit and Loss", "RETAINED_PROFIT", "NET_INCOME", "field_override", True,
     "OVERRIDE: Indian label says RETAINED_PROFIT, but Field Name = post-tax "
     "profit = net income (lucro liquido do exercicio). RETAINED_PROFIT is a "
     "different (cumulative) concept. Verify the intended SBS semantics."),
    ("netGroupProfitAmount",   "Profit and Loss", "No direct match", None, "new", False, "No old anchor."),

    # ---- Cash Flow ---------------------------------------------------------
    ("depreciationAmount",     "Cash Flow", "DEPRECIATION_PL", "DEPRECIATION", "semantic", False,
     "depreciacao e amortizacao."),
    ("operatingCashFlowAmount","Cash Flow", "OPERATING_CASH_FLOW", "CFO", "semantic", False,
     "caixa liquido das atividades operacionais."),
    ("capitalExpenditureAmount","Cash Flow", "CAPITAL_EXPEDITURES", "CAPEX", "semantic", False,
     "aquisicao de imobilizado / intangivel."),
    ("netCashInvestmentAmount","Cash Flow", "No direct match", "CFI", "field_override", True,
     "OVERRIDE: Indian label = 'No direct match', but Field Name = net cash "
     "from investing = CFI (caixa liquido das atividades de investimento). Verify."),
]

# Old primaries whose aliases were merged into another SBS field (so they are
# NOT orphans): old_primary -> sbs_field that absorbed them.
MERGE_INTO = {
    "SHORT_TERM_INVESTMENTS": "cashAndSecuritiesAmount",
}

# Extra aliases attached directly to an SBS field that has no old-primary source.
# financialAssetsAmount (#7, "Financial assets — investments, securities, loans
# granted") is the NON-CURRENT home for securities/investments. These captions
# only land here when the row sits in the non-current section (see the
# securities ambiguity group below); in the current section the very same
# caption stays in cashAndSecuritiesAmount.
EXTRA_ALIASES = {
    "financialAssetsAmount": [
        # securities / marketable investments (shared, unqualified captions)
        "titulos e valores mobiliarios",
        "valores mobiliarios",
        "aplicacoes financeiras",
        "aplicacao financeira",
        "investimentos financeiros",
        "investimentos temporarios",
        "operacoes compromissadas",
        "fundos de investimento",
        "cdb rdb",
        # equity participations / long-term investments
        "investimentos",
        "participacoes societarias",
        "participacoes em controladas",
        "participacoes em coligadas",
        "investimentos em controladas",
        "investimentos em coligadas",
        # explicitly long-term forms
        "aplicacoes financeiras de longo prazo",
        "aplicacoes financeiras nao circulante",
        "titulos e valores mobiliarios de longo prazo",
        # audit 2026-11-04: Serasa «TOT INVESTIMENTOS»; ITR 1.02.01.01 «Aplicações Financeiras Avaliadas
        # a Valor Justo através do Resultado» (WEG, ZAMP) — shared with cash (securities twin group)
        "tot investimentos",
        "aplicacoes financeiras avaliadas a valor justo atraves do resultado",
    ],
    # --- lexicon gaps found auditing real reports (HIGH confidence) ---------
    # Bare operating result (no 'bruto' -> that is gross profit; the foreign-token
    # gate keeps the two apart). Complements the long descriptive forms already
    # present under operatingProfitAmount.
    "operatingProfitAmount": [
        "resultado operacional",
        "lucro operacional",
        "resultado operacional liquido",
        # full EBIT captions that ALSO name the taxes (corpus audit 2026-11-04: Raízen p22,
        # Moreno p6, Romagnole p8, Guabi p2, MBP p11). The extra tokens 'imposto' / 'renda' /
        # 'contribuicao' are foreign to the short EBIT aliases and 'resultado financeiro' is
        # foreign to the pre-tax ones, so without these the EBIT line stayed unlabelled.
        "lucro antes do resultado financeiro e do imposto sobre a renda e da contribuicao social",
        "lucro antes do resultado financeiro e do imposto de renda e da contribuicao social",
        "lucro antes do resultado financeiro e dos impostos",
        "lucro antes das receitas despesas financeiras liquidas e impostos",
        "resultado antes das receitas despesas financeiras liquidas e impostos",
        "resultado antes do efeito financeiro",
        # ALUBAR p7 (ALL-CAPS, merged by the Stage-1 «E» continuation rule)
        "resultado antes do resultado financeiro imposto de renda e contribuicao social",
        "resultado antes do resultado financeiro e do imposto de renda e da contribuicao social",
        # audit 2026-11-04 (DIMENSIONAL p2, Delfia p6)
        "lucro antes das receitas e despesas financeiras",
        "resultado antes das receitas e despesas financeiras",
        "resultado operacional antes do resultado financeiro",
        "lucro operacional antes do resultado financeiro",
    ],
    # 'Lucro operacional bruto' = gross profit (the 'bruto' token is foreign to the bare
    # 'lucro operacional' EBIT alias, so the line stayed unlabelled: MBP p11, Nissan p8).
    "grossProfitAmount": [
        "lucro operacional bruto",
    ],
    # SERASA closes its DRE with 'GIR DA ATIVIDADE (EBITDA)' (6 of 7 Serasa reports).
    "ebitdaAmount": [
        "gir da atividade ebitda",
        "ebitda",
    ],
    # 'Provisão para IR / CSLL' phrasings: the leading 'provisao' is a foreign
    # token against the bare 'imposto de renda' alias, so these need explicit
    # entries. Income-tax expense.
    "incomeTaxExpenseRefundAmount": [
        "provisao para imposto de renda",
        "provisao para contribuicao social",
        "provisao para imposto de renda e contribuicao social",
        # corpus audit 2026-11-04 (ALUBAR p7, DUTRA p2, Raízen p22, Documento1 p2)
        "impostos sobre o lucro",
        "imposto sobre a renda e contribuicao social",
        "ir e contribuicao social",
        "ir e contribuicao social correntes",
        "ir e contribuicao social diferidos",
        "imposto de renda pessoa juridica irpj",
        "contribuicao social sobre o lucro liquido cssl",
    ],
    # COGS sub-forms with 'vendidos' (sold) rather than 'prestados' (rendered).
    "rawMaterialsAndConsumablesAmount": [
        "custos de servicos vendidos",
        "custo de servicos vendidos",
        "custos de mercadorias revendidas",
        # corpus audit 2026-11-04: the plain cost-of-sales caption was missing (Moreno p6,
        # Romagnole p8, Guabi p2); 'CUSTO INDL/COML' = Serasa's custo industrial/comercial.
        "custo das vendas",
        "custos das vendas",
        "custo indl coml",
        # audit 2026-11-04 (Benoit p4: «(-) CUSTO DAS MERC. E SERVIÇOS»)
        "custo das merc e servicos",
        "custo das mercadorias e servicos",
        "custos das mercadorias e servicos",
    ],
    # Pre-tax profit, full "...e contribuição social" forms (with 'imposto sobre
    # a renda' as well as 'imposto de renda'). The existing pretax aliases stop
    # at 'imposto de renda'; these complete the phrase so an income-statement EBT
    # line is labelled pretax rather than left null. The before-tax guard
    # (_is_pretax_label) already prevents the tax field from stealing them.
    "pretaxProfitAmount": [
        "lucro antes do imposto sobre a renda e contribuicao social",
        "resultado antes do imposto sobre a renda e contribuicao social",
        "lucro antes do imposto de renda e contribuicao social",
        "resultado antes do imposto de renda e contribuicao social",
        "lucro do exercicio antes do ir e csll",          # Documento1 p2
        # audit 2026-11-04 (Entrepay p6, São Salvador p22)
        "lucro antes de irpj e csll",
        "lucro antes do irpj e da csll",
        "lucro operacional antes do imposto de renda e da contribuicao social",
        "lucro operacional antes do imposto de renda e contribuicao social",
    ],
    # --- new SBS fields previously alias-less (SEMANTIC mapping — verify) ----
    # totalRemunWagesAmount = EMPLOYEE_BENEFITS_EXPENSES (despesas com pessoal,
    # remuneracao e encargos sociais).
    "totalRemunWagesAmount": [
        "remuneracao e encargos sociais",
        "despesas com pessoal",
        "remuneracoes e encargos sociais",
        "gastos com pessoal",
        "salarios e encargos sociais",
    ],
    # Cash-flow totals / capex captions missing from the old lexicon (corpus audit 2026-11-04:
    # Nissan p11; Raízen p26, Eztec p6, Solar p6, RECONTEC p13, MBP p14, 1Q26 p26). NOT the
    # intermediate 'Caixa proveniente(s) das atividades operacionais' (before interest / taxes
    # paid — ALUBAR p10, Romagnole p11): only the 'caixa liquido ...' total is operatingCashFlow.
    "operatingCashFlowAmount": [
        "caixa liquido consumido pelas atividades operacionais",
    ],
    "capitalExpenditureAmount": [
        "aquisicao de ativos imobilizado e intangivel",
        "aquisicao de bens do imobilizado e intangivel",
        "aquisicao de imobilizado ativo biologico intangivel",
        "acrescimo do imobilizado intangivel",
        "adicao de imobilizado",
        "aquisicao de intangivel",
        # audit 2026-11-04 (Unipar 6.02.03; São Salvador p25 — bio-asset purchases count as CAPEX,
        # user decision 2026-11-04)
        "compras de imobilizado e intangivel",
        "compras de ativo imobilizado e intangivel",
        "aquisicao de matrizes de producao",
        "aquisicao de mudas de eucaliptos",
        "aquisicao de ativos biologicos",
        "aquisicao de ativo biologico",
    ],
    # depreciation: «… e Exaustão» (WEG 6.01.01.02), «… do imobilizado e intangível» (ZAMP 6.01.01.02),
    # bio-asset amortisation (São Salvador p25 — counted, user decision 2026-11-04)
    "depreciationAmount": [
        "depreciacao amortizacao e exaustao",
        "depreciacao amortizacao exaustao",
        "depreciacao e amortizacao do imobilizado e intangivel",
        "amortizacao do ativo biologico",
        "amortizacao de ativos biologicos",
        "exaustao do ativo biologico",
    ],
    # dividends incl. interest on own capital (JCP, the Brazilian dividend form) — WEG 6.03.04
    "dividendAmount": [
        "pagamento de dividendos juros s capital proprio",
        "pagamento de dividendos e juros sobre capital proprio",
        "pagamento de dividendos e juros sobre o capital proprio",
        "dividendos e juros sobre capital proprio pagos",
    ],
    # investing cash flow, «proveniente das (utilizado nas)» wording (São Salvador p25)
    "netCashInvestmentAmount": [
        "fluxo de caixa liquido proveniente das utilizado nas atividades de investimento",
        "fluxo de caixa liquido das atividades de investimento",
    ],
    # stock: the company's OWN inventory (Benoit p1 «ESTOQUES PRÓPRIO»; «DE TERCEIROS» = goods held
    # for third parties, booked against an equal liability)
    "stockAmount": [
        "estoques proprio",
        "estoques proprios",
        "estoque proprio",
    ],
    # other non-operating income (DIMENSIONAL p2)
    "nonTradingIncomeAmount": [
        "outras receitas despesas nao operacionais",
        "outras despesas receitas nao operacionais",
        "outras receitas nao operacionais",
    ],
    # CREDITS AND LOANS (user mapping 2026-11-04) — Entrepay «Empréstimos e outras obrigações», Benoit
    # «BANCOS COM EMPRESTIMOS» / «EMPRESTIMOS SÓCIOS/CLIENTES» / «MÚTUOS SÓCIOS», Serasa «TITULOS
    # EMITIDOS» (issued notes are borrowings). Unqualified: the debt twin group resolves current /
    # non-current (the captions are listed there as shared).
    "shortTermLiabilitiesAmount": [
        "emprestimos e outras obrigacoes",
        "bancos com emprestimos",
        "emprestimos socios clientes",
        "emprestimos de socios",
        "emprestimos socios",
                "mutuos socios",
        "titulos emitidos",
    ],
    # interestIncomeAndSimilarAmount: financial income (receitas financeiras).
    "interestIncomeAndSimilarAmount": [
        "receitas financeiras",
        "receita financeira",
    ],
    # provisions (USER DECISION 2026-11-05, «Provisions and reserves for liabilities and charges», Indian
    # SHORT_TERM_PROVISIONS + LONG_TERM_PROVISIONS): ONLY the IAS 37 liabilities — contingencies / lawsuits
    # (tax, civil, labour), decommissioning / environmental restoration, warranties, onerous contracts —
    # circulante + não circulante, SUMMED (an «other» field of sbs_fields). NOT provisions (no alias, keep it
    # so): «Salários, provisões e encargos sociais» and «Provisão para férias / 13º» (payroll accruals),
    # «Provisão para IR e CS» (taxes payable), «Provisão para perdas esperadas / PCLD» (asset allowances),
    # «Provisão para perdas em investimentos» (an investee's negative equity), and the captions that do not
    # say what they provide for: bare «Provisões» (Alvorada: a payroll-like current line), «Provisões
    # diversas», «Outras provisões», «Provisões a pagar». Balance liabilities only (EXTRA_SCOPE + STRICT_SCOPE:
    # the cash-flow adjustment «Provisões para riscos…» is never the balance figure).
    "provisions": [
        # contingencies / lawsuits
        "provisoes para riscos fiscais civeis e trabalhistas",
        "provisao para riscos fiscais civeis e trabalhistas",
        "provisoes para riscos tributarios civeis e trabalhistas",
        "provisao para riscos tributarios civeis e trabalhistas",
        "provisoes tributarias civeis e trabalhistas",
        "provisao para riscos civeis trabalhistas e tributarios",
        "provisoes fiscais previdenciarias trabalhistas e civeis",      # CVM 2.0x.0x.01
        "provisao para riscos",
        "provisoes para riscos",
        "provisao para riscos e processos judiciais",
        "provisao para riscos trabalhistas",
        "provisao para riscos fiscais",
        "provisao para riscos civeis",
        "provisao para contingencias",
        "provisoes para contingencias",
        "provisao para contingencias trabalhistas",
        "provisao para contingencias fiscais",
        "provisao para contingencias civeis",
        "provisao para demandas judiciais",
        "provisoes para demandas judiciais",
        "provisao para processos judiciais",
        "provisoes para processos judiciais",
        "contingencias",
        "contingencias lp",
        # decommissioning / environmental restoration
        "provisoes para passivos ambientais e de desativacao",          # CVM 2.0x.0x.02.03
        "provisao para desmobilizacao",
        "provisao para desmobilizacao de ativos",
        "provisao para recuperacao ambiental",
        # warranties
        "provisao para garantia",
        "provisao para garantias",
        "provisao de garantia",
        "provisao de garantias",
        "provisao para garantia de produtos",
        "provisao para garantias de produtos",
        # onerous contracts (IAS 37.66)
        "provisao de contrato oneroso",
        "provisao para contratos onerosos",
    ],
    # NB: totalLiabilitiesAmount is INTENTIONALLY alias-less. Per the SBS xlsx
    # (#23) it is "Total liabilities (short-term + long-term + provisions)" —
    # liabilities ONLY, excluding equity. The Portuguese balance bottom lines we
    # see in the wild ("Total do passivo e patrimônio líquido", "Passivo Total")
    # are the GRAND TOTAL (liabilities + equity == total assets), a DIFFERENT
    # concept. Text-aliasing them here mislabels the grand total as total
    # liabilities (and the true total liabilities = current + non-current is
    # usually not even printed as its own line). totalLiabilitiesAmount is
    # instead resolved ARITHMETICALLY in Stage 3 (_stage_balance_liability_totals
    # in sbs_labeler.py): it relabels a real "Total do passivo" line only when its
    # value equals total current + total non-current liabilities (financialLiabilitiesShortTerm
    # + financialLiabilitiesLongTerm, user mapping 2026-11-04), otherwise synthesizes a derived row.
}

# Exact-dictionary-ONLY aliases (matched verbatim by the order-sensitive dict,
# never by the order-insensitive fuzzy backstop). Currently empty: the only
# previous entry was "passivo total" for totalLiabilitiesAmount, removed because
# "Passivo Total" is the balance GRAND TOTAL (liabilities + equity), not total
# liabilities — see the note above. The mechanism is kept for future captions
# whose token SET collides with another field while their word ORDER disambiguates.
EXACT_ONLY_ALIASES = {}

# --------------------------------------------------------------------------- #
# Ambiguity groups — one caption, two current/non-current twins.
# When a row matches a field in an ambiguity group but its OWN label has no
# explicit current/non-current qualifier, sbs_labeler resolves which twin by:
#   1) explicit qualifier in the label  2) CODE_VALUE prefix  3) positional
#   section  4) else needs_review (never guesses).
# `shared_aliases` = the normalized captions that are genuinely ambiguous (they
# appear under BOTH twins); a row whose match is driven only by these triggers
# resolution. Captions unique to one twin (e.g. pure "caixa") are NOT ambiguous.
# --------------------------------------------------------------------------- #
AMBIGUITY_GROUPS = [
    {
        "name": "debt",
        "scope": "BALANCE_LIABILITIES_LST",
        # credits and loans (user mapping 2026-11-04)
        "current": "shortTermLiabilitiesAmount",
        "noncurrent": "longTermLiabilitiesAmount",
        # the generic caption lives only in the ST set; treat as shared so an
        # unqualified row is always resolved, never silently taken as ST.
        "shared_aliases": [
            "emprestimos e financiamentos",
            "financiamentos",
            "debentures",
            "emprestimos",
            # audit 2026-11-04 captions (credits and loans, either section)
            "emprestimos e outras obrigacoes",
            "bancos com emprestimos",
            "emprestimos socios clientes",
            "emprestimos de socios",
            "emprestimos socios",
                        "mutuos socios",
            "titulos emitidos",
        ],
    },
    {
        "name": "securities",
        "scope": "BALANCE_ASSETS_LST",
        "current": "cashAndSecuritiesAmount",
        "noncurrent": "financialAssetsAmount",
        "shared_aliases": [
            "titulos e valores mobiliarios",
            "valores mobiliarios",
            "aplicacoes financeiras",
            "aplicacao financeira",
            "investimentos financeiros",
            "investimentos temporarios",
            "operacoes compromissadas",
            "fundos de investimento",
            "cdb rdb",
            "investimentos",
            "tot investimentos",
            "aplicacoes financeiras avaliadas a valor justo atraves do resultado",
        ],
    },
]

# --------------------------------------------------------------------------- #
# CVM/B3 hierarchical code -> SBS field (ITR / DFP filings with CODE_VALUE).
# --------------------------------------------------------------------------- #
# Brazilian standardized filings number every line with a hierarchical account
# code. The top levels (1..3) are STANDARD across companies and map 1:1 to an SBS
# aggregate; deeper levels (4..5) are company-specific AND positional (the same
# code denotes different concepts on different pages — verified on the ITR
# sample), so ONLY levels 1..3 are mapped here. Used as a high-confidence,
# OCR-robust labelling source for rows that carry a code; rows whose code is not
# in this table fall back to text matching. Note: code "2" = "Passivo Total" is
# the balance GRAND TOTAL (liabilities + equity == total assets == code "1") and
# is DELIBERATELY unmapped — it is not total liabilities (see the note inside the
# map and _stage_balance_liability_totals in sbs_labeler.py).
CVM_CODE_MAP = {
    # Balance — assets
    "1":       "totalAssetsAmount",
    "1.01":    "totalCurrentAssetsAmount",
    "1.01.01": "cashAndSecuritiesAmount",
    "1.02":    "totalFixedAssetsAmount",          # total NON-current assets
    "1.02.03": "fixedAssetsAmount",               # imobilizado (PP&E)
    "1.02.04": "intangibleAssetsAmount",
    # Balance — liabilities + equity.
    # NB: code "2" ("Passivo Total") is DELIBERATELY unmapped. It is the balance
    # GRAND TOTAL (liabilities + equity == total assets == code "1"), NOT total
    # liabilities (#23 = liabilities only, excluding equity). Mapping it to
    # totalLiabilitiesAmount double-counts equity and breaks the balance identity
    # (total assets == total liabilities + shareholders' funds). totalLiabilities
    # is resolved arithmetically instead (see sbs_labeler _stage_balance_liability_totals).
    # user mapping 2026-11-04: section totals -> financialLiabilities*, loans -> short/longTerm*
    "2.01":    "financialLiabilitiesShortTerm",   # Passivo Circulante (total)
    "2.01.04": "shortTermLiabilitiesAmount",      # Empréstimos e Financiamentos (circulante)
    "2.02":    "financialLiabilitiesLongTerm",    # Passivo Não Circulante (total)
    "2.02.01": "longTermLiabilitiesAmount",       # Empréstimos e Financiamentos (não circulante)
    # provisions (user decision 2026-11-05): the CVM «Provisões» groups = the IAS 37 provisions (Provisões
    # Fiscais Previdenciárias Trabalhistas e Cíveis + Outras Provisões). WEG prints only 2.02.04 Provisões
    # (796.530) — the bare caption cannot be an alias (Alvorada's «PROVISÕES» is a payroll-like line).
    # Their children labelled by alias are not counted twice (sbs_fields hierarchy, code descendants).
    "2.01.06": "provisions",                      # Provisões (circulante)
    "2.02.04": "provisions",                      # Provisões (não circulante)
    "2.03":    "shareholdersFundsAmount",
    # Income statement
    "3.01":    "turnoverAmount",
    "3.02":    "rawMaterialsAndConsumablesAmount",
    "3.03":    "grossProfitAmount",
    "3.05":    "operatingProfitAmount",           # EBIT
    "3.07":    "pretaxProfitAmount",
    "3.08":    "incomeTaxExpenseRefundAmount",
    "3.11":    "posttaxProfitAmount",             # net income for the period
    # Cash flow
    "6.01":    "operatingCashFlowAmount",
    "6.02":    "netCashInvestmentAmount",
}

# YAML section -> the structured_output statement bucket a field's rows live in.
# Used downstream as a tie-breaker: an asset-statement "circulante" must not be
# confused with a liability-statement "circulante".
SECTION2STMT = {
    "balance_assets": "BALANCE_ASSETS_LST",
    "balance_liabilities_equity": "BALANCE_LIABILITIES_LST",
    "income_statement": "INCOME_STATEMENT_LST",
    "cash_flow": "CASH_FLOW_LST",
    "meta_labels": "META",
}

# Explicit statement scope for SBS fields that have NO old-primary source and
# are not part of an ambiguity group (so neither path above sets a scope), but
# whose home statement is unambiguous. Lets these newly-aliased fields take part
# in scope tie-breaking instead of being treated as scope-less.
EXTRA_SCOPE = {
    "ebitdaAmount": "INCOME_STATEMENT_LST",
    "totalRemunWagesAmount": "INCOME_STATEMENT_LST",
    "interestIncomeAndSimilarAmount": "INCOME_STATEMENT_LST",
    "totalLiabilitiesAmount": "BALANCE_LIABILITIES_LST",
    "provisions": "BALANCE_LIABILITIES_LST",          # user decision 2026-11-05
}

# Fields that are NEVER kept outside their allowed statements (no cross_scope recovery in the Stage-3
# scope-gate): a balance stock whose caption the cash flow repeats as a flow. «Provisões para riscos…»
# in the DFC is the period's adjustment (Cibra p11: 887 / 1.767), never the balance provision (65.986 /
# 65.099) — user decision 2026-11-05: cash-flow lines are not provisions, even when the document's
# balance line went unlabelled.
STRICT_SCOPE = {"provisions"}

# --------------------------------------------------------------------------- #
# Structural address per field (consumed by the Stage-3 pipeline in
# sbs_labeler.py). Three decision tables, kept as DATA so the labeller stays
# generic. Each was validated on the reference goldens (see the redesign memo).
# --------------------------------------------------------------------------- #
# DECISION TABLE 1 — balance home_section (used by the section-validation check:
# an asset-side field appearing in the liabilities bucket is flagged). Fields
# not listed get home_section=None (no side check). Totals that exist on both
# sides conceptually (provisions/pension) are intentionally unset.
HOME_SECTION = {
    # assets
    "cashAndSecuritiesAmount": "current",
    "tradeDebtorsAmount": "current",
    "stockAmount": "current",
    "totalCurrentAssetsAmount": "current_total",
    "fixedAssetsAmount": "noncurrent",
    "intangibleAssetsAmount": "noncurrent",
    "goodwillAmount": "noncurrent",
    "financialAssetsAmount": "noncurrent",
    "totalFixedAssetsAmount": "noncurrent_total",
    "totalAssetsAmount": "grand_total",
    # liabilities + equity
    "tradeCreditorsAmount": "current",
    "shortTermLiabilitiesAmount": "current",           # ST credits and loans (user mapping 2026-11-04)
    "financialLiabilitiesShortTerm": "current_total",  # total current liabilities
    "longTermLiabilitiesAmount": "noncurrent",         # LT credits and loans
    "financialLiabilitiesLongTerm": "noncurrent_total",  # total non-current liabilities
    "shareholdersFundsAmount": "equity",
    "totalLiabilitiesAmount": "grand_total",
}

# DECISION TABLE 2 — double-home whitelist. These concepts legitimately appear
# in TWO statements; every field NOT listed gets exactly one allowed statement
# (its home). value = the full allowed set, home first. Outside this whitelist
# the scope-gate blocks any out-of-statement match.
DOUBLE_HOME = {
    "depreciationAmount": ["CASH_FLOW_LST", "INCOME_STATEMENT_LST"],
    "dividendAmount": ["INCOME_STATEMENT_LST", "CASH_FLOW_LST"],
    "posttaxProfitAmount": ["INCOME_STATEMENT_LST", "CASH_FLOW_LST"],
    "incomeTaxExpenseRefundAmount": ["INCOME_STATEMENT_LST", "CASH_FLOW_LST"],
}

# DECISION TABLE 3 — income ordinal TIERS. The xlsx '#' column is a schema
# enumeration, not a presentation order — it arbitrarily orders true siblings
# (interestPaid vs interestIncome) that real DRE statements print in either
# order. TIERS group siblings, so within-tier order never trips the income
# order check; only a later tier appearing ABOVE an earlier one is an anomaly.
INCOME_TIER = {
    "turnoverAmount": 1,
    "rawMaterialsAndConsumablesAmount": 2,
    "grossProfitAmount": 3,
    "nonTradingIncomeAmount": 4,
    "totalRemunWagesAmount": 4,
    "operatingProfitAmount": 5,
    "ebitdaAmount": 5,
    "interestPaidAmount": 6,
    "interestIncomeAndSimilarAmount": 6,
    "pretaxProfitAmount": 7,
    "incomeTaxExpenseRefundAmount": 8,
    "posttaxProfitAmount": 9,
    "netGroupProfitAmount": 9,
    "dividendAmount": 10,
}


def load_old_primaries(path):
    """OLD_PRIMARY -> {section, key, aliases:[raw]} from labels_OLD.yaml."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = {}
    for section, entries in doc["sections"].items():
        for key, spec in entries.items():
            prim = spec["primary"]
            out[prim] = {
                "section": section,
                "key": key,
                "aliases": list(spec.get("aliases") or []),
            }
    return out


def load_indian_labels(path):
    """Sanity map: Field Name -> Indian Labels straight from the xlsx."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["SBS Fields Structure"]
    out = {}
    for r in range(2, ws.max_row + 1):
        fname = ws.cell(r, 3).value
        indian = ws.cell(r, 6).value
        if fname:
            out[str(fname).strip()] = (str(indian).strip() if indian else None)
    return out


def load_field_meta(path):
    """Field Name -> (canonical_ordinal, category) from the xlsx, category
    forward-filled across the merged Category cells. Data-driven structural
    order (turnover #25 above net income #38), not a hand-guess."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["SBS Fields Structure"]
    out = {}
    last_cat = None
    for r in range(2, ws.max_row + 1):
        cat = ws.cell(r, 1).value
        num = ws.cell(r, 2).value
        fname = ws.cell(r, 3).value
        if cat:
            last_cat = str(cat).strip()
        if fname:
            out[str(fname).strip()] = (int(num) if num is not None else None, last_cat)
    return out


def main():
    old = load_old_primaries(YAML_PATH)
    xlsx = load_indian_labels(XLSX_PATH)
    field_meta = load_field_meta(XLSX_PATH)   # field -> (canonical_ordinal, category)

    # ---- cross-check: every JOIN field exists in the xlsx, none missed -----
    join_fields = {row[0] for row in JOIN}
    missing_from_join = set(xlsx) - join_fields
    missing_from_xlsx = join_fields - set(xlsx)
    if missing_from_join or missing_from_xlsx:
        print("!! Field-Name mismatch vs xlsx:", file=sys.stderr)
        if missing_from_join:
            print("   in xlsx but not in JOIN:", sorted(missing_from_join), file=sys.stderr)
        if missing_from_xlsx:
            print("   in JOIN but not in xlsx:", sorted(missing_from_xlsx), file=sys.stderr)
        sys.exit(1)

    fields = {}
    used_old = set()
    # alias_index built as alias -> set(fields) first, split into clean/conflict after.
    idx = {}

    def add_aliases(sbs_field, raw_aliases):
        for raw in raw_aliases:
            nk = normalize(raw)
            if not nk:
                continue
            idx.setdefault(nk, {})  # normalized -> {field: raw_example}
            idx[nk].setdefault(sbs_field, raw)

    for sbs_field, category, indian, old_prim, mtype, needs_rev, note in JOIN:
        aliases = []
        if old_prim is not None:
            if old_prim not in old:
                print(f"!! JOIN references unknown old primary {old_prim!r} "
                      f"for {sbs_field}", file=sys.stderr)
                sys.exit(1)
            aliases = list(old[old_prim]["aliases"])
            used_old.add(old_prim)

        # merge-ins (e.g. SHORT_TERM_INVESTMENTS -> cashAndSecuritiesAmount)
        merged_from = [op for op, tgt in MERGE_INTO.items() if tgt == sbs_field]
        for op in merged_from:
            aliases += old[op]["aliases"]
            used_old.add(op)

        # extra aliases attached directly to a field with no old-primary source
        aliases += EXTRA_ALIASES.get(sbs_field, [])

        # de-dup aliases by normalized form, keep first raw spelling
        seen, deduped = set(), []
        for a in aliases:
            nk = normalize(a)
            if nk and nk not in seen:
                seen.add(nk)
                deduped.append(a)

        # exact-dictionary-only aliases (matched verbatim, never fuzzily — see
        # EXACT_ONLY_ALIASES). Kept separate from `aliases` so they never enter
        # the fuzzy variant list / alias_index, only the exact dict.
        exact_only = []
        for a in EXACT_ONLY_ALIASES.get(sbs_field, []):
            nk = normalize(a)
            if nk and nk not in seen:
                seen.add(nk)
                exact_only.append(a)

        # statement scope: the structured_output bucket whose rows this field
        # can match. Derived from the source old primary's YAML section, or from
        # an ambiguity group when the field has no old primary (e.g.
        # financialAssetsAmount, whose home is set by the securities group).
        scope = None
        if old_prim is not None:
            scope = SECTION2STMT.get(old[old_prim]["section"])
        else:
            for g in AMBIGUITY_GROUPS:
                if sbs_field in (g["current"], g["noncurrent"]):
                    scope = g["scope"]
                    break
            # explicit override for newly-aliased, source-less fields
            if scope is None:
                scope = EXTRA_SCOPE.get(sbs_field)

        # ---- structural address (consumed by the Stage-3 pipeline) ----------
        # canonical_ordinal: data-driven from the xlsx '#' column.
        ordn = field_meta.get(sbs_field, (None, None))[0]
        # allowed_statements (точка 1): the double-home whitelist, else the single
        # home scope. META / scope-less fields get [] (never scope-matched).
        if sbs_field in DOUBLE_HOME:
            allowed = list(DOUBLE_HOME[sbs_field])
            home = allowed[0]
        elif scope and scope != "META":
            allowed = [scope]
            home = scope
        else:
            allowed = []
            home = scope

        entry = {
            "category": category,
            "indian_label": indian,
            "source_old_primary": old_prim,
            "statement_scope": scope,
            "match_type": mtype,
            "needs_review": needs_rev,
            # structural address for the redesigned Stage-3 pipeline:
            "canonical_ordinal": ordn,
            "income_tier": INCOME_TIER.get(sbs_field),
            "home_statement": home,
            "allowed_statements": allowed,
            "home_section": HOME_SECTION.get(sbs_field),
            "allow_blocking": bool(allowed),
            "aliases": deduped,
        }
        if sbs_field in STRICT_SCOPE:
            entry["strict_scope"] = True
        if exact_only:
            entry["exact_aliases"] = exact_only
        if merged_from:
            entry["merged_old_primaries"] = merged_from
        if note:
            entry["note"] = note
        fields[sbs_field] = entry
        add_aliases(sbs_field, deduped)

    # ---- split reverse-index into unambiguous vs conflicting ---------------
    alias_index = {}
    alias_conflicts = {}
    for nk, field_map in sorted(idx.items()):
        if len(field_map) == 1:
            alias_index[nk] = next(iter(field_map))
        else:
            alias_conflicts[nk] = sorted(field_map.keys())

    # ---- orphaned old primaries (no SBS target, not merged) ----------------
    unmapped = {}
    for prim, spec in old.items():
        if prim in used_old:
            continue
        if not spec["aliases"]:
            # metadata-style anchors with no aliases & no target — skip silently
            continue
        unmapped[prim] = {"section": spec["section"], "aliases": spec["aliases"]}

    # ---- sanity: every CVM code target must be a real SBS field ------------
    for code, fld in CVM_CODE_MAP.items():
        if fld not in fields:
            print(f"!! CVM_CODE_MAP code {code!r} targets unknown field {fld!r}",
                  file=sys.stderr)
            sys.exit(1)

    # ---- ambiguity groups (normalized shared aliases for runtime use) ------
    ambiguity_groups = []
    for g in AMBIGUITY_GROUPS:
        shared_norm = sorted({normalize(a) for a in g["shared_aliases"] if normalize(a)})
        # sanity: both twins must exist as fields
        for twin in (g["current"], g["noncurrent"]):
            if twin not in fields:
                print(f"!! ambiguity group {g['name']!r} references unknown "
                      f"field {twin!r}", file=sys.stderr)
                sys.exit(1)
        ambiguity_groups.append({
            "name": g["name"],
            "scope": g["scope"],
            "current": g["current"],
            "noncurrent": g["noncurrent"],
            "shared_aliases_normalized": shared_norm,
        })

    result = {
        "schema_version": "2.0",
        "description": (
            "Maps OCR line-item `label` text to SBS Field Name (primary_sbs_label). "
            "Use alias_index for O(1) lookup on anchors_lib.normalize(label); fall "
            "back to anchor_match() for fuzzy hits. alias_conflicts require page "
            "context (asset vs liability, current vs non-current) to resolve."
        ),
        "generated_from": {"yaml": YAML_PATH, "xlsx": XLSX_PATH},
        "normalization": (
            "alias_index/alias_conflicts keys are anchors_lib.normalize() output: "
            "NFKD accent strip, lowercase, drop '(Nota..)', strip leading +/-/=, "
            "drop punctuation, collapse whitespace."
        ),
        "fields": fields,
        "code_map": CVM_CODE_MAP,
        "ambiguity_groups": ambiguity_groups,
        "alias_index": alias_index,
        "alias_conflicts": alias_conflicts,
        "unmapped_old_anchors": unmapped,
        "stats": {
            "sbs_fields_total": len(fields),
            "sbs_fields_with_aliases": sum(1 for f in fields.values() if f["aliases"]),
            "sbs_fields_new_no_aliases": sum(1 for f in fields.values() if not f["aliases"]),
            "sbs_fields_needs_review": sum(1 for f in fields.values() if f["needs_review"]),
            "alias_index_unique": len(alias_index),
            "alias_conflicts": len(alias_conflicts),
            "unmapped_old_anchors": len(unmapped),
        },
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"wrote {OUT_PATH}")
    print(json.dumps(result["stats"], indent=2))
    if alias_conflicts:
        print("\nALIAS CONFLICTS (need page context to resolve):")
        for nk, fs in alias_conflicts.items():
            print(f"  {nk!r:40} -> {fs}")
    print("\nUNMAPPED OLD ANCHORS (kept for reference):", sorted(unmapped))


if __name__ == "__main__":
    main()
