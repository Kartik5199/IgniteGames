import os
import bcrypt
import jwt
import datetime
from flask import Flask, request, jsonify, Blueprint, render_template, make_response, redirect
from bson import ObjectId
from bson.errors import InvalidId
from pytz import timezone, utc
from flask_cors import CORS
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, create_access_token
from pymongo import MongoClient
from dotenv import load_dotenv
import razorpay

# Load environment variables
load_dotenv()

# =======================
# 🔹 CONFIGURATION
# =======================
class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    MONGO_URI = os.getenv('MONGO_URI')
    RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")


# =======================
# INITIALIZE FLASK APP & DATABASE
# =======================
app = Flask(__name__)
CORS(app)
app.config.from_object(Config)

# Razorpay client
razorpay_client = razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))


# ✅ JWT configuration
app.config["JWT_TOKEN_LOCATION"] = ["headers"]
app.config["JWT_HEADER_NAME"] = "Authorization"
app.config["JWT_HEADER_TYPE"] = "Bearer"
jwt = JWTManager(app)

# ✅ MongoDB Client
client = MongoClient(Config.MONGO_URI)
db = client.get_database("ignitegames")

# ✅ Test MongoDB connection
try:
    client.admin.command('ping')
    print("✅ Connected to MongoDB Atlas!")
except Exception as e:
    print("❌ Failed to connect to MongoDB:", e)


# =======================
# 🔹 UTILITIES
# =======================
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

def generate_token(email):
    payload = {
        "email": email,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=1)
    }
    return jwt.encode(payload, Config.SECRET_KEY, algorithm="HS256")


# =======================
# 🔹 USER MODEL
# =======================
class User:
    @staticmethod
    def create_user(data):
        data['password'] = hash_password(data['password'])
        return db.users.insert_one(data)

    @staticmethod
    def find_user(email):
        return db.users.find_one({"email": email})


# =======================
# 🔹 ROUTES BLUEPRINT
# =======================
routes_bp = Blueprint('routes', __name__)

# Home and Static Pages
@routes_bp.route('/')
def home():
    return render_template('index.html')

@routes_bp.route('/login')
def login_page():
    return render_template('login.html')

@routes_bp.route('/register')
def register_page():
    return render_template('register.html')

@routes_bp.route('/catalog')
def catalog():
    return no_cache_response(render_template('catalog.html'))

@routes_bp.route('/checkout')
def checkout():
    return no_cache_response(render_template('checkout.html'))

@routes_bp.route('/product')
def product():
    return no_cache_response(render_template('product.html'))

@routes_bp.route('/profile')
def profile_page():
    return no_cache_response(render_template('profile.html'))

@routes_bp.route('/contact')
def contact():
    return no_cache_response(redirect("mailto:your-email@example.com"))

@routes_bp.route('/payments')
def payments_page():
    return no_cache_response(render_template("payments.html",razorpay_key_id=Config.RAZORPAY_KEY_ID))

def no_cache_response(response):
    if not isinstance(response, (str, bytes)):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# =======================
# 🔹 AUTH ROUTES
# =======================
@routes_bp.route('/auth/register', methods=['POST'])
def register():
    data = request.json
    if User.find_user(data['email']):
        return jsonify({"message": "User already exists"}), 400

    User.create_user(data)
    return jsonify({"message": "User registered successfully"}), 201

@routes_bp.route('/auth/login', methods=['POST'])
def login():
    data = request.json
    user = User.find_user(data['email'])

    if user and verify_password(data['password'], user['password']):
        token = create_access_token(identity=user['email'])
        return jsonify({"token": token}), 200

    return jsonify({"message": "Invalid credentials"}), 401


# =======================
# 🔹 ORDER ROUTES
# =======================
@routes_bp.route('/orders/place', methods=['POST'])
@jwt_required()
def place_order():
    data = request.json
    user_email = get_jwt_identity()

    if not data.get('product_id') or not data.get('quantity'):
        return jsonify({"error": "Missing product_id or quantity"}), 400

    order = {
        "user_email": user_email,
        "product_id": ObjectId(data['product_id']),
        "quantity": data['quantity'],
        "status": "pending",
        "purchase_date": datetime.datetime.utcnow()
    }
    db.orders.insert_one(order)
    return jsonify({"message": "Order placed successfully!"}), 201

@routes_bp.route('/create/order', methods=['POST'])
@jwt_required()
def create_razorpay_order():
    data = request.json
    amount = data.get("amount")  # in rupees
    currency = "INR"

    if not amount:
        return jsonify({"error": "Amount is required"}), 400

    amount_in_paise = int(float(amount) * 100)  # Convert to paise
    razorpay_order = razorpay_client.order.create({
        "amount": amount_in_paise,
        "currency": currency,
        "payment_capture": 1  # Auto capture after payment
    })

    return jsonify({
        "order_id": razorpay_order['id'],
        "amount": razorpay_order['amount'],
        "currency": razorpay_order['currency'],
        "key": Config.RAZORPAY_KEY_ID
    }), 200


@app.route('/payment/success', methods=['POST'])
@jwt_required()
def payment_success():
    data = request.get_json()
    user_email = get_jwt_identity()  
    product_id = data.get("product_id")
    payment_id = data.get("payment_id")
    order_id = data.get("order_id")

    # Convert product_id to ObjectId if necessary
    try:
        product_id = ObjectId(product_id)
    except Exception as e:
        return jsonify({"message": "Invalid product_id format."}), 400

    # Update the order status from "pending" to "completed"
    result = db.orders.update_one(
        {
            "user_email": user_email,  # Match the email
            "product_id": product_id,  # Ensure it's an ObjectId
            "status": "pending"  # Ensure the order is still in pending status
        },
        {
            "$set": {
                "status": "completed",
                "payment_id": payment_id
            }
        }
    )

    # Check if the update was successful
    if result.modified_count == 0:
        return jsonify({"message": "Order not found or already completed."}), 404

    return jsonify({"message": "Payment successful", "redirect_url": f"/download/{str(product_id)}"})



@routes_bp.route('/orders/history', methods=['GET'])
@jwt_required()
def order_history():
    user_email = get_jwt_identity()
    orders = list(db.orders.find({"user_email": user_email}))

    for order in orders:
        try:
            product_id = order['product_id']
            if isinstance(product_id, str):
                product_id = ObjectId(product_id)

            product = db.products.find_one({"_id": product_id})
        except Exception:
            product = None

        if product:
            order['product_name'] = product.get('name', 'Unknown Product')
            order['product_image'] = product.get('image_url', '')
            order['product_price'] = product.get('price', 'N/A')
        else:
            order['product_name'] = 'Unknown Product'
            order['product_image'] = ''
            order['product_price'] = 'N/A'

        order['_id'] = str(order['_id'])
        order['product_id'] = str(order['product_id'])

        # Convert purchase_date to IST
        if order.get("purchase_date"):
            utc_time = order["purchase_date"].replace(tzinfo=utc)
            ist = timezone("Asia/Kolkata")
            local_time = utc_time.astimezone(ist)
            order['purchase_date'] = local_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            order['purchase_date'] = 'N/A'

    return jsonify({"orders": orders}), 200


# =======================
# 🔹 PRODUCT ROUTES
# =======================
@routes_bp.route('/products/add', methods=['POST'])
def add_product():
    data = request.json
    if not all(key in data for key in ["name", "description", "price", "image_url"]):
        return jsonify({"error": "Missing required fields"}), 400

    product = {
        "name": data["name"],
        "description": data["description"],
        "price": data["price"],
        "image_url": data["image_url"]
    }
    db.products.insert_one(product)
    return jsonify({"message": "Product added successfully!"}), 201

@routes_bp.route('/products/all', methods=['GET'])
def get_all_products():
    products = list(db.products.find({}))
    for product in products:
        product['_id'] = str(product['_id'])
    return jsonify({"products": products}), 200

@app.route('/download/<product_id>')
def download_page(product_id):
    try:
        product = db.products.find_one({"_id": ObjectId(product_id)})
    except InvalidId:
        return "Invalid Product ID", 400

    if not product:
        return "Product not found", 404

    return render_template("download.html", product=product)



# =======================
# 🔹 USER ROUTES
# =======================
@routes_bp.route('/users/profile', methods=['GET'])
@jwt_required()
def get_profile():
    user_email = get_jwt_identity()
    user = db.users.find_one({"email": user_email}, {"_id": 0, "password": 0})
    return jsonify({"user": user}), 200

@routes_bp.route('/users/update', methods=['PUT'])
@jwt_required()
def update_profile():
    user_email = get_jwt_identity()
    data = request.json
    update_data = {key: value for key, value in data.items() if key in ["name", "username"]}
    db.users.update_one({"email": user_email}, {"$set": update_data})
    return jsonify({"message": "Profile updated successfully!"}), 200


# =======================
# 🔹 REGISTER BLUEPRINT AND RUN APP
# =======================
app.register_blueprint(routes_bp)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting Flask server on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)

