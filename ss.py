from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=BASE_DIR, static_folder=os.path.join(BASE_DIR, 'static'))
app.secret_key = 'your_secret_key_here'
app.permanent_session_lifetime = timedelta(hours=8)


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
            if 'username' not in session or session.get('role') not in roles:
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
                print(f"Добавлена колонка {column} в {table}")

        def ensure_table(name, sql):
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'")
            if not cursor.fetchone():
                cursor.execute(sql)
                print(f"Создана таблица {name}")

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
        db.commit()



PUBLIC_ROUTES = {'login', 'logout', 'static', 'goodbye', 'welcome'}

ROLE_PREFIXES = {
    'director':         ['/director'],
    'senior_manager':   ['/senior_manager'],
    'manager':          ['/manager'],
    'repetitor':        ['/repetitor'],
    'senior_repetitor': ['/senior_repetitor'],
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

    for required_role, prefixes in ROLE_PREFIXES.items():
        for prefix in prefixes:
            if path.startswith(prefix):
                if role != required_role and not (required_role == 'senior_manager' and role == 'director'):
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
    if 'username' not in session or session.get('role') != 'director':
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


@app.route('/director/salary', methods=['GET', 'POST'])
@roles_required('director')
def director_salary():
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if action == 'set_rate':
            rep_u = request.form['repetitor_username']
            rate_percent = float(request.form.get('rate_percent', 50))
            trial_rate = float(request.form.get('trial_lesson_rate', 500))
            cursor.execute('SELECT id FROM salary_rates WHERE repetitor_username=?', (rep_u,))
            if cursor.fetchone():
                cursor.execute('UPDATE salary_rates SET rate_percent=?, trial_lesson_rate=?, set_by=?, updated_at=? WHERE repetitor_username=?',
                               (rate_percent, trial_rate, session['username'], now, rep_u))
            else:
                cursor.execute('INSERT INTO salary_rates (repetitor_username, rate_percent, trial_lesson_rate, set_by, updated_at) VALUES (?, ?, ?, ?, ?)',
                               (rep_u, rate_percent, trial_rate, session['username'], now))
            db.commit()
            flash(f"Ставка для {rep_u} обновлена: {rate_percent}%, пробное: {trial_rate} руб.")
        elif action == 'mark_paid':
            rep_u = request.form['repetitor_username']
            cursor.execute('UPDATE salary_accruals SET paid=1, paid_at=? WHERE repetitor_username=? AND paid=0', (now, rep_u))
            db.commit()
            flash(f"Зарплата для {rep_u} выплачена!")
        elif action == 'accrue_lesson':
            rep_u = request.form['repetitor_username']
            lesson_amount = float(request.form.get('lesson_amount', 0))
            lesson_type = request.form.get('lesson_type', 'regular')
            lesson_id = request.form.get('lesson_id')
            cursor.execute('SELECT rate_percent, trial_lesson_rate FROM salary_rates WHERE repetitor_username=?', (rep_u,))
            rate_row = cursor.fetchone()
            if not rate_row:
                flash("Сначала установите ставку для этого репетитора!")
            else:
                accrual = rate_row[1] if lesson_type == 'trial' else lesson_amount * (rate_row[0] / 100)
                cursor.execute('INSERT INTO salary_accruals (repetitor_username, lesson_id, lesson_type, lesson_amount, accrual_amount, accrued_at) VALUES (?, ?, ?, ?, ?, ?)',
                               (rep_u, lesson_id, lesson_type, lesson_amount, accrual, now))
                db.commit()
                flash(f"Начислено {accrual:.2f} руб. для {rep_u}")
        return redirect(url_for('director_salary'))

    cursor.execute('''SELECT u.username, u.name, u.subject, sr.rate_percent, sr.trial_lesson_rate, sr.updated_at
                      FROM users u LEFT JOIN salary_rates sr ON sr.repetitor_username = u.username
                      WHERE u.role IN ("repetitor", "senior_repetitor") ORDER BY u.name''')
    repetitors = cursor.fetchall()
    salary_data = []
    for rep in repetitors:
        username = rep[0]
        cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (username,))
        unpaid = cursor.fetchone()[0]
        cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=1', (username,))
        total_paid = cursor.fetchone()[0]
        cursor.execute('SELECT id, lesson_type, lesson_amount, accrual_amount, accrued_at, paid FROM salary_accruals WHERE repetitor_username=? ORDER BY accrued_at DESC LIMIT 10', (username,))
        recent_accruals = cursor.fetchall()
        salary_data.append({'username': username, 'name': rep[1] or username, 'subject': rep[2] or '',
                             'rate_percent': rep[3], 'trial_rate': rep[4], 'rate_updated': rep[5],
                             'unpaid_amount': unpaid, 'total_paid': total_paid, 'recent_accruals': recent_accruals})
    return render_template('director_salary.html', salary_data=salary_data)


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
                      FROM trial_lessons tl WHERE tl.repetitor_username IS NOT NULL ORDER BY tl.scheduled_date, tl.scheduled_time''')
    scheduled_trials = cursor.fetchall()
    cursor.execute('SELECT username, subject FROM users WHERE role IN ("repetitor", "senior_repetitor")')
    repetitors = cursor.fetchall()
    return render_template('senior_manager_trial_lessons.html', unassigned_trials=unassigned_trials,
                           scheduled_trials=scheduled_trials, repetitors=repetitors, is_director=is_director)


@app.route('/senior_manager/trial_lessons/reassign/<int:trial_id>', methods=['POST'])
@roles_required('senior_manager', 'director')
def senior_manager_reassign_trial(trial_id):
    new_repetitor = request.form.get('new_repetitor')
    if new_repetitor:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT student_name, subject FROM trial_lessons WHERE id = ?', (trial_id,))
        trial = cursor.fetchone()
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
            cursor.execute('SELECT username FROM users WHERE role IN ("repetitor", "senior_repetitor") AND subject = ?', (selected_student[2],))
            repetitors = [row[0] for row in cursor.fetchall()]
            if not repetitors:
                cursor.execute('SELECT username FROM users WHERE role IN ("repetitor", "senior_repetitor")')
                repetitors = [row[0] for row in cursor.fetchall()]
                flash(f"Нет репетиторов по предмету '{selected_student[2]}'. Показаны все репетиторы.")
    return render_template('senior_manager_assign.html', students=students, selected_student=selected_student, repetitors=repetitors)


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

    return render_template('manager_stats.html',
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
            report_date = request.form.get('report_date', datetime.now().strftime('%Y-%m-%d'))
            notes = request.form.get('notes', '')
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''INSERT INTO manager_responses (manager_username, responses_sent, client_replied, client_enrolled, report_date, notes, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?)''',
                           (username, responses_sent, client_replied, client_enrolled, report_date, notes, now))
            db.commit()
            flash("Отчёт об откликах сохранён!")
        return redirect(url_for('manager_responses'))
    cursor.execute('SELECT id, responses_sent, client_replied, client_enrolled, report_date, notes FROM manager_responses WHERE manager_username = ? ORDER BY report_date DESC', (username,))
    history = cursor.fetchall()
    cursor.execute('SELECT COALESCE(SUM(responses_sent),0), COALESCE(SUM(client_replied),0), COALESCE(SUM(client_enrolled),0) FROM manager_responses WHERE manager_username = ?', (username,))
    totals = cursor.fetchone()
    return render_template('manager_responses.html', history=history, totals=totals)



@app.route('/senior_repetitor')
@roles_required('senior_repetitor')
def senior_repetitor():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT student_name, subject, repetitor_username, manager_username, submitted_at FROM submitted_records')
    records = cursor.fetchall()
    return render_template('senior_repetitor.html', records=records)


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
    return render_template('repetitor.html', students=students, lessons=lessons, trial_count=trial_count)


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
    cursor.execute('SELECT rate_percent, trial_lesson_rate, updated_at FROM salary_rates WHERE repetitor_username=?', (username,))
    rate_row = cursor.fetchone()
    cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=0', (username,))
    unpaid = cursor.fetchone()[0]
    cursor.execute('SELECT COALESCE(SUM(accrual_amount), 0) FROM salary_accruals WHERE repetitor_username=? AND paid=1', (username,))
    total_paid = cursor.fetchone()[0]
    cursor.execute('SELECT id, lesson_type, lesson_amount, accrual_amount, accrued_at, paid, paid_at FROM salary_accruals WHERE repetitor_username=? ORDER BY accrued_at DESC', (username,))
    accruals = cursor.fetchall()
    return render_template('repetitor_salary.html', rate_row=rate_row, unpaid=unpaid, total_paid=total_paid, accruals=accruals)



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


if __name__ == '__main__':
    if not os.path.exists('users.db'):
        init_db()
    migrate_db()
    app.run(debug=True)
