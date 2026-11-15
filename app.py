import os, traceback
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ramplez_clean_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez_final.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
os.makedirs('uploads', exist_ok=True)
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=False)
    bio = db.Column(db.String(200), default="В Ramplez")
    avatar = db.Column(db.Text, default="")
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    typing_to = db.Column(db.Integer, default=0)
    typing_at = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    msg_type = db.Column(db.String(20), default="text")
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default="")
    reaction = db.Column(db.String(20), default="")
    hidden_for = db.Column(db.Integer, default=0)
    is_read = db.Column(db.Boolean, default=False)
    is_pinned = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.String(20), default="")

with app.app_context():
    db.create_all()

@app.errorhandler(Exception)
def herr(e):
    return f"<pre>{traceback.format_exc()}</pre>", 500

@app.route('/uploads/<path:fn>')
def ufile(fn):
    return send_from_directory('uploads', fn)

@app.route('/', methods=['GET', 'POST'])
def login():
    uid = session.get('user_id')
    if uid and User.query.filter_by(id=int(uid)).first():
        return redirect(url_for('chat'))
    session.clear()
    err = None
    if request.method == 'POST':
        act, un, pw = request.form.get('action'), request.form.get('username', '').strip().lower(), request.form.get('password', '')
        if act == 'register':
            nm = request.form.get('name', '').strip()
            if User.query.filter_by(username=un).first(): err = "Ник занят!"
            elif un and nm and pw:
                u = User(username=un, name=nm, password=pw)
                db.session.add(u); db.session.commit(); session['user_id'] = u.id
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=un, password=pw).first()
            if u: session['user_id'] = u.id; return redirect(url_for('chat'))
            err = "Неверные данные!"
    return render_template('login.html', error=err)

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    uid = session.get('user_id')
    if not uid: return redirect(url_for('login'))
    me = User.query.filter_by(id=int(uid)).first()
    if not me: session.clear(); return redirect(url_for('login'))
    me.last_seen = datetime.utcnow()
    u_id = request.args.get('u', type=int)

    if request.method == 'POST' and u_id:
        txt = request.form.get('text', '').strip()
        if txt:
            db.session.add(Message(sender_id=me.id, receiver_id=u_id, msg_type='text', content=txt, timestamp=datetime.now().strftime("%H:%M")))
            me.typing_to = 0; db.session.commit()
        return redirect(url_for('chat', u=u_id))

    users_data = [{"u": u, "unread": Message.query.filter_by(sender_id=u.id, receiver_id=me.id, is_read=False).filter(Message.hidden_for != me.id).count()} for u in User.query.filter(User.id != me.id).all()]
    other = User.query.filter_by(id=u_id).first() if u_id else None
    msgs, pinned = [], None
    if other:
        Message.query.filter_by(sender_id=other.id, receiver_id=me.id, is_read=False).update({"is_read": True})
        db.session.commit()
        msgs = Message.query.filter((((Message.sender_id == me.id) & (Message.receiver_id == other.id)) | ((Message.sender_id == other.id) & (Message.receiver_id == me.id))) & (Message.hidden_for != me.id)).order_by(Message.id.asc()).all()
        pinned = next((m for m in reversed(msgs) if m.is_pinned), None)
    else:
        db.session.commit()
    return render_template('chat.html', current_user=me, users_data=users_data, active_user=other, messages=msgs, pinned=pinned)

@app.route('/save_profile', methods=['POST'])
def save_profile():
    uid = session.get('user_id')
    if not uid: return redirect(url_for('login'))
    u = User.query.filter_by(id=int(uid)).first()
    if u:
        if request.form.get('name', '').strip(): u.name = request.form.get('name').strip()
        u.bio = request.form.get('bio', '').strip()
        f = request.files.get('avatar_file')
        if f and f.filename:
            fn = f"av_{u.id}_{int(datetime.now().timestamp())}_{secure_filename(f.filename)}"
            f.save(os.path.join('uploads', fn))
            u.avatar = f"/uploads/{fn}"
        db.session.commit()
    return redirect(request.referrer or url_for('chat'))

@app.route('/upload_file', methods=['POST'])
def upload_file():
    uid = session.get('user_id')
    r_id, f = request.form.get('receiver_id', type=int), request.files.get('file')
    if uid and r_id and f and f.filename:
        fn = f"{int(datetime.now().timestamp())}_{secure_filename(f.filename)}"
        f.save(os.path.join('uploads', fn))
        ext = fn.lower().split('.')[-1]
        mt = 'image' if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else ('audio' if ext in ['webm', 'mp3', 'ogg', 'wav'] else 'file')
        db.session.add(Message(sender_id=int(uid), receiver_id=r_id, msg_type=mt, content=f"/uploads/{fn}", file_name=f.filename, timestamp=datetime.now().strftime("%H:%M")))
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400

@app.route('/typing/<int:oid>', methods=['POST'])
def styp(oid):
    uid = session.get('user_id')
    if uid:
        me = User.query.filter_by(id=int(uid)).first()
        if me: me.typing_to, me.typing_at, me.last_seen = oid, datetime.utcnow(), datetime.utcnow(); db.session.commit()
    return jsonify({"ok": True})

@app.route('/poll/<int:oid>')
def poll(oid):
    uid = session.get('user_id')
    if not uid: return jsonify({"msgs": []})
    mid = int(uid)
    me = User.query.filter_by(id=mid).first()
    if me: me.last_seen = datetime.utcnow()
    Message.query.filter_by(sender_id=oid, receiver_id=mid, is_read=False).update({"is_read": True})
    db.session.commit()
    oth = User.query.filter_by(id=oid).first()
    now = datetime.utcnow()
    msgs = Message.query.filter((((Message.sender_id == mid) & (Message.receiver_id == oid)) | ((Message.sender_id == oid) & (Message.receiver_id == mid))) & (Message.hidden_for != mid)).order_by(Message.id.asc()).all()
    return jsonify({
        "online": bool(oth and oth.last_seen and (now - oth.last_seen).total_seconds() < 15),
        "typing": bool(oth and oth.typing_to == mid and oth.typing_at and (now - oth.typing_at).total_seconds() < 4),
        "msgs": [{"id": m.id, "mine": m.sender_id == mid, "type": m.msg_type, "content": m.content, "fname": m.file_name, "react": m.reaction, "read": m.is_read, "time": m.timestamp} for m in msgs]
    })

@app.route('/pin/<int:mid>/<int:u>')
def pin(mid, u):
    if session.get('user_id'):
        m = Message.query.filter_by(id=mid).first()
        if m: m.is_pinned = not m.is_pinned; db.session.commit()
    return redirect(url_for('chat', u=u))

@app.route('/del_me/<int:mid>/<int:u>')
def del_me(mid, u):
    uid = session.get('user_id')
    if uid:
        m = Message.query.filter_by(id=mid).first()
        if m:
            if m.hidden_for and m.hidden_for != int(uid): db.session.delete(m)
            else: m.hidden_for = int(uid)
            db.session.commit()
    return redirect(url_for('chat', u=u))

@app.route('/del_all/<int:mid>/<int:u>')
def del_all(mid, u):
    if session.get('user_id'):
        m = Message.query.filter_by(id=mid).first()
        if m: db.session.delete(m); db.session.commit()
    return redirect(url_for('chat', u=u))

@app.route('/react/<int:mid>/<int:u>/<emj>')
def react(mid, u, emj):
    if session.get('user_id'):
        m = Message.query.filter_by(id=mid).first()
        if m: m.reaction = "" if m.reaction == emj else emj; db.session.commit()
    return redirect(url_for('chat', u=u))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
