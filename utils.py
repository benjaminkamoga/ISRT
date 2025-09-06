from datetime import date
from models import db, TimeBasedSummary

def get_fiscal_year(dt: date):
    """Fiscal year starts in July."""
    if dt.month >= 7:
        return dt.year
    else:
        return dt.year - 1

def get_period_labels(dt: date):
    """Return period labels keyed by period type for a given date."""
    fiscal_year = get_fiscal_year(dt)

    # Bi-weekly label: split month by day 15
    bi_weekly_label = f"{fiscal_year}-BW{dt.month:02d}-1" if dt.day <= 15 else f"{fiscal_year}-BW{dt.month:02d}-2"

    # Monthly label
    monthly_label = f"{fiscal_year}-M{dt.month:02d}"

    # Quarterly based on fiscal year starting July
    fiscal_month = ((dt.month - 7) % 12) + 1
    quarter = ((fiscal_month - 1) // 3) + 1
    quarterly_label = f"{fiscal_year}-Q{quarter}"

    # Semi-annual
    semi_annual_label = f"{fiscal_year}-H1" if quarter in (1, 2) else f"{fiscal_year}-H2"

    # Annual
    annual_label = f"{fiscal_year}-Annual"

    return {
        "bi-weekly": bi_weekly_label,
        "monthly": monthly_label,
        "quarterly": quarterly_label,
        "semi-annual": semi_annual_label,
        "annual": annual_label,
        "fiscal_year": fiscal_year
    }

def update_time_based_summary(inspection_date, premises_inspected, defects_found, charges_issued):
    """
    Update or create TimeBasedSummary records for all relevant periods
    based on the inspection date, incrementing totals.
    """
    periods = get_period_labels(inspection_date)
    fiscal_year = periods.pop("fiscal_year")

    for period_type, period_label in periods.items():
        summary = TimeBasedSummary.query.filter_by(
            period_type=period_type,
            period_label=period_label
        ).first()

        if not summary:
            summary = TimeBasedSummary(
                period_type=period_type,
                period_label=period_label,
                fiscal_year=fiscal_year,
                premises_inspected=premises_inspected,
                defects_found=defects_found,
                charges_issued=charges_issued
            )
            db.session.add(summary)
        else:
            summary.premises_inspected += premises_inspected
            summary.defects_found += defects_found
            summary.charges_issued += charges_issued

    db.session.commit()
