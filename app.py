# app.py
import os
import json
import time
import re
import hmac
import secrets
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, send_from_directory, abort)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine

# ===================== КОНФИГ =====================

app = Flask(__name__)

# --- SECRET_KEY ---
_sk = os.environ.get('SECRET_KEY')
if not _sk:
    _sk = 'dev-only-fallback-change-me'
    if os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production':
        print('[SECURITY] ⚠ SECRET_KEY не задан в ENV! Сессии уязвимы. Установите SECRET_KEY на Render.')
app.secret_key = _sk

# --- DB (SQLite по умолчанию, Postgres через DATABASE_URL) ---
_db_url = os.environ.get('DATABASE_URL')
if _db_url:
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez.db?timeout=30'
    if os.environ.get('RENDER'):
        print('[WARN] ⚠ SQLite на Render: БД будет теряться при редеплое. Настройте Persistent Disk + DATABASE_URL.')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- Uploads folder (Persistent Disk на Render) ---
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
if os.environ.get('RENDER') and not os.environ.get('UPLOAD_FOLDER'):
    print('[WARN] ⚠ UPLOAD_FOLDER не задан — файлы будут теряться на Render!')

app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

# --- Session hardening ---
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# --- Разрешённые расширения ---
SAFE_INLINE_EXTS = {'png','jpg','jpeg','gif','webp','bmp','mp3','wav','ogg','webm','m4a','mp4'}
DANGEROUS_EXTS = {'php','phtml','php3','php4','php5','php7','phar','exe','dll','bat','cmd','com','scr','msi',
                  'vbs','vbe','js','jse','mjs','html','htm','xhtml','shtml','svg','xml','xsl','xslt','jar','apk'}

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()
    except Exception:
        pass

db = SQLAlchemy(app)

# ===================== МОДЕЛИ =====================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.String(200), default='')
    avatar = db.Column(db.Text, default='')
    typing_to = db.Column(db.Integer, default=0)
    typing_at = db.Column(db.Float, default=0.0)
    invited_by = db.Column(db.Integer, default=0)
    last_seen = db.Column(db.Float, default=0.0)
    onboarded = db.Column(db.Integer, default=0)
    activity = db.Column(db.String(50), default='')

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default='')
    reaction = db.Column(db.String(200), default='')     # legacy, не используется новым кодом
    hidden_for = db.Column(db.String(50), default='')
    timestamp = db.Column(db.String(10), default='')
    expires_at = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.Float, default=0.0)
    read_at = db.Column(db.Float, default=0.0)
    read_by = db.Column(db.String(200), default='')      # legacy, не используется
    pinned = db.Column(db.Integer, default=0)
    reply_to_id = db.Column(db.Integer, default=0)

class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.Float, default=0.0)

class ChatMember(db.Model):
    chat_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, primary_key=True)

class MessageRead(db.Model):
    __tablename__ = 'message_read'
    message_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, primary_key=True)
    read_at = db.Column(db.Float, default=0.0)

class MessageReaction(db.Model):
    __tablename__ = 'message_reaction'
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False)
    emoji = db.Column(db.String(20), nullable=False)
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', name='uq_msg_user_reaction'),)

# ===================== АВТО-МИГРАЦИЯ =====================

def ensure_schema():
    with app.app_context():
        db.create_all()
        try:
            insp = inspect(db.engine)
            if not db.engine.url.drivername.startswith('sqlite'):
                return
            tables = set(insp.get_table_names())

            if 'user' in tables:
                cols = {c['name'] for c in insp.get_columns('user')}
                if 'invited_by' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN invited_by INTEGER DEFAULT 0'))
                if 'last_seen' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN last_seen FLOAT DEFAULT 0.0'))
                if 'onboarded' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN onboarded INTEGER DEFAULT 0'))
                if 'activity' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN activity VARCHAR(50) DEFAULT ""'))

            if 'message' in tables:
                cols = {c['name'] for c in insp.get_columns('message')}
                if 'expires_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN expires_at FLOAT DEFAULT 0.0'))
                if 'created_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN created_at FLOAT DEFAULT 0.0'))
                if 'read_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN read_at FLOAT DEFAULT 0.0'))
                if 'read_by' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN read_by VARCHAR(200) DEFAULT ""'))
                if 'pinned' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN pinned INTEGER DEFAULT 0'))
                if 'reply_to_id' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN reply_to_id INTEGER DEFAULT 0'))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f'[migrate] warning: {e}')

        # --- Бэкфилл: старое поле reaction → MessageReaction ---
        try:
            if db.session.query(MessageReaction).count() == 0:
                rows = Message.query.filter(Message.reaction != '', Message.reaction.isnot(None)).all()
                for m in rows:
                    try:
                        data = json.loads(m.reaction)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(data, dict):
                        for emj, users in data.items():
                            if isinstance(users, list):
                                for uid in users:
                                    try:
                                        db.session.add(MessageReaction(
                                            message_id=m.id, user_id=int(uid), emoji=str(emj)[:20]))
                                    except (ValueError, TypeError):
                                        pass
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f'[migrate] backfill reactions warning: {e}')

        # --- Бэкфилл: старое поле read_by → MessageRead (для групповых) ---
        try:
            if db.session.query(MessageRead).count() == 0:
                rows = Message.query.filter(
                    Message.receiver_id < 0,
                    Message.read_by != '', Message.read_by.isnot(None)
                ).all()
                for m in rows:
                    for uid in m.read_by.split(','):
                        uid = uid.strip()
                        if not uid: continue
                        try:
                            db.session.add(MessageRead(message_id=m.id, user_id=int(uid), read_at=0))
                        except (ValueError, TypeError):
                            pass
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f'[migrate] backfill reads warning: {e}')

ensure_schema()

# ===================== CSRF =====================

@app.before_request
def _ensure_csrf():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)

@app.before_request
def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    # Форма логина/регистрации — традиционный POST, CSRF отключён (нет доступа к login.html)
    if request.path == '/':
        return
    token = session.get('csrf_token', '')
    sent = request.headers.get('X-CSRFToken', '')
    if not token or not sent or not hmac.compare_digest(token, sent):
        return jsonify({'ok': False, 'error': 'csrf'}), 403

# ===================== ХЕЛПЕРЫ =====================

def is_online(u): return bool(u and u.last_seen and (time.time() - u.last_seen) < 30)
def is_member(uid, gid): return ChatMember.query.filter_by(chat_id=gid, user_id=uid).first() is not None

def aggregate_reactions_json(message_id, reactions_by_msg):
    d = reactions_by_msg.get(message_id, {})
    if not d: return ''
    return json.dumps({k: sorted(v) for k, v in sorted(d.items())}, ensure_ascii=False)

# ===================== AUTH =====================

@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session: return redirect(url_for('chat'))
    ref = request.args.get('ref', type=int)
    if ref: session['pending_ref'] = ref
    error = None
    if request.method == 'POST':
        action = request.form.get('action', 'login')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip() or username
        if not username or not password:
            error
