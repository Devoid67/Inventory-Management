"""Simple database class for the Smart Inventory Qt app.

- One class owns the SQLite connection and all SQL.
- Uses the existing database file as-is; never resets or recreates it.
- Sales additions are purely additive (new columns / sale_items table).
- Legacy sales without item rows are kept as-is and reported as legacy.
- All queries use parameterized SQL (? placeholders).
"""
import sqlite3
from pathlib import Path

GST_RATE = 18.0
PAYMENT_METHODS = ("Cash", "UPI", "Card", "Other")


def bill_number(sale_id):
    """Stable human-readable bill number derived from the sale ID."""
    return f"BILL-{int(sale_id):06d}"


class Database:
    """Owns database/inventory.db and every SQL query."""

    def __init__(self, db_path="database/inventory.db"):
        # Relative to the project working directory (D:\FinalQTProject).
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Safe: only creates missing tables, never deletes data.
        self.create_product_table()
        self.create_sales_table()
        self.ensure_sales_schema()

    # ---------------- products ----------------
    def get_products(self):
        """Products for POS cards: id, name, category, price, stock."""
        self.cursor.execute("""
            SELECT id, product_name, category, selling_price, stock
            FROM products
            ORDER BY product_name
        """)
        return [
            {"id": r[0], "name": r[1], "category": r[2],
             "price": r[3], "stock": r[4]}
            for r in self.cursor.fetchall()
        ]

    # Backward-compatible alias used by older modules.
    def get_all_products(self):
        return self.get_products()

    def get_product_stock(self, product_id):
        self.cursor.execute("SELECT stock FROM products WHERE id = ?", (int(product_id),))
        row = self.cursor.fetchone()
        return int(row[0]) if row else None

    # ---------------- sales schema (additive only) ----------------
    def ensure_sales_schema(self):
        """Add nullable sales columns + sale_items table if missing (no data loss)."""
        cols = {row[1] for row in self.cursor.execute("PRAGMA table_info(sales)").fetchall()}
        if "customer_id" not in cols:
            self.cursor.execute("ALTER TABLE sales ADD COLUMN customer_id INTEGER")
            self.conn.commit()
        if "subtotal" not in cols:
            self.cursor.execute("ALTER TABLE sales ADD COLUMN subtotal REAL")
            self.conn.commit()
        if "discount" not in cols:
            self.cursor.execute("ALTER TABLE sales ADD COLUMN discount REAL DEFAULT 0")
            self.conn.commit()
        if "gst_amount" not in cols:
            self.cursor.execute("ALTER TABLE sales ADD COLUMN gst_amount REAL")
            self.conn.commit()
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS sale_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                product_name TEXT NOT NULL,
                sku TEXT,
                unit_price REAL NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                gst_rate REAL DEFAULT 18,
                line_total REAL NOT NULL
            )
        """)
        self.conn.commit()
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id)")
        self.conn.commit()

    # ---------------- checkout (single transaction) ----------------
    @staticmethod
    def calculate_totals(cart, gst_rate=GST_RATE):
        """One consistent calculation: discount is disabled, always 0."""
        subtotal = round(sum(float(i["price"]) * int(i["qty"]) for i in cart.values()), 2)
        gst = round(subtotal * float(gst_rate) / 100.0, 2)
        return {"subtotal": subtotal, "discount": 0.0,
                "gst": gst, "total": round(subtotal + gst, 2)}

    def checkout(self, cart, customer, payment_method, gst_rate=GST_RATE):
        """Save sale + items + stock deduction atomically. Returns (sale_id, totals).

        Raises ValueError with a user-facing message on any problem;
        the transaction is rolled back so no partial records remain.
        """
        if not cart:
            raise ValueError("Cart is empty.")
        if payment_method not in PAYMENT_METHODS:
            raise ValueError(f"Invalid payment method: {payment_method!r}.")
        customer = (customer or "").strip() or "Walk-in Customer"
        totals = self.calculate_totals(cart, gst_rate=gst_rate)
        # Snapshot + validate against live stock (also catches price drift).
        lines = []
        for item in cart.values():
            pid, qty = int(item["id"]), item["qty"]
            if not isinstance(qty, int) or qty <= 0:
                raise ValueError(f"Invalid quantity for {item.get('name', '?')}.")
            row = self.cursor.execute(
                "SELECT product_name, sku, selling_price, stock FROM products WHERE id = ?",
                (pid,)).fetchone()
            if row is None:
                raise ValueError(f"Product ID {pid} no longer exists.")
            name, sku, db_price, db_stock = row[0], row[1], float(row[2]), int(row[3])
            if abs(db_price - float(item["price"])) > 0.005:
                raise ValueError(
                    f"Price changed for {name}. Please reload products and rebuild the cart.")
            if db_stock < qty:
                raise ValueError(f"Insufficient stock for {name}: have {db_stock}, need {qty}.")
            lines.append({"product_id": pid, "product_name": name, "sku": sku,
                          "unit_price": round(db_price, 2), "quantity": qty,
                          "gst_rate": float(gst_rate),
                          "line_total": round(db_price * qty, 2)})
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.cursor.execute("""
                INSERT INTO sales
                (customer, payment_method, subtotal, discount, gst_amount, total_amount)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (customer, payment_method, totals["subtotal"],
                  totals["discount"], totals["gst"], totals["total"]))
            sale_id = self.cursor.lastrowid
            for line in lines:
                self.cursor.execute("""
                    INSERT INTO sale_items
                    (sale_id, product_id, product_name, sku, unit_price,
                     quantity, gst_rate, line_total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (sale_id, line["product_id"], line["product_name"], line["sku"],
                      line["unit_price"], line["quantity"], line["gst_rate"], line["line_total"]))
                self.cursor.execute("""
                    UPDATE products SET stock = stock - ?
                    WHERE id = ? AND stock >= ?
                """, (line["quantity"], line["product_id"], line["quantity"]))
                if self.cursor.rowcount != 1:
                    raise ValueError(
                        f"Insufficient stock for {line['product_name']} (changed during checkout).")
            self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise
        return sale_id, totals

    # ---------------- sales history ----------------
    def search_sales(self, query=""):
        """Bills matching an exact bill number/ID, or customer/payment text."""
        query = (query or "").strip()
        if not query:
            rows = self.cursor.execute("""
                SELECT id, sale_date, customer, payment_method, total_amount
                FROM sales ORDER BY id DESC
            """).fetchall()
        else:
            digits = query.upper().replace("BILL-", "").strip().lstrip("0")
            if digits.isdigit():
                # A bill number or plain ID identifies exactly one bill.
                rows = self.cursor.execute("""
                    SELECT id, sale_date, customer, payment_method, total_amount
                    FROM sales WHERE id = ? ORDER BY id DESC
                """, (int(digits),)).fetchall()
            else:
                like = f"%{query}%"
                rows = self.cursor.execute("""
                    SELECT id, sale_date, customer, payment_method, total_amount
                    FROM sales
                    WHERE customer LIKE ? OR payment_method LIKE ?
                    ORDER BY id DESC
                """, (like, like)).fetchall()
        return [{"id": r[0], "bill_number": bill_number(r[0]), "sale_date": r[1],
                 "customer": r[2], "payment_method": r[3], "total": r[4]} for r in rows]

    def get_sale_with_items(self, sale_id):
        """Saved sale + its items. Empty items => legacy bill (never fabricated)."""
        sale = self.cursor.execute("""
            SELECT id, customer, payment_method, subtotal, discount,
                   gst_amount, total_amount, sale_date
            FROM sales WHERE id = ?
        """, (int(sale_id),)).fetchone()
        if sale is None:
            return None
        items = self.cursor.execute("""
            SELECT product_id, product_name, sku, unit_price,
                   quantity, gst_rate, line_total
            FROM sale_items WHERE sale_id = ? ORDER BY id
        """, (int(sale_id),)).fetchall()
        return {
            "id": sale[0], "bill_number": bill_number(sale[0]),
            "customer": sale[1], "payment_method": sale[2],
            "subtotal": sale[3], "discount": sale[4], "gst_amount": sale[5],
            "total": sale[6], "sale_date": sale[7],
            "items": [{"product_id": r[0], "product_name": r[1], "sku": r[2],
                       "unit_price": r[3], "quantity": r[4],
                       "gst_rate": r[5], "line_total": r[6]} for r in items],
            "is_legacy_items": len(items) == 0,
        }

    # ---------------- legacy compatibility (unchanged SQL) ----------------
    def create_product_table(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT NOT NULL,
                sku TEXT UNIQUE,
                barcode TEXT UNIQUE,
                category TEXT,
                purchase_price REAL DEFAULT 0,
                selling_price REAL NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                gst REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def create_sales_table(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT,
                payment_method TEXT,
                total_amount REAL NOT NULL,
                sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def add_product(self, name, category, price, stock):
        self.cursor.execute("""
            INSERT INTO products (product_name, category, selling_price, stock)
            VALUES (?, ?, ?, ?)
        """, (name, category, price, stock))
        self.conn.commit()

    def add_product_full(self, name, sku=None, barcode=None, category=None,
                         purchase_price=0, selling_price=0, stock=0, gst=0):
        """Insert a product with all schema fields. Empty SKU/barcode become
        NULL so several products may leave them blank (UNIQUE columns)."""
        if not str(name).strip():
            raise ValueError("Product name is required.")
        def _blank_to_none(value):
            text = str(value).strip() if value is not None else ""
            return text if text else None
        category = str(category).strip() if category and str(category).strip() else None
        self.cursor.execute("""
            INSERT INTO products
            (product_name, sku, barcode, category, purchase_price,
             selling_price, stock, gst)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(name).strip(), _blank_to_none(sku), _blank_to_none(barcode),
              category, float(purchase_price), float(selling_price),
              int(stock), float(gst)))
        self.conn.commit()
        return self.cursor.lastrowid

    def add_sale(self, customer, payment_method, total_amount):
        self.cursor.execute("""
            INSERT INTO sales (customer, payment_method, total_amount)
            VALUES (?, ?, ?)
        """, (customer, payment_method, total_amount))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_sales_history(self):
        self.cursor.execute("""
            SELECT id, sale_date, customer, payment_method, total_amount
            FROM sales
            ORDER BY id DESC
        """)
        return self.cursor.fetchall()


# Previous modules do `from database import DatabaseManager`.
DatabaseManager = Database

