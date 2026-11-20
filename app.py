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
from sqlalchemy.engine import Engine

app = Flask(__name__)

# ===================== КОНФИГ =====================
app.secret_key = os.environ.get('SECRET_KEY', 'billionaire-secret-2026-ultra')
# По умолчанию используем локальную SQLite (ключи не нужны)
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///ramplez.db?timeout=30')
if _db_url.startswith('postgres://'): 
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True

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
    last_seen = db.Column(db.Float, default=0.0)
    theme = db.Column(db.String(20), default='gold') # Тема по умолчанию
    is_bot = db.Column(db.Integer, default=0)

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
    expires_at = db.Column(db.Float, default=0.0) # Секретные сообщения

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

# ===================== БЕЗОПАСНАЯ ИНИЦИАЛИЗАЦИЯ БД =====================
with app.app_context():
    try:
        db.create_all()
        # Создаем AI-бота при первом запуске
        if not User.query.filter_by(username='ai_bot').first():
            bot = User(username='ai_bot', name='🤖 Ramplez AI', password='bot', bio='Ваш личный помощник', is_bot=1)
            db.session.add(bot)
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"DB Init Error: {e}")

# ===================== SSE HUB (Скорость света) =====================
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

# ===================== CSRF ЗАЩИТА =====================
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

def get_my_contacts(me_id):
    sent = {r[0] for r in db.session.query(Message.receiver_id).filter(Message.sender_id == me_id, Message.receiver_id > 0).distinct()}
    received = {r[0] for r in db.session.query(Message.sender_id).filter(Message.receiver_id == me_id).distinct()}
    return list(sent | received)

def serialize_msg(m, sender_name=""):
    return {'id': m.id, 'sender_id': m.sender_id, 'receiver_id': m.receiver_id, 'msg_type': m.msg_type,
            'content': m.content, 'file_name': m.file_name, 'timestamp': m.timestamp,
            'created_at': m.created_at, 'read_at': m.read_at or 0.0, 'reaction': m.reaction,
            'pinned': m.pinned, 'reply_to_id': m.reply_to_id or 0, 'edited_at': m.edited_at or 0,
            'forwarded_from': m.forwarded_from or '', 'sender_name': sender_name, 'expires_at': m.expires_at or 0}

# ===================== AUTH =====================
@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session: return redirect(url_for('chat'))
    error = None
    if request.method == 'POST':
        action = request.form.get('action', 'login')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip() or username
        
        try:
            if not username or not password: error = 'Заполните поля'
            elif len(username) < 3 or not re.match(r'^[a-z0-9_]+$', username): error = 'Логин 3+ символов (a-z, 0-9, _)'
            elif action == 'register':
                if User.query.filter_by(username=username).first(): error = 'Логин занят'
                else:
                    u = User(username=username, name=name, password=generate_password_hash(password))
                    db.session.add(u); db.session.commit()
                    session['uid'] = u.id
                    return redirect(url_for('chat'))
            else:
                u = User.query.filter_by(username=username).first()
                if u and check_password_hash(u.password, password): 
                    session['uid'] = u.id; return redirect(url_for('chat'))
                error = 'Неверный логин или пароль'
        except Exception as e:
            db.session.rollback()
            error = 'Ошибка базы данных. Попробуйте снова.'
            
    return render_template('login.html', error=error)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

def _build_conversations(me):
    contact_ids = get_my_contacts(me.id)
    users = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    # Добавляем AI бота в список всегда
    bot = User.query.filter_by(username='ai_bot').first()
    if bot and bot.id not in contact_ids:
        users.append(bot)

    my_groups = (db.session.query(Chat).join(ChatMember, ChatMember.chat_id == Chat.id).filter(ChatMember.user_id == me.id).all())
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
    me.last_seen = time.time()
    try: db.session.commit()
    except: db.session.rollback()

    gid = request.args.get('g', type=int)
    oid = request.args.get('u', type=int)
    mode, other, group = None, None, None
    if gid and is_member(me.id, gid): group = db.session.get(Chat, gid); mode = 'group'
    elif oid: other = db.session.get(User, oid); mode = 'personal' if other and other.id != me.id else None

    conversations = _build_conversations(me)
    contact_ids = get_my_contacts(me.id)
    users_for_group = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    return render_template('chat.html', me=me, users=users_for_group, other=other, group=group, mode=mode, conversations=conversations, now=time.time(), theme=me.theme or 'gold')

# ===================== API =====================
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

# ===================== ОТПРАВКА И ПОЛУЧЕНИЕ (СКОРОСТЬ СВЕТА) =====================
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

@app.route('/send', methods=['POST'])
def send():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    oid = int(data.get('oid', 0))
    content = str(data.get('content', '')).strip()
    expires_in = int(data.get('expires_in', 0)) # Секретные сообщения
    
    if not oid or not content: return jsonify({'ok': False}), 400
    
    try:
        other = db.session.get(User, oid)
        if not other: return jsonify({'ok': False}), 404
        
        expires_at = time.time() + expires_in if expires_in > 0 else 0
        msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type='text', content=content, 
                      timestamp=datetime.now().strftime('%H:%M'), created_at=time.time(), expires_at=expires_at)
        db.session.add(msg)
        me = db.session.get(User, session['uid'])
        if me: me.typing_to = 0; me.last_seen = time.time()
        db.session.commit()
        
        msg_data = serialize_msg(msg, me.name if me else "")
        sse_publish([oid, session['uid']], 'new_message', msg_data)
        
        # Логика AI Бота
        if other.is_bot:
            threading.Thread(target=ai_bot_reply, args=(session['uid'], content)).start()
            
        return jsonify({'ok': True, 'message': msg_data})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

def ai_bot_reply(user_id, text):
    time.sleep(1) # Имитация раздумий
    bot = User.query.filter_by(username='ai_bot').first()
    if not bot: return
    reply_text = "Я искусственный интеллект. Вы сказали: " + text
    if 'привет' in text.lower(): reply_text = "Приветствую, мой господин! Чем могу помочь?"
    
    try:
        msg = Message(sender_id=bot.id, receiver_id=user_id, msg_type='text', content=reply_text, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
        db.session.add(msg); db.session.commit()
        sse_publish([user_id], 'new_message', serialize_msg(msg, bot.name))
    except: db.session.rollback()

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    oid = request.form.get('oid', type=int)
    gid = request.form.get('gid', type=int)
    f = request.files.get('file')
    if not f or not f.filename: return jsonify({'ok': False}), 400
    
    orig_name = secure_filename(f.filename) or 'file'
    ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else ''
    if ext in DANGEROUS_EXTS: return jsonify({'ok': False, 'error': 'file type not allowed'}), 400
    
    try:
        saved_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}_{orig_name}"
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_name))
        mtype = 'image' if ext in ('png','jpg','jpeg','gif','webp') else ('audio' if ext in ('webm','ogg','mp3','wav','m4a') else 'file')
        
        msg = Message(sender_id=session['uid'], receiver_id=(-gid if gid else oid), msg_type=mtype, 
                      content=url_for('uploaded_file', fn=saved_name), file_name=f.filename, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
        db.session.add(msg); db.session.commit()
        me = db.session.get(User, session['uid'])
        msg_data = serialize_msg(msg, me.name if me else "")
        
        if gid: sse_publish([mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()], 'new_message', msg_data)
        else: sse_publish([oid, session['uid']], 'new_message', msg_data)
        return jsonify({'ok': True, 'message': msg_data})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/poll/<int:oid>')
def poll(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    uid = session['uid']
    try:
        other = db.session.get(User, oid)
        if not other: return jsonify({'ok': False}), 404
        now = time.time()
        me = db.session.get(User, uid)
        if me: me.last_seen = now; db.session.commit()
        
        Message.query.filter(Message.receiver_id == uid, Message.expires_at > 0, Message.expires_at < now).delete()
        
        Message.query.filter_by(sender_id=oid, receiver_id=uid, read_at=0).update({'read_at': now})
        db.session.commit()
        
        msgs = Message.query.filter(
            ((Message.sender_id == uid) & (Message.receiver_id == oid)) |
            ((Message.sender_id == oid) & (Message.receiver_id == uid))
        ).order_by(Message.id.asc()).all()
        
        out = [serialize_msg(m, other.name if m.sender_id == oid else "Вы") for m in msgs if str(uid) not in (m.hidden_for or '')]
        return jsonify({'ok': True, 'messages': out, 'typing': bool(other.typing_to == uid and (now - (other.typing_at or 0)) < 3.5), 'other_online': is_online(other)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    try:
        me = db.session.get(User, session['uid'])
        if me:
            me.typing_to = oid; me.typing_at = time.time(); db.session.commit()
            sse_publish([oid], 'typing', {'from': me.id})
        return jsonify({'ok': True})
    except: db.session.rollback(); return jsonify({'ok': False}), 500

@app.route('/call_signal', methods=['POST'])
def call_signal():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    target_id = int(data.get('target_id', 0))
    if not target_id: return jsonify({'ok': False}), 400
    me = db.session.get(User, session['uid'])
    sse_publish([target_id], 'call_signal', {'from': session['uid'], 'from_name': me.name, 'type': data.get('type'), 'payload': data.get('payload')})
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
