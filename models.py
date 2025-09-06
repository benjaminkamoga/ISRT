from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ---------- MODELS ----------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    profile_pic = db.Column(db.String(200), nullable=True)


class PremiseCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class Premise(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('premise_category.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    region = db.Column(db.String(100), nullable=False)
    district = db.Column(db.String(100), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    category = db.relationship('PremiseCategory', backref=db.backref('premises', lazy=True))


class InspectionSummary(db.Model):
    __bind_key__ = 'inspection'
    __tablename__ = 'inspection_summary'

    id = db.Column(db.Integer, primary_key=True)
    inspection_name = db.Column(db.String(300), unique=True, nullable=False)
    inspection_type = db.Column(db.String(100), nullable=False)
    region = db.Column(db.String(100))
    district = db.Column(db.String(100))
    inspection_date = db.Column(db.Date)
    finalized = db.Column(db.Boolean, default=False)

    # Existing fields...
    total_premises = db.Column(db.Integer, default=0)
    total_defects = db.Column(db.JSON, default={})
    value_got_products = db.Column(db.Float, default=0.0)
    value_unregistered_products = db.Column(db.Float, default=0.0)
    value_dldm_not_allowed = db.Column(db.Float, default=0.0)
    total_charges = db.Column(db.Float, default=0.0)
    poe_total_charges = db.Column(db.Float, default=0.0)

    # NEW: store uploaded report
    official_report = db.Column(db.String(300), nullable=True)  # <--- ADD THIS

    # Recall fields
    recall_product_data = db.Column(db.JSON, nullable=True)
    recalled_products_summary = db.Column(db.JSON, default={})

    # NEW: store normal inspections keyed by type
    daily_normal_data = db.Column(db.JSON, default={})

    daily_inspections = db.relationship(
        'Inspection',
        back_populates='summary',
        lazy='dynamic',
        cascade="all, delete-orphan"
    )



class Inspection(db.Model):
    __bind_key__ = 'inspection'
    id = db.Column(db.Integer, primary_key=True)
    summary_id = db.Column(db.Integer, db.ForeignKey('inspection_summary.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    premises_data = db.Column(db.JSON, default={})
    defects_data = db.Column(db.JSON, default={})
    charges_data = db.Column(db.JSON, default={})

    # Daily recall info
    recall_product_data = db.Column(db.JSON, nullable=True)  # recalled products added that day
    recall_found_data = db.Column(db.JSON, nullable=True)    # premises with recalled products info for that day

    poe_total_charges = db.Column(db.Float, default=0.0)
    poe_name = db.Column(db.String(300), nullable=True)
    products_confiscated = db.Column(db.Boolean, default=False)
    poe_products_data = db.Column(db.JSON, nullable=True)

    official_report = db.Column(db.String(300), nullable=True)  # <--- Add this

    summary = db.relationship(
        'InspectionSummary',
        back_populates='daily_inspections'
    )


class TimeBasedSummary(db.Model):
    __bind_key__ = 'inspection'
    __tablename__ = 'time_based_summary'

    id = db.Column(db.Integer, primary_key=True)
    period_type = db.Column(db.String(20), nullable=False)
    period_label = db.Column(db.String(50), nullable=False, unique=True)
    fiscal_year = db.Column(db.Integer, nullable=False)
    inspection_date = db.Column(db.Date, nullable=True)
    premises_inspected = db.Column(db.Integer, default=0)
    defects_found = db.Column(db.Integer, default=0)
    charges_issued = db.Column(db.Float, default=0.0)


class DisposalActivity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    disposal_id = db.Column(db.String(50))
    type = db.Column(db.String(50))
    region = db.Column(db.String(50))
    district = db.Column(db.String(50))
    weight = db.Column(db.Float)
    value = db.Column(db.Float)
    parent_id = db.Column(db.Integer)
    period_date = db.Column(db.DateTime)
