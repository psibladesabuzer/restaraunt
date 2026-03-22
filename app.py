import html
import json
import os
import secrets
import sqlite3
import shutil
from datetime import datetime
from http import cookies
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "restaurant.db")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "restaurant_snapshot.json")
LEGACY_DB_PATH = os.path.join(BASE_DIR, "restaurant.db")
SESSIONS = {}

STATUS_LABELS = {
    "new": "Создан",
    "accepted": "Принят",
    "preparing": "Готовится",
    "ready": "Готов к выдаче",
    "payment_pending": "Ожидает оплату",
    "completed": "Выполнен",
    "cancelled": "Отменен",
}

WAITERS = "waiter"
CHEFS = "chef"
SOUS_CHEFS = "sous_chef"
COOKS = "cook"
MANAGERS = "manager"

DISH_CATALOG = [
    {"name": "Кофе", "price": 140.0},
    {"name": "Чай", "price": 120.0},
    {"name": "Сок", "price": 160.0},
    {"name": "Морс", "price": 110.0},
    {"name": "Вода", "price": 90.0},
    {"name": 'Салат "Оливье"', "price": 260.0},
    {"name": 'Салат "Мимоза"', "price": 280.0},
    {"name": 'Салат "Цезарь"', "price": 340.0},
    {"name": "Жаркое с говядиной", "price": 470.0},
    {"name": "Рыбные котлеты с рисом", "price": 390.0},
    {"name": "Мясные рулетики", "price": 430.0},
    {"name": 'Суп "Харчо"', "price": 290.0},
    {"name": 'Суп "Солянка"', "price": 310.0},
    {"name": 'Суп "Борщ"', "price": 270.0},
    {"name": "Шашлык", "price": 520.0},
    {"name": "Свинные ребрышки в соусе BBQ", "price": 560.0},
    {"name": "Чизкейк", "price": 240.0},
    {"name": "Морковный тарт", "price": 220.0},
    {"name": "Пышки с сахарной пудрой", "price": 170.0},
]

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def human_dt(value):
    if not value:
        return "—"
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def to_number(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def escape(value):
    return html.escape("" if value is None else str(value))


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def get_db():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def save_portable_snapshot():
    ensure_data_dir()
    conn = get_db()
    tables = ["users", "work_sessions", "orders", "order_items", "menu_items", "management_reports"]
    payload = {
        "saved_at": now_iso(),
        "tables": {},
    }
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        payload["tables"][table] = [dict(row) for row in rows]
    conn.close()
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as snapshot_file:
        json.dump(payload, snapshot_file, ensure_ascii=False, indent=2)


def restore_portable_snapshot(conn):
    if not os.path.exists(SNAPSHOT_PATH):
        return False
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as snapshot_file:
        payload = json.load(snapshot_file)
    tables = payload.get("tables", {})
    if not tables:
        return False
    cur = conn.cursor()
    for table in ["management_reports", "order_items", "orders", "work_sessions", "menu_items", "users"]:
        cur.execute(f"DELETE FROM {table}")
    for table in ["users", "work_sessions", "orders", "order_items", "menu_items", "management_reports"]:
        rows = tables.get(table, [])
        if not rows:
            continue
        columns = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        values = [tuple(row.get(column) for column in columns) for row in rows]
        cur.executemany(
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
            values,
        )
    conn.commit()
    return True


def ensure_users_role_schema(conn):
    create_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    create_sql = (create_sql_row["sql"] or "") if create_sql_row else ""
    if "sous_chef" in create_sql and "cook" in create_sql:
        return
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('waiter', 'chef', 'sous_chef', 'cook', 'manager')),
            hourly_rate REAL NOT NULL DEFAULT 0,
            salary_adjustment REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO users (id, username, password, full_name, role, hourly_rate, salary_adjustment, active, created_at)
        SELECT id, username, password, full_name, role, hourly_rate, salary_adjustment, active, created_at
        FROM users_old
        """
    )
    conn.execute("DROP TABLE users_old")
    conn.commit()


def ensure_order_item_tracking_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(order_items)").fetchall()}
    if "assigned_cook_id" not in columns:
        conn.execute("ALTER TABLE order_items ADD COLUMN assigned_cook_id INTEGER")
    if "item_status" not in columns:
        conn.execute("ALTER TABLE order_items ADD COLUMN item_status TEXT NOT NULL DEFAULT 'new'")
    if "ready_at" not in columns:
        conn.execute("ALTER TABLE order_items ADD COLUMN ready_at TEXT")
    if "served_at" not in columns:
        conn.execute("ALTER TABLE order_items ADD COLUMN served_at TEXT")
    conn.commit()


def ensure_orders_status_schema(conn):
    create_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
    ).fetchone()
    create_sql = (create_sql_row["sql"] or "") if create_sql_row else ""
    if "payment_pending" in create_sql and "payment_method" in create_sql:
        return
    conn.execute("ALTER TABLE orders RENAME TO orders_old")
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            waiter_id INTEGER NOT NULL,
            chef_id INTEGER,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            issued_at TEXT,
            payment_method TEXT,
            paid_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('new', 'accepted', 'preparing', 'ready', 'payment_pending', 'completed', 'cancelled')),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(waiter_id) REFERENCES users(id),
            FOREIGN KEY(chef_id) REFERENCES users(id)
        )
        """
    )
    old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders_old)").fetchall()}
    payment_method_expr = "payment_method" if "payment_method" in old_columns else "NULL"
    paid_at_expr = "paid_at" if "paid_at" in old_columns else "NULL"
    conn.execute(
        f"""
        INSERT INTO orders (id, order_number, waiter_id, chef_id, created_at, accepted_at, issued_at, payment_method, paid_at, status, updated_at)
        SELECT id, order_number, waiter_id, chef_id, created_at, accepted_at, issued_at, {payment_method_expr}, {paid_at_expr}, status, updated_at
        FROM orders_old
        """
    )
    conn.execute("DROP TABLE orders_old")
    conn.commit()


def ensure_management_reports_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(management_reports)").fetchall()}
    if "payment_method" not in columns:
        conn.execute("ALTER TABLE management_reports ADD COLUMN payment_method TEXT")
    conn.commit()


def init_db():
    ensure_data_dir()
    db_exists = os.path.exists(DB_PATH)
    if not db_exists and os.path.exists(LEGACY_DB_PATH):
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)
        db_exists = True
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('waiter', 'chef', 'sous_chef', 'cook', 'manager')),
            hourly_rate REAL NOT NULL DEFAULT 0,
            salary_adjustment REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS work_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            hours_worked REAL NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            waiter_id INTEGER NOT NULL,
            chef_id INTEGER,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            issued_at TEXT,
            payment_method TEXT,
            paid_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('new', 'accepted', 'preparing', 'ready', 'payment_pending', 'completed', 'cancelled')),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(waiter_id) REFERENCES users(id),
            FOREIGN KEY(chef_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            dish_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            notes TEXT DEFAULT '',
            price_per_item REAL NOT NULL DEFAULT 0,
            assigned_cook_id INTEGER,
            item_status TEXT NOT NULL DEFAULT 'new',
            ready_at TEXT,
            served_at TEXT,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS management_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE NOT NULL,
            order_number TEXT NOT NULL,
            dishes_count INTEGER NOT NULL,
            dishes_summary TEXT NOT NULL,
            accepted_at TEXT,
            issued_at TEXT,
            payment_method TEXT,
            total_revenue REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        );
        """
    )
    conn.commit()
    ensure_users_role_schema(conn)
    ensure_orders_status_schema(conn)
    ensure_order_item_tracking_schema(conn)
    ensure_management_reports_schema(conn)

    restored = False
    if not db_exists:
        restored = restore_portable_snapshot(conn)

    existing = cur.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
    if not existing:
        created = now_iso()
        cur.executemany(
            """
            INSERT INTO users (username, password, full_name, role, hourly_rate, salary_adjustment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("manager", "manager123", "Руководитель", MANAGERS, 0, 0, created),
                ("waiter1", "waiter123", "Официант 1", WAITERS, 350, 0, created),
                ("chef1", "chef123", "Шеф-повар 1", CHEFS, 520, 0, created),
                ("souschef1", "sous123", "Су-шеф 1", SOUS_CHEFS, 470, 0, created),
                ("cook1", "cook123", "Повар 1", COOKS, 420, 0, created),
            ],
        )
        conn.commit()
    else:
        created = now_iso()
        demo_users = [
            ("chef1", "chef123", "Шеф-повар 1", CHEFS, 520, 0),
            ("souschef1", "sous123", "Су-шеф 1", SOUS_CHEFS, 470, 0),
            ("cook1", "cook123", "Повар 1", COOKS, 420, 0),
        ]
        for username, password, full_name, role, hourly_rate, salary_adjustment in demo_users:
            exists = cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if not exists:
                cur.execute(
                    """
                    INSERT INTO users (username, password, full_name, role, hourly_rate, salary_adjustment, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, password, full_name, role, hourly_rate, salary_adjustment, created),
                )
        conn.commit()

    menu_count = cur.execute("SELECT COUNT(*) AS cnt FROM menu_items").fetchone()["cnt"]
    if not menu_count:
        created = now_iso()
        cur.executemany(
            """
            INSERT INTO menu_items (name, price, active, created_at)
            VALUES (?, ?, 1, ?)
            """,
            [(dish["name"], dish["price"], created) for dish in DISH_CATALOG],
        )
        conn.commit()
    else:
        for dish in DISH_CATALOG:
            cur.execute(
                """
                UPDATE menu_items
                SET price = ?
                WHERE name = ? AND (price IS NULL OR price = 0)
                """,
                (dish["price"], dish["name"]),
            )
        conn.commit()

    conn.close()
    if restored or not db_exists or not os.path.exists(SNAPSHOT_PATH):
        save_portable_snapshot()


def fetch_user_by_credentials(username, password):
    conn = get_db()
    user = conn.execute(
        """
        SELECT * FROM users
        WHERE username = ? AND password = ? AND active = 1
        """,
        (username, password),
    ).fetchone()
    conn.close()
    return user


def fetch_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def fetch_menu_items(active_only=True):
    conn = get_db()
    where = "WHERE active = 1" if active_only else ""
    items = conn.execute(
        f"SELECT * FROM menu_items {where} ORDER BY name"
    ).fetchall()
    conn.close()
    return items


def fetch_assignable_staff(conn, user):
    if user["role"] == CHEFS:
        return conn.execute(
            """
            SELECT id, full_name, role
            FROM users
            WHERE active = 1 AND role IN ('sous_chef', 'cook')
            ORDER BY CASE role WHEN 'sous_chef' THEN 1 ELSE 2 END, full_name
            """
        ).fetchall()
    if user["role"] == SOUS_CHEFS:
        return conn.execute(
            """
            SELECT id, full_name, role
            FROM users
            WHERE active = 1 AND (role = 'cook' OR id = ?)
            ORDER BY CASE WHEN id = ? THEN 1 ELSE 2 END, full_name
            """,
            (user["id"], user["id"]),
        ).fetchall()
    return []


def resolve_menu_item(menu_lookup, dish_name):
    normalized = dish_name.strip().casefold()
    for name, price in menu_lookup.items():
        if name.casefold() == normalized:
            return name, price
    for name, price in menu_lookup.items():
        if normalized and normalized in name.casefold():
            return name, price
    return None, None


def create_session(user_id):
    token = secrets.token_hex(24)
    SESSIONS[token] = {
        "user_id": user_id,
        "login_at": now_iso(),
    }
    return token


def get_current_user(environ):
    raw_cookie = environ.get("HTTP_COOKIE", "")
    jar = cookies.SimpleCookie()
    jar.load(raw_cookie)
    session_cookie = jar.get("session_id")
    if not session_cookie:
        return None
    session_data = SESSIONS.get(session_cookie.value)
    if not session_data:
        return None
    return fetch_user(session_data["user_id"])


def clear_session(environ):
    raw_cookie = environ.get("HTTP_COOKIE", "")
    jar = cookies.SimpleCookie()
    jar.load(raw_cookie)
    session_cookie = jar.get("session_id")
    if session_cookie:
        session_data = SESSIONS.pop(session_cookie.value, None)
        if session_data:
            track_login_session(session_data)


def track_login_session(session_data):
    login_at = session_data.get("login_at")
    user_id = session_data.get("user_id")
    if not login_at or not user_id:
        return
    started_at = datetime.strptime(login_at, "%Y-%m-%d %H:%M:%S")
    finished_at = datetime.now()
    hours = max((finished_at - started_at).total_seconds() / 3600, 0)
    conn = get_db()
    conn.execute(
        """
        INSERT INTO work_sessions (user_id, shift_date, hours_worked, notes, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            started_at.strftime("%Y-%m-%d"),
            round(hours, 2),
            f"Автоматически учтено по входу/выходу: {started_at.strftime('%H:%M')} - {finished_at.strftime('%H:%M')}",
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    save_portable_snapshot()


def parse_post_data(environ):
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        size = 0
    data = environ["wsgi.input"].read(size).decode("utf-8")
    return parse_qs(data, keep_blank_values=True)


def redirect(start_response, location, cookie_header=None):
    headers = [("Location", location)]
    if cookie_header:
        headers.append(("Set-Cookie", cookie_header))
    start_response("302 Found", headers)
    return [b""]


def forbidden(start_response):
    start_response("403 Forbidden", [("Content-Type", "text/html; charset=utf-8")])
    return [render_page("Доступ запрещен", "<p>У вас нет прав для просмотра этой страницы.</p>", None).encode("utf-8")]


def not_found(start_response):
    start_response("404 Not Found", [("Content-Type", "text/html; charset=utf-8")])
    return [render_page("Не найдено", "<p>Страница не найдена.</p>", None).encode("utf-8")]


def require_role(user, roles):
    return user and user["role"] in roles


def order_total(conn, order_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * price_per_item), 0) AS total
        FROM order_items
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()
    return float(row["total"] or 0)


def order_items_summary(conn, order_id):
    items = conn.execute(
        """
        SELECT dish_name, quantity, notes, price_per_item
        FROM order_items
        WHERE order_id = ?
        ORDER BY id
        """,
        (order_id,),
    ).fetchall()
    summary_parts = []
    total_qty = 0
    for item in items:
        total_qty += item["quantity"]
        note_suffix = f" ({item['notes']})" if item["notes"] else ""
        summary_parts.append(f"{item['dish_name']} x{item['quantity']}{note_suffix}")
    return total_qty, ", ".join(summary_parts), items


def item_status_label(status):
    return {
        "new": "Новое",
        "assigned": "Назначено",
        "ready": "Готово",
        "served": "Выдано",
        "cancelled": "Отменено",
    }.get(status, status)


def recalculate_order_state(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    items = conn.execute(
        """
        SELECT item_status, assigned_cook_id, served_at
        FROM order_items
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchall()
    if not order or not items:
        return
    statuses = [row["item_status"] for row in items]
    active_statuses = [status for status in statuses if status != "cancelled"]
    assigned_any = any(row["assigned_cook_id"] for row in items)
    accepted_at = order["accepted_at"]
    issued_at = order["issued_at"]
    payment_method = order["payment_method"]
    if order["chef_id"] and not accepted_at:
        accepted_at = now_iso()
    if not active_statuses and any(status == "cancelled" for status in statuses):
        status = "cancelled"
    elif all(status == "served" for status in active_statuses):
        served_times = [row["served_at"] for row in items if row["served_at"]]
        issued_at = max(served_times) if served_times else now_iso()
        status = "completed" if payment_method else "payment_pending"
    elif all(status in ("ready", "served") for status in active_statuses):
        status = "ready"
    elif assigned_any or any(status == "ready" for status in active_statuses):
        status = "preparing"
    elif order["chef_id"]:
        status = "accepted"
    else:
        status = "new"
    conn.execute(
        """
        UPDATE orders
        SET status = ?, accepted_at = ?, issued_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, accepted_at, issued_at, now_iso(), order_id),
    )


def sync_management_report(conn, order_id):
    order = conn.execute(
        """
        SELECT o.*, w.full_name AS waiter_name, c.full_name AS chef_name
        FROM orders o
        JOIN users w ON w.id = o.waiter_id
        LEFT JOIN users c ON c.id = o.chef_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not order:
        return
    dishes_count, dishes_summary, _ = order_items_summary(conn, order_id)
    total_revenue = order_total(conn, order_id)
    generated_at = now_iso()
    existing = conn.execute(
        "SELECT id FROM management_reports WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    payload = (
        order["order_number"],
        dishes_count,
        dishes_summary,
        order["accepted_at"],
        order["issued_at"],
        order["payment_method"],
        total_revenue,
        order["status"],
        generated_at,
        order_id,
    )
    if existing:
        conn.execute(
            """
            UPDATE management_reports
            SET order_number = ?, dishes_count = ?, dishes_summary = ?, accepted_at = ?,
                issued_at = ?, payment_method = ?, total_revenue = ?, status = ?, generated_at = ?
            WHERE order_id = ?
            """,
            payload,
        )
    else:
        conn.execute(
            """
            INSERT INTO management_reports (
                order_number, dishes_count, dishes_summary, accepted_at, issued_at,
                payment_method, total_revenue, status, generated_at, order_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )


def next_order_number(conn):
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"ORD-{today}-"
    rows = conn.execute(
        "SELECT order_number FROM orders WHERE order_number LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if not rows:
        return prefix + "001"
    last_num = int(rows["order_number"].split("-")[-1])
    return prefix + f"{last_num + 1:03d}"


def nav_for(user):
    if not user:
        return ""
    common = '<a href="/logout">Выйти</a>'
    if user["role"] == WAITERS:
        return f'<a href="/waiter/orders">Заказы</a>{common}'
    if user["role"] in (CHEFS, SOUS_CHEFS):
        return f'<a href="/kitchen/orders">Кухня</a>{common}'
    if user["role"] == COOKS:
        return f'<a href="/cook/items">Мои блюда</a>{common}'
    return (
        '<a href="/management/dashboard">Панель</a>'
        '<a href="/management/orders">Все заказы</a>'
        '<a href="/management/users">Сотрудники</a>'
        '<a href="/management/menu">Меню</a>'
        '<a href="/management/shifts">Смены</a>'
        '<a href="/management/reports">Отчеты</a>'
        f"{common}"
    )


def render_menu_rows():
    menu_items = fetch_menu_items(active_only=True)
    return f"""
    <div id="menu-items" class="grid">
        <div class="menu-row row-form" data-menu-row>
            <label>Поиск блюда
                <div class="dish-picker" data-dish-picker>
                    <input type="hidden" name="dish_query" data-dish-value>
                    <input type="text" data-dish-search placeholder="Начните вводить, например: Салат" autocomplete="off">
                    <div class="dish-dropdown" data-dish-dropdown></div>
                </div>
            </label>
            <label>Количество
                <input type="number" min="1" name="quantity" value="1" required>
            </label>
            <label>Цена за единицу
                <input type="number" step="0.01" min="0" name="price" value="0.00" readonly>
            </label>
            <label>Пожелания
                <input type="text" name="notes" placeholder="Без лука, горячее позже и т.д.">
            </label>
        </div>
    </div>
    <button type="button" class="secondary" id="add-menu-row">Добавить еще блюдо</button>
    <script>
        (function() {{
            const catalog = {json.dumps([{"name": dish["name"], "price": dish["price"]} for dish in menu_items], ensure_ascii=False)};
            const container = document.getElementById("menu-items");
            const addButton = document.getElementById("add-menu-row");

            function renderOptions(dropdown, query) {{
                const normalized = (query || "").trim().toLowerCase();
                const filtered = catalog.filter((dish) => dish.name.toLowerCase().includes(normalized)).slice(0, 8);
                if (!filtered.length) {{
                    dropdown.innerHTML = '<div class="dish-empty">Ничего не найдено</div>';
                    return;
                }}
                dropdown.innerHTML = filtered.map((dish) =>
                    `<button type="button" class="dish-option" data-dish-name="${{encodeURIComponent(dish.name)}}" data-dish-price="${{Number(dish.price).toFixed(2)}}">
                        <span>${{dish.name}}</span>
                        <strong>${{Number(dish.price).toFixed(2)}} ₽</strong>
                    </button>`
                ).join("");
            }}

            function attachRow(row) {{
                const picker = row.querySelector("[data-dish-picker]");
                const searchInput = row.querySelector("[data-dish-search]");
                const hiddenInput = row.querySelector("[data-dish-value]");
                const dropdown = row.querySelector("[data-dish-dropdown]");
                const priceInput = row.querySelector('input[name="price"]');

                function syncPrice(name) {{
                    const dish = catalog.find((item) => item.name === name.trim());
                    priceInput.value = dish ? Number(dish.price).toFixed(2) : "0.00";
                }}

                function selectDish(name) {{
                    hiddenInput.value = name;
                    searchInput.value = name;
                    syncPrice(name);
                    picker.classList.remove("open");
                }}

                function tryCommitTypedValue() {{
                    const exactDish = catalog.find((item) => item.name.toLowerCase() === searchInput.value.trim().toLowerCase());
                    if (exactDish) {{
                        selectDish(exactDish.name);
                    }}
                }}

                searchInput.addEventListener("focus", () => {{
                    picker.classList.add("open");
                    renderOptions(dropdown, searchInput.value);
                }});

                searchInput.addEventListener("input", () => {{
                    hiddenInput.value = "";
                    syncPrice("");
                    picker.classList.add("open");
                    renderOptions(dropdown, searchInput.value);
                }});

                searchInput.addEventListener("keydown", (event) => {{
                    if (event.key === "Enter") {{
                        const firstOption = dropdown.querySelector(".dish-option");
                        if (firstOption) {{
                            event.preventDefault();
                            selectDish(firstOption.dataset.dishName);
                        }}
                    }}
                    if (event.key === "Escape") {{
                        picker.classList.remove("open");
                    }}
                }});

                searchInput.addEventListener("blur", () => {{
                    window.setTimeout(tryCommitTypedValue, 120);
                }});

                dropdown.addEventListener("click", (event) => {{
                    const option = event.target.closest(".dish-option");
                    if (!option) return;
                    selectDish(decodeURIComponent(option.dataset.dishName));
                }});

                document.addEventListener("click", (event) => {{
                    if (!picker.contains(event.target)) {{
                        picker.classList.remove("open");
                    }}
                }});

                renderOptions(dropdown, "");
            }}

            addButton.addEventListener("click", () => {{
                const row = container.querySelector("[data-menu-row]").cloneNode(true);
                row.querySelectorAll("input").forEach((input) => {{
                    if (input.name === "quantity") input.value = "1";
                    else if (input.name === "price") input.value = "0.00";
                    else input.value = "";
                }});
                container.appendChild(row);
                attachRow(row);
            }});

            container.querySelectorAll("[data-menu-row]").forEach(attachRow);
        }})();
    </script>
    """


def render_page(title, content, user, flash=""):
    flash_html = f'<div class="flash">{escape(flash)}</div>' if flash else ""
    user_info = ""
    if user:
        roles = {
            WAITERS: "Официант",
            CHEFS: "Шеф-повар",
            SOUS_CHEFS: "Су-шеф",
            COOKS: "Повар",
            MANAGERS: "Руководство",
        }
        user_info = f"""
        <div class="user-bar">
            <div>
                <strong>{escape(user['full_name'])}</strong>
                <span>{roles.get(user['role'], user['role'])}</span>
            </div>
            <nav>{nav_for(user)}</nav>
        </div>
        """
    return f"""<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
        :root {{
            --bg: #f7f1e6;
            --paper: #fffaf1;
            --accent: #b85c38;
            --accent-soft: #f0d2bf;
            --text: #2a221c;
            --muted: #736459;
            --ok: #2f6f4f;
            --warn: #8b5e12;
            --danger: #8c2c2c;
            --line: #ddc9b5;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Georgia, "Times New Roman", serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(184, 92, 56, 0.14), transparent 26%),
                radial-gradient(circle at right center, rgba(104, 141, 115, 0.16), transparent 22%),
                linear-gradient(135deg, #f6efe1 0%, #efe2cf 100%);
            min-height: 100vh;
        }}
        .shell {{
            width: min(1180px, calc(100% - 32px));
            margin: 24px auto 40px;
        }}
        .user-bar {{
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: center;
            background: rgba(255, 250, 241, 0.9);
            backdrop-filter: blur(10px);
            padding: 18px 22px;
            border: 1px solid rgba(221, 201, 181, 0.85);
            border-radius: 20px;
            box-shadow: 0 12px 30px rgba(86, 63, 43, 0.08);
            margin-bottom: 18px;
        }}
        .user-bar nav {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .user-bar span {{
            display: block;
            color: var(--muted);
            font-size: 14px;
        }}
        a {{
            color: var(--accent);
            text-decoration: none;
            font-weight: 700;
        }}
        .card {{
            background: rgba(255, 250, 241, 0.92);
            border: 1px solid rgba(221, 201, 181, 0.9);
            border-radius: 24px;
            padding: 24px;
            box-shadow: 0 14px 36px rgba(72, 52, 35, 0.08);
            margin-bottom: 18px;
        }}
        h1, h2, h3 {{
            margin-top: 0;
            color: #5a2d1e;
        }}
        .hero {{
            padding: 34px 28px;
        }}
        .hero p {{
            color: var(--muted);
            max-width: 760px;
            line-height: 1.5;
        }}
        .grid {{
            display: grid;
            gap: 18px;
        }}
        .grid.two {{
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        }}
        .grid.three {{
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }}
        th, td {{
            text-align: left;
            padding: 12px 10px;
            border-bottom: 1px solid var(--line);
            vertical-align: top;
        }}
        th {{
            color: var(--muted);
            font-size: 14px;
        }}
        input, select, textarea, button {{
            width: 100%;
            padding: 11px 12px;
            border-radius: 14px;
            border: 1px solid #ceb7a2;
            background: #fffdf8;
            color: var(--text);
            font: inherit;
        }}
        textarea {{
            min-height: 92px;
            resize: vertical;
        }}
        button {{
            background: linear-gradient(135deg, #b85c38, #944224);
            color: white;
            border: none;
            font-weight: 700;
            cursor: pointer;
        }}
        .secondary {{
            background: linear-gradient(135deg, #6d876d, #4a654d);
        }}
        .danger {{
            background: linear-gradient(135deg, #944545, #6f2626);
        }}
        .flash {{
            margin-bottom: 18px;
            padding: 14px 16px;
            border-radius: 16px;
            background: #fbe5d8;
            border: 1px solid #ebc1ad;
        }}
        .pill {{
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            font-size: 13px;
            font-weight: 700;
        }}
        .metric {{
            border: 1px solid rgba(221, 201, 181, 0.95);
            border-radius: 18px;
            padding: 18px;
            background: #fffdf9;
        }}
        .metric strong {{
            display: block;
            font-size: 28px;
            margin-bottom: 8px;
            color: #5a2d1e;
        }}
        .row-form {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 10px;
            align-items: end;
        }}
        .dish-picker {{
            position: relative;
        }}
        .dish-picker input[data-dish-search] {{
            padding-right: 40px;
            background:
                linear-gradient(135deg, rgba(184, 92, 56, 0.05), rgba(109, 135, 109, 0.06)),
                #fffdf8;
        }}
        .dish-picker::after {{
            content: "▾";
            position: absolute;
            right: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--accent);
            pointer-events: none;
            font-size: 16px;
        }}
        .dish-dropdown {{
            position: absolute;
            top: calc(100% + 8px);
            left: 0;
            right: 0;
            display: none;
            background: #fffaf3;
            border: 1px solid #d8c1aa;
            border-radius: 18px;
            box-shadow: 0 16px 32px rgba(62, 44, 29, 0.18);
            padding: 8px;
            z-index: 20;
            max-height: 280px;
            overflow-y: auto;
        }}
        .dish-picker.open .dish-dropdown {{
            display: block;
        }}
        .dish-option {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            width: 100%;
            text-align: left;
            padding: 12px 14px;
            border-radius: 14px;
            border: none;
            background: transparent;
            color: var(--text);
            box-shadow: none;
        }}
        .dish-option:hover {{
            background: linear-gradient(135deg, #f5dfcf, #efe6d3);
        }}
        .dish-option span {{
            font-weight: 700;
        }}
        .dish-option strong {{
            color: var(--accent);
            white-space: nowrap;
            font-size: 14px;
        }}
        .dish-empty {{
            padding: 12px 14px;
            color: var(--muted);
            text-align: center;
        }}
        .small {{
            color: var(--muted);
            font-size: 14px;
        }}
        .status-new {{ color: #6d4d2f; }}
        .status-accepted {{ color: #355d7a; }}
        .status-preparing {{ color: var(--warn); }}
        .status-ready {{ color: var(--ok); }}
        .status-completed {{ color: var(--ok); }}
        .status-cancelled {{ color: var(--danger); }}
        .items-list {{
            margin: 0;
            padding-left: 18px;
        }}
        @media (max-width: 720px) {{
            .user-bar {{
                flex-direction: column;
                align-items: flex-start;
            }}
            .shell {{
                width: min(100% - 18px, 1180px);
            }}
            .card {{
                padding: 18px;
                border-radius: 18px;
            }}
        }}
    </style>
</head>
<body>
    <div class="shell">
        {user_info}
        {flash_html}
        {content}
    </div>
</body>
</html>"""


def login_page(message=""):
    content = """
    <section class="card hero">
        <h1>Синхронизация зала, кухни и руководства</h1>
        <p>
            Приложение разделяет доступ для официантов, поваров и руководства:
            официанты создают заказы, кухня берет их в работу и меняет статусы,
            а руководство получает общую отчетность и управляет учетными записями.
        </p>
    </section>
    <section class="card">
        <h2>Вход</h2>
        <form method="post" action="/login" class="grid">
            <label>
                Логин
                <input type="text" name="username" placeholder="manager">
            </label>
            <label>
                Пароль
                <input type="password" name="password" placeholder="manager123">
            </label>
            <button type="submit">Войти</button>
        </form>
    </section>
    """
    return render_page("Вход", content, None, message)


def waiter_orders_page(user, flash=""):
    conn = get_db()
    orders = conn.execute(
        """
        SELECT o.*, c.full_name AS chef_name
        FROM orders o
        LEFT JOIN users c ON c.id = o.chef_id
        WHERE o.waiter_id = ?
        ORDER BY o.created_at DESC, o.id DESC
        """,
        (user["id"],),
    ).fetchall()
    rows = []
    for order in orders:
        items = conn.execute(
            """
            SELECT oi.*, u.full_name AS cook_name
            FROM order_items oi
            LEFT JOIN users u ON u.id = oi.assigned_cook_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
            """,
            (order["id"],),
        ).fetchall()
        qty = sum(item["quantity"] for item in items)
        payment_label = {"cash": "Наличные", "card": "Карта"}.get(order["payment_method"], "Не указано")
        active_remaining = sum(1 for item in items if item["item_status"] not in ("served", "cancelled"))
        item_rows = []
        for item in items:
            serve_action = ""
            if item["item_status"] == "ready":
                serve_action = f"""
                <form method="post" action="/waiter/items/{item['id']}/serve">
                    <button type="submit" class="secondary">Выдать это блюдо</button>
                </form>
                """
            item_rows.append(
                f"""
                <li>
                    <strong>{escape(item['dish_name'])}</strong> x{item['quantity']} | {item['price_per_item']:.2f} ₽
                    <br><span class="small">Пожелания: {escape(item['notes']) or 'без пожеланий'} | Повар: {escape(item['cook_name'] or 'не назначен')} | Статус: {item_status_label(item['item_status'])}</span>
                    {serve_action}
                </li>
                """
            )
        payment_form = ""
        if active_remaining == 0 and order["status"] != "completed":
            payment_form = f"""
            <form method="post" action="/waiter/orders/{order['id']}/payment" class="row-form">
                <label>Оплата
                    <select name="payment_method" required>
                        <option value="">Выберите способ оплаты</option>
                        <option value="cash">Наличные</option>
                        <option value="card">Карта</option>
                    </select>
                </label>
                <button type="submit" class="secondary">Подтвердить оплату и завершить</button>
            </form>
            """
        rows.append(
            f"""
            <tr>
                <td>{escape(order['order_number'])}</td>
                <td><ul class="items-list">{''.join(item_rows)}</ul></td>
                <td>{qty}</td>
                <td>{escape(user['full_name'])}</td>
                <td>{human_dt(order['created_at'])}</td>
                <td>{escape(order['chef_name'] or 'Еще не принят')}</td>
                <td>{human_dt(order['issued_at'])}<br><span class="small">Оплата: {payment_label}</span></td>
                <td><span class="pill status-{escape(order['status'])}">{STATUS_LABELS[order['status']]}</span>{payment_form}</td>
            </tr>
            """
        )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Рабочее место официанта</h1>
        <p class="small">Официант видит только свои заказы и статусы кухни. Базы кухни и руководства ему недоступны.</p>
    </section>
    <div class="grid two">
        <section class="card">
            <h2>Новый заказ</h2>
            <form method="post" action="/waiter/orders/create" class="grid">
                {render_menu_rows()}
                <button type="submit">Отправить заказ на кухню</button>
            </form>
        </section>
        <section class="card">
            <h2>Правила статусов</h2>
            <table>
                <tr><th>Статус</th><th>Смысл</th></tr>
                <tr><td>Принят</td><td>Шеф-повар или су-шеф взял заказ в работу.</td></tr>
                <tr><td>Готовится</td><td>Блюда уже распределены по поварам, часть позиций может быть готова.</td></tr>
                <tr><td>Готов к выдаче</td><td>Все блюда по заказу готовы, но официант еще не вынес их полностью.</td></tr>
                <tr><td>Ожидает оплату</td><td>Все блюда выданы, официант должен выбрать способ оплаты.</td></tr>
                <tr><td>Выполнен</td><td>Все блюда выданы, оплата подтверждена, заказ закрыт.</td></tr>
                <tr><td>Отменен</td><td>Заказ отменен и отражен в отчетности.</td></tr>
            </table>
        </section>
    </div>
    <section class="card">
        <h2>Мои заказы</h2>
        <table>
            <tr>
                <th>Номер</th>
                <th>Позиции</th>
                <th>Количество блюд</th>
                <th>Официант</th>
                <th>Время создания</th>
                <th>Кто принял</th>
                <th>Время выдачи</th>
                <th>Статус</th>
            </tr>
            {"".join(rows) or '<tr><td colspan="8">Заказов пока нет.</td></tr>'}
        </table>
    </section>
    """
    return render_page("Официант", content, user, flash)


def kitchen_orders_page(user, flash=""):
    conn = get_db()
    assignable_staff = fetch_assignable_staff(conn, user)
    orders = conn.execute(
        """
        SELECT o.*, w.full_name AS waiter_name, c.full_name AS chef_name
        FROM orders o
        JOIN users w ON w.id = o.waiter_id
        LEFT JOIN users c ON c.id = o.chef_id
        WHERE o.status != 'completed'
        ORDER BY CASE o.status
            WHEN 'new' THEN 1
            WHEN 'accepted' THEN 2
            WHEN 'preparing' THEN 3
            WHEN 'ready' THEN 4
            WHEN 'cancelled' THEN 5
            ELSE 6 END,
            o.created_at ASC
        """
    ).fetchall()

    cards = []
    for order in orders:
        items = conn.execute(
            """
            SELECT oi.*, u.full_name AS cook_name
            FROM order_items oi
            LEFT JOIN users u ON u.id = oi.assigned_cook_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
            """,
            (order["id"],),
        ).fetchall()
        qty = sum(item["quantity"] for item in items)
        total = order_total(conn, order["id"])
        assigned_count = sum(1 for item in items if item["assigned_cook_id"])
        ready_count = sum(1 for item in items if item["item_status"] == "ready")
        served_count = sum(1 for item in items if item["item_status"] == "served")
        ready_dishes = [
            f"{item['dish_name']} x{item['quantity']}"
            for item in items
            if item["item_status"] == "ready"
        ]
        actions = ""
        if not order["chef_id"]:
            actions = f"""
            <form method="post" action="/kitchen/orders/{order['id']}/take">
                <button type="submit">Взять заказ</button>
            </form>
            """
        item_controls = []
        for item in items:
            assignment_html = ""
            if order["chef_id"] and order["status"] not in ("completed", "cancelled"):
                cook_options = '<option value="">Не назначен</option>' + "".join(
                    f'<option value="{worker["id"]}" {"selected" if worker["id"] == item["assigned_cook_id"] else ""}>{escape(worker["full_name"])} ({ "Су-шеф" if worker["role"] == SOUS_CHEFS else "Повар" })</option>'
                    for worker in assignable_staff
                )
                ready_button = ""
                if item["assigned_cook_id"] == user["id"] and item["item_status"] not in ("ready", "served", "cancelled"):
                    ready_button = f'<button type="submit" formaction="/kitchen/items/{item["id"]}/ready" formmethod="post" class="secondary">Отметить готовым</button>'
                assignment_html = f"""
                <div class="row-form">
                    <label>Повар<select name="cook_id_{item['id']}">{cook_options}</select></label>
                    {ready_button}
                </div>
                """
            item_controls.append(
                f"""
                <li>
                    <strong>{escape(item['dish_name'])}</strong> x{item['quantity']}
                    <br><span class="small">Пожелания: {escape(item['notes']) or 'без пожеланий'} | Повар: {escape(item['cook_name'] or 'не назначен')} | Статус: {item_status_label(item['item_status'])}</span>
                    {assignment_html}
                </li>
                """
            )
        cards.append(
            f"""
            <section class="card">
                <h3>{escape(order['order_number'])}</h3>
                <p class="small">Официант: {escape(order['waiter_name'])} | Создан: {human_dt(order['created_at'])}</p>
                <p><span class="pill status-{escape(order['status'])}">{STATUS_LABELS[order['status']]}</span></p>
                <p class="small">
                    Назначено: {assigned_count}/{len(items)} |
                    Готово: {ready_count}/{len(items)} |
                    Выдано: {served_count}/{len(items)}
                </p>
                <p class="small">Готовые блюда: {escape(', '.join(ready_dishes)) if ready_dishes else 'пока нет готовых позиций'}</p>
                <form method="post" action="/kitchen/orders/{order['id']}/assign-all" class="grid">
                    <ul class="items-list">{''.join(item_controls)}</ul>
                    {('<button type="submit" class="secondary">Сохранить назначения по всему заказу</button>' if order["chef_id"] and order["status"] not in ("completed", "cancelled") else '')}
                </form>
                <p class="small">Количество блюд: {qty} | Выручка по заказу: {total:.2f} ₽</p>
                <p class="small">Кто принял заказ: {escape(order['chef_name'] or 'Свободный заказ')}</p>
                {actions or ''}
            </section>
            """
        )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Панель шеф-повара и су-шефа</h1>
        <p class="small">Здесь можно брать заказы на координацию и назначать отдельного повара на каждое блюдо.</p>
    </section>
    <section class="grid two">
        {"".join(cards) or '<section class="card"><p>Активных заказов нет.</p></section>'}
    </section>
    """
    return render_page("Кухня", content, user, flash)


def cook_items_page(user, flash=""):
    conn = get_db()
    items = conn.execute(
        """
        SELECT oi.*, o.order_number, o.created_at, w.full_name AS waiter_name
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN users w ON w.id = o.waiter_id
        WHERE oi.assigned_cook_id = ? AND oi.item_status NOT IN ('served', 'cancelled')
        ORDER BY o.created_at ASC, oi.id ASC
        """,
        (user["id"],),
    ).fetchall()
    cards = []
    for item in items:
        action_html = ""
        if item["item_status"] != "ready":
            action_html = f"""
            <form method="post" action="/cook/items/{item['id']}/ready">
                <button type="submit">Отметить как готовое</button>
            </form>
            """
        cards.append(
            f"""
            <section class="card">
                <h3>{escape(item['order_number'])}</h3>
                <p class="small">Официант: {escape(item['waiter_name'])} | Создан: {human_dt(item['created_at'])}</p>
                <p><strong>{escape(item['dish_name'])}</strong> x{item['quantity']}</p>
                <p class="small">Пожелания: {escape(item['notes']) or 'без пожеланий'}</p>
                <p><span class="pill">{item_status_label(item['item_status'])}</span></p>
                {action_html or '<p class="small">Блюдо уже отмечено как готовое и ждет выдачи официантом.</p>'}
            </section>
            """
        )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Мои блюда</h1>
        <p class="small">Повар может только отмечать приготовленные блюда. Назначение выполняют шеф-повар и су-шеф.</p>
    </section>
    <section class="grid two">
        {"".join(cards) or '<section class="card"><p>Назначенных блюд пока нет.</p></section>'}
    </section>
    """
    return render_page("Мои блюда", content, user, flash)


def management_dashboard_page(user, flash=""):
    conn = get_db()
    metrics = conn.execute(
        """
        SELECT
            COUNT(*) AS total_orders,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_orders,
            SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN (
                SELECT COALESCE(SUM(quantity * price_per_item), 0)
                FROM order_items oi
                WHERE oi.order_id = orders.id
            ) ELSE 0 END), 0) AS revenue
        FROM orders
        """
    ).fetchone()
    users = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE active = 1").fetchone()["cnt"]
    latest_reports = conn.execute(
        """
        SELECT *
        FROM management_reports
        ORDER BY generated_at DESC
        LIMIT 10
        """
    ).fetchall()
    report_rows = "".join(
        f"""
        <tr>
            <td>{escape(report['order_number'])}</td>
            <td>{report['dishes_count']}</td>
            <td>{escape(report['dishes_summary'])}</td>
            <td>{human_dt(report['accepted_at'])}</td>
            <td>{human_dt(report['issued_at'])}</td>
            <td>{escape({'cash': 'Наличные', 'card': 'Карта'}.get(report['payment_method'], 'Не указано'))}</td>
            <td>{report['total_revenue']:.2f} ₽</td>
            <td>{STATUS_LABELS.get(report['status'], report['status'])}</td>
        </tr>
        """
        for report in latest_reports
    )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Панель руководства</h1>
        <p class="small">Руководство имеет полный доступ: аккаунты сотрудников, все заказы, корректировки и итоговую статистику.</p>
    </section>
    <section class="grid three">
        <div class="metric"><strong>{metrics['total_orders'] or 0}</strong>Всего заказов</div>
        <div class="metric"><strong>{metrics['completed_orders'] or 0}</strong>Выполненных заказов</div>
        <div class="metric"><strong>{metrics['cancelled_orders'] or 0}</strong>Отмененных заказов</div>
        <div class="metric"><strong>{metrics['revenue'] or 0:.2f} ₽</strong>Общая выручка</div>
        <div class="metric"><strong>{users}</strong>Активных сотрудников</div>
    </section>
    <section class="card">
        <h2>Последние автоматически сформированные строки отчета</h2>
        <table>
            <tr>
                <th>Номер заказа</th>
                <th>Кол-во блюд</th>
                <th>Название блюд</th>
                <th>Время принятия</th>
                <th>Время выдачи</th>
                <th>Оплата</th>
                <th>Выручка</th>
                <th>Статус</th>
            </tr>
            {report_rows or '<tr><td colspan="8">Отчетов пока нет.</td></tr>'}
        </table>
    </section>
    """
    return render_page("Панель руководства", content, user, flash)


def management_orders_page(user, flash="", query=None):
    conn = get_db()
    query = query or {}
    status_filter = (query.get("status", [""])[0] or "").strip()
    clauses = []
    params = []
    if status_filter:
        clauses.append("o.status = ?")
        params.append(status_filter)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    orders = conn.execute(
        f"""
        SELECT o.*, w.full_name AS waiter_name, c.full_name AS chef_name
        FROM orders o
        JOIN users w ON w.id = o.waiter_id
        LEFT JOIN users c ON c.id = o.chef_id
        {where}
        ORDER BY o.created_at DESC, o.id DESC
        """,
        params,
    ).fetchall()
    waiters = conn.execute("SELECT id, full_name FROM users WHERE role = 'waiter' AND active = 1 ORDER BY full_name").fetchall()
    chefs = conn.execute("SELECT id, full_name FROM users WHERE role IN ('chef', 'sous_chef') AND active = 1 ORDER BY full_name").fetchall()

    cards = []
    for order in orders:
        qty, _, items = order_items_summary(conn, order["id"])
        total = order_total(conn, order["id"])
        item_rows = "".join(
            f"<li>{escape(item['dish_name'])} x{item['quantity']} | {item['price_per_item']:.2f} ₽ | {escape(item['notes']) or 'без пожеланий'}</li>"
            for item in items
        )
        waiter_options = "".join(
            f'<option value="{w["id"]}" {"selected" if w["id"] == order["waiter_id"] else ""}>{escape(w["full_name"])}</option>'
            for w in waiters
        )
        chef_options = '<option value="">Не назначен</option>' + "".join(
            f'<option value="{c["id"]}" {"selected" if c["id"] == order["chef_id"] else ""}>{escape(c["full_name"])}</option>'
            for c in chefs
        )
        status_options = "".join(
            f'<option value="{key}" {"selected" if key == order["status"] else ""}>{value}</option>'
            for key, value in STATUS_LABELS.items()
        )
        cards.append(
            f"""
            <section class="card">
                <h3>{escape(order['order_number'])}</h3>
                <p class="small">Создан: {human_dt(order['created_at'])} | Принял: {escape(order['chef_name'] or '—')}</p>
                <ul class="items-list">{item_rows}</ul>
                <p class="small">Количество блюд: {qty} | Выручка: {total:.2f} ₽</p>
                <form method="post" action="/management/orders/{order['id']}/update" class="grid">
                    <div class="row-form">
                        <label>Официант<select name="waiter_id">{waiter_options}</select></label>
                        <label>Повар<select name="chef_id">{chef_options}</select></label>
                        <label>Статус<select name="status">{status_options}</select></label>
                    </div>
                    <div class="row-form">
                        <label>Время принятия<input type="text" name="accepted_at" value="{escape(order['accepted_at'] or '')}" placeholder="YYYY-MM-DD HH:MM:SS"></label>
                        <label>Время выдачи<input type="text" name="issued_at" value="{escape(order['issued_at'] or '')}" placeholder="YYYY-MM-DD HH:MM:SS"></label>
                    </div>
                    <button type="submit">Сохранить изменения</button>
                </form>
            </section>
            """
        )
    conn.close()

    status_options = '<option value="">Все статусы</option>' + "".join(
        f'<option value="{key}" {"selected" if key == status_filter else ""}>{value}</option>'
        for key, value in STATUS_LABELS.items()
    )
    content = f"""
    <section class="card">
        <h1>Все заказы</h1>
        <form method="get" action="/management/orders" class="row-form">
            <label>Фильтр по статусу<select name="status">{status_options}</select></label>
            <button type="submit" class="secondary">Показать</button>
        </form>
    </section>
    {''.join(cards) or '<section class="card"><p>Заказов нет.</p></section>'}
    """
    return render_page("Все заказы", content, user, flash)


def management_users_page(user, flash=""):
    conn = get_db()
    users = conn.execute(
        "SELECT * FROM users ORDER BY role, full_name"
    ).fetchall()
    cards = []
    for employee in users:
        stats = conn.execute(
            """
            SELECT
                COUNT(*) AS shifts_count,
                COALESCE(SUM(hours_worked), 0) AS hours_worked
            FROM work_sessions
            WHERE user_id = ?
            """,
            (employee["id"],),
        ).fetchone()
        if employee["role"] == WAITERS:
            order_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_orders,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
                    COALESCE(SUM(CASE WHEN status = 'completed' THEN (
                        SELECT COALESCE(SUM(quantity * price_per_item), 0)
                        FROM order_items oi
                        WHERE oi.order_id = orders.id
                    ) ELSE 0 END), 0) AS revenue
                FROM orders
                WHERE waiter_id = ?
                """,
                (employee["id"],),
            ).fetchone()
        elif employee["role"] in (CHEFS, SOUS_CHEFS):
            order_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_orders,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
                    COALESCE(SUM(CASE WHEN status = 'completed' THEN (
                        SELECT COALESCE(SUM(quantity * price_per_item), 0)
                        FROM order_items oi
                        WHERE oi.order_id = orders.id
                    ) ELSE 0 END), 0) AS revenue
                FROM orders
                WHERE chef_id = ?
                """,
                (employee["id"],),
            ).fetchone()
        elif employee["role"] == COOKS:
            order_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN item_status = 'ready' THEN 1 ELSE 0 END) AS completed_orders,
                    SUM(CASE WHEN item_status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
                    COALESCE(SUM(CASE WHEN item_status IN ('ready', 'served') THEN quantity * price_per_item ELSE 0 END), 0) AS revenue
                FROM order_items
                WHERE assigned_cook_id = ?
                """,
                (employee["id"],),
            ).fetchone()
        else:
            order_stats = {"total_orders": 0, "completed_orders": 0, "cancelled_orders": 0, "revenue": 0}
        salary = stats["hours_worked"] * employee["hourly_rate"] + employee["salary_adjustment"]
        role_options = "".join(
            f'<option value="{role}" {"selected" if role == employee["role"] else ""}>{label}</option>'
            for role, label in (
                (WAITERS, "Официант"),
                (CHEFS, "Шеф-повар"),
                (SOUS_CHEFS, "Су-шеф"),
                (COOKS, "Повар"),
                (MANAGERS, "Руководство"),
            )
        )
        cards.append(
            f"""
            <section class="card">
                <h3>{escape(employee['full_name'])}</h3>
                <p class="small">@{escape(employee['username'])} | {escape(employee['role'])} | {'активен' if employee['active'] else 'отключен'}</p>
                <p class="small">
                    Заказов: {order_stats['total_orders'] or 0},
                    выполнено: {order_stats['completed_orders'] or 0},
                    отменено: {order_stats['cancelled_orders'] or 0},
                    выручка: {order_stats['revenue'] or 0:.2f} ₽
                </p>
                <p class="small">
                    Часов: {stats['hours_worked'] or 0:.2f},
                    смен: {stats['shifts_count'] or 0},
                    текущая зарплата: {salary:.2f} ₽
                </p>
                <form method="post" action="/management/users/{employee['id']}/update" class="grid">
                    <div class="row-form">
                        <label>ФИО<input type="text" name="full_name" value="{escape(employee['full_name'])}" required></label>
                        <label>Логин<input type="text" name="username" value="{escape(employee['username'])}" required></label>
                        <label>Пароль<input type="text" name="password" value="{escape(employee['password'])}" required></label>
                    </div>
                    <div class="row-form">
                        <label>Роль<select name="role">{role_options}</select></label>
                        <label>Ставка в час<input type="number" min="0" step="0.01" name="hourly_rate" value="{employee['hourly_rate']}"></label>
                        <label>Корректировка зарплаты<input type="number" step="0.01" name="salary_adjustment" value="{employee['salary_adjustment']}"></label>
                        <label>Статус
                            <select name="active">
                                <option value="1" {"selected" if employee['active'] else ""}>Активен</option>
                                <option value="0" {"selected" if not employee['active'] else ""}>Отключен</option>
                            </select>
                        </label>
                    </div>
                    <button type="submit">Сохранить сотрудника</button>
                </form>
            </section>
            """
        )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Учетные записи и персональная статистика</h1>
        <p class="small">Руководство может создавать и изменять любые учетные записи, а также влиять на расчет зарплаты.</p>
        <form method="post" action="/management/users/create" class="grid">
            <h2>Создать нового сотрудника</h2>
            <div class="row-form">
                <label>ФИО<input type="text" name="full_name" required></label>
                <label>Логин<input type="text" name="username" required></label>
                <label>Пароль<input type="text" name="password" required></label>
            </div>
            <div class="row-form">
                <label>Роль
                    <select name="role">
                        <option value="waiter">Официант</option>
                        <option value="chef">Шеф-повар</option>
                        <option value="sous_chef">Су-шеф</option>
                        <option value="cook">Повар</option>
                        <option value="manager">Руководство</option>
                    </select>
                </label>
                <label>Ставка в час<input type="number" step="0.01" min="0" name="hourly_rate" value="0"></label>
                <label>Корректировка зарплаты<input type="number" step="0.01" name="salary_adjustment" value="0"></label>
            </div>
            <button type="submit">Создать учетную запись</button>
        </form>
    </section>
    {''.join(cards)}
    """
    return render_page("Сотрудники", content, user, flash)


def management_menu_page(user, flash=""):
    menu_items = fetch_menu_items(active_only=False)
    rows = "".join(
        f"""
        <tr>
            <td>{escape(item['name'])}</td>
            <td>{item['price']:.2f} ₽</td>
            <td>{'Активно' if item['active'] else 'Скрыто'}</td>
            <td>
                <form method="post" action="/management/menu/{item['id']}/update" class="row-form">
                    <label>Название<input type="text" name="name" value="{escape(item['name'])}" required></label>
                    <label>Цена<input type="number" step="0.01" min="0" name="price" value="{item['price']:.2f}" required></label>
                    <label>Статус
                        <select name="active">
                            <option value="1" {"selected" if item['active'] else ""}>Активно</option>
                            <option value="0" {"selected" if not item['active'] else ""}>Скрыто</option>
                        </select>
                    </label>
                    <button type="submit">Сохранить блюдо</button>
                </form>
            </td>
        </tr>
        """
        for item in menu_items
    )
    content = f"""
    <section class="card">
        <h1>Справочник блюд</h1>
        <p class="small">Официанты выбирают блюда только из этого списка. Цена подтягивается автоматически из меню.</p>
        <form method="post" action="/management/menu/create" class="row-form">
            <label>Название блюда<input type="text" name="name" required></label>
            <label>Цена<input type="number" step="0.01" min="0" name="price" value="0.00" required></label>
            <button type="submit">Добавить блюдо</button>
        </form>
    </section>
    <section class="card">
        <table>
            <tr>
                <th>Блюдо</th>
                <th>Цена</th>
                <th>Статус</th>
                <th>Управление</th>
            </tr>
            {rows or '<tr><td colspan="4">В меню пока нет блюд.</td></tr>'}
        </table>
    </section>
    """
    return render_page("Меню", content, user, flash)


def management_shifts_page(user, flash=""):
    conn = get_db()
    employees = conn.execute(
        "SELECT id, full_name, role FROM users WHERE active = 1 ORDER BY full_name"
    ).fetchall()
    sessions = conn.execute(
        """
        SELECT ws.*, u.full_name, u.role
        FROM work_sessions ws
        JOIN users u ON u.id = ws.user_id
        ORDER BY ws.shift_date DESC, ws.id DESC
        """
    ).fetchall()
    employee_options = "".join(
        f'<option value="{emp["id"]}">{escape(emp["full_name"])} ({escape(emp["role"])})</option>'
        for emp in employees
    )
    rows = "".join(
        f"""
        <tr>
            <td>{escape(row['full_name'])}</td>
            <td>{escape(row['role'])}</td>
            <td>{escape(row['shift_date'])}</td>
            <td>{row['hours_worked']:.2f}</td>
            <td>{escape(row['notes'])}</td>
        </tr>
        """
        for row in sessions
    )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Учет смен и времени работы</h1>
        <form method="post" action="/management/shifts/create" class="grid">
            <div class="row-form">
                <label>Сотрудник<select name="user_id">{employee_options}</select></label>
                <label>Дата смены<input type="date" name="shift_date" required></label>
                <label>Часы<input type="number" step="0.25" min="0" name="hours_worked" value="8" required></label>
            </div>
            <label>Комментарий<textarea name="notes" placeholder="Например: вечерняя смена"></textarea></label>
            <button type="submit">Добавить смену</button>
        </form>
    </section>
    <section class="card">
        <h2>История смен</h2>
        <table>
            <tr>
                <th>Сотрудник</th>
                <th>Роль</th>
                <th>Дата</th>
                <th>Часы</th>
                <th>Комментарий</th>
            </tr>
            {rows or '<tr><td colspan="5">Смен пока нет.</td></tr>'}
        </table>
    </section>
    """
    return render_page("Смены", content, user, flash)


def management_reports_page(user, flash="", query=None):
    conn = get_db()
    query = query or {}
    date_from = (query.get("date_from", [""])[0] or "").strip()
    date_to = (query.get("date_to", [""])[0] or "").strip()
    clauses = []
    params = []
    if date_from:
        clauses.append("date(generated_at) >= date(?)")
        params.append(date_from)
    if date_to:
        clauses.append("date(generated_at) <= date(?)")
        params.append(date_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    reports = conn.execute(
        f"""
        SELECT *
        FROM management_reports
        {where}
        ORDER BY generated_at DESC
        """,
        params,
    ).fetchall()
    summary = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN status = 'completed' THEN total_revenue ELSE 0 END), 0) AS revenue,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_orders,
            SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders
        FROM management_reports
        {where}
        """,
        params,
    ).fetchone()
    rows = "".join(
        f"""
        <tr>
            <td>{escape(report['order_number'])}</td>
            <td>{report['dishes_count']}</td>
            <td>{escape(report['dishes_summary'])}</td>
            <td>{human_dt(report['accepted_at'])}</td>
            <td>{human_dt(report['issued_at'])}</td>
            <td>{escape({'cash': 'Наличные', 'card': 'Карта'}.get(report['payment_method'], 'Не указано'))}</td>
            <td>{report['total_revenue']:.2f} ₽</td>
            <td>{STATUS_LABELS.get(report['status'], report['status'])}</td>
            <td>{human_dt(report['generated_at'])}</td>
        </tr>
        """
        for report in reports
    )
    conn.close()

    content = f"""
    <section class="card">
        <h1>Отчет за период</h1>
        <form method="get" action="/management/reports" class="row-form">
            <label>С<input type="date" name="date_from" value="{escape(date_from)}"></label>
            <label>По<input type="date" name="date_to" value="{escape(date_to)}"></label>
            <button type="submit" class="secondary">Построить отчет</button>
        </form>
    </section>
    <section class="grid three">
        <div class="metric"><strong>{summary['revenue'] or 0:.2f} ₽</strong>Общая выручка</div>
        <div class="metric"><strong>{summary['completed_orders'] or 0}</strong>Выполненных заказов</div>
        <div class="metric"><strong>{summary['cancelled_orders'] or 0}</strong>Отмененных заказов</div>
    </section>
    <section class="card">
        <h2>Строки отчета</h2>
        <table>
            <tr>
                <th>Номер заказа</th>
                <th>Кол-во блюд</th>
                <th>Название блюд</th>
                <th>Время принятия</th>
                <th>Время выдачи</th>
                <th>Оплата</th>
                <th>Выручка</th>
                <th>Статус</th>
                <th>Сформирован</th>
            </tr>
            {rows or '<tr><td colspan="9">За выбранный период данных нет.</td></tr>'}
        </table>
    </section>
    """
    return render_page("Отчеты", content, user, flash)


def create_order(user, data):
    conn = get_db()
    cur = conn.cursor()
    menu_lookup = {
        row["name"]: row["price"]
        for row in conn.execute("SELECT name, price FROM menu_items WHERE active = 1").fetchall()
    }
    order_number = next_order_number(conn)
    created_at = now_iso()
    cur.execute(
        """
        INSERT INTO orders (order_number, waiter_id, created_at, status, updated_at)
        VALUES (?, ?, ?, 'new', ?)
        """,
        (order_number, user["id"], created_at, created_at),
    )
    order_id = cur.lastrowid
    added_items = 0
    dish_names = data.get("dish_query", [])
    quantities = data.get("quantity", [])
    prices = data.get("price", [])
    notes_list = data.get("notes", [])
    for idx, raw_name in enumerate(dish_names):
        dish_name = (raw_name or "").strip()
        if not dish_name:
            continue
        quantity_raw = quantities[idx] if idx < len(quantities) else "1"
        notes = (notes_list[idx] if idx < len(notes_list) else "").strip()
        posted_price = prices[idx] if idx < len(prices) else "0"
        matched_name, matched_price = resolve_menu_item(menu_lookup, dish_name)
        if not matched_name:
            continue
        try:
            quantity = max(1, int(quantity_raw or 1))
        except ValueError:
            quantity = 1
        price = max(0.0, to_number(posted_price, matched_price))
        cur.execute(
            """
            INSERT INTO order_items (order_id, dish_name, quantity, notes, price_per_item)
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, matched_name, quantity, notes, price),
        )
        added_items += 1
    if not added_items:
        conn.rollback()
        conn.close()
        return "Нужно заполнить хотя бы одно блюдо."
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return f"Заказ {order_number} отправлен на кухню."


def assign_item_to_cook(user, item_id, cook_id):
    conn = get_db()
    item = conn.execute(
        """
        SELECT oi.*, o.chef_id, o.id AS order_id
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.id = ?
        """,
        (item_id,),
    ).fetchone()
    if not item:
        conn.close()
        return "Блюдо не найдено."
    if not item["chef_id"]:
        conn.close()
        return "Сначала заказ должен принять шеф-повар или су-шеф."
    allowed_ids = {row["id"] for row in fetch_assignable_staff(conn, user)}
    if cook_id is not None and cook_id not in allowed_ids:
        conn.close()
        return "Нельзя назначить этого сотрудника на блюдо."
    status = "assigned" if cook_id else "new"
    conn.execute(
        """
        UPDATE order_items
        SET assigned_cook_id = ?, item_status = ?, ready_at = CASE WHEN ? = 'ready' THEN ready_at ELSE NULL END, served_at = CASE WHEN ? = 'served' THEN served_at ELSE NULL END
        WHERE id = ?
        """,
        (cook_id, status, status, status, item_id),
    )
    recalculate_order_state(conn, item["order_id"])
    sync_management_report(conn, item["order_id"])
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Повар назначен на блюдо."


def assign_order_items_bulk(user, order_id, data):
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if not order:
        conn.close()
        return "Заказ не найден."
    if not order["chef_id"]:
        conn.close()
        return "Сначала заказ должен принять шеф-повар или су-шеф."
    allowed_ids = {row["id"] for row in fetch_assignable_staff(conn, user)}
    items = conn.execute(
        "SELECT id FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchall()
    for item in items:
        raw_value = (data.get(f"cook_id_{item['id']}", [""])[0] or "").strip()
        cook_id = int(raw_value) if raw_value else None
        if cook_id is not None and cook_id not in allowed_ids:
            conn.close()
            return "В списке назначений есть недопустимый сотрудник."
        status = "assigned" if cook_id else "new"
        conn.execute(
            """
            UPDATE order_items
            SET assigned_cook_id = ?, item_status = CASE
                WHEN item_status IN ('ready', 'served', 'cancelled') THEN item_status
                ELSE ?
            END
            WHERE id = ?
            """,
            (cook_id, status, item["id"]),
        )
    recalculate_order_state(conn, order_id)
    sync_management_report(conn, order_id)
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Назначения по всем блюдам сохранены."


def mark_cook_item_ready(user, item_id):
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM order_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item:
        conn.close()
        return "Блюдо не найдено."
    if item["assigned_cook_id"] != user["id"]:
        conn.close()
        return "Можно отмечать только свои блюда."
    conn.execute(
        """
        UPDATE order_items
        SET item_status = 'ready', ready_at = ?
        WHERE id = ?
        """,
        (now_iso(), item_id),
    )
    recalculate_order_state(conn, item["order_id"])
    sync_management_report(conn, item["order_id"])
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Блюдо отмечено как готовое."


def serve_waiter_item(user, item_id):
    conn = get_db()
    item = conn.execute(
        """
        SELECT oi.*, o.waiter_id, o.id AS order_id
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.id = ?
        """,
        (item_id,),
    ).fetchone()
    if not item:
        conn.close()
        return "Блюдо не найдено."
    if item["waiter_id"] != user["id"]:
        conn.close()
        return "Выдавать можно только блюда из своих заказов."
    if item["item_status"] != "ready":
        conn.close()
        return "Выдать можно только готовое блюдо."
    served_at = now_iso()
    conn.execute(
        """
        UPDATE order_items
        SET item_status = 'served', served_at = ?
        WHERE id = ?
        """,
        (served_at, item_id),
    )
    recalculate_order_state(conn, item["order_id"])
    sync_management_report(conn, item["order_id"])
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Блюдо отмечено как выданное клиенту."


def complete_waiter_order(user, order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return "Заказ не найден."
    if order["waiter_id"] != user["id"]:
        conn.close()
        return "Можно завершать только свои заказы."
    if order["status"] != "ready":
        conn.close()
        return "Завершить можно только заказ со статусом 'Готов к выдаче'."
    issued_at = now_iso()
    conn.execute(
        """
        UPDATE orders
        SET status = 'completed', issued_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (issued_at, issued_at, order_id),
    )
    sync_management_report(conn, order_id)
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Заказ выдан клиенту и отмечен как выполненный."


def finalize_order_payment(user, order_id, payment_method):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return "Заказ не найден."
    if order["waiter_id"] != user["id"]:
        conn.close()
        return "Можно завершать только свои заказы."
    if payment_method not in ("cash", "card"):
        conn.close()
        return "Нужно выбрать способ оплаты."
    active_items = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM order_items
        WHERE order_id = ? AND item_status != 'cancelled' AND item_status != 'served'
        """,
        (order_id,),
    ).fetchone()["cnt"]
    if active_items:
        conn.close()
        return "Сначала нужно выдать все блюда по заказу."
    paid_at = now_iso()
    conn.execute(
        """
        UPDATE orders
        SET payment_method = ?, paid_at = ?, status = 'completed', updated_at = ?
        WHERE id = ?
        """,
        (payment_method, paid_at, paid_at, order_id),
    )
    sync_management_report(conn, order_id)
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Оплата зафиксирована, заказ завершен."


def update_order_status_by_chef(user, order_id, new_status):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return "Заказ не найден."
    if order["chef_id"] != user["id"]:
        conn.close()
        return "Изменять статус может только назначенный повар."
    if new_status == "completed":
        conn.close()
        return "Статус 'Выполнен' ставит официант после выдачи клиенту."
    accepted_at = order["accepted_at"]
    issued_at = order["issued_at"]
    if new_status in ("accepted", "preparing") and not accepted_at:
        accepted_at = now_iso()
    if new_status in ("completed", "ready") and not accepted_at:
        accepted_at = now_iso()
    if new_status == "completed":
        issued_at = now_iso()
    if new_status == "cancelled":
        issued_at = order["issued_at"]
    conn.execute(
        """
        UPDATE orders
        SET status = ?, accepted_at = ?, issued_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, accepted_at, issued_at, now_iso(), order_id),
    )
    sync_management_report(conn, order_id)
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Статус заказа обновлен."


def manager_update_order(order_id, data):
    conn = get_db()
    chef_id = (data.get("chef_id", [""])[0] or "").strip()
    accepted_at = (data.get("accepted_at", [""])[0] or "").strip() or None
    issued_at = (data.get("issued_at", [""])[0] or "").strip() or None
    status = (data.get("status", ["new"])[0] or "new").strip()
    waiter_id = int(data.get("waiter_id", ["0"])[0])
    chef_value = int(chef_id) if chef_id else None
    if status in ("accepted", "preparing", "ready", "completed") and not accepted_at:
        accepted_at = now_iso()
    if status == "completed" and not issued_at:
        issued_at = now_iso()
    conn.execute(
        """
        UPDATE orders
        SET waiter_id = ?, chef_id = ?, status = ?, accepted_at = ?, issued_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (waiter_id, chef_value, status, accepted_at, issued_at, now_iso(), order_id),
    )
    sync_management_report(conn, order_id)
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Заказ обновлен руководством."


def create_user(data):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO users (username, password, full_name, role, hourly_rate, salary_adjustment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (data.get("username", [""])[0] or "").strip(),
                (data.get("password", [""])[0] or "").strip(),
                (data.get("full_name", [""])[0] or "").strip(),
                (data.get("role", ["waiter"])[0] or "waiter").strip(),
                max(0.0, to_number(data.get("hourly_rate", ["0"])[0])),
                to_number(data.get("salary_adjustment", ["0"])[0]),
                now_iso(),
            ),
        )
        conn.commit()
        message = "Сотрудник создан."
    except sqlite3.IntegrityError:
        message = "Пользователь с таким логином уже существует."
    finally:
        conn.close()
    if message == "Сотрудник создан.":
        save_portable_snapshot()
    return message


def create_menu_item(data):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO menu_items (name, price, active, created_at)
            VALUES (?, ?, 1, ?)
            """,
            (
                (data.get("name", [""])[0] or "").strip(),
                max(0.0, to_number(data.get("price", ["0"])[0])),
                now_iso(),
            ),
        )
        conn.commit()
        message = "Блюдо добавлено в меню."
    except sqlite3.IntegrityError:
        message = "Такое блюдо уже есть в меню."
    finally:
        conn.close()
    if message == "Блюдо добавлено в меню.":
        save_portable_snapshot()
    return message


def update_menu_item(menu_item_id, data):
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE menu_items
            SET name = ?, price = ?, active = ?
            WHERE id = ?
            """,
            (
                (data.get("name", [""])[0] or "").strip(),
                max(0.0, to_number(data.get("price", ["0"])[0])),
                int(data.get("active", ["1"])[0]),
                menu_item_id,
            ),
        )
        conn.commit()
        message = "Блюдо обновлено."
    except sqlite3.IntegrityError:
        message = "Не удалось сохранить блюдо: такое название уже используется."
    finally:
        conn.close()
    if message == "Блюдо обновлено.":
        save_portable_snapshot()
    return message


def update_user(user_id, data):
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE users
            SET full_name = ?, username = ?, password = ?, role = ?,
                hourly_rate = ?, salary_adjustment = ?, active = ?
            WHERE id = ?
            """,
            (
                (data.get("full_name", [""])[0] or "").strip(),
                (data.get("username", [""])[0] or "").strip(),
                (data.get("password", [""])[0] or "").strip(),
                (data.get("role", ["waiter"])[0] or "waiter").strip(),
                max(0.0, to_number(data.get("hourly_rate", ["0"])[0])),
                to_number(data.get("salary_adjustment", ["0"])[0]),
                int(data.get("active", ["1"])[0]),
                user_id,
            ),
        )
        conn.commit()
        message = "Данные сотрудника обновлены."
    except sqlite3.IntegrityError:
        message = "Не удалось сохранить: логин уже занят."
    finally:
        conn.close()
    if message == "Данные сотрудника обновлены.":
        save_portable_snapshot()
    return message


def create_shift(data):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO work_sessions (user_id, shift_date, hours_worked, notes, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(data.get("user_id", ["0"])[0]),
            (data.get("shift_date", [""])[0] or "").strip(),
            max(0.0, to_number(data.get("hours_worked", ["0"])[0])),
            (data.get("notes", [""])[0] or "").strip(),
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    save_portable_snapshot()
    return "Смена добавлена."


def app(environ, start_response):
    init_db()
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    user = get_current_user(environ)
    query = parse_qs(environ.get("QUERY_STRING", ""))

    if path == "/":
        if not user:
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [login_page().encode("utf-8")]
        home = {
            WAITERS: "/waiter/orders",
            CHEFS: "/kitchen/orders",
            SOUS_CHEFS: "/kitchen/orders",
            COOKS: "/cook/items",
            MANAGERS: "/management/dashboard",
        }[user["role"]]
        return redirect(start_response, home)

    if path == "/login":
        if method == "GET":
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [login_page().encode("utf-8")]
        data = parse_post_data(environ)
        authed = fetch_user_by_credentials(
            (data.get("username", [""])[0] or "").strip(),
            (data.get("password", [""])[0] or "").strip(),
        )
        if not authed:
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [login_page("Неверный логин или пароль.").encode("utf-8")]
        token = create_session(authed["id"])
        cookie_header = f"session_id={token}; Path=/; HttpOnly"
        target = {
            WAITERS: "/waiter/orders",
            CHEFS: "/kitchen/orders",
            SOUS_CHEFS: "/kitchen/orders",
            COOKS: "/cook/items",
            MANAGERS: "/management/dashboard",
        }[authed["role"]]
        return redirect(start_response, target, cookie_header)

    if path == "/logout":
        clear_session(environ)
        return redirect(start_response, "/login", "session_id=; Path=/; Max-Age=0")

    if path == "/waiter/orders":
        if not require_role(user, [WAITERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [waiter_orders_page(user).encode("utf-8")]

    if path == "/waiter/orders/create" and method == "POST":
        if not require_role(user, [WAITERS]):
            return forbidden(start_response)
        flash = create_order(user, parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [waiter_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/waiter/orders/") and path.endswith("/complete") and method == "POST":
        if not require_role(user, [WAITERS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        flash = complete_waiter_order(user, order_id)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [waiter_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/waiter/items/") and path.endswith("/serve") and method == "POST":
        if not require_role(user, [WAITERS]):
            return forbidden(start_response)
        item_id = int(path.split("/")[3])
        flash = serve_waiter_item(user, item_id)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [waiter_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/waiter/orders/") and path.endswith("/payment") and method == "POST":
        if not require_role(user, [WAITERS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        data = parse_post_data(environ)
        flash = finalize_order_payment(user, order_id, (data.get("payment_method", [""])[0] or "").strip())
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [waiter_orders_page(user, flash).encode("utf-8")]

    if path == "/kitchen/orders":
        if not require_role(user, [CHEFS, SOUS_CHEFS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user).encode("utf-8")]

    if path.startswith("/kitchen/orders/") and path.endswith("/take") and method == "POST":
        if not require_role(user, [CHEFS, SOUS_CHEFS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        conn = get_db()
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order and not order["chef_id"]:
            accepted_at = now_iso()
            conn.execute(
                """
                UPDATE orders
                SET chef_id = ?, status = 'accepted', accepted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (user["id"], accepted_at, accepted_at, order_id),
            )
            recalculate_order_state(conn, order_id)
            sync_management_report(conn, order_id)
            conn.commit()
            save_portable_snapshot()
            flash = "Заказ закреплен за вами."
        else:
            flash = "Этот заказ уже принят другим поваром."
        conn.close()
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/kitchen/orders/") and path.endswith("/status") and method == "POST":
        if not require_role(user, [CHEFS, SOUS_CHEFS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        data = parse_post_data(environ)
        flash = update_order_status_by_chef(user, order_id, (data.get("status", ["accepted"])[0] or "accepted").strip())
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/kitchen/items/") and path.endswith("/assign") and method == "POST":
        if not require_role(user, [CHEFS, SOUS_CHEFS]):
            return forbidden(start_response)
        item_id = int(path.split("/")[3])
        data = parse_post_data(environ)
        raw_cook_id = (data.get("cook_id", [""])[0] or "").strip()
        cook_id = int(raw_cook_id) if raw_cook_id else None
        flash = assign_item_to_cook(user, item_id, cook_id)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/kitchen/items/") and path.endswith("/ready") and method == "POST":
        if not require_role(user, [SOUS_CHEFS]):
            return forbidden(start_response)
        item_id = int(path.split("/")[3])
        flash = mark_cook_item_ready(user, item_id)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user, flash).encode("utf-8")]

    if path.startswith("/kitchen/orders/") and path.endswith("/assign-all") and method == "POST":
        if not require_role(user, [CHEFS, SOUS_CHEFS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        flash = assign_order_items_bulk(user, order_id, parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [kitchen_orders_page(user, flash).encode("utf-8")]

    if path == "/cook/items":
        if not require_role(user, [COOKS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [cook_items_page(user).encode("utf-8")]

    if path.startswith("/cook/items/") and path.endswith("/ready") and method == "POST":
        if not require_role(user, [COOKS]):
            return forbidden(start_response)
        item_id = int(path.split("/")[3])
        flash = mark_cook_item_ready(user, item_id)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [cook_items_page(user, flash).encode("utf-8")]

    if path == "/management/dashboard":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_dashboard_page(user).encode("utf-8")]

    if path == "/management/orders":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_orders_page(user, query=query).encode("utf-8")]

    if path.startswith("/management/orders/") and path.endswith("/update") and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        order_id = int(path.split("/")[3])
        flash = manager_update_order(order_id, parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_orders_page(user, flash=flash).encode("utf-8")]

    if path == "/management/users":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_users_page(user).encode("utf-8")]

    if path == "/management/users/create" and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        flash = create_user(parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_users_page(user, flash).encode("utf-8")]

    if path.startswith("/management/users/") and path.endswith("/update") and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        user_id = int(path.split("/")[3])
        flash = update_user(user_id, parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_users_page(user, flash).encode("utf-8")]

    if path == "/management/menu":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_menu_page(user).encode("utf-8")]

    if path == "/management/menu/create" and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        flash = create_menu_item(parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_menu_page(user, flash).encode("utf-8")]

    if path.startswith("/management/menu/") and path.endswith("/update") and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        menu_item_id = int(path.split("/")[3])
        flash = update_menu_item(menu_item_id, parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_menu_page(user, flash).encode("utf-8")]

    if path == "/management/shifts":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_shifts_page(user).encode("utf-8")]

    if path == "/management/shifts/create" and method == "POST":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        flash = create_shift(parse_post_data(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_shifts_page(user, flash).encode("utf-8")]

    if path == "/management/reports":
        if not require_role(user, [MANAGERS]):
            return forbidden(start_response)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [management_reports_page(user, query=query).encode("utf-8")]

    return not_found(start_response)


if __name__ == "__main__":
    init_db()
    host = "127.0.0.1"
    port = 8000
    print(f"Server running at http://{host}:{port}")
    with make_server(host, port, app) as server:
        server.serve_forever()
