import os, json, uuid
from datetime import datetime, date
from collections import defaultdict
from flask import current_app
import sqlite3

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from supabase import create_client, Client
from flask_sqlalchemy import SQLAlchemy

from models import db, User, PremiseCategory, Premise, InspectionSummary, Inspection, TimeBasedSummary
from utils import update_time_based_summary
import logging


# --------------------------
# Load .env for local development
# --------------------------
load_dotenv()  # optional for local testing

# --------------------------
# Initialize Flask
# --------------------------
app = Flask(__name__)

# --------------------------
# Configure logging
# --------------------------
logging.basicConfig(level=logging.INFO)

# --------------------------
# Flask session secret
# --------------------------
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_key")

# --------------------------
# Database setup
# --------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

db = SQLAlchemy()  # Initialize SQLAlchemy
USE_LOCAL_DB = False

# Try local SQLAlchemy first
if DATABASE_URL:
    try:
        app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        with app.app_context():
            db.create_all()
        logging.info("Connected to SQLAlchemy database successfully.")
        USE_LOCAL_DB = True
    except Exception as e:
        logging.error("Failed to connect to SQLAlchemy database: %s", e, exc_info=True)

# Setup Supabase client if local DB is not available
supabase: Client = None
if not USE_LOCAL_DB and SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("Connected to Supabase successfully.")
    except Exception as e:
        logging.error("Failed to connect to Supabase: %s", e, exc_info=True)

# --------------------------
# SQLAlchemy Models
# --------------------------
if USE_LOCAL_DB:
    class User(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(255), nullable=False)

# --------------------------
# Routes
# --------------------------
@app.route("/")
def index():
    try:
        if USE_LOCAL_DB:
            users = User.query.all()
            data = [{"id": u.id, "name": u.name} for u in users]
        elif supabase:
            response = supabase.table("users").select("*").execute()
            data = response.data
        else:
            data = {"error": "No database configured."}
        return jsonify(data)
    except Exception as e:
        logging.error("Error fetching users: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def update_inspections_json():
    """Generate or update inspections_from_db.json including Supabase inspections, disposal, and QA activities."""
    print("🔹 Running inspections JSON update...")

    # Helper function for fetching from Supabase
    def fetch_table_data(table_name):
        try:
            response = supabase.table(table_name).select("*").execute()
            return response.data or []
        except Exception as e:
            print(f"❌ Exception fetching data from '{table_name}':", e)
            return []

    # ------------------------------
    # Fetch inspections and summaries
    # ------------------------------
    inspections_data = fetch_table_data("inspection")
    print(f"✅ Fetched {len(inspections_data)} inspections from Supabase")

    summaries_data = fetch_table_data("inspection_summary")
    summary_map = {s['id']: s for s in summaries_data}
    print(f"✅ Fetched {len(summaries_data)} inspection summaries from Supabase")

    # ------------------------------
    # Ensure data folder and path
    # ------------------------------
    data_dir = os.path.join(current_app.root_path, "static", "data")
    os.makedirs(data_dir, exist_ok=True)
    json_path = os.path.join(data_dir, "inspections_from_db.json")

    # ------------------------------
    # Process inspections
    # ------------------------------
    daily_counters = {}
    processed_inspections = []

    def get_recall_products(insp):
        recall_products = []
        recall_data_raw = insp.get("recall_product_data", {})

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
        categories = [k for k in recall_data if k != "recalled_products"]

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

                batch_number = item.get("batchNumber", batch.get("batchNumber", "N/A"))
                manufacture_date = item.get("manufactureDate", batch.get("manufactureDate", "N/A"))
                expiry_date = item.get("expiryDate", batch.get("expiryDate", "N/A"))

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

    for insp in inspections_data:
        summary = summary_map.get(insp.get("summary_id"))
        overall_id = insp.get("summary_id")

        if overall_id not in daily_counters:
            daily_counters[overall_id] = 0
        letter = chr(ord('A') + daily_counters[overall_id])
        daily_counters[overall_id] += 1
        daily_id = f"{overall_id}{letter}" if overall_id else None

        # Premises
        if summary and summary.get("inspection_type") == 'POE Inspection':
            premises = [{"Premise Type": "POE", "Count": 1}]
        elif insp.get("premises_data"):
            premises = [{"Premise Type": k, "Count": v} for k, v in insp.get("premises_data", {}).items()]
        else:
            premises = []

        # Defects
        defects = {}
        if summary and summary.get("inspection_type") == 'POE Inspection':
            defects = {
                "POE": {
                    "gotMedicines": 1 if insp.get("got_products", True) else 0,
                    "unregisteredMedicines": 1 if insp.get("unregistered_products", True) else 0,
                    "nopermitproduct": 1 if insp.get("no_permit_products", True) else 0
                }
            }
        elif insp.get("premises_data"):
            for premise_type in insp.get("premises_data", {}):
                defects[premise_type] = insp.get("defects_data", {}).get(premise_type, {
                    "gotMedicines": 0,
                    "unregisteredMedicines": 0,
                    "noQualifiedPersonnel": 0,
                    "minimalRequirements": 0,
                    "unregisteredPremise": 0
                })

        # Recall products
        recall_products = get_recall_products(insp)

        # Charges
        charges = insp.get("charges_data") or {}
        if summary and summary.get("inspection_type") == 'POE Inspection':
            try:
                poe_products_dict = json.loads(insp.get("poe_products_data") or '{}')
            except Exception:
                poe_products_dict = {}
            charges.update({
                "Total Charges": insp.get("poe_total_charges", 0),
                **poe_products_dict
            })

        processed_inspections.append({
            "Daily ID": daily_id,
            "Overall ID": overall_id,
            "Inspection Name": summary.get("inspection_name") if summary else "N/A",
            "Inspection Type": summary.get("inspection_type") if summary else "N/A",
            "Date": insp.get("date")[:10] if insp.get("date") else "N/A",
            "Region": summary.get("region") if summary else "N/A",
            "District": summary.get("district") if summary else "N/A",
            "Premises Data": premises,
            "Defects Data": defects,
            "Recall Products": recall_products,
            "Charges & Confiscated Values": charges
        })

    # ------------------------------
    # Fetch Disposal Activities
    # ------------------------------
    disposal_activities = fetch_table_data("disposal_activity")
    for act in disposal_activities:
        if act.get("period_date"):
            act["period_date"] = act["period_date"][:10]
    print(f"✅ Fetched {len(disposal_activities)} disposal activities")

    # ------------------------------
    # Fetch QA Activities
    # ------------------------------
    qa_activities = []
    try:
        qa_activities = fetch_table_data("qa_activity")
        for qa in qa_activities:
            if qa.get("screening_date"):
                qa["screening_date"] = qa["screening_date"][:10]
        print(f"✅ Fetched {len(qa_activities)} QA activities")
    except Exception as e:
        print(f"⚠️ Skipping QA activities due to error: {e}")

    # ------------------------------
    # Save JSON to disk
    # ------------------------------
    final_data = {
        "Inspections": processed_inspections,
        "Disposal Activities": disposal_activities,
        "QA Activities": qa_activities
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=2, ensure_ascii=False)

    print(f"✅ inspections_from_db.json updated with "
          f"{len(processed_inspections)} inspections, "
          f"{len(disposal_activities)} disposal activities, "
          f"and {len(qa_activities)} QA activities at {json_path}")









MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB max size

@app.before_request
def limit_content_length():
    if request.content_length and request.content_length > MAX_CONTENT_LENGTH:
        return jsonify({'error': 'File too large (max 5MB)'}), 413



MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB limit



import glob


MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB limit

@app.route('/upload_profile_pic', methods=['POST'])
def upload_profile_pic():
    if 'profile_pic' not in request.files:
        return jsonify({'error': 'No file part in request'}), 400

    file = request.files['profile_pic']

    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Check file size
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    if file_length > MAX_CONTENT_LENGTH:
        return jsonify({'error': 'File size exceeds 5MB limit'}), 400

    # Secure the original filename
    filename = secure_filename(file.filename)

    # Extract extension to keep the file format
    _, ext = os.path.splitext(filename)  # includes dot, e.g. '.jpg'

    # Generate unique filename for uploading to Supabase
    unique_filename = f"{uuid.uuid4().hex}_{filename}"

    try:
        file_bytes = file.read()

        # Upload to Supabase storage bucket 'profile_pics'
        response = supabase.storage.from_('profile_pics').upload(unique_filename, file_bytes)

        if hasattr(response, 'error') and response.error:
            return jsonify({'error': f"Upload failed: {response.error.message}"}), 500

        username = session.get('username')
        if not username:
            return jsonify({'error': 'User not logged in'}), 401

        # Update user's profile_pic field in DB with the unique filename
        update_resp = supabase.table('user').update({'profile_pic': unique_filename}).eq('username', username).execute()

        if hasattr(update_resp, 'error') and update_resp.error:
            return jsonify({'error': f'Failed to update user profile_pic: {update_resp.error.message}'}), 500

        # Prepare local folder
        local_folder = os.path.join('static', 'images', 'profile_pics')
        os.makedirs(local_folder, exist_ok=True)

        # Download the uploaded image from Supabase
        download_response = supabase.storage.from_('profile_pics').download(unique_filename)
        if hasattr(download_response, 'error') and download_response.error:
            return jsonify({'error': f"Download failed: {download_response.error.message}"}), 500

        # Delete any existing files for this user with any common image extension
        for pattern in ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.bmp', '*.webp']:
            existing_files = glob.glob(os.path.join(local_folder, f"{username}{pattern}"))
            for file_path in existing_files:
                os.remove(file_path)

        # Save the new file as username + ext (e.g. john.png)
        local_filename = f"{username}{ext}"
        local_path = os.path.join(local_folder, local_filename)

        with open(local_path, 'wb') as f:
            f.write(download_response)

        SUPABASE_URL = "rhmvmrqkkhnztiequjwf.supabase.co"
        public_url = f"https://{SUPABASE_URL}/storage/v1/object/public/profile_pics/{unique_filename}"

        return jsonify({'img_url': public_url, 'local_path': local_path}), 200

    except Exception as e:
        return jsonify({'error': f'Exception during upload: {str(e)}'}), 500



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

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        resp = supabase.table("user").select("*").eq("username", username).execute()
        if resp.data:
            user = resp.data[0]
            from werkzeug.security import check_password_hash
            if check_password_hash(user["password"], password):
                session["username"] = user["username"]
                session["role"] = user["role"]
                return redirect(url_for("dashboard"))

        flash("Invalid credentials", "danger")

    return render_template("login.html")





@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))

    resp = supabase.table("user").select("*").eq("username", session["username"]).execute()
    user = resp.data[0] if resp.data else None

    profile_pic_url = None
    username = session.get("username")

    # Path to local profile pics folder (relative to app root)
    local_folder = os.path.join(current_app.root_path, "static", "images", "profile_pics")

    # Try to find a local profile pic for this user with common image extensions
    local_file = None
    for ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
        potential_path = os.path.join(local_folder, f"{username}.{ext}")
        if os.path.exists(potential_path):
            local_file = f"/static/images/profile_pics/{username}.{ext}"
            break

    if local_file:
        profile_pic_url = local_file  # Serve from local static folder
    else:
        # Fallback to Supabase storage URL if user has profile_pic in DB
        if user and user.get("profile_pic"):
            filename = user["profile_pic"]
            if not filename.startswith("profile_pics/"):
                filename = f"profile_pics/{filename}"
            profile_pic_url = f"https://rhmvmrqkkhnztiequjwf.supabase.co/storage/v1/object/public/{filename}"
        else:
            # Optional: fallback to a default avatar image
            profile_pic_url = url_for('static', filename='images/default_avatar.png')

    return render_template(
        "dashboard.html",
        role=session.get("role"),
        username=username,
        profile_pic_url=profile_pic_url
    )




@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))






# ---------------- USER MANAGEMENT ROUTES ----------------
# -------------------------
# USER ROUTES
# -------------------------

@app.route('/create_user', methods=['POST'])
def create_user():
    if 'username' not in session or session['role'] != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('dashboard'))

    username = request.form['new_username']
    password = request.form['new_password']
    role = request.form['new_role']

    # Check if user exists
    resp = supabase.table("user").select("*").eq("username", username).execute()
    if resp.data and len(resp.data) > 0:
        flash('Username already exists!', 'danger')
        return redirect(url_for('dashboard'))

    # Insert new user
    hashed_password = generate_password_hash(password)
    supabase.table("user").insert({
        "username": username,
        "password": hashed_password,
        "role": role
    }).execute()

    flash('User created successfully!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/manage_accounts')
def manage_accounts():
    if 'username' not in session or session['role'] != 'admin':
        flash('Access denied!', 'danger')
        return redirect(url_for('dashboard'))

    resp = supabase.table("user").select("*").execute()
    user = resp.data if resp.data else []

    return render_template('manage_accounts.html', user=user, current_user=session['username'])


@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if 'username' not in session or session['role'] != 'admin':
        return jsonify({'error': 'Access denied!'}), 403

    # Prevent deleting self
    user_resp = supabase.table("user").select("*").eq("id", user_id).execute()
    user_data = user_resp.data[0] if user_resp.data else None
    if not user_data:
        return jsonify({'error': 'User not found!'}), 404
    if user_data['username'] == session['username']:
        return jsonify({'error': 'You cannot delete your own account!'}), 400

    supabase.table("user").delete().eq("id", user_id).execute()
    return jsonify({'success': True})

# --- PROFILE PICTURE UPLOAD ---

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
    return render_template(
    "premise_data.html",
    regions=regions,
    categories=categories,
    role=session.get("role", "user") 
)





@app.route('/get_premises', methods=['GET'])
def get_premises():
    if 'role' not in session:
        return jsonify([])

    resp = supabase.table("premises").select("*").execute()
    return jsonify(resp.data)


@app.route('/save_premise', methods=['POST'])
def save_premise():
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Clean fields
    def format_title_case(s):
        return s.strip().title() if s else ''

    name = format_title_case(data.get('name'))
    category_name = data.get('category', '').strip()
    region = data.get('region', '').strip()
    district = data.get('district', '').strip()
    location = format_title_case(data.get('location'))
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if not all([name, category_name, region, district]):
        return jsonify({'error': 'Missing required fields'}), 400

    premise_id = data.get('id')

    if premise_id:  # update
        resp = supabase.table("premises").update({
            "name": name,
            "category": category_name,
            "region": region,
            "district": district,
            "location": location,
            "latitude": latitude,
            "longitude": longitude
        }).eq("id", premise_id).execute()
    else:  # insert
        resp = supabase.table("premises").insert({
            "name": name,
            "category": category_name,
            "region": region,
            "district": district,
            "location": location,
            "latitude": latitude,
            "longitude": longitude
        }).execute()

    return jsonify({'success': True, 'data': resp.data})


@app.route('/delete_premise/<int:premise_id>', methods=['DELETE'])
def delete_premise(premise_id):
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    try:
        # Delete from Supabase
        resp = supabase.table("premises").delete().eq("id", premise_id).execute()

        if not resp.data:
            return jsonify({'error': 'Premise not found'}), 404

        # Fetch updated premises from Supabase to sync JSON
        synced_resp = supabase.table("premises").select("*").execute()
        all_premises = synced_resp.data if synced_resp.data else []

        # Save to premises.json
        premises_file = os.path.join(current_app.root_path, "static", "data", "premises.json")
        with open(premises_file, "w", encoding="utf-8") as f:
            json.dump(all_premises, f, indent=4, ensure_ascii=False)

        return jsonify({'success': True})

    except Exception as e:
        print("Error deleting premise:", e)
        return jsonify({'success': False, 'message': str(e)}), 500



@app.route('/save_location', methods=['POST'])
def save_location():
    # Check if user is logged in
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    # Detect device type via User-Agent
    user_agent = request.headers.get('User-Agent', '').lower()
    mobile_keywords = ['iphone', 'android', 'ipad', 'ipod', 'mobile']
    if not any(keyword in user_agent for keyword in mobile_keywords):
        return jsonify({
            'error': 'Location can only be recorded from a mobile device. Please use a mobile device.'
        }), 403

    # Get request data
    data = request.get_json()
    premise_id = data.get('id')
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if premise_id is None or latitude is None or longitude is None:
        return jsonify({'error': 'Missing data'}), 400

    # Update premise in Supabase
    resp = supabase.table("premises").update({
        "latitude": latitude,
        "longitude": longitude
    }).eq("id", premise_id).execute()

    if not resp.data:
        return jsonify({'error': 'Premise not found'}), 404

    return jsonify({'success': True, 'updated': resp.data})









# Path to local JSON
PREMISES_FILE = "premises.json"
PARAMS_FILE = "static/data/observation_parameters.json"

# Helper: ensure premises file exists and synced with Supabase
def load_premises_file():
    data_dir = os.path.join(current_app.root_path, "static", "data")
    os.makedirs(data_dir, exist_ok=True)  # ensure folder exists
    premises_file = os.path.join(data_dir, PREMISES_FILE)

    # If file is missing or empty, fetch from Supabase
    if not os.path.exists(premises_file) or os.path.getsize(premises_file) == 0:
        try:
            resp = supabase.table("premises").select("*").execute()
            premises = resp.data if resp.data else []
        except Exception as e:
            print("Error fetching premises from Supabase:", e)
            premises = []

        # Save local JSON
        with open(premises_file, "w", encoding="utf-8") as f:
            json.dump(premises, f, indent=4, ensure_ascii=False)
        return premises, premises_file

    # Load JSON safely if it exists
    with open(premises_file, "r", encoding="utf-8") as f:
        try:
            premises = json.load(f)
        except json.JSONDecodeError:
            # If file is invalid, fetch from Supabase
            try:
                resp = supabase.table("premises").select("*").execute()
                premises = resp.data if resp.data else []
            except Exception as e:
                print("Error fetching premises from Supabase:", e)
                premises = []

            # Save corrected local JSON
            with open(premises_file, "w", encoding="utf-8") as fw:
                json.dump(premises, fw, indent=4, ensure_ascii=False)

    return premises, premises_file


@app.route('/save_observation', methods=['POST'])
def save_observation():
    if 'role' not in session:
        return jsonify({'error': 'Access denied!'}), 403

    data = request.get_json()
    premise_id = data.get('premiseId')
    obs_date = data.get('date')
    obs_data = data.get('defects') or []
    defect_values = data.get('defectValues') or {}
    none_selected = data.get('none', False)
    filter_district = data.get('district')

    if not premise_id or not obs_date:
        return jsonify({'success': False, 'message': 'Missing required fields'}), 400

    # Load observation parameters
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        obs_config = json.load(f)

    # Map frontend keys → parameter keys
    frontend_to_param = {
        "obsGot": "got",
        "obsUnreg": "unreg",
        "obsPersonnel": "personnel",
        "obsRequirements": "requirements",
        "obsUnregPremise": "unregPremise",
        "obsMedicalPractices": "medicalPractices",
        "obsDldmNotAllowed": "dldmNotAllowed"
    }

    obs_readable = []
    obs_values_saved = {}
    intensity = 0

    if not none_selected:
        for obs_key in obs_data:
            param_key = frontend_to_param.get(obs_key)
            if not param_key:
                continue
            param_info = obs_config["parameters"].get(param_key)
            if not param_info:
                continue
            obs_readable.append(param_info["label"])
            intensity += param_info.get("intensity", 0)
            raw_value = defect_values.get(obs_key)
            if raw_value:
                try:
                    cleaned = ''.join(filter(str.isdigit, str(raw_value)))
                    obs_values_saved[param_key] = int(cleaned) if cleaned else 0
                except:
                    obs_values_saved[param_key] = 0

    # Calculate PVI
    pvi_raw = sum((obs_values_saved.get(prod,0) * (conf.get("weight",0)/100))
                  for prod, conf in obs_config.get("weights", {}).items())
    total_policy_max = sum((conf.get("max_policy",0) or 0) * ((conf.get("weight",0) or 0)/100)
                           for conf in obs_config.get("weights", {}).values())
    absolute_pvi = round((pvi_raw / total_policy_max * 100),2) if total_policy_max>0 else 0

    # Load or recreate premises JSON (always synced)
    premises, premises_file = load_premises_file()
    premise = next((p for p in premises if p.get('id') == premise_id), None)

    if not premise:
        # If not in JSON → create it (fetch from Supabase if available)
        try:
            resp = supabase.table("premises").select("*").eq("id", premise_id).execute()
            premise_data = resp.data[0] if resp.data else {}
        except Exception as e:
            print("Error fetching premise from Supabase:", e)
            premise_data = {}

        premise = {
            "id": premise_id,
            "name": premise_data.get("name", f"Premise {premise_id}"),
            "category": premise_data.get("category", "Unknown"),
            "region": premise_data.get("region", "Unknown"),
            "district": premise_data.get("district", "Unknown"),
            "location": premise_data.get("location", "Unknown"),
            "latitude": premise_data.get("latitude",""),
            "longitude": premise_data.get("longitude",""),
            "observations": []
        }
        premises.append(premise)

    # Append observation
    premise['observations'].append({
        'date': obs_date,
        'observations': obs_readable if not none_selected else ["None"],
        'defect_values': obs_values_saved,
        'intensity': intensity,
        'pvi_raw': round(pvi_raw,2),
        'absolute_pvi': absolute_pvi
    })

    # Update totals & averages
    num_obs = len(premise['observations'])
    premise['total_intensity'] = sum(o.get('intensity',0) for o in premise['observations'])
    premise['average_intensity'] = round(premise['total_intensity']/num_obs,2) if num_obs>0 else 0
    premise['total_pvi_raw'] = round(sum(o.get('pvi_raw',0) for o in premise['observations']),2)
    premise['average_pvi_raw'] = round(premise['total_pvi_raw']/num_obs,2) if num_obs>0 else 0
    premise['total_absolute_pvi'] = round(sum(o.get('absolute_pvi',0) for o in premise['observations']),2)
    premise['average_absolute_pvi'] = round(premise['total_absolute_pvi']/num_obs,2) if num_obs>0 else 0

    # Update relative PVI
    relative_set = [p for p in premises if (p.get('district')==filter_district) or not filter_district]
    max_total_pvi_raw = max(sum(o.get('pvi_raw',0) for o in p.get('observations',[])) for p in relative_set) or 1
    for p in relative_set:
        total_pvi = sum(o.get('pvi_raw',0) for o in p.get('observations',[]))
        p['relative_pvi'] = round((total_pvi/max_total_pvi_raw)*100,2)

    # Violation rate
    violation_config = obs_config.get("violation",{})
    intensity_weight = violation_config.get("non_conformance",70)
    absolute_pvi_weight = violation_config.get("Pvi",30)
    avg_intensity = premise['average_intensity']
    avg_absolute_pvi = premise['average_absolute_pvi']
    premise['violation_rate'] = round((avg_intensity*intensity_weight/100)+(avg_absolute_pvi*absolute_pvi_weight/100),2)
    premise['relative_violation_rate'] = round((avg_intensity*intensity_weight/100)+(premise.get('relative_pvi',0)*absolute_pvi_weight/100),2)

    # Save to Supabase + regenerate local premises.json
    try:
        supabase.table("premises").upsert(premise).execute()

        # Fetch all premises from Supabase and overwrite local JSON
        resp = supabase.table("premises").select("*").execute()
        all_premises = resp.data if resp.data else []
        with open(premises_file, "w", encoding="utf-8") as f:
            json.dump(all_premises, f, indent=4, ensure_ascii=False)

    except Exception as e:
        print("Error saving to Supabase:", e)

    return jsonify({'success': True})











@app.route('/recalculate_all', methods=['POST'])
def recalculate_all():
    # ✅ Only admin can run recalculation
    if session.get('role') != 'admin':
        return jsonify({'error': 'Access denied!'}), 403

    # --- Load observation parameters ---
    try:
        with open("static/data/observation_parameters.json", "r", encoding="utf-8") as f:
            obs_config = json.load(f)
    except Exception as e:
        return jsonify({'success': False, 'message': f"Error loading observation parameters: {e}"}), 500

    weights_config = obs_config.get("weights", {})
    violation_config = obs_config.get("violation", {})
    intensity_weight = violation_config.get("non_conformance", 70)
    absolute_pvi_weight = violation_config.get("Pvi", 30)

    # --- Fetch all premises from Supabase ---
    resp = supabase.table("premises").select("*").execute()
    premises = resp.data
    if not premises:
        return jsonify({'success': False, 'message': 'No premises found in database'}), 404

    max_total_pvi_raw_global = 0

    # --- First pass: calculate totals and track max PVI raw ---
    for premise in premises:
        total_pvi_raw = 0
        total_absolute_pvi = 0
        total_intensity = 0

        for obs in premise.get('observations', []):
            defect_values = obs.get('defect_values', {})
            obs_intensity = 0

            # Calculate intensity dynamically
            for param_key, param_info in obs_config.get('parameters', {}).items():
                if param_key in defect_values or param_key in obs.get('observations', []):
                    obs_intensity += param_info.get('intensity', 0)
            obs['intensity'] = obs_intensity
            total_intensity += obs_intensity

            # Calculate pvi_raw
            pvi_raw = 0
            for product, conf in weights_config.items():
                weight = conf.get("weight", 0)
                value = defect_values.get(product, 0) or 0
                if weight > 0 and value > 0:
                    pvi_raw += (weight / 100.0) * value
            obs['pvi_raw'] = round(pvi_raw, 2)
            total_pvi_raw += pvi_raw

            # Absolute PVI
            total_policy_max = sum((conf.get("max_policy", 0) or 0) * ((conf.get("weight", 0) or 0)/100)
                                   for conf in weights_config.values())
            obs['absolute_pvi'] = round((pvi_raw / total_policy_max * 100), 2) if total_policy_max > 0 else 0
            total_absolute_pvi += obs['absolute_pvi']

        num_obs = len(premise.get('observations', []))
        premise['total_intensity'] = total_intensity
        premise['average_intensity'] = round(total_intensity / num_obs, 2) if num_obs > 0 else 0
        premise['total_pvi_raw'] = round(total_pvi_raw, 2)
        premise['average_pvi_raw'] = round(total_pvi_raw / num_obs, 2) if num_obs > 0 else 0
        premise['total_absolute_pvi'] = round(total_absolute_pvi, 2)
        premise['average_absolute_pvi'] = round(total_absolute_pvi / num_obs, 2) if num_obs > 0 else 0

        max_total_pvi_raw_global = max(max_total_pvi_raw_global, total_pvi_raw)

    # --- Second pass: relative values and violation rates ---
    for premise in premises:
        total_pvi_raw = sum(o.get('pvi_raw', 0) for o in premise.get('observations', []))
        premise['relative_pvi'] = round((total_pvi_raw / max_total_pvi_raw_global) * 100, 2) if max_total_pvi_raw_global > 0 else 0

        avg_intensity = premise['average_intensity']
        avg_absolute_pvi = premise['average_absolute_pvi']
        relative_pvi = premise['relative_pvi']

        premise['violation_rate'] = round(
            (avg_intensity * intensity_weight / 100) +
            (avg_absolute_pvi * absolute_pvi_weight / 100),
            2
        )

        premise['relative_violation_rate'] = round(
            (avg_intensity * intensity_weight / 100) +
            (relative_pvi * absolute_pvi_weight / 100),
            2
        )

        # === Push updates to Supabase ===
        supabase.table("premises").update({
            "observations": premise.get("observations", []),
            "total_intensity": premise['total_intensity'],
            "average_intensity": premise['average_intensity'],
            "total_pvi_raw": premise['total_pvi_raw'],
            "average_pvi_raw": premise['average_pvi_raw'],
            "total_absolute_pvi": premise['total_absolute_pvi'],
            "average_absolute_pvi": premise['average_absolute_pvi'],
            "relative_pvi": premise['relative_pvi'],
            "violation_rate": premise['violation_rate'],
            "relative_violation_rate": premise['relative_violation_rate']
        }).eq("id", premise["id"]).execute()

    return jsonify({'success': True, 'message': 'All premises recalculated and updated in Supabase'})

from flask import request, jsonify

@app.route("/recalculate_all_data", methods=["POST"])
def recalculate_all_data():
    try:
        params = request.get_json()
        if not params:
            return jsonify({"success": False, "error": "No parameters received"}), 400

        # TODO: call your recalculation logic here
        # Example:
        # result = recalc_all_inspections(params)
        # For now, just simulate:
        print("Recalculating all data with parameters:", params)

        # Return success
        return jsonify({"success": True})
    except Exception as e:
        print("Error during recalculation:", e)
        return jsonify({"success": False, "error": str(e)}), 500



@app.route('/get_observations/<int:premise_id>', methods=['GET'])
def get_observations(premise_id):
    if 'role' not in session:
        return jsonify([])

    # === Fetch premise from Supabase ===
    resp = supabase.table("premises").select("observations").eq("id", premise_id).execute()
    if not resp.data:
        return jsonify([])

    premise = resp.data[0]
    observations = premise.get("observations") or []

    # === Format defect values ===
    processed_obs = []
    for obs in observations:
        obs_copy = obs.copy()
        formatted_obs = []

        defect_values = obs.get("defect_values", {})
        for defect in obs.get("observations", []):
            value = None
            if "GOT Medicines" in defect:
                value = defect_values.get("got")
            elif "Unregistered Medicines" in defect:
                value = defect_values.get("unreg")
            elif "DLDM NOT ALLOWED Medicines" in defect:
                value = defect_values.get("dldmNotAllowed")

            if value is not None:
                formatted_obs.append(f"{defect} (Tsh {value:,}/=)")
            else:
                formatted_obs.append(defect)

        obs_copy["observations"] = formatted_obs
        processed_obs.append(obs_copy)

    return jsonify(processed_obs)


PARAMS_FILE = "static/data/observation_parameters.json"


# Load parameters
@app.route("/get_parameters")
def get_parameters():
    if 'role' not in session or session['role'] != 'admin':
        return jsonify({"error": "Access denied"}), 403
    
    with open(PARAMS_FILE, "r", encoding="utf-8") as f:
        params = json.load(f)
    return jsonify(params)


# Save updated parameters
@app.route("/save_parameters", methods=["POST"])
def save_parameters():
    if 'role' not in session or session['role'] != 'admin':
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    # ===== Validate 'parameters' intensities =====
    parameters = data.get("parameters", {})
    for key, val in parameters.items():
        intensity = val.get("intensity")
        try:
            intensity_int = int(float(intensity))
            if intensity_int < 0:
                return jsonify({"error": f"Intensity for {key} must be >= 0"}), 400
            val["intensity"] = intensity_int
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid intensity for {key}: {intensity}"}), 400

    # ===== Validate 'weights' =====
    weights = data.get("weights", {})
    for key, val in weights.items():
        # Weight %
        weight = val.get("weight")
        try:
            weight_int = int(float(weight))
            if weight_int < 0:
                return jsonify({"error": f"Weight for {key} must be >= 0"}), 400
            val["weight"] = weight_int
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid weight for {key}: {weight}"}), 400

        # Max policy value
        max_policy = val.get("max_policy")
        try:
            max_policy_int = int(float(max_policy))
            if max_policy_int < 0:
                return jsonify({"error": f"Max policy for {key} must be >= 0"}), 400
            val["max_policy"] = max_policy_int
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid max policy for {key}: {max_policy}"}), 400

    # ===== Validate 'violation' =====
    violation = data.get("violation", {})
    for key in ["non_conformance", "Pvi"]:
        val = violation.get(key)
        try:
            val_int = int(float(val))
            if val_int < 0 or val_int > 100:
                return jsonify({"error": f"{key} must be between 0 and 100"}), 400
            violation[key] = val_int
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid value for {key}: {val}"}), 400

    # Save the full structure
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    return jsonify({"success": True})



















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

    # --- Check if summary exists in Supabase ---
    summary = None
    if inspection_name:
        resp = supabase.table("inspection_summary").select("*").eq("inspection_name", inspection_name).execute()
        summary = resp.data[0] if resp.data else None

    if not summary:
        inspection_name = f"{inspection_type} - {region} - {district} - {date_obj.strftime('%Y%m%d')}"
        # ensure unique
        count = 1
        while True:
            resp = supabase.table("inspection_summary").select("*").eq("inspection_name", inspection_name).execute()
            if not resp.data:
                break
            inspection_name = f"{inspection_type} - {region} - {district} - {date_obj.strftime('%Y%m%d')}_{count}"
            count += 1

        resp = supabase.table("inspection_summary").insert({
            "inspection_name": inspection_name,
            "inspection_type": inspection_type,
            "region": region,
            "district": district,
            "finalized": False,
            "inspection_date": date_obj.isoformat(),
            "recall_product_data": recall_data
        }).execute()
        summary = resp.data[0]
    else:
        # update recall products
        existing_products = summary.get("recall_product_data", {}) or {}
        merged = {**existing_products, **recall_data}
        resp = supabase.table("inspection_summary").update({
            "recall_product_data": merged
        }).eq("id", summary["id"]).execute()
        summary = resp.data[0]

    # --- Create daily inspection ---
    resp = supabase.table("inspection").insert({
        "summary_id": summary["id"],
        "date": date_obj.isoformat(),
        "premises_data": premises_data,
        "defects_data": defects_data,
        "recall_product_data": recall_data,
        "charges_data": {
            'total': total_charges,
            'got_value': got_value,
            'unregistered_value': unregistered_value,
            'dldm_value': dldm_value
        }
    }).execute()
    daily = resp.data[0]

    # --- Aggregate totals ---
    resp = supabase.table("inspection").select("*").eq("summary_id", summary["id"]).execute()
    daily_all = resp.data

    total_defects_agg = defaultdict(int)
    total_premises = 0
    val_got = val_unreg = val_dldm = val_total = 0

    for d in daily_all:
        defects = d.get("defects_data", {}) or {}
        for k, v in defects.items():
            if isinstance(v, int):
                total_defects_agg[k] += v
            elif isinstance(v, dict):
                total_defects_agg[k] += sum(v.values())
        if d.get("premises_data"):
            total_premises += sum(d["premises_data"].values())
        charges = d.get("charges_data", {}) or {}
        val_got += charges.get("got_value", 0)
        val_unreg += charges.get("unregistered_value", 0)
        val_dldm += charges.get("dldm_value", 0)
        val_total += charges.get("total", 0)

    # update summary
    supabase.table("inspection_summary").update({
        "total_premises": total_premises,
        "total_defects": total_defects_agg,
        "value_got_products": val_got,
        "value_unregistered_products": val_unreg,
        "value_dldm_not_allowed": val_dldm,
        "total_charges": val_total,
        "inspection_date": date_obj.isoformat(),
        "finalized": end_flag
    }).eq("id", summary["id"]).execute()

    # ------------------------------
    # Generate JSON after save
    # ------------------------------
    try:
        update_inspections_json()
    except Exception as e:
        print("❌ Failed to update inspections JSON:", e)

    return jsonify({
        'success': True,
        'inspection_name': summary["inspection_name"],
        'summary_id': summary["id"],
        'daily_id': daily["id"]
    })


# ----------------- END INSPECTION -----------------
@app.route('/api/inspection/end', methods=['POST'])
def end_inspection():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    data['end'] = True
    return save_inspection()




# Initialize Supabase client (ensure your keys are set)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/api/inspection/get/<inspection_name>')
def get_inspection(inspection_name):
    """
    Fetches daily inspection data for a given inspection summary by name from Supabase.
    Works for both normal inspections and recall inspections.
    """
    # Fetch summary from Supabase
    resp = supabase.table("inspection_summary").select("*").eq("inspection_name", inspection_name).execute()
    if not resp.data:
        return jsonify({"error": "Inspection not found"}), 404

    summary = resp.data[0]

    # Fetch daily inspections linked to this summary
    resp_daily = supabase.table("inspection").select("*").eq("summary_id", summary["id"]).execute()
    daily_inspections = resp_daily.data or []

    # Optional: group daily inspections by type if needed
    daily_normal_data = {}
    for d in daily_inspections:
        insp_type = summary.get("inspection_type", "Unknown")
        if insp_type not in daily_normal_data:
            daily_normal_data[insp_type] = []
        daily_normal_data[insp_type].append(d)

    # Recall products
    recall_data = summary.get("recall_product_data") or {}

    # Build response
    response = {
        "inspection_name": summary["inspection_name"],
        "inspection_type": summary.get("inspection_type"),
        "region": summary.get("region"),
        "district": summary.get("district"),
        "inspection_date": summary.get("inspection_date"),
        "finalized": summary.get("finalized"),
        "total_premises": summary.get("total_premises"),
        "total_defects": summary.get("total_defects"),
        "value_got_products": summary.get("value_got_products"),
        "value_unregistered_products": summary.get("value_unregistered_products"),
        "value_dldm_not_allowed": summary.get("value_dldm_not_allowed"),
        "total_charges": summary.get("total_charges"),
        "poe_total_charges": summary.get("poe_total_charges"),
        "daily_normal_data": daily_normal_data,
        "recall_product_data": recall_data,
        "daily_inspections": daily_inspections  # optional: raw daily records
    }

    return jsonify(response)




@app.route('/continue_normal_inspection')
def continue_normal_inspection():
    inspection_name = request.args.get('inspection_name')
    region = request.args.get('region')
    district = request.args.get('district')
    inspection_type = request.args.get('inspection_type')

    # 🔹 Fetch the inspection summary from Supabase
    resp = supabase.table("inspection_summary").select("*").eq("inspection_name", inspection_name).execute()
    if not resp.data:
        flash("Inspection not found", "danger")
        return redirect(url_for("dashboard"))

    summary = resp.data[0]

    # 🔹 Get all daily inspection rows linked to this summary
    resp_daily = supabase.table("inspection").select("*").eq("summary_id", summary["id"]).execute()
    daily_entries = resp_daily.data or []

    # 🔹 Define fixed premise categories (for consistency with old logic)
    categories = [
        "Dispensary", "Health Centre", "Polyclinic", "Hospital",
        "Medical Lab (Private)", "Medical Lab (GOT)", "Pharmacy (Human)", "Pharmacy (Vet)",
        "DLDM (Human)", "DLDM (Vet)", "Non Medical shops", "Ware House", "Arbitary Sellers"
    ]
    categories_data = [{"id": idx + 1, "name": category} for idx, category in enumerate(categories)]

    return render_template(
        'continue_normal_inspection.html',
        inspection_name=summary["inspection_name"],
        region=summary["region"],
        district=summary["district"],
        inspection_type=summary["inspection_type"],
        daily_entries=daily_entries,   # JSON-serializable
        categories=categories_data
    )





@app.route('/unfinished_inspections')
def unfinished_inspections():
    if 'username' not in session:
        return jsonify([]), 401

    # Fetch unfinished inspections from Supabase
    resp = supabase.table("inspection_summary").select("*").eq("finalized", False).execute()
    unfinished = resp.data or []

    # Build response
    inspections_list = []
    for ins in unfinished:
        inspections_list.append({
            'summary_id': ins.get('id'),
            'inspection_name': ins.get('inspection_name'),
            'region': ins.get('region'),
            'district': ins.get('district'),
            'inspection_type': ins.get('inspection_type')
        })

    return jsonify(inspections_list)





@app.route('/continue_recall_inspection')
def continue_recall_inspection():
    inspection_name = request.args.get('inspection_name')
    region = request.args.get('region')
    district = request.args.get('district')

    # 🔹 Fetch the inspection summary from Supabase
    resp = supabase.table("inspection_summary").select("*").eq("inspection_name", inspection_name).execute()
    if not resp.data:
        flash("Inspection not found", "danger")
        return redirect(url_for("dashboard"))

    summary = resp.data[0]

    # 🔹 Fetch all daily inspections linked to this summary
    resp_daily = supabase.table("inspection").select("*").eq("summary_id", summary["id"]).execute()
    daily_entries = resp_daily.data or []

    # 🔹 Collect recalled products
    recalled_products = []
    for daily in daily_entries:
        recall_data = daily.get("recall_product_data") or {}

        # Handle JSON string case safely
        if isinstance(recall_data, str):
            try:
                recall_data = json.loads(recall_data)
            except json.JSONDecodeError:
                recall_data = {}

        if not isinstance(recall_data, dict):
            recall_data = {}

        products = recall_data.get("recalled_products", [])
        if isinstance(products, list):
            recalled_products.extend(products)

    return render_template(
        'continue_recall_inspection.html',
        inspection_name=summary["inspection_name"],
        region=summary["region"],
        district=summary["district"],
        categories=RECALL_CATEGORIES,
        recalled_products=recalled_products
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

    # --- Check if summary exists in Supabase ---
    resp = supabase.table("inspection_summary").select("*").eq("inspection_name", formatted_name).execute()
    summary = resp.data[0] if resp.data else None

    if not summary:
        # Insert new summary
        resp = supabase.table("inspection_summary").insert({
            "inspection_name": formatted_name,
            "inspection_type": "POE Inspection",
            "region": region,
            "district": district,
            "inspection_date": inspection_date.isoformat(),
            "total_charges": total_charges,
            "finalized": True,
            "recall_product_data": {}  # keep consistent with schema
        }).execute()
        if not resp.data:
            return jsonify({'success': False, 'error': 'Failed to save summary'}), 500
        summary = resp.data[0]
    else:
        # Update existing summary
        resp = supabase.table("inspection_summary").update({
            "inspection_date": inspection_date.isoformat(),
            "total_charges": total_charges,
            "finalized": True,
            "recall_product_data": {}
        }).eq("id", summary["id"]).execute()
        if not resp.data:
            return jsonify({'success': False, 'error': 'Failed to update summary'}), 500
        summary = resp.data[0]

    # --- Insert POE inspection record ---
    resp = supabase.table("inspection").insert({
        "summary_id": summary["id"],
        "date": inspection_date.isoformat(),
        "poe_name": formatted_name,
        "products_confiscated": products_confiscated,
        "poe_total_charges": total_charges,
        "poe_products_data": poe_products,
        "recall_product_data": {}
    }).execute()

    if not resp.data:
        return jsonify({'success': False, 'error': 'Failed to save inspection'}), 500

    # --- Update inspections JSON immediately ---
    try:
        print("🔹 Updating inspections JSON after POE inspection...")
        update_inspections_json()
        print("✅ Inspections JSON updated.")
    except Exception as e:
        print("❌ Failed to update inspections JSON:", e)

    return jsonify({
        'success': True,
        'inspection_name': summary["inspection_name"],
        'summary_id': summary["id"]
    })


















@app.route('/reports/overall_reports')
def overall_reports():
    # --- Fetch inspections from Supabase ---
    inspections_resp = supabase.table("inspection").select("*").execute()
    inspections = inspections_resp.data or []

    # --- Fetch inspection summaries from Supabase ---
    summaries_resp = supabase.table("inspection_summary").select("*").execute()
    summaries = summaries_resp.data or []

    # --- Assign summary objects to each inspection ---
    for insp in inspections:
        summary_id = insp.get("summary_id")
        insp["summary"] = next((s for s in summaries if s["id"] == summary_id), None)

        # Parse POE inspection products JSON
        summary_obj = insp["summary"]
        if summary_obj and summary_obj.get("inspection_type") == 'POE Inspection':
            try:
                insp["poe_products_dict"] = json.loads(insp.get("poe_products_data") or '{}')
            except Exception:
                insp["poe_products_dict"] = {}
        else:
            insp["poe_products_dict"] = {}

    # --- Assign Overall ID and Daily ID ---
    overall_map = {}
    daily_counters = {}
    next_id = 1

    for insp in inspections:
        summary_obj = insp.get("summary")
        if summary_obj:
            name = summary_obj["inspection_name"]
            if name not in overall_map:
                overall_map[name] = next_id
                next_id += 1
            insp["overall_id"] = overall_map[name]

            if insp["overall_id"] not in daily_counters:
                daily_counters[insp["overall_id"]] = 0
            letter = chr(ord('A') + daily_counters[insp["overall_id"]])
            daily_counters[insp["overall_id"]] += 1
            insp["daily_id"] = f"{insp['overall_id']}{letter}"
        else:
            insp["overall_id"] = None
            insp["daily_id"] = None

    # --- Regions & Districts ---
    regions = {
        "Mtwara": ["Mtwara MC", "Mtwara DC", "Masasi DC", "Masasi TC", "Nanyumbu", "Newala TC", "Newala DC", "Tandahimba", "Nanyamba"],
        "Lindi": ["Lindi MC", "Kilwa", "Nachingwea", "Liwale", "Ruangwa", "Mtama"],
        "Ruvuma": ["Songea MC", "Songea DC", "Mbinga TC", "Mbinga DC", "Madaba", "Nyasa", "Namtumbo", "Tunduru"]
    }

    all_districts = [{"name": d, "region": r} for r, districts in regions.items() for d in districts]

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
        districts=all_districts,
        inspection_types=inspection_types,
        request=request
    )





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

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Invalid file type'}), 400

    # Max file size: 5MB
    file.seek(0, os.SEEK_END)
    if file.tell() > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'File exceeds 5MB limit'}), 400
    file.seek(0)

    filename = f'report_{report_id}_{file.filename}'
    file_bytes = file.read()

    try:
        # Try removing existing file with the same name (ignore errors)
        try:
            supabase.storage.from_('reports').remove([filename])
        except Exception:
            pass

        # ✅ Upload file (this is where exception might happen)
        supabase.storage.from_('reports').upload(filename, file_bytes)

        # ✅ Update the DB to link the report
        update_response = supabase.table('inspection_summary') \
            .update({'official_report': filename}) \
            .eq('id', report_id) \
            .execute()

        # ✅ Check for DB update error
        if hasattr(update_response, 'error') and update_response.error:
            return jsonify({'success': False, 'error': str(update_response.error)}), 500

        # ✅ Get public download URL
        download_url = supabase.storage.from_('reports').get_public_url(filename)

        return jsonify({'success': True, 'download_url': download_url}), 200

    except Exception as e:
        return jsonify({'success': False, 'error': f'Exception during upload: {str(e)}'}), 500


@app.route('/download_report/<filename>')
def download_report(filename):
    try:
        # Get a signed download URL (expires in 1 hour)
        res = supabase.storage.from_('reports').create_signed_url(filename, 3600)
        signed_url = res.get('signedURL')

        if not signed_url:
            flash("Failed to generate download link.")
            return redirect(request.referrer)

        return redirect(signed_url)  # Redirects to actual download link
    except Exception as e:
        flash(f"Error: {str(e)}")
        return redirect(request.referrer)


@app.route('/delete_inspection/<int:summary_id>', methods=['DELETE'])
def delete_inspection(summary_id):
    try:
        # Fetch report to get filename before deletion
        summary_resp = supabase.table("inspection_summary").select("official_report").eq("id", summary_id).single().execute()
        summary_data = summary_resp.data
        if not summary_data:
            return jsonify({'success': False, 'error': 'Inspection summary not found'})

        report_file = summary_data.get("official_report")

        # Delete from database
        supabase.table("inspection_summary").delete().eq("id", summary_id).execute()

        # Optional: delete from storage
        if report_file:
            supabase.storage.from_('reports').remove([report_file])

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500





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


@app.route('/help/about', endpoint='help_about')
def help_about():
    return render_template('about_app.html')



@app.route('/help/howto', endpoint='help_howto')
def help_howto():
    return render_template('how_to_use_tool.html')














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







# Save Disposal to Supabase
# Save Disposal to Supabase
@app.route('/save_disposal', methods=['POST'])
def save_disposal():
    data = request.json
    if not data:
        return jsonify({'status': 'error', 'message': 'No data sent'}), 400

    required_fields = ['type', 'region', 'district', 'weight', 'value', 'period_date']
    for i, row in enumerate(data):
        for field in required_fields:
            if row.get(field) in [None, '']:
                return jsonify({
                    'status': 'error',
                    'message': f"Row {i + 1} is missing required field '{field}'"
                }), 400

    try:
        for row in data:
            row_id = row.get('id')
            disposal_id = row.get('disposal_id') or str(uuid.uuid4())

            values = {
                'disposal_id': disposal_id,
                'type': row.get('type'),
                'region': row.get('region'),
                'district': row.get('district'),
                'weight': row.get('weight', 0),
                'value': row.get('value', 0),
                'parent_id': row.get('parent_id'),
                'period_date': row.get('period_date'),
            }

            if row_id:
                # Update existing row by id
                resp = supabase.table('disposal_activity').update(values).eq('id', row_id).execute()
            else:
                # Insert new row
                resp = supabase.table('disposal_activity').insert(values).execute()

            # Safe error check
            if not resp.data:
                return jsonify({'status': 'error', 'message': 'Supabase insert/update failed.'}), 500

        # Refresh JSON after saving
        update_inspections_json()
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Delete Disposal from Supabase
# Delete Disposal from Supabase
@app.route('/delete_disposal', methods=['POST'])
def delete_disposal():
    data = request.json
    disposal_id = data.get('disposal_id')

    if not disposal_id:
        return jsonify({'status': 'error', 'message': 'No disposal_id provided'}), 400

    try:
        # Attempt delete
        resp = supabase.table('disposal_activity').delete().eq('disposal_id', disposal_id).execute()

        # Check if any row was actually deleted
        if not resp.data:
            return jsonify({'status': 'error', 'message': f"No matching disposal found with ID: {disposal_id}"}), 404

        # Refresh JSON
        update_inspections_json()
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500








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
# Save QA to Supabase
# Save QA to Supabase
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
        for row in data:
            row_id = row.get('id')
            sample_id = row.get('sample_id') or str(uuid.uuid4())
            values = {
                'sample_id': sample_id,
                'type': row.get('type'),
                'center': row.get('center'),
                'number_of_samples': int(row.get('number_of_samples', 0)),
                'passed': int(row.get('passed', 0)),
                'parent_id': row.get('parent_id'),
                'screening_date': row.get('screening_date'),
            }

            if row_id:
                resp = supabase.table('qa_activity').update(values).eq('id', row_id).execute()
            else:
                resp = supabase.table('qa_activity').insert(values).execute()

            # Check for failure
            if not resp.data:
                return jsonify({'status': 'error', 'message': 'Failed to save QA activity'}), 500

        update_inspections_json()
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Delete QA from Supabase
@app.route('/delete_qa', methods=['POST'])
def delete_qa():
    data = request.json
    sample_id = data.get('sample_id')
    type_ = data.get('type')

    if not sample_id or not type_:
        return jsonify({'status': 'error', 'message': 'sample_id and type are required'}), 400

    try:
        resp = supabase.table('qa_activity').delete().eq('sample_id', sample_id).eq('type', type_).execute()

        # Check if any rows were deleted
        if not resp.data:
            return jsonify({'status': 'error', 'message': 'No matching QA activity found'}), 404

        update_inspections_json()
        return jsonify({'status': 'success'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500













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









@app.route("/parameters")
def parameters():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("dashboard"))  # Block non-admins
    return render_template("parameters.html")


















@app.route("/keepalive")
def keepalive():
    return "OK", 200



# --- Run app ---
if __name__ == "__main__":
    with app.app_context():
        # Initialize database
        init_db()

        # Generate/update inspections JSON at startup
        print("🔹 Generating inspections JSON at startup...")
        try:
            update_inspections_json()
        except Exception as e:
            print("❌ Error generating inspections JSON:", e)

    # Run Flask app
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=False
    )
