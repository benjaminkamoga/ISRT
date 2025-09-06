# setup_users.py
from app import db, app, User
from werkzeug.security import generate_password_hash

with app.app_context():
    # Create tables for default bind (users.db)
    db.create_all()

    # Check if admin user already exists
    if not User.query.filter_by(username="admin").first():
        admin_user = User(
            username="admin",
            password=generate_password_hash("admin123"),  # change this password if you want
            role="admin"
        )
        db.session.add(admin_user)
        db.session.commit()
        print("✅ users.db created with initial admin user: admin / admin123")
    else:
        print("⚠ Admin user already exists in users.db")
