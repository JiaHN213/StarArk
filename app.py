import os, secrets, shutil, uuid, io, tempfile, subprocess, csv, re, json
from datetime import datetime, timezone
from functools import wraps
import requests as http_requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, jsonify, send_file, session, g
from flask_login import LoginManager, login_user, logout_user, current_user, UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import markdown as md_lib, bleach

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'user_data')
DB_PATH = os.path.join(DATA_DIR, 'data.db')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///' + DB_PATH)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 300}
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def safe_filename(name):
    """Sanitize filename: remove path separators but keep Unicode."""
    if not name: return 'unnamed'
    name = name.replace('\\', '_').replace('/', '_').replace('\x00', '')
    name = name.strip('. ')
    if not name: return 'unnamed'
    if len(name) > 200: name = name[:200]
    return name

def render_markdown(text):
    tags = ['h1','h2','h3','h4','h5','h6','p','br','hr','a','img','ul','ol','li','strong','em','b','i','u','del','s','blockquote','pre','code','table','thead','tbody','tr','th','td','span','div']
    attrs = {'a':['href','title','target'],'img':['src','alt','title','width','height'],'code':['class'],'pre':['class'],'span':['class'],'div':['class'],'th':['align'],'td':['align']}
    return bleach.clean(md_lib.markdown(text or '', extensions=['extra','tables','fenced_code']), tags=tags, attributes=attrs, strip=True)
def format_size(sz):
    if sz<1024: return f"{sz} B"
    elif sz<1048576: return f"{sz/1024:.1f} KB"
    elif sz<1073741824: return f"{sz/1048576:.1f} MB"
    return f"{sz/1073741824:.2f} GB"
def format_time(dt): return dt.strftime('%Y-%m-%d %H:%M') if dt else ''

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True); username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False); is_admin = db.Column(db.Boolean, default=False)
    is_super_admin = db.Column(db.Boolean, default=False); is_approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    notes = db.relationship('Note', backref='author', lazy='dynamic', cascade='all, delete-orphan')
    files = db.relationship('File', backref='uploader', lazy='dynamic', cascade='all, delete-orphan')
    blog_posts = db.relationship('BlogPost', backref='author', lazy='dynamic', cascade='all, delete-orphan')
    folders = db.relationship('Folder', backref='owner', lazy='dynamic', cascade='all, delete-orphan')

class Folder(db.Model):
    __tablename__ = 'folders'
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False); parent_id = db.Column(db.Integer, db.ForeignKey('folders.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    files = db.relationship('File', backref='folder', lazy='dynamic', foreign_keys='File.folder_id')

class Note(db.Model):
    __tablename__ = 'notes'
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(200), nullable=False); content = db.Column(db.Text, default=''); is_shared = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class File(db.Model):
    __tablename__ = 'files'
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False); storage_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, default=0); folder_id = db.Column(db.Integer, db.ForeignKey('folders.id'), nullable=True)
    is_shared = db.Column(db.Boolean, default=False); uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class BlogPost(db.Model):
    __tablename__ = 'blog_posts'
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(200), nullable=False); content = db.Column(db.Text, default=''); is_public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class CodeSnippet(db.Model):
    __tablename__ = 'code_snippets'
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(200), nullable=False); language = db.Column(db.String(50), default='plaintext')
    content = db.Column(db.Text, default=''); is_shared = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    author = db.relationship('User', backref=db.backref('code_snippets', lazy='dynamic'))

class Resume(db.Model):
    __tablename__ = 'resumes'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(200), nullable=False, default='我的简历')
    target_company = db.Column(db.String(200), default='')
    target_role = db.Column(db.String(200), default='')
    data = db.Column(db.Text, default='{}')  # JSON: basics/summary/education/experience/projects/skills/awards
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    author = db.relationship('User', backref=db.backref('resumes', lazy='dynamic', cascade='all, delete-orphan'))

class SiteSetting(db.Model):
    __tablename__ = 'site_settings'; key = db.Column(db.String(100), primary_key=True); value = db.Column(db.Text, default='')

class UserSession(db.Model):
    __tablename__ = 'user_sessions'; id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False); token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

def get_deepseek_key():
    import os as _os
    kf = _os.path.join(DATA_DIR, 'deepseek_key.txt')
    if _os.path.exists(kf):
        with open(kf,'r',encoding='utf-8') as f:
            k = f.read().strip()
            if k: return k
    return os.environ.get('DEEPSEEK_API_KEY','').strip()

def get_user_upload_dir(uid):
    u = User.query.get(uid)
    d = os.path.join(UPLOAD_DIR, (u.username if u else str(uid)).replace('/','_').replace('\\','_'))
    os.makedirs(d, exist_ok=True)
    return d

def get_file_path(fr): return os.path.join(get_user_upload_dir(fr.user_id), fr.storage_path)

def get_breadcrumb(folder):
    trail = []
    while folder: trail.append(folder); folder = Folder.query.get(folder.parent_id) if folder.parent_id else None
    trail.reverse(); return trail

def get_max_upload_bytes():
    s = SiteSetting.query.get('max_upload_mb')
    try: return (int(s.value) if s and s.value else 200) * 1048576
    except: return 200 * 1048576

def get_site_name():
    s = SiteSetting.query.get('site_name')
    return s.value if s and s.value else 'StarArk'

def get_site_guide():
    s = SiteSetting.query.get('site_guide')
    return (s.value if s else '').replace('{site_name}', get_site_name())

def get_user_folders(uid, pid=None):
    return Folder.query.filter_by(user_id=uid, parent_id=pid).order_by(Folder.name.asc()).all()

CODE_EXTENSIONS = {'.py','.js','.ts','.html','.css','.java','.go','.rs','.rb','.php','.swift','.kt','.sql','.sh','.bash','.ps1','.cmd','.bat','.c','.cpp','.h','.hpp','.lua','.r','.m','.vue','.svelte','.scss','.less','.xml','.json','.yaml','.yml','.toml','.ini','.cfg','.tf','.hcl','.proto','.graphql','.cmake','.md','.txt','.csv','.log','.env','.gitignore','.dockerfile'}
PREVIEW_TYPES = {'.png':'image','.jpg':'image','.jpeg':'image','.gif':'image','.svg':'image','.webp':'image','.bmp':'image','.ico':'image','.pdf':'pdf','.mp3':'audio','.wav':'audio','.flac':'audio','.ogg':'audio','.m4a':'audio','.mp4':'video','.webm':'video','.avi':'video','.mkv':'video','.mov':'video','.md':'markdown','.csv':'csv','.tsv':'csv','.json':'data','.xml':'data','.yaml':'data','.yml':'data'}

def get_preview_type(fn):
    ext = os.path.splitext(fn.lower())[1]
    if ext in PREVIEW_TYPES: return PREVIEW_TYPES[ext]
    code_exts = {'.py','.js','.jsx','.ts','.tsx','.java','.kt','.c','.cpp','.h','.hpp','.go','.rs','.rb','.php','.swift','.scala','.groovy','.lua','.nim','.zig','.r','.sql','.tf','.hcl','.proto','.graphql','.vue','.svelte','.css','.scss','.less','.html','.htm','.sh','.bash','.zsh','.ps1','.bat','.cmd','.tex','.cmake','.prisma'}
    if ext in code_exts: return 'code'
    return 'text'

def get_text_file_content(fr):
    try:
        with open(get_file_path(fr),'r',encoding='utf-8',errors='replace') as f: return f.read()
    except: return None

def get_pdf_text(fr):
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(get_file_path(fr)); text = []
        for page in reader.pages[:10]:
            t = page.extract_text()
            if t: text.append(t)
        return '\n'.join(text)[:8000]
    except ImportError: return '[PDF text extraction requires PyPDF2]'
    except Exception as e: return f'[PDF: {e}]'

def is_code_file(fn):
    return os.path.splitext(fn.lower())[1] in CODE_EXTENSIONS or os.path.basename(fn.lower()) in CODE_EXTENSIONS

FILE_ICON_MAP = {'py':'bi-filetype-py','js':'bi-filetype-js','html':'bi-filetype-html','css':'bi-filetype-css','json':'bi-filetype-json','md':'bi-filetype-md','pdf':'bi-filetype-pdf','zip':'bi-file-earmark-zip','png':'bi-file-image','jpg':'bi-file-image','jpeg':'bi-file-image','gif':'bi-file-image','svg':'bi-file-image','mp3':'bi-file-music','mp4':'bi-file-play','doc':'bi-filetype-doc','docx':'bi-filetype-docx','xls':'bi-filetype-xls','xlsx':'bi-filetype-xlsx','ppt':'bi-filetype-ppt','pptx':'bi-filetype-pptx','sql':'bi-filetype-sql','java':'bi-filetype-java','php':'bi-filetype-php','rb':'bi-filetype-rb','xml':'bi-filetype-xml'}
def get_file_icon(fn):
    return FILE_ICON_MAP.get(fn.rsplit('.',1)[-1].lower() if '.' in fn else '', 'bi-file-earmark')

def login_required(f):
    @wraps(f)
    def dec(*a,**k):
        if current_user.is_authenticated or (hasattr(g,'token_user') and g.token_user): return f(*a,**k)
        return app.login_manager.unauthorized()
    return dec

def admin_required(f):
    @wraps(f)
    @login_required
    def dec(*a,**k):
        if not (current_user.is_admin or current_user.is_super_admin): abort(403)
        return f(*a,**k)
    return dec

def super_admin_required(f):
    @wraps(f)
    @login_required
    def dec(*a,**k):
        if not current_user.is_super_admin: abort(403)
        return f(*a,**k)
    return dec

app.jinja_env.filters['format_size'] = format_size
app.jinja_env.filters['format_time'] = format_time
app.jinja_env.filters['markdown'] = render_markdown
app.jinja_env.filters['file_icon'] = get_file_icon
app.jinja_env.filters['is_code'] = is_code_file

def resume_photo_url(resume, photo_rel):
    """把 'resume_photos/xxx.jpg' 转换成可访问 URL"""
    if not photo_rel: return ''
    if photo_rel.startswith('http://') or photo_rel.startswith('https://'): return photo_rel
    if photo_rel.startswith('resume_photos/'):
        return url_for('resume_photo', resume_id=resume.id, filename=os.path.basename(photo_rel))
    return ''
app.jinja_env.globals['resume_photo_url'] = resume_photo_url

CODE_LANGUAGES = [('python','Python'),('javascript','JavaScript'),('typescript','TypeScript'),('html','HTML'),('css','CSS'),('java','Java'),('go','Go'),('rust','Rust'),('cpp','C++'),('c','C'),('php','PHP'),('ruby','Ruby'),('swift','Swift'),('kotlin','Kotlin'),('sql','SQL'),('bash','Bash'),('powershell','PowerShell'),('yaml','YAML'),('json','JSON'),('xml','XML'),('markdown','Markdown'),('lua','Lua'),('r','R'),('plaintext','Plain Text')]
AI_CONTEXTS = {'paper':'Paper reading assistant.','code_engineer':'Senior software engineer.','code_writer':'Coding assistant.','writer':'Writing editor.','general':'General assistant.'}

@app.before_request
def resolve_token_user():
    token = request.args.get('token')
    if token:
        us = UserSession.query.filter_by(token=token).first()
        if us:
            u = User.query.get(us.user_id)
            if u: g.token_user = u; login_user(u); return
    g.token_user = None

@app.context_processor
def inject_globals():
    s = SiteSetting.query.get('max_upload_mb')
    try: mm = int(s.value) if s and s.value else 200
    except: mm = 200
    token = request.args.get('token','')
    path = request.path; ai_ctx = 'general'; ai_content = ''
    try:
        parts = [p for p in path.split('/') if p]
        if parts and parts[0] == 'files' and 'preview' in path:
            ai_ctx = 'paper'
            if len(parts) >= 2 and parts[1].isdigit():
                fr = File.query.get(int(parts[1]))
                if fr: ai_content = get_pdf_text(fr) if get_preview_type(fr.original_filename)=='pdf' else (get_text_file_content(fr) or f'File: {fr.original_filename}')[:4000]
        elif parts and parts[0] == 'code':
            ai_ctx = 'code_engineer'
            if len(parts) >= 2 and parts[1].isdigit():
                sn = CodeSnippet.query.get(int(parts[1]))
                if sn: ai_content = f'Language: {sn.language}\n```{sn.language}\n{sn.content[:3000]}\n```'
        elif parts and parts[0] == 'notes':
            ai_ctx = 'writer'
            if len(parts) >= 2 and parts[1].isdigit():
                note = Note.query.get(int(parts[1]))
                if note: ai_content = f'Title: {note.title}\n{note.content[:3000]}'
        elif parts and parts[0] == 'blog':
            ai_ctx = 'writer'
            if len(parts) >= 2 and parts[1].isdigit():
                post = BlogPost.query.get(int(parts[1]))
                if post: ai_content = f'Title: {post.title}\n{post.content[:3000]}'
        elif parts and parts[0] == 'shared':
            guide = get_site_guide()
            if guide: ai_content = guide[:2000]
    except: pass
    return {'site_name':get_site_name(),'max_upload_mb':mm,'site_guide':get_site_guide(),'session_token':token,'ai_context':ai_ctx,'ai_page_content':ai_content,'local_ip':request.host.split(':')[0] if request.host else 'localhost'}

@app.route('/register', methods=['GET','POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('index'))
    if request.method == 'POST':
        un = request.form.get('username','').strip(); pw = request.form.get('password','').strip(); pc = request.form.get('password_confirm','').strip()
        if not un or not pw: flash('Username and password required.','danger'); return render_template('register.html')
        if len(un)<2 or len(un)>80: flash('Username 2-80 chars.','danger'); return render_template('register.html')
        if len(pw)<4: flash('Password min 4 chars.','danger'); return render_template('register.html')
        if pw!=pc: flash('Passwords do not match.','danger'); return render_template('register.html')
        if User.query.filter_by(username=un).first(): flash('Username taken.','danger'); return render_template('register.html')
        db.session.add(User(username=un,password_hash=generate_password_hash(pw),is_approved=False))
        db.session.commit(); flash('Registration submitted, pending approval.','info'); return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        un = request.form.get('username','').strip(); pw = request.form.get('password','').strip()
        if not un or not pw: flash('Enter username and password.','danger'); return render_template('login.html')
        u = User.query.filter_by(username=un).first()
        if u and check_password_hash(u.password_hash,pw):
            if not u.is_approved and not u.is_admin and not u.is_super_admin:
                flash('Account pending admin approval.','warning'); return render_template('login.html')
            login_user(u); token = secrets.token_hex(32)
            db.session.add(UserSession(user_id=u.id,token=token)); db.session.commit()
            flash(f'Welcome, {u.username}!','success')
            raw_next = request.args.get('next','')
            from urllib.parse import urlparse
            nxt = raw_next if raw_next and not urlparse(raw_next).netloc else url_for('index')
            if '?' in nxt: nxt += '&token=' + token
            else: nxt += '?token=' + token
            return redirect(nxt)
        flash('Invalid credentials.','danger')
    return render_template('login.html')

@app.route('/change-password', methods=['GET','POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old = request.form.get('old_password',''); new = request.form.get('new_password',''); confirm = request.form.get('confirm_password','')
        if not old or not new: flash('All fields required.','danger'); return render_template('change_password.html')
        if len(new) < 4: flash('New password min 4 chars.','danger'); return render_template('change_password.html')
        if new != confirm: flash('Passwords do not match.','danger'); return render_template('change_password.html')
        if not check_password_hash(current_user.password_hash, old): flash('Wrong current password.','danger'); return render_template('change_password.html')
        current_user.password_hash = generate_password_hash(new); db.session.commit()
        flash('Password changed.','success'); return redirect(url_for('index'))
    return render_template('change_password.html')

@app.route('/logout')
@login_required
def logout():
    token = request.args.get('token')
    if token: UserSession.query.filter_by(token=token).delete(); db.session.commit()
    logout_user(); flash('Logged out.','info'); return redirect(url_for('login'))

@app.route('/')
def index():
    if not current_user.is_authenticated: return redirect(url_for('login'))
    stats = {'note':Note.query.filter_by(user_id=current_user.id).count(),'file':File.query.filter_by(user_id=current_user.id).count(),'code':CodeSnippet.query.filter_by(user_id=current_user.id).count(),'blog':BlogPost.query.filter_by(user_id=current_user.id).count(),'shared':Note.query.filter_by(is_shared=True).count()+CodeSnippet.query.filter_by(is_shared=True).count()+File.query.filter_by(is_shared=True).count()+BlogPost.query.filter_by(is_public=True).count()}
    rn = Note.query.filter_by(user_id=current_user.id).order_by(Note.updated_at.desc()).limit(5).all()
    rf = File.query.filter_by(user_id=current_user.id).order_by(File.uploaded_at.desc()).limit(5).all()
    return render_template('index.html', stats=stats, recent_notes_list=rn, recent_files_list=rf)

@app.route('/notes')
@login_required
def notes():
    nq = Note.query.order_by(Note.updated_at.desc()).all() if current_user.is_super_admin else Note.query.filter_by(user_id=current_user.id).order_by(Note.updated_at.desc()).all()
    return render_template('notes.html', notes=nq)

@app.route('/notes/<int:note_id>')
@login_required
def note_view(note_id):
    note = Note.query.get_or_404(note_id)
    if note.user_id!=current_user.id and not note.is_shared and not current_user.is_super_admin: abort(403)
    return render_template('note_view.html', note=note)

@app.route('/notes/new', methods=['GET','POST'])
@login_required
def note_create():
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('note_form.html', note=None)
        db.session.add(Note(user_id=current_user.id,title=t,content=request.form.get('content',''),is_shared=request.form.get('is_shared')=='on'))
        db.session.commit(); flash('Note created.','success'); return redirect(url_for('notes'))
    return render_template('note_form.html', note=None)

@app.route('/notes/<int:note_id>/edit', methods=['GET','POST'])
@login_required
def note_edit(note_id):
    note = Note.query.get_or_404(note_id)
    if note.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('note_form.html', note=note)
        note.title=t; note.content=request.form.get('content',''); note.is_shared=request.form.get('is_shared')=='on'
        db.session.commit(); flash('Note updated.','success'); return redirect(url_for('notes'))
    return render_template('note_form.html', note=note)

@app.route('/notes/<int:note_id>/delete', methods=['POST'])
@login_required
def note_delete(note_id):
    note = Note.query.get_or_404(note_id)
    if note.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    db.session.delete(note); db.session.commit(); flash('Note deleted.','info')
    return redirect(request.referrer or url_for('notes'))

@app.route('/notes/<int:note_id>/toggle-share', methods=['POST'])
@login_required
def note_toggle_share(note_id):
    note = Note.query.get_or_404(note_id)
    if note.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    note.is_shared = not note.is_shared; db.session.commit()
    flash('Shared.' if note.is_shared else 'Unshared.','info')
    return redirect(request.referrer or url_for('notes'))


@app.route('/files')
@login_required
def files():
    fid = request.args.get('folder', type=int); cf = None; target_uid = current_user.id; bc = []
    if fid:
        cf = Folder.query.get_or_404(fid)
        if cf.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
        target_uid = cf.user_id; bc = get_breadcrumb(cf)
    folders = Folder.query.filter_by(parent_id=None).order_by(Folder.name.asc()).all() if (current_user.is_super_admin and not fid) else get_user_folders(target_uid, fid)
    flist = File.query.filter(File.folder_id==(fid if fid else None)).order_by(File.uploaded_at.desc()).all() if current_user.is_super_admin else File.query.filter_by(user_id=current_user.id).filter(File.folder_id==(fid if fid else None)).order_by(File.uploaded_at.desc()).all()
    af = Folder.query.order_by(Folder.name.asc()).all() if (current_user.is_super_admin and not fid) else Folder.query.filter_by(user_id=target_uid).order_by(Folder.name.asc()).all()
    return render_template('files.html', files=flist, folders=folders, current_folder=cf, breadcrumb=bc, all_folders=af)

@app.route('/files/folder/create', methods=['POST'])
@login_required
def folder_create():
    name = request.form.get('name','').strip(); pid = request.form.get('parent_id', type=int)
    if not name: flash('Folder name required.','danger'); return redirect(url_for('files',folder=pid))
    tuid = current_user.id
    if pid:
        p = Folder.query.get_or_404(pid)
        if p.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
        tuid = p.user_id
    db.session.add(Folder(user_id=tuid, name=name, parent_id=pid or None))
    db.session.commit(); flash(f'Folder created.','success'); return redirect(url_for('files', folder=pid))

@app.route('/files/folder/<int:folder_id>/rename', methods=['POST'])
@login_required
def folder_rename(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    if folder.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    nn = request.form.get('name','').strip()
    if nn: folder.name = nn; db.session.commit(); return jsonify({'ok':True})
    return jsonify({'ok':False,'error':'Name required'})

@app.route('/files/folder/<int:folder_id>/delete', methods=['POST'])
@login_required
def folder_delete(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    if folder.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    pid = folder.parent_id
    for f in folder.files.all(): f.folder_id = pid
    for ch in Folder.query.filter_by(parent_id=folder_id).all(): ch.parent_id = pid
    db.session.delete(folder); db.session.commit()
    flash(f'Folder deleted.','info'); return redirect(url_for('files', folder=pid))

@app.route('/files/upload', methods=['POST'])
@login_required
def file_upload():
    flist = request.files.getlist('file')
    if not flist or all(f.filename=='' for f in flist): flash('No file selected.','danger'); return redirect(url_for('files'))
    fid = request.form.get('folder_id', type=int); tuid = current_user.id
    if fid:
        folder = Folder.query.get_or_404(fid)
        if folder.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
        tuid = folder.user_id
    maxb = get_max_upload_bytes(); up=0; fail=0
    for f in flist:
        if f.filename=='': continue
        f.seek(0,os.SEEK_END); sz=f.tell(); f.seek(0)
        if sz>maxb: fail+=1; continue
        rel=f.filename.replace('\\','/'); subdirs=rel.split('/')[:-1]
        oname=safe_filename(rel.split('/')[-1])
        if not oname or oname=='unnamed': continue
        cpid=fid
        for sdn in subdirs:
            if not sdn.strip(): continue
            ex=Folder.query.filter_by(user_id=tuid,name=sdn,parent_id=cpid).first()
            if ex: cpid=ex.id
            else: nf=Folder(user_id=tuid,name=sdn,parent_id=cpid); db.session.add(nf); db.session.flush(); cpid=nf.id
        ext=('.'+oname.rsplit('.',1)[1].lower()) if '.' in oname else ''
        sn=uuid.uuid4().hex+ext; udir=get_user_upload_dir(tuid)
        f.save(os.path.join(udir,sn)); fsz=os.path.getsize(os.path.join(udir,sn))
        db.session.add(File(user_id=tuid,original_filename=oname,storage_path=sn,file_size=fsz,folder_id=cpid if cpid else fid))
        up+=1
    db.session.commit()
    if up: flash(f'{up} file(s) uploaded.' + (f' {fail} skipped.' if fail else ''), 'success')
    elif fail: flash('All files exceeded size limit.','danger')
    return redirect(url_for('files', folder=fid))

@app.route('/files/<int:file_id>/download')
@login_required
def file_download(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not fr.is_shared and not current_user.is_super_admin: abort(403)
    return send_from_directory(get_user_upload_dir(fr.user_id), fr.storage_path, download_name=fr.original_filename, as_attachment=True)

@app.route('/files/<int:file_id>/raw')
@login_required
def file_raw(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not fr.is_shared and not current_user.is_super_admin: abort(403)
    return send_from_directory(get_user_upload_dir(fr.user_id), fr.storage_path, download_name=fr.original_filename, as_attachment=False)

@app.route('/files/<int:file_id>/preview')
@login_required
def file_preview(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not fr.is_shared and not current_user.is_super_admin: abort(403)
    pt = get_preview_type(fr.original_filename); content = None; cr = None
    if pt in ('code','text','markdown','data','csv'):
        content = get_text_file_content(fr)
        if content is None: flash('Cannot read file.','danger'); return redirect(request.referrer or url_for('files'))
    if pt=='csv' and content:
        try: cr = list(csv.reader(io.StringIO(content)))
        except: cr = None
    return render_template('file_preview.html', file=fr, ptype=pt, content=content, csv_rows=cr, lines=content.count('\n')+1 if content else 0, ext=os.path.splitext(fr.original_filename)[1].lstrip('.').lower())

@app.route('/files/<int:file_id>/save', methods=['POST'])
@login_required
def file_save(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    if get_preview_type(fr.original_filename) not in ('code','text','markdown','data','csv'): return jsonify({'ok':False,'error':'Cannot edit'})
    nc = request.form.get('content','')
    try:
        with open(get_file_path(fr),'w',encoding='utf-8') as fh: fh.write(nc)
        fr.file_size = os.path.getsize(get_file_path(fr)); db.session.commit()
        return jsonify({'ok':True})
    except Exception as e: return jsonify({'ok':False,'error':str(e)})

@app.route('/files/<int:file_id>/rename', methods=['POST'])
@login_required
def file_rename(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    nn = request.form.get('name','').strip()
    if nn: fr.original_filename = nn; db.session.commit(); return jsonify({'ok':True})
    return jsonify({'ok':False,'error':'Name required'})

@app.route('/files/<int:file_id>/move', methods=['POST'])
@login_required
def file_move(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    tf = request.form.get('target_folder_id')
    if tf=='' or tf=='0' or tf is None: fr.folder_id = None
    else:
        t = Folder.query.get_or_404(int(tf))
        if t.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
        fr.folder_id = int(tf)
    db.session.commit(); flash('Moved.','info'); return redirect(request.referrer or url_for('files'))

@app.route('/files/<int:file_id>/delete', methods=['POST'])
@login_required
def file_delete(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    fp = get_file_path(fr)
    if os.path.exists(fp): os.remove(fp)
    db.session.delete(fr); db.session.commit(); flash('File deleted.','info')
    return redirect(request.referrer or url_for('files'))

@app.route('/files/<int:file_id>/toggle-share', methods=['POST'])
@login_required
def file_toggle_share(file_id):
    fr = File.query.get_or_404(file_id)
    if fr.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    fr.is_shared = not fr.is_shared; db.session.commit()
    flash('Shared.' if fr.is_shared else 'Unshared.','info')
    return redirect(request.referrer or url_for('files'))

@app.route('/blog')
@login_required
def blog():
    pq = BlogPost.query.order_by(BlogPost.created_at.desc()).all() if current_user.is_super_admin else BlogPost.query.filter_by(user_id=current_user.id).order_by(BlogPost.created_at.desc()).all()
    return render_template('blog.html', posts=pq)

@app.route('/blog/<int:post_id>')
@login_required
def blog_view(post_id):
    post = BlogPost.query.get_or_404(post_id)
    if post.user_id!=current_user.id and not post.is_public and not current_user.is_super_admin: abort(403)
    return render_template('blog_view.html', post=post)

@app.route('/blog/new', methods=['GET','POST'])
@login_required
def blog_create():
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('blog_form.html', post=None)
        db.session.add(BlogPost(user_id=current_user.id,title=t,content=request.form.get('content',''),is_public=request.form.get('is_public')=='on'))
        db.session.commit(); flash('Blog published.','success'); return redirect(url_for('blog'))
    return render_template('blog_form.html', post=None)

@app.route('/blog/<int:post_id>/edit', methods=['GET','POST'])
@login_required
def blog_edit(post_id):
    post = BlogPost.query.get_or_404(post_id)
    if post.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('blog_form.html', post=post)
        post.title=t; post.content=request.form.get('content',''); post.is_public=request.form.get('is_public')=='on'
        db.session.commit(); flash('Blog updated.','success'); return redirect(url_for('blog'))
    return render_template('blog_form.html', post=post)

@app.route('/blog/<int:post_id>/delete', methods=['POST'])
@login_required
def blog_delete(post_id):
    post = BlogPost.query.get_or_404(post_id)
    if post.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    db.session.delete(post); db.session.commit(); flash('Blog deleted.','info')
    return redirect(request.referrer or url_for('blog'))

@app.route('/blog/<int:post_id>/toggle-public', methods=['POST'])
@login_required
def blog_toggle_public(post_id):
    post = BlogPost.query.get_or_404(post_id)
    if post.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    post.is_public = not post.is_public; db.session.commit()
    flash('Published.' if post.is_public else 'Private.','info')
    return redirect(request.referrer or url_for('blog'))

# ─── Resume ─────────────────────────────────────────────────
DEFAULT_RESUME_DATA = {
    'basics': {'name':'','title':'','phone':'','email':'','location':'','links':[],'photo':'','photo_shape':'square'},
    'summary': '',
    'education': [],
    'experience': [],
    'projects': [],
    'skills': [],
    'awards': [],
    'style': {
        'font_size': 100,        # 80 ~ 140 (百分比)
        'line_height': 1.55,     # 1.0 ~ 2.5
        'section_gap': 5,        # mm, 2 ~ 12
        'page_margin': 'default',# narrow / default / wide
        'font_family': 'sans'    # sans / serif / songti / heiti / mono
    }
}
PHOTO_ALLOWED_EXT = {'.jpg','.jpeg','.png','.webp'}
PHOTO_MAX_BYTES = 8 * 1024 * 1024  # 8 MB

def get_resume_photo_dir(uid):
    u = User.query.get(uid)
    name = (u.username if u else str(uid)).replace('/','_').replace('\\','_')
    d = os.path.join(UPLOAD_DIR, name, 'resume_photos')
    os.makedirs(d, exist_ok=True)
    return d

def _normalize_resume_data(d):
    """规范化 AI 或外部导入的数据，确保结构符合前端期望。"""
    if not isinstance(d, dict): d = {}

    # basics
    b = d.get('basics')
    if not isinstance(b, dict): b = {}
    for k in ('name','title','phone','email','location','photo','photo_shape'):
        b.setdefault(k, '')
    links = b.get('links')
    if isinstance(links, dict):
        b['links'] = [{'label':k, 'url':v if isinstance(v,str) else ''} for k,v in links.items()]
    elif isinstance(links, list):
        nlinks = []
        for it in links:
            if isinstance(it, dict):
                nlinks.append({'label': str(it.get('label','')), 'url': str(it.get('url',''))})
            elif isinstance(it, str):
                nlinks.append({'label':'', 'url': it})
        b['links'] = nlinks
    else:
        b['links'] = []
    d['basics'] = b

    # summary
    if not isinstance(d.get('summary'), str):
        s = d.get('summary')
        d['summary'] = '' if s is None else (str(s) if not isinstance(s,(list,dict)) else json.dumps(s, ensure_ascii=False))

    # 列表字段统一处理
    def to_list_of_dict(arr, field_map):
        """把任意形状转成 list of dict（按 field_map 的 key 兜底）"""
        out = []
        if isinstance(arr, dict):
            # 字典 → 每个 entry 转成 dict
            for k, v in arr.items():
                if isinstance(v, dict):
                    item = dict(v)
                    # 把 dict key 当成 category/name
                    item.setdefault(field_map['name_field'], k)
                    out.append(item)
                else:
                    out.append({field_map['name_field']: k, field_map['value_field']: str(v) if v else ''})
            return out
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    out.append(it)
                elif isinstance(it, str):
                    out.append({field_map['name_field']: '', field_map['value_field']: it})
            return out
        return []

    # education
    d['education'] = to_list_of_dict(d.get('education'), {'name_field':'school','value_field':'description'})
    for e in d['education']:
        for k in ('school','degree','major','start','end','gpa','description'):
            e.setdefault(k, '')
            if not isinstance(e[k], str): e[k] = json.dumps(e[k], ensure_ascii=False) if e[k] else ''

    # experience
    d['experience'] = to_list_of_dict(d.get('experience'), {'name_field':'company','value_field':'description'})
    for e in d['experience']:
        for k in ('company','title','start','end','location','description'):
            e.setdefault(k, '')
            if isinstance(e[k], list): e[k] = '\n'.join(str(x) for x in e[k])
            elif not isinstance(e[k], str): e[k] = str(e[k]) if e[k] else ''

    # projects
    d['projects'] = to_list_of_dict(d.get('projects'), {'name_field':'name','value_field':'description'})
    for p in d['projects']:
        for k in ('name','role','start','end','tech','description','contribution','achievements'):
            p.setdefault(k, '')
            if isinstance(p[k], list): p[k] = '\n'.join(str(x) for x in p[k])
            elif not isinstance(p[k], str): p[k] = str(p[k]) if p[k] else ''

    # skills（重点：AI 经常返回 dict）
    d['skills'] = to_list_of_dict(d.get('skills'), {'name_field':'category','value_field':'items'})
    for s in d['skills']:
        s.setdefault('category', '')
        s.setdefault('items', '')
        # items 可能是 list
        if isinstance(s['items'], list): s['items'] = '、'.join(str(x) for x in s['items'])
        elif not isinstance(s['items'], str): s['items'] = str(s['items']) if s['items'] else ''
        if not isinstance(s['category'], str): s['category'] = str(s['category']) if s['category'] else ''

    # awards
    d['awards'] = to_list_of_dict(d.get('awards'), {'name_field':'name','value_field':'description'})
    for a in d['awards']:
        for k in ('name','date','description'):
            a.setdefault(k, '')
            if not isinstance(a[k], str): a[k] = str(a[k]) if a[k] else ''

    # style（排版调节）
    st = d.get('style')
    if not isinstance(st, dict): st = {}
    def _int(v, dft, lo, hi):
        try: r = int(v)
        except Exception: r = dft
        return max(lo, min(hi, r))
    def _float(v, dft, lo, hi):
        try: r = float(v)
        except Exception: r = dft
        return max(lo, min(hi, r))
    st['font_size']   = _int(st.get('font_size', 100), 100, 80, 150)
    st['line_height'] = _float(st.get('line_height', 1.55), 1.55, 1.0, 2.8)
    st['section_gap'] = _int(st.get('section_gap', 5), 5, 1, 20)
    pm = st.get('page_margin', 'default')
    st['page_margin'] = pm if pm in ('narrow','default','wide') else 'default'
    ff = st.get('font_family', 'sans')
    st['font_family'] = ff if ff in ('sans','serif','songti','heiti','mono') else 'sans'
    d['style'] = st

    # 补默认字段
    for k in DEFAULT_RESUME_DATA:
        d.setdefault(k, json.loads(json.dumps(DEFAULT_RESUME_DATA[k])))
    return d


def _load_resume_data(r):
    try:
        d = json.loads(r.data or '{}')
    except Exception:
        d = {}
    return _normalize_resume_data(d)

@app.route('/resume')
@login_required
def resume_list():
    rs = Resume.query.order_by(Resume.updated_at.desc()).all() if current_user.is_super_admin else Resume.query.filter_by(user_id=current_user.id).order_by(Resume.updated_at.desc()).all()
    return render_template('resume.html', resumes=rs)

@app.route('/resume/new', methods=['POST'])
@login_required
def resume_create():
    title = request.form.get('title','我的简历').strip() or '我的简历'
    data = json.dumps(DEFAULT_RESUME_DATA, ensure_ascii=False)
    # 自动用用户名填初值
    init = json.loads(data)
    init['basics']['name'] = current_user.username
    r = Resume(user_id=current_user.id, title=title, data=json.dumps(init, ensure_ascii=False))
    db.session.add(r); db.session.commit()
    flash('简历已创建，开始编辑吧。','success')
    return redirect(url_for('resume_edit', resume_id=r.id))

@app.route('/resume/<int:resume_id>')
@login_required
def resume_view(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    return render_template('resume_view.html', resume=r, data=_load_resume_data(r))

@app.route('/resume/<int:resume_id>/edit', methods=['GET','POST'])
@login_required
def resume_edit(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    if request.method == 'POST':
        # 接收 JSON
        payload = request.get_json(silent=True) or {}
        title = (payload.get('title') or r.title).strip() or '我的简历'
        target_company = (payload.get('target_company') or '').strip()
        target_role = (payload.get('target_role') or '').strip()
        data = payload.get('data') or {}
        # 归一化（防止前端发来非预期结构）
        cleaned = _normalize_resume_data(data)
        r.title = title[:200]
        r.target_company = target_company[:200]
        r.target_role = target_role[:200]
        r.data = json.dumps(cleaned, ensure_ascii=False)
        db.session.commit()
        return jsonify({'ok': True, 'updated_at': r.updated_at.strftime('%H:%M:%S')})
    return render_template('resume_form.html', resume=r, data=_load_resume_data(r))

@app.route('/resume/<int:resume_id>/delete', methods=['POST'])
@login_required
def resume_delete(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    db.session.delete(r); db.session.commit()
    flash('简历已删除。','info')
    return redirect(url_for('resume_list'))

@app.route('/resume/<int:resume_id>/duplicate', methods=['POST'])
@login_required
def resume_duplicate(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    new = Resume(
        user_id=current_user.id,
        title=(r.title + ' 副本')[:200],
        target_company=r.target_company,
        target_role=r.target_role,
        data=r.data
    )
    db.session.add(new); db.session.commit()
    flash('已复制简历，可针对新岗位调整。','success')
    return redirect(url_for('resume_edit', resume_id=new.id))

@app.route('/resume/<int:resume_id>/style', methods=['POST'])
@login_required
def resume_save_style(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    payload = request.get_json(silent=True) or {}
    cur = _load_resume_data(r)
    # 仅更新 style 字段，并归一化
    cur['style'] = payload
    cleaned = _normalize_resume_data(cur)
    r.data = json.dumps(cleaned, ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True, 'style': cleaned['style']})


@app.route('/resume/<int:resume_id>/photo', methods=['POST'])
@login_required
def resume_upload_photo(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    f = request.files.get('photo')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': '未选择文件'})
    ext = os.path.splitext(f.filename.lower())[1]
    if ext not in PHOTO_ALLOWED_EXT:
        return jsonify({'ok': False, 'error': '仅支持 jpg / jpeg / png / webp'})
    f.seek(0, os.SEEK_END); sz = f.tell(); f.seek(0)
    if sz > PHOTO_MAX_BYTES:
        return jsonify({'ok': False, 'error': f'图片过大（>{PHOTO_MAX_BYTES // 1048576}MB）'})

    # 删除该简历旧照片
    cur = _load_resume_data(r)
    old = (cur.get('basics') or {}).get('photo','')
    if old and old.startswith('resume_photos/'):
        try:
            op = os.path.join(get_resume_photo_dir(r.user_id), os.path.basename(old))
            if os.path.exists(op): os.remove(op)
        except Exception: pass

    # 保存新照片
    new_name = f'r{r.id}_{uuid.uuid4().hex[:12]}{ext}'
    pdir = get_resume_photo_dir(r.user_id)
    f.save(os.path.join(pdir, new_name))

    # 写入 resume.data
    cur.setdefault('basics', {})
    cur['basics']['photo'] = 'resume_photos/' + new_name
    shape = (request.form.get('shape') or cur['basics'].get('photo_shape') or 'square').lower()
    if shape not in ('square','circle','rounded'): shape = 'square'
    cur['basics']['photo_shape'] = shape
    r.data = json.dumps(cur, ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True, 'photo': cur['basics']['photo'], 'shape': shape, 'url': url_for('resume_photo', resume_id=r.id, filename=new_name)})


@app.route('/resume/<int:resume_id>/photo/<path:filename>')
@login_required
def resume_photo(resume_id, filename):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    # 防止路径穿越
    safe = os.path.basename(filename)
    pdir = get_resume_photo_dir(r.user_id)
    fp = os.path.join(pdir, safe)
    if not os.path.exists(fp): abort(404)
    return send_from_directory(pdir, safe, as_attachment=False)


@app.route('/resume/<int:resume_id>/photo/delete', methods=['POST'])
@login_required
def resume_delete_photo(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    cur = _load_resume_data(r)
    old = (cur.get('basics') or {}).get('photo','')
    if old and old.startswith('resume_photos/'):
        try:
            op = os.path.join(get_resume_photo_dir(r.user_id), os.path.basename(old))
            if os.path.exists(op): os.remove(op)
        except Exception: pass
    cur.setdefault('basics', {})
    cur['basics']['photo'] = ''
    r.data = json.dumps(cur, ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/resume/<int:resume_id>/photo/shape', methods=['POST'])
@login_required
def resume_photo_shape(resume_id):
    r = Resume.query.get_or_404(resume_id)
    if r.user_id != current_user.id and not current_user.is_super_admin: abort(403)
    shape = (request.get_json(silent=True) or {}).get('shape','square')
    if shape not in ('square','circle','rounded'): shape = 'square'
    cur = _load_resume_data(r)
    cur.setdefault('basics', {})
    cur['basics']['photo_shape'] = shape
    r.data = json.dumps(cur, ensure_ascii=False)
    db.session.commit()
    return jsonify({'ok': True, 'shape': shape})


@app.route('/code')
@login_required
def code():
    sq = CodeSnippet.query.order_by(CodeSnippet.updated_at.desc()).all() if current_user.is_super_admin else CodeSnippet.query.filter_by(user_id=current_user.id).order_by(CodeSnippet.updated_at.desc()).all()
    return render_template('code.html', snippets=sq, languages=CODE_LANGUAGES)

@app.route('/code/new', methods=['GET','POST'])
@login_required
def code_create():
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('code_form.html', snippet=None, languages=CODE_LANGUAGES)
        db.session.add(CodeSnippet(user_id=current_user.id,title=t,language=request.form.get('language','plaintext'),content=request.form.get('content',''),is_shared=request.form.get('is_shared')=='on'))
        db.session.commit(); flash('Code created.','success'); return redirect(url_for('code'))
    return render_template('code_form.html', snippet=None, languages=CODE_LANGUAGES)

@app.route('/code/<int:snippet_id>')
@login_required
def code_view(snippet_id):
    sn = CodeSnippet.query.get_or_404(snippet_id)
    if sn.user_id!=current_user.id and not sn.is_shared and not current_user.is_super_admin: abort(403)
    return render_template('code_view_detail.html', snippet=sn)

@app.route('/code/<int:snippet_id>/edit', methods=['GET','POST'])
@login_required
def code_edit(snippet_id):
    sn = CodeSnippet.query.get_or_404(snippet_id)
    if sn.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    if request.method=='POST':
        t = request.form.get('title','').strip()
        if not t: flash('Title required.','danger'); return render_template('code_form.html', snippet=sn, languages=CODE_LANGUAGES)
        sn.title=t; sn.language=request.form.get('language',sn.language); sn.content=request.form.get('content',''); sn.is_shared=request.form.get('is_shared')=='on'
        db.session.commit(); flash('Code updated.','success'); return redirect(url_for('code'))
    return render_template('code_form.html', snippet=sn, languages=CODE_LANGUAGES)

@app.route('/code/<int:snippet_id>/delete', methods=['POST'])
@login_required
def code_delete(snippet_id):
    sn = CodeSnippet.query.get_or_404(snippet_id)
    if sn.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    db.session.delete(sn); db.session.commit(); flash('Code deleted.','info')
    return redirect(request.referrer or url_for('code'))

@app.route('/code/<int:snippet_id>/toggle-share', methods=['POST'])
@login_required
def code_toggle_share(snippet_id):
    sn = CodeSnippet.query.get_or_404(snippet_id)
    if sn.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    sn.is_shared = not sn.is_shared; db.session.commit()
    flash('Shared.' if sn.is_shared else 'Unshared.','info')
    return redirect(request.referrer or url_for('code'))

def _run_python(code):
    tp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',suffix='.py',delete=False,encoding='utf-8') as f: f.write(code); tp=f.name
        r = subprocess.run(['python',tp], capture_output=True, text=True, timeout=8, cwd=tempfile.gettempdir(), env={**os.environ,'PYTHONUNBUFFERED':'1'})
        return jsonify({'ok':True,'stdout':r.stdout,'stderr':r.stderr,'exit_code':r.returncode})
    except subprocess.TimeoutExpired: return jsonify({'error':'Timeout (8s)'})
    except Exception as e: return jsonify({'error':str(e)})
    finally:
        if tp and os.path.exists(tp): os.unlink(tp)

@app.route('/code/<int:snippet_id>/run', methods=['POST'])
@login_required
def code_run(snippet_id):
    sn = CodeSnippet.query.get_or_404(snippet_id)
    if sn.user_id!=current_user.id and not current_user.is_super_admin: abort(403)
    if sn.language!='python': return jsonify({'error':'Only Python supported'})
    return _run_python(sn.content)

@app.route('/code/run', methods=['POST'])
@login_required
def code_run_direct():
    d = request.get_json(silent=True) or {}
    if d.get('language','')!='python': return jsonify({'error':'Only Python supported'})
    return _run_python(d.get('content',''))

@app.route('/code/check', methods=['POST'])
@login_required
def code_check():
    d = request.get_json(silent=True) or {}; lang = d.get('language',''); content = d.get('content',''); issues = []
    if lang=='python':
        try: compile(content,'<check>','exec')
        except SyntaxError as e: issues.append({'line':e.lineno or 1,'msg':f'Syntax: {e.msg}','type':'error'})
        lines=content.split('\n'); ht=hs=False; il=set()
        for i,line in enumerate(lines,1):
            if not line.strip(): continue
            stripped=line.lstrip()
            if stripped==line: continue
            leading=line[:len(line)-len(stripped)]
            if '\t' in leading: ht=True
            if leading.startswith(' '): hs=True
            il.add(len(leading.expandtabs(4)))
        if ht and hs: issues.append({'line':1,'msg':'Mixed tabs/spaces','type':'warning'})
        if il:
            steps=set(); sl=sorted(il)
            for j in range(1,len(sl)):
                d2=sl[j]-sl[j-1]
                if d2>0: steps.add(d2)
            if len(steps)>1: issues.append({'line':1,'msg':f'Inconsistent indent steps {sorted(steps)}','type':'warning'})
        for i,line in enumerate(lines,1):
            if len(line)>120: issues.append({'line':i,'msg':f'Line {i} exceeds 120 chars','type':'info'})
            if len(issues)>=8: break
    elif lang in ('javascript','typescript'):
        bc=0
        for i,line in enumerate(content.split('\n'),1):
            bc+=line.count('{')-line.count('}')+line.count('(')-line.count(')')+line.count('[')-line.count(']')
            if len(line)>120: issues.append({'line':i,'msg':f'Line {i} exceeds 120 chars','type':'info'})
        if bc!=0: issues.append({'line':1,'msg':f'Bracket mismatch (net: {bc})','type':'error'})
    elif lang=='html':
        tags=re.findall(r'<(/?)\s*(\w+)',content); stack=[]
        void_tags={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
        for slash,tag in tags:
            tl=tag.lower()
            if not slash and tl not in void_tags: stack.append(tl)
            elif slash and stack and stack[-1]==tl: stack.pop()
        if stack: issues.append({'line':1,'msg':f'Unclosed tags: {", ".join(stack[-3:])}','type':'warning'})
    else:
        for i,line in enumerate(content.split('\n'),1):
            if len(line)>120: issues.append({'line':i,'msg':f'Line {i} exceeds 120 chars','type':'info'})
            if len(issues)>=8: break
    return jsonify({'ok':not any(i['type']=='error' for i in issues),'issues':issues})

@app.route('/learn')
@login_required
def learn_index():
    return redirect(url_for('learn_ai_agent'))

@app.route('/learn/ai-agent')
@login_required
def learn_ai_agent():
    return render_template('learn_ai_agent.html')


@app.route('/shared')
@login_required
def shared():
    return render_template('shared.html', shared_notes=Note.query.filter_by(is_shared=True).order_by(Note.updated_at.desc()).all(), shared_files=File.query.filter_by(is_shared=True).order_by(File.uploaded_at.desc()).all(), public_posts=BlogPost.query.filter_by(is_public=True).order_by(BlogPost.created_at.desc()).all(), shared_snippets=CodeSnippet.query.filter_by(is_shared=True).order_by(CodeSnippet.updated_at.desc()).all())

@app.route('/admin')
@admin_required
def admin():
    stats = {'notes':Note.query.count(),'files':File.query.count(),'codes':CodeSnippet.query.count(),'blogs':BlogPost.query.count(),'shared_notes':Note.query.filter_by(is_shared=True).count(),'shared_files':File.query.filter_by(is_shared=True).count(),'shared_codes':CodeSnippet.query.filter_by(is_shared=True).count(),'public_blogs':BlogPost.query.filter_by(is_public=True).count()}
    return render_template('admin.html', stats=stats)

@app.route('/admin/users')
@super_admin_required
def admin_users():
    return render_template('admin_users.html', users=User.query.order_by(User.id.asc()).all())

@app.route('/admin/settings', methods=['GET','POST'])
@admin_required
def admin_settings():
    if request.method=='POST':
        sn = request.form.get('site_name','').strip()
        if sn:
            s = SiteSetting.query.get('site_name')
            if s: s.value = sn
            else: db.session.add(SiteSetting(key='site_name',value=sn))
        mm = request.form.get('max_upload_mb','').strip()
        if mm and mm.isdigit():
            m = int(mm)
            if 1<=m<=2048:
                s = SiteSetting.query.get('max_upload_mb')
                if s: s.value = str(m)
                else: db.session.add(SiteSetting(key='max_upload_mb',value=str(m)))
                app.config['MAX_CONTENT_LENGTH'] = m*1048576
        sg = request.form.get('site_guide','')
        s = SiteSetting.query.get('site_guide')
        if s: s.value = sg
        else: db.session.add(SiteSetting(key='site_guide',value=sg))
        dk = request.form.get('deepseek_key','').strip()
        if dk:
            with open(os.path.join(DATA_DIR,'deepseek_key.txt'),'w',encoding='utf-8') as f: f.write(dk)
        db.session.commit(); flash('Settings saved.','success')
        return redirect(url_for('admin_settings'))
    cn = get_site_name()
    s = SiteSetting.query.get('max_upload_mb')
    try: cm = int(s.value) if s and s.value else 200
    except: cm = 200
    return render_template('admin_settings.html', site_name=cn, max_upload_mb=cm, site_guide=get_site_guide(), deepseek_key=get_deepseek_key())

@app.route('/admin/users/<int:user_id>/export')
@super_admin_required
def admin_export_user(user_id):
    u = User.query.get_or_404(user_id)
    data = {'username':u.username,'created_at':str(u.created_at),'notes':[],'code_snippets':[],'blog_posts':[],'files':[]}
    for n in u.notes.all(): data['notes'].append({'title':n.title,'content':n.content,'is_shared':n.is_shared,'created_at':str(n.created_at)})
    for s in u.code_snippets.all(): data['code_snippets'].append({'title':s.title,'language':s.language,'content':s.content,'is_shared':s.is_shared,'created_at':str(s.created_at)})
    for p in u.blog_posts.all(): data['blog_posts'].append({'title':p.title,'content':p.content,'is_public':p.is_public,'created_at':str(p.created_at)})
    for f in u.files.all(): data['files'].append({'filename':f.original_filename,'size':f.file_size,'is_shared':f.is_shared,'uploaded_at':str(f.uploaded_at)})
    return send_file(io.BytesIO(json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')), mimetype='application/json', as_attachment=True, download_name=f'{u.username}_export.json')

@app.route('/admin/users/<int:user_id>/approve', methods=['POST'])
@admin_required
def admin_approve(user_id):
    u = User.query.get_or_404(user_id); u.is_approved = True; db.session.commit()
    flash(f'User approved.','success'); return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
@super_admin_required
def admin_toggle(user_id):
    u = User.query.get_or_404(user_id)
    if u.id==current_user.id: flash('Cannot change own status.','danger'); return redirect(url_for('admin_users'))
    u.is_admin = not u.is_admin
    if not u.is_admin: u.is_super_admin = False
    db.session.commit(); flash(f'User status updated.','info')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@super_admin_required
def admin_delete_user(user_id):
    u = User.query.get_or_404(user_id)
    if u.id==current_user.id: flash('Cannot delete yourself.','danger'); return redirect(url_for('admin_users'))
    ud = get_user_upload_dir(u.id)
    if os.path.exists(ud): shutil.rmtree(ud)
    db.session.delete(u); db.session.commit()
    flash(f'User deleted.','info'); return redirect(url_for('admin_users'))

@app.route('/api/ai/chat', methods=['POST'])
@login_required
def ai_chat():
    api_key = get_deepseek_key()
    if not api_key: return jsonify({'reply': 'Set DeepSeek API Key in admin settings.'})
    data = request.get_json(silent=True) or {}
    messages = data.get('messages',[]); context_key = data.get('context','general'); page_content = data.get('page_content','')
    system_prompt = AI_CONTEXTS.get(context_key, AI_CONTEXTS['general'])
    if page_content: system_prompt += f'\n\nPage content:\n{page_content[:4000]}'
    payload = {'model':'deepseek-chat','messages':[{'role':'system','content':system_prompt}]+messages,'temperature':0.7,'max_tokens':2048,'stream':False}
    try:
        resp = http_requests.post('https://api.deepseek.com/v1/chat/completions', headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'}, json=payload, timeout=30)
        if resp.status_code!=200: return jsonify({'reply':f'API error {resp.status_code}'})
        result = resp.json()
        return jsonify({'reply':result['choices'][0]['message']['content']})
    except Exception as e: return jsonify({'reply':f'Error: {str(e)}'})

@app.errorhandler(403)
def forbidden(e): return render_template('error.html', code=403, message='Forbidden.'), 403
@app.errorhandler(404)
def not_found(e): return render_template('error.html', code=404, message='Not found.'), 404

DEFAULT_GUIDE = """# 欢迎来到 {site_name}

你的个人局域网空间 —— 笔记、文件、代码、博客、简历，一站式管理。

## 功能模块

- **笔记**：Markdown 编辑与实时预览，一键共享到广场
- **代码**：多语言语法高亮，Python 在线执行，格式 / 语法检查
- **文件**：拖拽上传、在线预览（图片 / PDF / 音视频 / CSV / 代码）、文件夹管理
- **博客**：Markdown 排版，公开或私密发布
- **简历**：可视化编辑、证件照上传、7 套模板、排版微调、一键导出高清 PDF
- **AI 学院**：内置《AI Agent 开发实战》零基础教程 + 实战项目
- **广场**：发现全站共享内容
- **AI 助手**：右侧栏对话助手，按页面自动切换角色

## 使用技巧

- **键盘快捷键**：`N` 笔记 · `C` 代码 · `U` 文件 · `B` 博客 · `R` 简历 · `S` 广场 · `H` 首页
- **拖拽上传**：文件页直接把文件 / 文件夹拖入即可上传
- **行内重命名**：点击文件 / 文件夹名旁的铅笔图标
- **主题切换**：右上角按钮切换暗 / 亮主题
- **悬浮菜单**：右下角 + 号快速新建内容

> 提示：管理员可在「管理 → 站点设置」中修改本指南内容。
"""

def ensure_admin():
    au = os.environ.get('ADMIN_USERNAME','admin').strip(); ap = os.environ.get('ADMIN_PASSWORD','').strip()
    if not ap: return
    ex = User.query.filter_by(username=au).first()
    if ex:
        if not check_password_hash(ex.password_hash, ap): ex.password_hash = generate_password_hash(ap)
        ex.is_admin = True; ex.is_super_admin = True; ex.is_approved = True
    else:
        db.session.add(User(username=au,password_hash=generate_password_hash(ap),is_admin=True,is_super_admin=True,is_approved=True))
    db.session.commit()
    if not SiteSetting.query.get('site_guide'):
        db.session.add(SiteSetting(key='site_guide',value=DEFAULT_GUIDE)); db.session.commit()

def safe_startup():
    import sqlite3
    dp = DB_PATH; is_sqlite = app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite')
    if is_sqlite and os.path.exists(dp):
        print('  [DB] Database found - data preserved.')
        try: shutil.copy2(dp, os.path.join(DATA_DIR,'data.db.backup'))
        except: pass
        db.create_all()
        conn = sqlite3.connect(dp); c = conn.cursor()
        for tbl, cols in {'users':[('is_admin','BOOLEAN DEFAULT 0'),('is_super_admin','BOOLEAN DEFAULT 0'),('is_approved','BOOLEAN DEFAULT 0')],'files':[('folder_id','INTEGER')]}.items():
            try:
                c.execute(f"PRAGMA table_info({tbl})"); ec = {r[1] for r in c.fetchall()}
                for cn, cd in cols:
                    if cn not in ec: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {cn} {cd}"); print(f'  [MIGRATE] +{tbl}.{cn}')
            except: pass
        conn.commit(); conn.close()
    else:
        print(f'  [DB] Creating tables...'); db.create_all()
    ensure_admin()
    s = SiteSetting.query.get('max_upload_mb')
    try: mb = int(s.value) if s and s.value else 200
    except: mb = 200
    app.config['MAX_CONTENT_LENGTH'] = mb * 1048576
    print(f'  [OK] Max upload: {mb}MB. Ready.')

if __name__ == '__main__':
    with app.app_context(): safe_startup()
    hot = os.environ.get('HOT_RELOAD','1')=='1'
    host = os.environ.get('HOST','0.0.0.0'); port = int(os.environ.get('PORT','2213'))
    print(f'  [START] http://{host}:{port}  hot-reload: {hot}')
    # Show all available IPs
    import socket
    try:
        ips = []
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith(('192.168.', '10.', '172.')) and not ip.startswith('198.18.'):
                ips.append(ip)
        if not ips:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80)); ips = [s.getsockname()[0]]; s.close()
        for ip in ips:
            print(f'  [LAN] Access from other devices: http://{ip}:{port}')
    except: pass
    app.run(host=host, port=port, debug=hot, use_reloader=hot)
