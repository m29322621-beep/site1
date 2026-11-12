from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify, send_from_directory, abort
import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps
import re
import uuid
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=BASE_DIR, static_folder=os.path.join(BASE_DIR, 'static'))
app.secret_key = 'your_secret_key_here'
app.permanent_session_lifetime = timedelta(hours=8)

# ===== Common helpers for the extended features =====
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
ALLOWED_UPLOAD = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'heic'}
LESSON_DURATIONS = (60, 90, 120)
MAX_STUDENTS_CHOICES = (5, 7, 9)
ROLE_LABELS = {'director': 'директор', 'senior_manager': 'старший менеджер', 'manager': 'менеджер',
               'senior_repetitor': 'старший репетитор', 'repetitor': 'репетитор',
               'admin': 'админ'}
WEEKDAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']


def msk_now():
    return datetime.utcnow() + timedelta(hours=3)


def msk_today():
    return msk_now().strftime('%Y-%m-%d')


def save_upload(file_storage):
    """Returns saved file name, '' if nothing uploaded, None if the file type is not allowed."""
    if not file_storage or not file_storage.filename:
        return ''
    name = secure_filename(file_storage.filename) or file_storage.filename
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED_UPLOAD:
        return None
    stored = uuid.uuid4().hex + '.' + ext
    file_storage.save(os.path.join(UPLOAD_DIR, stored))
    return stored


def get_setting(cursor, key, default=None):
    cursor.execute('SELECT value FROM settings WHERE key=?', (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def set_setting(cursor, key, value):
    cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))


def week_start(d):
    return d - timedelta(days=d.weekday())


def repetitor_load(cursor, username):
    """(number of regular students, max students or None)."""
    cursor.execute('''SELECT COUNT(DISTINCT student_name) FROM submitted_records
                      WHERE repetitor_username=? AND lesson_type="regular"''', (username,))
    count = cursor.fetchone()[0]
    cursor.execute('SELECT max_students FROM users WHERE username=?', (username,))
    row = cursor.fetchone()
    return count, (row[0] if row and row[0] else None)


def pending_report_lessons(cursor, username):
    """Regular lessons that already ended but have no report yet."""
    duration = int(get_setting(cursor, 'lesson_duration_min', 60))
    cursor.execute('''SELECT id, student_name, subject, lesson_date, lesson_time FROM regular_lessons
                      WHERE repetitor_username=? AND status="scheduled" AND COALESCE(report_submitted,0)=0
                      ORDER BY lesson_date, lesson_time''', (username,))
    now = datetime.now()
    result = []
    for row in cursor.fetchall():
        try:
            start = datetime.strptime(f'{row[3]} {row[4]}', '%Y-%m-%d %H:%M')
        except ValueError:
            continue
        if now >= start + timedelta(minutes=duration):
            result.append(row)
    return result


def _weekday_of(date_str):
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').weekday()
    except (TypeError, ValueError):
        return -1


def bound_repetitors(cursor, student_name):
    """Repetitors who already worked with this student (the 'bound' ones)."""
    found = set()
    for sql in ('SELECT repetitor_username FROM student_info WHERE student_name=?',
                'SELECT repetitor_username FROM submitted_records WHERE student_name=?',
                'SELECT repetitor_username FROM trial_lessons WHERE student_name=?'):
        cursor.execute(sql, (student_name,))
        found.update(r[0] for r in cursor.fetchall() if r[0])
    return found


def response_periods(cursor, username):
    """Responses sent today / this week / previous week (weeks reset on Monday, MSK)."""
    today = msk_now().date()
    monday = week_start(today)
    prev = monday - timedelta(days=7)
    fmt = lambda d: d.strftime('%Y-%m-%d')
    def total(a, b):
        cursor.execute('''SELECT COALESCE(SUM(responses_sent),0), COALESCE(SUM(client_replied),0), COALESCE(SUM(client_enrolled),0)
                          FROM manager_responses WHERE manager_username=? AND report_date BETWEEN ? AND ?''', (username, fmt(a), fmt(b)))
        return cursor.fetchone()
    return {'today': total(today, today), 'week': total(monday, today), 'prev_week': total(prev, monday - timedelta(days=1))}


def log_lesson_change(cursor, repetitor, lesson_id, kind, new_date, new_time, reason):
    cursor.execute('SELECT student_name, lesson_date, lesson_time FROM regular_lessons WHERE id=? AND repetitor_username=?', (lesson_id, repetitor))
    old = cursor.fetchone()
    if not old:
        return
    cursor.execute('''INSERT INTO lesson_changes (repetitor_username, student_name, kind, old_date, old_time, new_date, new_time, reason, created_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                   (repetitor, old[0], kind, old[1], old[2], new_date, new_time or (old[2] if new_date else None), reason,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


PANELS = {'admin': 'admin', 'director': 'director', 'senior_manager': 'senior_manager', 'manager': 'manager',
          'repetitor': 'repetitor_main', 'senior_repetitor': 'senior_repetitor'}


def panel_url():
    """Own panel of the logged-in user: the target of every "Назад" button that leads home."""
    return url_for(PANELS.get(session.get('role'), 'login'))


@app.context_processor
def inject_panel_url():
    return {'panel_url': panel_url()}


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect('users.db')
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'username' not in session or (session.get('role') not in roles and session.get('role') != 'admin'):
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            subject TEXT,
            name TEXT,
            added_by TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS manager_repetitor_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL UNIQUE,
            repetitor_username TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            repetitor_username TEXT NOT NULL,
            subject TEXT NOT NULL,
            lesson_date TEXT NOT NULL,
            lesson_time TEXT NOT NULL,
            status TEXT DEFAULT 'scheduled',
            cancellation_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS senior_manager_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL,
            student_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            additional_info TEXT,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS trial_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            manager_username TEXT NOT NULL,
            repetitor_username TEXT,
            scheduled_date TEXT,
            scheduled_time TEXT,
            status TEXT DEFAULT 'unassigned',
            result TEXT,
            reason TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS regular_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            repetitor_username TEXT NOT NULL,
            subject TEXT NOT NULL,
            lesson_date TEXT NOT NULL,
            lesson_time TEXT NOT NULL,
            lesson_type TEXT DEFAULT 'regular',
            status TEXT DEFAULT 'scheduled',
            cancellation_reason TEXT,
            check_uploaded INTEGER DEFAULT 0,
            report_submitted INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS manager_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL,
            student_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            additional_info TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS submitted_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL,
            student_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            repetitor_username TEXT,
            viewed INTEGER DEFAULT 0,
            lesson_type TEXT DEFAULT 'trial',
            additional_info TEXT,
            added_by_role TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS manager_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL,
            responses_sent INTEGER DEFAULT 0,
            client_replied INTEGER DEFAULT 0,
            client_enrolled INTEGER DEFAULT 0,
            report_date TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS salary_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repetitor_username TEXT NOT NULL UNIQUE,
            rate_percent REAL NOT NULL DEFAULT 50.0,
            trial_lesson_rate REAL NOT NULL DEFAULT 500.0,
            set_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS salary_accruals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repetitor_username TEXT NOT NULL,
            lesson_id INTEGER,
            lesson_type TEXT NOT NULL,
            lesson_amount REAL NOT NULL,
            accrual_amount REAL NOT NULL,
            accrued_at TEXT NOT NULL,
            paid INTEGER DEFAULT 0,
            paid_at TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS kosyaki (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_username TEXT NOT NULL,
            reporter_role TEXT NOT NULL,
            target_username TEXT NOT NULL,
            reason TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS teacher_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_username TEXT NOT NULL,
            teacher_username TEXT NOT NULL,
            period TEXT NOT NULL,
            report_text TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )''')

        cursor.execute("PRAGMA table_info(users)")
        cols = [c[1] for c in cursor.fetchall()]
        if 'name' not in cols:
            cursor.execute('ALTER TABLE users ADD COLUMN name TEXT')
            cursor.execute('UPDATE users SET name = username WHERE name IS NULL')

        cursor.execute("INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)",
                       ('admin', 'admin', 'admin', 'admin'))
        cursor.execute('UPDATE users SET role="admin" WHERE username="admin"')
        cursor.execute('UPDATE users SET name = username WHERE name IS NULL')
        db.commit()


def migrate_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()

        def ensure_column(table, column, col_type='TEXT'):
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [c[1] for c in cursor.fetchall()]
            if column not in cols:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {col_type}')

        def ensure_table(name, sql):
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'")
            if not cursor.fetchone():
                cursor.execute(sql)

        ensure_column('submitted_records', 'additional_info')
        ensure_column('users', 'added_by')
        ensure_column('submitted_records', 'repetitor_username')
        ensure_column('submitted_records', 'added_by_role')
        ensure_column('manager_records', 'additional_info')

        ensure_table('manager_repetitor_bindings', '''CREATE TABLE manager_repetitor_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL UNIQUE,
            repetitor_username TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )''')
        ensure_table('salary_rates', '''CREATE TABLE salary_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repetitor_username TEXT NOT NULL UNIQUE,
            rate_percent REAL NOT NULL DEFAULT 50.0,
            trial_lesson_rate REAL NOT NULL DEFAULT 500.0,
            set_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        ensure_table('salary_accruals', '''CREATE TABLE salary_accruals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repetitor_username TEXT NOT NULL,
            lesson_id INTEGER,
            lesson_type TEXT NOT NULL,
            lesson_amount REAL NOT NULL,
            accrual_amount REAL NOT NULL,
            accrued_at TEXT NOT NULL,
            paid INTEGER DEFAULT 0,
            paid_at TEXT
        )''')
        ensure_table('manager_responses', '''CREATE TABLE manager_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL,
            responses_sent INTEGER DEFAULT 0,
            client_replied INTEGER DEFAULT 0,
            client_enrolled INTEGER DEFAULT 0,
            report_date TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        )''')
        ensure_table('kosyaki', '''CREATE TABLE kosyaki (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_username TEXT NOT NULL,
            reporter_role TEXT NOT NULL,
            target_username TEXT NOT NULL,
            reason TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        )''')
        ensure_table('teacher_reports', '''CREATE TABLE teacher_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_username TEXT NOT NULL,
            teacher_username TEXT NOT NULL,
            period TEXT NOT NULL,
            report_text TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )''')

        # ===== extended features: columns =====
        ensure_column('users', 'max_students', 'INTEGER')
        ensure_column('salary_rates', 'rate_type', "TEXT DEFAULT 'percent'")
        ensure_column('salary_rates', 'fixed_amount', 'REAL DEFAULT 0')
        ensure_column('salary_accruals', 'report_id', 'INTEGER')
        ensure_column('manager_responses', 'is_late', 'INTEGER DEFAULT 0')

        # ===== extended features: tables =====
        ensure_table('settings', '''CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)''')
        ensure_table('student_info', '''CREATE TABLE student_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT NOT NULL,
            repetitor_username TEXT NOT NULL,
            subject TEXT, parent_name TEXT, contact TEXT, grade TEXT, timezone TEXT,
            price TEXT, notes TEXT, created_at TEXT,
            UNIQUE(student_name, repetitor_username)
        )''')
        ensure_table('lesson_reports', '''CREATE TABLE lesson_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_id INTEGER, repetitor_username TEXT NOT NULL, student_name TEXT,
            lesson_date TEXT, lesson_time TEXT,
            held INTEGER NOT NULL, reason TEXT, work_done TEXT, amount REAL DEFAULT 0,
            check_file TEXT, status TEXT DEFAULT 'pending',
            checked_by TEXT, checked_at TEXT, created_at TEXT NOT NULL
        )''')
        ensure_table('report_reviews', '''CREATE TABLE report_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL, reviewer TEXT NOT NULL,
            rating INTEGER, complaint TEXT, created_at TEXT NOT NULL,
            UNIQUE(report_id, reviewer)
        )''')
        ensure_table('salary_payouts', '''CREATE TABLE salary_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL, amount REAL NOT NULL, paid_by TEXT, paid_at TEXT NOT NULL, note TEXT
        )''')
        ensure_table('manager_salary_rates', '''CREATE TABLE manager_salary_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_username TEXT NOT NULL UNIQUE,
            per_response REAL DEFAULT 0, per_trial REAL DEFAULT 0, updated_at TEXT
        )''')
        ensure_table('lesson_changes', '''CREATE TABLE lesson_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repetitor_username TEXT NOT NULL, student_name TEXT, kind TEXT NOT NULL,
            old_date TEXT, old_time TEXT, new_date TEXT, new_time TEXT, reason TEXT, created_at TEXT NOT NULL
        )''')
        db.commit()



PUBLIC_ROUTES = {'login', 'logout', 'static', 'goodbye', 'welcome'}

PREFIX_ROLES = {
    '/director':         {'director', 'admin'},
    '/senior_manager':   {'senior_manager', 'director', 'admin'},
    '/manager':          {'manager'},
    '/repetitor':        {'repetitor', 'senior_repetitor'},
    '/senior_repetitor': {'senior_repetitor'},
}

@app.before_request
def check_auth():
    endpoint = request.endpoint
    path = request.path

    if endpoint in PUBLIC_ROUTES or endpoint == 'static':
        return

    if endpoint in (None,):
        return

    if path in ('/', '/goodbye'):
        return

    if 'username' not in session:
        return redirect(url_for('login'))

    role = session.get('role')

    for prefix, allowed in PREFIX_ROLES.items():
        if path.startswith(prefix) and role not in allowed:
            return redirect(url_for('login'))



@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ? AND password = ?', (username, password))
        user = cursor.fetchone()
        if user:
            session.permanent = True
            session['username'] = username
            session['role'] = user[3]
            routes = {
                'director': 'director',
                'repetitor': 'repetitor_main',
                'manager': 'manager',
                'senior_manager': 'senior_manager',
                'admin': 'admin',
                'senior_repetitor': 'senior_repetitor',
            }
            return redirect(url_for(routes.get(user[3], 'welcome')))
        else:
            return "Неверный логин или пароль!"
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/welcome')
def welcome():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('welcome.html')


@app.route('/goodbye')
def goodbye():
    return render_template('goodbye.html')
print(1)


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if 'delete' in request.form:
            cursor.execute("DELETE FROM users WHERE id=?", (request.form['delete'],))
            db.commit()
            flash("Пользователь удалён!")
        elif 'username' in request.form and 'password' in request.form:
            username = request.form['username']
            password = request.form['password']
            name = request.form['name']
            role = request.form['role']
            subject = None
            if role == 'repetitor':
                subject = request.form.get('subject')
                if not subject:
                    flash("Для репетитора необходимо указать предмет!")
                    return redirect(url_for('admin'))
            try:
                cursor.execute("INSERT INTO users (username, password, role, subject, name) VALUES (?, ?, ?, ?, ?)",
                               (username, password, role, subject, name))
                db.commit()
                flash("Пользователь добавлен!")
            except sqlite3.IntegrityError:
                flash("Пользователь с таким логином уже существует!")
    cursor.execute('SELECT id, username, role, subject, added_by FROM users WHERE username != "admin"')
    users = cursor.fetchall()
    return render_template("admin.html", users=users)


@app.route('/admin/students', methods=['GET', 'POST'])
def admin_students():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST' and 'clear' in request.form:
        cursor.execute('DELETE FROM submitted_records')
        db.commit()
        flash("Таблица учеников очищена!")
    cursor.execute('SELECT manager_username, student_name, subject, submitted_at, repetitor_username, additional_info, added_by_role FROM submitted_records')
    records = cursor.fetchall()
    return render_template('admin_students.html', records=records)



@app.route('/director')
def director():
    if 'username' not in session or session.get('role') not in ('director', 'admin'):
        return redirect(url_for('login'))
    return render_template('director.html')


@app.route('/director/add_user', methods=['GET', 'POST'])
@roles_required('director')
def director_add_user_db():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if 'delete' in request.form:
            user_id = request.form['delete']
            cursor.execute('SELECT username, role, added_by FROM users WHERE id=?', (user_id,))
            target = cursor.fetchone()
            if target:
                if target[2] != session['username']:
                    flash("Нельзя удалить пользователя, добавленного другим директором или администратором!")
                elif target[1] in ('admin',):
                    flash("Нельзя удалить администратора!")
                else:
                    cursor.execute('DELETE FROM users WHERE id=?', (user_id,))
                    db.commit()
                    flash("Пользователь удалён!")
            return redirect(url_for('director_add_user_db'))
        username = request.form['username']
        password = request.form['password']
        role = request.form['role']
        if role == 'director':
            flash("Директор не может создавать других директоров!")
            return redirect(url_for('director_add_user_db'))
        subject = None
        if role == 'repetitor':
            subject = request.form.get('subject')
            if not subject:
                flash("Для репетитора необходимо указать предмет!")
                return redirect(url_for('director_add_user_db'))
        try:
            name = request.form.get('name', username)
            cursor.execute('INSERT INTO users (username, password, role, subject, name, added_by) VALUES (?, ?, ?, ?, ?, ?)',
                           (username, password, role, subject, name, session['username']))
            db.commit()
            flash(f"Пользователь {username} успешно добавлен!")
        except sqlite3.IntegrityError:
            flash(f"Пользователь {username} уже существует!")
    cursor.execute('SELECT id, username, role, subject, added_by FROM users WHERE username != "admin"')
    users = cursor.fetchall()
    return render_template('director_add_user.html', users=users)


@app.route('/director/change_repetitor', methods=['GET', 'POST'])
@roles_required('director')
def director_change_unique():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        cursor.execute('UPDATE submitted_records SET repetitor_username=? WHERE id=?',
                       (request.form['new_repetitor'], request.form['student_id']))
        db.commit()
        flash("Репетитор изменён!")
    cursor.execute('SELECT id, student_name, repetitor_username FROM submitted_records WHERE repetitor_username IS NOT NULL')
    students = cursor.fetchall()
    cursor.execute('SELECT username FROM users WHERE role="repetitor" OR role="senior_repetitor"')
    repetitors = cursor.fetchall()
    return render_template('director_change.html', students=students, repetitors=repetitors)


@app.route('/director/tables')
@roles_required('director')
def director_tables():
    return render_template('director_tables.html')


@app.route('/director/tables/trial_lessons')
@roles_required('director')
def director_trial_lessons_table():
    db = get_db()
    cursor = db.cursor()
    filter_type = request.args.get('filter', 'all')
    query = '''SELECT tl.id, tl.student_name, tl.subject, tl.manager_username,
                      tl.repetitor_username, tl.scheduled_date, tl.scheduled_time,
                      tl.status, tl.result, tl.reason, tl.notes, tl.created_at
               FROM trial_lessons tl'''
    if filter_type == 'active':
        query += ' WHERE tl.status IN ("unassigned", "scheduled")'
    elif filter_type == 'completed':
        query += ' WHERE tl.status = "completed"'
    query += ' ORDER BY tl.created_at DESC'
    cursor.execute(query)
    trials = cursor.fetchall()
    cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE status IN ("unassigned", "scheduled")')
    active_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE status = "completed"')
    completed_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM trial_lessons')
    total_count = cursor.fetchone()[0]
    return render_template('director_trial_lessons_table.html', trials=trials, filter_type=filter_type,
                           active_count=active_count, completed_count=completed_count, total_count=total_count)


@app.route('/director/tables/trial_lessons/<int:trial_id>/details')
@roles_required('director')
def director_trial_lesson_details(trial_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''SELECT student_name, subject, manager_username, repetitor_username,
                             scheduled_date, scheduled_time, status, result, reason, notes,
                             created_at, updated_at
                      FROM trial_lessons WHERE id = ?''', (trial_id,))
    trial = cursor.fetchone()
    if not trial:
        return jsonify({'error': 'Trial lesson not found'}), 404
    return jsonify({
        'student_name': trial[0], 'subject': trial[1], 'manager': trial[2],
        'repetitor': trial[3] or 'Не назначен',
        'scheduled_date': trial[4] or 'Не назначено', 'scheduled_time': trial[5] or 'Не назначено',
        'status': trial[6],
        'status_text': {'unassigned': 'Не назначено', 'scheduled': 'Назначено', 'completed': 'Завершено'}.get(trial[6], trial[6]),
        'result': trial[7],
        'result_text': {'enrolled': 'Записался', 'refused': 'Не записался', 'thinking': 'Думает', 'absent': 'Не было'}.get(trial[7], trial[7] or 'Нет данных'),
        'reason': trial[8] or 'Не указана', 'notes': trial[9] or 'Нет заметок',
        'created_at': trial[10], 'updated_at': trial[11]
    })


@app.route('/director/new_record', methods=['GET', 'POST'])
@roles_required('director')
def director_new_record():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        student_name = request.form['student_name']
        subject = request.form['subject']
        additional_info = request.form.get('additional_info', '')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''INSERT INTO senior_manager_records (manager_username, student_name, subject, additional_info, role, created_at)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (session['username'], student_name, subject, additional_info, 'director', now))
        db.commit()
        flash("Запись успешно добавлена!")
        return redirect(url_for('director_records_table'))
    return render_template('director_new_record.html')


@app.route('/director/records', methods=['GET', 'POST'])
@roles_required('director')
def director_records_table():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if 'delete' in request.form:
            cursor.execute('DELETE FROM senior_manager_records WHERE id=? AND manager_username=?',
                           (request.form['delete'], session['username']))
            db.commit()
        elif 'clear' in request.form:
            cursor.execute('DELETE FROM senior_manager_records WHERE manager_username=?', (session['username'],))
            db.commit()
        elif 'send_director' in request.form:
            cursor.execute('SELECT student_name, subject, additional_info FROM senior_manager_records WHERE manager_username=? AND role="director"',
                           (session['username'],))
            records = cursor.fetchall()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in records:
                cursor.execute('''INSERT INTO submitted_records (manager_username, student_name, subject, submitted_at, additional_info, added_by_role)
                                  VALUES (?, ?, ?, ?, ?, ?)''',
                               (session['username'], r[0], r[1], now, r[2], 'director'))
                cursor.execute('''SELECT id FROM trial_lessons WHERE student_name=? AND subject=? AND manager_username=? AND status="unassigned" AND repetitor_username IS NULL''',
                               (r[0], r[1], session['username']))
                if not cursor.fetchone():
                    cursor.execute('''INSERT INTO trial_lessons (student_name, subject, manager_username, repetitor_username, status, created_at, updated_at)
                                      VALUES (?, ?, ?, NULL, "unassigned", ?, ?)''',
                                   (r[0], r[1], session['username'], now, now))
            cursor.execute('DELETE FROM senior_manager_records WHERE manager_username=? AND role="director"', (session['username'],))
            db.commit()
            flash("Данные успешно отправлены!")
            return redirect(url_for('director_records_table'))
    cursor.execute('SELECT id, student_name, subject, additional_info, created_at FROM senior_manager_records WHERE manager_username=? AND role="director"',
                   (session['username'],))
    records = cursor.fetchall()
    return render_template('director_records_table.html', records=records)


@app.route('/director/stats_menu')
@roles_required('director')
def director_stats_menu():
    return redirect(url_for('director_stats_my'))


@app.route('/director/stats/my')
@roles_required('director')
def director_stats_my():
    db = get_db()
    cursor = db.cursor()
    manager_username = session['username']
    cursor.execute('SELECT COALESCE(name, username), COALESCE(role, "") FROM users WHERE username=?', (manager_username,))
    row = cursor.fetchone()
    display_name = row[0] if row else manager_username
    role = row[1] if row else ''
    role_ru = {'director': 'директор', 'senior_manager': 'старший менеджер', 'manager': 'менеджер',
               'repetitor': 'репетитор', 'senior_repetitor': 'старший репетитор', 'admin': 'админ'}
    manager_title = f"{display_name} ({role_ru.get(role, role)})"
    cursor.execute('SELECT COUNT(*), COUNT(DISTINCT subject) FROM submitted_records WHERE manager_username = ?', (manager_username,))
    totals = cursor.fetchone()
    total_records = totals[0] if totals else 0
    subjects_count = totals[1] if totals else 0
    cursor.execute('SELECT subject, COUNT(*) FROM submitted_records WHERE manager_username = ? GROUP BY subject ORDER BY 2 DESC', (manager_username,))
    subject_stats = cursor.fetchall()
    cursor.execute('''SELECT COUNT(DISTINCT sr.student_name) FROM submitted_records sr
                      JOIN trial_lessons tl ON sr.student_name = tl.student_name AND sr.subject = tl.subject
                      WHERE sr.manager_username = ? AND tl.result = "enrolled"''', (manager_username,))
    enrolled_count = cursor.fetchone()[0]
    conversion_rate = (enrolled_count / total_records * 100) if total_records > 0 else 0
    detailed_stats = [{'manager': manager_title, 'total_records': total_records, 'subjects_count': subjects_count,
                       'subject_stats': subject_stats, 'enrolled_count': enrolled_count, 'conversion_rate': conversion_rate}]
    return render_template('senior_manager_stats.html', detailed_stats=detailed_stats, total_managers=1)


@app.route('/director/stats/all')
@roles_required('director')
def director_stats_all():
    return redirect(url_for('senior_manager_stats'))


@app.route('/director/assign', methods=['GET', 'POST'])
@roles_required('director')
def director_assign():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, student_name, subject FROM submitted_records WHERE repetitor_username IS NULL')
    students = cursor.fetchall()
    cursor.execute('SELECT username FROM users WHERE role="repetitor" OR role="senior_repetitor"')
    repetitors = cursor.fetchall()
    if request.method == 'POST':
        cursor.execute('UPDATE submitted_records SET repetitor_username=? WHERE id=?',
                       (request.form['repetitor'], request.form['student_id']))
        db.commit()
        flash("Репетитор назначен!")
        return redirect(url_for('director_assign'))
    return render_template('director_assign.html', students=students, repetitors=repetitors)


@app.route('/director/change', methods=['GET', 'POST'])
@roles_required('director')
def director_change():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, student_name, subject, repetitor_username FROM submitted_records WHERE repetitor_username IS NOT NULL')
    students = cursor.fetchall()
    cursor.execute('SELECT username FROM users WHERE role="repetitor" OR role="senior_repetitor"')
    repetitors = cursor.fetchall()
    if request.method == 'POST':
        cursor.execute('UPDATE submitted_records SET repetitor_username=? WHERE id=?',
                       (request.form['repetitor'], request.form['student_id']))
        db.commit()
        flash("Репетитор изменён!")
        return redirect(url_for('director_change'))
    return render_template('director_change.html', students=students, repetitors=repetitors)



@app.route('/senior_manager', methods=['GET', 'POST'])
@roles_required('senior_manager', 'director')
def senior_manager():
    is_director = (session.get('role') == 'director')
    db = get_db()
    cursor = db.cursor()

    if request.method == 'POST' and 'show_new' in request.form:
        cursor.execute('SELECT COUNT(*) FROM submitted_records WHERE viewed=0')
        new_count = cursor.fetchone()[0]
        cursor.execute('SELECT id, student_name, subject, manager_username, submitted_at FROM submitted_records WHERE viewed=0')
        records = cursor.fetchall()
        cursor.execute('UPDATE submitted_records SET viewed=1 WHERE viewed=0')
        db.commit()
        cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE status="unassigned"')
        unassigned_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM submitted_records WHERE viewed=0')
        new_count = cursor.fetchone()[0]
        return render_template('senior_manager.html', new_count=new_count, records=records,
                               unassigned_count=unassigned_count, is_main_panel=False, is_director=is_director)

    cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE status="unassigned"')
    unassigned_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM submitted_records WHERE viewed=0')
    new_count = cursor.fetchone()[0]
    return render_template('senior_manager.html', unassigned_count=unassigned_count, new_count=new_count,
                           is_main_panel=True, is_director=is_director)


@app.route('/senior_manager/table')
@roles_required('senior_manager', 'director')
def senior_manager_table():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, student_name, subject, manager_username, repetitor_username, submitted_at, additional_info, added_by_role FROM submitted_records ORDER BY submitted_at DESC')
    records = cursor.fetchall()
    return render_template('senior_manager_all_records.html', records=records)





@app.route('/senior_manager/stats')
@roles_required('senior_manager', 'director')
def senior_manager_stats():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''SELECT sr.manager_username, COALESCE(u.name, sr.manager_username), COALESCE(u.role, ''),
                             COUNT(*), COUNT(DISTINCT sr.subject)
                      FROM submitted_records sr LEFT JOIN users u ON u.username = sr.manager_username
                      GROUP BY sr.manager_username ORDER BY 4 DESC''')
    managers_stats = cursor.fetchall()
    role_ru = {'director': 'директор', 'senior_manager': 'старший менеджер', 'manager': 'менеджер',
               'repetitor': 'репетитор', 'senior_repetitor': 'старший репетитор', 'admin': 'админ'}
    detailed_stats = []
    for manager in managers_stats:
        manager_username, display_name, role, total_records, subjects_count = manager
        cursor.execute('SELECT subject, COUNT(*) FROM submitted_records WHERE manager_username = ? GROUP BY subject ORDER BY 2 DESC', (manager_username,))
        subject_stats = cursor.fetchall()
        cursor.execute('''SELECT COUNT(DISTINCT sr.student_name) FROM submitted_records sr
                          JOIN trial_lessons tl ON sr.student_name = tl.student_name AND sr.subject = tl.subject
                          WHERE sr.manager_username = ? AND tl.result = "enrolled"''', (manager_username,))
        enrolled_count = cursor.fetchone()[0]
        conversion_rate = (enrolled_count / total_records * 100) if total_records > 0 else 0
        cursor.execute('SELECT repetitor_username FROM manager_repetitor_bindings WHERE manager_username=?', (manager_username,))
        binding = cursor.fetchone()
        bound_rep = None
        if binding:
            cursor.execute('SELECT name FROM users WHERE username=?', (binding[0],))
            r = cursor.fetchone()
            bound_rep = r[0] if r else binding[0]
        detailed_stats.append({'manager': f"{display_name} ({role_ru.get(role, role)})",
                                'total_records': total_records, 'subjects_count': subjects_count,
                                'subject_stats': subject_stats, 'enrolled_count': enrolled_count,
                                'conversion_rate': conversion_rate, 'bound_repetitor': bound_rep})
    return render_template('senior_manager_stats.html', detailed_stats=detailed_stats, total_managers=len(detailed_stats))


@app.route('/senior_manager/stats/my')
@roles_required('senior_manager', 'director')
def senior_manager_stats_my():
    db = get_db()
    cursor = db.cursor()
    manager_username = session['username']
    cursor.execute('SELECT COALESCE(name, username), COALESCE(role, "") FROM users WHERE username=?', (manager_username,))
    row = cursor.fetchone()
    display_name = row[0] if row else manager_username
    role = row[1] if row else ''
    role_ru = {'director': 'директор', 'senior_manager': 'старший менеджер', 'manager': 'менеджер',
               'repetitor': 'репетитор', 'senior_repetitor': 'старший репетитор', 'admin': 'админ'}
    cursor.execute('SELECT COUNT(*), COUNT(DISTINCT subject) FROM submitted_records WHERE manager_username = ?', (manager_username,))
    totals = cursor.fetchone()
    total_records, subjects_count = (totals[0], totals[1]) if totals else (0, 0)
    cursor.execute('SELECT subject, COUNT(*) FROM submitted_records WHERE manager_username = ? GROUP BY subject ORDER BY 2 DESC', (manager_username,))
    subject_stats = cursor.fetchall()
    cursor.execute('''SELECT COUNT(DISTINCT sr.student_name) FROM submitted_records sr
                      JOIN trial_lessons tl ON sr.student_name = tl.student_name AND sr.subject = tl.subject
                      WHERE sr.manager_username = ? AND tl.result = "enrolled"''', (manager_username,))
    enrolled_count = cursor.fetchone()[0]
    conversion_rate = (enrolled_count / total_records * 100) if total_records > 0 else 0
    cursor.execute('SELECT repetitor_username FROM manager_repetitor_bindings WHERE manager_username=?', (manager_username,))
    binding = cursor.fetchone()
    bound_rep = None
    if binding:
        cursor.execute('SELECT name FROM users WHERE username=?', (binding[0],))
        r = cursor.fetchone()
        bound_rep = r[0] if r else binding[0]
    detailed_stats = [{'manager': f"{display_name} ({role_ru.get(role, role)})",
                       'total_records': total_records, 'subjects_count': subjects_count,
                       'subject_stats': subject_stats, 'enrolled_count': enrolled_count,
                       'conversion_rate': conversion_rate, 'bound_repetitor': bound_rep}]
    return render_template('senior_manager_stats.html', detailed_stats=detailed_stats, total_managers=1)


@app.route('/senior_manager/stats_menu')
@roles_required('senior_manager', 'director')
def senior_manager_stats_menu():
    return redirect(url_for('senior_manager_stats_my'))


@app.route('/senior_manager/trial_lessons')
@roles_required('senior_manager', 'director')
def senior_manager_trial_lessons():
    db = get_db()
    cursor = db.cursor()
    is_director = (session.get('role') == 'director')
    cursor.execute('''SELECT tl.id, tl.student_name, tl.subject, tl.manager_username,
                             tl.scheduled_date, tl.scheduled_time, tl.created_at
                      FROM trial_lessons tl WHERE tl.repetitor_username IS NULL ORDER BY tl.created_at''')
    unassigned_trials = cursor.fetchall()
    cursor.execute('''SELECT tl.id, tl.student_name, tl.subject, tl.manager_username,
                             tl.repetitor_username, tl.scheduled_date, tl.scheduled_time, tl.created_at
                      FROM trial_lessons tl WHERE tl.repetitor_username IS NOT NULL AND tl.status = "scheduled"
                      ORDER BY tl.scheduled_date, tl.scheduled_time''')
    scheduled_trials = cursor.fetchall()
    day_filter = request.args.get('day', '')
    rep_filter = request.args.get('rep', '')
    if day_filter.isdigit():
        scheduled_trials = [t for t in scheduled_trials if t[5] and _weekday_of(t[5]) == int(day_filter)]
    if rep_filter:
        scheduled_trials = [t for t in scheduled_trials if t[4] == rep_filter]
    cursor.execute('SELECT username, subject FROM users WHERE role IN ("repetitor", "senior_repetitor")')
    repetitors = []
    for username, subject in cursor.fetchall():
        count, limit = repetitor_load(cursor, username)
        repetitors.append((username, subject, count, limit))
    return render_template('senior_manager_trial_lessons.html', unassigned_trials=unassigned_trials,
                           scheduled_trials=scheduled_trials, repetitors=repetitors, is_director=is_director,
                           day_filter=day_filter, rep_filter=rep_filter, weekdays=WEEKDAYS)


@app.route('/senior_manager/trial_lessons/reassign/<int:trial_id>', methods=['POST'])
@roles_required('senior_manager', 'director')
def senior_manager_reassign_trial(trial_id):
    new_repetitor = request.form.get('new_repetitor')
    if new_repetitor:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT student_name, subject FROM trial_lessons WHERE id = ?', (trial_id,))
        trial = cursor.fetchone()
        count, limit = repetitor_load(cursor, new_repetitor)
        if trial and limit and count >= limit:
            flash(f"У репетитора {new_repetitor} уже максимум учеников ({limit}).")
            trial = None
        if trial:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE trial_lessons SET repetitor_username = ?, updated_at = ? WHERE id = ?', (new_repetitor, now, trial_id))
            cursor.execute('''UPDATE submitted_records SET repetitor_username = ?
                              WHERE id = (SELECT id FROM submitted_records WHERE student_name = ? AND subject = ? ORDER BY submitted_at DESC LIMIT 1)''',
                           (new_repetitor, trial[0], trial[1]))
            db.commit()
            flash(f"Пробное занятие переназначено репетитору {new_repetitor}!")
    return redirect(url_for('senior_manager_trial_lessons'))


@app.route('/senior_manager/assign_repetitor', methods=['GET', 'POST'])
@roles_required('senior_manager', 'director')
def senior_manager_assign_repetitor():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST' and request.form.get('repetitor_username'):
        student_id = request.form['student_id']
        repetitor_username = request.form['repetitor_username']
        cursor.execute('SELECT student_name, subject, manager_username FROM submitted_records WHERE id=?', (student_id,))
        student = cursor.fetchone()
        count, limit = repetitor_load(cursor, repetitor_username)
        if student and limit and count >= limit:
            flash(f"У репетитора {repetitor_username} уже максимум учеников ({limit}).")
            student = None
        if student:
            cursor.execute('UPDATE submitted_records SET repetitor_username=? WHERE id=?', (repetitor_username, student_id))
            cursor.execute('SELECT id FROM trial_lessons WHERE student_name=? AND subject=? AND repetitor_username=?',
                           (student[0], student[1], repetitor_username))
            if not cursor.fetchone():
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('''INSERT INTO trial_lessons (student_name, subject, manager_username, repetitor_username, status, created_at, updated_at)
                                  VALUES (?, ?, ?, ?, "unassigned", ?, ?)''',
                               (student[0], student[1], student[2], repetitor_username, now, now))
            db.commit()
            flash(f"Репетитор {repetitor_username} назначен ученику!")
        return redirect(url_for('senior_manager_assign_repetitor'))
    cursor.execute('SELECT id, student_name, subject FROM submitted_records WHERE repetitor_username IS NULL')
    students = cursor.fetchall()
    student_id = request.args.get('student_id')
    selected_student = None
    repetitors = []
    if student_id:
        cursor.execute('SELECT id, student_name, subject FROM submitted_records WHERE id = ?', (student_id,))
        selected_student = cursor.fetchone()
        if selected_student:
            cursor.execute('SELECT username, name FROM users WHERE role IN ("repetitor", "senior_repetitor") AND subject = ?', (selected_student[2],))
            found = cursor.fetchall()
            if not found:
                cursor.execute('SELECT username, name FROM users WHERE role IN ("repetitor", "senior_repetitor")')
                found = cursor.fetchall()
                flash(f"Нет репетиторов по предмету '{selected_student[2]}'. Показаны все репетиторы.")
            bound = bound_repetitors(cursor, selected_student[1])
            query = request.args.get('q', '').strip().lower()
            for username, name in found:
                if query and query not in (username or '').lower() and query not in (name or '').lower():
                    continue
                count, limit = repetitor_load(cursor, username)
                repetitors.append({'username': username, 'name': name or username, 'count': count, 'limit': limit,
                                   'full': bool(limit and count >= limit), 'bound': username in bound})
            sort = request.args.get('sort', 'count')
            repetitors.sort(key=lambda r: (not r['bound'], r['count'] if sort == 'count' else r['name'].lower()))
    return render_template('senior_manager_assign.html', students=students, selected_student=selected_student, repetitors=repetitors,
                           q=request.args.get('q', ''), sort=request.args.get('sort', 'count'))


@app.route('/senior_manager/new_record', methods=['GET', 'POST'])
@roles_required('senior_manager', 'director')
def senior_manager_new_record():
    if request.method == 'POST':
        student_name = request.form['student_name']
        subject = request.form['subject']
        additional_info = request.form.get('additional_info', '')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''INSERT INTO senior_manager_records (manager_username, student_name, subject, additional_info, role, created_at)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (session['username'], student_name, subject, additional_info, session['role'], now))
        db.commit()
        flash("Запись добавлена!")
        return redirect(url_for('senior_manager_temp_table'))
    return render_template('manager_new.html')


@app.route('/senior_manager/temp_table', methods=['GET', 'POST'])
@roles_required('senior_manager', 'director')
def senior_manager_temp_table():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if 'delete' in request.form:
            cursor.execute('DELETE FROM senior_manager_records WHERE id=? AND manager_username=? AND role="senior_manager"',
                           (request.form['delete'], session['username']))
            db.commit()
            return redirect(url_for('senior_manager_temp_table'))
        if 'clear' in request.form:
            cursor.execute('DELETE FROM senior_manager_records WHERE manager_username=? AND role="senior_manager"', (session['username'],))
            db.commit()
            return redirect(url_for('senior_manager_temp_table'))
        if 'send' in request.form:
            cursor.execute('SELECT student_name, subject, additional_info FROM senior_manager_records WHERE manager_username=? AND role="senior_manager"',
                           (session['username'],))
            records_to_send = cursor.fetchall()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in records_to_send:
                cursor.execute('''INSERT INTO submitted_records (manager_username, student_name, subject, submitted_at, additional_info, added_by_role)
                                  VALUES (?, ?, ?, ?, ?, ?)''',
                               (session['username'], r[0], r[1], now, r[2], 'senior_manager'))
            cursor.execute('DELETE FROM senior_manager_records WHERE manager_username=? AND role="senior_manager"', (session['username'],))
            db.commit()
            flash("Данные успешно отправлены!")
            return redirect(url_for('senior_manager_temp_table'))
    cursor.execute('SELECT id, student_name, subject, additional_info, created_at FROM senior_manager_records WHERE manager_username=? AND role="senior_manager" ORDER BY created_at DESC',
                   (session['username'],))
    records = cursor.fetchall()
    return render_template('senior_manager_temp_table.html', records=records)


@app.route('/senior_manager/mark_viewed', methods=['POST'])
@roles_required('senior_manager', 'director')
def senior_manager_mark_viewed():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('UPDATE submitted_records SET viewed=1 WHERE id=?', (request.form['record_id'],))
    db.commit()
    return redirect(url_for('senior_manager'))


@app.route('/senior_manager/manage_repetitors', methods=['GET', 'POST'])
@roles_required('senior_manager', 'director')
def manage_repetitors():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT username, name, subject FROM users WHERE role IN ("repetitor", "senior_repetitor") ORDER BY name')
    repetitors = cursor.fetchall()
    cursor.execute('''SELECT b.manager_username, b.repetitor_username, u.name FROM manager_repetitor_bindings b
                      LEFT JOIN users u ON u.username = b.manager_username''')
    bindings = {row[1]: {'manager_username': row[0], 'manager_name': row[2] or row[0]} for row in cursor.fetchall()}
    cursor.execute('SELECT username, name FROM users WHERE role = "manager" AND username NOT IN (SELECT manager_username FROM manager_repetitor_bindings) ORDER BY name')
    free_managers = cursor.fetchall()
    return render_template('manage_repetitors.html', repetitors=repetitors, bindings=bindings, free_managers=free_managers)


@app.route('/senior_manager/attach_manager', methods=['POST'])
@roles_required('senior_manager', 'director')
def attach_manager():
    repetitor_username = request.form.get('repetitor_username')
    manager_username = request.form.get('manager_username')
    if not repetitor_username or not manager_username:
        flash("Не указаны репетитор или менеджер")
        return redirect(url_for('manage_repetitors'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM manager_repetitor_bindings WHERE repetitor_username=?', (repetitor_username,))
    if cursor.fetchone():
        flash(f"Репетитор {repetitor_username} уже привязан")
        return redirect(url_for('manage_repetitors'))
    cursor.execute('SELECT * FROM manager_repetitor_bindings WHERE manager_username=?', (manager_username,))
    if cursor.fetchone():
        flash(f"Менеджер {manager_username} уже привязан")
        return redirect(url_for('manage_repetitors'))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('INSERT INTO manager_repetitor_bindings (manager_username, repetitor_username, created_at) VALUES (?, ?, ?)',
                   (manager_username, repetitor_username, now))
    db.commit()
    flash(f"Менеджер {manager_username} привязан к репетитору {repetitor_username}")
    return redirect(url_for('manage_repetitors'))


@app.route('/senior_manager/detach_manager', methods=['POST'])
@roles_required('senior_manager', 'director')
def detach_manager():
    repetitor_username = request.form.get('repetitor_username')
    if not repetitor_username:
        flash("Не указан репетитор")
        return redirect(url_for('manage_repetitors'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM manager_repetitor_bindings WHERE repetitor_username=?', (repetitor_username,))
    db.commit()
    flash(f"Привязка репетитора {repetitor_username} удалена")
    return redirect(url_for('manage_repetitors'))



@app.route('/manager')
@roles_required('manager')
def manager():
    return render_template('manager.html')


@app.route('/manager/new', methods=['GET', 'POST'])
@roles_required('manager')
def manager_new():
    if request.method == 'POST':
        student_name = request.form['student_name']
        subject = request.form['subject']
        additional_info = request.form.get('additional_info', '')
        db = get_db()
        cursor = db.cursor()
        cursor.execute('INSERT INTO manager_records (manager_username, student_name, subject, additional_info) VALUES (?, ?, ?, ?)',
                       (session['username'], student_name, subject, additional_info))
        db.commit()
        flash("Запись успешно добавлена!")
        return redirect(url_for('manager_table'))
    return render_template('manager_new.html')


@app.route('/manager/table', methods=['GET', 'POST'])
@roles_required('manager')
def manager_table():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if 'delete' in request.form:
            cursor.execute('DELETE FROM manager_records WHERE id=? AND manager_username=?',
                           (request.form['delete'], session['username']))
            db.commit()
        elif 'clear' in request.form:
            cursor.execute('DELETE FROM manager_records WHERE manager_username=?', (session['username'],))
            db.commit()
        elif 'send' in request.form:
            cursor.execute('SELECT student_name, subject, additional_info FROM manager_records WHERE manager_username=?',
                           (session['username'],))
            records = cursor.fetchall()
            cursor.execute('SELECT repetitor_username FROM manager_repetitor_bindings WHERE manager_username=?', (session['username'],))
            binding = cursor.fetchone()
            bound_repetitor = binding[0] if binding else None
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in records:
                cursor.execute('''INSERT INTO submitted_records (manager_username, student_name, subject, submitted_at, additional_info, added_by_role, repetitor_username)
                                  VALUES (?, ?, ?, ?, ?, ?, ?)''',
                               (session['username'], r[0], r[1], now, r[2], 'manager', bound_repetitor))
                cursor.execute('SELECT id FROM trial_lessons WHERE student_name=? AND subject=? AND manager_username=?',
                               (r[0], r[1], session['username']))
                existing = cursor.fetchone()
                if existing:
                    cursor.execute('UPDATE trial_lessons SET repetitor_username=?, status=?, updated_at=? WHERE id=?',
                                   (bound_repetitor, 'unassigned', now, existing[0]))
                else:
                    cursor.execute('''INSERT INTO trial_lessons (student_name, subject, manager_username, repetitor_username, status, created_at, updated_at)
                                      VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                   (r[0], r[1], session['username'], bound_repetitor, 'unassigned', now, now))
            cursor.execute('DELETE FROM manager_records WHERE manager_username=?', (session['username'],))
            db.commit()
            flash("Данные успешно отправлены!")
    cursor.execute('SELECT id, student_name, subject, additional_info FROM manager_records WHERE manager_username=?',
                   (session['username'],))
    records = cursor.fetchall()
    return render_template('manager_table.html', records=records)


@app.route('/manager/stats')
@roles_required('manager')
def manager_stats():
    db = get_db()
    cursor = db.cursor()
    username = session['username']
    cursor.execute('SELECT COUNT(*) FROM submitted_records WHERE manager_username = ?', (username,))
    total_sent = cursor.fetchone()[0]
    cursor.execute('SELECT subject, COUNT(*) FROM submitted_records WHERE manager_username = ? GROUP BY subject ORDER BY 2 DESC', (username,))
    subjects_stats = cursor.fetchall()
    conversion_data = []
    for subj, count in subjects_stats:
        cursor.execute('''SELECT COUNT(DISTINCT tl.student_name) FROM trial_lessons tl
                          JOIN submitted_records sr ON tl.student_name = sr.student_name AND tl.subject = sr.subject
                          WHERE sr.manager_username = ? AND sr.subject = ? AND tl.result = "enrolled"''', (username, subj))
        enrolled = cursor.fetchone()[0]
        conversion_data.append({'subject': subj, 'total_students': count, 'enrolled_students': enrolled,
                                 'conversion_rate': (enrolled / count * 100) if count > 0 else 0})
    total_enrolled = sum(d['enrolled_students'] for d in conversion_data)
    total_conversion_rate = (total_enrolled / total_sent * 100) if total_sent > 0 else 0

    cursor.execute('SELECT id, responses_sent, client_replied, client_enrolled, report_date, notes FROM manager_responses WHERE manager_username = ? ORDER BY report_date DESC', (username,))
    responses_history = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(responses_sent),0), COALESCE(SUM(client_replied),0), COALESCE(SUM(client_enrolled),0) FROM manager_responses WHERE manager_username = ?', (username,))
    resp_totals = cursor.fetchone()

    periods = response_periods(cursor, username)
    return render_template('manager_stats.html', periods=periods,
                           total_sent=total_sent, subjects_stats=subjects_stats,
                           conversion_data=conversion_data, total_conversion_rate=total_conversion_rate,
                           responses_history=responses_history, resp_totals=resp_totals)


@app.route('/manager/responses', methods=['GET', 'POST'])
@roles_required('manager')
def manager_responses():
    db = get_db()
    cursor = db.cursor()
    username = session['username']
    if request.method == 'POST':
        if 'delete' in request.form:
            cursor.execute('DELETE FROM manager_responses WHERE id=? AND manager_username=?',
                           (request.form['delete'], username))
            db.commit()
            flash("Запись удалена!")
        else:
            responses_sent = int(request.form.get('responses_sent', 0))
            client_replied = int(request.form.get('client_replied', 0))
            client_enrolled = int(request.form.get('client_enrolled', 0))
            report_date = request.form.get('report_date') or msk_today()
            notes = request.form.get('notes', '')
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''INSERT INTO manager_responses (manager_username, responses_sent, client_replied, client_enrolled, report_date, notes, created_at, is_late)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                           (username, responses_sent, client_replied, client_enrolled, report_date, notes, now,
                            1 if report_date < msk_today() else 0))
            db.commit()
            flash("Отчёт об откликах сохранён!")
        return redirect(url_for('manager_responses'))
    cursor.execute('SELECT id, responses_sent, client_replied, client_enrolled, report_date, notes, is_late FROM manager_responses WHERE manager_username = ? ORDER BY report_date DESC', (username,))
    history = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(responses_sent),0), COALESCE(SUM(client_replied),0), COALESCE(SUM(client_enrolled),0) FROM manager_responses WHERE manager_username = ?', (username,))
    totals = cursor.fetchone()
    cursor.execute('SELECT COUNT(*) FROM manager_responses WHERE manager_username=? AND report_date=?', (username, msk_today()))
    return render_template('manager_responses.html', history=history, totals=totals, today=msk_today(),
                           today_done=cursor.fetchone()[0] > 0, periods=response_periods(cursor, username))



@app.route('/senior_repetitor')
@roles_required('senior_repetitor')
def senior_repetitor():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT student_name, subject, repetitor_username, manager_username, submitted_at FROM submitted_records')
    records = cursor.fetchall()
    return render_template('senior_repetitor.html', records=records,
                           pending_reports=len(pending_report_lessons(cursor, session['username'])))


@app.route('/senior_repetitor/kosyaki', methods=['GET', 'POST'])
@roles_required('senior_repetitor')
def senior_repetitor_kosyaki():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        target = request.form['target_username']
        reason = request.form['reason']
        description = request.form['description']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''INSERT INTO kosyaki (reporter_username, reporter_role, target_username, reason, description, created_at)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (session['username'], 'senior_repetitor', target, reason, description, now))
        db.commit()
        flash(f"Косяк зафиксирован для {target}!")
        return redirect(url_for('senior_repetitor_kosyaki'))
    cursor.execute('SELECT username, name FROM users WHERE role IN ("repetitor") ORDER BY name')
    repetitors = cursor.fetchall()
    cursor.execute('SELECT k.id, k.target_username, u.name, k.reason, k.description, k.created_at FROM kosyaki k LEFT JOIN users u ON u.username = k.target_username WHERE k.reporter_role = "senior_repetitor" ORDER BY k.created_at DESC')
    kosyaki_list = cursor.fetchall()
    return render_template('senior_repetitor_kosyaki.html', repetitors=repetitors, kosyaki_list=kosyaki_list)


@app.route('/senior_repetitor/teacher_report', methods=['GET', 'POST'])
@roles_required('senior_repetitor')
def senior_repetitor_teacher_report():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        teacher = request.form['teacher_username']
        period = request.form['period']
        report_text = request.form['report_text']
        rating = int(request.form['rating'])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''INSERT INTO teacher_reports (reporter_username, teacher_username, period, report_text, rating, created_at)
                          VALUES (?, ?, ?, ?, ?, ?)''',
                       (session['username'], teacher, period, report_text, rating, now))
        db.commit()
        flash("Отчёт сохранён!")
        return redirect(url_for('senior_repetitor_teacher_report'))
    cursor.execute('SELECT username, name FROM users WHERE role = "repetitor" ORDER BY name')
    repetitors = cursor.fetchall()
    cursor.execute('''SELECT tr.id, u.name, tr.period, tr.report_text, tr.rating, tr.created_at
                      FROM teacher_reports tr LEFT JOIN users u ON u.username = tr.teacher_username
                      WHERE tr.reporter_username = ? ORDER BY tr.created_at DESC''', (session['username'],))
    reports = cursor.fetchall()
    return render_template('senior_repetitor_teacher_report.html', repetitors=repetitors, reports=reports)


@app.route('/senior_repetitor/stats')
@roles_required('senior_repetitor')
def senior_repetitor_stats():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''SELECT u.username, u.name, u.subject,
                             COUNT(DISTINCT sr.student_name) as student_count,
                             COUNT(rl.id) as lesson_count
                      FROM users u
                      LEFT JOIN submitted_records sr ON sr.repetitor_username = u.username
                      LEFT JOIN regular_lessons rl ON rl.repetitor_username = u.username AND rl.status = "scheduled"
                      WHERE u.role = "repetitor"
                      GROUP BY u.username ORDER BY u.name''')
    stats = cursor.fetchall()
    cursor.execute('''SELECT teacher_username, AVG(rating) as avg_rating, COUNT(*) as report_count
                      FROM teacher_reports GROUP BY teacher_username''')
    ratings = {row[0]: {'avg': round(row[1], 1), 'count': row[2]} for row in cursor.fetchall()}
    cursor.execute('''SELECT target_username, COUNT(*) as kosyak_count
                      FROM kosyaki GROUP BY target_username''')
    kosyaki = {row[0]: row[1] for row in cursor.fetchall()}
    return render_template('senior_repetitor_stats.html', stats=stats, ratings=ratings, kosyaki=kosyaki)



@app.route('/repetitor')
def repetitor_main():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    now = datetime.now()
    cursor.execute('SELECT id, scheduled_date, scheduled_time FROM trial_lessons WHERE repetitor_username=? AND status="scheduled"', (session['username'],))
    for trial in cursor.fetchall():
        if trial[1] and trial[2]:
            try:
                if now > datetime.strptime(f"{trial[1]} {trial[2]}", "%Y-%m-%d %H:%M") + timedelta(hours=1):
                    cursor.execute('UPDATE trial_lessons SET status="completed", result=NULL, reason=NULL, updated_at=? WHERE id=? AND repetitor_username=?',
                                   (now.strftime("%Y-%m-%d %H:%M:%S"), trial[0], session['username']))
            except ValueError:
                pass
    db.commit()
    cursor.execute('SELECT student_name, subject, manager_username, submitted_at FROM submitted_records WHERE repetitor_username=?', (session['username'],))
    students = cursor.fetchall()
    cursor.execute('SELECT student_name, lesson_date, lesson_time FROM regular_lessons WHERE repetitor_username=? AND status="scheduled" ORDER BY lesson_date, lesson_time', (session['username'],))
    lessons = cursor.fetchall()
    cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE repetitor_username=? AND status="unassigned"', (session['username'],))
    trial_count = cursor.fetchone()[0]
    return render_template('repetitor.html', students=students, lessons=lessons, trial_count=trial_count,
                           pending_reports=len(pending_report_lessons(cursor, session['username'])))


@app.route('/repetitor/dashboard')
def repetitor_dashboard():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT student_name, subject, manager_username, submitted_at FROM submitted_records WHERE repetitor_username=?', (session['username'],))
    students = cursor.fetchall()
    return render_template('repetitor_dashboard.html', students=students)


@app.route('/repetitor/trial_lessons')
def repetitor_trial_lessons():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    now = datetime.now()
    cursor.execute('SELECT id, scheduled_date, scheduled_time FROM trial_lessons WHERE repetitor_username=? AND status="scheduled"', (session['username'],))
    for trial in cursor.fetchall():
        if trial[1] and trial[2]:
            try:
                if now > datetime.strptime(f"{trial[1]} {trial[2]}", "%Y-%m-%d %H:%M") + timedelta(hours=1):
                    cursor.execute('UPDATE trial_lessons SET status="completed", result=NULL, reason=NULL, updated_at=? WHERE id=? AND repetitor_username=?',
                                   (now.strftime("%Y-%m-%d %H:%M:%S"), trial[0], session['username']))
            except ValueError:
                pass
    db.commit()
    cursor.execute('''SELECT id, student_name, subject, scheduled_date, scheduled_time, status, result, reason, notes
                      FROM trial_lessons WHERE repetitor_username=?
                      ORDER BY CASE status WHEN "unassigned" THEN 1 WHEN "scheduled" THEN 2 WHEN "completed" THEN 3 END,
                               scheduled_date, scheduled_time''', (session['username'],))
    trials = cursor.fetchall()
    unassigned = [t for t in trials if t[5] == 'unassigned']
    scheduled = [t for t in trials if t[5] == 'scheduled']
    completed = [t for t in trials if t[5] == 'completed']
    return render_template('repetitor_trial_lessons.html', unassigned=unassigned, scheduled=scheduled, completed=completed)


@app.route('/repetitor/trial_lessons/schedule/<int:trial_id>', methods=['GET', 'POST'])
def schedule_trial_lesson(trial_id):
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('UPDATE trial_lessons SET scheduled_date=?, scheduled_time=?, status="scheduled", updated_at=? WHERE id=? AND repetitor_username=?',
                       (request.form['scheduled_date'], request.form['scheduled_time'], now, trial_id, session['username']))
        db.commit()
        flash("Дата и время пробного занятия назначены!")
        return redirect(url_for('repetitor_trial_lessons'))
    cursor.execute('SELECT student_name, subject FROM trial_lessons WHERE id=? AND repetitor_username=?', (trial_id, session['username']))
    trial = cursor.fetchone()
    return render_template('schedule_trial.html', trial=trial, trial_id=trial_id)


@app.route('/repetitor/trial_lessons/result/<int:trial_id>', methods=['GET', 'POST'])
def trial_result(trial_id):
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        attendance = request.form['attendance']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if attendance == 'present':
            result = request.form['result']
            if result == 'enrolled':
                schedule_option = request.form.get('schedule_option')
                if schedule_option == 'now':
                    lesson_date = request.form.get('lesson_date')
                    lesson_time = request.form.get('lesson_time')
                    if lesson_date and lesson_time:
                        cursor.execute('''INSERT INTO regular_lessons (student_name, repetitor_username, subject, lesson_date, lesson_time, lesson_type, status, created_at, updated_at)
                                          VALUES (?, ?, ?, ?, ?, "regular", "scheduled", ?, ?)''',
                                       (request.form['student_name'], session['username'], request.form['subject'], lesson_date, lesson_time, now, now))
                cursor.execute('UPDATE submitted_records SET lesson_type="regular" WHERE student_name=? AND subject=? AND repetitor_username=?',
                               (request.form['student_name'], request.form['subject'], session['username']))
                cursor.execute('UPDATE trial_lessons SET status="completed", result=?, notes=?, updated_at=? WHERE id=? AND repetitor_username=?',
                               (result, request.form.get('notes', ''), now, trial_id, session['username']))
            elif result == 'refused':
                cursor.execute('UPDATE trial_lessons SET status="completed", result=?, reason=?, notes=?, updated_at=? WHERE id=? AND repetitor_username=?',
                               (result, request.form.get('refusal_reason', ''), request.form.get('notes', ''), now, trial_id, session['username']))
            elif result == 'thinking':
                cursor.execute('UPDATE trial_lessons SET status="scheduled", result=?, notes=?, updated_at=? WHERE id=? AND repetitor_username=?',
                               (result, request.form.get('notes', ''), now, trial_id, session['username']))
        else:
            cursor.execute('UPDATE trial_lessons SET status="completed", result="absent", reason=?, updated_at=? WHERE id=? AND repetitor_username=?',
                           (request.form.get('absence_reason', ''), now, trial_id, session['username']))
        db.commit()
        flash("Результат пробного занятия сохранен!")
        return redirect(url_for('repetitor_trial_lessons'))
    cursor.execute('SELECT student_name, subject, scheduled_date, scheduled_time FROM trial_lessons WHERE id=? AND repetitor_username=?', (trial_id, session['username']))
    trial = cursor.fetchone()
    return render_template('trial_result.html', trial=trial, trial_id=trial_id)


@app.route('/repetitor/trial_lessons/reschedule/<int:trial_id>', methods=['GET', 'POST'])
def reschedule_trial_lesson(trial_id):
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('UPDATE trial_lessons SET scheduled_date=?, scheduled_time=?, updated_at=? WHERE id=? AND repetitor_username=?',
                       (request.form['scheduled_date'], request.form['scheduled_time'], now, trial_id, session['username']))
        db.commit()
        flash("Дата пробного занятия изменена!")
        return redirect(url_for('repetitor_trial_lessons'))
    cursor.execute('SELECT student_name, subject, scheduled_date, scheduled_time FROM trial_lessons WHERE id=? AND repetitor_username=?', (trial_id, session['username']))
    trial = cursor.fetchone()
    return render_template('reschedule_trial.html', trial=trial, trial_id=trial_id)


@app.route('/api/trial_lessons/count')
def api_trial_lessons_count():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return jsonify({'count': 0})
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT COUNT(*) FROM trial_lessons WHERE repetitor_username=? AND status="unassigned"', (session['username'],))
    return jsonify({'count': cursor.fetchone()[0]})


@app.route('/repetitor/schedule_lesson', methods=['GET', 'POST'])
def schedule_lesson():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        student_name = request.form['student_name']
        lesson_date = request.form['lesson_date']
        lesson_time = request.form['lesson_time']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lesson_date_obj = datetime.strptime(lesson_date, '%Y-%m-%d').date()
        if lesson_date_obj < datetime(2025, 1, 1).date():
            flash("Дата не может быть раньше 2025 года!")
            return redirect(url_for('schedule_lesson'))
        if lesson_date_obj > (datetime.now() + timedelta(days=365)).date():
            flash("Дата не может быть более чем на год вперёд!")
            return redirect(url_for('schedule_lesson'))
        cursor.execute('''INSERT INTO regular_lessons (student_name, repetitor_username, subject, lesson_date, lesson_time, lesson_type, status, created_at, updated_at)
                          VALUES (?, ?, ?, ?, ?, "regular", "scheduled", ?, ?)''',
                       (student_name, session['username'], request.form.get('subject', ''), lesson_date, lesson_time, now, now))
        db.commit()
        flash("Занятие назначено!")
        return redirect(url_for('repetitor_main'))
    cursor.execute('SELECT DISTINCT student_name, subject FROM submitted_records WHERE repetitor_username=?', (session['username'],))
    students = cursor.fetchall()
    return render_template('schedule_lesson.html', students=students)


@app.route('/repetitor/add_lesson', methods=['GET', 'POST'])
def add_lesson():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        student_name = request.form['student_name']
        lesson_date = request.form['lesson_date']
        lesson_time = request.form['lesson_time']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lesson_date_obj = datetime.strptime(lesson_date, '%Y-%m-%d').date()
        if lesson_date_obj < datetime(2025, 1, 1).date() or lesson_date_obj > (datetime.now() + timedelta(days=365)).date():
            flash("Некорректная дата!")
            return redirect(url_for('add_lesson'))
        cursor.execute('''INSERT INTO regular_lessons (student_name, repetitor_username, subject, lesson_date, lesson_time, lesson_type, status, created_at, updated_at)
                          VALUES (?, ?, ?, ?, ?, "regular", "scheduled", ?, ?)''',
                       (student_name, session['username'], request.form.get('subject', ''), lesson_date, lesson_time, now, now))
        db.commit()
        flash("Дополнительное занятие добавлено!")
        return redirect(url_for('repetitor_main'))
    cursor.execute('SELECT DISTINCT student_name, subject FROM submitted_records WHERE repetitor_username=?', (session['username'],))
    students = cursor.fetchall()
    return render_template('add_lesson.html', students=students)


@app.route('/repetitor/reschedule_lesson', methods=['GET', 'POST'])
def reschedule_lesson():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    next_url = request.args.get('next')
    lesson_id = request.args.get('lesson_id')
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST' and 'confirm_reschedule' in request.form:
        lesson_id = request.form['lesson_id']
        new_date = request.form['lesson_date']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_date_obj = datetime.strptime(new_date, '%Y-%m-%d').date()
        if new_date_obj < datetime(2025, 1, 1).date() or new_date_obj > (datetime.now() + timedelta(days=365)).date():
            flash("Некорректная дата!")
            return redirect(url_for('reschedule_lesson', lesson_id=lesson_id, next=next_url))
        log_lesson_change(cursor, session['username'], lesson_id, 'reschedule', new_date, None, request.form.get('reason', ''))
        cursor.execute('UPDATE regular_lessons SET lesson_date=?, updated_at=? WHERE id=? AND repetitor_username=?',
                       (new_date, now, lesson_id, session['username']))
        db.commit()
        flash("Занятие перенесено!")
        return redirect(next_url or url_for('repetitor_main'))
    if lesson_id:
        cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE id=? AND repetitor_username=? AND status="scheduled"',
                       (lesson_id, session['username']))
        selected_lesson = cursor.fetchone()
        if not selected_lesson:
            flash("Занятие не найдено")
            return redirect(next_url or url_for('repetitor_main'))
        return render_template('reschedule_lesson.html', lessons=[], selected_lesson=selected_lesson, next_url=next_url)
    cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE repetitor_username=? AND status="scheduled" ORDER BY student_name, lesson_date, lesson_time',
                   (session['username'],))
    lessons = cursor.fetchall()
    return render_template('reschedule_lesson.html', lessons=lessons, selected_lesson=None, next_url=next_url)


@app.route('/repetitor/cancel_lesson', methods=['GET', 'POST'])
def cancel_lesson():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    next_url = request.args.get('next')
    lesson_id = request.args.get('lesson_id')
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST' and 'confirm_cancel' in request.form:
        lesson_id = request.form['lesson_id']
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_lesson_change(cursor, session['username'], lesson_id, 'cancel', None, None, request.form['cancellation_reason'])
        cursor.execute('UPDATE regular_lessons SET status="cancelled", cancellation_reason=?, updated_at=? WHERE id=? AND repetitor_username=?',
                       (request.form['cancellation_reason'], now, lesson_id, session['username']))
        db.commit()
        flash("Занятие отменено!")
        return redirect(next_url or url_for('repetitor_main'))
    if lesson_id:
        cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE id=? AND repetitor_username=? AND status="scheduled"',
                       (lesson_id, session['username']))
        selected_lesson = cursor.fetchone()
        if not selected_lesson:
            flash("Занятие не найдено")
            return redirect(next_url or url_for('repetitor_main'))
        return render_template('cancel_lesson.html', selected_lesson=selected_lesson, lessons=[], next_url=next_url)
    cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE repetitor_username=? AND status="scheduled" ORDER BY student_name, lesson_date, lesson_time',
                   (session['username'],))
    lessons = cursor.fetchall()
    return render_template('cancel_lesson.html', lessons=lessons, selected_lesson=None, next_url=next_url)


@app.route('/repetitor/regular_students')
def repetitor_regular_students():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''SELECT student_name, subject, manager_username, submitted_at, additional_info
                      FROM submitted_records WHERE repetitor_username=? AND lesson_type="regular" ORDER BY submitted_at DESC''', (session['username'],))
    regular_students = cursor.fetchall()
    return render_template('repetitor_regular_students.html', students=regular_students)


@app.route('/repetitor/regular_students/manage', methods=['GET', 'POST'])
def repetitor_regular_students_manage():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if action == 'cancel_lesson':
            lesson_id = request.form.get('lesson_id')
            if lesson_id:
                cursor.execute('UPDATE regular_lessons SET status="cancelled", cancellation_reason=?, updated_at=? WHERE id=? AND repetitor_username=?',
                               (request.form.get('cancellation_reason', ''), now, lesson_id, session['username']))
                db.commit()
                flash("Занятие отменено!")
        elif action == 'add_lesson':
            student_name = request.form.get('student_name')
            subject = request.form.get('subject')
            lesson_date = request.form.get('lesson_date')
            lesson_time = request.form.get('lesson_time')
            if student_name and subject and lesson_date and lesson_time:
                try:
                    new_date_obj = datetime.strptime(lesson_date, '%Y-%m-%d').date()
                    if new_date_obj < datetime(2025, 1, 1).date() or new_date_obj > (datetime.now() + timedelta(days=365)).date():
                        flash("Некорректная дата!")
                        return redirect(url_for('repetitor_regular_students_manage'))
                except ValueError:
                    flash("Неверный формат даты!")
                    return redirect(url_for('repetitor_regular_students_manage'))
                cursor.execute('''INSERT INTO regular_lessons (student_name, repetitor_username, subject, lesson_date, lesson_time, lesson_type, status, created_at, updated_at)
                                  VALUES (?, ?, ?, ?, ?, "regular", "scheduled", ?, ?)''',
                               (student_name, session['username'], subject, lesson_date, lesson_time, now, now))
                db.commit()
                flash("Занятие добавлено!")
        return redirect(url_for('repetitor_regular_students_manage'))
    cursor.execute('SELECT student_name, subject, additional_info FROM submitted_records WHERE repetitor_username=? AND lesson_type="regular" ORDER BY student_name',
                   (session['username'],))
    students = cursor.fetchall()
    student_lessons = {}
    for student in students:
        cursor.execute('SELECT id, lesson_date, lesson_time, status, cancellation_reason FROM regular_lessons WHERE student_name=? AND repetitor_username=? ORDER BY lesson_date, lesson_time',
                       (student[0], session['username']))
        student_lessons[student[0]] = cursor.fetchall()
    return render_template('repetitor_regular_students_manage.html', students=students, student_lessons=student_lessons)


@app.route('/repetitor/regular_lesson/edit/<int:lesson_id>', methods=['GET', 'POST'])
def edit_regular_lesson(lesson_id):
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        new_date = request.form.get('lesson_date')
        new_time = request.form.get('lesson_time')
        if new_date and new_time:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                new_date_obj = datetime.strptime(new_date, '%Y-%m-%d').date()
                if new_date_obj < datetime(2025, 1, 1).date() or new_date_obj > (datetime.now() + timedelta(days=365)).date():
                    flash("Некорректная дата!")
                    return redirect(url_for('edit_regular_lesson', lesson_id=lesson_id))
            except ValueError:
                flash("Неверный формат даты!")
                return redirect(url_for('edit_regular_lesson', lesson_id=lesson_id))
            log_lesson_change(cursor, session['username'], lesson_id, 'reschedule', new_date, new_time, '')
            cursor.execute('UPDATE regular_lessons SET lesson_date=?, lesson_time=?, updated_at=? WHERE id=? AND repetitor_username=?',
                           (new_date, new_time, now, lesson_id, session['username']))
            db.commit()
            flash("Время занятия изменено!")
            return redirect(url_for('repetitor_regular_students_manage'))
    cursor.execute('SELECT id, student_name, lesson_date, lesson_time, subject FROM regular_lessons WHERE id=? AND repetitor_username=?',
                   (lesson_id, session['username']))
    lesson = cursor.fetchone()
    if not lesson:
        flash("Занятие не найдено")
        return redirect(url_for('repetitor_regular_students_manage'))
    return render_template('edit_regular_lesson.html', lesson=lesson)


@app.route('/repetitor/schedule')
def repetitor_schedule():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE repetitor_username=? AND lesson_type="regular" AND status="scheduled" ORDER BY lesson_date, lesson_time',
                   (session['username'],))
    regular_lessons = cursor.fetchall()
    cursor.execute('SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons WHERE repetitor_username=? AND status="scheduled" ORDER BY lesson_date, lesson_time',
                   (session['username'],))
    all_lessons = cursor.fetchall()
    days_ru = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

    def group_by_day(lessons):
        schedule = {day: [] for day in days_ru}
        for lesson in lessons:
            try:
                day_name = days_ru[datetime.strptime(lesson[2], '%Y-%m-%d').weekday()]
                schedule[day_name].append({'id': lesson[0], 'student': lesson[1], 'time': lesson[3], 'date': lesson[2]})
            except Exception:
                continue
        return schedule

    return render_template('repetitor_schedule.html', regular_schedule=group_by_day(regular_lessons), current_schedule=group_by_day(all_lessons))


@app.route('/repetitor/salary')
def repetitor_salary():
    if 'username' not in session or session.get('role') not in ('repetitor', 'senior_repetitor'):
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    username = session['username']
    cursor.execute('SELECT rate_percent, rate_type, updated_at, fixed_amount FROM salary_rates WHERE repetitor_username=?', (username,))
    rate_row = cursor.fetchone()
    cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (username,))
    unpaid = cursor.fetchone()[0]
    cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=1', (username,))
    total_paid = cursor.fetchone()[0]
    cursor.execute('SELECT id, lesson_type, lesson_amount, accrual_amount, accrued_at, paid, paid_at FROM salary_accruals WHERE repetitor_username=? ORDER BY accrued_at DESC', (username,))
    accruals = cursor.fetchall()
    cursor.execute('SELECT amount, paid_at, note FROM salary_payouts WHERE username=? ORDER BY paid_at DESC', (username,))
    return render_template('repetitor_salary.html', rate_row=rate_row, unpaid=unpaid, total_paid=total_paid, accruals=accruals, payouts=cursor.fetchall())



@app.route('/fix_repetitors')
def fix_repetitors():
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute('UPDATE users SET role="repetitor", subject="Математика" WHERE role="repetitor_math"')
    cursor.execute('UPDATE users SET role="repetitor", subject="Информатика" WHERE role="repetitor_informatika"')
    cursor.execute('UPDATE users SET role="repetitor", subject="Обществознание" WHERE role="repetitor_obshestvo"')
    db.commit()
    return "Репетиторы исправлены!"


@app.route('/uploads/<path:name>')
def uploaded_file(name):
    if session.get('role') not in ('admin', 'director', 'senior_repetitor', 'repetitor'):
        abort(403)
    return send_from_directory(UPLOAD_DIR, name)


# ===================== Conducted lessons, checks, settings, students =====================
def parse_money(text):
    try:
        return float(re.sub(r'[^0-9.,]', '', str(text or '')).replace(',', '.') or 0)
    except ValueError:
        return 0.0


def accrue_for_report(cursor, report_id):
    """Creates the salary accrual for a verified lesson report. Returns (ok, message)."""
    cursor.execute('SELECT repetitor_username, amount FROM lesson_reports WHERE id=?', (report_id,))
    rep = cursor.fetchone()
    if not rep:
        return False, "Отчёт не найден."
    cursor.execute('SELECT id FROM salary_accruals WHERE report_id=?', (report_id,))
    if cursor.fetchone():
        return True, "Начисление уже было создано."
    cursor.execute('SELECT rate_percent, rate_type, fixed_amount FROM salary_rates WHERE repetitor_username=?', (rep[0],))
    rate = cursor.fetchone()
    if not rate:
        return False, "Ставка репетитора не установлена, начисление не создано."
    accrual = (rate[2] or 0) if rate[1] == 'fixed' else rep[1] * (rate[0] / 100)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''INSERT INTO salary_accruals (repetitor_username, lesson_id, lesson_type, lesson_amount, accrual_amount, accrued_at, report_id)
                      VALUES (?, NULL, "regular", ?, ?, ?, ?)''', (rep[0], rep[1], accrual, now, report_id))
    return True, f"Начислено {accrual:.2f} руб."


@app.route('/repetitor/conducted', methods=['GET', 'POST'])
def repetitor_conducted():
    db = get_db()
    cursor = db.cursor()
    me = session['username']
    if request.method == 'POST':
        lesson_id = request.form.get('lesson_id')
        cursor.execute('''SELECT id, student_name, lesson_date, lesson_time FROM regular_lessons
                          WHERE id=? AND repetitor_username=? AND status="scheduled" AND COALESCE(report_submitted,0)=0''', (lesson_id, me))
        lesson = cursor.fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not lesson:
            flash("Занятие не найдено или отчёт по нему уже отправлен.")
        elif request.form.get('held') == '0':
            reason = request.form.get('reason', '').strip()
            if not reason:
                flash("Укажите причину, почему занятия не было.")
            else:
                cursor.execute('''INSERT INTO lesson_reports (lesson_id, repetitor_username, student_name, lesson_date, lesson_time, held, reason, status, created_at)
                                  VALUES (?, ?, ?, ?, ?, 0, ?, "not_held", ?)''', (lesson[0], me, lesson[1], lesson[2], lesson[3], reason, now))
                cursor.execute('UPDATE regular_lessons SET status="missed", report_submitted=1, updated_at=? WHERE id=?', (now, lesson[0]))
                db.commit()
                flash("Отмечено, что занятия не было.")
        else:
            shot = save_upload(request.files.get('check_file'))
            work = request.form.get('work_done', '').strip()
            amount = parse_money(request.form.get('amount'))
            if shot is None:
                flash("Чек: допустимы только изображения и PDF.")
            elif not shot:
                flash("Приложите чек!")
            elif not work:
                flash("Опишите, что было сделано на занятии.")
            elif amount <= 0:
                flash("Укажите сумму занятия.")
            else:
                cursor.execute('''INSERT INTO lesson_reports (lesson_id, repetitor_username, student_name, lesson_date, lesson_time, held, work_done, amount, check_file, status, created_at)
                                  VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, "pending", ?)''', (lesson[0], me, lesson[1], lesson[2], lesson[3], work, amount, shot, now))
                cursor.execute('UPDATE regular_lessons SET status="completed", report_submitted=1, updated_at=? WHERE id=?', (now, lesson[0]))
                db.commit()
                flash("Отчёт и чек отправлены на проверку директору.")
        return redirect(url_for('repetitor_conducted'))
    pending = pending_report_lessons(cursor, me)
    prices = {}
    for lesson in pending:
        cursor.execute('SELECT price FROM student_info WHERE student_name=? AND repetitor_username=?', (lesson[1], me))
        row = cursor.fetchone()
        prices[lesson[1]] = parse_money(row[0]) if row else 0
    cursor.execute('''SELECT id, student_name, lesson_date, lesson_time, held, reason, work_done, amount, check_file, status
                      FROM lesson_reports WHERE repetitor_username=? ORDER BY created_at DESC LIMIT 100''', (me,))
    return render_template('repetitor_conducted.html', pending=pending, prices=prices, history=cursor.fetchall(),
                           duration=int(get_setting(cursor, 'lesson_duration_min', 60)))


@app.route('/director/checks', methods=['GET', 'POST'])
@roles_required('director')
def director_checks():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        verdict = request.form.get('verdict')
        report_id = request.form.get('report_id')
        status_map = {'yes': 'verified', 'no': 'rejected', 'fake': 'fake'}
        cursor.execute('SELECT status FROM lesson_reports WHERE id=?', (report_id,))
        row = cursor.fetchone()
        if not row or verdict not in status_map:
            flash("Отчёт не найден.")
        elif row[0] != 'pending':
            flash("Этот отчёт уже проверен.")
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE lesson_reports SET status=?, checked_by=?, checked_at=? WHERE id=?',
                           (status_map[verdict], session['username'], now, report_id))
            msg = {'yes': 'Чек подтверждён. ', 'no': 'Чек отклонён.', 'fake': 'Чек отмечен как фейк.'}[verdict]
            if verdict == 'yes':
                ok, text = accrue_for_report(cursor, report_id)
                msg += text
            db.commit()
            flash(msg)
        return redirect(url_for('director_checks', status=request.args.get('status', 'pending')))
    status = request.args.get('status', 'pending')
    cursor.execute('SELECT status, COUNT(*) FROM lesson_reports WHERE held=1 GROUP BY status')
    counts = dict(cursor.fetchall())
    return render_template('director_checks.html', rows=_checks_rows(cursor, status), status=status, counts=counts)


def _checks_rows(cursor, status):
    where = '' if status == 'all' else 'AND r.status = ?'
    params = () if status == 'all' else (status,)
    cursor.execute(f'''SELECT r.id, r.repetitor_username, u.name, r.student_name, r.lesson_date, r.lesson_time, r.amount, r.work_done,
                              r.check_file, r.status, r.created_at,
                              (SELECT ROUND(AVG(rating), 1) FROM report_reviews WHERE report_id = r.id),
                              (SELECT GROUP_CONCAT(complaint, ' | ') FROM report_reviews WHERE report_id = r.id AND complaint != '')
                       FROM lesson_reports r LEFT JOIN users u ON u.username = r.repetitor_username
                       WHERE r.held = 1 {where} ORDER BY r.created_at DESC LIMIT 300''', params)
    return cursor.fetchall()


@app.route('/director/settings', methods=['GET', 'POST'])
@roles_required('director')
def director_settings():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        if request.form.get('action') == 'duration':
            value = request.form.get('duration', '')
            if value.isdigit() and int(value) in LESSON_DURATIONS:
                set_setting(cursor, 'lesson_duration_min', int(value))
                db.commit()
                flash(f"Время до уведомления о проведённом занятии: {value} мин.")
        elif request.form.get('action') == 'limit':
            value = request.form.get('max_students', '')
            limit = int(value) if value.isdigit() and int(value) in MAX_STUDENTS_CHOICES else None
            cursor.execute('UPDATE users SET max_students=? WHERE username=? AND role IN ("repetitor", "senior_repetitor")',
                           (limit, request.form.get('username')))
            db.commit()
            flash("Лимит учеников обновлён.")
        return redirect(url_for('director_settings'))
    cursor.execute('SELECT username, name, subject FROM users WHERE role IN ("repetitor", "senior_repetitor") ORDER BY name')
    reps = []
    for u in cursor.fetchall():
        count, limit = repetitor_load(cursor, u[0])
        reps.append({'username': u[0], 'name': u[1] or u[0], 'subject': u[2] or '', 'count': count, 'limit': limit})
    return render_template('director_settings.html', duration=int(get_setting(cursor, 'lesson_duration_min', 60)),
                           durations=LESSON_DURATIONS, choices=MAX_STUDENTS_CHOICES, reps=reps)


# ---------- student data ----------
def student_rows(cursor, repetitor):
    """Students of a repetitor with their card data (saved card)."""
    cursor.execute('''SELECT DISTINCT student_name, subject FROM submitted_records
                      WHERE repetitor_username=? AND lesson_type="regular"
                      UNION SELECT student_name, subject FROM student_info WHERE repetitor_username=?
                      ORDER BY 1''', (repetitor, repetitor))
    rows = []
    for name, subject in cursor.fetchall():
        cursor.execute('''SELECT parent_name, contact, grade, timezone, price, notes, subject FROM student_info
                          WHERE student_name=? AND repetitor_username=?''', (name, repetitor))
        info = cursor.fetchone()
        if not info:
            info = ('', '', '', '', '', '', subject)
        rows.append({'name': name, 'subject': subject or info[6] or '', 'parent_name': info[0] or '', 'contact': info[1] or '',
                     'grade': info[2] or '', 'timezone': info[3] or '', 'price': info[4] or '', 'notes': info[5] or ''})
    return rows


def save_student_info(cursor, name, repetitor, subject, form):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vals = [(form.get(f) or '').strip() for f in ('parent_name', 'contact', 'grade', 'timezone', 'price', 'notes')]
    cursor.execute('SELECT id FROM student_info WHERE student_name=? AND repetitor_username=?', (name, repetitor))
    if cursor.fetchone():
        cursor.execute('''UPDATE student_info SET parent_name=?, contact=?, grade=?, timezone=?, price=?, notes=?
                          WHERE student_name=? AND repetitor_username=?''', vals + [name, repetitor])
    else:
        cursor.execute('''INSERT INTO student_info (student_name, repetitor_username, subject, parent_name, contact, grade, timezone, price, notes, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', [name, repetitor, subject] + vals + [now])


@app.route('/repetitor/students_info', methods=['GET', 'POST'])
def repetitor_students_info():
    db = get_db()
    cursor = db.cursor()
    me = session['username']
    if request.method == 'POST':
        save_student_info(cursor, request.form['student_name'], me, request.form.get('subject', ''), request.form)
        db.commit()
        flash("Данные ученика сохранены.")
        return redirect(url_for('repetitor_students_info'))
    count, limit = repetitor_load(cursor, me)
    return render_template('repetitor_students_info.html', students=student_rows(cursor, me), count=count, limit=limit)


@app.route('/director/students', methods=['GET', 'POST'])
@roles_required('director')
def director_students():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        name = request.form.get('student_name', '').strip()
        rep = request.form.get('repetitor_username', '')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if action == 'delete':
            cursor.execute('DELETE FROM submitted_records WHERE student_name=? AND repetitor_username=?', (name, rep))
            cursor.execute('''UPDATE regular_lessons SET status="cancelled", cancellation_reason="Ученик удалён директором", updated_at=?
                              WHERE student_name=? AND repetitor_username=? AND status="scheduled"''', (now, name, rep))
            cursor.execute('DELETE FROM student_info WHERE student_name=? AND repetitor_username=?', (name, rep))
            db.commit()
            flash(f"Ученик {name} удалён у репетитора {rep}.")
        elif action in ('move', 'add'):
            target = request.form.get('new_repetitor') if action == 'move' else rep
            count, limit = repetitor_load(cursor, target)
            already = cursor.execute('SELECT 1 FROM submitted_records WHERE student_name=? AND repetitor_username=? AND lesson_type="regular"', (name, target)).fetchone()
            if not name or not target:
                flash("Укажите ученика и репетитора.")
            elif limit and count >= limit and not already:
                flash(f"У репетитора {target} уже максимум учеников ({limit}).")
            elif action == 'move':
                cursor.execute('UPDATE submitted_records SET repetitor_username=? WHERE student_name=? AND repetitor_username=?', (target, name, rep))
                cursor.execute('UPDATE regular_lessons SET repetitor_username=?, updated_at=? WHERE student_name=? AND repetitor_username=? AND status="scheduled"', (target, now, name, rep))
                cursor.execute('DELETE FROM student_info WHERE student_name=? AND repetitor_username=?', (name, target))
                cursor.execute('UPDATE student_info SET repetitor_username=? WHERE student_name=? AND repetitor_username=?', (target, name, rep))
                db.commit()
                flash(f"Ученик {name} перемещён к репетитору {target}.")
            else:
                subject = request.form.get('subject', '').strip()
                cursor.execute('''INSERT INTO submitted_records (manager_username, student_name, subject, submitted_at, added_by_role, repetitor_username, lesson_type)
                                  VALUES (?, ?, ?, ?, "director", ?, "regular")''', (session['username'], name, subject, now, target))
                save_student_info(cursor, name, target, subject, request.form)
                db.commit()
                flash(f"Ученик {name} добавлен репетитору {target}.")
        return redirect(url_for('director_students'))
    cursor.execute('SELECT username, name, subject FROM users WHERE role IN ("repetitor", "senior_repetitor") ORDER BY name')
    reps = cursor.fetchall()
    groups = []
    for u in reps:
        count, limit = repetitor_load(cursor, u[0])
        groups.append({'username': u[0], 'name': u[1] or u[0], 'subject': u[2] or '', 'count': count, 'limit': limit,
                       'students': student_rows(cursor, u[0])})
    return render_template('director_students.html', groups=groups, reps=reps)


# ---------- review of repetitors' lesson reports (senior repetitor) ----------
@app.route('/senior_repetitor/reports', methods=['GET', 'POST'])
@roles_required('senior_repetitor')
def senior_repetitor_reports():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        report_id = request.form.get('report_id')
        rating = request.form.get('rating', '')
        complaint = request.form.get('complaint', '').strip()
        rating = int(rating) if rating.isdigit() and 1 <= int(rating) <= 10 else None
        if rating is None and not complaint:
            flash("Поставьте оценку или напишите жалобу.")
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''INSERT OR REPLACE INTO report_reviews (report_id, reviewer, rating, complaint, created_at)
                              VALUES (?, ?, ?, ?, ?)''', (report_id, session['username'], rating, complaint, now))
            db.commit()
            flash("Оценка сохранена." if not complaint else "Жалоба отправлена директору.")
        return redirect(url_for('senior_repetitor_reports'))
    cursor.execute('''SELECT r.id, r.repetitor_username, u.name, r.student_name, r.lesson_date, r.lesson_time, r.work_done,
                             rv.rating, rv.complaint
                      FROM lesson_reports r LEFT JOIN users u ON u.username = r.repetitor_username
                      LEFT JOIN report_reviews rv ON rv.report_id = r.id AND rv.reviewer = ?
                      WHERE r.held = 1 ORDER BY r.created_at DESC LIMIT 150''', (session['username'],))
    return render_template('senior_repetitor_reports.html', rows=cursor.fetchall())


# ===================== Salary (repetitors + managers) =====================
def manager_unaccrued(cursor, username):
    """Responses reports and held trial lessons of a manager that were not accrued yet."""
    cursor.execute('''SELECT id, responses_sent FROM manager_responses WHERE manager_username=?
                      AND id NOT IN (SELECT lesson_id FROM salary_accruals WHERE repetitor_username=? AND lesson_type="responses")''', (username, username))
    responses = cursor.fetchall()
    cursor.execute('''SELECT id FROM trial_lessons WHERE manager_username=? AND status="completed" AND result IN ("enrolled", "refused", "thinking")
                      AND id NOT IN (SELECT lesson_id FROM salary_accruals WHERE repetitor_username=? AND lesson_type="trial_held")''', (username, username))
    return responses, cursor.fetchall()


@app.route('/director/salary', methods=['GET', 'POST'])
@roles_required('director')
def director_salary():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user = request.form.get('username') or request.form.get('repetitor_username', '')
        if action == 'set_rate':
            rate_type = request.form.get('rate_type', 'percent')
            rate_type = rate_type if rate_type in ('percent', 'fixed') else 'percent'
            try:
                rate_percent = float(request.form.get('rate_percent') or 50)
                fixed_amount = float(request.form.get('fixed_amount') or 0)
                trial_rate = 0  # пробные занятия всегда бесплатны
            except ValueError:
                flash("Проверьте числа в ставке.")
                return redirect(url_for('director_salary'))
            cursor.execute('SELECT id FROM salary_rates WHERE repetitor_username=?', (user,))
            if cursor.fetchone():
                cursor.execute('''UPDATE salary_rates SET rate_percent=?, trial_lesson_rate=?, rate_type=?, fixed_amount=?, set_by=?, updated_at=?
                                  WHERE repetitor_username=?''', (rate_percent, trial_rate, rate_type, fixed_amount, session['username'], now, user))
            else:
                cursor.execute('''INSERT INTO salary_rates (repetitor_username, rate_percent, trial_lesson_rate, rate_type, fixed_amount, set_by, updated_at)
                                  VALUES (?, ?, ?, ?, ?, ?, ?)''', (user, rate_percent, trial_rate, rate_type, fixed_amount, session['username'], now))
            db.commit()
            label = f"{fixed_amount:g} руб. за занятие" if rate_type == 'fixed' else f"{rate_percent:g}% от занятия"
            flash(f"Ставка для {user} обновлена: {label}.")
        elif action == 'set_manager_rate':
            try:
                per_response = float(request.form.get('per_response') or 0)
                per_trial = float(request.form.get('per_trial') or 0)
            except ValueError:
                flash("Проверьте числа в ставке.")
                return redirect(url_for('director_salary'))
            cursor.execute('''INSERT INTO manager_salary_rates (manager_username, per_response, per_trial, updated_at) VALUES (?, ?, ?, ?)
                              ON CONFLICT(manager_username) DO UPDATE SET per_response=excluded.per_response, per_trial=excluded.per_trial, updated_at=excluded.updated_at''',
                           (user, per_response, per_trial, now))
            db.commit()
            flash(f"Ставка менеджера {user}: {per_response:g} руб. за отклик, {per_trial:g} руб. за проведённое пробное.")
        elif action == 'accrue_manager':
            cursor.execute('SELECT per_response, per_trial FROM manager_salary_rates WHERE manager_username=?', (user,))
            rate = cursor.fetchone()
            if not rate:
                flash("Сначала установите ставку менеджера.")
            else:
                responses, trials = manager_unaccrued(cursor, user)
                total = 0.0
                for rid, sent in responses:
                    amount = (sent or 0) * rate[0]
                    total += amount
                    cursor.execute('''INSERT INTO salary_accruals (repetitor_username, lesson_id, lesson_type, lesson_amount, accrual_amount, accrued_at)
                                      VALUES (?, ?, "responses", ?, ?, ?)''', (user, rid, sent or 0, amount, now))
                for (tid,) in trials:
                    total += rate[1]
                    cursor.execute('''INSERT INTO salary_accruals (repetitor_username, lesson_id, lesson_type, lesson_amount, accrual_amount, accrued_at)
                                      VALUES (?, ?, "trial_held", 1, ?, ?)''', (user, tid, rate[1], now))
                db.commit()
                flash(f"Менеджеру {user} начислено {total:.2f} руб. (отчётов об откликах: {len(responses)}, пробных: {len(trials)}).")
        elif action == 'mark_paid':
            cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (user,))
            unpaid = cursor.fetchone()[0]
            try:
                amount = float(request.form.get('amount') or unpaid)
            except ValueError:
                amount = unpaid
            if amount <= 0:
                flash("Нечего выплачивать.")
            else:
                cursor.execute('UPDATE salary_accruals SET paid=1, paid_at=? WHERE repetitor_username=? AND paid=0', (now, user))
                cursor.execute('INSERT INTO salary_payouts (username, amount, paid_by, paid_at, note) VALUES (?, ?, ?, ?, ?)',
                               (user, amount, session['username'], now, request.form.get('note', '').strip()))
                db.commit()
                flash(f"Выплата для {user} отмечена: {amount:.2f} руб.")
        elif action == 'accrue_lesson':
            lesson_type = request.form.get('lesson_type', 'regular')
            try:
                lesson_amount = float(request.form.get('lesson_amount') or 0)
            except ValueError:
                lesson_amount = 0
            cursor.execute('SELECT rate_percent, trial_lesson_rate, rate_type, fixed_amount FROM salary_rates WHERE repetitor_username=?', (user,))
            rate = cursor.fetchone()
            if not rate:
                flash("Сначала установите ставку для этого репетитора!")
            else:
                if rate[2] == 'fixed':
                    accrual = rate[3] or 0
                else:
                    accrual = lesson_amount * (rate[0] / 100)
                cursor.execute('INSERT INTO salary_accruals (repetitor_username, lesson_id, lesson_type, lesson_amount, accrual_amount, accrued_at) VALUES (?, ?, ?, ?, ?, ?)',
                               (user, request.form.get('lesson_id'), lesson_type, lesson_amount, accrual, now))
                db.commit()
                flash(f"Начислено {accrual:.2f} руб. для {user}")
        return redirect(url_for('director_salary', sort=request.args.get('sort', ''), role=request.args.get('role', '')))

    sort = request.args.get('sort', 'name')
    role_filter = request.args.get('role', 'all')
    roles = ('repetitor', 'senior_repetitor', 'manager', 'senior_manager')
    cursor.execute(f'''SELECT u.username, u.name, u.subject, u.role, sr.rate_percent, sr.trial_lesson_rate, sr.updated_at,
                              sr.rate_type, sr.fixed_amount, mr.per_response, mr.per_trial
                       FROM users u LEFT JOIN salary_rates sr ON sr.repetitor_username = u.username
                       LEFT JOIN manager_salary_rates mr ON mr.manager_username = u.username
                       WHERE u.role IN ({",".join('"%s"' % r for r in roles)}) ORDER BY u.name''')
    salary_data = []
    for u in cursor.fetchall():
        username = u[0]
        if role_filter != 'all' and u[3] != role_filter:
            continue
        cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (username,))
        unpaid = cursor.fetchone()[0]
        cursor.execute('SELECT COALESCE(SUM(amount), 0) FROM salary_payouts WHERE username=?', (username,))
        total_paid = cursor.fetchone()[0]
        cursor.execute('SELECT id, lesson_type, lesson_amount, accrual_amount, accrued_at, paid FROM salary_accruals WHERE repetitor_username=? ORDER BY accrued_at DESC LIMIT 10', (username,))
        recent = cursor.fetchall()
        is_manager = u[3] in ('manager', 'senior_manager')
        pending_responses = pending_trials = 0
        if is_manager:
            responses, trials = manager_unaccrued(cursor, username)
            pending_responses, pending_trials = sum(r[1] or 0 for r in responses), len(trials)
        salary_data.append({'username': username, 'name': u[1] or username, 'subject': u[2] or '', 'role': u[3],
                            'role_label': ROLE_LABELS.get(u[3], u[3]), 'is_manager': is_manager,
                            'rate_percent': u[4], 'trial_rate': u[5], 'rate_updated': u[6], 'rate_type': u[7] or 'percent',
                            'fixed_amount': u[8], 'per_response': u[9], 'per_trial': u[10],
                            'unpaid_amount': unpaid, 'total_paid': total_paid, 'recent_accruals': recent,
                            'pending_responses': pending_responses, 'pending_trials': pending_trials})
    if sort == 'amount':
        salary_data.sort(key=lambda d: -d['unpaid_amount'])
    elif sort == 'role':
        order = {r: i for i, r in enumerate(roles)}
        salary_data.sort(key=lambda d: (order.get(d['role'], 9), d['name']))
    return render_template('director_salary.html', salary_data=salary_data, sort=sort, role_filter=role_filter,
                           role_labels={r: ROLE_LABELS[r] for r in roles})


@app.route('/manager/salary')
def manager_salary():
    db = get_db()
    cursor = db.cursor()
    me = session['username']
    cursor.execute('SELECT per_response, per_trial FROM manager_salary_rates WHERE manager_username=?', (me,))
    rate = cursor.fetchone()
    cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (me,))
    unpaid = cursor.fetchone()[0]
    cursor.execute('SELECT COALESCE(SUM(amount), 0) FROM salary_payouts WHERE username=?', (me,))
    paid = cursor.fetchone()[0]
    cursor.execute('SELECT lesson_type, lesson_amount, accrual_amount, accrued_at, paid FROM salary_accruals WHERE repetitor_username=? ORDER BY accrued_at DESC LIMIT 50', (me,))
    accruals = cursor.fetchall()
    cursor.execute('SELECT amount, paid_at, note FROM salary_payouts WHERE username=? ORDER BY paid_at DESC', (me,))
    return render_template('manager_salary.html', rate=rate, unpaid=unpaid, paid=paid, accruals=accruals, payouts=cursor.fetchall())


# ===================== Reports: weekly, ledger, violations, school schedule =====================
def parse_week(arg):
    try:
        d = datetime.strptime(arg, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        d = datetime.now().date()
    start = week_start(d)
    return start, start + timedelta(days=6)


def build_weekly_report(cursor, rep, start, end):
    s, e = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    cursor.execute('''SELECT lesson_date, lesson_time, student_name, subject, status FROM regular_lessons
                      WHERE repetitor_username=? AND lesson_date BETWEEN ? AND ? AND status != "cancelled"
                      ORDER BY lesson_date, lesson_time''', (rep, s, e))
    lessons = cursor.fetchall()
    schedule_by_student = {}
    for d, t, name, subject, status in lessons:
        wd = WEEKDAYS[datetime.strptime(d, '%Y-%m-%d').weekday()]
        schedule_by_student.setdefault(name, []).append(f'{wd} {t}')
    students = student_rows(cursor, rep)
    for st in students:
        st['schedule'] = '; '.join(schedule_by_student.get(st['name'], []))
    cursor.execute('''SELECT kind, student_name, old_date, old_time, new_date, new_time, reason, created_at FROM lesson_changes
                      WHERE repetitor_username=? AND date(created_at) BETWEEN ? AND ? ORDER BY created_at''', (rep, s, e))
    changes = cursor.fetchall()
    cursor.execute('''SELECT created_at, reason, description, reporter_username FROM kosyaki
                      WHERE target_username=? AND date(created_at) BETWEEN ? AND ? ORDER BY created_at''', (rep, s, e))
    violations = cursor.fetchall()
    cursor.execute('''SELECT checked_at, "Доход с занятия", student_name, amount FROM lesson_reports
                      WHERE repetitor_username=? AND status="verified" AND date(checked_at) BETWEEN ? AND ?''', (rep, s, e))
    ops = [(r[0], r[1], r[2], r[3]) for r in cursor.fetchall()]
    cursor.execute('''SELECT paid_at, "Выплата зарплаты", COALESCE(note, ""), -amount FROM salary_payouts
                      WHERE username=? AND date(paid_at) BETWEEN ? AND ?''', (rep, s, e))
    ops += cursor.fetchall()
    ops.sort(key=lambda r: r[0])
    income = sum(o[3] for o in ops if o[3] > 0)
    paid = -sum(o[3] for o in ops if o[3] < 0)
    cursor.execute('''SELECT COUNT(*) FROM lesson_reports WHERE repetitor_username=? AND held=1 AND date(created_at) BETWEEN ? AND ?''', (rep, s, e))
    held_count = cursor.fetchone()[0]
    return {'lessons': lessons, 'students': students, 'changes': changes, 'violations': violations, 'ops': ops,
            'income': income, 'paid': paid, 'held_count': held_count}


def weekly_report_view(back_url, rep_choices=None):
    db = get_db()
    cursor = db.cursor()
    start, end = parse_week(request.args.get('week'))
    rep = request.args.get('rep') if rep_choices is not None else session['username']
    if rep_choices is not None and not rep and rep_choices:
        rep = rep_choices[0][0]
    cursor.execute('SELECT name, subject FROM users WHERE username=?', (rep,))
    u = cursor.fetchone()
    data = build_weekly_report(cursor, rep, start, end)
    return render_template('weekly_report.html', rep=rep, rep_name=(u[0] if u else rep), subject=(u[1] if u else ''),
                           start=start, end=end, prev_week=(start - timedelta(days=7)).strftime('%Y-%m-%d'),
                           next_week=(start + timedelta(days=7)).strftime('%Y-%m-%d'), rep_choices=rep_choices,
                           back_url=back_url, weekdays=WEEKDAYS, **data)


@app.route('/repetitor/weekly_report')
def repetitor_weekly_report():
    back = url_for('senior_repetitor') if session.get('role') == 'senior_repetitor' else url_for('repetitor_main')
    return weekly_report_view(back)


@app.route('/director/weekly_report')
@roles_required('director')
def director_weekly_report():
    cursor = get_db().cursor()
    cursor.execute('SELECT username, name FROM users WHERE role IN ("repetitor", "senior_repetitor") ORDER BY name')
    return weekly_report_view(panel_url(), cursor.fetchall())


@app.route('/director/ledger')
@roles_required('director')
def director_ledger():
    db = get_db()
    cursor = db.cursor()
    today = datetime.now().date()
    start = request.args.get('from') or (today - timedelta(days=30)).strftime('%Y-%m-%d')
    end = request.args.get('to') or today.strftime('%Y-%m-%d')
    cursor.execute('''SELECT r.checked_at, "Доход с занятия", COALESCE(u.name, r.repetitor_username), r.student_name, r.amount
                      FROM lesson_reports r LEFT JOIN users u ON u.username = r.repetitor_username
                      WHERE r.status="verified" AND date(r.checked_at) BETWEEN ? AND ?''', (start, end))
    ops = [list(r) for r in cursor.fetchall()]
    cursor.execute('''SELECT p.paid_at, "Выплата зарплаты", COALESCE(u.name, p.username), COALESCE(p.note, ""), -p.amount
                      FROM salary_payouts p LEFT JOIN users u ON u.username = p.username
                      WHERE date(p.paid_at) BETWEEN ? AND ?''', (start, end))
    ops += [list(r) for r in cursor.fetchall()]
    ops.sort(key=lambda r: r[0], reverse=True)
    income = sum(o[4] for o in ops if o[4] > 0)
    expense = -sum(o[4] for o in ops if o[4] < 0)
    return render_template('director_ledger.html', ops=ops, income=income, expense=expense, start=start, end=end)


@app.route('/senior_manager/violations')
def violations_table():
    db = get_db()
    cursor = db.cursor()
    role = request.args.get('role', 'all')
    sql = '''SELECT k.created_at, k.target_username, COALESCE(u.name, k.target_username), COALESCE(u.role, ""),
                    k.reason, k.description, k.reporter_username, k.reporter_role
             FROM kosyaki k LEFT JOIN users u ON u.username = k.target_username'''
    params = ()
    if role == 'managers':
        sql += ' WHERE u.role IN ("manager", "senior_manager")'
    elif role == 'repetitors':
        sql += ' WHERE u.role IN ("repetitor", "senior_repetitor")'
    cursor.execute(sql + ' ORDER BY k.created_at DESC', params)
    rows = [r[:3] + (ROLE_LABELS.get(r[3], r[3]),) + r[4:7] + (ROLE_LABELS.get(r[7], r[7]),) for r in cursor.fetchall()]
    return render_template('violations_table.html', rows=rows, role=role)


@app.route('/senior_manager/school_schedule')
def school_schedule():
    db = get_db()
    cursor = db.cursor()
    start, end = parse_week(request.args.get('week'))
    view = request.args.get('view', 'days')
    rep = request.args.get('rep', '')
    s, e = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    sql = '''SELECT l.lesson_date, l.lesson_time, l.student_name, l.subject, l.repetitor_username, COALESCE(u.name, l.repetitor_username), "Постоянное", l.status
             FROM regular_lessons l LEFT JOIN users u ON u.username = l.repetitor_username
             WHERE l.lesson_date BETWEEN ? AND ? AND l.status != "cancelled"'''
    params = [s, e]
    trial_sql = '''SELECT t.scheduled_date, t.scheduled_time, t.student_name, t.subject, t.repetitor_username, COALESCE(u.name, t.repetitor_username), "Пробное", t.status
                   FROM trial_lessons t LEFT JOIN users u ON u.username = t.repetitor_username
                   WHERE t.scheduled_date BETWEEN ? AND ? AND t.repetitor_username IS NOT NULL'''
    if rep:
        sql += ' AND l.repetitor_username=?'
        trial_sql += ' AND t.repetitor_username=?'
        params_r = params + [rep]
    else:
        params_r = params
    cursor.execute(sql, params_r)
    rows = cursor.fetchall()
    cursor.execute(trial_sql, params_r)
    rows += cursor.fetchall()
    rows.sort(key=lambda r: (r[0] or '', r[1] or ''))
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        days.append({'date': ds, 'label': f'{WEEKDAYS[i]}, {d.strftime("%d.%m")}', 'lessons': [r for r in rows if r[0] == ds]})
    cursor.execute('SELECT username, name FROM users WHERE role IN ("repetitor", "senior_repetitor") ORDER BY name')
    return render_template('school_schedule.html', start=start, end=end, view=view, rep=rep, rows=rows, days=days,
                           reps=cursor.fetchall(), prev_week=(start - timedelta(days=7)).strftime('%Y-%m-%d'),
                           next_week=(start + timedelta(days=7)).strftime('%Y-%m-%d'),
                           back_url=panel_url())



if __name__ == '__main__':
    if not os.path.exists('users.db'):
        init_db()
    migrate_db()
    app.run(debug=True)
