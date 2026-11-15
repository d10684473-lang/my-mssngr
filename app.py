import os, traceback
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ramplez_v5_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez_v5.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
os.makedirs('uploads', exist_ok=True)

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=False)
    bio = db.Column(db.String(200), default="Привет! Я в Ramplez.")
    avatar = db.Column(db.Text, default="")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, nullable=False)
    receiver_id = db.Column(db.Integer, nullable=False)
    msg_type = db.Column(db.String(20), default="text")
    content = db.Column(db.Text, nullable=False)
    file_name = db.Column(db.String(200), default="")
    reaction = db.Column(db.String(20), default="")
    hidden_for = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.String(20), default="")

with app.app_context():
    db.create_all()

@app.errorhandler(Exception)
def handle_err(e):
    return f"<pre style='padding:20px'>{traceback.format_exc()}</pre>", 500

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory('uploads', filename)

@app.route('/', methods=['GET', 'POST'])
def login():
    uid = session.get('user_id')
    if uid and User.query.filter_by(id=int(uid)).first():
        return redirect(url_for('chat'))
    session.clear()
    err = None
    if request.method == 'POST':
        act = request.form.get('action')
        un = request.form.get('username', '').strip().lower()
        pw = request.form.get('password', '')
        if act == 'register':
            nm = request.form.get('name', '').strip()
            if User.query.filter_by(username=un).first():
                err = "Никнейм занят!"
            elif un and nm and pw:
                u = User(username=un, name=nm, password=pw)
                db.session.add(u)
                db.session.commit()
                session['user_id'] = u.id
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=un, password=pw).first()
            if u:
                session['user_id'] = u.id
                return redirect(url_for('chat'))
            err = "Неверный логин или пароль!"
    return render_template('login.html', error=err)

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    uid = session.get('user_id')
    if not uid:
        return redirect(url_for('login'))
    me = User.query.filter_by(id=int(uid)).first()
    if not me:
        session.clear()
        return redirect(url_for('login'))

    u_id = request.args.get('u', type=int)
    if request.method == 'POST' and u_id:
        txt = request.form.get('text', '').strip()
        if txt:
            m = Message(sender_id=me.id, receiver_id=u_id, msg_type='text', content=txt, timestamp=datetime.now().strftime("%H:%M"))
            db.session.add(m)
            db.session.commit()
        return redirect(url_for('chat', u=u_id))

    users = User.query.filter(User.id != me.id).all()
    other = User.query.filter_by(id=u_id).first() if u_id else None
    msgs = []
    if other:
        msgs = Message.query.filter(
            (((Message.sender_id == me.id) & (Message.receiver_id == other.id)) |
             ((Message.sender_id == other.id) & (Message.receiver_id == me.id))) &
            (Message.hidden_for != me.id)
        ).order_by(Message.id.asc()).all()
    return render_template('chat.html', current_user=me, users=users, active_user=other, messages=msgs)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    uid = session.get('user_id')
    if not uid:
        return jsonify({"ok": False}), 401
    r_id = request.form.get('receiver_id', type=int)
    f = request.files.get('file')
    if r_id and f and f.filename:
        safe_name = f"{int(datetime.now().timestamp())}_{secure_filename(f.filename)}"
        f.save(os.path.join('uploads', safe_name))
        ext = safe_name.lower().split('.')[-1]
        m_type = 'image' if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else ('audio' if ext in ['webm', 'mp3', 'ogg', 'wav'] else 'file')
        m = Message(sender_id=int(uid), receiver_id=r_id, msg_type=m_type, content=f"/uploads/{safe_name}", file_name=f.filename, timestamp=datetime.now().strftime("%H:%M"))
        db.session.add(m)
        db.session.commit()
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400

@app.route('/update_profile', methods=['POST'])
def update_profile():
    uid = session.get('user_id')
    if not uid:
        return jsonify({"ok": False}), 401
    u = User.query.filter_by(id=int(uid)).first()
    d = request.get_json(silent=True) or {}
    if u:
        if d.get('name', '').strip(): u.name = d['name'].strip()
        if 'bio' in d: u.bio = d['bio'].strip()
        if d.get('avatar'): u.avatar = d['avatar']
        db.session.commit()
    return jsonify({"ok": True})

@app.route('/del_me/<int:mid>/<int:uid_chat>')
def del_me(mid, uid_chat):
    uid = session.get('user_id')
    if uid:
        m = Message.query.filter_by(id=mid).first()
        if m:
            if m.hidden_for and m.hidden_for != int(uid):
                db.session.delete(m)
            else:
                m.hidden_for = int(uid)
            db.session.commit()
    return redirect(url_for('chat', u=uid_chat))

@app.route('/del_all/<int:mid>/<int:uid_chat>')
def del_all(mid, uid_chat):
    if session.get('user_id'):
        m = Message.query.filter_by(id=mid).first()
        if m:
            db.session.delete(m)
            db.session.commit()
    return redirect(url_for('chat', u=uid_chat))

@app.route('/react/<int:mid>/<int:uid_chat>/<emj>')
def react(mid, uid_chat, emj):
    if session.get('user_id'):
        m = Message.query.filter_by(id=mid).first()
        if m:
            m.reaction = "" if m.reaction == emj else emj
            db.session.commit()
    return redirect(url_for('chat', u=uid_chat))

@app.route('/msg_count/<int:other_id>')
def msg_count(other_id):
    uid = session.get('user_id')
    if not uid: return jsonify({"count": 0})
    my_id = int(uid)
    cnt = Message.query.filter(
        (((Message.sender_id == my_id) & (Message.receiver_id == other_id)) |
         ((Message.sender_id == other_id) & (Message.receiver_id == my_id))) &
        (Message.hidden_for != my_id)
    ).count()
    return jsonify({"count": cnt})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
