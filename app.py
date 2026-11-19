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
            error = 'Заполните поля'
        elif action == 'register':
            if User.query.filter_by(username=username).first():
                error = 'Логин занят'
            else:
                u = User(username=username, name=name,
                         password=generate_password_hash(password),
                         invited_by=session.get('pending_ref') or 0)
                db.session.add(u); db.session.commit()
                session['uid'] = u.id
                session.pop('pending_ref', None)
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=username).first()
            if u and check_password_hash(u.password, password):
                session['uid'] = u.id
                return redirect(url_for('chat'))
            error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ===================== CHAT =====================

@app.route('/chat')
def chat():
    if 'uid' not in session: return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me:
        session.clear(); return redirect(url_for('index'))

    me.last_seen = time.time(); db.session.commit()

    users = User.query.filter(User.id != me.id).all()
    my_groups = (db.session.query(Chat)
                 .join(ChatMember, ChatMember.chat_id == Chat.id)
                 .filter(ChatMember.user_id == me.id).all())

    gid = request.args.get('g', type=int)
    oid = request.args.get('u', type=int)
    mode, other, group = None, None, None

    if gid and is_member(me.id, gid):
        group = db.session.get(Chat, gid); mode = 'group'
    elif oid:
        other = db.session.get(User, oid)
        mode = 'personal' if other and other.id != me.id else None

    conversations = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        conversations.append({
            'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar,
            'sub': u.bio or ('@' + u.username), 'online': is_online(u),
            'unread': unread,
            'active': (mode == 'personal' and other and other.id == u.id)
        })
    for g in my_groups:
        g_msgs = Message.query.filter_by(receiver_id=-g.id).all()
        if g_msgs:
            msg_ids = [m.id for m in g_msgs]
            read_ids = {r.message_id for r in MessageRead.query.filter(
                MessageRead.user_id == me.id,
                MessageRead.message_id.in_(msg_ids)).all()}
            unread_g = sum(1 for m in g_msgs if m.sender_id != me.id and m.id not in read_ids)
        else:
            unread_g = 0
        conversations.append({
            'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '',
            'sub': 'Групповой чат', 'online': False, 'unread': unread_g,
            'active': (mode == 'group' and group and group.id == g.id)
        })

    return render_template('chat.html', me=me, users=users, other=other, group=group,
                           mode=mode, conversations=conversations, now=time.time())

@app.route('/api/conversations')
def api_conversations():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if not me: return jsonify({'ok': False}), 401
    users = User.query.filter(User.id != me.id).all()
    my_groups = (db.session.query(Chat)
                 .join(ChatMember, ChatMember.chat_id == Chat.id)
                 .filter(ChatMember.user_id == me.id).all())
    out = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        out.append({'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar,
                    'sub': u.bio or ('@' + u.username), 'online': is_online(u), 'unread': unread})
    for g in my_groups:
        g_msgs = Message.query.filter_by(receiver_id=-g.id).all()
        if g_msgs:
            msg_ids = [m.id for m in g_msgs]
            read_ids = {r.message_id for r in MessageRead.query.filter(
                MessageRead.user_id == me.id,
                MessageRead.message_id.in_(msg_ids)).all()}
            unread_g = sum(1 for m in g_msgs if m.sender_id != me.id and m.id not in read_ids)
        else:
            unread_g = 0
        out.append({'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '',
                    'sub': 'Групповой чат', 'online': False, 'unread': unread_g})
    return jsonify({'ok': True, 'conversations': out})

@app.route('/api/me')
def api_me():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if not me: return jsonify({'ok': False}), 401
    invited = User.query.filter_by(invited_by=me.id).count()
    return jsonify({'ok': True, 'invited_count': invited,
                    'invite_link': f'{request.host_url}join/{me.id}'})

# ===================== ПРОФИЛЬ API =====================

@app.route('/api/profile/<int:uid>')
def api_profile(uid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me_id = session['uid']
    user = db.session.get(User, uid)
    if not user: return jsonify({'ok': False}), 404

    msgs = Message.query.filter(
        ((Message.sender_id == me_id) & (Message.receiver_id == uid)) |
        ((Message.sender_id == uid) & (Message.receiver_id == me_id))
    ).order_by(Message.id.desc()).all()

    media, files, audio, links = [], [], [], []
    for m in msgs:
        if m.hidden_for and str(me_id) in m.hidden_for.split(','): continue
        if m.msg_type == 'image': media.append({'id': m.id, 'url': m.content, 'timestamp': m.timestamp})
        elif m.msg_type == 'file': files.append({'id': m.id, 'url': m.content, 'name': m.file_name, 'timestamp': m.timestamp})
        elif m.msg_type == 'audio': audio.append({'id': m.id, 'url': m.content, 'timestamp': m.timestamp})
        elif m.msg_type == 'text':
            for u in re.findall(r'(https?://\S+)', m.content):
                links.append({'id': m.id, 'url': u, 'text': m.content, 'timestamp': m.timestamp})

    return jsonify({
        'ok': True, 'id': user.id, 'name': user.name, 'username': user.username,
        'bio': user.bio, 'avatar': user.avatar, 'online': is_online(user),
        'last_seen': user.last_seen,
        'media': media, 'files': files, 'audio': audio, 'links': links
    })

# ===================== SEND / POLL =====================

@app.route('/send', methods=['POST'])
def send():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    oid = int(data.get('oid', 0))
    content = str(data.get('content', '')).strip()
    reply_to_id = int(data.get('reply_to_id', 0)) or 0
    if not oid or not content: return jsonify({'ok': False}), 400
    if len(content) > 5000: return jsonify({'ok': False, 'error': 'too long'}), 400

    other = db.session.get(User, oid)
    if not other: return jsonify({'ok': False, 'error': 'user not found'}), 404

    if reply_to_id:
        rm = db.session.get(Message, reply_to_id)
        if not rm or {rm.sender_id, rm.receiver_id} != {session['uid'], oid}:
            reply_to_id = 0

    msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type='text',
                  content=content, timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time(), reply_to_id=reply_to_id)
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = 0
        me.last_seen = time.time()
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id})

@app.route('/poll/<int:oid>')
def poll(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    uid = session['uid']
    if uid == oid: return jsonify({'ok': False, 'error': 'self'}), 400
    other = db.session.get(User, oid)
    if not other: return jsonify({'ok': False, 'error': 'user not found'}), 404

    now = time.time()
    me = db.session.get(User, uid)
    if me:
        me.last_seen = now
        db.session.commit()

    unread = Message.query.filter_by(sender_id=oid, receiver_id=uid, read_at=0).all()
    if unread:
        for um in unread: um.read_at = now
        db.session.commit()

    msgs = Message.query.filter(
        ((Message.sender_id == uid) & (Message.receiver_id == oid)) |
        ((Message.sender_id == oid) & (Message.receiver_id == uid))
    ).order_by(Message.id.asc()).all()

    # Reply messages
    reply_ids = [m.reply_to_id for m in msgs if m.reply_to_id]
    reply_map = {}
    if reply_ids:
        for rm in Message.query.filter(Message.id.in_(reply_ids)).all():
            reply_map[rm.id] = rm
    reply_sender_ids = {rm.sender_id for rm in reply_map.values()}
    reply_senders = {}
    if reply_sender_ids:
        for u in User.query.filter(User.id.in_(reply_sender_ids)).all():
            reply_senders[u.id] = u.name

    # Batch reactions
    msg_ids = [m.id for m in msgs]
    reactions_by_msg = {}
    if msg_ids:
        for r in MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids)).all():
            reactions_by_msg.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)

    out = []
    for m in msgs:
        if m.expires_at and m.expires_at < now: continue
        if m.hidden_for and str(uid) in m.hidden_for.split(','): continue
        item = {
            'id': m.id, 'sender_id': m.sender_id, 'msg_type': m.msg_type,
            'content': m.content, 'file_name': m.file_name,
            'reaction': aggregate_reactions_json(m.id, reactions_by_msg),
            'timestamp': m.timestamp, 'created_at': m.created_at,
            'read_at': m.read_at, 'pinned': m.pinned,
            'reply_to_id': m.reply_to_id or 0,
            'read_by_ids': []
        }
        if m.reply_to_id and m.reply_to_id in reply_map:
            rm = reply_map[m.reply_to_id]
            item['reply_to'] = {
                'sender_id': rm.sender_id,
                'sender_name': reply_senders.get(rm.sender_id, 'Аноним'),
                'content_preview': (rm.content or '')[:60].replace('\n', ' '),
                'msg_type': rm.msg_type
            }
        out.append(item)

    members = []
    if me: members.append({'id': me.id, 'name': me.name})
    members.append({'id': other.id, 'name': other.name})

    return jsonify({
        'ok': True, 'messages': out,
        'typing': bool(other.typing_to == uid and (now - (other.typing_at or 0)) < 3.5),
        'other_online': is_online(other),
        'members': members
    })

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = oid
        me.typing_at = time.time()
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/typing_group/<int:gid>', methods=['POST'])
def typing_group(gid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    if not is_member(session['uid'], gid): return jsonify({'ok': False}), 403
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = -gid
        me.typing_at = time.time()
        db.session.commit()
    return jsonify({'ok': True})

# ===================== GROUPS =====================

@app.route('/send_group', methods=['POST'])
def send_group():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    gid = int(data.get('gid', 0))
    content = str(data.get('content', '')).strip()
    reply_to_id = int(data.get('reply_to_id', 0)) or 0
    if not gid or not content or not is_member(session['uid'], gid): return jsonify({'ok': False}), 400
    if len(content) > 5000: return jsonify({'ok': False, 'error': 'too long'}), 400

    if reply_to_id:
        rm = db.session.get(Message, reply_to_id)
        if not rm or rm.receiver_id != -gid:
            reply_to_id = 0

    msg = Message(sender_id=session['uid'], receiver_id=-gid, msg_type='text',
                  content=content, timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time(), reply_to_id=reply_to_id)
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = 0
        me.last_seen = time.time()
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id})

@app.route('/poll_group/<int:gid>')
def poll_group(gid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    if not is_member(session['uid'], gid): return jsonify({'ok': False}), 403

    now = time.time()
    uid = session['uid']
    me = db.session.get(User, uid)
    if me:
        me.last_seen = now
        db.session.commit()

    msgs = Message.query.filter(Message.receiver_id == -gid).order_by(Message.id.asc()).all()
    msg_ids = [m.id for m in msgs]

    member_ids = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
    member_users = User.query.filter(User.id.in_(member_ids)).all() if member_ids else []
    users_map = {u.id: u.name for u in member_users}
    members_data = [{'id': u.id, 'name': u.name} for u in member_users]

    # Reads
    reads_by_msg = {}
    if msg_ids:
        for r in MessageRead.query.filter(MessageRead.message_id.in_(msg_ids)).all():
            reads_by_msg.setdefault(r.message_id, []).append(r.user_id)

    new_reads = []
    for m in msgs:
        if m.sender_id == uid: continue
        if uid in reads_by_msg.get(m.id, []): continue
        new_reads.append(MessageRead(message_id=m.id, user_id=uid, read_at=now))
        reads_by_msg.setdefault(m.id, []).append(uid)

    if new_reads:
        for r in new_reads: db.session.add(r)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            reads_by_msg = {}
            for r in MessageRead.query.filter(MessageRead.message_id.in_(msg_ids)).all():
                reads_by_msg.setdefault(r.message_id, []).append(r.user_id)

    # Reactions
    reactions_by_msg = {}
    if msg_ids:
        for r in MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids)).all():
            reactions_by_msg.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)

    # Reply messages
    reply_ids = [m.reply_to_id for m in msgs if m.reply_to_id]
    reply_map = {}
    if reply_ids:
        for rm in Message.query.filter(Message.id.in_(reply_ids)).all():
            reply_map[rm.id] = rm

    out = []
    for m in msgs:
        if m.expires_at and m.expires_at < now: continue
        if m.hidden_for and str(uid) in m.hidden_for.split(','): continue
        item = {
            'id': m.id, 'sender_id': m.sender_id,
            'sender_name': users_map.get(m.sender_id, 'Аноним'),
            'msg_type': m.msg_type, 'content': m.content, 'file_name': m.file_name,
            'reaction': aggregate_reactions_json(m.id, reactions_by_msg),
            'timestamp': m.timestamp, 'created_at': m.created_at,
            'read_by_ids': sorted(reads_by_msg.get(m.id, [])),
            'pinned': m.pinned,
            'reply_to_id': m.reply_to_id or 0
        }
        if m.reply_to_id and m.reply_to_id in reply_map:
            rm = reply_map[m.reply_to_id]
            item['reply_to'] = {
                'sender_id': rm.sender_id,
                'sender_name': users_map.get(rm.sender_id, 'Аноним'),
                'content_preview': (rm.content or '')[:60].replace('\n', ' '),
                'msg_type': rm.msg_type
            }
        out.append(item)

    typing_users = []
    for mb in ChatMember.query.filter_by(chat_id=gid).all():
        if mb.user_id == uid: continue
        u = db.session.get(User, mb.user_id)
        if u and u.typing_to == -gid and (now - (u.typing_at or 0)) < 3.5:
            typing_users.append(u.name)

    return jsonify({
        'ok': True, 'messages': out,
        'typing': bool(typing_users), 'typing_names': typing_users,
        'members': members_data
    })

@app.route('/create_group', methods=['POST'])
def create_group():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    title = str(data.get('title', '')).strip()
    ids = data.get('member_ids') or []
    if not title or not isinstance(ids, list): return jsonify({'ok': False}), 400
    try:
        ids = [int(x) for x in ids if int(x) != session['uid']]
    except (ValueError, TypeError):
        return jsonify({'ok': False}), 400
    g = Chat(title=title[:100], owner_id=session['uid'], created_at=time.time())
    db.session.add(g); db.session.commit()
    for uid_ in set(ids + [session['uid']]):
        db.session.add(ChatMember(chat_id=g.id, user_id=uid_))
    db.session.commit()
    return jsonify({'ok': True, 'gid': g.id})

@app.route('/leave_group/<int:gid>', methods=['POST'])
def leave_group(gid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    ChatMember.query.filter_by(chat_id=gid, user_id=session['uid']).delete()
    db.session.commit()
    return jsonify({'ok': True})

# ===================== MESSAGE ACTIONS =====================

@app.route('/pin_msg/<int:mid>', methods=['POST'])
def pin_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        msg.pinned = 1 if msg.pinned == 0 else 0
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    oid = request.form.get('oid', type=int)
    gid = request.form.get('gid', type=int)
    f = request.files.get('file')
    if not f or not f.filename: return jsonify({'ok': False}), 400
    if gid and not is_member(session['uid'], gid): return jsonify({'ok': False}), 403
    if not gid and not oid: return jsonify({'ok': False}), 400

    orig_name = secure_filename(f.filename) or 'file'
    ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else ''
    if ext in DANGEROUS_EXTS:
        return jsonify({'ok': False, 'error': 'file type not allowed'}), 400

    saved_name = f"{int(time.time() * 1000)}_{orig_name}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_name))

    mtype = 'image' if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp') \
        else ('audio' if ext in ('webm', 'ogg', 'mp3', 'wav', 'm4a') else 'file')

    msg = Message(sender_id=session['uid'],
                  receiver_id=(-gid if gid else oid),
                  msg_type=mtype,
                  content=url_for('uploaded_file', fn=saved_name),
                  file_name=f.filename,
                  timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time())
    db.session.add(msg); db.session.commit()
    return jsonify({'ok': True})

@app.route('/uploads/<path:fn>')
def uploaded_file(fn):
    base = os.path.abspath(app.config['UPLOAD_FOLDER'])
    target = os.path.abspath(os.path.join(base, fn))
    if not (target == base or target.startswith(base + os.sep)):
        abort(403)
    resp = send_from_directory(base, fn)
    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
    if ext not in SAFE_INLINE_EXTS:
        resp.headers['Content-Disposition'] = f'attachment; filename="{os.path.basename(fn)}"'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    me = db.session.get(User, session['uid'])
    if me:
        if data.get('name'): me.name = str(data.get('name')).strip()[:80]
        me.bio = str(data.get('bio', '')).strip()[:200]
        av = data.get('avatar')
        if av and str(av).startswith('data:image/'):
            me.avatar = av
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/complete_onboarding', methods=['POST'])
def complete_onboarding():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me:
        me.onboarded = 1
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/del_me/<int:mid>/<int:u>', methods=['POST'])
def del_me(mid, u):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        hidden = set(msg.hidden_for.split(',')) if msg.hidden_for else set()
        hidden.discard('')
        hidden.add(str(session['uid']))
        msg.hidden_for = ','.join(hidden)
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/del_all/<int:mid>/<int:u>', methods=['POST'])
def del_all(mid, u):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg and msg.sender_id == session['uid']:
        db.session.delete(msg)
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/react_msg/<int:mid>/<int:u>', methods=['POST'])
def react_msg(mid, u):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    emj = str(data.get('emoji', '')).strip()[:20]
    if not emj: return jsonify({'ok': False}), 400

    msg = db.session.get(Message, mid)
    if not msg: return jsonify({'ok': False}), 404

    uid = session['uid']
    # Проверка доступа
    if msg.receiver_id >= 0:
        if uid not in (msg.sender_id, msg.receiver_id):
            return jsonify({'ok': False}), 403
    else:
        if not is_member(uid, -msg.receiver_id):
            return jsonify({'ok': False}), 403

    existing = MessageReaction.query.filter_by(message_id=mid, user_id=uid).first()
    if existing:
        if existing.emoji == emj:
            db.session.delete(existing)
        else:
            existing.emoji = emj
    else:
        db.session.add(MessageReaction(message_id=mid, user_id=uid, emoji=emj))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/join/<int:uid>')
def join(uid):
    session['pending_ref'] = uid
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
