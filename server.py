import os
import requests
from flask import Flask, render_template, request, jsonify, url_for, session, redirect, make_response
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Charge les variables du fichier .env
load_dotenv()

app = Flask(__name__)

# Configuration des sessions Flask
app.config['SESSION_TYPE'] = 'filesystem'
from flask_session import Session
Session(app)

# Récupération sécurisée depuis le .env (avec valeur par défaut aléatoire pour la secret_key si absent)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'webm'}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 Mo

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ⚠️ RÉCUPÉRATION DES SECRETS DEPUIS LE .env
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
TWITCH_REDIRECT_URI = os.getenv('TWITCH_REDIRECT_URI', 'http://localhost:5000/auth/callback')

# 👑 LISTE DES PSEUDOS AUTORISÉS À MODÉRER (en minuscules)
ALLOWED_MODERATORS = ['jaileumor']

moderation_queue = []

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/overlay')
def overlay():
    return render_template('overlay.html')

# --- SYSTEME D'AUTHENTIFICATION TWITCH OAUTH ---

@app.route('/mod')
def mod_panel():
    if 'user' not in session:
        return redirect(url_for('twitch_login'))
    
    if session['user'] not in ALLOWED_MODERATORS:
        return f"<h1>Accès refusé ❌</h1><p>Le compte Twitch <b>{session['user']}</b> n'a pas les droits de modération.</p><a href='/logout'>Se déconnecter</a>", 403

    # On crée la réponse et on ajoute les en-têtes pour interdire au navigateur de cacher la page
    response = make_response(render_template('mod.html'))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route('/auth/login')
def twitch_login():
    twitch_auth_url = (
        f"https://id.twitch.tv/oauth2/authorize"
        f"?client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={TWITCH_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=user:read:email"
    )
    return redirect(twitch_auth_url)

@app.route('/auth/callback')
def twitch_callback():
    code = request.args.get('code')
    if not code:
        return "Erreur d'authentification Twitch", 400

    token_url = "https://id.twitch.tv/oauth2/token"
    token_data = {
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': TWITCH_REDIRECT_URI
    }
    
    token_res = requests.post(token_url, data=token_data)
    if token_res.status_code != 200:
        return "Échec de l'authentification auprès de Twitch", 500
    
    access_token = token_res.json().get('access_token')

    user_headers = {
        'Client-ID': TWITCH_CLIENT_ID,
        'Authorization': f'Bearer {access_token}'
    }
    user_res = requests.get('https://api.twitch.tv/helix/users', headers=user_headers)
    
    if user_res.status_code != 200:
        return "Impossible de récupérer le profil Twitch", 500

    user_data = user_res.json()['data'][0]
    username = user_data['login'].lower()

    session['user'] = username
    return redirect(url_for('mod_panel'))

@app.route('/logout')
def logout():
    # 1. Vide la session
    session.clear()
    
    # 2. Crée une redirection et force le navigateur à supprimer le cookie de session
    response = redirect(url_for('index'))
    response.delete_cookie('session') # Supprime le cookie de session Flask
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# --- FIN AUTHENTIFICATION ---

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'Aucun fichier envoyé'}), 400
    
    file = request.files['file']
    username = request.form.get('username', 'Anonyme').strip()
    message = request.form.get('message', '').strip()

    if file.filename == '' :
        return jsonify({'error': 'Nom de fichier vide'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        import uuid
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)

        file_ext = filename.rsplit('.', 1)[1].lower()
        file_type = 'video' if file_ext in {'mp4', 'webm'} else 'image'

        alert_item = {
            'id': uuid.uuid4().hex,
            'url': url_for('static', filename=f'uploads/{unique_filename}'),
            'username': username if username else 'Anonyme',
            'message': message,
            'type': file_type
        }

        moderation_queue.append(alert_item)
        socketio.emit('update_queue', moderation_queue)

        return jsonify({'success': True, 'message': 'Média envoyé avec succès !'})

    return jsonify({'error': 'Format de fichier non autorisé'}), 400

@app.route('/api/queue', methods=['GET'])
def get_queue():
    return jsonify(moderation_queue)

@socketio.on('moderate_action')
def handle_moderation(data):
    action = data.get('action')
    item_id = data.get('id')

    global moderation_queue
    target_item = next((item for item in moderation_queue if item['id'] == item_id), None)

    if target_item:
        moderation_queue = [item for item in moderation_queue if item['id'] != item_id]
        socketio.emit('update_queue', moderation_queue)

        if action == 'accept':
            socketio.emit('trigger_alert', target_item)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)