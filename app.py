from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ramplez_secret_key_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messenger.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=False)
    bio = db.Column(db.String(200), default="Привет! Я в Ramplez.")

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.String(20), default=lambda: datetime.now().strftime("%H:%M"))

with app.app_context():
    db.create_all()

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

@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    current_user = db.session.get(User, session['user_id'])
    if not current_user:
        session.pop('user_id', None)
        return redirect(url_for('login'))

    users = User.query.filter(User.id != current_user.id).all()
    active_user_id = request.args.get('u', type=int)
    active_user = db.session.get(User, active_user_id) if active_user_id else None
    
    messages = []
    if active_user:
        messages = Message.query.filter(
            ((Message.sender_id == current_user.id) & (Message.receiver_id == active_user.id)) |
            ((Message.sender_id == active_user.id) & (Message.receiver_id == current_user.id))
        ).order_by(Message.id.asc()).all()

    return render_template('chat.html', current_user=current_user, users=users, active_user=active_user, messages=messages)

@app.route('/send_msg', methods=['POST'])
def send_msg():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    
    data = request.get_json()
    receiver_id = int(data.get('receiver_id'))
    text = data.get('text', '').strip()
    
    if text:
        new_msg = Message(sender_id=session['user_id'], receiver_id=receiver_id, text=text)
        db.session.add(new_msg)
        db.session.commit()
        return jsonify({"status": "ok", "id": new_msg.id, "text": new_msg.text, "time": new_msg.timestamp})
    return jsonify({"status": "empty"}), 400

@app.route('/get_msgs/<int:other_id>')
def get_msgs(other_id):
    if 'user_id' not in session:
        return jsonify({"messages": []})
    
    my_id = session['user_id']
    msgs = Message.query.filter(
        ((Message.sender_id == my_id) & (Message.receiver_id == other_id)) |
        ((Message.sender_id == other_id) & (Message.receiver_id == my_id))
    ).order_by(Message.id.asc()).all()

    return jsonify({
        "messages": [
            {"id": m.id, "sender_id": m.sender_id, "text": m.text, "time": m.timestamp}
            for m in msgs
        ]
    })

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
