from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super_secret_key_999'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///messenger.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Таблица пользователей
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=False)
    bio = db.Column(db.String(200), default="Привет! Я использую новый мессенджер.")

# Таблица сообщений
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
        return redirect(url_for('chat'))
    
    error = None
    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username').strip().lower()
        password = request.form.get('password')

        if action == 'register':
            name = request.form.get('name').strip()
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
    
    current_user = User.query.get(session['user_id'])
    # Получаем всех остальных пользователей для списка контактов
    users = User.query.filter(User.id != current_user.id).all()
    return render_template('chat.html', current_user=current_user, users=users)

@app.route('/get_messages/<int:other_user_id>')
def get_messages(other_user_id):
    if 'user_id' not in session:
        return {"messages": []}
    
    my_id = session['user_id']
    msgs = Message.query.filter(
        ((Message.sender_id == my_id) & (Message.receiver_id == other_user_id)) |
        ((Message.sender_id == other_user_id) & (Message.receiver_id == my_id))
    ).order_by(Message.id.asc()).all()

    return {
        "messages": [
            {
                "sender_id": m.sender_id,
                "text": m.text,
                "time": m.timestamp
            } for m in msgs
        ]
    }

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

@socketio.on('join')
def on_join(data):
    join_room(str(data['user_id']))

@socketio.on('private_message')
def handle_private_message(data):
    sender_id = data['sender_id']
    receiver_id = data['receiver_id']
    text = data['text']

    new_msg = Message(sender_id=sender_id, receiver_id=receiver_id, text=text)
    db.session.add(new_msg)
    db.session.commit()

    msg_data = {
        'sender_id': sender_id,
        'receiver_id': receiver_id,
        'text': text,
        'time': new_msg.timestamp
    }

    # Отправляем сообщение и получателю, и отправителю
    emit('new_message', msg_data, room=str(receiver_id))
    emit('new_message', msg_data, room=str(sender_id))

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
