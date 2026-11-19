# app.py
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

# ===================== КОНФИГ =====================
app = Flask(__name__)

_sk = os.environ.get('SECRET_KEY')
if not _sk:
    _sk = 'dev-only-fallback-change-me'
    if os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production':
        print('[SECURITY] ⚠ SECRET_KEY не задан в ENV!')
app.secret_key = _sk

_db_url = os.environ.get('DATABASE_URL')
if _db_url:
    if _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez.db?timeout=30'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

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

# ===================== SSE HUB =====================
# In-memory pub/sub. При нескольких worker'ах — нужен Redis pub/sub.
_sse_subscribers = {}   # uid -> list[queue.Queue]
_sse_lock = threading.Lock()

def sse_publish(user_ids, event_type, data):
    payload = (event_type, data)
    with _sse_lock:
        for uid in user_ids:
            for q in _sse_subscribers.get(uid, []):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

def sse_subscribe(uid):
    q = queue.Queue(maxsize=200)
    with _sse_lock:
        _sse_subscribers.setdefault(uid, []).append(q)
    return q

def sse_unsubscribe(uid, q):
    with _sse_lock:
        lst = _sse_subscribers.get(uid, [])
        if q in lst:
            lst.remove(q)
        if not lst:
            _sse_subscribers.pop(uid, None)

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
    activity = db.Column(db.String(50), default='')
    is_bot = db.Column(db.Integer, default=0)

class Message(db.Model):
    __tablename__ = 'message'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False, index=True)
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default='')
    reaction = db.Column(db.String(200), default='')
    hidden_for = db.Column(db.String(50), default='')
    timestamp = db.Column(db.String(10), default='')
    expires_at = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.Float, default=0.0, index=True)
    read_at = db.Column(db.Float, default=0.0)
    read_by = db.Column(db.String(200), default='')
    pinned = db.Column(db.Integer, default=0)
    reply_to_id = db.Column(db.Integer, default=0)
    edited_at = db.Column(db.Float, default=0.0)
    mentions = db.Column(db.String(500), default='')
    forwarded_from = db.Column(db.String(100), default='')

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

class MessageReaction(db.Model):
    __tablename__ = 'message_reaction'
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
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
                if 'is_bot' not in cols: db.session.execute(text('ALTER TABLE user ADD COLUMN is_bot INTEGER DEFAULT 0'))

            if 'message' in tables:
                cols = {c['name'] for c in insp.get_columns('message')}
                if 'expires_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN expires_at FLOAT DEFAULT 0.0'))
                if 'created_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN created_at FLOAT DEFAULT 0.0'))
                if 'read_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN read_at FLOAT DEFAULT 0.0'))
                if 'read_by' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN read_by VARCHAR(200) DEFAULT ""'))
                if 'pinned' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN pinned INTEGER DEFAULT 0'))
                if 'reply_to_id' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN reply_to_id INTEGER DEFAULT 0'))
                if 'edited_at' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN edited_at FLOAT DEFAULT 0.0'))
                if 'mentions' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN mentions VARCHAR(500) DEFAULT ""'))
                if 'forwarded_from' not in cols: db.session.execute(text('ALTER TABLE message ADD COLUMN forwarded_from VARCHAR(100) DEFAULT ""'))

            # Индексы для быстрого поиска (важно для масштаба)
            for idx_sql in [
                'CREATE INDEX IF NOT EXISTS ix_msg_send_recv ON message(sender_id, receiver_id, id)',
                'CREATE INDEX IF NOT EXISTS ix_msg_recv_send ON message(receiver_id, sender_id, id)',
                'CREATE INDEX IF NOT EXISTS ix_msg_created ON message(created_at)',
                'CREATE INDEX IF NOT EXISTS ix_user_username ON user(username)',
            ]:
                try: db.session.execute(text(idx_sql))
                except Exception: pass
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f'[migrate] warning: {e}')

        # Бэкфилл реакций
        try:
            if db.session.query(MessageReaction).count() == 0:
                for m in Message.query.filter(Message.reaction != '', Message.reaction.isnot(None)).all():
                    try: data = json.loads(m.reaction)
                    except (json.JSONDecodeError, TypeError): continue
                    if isinstance(data, dict):
                        for emj, users in data.items():
                            if isinstance(users, list):
                                for uid in users:
                                    try: db.session.add(MessageReaction(message_id=m.id, user_id=int(uid), emoji=str(emj)[:20]))
                                    except (ValueError, TypeError): pass
                db.session.commit()
        except Exception as e:
            db.session.rollback()

        # Бэкфилл прочтений
        try:
            if db.session.query(MessageRead).count() == 0:
                for m in Message.query.filter(Message.receiver_id < 0, Message.read_by != '', Message.read_by.isnot(None)).all():
                    for uid in m.read_by.split(','):
                        uid = uid.strip()
                        if not uid: continue
                        try: db.session.add(MessageRead(message_id=m.id, user_id=int(uid), read_at=0))
                        except (ValueError, TypeError): pass
                db.session.commit()
        except Exception as e:
            db.session.rollback()

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
    if request.path == '/':
        return
    token = session.get('csrf_token', '')
    sent = request.headers.get('X-CSRFToken', '')
    if not token or not sent or not hmac.compare_digest(token, sent):
        return jsonify({'ok': False, 'error': 'csrf'}), 403

# ===================== ХЕЛПЕРЫ =====================

def is_online(u): return bool(u and u.last_seen and (time.time() - u.last_seen) < 30)
def is_member(uid, gid): return ChatMember.query.filter_by(chat_id=gid, user_id=uid).first() is not None

def parse_mentions(content):
    if not content: return []
    usernames = set(re.findall(r'@([a-zA-Z0-9_]{2,50})', content))
    if not usernames: return []
    users = User.query.filter(User.username.in_(usernames)).all()
    return [u.id for u in users]

def aggregate_reactions_json(message_id, reactions_by_msg):
    d = reactions_by_msg.get(message_id, {})
    if not d: return ''
    return json.dumps({k: sorted(v) for k, v in sorted(d.items())}, ensure_ascii=False)

def get_or_create_bot():
    bot = User.query.filter_by(username='ramplez').first()
    if not bot:
        bot = User(username='ramplez', name='Ramplez',
                   password=generate_password_hash(secrets.token_hex(16)),
                   bio='Официальный бот Ramplez 👋',
                   onboarded=1, is_bot=1)
        db.session.add(bot)
        db.session.commit()
    return bot

def get_my_contacts(me_id):
    """Только те, с кем есть переписка. Масштабируется на миллионы."""
    sent = {r[0] for r in db.session.query(Message.receiver_id).filter(
        Message.sender_id == me_id, Message.receiver_id > 0).distinct()}
    received = {r[0] for r in db.session.query(Message.sender_id).filter(
        Message.receiver_id == me_id).distinct()}
    return list(sent | received)

def user_brief(u):
    return {'id': u.id, 'username': u.username, 'name': u.name,
            'avatar': u.avatar, 'online': is_online(u),
            'bio': u.bio or ''}

# ===================== SSE =====================

@app.route('/stream')
def stream():
    if 'uid' not in session:
        return Response('unauthorized', 401)
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
                        yield ": ping\n\n"
                        last_ping = time.time()
        except GeneratorExit:
            pass
        finally:
            sse_unsubscribe(uid, q)

    resp = Response(gen(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp

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
        elif len(username) < 3 or len(username) > 30:
            error = 'Логин 3-30 символов'
        elif not re.match(r'^[a-z0-9_]+$', username):
            error = 'Только a-z, 0-9, _'
        elif action == 'register':
            if username == 'ramplez':
                error = 'Логин занят'
            elif User.query.filter_by(username=username).first():
                error = 'Логин занят'
            else:
                u = User(username=username, name=name,
                         password=generate_password_hash(password),
                         invited_by=session.get('pending_ref') or 0)
                db.session.add(u); db.session.commit()
                session['uid'] = u.id
                session.pop('pending_ref', None)

                # Приветствие от бота
                bot = get_or_create_bot()
                if bot.id != u.id:
                    welcome = Message(
                        sender_id=bot.id, receiver_id=u.id, msg_type='text',
                        content='Привет 👋 Добро пожаловать в Ramplez!\n\n'
                                '• Найди друзей по username через 🔍\n'
                                '• Создай группу кнопкой ＋\n'
                                '• Отправь первый стикер 😎\n\n'
                                'Мы работаем в реальном времени — сообщения приходят мгновенно ⚡',
                        timestamp=datetime.now().strftime('%H:%M'),
                        created_at=time.time())
                    db.session.add(welcome)
                    db.session.commit()
                    return redirect(url_for('chat', u=bot.id))
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

def _build_conversations(me):
    """Список чатов: только те, с кем есть переписка или общие группы."""
    contact_ids = get_my_contacts(me.id)
    users = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    my_groups = (db.session.query(Chat)
                 .join(ChatMember, ChatMember.chat_id == Chat.id)
                 .filter(ChatMember.user_id == me.id).all())

    # Дедупликация ботов: если есть @ramplez — не дублируем
    seen_bot = any(u.is_bot for u in users)
    if not seen_bot:
        bot = User.query.filter_by(username='ramplez').first()
        if bot and bot.id != me.id:
            users.append(bot)

    convs = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        convs.append({
            'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar,
            'sub': u.bio or ('@' + u.username), 'online': is_online(u),
            'unread': unread, 'username': u.username
        })

    for g in my_groups:
        g_msgs = Message.query.filter_by(receiver_id=-g.id).order_by(Message.id.desc()).limit(300).all()
        unread_g = 0
        if g_msgs:
            msg_ids = [m.id for m in g_msgs if m.sender_id != me.id]
            if msg_ids:
                read_ids = {r.message_id for r in MessageRead.query.filter(
                    MessageRead.user_id == me.id,
                    MessageRead.message_id.in_(msg_ids)).all()}
                unread_g = sum(1 for m in g_msgs if m.sender_id != me.id and m.id not in read_ids)
        convs.append({
            'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '',
            'sub': 'Групповой чат', 'online': False, 'unread': unread_g,
            'username': ''
        })

    convs.sort(key=lambda c: (not c['online'], -c['unread'], c['title'].lower()))
    return convs

@app.route('/chat')
def chat():
    if 'uid' not in session: return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me:
        session.clear(); return redirect(url_for('index'))

    me.last_seen = time.time(); db.session.commit()

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

    conversations = _build_conversations(me)

    # Контакты для модалки "новая группа"
    contact_ids = get_my_contacts(me.id)
    users_for_group = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []

    return render_template('chat.html', me=me, users=users_for_group,
                           other=other, group=group, mode=mode,
                           conversations=conversations, now=time.time())

@app.route('/api/conversations')
def api_conversations():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if not me: return jsonify({'ok': False}), 401
    return jsonify({'ok': True, 'conversations': _build_conversations(me)})

@app.route('/api/me')
def api_me():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if not me: return jsonify({'ok': False}), 401
    invited = User.query.filter_by(invited_by=me.id).count()
    return jsonify({'ok': True, 'invited_count': invited,
                    'invite_link': f'{request.host_url}join/{me.id}',
                    'username': me.username})

@app.route('/api/search_users')
def api_search_users():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    q = (request.args.get('q') or '').strip().lower()
    if not q or len(q) < 2: return jsonify({'ok': True, 'users': []})
    # Поиск по префиксу username (indexed) — быстро даже на миллионах
    users = User.query.filter(
        User.username.ilike(f'{q}%'),
        User.id != session['uid']
    ).order_by(User.username).limit(20).all()
    return jsonify({'ok': True, 'users': [user_brief(u) for u in users]})

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
    ).order_by(Message.id.desc()).limit(500).all()

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
        'media': media[:60], 'files': files[:60], 'audio': audio[:60], 'links': links[:60]
    })

# ===================== SEND / POLL =====================

def _prepare_reply(reply_to_id, uid, oid):
    if not reply_to_id: return 0
    rm = db.session.get(Message, reply_to_id)
    if not rm: return 0
    if rm.receiver_id >= 0:
        if uid not in (rm.sender_id, rm.receiver_id): return 0
        if {rm.sender_id, rm.receiver_id} != {uid, oid}: return 0
    else:
        if rm.receiver_id != -oid: return 0
    return reply_to_id

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

    reply_to_id = _prepare_reply(reply_to_id, session['uid'], oid)
    mentions = parse_mentions(content)

    msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type='text',
                  content=content, timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time(), reply_to_id=reply_to_id,
                  mentions=','.join(str(u) for u in mentions))
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = 0
        me.last_seen = time.time()
    db.session.commit()

    # Мгновенно пушим получателю и себе
    sse_publish([oid, session['uid']], 'new_message',
                {'from': session['uid'], 'to': oid, 'msg_id': msg.id, 'kind': 'personal'})
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
        sse_publish([oid], 'read_receipt', {'by': uid, 'from': uid})

    msgs = Message.query.filter(
        ((Message.sender_id == uid) & (Message.receiver_id == oid)) |
        ((Message.sender_id == oid) & (Message.receiver_id == uid))
    ).order_by(Message.id.asc()).all()

    reply_ids = [m.reply_to_id for m in msgs if m.reply_to_id]
    reply_map = {}
    if reply_ids:
        for rm in Message.query.filter(Message.id.in_(reply_ids)).all():
            reply_map[rm.id] = rm

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
            'edited_at': m.edited_at or 0,
            'forwarded_from': m.forwarded_from or '',
            'mentions': [int(x) for x in (m.mentions or '').split(',') if x.strip()],
            'read_by_ids': []
        }
        if m.reply_to_id and m.reply_to_id in reply_map:
            rm = reply_map[m.reply_to_id]
            sender_name = other.name if rm.sender_id == oid else (me.name if me else 'Вы')
            item['reply_to'] = {
                'sender_id': rm.sender_id,
                'sender_name': sender_name,
                'content_preview': (rm.content or '')[:60].replace('\n', ' '),
                'msg_type': rm.msg_type
            }
        out.append(item)

    members = []
    if me: members.append({'id': me.id, 'name': me.name, 'username': me.username})
    members.append({'id': other.id, 'name': other.name, 'username': other.username})

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
        sse_publish([oid], 'typing', {'from': me.id, 'name': me.name})
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
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all() if mb.user_id != me.id]
        sse_publish(recipients, 'typing_group', {'gid': gid, 'from': me.id, 'name': me.name})
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

    mentions = parse_mentions(content)
    msg = Message(sender_id=session['uid'], receiver_id=-gid, msg_type='text',
                  content=content, timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time(), reply_to_id=reply_to_id,
                  mentions=','.join(str(u) for u in mentions))
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = 0
        me.last_seen = time.time()
    db.session.commit()

    recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
    sse_publish(recipients, 'new_message', {'from': session['uid'], 'gid': gid, 'msg_id': msg.id, 'kind': 'group'})
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
    members_data = [{'id': u.id, 'name': u.name, 'username': u.username} for u in member_users]

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
        try: db.session.commit()
        except IntegrityError:
            db.session.rollback()
            reads_by_msg = {}
            for r in MessageRead.query.filter(MessageRead.message_id.in_(msg_ids)).all():
                reads_by_msg.setdefault(r.message_id, []).append(r.user_id)

    reactions_by_msg = {}
    if msg_ids:
        for r in MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids)).all():
            reactions_by_msg.setdefault(r.message_id, {}).setdefault(r.emoji, []).append(r.user_id)

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
            'reply_to_id': m.reply_to_id or 0,
            'edited_at': m.edited_at or 0,
            'forwarded_from': m.forwarded_from or '',
            'mentions': [int(x) for x in (m.mentions or '').split(',') if x.strip()]
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

    for uid_ in set(ids):
        sse_publish([uid_], 'new_group', {'gid': g.id, 'title': g.title})
    return jsonify({'ok': True, 'gid': g.id})

@app.route('/leave_group/<int:gid>', methods=['POST'])
def leave_group(gid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    ChatMember.query.filter_by(chat_id=gid, user_id=session['uid']).delete()
    db.session.commit()
    return jsonify({'ok': True})

# ===================== MESSAGE ACTIONS =====================

@app.route('/edit_msg/<int:mid>', methods=['POST'])
def edit_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    new_content = str(data.get('content', '')).strip()
    if not new_content or len(new_content) > 5000:
        return jsonify({'ok': False, 'error': 'invalid'}), 400
    msg = db.session.get(Message, mid)
    if not msg: return jsonify({'ok': False}), 404
    if msg.sender_id != session['uid']: return jsonify({'ok': False}), 403
    if msg.msg_type != 'text': return jsonify({'ok': False}), 400
    msg.content = new_content
    msg.edited_at = time.time()
    msg.mentions = ','.join(str(u) for u in parse_mentions(new_content))
    db.session.commit()

    if msg.receiver_id >= 0:
        sse_publish([msg.receiver_id, msg.sender_id], 'edit', {'mid': mid})
    else:
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=-msg.receiver_id).all()]
        sse_publish(recipients, 'edit', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/pin_msg/<int:mid>', methods=['POST'])
def pin_msg(mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        msg.pinned = 1 if msg.pinned == 0 else 0
        db.session.commit()
        if msg.receiver_id >= 0:
            sse_publish([msg.sender_id, msg.receiver_id], 'pin', {'mid': mid})
        else:
            recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=-msg.receiver_id).all()]
            sse_publish(recipients, 'pin', {'mid': mid})
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

    saved_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}_{orig_name}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_name))

    mtype = 'image' if ext in ('png','jpg','jpeg','gif','webp','bmp') \
        else ('audio' if ext in ('webm','ogg','mp3','wav','m4a') else 'file')

    msg = Message(sender_id=session['uid'],
                  receiver_id=(-gid if gid else oid),
                  msg_type=mtype,
                  content=url_for('uploaded_file', fn=saved_name),
                  file_name=f.filename,
                  timestamp=datetime.now().strftime('%H:%M'),
                  created_at=time.time())
    db.session.add(msg); db.session.commit()

    if gid:
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
        sse_publish(recipients, 'new_message', {'from': session['uid'], 'gid': gid, 'msg_id': msg.id, 'kind': 'group'})
    else:
        sse_publish([oid, session['uid']], 'new_message',
                    {'from': session['uid'], 'to': oid, 'msg_id': msg.id, 'kind': 'personal'})
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
        recv = msg.receiver_id
        db.session.delete(msg); db.session.commit()
        if recv >= 0:
            sse_publish([recv], 'delete', {'mid': mid})
        else:
            recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=-recv).all()]
            sse_publish(recipients, 'delete', {'mid': mid})
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
    if msg.receiver_id >= 0:
        if uid not in (msg.sender_id, msg.receiver_id):
            return jsonify({'ok': False}), 403
    else:
        if not is_member(uid, -msg.receiver_id):
            return jsonify({'ok': False}), 403

    existing = MessageReaction.query.filter_by(message_id=mid, user_id=uid).first()
    if existing:
        if existing.emoji == emj: db.session.delete(existing)
        else: existing.emoji = emj
    else:
        db.session.add(MessageReaction(message_id=mid, user_id=uid, emoji=emj))
    db.session.commit()

    if msg.receiver_id >= 0:
        sse_publish([msg.sender_id, msg.receiver_id], 'react', {'mid': mid})
    else:
        recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=-msg.receiver_id).all()]
        sse_publish(recipients, 'react', {'mid': mid})
    return jsonify({'ok': True})

@app.route('/join/<int:uid>')
def join(uid):
    session['pending_ref'] = uid
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
