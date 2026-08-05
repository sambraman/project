"""
concepts.py — XBRL tag fallback lists.

Companies tag the same economic quantity with different US-GAAP concepts (and
change tags over time). For each logical field we list candidate concepts in
priority order; extract.py takes the first that has usable annual data.

Each field is also marked as a FLOW (income-statement / period measure — has a
start+end) or a STOCK (balance-sheet / point-in-time measure — end only), which
extract.py uses to pick the right annual facts.
"""

# field -> (list of candidate us-gaap concepts, "flow" | "stock")
FIELDS = {
    "revenue": ([
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ], "flow"),
    "cost_of_revenue": ([
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ], "flow"),
    "gross_profit": ([
        "GrossProfit",
    ], "flow"),
    "operating_income": ([
        "OperatingIncomeLoss",
    ], "flow"),
    "net_income": ([
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ], "flow"),
    "interest_expense": ([
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestAndDebtExpense",
    ], "flow"),
    "eps_diluted": ([
        "EarningsPerShareDiluted",
        "EarningsPerShareBasicAndDiluted",
    ], "flow"),
    "eps_basic": ([
        "EarningsPerShareBasic",
    ], "flow"),

    "assets": ([
        "Assets",
    ], "stock"),
    "assets_current": ([
        "AssetsCurrent",
    ], "stock"),
    "liabilities": ([
        "Liabilities",
    ], "stock"),
    "liabilities_current": ([
        "LiabilitiesCurrent",
    ], "stock"),
    "equity": ([
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ], "stock"),
    "long_term_debt": ([
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    ], "stock"),
    "short_term_debt": ([
        "DebtCurrent",
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
    ], "stock"),
    "shares_outstanding": ([
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ], "stock"),
}
