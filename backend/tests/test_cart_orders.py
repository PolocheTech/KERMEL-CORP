import sqlite3

import pytest
from fastapi import HTTPException

from app import database
from app.products_store import initialize_products
from app.routers import cart, orders, products
from app.schemas import (
    CartItemAdd,
    CartItemChange,
    CreateOrderRequest,
    OrderStatusChange,
)


@pytest.fixture
def customer(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "kermel.db")
    database.initialize_database()
    initialize_products()

    with database.get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (name, email, password_hash)
            VALUES (?, ?, ?)
            """,
            ("Cliente de prueba", "cliente@example.test", "hash"),
        )
        user_id = cursor.lastrowid

    return {"id": user_id, "username": "cliente@example.test", "role": "cliente"}


def _product_id() -> int:
    with database.get_connection() as connection:
        row = connection.execute(
            "SELECT id FROM products WHERE name = ?",
            ("Ryzen 5 5600",),
        ).fetchone()
    return row["id"]


def test_public_product_list_is_filtered_and_paginated(customer):
    filters = {
        "category_id": None,
        "brand": None,
        "min_price": None,
        "max_price": None,
        "search": None,
        "socket": None,
    }
    first_page = products.list_products(**filters, page=1, page_size=2)
    second_page = products.list_products(**filters, page=2, page_size=2)

    assert len(first_page["items"]) == 2
    assert first_page["total"] == 18
    assert first_page["total_pages"] == 9
    assert {item["id"] for item in first_page["items"]}.isdisjoint(
        item["id"] for item in second_page["items"]
    )

    socket_products = products.list_products(
        **{**filters, "socket": "AM4"},
        page=1,
        page_size=20,
    )
    assert socket_products["total"] > 0
    assert all(item["socket"] == "AM4" for item in socket_products["items"])


def test_catalog_pagination_handles_ten_thousand_products(customer):
    with database.get_connection() as connection:
        category_id = connection.execute(
            "SELECT id FROM categories ORDER BY id LIMIT 1",
        ).fetchone()["id"]
        connection.executemany(
            """
            INSERT INTO products (
                id, name, category, brand, price, socket, stock,
                icon, specs, image, category_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    product_id,
                    f"Producto de prueba {product_id}",
                    "Prueba",
                    "Marca de prueba",
                    1000,
                    "AM4",
                    1,
                    "📦",
                    "[]",
                    None,
                    category_id,
                )
                for product_id in range(100, 10082)
            ),
        )

    last_page = products.list_products(
        category_id=None,
        brand=None,
        min_price=None,
        max_price=None,
        search=None,
        socket=None,
        page=500,
        page_size=20,
    )
    assert last_page["total"] == 10_000
    assert len(last_page["items"]) == 20
    assert last_page["total_pages"] == 500


def test_cart_quantity_is_adjusted_to_available_stock(customer):
    product_id = _product_id()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO carts (user_id, updated_at) VALUES (?, ?)",
            (customer["id"], "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO cart_items (user_id, product_id, quantity)
            VALUES (?, ?, ?)
            """,
            (customer["id"], product_id, 10),
        )

    added = cart.add_cart_item(
        CartItemAdd(product_id=product_id, quantity=5),
        customer,
    )
    assert added["quantity"] == 12
    assert added["adjusted"] is True

    updated = cart.update_cart_item(
        product_id,
        CartItemChange(quantity=100),
        customer,
    )
    assert updated["quantity"] == 12
    assert updated["adjusted"] is True

    with database.get_connection() as connection:
        row = connection.execute(
            """
            SELECT quantity FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (customer["id"], product_id),
        ).fetchone()
    assert row["quantity"] == 12


def test_cash_on_delivery_is_confirmed_only_with_delivery(customer):
    product_id = _product_id()
    with database.get_connection() as connection:
        connection.execute(
            "INSERT INTO carts (user_id, updated_at) VALUES (?, ?)",
            (customer["id"], "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO cart_items (user_id, product_id, quantity)
            VALUES (?, ?, 2)
            """,
            (customer["id"], product_id),
        )

    created = orders.create_order(
        CreateOrderRequest(payment_method="efectivo_contra_entrega"),
        customer,
    )
    order_id = created["id"]
    assert created["estado"] == "Pendiente"
    assert created["payment_status"] == "pendiente"
    assert created["payment_method"] == "efectivo_contra_entrega"

    orders.update_order_status(
        order_id,
        OrderStatusChange(estado="Confirmado"),
        {},
    )
    shipped = orders.update_order_status(
        order_id,
        OrderStatusChange(estado="Enviado"),
        {},
    )
    assert shipped["payment_status"] == "pendiente"

    with pytest.raises(HTTPException) as error:
        orders.update_order_status(
            order_id,
            OrderStatusChange(estado="Entregado"),
            {},
        )
    assert error.value.status_code == 409

    delivered = orders.confirm_delivery_and_cash(order_id, {})
    assert delivered["estado"] == "Entregado"
    assert delivered["payment_status"] == "pagado"
    assert delivered["paid_at"]

    repeated = orders.confirm_delivery_and_cash(order_id, {})
    assert repeated["paid_at"] == delivered["paid_at"]


def test_legacy_order_migration_preserves_order_details(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    monkeypatch.setattr(database, "DATABASE_PATH", database_path)

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'cliente'
            );
            INSERT INTO users VALUES (1, 'Cliente', 'c@example.test', 'hash', 'cliente');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                created_by INTEGER NOT NULL REFERENCES users(id),
                cliente TEXT NOT NULL,
                producto TEXT NOT NULL,
                categoria TEXT NOT NULL,
                total INTEGER NOT NULL,
                estado TEXT NOT NULL CHECK (
                    estado IN ('Pendiente', 'Confirmado', 'Cancelado')
                ),
                created_at TEXT NOT NULL
            );
            INSERT INTO orders VALUES (
                7, 1, 'c@example.test', 'Producto antiguo',
                'Categoría', 120, 'Pendiente', '2026-01-01T00:00:00+00:00'
            );
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL
            );
            INSERT INTO order_items VALUES (1, 7, 1, 'Producto antiguo', 1, 120);
            """
        )

    database.initialize_database()

    with database.get_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = 7",
        ).fetchone()
        item = connection.execute(
            "SELECT * FROM order_items WHERE order_id = 7",
        ).fetchone()

    assert order["payment_status"] == "no_registrado"
    assert order["payment_method"] is None
    assert item["product_name"] == "Producto antiguo"
