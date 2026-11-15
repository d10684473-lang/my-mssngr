from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ramplez_v3_secret_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ramplez_v3.db'
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
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    msg_type = db.Column(db.String(20), default="text")
    content = db.Column(db.Text, nullable=False)
    reaction = db.Column(db.String(20), default="")
    timestamp = db.Column(db.String(20), default=lambda: datetime.now().strftime("%H:%M"))

with app.app_context():
    db.create_all()

@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
        if user:
            return redirect(url_for('chat'))
        session.pop('user_id', None)
    
    error = None
    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        if action == 'register':
            name = request.form.get('name', '').strip()
            if User.query.filter_by(username=username).first():
                error = "Этот @username уже занят!"
            else:
                new_user = User(username=username, name=name, password=password)
                db.session.add(new_user)
                db.session.commit()
                session['user_id'] = new_user.id
                return redirect(url_for('chat'))
        elif action == 'login':
            user = User.query.filter_by(username=username, password=password).first()
            if user:
                session['user_id'] = user.id
                return redirect(url_for('chat'))
            else:
                error = "Неверный логин или пароль!"

    return render_template('login.html', error=error)

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_user = db.session.get(User, session['user_id'])
    if not current_user:
        session.pop('user_id', None)
        return redirect(url_for('login'))

    current_user.last_seen = datetime.utcnow()
    db.session.commit()

    active_user_id = request.args.get('u', type=int)

    # Прямая отправка текста через форму (работает даже если JS отключён)
    if request.method == 'POST' and active_user_id:
        text = request.form.get('text', '').strip()
        if text:
            new_msg = Message(sender_id=current_user.id, receiver_id=active_user_id, msg_type='text', content=text)
            db.session.add(new_msg)
            db.session.commit()
        return redirect(url_for('chat', u=active_user_id))

    users = User.query.filter(User.id != current_user.id).all()
    active_user = db.session.get(User, active_user_id) if active_user_id else None
    
    messages = []
    if active_user:
        messages = Message.query.filter(
            ((Message.sender_id == current_user.id) & (Message.receiver_id == active_user.id)) |
            ((Message.sender_id == active_user.id) & (Message.receiver_id == current_user.id))
        ).order_by(Message.id.asc()).all()

    return render_template('chat.html', current_user=current_user, users=users, active_user=active_user, messages=messages)

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    user = db.session.get(User, session['user_id'])
    data = request.get_json()
    if data.get('name'):
        user.name = data['name'].strip()
    if 'bio' in data:
        user.bio = data['bio'].strip()
    if data.get('avatar'):
        user.avatar = data['avatar']
    db.session.commit()
    return jsonify({"status": "ok"})

@app.route('/send_media', methods=['POST'])
def send_media():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    data = request.get_json()
    receiver_id = int(data.get('receiver_id'))
    msg_type = data.get('msg_type', 'text')
    content = data.get('content', '').strip()
    if content:
        new_msg = Message(sender_id=session['user_id'], receiver_id=receiver_id, msg_type=msg_type, content=content)
        db.session.add(new_msg)
        db.session.commit()
        return jsonify({"status": "ok"})
    return jsonify({"status": "empty"}), 400

@app.route('/delete_msg/<int:msg_id>/<int:u_id>')
def delete_msg(msg_id, u_id):
    if 'user_id' in session:
        msg = db.session.get(Message, msg_id)
        if msg and msg.sender_id == session['user_id']:
            db.session.delete(msg)
            db.session.commit()
    return redirect(url_for('chat', u=u_id))

@app.route('/react_msg/<int:msg_id>/<int:u_id>/<emoji>')
def react_msg(msg_id, u_id, emoji):
    if 'user_id' in session:
        msg = db.session.get(Message, msg_id)
        if msg:
            msg.reaction = "" if msg.reaction == emoji else emoji
            db.session.commit()
    return redirect(url_for('chat', u=u_id))

@app.route('/get_msgs/<int:other_id>')
def get_msgs(other_id):
    if 'user_id' not in session:
        return jsonify({"messages": []})
    my_id = session['user_id']
    me = db.session.get(User, my_id)
    if me:
        me.last_seen = datetime.utcnow()
        db.session.commit()

    other = db.session.get(User, other_id)
    is_online = False
    if other and other.last_seen:
        is_online = (datetime.utcnow() - other.last_seen).total_seconds() < 15

    msgs = Message.query.filter(
        ((Message.sender_id == my_id) & (Message.receiver_id == other_id)) |
        ((Message.sender_id == other_id) & (Message.receiver_id == my_id))
    ).order_by(Message.id.asc()).all()

    return jsonify({
        "is_online": is_online,
        "count": len(msgs)
    })

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
