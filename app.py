#!/usr/bin/env python3
"""
Local Memory Companion
- Safe, non-romantic companion chat
- Persistent SQLite memory
- Daily summaries
- Plain-text journal export
- Simple desktop UI (Tkinter)
"""

from __future__ import annotations

import os
import sqlite3
import threading
import queue
import hashlib
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Any, Optional

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from openai import OpenAI


APP_NAME = "Local Memory Companion"
APP_DIR = Path.home() / ".local_memory_companion"
DB_PATH = APP_DIR / "companion.db"
TXT_LOG_PATH = APP_DIR / "memory_log.txt"
EXPORTS_DIR = APP_DIR / "exports"

DEFAULT_MODEL = "gpt-5-mini"

SYSTEM_PROMPT = """You are a warm, grounded, supportive AI companion for everyday conversation.
Keep the tone kind, calm, friendly, and emotionally aware.
Do not act like a romantic partner.
Do not claim human feelings or a real-world relationship.
Be helpful, lightly encouraging, and natural.
When useful, remember practical preferences, goals, projects, likes/dislikes, and important context from the supplied memory.
Avoid overusing memory unless it is relevant.
"""

APP_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ChatTurn:
    role: str
    content: str
    created_at: str


class MemoryDB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_key ON facts(key);

            CREATE TABLE IF NOT EXISTS daily_summaries (
                day TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def add_turn(self, role: str, content: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO conversations(role, content, created_at) VALUES (?, ?, ?)",
            (role, content, now),
        )
        self.conn.commit()

    def get_recent_turns(self, limit: int = 16) -> List[ChatTurn]:
        cur = self.conn.execute(
            """
            SELECT role, content, created_at
            FROM conversations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        rows.reverse()
        return [ChatTurn(row["role"], row["content"], row["created_at"]) for row in rows]

    def upsert_fact(self, key: str, value: str, source: str = "inferred") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO facts(key, value, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (key, value, source, now, now),
        )
        self.conn.commit()

    def get_facts(self, limit: int = 50) -> List[Dict[str, str]]:
        cur = self.conn.execute(
            """
            SELECT key, value, source, created_at, updated_at
            FROM facts
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def delete_fact(self, key: str) -> None:
        self.conn.execute("DELETE FROM facts WHERE key = ?", (key,))
        self.conn.commit()

    def get_summary_for_day(self, day: str) -> Optional[str]:
        cur = self.conn.execute("SELECT summary FROM daily_summaries WHERE day = ?", (day,))
        row = cur.fetchone()
        return row["summary"] if row else None

    def upsert_daily_summary(self, day: str, summary: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daily_summaries(day, summary, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                summary=excluded.summary,
                created_at=excluded.created_at
            """,
            (day, summary, now),
        )
        self.conn.commit()

    def get_turns_for_day(self, day: str) -> List[ChatTurn]:
        cur = self.conn.execute(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE date(created_at) = ?
            ORDER BY id ASC
            """,
            (day,),
        )
        return [ChatTurn(row["role"], row["content"], row["created_at"]) for row in cur.fetchall()]

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        cur = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def export_txt_log(self, path: Path) -> None:
        cur = self.conn.execute(
            "SELECT role, content, created_at FROM conversations ORDER BY id ASC"
        )
        with path.open("w", encoding="utf-8") as f:
            f.write(f"{APP_NAME} log export\n")
            f.write("=" * 60 + "\n\n")
            for row in cur.fetchall():
                f.write(f"[{row['created_at']}] {row['role'].upper()}:\n{row['content']}\n\n")

    def export_facts_txt(self, path: Path) -> None:
        facts = self.get_facts(limit=500)
        with path.open("w", encoding="utf-8") as f:
            f.write("Stored memory facts\n")
            f.write("=" * 40 + "\n\n")
            for fact in facts:
                f.write(f"- {fact['key']}: {fact['value']}\n")

    def close(self) -> None:
        self.conn.close()


class CompanionEngine:
    def __init__(self, db: MemoryDB):
        self.db = db
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    def _build_memory_context(self) -> str:
        facts = self.db.get_facts(limit=24)
        if not facts:
            return "No stored memory yet."

        lines = []
        for fact in facts:
            lines.append(f"- {fact['key']}: {fact['value']}")
        return "\n".join(lines)

    def _build_recent_context(self) -> str:
        turns = self.db.get_recent_turns(limit=12)
        if not turns:
            return "No recent conversation."
        lines = []
        for t in turns:
            lines.append(f"{t.role}: {t.content}")
        return "\n".join(lines)

    def _safety_identifier(self) -> str:
        raw = os.getenv("USERNAME", "local_user")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def chat(self, user_text: str) -> str:
        memory_text = self._build_memory_context()
        recent_text = self._build_recent_context()

        prompt = f"""
{SYSTEM_PROMPT}

Stored memory:
{memory_text}

Recent conversation:
{recent_text}

User message:
{user_text}

Respond naturally and helpfully. Keep it concise unless detail is useful.
""".strip()

        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=500,
            safety_identifier=self._safety_identifier(),
        )

        reply = getattr(response, "output_text", "").strip()
        if not reply:
            raise RuntimeError("The model returned an empty response.")

        self.db.add_turn("user", user_text)
        self.db.add_turn("assistant", reply)
        self._append_plaintext_log("You", user_text)
        self._append_plaintext_log("AI", reply)
        self.refresh_memory()
        self.ensure_today_summary()
        return reply

    def refresh_memory(self) -> None:
        recent = self.db.get_recent_turns(limit=20)
        convo = "\n".join(f"{t.role}: {t.content}" for t in recent)

        prompt = f"""
You extract useful long-term memory from conversation.

Rules:
- Save only stable, practical, future-useful facts.
- No romantic framing.
- No sensitive medical or highly personal content.
- Prefer preferences, goals, projects, recurring needs, likes/dislikes, names of important files or tools.
- Return strict JSON: {{"facts":[{{"key":"...", "value":"..."}}]}}
- At most 8 facts.
- If nothing useful, return {{"facts":[]}}.

Conversation:
{convo}
""".strip()

        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=400,
            safety_identifier=self._safety_identifier(),
        )
        text = getattr(response, "output_text", "").strip()

        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        facts = data.get("facts", [])
        if isinstance(facts, list):
            for fact in facts:
                key = str(fact.get("key", "")).strip()
                value = str(fact.get("value", "")).strip()
                if key and value:
                    self.db.upsert_fact(key, value, source="conversation")

    def ensure_today_summary(self) -> str:
        day = date.today().isoformat()
        existing = self.db.get_summary_for_day(day)
        if existing:
            return existing

        turns = self.db.get_turns_for_day(day)
        if not turns:
            summary = "No conversation recorded today."
            self.db.upsert_daily_summary(day, summary)
            return summary

        convo = "\n".join(f"{t.role}: {t.content}" for t in turns)
        prompt = f"""
Summarize today's conversation in 4-6 bullet-style sentences as a plain paragraph.
Focus on practical topics, goals, ideas, and what mattered today.
Do not use romantic language.

Conversation:
{convo}
""".strip()

        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=250,
            safety_identifier=self._safety_identifier(),
        )
        summary = getattr(response, "output_text", "").strip() or "Summary unavailable."
        self.db.upsert_daily_summary(day, summary)
        return summary

    def regenerate_today_summary(self) -> str:
        day = date.today().isoformat()
        turns = self.db.get_turns_for_day(day)
        convo = "\n".join(f"{t.role}: {t.content}" for t in turns) if turns else "No conversation today."

        prompt = f"""
Summarize today's conversation in 4-6 bullet-style sentences as a plain paragraph.
Focus on practical topics, goals, ideas, and what mattered today.
Do not use romantic language.

Conversation:
{convo}
""".strip()

        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=250,
            safety_identifier=self._safety_identifier(),
        )
        summary = getattr(response, "output_text", "").strip() or "Summary unavailable."
        self.db.upsert_daily_summary(day, summary)
        return summary

    def _append_plaintext_log(self, speaker: str, text: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with TXT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] {speaker}: {text}\n")


class CompanionApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x720")
        self.minsize(960, 640)

        self.db = MemoryDB(DB_PATH)
        self.engine = CompanionEngine(self.db)
        self.jobs: "queue.Queue[tuple[str, str]]" = queue.Queue()

        self._build_ui()
        self._load_history()
        self.after(150, self._poll_jobs)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0)

        left = ttk.Frame(self, padding=12)
        right = ttk.Frame(self, padding=12)
        footer = ttk.Frame(self, padding=12)

        left.grid(row=0, column=0, sticky="nsew")
        right.grid(row=0, column=1, sticky="nsew")
        footer.grid(row=1, column=0, columnspan=2, sticky="ew")

        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(left, text="Conversation", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        self.chat_box = tk.Text(left, wrap="word", font=("Segoe UI", 11), state="disabled")
        self.chat_box.grid(row=1, column=0, sticky="nsew")

        input_row = ttk.Frame(left)
        input_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        input_row.columnconfigure(0, weight=1)

        self.input_box = tk.Text(input_row, height=5, wrap="word", font=("Segoe UI", 11))
        self.input_box.grid(row=0, column=0, sticky="ew")
        self.input_box.bind("<Control-Return>", self._send_event)

        send_btn = ttk.Button(input_row, text="Send", command=self.send_message)
        send_btn.grid(row=0, column=1, padx=(8, 0), sticky="ns")

        ttk.Label(right, text="Stored Memory", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        self.memory_box = tk.Text(right, wrap="word", font=("Consolas", 10), state="disabled", height=16)
        self.memory_box.grid(row=1, column=0, sticky="nsew")

        mem_btns = ttk.Frame(right)
        mem_btns.grid(row=2, column=0, sticky="ew", pady=(8, 8))
        ttk.Button(mem_btns, text="Refresh Memory", command=self.refresh_memory_view).pack(side="left")
        ttk.Button(mem_btns, text="Add Fact", command=self.add_fact_dialog).pack(side="left", padx=6)
        ttk.Button(mem_btns, text="Delete Fact", command=self.delete_fact_dialog).pack(side="left")

        ttk.Label(right, text="Today's Summary", font=("Segoe UI", 14, "bold")).grid(
            row=3, column=0, sticky="nw"
        )

        self.summary_box = tk.Text(right, wrap="word", font=("Segoe UI", 10), state="disabled", height=10)
        self.summary_box.grid(row=4, column=0, sticky="nsew", pady=(8, 0))

        footer.columnconfigure(0, weight=1)
        ttk.Button(footer, text="Export Chat Log", command=self.export_log).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Export Memory Facts", command=self.export_facts).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(footer, text="Rebuild Today's Summary", command=self.rebuild_summary).grid(row=0, column=2, sticky="w")

        model_label = ttk.Label(footer, text=f"Model: {os.getenv('OPENAI_MODEL', DEFAULT_MODEL)}")
        model_label.grid(row=0, column=3, sticky="e")

        self.refresh_memory_view()
        self.refresh_summary_view()

    def _load_history(self) -> None:
        turns = self.db.get_recent_turns(limit=100)
        for t in turns:
            self._append_chat(t.role, t.content)

    def _append_chat(self, role: str, text: str) -> None:
        self.chat_box.configure(state="normal")
        prefix = "You" if role == "user" else "AI"
        self.chat_box.insert("end", f"{prefix}: {text}\n\n")
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def _set_summary(self, text: str) -> None:
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", text)
        self.summary_box.configure(state="disabled")

    def refresh_summary_view(self) -> None:
        day = date.today().isoformat()
        summary = self.db.get_summary_for_day(day) or "No summary yet."
        self._set_summary(summary)

    def refresh_memory_view(self) -> None:
        facts = self.db.get_facts(limit=100)
        self.memory_box.configure(state="normal")
        self.memory_box.delete("1.0", "end")
        if not facts:
            self.memory_box.insert("1.0", "No stored memory yet.")
        else:
            for fact in facts:
                self.memory_box.insert("end", f"{fact['key']} = {fact['value']}\n")
        self.memory_box.configure(state="disabled")

    def _send_event(self, event) -> str:
        self.send_message()
        return "break"

    def send_message(self) -> None:
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return

        if not os.getenv("OPENAI_API_KEY"):
            messagebox.showerror(
                "Missing API key",
                "Set OPENAI_API_KEY in your environment before starting the app."
            )
            return

        self.input_box.delete("1.0", "end")
        self._append_chat("user", text)

        def worker():
            try:
                reply = self.engine.chat(text)
                self.jobs.put(("reply", reply))
            except Exception as e:
                self.jobs.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_jobs(self) -> None:
        try:
            while True:
                kind, payload = self.jobs.get_nowait()
                if kind == "reply":
                    self._append_chat("assistant", payload)
                    self.refresh_memory_view()
                    self.refresh_summary_view()
                elif kind == "error":
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_jobs)

    def add_fact_dialog(self) -> None:
        win = tk.Toplevel(self)
        win.title("Add Fact")
        win.geometry("420x180")
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="Key").pack(anchor="w", padx=12, pady=(12, 4))
        key_entry = ttk.Entry(win)
        key_entry.pack(fill="x", padx=12)

        ttk.Label(win, text="Value").pack(anchor="w", padx=12, pady=(12, 4))
        value_entry = ttk.Entry(win)
        value_entry.pack(fill="x", padx=12)

        def save():
            key = key_entry.get().strip()
            value = value_entry.get().strip()
            if not key or not value:
                messagebox.showwarning("Missing data", "Enter both a key and a value.")
                return
            self.db.upsert_fact(key, value, source="manual")
            self.refresh_memory_view()
            win.destroy()

        ttk.Button(win, text="Save", command=save).pack(pady=12)

    def delete_fact_dialog(self) -> None:
        win = tk.Toplevel(self)
        win.title("Delete Fact")
        win.geometry("420x120")
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="Fact key to delete").pack(anchor="w", padx=12, pady=(12, 4))
        key_entry = ttk.Entry(win)
        key_entry.pack(fill="x", padx=12)

        def remove():
            key = key_entry.get().strip()
            if not key:
                return
            self.db.delete_fact(key)
            self.refresh_memory_view()
            win.destroy()

        ttk.Button(win, text="Delete", command=remove).pack(pady=12)

    def rebuild_summary(self) -> None:
        def worker():
            try:
                summary = self.engine.regenerate_today_summary()
                self.jobs.put(("reply", f"[Summary rebuilt]\n{summary}"))
            except Exception as e:
                self.jobs.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def export_log(self) -> None:
        target = filedialog.asksaveasfilename(
            title="Export chat log",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt")],
            initialfile=f"chat_log_{date.today().isoformat()}.txt"
        )
        if not target:
            return
        self.db.export_txt_log(Path(target))
        messagebox.showinfo("Done", "Chat log exported.")

    def export_facts(self) -> None:
        target = filedialog.asksaveasfilename(
            title="Export memory facts",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt")],
            initialfile=f"memory_facts_{date.today().isoformat()}.txt"
        )
        if not target:
            return
        self.db.export_facts_txt(Path(target))
        messagebox.showinfo("Done", "Memory facts exported.")


def main() -> None:
    app = CompanionApp()
    app.mainloop()


if __name__ == "__main__":
    main()
