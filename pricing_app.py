"""
Benevity Deal Desk Modeler — Single-file Streamlit app.
Run: streamlit run pricing_app.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st


# ══════════════════════════════════════════════════════════════════════════════
# Domain Types
# ══════════════════════════════════════════════════════════════════════════════


class PricingType(Enum):
    PER_USER = "Per User"
    FLAT_FEE = "Flat Fee"


class UpliftMode(Enum):
    MANUAL = "Manual Uplift"
    GOAL_SEEK = "Goal-Seek TCV"


@dataclass
class Product:
    name: str
    pricing_type: PricingType
    base_price: float


@dataclass
class Fee:
    name: str
    price: float


@dataclass
class YearResult:
    year: int
    uplift_pct: float
    users: int
    product_rates: dict[str, float]
    arr: float
    one_time_charges: dict[str, float]
    total_billed: float
    realized_npv: float


@dataclass
class ScenarioConfig:
    name: str = "Scenario"
    term: int = 3
    payment_label: str = "Upfront (0 Days)"
    mode: UpliftMode = UpliftMode.MANUAL
    added_fees: list[str] = field(default_factory=list)
    target_tcv: float = 250_000.0


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

MAX_TERM = 5
PAY_TERMS: dict[str, int] = {
    "Upfront (0 Days)": 0,
    "Net 30": 30,
    "Net 60": 60,
    "Net 90": 90,
}
UPLIFT_MIN, UPLIFT_MAX = 0, 30
GS_ITERATIONS = 50
GS_TOLERANCE = 1.0
GS_LOWER_BOUND = -0.50
GS_UPPER_BOUND = 2.0

DEFAULT_PRODUCTS = [
    Product("Spark", PricingType.PER_USER, 0.0),
    Product("Grants", PricingType.FLAT_FEE, 0.0),
]
DEFAULT_FEES = [Fee("Custom Integration / Migration", 0.0)]
DEFAULT_USERS = 0

SCENARIO_DEFAULTS: list[ScenarioConfig] = [
    ScenarioConfig("1 Yr Baseline", term=1),
    ScenarioConfig("3 YR - Deal", term=3),
    ScenarioConfig("Scenario 3", term=3),
    ScenarioConfig("Scenario 4", term=3),
]


# ══════════════════════════════════════════════════════════════════════════════
# Engine (pure functions — no Streamlit)
# ══════════════════════════════════════════════════════════════════════════════


def pad_to_term(values: list[float], length: int, fill: float) -> list[float]:
    """Extend `values` to `length` by repeating the last element, or `fill` if empty."""
    if not values:
        return [fill] * length
    out = list(values)
    while len(out) < length:
        out.append(out[-1])
    return out[:length]


def empty_discount_ledger() -> pd.DataFrame:
    return pd.DataFrame({
        "Year": pd.Series(dtype="int"),
        "Product": pd.Series(dtype="str"),
        "Discount (%)": pd.Series(dtype="float"),
    })


def _get_discount(ledger: pd.DataFrame, year: int, product_name: str) -> float:
    """Return the fractional discount (0-1) for a product in a given year."""
    if ledger.empty:
        return 0.0
    match = ledger[(ledger["Year"] == year) & (ledger["Product"] == product_name)]
    if match.empty:
        return 0.0
    return float(match["Discount (%)"].iloc[0]) / 100


def compute_effective_prices(
    products: list[Product],
    uplifts: list[float],
    ledger: pd.DataFrame,
    num_years: int,
) -> dict[str, list[float]]:
    """
    Build a year-indexed price schedule for each product.
    Single forward pass per product — O(products × years).
    """
    prices: dict[str, list[float]] = {}
    for product in products:
        yearly = []
        price = product.base_price
        for year in range(num_years):
            price *= 1 + uplifts[year]
            discount = _get_discount(ledger, year + 1, product.name)
            effective = price * (1 - discount)
            yearly.append(effective)
        prices[product.name] = yearly
    return prices


def run_model(
    users: list[int],
    products: list[Product],
    fees: list[Fee],
    added_fee_names: list[str],
    uplifts: list[float],
    payment_days: int,
    wacc: float,
    ledger: pd.DataFrame,
    num_years: int = MAX_TERM,
) -> list[YearResult]:
    uplifts = pad_to_term(uplifts, num_years, fill=0.0)
    price_schedule = compute_effective_prices(products, uplifts, ledger, num_years)

    results: list[YearResult] = []
    for year_idx in range(num_years):
        year_num = year_idx + 1
        user_count = users[year_idx]

        product_rates: dict[str, float] = {}
        arr = 0.0
        for product in products:
            rate = price_schedule[product.name][year_idx]
            product_rates[product.name] = rate
            if product.pricing_type == PricingType.PER_USER:
                arr += user_count * rate
            else:
                arr += rate

        one_time: dict[str, float] = {}
        ot_total = 0.0
        for fee in fees:
            key = f"{fee.name} (One-Time)"
            if year_num == 1 and fee.name in added_fee_names:
                one_time[key] = fee.price
                ot_total += fee.price
            else:
                one_time[key] = 0.0

        total_billed = arr + ot_total
        discount_years = year_idx + payment_days / 365
        realized_npv = total_billed / (1 + wacc) ** discount_years

        results.append(YearResult(
            year=year_num,
            uplift_pct=uplifts[year_idx],
            users=user_count,
            product_rates=product_rates,
            arr=arr,
            one_time_charges=one_time,
            total_billed=total_billed,
            realized_npv=realized_npv,
        ))
    return results


def goal_seek_uplift(
    target_tcv: float,
    users: list[int],
    products: list[Product],
    fees: list[Fee],
    added_fee_names: list[str],
    payment_days: int,
    wacc: float,
    ledger: pd.DataFrame,
    term: int,
) -> float:
    """Binary search for a flat uplift rate that hits `target_tcv`."""
    lo, hi = GS_LOWER_BOUND, GS_UPPER_BOUND
    best = 0.0
    for _ in range(GS_ITERATIONS):
        mid = (lo + hi) / 2
        schedule = run_model(
            users=users, products=products, fees=fees,
            added_fee_names=added_fee_names, uplifts=[mid] * term,
            payment_days=payment_days, wacc=wacc, ledger=ledger, num_years=term,
        )
        tcv = sum(r.total_billed for r in schedule)
        if tcv > target_tcv:
            hi = mid
        else:
            lo = mid
        best = mid
        if abs(tcv - target_tcv) < GS_TOLERANCE:
            break
    return best


def results_to_dataframe(results: list[YearResult]) -> pd.DataFrame:
    """Flatten YearResult list into a display-ready DataFrame."""
    rows = []
    for r in results:
        row: dict[str, object] = {
            "Year": f"Year {r.year}",
            "Uplift": f"{r.uplift_pct * 100:.2f}%",
            "Users": r.users,
        }
        for name, rate in r.product_rates.items():
            row[f"{name} Rate"] = rate
        row["ARR"] = r.arr
        for key, val in r.one_time_charges.items():
            row[key] = val
        row["Total Billed"] = r.total_billed
        row["Realized NPV"] = r.realized_npv
        rows.append(row)
    return pd.DataFrame(rows)


def results_to_export_dataframe(label: str, results: list[YearResult]) -> pd.DataFrame:
    """
    Flatten YearResult list into an export-ready DataFrame for Google Sheets.
    Columns: Scenario, Year, Uplift (%), Users, one column per product rate, ARR.
    """
    rows = []
    for r in results:
        row: dict[str, object] = {
            "Scenario": label,
            "Year": r.year,
            "Uplift (%)": round(r.uplift_pct * 100, 4),
            "Users": r.users,
        }
        for name, rate in r.product_rates.items():
            row[f"{name} Rate ($)"] = round(rate, 4)
        row["ARR ($)"] = round(r.arr, 2)
        rows.append(row)
    return pd.DataFrame(rows)


def build_export_xlsx(results: list[dict]) -> bytes:
    """
    Build an XLSX with three tables:
      1. Scenario 1 schedule
      2. Scenario 2 schedule
      3. Billed Savings (Nominal) comparison
    Gray headers, bold totals, dollar/percent formatting.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    HEADER_FILL = PatternFill("solid", fgColor="F1F5F9")
    TOTAL_FILL  = PatternFill("solid", fgColor="E2E8F0")
    HEADER_FONT = Font(bold=True, size=10)
    TOTAL_FONT  = Font(bold=True, size=10)
    MONEY_FMT   = '"$"#,##0.00'
    UPLIFT_FMT  = '0.00"%"'   # value is already ×100
    PCT_FMT     = '0.00%'     # value is decimal (0.05 = 5%)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Deal Desk Export"
    row = 1

    def write_header(cols: list[str]) -> None:
        nonlocal row
        for c, name in enumerate(cols, 1):
            cell = ws.cell(row=row, column=c, value=name)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
        row += 1

    def apply_fmt(cell, col_name: str, is_total: bool = False) -> None:
        if is_total:
            cell.fill = TOTAL_FILL
            cell.font = TOTAL_FONT
        if "Rate ($)" in col_name or col_name in ("ARR ($)", "Saving ($)") or col_name.endswith(" ($)"):
            cell.number_format = MONEY_FMT
        elif col_name == "Uplift (%)":
            cell.number_format = UPLIFT_FMT
        elif col_name == "Saving (%)":
            cell.number_format = PCT_FMT

    # ── Scenario tables ──
    export_scenarios = [s for s in results if s["Schedule"]]
    for scenario in export_scenarios:
        df = results_to_export_dataframe(scenario["Label"], scenario["Schedule"])
        write_header(list(df.columns))
        for _, data_row in df.iterrows():
            for c, (col_name, val) in enumerate(data_row.items(), 1):
                cell = ws.cell(row=row, column=c, value=val)
                apply_fmt(cell, col_name)
            row += 1
        row += 3  # spacer

    # ── Billed Savings table ──
    if len(export_scenarios) >= 2:
        baseline = export_scenarios[0]
        target   = export_scenarios[1]
        shared   = min(baseline["Term"], target["Term"])

        billed_rows: list[dict] = []
        total_base = total_tgt = 0.0
        for yr in range(shared):
            base_yr = baseline["FullSchedule"][yr]
            tgt_yr  = target["FullSchedule"][yr]
            saving  = base_yr.total_billed - tgt_yr.total_billed
            total_base += base_yr.total_billed
            total_tgt  += tgt_yr.total_billed
            billed_rows.append({
                "Year": f"Year {yr + 1}",
                baseline["Label"]: base_yr.total_billed,
                target["Label"]:   tgt_yr.total_billed,
                "Saving ($)": saving,
                "Saving (%)": saving / base_yr.total_billed if base_yr.total_billed else 0,
            })
        total_saving = total_base - total_tgt
        billed_rows.append({
            "Year": "Total",
            baseline["Label"]: total_base,
            target["Label"]:   total_tgt,
            "Saving ($)": total_saving,
            "Saving (%)": total_saving / total_base if total_base else 0,
        })

        billed_cols = list(billed_rows[0].keys())
        write_header(billed_cols)
        for br in billed_rows:
            is_total = br["Year"] == "Total"
            for c, col_name in enumerate(billed_cols, 1):
                cell = ws.cell(row=row, column=c, value=br[col_name])
                apply_fmt(cell, col_name, is_total=is_total)
            row += 1

    # ── Auto-fit column widths ──
    for col in ws.columns:
        width = max((len(str(cell.value)) if cell.value is not None else 0) for cell in col)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(width + 4, 32)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit UI
# ══════════════════════════════════════════════════════════════════════════════

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
.stApp, .block-container, header, [data-testid="stHeader"] { background: #FAFBFC !important; }
html, body, h1, h2, h3, h4, h5, p, label,
.stMarkdown, th, td, li, [class*="StyledText"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
    color: #1E293B !important;
}
.block-container { padding-top: 2rem !important; max-width: 1400px !important; }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
.brand-logo {
    font-size: 32px; font-weight: 800; letter-spacing: -0.5px;
    background: linear-gradient(135deg, #0066CC 0%, #0088FF 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.brand-divider { width: 2px; height: 28px; background: #CBD5E1; border-radius: 1px; }
.brand-sub { font-size: 15px !important; font-weight: 500 !important; color: #64748B !important; letter-spacing: 0.2px; }
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #F0F7FF 0%, #FFFFFF 100%) !important;
    border: 1px solid #DBEAFE !important; border-left: 4px solid #0066CC !important;
    padding: 16px 20px !important; border-radius: 10px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,.04) !important;
}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] div { font-size: 26px !important; font-weight: 700 !important; color: #0066CC !important; }
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] div { font-size: 11px !important; font-weight: 600 !important; color: #64748B !important; text-transform: uppercase !important; letter-spacing: 0.8px !important; }
[data-testid="stSidebar"] { background: #FFFFFF !important; border-right: 1px solid #E2E8F0 !important; }
[data-testid="stSidebar"] .stMarkdown h3 { font-size: 13px !important; font-weight: 700 !important; text-transform: uppercase !important; letter-spacing: 0.8px !important; color: #64748B !important; margin-top: 8px !important; }
.stButton > button { background: #0066CC !important; color: #FFF !important; border: none !important; font-weight: 600 !important; border-radius: 8px !important; font-size: 13px !important; padding: 6px 16px !important; transition: all .15s ease !important; }
.stButton > button:hover { background: #0055AA !important; transform: translateY(-1px) !important; }
.stButton > button * { color: #FFF !important; }
.del-btn button { background: #FFF !important; color: #94A3B8 !important; border: 1px solid #E2E8F0 !important; border-radius: 6px !important; padding: 2px 8px !important; font-size: 12px !important; }
.del-btn button:hover { background: #FEE2E2 !important; border-color: #FECACA !important; }
.del-btn button * { color: #94A3B8 !important; }
[data-testid="stDataFrame"] { border-radius: 10px !important; overflow: hidden !important; }
th { background: #F1F5F9 !important; font-weight: 600 !important; font-size: 12px !important; text-transform: uppercase !important; letter-spacing: 0.5px !important; color: #64748B !important; border-bottom: 2px solid #E2E8F0 !important; }
.stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 2px solid #E2E8F0; }
.stTabs [data-baseweb="tab"] { font-weight: 600 !important; font-size: 13px !important; padding: 10px 20px !important; color: #94A3B8 !important; border-bottom: 2px solid transparent; margin-bottom: -2px; }
.stTabs [aria-selected="true"] { color: #0066CC !important; border-bottom-color: #0066CC !important; background: transparent !important; }
.scenario-header { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #94A3B8; margin-bottom: 4px; }
.streamlit-expanderHeader { font-size: 13px !important; font-weight: 600 !important; color: #64748B !important; background: transparent !important; }
.section-header { font-size: 18px !important; font-weight: 700 !important; color: #1E293B !important; margin-bottom: 4px !important; }
.section-sub { font-size: 13px !important; color: #94A3B8 !important; margin-bottom: 16px !important; }
.stat-row { display: flex; gap: 24px; margin-top: 12px; }
.stat-item { display: flex; flex-direction: column; }
.stat-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: #94A3B8; }
.stat-val { font-size: 15px; font-weight: 700; color: #1E293B; }
[data-testid="stAlert"] { border-radius: 8px !important; font-size: 13px !important; }
#MainMenu, footer, [data-testid="stDecoration"] { display: none !important; }
.stRadio > div { gap: 4px !important; }
.stRadio label { font-size: 13px !important; }
</style>
"""

# ── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Benevity Deal Desk", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
st.markdown(
    '<div class="brand">'
    '<span class="brand-logo">Benevity</span>'
    '<span class="brand-divider"></span>'
    '<span class="brand-sub">Deal Desk Modeler</span>'
    "</div>",
    unsafe_allow_html=True,
)
st.markdown("")


# ── Session State ────────────────────────────────────────────────────────────


def _init_state() -> None:
    if "portfolio" not in st.session_state:
        st.session_state.portfolio = [
            {"name": p.name, "type": p.pricing_type.value, "price": p.base_price}
            for p in DEFAULT_PRODUCTS
        ]
    if "one_time_fees" not in st.session_state:
        st.session_state.one_time_fees = [
            {"name": f.name, "price": f.price} for f in DEFAULT_FEES
        ]
    for idx, cfg in enumerate(SCENARIO_DEFAULTS):
        defaults = {
            f"s_term_{idx}": cfg.term,
            f"s_name_{idx}": cfg.name,
            f"s_pay_{idx}": cfg.payment_label,
            f"s_mode_{idx}": cfg.mode.value,
            f"s_add_{idx}": list(cfg.added_fees),
            f"s_ledger_{idx}": empty_discount_ledger(),
        }
        for key, value in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = value


_init_state()


# ── Converters ───────────────────────────────────────────────────────────────


def _products_from_state() -> list[Product]:
    return [
        Product(p["name"], PricingType(p["type"]), p["price"])
        for p in st.session_state.portfolio
    ]


def _fees_from_state() -> list[Fee]:
    return [Fee(f["name"], f["price"]) for f in st.session_state.one_time_fees]


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### Deal Setup")
    num_scenarios = st.slider("Scenarios", 2, 4, 2)
    wacc = st.number_input("WACC (%)", value=10.0, step=1.0) / 100

    st.markdown("---")
    st.markdown("### Users")
    growth = st.radio(
        "Model", ["Flat", "Custom Ramp"], horizontal=True, label_visibility="collapsed"
    )

    if growth == "Flat":
        flat_users = st.number_input(
            "Users (all years)", value=DEFAULT_USERS, step=100, min_value=0
        )
        user_ramp = [flat_users] * MAX_TERM
    else:
        ramp_years = st.slider("Years", 1, MAX_TERM, 3, key="ramp_yrs")
        ramp_values: list[int] = []
        col_a, col_b = st.columns(2)
        for yr in range(ramp_years):
            with col_a if yr % 2 == 0 else col_b:
                ramp_values.append(
                    st.number_input(
                        f"Yr {yr + 1}", value=DEFAULT_USERS, step=100, min_value=0, key=f"ramp_{yr}"
                    )
                )
        user_ramp = pad_to_term(
            [float(v) for v in ramp_values], MAX_TERM, fill=float(DEFAULT_USERS)
        )
        user_ramp = [int(u) for u in user_ramp]

    # ── One-Time Fees ──
    st.markdown("---")
    st.markdown("### One-Time Fees")

    delete_fee_idx: int | None = None
    for idx, fee in enumerate(st.session_state.one_time_fees):
        with st.container(border=True):
            name_col, del_col = st.columns([5, 1])
            with name_col:
                fee["name"] = st.text_input(
                    "Fee Name", value=fee["name"], key=f"fn_{idx}", label_visibility="collapsed"
                )
            with del_col:
                if st.button("✕", key=f"fd_{idx}", use_container_width=True):
                    delete_fee_idx = idx
            fee["price"] = st.number_input(
                "Price ($)", value=fee["price"], key=f"fp_{idx}", format="%.0f", min_value=0.0
            )

    if delete_fee_idx is not None:
        st.session_state.one_time_fees.pop(delete_fee_idx)
        st.rerun()

    add_col, clear_col = st.columns(2)
    with add_col:
        if st.button("＋ Add Fee", key="add_fee", use_container_width=True):
            st.session_state.one_time_fees.append({"name": "New Fee", "price": 0.0})
            st.rerun()
    with clear_col:
        if st.button("Clear All", key="clr_fee", use_container_width=True):
            st.session_state.one_time_fees = []
            st.rerun()

    # ── Products ──
    st.markdown("---")
    st.markdown("### Products")

    delete_prod_idx: int | None = None
    for idx, prod in enumerate(st.session_state.portfolio):
        with st.container(border=True):
            name_col, del_col = st.columns([5, 1])
            with name_col:
                prod["name"] = st.text_input(
                    "Product Name", value=prod["name"], key=f"pn_{idx}", label_visibility="collapsed"
                )
            with del_col:
                if st.button("✕", key=f"pd_{idx}", use_container_width=True):
                    delete_prod_idx = idx
            type_col, price_col = st.columns(2)
            with type_col:
                type_options = [t.value for t in PricingType]
                prod["type"] = st.selectbox(
                    "Type", type_options, index=type_options.index(prod["type"]), key=f"pt_{idx}"
                )
            with price_col:
                prod["price"] = st.number_input(
                    "Base Price ($)", value=prod["price"], key=f"pp_{idx}", format="%.2f", min_value=0.0
                )

    if delete_prod_idx is not None:
        st.session_state.portfolio.pop(delete_prod_idx)
        st.rerun()

    add_col, clear_col = st.columns(2)
    with add_col:
        if st.button("＋ Add Product", key="add_prod", use_container_width=True):
            st.session_state.portfolio.append(
                {"name": "New Product", "type": PricingType.PER_USER.value, "price": 0.0}
            )
            st.rerun()
    with clear_col:
        if st.button("Clear All", key="clr_prod", use_container_width=True):
            st.session_state.portfolio = []
            st.rerun()


# ── Scenarios ────────────────────────────────────────────────────────────────

available_fee_names = [f["name"] for f in st.session_state.one_time_fees]
product_names = [p["name"] for p in st.session_state.portfolio]
products = _products_from_state()
fees = _fees_from_state()

results: list[dict] = []
scenario_cols = st.columns(num_scenarios, gap="large")

for scene_idx, col in enumerate(scenario_cols):
    with col:
        st.markdown(
            f'<p class="scenario-header">Scenario {scene_idx + 1}</p>',
            unsafe_allow_html=True,
        )
        label = st.text_input(
            "Label", value=st.session_state[f"s_name_{scene_idx}"],
            key=f"wn_{scene_idx}", label_visibility="collapsed", placeholder="Scenario name…",
        )
        st.session_state[f"s_name_{scene_idx}"] = label

        term_col, pay_col, mode_col = st.columns(3)
        with term_col:
            term_options = list(range(1, MAX_TERM + 1))
            term = st.selectbox(
                "Term", term_options,
                index=term_options.index(st.session_state[f"s_term_{scene_idx}"]),
                key=f"wt_{scene_idx}",
            )
            st.session_state[f"s_term_{scene_idx}"] = term

        with pay_col:
            pay_options = list(PAY_TERMS.keys())
            payment = st.selectbox(
                "Payment", pay_options,
                index=pay_options.index(st.session_state[f"s_pay_{scene_idx}"]),
                key=f"wp_{scene_idx}",
            )
            st.session_state[f"s_pay_{scene_idx}"] = payment

        with mode_col:
            mode_options = [m.value for m in UpliftMode]
            current_mode = st.session_state[f"s_mode_{scene_idx}"]
            mode_index = mode_options.index(current_mode) if current_mode in mode_options else 0
            mode = st.selectbox("Mode", mode_options, index=mode_index, key=f"wm_{scene_idx}")
            st.session_state[f"s_mode_{scene_idx}"] = mode

        valid_added = [f for f in st.session_state[f"s_add_{scene_idx}"] if f in available_fee_names]
        added = st.multiselect(
            "One-Time Fees", options=available_fee_names, default=valid_added, key=f"wa_{scene_idx}"
        )
        st.session_state[f"s_add_{scene_idx}"] = added

        payment_days = PAY_TERMS[payment]
        ledger = st.session_state[f"s_ledger_{scene_idx}"]

        with st.expander("Custom Line-Item Discounts", expanded=False):
            ledger_config = {
                "Year": st.column_config.NumberColumn("Year", min_value=1, max_value=MAX_TERM, step=1, required=True),
                "Product": st.column_config.SelectboxColumn("Product", options=product_names, required=True),
                "Discount (%)": st.column_config.NumberColumn("%", min_value=0.0, max_value=100.0, step=0.1, required=True),
            }
            ledger = st.data_editor(
                ledger, column_config=ledger_config, num_rows="dynamic",
                use_container_width=True, key=f"ed_{scene_idx}", hide_index=True,
            )
            st.session_state[f"s_ledger_{scene_idx}"] = ledger

        # ── Uplift Config ──
        uplifts: list[float] = []

        if mode == UpliftMode.GOAL_SEEK.value:
            if f"s_target_{scene_idx}" not in st.session_state:
                st.session_state[f"s_target_{scene_idx}"] = 250_000.0
            target = st.number_input(
                "Target TCV ($)", value=st.session_state[f"s_target_{scene_idx}"],
                step=1000.0, key=f"wtv_{scene_idx}", min_value=0.0,
            )
            st.session_state[f"s_target_{scene_idx}"] = target

            if not products:
                st.warning("Add a product first.")
                solved = 0.0
            else:
                solved = goal_seek_uplift(
                    target_tcv=target, users=user_ramp, products=products, fees=fees,
                    added_fee_names=added, payment_days=payment_days, wacc=wacc,
                    ledger=ledger, term=term,
                )

            if solved < -0.20:
                st.warning(f"Steep discount needed: {solved * 100:.1f}% — verify target.")
            else:
                st.caption(f"→ Solved uplift: **{solved * 100:.2f}%**")
            uplifts = [solved] * term

        else:
            variable = st.toggle("Variable uplifts", value=False, key=f"vt_{scene_idx}")
            if not variable:
                flat_uplift = (
                    st.slider("Flat Uplift %", UPLIFT_MIN, UPLIFT_MAX, 0, key=f"fu_{scene_idx}") / 100
                )
                uplifts = [flat_uplift] * term
            else:
                for yr_idx in range(term):
                    uplifts.append(
                        st.slider(
                            f"Yr {yr_idx + 1} %", UPLIFT_MIN, UPLIFT_MAX, 0,
                            key=f"vu_{scene_idx}_{yr_idx}",
                        ) / 100
                    )

        # ── Run Model ──
        full_schedule = run_model(
            users=user_ramp, products=products, fees=fees, added_fee_names=added,
            uplifts=uplifts, payment_days=payment_days, wacc=wacc, ledger=ledger,
        )
        schedule = full_schedule[:term]

        tcv = sum(r.total_billed for r in schedule)
        npv = sum(r.realized_npv for r in schedule)
        ending_arr = schedule[-1].arr if schedule else 0

        st.metric("Nominal TCV", f"${tcv:,.0f}")
        st.markdown(
            f'<div class="stat-row">'
            f'<div class="stat-item"><span class="stat-label">Realized NPV</span>'
            f'<span class="stat-val">${npv:,.0f}</span></div>'
            f'<div class="stat-item"><span class="stat-label">Ending ARR</span>'
            f'<span class="stat-val">${ending_arr:,.0f}</span></div>'
            f"</div>",
            unsafe_allow_html=True,
        )

        results.append({
            "Label": label,
            "Schedule": schedule,
            "FullSchedule": full_schedule,
            "TCV": tcv,
            "NPV": npv,
            "Ending ARR": ending_arr,
            "Term": term,
            "Added Fees": added,
        })


# ── Detail Tabs ──────────────────────────────────────────────────────────────

st.markdown("")
st.markdown("")

tab_names = ["📊 Breakdown"]
if num_scenarios > 1:
    tab_names.append(f"⚖️ vs. {results[0]['Label']}")
tab_names.append("📥 Export")
tabs = st.tabs(tab_names)

# ── Tab: Breakdown ──
with tabs[0]:
    st.markdown('<p class="section-header">Financial Breakdown</p>', unsafe_allow_html=True)
    st.markdown('<p class="section-sub">Year-by-year schedule per scenario</p>', unsafe_allow_html=True)
    for idx in range(num_scenarios):
        st.markdown(f"**{results[idx]['Label']}**")
        df = results_to_dataframe(results[idx]["Schedule"])
        fmt = {"Users": "{:,}"}
        for col_name in df.columns:
            if any(k in col_name for k in ("Rate", "Fee", "Billed", "ARR", "NPV", "One-Time")):
                fmt[col_name] = "${:,.2f}"
        st.dataframe(df.style.format(fmt), use_container_width=True, hide_index=True)
        if idx < num_scenarios - 1:
            st.divider()

# ── Tab: Comparison ──
if num_scenarios > 1:
    with tabs[1]:
        st.markdown('<p class="section-header">Deal Comparison</p>', unsafe_allow_html=True)
        st.markdown(
            '<p class="section-sub">Each scenario measured against the baseline</p>',
            unsafe_allow_html=True,
        )

        baseline = results[0]
        for comp_idx in range(1, num_scenarios):
            target_scenario = results[comp_idx]
            shared_years = min(baseline["Term"], target_scenario["Term"])
            st.markdown(
                f"**{target_scenario['Label']}** vs. **{baseline['Label']}** "
                f"— first {shared_years} yr{'s' if shared_years > 1 else ''}"
            )

            per_user_rows: list[dict] = []
            billed_rows: list[dict] = []
            total_baseline_billed = 0.0
            total_target_billed = 0.0

            for yr in range(shared_years):
                base_yr = baseline["FullSchedule"][yr]
                tgt_yr = target_scenario["FullSchedule"][yr]
                user_count = tgt_yr.users

                base_per_user = base_yr.total_billed / user_count if user_count else 0
                tgt_per_user = tgt_yr.total_billed / user_count if user_count else 0
                saving_per_user = base_per_user - tgt_per_user

                per_user_rows.append({
                    "Year": f"Year {yr + 1}",
                    "Blended $/User": tgt_per_user,
                    f"Savings vs. {baseline['Label']}": saving_per_user,
                    "Saving %": saving_per_user / base_per_user if base_per_user else 0,
                })

                base_billed = base_yr.total_billed
                tgt_billed = tgt_yr.total_billed
                total_baseline_billed += base_billed
                total_target_billed += tgt_billed
                saving = base_billed - tgt_billed

                billed_rows.append({
                    "Year": f"Year {yr + 1}",
                    f"{baseline['Label']}": base_billed,
                    f"{target_scenario['Label']}": tgt_billed,
                    "Saving ($)": saving,
                    "Saving (%)": saving / base_billed if base_billed else 0,
                })

            total_saving = total_baseline_billed - total_target_billed
            billed_rows.append({
                "Year": "Total",
                f"{baseline['Label']}": total_baseline_billed,
                f"{target_scenario['Label']}": total_target_billed,
                "Saving ($)": total_saving,
                "Saving (%)": total_saving / total_baseline_billed if total_baseline_billed else 0,
            })

            st.caption("Per-User Blended Rate")
            st.dataframe(
                pd.DataFrame(per_user_rows).style.format({
                    "Blended $/User": "${:,.2f}",
                    f"Savings vs. {baseline['Label']}": "${:,.2f}",
                    "Saving %": "{:.2%}",
                }),
                use_container_width=True, hide_index=True,
            )

            st.caption("Billed Savings (Nominal)")
            billed_df = pd.DataFrame(billed_rows)

            def highlight_total(row: pd.Series) -> list[str]:
                style = "background:#F1F5F9;font-weight:600"
                return [style] * len(row) if row["Year"] == "Total" else [""] * len(row)

            st.dataframe(
                billed_df.style.apply(highlight_total, axis=1).format({
                    f"{baseline['Label']}": "${:,.2f}",
                    f"{target_scenario['Label']}": "${:,.2f}",
                    "Saving ($)": "${:,.2f}",
                    "Saving (%)": "{:.2%}",
                }),
                use_container_width=True, hide_index=True,
            )

            npv_delta = baseline["NPV"] - target_scenario["NPV"]
            if npv_delta > 0:
                st.error(f"NPV impact: **−${npv_delta:,.0f}** vs. baseline")
            else:
                st.success(f"NPV improvement: **+${abs(npv_delta):,.0f}** vs. baseline")

            if comp_idx < num_scenarios - 1:
                st.markdown("---")

# ── Tab: Export ──
with tabs[-1]:
    st.markdown('<p class="section-header">Export</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="section-sub">Download a CSV per scenario, ready to import into Google Sheets</p>',
        unsafe_allow_html=True,
    )

    deal_name = st.text_input(
        "Deal / Account Name",
        placeholder="e.g. Acme Corp",
        help="Used to name the downloaded files.",
    )

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", deal_name.strip()).strip("_") if deal_name.strip() else "deal"
    today = date.today().isoformat()

    export_scenarios = [s for s in results if s["Schedule"]]

    if export_scenarios:
        xlsx_bytes = build_export_xlsx(results)
        filename = f"{slug}_{today}.xlsx"
        st.download_button(
            "⬇ Download Excel",
            data=xlsx_bytes,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    else:
        st.info("Configure at least one scenario to enable export.")
