"""A plain CRUD web app: no AI packages, hosts, or chat-payload shapes."""

import sqlite3

from flask import Flask, jsonify, request

app = Flask(__name__)
DB = "inventory.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/items")
def list_items():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, qty FROM items").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/items")
def create_item():
    payload = request.get_json()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, qty) VALUES (?, ?)",
            (payload["name"], payload.get("qty", 0)),
        )
    return jsonify({"id": cur.lastrowid}), 201


@app.delete("/items/<int:item_id>")
def delete_item(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return "", 204
