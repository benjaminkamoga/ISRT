from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date
from collections import defaultdict
import os, json
from flask_sqlalchemy import SQLAlchemy
from models import db, User, PremiseCategory, Premise, InspectionSummary, Inspection, TimeBasedSummary
from utils import update_time_based_summary
import sqlite3
import uuid

# --- Initialize Flask app ---
app = Flask(__name__)

# --- Secret Key ---
# Use environment variable for production (Render)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_key')

# --- Database config ---
basedir = os.path.abspath(os.path.dirname(__file__))

# Bind your multiple SQLite DBs
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'users.db')
app.config['SQLALCHEMY_BINDS'] = {
    'inspection': 'sqlite:///' + os.path.join(basedir, 'is.db'),
    'disposal': 'sqlite:///' + os.path.join(basedir, 'disposal.db')  # optional if you use it
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Initialize SQLAlchemy ---
db.init_app(app)




def update_inspections_json():
    """Generate or update inspections_from_db.json in static/data/ including disposal and QA activities"""
    print("🔹 Running inspections JSON update...")
    with app.app_context():
        inspections = Inspection.query.all()
        print(f"Total inspections in DB: {len(inspections)}")

        # Ensure static/data exists
        data_dir = os.path.join(current_app.root_path, "static", "data")
        os.makedirs(data_dir, exist_ok=True)
        json_path = os.path.join(data_dir, "inspections_from_db.json")

        # ------------------------------
        # Process inspections (existing logic)
        # ------------------------------
        for insp in inspections:
            if insp.summary and insp.summary.inspection_type == 'POE Inspection':
                try:
                    insp.poe_products_dict = json.loads(insp.poe_products_data or '{}')
                except Exception:
                    insp.poe_products_dict = {}
            else:
                insp.poe_products_dict = {}

        # Assign Overall ID and Daily ID
        daily_counters = {}
        for insp in inspections:
            if insp.summary:
                insp.overall_id = insp.summary.id
                if insp.overall_id not in daily_counters:
                    daily_counters[insp.overall_id] = 0
                letter = chr(ord('A') + daily_counters[insp.overall_id])
                daily_counters[insp.overall_id] += 1
                insp.daily_id = f"{insp.overall_id}{letter}"
            else:
                insp.overall_id = None
                insp.daily_id = None

        # Helper: safely get recall products
        def get_recall_products(insp):
            recall_products = []
            recall_data_raw = getattr(insp, "recall_product_data", {})

            if isinstance(recall_data_raw, str):
                try:
                    recall_data = json.loads(recall_data_raw or "{}")
                except Exception:
                    recall_data = {}
            elif isinstance(recall_data_raw, dict):
                recall_data = recall_data_raw
            else:
                recall_data = {}

            products_list = recall_data.get("recalled_products", [])
            categories = [k for k in recall_data.keys() if k != "recalled_products"]

            for cat_name in categories:
                cat = recall_data.get(cat_name, {})
                for item in cat.get("products_found", []):
                    product_index = item.get("product_index")
                    batch_index = item.get("batch_index")

                    product = {}
                    batch = {}

                    if product_index is not None and product_index < len(products_list):
                        product = products_list[product_index]
                        if batch_index is not None and batch_index < len(product.get("batches", [])):
                            batch = product["batches"][batch_index]

                    batch_number = "N/A"
                    manufacture_date = "N/A"
                    expiry_date = "N/A"

                    if batch_index is not None:
                        if product.get("batches") and batch_index < len(product["batches"]):
                            b = product["batches"][batch_index]
                            batch_number = b.get("batchNumber", batch_number)
                            manufacture_date = b.get("manufactureDate", manufacture_date)
                            expiry_date = b.get("expiryDate", expiry_date)
                        elif item.get("batches") and batch_index < len(item["batches"]):
                            b = item["batches"][batch_index]
                            batch_number = b.get("batchNumber", batch_number)
                            manufacture_date = b.get("manufactureDate", manufacture_date)
                            expiry_date = b.get("expiryDate", expiry_date)

                    batch_number = item.get("batchNumber", batch_number)
                    manufacture_date = item.get("manufactureDate", manufacture_date)
                    expiry_date = item.get("expiryDate", expiry_date)

                    recall_products.append({
                        "brandName": product.get("brandName") or item.get("brandName", "N/A"),
                        "genericName": product.get("genericName") or item.get("genericName", "N/A"),
                        "manufacturer": product.get("manufacturer") or item.get("manufacturer", "N/A"),
                        "uom": product.get("uom") or item.get("uom", "N/A"),
                        "batchNumber": batch_number,
                        "manufactureDate": manufacture_date,
                        "expiryDate": expiry_date,
                        "premises": item.get("premises", 0),
                        "category": cat_name,
                        "value": item.get("value", 0),
                        "quantity": item.get("quantity", 0),
                        "reason": product.get("reason", "N/A")
                    })

            return recall_products

        # Build inspections list
        inspections_list = []
        for insp in inspections:
            if insp.summary and insp.summary.inspection_type == 'POE Inspection':
                premises = [{"Premise Type": "POE", "Count": 1}]
            elif insp.premises_data:
                premises = [{"Premise Type": k, "Count": v} for k, v in insp.premises_data.items()]
            else:
                premises = []

            defects = {}
            if insp.summary and insp.summary.inspection_type == 'POE Inspection':
                defects = {
                    "POE": {
                        "gotMedicines": 1 if getattr(insp, "got_products", True) else 0,
                        "unregisteredMedicines": 1 if getattr(insp, "unregistered_products", True) else 0,
                        "nopermitproduct": 1 if getattr(insp, "no_permit_products", True) else 0
                    }
                }
            elif insp.premises_data:
                for premise_type in insp.premises_data.keys():
                    if insp.defects_data and premise_type in insp.defects_data and isinstance(insp.defects_data[premise_type], dict):
                        defects[premise_type] = insp.defects_data[premise_type]
                    else:
                        defects[premise_type] = {
                            "gotMedicines": 0,
                            "unregisteredMedicines": 0,
                            "noQualifiedPersonnel": 0,
                            "minimalRequirements": 0,
                            "unregisteredPremise": 0
                        }

            recall_products = get_recall_products(insp)
            charges = insp.charges_data if insp.charges_data else {}
            if insp.summary and insp.summary.inspection_type == 'POE Inspection':
                charges.update({
                    "Total Charges": getattr(insp, "poe_total_charges", 0),
                    **insp.poe_products_dict
                })

            insp_data = {
                "Daily ID": insp.daily_id,
                "Overall ID": insp.overall_id,
                "Inspection Name": insp.summary.inspection_name if insp.summary else "N/A",
                "Inspection Type": insp.summary.inspection_type if insp.summary else "N/A",
                "Date": insp.date.strftime('%Y-%m-%d') if insp.date else "N/A",
                "Region": insp.summary.region if insp.summary else "N/A",
                "District": insp.summary.district if insp.summary else "N/A",
                "Premises Data": premises,
                "Defects Data": defects,
                "Recall Products": recall_products,
                "Charges & Confiscated Values": charges or {}
            }
            inspections_list.append(insp_data)

        # ------------------------------
        # Fetch disposal activities from disposal.db
        # ------------------------------
        disposal_activities = []
        db_path = os.path.join(current_app.root_path, "disposal.db")
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM disposal_activity")
                disposal_activities = [dict(row) for row in cursor.fetchall()]

            for act in disposal_activities:
                if act.get("period_date"):
                    act["period_date"] = act["period_date"][:10]  # YYYY-MM-DD
        except sqlite3.Error as e:
            print("❌ Error fetching disposal activities:", e)

        print(f"✅ Fetched {len(disposal_activities)} disposal activities")

        # ------------------------------
        # Fetch QA activities from disposal.db
        # ------------------------------
        qa_activities = []
        try:
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM qa_activity")
                qa_activities = [dict(row) for row in cursor.fetchall()]

            for qa in qa_activities:
                if qa.get("screening_date"):
                    qa["screening_date"] = qa["screening_date"][:10]
        except sqlite3.Error as e:
            print("❌ Error fetching QA activities:", e)

        print(f"✅ Fetched {len(qa_activities)} QA activities")

        # ------------------------------
        # Save combined JSON
        # ------------------------------
        final_data = {
            "Inspections": inspections_list,
            "Disposal Activities": disposal_activities,
            "QA Activities": qa_activities
        }

        with open(json_path, "w", encoding="utf-8") as f:
            import json
            json.dump(final_data, f, indent=2, ensure_ascii=False)

        print(f"✅ inspections_from_db.json updated with {len(inspections_list)} inspections, "
              f"{len(disposal_activities)} disposal activities, and {len(qa_activities)} QA activities at {json_path}")



UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'static', 'uploads', 'profile_pics')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# Regions and Categories
# to define centrally list of categories
regions = {
    "Mtwara": ["Mtwara Mc", "Mtwara DC", "Masasi DC", "Masasi TC", "Nanyumbu", "Newala TC", "Newala DC", "Tandahimba", "Nanyamba"],
    "Lindi": ["Lindi MC", "Kilwa", "Nachingwea", "Liwale", "Ruangwa", "Mtama"],
    "Ruvuma": ["Songea MC", "Songea DC", "Mbinga TC", "Mbinga DC", "Madaba", "Nyasa", "Namtumbo", "Tunduru"]
}

categories = [
    "Dispensary", "Health Centre", "Polyclinic", "Hospital",
    "Medical Lab (Private)", "Medical Lab (GOT)", "Pharmacy (Human)", "Pharmacy (Vet)",
    "DLDM (Human)", "DLDM (Vet)","Non Medical shops", "Ware House","Medical Devices Shops","Arbitary Sellers"
]

RECALL_CATEGORIES = [cat for cat in categories if cat != "Arbitary Sellers"]



NORMAL_INSPECTION_CATEGORIES = [
    "Routine Inspection",
    "Follow-up Inspection",
    "Medical Device Inspection",
    "Special Inspection"
]


def format_title_case(text):
    return ' '.join(word.capitalize() for word in text.split())

# --- AUTH ROUTES ---


@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session['username'] = user.username
            session['role'] = user.role
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'danger')
    return render_template('login.html')



@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))
    user = User.query.filter_by(username=session['username']).first()
    profile_pic_url = url_for('static', filename=user.profile_pic) if user and user.profile_pic else None
    return render_template('dashboard.html', role=session['role'], username=session['username'], profile_pic_url=profile_pic_url)

# --- USER MANAGEMENT ---
@app.route('/create_user', methods=['POST'])
def create_user():
    if 'username' not in session or session['role'] != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('dashboard'))
    username = request.form['new_username']
    password = request.form['new_password']
    role = request.form['new_role']
    if User.query.filter_by(username=username).first():
        flash('Username already exists!', 'danger')
        return redirect(url_for('dashboard'))
    db.session.add(User(username=username, password=generate_password_hash(password), role=role))
    db.session.commit()
    flash('User created successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/manage_accounts')
def manage_accounts():
    if 'username' not in session or session['role'] != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('dashboard'))
    users = User.query.all()
    return render_template('manage_accounts.html', users=users, current_user=session['username'])

@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if 'username' not in session or session['role'] != 'admin':
        return jsonify({'error': 'Access denied!'}), 403
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found!'}), 404
    if user.username == session['username']:
        return jsonify({'error': 'You cannot delete your own account!'}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({'success': True})

# --- PROFILE PICTURE UPLOAD ---
@app.route('/upload_profile_pic', methods=['POST'])
def upload_profile_pic():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    file = request.files.get('profile_pic')
    if not file or file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400
    filename = secure_filename(f"{session['username']}_{file.filename}")
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    user = User.query.filter_by(username=session['username']).first()
    if user:
        user.profile_pic = f'uploads/profile_pics/{filename}'
        db.session.commit()
    return jsonify({'img_url': url_for('static', filename=f'uploads/profile_pics/{filename}')})

# --- PREMISE DATA CRUD ---
# --- PREMISE DATA CRUD ---
@app.route('/premise_data')
def premise_data():
    if 'role' not in session:
        flash("Access denied!", "danger")
        return redirect(url_for('dashboard'))

    regions = ["Mtwara", "Lindi", "Ruvuma"]  # or load dynamically if needed
    # Categories can be hardcoded or loaded from your JSON file if needed
    categories = [
        "DLDM (Human)", "DLDM (Vet)", "Pharmacy (Human)", "Pharmacy (Vet)",
        "Hospitals", "Health Centre", "Dispensaries",
        "Laboratory (GOT)", "Laboratory (Private)", "Polyclinic",
        "Warehouse", "Medical Device Shop"
    ]
    return render_template("premise_data.html", regions=regions, categories=categories)


# Helper: ensure premises file exists
def load_premises_file():
    data_dir = os.path.join(current_app.root_path, "static", "data")
    os.makedirs(data_dir, exist_ok=True)  # ensure folder exists
    premises_file = os.path.join(data_dir, "premises.json")

    # Create file if missing or empty
    if not os.path.exists(premises_file) or os.path.getsize(premises_file) == 0:
        with open(premises_file, "w", encoding="utf-8") as f:
            json.dump([], f)

    # Load JSON safely
    with open(premises_file, "r", encoding="utf-8") as f:
        try:
            premises = json.load(f)
        except json.JSONDecodeError:
            # If file is invalid/empty, reset to empty list
            premises = []
            with open(premises_file, "w", encoding="utf-8") as fw:
                json.dump(premises, fw)

    return premises, premises_file

@app.route('/get_premises', methods=['GET'])
def get_premises():
    if 'role' not in session:
        return jsonify([])

    premises, _ = load_premises_file()
    return jsonify(premises)


@app.route('/save_premise', methods=['POST'])
def save_premise():
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Extract and clean data
    def format_title_case(s):
        return s.strip().title() if s else ''

    name = format_title_case(data.get('name'))
    location = format_title_case(data.get('location'))
    category_name = data.get('category', '').strip()
    region = data.get('region', '').strip()
    district = data.get('district', '').strip()
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if not all([name, category_name, region, district]):
        return jsonify({'error': 'Missing required fields'}), 400

    premises, premises_file = load_premises_file()
    premise_id = data.get('id')
    if premise_id in [None, '', 0]:
        premise_id = None

    if premise_id:  # Edit existing
        try:
            premise_id = int(premise_id)
        except ValueError:
            return jsonify({'error': 'Invalid premise ID'}), 400
        premise = next((p for p in premises if p.get("id") == premise_id), None)
        if not premise:
            return jsonify({'error': 'Premise not found'}), 404
        premise.update({
            "name": name,
            "category": category_name,
            "region": region,
            "district": district,
            "location": location
        })
        if latitude: premise["latitude"] = latitude
        if longitude: premise["longitude"] = longitude
    else:  # Add new
        new_id = max([p.get("id", 0) for p in premises], default=0) + 1
        premise = {
            "id": new_id,
            "name": name,
            "category": category_name,
            "region": region,
            "district": district,
            "location": location,
            "latitude": latitude,
            "longitude": longitude
        }
        premises.append(premise)

    with open(premises_file, "w", encoding="utf-8") as f:
        json.dump(premises, f, indent=4)

    return jsonify({'success': True})


@app.route('/delete_premise/<int:premise_id>', methods=['DELETE'])
def delete_premise(premise_id):
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    premises, premises_file = load_premises_file()
    new_premises = [p for p in premises if p.get("id") != premise_id]
    if len(new_premises) == len(premises):
        return jsonify({'error': 'Premise not found'}), 404

    with open(premises_file, "w", encoding="utf-8") as f:
        json.dump(new_premises, f, indent=4)

    return jsonify({'success': True})


@app.route('/save_location', methods=['POST'])
def save_location():
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    data = request.get_json()
    premise_id = data.get('id')
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if premise_id is None or latitude is None or longitude is None:
        return jsonify({'error': 'Missing data'}), 400

    premises, premises_file = load_premises_file()
    premise = next((p for p in premises if p.get("id") == premise_id), None)
    if not premise:
        return jsonify({'error': 'Premise not found'}), 404

    premise["latitude"] = latitude
    premise["longitude"] = longitude

    with open(premises_file, "w", encoding="utf-8") as f:
        json.dump(premises, f, indent=4)

    return jsonify({'success': True})



@app.route('/save_observation', methods=['POST'])
def save_observation():
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    data = request.get_json()
    premise_id = data.get('premiseId')
    obs_date = data.get('date')
    obs_data = data.get('observations')

    if not premise_id or not obs_date or not obs_data:
        return jsonify({'success': False, 'message': 'Missing data'}), 400

    # Map keys to labels and intensity values
    obs_weights = {
        "got": {"label": "GOT Medicines", "intensity": 30},
        "unreg": {"label": "Unregistered Medicines", "intensity": 30},
        "personnel": {"label": "No Qualified Personnel", "intensity": 5},
        "requirements": {"label": "Premise doesn't meet GSP Requirement", "intensity": 5},
        "unregPremise": {"label": "Unregistered Premise", "intensity": 5},
        "medicalPractices": {"label": "Medical Practices", "intensity": 5},
        "dldmNotAllowed": {"label": "DLDM NOT ALLOWED Medicines", "intensity": 10}
    }

    obs_readable = []
    intensity = 0
    for key, info in obs_weights.items():
        if obs_data.get(key):
            obs_readable.append(info["label"])
            intensity += info["intensity"]

    premises, premises_file = load_premises_file()
    premise = next((p for p in premises if p.get('id') == premise_id), None)
    if not premise:
        return jsonify({'success': False, 'message': 'Premise not found'}), 404

    if 'observations' not in premise:
        premise['observations'] = []

    # Add the observation with calculated intensity
    premise['observations'].append({
        'date': obs_date,
        'observations': obs_readable,
        'intensity': intensity
    })

    # --- Calculate total intensity ---
    total_intensity = sum(o.get('intensity', 0) for o in premise['observations'])
    premise['total_intensity'] = total_intensity

    # --- Calculate average intensity ---
    num_obs = len(premise['observations'])
    avg_intensity = round(total_intensity / num_obs, 2) if num_obs > 0 else 0
    premise['average_intensity'] = avg_intensity

    # Save back to file
    with open(premises_file, "w", encoding="utf-8") as f:
        json.dump(premises, f, indent=4)

    return jsonify({
        'success': True,
        'intensity': intensity,
        'total_intensity': total_intensity,
        'average_intensity': avg_intensity
    })






@app.route('/get_observations/<int:premise_id>', methods=['GET'])
def get_observations(premise_id):
    if 'role' not in session:
        return jsonify([])

    premises, _ = load_premises_file()
    premise = next((p for p in premises if p.get('id') == premise_id), None)
    if not premise:
        return jsonify([])

    return jsonify(premise.get('observations', []))




# --- INSPECTION PAGES & API ---
@app.route('/new_inspection', methods=['GET', 'POST'])
def new_inspection():
    # Ensure user is logged in
    if 'username' not in session:
        return redirect(url_for('login'))

    # Optional: restrict access only to allowed roles
    allowed_roles = ['admin', 'champion', 'user']
    if session.get('role') not in allowed_roles:
        flash("You don't have permission to access this page.")
        return redirect(url_for('dashboard'))

    # Define inspection types
    inspection_types = [
        "Routine Inspection",
        "Follow up Inspection",
        "Recall Inspection",
        "Medical Device Inspection",
        "Special Inspection",
        "POE Inspection"
    ]

    if request.method == 'POST':
        selected_type = request.form.get('inspection_type')
        region = request.form.get('region')
        district = request.form.get('district')

        # Choose template based on type
        template = 'recall_inspection.html' if selected_type == 'Recall Inspection' else 'inspection_form.html'

        return render_template(
            template,
            inspection_type=selected_type,
            region=region,
            district=district,
            categories=categories if selected_type != 'Recall Inspection' else RECALL_CATEGORIES,
            inspection_name=''
        )

    # GET request
    return render_template('new_inspection.html', inspection_types=inspection_types, regions=regions)

@app.route('/continue_inspection')
def continue_inspection():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('continue_inspection.html')

@app.route('/inspection_form')
def inspection_form():
    if 'username' not in session:
        return redirect(url_for('login'))
    inspection_type = request.args.get('inspection_type', '').lower()
    region = request.args.get('region', '')
    district = request.args.get('district', '')
    template = 'recall_inspection.html' if 'recall' in inspection_type else 'inspection_form.html'
    return render_template(template, inspection_type=inspection_type.title(), region=region, district=district, categories=RECALL_CATEGORIES if 'recall' in inspection_type else categories)












  
# ----------------- SAVE INSPECTION -----------------
@app.route('/api/inspection/save', methods=['POST'])
def save_inspection():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Required fields
    required_fields = ['inspection_type', 'region', 'district', 'inspection_date', 'premises_inspected', 'defects']
    for f in required_fields:
        if f not in data or data[f] is None:
            return jsonify({'error': f'Missing required field: {f}'}), 400

    inspection_type = data['inspection_type']
    region = data['region']
    district = data['district']
    date_str = data['inspection_date']
    premises_data = data['premises_inspected']
    defects_data = data['defects']
    recall_data = data.get('recall_data', {})
    inspection_name = data.get('inspection_name', None)
    purename = data.get('purename', 'N/A')
    end_flag = data.get('end', False)

    charges = data.get('charges', {})
    got_value = charges.get('got_value', 0)
    unregistered_value = charges.get('unregistered_value', 0)
    dldm_value = charges.get('dldm_value', 0)
    total_charges = charges.get('total_charges', 0)

    # Parse date
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

    # --- Get or create summary ---
    summary = None
    if inspection_name:
        summary = InspectionSummary.query.filter_by(inspection_name=inspection_name).first()

    if not summary:
        inspection_name = f"{inspection_type} - {region} - {district} - {date_obj.strftime('%Y%m%d')}"
        count = 1
        while InspectionSummary.query.filter_by(inspection_name=inspection_name).first():
            inspection_name = f"{inspection_type} - {region} - {district} - {date_obj.strftime('%Y%m%d')}_{count}"
            count += 1

        summary = InspectionSummary(
            inspection_name=inspection_name,
            inspection_type=inspection_type,
            region=region,
            district=district,
            finalized=False,
            inspection_date=date_obj,
            recall_product_data=recall_data
        )
        db.session.add(summary)
        db.session.commit()
    else:
        # update recall products only
        existing_products = summary.recall_product_data or {}
        summary.recall_product_data = {**existing_products, **recall_data}
        db.session.commit()

    # ✅ Always create a NEW daily inspection under the same summary_id
    daily = Inspection(
        summary_id=summary.id,
        date=date_obj,
        premises_data=premises_data,
        defects_data=defects_data,
        recall_product_data=recall_data,
        charges_data={
            'total': total_charges,
            'got_value': got_value,
            'unregistered_value': unregistered_value,
            'dldm_value': dldm_value
        },
    
    )
  

    # --- Aggregate totals for summary ---
    daily_all = Inspection.query.filter_by(summary_id=summary.id).all()
    total_defects_agg = defaultdict(int)
    for d in daily_all:
        defects = d.defects_data or {}
        for k, v in defects.items():
            if isinstance(v, int):
                total_defects_agg[k] += v
            elif isinstance(v, dict):
                total_defects_agg[k] += sum(v.values())

    summary.total_premises = sum(sum(d.premises_data.values()) for d in daily_all if d.premises_data)
    summary.total_defects = total_defects_agg
    summary.value_got_products = sum(d.charges_data.get('got_value', 0) for d in daily_all)
    summary.value_unregistered_products = sum(d.charges_data.get('unregistered_value', 0) for d in daily_all)
    summary.value_dldm_not_allowed = sum(d.charges_data.get('dldm_value', 0) for d in daily_all)
    summary.total_charges = sum(d.charges_data.get('total', 0) for d in daily_all)
    summary.inspection_date = date_obj

    if end_flag:
        summary.finalized = True

    db.session.add(daily)
    db.session.commit()

      # --- Update JSON immediately after saving ---
    print("🔹 Updating inspections JSON...")
    update_inspections_json()
    print("✅ Inspections JSON updated.")

    return jsonify({
        'success': True,
        'inspection_name': summary.inspection_name,
        'summary_id': summary.id,
        'daily_id': daily.id
    })


# ----------------- END INSPECTION -----------------
@app.route('/api/inspection/end', methods=['POST'])
def end_inspection():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    data['end'] = True
    return save_inspection()


@app.route('/api/inspection/get/<inspection_name>')
def get_inspection(inspection_name):
    """
    Fetches daily inspection data for a given inspection summary by name.
    Works for both normal inspections and recall inspections.
    """
    # Find the inspection summary
    summary = InspectionSummary.query.filter_by(inspection_name=inspection_name).first()
    if not summary:
        return jsonify({"error": "Inspection not found"}), 404

    # Determine inspection type from query parameters (optional)
    inspection_type = request.args.get('inspection_type', summary.inspection_type)

    # For normal inspections, fetch from daily_normal_data
    daily_normal_data = summary.daily_normal_data or {}
    normal_data = daily_normal_data.get(inspection_type, {})

    # For recall inspections, fetch recall_product_data
    recall_data = summary.recall_product_data or {}

    # Build a response dictionary
    response = {
        "inspection_name": summary.inspection_name,
        "inspection_type": summary.inspection_type,
        "region": summary.region,
        "district": summary.district,
        "inspection_date": summary.inspection_date.strftime('%Y-%m-%d') if summary.inspection_date else None,
        "finalized": summary.finalized,
        "total_premises": summary.total_premises,
        "total_defects": summary.total_defects,
        "value_got_products": summary.value_got_products,
        "value_unregistered_products": summary.value_unregistered_products,
        "value_dldm_not_allowed": summary.value_dldm_not_allowed,
        "total_charges": summary.total_charges,
        "poe_total_charges": summary.poe_total_charges,
        "daily_normal_data": normal_data,      # Normal inspections keyed by type
        "recall_product_data": recall_data     # Recall inspections
    }

    return jsonify(response)




@app.route('/continue_normal_inspection')
def continue_normal_inspection():
    inspection_name = request.args.get('inspection_name')
    region = request.args.get('region')
    district = request.args.get('district')
    inspection_type = request.args.get('inspection_type')

    # Fetch the inspection summary
    summary = InspectionSummary.query.filter_by(inspection_name=inspection_name).first()
    if not summary:
        flash("Inspection not found", "danger")
        return redirect(url_for("dashboard"))

    # Safely get daily data based on type
    daily_data_field = f"daily_{inspection_type.lower().replace(' ', '_')}_data"
    daily_entries = getattr(summary, daily_data_field, []) or []

    # Use predefined categories instead of querying the database
    categories = [
        "Dispensary", "Health Centre", "Polyclinic", "Hospital",
        "Medical Lab (Private)", "Medical Lab (GOT)", "Pharmacy (Human)", "Pharmacy (Vet)",
        "DLDM (Human)", "DLDM (Vet)", "Non Medical shops", "Ware House", "Arbitary Sellers"
    ]
    categories_data = [{"id": idx + 1, "name": category} for idx, category in enumerate(categories)]

    # Serialize daily entries if needed (assuming it's a list of dicts)
    daily_entries_data = daily_entries  # If already dicts/lists, fine. Otherwise, convert here.

    return render_template(
        'continue_normal_inspection.html',
        inspection_name=inspection_name,
        region=region,
        district=district,
        inspection_type=inspection_type,
        daily_entries=daily_entries_data,
        categories=categories_data  # JSON-serializable for template JS
    )





@app.route('/unfinished_inspections')
def unfinished_inspections():
    if 'username' not in session:
        return jsonify([]), 401

    unfinished = InspectionSummary.query.filter_by(finalized=False).all()

    inspections_list = []
    for ins in unfinished:
        inspections_list.append({
            'summary_id': ins.id,
            'inspection_name': ins.inspection_name,
            'region': ins.region,
            'district': ins.district,
            'inspection_type': ins.inspection_type
        })

    return jsonify(inspections_list)





@app.route('/continue_recall_inspection')
def continue_recall_inspection():
    inspection_name = request.args.get('inspection_name')
    region = request.args.get('region')
    district = request.args.get('district')

    summary = InspectionSummary.query.filter_by(inspection_name=inspection_name).first()
    if not summary:
        flash("Inspection not found", "danger")
        return redirect(url_for("dashboard"))

    # --- Prepare recalled products for template ---
    recalled_products = []
    for daily in summary.daily_inspections:
        recall_data = daily.recall_product_data or {}
        if isinstance(recall_data, str):
            try:
                recall_data = json.loads(recall_data)
            except json.JSONDecodeError:
                recall_data = {}
        elif not isinstance(recall_data, dict):
            recall_data = {}

        products = recall_data.get('recalled_products', [])
        recalled_products.extend(products)

    return render_template(
        'continue_recall_inspection.html',
        inspection_name=inspection_name,
        region=region,
        district=district,
        categories=RECALL_CATEGORIES,
        recalled_products=recalled_products  # pass directly to template
    )



@app.route('/poe_inspection_form')
def poe_inspection():
    region = request.args.get('region')
    district = request.args.get('district')
    return render_template('poe_inspection.html', region=region, district=district)


@app.route('/recall_inspection')
def recall_inspection():
    region = request.args.get('region')
    district = request.args.get('district')
    return render_template(
        'recall_inspection.html',
        inspection_type="Recall Inspection",
        categories=RECALL_CATEGORIES,
        region=region,
        district=district,
        is_new=True
    )

@app.route("/inspection_form_resume")
def inspection_form_resume():
    inspection_name = request.args.get("inspection_name")
    # fetch inspection data from DB
    inspection = InspectionSummary.query.filter_by(inspection_name=inspection_name).first()
    if not inspection:
        return "Inspection not found", 404
    
    # fetch other needed data here, e.g., recalled products
    recalled_products = inspection.recalled_products_summary or []
    
    # return template with data
    return render_template(
        "inspection_form_resume.html",
        inspection_name=inspection.inspection_name,
        region=inspection.region,
        district=inspection.district,
        recalled_products=recalled_products
    )





@app.route('/api/poe_inspection/save', methods=['POST'])
def save_poe_inspection():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    poe_name_raw = data.get('poe_name')
    inspection_date_str = data.get('inspection_date')
    region = data.get('region')
    district = data.get('district')
    products_confiscated = data.get('products_confiscated', False)
    total_charges = data.get('total_charges', 0.0)
    poe_products = data.get('poe_products', {})

    # Parse inspection date
    try:
        inspection_date = datetime.strptime(inspection_date_str, "%Y-%m-%d").date()
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid inspection date'}), 400

    formatted_name = f"POE Inspection conducted on {poe_name_raw} in {district}-{region} on {inspection_date_str}"

    # Check if summary exists
    summary = InspectionSummary.query.filter_by(
        inspection_name=formatted_name,
        inspection_type='POE Inspection',
        region=region,
        district=district
    ).first()

    if not summary:
        summary = InspectionSummary(
            inspection_name=formatted_name,
            inspection_type='POE Inspection',
            region=region,
            district=district,
            inspection_date=inspection_date,
            total_charges=total_charges,
            finalized=True,
            recall_product_data={}  # empty dict for POE inspections
        )
        db.session.add(summary)
        db.session.flush()
    else:
        summary.inspection_date = inspection_date
        summary.total_charges = total_charges
        summary.finalized = True
        summary.recall_product_data = {}

    # Save the actual POE inspection
    inspection = Inspection(
        summary_id=summary.id,
        date=inspection_date,
        poe_name=formatted_name,
        products_confiscated=products_confiscated,
        poe_total_charges=total_charges,
        poe_products_data=json.dumps(poe_products),
        recall_product_data={}  # ensure dict
    )
    db.session.add(inspection)
    db.session.commit()

    # --- Update inspections JSON immediately ---
    print("🔹 Updating inspections JSON after POE inspection...")
    update_inspections_json()
    print("✅ Inspections JSON updated.")

    return jsonify({
        'success': True,
        'inspection_name': formatted_name,
        'summary_id': summary.id
    })


















@app.route('/reports/overall_reports')
def overall_reports():
    inspections = Inspection.query.all()

    # Parse POE inspection products JSON
    for insp in inspections:
        if insp.summary and insp.summary.inspection_type == 'POE Inspection':
            try:
                insp.poe_products_dict = json.loads(insp.poe_products_data or '{}')
            except Exception:
                insp.poe_products_dict = {}
        else:
            insp.poe_products_dict = {}

    # Assign Overall ID and Daily ID
    overall_map = {}
    daily_counters = {}
    next_id = 1

    for insp in inspections:
        if insp.summary:
            name = insp.summary.inspection_name
            if name not in overall_map:
                overall_map[name] = next_id
                next_id += 1
            insp.overall_id = overall_map[name]
            if insp.overall_id not in daily_counters:
                daily_counters[insp.overall_id] = 0
            letter = chr(ord('A') + daily_counters[insp.overall_id])
            daily_counters[insp.overall_id] += 1
            insp.daily_id = f"{insp.overall_id}{letter}"
        else:
            insp.overall_id = None
            insp.daily_id = None

    # Regions & districts
    regions = {
        "Mtwara": ["Mtwara MC", "Mtwara DC", "Masasi DC", "Masasi TC", "Nanyumbu", "Newala TC", "Newala DC", "Tandahimba", "Nanyamba"],
        "Lindi": ["Lindi MC", "Kilwa", "Nachingwea", "Liwale", "Ruangwa", "Mtama"],
        "Ruvuma": ["Songea MC", "Songea DC", "Mbinga TC", "Mbinga DC", "Madaba", "Nyasa", "Namtumbo", "Tunduru"]
    }

    # Flatten all districts for initial population
    all_districts = []
    for region, district_list in regions.items():
        for district in district_list:
            all_districts.append({"name": district, "region": region})

    inspection_types = [
        "Routine Inspection",
        "Follow up Inspection",
        "Recall Inspection",
        "Medical Device Inspection",
        "Special Inspection",
        "POE Inspection"
    ]

    return render_template(
        'overall_reports.html',
        inspections=inspections,
        regions=regions,
        districts=all_districts,   # ✅ pass this
        inspection_types=inspection_types,
        request=request
    )



from flask import Flask, request, send_file, redirect, url_for, flash

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # Ensure folder exists

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'xlsx'}  # Allowed file types

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'xlsx'}

def allowed_file(filename):
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


@app.route('/upload_report/<int:report_id>', methods=['POST'])
def upload_report(report_id):
    if 'official_report' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'}), 400
    
    file = request.files['official_report']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    if file and allowed_file(file.filename):
        inspection_summary = InspectionSummary.query.get(report_id)
        if not inspection_summary:
            return jsonify({'success': False, 'error': 'Inspection not found'}), 404

        # Optional: delete old file if exists
        if inspection_summary.official_report:
            old_path = os.path.join(UPLOAD_FOLDER, inspection_summary.official_report)
            if os.path.exists(old_path):
                os.remove(old_path)

        filename = f'report_{report_id}_{file.filename}'
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        # Save filename in DB
        inspection_summary.official_report = filename
        db.session.commit()

        return jsonify({
            'success': True,
            'download_url': url_for('download_report', filename=filename)
        }), 200
    else:
        return jsonify({'success': False, 'error': 'Invalid file type'}), 400



@app.route('/download_report/<filename>')
def download_report(filename):
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True)
    else:
        flash("File not found.")
        return redirect(request.referrer)


@app.route('/delete_inspection/<int:summary_id>', methods=['DELETE'])
def delete_inspection(summary_id):
    try:
        summary = InspectionSummary.query.get(summary_id)
        if not summary:
            return jsonify({'success': False, 'error': 'Inspection summary not found'})
        
        db.session.delete(summary)  # cascades to all daily inspections
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})





@app.route("/export_pdf")
def export_pdf():
    # Fetch all inspection summaries
    inspections = InspectionSummary.query.all()  # your model

    # Extract unique inspection types, regions, and districts
    inspection_types = list({insp.inspection_type for insp in inspections if insp.inspection_type})
    regions = list({insp.region for insp in inspections if insp.region})
    districts = list({insp.district for insp in inspections if insp.district})

    # Render template as normal HTML (no server-side PDF generation)
    return render_template(
        "overall_reports.html",
        inspections=inspections,
        inspection_types=inspection_types,
        regions=regions,
        districts=districts,
        pdf_mode=True  # optional: can hide buttons/modals if needed
    )


















from flask import current_app, send_from_directory

@app.route('/time_based_report')
def time_based_report():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    # Render the page; the JS will fetch JSON via /api/inspections
    return render_template(
        'time_based_report.html',
        role=session.get('role'),
        username=session.get('username')
    )


@app.route('/api/inspections')
def api_inspections():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data_dir = os.path.join(current_app.root_path, "static", "data")
    json_path = os.path.join(data_dir, "inspections_from_db.json")

    if not os.path.exists(json_path):
        return jsonify({"error": "Inspections JSON not found"}), 404

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Return only the inspections list so the JS still works
    inspections_list = data.get("Inspections", [])
    return jsonify(inspections_list)

@app.route('/api/disposal_activities')
def api_disposal_activities():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    # Define the path to your combined JSON file
    data_dir = os.path.join(current_app.root_path, "static", "data")
    json_path = os.path.join(data_dir, "inspections_from_db.json")

    # Check if the file exists
    if not os.path.exists(json_path):
        return jsonify({"error": "Inspections and Disposal Activities JSON not found"}), 404

    # Open and load the JSON file
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Extract only the "Disposal Activities" from the loaded data
    disposal_activities = data.get("Disposal Activities", [])

    # Return the disposal activities as a JSON response
    return jsonify(disposal_activities)

# --- API for QA Samples ---
@app.route('/api/qa_samples')
def api_qa_samples():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data_dir = os.path.join(current_app.root_path, "static", "data")
    json_path = os.path.join(data_dir, "inspections_from_db.json")
    if not os.path.exists(json_path):
        return jsonify({"error": "Inspections and QA JSON not found"}), 404

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    qa_samples = data.get("QA Activities", [])
    # Ensure every sample has both 'number_of_samples' and 'passed'
    for q in qa_samples:
        q['number_of_samples'] = int(q.get('number_of_samples',0))
        q['passed'] = int(q.get('passed',0))
    return jsonify(qa_samples)


@app.route('/api/targets', methods=['GET'])
def api_targets():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data_dir = os.path.join(current_app.root_path, "static", "data")
    json_path = os.path.join(data_dir, "inspections_from_db.json")
    targets_path = os.path.join(data_dir, "targets.json")

    # Check files exist
    if not os.path.exists(json_path):
        return jsonify({"error": "Inspection data not found"}), 404
    if not os.path.exists(targets_path):
        return jsonify({"error": "Targets data not found"}), 404

    # Load inspections
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        inspections_list = data.get("Inspections", [])

    # Load saved targets
    with open(targets_path, "r", encoding="utf-8") as f:
        saved_targets = json.load(f)

    # Count premises per category
    counts = {cat: 0 for cat in saved_targets}
    for insp in inspections_list:
        for premise in insp.get("Premises Data", []):
            cat_name = premise.get("Premise Type")
            if cat_name in counts:
                counts[cat_name] += premise.get("Count", 0)

    # Build response
    targets = []
    for cat, target in saved_targets.items():
        targets.append({
            "category": cat,
            "annual_target": target,
            "current_count": counts.get(cat, 0)
        })

    return jsonify(targets)


@app.route('/api/update_target', methods=['POST'])
def update_target():
    # Check if user is logged in
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized. Please log in first.'}), 401

    # Get current user role
    role = session.get('role', 'user')  # Default to 'user' if missing

    # Allow only admin and champion to edit
    if role not in ['admin', 'champion']:
        return jsonify({
            "success": False,
            "message": "You are not authorized to edit this target. Please contact an Admin or Champion."
        }), 200  # ✅ Use 200 instead of 403 to avoid 'unknown error'

    # Define path to targets.json
    data_dir = os.path.join(current_app.root_path, "static", "data")
    targets_path = os.path.join(data_dir, "targets.json")

    # Check if file exists
    if not os.path.exists(targets_path):
        return jsonify({"success": False, "message": "Targets file not found!"}), 500

    # Load existing targets
    with open(targets_path, "r", encoding="utf-8") as f:
        targets = json.load(f)

    # Get data from request
    req = request.json
    category = req.get("category")
    new_target = req.get("annual_target")

    # Validate category
    if category not in targets:
        return jsonify({"success": False, "message": "Invalid category provided."}), 400

    # Update target value
    try:
        targets[category] = int(new_target)
        with open(targets_path, "w", encoding="utf-8") as f:
            json.dump(targets, f, indent=4)
    except ValueError:
        return jsonify({"success": False, "message": "Target value must be a number."}), 400

    return jsonify({"success": True, "message": "Target updated successfully!"})


@app.route('/help', endpoint='help')
def help():
    return """
    <html><body>
    <h1>Help Page</h1>
    <p>This is a placeholder help page.</p>
    <a href="/">Back to Dashboard</a>
    </body></html>
    """
















def init_db():
    conn = sqlite3.connect('disposal.db')
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS disposal_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        disposal_id TEXT NOT NULL,   -- remove UNIQUE
        type TEXT,
        region TEXT,
        district TEXT,
        weight REAL DEFAULT 0,
        value REAL DEFAULT 0,
        parent_id TEXT,
        period_date TEXT
    )
''')

    conn.commit()
    conn.close()

init_db()







@app.route('/save_disposal', methods=['POST'])
def save_disposal():
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data sent'}), 400

    required_fields = ['type', 'region', 'district', 'weight', 'value', 'period_date']
    
    # Validate all rows first
    for i, row in enumerate(data):
        for field in required_fields:
            if row.get(field) in [None, '']:
                return jsonify({
                    'status': 'error',
                    'message': f"Row {i+1} is missing required field '{field}'"
                }), 400

    try:
        with sqlite3.connect('disposal.db', timeout=10) as conn:
            c = conn.cursor()

            for row in data:
                row_id = row.get('id')
                period_date = row.get('period_date')
                disposal_id = row.get('disposal_id')  # frontend should send this if existing

                if row_id:  # update existing row
                    c.execute('''
                        UPDATE disposal_activity
                        SET type=?, region=?, district=?, weight=?, value=?, parent_id=?, period_date=?
                        WHERE id=?
                    ''', (
                        row.get('type'),
                        row.get('region'),
                        row.get('district'),
                        row.get('weight', 0),
                        row.get('value', 0),
                        row.get('parent_id'),
                        period_date,
                        row_id
                    ))
                else:  # insert new row
                    if not disposal_id:
                        disposal_id = str(uuid.uuid4())  # new disposal entry

                    c.execute('''
                        INSERT INTO disposal_activity
                        (disposal_id, type, region, district, weight, value, parent_id, period_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        disposal_id,
                        row.get('type'),
                        row.get('region'),
                        row.get('district'),
                        row.get('weight', 0),
                        row.get('value', 0),
                        row.get('parent_id'),
                        period_date
                    ))

            conn.commit()

        # Refresh JSON file if you keep one for frontend
        update_inspections_json()
        return jsonify({'status': 'success'})

    except sqlite3.IntegrityError as e:
        return jsonify({'status': 'error', 'message': f'Database integrity error: {e}'}), 500
    except sqlite3.OperationalError as e:
        return jsonify({'status': 'error', 'message': f'Database operational error: {e}'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500




# === Delete Disposal Data ===
@app.route('/delete_disposal', methods=['POST'])
def delete_disposal():
    data = request.json
    disposal_id = data.get('disposal_id')

    if not disposal_id:
        return jsonify({'status': 'error', 'message': 'No disposal_id provided'}), 400

    conn = sqlite3.connect('disposal.db')
    c = conn.cursor()

    # Delete all rows with this disposal_id
    c.execute('DELETE FROM disposal_activity WHERE disposal_id = ?', (disposal_id,))

    conn.commit()
    conn.close()

    # ✅ Update JSON after delete
    update_inspections_json()

    return jsonify({'status': 'success'})









# --- Initialize QA Table ---
def init_qa_table():
    conn = sqlite3.connect('disposal.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS qa_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id TEXT NOT NULL,
            type TEXT NOT NULL,
            center TEXT,
            number_of_samples INTEGER DEFAULT 0,
            passed INTEGER DEFAULT 0,
            parent_id TEXT,
            screening_date TEXT,
            UNIQUE(sample_id, type)  -- only one row per type
        )
    ''')
    conn.commit()
    conn.close()

init_qa_table()



# --- Save QA ---
@app.route('/save_qa', methods=['POST'])
def save_qa():
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data sent'}), 400

    required_fields = ['type', 'center', 'number_of_samples', 'screening_date', 'passed']
    for i, row in enumerate(data):
        for field in required_fields:
            if row.get(field) in [None, '']:
                return jsonify({
                    'status': 'error',
                    'message': f"Row {i+1} is missing required field '{field}'"
                }), 400

    try:
        with sqlite3.connect('disposal.db', timeout=10) as conn:
            c = conn.cursor()
            for row in data:
                row_id = row.get('id')
                screening_date = row.get('screening_date')

                if row_id:  # update existing
                    c.execute('''
                        UPDATE qa_activity
                        SET type=?, center=?, number_of_samples=?, passed=?, parent_id=?, screening_date=?
                        WHERE id=?
                    ''', (
                        row.get('type'),
                        row.get('center'),
                        int(row.get('number_of_samples', 0)),
                        int(row.get('passed', 0)),
                        row.get('parent_id'),
                        screening_date,
                        row_id
                    ))
                else:  # insert new
                    new_sample_id = row.get('sample_id') or str(uuid.uuid4())
                    c.execute('''
                        INSERT INTO qa_activity
                        (sample_id, type, center, number_of_samples, passed, parent_id, screening_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        new_sample_id,
                        row.get('type'),
                        row.get('center'),
                        int(row.get('number_of_samples', 0)),
                        int(row.get('passed', 0)),
                        row.get('parent_id'),
                        screening_date
                    ))
            conn.commit()
        update_inspections_json()  # reuse existing JSON update logic
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# --- Delete QA ---
@app.route('/delete_qa', methods=['POST'])
def delete_qa():
    data = request.json
    sample_id = data.get('sample_id')
    type_ = data.get('type')  # get the type of the row to delete

    if not sample_id or not type_:
        return jsonify({'status': 'error', 'message': 'sample_id and type are required'}), 400

    conn = sqlite3.connect('disposal.db')
    c = conn.cursor()
    # Delete only the row with this sample_id AND type
    c.execute('DELETE FROM qa_activity WHERE sample_id = ? AND type = ?', (sample_id, type_))
    conn.commit()
    conn.close()

    update_inspections_json()  # update JSON if needed

    return jsonify({'status': 'success'})














JSON_FILE = os.path.join(app.root_path, 'static', 'data', 'inspections_from_db.json')

@app.route('/data-analysis')
def data_analysis():
    # Load JSON data
    with open(JSON_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Pass JSON data to template
    return render_template('data_analysis.html', data=data)






# Ensure the folder exists
DATA_FOLDER = 'static/data'
os.makedirs(DATA_FOLDER, exist_ok=True)

QA_FILE = os.path.join(DATA_FOLDER, 'QA_target.json')

# Create QA_target.json if it doesn't exist
if not os.path.exists(QA_FILE):
    qa_data = [
        {"center": "LIGULA RRH", "region":"Mtwara", "medicine_target":0, "device_target":0},
        {"center": "SONGEA RRH", "region":"Ruvuma", "medicine_target":0, "device_target":0},
        {"center": "SOKOINE RRH", "region":"Lindi", "medicine_target":0, "device_target":0}
    ]
    with open(QA_FILE, 'w') as f:
        json.dump(qa_data, f, indent=2)

# Serve HTML page
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# Get QA targets
@app.route('/api/qa_targets', methods=['GET'])
def get_qa_targets():
    with open(QA_FILE, 'r') as f:
        data = json.load(f)
    return jsonify(data)

# Update QA target → Only admin & champion can edit
@app.route('/api/update_qa_target', methods=['POST'])
def update_qa_target():
    # Check if user is logged in
    if 'username' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized. Please log in first."
        }), 401

    # Get current user role
    role = session.get('role', 'user')  # Default role = user

    # Allow only admin and champion to edit
    if role not in ['admin', 'champion']:
        return jsonify({
            "success": False,
            "message": "You are not authorized to edit this target. Please contact an Admin or Champion."
        }), 200  # ✅ Return 200 instead of 403 to avoid frontend 'unknown error'

    # Define QA target file path
    data_dir = os.path.join(current_app.root_path, "static", "data")
    qa_path = os.path.join(data_dir, "QA_target.json")

    # Check if QA target file exists
    if not os.path.exists(qa_path):
        return jsonify({
            "success": False,
            "message": "QA targets file not found!"
        }), 500

    # Load QA target data
    with open(qa_path, "r", encoding="utf-8") as f:
        qa_data = json.load(f)

    # Get request data
    data = request.json
    center = data.get('center')
    med = data.get('medicine_target')
    dev = data.get('device_target')

    # Find the QA center to update
    updated = False
    for target in qa_data:
        if target['center'] == center:
            if med is not None:
                target['medicine_target'] = int(med)
            if dev is not None:
                target['device_target'] = int(dev)
            updated = True
            break

    # If center not found, return error
    if not updated:
        return jsonify({
            "success": False,
            "message": "QA center not found."
        }), 400

    # Save updated QA targets
    with open(qa_path, "w", encoding="utf-8") as f:
        json.dump(qa_data, f, indent=4)

    return jsonify({
        "success": True,
        "message": "QA target updated successfully!"
    })































if __name__ == "__main__":
    update_inspections_json()  # optional for first run
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)


