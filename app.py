import os, time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'whatsapp-meta-core-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///whatsapp.db?timeout=30'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db = SQLAlchemy(app)

@event.listens_for(Engine, "connect")
def _sqlite_wal(dbapi_conn, _):
    c = dbapi_conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.close()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    bio = db.Column(db.String(200), default='Привет! Я использую WhatsApp.')
    avatar = db.Column(db.Text, default='')
    typing_to = db.Column(db.Integer, default=0)
    typing_at = db.Column(db.Float, default=0.0)
    last_seen = db.Column(db.Float, default=0.0)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default='')
    reaction = db.Column(db.String(20), default='')
    hidden_for = db.Column(db.String(50), default='')
    is_read = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.Float, default=0.0)
    timestamp = db.Column(db.String(10), default='')

with app.app_context():
    db.create_all()

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session: return redirect(url_for('chat'))
    error = None
    if request.method == 'POST':
        act = request.form.get('action', 'login')
        uname = request.form.get('username', '').strip().lower()
        pwd = request.form.get('password', '')
        name = request.form.get('name', '').strip() or uname
        if not uname or not pwd: error = 'Заполните все поля'
        elif act == 'register':
            if User.query.filter_by(username=uname).first(): error = 'Этот логин уже занят'
            else:
                u = User(username=uname, name=name, password=generate_password_hash(pwd), last_seen=time.time())
                db.session.add(u); db.session.commit(); session['uid'] = u.id
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=uname).first()
            if u and check_password_hash(u.password, password=pwd):
                session['uid'] = u.id; return redirect(url_for('chat'))
            error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/chat')
def chat():
    if 'uid' not in session: return redirect(url_for('index'))
    me = db.session.get(User, session['uid'])
    if not me: session.clear(); return redirect(url_for('index'))
    me.last_seen = time.time(); db.session.commit()
    users = User.query.filter(User.id != me.id).all()
    oid = request.args.get('u', type=int) or (users[0].id if users else 0)
    return render_template('chat.html', me=me, users=users, other=db.session.get(User, oid) if oid else None)

@app.route('/send', methods=['POST'])
def send():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    d = request.get_json(silent=True) or {}
    oid, content, mtype = int(d.get('oid', 0)), str(d.get('content', '')).strip(), str(d.get('msg_type', 'text'))
    ttl = int(d.get('ttl', 0))
    if not oid or not content: return jsonify({'ok': False}), 400
    now_str = datetime.now().strftime('%H:%M')
    exp = (time.time() + ttl) if ttl > 0 else 0.0
    msg = Message(sender_id=session['uid'], receiver_id=oid, msg_type=mtype, content=content, expires_at=exp, timestamp=now_str)
    db.session.add(msg)
    if content.lower().startswith('/ai '):
        q = content[4:].strip()
        db.session.add(Message(sender_id=oid, receiver_id=session['uid'], msg_type='text', content=f"✨ Meta AI:\nПо запросу «{q}»: задача принята в обработку, сквозное шифрование активно.", timestamp=now_str))
    me = db.session.get(User, session['uid'])
    if me: me.typing_to = 0; me.last_seen = time.time()
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id, 'timestamp': now_str})

@app.route('/poll/<int:oid>')
def poll(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    uid, now = session['uid'], time.time()
    me = db.session.get(User, uid)
    if me: me.last_seen = now
    Message.query.filter((Message.expires_at > 0) & (Message.expires_at <= now)).delete()
    for u_msg in Message.query.filter_by(sender_id=oid, receiver_id=uid, is_read=False).all():
        u_msg.is_read = True
    db.session.commit()

    msgs = Message.query.filter(((Message.sender_id == uid) & (Message.receiver_id == oid)) | ((Message.sender_id == oid) & (Message.receiver_id == uid))).order_by(Message.id.asc()).all()
    out = [{'id': m.id, 'sender_id': m.sender_id, 'msg_type': m.msg_type, 'content': m.content, 'file_name': m.file_name, 'reaction': m.reaction, 'is_read': m.is_read, 'ttl': m.expires_at > 0, 'timestamp': m.timestamp} for m in msgs if str(uid) not in (m.hidden_for.split(',') if m.hidden_for else [])]
    other = db.session.get(User, oid)
    return jsonify({'ok': True, 'messages': out, 'typing': bool(other and other.typing_to == uid and (now - (other.typing_at or 0)) < 3.0), 'online': bool(other and (now - (other.last_seen or 0)) < 5.0)})

@app.route('/typing/<int:oid>', methods=['POST'])
def typing(oid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    me = db.session.get(User, session['uid'])
    if me: me.typing_to = oid; me.typing_at = time.time(); db.session.commit()
    return jsonify({'ok': True})

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    oid, f = request.form.get('oid', type=int), request.files.get('file')
    if not oid or not f or not f.filename: return jsonify({'ok': False}), 400
    orig = secure_filename(f.filename) or 'file'
    saved = f"{int(time.time() * 1000)}_{orig}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], saved))
    ext = orig.rsplit('.', 1)[-1].lower() if '.' in orig else ''
    mtype = 'image' if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp') else ('audio' if ext in ('webm', 'ogg', 'mp3', 'wav', 'm4a') else 'file')
    db.session.add(Message(sender_id=session['uid'], receiver_id=oid, msg_type=mtype, content=url_for('uploaded_file', fn=saved), file_name=f.filename, timestamp=datetime.now().strftime('%H:%M')))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/uploads/<path:fn>')
def uploaded_file(fn): return send_from_directory(app.config['UPLOAD_FOLDER'], fn)

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'uid' not in session: return jsonify({'ok': False}), 401
    d, me = request.get_json(silent=True) or {}, db.session.get(User, session['uid'])
    if me:
        if str(d.get('name', '')).strip(): me.name = str(d.get('name')).strip()[:80]
        me.bio = str(d.get('bio', '')).strip()[:200]
        if str(d.get('avatar', '')).startswith('data:image/'): me.avatar = d.get('avatar')
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/action/<act>/<int:mid>', methods=['POST'])
def msg_action(act, mid):
    if 'uid' not in session: return jsonify({'ok': False}), 401
    m = db.session.get(Message, mid)
    if not m: return jsonify({'ok': False}), 404
    if act == 'del_me':
        h = set(m.hidden_for.split(',')) if m.hidden_for else set(); h.discard(''); h.add(str(session['uid']))
        m.hidden_for = ','.join(h)
    elif act == 'del_all' and m.sender_id == session['uid']:
        db.session.delete(m)
    elif act.startswith('react_'):
        emj = act[6:]
        m.reaction = '' if m.reaction == emj else emj
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
