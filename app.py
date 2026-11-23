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

app.secret_key = os.environ.get('SECRET_KEY', 'super-secret-god-mode-2026')
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///ramplez.db?timeout=30')
if _db_url.startswith('postgres://'): _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
DANGEROUS_EXTS = {'php', 'phtml', 'exe', 'dll', 'bat', 'cmd', 'js', 'html', 'htm', 'svg', 'xml', 'apk'}

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except: pass

db = SQLAlchemy(app)

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

with app.app_context():
    db.create_all()

# ===================== SSE =====================
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
    q = queue.Queue(maxsize=100)
    with _sse_lock: _sse_subscribers.setdefault(uid, []).append(q)
    return q

def sse_unsubscribe(uid, q):
    with _sse_lock:
        lst = _sse_subscribers.get(uid, [])
        if q in lst: lst.remove(q)
        if not lst: _sse_subscribers.pop(uid, None)

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
                    if time.time() - last_ping > 15:
                        yield ": ping\n\n"; last_ping = time.time()
        except: pass
        finally: sse_unsubscribe(uid, q)
    resp = Response(gen(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'; resp.headers['X-Accel-Buffering'] = 'no'
    return resp

# ===================== AUTH & HELPERS =====================
def is_online(u): return bool(u and u.last_seen and (time.time() - u.last_seen) < 30)
def is_member(uid, gid): return ChatMember.query.filter_by(chat_id=gid, user_id=uid).first() is not None

def serialize_msg(m, sender_name=""):
    return {'id': m.id, 'sender_id': m.sender_id, 'receiver_id': m.receiver_id, 'msg_type': m.msg_type,
            'content': m.content, 'file_name': m.file_name, 'timestamp': m.timestamp,
            'created_at': m.created_at, 'read_at': m.read_at or 0.0, 'reaction': m.reaction,
            'sender_name': sender_name}

@app.before_request
def _ensure_csrf():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_urlsafe(32)

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session: return redirect(url_for('chat'))
    error = None
    if request.method == 'POST':
        action = request.form.get('action', 'login')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip() or username
        if not username or not password: error = 'Заполните поля'
        elif action == 'register':
            if User.query.filter_by(username=username).first(): error = 'Занят'
            else:
                u = User(username=username, name=name, password=generate_password_hash(password))
                db.session.add(u); db.session.commit()
                session['uid'] = u.id; return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=username).first()
            if u and check_password_hash(u.password, password): session['uid'] = u.id; return redirect(url_for('chat'))
            error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

@app.route('/chat')
def chat():
    if 'uid' not in session: return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me: session.clear(); return redirect(url_for('index'))
    me.last_seen = time.time(); db.session.commit()

    gid = request.args.get('g', type=int); oid = request.args.get('u', type=int)
    mode, other, group = None, None, None
    if gid and is_member(me.id, gid): group = db.session.get(Chat, gid); mode = 'group'
    elif oid: other = db.session.get(User, oid); mode = 'personal' if other and other.id != me.id else None

    # Получаем список контактов (с кем есть переписка)
    sent = {r[0] for r in db.session.query(Message.receiver_id).filter(Message.sender_id == me.id, Message.receiver_id > 0).distinct()}
    received = {r[0] for r in db.session.query(Message.sender_id).filter(Message.receiver_id == me.id).distinct()}
    contact_ids = list(sent | received)
    
    users = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    my_groups = db.session.query(Chat).join(ChatMember).filter(ChatMember.user_id == me.id).all()
    
    convs = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        convs.append({'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar, 'sub': u.bio or ('@' + u.username), 'online': is_online(u), 'unread': unread})
    for g in my_groups:
        convs.append({'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '', 'sub': 'Групповой чат', 'online': False, 'unread': 0})
    
    convs.sort(key=lambda c: (not c['online'], c['title'].lower()))

    return render_template('chat.html', me=me, users=users, other=other, group=group, mode=mode, conversations=convs)

@app.route('/api/conversations')
def api_conversations():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    sent = {r[0] for r in db.session.query(Message.receiver_id).filter(Message.sender_id == me.id, Message.receiver_id > 0).distinct()}
    received = {r[0] for r in db.session.query(Message.sender_id).filter(Message.receiver_id == me.id).distinct()}
    contact_ids = list(sent | received)
    users = User.query.filter(User.id.in_(contact_ids)).all() if contact_ids else []
    my_groups = db.session.query(Chat).join(ChatMember).filter(ChatMember.user_id == me.id).all()
    
    convs = []
    for u in users:
        unread = Message.query.filter_by(sender_id=u.id, receiver_id=me.id, read_at=0).count()
        convs.append({'type': 'user', 'id': u.id, 'title': u.name, 'avatar': u.avatar, 'sub': u.bio or ('@' + u.username), 'online': is_online(u), 'unread': unread})
    for g in my_groups:
        convs.append({'type': 'group', 'id': g.id, 'title': g.title, 'avatar': '', 'sub': 'Групповой чат', 'online': False, 'unread': 0})
    
    return jsonify({'ok': True, 'conversations': convs})

@app.route('/api/search_users')
def api_search_users():
    q = (request.args.get('q') or '').strip().lower()
    if not q: return jsonify({'ok': True, 'users': []})
    users = User.query.filter(User.username.ilike(f'%{q}%'), User.id != session['uid']).limit(20).all()
    return jsonify({'ok': True, 'users': [{'id': u.id, 'username': u.username, 'name': u.name, 'online': is_online(u)} for u in users]})

@app.route('/send', methods=['POST'])
def send():
    data = request.get_json() or {}
    oid = int(data.get('oid', 0)); content = str(data.get('content', '')).strip()
    if not oid or not content: return jsonify({'ok': False})
    msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type='text', content=content, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
    db.session.add(msg); db.session.commit()
    me = db.session.get(User, session['uid'])
    msg_data = serialize_msg(msg, me.name)
    sse_publish([oid, session['uid']], 'new_message', msg_data)
    return jsonify({'ok': True, 'message': msg_data})

@app.route('/send_group', methods=['POST'])
def send_group():
    data = request.get_json() or {}
    gid = int(data.get('gid', 0)); content = str(data.get('content', '')).strip()
    if not gid or not content or not is_member(session['uid'], gid): return jsonify({'ok': False})
    msg = Message(sender_id=session['uid'], receiver_id=-gid, msg_type='text', content=content, timestamp=datetime.now().strftime('%H:%M'), created_at=time.time())
    db.session.add(msg); db.session.commit()
    me = db.session.get(User, session['uid'])
    msg_data = serialize_msg(msg, me.name)
    recipients = [mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()]
    sse_publish(recipients, 'new_message', msg_data)
    return jsonify({'ok': True, 'message': msg_data})

@app.route('/poll/<int:oid>')
def poll(oid):
    uid = session['uid']
    now = time.time()
    Message.query.filter_by(sender_id=oid, receiver_id=uid, read_at=0).update({'read_at': now})
    db.session.commit()
    msgs = Message.query.filter(((Message.sender_id == uid) & (Message.receiver_id == oid)) | ((Message.sender_id == oid) & (Message.receiver_id == uid))).order_by(Message.id.asc()).all()
    other = db.session.get(User, oid)
    out = [serialize_msg(m, other.name if m.sender_id == oid else "Вы") for m in msgs]
    return jsonify({'ok': True, 'messages': out, 'typing': bool(other.typing_to == uid and (now - (other.typing_at or 0)) < 3.5), 'other_online': is_online(other)})

@app.route('/poll_group/<int:gid>')
def poll_group(gid):
    now = time.time()
    msgs = Message.query.filter(Message.receiver_id == -gid).order_by(Message.id.asc()).all()
    member_users = User.query.filter(User.id.in_([mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all()])).all()
    users_map = {u.id: u.name for u in member_users}
    out = [serialize_msg(m, users_map.get(m.sender_id, 'Аноним')) for m in msgs]
    return jsonify({'ok': True, 'messages': out})

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    me = db.session.get(User, session['uid'])
    if me: me.typing_to = oid; me.typing_at = time.time(); db.session.commit(); sse_publish([oid], 'typing', {'from': me.id})
    return jsonify({'ok': True})

@app.route('/typing_group/<int:gid>', methods=['POST'])
def typing_group(gid):
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = -gid; me.typing_at = time.time(); db.session.commit()
        sse_publish([mb.user_id for mb in ChatMember.query.filter_by(chat_id=gid).all() if mb.user_id != me.id], 'typing_group', {'gid': gid})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
