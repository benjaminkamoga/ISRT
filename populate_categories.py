from app import app, db
from models import PremiseCategory

# List of default categories
default_categories = [
    "Dispensary",
    "Health Centre",
    "Polyclinic",
    "Hospital",
    "Medical Lab (Private)",
    "Medical Lab (GOT)",
    "Pharmacy (Human)",
    "Pharmacy (Vet)",
    "DLDM (Human)",
    "DLDM (Vet)"
]

with app.app_context():
    for cat_name in default_categories:
        existing = PremiseCategory.query.filter_by(name=cat_name).first()
        if not existing:
            cat = PremiseCategory(name=cat_name)
            db.session.add(cat)
    db.session.commit()
    print("Default Premise Categories added successfully!")
