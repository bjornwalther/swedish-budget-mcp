"""ASCII visualization formatters for statsbudget-mcp.

Three output formats optimized for token efficiency and readability
in chat interfaces, terminals, and screen readers:

1. bar: Proportional bars showing relative size of budget items
2. flow: Income-to-expenditure flow diagram (text Sankey)
3. decision: Decision chain showing who voted for what

All formats use Unicode block characters for visual weight.
Typical output: 200-400 tokens for a full budget overview.
"""

from __future__ import annotations

from typing import Any

# Block characters for proportional bars
FULL = "\u2588"  # █
LIGHT = "\u2591"  # ░
BAR_WIDTH = 20


def _bar(value: float, max_value: float, width: int = BAR_WIDTH) -> str:
    """Render a proportional bar using block characters."""
    if max_value <= 0:
        return LIGHT * width
    filled = round(value / max_value * width)
    filled = max(0, min(width, filled))
    return FULL * filled + LIGHT * (width - filled)


def _fmt_msek(value: float) -> str:
    """Format MSEK as readable string with appropriate unit."""
    if abs(value) >= 1000:
        return f"{value / 1000:.0f} mdr"
    return f"{value:.0f} mnkr"


def _fmt_pct(value: float, total: float) -> str:
    """Format as percentage of total."""
    if total == 0:
        return "  - %"
    return f"{value / total * 100:4.1f}%"


def format_budget_bars(
    overview: dict[str, Any],
    top_n: int = 15,
) -> str:
    """Format budget overview as proportional ASCII bars.

    Shows expenditure areas ranked by outcome, with bars showing
    relative size. Compact: ~15 tokens per line.

    Args:
        overview: Output from get_budget_overview tool.
        top_n: Number of areas to show (default 15, rest grouped).
    """
    year = overview["year"]
    total_exp = overview["total_expenditure_msek"]
    total_inc = overview["total_income_msek"]
    balance = overview["balance_msek"]

    areas = sorted(
        overview.get("areas", []),
        key=lambda a: a.get("outcome_msek", 0) or 0,
        reverse=True,
    )

    max_val = areas[0]["outcome_msek"] if areas else 1

    lines = [
        f"Statsbudgeten {year}",
        f"Utgifter {_fmt_msek(total_exp)}  "
        f"Inkomster {_fmt_msek(total_inc)}  "
        f"Saldo* {'+' if balance >= 0 else ''}{_fmt_msek(balance)}",
        "* ej officiellt budgetsaldo, se balance_note",
        "",
    ]

    shown_total = 0.0
    for _i, area in enumerate(areas[:top_n]):
        outcome = area.get("outcome_msek", 0) or 0
        shown_total += outcome
        name = area["area_name"]
        if len(name) > 24:
            name = name[:22] + ".."
        bar = _bar(outcome, max_val)
        lines.append(
            f"{name:<24} {bar} {_fmt_msek(outcome):>8} {_fmt_pct(outcome, total_exp)}"
        )

    if len(areas) > top_n:
        rest = total_exp - shown_total
        bar = _bar(rest, max_val)
        lines.append(
            f"{'...ovriga (' + str(len(areas) - top_n) + ' omr.)':<24} "
            f"{bar} {_fmt_msek(rest):>8} {_fmt_pct(rest, total_exp)}"
        )

    return "\n".join(lines)


def format_budget_flow(
    overview: dict[str, Any],
    revenue: dict[str, Any] | None = None,
    top_n: int = 10,
) -> str:
    """Format budget as income-to-expenditure flow diagram.

    Left side: income sources. Right side: expenditure areas.
    Center: balance. Resembles a text-based Sankey diagram.

    Args:
        overview: Output from get_budget_overview tool.
        revenue: Output from get_revenue tool (optional, enhances left side).
        top_n: Number of expenditure areas to show.
    """
    total_exp = overview["total_expenditure_msek"]
    total_inc = overview["total_income_msek"]
    balance = total_inc - total_exp

    # Left side: income
    if revenue and "revenue_msek" in revenue:
        rev = revenue["revenue_msek"]
        income_lines = []
        income_items = [
            ("Skatt arbete", rev.get("labour")),
            ("Konsumtion", rev.get("consumption")),
            ("Kapital", rev.get("capital")),
            ("Ovrigt", rev.get("other")),
        ]
        for label, val in income_items:
            if val is not None:
                income_lines.append(f"  {label:<14} {_fmt_msek(val):>8}")
    else:
        income_lines = [f"  Total        {_fmt_msek(total_inc):>8}"]

    # Right side: expenditure
    areas = sorted(
        overview.get("areas", []),
        key=lambda a: a.get("outcome_msek", 0) or 0,
        reverse=True,
    )

    exp_lines = []
    shown = 0.0
    for area in areas[:top_n]:
        outcome = area.get("outcome_msek", 0) or 0
        shown += outcome
        name = area["area_name"]
        if len(name) > 20:
            name = name[:18] + ".."
        exp_lines.append(f"  {name:<20} {_fmt_msek(outcome):>8}")

    if len(areas) > top_n:
        rest = total_exp - shown
        exp_lines.append(f"  {'...ovrigt':<20} {_fmt_msek(rest):>8}")

    # Build flow
    balance_sign = "+" if balance >= 0 else ""
    balance_label = f"Saldo* {balance_sign}{_fmt_msek(balance)}"

    lines = [
        f"{'INKOMSTER':^30}     {'UTGIFTER':^30}",
        f"{_fmt_msek(total_inc):^30}     {_fmt_msek(total_exp):^30}",
        f"{'=' * 30}     {'=' * 30}",
    ]

    max_lines = max(len(income_lines), len(exp_lines))
    mid = max_lines // 2

    for i in range(max_lines):
        left = income_lines[i] if i < len(income_lines) else ""
        right = exp_lines[i] if i < len(exp_lines) else ""

        if i == mid - 1:
            connector = " \u2500\u2500\u2510 \u250c\u2500\u2500 "
        elif i == mid:
            connector = " \u2500\u2500\u253c\u2500\u253c\u2500\u2500 "
        elif i == mid + 1:
            connector = " \u2500\u2500\u2518 \u2514\u2500\u2500 "
        elif mid - 1 < i < mid + 1:
            connector = "   \u2502 \u2502   "
        else:
            connector = "         "

        lines.append(f"{left:<30}{connector}{right}")

    lines.append("")
    lines.append(f"  {balance_label:^60}")
    lines.append(f"  {'* ej officiellt budgetsaldo, se balance_note':^60}")

    return "\n".join(lines)


def format_decision_chain(
    year: int,
    area_id: str | None = None,
    area_name: str | None = None,
    area_budget_msek: float | None = None,
    appropriations: list[dict[str, Any]] | None = None,
    votes: dict[str, Any] | None = None,
) -> str:
    """Format budget decision chain showing who decided what.

    Shows the legislative path from proposition to vote, with
    party-level voting results and appropriation breakdown.

    Args:
        year: Budget year.
        area_id: Expenditure area ID (e.g. "06").
        area_name: Area name.
        area_budget_msek: Total area budget.
        appropriations: List of appropriation dicts with name + outcome.
        votes: Voting results dict (optional).
    """
    rm = f"{year - 1}/{str(year)[2:]}"
    lines = [
        f"BESLUTSKEDJA {year}",
        f"Prop {rm}:1 -> FiU1 (Rambeslutet) -> Riksdagen",
        "",
    ]

    if votes:
        yes = votes.get("yes", 0)
        no = votes.get("no", 0)
        absent = votes.get("absent", 0)
        lines.append("ROSTNING utgiftsramar:")
        if "yes_parties" in votes:
            lines.append(f"  JA  {yes:>3}  {votes['yes_parties']}")
        else:
            lines.append(f"  JA  {yes:>3}")
        if "no_parties" in votes:
            lines.append(f"  NEJ {no:>3}  {votes['no_parties']}")
        else:
            lines.append(f"  NEJ {no:>3}")
        lines.append(f"  FRA {absent:>3}")
        lines.append("")

    if area_id and area_name:
        budget_str = f" {_fmt_msek(area_budget_msek)}" if area_budget_msek else ""
        fiu = f"FiU{int(area_id) + 5}" if area_id.isdigit() else "FiU"
        lines.append(f"Omrade {area_id} {area_name}{budget_str} -> {fiu}")
        lines.append("")

        if appropriations:
            max_val = max(
                (a.get("outcome_msek", 0) or 0 for a in appropriations),
                default=1,
            )
            sorted_apps = sorted(
                appropriations,
                key=lambda a: a.get("outcome_msek", 0) or 0,
                reverse=True,
            )
            for app in sorted_apps[:10]:
                outcome = app.get("outcome_msek", 0) or 0
                name = app.get("appropriation_name", "?")
                if len(name) > 28:
                    name = name[:26] + ".."
                bar = _bar(outcome, max_val, width=14)
                lines.append(f"  {name:<28} {_fmt_msek(outcome):>8} {bar}")

            if len(appropriations) > 10:
                lines.append(f"  ...och {len(appropriations) - 10} anslag till")

    return "\n".join(lines)


def format_comparison_bars(
    comparisons: list[dict[str, Any]],
    year_a: int,
    year_b: int,
    top_n: int = 15,
) -> str:
    """Format year-over-year budget comparison as ASCII bars.

    Shows delta per area with directional arrows.

    Args:
        comparisons: Output from compare_budgets tool.
        year_a: Baseline year.
        year_b: Comparison year.
        top_n: Number of areas to show.
    """
    sorted_comp = sorted(
        comparisons,
        key=lambda c: abs(c.get("delta_msek", 0) or 0),
        reverse=True,
    )

    max_delta = max(
        (abs(c.get("delta_msek", 0) or 0) for c in sorted_comp),
        default=1,
    )

    lines = [
        f"Forandring {year_a} -> {year_b}",
        "",
    ]

    for comp in sorted_comp[:top_n]:
        name = comp.get("area_name", "?")
        if len(name) > 22:
            name = name[:20] + ".."
        delta = comp.get("delta_msek", 0) or 0
        pct = comp.get("delta_pct")
        pct_str = f"{pct:+.1f}%" if pct is not None else "  n/a"

        direction = "\u25b2" if delta > 0 else "\u25bc" if delta < 0 else "\u25cf"
        bar = _bar(abs(delta), max_delta, width=12)

        lines.append(
            f"{direction} {name:<22} {'+' if delta >= 0 else ''}"
            f"{_fmt_msek(delta):>8} {pct_str:>7} {bar}"
        )

    return "\n".join(lines)


def format_laffer_timeline(
    timeseries: list[dict[str, Any]],
    height: int = 12,
    width: int = 60,
) -> str:
    """Format Laffer curve data as ASCII timeline chart.

    X-axis: years. Y-axis: tax quota (% of GDP).
    Reform years marked with vertical lines and labels.

    Args:
        timeseries: Output from get_laffer_timeseries tool.
        height: Chart height in lines (default 12).
        width: Chart width in characters (default 60).
    """
    if not timeseries:
        return "Ingen data."

    quotas = [t["tax_quota_pct"] for t in timeseries if t.get("tax_quota_pct") is not None]
    years = [t["year"] for t in timeseries]

    if not quotas:
        return "Ingen skattekvot-data."

    y_min = int(min(quotas) - 2)
    y_max = int(max(quotas) + 2)
    y_range = y_max - y_min

    # Sample points to fit width
    step = max(1, len(timeseries) // width)
    sampled = timeseries[::step]

    # Build chart grid
    grid = [[" " for _ in range(len(sampled) + 6)] for _ in range(height)]

    # Y-axis labels
    for row in range(height):
        y_val = y_max - (row / (height - 1)) * y_range
        if row % 3 == 0:
            label = f"{y_val:4.0f}%"
            for i, ch in enumerate(label):
                if i < 6:
                    grid[row][i] = ch

    # Plot points
    reforms_in_chart = []
    for col, point in enumerate(sampled):
        quota = point.get("tax_quota_pct")
        if quota is None:
            continue
        row = round((y_max - quota) / y_range * (height - 1))
        row = max(0, min(height - 1, row))
        x = col + 6

        if point.get("reform"):
            grid[row][x] = "\u25c6"  # \u25c6
            reforms_in_chart.append((point["year"], point["reform"], quota))
            # Vertical line
            for r in range(height):
                if grid[r][x] == " ":
                    grid[r][x] = "\u2502"  # \u2502
        else:
            grid[row][x] = "\u2022"  # \u2022

    lines = [f"Skattekvot (% av BNP) {years[0]}-{years[-1]}", ""]

    for row in grid:
        lines.append("".join(row))

    # X-axis
    x_axis = " " * 6
    for i, point in enumerate(sampled):
        if i % (len(sampled) // 5 + 1) == 0:
            x_axis += str(point["year"])[-2:]
        else:
            x_axis += "  " if i % 2 == 0 else ""
    lines.append(x_axis[:len(sampled) + 6])

    # Reform annotations
    if reforms_in_chart:
        lines.append("")
        for yr, label, quota in reforms_in_chart:
            lines.append(f"  \u25c6 {yr} {label} ({quota:.1f}%)")

    return "\n".join(lines)
