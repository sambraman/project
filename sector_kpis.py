"""
sector_kpis.py — the metrics that actually matter for a given vertical.

Generic fundamentals (margin, ROE, D/E) are sector-blind and therefore weakly
informative: a bank's "gross margin" is meaningless, an insurer's revenue growth
tells you nothing about underwriting discipline. This module extracts the KPIs
an analyst covering that vertical would actually pull, straight from XBRL facts.

    from sector_kpis import compute_sector_kpis
    res = compute_sector_kpis("MSFT")               # auto-detects hyperscaler
    res = compute_sector_kpis("JPM", sector="banks")

Two kinds of KPI:
  TAG     read directly from an XBRL concept (with fallbacks — filers disagree
          on tags and change them over time).
  DERIVED computed from other KPIs by a formula (NIM, efficiency ratio,
          combined ratio...). Almost every *good* sector KPI is derived; that's
          precisely why they aren't in companyfacts already.

HONEST LIMITS — read before trusting output:
  * Segment revenue (Azure, AWS, Google Cloud) is tagged with a *dimensional*
    axis. SEC's companyfacts API flattens facts and drops dimensions, so clean
    segment revenue is NOT reliably retrievable here. See cloud_revenue below
    for what we do instead and why it's best-effort.
  * Regulatory capital (CET1) and insurance ratios are inconsistently tagged;
    many filers put them only in narrative text or exhibits.
  * Every value carries a `basis` and `tag` so you can audit where it came
    from. Treat a KPI with basis="derived" as an estimate, not a filed figure.

    python sector_kpis.py            # offline self-test
    python sector_kpis.py MSFT       # live, if network is available
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict

from fundamentals.edgar_client import company_facts, company_submissions, ticker_to_cik
from fundamentals import quarterly as q


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class KPI:
    key: str
    label: str
    value: float | None
    unit: str                 # USD | ratio | percent | shares
    period: str = ""          # e.g. 2026Q1
    basis: str = ""           # tag | derived | ttm
    tag: str = ""             # the XBRL concept actually used
    note: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass
class SectorKPIResult:
    ticker: str
    sector: str
    period: str = ""
    kpis: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return {"ticker": self.ticker, "sector": self.sector,
                "period": self.period,
                "kpis": [k.as_dict() for k in self.kpis],
                "warnings": self.warnings,
                "coverage": self.coverage}

    @property
    def coverage(self) -> float:
        if not self.kpis:
            return 0.0
        got = sum(1 for k in self.kpis if k.value is not None)
        return round(100.0 * got / len(self.kpis), 1)

    def get(self, key: str):
        for k in self.kpis:
            if k.key == key:
                return k.value
        return None


# --------------------------------------------------------------------------- #
# Tag specs.  (key, label, [candidate concepts], kind, unit, note)
# kind: "flow" (duration) | "instant" (point in time)
# --------------------------------------------------------------------------- #
HYPERSCALER_TAGS = [
    ("capex", "Capital Expenditures",
     ["PaymentsToAcquirePropertyPlantAndEquipment",
      "PaymentsToAcquireProductiveAssets",              # AMZN's tag
      "PaymentsToAcquireOtherPropertyPlantAndEquipment"],
     "flow", "USD", "Purchases of PP&E from the cash flow statement."),
    ("finance_lease_additions", "Finance Lease Additions",
     ["RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability",
      "FinanceLeaseRightOfUseAssetObtainedInExchangeForFinanceLeaseLiability"],
     "flow", "USD",
     "Datacenter capacity is increasingly taken on via finance leases, which "
     "bypass the capex line. Headline capex UNDERSTATES true infrastructure "
     "spend without this."),
    ("rpo", "Remaining Performance Obligations",
     ["RevenueRemainingPerformanceObligation"],
     "instant", "USD",
     "Contracted-but-unrecognized revenue — the cleanest forward demand signal "
     "for cloud. Reliably tagged."),
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("operating_cash_flow", "Operating Cash Flow",
     ["NetCashProvidedByUsedInOperatingActivities",
      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
     "flow", "USD", ""),
    ("depreciation", "Depreciation & Amortization",
     ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
      "Depreciation"],
     "flow", "USD",
     "Watch alongside capex: extending server useful lives flatters EPS while "
     "cash spend keeps rising."),
]

BANK_TAGS = [
    # Reduced from the full 12 to the six that actually drive the thesis.
    ("net_interest_income", "Net Interest Income",
     ["InterestIncomeExpenseNet",
      "InterestIncomeExpenseAfterProvisionForLoanLoss"],
     "flow", "USD", ""),
    ("earning_assets", "Total Assets (NIM proxy denominator)",
     ["Assets"], "instant", "USD",
     "True NIM uses average EARNING assets; total assets is a documented "
     "approximation that biases NIM slightly low."),
    ("noninterest_expense", "Noninterest Expense",
     ["NoninterestExpense", "OperatingExpenses"], "flow", "USD", ""),
    ("noninterest_income", "Noninterest Income",
     ["NoninterestIncome"], "flow", "USD", ""),
    ("provision_credit_losses", "Provision for Credit Losses",
     ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForCreditLosses",
      "ProvisionForLoanAndLeaseLosses"],
     "flow", "USD",
     "Post-CECL this is forward-looking and the single most volatile earnings "
     "swing factor."),
    ("net_charge_offs", "Net Charge-offs",
     ["FinancingReceivableAllowanceForCreditLossWriteOffs",
      "AllowanceForLoanAndLeaseLossesWriteOffsNet"],
     "flow", "USD", ""),
    ("loans", "Total Loans",
     ["NotesReceivableGross", "LoansAndLeasesReceivableNetReportedAmount",
      "FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss"],
     "instant", "USD", ""),
    ("deposits", "Total Deposits",
     ["Deposits", "DepositsDomestic"], "instant", "USD",
     "Deposit stability became a first-order risk factor after 2023; track the "
     "trend, not the level."),
    ("cet1_ratio", "CET1 Ratio",
     ["TierOneRiskBasedCapitalToRiskWeightedAssets",
      "CommonEquityTierOneCapitalToRiskWeightedAssets",
      "TierOneCommonCapitalToRiskWeightedAssets"],
     "instant", "percent",
     "Inconsistently tagged; many banks disclose CET1 only in narrative text."),
    ("net_income", "Net Income", ["NetIncomeLoss"], "flow", "USD", ""),
    ("equity", "Common Equity", ["StockholdersEquity"], "instant", "USD", ""),
]

INSURANCE_TAGS = [
    ("premiums_earned", "Net Premiums Earned",
     ["PremiumsEarnedNet", "PremiumsEarnedNetPropertyAndCasualty"],
     "flow", "USD", ""),
    ("premiums_written", "Net Premiums Written",
     ["PremiumsWrittenNet"], "flow", "USD",
     "Written leads earned; a written/earned gap signals a growth or "
     "retrenchment inflection before it hits revenue."),
    ("losses_incurred", "Losses & LAE Incurred",
     ["PolicyholderBenefitsAndClaimsIncurredNet",
      "LiabilityForClaimsAndClaimsAdjustmentExpenseIncurredClaims",
      "PolicyholderBenefitsAndClaimsIncurredHealthCare"],
     "flow", "USD", ""),
    ("underwriting_expense", "Underwriting / Acquisition Expense",
     ["DeferredPolicyAcquisitionCostAmortizationExpense",
      "OtherUnderwritingExpense"],
     "flow", "USD", ""),
    ("net_investment_income", "Net Investment Income",
     ["NetInvestmentIncome"], "flow", "USD",
     "The float earnings. In a higher-rate regime this can matter more than "
     "underwriting."),
    ("reserves", "Loss & LAE Reserves",
     ["LiabilityForClaimsAndClaimsAdjustmentExpense"], "instant", "USD", ""),
    ("equity", "Shareholders' Equity", ["StockholdersEquity"], "instant", "USD", ""),
    ("shares", "Shares Outstanding",
     ["CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"],
     "instant", "shares", ""),
]

SEMICONDUCTOR_TAGS = [
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("gross_profit", "Gross Profit", ["GrossProfit"], "flow", "USD", ""),
    ("inventory", "Inventory", ["InventoryNet"], "instant", "USD",
     "Inventory days is the cycle tell — it turns before revenue does."),
    ("cost_of_revenue", "Cost of Revenue",
     ["CostOfRevenue", "CostOfGoodsAndServicesSold"], "flow", "USD", ""),
    ("capex", "Capital Expenditures",
     ["PaymentsToAcquirePropertyPlantAndEquipment"], "flow", "USD", ""),
    ("rnd", "R&D Expense", ["ResearchAndDevelopmentExpense"], "flow", "USD", ""),
]

REIT_TAGS = [
    ("net_income", "Net Income", ["NetIncomeLoss"], "flow", "USD", ""),
    ("depreciation", "Real Estate Depreciation",
     ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"],
     "flow", "USD", ""),
    ("gain_on_sale", "Gains on Property Sales",
     ["GainLossOnSaleOfPropertiesNetOfApplicableIncomeTaxes",
      "GainLossOnSaleOfProperties"], "flow", "USD", ""),
    ("revenue", "Total Revenue",
     ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
     "flow", "USD", ""),
    ("debt", "Total Debt",
     ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"],
     "instant", "USD", ""),
]

UTILITY_TAGS = [
    ("revenue", "Revenue", ["Revenues",
                            "RevenueFromContractWithCustomerExcludingAssessedTax"],
     "flow", "USD", ""),
    ("capex", "Capital Expenditures",
     ["PaymentsToAcquirePropertyPlantAndEquipment"], "flow", "USD",
     "For a regulated utility capex IS the growth algorithm — it builds the "
     "rate base that earns the allowed return."),
    ("net_ppe", "Net PP&E (rate base proxy)",
     ["PropertyPlantAndEquipmentNet"], "instant", "USD", ""),
    ("net_income", "Net Income", ["NetIncomeLoss"], "flow", "USD", ""),
    ("equity", "Equity", ["StockholdersEquity"], "instant", "USD", ""),
]

ENERGY_TAGS = [
    ("revenue", "Revenue", ["Revenues",
                            "RevenueFromContractWithCustomerExcludingAssessedTax"],
     "flow", "USD", ""),
    ("capex", "Capital Expenditures",
     ["PaymentsToAcquirePropertyPlantAndEquipment",
      "PaymentsToAcquireOilAndGasProperty"], "flow", "USD", ""),
    ("operating_cash_flow", "Operating Cash Flow",
     ["NetCashProvidedByUsedInOperatingActivities"], "flow", "USD", ""),
    ("dd_a", "DD&A",
     ["DepreciationDepletionAndAmortization"], "flow", "USD",
     "Depletion is the E&P-specific piece — it proxies reserve consumption."),
    ("proved_reserves", "Proved Reserves",
     ["ProvedDevelopedAndUndevelopedReservesNet"], "instant", "shares",
     "Often disclosed only in the 10-K supplemental oil & gas tables."),
    ("net_income", "Net Income", ["NetIncomeLoss"], "flow", "USD", ""),
]

PHARMA_TAGS = [
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("rnd", "R&D Expense", ["ResearchAndDevelopmentExpense"], "flow", "USD",
     "R&D intensity is the pipeline investment rate — the core input for a "
     "business whose revenue is a decaying patent annuity."),
    ("sga", "SG&A", ["SellingGeneralAndAdministrativeExpense"], "flow", "USD", ""),
    ("gross_profit", "Gross Profit", ["GrossProfit"], "flow", "USD", ""),
    ("intangibles", "Intangibles & Goodwill",
     ["IntangibleAssetsNetExcludingGoodwill", "Goodwill"], "instant", "USD",
     "Large intangibles usually mean growth was bought, not discovered."),
    ("net_income", "Net Income", ["NetIncomeLoss"], "flow", "USD", ""),
]

RETAIL_TAGS = [
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("cost_of_revenue", "Cost of Revenue",
     ["CostOfGoodsAndServicesSold", "CostOfRevenue"], "flow", "USD", ""),
    ("inventory", "Inventory", ["InventoryNet"], "instant", "USD", ""),
    ("payables", "Accounts Payable", ["AccountsPayableCurrent"], "instant", "USD",
     "Against inventory this gives the cash conversion cycle — how much of the "
     "shelf the supplier is financing."),
    ("operating_income", "Operating Income",
     ["OperatingIncomeLoss"], "flow", "USD", ""),
    ("lease_liability", "Operating Lease Liability",
     ["OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiability"],
     "instant", "USD", "The real fixed-cost burden for a store footprint."),
]

AIRLINE_TAGS = [
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("fuel_expense", "Fuel Expense",
     ["FuelCosts", "AircraftFuelExpense"], "flow", "USD",
     "Typically the largest and most volatile single cost line."),
    ("operating_expense", "Operating Expense",
     ["OperatingExpenses", "CostsAndExpenses"], "flow", "USD", ""),
    ("operating_income", "Operating Income",
     ["OperatingIncomeLoss"], "flow", "USD", ""),
    ("debt", "Total Debt",
     ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"],
     "instant", "USD", "Airlines are structurally levered; watch the trend."),
    ("ppe", "Fleet (Net PP&E)",
     ["PropertyPlantAndEquipmentNet"], "instant", "USD", ""),
]

MEDIA_TAGS = [
    ("revenue", "Revenue",
     ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
     "flow", "USD", ""),
    ("content_amortization", "Content Amortization",
     ["AmortizationOfIntangibleAssets",
      "CapitalizedContentCostsAmortizationExpense"], "flow", "USD",
     "For streamers this is the real cost of goods; cash content spend and "
     "amortized content can diverge for years."),
    ("content_assets", "Content Assets",
     ["CapitalizedContentCostsNet"], "instant", "USD", ""),
    ("operating_income", "Operating Income",
     ["OperatingIncomeLoss"], "flow", "USD", ""),
    ("operating_cash_flow", "Operating Cash Flow",
     ["NetCashProvidedByUsedInOperatingActivities"], "flow", "USD", ""),
]

SECTOR_TAGS = {
    "hyperscalers": HYPERSCALER_TAGS,
    "banks": BANK_TAGS,
    "insurance": INSURANCE_TAGS,
    "semiconductors": SEMICONDUCTOR_TAGS,
    "reits": REIT_TAGS,
    "utilities": UTILITY_TAGS,
    "energy": ENERGY_TAGS,
    "pharma": PHARMA_TAGS,
    "retail": RETAIL_TAGS,
    "airlines": AIRLINE_TAGS,
    "media": MEDIA_TAGS,
}


# --------------------------------------------------------------------------- #
# Derived KPIs — where the actual analytical value is
# Each: (key, label, unit, fn(raw)->value|None, note)
# `raw` is {key: value} of the tag-extracted KPIs for the latest period.
# --------------------------------------------------------------------------- #
def _div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


DERIVED = {
    "hyperscalers": [
        ("true_capex", "True Infrastructure Spend", "USD",
         lambda r: None if r.get("capex") is None else
         r["capex"] + (r.get("finance_lease_additions") or 0.0),
         "Capex + finance lease additions. The number headline capex misses."),
        ("capex_intensity", "Capex Intensity (% of revenue)", "ratio",
         lambda r: _div(r.get("capex"), r.get("revenue_ttm")),
         "Capex / TTM revenue. The AI-buildout gauge."),
        ("true_capex_intensity", "True Capex Intensity", "ratio",
         lambda r: _div(
             None if r.get("capex") is None else
             r["capex"] + (r.get("finance_lease_additions") or 0.0),
             r.get("revenue_ttm")),
         "Includes leased capacity."),
        ("fcf", "Free Cash Flow", "USD",
         lambda r: None if (r.get("operating_cash_flow") is None
                            or r.get("capex") is None)
         else r["operating_cash_flow"] - r["capex"],
         "OCF minus capex. Compresses hard during a buildout."),
        ("rpo_coverage", "RPO / TTM Revenue", "ratio",
         lambda r: _div(r.get("rpo"), r.get("revenue_ttm")),
         "Years of contracted revenue in the backlog. Rising = demand ahead of "
         "recognition."),
        ("capex_to_ocf", "Capex / Operating Cash Flow", "ratio",
         lambda r: _div(r.get("capex"), r.get("operating_cash_flow")),
         "How much of internally generated cash the buildout absorbs. Above "
         "~1.0 the spend is being externally financed."),
        ("depreciation_to_capex", "D&A / Capex", "ratio",
         lambda r: _div(r.get("depreciation"), r.get("capex")),
         "Falling ratio during heavy capex can mean useful-life extensions are "
         "deferring the P&L hit."),
    ],
    "banks": [
        ("nim", "Net Interest Margin", "ratio",
         lambda r: _div(r.get("net_interest_income_ttm"), r.get("earning_assets")),
         "TTM net interest income / total assets. Approximation: true NIM uses "
         "average earning assets, so this reads slightly low."),
        ("efficiency_ratio", "Efficiency Ratio", "ratio",
         lambda r: _div(r.get("noninterest_expense"),
                        (r.get("net_interest_income") or 0) +
                        (r.get("noninterest_income") or 0) or None),
         "Cost to produce a dollar of revenue. Lower is better; ~55% is good "
         "for a large US bank."),
        ("nco_ratio", "Net Charge-off Ratio", "ratio",
         lambda r: _div(r.get("net_charge_offs"), r.get("loans")),
         "Realized credit losses against the loan book."),
        ("roe", "Return on Equity", "ratio",
         lambda r: _div(r.get("net_income_ttm"), r.get("equity")),
         "TTM."),
        ("roa", "Return on Assets", "ratio",
         lambda r: _div(r.get("net_income_ttm"), r.get("earning_assets")),
         "TTM. ~1%+ is healthy."),
        ("loan_to_deposit", "Loan / Deposit Ratio", "ratio",
         lambda r: _div(r.get("loans"), r.get("deposits")),
         "Funding pressure gauge. Above ~90% means less room to lend without "
         "paying up for deposits."),
    ],
    "insurance": [
        ("loss_ratio", "Loss Ratio", "ratio",
         lambda r: _div(r.get("losses_incurred"), r.get("premiums_earned")),
         "Claims as a share of earned premium."),
        ("expense_ratio", "Expense Ratio", "ratio",
         lambda r: _div(r.get("underwriting_expense"), r.get("premiums_earned")),
         "Acquisition + underwriting costs over earned premium."),
        ("combined_ratio", "Combined Ratio", "ratio",
         lambda r: None if (_div(r.get("losses_incurred"), r.get("premiums_earned"))
                            is None)
         else (_div(r.get("losses_incurred"), r.get("premiums_earned")) or 0)
         + (_div(r.get("underwriting_expense"), r.get("premiums_earned")) or 0),
         "THE insurance metric. Below 1.0 = underwriting profit; above 1.0 the "
         "insurer relies on investment income to make money."),
        ("written_to_earned", "Written / Earned Premium", "ratio",
         lambda r: _div(r.get("premiums_written"), r.get("premiums_earned")),
         "Above 1.0 = book is growing; below 1.0 = shrinking. Leads revenue."),
        ("reserve_leverage", "Reserves / Equity", "ratio",
         lambda r: _div(r.get("reserves"), r.get("equity")),
         "Higher leverage means small reserve errors hit book value hard."),
        ("book_value_per_share", "Book Value per Share", "USD",
         lambda r: _div(r.get("equity"), r.get("shares")),
         "BVPS growth + dividends is the real scorecard for P&C compounders."),
    ],
    "semiconductors": [
        ("gross_margin", "Gross Margin", "ratio",
         lambda r: _div(r.get("gross_profit"), r.get("revenue")),
         "The cycle's clearest signal — pricing power shows up here first."),
        ("inventory_days", "Inventory Days", "ratio",
         lambda r: None if _div(r.get("inventory"), r.get("cost_of_revenue"))
         is None else _div(r.get("inventory"), r.get("cost_of_revenue")) * 91.0,
         "Days of inventory on hand. Builds ahead of a downturn."),
        ("capex_intensity", "Capex Intensity", "ratio",
         lambda r: _div(r.get("capex"), r.get("revenue")),
         "Fab spend against revenue — the supply side of the cycle."),
        ("rnd_intensity", "R&D Intensity", "ratio",
         lambda r: _div(r.get("rnd"), r.get("revenue")), ""),
    ],
    "reits": [
        ("ffo", "Funds From Operations", "USD",
         lambda r: None if r.get("net_income") is None else
         r["net_income"] + (r.get("depreciation") or 0.0)
         - (r.get("gain_on_sale") or 0.0),
         "NAREIT FFO: net income + real estate depreciation - property sale "
         "gains. Earnings are meaningless for a REIT without this."),
        ("ffo_margin", "FFO Margin", "ratio",
         lambda r: _div(
             None if r.get("net_income") is None else
             r["net_income"] + (r.get("depreciation") or 0.0)
             - (r.get("gain_on_sale") or 0.0),
             r.get("revenue")), ""),
    ],
    "utilities": [
        ("capex_intensity", "Capex Intensity", "ratio",
         lambda r: _div(r.get("capex"), r.get("revenue")), ""),
        ("rate_base_growth", "Net PP&E Growth YoY", "ratio",
         lambda r: r.get("net_ppe_yoy"),
         "Rate base growth is the earnings growth algorithm for a regulated "
         "utility."),
        ("roe", "Return on Equity", "ratio",
         lambda r: _div(r.get("net_income_ttm"), r.get("equity")),
         "Compare against the allowed ROE in the utility's rate case."),
    ],
    "energy": [
        ("fcf", "Free Cash Flow", "USD",
         lambda r: None if (r.get("operating_cash_flow") is None
                            or r.get("capex") is None)
         else r["operating_cash_flow"] - r["capex"],
         "The whole thesis for the sector post-2020: capital discipline over "
         "production growth."),
        ("reinvestment_rate", "Capex / Operating Cash Flow", "ratio",
         lambda r: _div(r.get("capex"), r.get("operating_cash_flow")),
         "Below ~0.5 signals genuine discipline; a sharp rise is the classic "
         "late-cycle warning."),
        ("dda_intensity", "DD&A / Revenue", "ratio",
         lambda r: _div(r.get("dd_a"), r.get("revenue")), ""),
    ],
    "pharma": [
        ("rnd_intensity", "R&D Intensity", "ratio",
         lambda r: _div(r.get("rnd"), r.get("revenue")),
         "Pipeline investment rate. Big pharma clusters near 15-25%."),
        ("gross_margin", "Gross Margin", "ratio",
         lambda r: _div(r.get("gross_profit"), r.get("revenue")), ""),
        ("sga_intensity", "SG&A Intensity", "ratio",
         lambda r: _div(r.get("sga"), r.get("revenue")),
         "SG&A well above R&D suggests marketing is doing more work than the "
         "science."),
    ],
    "retail": [
        ("gross_margin", "Gross Margin", "ratio",
         lambda r: None if (r.get("revenue") in (None, 0)
                            or r.get("cost_of_revenue") is None)
         else (r["revenue"] - r["cost_of_revenue"]) / r["revenue"], ""),
        ("inventory_days", "Inventory Days", "ratio",
         lambda r: None if _div(r.get("inventory"), r.get("cost_of_revenue"))
         is None else _div(r.get("inventory"), r.get("cost_of_revenue")) * 91.0,
         "Rising inventory days ahead of flat sales is the markdown warning."),
        ("payable_days", "Payable Days", "ratio",
         lambda r: None if _div(r.get("payables"), r.get("cost_of_revenue"))
         is None else _div(r.get("payables"), r.get("cost_of_revenue")) * 91.0,
         "Payables above inventory days = suppliers finance the shelf "
         "(negative working capital)."),
        ("operating_margin", "Operating Margin", "ratio",
         lambda r: _div(r.get("operating_income"), r.get("revenue")), ""),
    ],
    "airlines": [
        ("fuel_intensity", "Fuel / Revenue", "ratio",
         lambda r: _div(r.get("fuel_expense"), r.get("revenue")),
         "The single biggest swing factor in airline earnings."),
        ("operating_margin", "Operating Margin", "ratio",
         lambda r: _div(r.get("operating_income"), r.get("revenue")),
         "Mid-single-digit is normal; the operating leverage cuts both ways."),
        ("debt_to_fleet", "Debt / Net PP&E", "ratio",
         lambda r: _div(r.get("debt"), r.get("ppe")),
         "How much of the fleet is financed."),
    ],
    "media": [
        ("content_intensity", "Content Amortization / Revenue", "ratio",
         lambda r: _div(r.get("content_amortization"), r.get("revenue")),
         "The effective cost of goods for a streaming business."),
        ("operating_margin", "Operating Margin", "ratio",
         lambda r: _div(r.get("operating_income"), r.get("revenue")), ""),
        ("content_to_ocf", "Content Assets / Operating Cash Flow", "ratio",
         lambda r: _div(r.get("content_assets"), r.get("operating_cash_flow_ttm")),
         "How many years of cash flow are tied up on the shelf."),
    ],
}
# --------------------------------------------------------------------------- #
HYPERSCALER_TICKERS = {"MSFT", "AMZN", "GOOGL", "GOOG", "ORCL", "META",
                       "IBM", "CRM", "SNOW", "NOW", "BABA"}
SEMI_TICKERS = {"NVDA", "AMD", "INTC", "MU", "AVGO", "TSM", "QCOM", "TXN",
                "ADI", "MRVL", "LRCX", "AMAT", "KLAC", "ASML", "WDC", "STX"}

# SIC ranges -> our sector keys.  # VERIFY against edge cases
SIC_SECTORS = [
    ((6020, 6099), "banks"),
    ((6199, 6199), "banks"),
    ((6311, 6411), "insurance"),
    ((6798, 6798), "reits"),
    ((4900, 4991), "utilities"),
    ((3674, 3674), "semiconductors"),
    ((3559, 3559), "semiconductors"),
    ((7372, 7379), "hyperscalers"),
    ((1311, 1389), "energy"),
    ((2911, 2911), "energy"),
    ((2834, 2836), "pharma"),
    ((8731, 8731), "pharma"),
    ((5200, 5990), "retail"),
    ((4512, 4513), "airlines"),
    ((4841, 4899), "media"),
    ((7812, 7841), "media"),
]


def detect_sector(ticker: str, sic=None) -> str:
    """Ticker overrides first (a hyperscaler's SIC is just 'software'), then SIC."""
    t = ticker.upper()
    if t in HYPERSCALER_TICKERS:
        return "hyperscalers"
    if t in SEMI_TICKERS:
        return "semiconductors"
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return ""
    for (lo, hi), sec in SIC_SECTORS:
        if lo <= code <= hi:
            return sec
    return ""


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _find_node(facts: dict, concept: str):
    """Look up a concept across every namespace in companyfacts (us-gaap first,
    then dei, then the filer's own extension namespace)."""
    ns = facts.get("facts") or {}
    for space in ("us-gaap", "dei", "ifrs-full", "srt"):
        node = (ns.get(space) or {}).get(concept)
        if node:
            return node
    for space, concepts in ns.items():
        if space in ("us-gaap", "dei", "ifrs-full", "srt"):
            continue
        node = (concepts or {}).get(concept)
        if node:
            return node
    return None


def extract_tag_kpis(facts: dict, specs: list):
    """Run the tag specs against companyfacts. Returns (kpis, raw, period)."""
    kpis, raw = [], {}
    periods = []

    for key, label, concepts, kind, unit, note in specs:
        picked_series, picked_tag = {}, ""
        for concept in concepts:
            node = _find_node(facts, concept)
            if not node:
                continue
            if kind == "flow":
                series = q.derive_q4(q.quarterly_flow(node), q.annual_flow(node))
            else:
                series = q.instant(node)
            if series:
                picked_series, picked_tag = series, concept
                break

        if not picked_series:
            kpis.append(KPI(key, label, None, unit, "", "tag", "",
                            note or "No matching XBRL concept found."))
            continue

        period = q.latest_period(picked_series)
        value = picked_series[period]
        periods.append(period)
        raw[key] = value
        if kind == "flow":
            t = q.ttm(picked_series, period)
            if t is not None:
                raw[f"{key}_ttm"] = t
        else:
            y = q.yoy(picked_series, period)
            if y is not None:
                raw[f"{key}_yoy"] = y
        kpis.append(KPI(key, label, value, unit, period, "tag", picked_tag, note))

    latest = max(periods) if periods else ""
    return kpis, raw, latest


def compute_derived(sector: str, raw: dict, period: str):
    out = []
    for key, label, unit, fn, note in DERIVED.get(sector, []):
        try:
            val = fn(raw)
        except Exception:
            val = None
        out.append(KPI(key, label, val, unit, period, "derived", "", note))
    return out


# --------------------------------------------------------------------------- #
# Cloud / segment revenue — the honest limitation
# --------------------------------------------------------------------------- #
CLOUD_CONCEPT_HINTS = [
    # Filer extension concepts that have appeared for cloud lines.  # VERIFY
    "CloudServicesRevenue", "CloudRevenue", "CloudAndLicenseRevenue",
    "CloudServicesAndLicenseSupportRevenue", "SubscriptionAndSupportRevenue",
]


def try_cloud_revenue(facts: dict):
    """Best-effort cloud/segment revenue.

    WHY THIS IS HARD: segment revenue (Azure, AWS, Google Cloud) is reported
    against a dimensional axis (StatementBusinessSegmentsAxis). SEC's
    companyfacts endpoint flattens facts and DISCARDS dimensions, so the
    segment breakout simply isn't in this payload. What we can catch is the
    subset of filers who define a *company extension concept* for a cloud line
    — Oracle-style — which does survive into companyfacts.

    Returns a KPI that is explicit about being unavailable rather than
    silently reporting total revenue as if it were cloud revenue.
    """
    for concept in CLOUD_CONCEPT_HINTS:
        node = _find_node(facts, concept)
        if not node:
            continue
        series = q.derive_q4(q.quarterly_flow(node), q.annual_flow(node))
        if series:
            p = q.latest_period(series)
            return KPI("cloud_revenue", "Cloud Revenue", series[p], "USD", p,
                       "tag", concept,
                       "From a filer extension concept. Verify the definition "
                       "against the 10-Q — extension tags are not standardized.")
    return KPI(
        "cloud_revenue", "Cloud Revenue", None, "USD", "", "unavailable", "",
        "NOT AVAILABLE from companyfacts: segment revenue is dimensional and "
        "the API drops dimensions. Sources that do carry it: the 10-Q R-files "
        "(Financial Report exhibits), the raw XBRL instance document, or the "
        "earnings-release 8-K EX-99.1. RPO and capex below are reliable and are "
        "usually the better forward signal anyway.")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def compute_sector_kpis(ticker: str, sector: str | None = None,
                        facts: dict | None = None) -> SectorKPIResult:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    if facts is None:
        cik, facts = company_facts(ticker)
        if sector is None:
            sub = company_submissions(cik) or {}
            sector = detect_sector(ticker, sub.get("sic"))
    sector = (sector or detect_sector(ticker) or "").lower()

    if sector not in SECTOR_TAGS:
        return SectorKPIResult(
            ticker, sector or "unknown", "", [],
            [f"No KPI profile for sector '{sector or 'unknown'}'. Supported: "
             + ", ".join(sorted(SECTOR_TAGS)) + ". Pass sector= explicitly."])

    kpis, raw, period = extract_tag_kpis(facts, SECTOR_TAGS[sector])
    kpis.extend(compute_derived(sector, raw, period))

    warnings = []
    if sector == "hyperscalers":
        cloud = try_cloud_revenue(facts)
        kpis.insert(0, cloud)
        if cloud.value is None:
            warnings.append(cloud.note)
        if raw.get("finance_lease_additions"):
            warnings.append(
                "Finance lease additions are material here — headline capex "
                "understates infrastructure spend. Use true_capex.")
    if sector == "banks":
        warnings.append(
            "NIM uses total assets as the denominator (average earning assets "
            "isn't in companyfacts), so it reads slightly low. CET1 is "
            "inconsistently tagged and may be absent.")

    missing = [k.key for k in kpis if k.value is None]
    if missing:
        warnings.append("No value found for: " + ", ".join(missing))

    return SectorKPIResult(ticker, sector, period, kpis, warnings)


# --------------------------------------------------------------------------- #
# Self-test (offline, synthetic facts)
# --------------------------------------------------------------------------- #
def _facts(concept_map: dict) -> dict:
    """Build a minimal companyfacts payload from {concept: [fact,...]}."""
    return {"facts": {"us-gaap": {c: {"units": {"USD": f}}
                                  for c, f in concept_map.items()}}}


def _quarters(vals, concept_start="2025-01-01"):
    """Four quarterly facts for 2025Q1..2025Q4 from a list of four values."""
    spans = [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
             ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31")]
    return [{"form": "10-Q", "start": s, "end": e, "val": v, "filed": "2026-02-01"}
            for (s, e), v in zip(spans, vals)]


def _instants(val, end="2025-12-31"):
    return [{"form": "10-Q", "end": end, "val": val, "filed": "2026-02-01"}]


def _self_test():
    print("Hyperscaler KPIs")
    f = _facts({
        "PaymentsToAcquirePropertyPlantAndEquipment": _quarters([10, 12, 14, 20]),
        "RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability":
            _quarters([2, 2, 3, 5]),
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            _quarters([100, 105, 110, 120]),
        "NetCashProvidedByUsedInOperatingActivities": _quarters([30, 32, 34, 40]),
        "RevenueRemainingPerformanceObligation": _instants(500),
        "DepreciationDepletionAndAmortization": _quarters([8, 8, 9, 9]),
    })
    r = compute_sector_kpis("MSFT", sector="hyperscalers", facts=f)
    assert r.sector == "hyperscalers" and r.period == "2025Q4", (r.sector, r.period)
    print(f"  \u2713 resolved sector + latest period ({r.period})")
    assert r.get("capex") == 20
    print("  \u2713 capex read from the 10-Q cash flow tag")
    assert r.get("true_capex") == 25, r.get("true_capex")
    print("  \u2713 true_capex adds finance leases (20 + 5) \u2014 the number capex misses")
    assert r.get("rpo") == 500
    assert abs(r.get("rpo_coverage") - 500 / 435) < 1e-9
    print("  \u2713 RPO and RPO/TTM-revenue coverage")
    assert r.get("fcf") == 40 - 20
    print("  \u2713 FCF = OCF - capex")
    assert abs(r.get("capex_to_ocf") - 0.5) < 1e-9
    print("  \u2713 capex/OCF buildout-financing gauge")
    cloud = [k for k in r.kpis if k.key == "cloud_revenue"][0]
    assert cloud.value is None and cloud.basis == "unavailable"
    assert "dimensional" in cloud.note
    print("  \u2713 cloud revenue reports UNAVAILABLE rather than faking it")

    print("\nBank KPIs")
    fb = _facts({
        "InterestIncomeExpenseNet": _quarters([100, 100, 100, 100]),
        "Assets": _instants(20000),
        "NoninterestExpense": _quarters([60, 60, 60, 60]),
        "NoninterestIncome": _quarters([40, 40, 40, 40]),
        "ProvisionForLoanLeaseAndOtherLosses": _quarters([5, 6, 7, 8]),
        "Deposits": _instants(15000),
        "NotesReceivableGross": _instants(12000),
        "NetIncomeLoss": _quarters([50, 50, 50, 50]),
        "StockholdersEquity": _instants(2000),
    })
    rb = compute_sector_kpis("JPM", sector="banks", facts=fb)
    assert abs(rb.get("nim") - 400 / 20000) < 1e-9
    print("  \u2713 NIM = TTM net interest income / assets")
    assert abs(rb.get("efficiency_ratio") - 60 / 140) < 1e-9
    print("  \u2713 efficiency ratio = noninterest expense / total revenue")
    assert abs(rb.get("loan_to_deposit") - 0.8) < 1e-9
    print("  \u2713 loan/deposit funding gauge")
    assert abs(rb.get("roe") - 200 / 2000) < 1e-9
    print("  \u2713 ROE on TTM earnings")
    assert rb.get("provision_credit_losses") == 8
    print("  \u2713 provision for credit losses (CECL swing factor)")

    print("\nInsurance KPIs")
    fi = _facts({
        "PremiumsEarnedNet": _quarters([100, 100, 100, 100]),
        "PremiumsWrittenNet": _quarters([110, 110, 110, 110]),
        "PolicyholderBenefitsAndClaimsIncurredNet": _quarters([60, 60, 60, 65]),
        "DeferredPolicyAcquisitionCostAmortizationExpense":
            _quarters([28, 28, 28, 30]),
        "LiabilityForClaimsAndClaimsAdjustmentExpense": _instants(800),
        "StockholdersEquity": _instants(400),
        "CommonStockSharesOutstanding": _instants(100),
    })
    ri = compute_sector_kpis("CB", sector="insurance", facts=fi)
    assert abs(ri.get("loss_ratio") - 0.65) < 1e-9
    assert abs(ri.get("expense_ratio") - 0.30) < 1e-9
    assert abs(ri.get("combined_ratio") - 0.95) < 1e-9
    print("  \u2713 combined ratio = loss + expense (0.95 = underwriting profit)")
    assert abs(ri.get("written_to_earned") - 1.1) < 1e-9
    print("  \u2713 written/earned flags a growing book before revenue shows it")
    assert abs(ri.get("book_value_per_share") - 4.0) < 1e-9
    print("  \u2713 BVPS \u2014 the P&C compounding scorecard")

    print("\nDetection + robustness")
    assert detect_sector("MSFT") == "hyperscalers"
    assert detect_sector("MU") == "semiconductors"
    assert detect_sector("XYZ", sic=6022) == "banks"
    assert detect_sector("XYZ", sic=6798) == "reits"
    print("  \u2713 sector detection: ticker override, then SIC")
    empty = compute_sector_kpis("ZZZZ", sector="banks", facts={"facts": {}})
    assert empty.coverage == 0.0 and empty.warnings
    print("  \u2713 empty facts -> 0% coverage + warnings, never an exception")
    unknown = compute_sector_kpis("ZZZZ", sector="widgets", facts={"facts": {}})
    assert unknown.warnings and not unknown.kpis
    print("  \u2713 unknown sector explains itself instead of guessing")
    d = compute_sector_kpis("MSFT", sector="hyperscalers", facts=f).as_dict()
    assert "coverage" in d and d["kpis"][0]["key"] == "cloud_revenue"
    print(f"  \u2713 serializes for the API (coverage {d['coverage']}%)")

    print("\nSECTOR KPI CHECKS PASS")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _self_test()
    for t in argv:
        try:
            res = compute_sector_kpis(t)
        except Exception as e:
            print(f"{t}: {type(e).__name__}: {e}")
            continue
        print(f"\n{res.ticker} \u2014 {res.sector or 'unknown'} \u2014 "
              f"{res.period} \u2014 coverage {res.coverage}%")
        print("-" * 70)
        for k in res.kpis:
            if k.value is None:
                print(f"  {k.label:<34} {'\u2014':>16}   ({k.basis})")
            elif k.unit == "ratio":
                print(f"  {k.label:<34} {k.value:>15.3f}   ({k.basis})")
            else:
                print(f"  {k.label:<34} {k.value:>15,.0f}   ({k.basis})")
        for w in res.warnings:
            print(f"  \u26a0 {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
