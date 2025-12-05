import os
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
import requests
from dotenv import load_dotenv
import logging
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import base64
import secrets

load_dotenv()
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
IMAGES_DIR = os.path.join(os.path.dirname(__file__), 'images')
CSS_DIR = os.path.join(os.path.dirname(__file__), 'css')

limiter = Limiter(
    get_remote_address,
    app=None,
    default_limits=["100 per hour"]
)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static')
app.config['CSRF_COOKIE_SECURE'] = os.getenv('CSRF_COOKIE_SECURE', 'false').lower() == 'true'
CSRF_COOKIE_NAME = 'csrf-token'

def _new_csrf_token():
    return secrets.token_urlsafe(32)

def ensure_csrf_cookie(resp, token=None):
    token = token or request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = _new_csrf_token()
    resp.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=7 * 24 * 60 * 60,
        samesite='Lax',
        secure=app.config['CSRF_COOKIE_SECURE'],
        httponly=False
    )
    return resp

def verify_csrf():
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get('X-CSRF-Token')
    if not cookie_token or not header_token:
        return False
    try:
        return secrets.compare_digest(cookie_token, header_token)
    except Exception:
        return False

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo") 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logo_path = os.path.join(IMAGES_DIR, 'logo.png')
if not os.path.exists(logo_path):
    logger.warning("Expected logo not found at %s", logo_path)

FAVICON_BYTES = base64.b64decode('iVBORw0KGgo=')

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(__file__), 'tutor.html')
    return ensure_csrf_cookie(send_file(html_path))

@app.route('/tutor')
def tutor_main():
    html_path = os.path.join(os.path.dirname(__file__), 'tutor.html')
    return ensure_csrf_cookie(send_file(html_path))

@app.route('/api/csrf', methods=['GET'])
def csrf_token():
    token = request.cookies.get(CSRF_COOKIE_NAME) or _new_csrf_token()
    resp = jsonify({"token": token})
    return ensure_csrf_cookie(resp, token=token)

@app.route('/api/areas', methods=['POST'])
@limiter.limit("5 per minute")  
def get_areas():
    if not verify_csrf():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    if not OPENAI_API_KEY:
        return jsonify({"error": "API key missing"}), 500
    data = request.get_json()
    coach = data.get("coach", "").strip()
    if not coach:
        return jsonify({"areas": []})
    prompt = [
        {"role": "system", "content": (
            f"You are an expert career coach. "
            f"List ONLY the main interview/practice areas for the career: '{coach}'. "
            "Return a valid JSON array with 10 to 30 distinct strings covering the breadth of the role. "
            "No commentary, no explanation, no markdown, no keys, no extra text."
        )},
        {"role": "user", "content": (
            f"Provide 10-30 core interview/practice areas for a '{coach}' candidate. "
            "Output ONLY a JSON array of strings."
        )}
    ]
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL, 
        "messages": prompt,
        "max_tokens": 300,
        "temperature": 0.0 
    }
    try:
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        content = resp.json()["choices"][0]["message"]["content"]
        logger.info(f"OpenAI /api/areas response: {content}")
        import json
        try:
            areas = json.loads(content)
            if isinstance(areas, list):
                return jsonify({"areas": areas})
        except Exception as e:
            logger.warning(f"Direct JSON parse failed: {e}")
            start = content.find("[")
            end = content.find("]", start)
            if start >= 0 and end > start:
                arr = content[start:end+1]
                try:
                    areas = json.loads(arr)
                    if isinstance(areas, list):
                        return jsonify({"areas": areas})
                except Exception as e2:
                    logger.error(f"Fallback array parse failed: {e2}")
        logger.error("Failed to extract areas from OpenAI response.")
        return jsonify({"areas": []})
    except Exception as e:
        logger.error(f"Exception in /api/areas: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/chat', methods=['POST'])
@limiter.limit("5 per minute")  
def chat():
    if not verify_csrf():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    if not OPENAI_API_KEY:
        return jsonify({"error": "API key missing"}), 500

    data = request.get_json()
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL, 
        "messages": data.get("messages"),
        "max_tokens": data.get("max_tokens", 800),
        "temperature": data.get("temperature", 0.2)
    }
    try:
        app.logger.debug("OpenAI payload: %s", payload)
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        app.logger.debug("OpenAI response: %s", resp.text)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/check_area', methods=['POST'])
@limiter.limit("10 per minute")
def check_area():
    if not verify_csrf():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    if not OPENAI_API_KEY:
        return jsonify({"error": "API key missing"}), 500
    data = request.get_json()
    area = data.get("area", "").strip()
    if not area:
        return jsonify({"safe": False, "reason": "No area provided"})
    prompt = [
        {"role": "system", "content": (
            "You are a strict career safety classifier. "
            "Only allow mainstream, legal, and ethical career areas suitable for professional coaching. "
            "If the area is unsafe, illegal, unethical, or inappropriate (e.g., suicide, sex work, criminal activity), respond ONLY with JSON: {\"safe\": false, \"reason\": \"<short reason>\"}. "
            "If the area is safe and appropriate for coaching, respond ONLY with JSON: {\"safe\": true}. "
            "No commentary, no extra text, no markdown."
        )},
        {"role": "user", "content": f"Is the area '{area}' safe and appropriate for career coaching?"}
    ]
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": prompt,
        "max_tokens": 60,
        "temperature": 0.0
    }
    try:
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        content = resp.json()["choices"][0]["message"]["content"]
        import json
        try:
            result = json.loads(content)
            if isinstance(result, dict) and "safe" in result:
                return jsonify(result)
        except Exception as e:
            logger.error(f"Failed to parse safety check JSON: {e}")
        return jsonify({"safe": False, "reason": "Could not verify area safety"})
    except Exception as e:
        logger.error(f"Exception in /api/check_area: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/images/<path:filename>')
def images(filename):
    return send_from_directory(IMAGES_DIR, filename)

@app.route('/css/<path:filename>')
def css(filename):
    return send_from_directory(CSS_DIR, filename)

@app.route('/favicon.ico')
def favicon():
    return Response(FAVICON_BYTES, mimetype='image/png')

limiter.init_app(app)

if __name__ == "__main__":
    app.run(port=3000)