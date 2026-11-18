# app.py
import os
import time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import event
from sqlalchemy.engine import Engine

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ramplez-super-secret-key-2026')

# Оптимизация и защита SQLite
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez.db?timeout=30'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # Лимит 25 МБ на файл

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Включаем WAL-режим для предотвращения блокировок базы при частом поллинге
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.String(200), default='')
    avatar = db.Column(db.Text, default='')
    typing_to = db.Column(db.Integer, default=0)
    typing_at = db.Column(db.Float, default=0.0)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default='')
    reaction = db.Column(db.String(20), default='')
    hidden_for = db.Column(db.String(50), default='')
    timestamp = db.Column(db.String(10), default='')

with app.app_context():
    db.create_all()

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session:
        return redirect(url_for('chat'))
    error = None
    if request.method == 'POST':
        action = request.form.get('action', 'login')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip() or username

        if not username or not password:
            error = 'Заполните все обязательные поля'
        elif action == 'register':
            if User.query.filter_by(username=username).first():
                error = 'Этот логин уже занят'
            else:
                u = User(username=username, name=name, password=generate_password_hash(password))
                db.session.add(u)
                db.session.commit()
                session['uid'] = u.id
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=username).first()
            if u and check_password_hash(u.password, password):
                session['uid'] = u.id
                return redirect(url_for('chat'))
            error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/chat')
def chat():
    if 'uid' not in session:
        return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me:
        session.clear()
        return redirect(url_for('index'))
    users = User.query.filter(User.id != me.id).all()
    oid = request.args.get('u', type=int) or (users[0].id if users else 0)
    other = db.session.get(User, oid) if oid else None
    return render_template('chat.html', me=me, users=users, other=other)

@app.route('/send', methods=['POST'])
def send():
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    oid = int(data.get('oid', 0))
    content = str(data.get('content', '')).strip()
    if not oid or not content:
        return jsonify({'ok': False}), 400
    msg = Message(
        sender_id=session['uid'],
        receiver_id=oid,
        msg_type='text',
        content=content,
        timestamp=datetime.now().strftime('%H:%M')
    )
    db.session.add(msg)
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = 0
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id})

@app.route('/poll/<int:oid>')
def poll(oid):
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    uid = session['uid']
    msgs = Message.query.filter(
        ((Message.sender_id == uid) & (Message.receiver_id == oid)) |
        ((Message.sender_id == oid) & (Message.receiver_id == uid))
    ).order_by(Message.id.asc()).all()

    out = []
    for m in msgs:
        hidden = m.hidden_for.split(',') if m.hidden_for else []
        if str(uid) in hidden:
            continue
        out.append({
            'id': m.id,
            'sender_id': m.sender_id,
            'msg_type': m.msg_type,
            'content': m.content,
            'file_name': m.file_name,
            'reaction': m.reaction,
            'timestamp': m.timestamp
        })

    other = db.session.get(User, oid)
    is_typing = bool(other and other.typing_to == uid and (time.time() - (other.typing_at or 0)) < 3.5)
    return jsonify({'ok': True, 'messages': out, 'typing': is_typing})

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me:
        me.typing_to = oid
        me.typing_at = time.time()
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    oid = request.form.get('oid', type=int)
    f = request.files.get('file')
    if not oid or not f or not f.filename:
        return jsonify({'ok': False}), 400
    orig_name = secure_filename(f.filename) or 'file'
    saved_name = f"{int(time.time() * 1000)}_{orig_name}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved_name))

    ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else ''
    if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        mtype = 'image'
    elif ext in ('webm', 'ogg', 'mp3', 'wav', 'm4a'):
        mtype = 'audio'
    else:
        mtype = 'file'

    msg = Message(
        sender_id=session['uid'],
        receiver_id=oid,
        msg_type=mtype,
        content=url_for('uploaded_file', fn=saved_name),
        file_name=f.filename,
        timestamp=datetime.now().strftime('%H:%M')
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/uploads/<path:fn>')
def uploaded_file(fn):
    return send_from_directory(app.config['UPLOAD_FOLDER'], fn)

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    data = request.get_json(silent=True) or {}
    me = db.session.get(User, session['uid'])
    if me:
        name = str(data.get('name', '')).strip()
        if name:
            me.name = name[:80]
        me.bio = str(data.get('bio', '')).strip()[:200]
        avatar = data.get('avatar')
        if avatar and str(avatar).startswith('data:image/'):
            me.avatar = avatar
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/del_me/<int:mid>/<int:u>', methods=['GET', 'POST'])
def del_me(mid, u):
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        hidden = set(msg.hidden_for.split(',')) if msg.hidden_for else set()
        hidden.discard('')
        hidden.add(str(session['uid']))
        msg.hidden_for = ','.join(hidden)
        db.session.commit()
    return jsonify({'ok': True}) if request.method == 'POST' else redirect(url_for('chat', u=u))

@app.route('/del_all/<int:mid>/<int:u>', methods=['GET', 'POST'])
def del_all(mid, u):
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg and msg.sender_id == session['uid']:
        db.session.delete(msg)
        db.session.commit()
    return jsonify({'ok': True}) if request.method == 'POST' else redirect(url_for('chat', u=u))

@app.route('/react_msg/<int:mid>/<int:u>/<emj>', methods=['GET', 'POST'])
def react_msg(mid, u, emj):
    if 'uid' not in session:
        return jsonify({'ok': False}), 401
    msg = db.session.get(Message, mid)
    if msg:
        msg.reaction = '' if msg.reaction == emj else emj
        db.session.commit()
    return jsonify({'ok': True}) if request.method == 'POST' else redirect(url_for('chat', u=u))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
