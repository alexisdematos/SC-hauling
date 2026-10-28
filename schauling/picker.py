"""A search-as-you-type list, so names never need to be typed exactly."""
import re
import tkinter as tk
import unicodedata

BG = "#0b1620"
FIELD = "#13232f"
FG = "#d8e6f0"
DIM = "#6f8796"
SELECT = "#2b4a5e"
FONT = ("Segoe UI", 10)
ROWS = 12


def _norm(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", text.lower())


def matches(query, items):
    """Items whose words cover every query word (prefix match), best first.

    `items` is a list of (label, value). "nb int" finds "NB Int. Spaceport",
    "hur l1" finds "HUR-L1 Green Glade Station", "lev" finds "Levski".
    """
    q = _norm(query)
    if not q:
        return list(items)
    scored = []
    for label, value in items:
        words = _norm(label)
        if all(any(w.startswith(p) for w in words) for p in q):
            # Name starting with the query first, then shorter names.
            scored.append((0 if words[:1] and words[0].startswith(q[0]) else 1, len(label), label, value))
    return [(label, value) for _, _, label, value in sorted(scored)]


def pick(parent, title, items, current=None, extra=()):
    """Modal search list. Returns the chosen value, or None when cancelled.

    `extra` rows (label, value) stay on top of the list, such as
    ("(anywhere)", "").
    """
    result = {"value": None}
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=BG)
    win.attributes("-topmost", True)
    win.transient(parent)

    tk.Label(win, text=title, bg=BG, fg=DIM, font=FONT, anchor="w").pack(fill="x", padx=10, pady=(8, 2))
    query = tk.StringVar(value="")
    entry = tk.Entry(win, textvariable=query, bg=FIELD, fg=FG, insertbackground=FG, font=FONT,
                     relief="flat", width=46)
    entry.pack(fill="x", padx=10, ipady=4)
    box = tk.Listbox(win, bg=FIELD, fg=FG, font=FONT, height=ROWS, relief="flat", activestyle="none",
                     selectbackground=SELECT, selectforeground=FG, highlightthickness=0)
    box.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    shown = []

    def refresh(*_):
        shown[:] = list(extra) + matches(query.get(), items)
        box.delete(0, "end")
        for label, _ in shown:
            box.insert("end", label)
        if shown:
            # Typing selects the best match, not the fixed rows on top.
            first = len(extra) if query.get() and len(shown) > len(extra) else 0
            index = next((i for i, (_, v) in enumerate(shown) if v == current and not query.get()), first)
            box.selection_set(index)
            box.see(index)

    def move(step):
        if not shown:
            return "break"
        sel = box.curselection()
        i = max(0, min(len(shown) - 1, (sel[0] if sel else -1) + step))
        box.selection_clear(0, "end")
        box.selection_set(i)
        box.see(i)
        return "break"

    def choose(*_):
        sel = box.curselection()
        if sel:
            result["value"] = shown[sel[0]][1]
        win.destroy()

    query.trace_add("write", refresh)
    entry.bind("<Down>", lambda e: move(1))
    entry.bind("<Up>", lambda e: move(-1))
    entry.bind("<Next>", lambda e: move(ROWS))
    entry.bind("<Prior>", lambda e: move(-ROWS))
    entry.bind("<Return>", choose)
    box.bind("<Double-Button-1>", choose)
    box.bind("<Return>", choose)
    win.bind("<Escape>", lambda e: win.destroy())

    refresh()
    win.update_idletasks()
    x = parent.winfo_rootx() + 20
    y = parent.winfo_rooty() + 20
    win.geometry(f"+{x}+{y}")
    entry.focus_force()
    win.grab_set()
    parent.wait_window(win)
    return result["value"]
