from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.dialects.postgresql import JSON
from datetime import datetime

# Create SQLAlchemy object (singleton)
db = SQLAlchemy()


# ---------- MODELS ----------

class User(db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    profile_pic = db.Column(db.String(200), nullable=True)


class PremiseCategory(db.Model):
    __tablename__ = 'premise_category'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    premises = db.relationship('Premise', back_populates='category', lazy='dynamic')


class Premise(db.Model):
    __tablename__ = 'premise'

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('premise_category.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    region = db.Column(db.String(100), nullable=False)
    district = db.Column(db.String(100), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    category = db.relationship('PremiseCategory', back_populates='premises')


class InspectionSummary(db.Model):
    __tablename__ = 'inspection_summary'

    id = db.Column(db.Integer, primary_key=True)
    inspection_name = db.Column(db.String(300), unique=True, nullable=False)
    inspection_type = db.Column(db.String(100), nullable=False)
    region = db.Column(db.String(100))
    district = db.Column(db.String(100))
    inspection_date = db.Column(db.Date)
    finalized = db.Column(db.Boolean, default=False)

    total_premises = db.Column(db.Integer, default=0)
    total_defects = db.Column(MutableDict.as_mutable(JSON), default=dict)
    value_got_products = db.Column(db.Float, default=0.0)
    value_unregistered_products = db.Column(db.Float, default=0.0)
    value_dldm_not_allowed = db.Column(db.Float, default=0.0)
    total_charges = db.Column(db.Float, default=0.0)
    poe_total_charges = db.Column(db.Float, default=0.0)

    official_report = db.Column(db.String(300), nullable=True)
    recall_product_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    recalled_products_summary = db.Column(MutableDict.as_mutable(JSON), default=dict)
    daily_normal_data = db.Column(MutableDict.as_mutable(JSON), default=dict)

    daily_inspections = db.relationship(
        'Inspection',
        back_populates='summary',
        lazy='dynamic',
        cascade="all, delete-orphan"
    )


class Inspection(db.Model):
    __tablename__ = 'inspection'

    id = db.Column(db.Integer, primary_key=True)
    summary_id = db.Column(db.Integer, db.ForeignKey('inspection_summary.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    premises_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    defects_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    charges_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    recall_product_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    recall_found_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    poe_total_charges = db.Column(db.Float, default=0.0)
    poe_name = db.Column(db.String(300), nullable=True)
    products_confiscated = db.Column(db.Boolean, default=False)
    poe_products_data = db.Column(MutableDict.as_mutable(JSON), default=dict)
    official_report = db.Column(db.String(300), nullable=True)

    summary = db.relationship(
        'InspectionSummary',
        back_populates='daily_inspections'
    )


class TimeBasedSummary(db.Model):
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
    __tablename__ = 'disposal_activity'

    id = db.Column(db.Integer, primary_key=True)
    disposal_id = db.Column(db.String(50))
    type = db.Column(db.String(50))
    region = db.Column(db.String(50))
    district = db.Column(db.String(50))
    weight = db.Column(db.Float)
    value = db.Column(db.Float)
    parent_id = db.Column(db.Integer)
    period_date = db.Column(db.DateTime)


class QAActivity(db.Model):
    __tablename__ = 'qa_activity'

    id = db.Column(db.Integer, primary_key=True)
    premise_id = db.Column(db.Integer)
    screening_date = db.Column(db.DateTime)
    remarks = db.Column(db.String(500))
