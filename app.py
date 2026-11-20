# -*- coding: utf-8 -*-
"""Ramplez 3.0 — realtime messenger backend."""
import os, json, time, re, hmac, secrets, queue, threading, zlib, struct
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, session,
                   jsonify, send_from_directory, abort, Response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, text, func, and_, or_
from sqlalchemy.engine import Engine
from sqlalchemy.orm import aliased
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
_db = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ramplez.db'))
if _db.startswith('postgres://'): _db = _db.replace('postgres://', 'postgresql://', 1)
if _db.startswith('sqlite'): _db += ('&' if '?' in _db else '?') + 'timeout=30&check_same_thread=False'
app.config.update(SQLALCHEMY_DATABASE_URI=_db, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                  SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True},
                  UPLOAD_FOLDER=os.environ.get('UPLOAD_FOLDER', 'uploads'),
                  MAX_CONTENT_LENGTH=32 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.environ.get('HTTPS', '1') == '1',
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.json.ensure_ascii = False
PAGE = 60
IMG_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
AUD_EXT = {'webm', 'ogg', 'mp3', 'wav', 'm4a', 'mp4', 'aac'}
DOC_EXT = {'pdf', 'txt', 'zip', 'rar', '7z', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'csv', 'json', 'apk'}
ALLOWED = IMG_EXT | AUD_EXT | DOC_EXT
EMOJI = ['👍', '❤️', '😂', '🔥', '😮', '😢', '🎉', '👎']
MAXLEN = 4096


@event.listens_for(Engine, 'connect')
def _pragma(conn, rec):
    try:
        c = conn.cursor()
        c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA busy_timeout=8000'); c.close()
    except Exception:
        pass


db = SQLAlchemy(app)
now = lambda: time.time()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, index=True, nullable=False)
    name = db.Column(db.String(64), nullable=False)
    pw = db.Column(db.String(255), nullable=False)
    bio = db.Column(db.String(200), default='')
    avatar = db.Column(db.Text, default='')
    prefs = db.Column(db.Text, default='{}')
    last_seen = db.Column(db.Float, default=0.0)
    onboarded = db.Column(db.Integer, default=0)
    created_at = db.Column(db.Float, default=now)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), index=True, nullable=False)
    sender_id = db.Column(db.Integer, index=True, nullable=False)
    kind = db.Column(db.String(10), default='text')
    body = db.Column(db.Text, default='')
    fname = db.Column(db.String(160), default='')
    fsize = db.Column(db.Integer, default=0)
    reply_to = db.Column(db.Integer, default=0)
    fwd = db.Column(db.String(64), default='')
    pinned = db.Column(db.Integer, default=0)
    hidden = db.Column(db.Text, default='')
    created_at = db.Column(db.Float, default=now, index=True)
    updated_at = db.Column(db.Float, default=now, index=True)
    edited_at = db.Column(db.Float, default=0.0)


class Reaction(db.Model):
    mid = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.Integer, primary_key=True)
    emoji = db.Column(db.String(8), nullable=False)


class ReadState(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    uid = db.Column(db.Integer, primary_key=True)
    last_id = db.Column(db.Integer, default=0)
    at = db.Column(db.Float, default=now)


class Tomb(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), index=True, nullable=False)
    mid = db.Column(db.Integer, nullable=False)
    at = db.Column(db.Float, default=now, index=True)


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(80), nullable=False)
    avatar = db.Column(db.Text, default='')
    owner_id = db.Column(db.Integer, nullable=False)
    code = db.Column(db.String(16), unique=True, index=True)
    created_at = db.Column(db.Float, default=now)


class Member(db.Model):
    gid = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.Integer, primary_key=True, index=True)
    role = db.Column(db.String(10), default='member')


class Block(db.Model):
    uid = db.Column(db.Integer, primary_key=True)
    bid = db.Column(db.Integer, primary_key=True)


with app.app_context():
    db.create_all()

_subs, _lock = {}, threading.Lock()
_presence, _typing, _rl, _memcache = {}, {}, {}, {}


def publish(uids, ev, data):
    with _lock:
        for u in set(uids):
            for q in _subs.get(u, [])[:6]:
                try: q.put_nowait((ev, data))
                except queue.Full: pass


def touch(uid): _presence[uid] = now()
def online(uid): return now() - _presence.get(uid, 0) < 45


def ratelimit(uid, bucket, limit, window):
    k = (uid, bucket); t = now()
    arr = [x for x in _rl.get(k, []) if t - x < window]
    if len(arr) >= limit: return False
    arr.append(t); _rl[k] = arr; return True


def members(gid):
    c = _memcache.get(gid)
    if c and now() - c[0] < 10: return c[1]
    ids = [m.uid for m in Member.query.filter_by(gid=gid).all()]
    _memcache[gid] = (now(), ids)
    return ids


def pkey(a, b): return 'p:%d:%d' % (min(a, b), max(a, b))


def resolve(uid, key):
    try:
        if key.startswith('g:'):
            gid = int(key[2:])
            if uid not in members(gid): return None
            return {'kind': 'group', 'key': key, 'gid': gid, 'peers': members(gid)}
        if key.startswith('p:'):
            _, a, b = key.split(':'); a, b = int(a), int(b)
            if uid not in (a, b): return None
            return {'kind': 'personal', 'key': key, 'other': b if a == uid else a, 'peers': [a, b]}
    except Exception:
        return None
    return None


def blocked(a, b):
    return Block.query.filter(or_(and_(Block.uid == a, Block.bid == b),
                                  and_(Block.uid == b, Block.bid == a))).first() is not None


def msg_access(uid, m): return resolve(uid, m.key) is not None
def hidden_list(m): return [x for x in (m.hidden or '').split(',') if x]
def visible(uid, m): return str(uid) not in hidden_list(m)
def bump(m): m.updated_at = now()
def notify(ctx, ev, data): publish(ctx['peers'], ev, data)
def jget(): return request.get_json(silent=True) or {}


def ival(v, d=0):
    try: return int(v)
    except Exception: return d


def fval(v, d=0.0):
    try: return float(v)
    except Exception: return d


def pack(m, rmap):
    return {'id': m.id, 'key': m.key, 'sender': m.sender_id, 'kind': m.kind, 'body': m.body,
            'fname': m.fname, 'fsize': m.fsize, 'reply': m.reply_to or 0, 'fwd': m.fwd or '',
            'pinned': m.pinned, 'created': m.created_at, 'updated': m.updated_at,
            'edited': m.edited_at or 0, 'reactions': rmap.get(m.id, [])}


def reactions_for(ids):
    out = {}
    if not ids: return out
    for r in Reaction.query.filter(Reaction.mid.in_(ids)).all():
        out.setdefault(r.mid, []).append({'u': r.uid, 'e': r.emoji})
    return out


def reads_for(key):
    return {r.uid: r.last_id for r in ReadState.query.filter_by(key=key).all()}


def my_keys(uid):
    ks = [pkey(uid, uid)]
    rows = db.session.query(Message.key).filter(
        or_(Message.key.like('p:%d:%%' % uid), Message.key.like('p:%%:%d' % uid))).distinct().all()
    for (k,) in rows:
        p = k.split(':')
        if len(p) == 3 and str(uid) in p[1:] and k not in ks: ks.append(k)
    for m in Member.query.filter_by(uid=uid).all(): ks.append('g:%d' % m.gid)
    return ks


def user_brief(u):
    return {'id': u.id, 'name': u.name, 'username': u.username, 'avatar': u.avatar or '',
            'bio': u.bio or '', 'online': online(u.id), 'last_seen': u.last_seen or 0}


def conversations(uid):
    keys = my_keys(uid)
    sub = db.session.query(func.max(Message.id).label('mid')).filter(
        Message.key.in_(keys)).group_by(Message.key).subquery()
    lasts = {m.key: m for m in Message.query.filter(Message.id.in_(db.session.query(sub.c.mid))).all()}
    rs = aliased(ReadState)
    unread = dict(db.session.query(Message.key, func.count(Message.id))
                  .outerjoin(rs, and_(rs.key == Message.key, rs.uid == uid))
                  .filter(Message.key.in_(keys), Message.sender_id != uid,
                          Message.id > func.coalesce(rs.last_id, 0)).group_by(Message.key).all())
    ids = set()
    for k in keys:
        if k.startswith('p:'):
            a, b = int(k.split(':')[1]), int(k.split(':')[2])
            ids.add(b if a == uid else a)
    names = {u.id: u for u in User.query.filter(User.id.in_(ids)).all()} if ids else {}
    out = []
    for k in keys:
        last = lasts.get(k)
        prev = (last.body if last and last.kind == 'text' else ('📎 ' + (last.fname or last.kind) if last else ''))[:60]
        if k.startswith('g:'):
            g = db.session.get(Group, int(k[2:]))
            if not g: continue
            out.append({'key': k, 'type': 'group', 'id': g.id, 'title': g.title, 'avatar': g.avatar or '',
                        'online': False, 'unread': unread.get(k, 0), 'sub': prev or 'Группа',
                        'at': last.created_at if last else g.created_at})
        else:
            a, b = int(k.split(':')[1]), int(k.split(':')[2])
            oid = b if a == uid else a
            if oid == uid:
                out.append({'key': k, 'type': 'saved', 'id': uid, 'title': 'Избранное', 'avatar': '',
                            'online': False, 'unread': 0, 'sub': prev or 'Заметки себе',
                            'at': last.created_at if last else 0})
                continue
            u = names.get(oid)
            if not u: continue
            out.append({'key': k, 'type': 'user', 'id': oid, 'title': u.name, 'avatar': u.avatar or '',
                        'online': online(oid), 'unread': unread.get(k, 0), 'sub': prev or '@' + u.username,
                        'at': last.created_at if last else 0})
    out.sort(key=lambda c: -(c['at'] or 0))
    return out


def envelope(uid, ctx, msgs, since, extra=None):
    rmap = reactions_for([m.id for m in msgs])
    rids = list({m.reply_to for m in msgs if m.reply_to})
    quotes, names = {}, {}
    if rids:
        for r in Message.query.filter(Message.id.in_(rids)).all():
            s = db.session.get(User, r.sender_id)
            quotes[r.id] = {'id': r.id, 'name': s.name if s else '—', 'kind': r.kind,
                            'body': (r.body if r.kind == 'text' else '📎 ' + (r.fname or r.kind))[:90]}
    for sid in {m.sender_id for m in msgs}:
        s = db.session.get(User, sid)
        names[sid] = {'name': s.name if s else 'Аккаунт удалён', 'avatar': s.avatar if s else ''}
    data = {'ok': True, 'now': now(), 'msgs': [pack(m, rmap) for m in msgs], 'quotes': quotes, 'names': names,
            'deleted': [t.mid for t in Tomb.query.filter(Tomb.key == ctx['key'], Tomb.at > since).all()] if since else [],
            'reads': reads_for(ctx['key']), 'typing': [], 'title': '', 'avatar': '', 'online': False,
            'members': [], 'pinned': []}
    t = now()
    if ctx['kind'] == 'group':
        g = db.session.get(Group, ctx['gid'])
        us = User.query.filter(User.id.in_(ctx['peers'])).all()
        roles = {m.uid: m.role for m in Member.query.filter_by(gid=ctx['gid']).all()}
        data['title'] = g.title if g else 'Группа'
        data['avatar'] = g.avatar if g else ''
        data['members'] = [dict(user_brief(u), role=roles.get(u.id, 'member')) for u in us]
        data['typing'] = [u.name for u in us if u.id != uid and t - _typing.get((ctx['key'], u.id), 0) < 4]
        data['code'] = g.code if g else ''
        data['owner'] = g.owner_id if g else 0
    else:
        o = db.session.get(User, ctx['other'])
        self_chat = ctx['other'] == uid
        data['title'] = 'Избранное' if self_chat else (o.name if o else 'Аккаунт удалён')
        data['avatar'] = o.avatar if o and not self_chat else ''
        data['online'] = online(ctx['other']) and not self_chat
        data['other'] = user_brief(o) if o else None
        data['typing'] = ['печатает'] if t - _typing.get((ctx['key'], ctx['other']), 0) < 4 else []
        data['blocked'] = blocked(uid, ctx['other']) if not self_chat else False
    pins = Message.query.filter_by(key=ctx['key'], pinned=1).order_by(Message.id.desc()).limit(5).all()
    data['pinned'] = [{'id': m.id, 'body': (m.body or m.fname or m.kind)[:80]} for m in pins]
    if extra: data.update(extra)
    return data


@app.before_request
def _csrf_seed():
    if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(24)


@app.before_request
def _csrf_check():
    if request.method in ('GET', 'HEAD', 'OPTIONS'): return
    if request.endpoint in ('index', 'join'): return
    if not hmac.compare_digest(session.get('csrf', ''), request.headers.get('X-CSRF', '')):
        return jsonify(ok=False, error='csrf'), 403


@app.after_request
def _headers(r):
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['X-Frame-Options'] = 'DENY'
    r.headers['Referrer-Policy'] = 'no-referrer'
    if request.path.startswith('/api/'): r.headers['Cache-Control'] = 'no-store'
    return r


def me():
    u = db.session.get(User, session['uid']) if 'uid' in session else None
    if u: touch(u.id)
    return u


def need(f):
    @wraps(f)
    def w(*a, **kw):
        u = me()
        if not u: return jsonify(ok=False, error='auth'), 401
        return f(u, *a, **kw)
    return w


DEFAULT_PREFS = {'theme': 'dark', 'accent': '#4f8cff', 'font': 15, 'radius': 18, 'sound': True,
                 'notify': True, 'wall': 'aurora', 'enter': True, 'seen': True}


def prefs_of(u):
    p = dict(DEFAULT_PREFS)
    try: p.update(json.loads(u.prefs or '{}'))
    except Exception: pass
    return p


@app.route('/', methods=['GET', 'POST'])
def index():
    if 'uid' in session and db.session.get(User, session['uid']): return redirect('/chat')
    err = None
    if request.method == 'POST':
        act = request.form.get('act', 'login')
        un = (request.form.get('username') or '').strip().lower()
        pw = request.form.get('password') or ''
        nm = (request.form.get('name') or '').strip()[:64] or un
        if not re.fullmatch(r'[a-z0-9_]{3,32}', un): err = 'Логин: 3–32 символа, a-z 0-9 _'
        elif len(pw) < 6: err = 'Пароль от 6 символов'
        elif act == 'register':
            if User.query.filter_by(username=un).first(): err = 'Логин занят'
            else:
                u = User(username=un, name=nm, pw=generate_password_hash(pw))
                db.session.add(u); db.session.commit()
                session.permanent = True; session['uid'] = u.id
                return redirect('/chat')
        else:
            u = User.query.filter_by(username=un).first()
            if u and check_password_hash(u.pw, pw):
                session.permanent = True; session['uid'] = u.id
                return redirect('/chat')
            err = 'Неверный логин или пароль'
    return render_template('login.html', err=err, csrf=session['csrf'])


@app.route('/logout')
def logout():
    session.clear(); return redirect('/')


@app.route('/join/<code>')
def join(code):
    g = Group.query.filter_by(code=code).first()
    if not g: return redirect('/chat')
    if 'uid' not in session:
        session['join'] = code; return redirect('/')
    if session['uid'] not in members(g.id):
        db.session.add(Member(gid=g.id, uid=session['uid'])); db.session.commit()
        _memcache.pop(g.id, None); publish(members(g.id), 'conv', {})
    return redirect('/chat?k=g:%d' % g.id)


@app.route('/chat')
def chat():
    u = me()
    if not u: return redirect('/')
    if session.get('join'):
        c = session.pop('join'); g = Group.query.filter_by(code=c).first()
        if g:
            if u.id not in members(g.id):
                db.session.add(Member(gid=g.id, uid=u.id)); db.session.commit(); _memcache.pop(g.id, None)
            return redirect('/chat?k=g:%d' % g.id)
        return redirect('/chat')
    key = request.args.get('k', '')
    ctx = resolve(u.id, key) if key else None
    u.last_seen = now(); db.session.commit()
    mej = {'id': u.id, 'name': u.name, 'username': u.username, 'avatar': u.avatar or '', 'bio': u.bio or ''}
    return render_template('chat.html', me=u, mej=mej, prefs=prefs_of(u), csrf=session['csrf'],
                           key=ctx['key'] if ctx else '', convs=conversations(u.id),
                           users=[user_brief(x) for x in User.query.filter(User.id != u.id).limit(500).all()],
                           ver=int(now()))


@app.route('/api/stream')
def stream():
    u = me()
    if not u: return Response('no', status=401)
    uid = u.id
    q = queue.Queue(maxsize=256)
    with _lock: _subs.setdefault(uid, []).append(q)

    def gen():
        try:
            yield 'retry: 2000\nevent: ready\ndata: {}\n\n'
            last = now()
            while True:
                try:
                    ev, d = q.get(timeout=1)
                    yield 'event: %s\ndata: %s\n\n' % (ev, json.dumps(d, ensure_ascii=False))
                except queue.Empty:
                    if now() - last > 15:
                        touch(uid); yield ': p\n\n'; last = now()
        except GeneratorExit:
            pass
        finally:
            with _lock:
                l = _subs.get(uid, [])
                if q in l: l.remove(q)
                if not l: _subs.pop(uid, None)

    r = Response(gen(), mimetype='text/event-stream')
    r.headers['Cache-Control'] = 'no-cache'
    r.headers['X-Accel-Buffering'] = 'no'
    return r


@app.route('/api/ping', methods=['POST'])
@need
def ping(u):
    touch(u.id)
    if now() - (u.last_seen or 0) > 60:
        u.last_seen = now(); db.session.commit()
    return jsonify(ok=True, now=now())


@app.route('/api/conversations')
@need
def api_convs(u):
    return jsonify(ok=True, convs=conversations(u.id))


@app.route('/api/history/<key>')
@need
def api_history(u, key):
    ctx = resolve(u.id, key)
    if not ctx: return jsonify(ok=False), 403
    before = ival(request.args.get('before'))
    q = Message.query.filter_by(key=key)
    if before: q = q.filter(Message.id < before)
    raw = q.order_by(Message.id.desc()).limit(PAGE + 1).all()
    more = len(raw) > PAGE
    rows = [m for m in raw[:PAGE] if visible(u.id, m)]
    rows.reverse()
    return jsonify(envelope(u.id, ctx, rows, 0, {'more': more}))


@app.route('/api/sync/<key>')
@need
def api_sync(u, key):
    ctx = resolve(u.id, key)
    if not ctx: return jsonify(ok=False), 403
    since = fval(request.args.get('since'), 0)
    rows = [m for m in Message.query.filter(Message.key == key, Message.updated_at > max(0, since - 1))
            .order_by(Message.id.asc()).limit(400).all() if visible(u.id, m)]
    return jsonify(envelope(u.id, ctx, rows, since))


@app.route('/api/send', methods=['POST'])
@need
def api_send(u):
    d = jget(); ctx = resolve(u.id, str(d.get('key', '')))
    if not ctx: return jsonify(ok=False, error='chat'), 403
    if not ratelimit(u.id, 'send', 12, 5): return jsonify(ok=False, error='Слишком быстро'), 429
    body = str(d.get('body', '')).strip()[:MAXLEN]
    if not body: return jsonify(ok=False), 400
    if ctx['kind'] == 'personal' and ctx['other'] != u.id and blocked(u.id, ctx['other']):
        return jsonify(ok=False, error='Диалог заблокирован'), 403
    reply = ival(d.get('reply'))
    if reply:
        rm = db.session.get(Message, reply)
        if not rm or rm.key != ctx['key']: reply = 0
    m = Message(key=ctx['key'], sender_id=u.id, kind='text', body=body, reply_to=reply)
    db.session.add(m); db.session.commit()
    _typing.pop((ctx['key'], u.id), None)
    p = pack(m, {})
    if reply:
        rm = db.session.get(Message, reply)
        s = db.session.get(User, rm.sender_id) if rm else None
        p['quote'] = {'id': rm.id, 'name': s.name if s else '—', 'kind': rm.kind,
                      'body': (rm.body if rm.kind == 'text' else '📎 ' + (rm.fname or rm.kind))[:90]} if rm else None
    notify(ctx, 'msg', p); publish(ctx['peers'], 'conv', {})
    return jsonify(ok=True, msg=p, now=now())


@app.route('/api/upload', methods=['POST'])
@need
def api_upload(u):
    ctx = resolve(u.id, request.form.get('key', ''))
    if not ctx: return jsonify(ok=False), 403
    if not ratelimit(u.id, 'up', 10, 60): return jsonify(ok=False, error='Лимит загрузок'), 429
    f = request.files.get('file')
    if not f or not f.filename: return jsonify(ok=False), 400
    safe = secure_filename(f.filename) or 'file'
    ext = safe.rsplit('.', 1)[-1].lower() if '.' in safe else ''
    if ext not in ALLOWED: return jsonify(ok=False, error='Тип файла запрещён'), 400
    f.stream.seek(0, os.SEEK_END); size = f.stream.tell(); f.stream.seek(0)
    if size > 32 * 1024 * 1024: return jsonify(ok=False, error='Файл > 32 МБ'), 400
    name = '%d_%s.%s' % (int(now() * 1000), secrets.token_hex(6), ext)
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], name))
    kind = 'image' if ext in IMG_EXT else ('audio' if ext in AUD_EXT else 'file')
    m = Message(key=ctx['key'], sender_id=u.id, kind=kind, body=url_for('uploaded', fn=name),
                fname=f.filename[:160], fsize=size, reply_to=ival(request.form.get('reply')))
    db.session.add(m); db.session.commit()
    p = pack(m, {}); notify(ctx, 'msg', p); publish(ctx['peers'], 'conv', {})
    return jsonify(ok=True, msg=p, now=now())


@app.route('/uploads/<path:fn>')
def uploaded(fn):
    base = os.path.abspath(app.config['UPLOAD_FOLDER'])
    tgt = os.path.abspath(os.path.join(base, fn))
    if not tgt.startswith(base + os.sep): abort(403)
    r = send_from_directory(base, fn)
    if fn.rsplit('.', 1)[-1].lower() not in (IMG_EXT | AUD_EXT):
        r.headers['Content-Disposition'] = 'attachment; filename="%s"' % os.path.basename(fn)
    r.headers['X-Content-Type-Options'] = 'nosniff'
    r.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return r


@app.route('/api/react', methods=['POST'])
@need
def api_react(u):
    d = jget(); m = db.session.get(Message, ival(d.get('mid')))
    if not m or not msg_access(u.id, m): return jsonify(ok=False), 403
    e = str(d.get('emoji', ''))
    if e not in EMOJI: return jsonify(ok=False, error='emoji'), 400
    r = Reaction.query.filter_by(mid=m.id, uid=u.id).first()
    if r and r.emoji == e: db.session.delete(r)
    elif r: r.emoji = e
    else: db.session.add(Reaction(mid=m.id, uid=u.id, emoji=e))
    bump(m); db.session.commit()
    notify(resolve(u.id, m.key), 'sync', {'key': m.key})
    return jsonify(ok=True)


@app.route('/api/edit', methods=['POST'])
@need
def api_edit(u):
    d = jget(); m = db.session.get(Message, ival(d.get('mid')))
    if not m or m.sender_id != u.id or m.kind != 'text': return jsonify(ok=False), 403
    b = str(d.get('body', '')).strip()[:MAXLEN]
    if not b: return jsonify(ok=False), 400
    m.body = b; m.edited_at = now(); bump(m); db.session.commit()
    notify(resolve(u.id, m.key), 'sync', {'key': m.key})
    return jsonify(ok=True)


@app.route('/api/delete', methods=['POST'])
@need
def api_delete(u):
    d = jget(); ids = d.get('ids') or ([ival(d.get('mid'))] if d.get('mid') else [])
    scope = d.get('scope', 'me'); key = None; done = []
    for mid in ids[:100]:
        m = db.session.get(Message, ival(mid))
        if not m or not msg_access(u.id, m): continue
        key = m.key; done.append(m.id)
        if scope == 'all':
            if m.sender_id != u.id: continue
            Reaction.query.filter_by(mid=m.id).delete()
            db.session.add(Tomb(key=m.key, mid=m.id)); db.session.delete(m)
        else:
            h = hidden_list(m)
            if str(u.id) not in h: h.append(str(u.id))
            m.hidden = ','.join(h); bump(m)
    db.session.commit()
    if key:
        ctx = resolve(u.id, key)
        if scope == 'all' and ctx: notify(ctx, 'sync', {'key': key})
        publish([u.id], 'hide', {'key': key, 'ids': done})
        publish([u.id], 'conv', {})
    return jsonify(ok=True, ids=done)


@app.route('/api/pin', methods=['POST'])
@need
def api_pin(u):
    m = db.session.get(Message, ival(jget().get('mid')))
    if not m or not msg_access(u.id, m): return jsonify(ok=False), 403
    m.pinned = 0 if m.pinned else 1; bump(m); db.session.commit()
    notify(resolve(u.id, m.key), 'sync', {'key': m.key})
    return jsonify(ok=True, pinned=m.pinned)


@app.route('/api/forward', methods=['POST'])
@need
def api_forward(u):
    d = jget(); m = db.session.get(Message, ival(d.get('mid')))
    if not m or not msg_access(u.id, m): return jsonify(ok=False), 403
    src = db.session.get(User, m.sender_id)
    label = (src.name if src else 'Аккаунт удалён')[:64]
    n = 0
    for k in (d.get('keys') or [])[:20]:
        ctx = resolve(u.id, str(k))
        if not ctx: continue
        if ctx['kind'] == 'personal' and ctx['other'] != u.id and blocked(u.id, ctx['other']): continue
        nm = Message(key=ctx['key'], sender_id=u.id, kind=m.kind, body=m.body,
                     fname=m.fname, fsize=m.fsize, fwd=label)
        db.session.add(nm); db.session.commit()
        notify(ctx, 'msg', pack(nm, {})); n += 1
    publish([u.id], 'conv', {})
    return jsonify(ok=True, n=n)


@app.route('/api/read', methods=['POST'])
@need
def api_read(u):
    d = jget(); ctx = resolve(u.id, str(d.get('key', '')))
    if not ctx: return jsonify(ok=False), 403
    lid = ival(d.get('last'))
    r = ReadState.query.filter_by(key=ctx['key'], uid=u.id).first()
    if not r:
        r = ReadState(key=ctx['key'], uid=u.id, last_id=0); db.session.add(r)
    if lid > r.last_id:
        r.last_id = lid; r.at = now(); db.session.commit()
        notify(ctx, 'read', {'key': ctx['key'], 'uid': u.id, 'last': lid})
        publish([u.id], 'conv', {})
    return jsonify(ok=True)


@app.route('/api/typing', methods=['POST'])
@need
def api_typing(u):
    ctx = resolve(u.id, str(jget().get('key', '')))
    if not ctx: return jsonify(ok=False), 403
    k = (ctx['key'], u.id)
    fresh = now() - _typing.get(k, 0) > 2
    _typing[k] = now()
    if fresh: publish([x for x in ctx['peers'] if x != u.id], 'typing', {'key': ctx['key']})
    return jsonify(ok=True)


@app.route('/api/search/users')
@need
def api_search_users(u):
    q = (request.args.get('q') or '').strip().lower()
    if not q: return jsonify(ok=True, users=[])
    like = '%' + q + '%'
    rows = User.query.filter(User.id != u.id, or_(User.username.ilike(like), User.name.ilike(like))).limit(25).all()
    return jsonify(ok=True, users=[user_brief(x) for x in rows])


@app.route('/api/search/messages')
@need
def api_search_msgs(u):
    q = (request.args.get('q') or '').strip(); key = request.args.get('key', '')
    if len(q) < 2: return jsonify(ok=True, hits=[])
    keys = [key] if key and resolve(u.id, key) else my_keys(u.id)
    rows = Message.query.filter(Message.key.in_(keys), Message.kind == 'text',
                                Message.body.ilike('%' + q + '%')).order_by(Message.id.desc()).limit(60).all()
    return jsonify(ok=True, hits=[{'id': m.id, 'key': m.key, 'body': m.body[:160],
                                   'sender': m.sender_id, 'at': m.created_at} for m in rows if visible(u.id, m)])


@app.route('/api/profile/<int:uid>')
@need
def api_profile(u, uid):
    o = db.session.get(User, uid)
    if not o: return jsonify(ok=False), 404
    k = pkey(u.id, uid)
    rows = Message.query.filter_by(key=k).order_by(Message.id.desc()).limit(400).all()
    media, files, voice, links = [], [], [], []
    for m in rows:
        if not visible(u.id, m): continue
        if m.kind == 'image': media.append({'id': m.id, 'url': m.body})
        elif m.kind == 'audio': voice.append({'id': m.id, 'url': m.body, 'at': m.created_at})
        elif m.kind == 'file': files.append({'id': m.id, 'url': m.body, 'name': m.fname, 'size': m.fsize})
        elif m.kind == 'text':
            for l in re.findall(r'https?://[^\s<>"]+', m.body)[:3]: links.append({'id': m.id, 'url': l})
    d = user_brief(o)
    d.update(ok=True, media=media[:60], files=files[:60], voice=voice[:60], links=links[:60],
             blocked=blocked(u.id, uid), key=k)
    if not prefs_of(o).get('seen', True): d['last_seen'] = 0
    return jsonify(d)


@app.route('/api/settings', methods=['POST'])
@need
def api_settings(u):
    d = jget(); p = prefs_of(u)
    if 'name' in d: u.name = str(d['name']).strip()[:64] or u.name
    if 'bio' in d: u.bio = str(d['bio']).strip()[:200]
    if 'avatar' in d:
        a = str(d['avatar'])
        if a == '': u.avatar = ''
        elif a.startswith('data:image/') and len(a) < 900000: u.avatar = a
    for k in DEFAULT_PREFS:
        if k in d: p[k] = d[k]
    p['font'] = max(12, min(22, ival(p['font'], 15)))
    p['radius'] = max(0, min(26, ival(p['radius'], 18)))
    if p['theme'] not in ('dark', 'light', 'auto'): p['theme'] = 'dark'
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', str(p['accent'])): p['accent'] = '#4f8cff'
    u.prefs = json.dumps(p, ensure_ascii=False); db.session.commit()
    return jsonify(ok=True, prefs=p)


@app.route('/api/password', methods=['POST'])
@need
def api_password(u):
    d = jget()
    if not check_password_hash(u.pw, str(d.get('old', ''))): return jsonify(ok=False, error='Старый пароль неверный'), 400
    if len(str(d.get('new', ''))) < 6: return jsonify(ok=False, error='Минимум 6 символов'), 400
    u.pw = generate_password_hash(str(d['new'])); db.session.commit()
    return jsonify(ok=True)


@app.route('/api/block', methods=['POST'])
@need
def api_block(u):
    bid = ival(jget().get('uid'))
    if not bid or bid == u.id or not db.session.get(User, bid): return jsonify(ok=False), 400
    b = Block.query.filter_by(uid=u.id, bid=bid).first()
    if b: db.session.delete(b)
    else: db.session.add(Block(uid=u.id, bid=bid))
    db.session.commit()
    return jsonify(ok=True, blocked=b is None)


@app.route('/api/group', methods=['POST'])
@need
def api_group(u):
    d = jget(); act = d.get('act', 'create')
    if act == 'create':
        title = str(d.get('title', '')).strip()[:80]
        ids = [ival(x) for x in (d.get('ids') or [])][:200]
        if not title: return jsonify(ok=False, error='Название'), 400
        g = Group(title=title, owner_id=u.id, code=secrets.token_urlsafe(6))
        db.session.add(g); db.session.commit()
        db.session.add(Member(gid=g.id, uid=u.id, role='owner'))
        for i in set(ids):
            if i and i != u.id and db.session.get(User, i): db.session.add(Member(gid=g.id, uid=i))
        db.session.commit(); _memcache.pop(g.id, None)
        publish(members(g.id), 'conv', {})
        return jsonify(ok=True, key='g:%d' % g.id)
    gid = ival(d.get('gid')); g = db.session.get(Group, gid)
    if not g or u.id not in members(gid): return jsonify(ok=False), 403
    admin = g.owner_id == u.id or Member.query.filter_by(gid=gid, uid=u.id, role='admin').first() is not None
    if act == 'rename' and admin:
        g.title = str(d.get('title', '')).strip()[:80] or g.title
    elif act == 'avatar' and admin:
        a = str(d.get('avatar', ''))
        if a == '' or (a.startswith('data:image/') and len(a) < 900000): g.avatar = a
    elif act == 'add' and admin:
        for x in (d.get('ids') or [])[:100]:
            i = ival(x)
            if i and db.session.get(User, i) and i not in members(gid): db.session.add(Member(gid=gid, uid=i))
        _memcache.pop(gid, None)
    elif act == 'kick' and admin:
        i = ival(d.get('uid'))
        if i != g.owner_id:
            Member.query.filter_by(gid=gid, uid=i).delete(); _memcache.pop(gid, None)
    elif act == 'role' and g.owner_id == u.id:
        mm = Member.query.filter_by(gid=gid, uid=ival(d.get('uid'))).first()
        if mm and mm.role != 'owner': mm.role = 'admin' if mm.role != 'admin' else 'member'
    elif act == 'leave':
        Member.query.filter_by(gid=gid, uid=u.id).delete(); _memcache.pop(gid, None)
        if g.owner_id == u.id:
            rest = [x for x in members(gid) if x != u.id]
            if rest:
                g.owner_id = rest[0]
                nm = Member.query.filter_by(gid=gid, uid=rest[0]).first()
                if nm: nm.role = 'owner'
            else:
                Member.query.filter_by(gid=gid).delete(); db.session.delete(g)
    else:
        return jsonify(ok=False, error='Нет прав'), 403
    db.session.commit(); _memcache.pop(gid, None)
    publish(members(gid) + [u.id], 'conv', {})
    publish(members(gid), 'sync', {'key': 'g:%d' % gid})
    return jsonify(ok=True)


@app.route('/api/export')
@need
def api_export(u):
    rows = Message.query.filter(Message.key.in_(my_keys(u.id)), Message.sender_id == u.id).order_by(Message.id).all()
    data = {'user': user_brief(u), 'exported': datetime.utcnow().isoformat(),
            'messages': [{'key': m.key, 'kind': m.kind, 'body': m.body, 'at': m.created_at} for m in rows]}
    return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename="ramplez-export.json"'})


@app.route('/api/delete_account', methods=['POST'])
@need
def api_delacc(u):
    if str(jget().get('confirm')) != u.username: return jsonify(ok=False, error='Введите свой логин'), 400
    for m in Message.query.filter_by(sender_id=u.id).all():
        db.session.add(Tomb(key=m.key, mid=m.id))
        Reaction.query.filter_by(mid=m.id).delete(); db.session.delete(m)
    Member.query.filter_by(uid=u.id).delete(); ReadState.query.filter_by(uid=u.id).delete()
    Reaction.query.filter_by(uid=u.id).delete(); Block.query.filter_by(uid=u.id).delete()
    _memcache.clear(); db.session.delete(u); db.session.commit(); session.clear()
    return jsonify(ok=True)


@app.route('/healthz')
def healthz():
    try:
        db.session.execute(text('SELECT 1')); return 'ok'
    except Exception:
        return 'db error', 500


@app.route('/manifest.json')
def manifest():
    return jsonify(name='Ramplez — мессенджер', short_name='Ramplez', id='/?src=pwa',
                   description='Быстрый мессенджер реального времени', start_url='/chat', scope='/',
                   display='standalone', display_override=['standalone', 'minimal-ui'],
                   orientation='portrait-primary', background_color='#0b1020', theme_color='#0b1020',
                   lang='ru', categories=['social', 'communication'],
                   icons=[{'src': '/icon/192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                          {'src': '/icon/512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                          {'src': '/icon/512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}])


_ICONS = {}


def make_icon(size):
    if size in _ICONS: return _ICONS[size]
    r = size * 0.22
    bx0, by0, bx1, by1 = size * .20, size * .26, size * .80, size * .66
    br = size * .14
    corners = ((bx0 + br, by0 + br), (bx1 - br, by0 + br), (bx0 + br, by1 - br), (bx1 - br, by1 - br))

    def inside(x, y):
        if bx0 + br <= x <= bx1 - br and by0 <= y <= by1: return True
        if bx0 <= x <= bx1 and by0 + br <= y <= by1 - br: return True
        for px, py in corners:
            if (x - px) ** 2 + (y - py) ** 2 <= br * br: return True
        t = (y - by1) / (size * .16)
        if 0 <= t <= 1 and bx0 + size * .06 <= x <= bx0 + size * .06 + (1 - t) * size * .20: return True
        return False

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            dx = max(r - x, x - (size - 1 - r), 0); dy = max(r - y, y - (size - 1 - r), 0)
            if dx * dx + dy * dy > r * r:
                raw += b'\x00\x00\x00\x00'; continue
            t = (x + y) / (2.0 * size)
            cr, cg, cb = int(79 + 45 * t), int(140 - 48 * t), int(255 - 5 * t)
            if inside(x, y): cr, cg, cb = 255, 255, 255
            raw += bytes((cr, cg, cb, 255))

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))
    _ICONS[size] = png
    return png


@app.route('/icon/<int:size>.png')
def icon(size):
    if size not in (144, 180, 192, 512): abort(404)
    r = Response(make_icon(size), mimetype='image/png')
    r.headers['Cache-Control'] = 'public, max-age=604800'
    return r


@app.route('/sw.js')
def sw():
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')


@app.errorhandler(404)
def e404(e):
    return (jsonify(ok=False, error='not found'), 404) if request.path.startswith('/api/') else ('Не найдено', 404)


@app.errorhandler(413)
def e413(e):
    return jsonify(ok=False, error='Файл слишком большой'), 413


@app.errorhandler(500)
def e500(e):
    db.session.rollback()
    return jsonify(ok=False, error='server'), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
