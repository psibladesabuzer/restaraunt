import sqlite3
import tempfile
import unittest
from pathlib import Path

import app


class RestaurantLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.original_paths = {
            "DATA_DIR": app.DATA_DIR,
            "DB_PATH": app.DB_PATH,
            "SNAPSHOT_PATH": app.SNAPSHOT_PATH,
            "LEGACY_DB_PATH": app.LEGACY_DB_PATH,
        }

        base = Path(self.temp_dir.name)
        app.DATA_DIR = str(base / "data")
        app.DB_PATH = str(base / "data" / "restaurant.db")
        app.SNAPSHOT_PATH = str(base / "data" / "restaurant_snapshot.json")
        app.LEGACY_DB_PATH = str(base / "legacy" / "restaurant.db")
        app.SESSIONS.clear()
        self.addCleanup(self._restore_app_paths)

        app.init_db()

    def _restore_app_paths(self):
        for name, value in self.original_paths.items():
            setattr(app, name, value)
        app.SESSIONS.clear()

    def _conn(self):
        return app.get_db()

    def _user(self, username, password):
        user = app.fetch_user_by_credentials(username, password)
        self.assertIsNotNone(user, f"Не найдена учетная запись {username}")
        return user

    def _menu(self):
        menu = app.fetch_menu_items()
        self.assertTrue(menu)
        return menu

    def _create_order(self, waiter, dishes_count=2):
        menu = self._menu()[:dishes_count]
        data = {
            "dish_query": [row["name"] for row in menu],
            "quantity": ["1" for _ in menu],
            "price": [str(row["price"]) for row in menu],
            "notes": ["" for _ in menu],
        }
        message = app.create_order(waiter, data)
        self.assertIn("отправлен на кухню", message)
        conn = self._conn()
        order = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
        items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order["id"],)).fetchall()
        conn.close()
        return order, items

    def _take_order(self, order_id, coordinator):
        conn = self._conn()
        accepted_at = app.now_iso()
        conn.execute(
            """
            UPDATE orders
            SET chef_id = ?, accepted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (coordinator["id"], accepted_at, accepted_at, order_id),
        )
        app.recalculate_order_state(conn, order_id)
        conn.commit()
        conn.close()

    def test_fetch_user_by_credentials_returns_only_active_user(self):
        user = app.fetch_user_by_credentials("waiter1", "waiter123")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], app.WAITERS)

        conn = self._conn()
        conn.execute("UPDATE users SET active = 0 WHERE username = 'waiter1'")
        conn.commit()
        conn.close()

        self.assertIsNone(app.fetch_user_by_credentials("waiter1", "waiter123"))

    def test_create_order_persists_order_and_items(self):
        waiter = self._user("waiter1", "waiter123")
        order, items = self._create_order(waiter, dishes_count=2)

        self.assertEqual(order["status"], "new")
        self.assertEqual(len(items), 2)
        self.assertTrue(Path(app.DB_PATH).exists())
        self.assertTrue(Path(app.SNAPSHOT_PATH).exists())

    def test_chef_can_assign_sous_chef_and_sous_chef_can_assign_self(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        sous = self._user("souschef1", "sous123")
        order, items = self._create_order(waiter, dishes_count=2)
        self._take_order(order["id"], chef)

        message = app.assign_item_to_cook(chef, items[0]["id"], sous["id"])
        self.assertIn("назначен", message)

        message = app.assign_order_items_bulk(
            sous,
            order["id"],
            {
                f"cook_id_{items[0]['id']}": [str(sous["id"])],
                f"cook_id_{items[1]['id']}": [str(self._user('cook1', 'cook123')["id"])],
            },
        )
        self.assertIn("сохранены", message)

        conn = self._conn()
        rows = conn.execute(
            "SELECT assigned_cook_id, item_status FROM order_items WHERE order_id = ? ORDER BY id",
            (order["id"],),
        ).fetchall()
        conn.close()

        self.assertEqual(rows[0]["assigned_cook_id"], sous["id"])
        self.assertEqual(rows[0]["item_status"], "assigned")

    def test_sous_chef_cannot_assign_head_chef_to_dish(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        sous = self._user("souschef1", "sous123")
        order, items = self._create_order(waiter, dishes_count=1)
        self._take_order(order["id"], chef)

        message = app.assign_item_to_cook(sous, items[0]["id"], chef["id"])
        self.assertIn("Нельзя назначить", message)

    def test_mark_cook_item_ready_requires_ownership(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        sous = self._user("souschef1", "sous123")
        cook = self._user("cook1", "cook123")
        order, items = self._create_order(waiter, dishes_count=1)
        self._take_order(order["id"], chef)
        app.assign_item_to_cook(chef, items[0]["id"], cook["id"])

        denied = app.mark_cook_item_ready(sous, items[0]["id"])
        self.assertIn("только свои", denied)

        allowed = app.mark_cook_item_ready(cook, items[0]["id"])
        self.assertIn("готовое", allowed)

        conn = self._conn()
        item = conn.execute("SELECT item_status, ready_at FROM order_items WHERE id = ?", (items[0]["id"],)).fetchone()
        conn.close()
        self.assertEqual(item["item_status"], "ready")
        self.assertIsNotNone(item["ready_at"])

    def test_waiter_can_serve_only_ready_item_from_own_order(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        cook = self._user("cook1", "cook123")
        order, items = self._create_order(waiter, dishes_count=1)
        self._take_order(order["id"], chef)
        app.assign_item_to_cook(chef, items[0]["id"], cook["id"])

        not_ready = app.serve_waiter_item(waiter, items[0]["id"])
        self.assertIn("только готовое", not_ready)

        app.mark_cook_item_ready(cook, items[0]["id"])
        served = app.serve_waiter_item(waiter, items[0]["id"])
        self.assertIn("выданное", served)

        conn = self._conn()
        order_row = conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()
        item = conn.execute("SELECT item_status FROM order_items WHERE id = ?", (items[0]["id"],)).fetchone()
        conn.close()

        self.assertEqual(item["item_status"], "served")
        self.assertEqual(order_row["status"], "payment_pending")

    def test_finalize_order_payment_requires_valid_payment_and_all_items_served(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        cook = self._user("cook1", "cook123")
        order, items = self._create_order(waiter, dishes_count=1)
        self._take_order(order["id"], chef)
        app.assign_item_to_cook(chef, items[0]["id"], cook["id"])

        early = app.finalize_order_payment(waiter, order["id"], "cash")
        self.assertIn("Сначала нужно выдать", early)

        app.mark_cook_item_ready(cook, items[0]["id"])
        app.serve_waiter_item(waiter, items[0]["id"])

        invalid_payment = app.finalize_order_payment(waiter, order["id"], "invoice")
        self.assertIn("выбрать способ оплаты", invalid_payment)

        completed = app.finalize_order_payment(waiter, order["id"], "card")
        self.assertIn("заказ завершен", completed)

        conn = self._conn()
        order_row = conn.execute(
            "SELECT status, payment_method FROM orders WHERE id = ?",
            (order["id"],),
        ).fetchone()
        report = conn.execute(
            "SELECT status, payment_method FROM management_reports WHERE order_id = ?",
            (order["id"],),
        ).fetchone()
        conn.close()

        self.assertEqual(order_row["status"], "completed")
        self.assertEqual(order_row["payment_method"], "card")
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["payment_method"], "card")

    def test_recalculate_order_state_handles_assignment_and_payment_pending(self):
        waiter = self._user("waiter1", "waiter123")
        chef = self._user("chef1", "chef123")
        cook = self._user("cook1", "cook123")
        order, items = self._create_order(waiter, dishes_count=2)
        self._take_order(order["id"], chef)
        app.assign_item_to_cook(chef, items[0]["id"], cook["id"])

        conn = self._conn()
        preparing = conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()["status"]
        conn.close()
        self.assertEqual(preparing, "preparing")

        app.mark_cook_item_ready(cook, items[0]["id"])
        conn = self._conn()
        still_preparing = conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()["status"]
        conn.close()
        self.assertEqual(still_preparing, "preparing")

        app.assign_item_to_cook(chef, items[1]["id"], cook["id"])
        app.mark_cook_item_ready(cook, items[1]["id"])
        conn = self._conn()
        ready = conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()["status"]
        conn.close()
        self.assertEqual(ready, "ready")


if __name__ == "__main__":
    unittest.main()
