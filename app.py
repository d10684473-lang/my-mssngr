import traceback
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ramplez_rock_solid_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez_v4.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

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
    reaction = db.Column(db.String(20), default="")
    timestamp = db.Column(db.String(20), default="")

with app.app_context():
    db.create_all()

@app.errorhandler(Exception)
def handle_exception(e):
    return f"<pre style='padding:20px;font-family:monospace;'>Ошибка сервера:\n{traceback.format_exc()}</pre>", 500

@app.route('/', methods=['GET', 'POST'])
def login():
    uid = session.get('user_id')
    if uid:
        user = User.query.filter_by(id=int(uid)).first()
        if user:
            return redirect(url_for('chat'))
        session.clear()

    error = None
    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        if action == 'register':
            name = request.form.get('name', '').strip()
            if not username or not name or not password:
                error = "Заполни все поля!"
            elif User.query.filter_by(username=username).first():
                error = "Этот @username уже занят!"
            else:
                u = User(username=username, name=name, password=password)
                db.session.add(u)
                db.session.commit()
                session['user_id'] = u.id
                return redirect(url_for('chat'))
        else:
            u = User.query.filter_by(username=username, password=password).first()
            if u:
                session['user_id'] = u.id
                return redirect(url_for('chat'))
            error = "Неверный логин или пароль!"

    return render_template('login.html', error=error)

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    uid = session.get('user_id')
    if not uid:
        return redirect(url_for('login'))

    current_user = User.query.filter_by(id=int(uid)).first()
    if not current_user:
        session.clear()
        return redirect(url_for('login'))

    u_id = request.args.get('u', type=int)

    if request.method == 'POST' and u_id:
        text = request.form.get('text', '').strip()
        if text:
            now_str = datetime.now().strftime("%H:%M")
            m = Message(sender_id=current_user.id, receiver_id=u_id, msg_type='text', content=text, timestamp=now_str)
            db.session.add(m)
            db.session.commit()
        return redirect(url_for('chat', u=u_id))

    users = User.query.filter(User.id != current_user.id).all()
    active_user = User.query.filter_by(id=u_id).first() if u_id else None
    messages = []

    if active_user:
        messages = Message.query.filter(
            ((Message.sender_id == current_user.id) & (Message.receiver_id == active_user.id)) |
            ((Message.sender_id == active_user.id) & (Message.receiver_id == current_user.id))
        ).order_by(Message.id.asc()).all()

    return render_template('chat.html', current_user=current_user, users=users, active_user=active_user, messages=messages)

@app.route('/send_media', methods=['POST'])
def send_media():
    uid = session.get('user_id')
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    r_id = int(data.get('receiver_id', 0))
    m_type = data.get('msg_type', 'text')
    content = data.get('content', '')
    if r_id and content:
        now_str = datetime.now().strftime("%H:%M")
        m = Message(sender_id=int(uid), receiver_id=r_id, msg_type=m_type, content=content, timestamp=now_str)
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
    data = request.get_json(silent=True) or {}
    if u:
        if data.get('name') and data['name'].strip():
            u.name = data['name'].strip()
        if 'bio' in data:
            u.bio = data['bio'].strip()
        if data.get('avatar'):
            u.avatar = data['avatar']
        db.session.commit()
    return jsonify({"ok": True})

@app.route('/delete_msg/<int:msg_id>/<int:u_id>')
def delete_msg(msg_id, u_id):
    uid = session.get('user_id')
    if uid:
        m = Message.query.filter_by(id=msg_id).first()
        if m and m.sender_id == int(uid):
            db.session.delete(m)
            db.session.commit()
    return redirect(url_for('chat', u=u_id))

@app.route('/react_msg/<int:msg_id>/<int:u_id>/<emoji>')
def react_msg(msg_id, u_id, emoji):
    if session.get('user_id'):
        m = Message.query.filter_by(id=msg_id).first()
        if m:
            m.reaction = "" if m.reaction == emoji else emoji
            db.session.commit()
    return redirect(url_for('chat', u=u_id))

@app.route('/msg_count/<int:other_id>')
def msg_count(other_id):
    uid = session.get('user_id')
    if not uid:
        return jsonify({"count": 0})
    my_id = int(uid)
    cnt = Message.query.filter(
        ((Message.sender_id == my_id) & (Message.receiver_id == other_id)) |
        ((Message.sender_id == other_id) & (Message.receiver_id == my_id))
    ).count()
    return jsonify({"count": cnt})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
