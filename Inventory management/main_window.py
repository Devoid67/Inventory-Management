"""Main window: sidebar navigation, POS grid, cart, checkout, sales history.

- Layout lives in main_window.ui (Qt Designer); behaviour lives here.
- QUiLoader reads the .ui file at startup, so there is no generated UI code.
- All SQL and transactions live in database.py; handlers below pass data
  to the Database class and only format the results for display.
- Discounts are disabled (always 0) and GST is a flat 18%.
"""

import sqlite3

from PySide6.QtCore import QFile, Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from database import GST_RATE, Database, bill_number

PAYMENT_METHODS = ("Cash", "UPI", "Card", "Other")


class ProductDialog(QDialog):
    """Product-entry dialog matching the POS style (all schema fields)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Product")
        self.setMinimumWidth(420)
        self.name_edit = QLineEdit()
        self.sku_edit = QLineEdit()
        self.barcode_edit = QLineEdit()
        self.category_edit = QLineEdit()
        self.purchase_edit = QLineEdit("0")
        self.selling_edit = QLineEdit()
        self.stock_edit = QLineEdit("0")
        self.gst_edit = QLineEdit("0")
        form = QFormLayout(self)
        form.addRow("Product Name *", self.name_edit)
        form.addRow("SKU (optional)", self.sku_edit)
        form.addRow("Barcode (optional)", self.barcode_edit)
        form.addRow("Category", self.category_edit)
        form.addRow("Purchase Price", self.purchase_edit)
        form.addRow("Selling Price *", self.selling_edit)
        form.addRow("Stock *", self.stock_edit)
        form.addRow("GST %", self.gst_edit)

        self.info_label = QLabel("Fields marked with * are required.")
        form.addRow(self.info_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def values(self):
        return {
            "name": self.name_edit.text().strip(),
            "sku": self.sku_edit.text().strip(),
            "barcode": self.barcode_edit.text().strip(),
            "category": self.category_edit.text().strip(),
            "purchase_price": self.purchase_edit.text().strip(),
            "selling_price": self.selling_edit.text().strip(),
            "stock": self.stock_edit.text().strip(),
            "gst": self.gst_edit.text().strip(),
        }


def parse_product_form(values):
    """Validate dialog input; raises ValueError with a user-facing message."""
    name = values["name"].strip()
    if not name:
        raise ValueError("Product name is required.")
    try:
        selling = float(values["selling_price"])
    except ValueError:
        raise ValueError("Selling price must be a valid number.")
    if selling < 0:
        raise ValueError("Selling price cannot be negative.")
    try:
        stock_text = values["stock"].strip()
        stock = int(stock_text) if stock_text else 0
        if "." in stock_text:
            raise ValueError("Stock must be a whole number.")
    except ValueError as exc:
        if "whole number" in str(exc):
            raise
        raise ValueError("Stock must be a valid whole number.")
    if stock < 0:
        raise ValueError("Stock cannot be negative.")
    try:
        purchase = float(values["purchase_price"] or 0)
    except ValueError:
        raise ValueError("Purchase price must be a valid number.")
    if purchase < 0:
        raise ValueError("Purchase price cannot be negative.")
    try:
        gst = float(values["gst"] or 0)
    except ValueError:
        raise ValueError("GST must be a valid number.")
    if gst < 0 or gst > 100:
        raise ValueError("GST must be between 0 and 100.")
    return {
        "name": name,
        "sku": values["sku"].strip() or None,
        "barcode": values["barcode"].strip() or None,
        "category": values["category"].strip() or None,
        "purchase_price": purchase,
        "selling_price": selling,
        "stock": stock,
        "gst": gst,
    }


class MainWindow(QMainWindow):
    """Sidebar + POS + cart + sales history (sales scope only)."""

    CARD_WIDTH = 200
    CARD_HEIGHT = 250
    GRID_SPACING = 12

    # Teal pill marks the active sidebar item (visual only).
    NAV_ACTIVE = (
        "QPushButton { background: #0F766E; color: white; font-weight: bold; "
        "padding: 12px 16px; text-align: left; border: none; border-radius: 8px; } "
        "QPushButton:hover { background: #0D655E; }"
    )
    NAV_IDLE = ""  # falls back to the sidebar stylesheet in main_window.ui

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Smart Inventory ERP")
        self.resize(1400, 800)
        self.setMinimumSize(900, 600)

        # In-memory cart: {product_id: {id, name, price, qty}}.
        self.cart = {}
        self.products = []
        self._current_columns = 0
        self._checkout_in_progress = False

        self.db = Database()
        self._load_ui()
        self._find_widgets()
        self._setup_tables()
        self._connect_signals()
        self.load_products()
        self.refresh_cart()
        self.show_billing()

    # ---------------- UI loading ----------------
    def _load_ui(self):
        # QUiLoader = Qt's runtime .ui reader (no generated Python needed).
        ui_file = QFile("main_window.ui")
        ui_file.open(QFile.ReadOnly)
        central = QUiLoader().load(ui_file, self)
        ui_file.close()
        self.setCentralWidget(central)
        # Stretch: sidebar fixed, middle expands, cart fixed.
        layout = central.layout()
        if layout is not None:
            layout.setStretch(0, 0)
            layout.setStretch(1, 1)
            layout.setStretch(2, 0)
        # Debounce window-resize grid recalculation.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(80)
        self._resize_timer.timeout.connect(self._relayout_if_needed)

    def _find_widgets(self):
        # Every name below must match an objectName in main_window.ui.
        get = self.findChild
        self.search_edit = get(QLineEdit, "searchLineEdit")
        self.search_row = get(QWidget, "searchRow")
        row_layout = self.search_row.layout()
        if row_layout is not None:
            row_layout.setStretch(0, 1)
            row_layout.setStretch(1, 0)
        self.add_button = get(QPushButton, "addProductButton")
        self.scroll_area = get(QScrollArea, "productScrollArea")
        self.grid_host = get(QWidget, "productGridWidget")
        self.header_title = get(QLabel, "headerTitle")
        self.header_subtitle = get(QLabel, "headerSubtitle")
        self.history_box = get(QWidget, "historyWidget")
        self.history_search = get(QLineEdit, "historySearchEdit")
        self.history_table = get(QTableView, "historyTable")
        self.history_details = get(QLabel, "historyDetailsLabel")
        self.history_items = get(QTableView, "historyItemsTable")
        self.cart_table = get(QTableView, "cartTableView")
        self.item_count_label = get(QLabel, "itemCountLabel")
        self.subtotal_label = get(QLabel, "subtotalLabel")
        self.discount_label = get(QLabel, "discountLabel")
        self.gst_label = get(QLabel, "gstLabel")
        self.grand_label = get(QLabel, "grandTotalLabel")
        self.customer_combo = get(QComboBox, "customerComboBox")
        self.payment_combo = get(QComboBox, "paymentComboBox")
        self.complete_button = get(QPushButton, "completeSaleButton")
        self.clear_button = get(QPushButton, "clearCartButton")
        self.qty_dec_button = get(QPushButton, "qtyDecButton")
        self.qty_inc_button = get(QPushButton, "qtyIncButton")
        self.remove_button = get(QPushButton, "removeItemButton")
        self.nav_billing = get(QPushButton, "navBillingBtn")
        self.nav_history = get(QPushButton, "navHistoryBtn")
        self.nav_exit = get(QPushButton, "navExitBtn")
        # Built-in clear button so the field is easy to notice and reset.
        self.search_edit.setClearButtonEnabled(True)
        self.history_search.setClearButtonEnabled(True)

    def _setup_tables(self):
        self.cart_model = QStandardItemModel(0, 4, self)
        self.cart_model.setHorizontalHeaderLabels(["Product", "Qty", "Price", "Total"])
        self.cart_table.setModel(self.cart_model)
        self.cart_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cart_table.setEditTriggers(QTableView.NoEditTriggers)
        self.cart_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cart_table.setAlternatingRowColors(True)
        self.cart_table.setShowGrid(False)
        self.cart_table.verticalHeader().setVisible(False)
        self.cart_table.verticalHeader().setDefaultSectionSize(28)
        self.cart_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_model = QStandardItemModel(0, 5, self)
        self.history_model.setHorizontalHeaderLabels(
            ["Bill No", "Date", "Customer", "Payment", "Total"]
        )
        self.history_table.setModel(self.history_model)
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history_table.setEditTriggers(QTableView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_items_model = QStandardItemModel(0, 4, self)
        self.history_items_model.setHorizontalHeaderLabels(
            ["Item", "Qty", "Unit Price", "Total"]
        )
        self.history_items.setModel(self.history_items_model)
        self.history_items.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history_items.setEditTriggers(QTableView.NoEditTriggers)
        for table in (self.history_table, self.history_items):
            table.setAlternatingRowColors(True)
            table.setShowGrid(False)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(28)

    def _connect_signals(self):
        self.search_edit.textChanged.connect(
            lambda _t="": self.populate_grid(force=True)
        )
        self.add_button.clicked.connect(self.open_add_product)
        self.clear_button.clicked.connect(self.clear_cart)
        self.complete_button.clicked.connect(self.complete_sale)
        self.qty_inc_button.clicked.connect(lambda: self.change_selected_qty(1))
        self.qty_dec_button.clicked.connect(lambda: self.change_selected_qty(-1))
        self.remove_button.clicked.connect(self.remove_selected_item)
        self.nav_billing.clicked.connect(self.show_billing)
        self.nav_history.clicked.connect(self.show_history)
        self.nav_exit.clicked.connect(self.close)
        self.history_search.textChanged.connect(lambda _t="": self.refresh_history())
        self.history_table.selectionModel().selectionChanged.connect(
            lambda _s, _d: self.show_sale_details()
        )

    # ---------------- products ----------------
    def load_products(self):
        """Read products from SQLite, then draw the cards."""
        self.products = self.db.get_products()
        self.populate_grid(force=True)

    def _grid_layout(self):
        layout = self.grid_host.layout()
        layout.setSpacing(self.GRID_SPACING)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        return layout

    def _clear_grid(self):
        layout = self._grid_layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # detach now so old cards never overlap new ones
                widget.deleteLater()

    def _available_width(self):
        width = self.scroll_area.viewport().width()
        if width > 0:
            return width
        return self.scroll_area.width() or self.grid_host.width() or 600

    def _columns_for_width(self, width):
        stride = self.CARD_WIDTH + self.GRID_SPACING
        return max(1, min(6, (width + self.GRID_SPACING) // stride))

    def populate_grid(self, force=False):
        """Draw compact product cards matching the search box."""
        query = self.search_edit.text().strip().lower()
        matching = [p for p in self.products if query in p["name"].lower()]
        columns = self._columns_for_width(self._available_width())
        if (
            not force
            and columns == self._current_columns
            and getattr(self, "_last_query", None) == query
            and self._grid_layout().count() == len(matching)
        ):
            return
        self._current_columns = columns
        self._last_query = query
        self._clear_grid()
        layout = self._grid_layout()
        for i, product in enumerate(matching):
            layout.addWidget(self._product_card(product), i // columns, i % columns)
        layout.setColumnStretch(columns, 1)  # keep cards left-aligned

    def _product_card(self, product):
        """One compact fixed-size card: image, name, price, stock, Add button."""
        from PySide6.QtWidgets import QSizePolicy

        card = QFrame()
        card.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        card.setStyleSheet(
            "QFrame { background: white; border: 1px solid #E5E7EB; border-radius: 8px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 10)
        layout.setSpacing(6)
        image = QLabel("\U0001f4e6")
        image.setFixedHeight(80)
        image.setAlignment(Qt.AlignCenter)
        image.setStyleSheet(
            "background: #ECECEC; font-size: 28px; border: none; color: #1F2937;"
        )
        name = QLabel(product["name"])
        name.setAlignment(Qt.AlignCenter)
        name.setWordWrap(True)  # long names wrap instead of clipping
        name.setMinimumHeight(28)  # always room for the name
        name.setStyleSheet(
            "font-weight: bold; font-size: 11pt; border: none; color: #1F2937; background: transparent;"
        )
        price = QLabel(f"\u20b9{product['price']}")
        price.setAlignment(Qt.AlignCenter)
        price.setStyleSheet(
            "color: #0F766E; font-weight: bold; font-size: 14px; border: none;"
        )
        stock = QLabel(f"Stock : {product['stock']}")
        stock.setAlignment(Qt.AlignCenter)
        stock.setStyleSheet("color: #6B7280; font-size: 9pt; border: none;")
        button = QPushButton("+ Add to Cart")
        button.setStyleSheet(
            "QPushButton { background: #0F766E; color: white; padding: 8px; border: none; border-radius: 8px; font-weight: bold; } QPushButton:hover { background: #0D655E; } QPushButton:disabled { background: #9CA3AF; }"
        )
        button.clicked.connect(lambda _checked=False, p=product: self.add_to_cart(p))
        for widget in (image, name, price, stock, button):
            layout.addWidget(widget)
        return card

    def _relayout_if_needed(self):
        if not self.scroll_area.isVisible():
            return
        if self._columns_for_width(self._available_width()) != self._current_columns:
            self.populate_grid(force=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._resize_timer.isActive():
            self._resize_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._resize_timer.isActive():
            self._resize_timer.start()

    # ---------------- cart ----------------
    def add_to_cart(self, product):
        """Add one unit; repeat adds increase the quantity (validated at checkout)."""
        pid = product["id"]
        if pid in self.cart:
            self.cart[pid]["qty"] += 1
        else:
            self.cart[pid] = {
                "id": pid,
                "name": product["name"],
                "price": product["price"],
                "qty": 1,
            }
        self.refresh_cart()

    def _selected_cart_id(self):
        selection = self.cart_table.selectionModel().selectedRows()
        if not selection:
            return None
        try:
            name = self.cart_model.item(selection[0].row(), 0).text()
        except AttributeError:
            return None
        for pid, item in self.cart.items():
            if item["name"] == name:
                return pid
        return None

    def change_selected_qty(self, delta):
        """Change the selected row's quantity by delta (never below 1 or above stock)."""
        pid = self._selected_cart_id()
        if pid is None:
            QMessageBox.warning(
                self, "No Selection", "Select a cart row to change its quantity."
            )
            return
        new_qty = self.cart[pid]["qty"] + delta
        if new_qty < 1:
            del self.cart[pid]
        else:
            live_stock = self.db.get_product_stock(pid)
            if live_stock is not None and new_qty > live_stock:
                QMessageBox.warning(
                    self,
                    "Insufficient Stock",
                    f"Only {live_stock} available for {self.cart[pid]['name']}.",
                )
                return
            self.cart[pid]["qty"] = new_qty
        self.refresh_cart()

    def remove_selected_item(self):
        pid = self._selected_cart_id()
        if pid is None:
            QMessageBox.warning(self, "No Selection", "Select a cart row to remove.")
            return
        del self.cart[pid]
        self.refresh_cart()

    def cart_totals(self):
        """Single calculation used by the cart display (discount always 0)."""
        return self.db.calculate_totals(self.cart, gst_rate=GST_RATE)

    def refresh_cart(self):
        self.cart_model.removeRows(0, self.cart_model.rowCount())
        count = 0
        for item in self.cart.values():
            line_total = item["price"] * item["qty"]
            count += item["qty"]
            row = [
                QStandardItem(str(item["name"])),
                QStandardItem(str(item["qty"])),
                QStandardItem(f"\u20b9{item['price']}"),
                QStandardItem(f"\u20b9{line_total}"),
            ]
            for cell in row:
                cell.setEditable(False)
            self.cart_model.appendRow(row)
        totals = self.cart_totals()
        self.subtotal_label.setText(f"Subtotal: \u20b9{totals['subtotal']:.2f}")
        self.discount_label.setText(
            f"Discount: \u20b9{totals['discount']:.2f} (disabled)"
        )
        self.gst_label.setText(f"GST ({GST_RATE:.0f}%): \u20b9{totals['gst']:.2f}")
        self.grand_label.setText(f"Grand Total: \u20b9{totals['total']:.2f}")
        self.item_count_label.setText(f"{count} {'Item' if count == 1 else 'Items'}")

    def clear_cart(self):
        if self.cart:
            self.cart.clear()
            self.refresh_cart()

    def complete_sale(self):
        """Transactional checkout: sale + items + stock deduction, or full rollback."""
        if self._checkout_in_progress:
            return  # duplicate submission guard
        if not self.cart:
            QMessageBox.warning(
                self, "Empty Cart", "Please add products before completing the sale."
            )
            return
        if self.payment_combo.currentText() not in PAYMENT_METHODS:
            QMessageBox.warning(
                self, "Invalid Payment", "Select a valid payment method."
            )
            return
        customer = self.customer_combo.currentText().strip() or "Walk-in Customer"
        self._checkout_in_progress = True
        self.complete_button.setEnabled(False)
        try:
            sale_id, totals = self.db.checkout(
                dict(self.cart),
                customer,
                self.payment_combo.currentText(),
                gst_rate=GST_RATE,
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Checkout Failed", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Checkout Failed", f"Database error: {exc}")
            return
        finally:
            self._checkout_in_progress = False
            self.complete_button.setEnabled(True)
        # Success only: clear cart and refresh every product/stock view.
        self.cart.clear()
        self.refresh_cart()
        self.load_products()
        if self.history_box.isVisible():
            self.refresh_history()
        QMessageBox.information(
            self,
            "Sale Completed",
            f"Sale completed successfully!\n\nBill {bill_number(sale_id)}\n"
            f"Total Amount: \u20b9{totals['total']:.2f}",
        )

    # ---------------- sales history ----------------
    def refresh_history(self):
        records = self.db.search_sales(self.history_search.text())
        self.history_model.removeRows(0, self.history_model.rowCount())
        for record in records:
            row = [
                QStandardItem(record["bill_number"]),
                QStandardItem(record["sale_date"]),
                QStandardItem(str(record["customer"] or "Walk-in Customer")),
                QStandardItem(str(record["payment_method"] or "—")),
                QStandardItem(f"\u20b9{float(record['total']):.2f}"),
            ]
            row[0].setData(record["id"])
            for cell in row:
                cell.setEditable(False)
            self.history_model.appendRow(row)
        if not self.history_model.rowCount():
            self.history_details.setText("No bills match the current search.")
            self.history_items_model.removeRows(0, self.history_items_model.rowCount())

    def _selected_sale_id(self):
        selection = self.history_table.selectionModel().selectedRows()
        if not selection:
            return None
        try:
            return int(self.history_model.item(selection[0].row(), 0).data())
        except (TypeError, ValueError):
            return None

    def show_sale_details(self):
        sale_id = self._selected_sale_id()
        if sale_id is None:
            return
        details = self.db.get_sale_with_items(sale_id)
        if details is None:
            self.history_details.setText("Bill not found.")
            return
        self.history_details.setText(
            f"{details['bill_number']} | {details['sale_date']} | "
            f"{details['customer'] or 'Walk-in Customer'} | "
            f"{details['payment_method'] or '—'} | \u20b9{float(details['total']):.2f}"
        )
        self.history_items_model.removeRows(0, self.history_items_model.rowCount())
        if details["is_legacy_items"]:
            self.history_details.setText(
                self.history_details.text()
                + " — legacy bill, item details unavailable."
            )
            return
        for item in details["items"]:
            row = [
                QStandardItem(str(item["product_name"])),
                QStandardItem(str(item["quantity"])),
                QStandardItem(f"\u20b9{float(item['unit_price']):.2f}"),
                QStandardItem(f"\u20b9{float(item['line_total']):.2f}"),
            ]
            for cell in row:
                cell.setEditable(False)
            self.history_items_model.appendRow(row)

    # ---------------- navigation ----------------
    def _update_nav_styles(self, active):
        """Highlight the active sidebar item; visual only, no logic change."""
        self.nav_billing.setStyleSheet(
            self.NAV_ACTIVE if active == "billing" else self.NAV_IDLE
        )
        self.nav_history.setStyleSheet(
            self.NAV_ACTIVE if active == "history" else self.NAV_IDLE
        )

    def show_billing(self):
        self._update_nav_styles("billing")
        self.header_title.setText("Sales / POS")
        self.header_subtitle.setText("Search products and manage the cart")
        self.search_row.show()
        self.scroll_area.show()
        self.history_box.hide()

    def show_history(self):
        self._update_nav_styles("history")
        self.header_title.setText("Sales History")
        self.header_subtitle.setText(
            "Bills from saved records (legacy bills have no item details)"
        )
        self.search_row.hide()
        self.scroll_area.hide()
        self.history_box.show()
        self.refresh_history()

    def open_add_product(self):
        """Add a product via dialog; refresh the grid on success, save nothing on cancel."""
        dialog = ProductDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            parsed = parse_product_form(dialog.values())
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid Input", str(exc))
            return
        try:
            new_id = self.db.add_product_full(
                parsed["name"],
                parsed["sku"],
                parsed["barcode"],
                parsed["category"],
                parsed["purchase_price"],
                parsed["selling_price"],
                parsed["stock"],
                parsed["gst"],
            )
        except sqlite3.IntegrityError:
            QMessageBox.critical(
                self,
                "Save Failed",
                "SKU or barcode already exists. Use a unique value or leave it blank.",
            )
            return
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            return
        QMessageBox.information(
            self, "Success", f"Product added successfully! (ID #{new_id})"
        )
        self.load_products()
