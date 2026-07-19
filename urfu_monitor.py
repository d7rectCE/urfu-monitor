#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
УрФУ — монитор конкурсных списков

Читает публичный эндпоинт страницы https://urfu.ru/ru/alpha/full/
    GET https://urfu.ru/api/entrant/?page=1&size=50&search=<регномер>

Установка:
    pip install requests plyer
Запуск:
    python urfu_monitor.py
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, filedialog

import requests

API = "https://urfu.ru/api/entrant/"
CFG = os.path.join(os.path.expanduser("~"), ".urfu_monitor.json")
SNAP = os.path.join(os.path.expanduser("~"), ".urfu_monitor_snapshots.json")
PAGE_SIZE = 50
MAX_PAGES = 5

HEADERS = {
    "User-Agent": "urfu-monitor/1.0 (personal admission tracker)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://urfu.ru/ru/alpha/full/",
}

WATCH = ("total_mark", "status", "marks", "achievs", "edu_doc_original",
         "status_epgu", "isInCompetition", "priority", "avgm")

RU = {
    "total_mark": "сумма баллов",
    "status": "статус",
    "marks": "оценки за ВИ",
    "achievs": "инд. достижения",
    "edu_doc_original": "оригинал документа",
    "status_epgu": "статус ЕПГУ",
    "isInCompetition": "участие в конкурсе",
    "priority": "приоритет",
    "avgm": "средний балл",
}


class NotFound(Exception):
    """Номер не найден в выдаче API."""


# ------------------------------------------------------------- уведомления

def notify(title, message):
    try:
        from plyer import notification as _n
        _n.notify(title=title, message=message[:250],
                  app_name="УрФУ монитор", timeout=25)
        return
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            os.system('osascript -e {!r}'.format(
                'display notification "{}" with title "{}"'.format(
                    message.replace('"', "'")[:200], title)))
        elif sys.platform.startswith("linux"):
            os.system('notify-send {!r} {!r}'.format(title, message[:200]))
    except Exception:
        pass


def beep():
    """Короткий системный сигнал. Вызывать только из главного потока."""
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass


# ------------------------------------------------------------------ данные

_session = requests.Session()
_session.headers.update(HEADERS)


def fetch(regnum, timeout=20):
    """
    Возвращает (заявления, last_import) строго для указанного номера.

    Поиск в API идёт по подстроке, поэтому результат обязательно проверяется
    на точное совпадение regnum. Если совпадения нет — NotFound, но НИКОГДА
    не берётся чужая запись «за компанию».
    """
    regnum = str(regnum)
    last_import = None
    for page in range(1, MAX_PAGES + 1):
        r = _session.get(API, params={"page": page, "size": PAGE_SIZE,
                                      "search": regnum}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("result") != "success":
            raise RuntimeError(f"API вернул result={data.get('result')!r}")
        last_import = data.get("last_import", last_import)
        items = data.get("items") or []
        for it in items:
            if str(it.get("regnum")) == regnum:
                return it.get("applications") or [], last_import
        total = data.get("count") or 0
        if len(items) < PAGE_SIZE or page * PAGE_SIZE >= total:
            break
    raise NotFound(f"номер {regnum} не найден в выдаче")


def key_of(app):
    """Ключ заявления: конкурс + основа + программа + направление."""
    return "|".join(str(app.get(k, "")) for k in
                    ("competition", "compensation", "program", "speciality"))


def indexed(apps):
    """Ключ → заявление, с разведением возможных дублей."""
    out, seen = {}, {}
    for a in apps or []:
        k = key_of(a)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            k = f"{k}#{seen[k]}"
        out[k] = a
    return out


def marks_text(app):
    m = app.get("marks") or {}
    if not m:
        return "—"
    parts = []
    for name, info in m.items():
        val = info.get("mark") if isinstance(info, dict) else info
        parts.append(f"{name if len(name) <= 34 else name[:33] + '…'}: {val}")
    return "; ".join(parts)


def _short(s, n=40):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def diff_apps(old_list, new_list):
    """Человекочитаемые изменения между двумя снимками."""
    old, new = indexed(old_list), indexed(new_list)
    changes, changed_keys = [], set()

    for k, a in new.items():
        if k not in old:
            changes.append(f"➕ Новое заявление: {_short(a.get('program'))} "
                           f"({a.get('compensation')})")
            changed_keys.add(k)
            continue
        b = old[k]
        for f in WATCH:
            ov, nv = b.get(f), a.get(f)
            if ov == nv:
                continue
            if f == "marks":
                om = {n: (i.get("mark") if isinstance(i, dict) else i)
                      for n, i in (ov or {}).items()}
                nm = {n: (i.get("mark") if isinstance(i, dict) else i)
                      for n, i in (nv or {}).items()}
                if om == nm:
                    continue
                for name in sorted(set(om) | set(nm)):
                    if om.get(name) != nm.get(name):
                        changes.append(
                            f"📝 {_short(a.get('program'))}: «{_short(name, 45)}» "
                            f"{om.get(name, '—')} → {nm.get(name, '—')}")
            else:
                changes.append(f"📌 {_short(a.get('program'))} — "
                               f"{RU.get(f, f)}: {ov} → {nv}")
            changed_keys.add(k)

    for k, b in old.items():
        if k not in new:
            changes.append(f"➖ Заявление пропало: {_short(b.get('program'))}")

    return changes, changed_keys


# --------------------------------------------------------------------- GUI

COLS = [
    ("priority", "Приор.", 55),
    ("competition", "Конкурс", 165),
    ("compensation", "Основа", 115),
    ("institute", "Институт", 90),
    ("program", "Образовательная программа", 290),
    ("speciality", "Направление", 220),
    ("total_mark", "Сумма", 60),
    ("achievs", "ИД", 40),
    ("marks", "Вступительные испытания", 290),
    ("status", "Статус", 145),
    ("edu_doc_original", "Оригинал", 75),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("УрФУ — монитор конкурсных списков")
        self.geometry("1400x740")
        self.minsize(1000, 560)

        self.q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.stop_event.set()          # ни один поток не запущен

        self.cfg = self._load_json(CFG, {})
        self.snapshots = self._load_json(SNAP, {})   # {номер: [заявления]}
        self.active_num = None
        self.prev_import = None
        self.rows_cache = []
        self.changed_keys = set()
        self.changed_at = None

        self._build()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if self.cfg.get("autostart") and self.cfg.get("number"):
            self.after(600, self.start)

    # ------------------------------------------------------------ вёрстка

    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        st.configure("Big.TButton", font=("Segoe UI", 10, "bold"))

        bar = ttk.Frame(self, padding=(14, 12, 14, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Рег. номер").grid(row=0, column=0, sticky="w")
        self.v_num = tk.StringVar(value=self.cfg.get("number", ""))
        self.e_num = ttk.Entry(bar, textvariable=self.v_num, width=16,
                               font=("Segoe UI", 13))
        self.e_num.grid(row=1, column=0, padx=(0, 16))
        self.e_num.bind("<Return>", lambda _: self.check_now())

        ttk.Label(bar, text="Интервал, сек").grid(row=0, column=1, sticky="w")
        self.v_int = tk.StringVar(value=str(self.cfg.get("interval", 180)))
        self.sp_int = ttk.Spinbox(bar, from_=30, to=7200, increment=30, width=8,
                                  textvariable=self.v_int, font=("Segoe UI", 11))
        self.sp_int.grid(row=1, column=1, padx=(0, 16))

        self.v_auto = tk.BooleanVar(value=self.cfg.get("autostart", False))
        ttk.Checkbutton(bar, text="Автозапуск", variable=self.v_auto,
                        command=self._save_cfg).grid(row=1, column=2, padx=(0, 8))
        self.v_sound = tk.BooleanVar(value=self.cfg.get("sound", True))
        ttk.Checkbutton(bar, text="Звук", variable=self.v_sound,
                        command=self._save_cfg).grid(row=1, column=3, padx=(0, 8))
        self.v_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Только с баллами", variable=self.v_only,
                        command=self._refilter).grid(row=1, column=4, padx=(0, 16))

        self.b_start = ttk.Button(bar, text="▶  Старт", style="Big.TButton",
                                  command=self.start)
        self.b_start.grid(row=1, column=5, padx=3)
        self.b_stop = ttk.Button(bar, text="■  Стоп", command=self.stop,
                                 state="disabled")
        self.b_stop.grid(row=1, column=6, padx=3)
        self.b_once = ttk.Button(bar, text="⟳  Обновить", command=self.check_now)
        self.b_once.grid(row=1, column=7, padx=3)
        ttk.Button(bar, text="💾  Экспорт",
                   command=self.export).grid(row=1, column=8, padx=3)

        info = ttk.Frame(self, padding=(14, 0, 14, 8))
        info.pack(fill="x")
        self.lbl_status = ttk.Label(info, text="Готов", foreground="#555")
        self.lbl_status.pack(side="left")
        self.lbl_import = ttk.Label(info, text="", foreground="#888")
        self.lbl_import.pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        f1 = ttk.Frame(nb)
        nb.add(f1, text="Заявления")
        self.tree = ttk.Treeview(f1, show="headings",
                                 columns=[c[0] for c in COLS])
        for cid, title, w in COLS:
            self.tree.heading(cid, text=title)
            anchor = "center" if cid in ("priority", "total_mark", "achievs",
                                         "edu_doc_original") else "w"
            self.tree.column(cid, width=w, anchor=anchor,
                             stretch=(cid in ("program", "marks", "speciality")))
        vs = ttk.Scrollbar(f1, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(f1, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        f1.rowconfigure(0, weight=1)
        f1.columnconfigure(0, weight=1)
        # отдельные теги, чтобы подсветка не съедала цвет текста
        self.tree.tag_configure("incomp", foreground="#1a7f37")
        self.tree.tag_configure("pending", foreground="#888888")
        self.tree.tag_configure("chg_incomp", background="#fff3bf",
                                foreground="#1a7f37")
        self.tree.tag_configure("chg_pending", background="#fff3bf",
                                foreground="#7a6a20")

        f2 = ttk.Frame(nb)
        nb.add(f2, text="История изменений")
        self.txt = tk.Text(f2, wrap="word", font=("Consolas", 10),
                           bg="#1b1b1b", fg="#d8d8d8", insertbackground="#ddd",
                           padx=10, pady=8)
        sb = ttk.Scrollbar(f2, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ------------------------------------------------------------- очередь

    def log(self, msg):
        self.q.put(("log", f"[{datetime.now():%d.%m %H:%M:%S}] {msg}"))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.txt.insert("end", payload + "\n")
                    self.txt.see("end")
                elif kind == "status":
                    self.lbl_status.config(text=payload)
                elif kind == "import":
                    self.lbl_import.config(text=payload)
                elif kind == "rows":
                    rows, changed, stamp = payload
                    self.rows_cache = rows
                    if changed or stamp is None:
                        self.changed_keys = changed
                        self.changed_at = stamp
                    self._refilter()
                elif kind == "beep":
                    if self.v_sound.get():
                        beep()
                        self.bell()
                elif kind == "finished":
                    self._set_running(False)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _set_running(self, running):
        self.b_start.config(state="disabled" if running else "normal")
        self.b_stop.config(state="normal" if running else "disabled")
        self.b_once.config(state="disabled" if running else "normal")
        self.e_num.config(state="disabled" if running else "normal")
        self.sp_int.config(state="disabled" if running else "normal")

    def _refilter(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for a in self.rows_cache:
            if self.v_only.get() and not a.get("total_mark"):
                continue
            k = key_of(a)
            in_comp = bool(a.get("isInCompetition"))
            hot = any(ck == k or ck.startswith(k + "#")
                      for ck in self.changed_keys)
            if hot:
                tag = "chg_incomp" if in_comp else "chg_pending"
            else:
                tag = "incomp" if in_comp else "pending"
            self.tree.insert("", "end", tags=(tag,), values=(
                a.get("priority", ""),
                a.get("competition", ""),
                a.get("compensation", ""),
                a.get("institute", ""),
                a.get("program", ""),
                a.get("speciality", ""),
                a.get("total_mark", 0),
                a.get("achievs", 0),
                marks_text(a),
                a.get("status", ""),
                "да" if a.get("edu_doc_original") else "нет",
            ))
        if self.changed_at:
            self.lbl_status.config(
                text=self.lbl_status.cget("text") +
                     f"   ·   подсвечены изменения от {self.changed_at}")

    # ---------------------------------------------------------- управление

    def _spawn(self, num, interval):
        """Гарантированно гасит прошлый поток и запускает новый."""
        self.stop_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)
        ev = threading.Event()          # новый объект, старый поток его не видит
        self.stop_event = ev
        self._set_running(True)
        self.worker = threading.Thread(target=self._loop,
                                       args=(num, interval, ev), daemon=True)
        self.worker.start()

    def _validate(self):
        num = self.v_num.get().strip()
        if not num.isdigit():
            messagebox.showwarning("Номер", "Введите числовой рег. номер.")
            return None, None
        try:
            interval = max(30, int(self.v_int.get()))
        except (ValueError, tk.TclError):
            interval = 180
        return num, interval

    def start(self):
        num, interval = self._validate()
        if num is None:
            return
        self._save_cfg()
        self._prepare_number(num)
        self._spawn(num, interval)

    def check_now(self):
        num, _ = self._validate()
        if num is None:
            return
        self._save_cfg()
        self._prepare_number(num)
        self._spawn(num, None)

    def _prepare_number(self, num):
        """Сброс состояния при смене отслеживаемого номера."""
        if num != self.active_num:
            self.active_num = num
            self.prev_import = None
            self.changed_keys = set()
            self.changed_at = None
            self.rows_cache = []
            self._refilter()
            if num in self.snapshots:
                self.log(f"Номер {num}: сравниваю с сохранённым снимком.")
            else:
                self.log(f"Номер {num}: снимка ещё нет, "
                         f"первая загрузка будет базовой.")

    def stop(self):
        self.stop_event.set()
        self.log("Останавливаю мониторинг…")

    def export(self):
        if not self.rows_cache:
            messagebox.showinfo("Экспорт", "Нет данных для сохранения.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile=f"urfu_{self.active_num or 'export'}_"
                        f"{datetime.now():%Y%m%d_%H%M}.json")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.rows_cache, f, ensure_ascii=False, indent=2)
            self.log(f"Сохранено: {path}")

    # ------------------------------------------------------- рабочий поток

    def _loop(self, num, interval, ev):
        fails = 0
        try:
            while not ev.is_set():
                self.q.put(("status", "Запрашиваю данные…"))
                apps = last_import = None
                try:
                    apps, last_import = fetch(num)
                    fails = 0
                except NotFound:
                    self.log(f"Номер {num} не найден — проверьте, "
                             f"верно ли он введён.")
                    self.q.put(("status", "Номер не найден"))
                except requests.RequestException as e:
                    fails += 1
                    self.log(f"Сеть недоступна ({fails}): {e}")
                    self.q.put(("status", "Ошибка сети"))
                    if fails == 5:
                        notify("УрФУ монитор",
                               "5 неудачных запросов подряд — проверьте сеть")
                except Exception as e:
                    fails += 1
                    self.log(f"Неожиданный ответ API ({fails}): {e}")
                    self.q.put(("status", f"Ошибка: {e}"))

                if apps is not None:
                    try:
                        self._handle(num, apps, last_import)
                    except Exception as e:
                        self.log(f"Ошибка обработки данных: {e}")

                if interval is None or ev.is_set():
                    break
                nxt = time.time() + interval
                while not ev.is_set() and time.time() < nxt:
                    left = int(nxt - time.time())
                    self.q.put(("status", f"Следующая проверка через "
                                          f"{left // 60:02d}:{left % 60:02d}"))
                    ev.wait(1)
        finally:
            if not ev.is_set():
                ev.set()
            self.q.put(("status", "Ожидание" if interval is None
                        else "Мониторинг остановлен"))
            self.q.put(("finished", None))

    def _handle(self, num, apps, last_import):
        if last_import:
            try:
                dt = datetime.fromisoformat(last_import)
                self.q.put(("import",
                            f"Выгрузка вуза от {dt:%d.%m.%Y %H:%M}"))
            except ValueError:
                self.q.put(("import", f"last_import: {last_import}"))

        prev = self.snapshots.get(num)
        changed_keys, stamp = set(), None

        if prev is None:
            in_comp = sum(1 for a in apps if a.get("isInCompetition"))
            best = max((a.get("total_mark") or 0 for a in apps), default=0)
            self.log(f"Базовый снимок: заявлений {len(apps)}, "
                     f"в конкурсе {in_comp}, макс. балл {best}")
        else:
            changes, changed_keys = diff_apps(prev, apps)
            if changes:
                stamp = datetime.now().strftime("%d.%m %H:%M")
                self.log("═══ ИЗМЕНЕНИЯ ═══\n" + "\n".join(changes) + "\n")
                notify("УрФУ: изменения в списках!", "\n".join(changes[:4]))
                self.q.put(("beep", None))
            elif last_import != self.prev_import and self.prev_import:
                self.log("Выгрузка обновилась, по вашему номеру без изменений.")

        self.prev_import = last_import
        self.snapshots[num] = apps
        self._save_snapshots()
        self.q.put(("rows", (apps, changed_keys, stamp)))

    # --------------------------------------------------------- сохранение

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save_cfg(self):
        try:
            with open(CFG, "w", encoding="utf-8") as f:
                json.dump({"number": self.v_num.get().strip(),
                           "interval": self.v_int.get(),
                           "autostart": self.v_auto.get(),
                           "sound": self.v_sound.get()},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def _save_snapshots(self):
        try:
            tmp = SNAP + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshots, f, ensure_ascii=False)
            os.replace(tmp, SNAP)
        except Exception:
            pass

    def _close(self):
        self.stop_event.set()
        self._save_cfg()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
