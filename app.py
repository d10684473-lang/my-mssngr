import os
import json
import time
import re
import hmac
import secrets
import queue
import threading
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, send_from_directory, abort, Response)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine

app = Flask(__name__)

# ===================== КОНФИГ =====================
app.secret_key = os.environ.get('SECRET_KEY', 'billion-dollar-secret-key-2026')
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///ramplez.db?timeout=30')
if _db_url.startswith('postgres://'): _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
SAFE_INLINE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp3', 'wav', 'ogg', 'webm', 'm4a', 'mp4'}
DANGEROUS_EXTS = {'php', 'phtml', 'exe', 'dll', 'bat', 'cmd', 'js', 'html', 'htm', 'svg', 'xml', 'apk'}

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except Exception: pass

db = SQLAlchemy(app)

# ===================== SSE HUB =====================
_sse_subscribers = {}
_sse_lock = threading.Lock()

def sse_publish(user_ids, event_type, data):
    payload = (event_type, data)
    with _sse_lock:
        for uid in user_ids:
            for q in _sse_subscribers.get(uid, []):
                try: q.put_nowait(payload)
                except queue.Full: pass

def sse_subscribe(uid):
    q = queue.Queue(maxsize=200)
    with _sse_lock: _sse_subscribers.setdefault(uid, []).append(q)
    return q

def sse_unsubscribe(uid, q):
    with _sse_lock:
        lst = _sse_subscribers.get(uid, [])
        if q in lst: lst.remove(q)
        if not lst: _sse_subscribers.pop(uid, None)

# ===================== МОДЕЛИ =====================
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.String(200), default='')
    avatar = db.Column(db.Text, default='')
    typing_to = db.Column(db.Integer, default=0)
    typing_at = db.Column(db.Float, default=0.0)
    invited_by = db.Column(db.Integer, default=0)
    last_seen = db.Column(db.Float, default=0.0)
    onboarded = db.Column(db.Integer, default=0)
    is_bot = db.Column(db.Integer, default=0)
    theme = db.Column(db.String(20), default='dark')

class Message(db.Model):
    __tablename__ = 'message'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False, index=True)
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default='')
    timestamp = db.Column(db.String(10), default='')
    created_at = db.Column(db.Float, default=0.0, index=True)
    read_at = db.Column(db.Float, default=0.0)
    reaction = db.Column(db.String(20), default='')
    pinned = db.Column(db.Integer, default=0)
    reply_to_id = db.Column(db.Integer, default=0)
    edited_at = db.Column(db.Float, default=0.0)
    forwarded_from = db.Column(db.String(100), default='')
    hidden_for = db.Column(db.String(50), default='')

class Chat(db.Model):
    __tablename__ = 'chat'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.Float, default=0.0)

class ChatMember(db.Model):
    __tablename__ = 'chat_member'
    chat_id = db.Column(db.Integer, primary_key=True, index=True)
    user_id = db.Column(db.Integer, primary_key=True, index=True)

class MessageRead(db.Model):
    __tablename__ = 'message_read'
    message_id = db.Column(db.Integer, primary_key=True, index=True)
    user_id = db.Column(db.Integer, primary_key=True, index=True)
    read_at = db.Column(db.Float, default=0.0)

# ===================== АВТО-МИГРАЦИЯ =====================
def ensure_schema():
    with app.app_context():
        db.create_all()
        try:
            insp = inspect(db.engine)
            if not db.engine.url.drivername.startswith('sqlite'): return
            tables = set(insp.get_table_names())
            if 'user' in tables:
                cols = {c['name'] for c in insp.get_columns('user')}
                if 'theme' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN theme VARCHAR(20) DEFAULT "dark"'))
            if 'message' in tables:
                cols = {c['name'] for c in insp.get_columns('message')}
                if 'reply_to_id' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN reply_to_id INTEGER DEFAULT 0'))
                if 'edited_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN edited_at FLOAT DEFAULT 0.0'))
                if 'reaction' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN reaction VARCHAR(20) DEFAULT ""'))
                if 'forwarded_from' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN forwarded_from VARCHAR(100) DEFAULT ""'))
                if 'hidden_for' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN hidden_for VARCHAR(50) DEFAULT ""'))
            db.session.commit()
        except Exception:
            db.session.rollback()

ensure_schema()

# ===================== CSRF =====================
@app.before_request
def _ensure_csrf():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_urlsafe(32)

@app.before_request
def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS'): return
    if request.path == '/': return
    token = session.get('csrf_token', '')
    sent = request.headers.get('X-CSRFToken', '')
    if not token or not sent or not hmac.compare_digest(token, sent):
        return jsonify({'ok': False, 'error': 'csrf'}), 403

# ===================== ХЕЛПЕРЫ =====================
def is_online(u): return bool(u and u.last_seen and (time.time() - u.last_seen) < 30)
def is_member(uid, gid): return ChatMember.query.filter_by(chat_id=gid, user_id=uid).first() is not None

def get_or_create_bot():
    bot = User.query.filter_by(username='ramplez').first()
    if not bot:
        bot = User(username='ramplez', name='Ramplez', password=generate_password_hash(secrets.token_hex(16)), bio='Официальный бот 👋', onboarded=1, is_bot=1)
        db.session.add(bot); db.session.commit()
    return bot

def get_my_contacts(me_id):
    sent = {r[0] for r in db.session.query(Message.receiver_id).filter(Message.sender_id == me_id, Message.receiver_id > 0).distinct()}
    received = {r[0] for r in db.session.query(Message.sender_id).filter(Message.receiver_id == me_id).distinct()}
    return list(sent | received)

def serialize_msg(m, sender_name=""):
    return {
        'id': m.id, 'sender_id': m.sender_id, 'receiver_id': m.receiver_id, 'msg_type': m.msg_type,
        'content': m.content, 'file_name': m.file_name, 'timestamp': m.timestamp,
        'created_at': m.created_at, 'read_at': m.read_at, 'reaction': m.reaction,
        'pinned': m.pinned, 'reply_to_id': m.reply_to_id or 0, 'edited_at': m.edited_at or 0,
        'forwarded_from': m.forwarded_from or '', 'sender_name': sender_name
    }

# ===================== SSE =====================
@app.route('/stream')
def stream():
    if 'uid' not in session: return Response('unauthorized', 401)
    uid = session['uid']
    q = sse_subscribe(uid)
    def gen():
        try:
            yield f"event: hello\ndata: {json.dumps({'ok': True})}\n\n"
            last_ping = time.time()
            while True:
                try:
                    event, data = q.get(timeout=1.0)
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    if time.time() - last_ping > 20:
                        yield ": ping\n\n"; last_ping = time.time()
        except GeneratorExit: pass
        finally: sse_unsubscribe(uid, q)
    resp = Response(gen(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'; resp.headers['X-Accel-Buffering'] = 'no'
    return resp

# ===================== AUTH =====================
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
        if not username or not password: error = 'Заполните поля'
        elif len(username) < 3 or not re.match(r'^[a-z0-9_]+$', username): error = 'Логин 3+ символов (a-z, 0-9, _)'
        elif action == 'register':
            if username == 'ramplez' or User.query.filter_by(username=username).first(): error = 'Логин занят'
            else:
                u = User(username=username, name=name, password=generate_password_hash(password), invited_by=session.get('pending_ref') or 0)
                db.session.add(u); db.session.commit()
                session['uid'] = u.id; session.pop('pending_ref', None)
                bot = get_or_create_bot()
                if bot.id != u.id:
                    welcome = Message(sender_id=bot.id, receiver_id=u.id, msg_type='text', content='Привет 👋 Добро пожаловать в Ramplez!\n\n• Найди друзей через 🔍\n• Создай группу ＋\n\nМы работаем в реальном времени ⚡', timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
                    db.session.add(welcome); db.session.commit()
                    return redirect(url_for('chat', u=bot.id))
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=username).first()
            if u and check_password_hash(u.password, password): session['uid'] = u.id; return redirect(url_for('chat'))
            error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

# ===================== CHAT =====================
def _build_conversations(me):
    contact_ids = get_my_contacts(me.id)
    users = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    my_groups = (db.session.query(Chat).join(ChatMember, ChatMember.chat_id == Chat.id).filter(ChatMember.user_id == me.id).all())
    if not any(u.is_bot for u in users):
        bot = User.query.filter_by(username='ramplez').first()
        if bot and bot.id != me.id: users.append(bot)

    convs = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        convs.append({'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar, 'sub': u.bio or ('@' + u.username), 'online': is_online(u), 'unread': unread, 'username': u.username})
    for g in my_groups:
        g_msgs = Message.query.filter_by(receiver_id=-g.id).order_by(Message.id.desc()).limit(100).all()
        unread_g = sum(1 for m in g_msgs if m.sender_id != me.id and not MessageRead.query.filter_by(message_id=m.id, user_id=me.id).first())
        convs.append({'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '', 'sub': 'Групповой чат', 'online': False, 'unread': unread_g, 'username': ''})
    convs.sort(key=lambda c: (not c['online'], -c['unread'], c['title'].lower()))
    return convs

@app.route('/chat')
def chat():
    if 'uid' not in session: return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me: session.clear(); return redirect(url_for('index'))
    me.last_seen = time.time(); db.session.commit()

    gid = request.args.get('g', type=int)
    oid = request.args.get('u', type=int)
    mode, other, group = None, None, None

    if gid and is_member(me.id, gid): group = db.session.get(Chat, gid); mode = 'group'
    elif oid: other = db.session.get(User, oid); mode = 'personal' if other and other.id != me.id else None

    conversations = _build_conversations(me)
    contact_ids = get_my_contacts(me.id)
    users_for_group = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []

    return render_template('chat.html', me=me, users=users_for_group, other=other, group=group, mode=mode, conversations=conversations, now=time.time(), theme=me.theme or 'dark')

@app.route('/api/conversations')
def api_conversations():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    return jsonify({'ok': True, 'conversations': _build_conversations(me)})

@app.route('/api/search_users')
def api_search_users():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    q = (request.args.get('q') or '').strip().lower()
    if not q or len(q) < 2: return jsonify({'ok': True, 'users': []})
    users = User.query.filter(User.username.ilike(f'{q}%'), User.id != session['uid']).order_by(User.username).limit(20).all()
    return jsonify({'ok': True, 'users': [{'id': u.id, 'username': u.username, 'name': u.name, 'avatar': u.avatar, 'online': is_online(u)} for u in users]})

@app.route('/api/search_messages')
def api_search_messages():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2: return jsonify({'ok': True, 'results': []})
    like = f'%{q}%'
    msgs = Message.query.filter(Message.content.ilike(like), Message.msg_type == 'text').order_by(Message.id.desc()).limit(50).all()
    return jsonify({'ok': True, 'results': [serialize_msg(m) for m in msgs if str(session['uid']) not in (m.hidden_for or '')]})

# ===================== SEND / POLL =====================
@app.route('/send', methods=['POST'])
def send():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    oid = int(data.get('oid', 0))
    content = str(data.get('content', '')).strip()
    reply_to_id = int(data.get('reply_to_id', 0)) or 0
    if not oid or not content: return jsonify({'ok': False}), 400

    other = db.session.get(User, oid)
    if not other: return jsonify({'ok': False}), 404

    msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type='text', content=content, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time(), reply_to_id=reply_to_id)
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me: me.typing_to = 0; me.last_seen = time.time()
    db.session.commit()

    msg_data = serialize_msg(msg, me.name if me else "")
    sse_publish([oid, session['uid']], 'new_message', msg_data)
    return jsonify({'ok': True, 'message': msg_data})

@app.route('/send_group', methods=['POST'])
def send_group():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    gid = int(data.get('gid', 0))
    content = str(data.get('content', '')).strip()
    reply_to_id = int(data.get('reply_to_id', 0)) or 0
    if not gid or not content or not is_member(session['uid'], gid): return jsonify({'ok': False}), 400

    msg = Message(sender_id=session['uid'], receiver_id=-gid, msg_type='text', content=content, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time(), reply_to_id=reply_to_id)
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me: me.typing_to = 0; me.last_seen = time.time()
    db.session.commit()

    recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
    msg_data = serialize_msg(msg, me.name if me else "")
    sse_publish(recipients, 'new_message', msg_data)
    return jsonify({'ok': True, 'message': msg_data})

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    oid = request.form.get('oid', type=int)
    gid = request.form.get('gid', type=int)
    f = request.files.get('file')
    if not f or not f.filename: return jsonify({'ok': False}), 400
    if gid and not is_member(session['uid'], gid): return jsonify({'ok': False}), 403

    orig_name = secure_filename(f.filename) or 'file'
    ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else ''
    if ext in DANGEROUS_EXTS: return jsonify({'ok': False, 'error': 'file type not allowed'}), 400

    saved_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}_{orig_name}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_name))

    mtype = 'image' if ext in ('png','jpg','jpeg','gif','webp') else ('audio' if ext in ('webm','ogg','mp3','wav','m4a') else 'file')
    
    msg = Message(sender_id=session['uid'], receiver_id=(-gid if gid else oid), msg_type=mtype, content=url_for('uploaded_file', fn=saved_name), file_name=f.filename, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
    db.session.add(msg); db.session.commit()

    me = db.session.get(User, session['uid'])
    msg_data = serialize_msg(msg, me.name if me else "")
    
    if gid:
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
        sse_publish(recipients, 'new_message', msg_data)
    else:
        sse_publish([oid, session['uid']], 'new_message', msg_data)
    return jsonify({'ok': True, 'message': msg_data})

@app.route('/uploads/<path:fn>')
def uploaded_file(fn):
    base = os.path.abspath(app.config['UPLOAD_FOLDER'])
    target = os.path.abspath(os.path.join(base, fn))
    if not target.startswith(base + os.sep): abort(403)
    resp = send_from_directory(base, fn)
    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
    if ext not in SAFE_INLINE_EXTS: resp.headers['Content-Disposition'] = f'attachment; filename="{os.path.basename(fn)}"'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

@app.route('/poll/<int:oid>')
def poll(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    uid = session['uid']
    other = db.session.get(User, oid)
    if not other: return jsonify({'ok': False}), 404

    now = time.time()
    me = db.session.get(User, uid)
    if me: me.last_seen = now; db.session.commit()

    # Помечаем как прочитанные мгновенно
    Message.query.filter_by(sender_id=oid, receiver_id=uid, read_at=0).update({'read_at': now})
    db.session.commit()

    msgs = Message.query.filter(
        ((Message.sender_id == uid) & (Message.receiver_id == oid)) |
        ((Message.sender_id == oid) & (Message.receiver_id == uid))
    ).order_by(Message.id.asc()).all()

    out = [serialize_msg(m, other.name if m.sender_id == oid else "Вы") for m in msgs if str(uid) not in (m.hidden_for or '')]
    return jsonify({'ok': True, 'messages': out, 'typing': bool(other.typing_to == uid and (now - (other.typing_at or 0)) < 3.5), 'other_online': is_online(other)})

@app.route('/poll_group/<int:gid>')
def poll_group(gid):
    if 'uid' not in session or not is_member(session['uid'], gid): return jsonify({'ok': False}), 403
    uid = session['uid']
    now = time.time()
    me = db.session.get(User, uid)
    if me: me.last_seen = now; db.session.commit()

    msgs = Message.query.filter(Message.receiver_id == -gid).order_by(Message.id.asc()).all()
    member_users = User.query.filter(User.id.in_([mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()])).all()
    users_map = {u.id: u.name for u in member_users}
    members_data = [{'id': u.id, 'name': u.name, 'username': u.username} for u in member_users]

    for m in msgs:
        if m.sender_id != uid and not MessageRead.query.filter_by(message_id=m.id, user_id=uid).first():
            db.session.add(MessageRead(message_id=m.id, user_id=uid, read_at=now))
    db.session.commit()

    out = [serialize_msg(m, users_map.get(m.sender_id, 'Аноним')) for m in msgs if str(uid) not in (m.hidden_for or '')]
    typing_users = [u.name for u in member_users if u.id != uid and u.typing_to == -gid and (now - (u.typing_at or 0)) < 3.5]
    return jsonify({'ok': True, 'messages': out, 'typing': bool(typing_users), 'typing_names': typing_users, 'members': members_data})

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = oid; me.typing_at = time.time(); db.session.commit()
        sse_publish([oid], 'typing', {'from': me.id})
    return jsonify({'ok': True})

@app.route('/typing_group/<int:gid>', methods=['POST'])
def typing_group(gid):
    if 'uid' not in session or not is_member(session['uid'], gid): return jsonify({'ok': False}), 403
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = -gid; me.typing_at = time.time(); db.session.commit()
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all() if mb.user_id != me.id]
        sse_publish(recipients, 'typing_group', {'gid': gid, 'from': me.id})
    return jsonify({'ok': True})

@app.route('/react_msg/<int:mid>', methods=['POST'])
def react_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    emj = str(data.get('emoji', '')).strip()[:20]
    if not emj: return jsonify({'ok': False}), 400
    msg = db.session.get(Message, mid)
    if not msg: return jsonify({'ok': False}), 404
    
    msg.reaction = emj if msg.reaction != emj else ''
    db.session.commit()
    
    if msg.receiver_id >= 0: sse_publish([msg.sender_id, msg.receiver_id], 'react', {'mid': mid})
    else: sse_publish([mb.user_id for mb in ChatMember.query.filter_by(chat_id=-msg.receiver_id).all()], 'react', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/edit_msg/<int:mid>', methods=['POST'])
def edit_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    new_content = str(data.get('content', '')).strip()
    msg = db.session.get(Message, mid)
    if msg and msg.sender_id == session['uid']:
        msg.content = new_content
        msg.edited_at = time.time()
        db.session.commit()
        sse_publish([msg.sender_id, msg.receiver_id], 'edit', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/forward_msg', methods=['POST'])
def forward_msg():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    mid = int(data.get('mid', 0))
    targets = data.get('targets') or []
    src = db.session.get(Message, mid)
    if not src or not targets: return jsonify({'ok': False}), 400

    original_sender = db.session.get(User, src.sender_id)
    fwd_label = (original_sender.name if original_sender else 'Аноним')[:80]
    
    for t in targets:
        tid = int(t.get('id', 0))
        if t.get('type') == 'user':
            msg = Message(sender_id=session['uid'], receiver_id=tid, msg_type=src.msg_type, content=src.content, file_name=src.file_name, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time(), forwarded_from=fwd_label)
            db.session.add(msg)
            sse_publish([tid, session['uid']], 'new_message', serialize_msg(msg, 'Вы'))
        elif t.get('type') == 'group':
            msg = Message(sender_id=session['uid'], receiver_id=-tid, msg_type=src.msg_type, content=src.content, file_name=src.file_name, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time(), forwarded_from=fwd_label)
            db.session.add(msg)
            sse_publish([mb.user_id for mb in ChatMember.query.filter_by(chat_id=tid).all()], 'new_message', serialize_msg(msg, 'Вы'))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/del_me/<int:mid>/<int:u>', methods=['POST'])
def del_me(mid, u):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        hidden = set(msg.hidden_for.split(',')) if msg.hidden_for else set()
        hidden.add(str(session['uid']))
        msg.hidden_for = ','.join(hidden)
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/del_all/<int:mid>/<int:u>', methods=['POST'])
def del_all(mid, u):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg and msg.sender_id == session['uid']:
        recv = msg.receiver_id
        db.session.delete(msg); db.session.commit()
        sse_publish([recv], 'delete', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/pin_msg/<int:mid>', methods=['POST'])
def pin_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        msg.pinned = 1 if msg.pinned == 0 else 0
        db.session.commit()
        if msg.receiver_id >= 0: sse_publish([msg.sender_id, msg.receiver_id], 'pin', {'mid': mid})
        else: sse_publish([mb.user_id for mb in ChatMember.query.filter_by(chat_id=-msg.receiver_id).all()], 'pin', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    me = db.session.get(User, session['uid'])
    if me:
        if data.get('name'): me.name = str(data.get('name')).strip()[:80]
        me.bio = str(data.get('bio', '')).strip()[:200]
        if data.get('theme'): me.theme = str(data.get('theme'))
        av = data.get('avatar')
        if av and str(av).startswith('data:image/'): me.avatar = av
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/complete_onboarding', methods=['POST'])
def complete_onboarding():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me: me.onboarded = 1; db.session.commit()
    return jsonify({'ok': True})

@app.route('/create_group', methods=['POST'])
def create_group():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    title = str(data.get('title', '')).strip()
    ids = data.get('member_ids') or []
    if not title or not isinstance(ids, list): return jsonify({'ok': False}), 400
    try: ids = [int(x) for x in ids if int(x) != session['uid']]
    except: return jsonify({'ok': False}), 400
    
    g = Chat(title=title[:100], owner_id=session['uid'], created_at=time.time())
    db.session.add(g); db.session.commit()
    for uid_ in set(ids + [session['uid']]): db.session.add(ChatMember(chat_id=g.id, user_id=uid_))
    db.session.commit()
    for uid_ in set(ids): sse_publish([uid_], 'new_group', {'gid': g.id})
    return jsonify({'ok': True, 'gid': g.id})

@app.route('/leave_group/<int:gid>', methods=['POST'])
def leave_group(gid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    ChatMember.query.filter_by(chat_id=gid, user_id=session['uid']).delete(); db.session.commit()
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
