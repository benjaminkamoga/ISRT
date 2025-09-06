from app import app, db

with app.app_context():
    # Create all tables in default DB and all binds
    db.create_all()  # Flask-SQLAlchemy 3.x automatically handles __bind_key__
    print("✅ All tables created successfully.")

    # Now safe to run your JSON update
    from app import update_inspections_json
    update_inspections_json()
    print("✅ Inspections JSON updated.")
