#!/usr/bin/env python3
"""贡献控制台 contrib-console v0

用法:
  python3 console.py         启动服务,监听 localhost:7799
  python3 console.py sweep   只跑一次采集后退出(供 launchd/cron 用)

Python 3 标准库 only。数据抓取一律走本机已登录的 gh CLI。
"""
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

ME = "tomsen-ai"
PORT = 7799
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contrib.db")

BOT_RE = re.compile(r"bot\]|github-actions|omnigent-ci|copilot|dependabot|renovate", re.I)
LABEL_ALERT_RE = re.compile(r"needs-demo|help wanted|\bP[01]\b", re.I)
CI_IGNORE = {"Maintainer Approval"}  # 外部 PR 常驻门禁,不算异常
# pending/in-progress 不算 ci_bad:只有明确失败态才把球判给我
CI_BAD_STATES = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}

DDL = """
CREATE TABLE IF NOT EXISTS projects (
  repo TEXT PRIMARY KEY,
  tier TEXT,
  style TEXT,
  playbook TEXT,
  tz_hint TEXT,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  type TEXT NOT NULL,
  title TEXT, url TEXT,
  my_role TEXT NOT NULL,
  note TEXT,
  state TEXT,
  labels TEXT,
  assignees TEXT,
  ci_bad TEXT,
  review_decision TEXT,
  last_actor TEXT,
  last_actor_is_me INTEGER,
  last_activity_at TEXT,
  last_activity_summary TEXT,
  created_at TEXT, updated_at TEXT,
  seen_at TEXT,
  others_replied INTEGER,
  triage TEXT DEFAULT 'unread',
  snooze_until TEXT,
  next_action TEXT,
  UNIQUE(repo, number)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT, summary TEXT,
  seen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

SEED_PROJECTS = [
    ("omnigent-ai/omnigent", "主战场", "静默动作型",
     "issue+PR同发,不写小作文,看标签行事", "亚太维护者北京8-17时;dhruv北京凌晨1-8时"),
    ("OpenHands/software-agent-sdk", "副战场", "外场社区型", "Vasco三幕剧第二幕:周1-2个fix", ""),
    ("OpenHands/OpenHands", "副战场", "外场社区型", "只跟踪不主动投", ""),
    ("OpenHands/OpenHands-CLI", "副战场", "外场社区型", "SDK线可选延伸", ""),
]

SEED_WATCHERS = [
    ("omnigent-ai/omnigent", 1808, "pr", "合并后立刻 issue+PR 修 tool-part 丢失"),
    ("omnigent-ai/omnigent", 1657, "issue", "我报的P0,SabhyaC26 在修"),
    ("omnigent-ai/omnigent", 1748, "issue", "我报的P1,被 #1807 抢修"),
    ("omnigent-ai/omnigent", 1778, "issue", "我报的P1,被 #1808 抢修"),
    ("omnigent-ai/omnigent", 1807, "pr", "Arshgill01 用了我的 session/cancel 方案"),
    ("omnigent-ai/omnigent", 1526, "issue", "god-file 分解,help wanted 挂着,中期目标"),
    ("OpenHands/software-agent-sdk", 3906, "issue", "我报的性能issue,排期连击 PR"),
]

SWEEP_LOCK = threading.Lock()


# ---------------------------------------------------------------- db helpers

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(DDL)
    for stmt in ("ALTER TABLE items ADD COLUMN others_replied INTEGER",
                 "ALTER TABLE projects ADD COLUMN sort_order INTEGER",
                 "ALTER TABLE items ADD COLUMN linked TEXT"):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列已存在
    if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO projects (repo, tier, style, playbook, tz_hint, active) VALUES (?,?,?,?,?,1)",
            SEED_PROJECTS)
        conn.executemany(
            "INSERT OR IGNORE INTO items (repo, number, type, my_role, note) VALUES (?,?,?,'watcher',?)",
            SEED_WATCHERS)
        conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def meta_get_json(conn, k):
    v = meta_get(conn, k)
    return json.loads(v) if v else []


# ---------------------------------------------------------------- gh wrapper

def gh_json(args, errors=None):
    """跑 gh 命令,返回解析后的 JSON;失败返回 None 并记 errors。"""
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
    except Exception as e:
        if errors is not None:
            errors.append(f"gh {' '.join(args[:3])}: {e}")
        return None
    if p.returncode != 0:
        if errors is not None:
            errors.append(f"gh {' '.join(args[:4])}: {p.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        if errors is not None:
            errors.append(f"gh {' '.join(args[:4])}: 非 JSON 输出")
        return None


# ---------------------------------------------------------------- sweep

def _clean_body(body):
    """摘要用:去 HTML 标签、压空白,截150字符;纯图片评论给个占位。"""
    text = re.sub(r"<[^>]+>", "", body or "")
    text = re.sub(r"\s+", " ", text).strip()[:150]
    if not text and (body or "").strip():
        return "(图片/附件)"
    return text


def _parse_timeline(data, is_pr):
    """评论+review 合并时间线,滤掉机器人。
    返回 (最后一条人工动作 | None, 是否有过我以外的人工回复)。"""
    entries = []
    for c in data.get("comments") or []:
        author = (c.get("author") or {}).get("login") or ""
        if not author or BOT_RE.search(author):
            continue
        entries.append((c.get("createdAt") or "", author, _clean_body(c.get("body")), "comment"))
    if is_pr:
        for r in data.get("reviews") or []:
            author = (r.get("author") or {}).get("login") or ""
            if not author or BOT_RE.search(author):
                continue
            body = _clean_body(r.get("body"))
            summary = body if body else f"review: {r.get('state', '?')}"
            entries.append((r.get("submittedAt") or "", author, summary, "review"))
    others_replied = 1 if any(e[1] != ME for e in entries) else 0
    if not entries:
        return None, 0
    entries.sort(key=lambda e: e[0])
    return entries[-1], others_replied  # (ts, actor, summary, kind), 0/1


def _parse_ci_bad(data):
    bad = []
    for c in data.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or "?"
        if name in CI_IGNORE:
            continue
        verdict = (c.get("conclusion") or c.get("state") or "").upper()
        if verdict in CI_BAD_STATES:
            bad.append(name)
    return sorted(set(bad))


def refresh_item(conn, item, errors):
    """抓取单个条目最新状态,diff 产事件,更新库。"""
    repo, number, typ = item["repo"], item["number"], item["type"]
    is_pr = typ == "pr"
    if is_pr:
        fields = ("number,title,url,state,labels,assignees,reviewDecision,statusCheckRollup,"
                  "comments,reviews,createdAt,updatedAt,mergedAt,closingIssuesReferences")
        data = gh_json(["pr", "view", str(number), "--repo", repo, "--json", fields], errors)
    else:
        fields = "number,title,url,state,labels,assignees,comments,createdAt,updatedAt"
        data = gh_json(["issue", "view", str(number), "--repo", repo, "--json", fields], errors)
    if data is None:
        return

    new_state = data.get("state") or ""
    labels = sorted(l["name"] for l in data.get("labels") or [])
    assignees = sorted(a["login"] for a in data.get("assignees") or [])
    ci_bad = _parse_ci_bad(data) if is_pr else []
    review_decision = data.get("reviewDecision") or None
    linked = sorted({x["number"] for x in data.get("closingIssuesReferences") or []}) if is_pr else []
    last, others_replied = _parse_timeline(data, is_pr)

    events = []  # (kind, actor, summary, ts)
    ts = now_iso()

    old_state = item["state"]
    if old_state and new_state != old_state:
        events.append(("state_change", None, f"{old_state} → {new_state}", ts))
        if item["my_role"] == "watcher":
            note = item["note"] or ""
            events.append(("watch_trigger", None,
                           f"#{number} 已{'合并' if new_state == 'MERGED' else '关闭' if new_state == 'CLOSED' else new_state} → note:{note}", ts))

    if last:
        la_ts, la_actor, la_summary, la_kind = last
        if item["last_activity_at"] is None or la_ts > item["last_activity_at"]:
            events.append(("new_review" if la_kind == "review" else "new_comment",
                           la_actor, la_summary, la_ts or ts))

    if is_pr and item["ci_bad"] is not None:
        old_bad = json.loads(item["ci_bad"])
        if set(old_bad) != set(ci_bad):
            summary = ("CI 非绿:" + ", ".join(ci_bad)) if ci_bad else "CI 恢复全绿"
            events.append(("ci_change", None, summary, ts))

    if item["labels"] is not None:
        old_labels = set(json.loads(item["labels"]))
        added = [l for l in labels if l not in old_labels]
        alert = [l for l in added if LABEL_ALERT_RE.search(l)]
        if alert:
            events.append(("label_change", None, "+" + ", +".join(alert), ts))

    if item["assignees"] is not None:
        old_a = json.loads(item["assignees"])
        if old_a != assignees:
            events.append(("assignee_change", None,
                           f"assignees: {', '.join(old_a) or '(无)'} → {', '.join(assignees) or '(无)'}", ts))

    conn.execute("""UPDATE items SET title=?, url=?, state=?, labels=?, assignees=?, ci_bad=?,
                    review_decision=?, linked=?, last_actor=?, last_actor_is_me=?, last_activity_at=?,
                    last_activity_summary=?, others_replied=?, created_at=?, updated_at=? WHERE id=?""",
                 (data.get("title"), data.get("url"), new_state,
                  json.dumps(labels, ensure_ascii=False),
                  json.dumps(assignees, ensure_ascii=False),
                  json.dumps(ci_bad, ensure_ascii=False) if is_pr else json.dumps([]),
                  review_decision, json.dumps(linked),
                  last[1] if last else None,
                  (1 if last[1] == ME else 0) if last else None,
                  last[0] if last else None,
                  last[2] if last else None,
                  others_replied,
                  data.get("createdAt"), data.get("updatedAt"), item["id"]))
    for kind, actor, summary, ets in events:
        conn.execute("INSERT INTO events (item_id, ts, kind, actor, summary) VALUES (?,?,?,?,?)",
                     (item["id"], ets, kind, actor, summary))
    if events:
        # 有新动静的已结条目回到未读,避免被 done 永久吞掉
        conn.execute("UPDATE items SET triage='unread' WHERE id=? AND triage='done'", (item["id"],))
    conn.commit()


def sweep(verbose=False):
    """完整采集一轮。返回 errors 列表。"""
    errors = []
    conn = db()
    active_repos = {r["repo"] for r in conn.execute("SELECT repo FROM projects WHERE active=1")}
    known_repos = {r["repo"] for r in conn.execute("SELECT repo FROM projects")}
    ignored = set(meta_get_json(conn, "ignored_repos"))
    pending = set(meta_get_json(conn, "pending_repos"))

    # 1. 自动发现
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    discovered = []  # (repo, number, type)
    res = gh_json(["search", "prs", "--author", ME, "--state", "open",
                   "--json", "repository,number", "--limit", "100"], errors)
    for it in res or []:
        discovered.append((it["repository"]["nameWithOwner"], it["number"], "pr"))
    res = gh_json(["search", "issues", "--author", ME, "--state", "open",
                   "--json", "repository,number", "--limit", "100"], errors)
    for it in res or []:
        discovered.append((it["repository"]["nameWithOwner"], it["number"], "issue"))
    res = gh_json(["search", "prs", "--author", ME, "--state", "closed",
                   "--json", "repository,number,closedAt", "--limit", "30"], errors)
    for it in res or []:
        if (it.get("closedAt") or "") >= week_ago:
            discovered.append((it["repository"]["nameWithOwner"], it["number"], "pr"))

    for repo, number, typ in discovered:
        if repo.startswith(ME + "/"):
            continue  # 硬排除:自有仓库(测试沙箱噪音)
        if repo in active_repos:
            conn.execute("INSERT OR IGNORE INTO items (repo, number, type, my_role) VALUES (?,?,?,'author')",
                         (repo, number, typ))
        elif repo not in known_repos and repo not in ignored:
            pending.add(repo)
    meta_set(conn, "pending_repos", json.dumps(sorted(pending - ignored - known_repos)))
    conn.commit()

    # 2+3. 逐条抓取(open 或还没抓过的;已结条目不再刷。
    #      linked IS NULL 的已结 PR 补抓一次,回填 issue 关联)
    rows = conn.execute("""SELECT * FROM items WHERE state IS NULL OR state='OPEN'
                              OR (linked IS NULL AND type='pr')
                           ORDER BY repo, number""").fetchall()
    for i, item in enumerate(rows):
        if verbose:
            print(f"[{i + 1}/{len(rows)}] {item['repo']}#{item['number']}", flush=True)
        refresh_item(conn, item, errors)
        if i < len(rows) - 1:
            time.sleep(0.5)  # 防限速

    meta_set(conn, "last_sweep_at", now_iso())
    meta_set(conn, "last_sweep_errors", json.dumps(errors, ensure_ascii=False))
    conn.commit()
    conn.close()
    return errors


# ---------------------------------------------------------------- 派生逻辑

def compute_ball(it):
    """OPEN 条目的球权。mine=该我动;theirs=等对面。"""
    if it["state"] != "OPEN":
        return None
    if it["last_actor_is_me"] == 0:
        return "mine"
    if it["review_decision"] == "CHANGES_REQUESTED":
        return "mine"
    if it["ci_bad"]:
        return "mine"
    return "theirs"


def build_dashboard(conn):
    now = now_iso()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    projects = [dict(r) for r in conn.execute(
        "SELECT * FROM projects ORDER BY COALESCE(sort_order, rowid), rowid")]
    unseen = {r["item_id"]: r["n"] for r in
              conn.execute("SELECT item_id, COUNT(*) n FROM events WHERE seen=0 GROUP BY item_id")}

    items = []
    for r in conn.execute("SELECT * FROM items ORDER BY repo, number"):
        it = dict(r)
        it["labels"] = json.loads(it["labels"]) if it["labels"] else []
        it["assignees"] = json.loads(it["assignees"]) if it["assignees"] else []
        it["ci_bad"] = json.loads(it["ci_bad"]) if it["ci_bad"] else []
        it["linked"] = json.loads(it["linked"]) if it["linked"] else []
        it["ball"] = compute_ball(it)
        it["unseen_events"] = unseen.get(it["id"], 0)
        it["events"] = None  # 占位,循环后统一填(events_by_item 在下方构建)
        it["unread"] = bool(it["unseen_events"]) or bool(
            it["last_activity_at"] and (not it["seen_at"] or it["last_activity_at"] > it["seen_at"]))
        ref = it["last_activity_at"] or it["created_at"]
        it["age_days"] = None
        if ref:
            try:
                dt = datetime.strptime(ref, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                it["age_days"] = round((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1)
            except ValueError:
                pass
        items.append(it)

    def snoozed(it):
        return bool(it["snooze_until"]) and it["snooze_until"] > now

    todo = [it for it in items if it["ball"] == "mine" and it["triage"] != "done" and not snoozed(it)]
    todo.sort(key=lambda x: x["last_activity_at"] or x["created_at"] or "")

    stale = []
    for it in items:
        if it["ball"] != "theirs":
            continue
        if it["review_decision"] == "APPROVED":
            it["wait_kind"] = "等合并"
        elif not it["others_replied"]:
            it["wait_kind"] = "等首审"  # 除我以外 0 条人工回复
        else:
            it["wait_kind"] = "等复审"
        stale.append(it)
    stale.sort(key=lambda x: -(x["age_days"] or 0))

    closed_week = [it for it in items if it["state"] in ("MERGED", "CLOSED")
                   and (it["updated_at"] or "") >= week_ago]
    closed_week.sort(key=lambda x: x["updated_at"] or "", reverse=True)

    in_zones = {it["id"] for it in todo} | {it["id"] for it in stale} | {it["id"] for it in closed_week}
    idle = [it for it in items if it["id"] not in in_zones]

    triggers = [dict(r) for r in conn.execute(
        """SELECT e.id AS event_id, e.item_id, e.ts, e.summary, i.repo, i.number, i.type,
                  i.title, i.url, i.note
           FROM events e JOIN items i ON i.id = e.item_id
           WHERE e.kind='watch_trigger' AND e.seen=0 ORDER BY e.ts DESC""")]

    # 每条 item 最近的事件记录(任务展开时的"记录"时间线)
    events_by_item = {}
    for e in conn.execute("""SELECT item_id, ts, kind, actor, summary, seen FROM events
                             ORDER BY ts DESC LIMIT 400"""):
        lst = events_by_item.setdefault(e["item_id"], [])
        if len(lst) < 15:
            lst.append(dict(e))
    for it in items:
        it["events"] = events_by_item.get(it["id"], [])

    return {
        "todo": todo,
        "watch_triggers": triggers,
        "stale": stale,
        "closed_week": closed_week,
        "idle": idle,
        "idle_count": len(idle),
        "projects": projects,
        "pending_repos": meta_get_json(conn, "pending_repos"),
        "last_sweep_at": meta_get(conn, "last_sweep_at"),
        "last_sweep_errors": meta_get_json(conn, "last_sweep_errors"),
        "now": now,
    }


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/dashboard":
            conn = db()
            try:
                self._json(build_dashboard(conn))
            finally:
                conn.close()
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _route_post(self):
        path = self.path
        if path == "/api/refresh":
            with SWEEP_LOCK:
                errors = sweep()
            conn = db()
            try:
                d = build_dashboard(conn)
                d["sweep_errors"] = errors
                self._json(d)
            finally:
                conn.close()
            return

        m = re.match(r"^/api/item/(\d+)/seen$", path)
        if m:
            item_id = int(m.group(1))
            conn = db()
            try:
                conn.execute("UPDATE items SET seen_at=? WHERE id=?", (now_iso(), item_id))
                conn.execute("UPDATE events SET seen=1 WHERE item_id=?", (item_id,))
                conn.commit()
                self._json({"ok": True})
            finally:
                conn.close()
            return

        m = re.match(r"^/api/item/(\d+)/triage$", path)
        if m:
            item_id = int(m.group(1))
            b = self._body()
            if b.get("triage") not in ("unread", "todo", "done"):
                self._json({"error": "triage 必须是 unread|todo|done"}, 400)
                return
            conn = db()
            try:
                conn.execute("UPDATE items SET triage=?, snooze_until=?, next_action=? WHERE id=?",
                             (b["triage"], b.get("snooze_until"), b.get("next_action"), item_id))
                conn.commit()
                self._json({"ok": True})
            finally:
                conn.close()
            return

        if path == "/api/watch":
            b = self._body()
            repo, number, typ = b.get("repo", "").strip(), b.get("number"), b.get("type")
            if not repo or not number or typ not in ("pr", "issue"):
                self._json({"error": "需要 repo/number/type(pr|issue)"}, 400)
                return
            conn = db()
            try:
                conn.execute("""INSERT INTO items (repo, number, type, my_role, note) VALUES (?,?,?,'watcher',?)
                                ON CONFLICT(repo, number) DO UPDATE SET note=excluded.note""",
                             (repo, int(number), typ, b.get("note") or ""))
                conn.commit()
                row = conn.execute("SELECT * FROM items WHERE repo=? AND number=?", (repo, int(number))).fetchone()
                errors = []
                refresh_item(conn, row, errors)  # 立即抓一次,不等下轮 sweep
                self._json({"ok": True, "errors": errors})
            finally:
                conn.close()
            return

        if path == "/api/project":
            b = self._body()
            repo = b.get("repo", "").strip()
            if not repo:
                self._json({"error": "需要 repo"}, 400)
                return
            conn = db()
            try:
                conn.execute("""INSERT INTO projects (repo, tier, style, playbook, tz_hint, active)
                                VALUES (?,?,?,?,?,1)
                                ON CONFLICT(repo) DO UPDATE SET tier=excluded.tier, style=excluded.style,
                                  playbook=excluded.playbook, tz_hint=excluded.tz_hint, active=1""",
                             (repo, b.get("tier") or "", b.get("style") or "",
                              b.get("playbook") or "", b.get("tz_hint") or ""))
                pending = [r for r in meta_get_json(conn, "pending_repos") if r != repo]
                meta_set(conn, "pending_repos", json.dumps(pending))
                conn.commit()
                self._json({"ok": True})
            finally:
                conn.close()
            return

        if path == "/api/projects/order":
            repos = self._body().get("repos") or []
            conn = db()
            try:
                for i, repo in enumerate(repos):
                    conn.execute("UPDATE projects SET sort_order=? WHERE repo=?", (i, repo))
                conn.commit()
                self._json({"ok": True})
            finally:
                conn.close()
            return

        if path == "/api/project/ignore":
            b = self._body()
            repo = b.get("repo", "").strip()
            conn = db()
            try:
                ignored = set(meta_get_json(conn, "ignored_repos"))
                ignored.add(repo)
                meta_set(conn, "ignored_repos", json.dumps(sorted(ignored)))
                pending = [r for r in meta_get_json(conn, "pending_repos") if r != repo]
                meta_set(conn, "pending_repos", json.dumps(pending))
                conn.commit()
                self._json({"ok": True})
            finally:
                conn.close()
            return

        self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------- 页面

PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>贡献控制台</title>
<style>
  :root {
    --bg: #0a0b0d; --panel: #131416; --hover: #17181b;
    --line: rgba(255,255,255,.07); --line2: rgba(255,255,255,.14);
    --fg: #eeeff1; --dim: #8a8f98; --faint: #5e636e;
    --accent: #5e6ad2; --red: #f2555a; --green: #4cb782; --yellow: #d9a344;
  }
  * { box-sizing: border-box; }
  html { -webkit-font-smoothing: antialiased; }
  body { background: var(--bg); color: var(--fg); margin: 0;
         font: 13px/1.6 "Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
         letter-spacing: .01em; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 30px 24px 90px; }
  header { display: flex; align-items: center; gap: 14px; }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  #sweepinfo { color: var(--faint); font-size: 12px; margin-left: auto; }
  button { display: inline-flex; align-items: center; gap: 5px;
           background: rgba(255,255,255,.04); color: var(--dim);
           border: 1px solid var(--line); border-radius: 6px;
           padding: 2px 10px; font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap;
           transition: color .12s, border-color .12s, background .12s; }
  button:hover { color: var(--fg); border-color: var(--line2); background: rgba(255,255,255,.07); }
  button:disabled { opacity: .45; cursor: wait; }
  a { color: inherit; text-decoration: none; }
  .ic { width: 13px; height: 13px; flex: none; }
  h2 { display: flex; align-items: center; gap: 7px;
       font-size: 12px; font-weight: 500; color: var(--dim); margin: 26px 0 4px;
       padding-bottom: 7px; border-bottom: 1px solid var(--line); }
  .count { color: var(--faint); font-weight: 400; }
  .item { padding: 8px 10px 8px 18px; border-radius: 8px; position: relative; transition: background .12s; }
  .item:hover { background: rgba(255,255,255,.03); }
  .item.unread::before { content: ""; position: absolute; left: 5px; top: 17px;
                         width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
  .l1 { display: flex; align-items: baseline; gap: 10px; }
  .ref { font-size: 12px; color: var(--faint); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .title { font-weight: 500; font-size: 13px; min-width: 0;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  a.title:hover { color: #fff; }
  .spacer { flex: 1; }
  .age { color: var(--faint); font-size: 11.5px; white-space: nowrap; }
  .acts { display: flex; gap: 4px; opacity: 0; transition: opacity .12s; }
  .item:hover .acts { opacity: 1; }
  .l2 { color: var(--dim); font-size: 12px; margin-top: 1px;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .l2 b { color: var(--fg); font-weight: 500; }
  .note { color: var(--yellow); }
  .pill { display: inline-flex; align-items: center; gap: 4px;
          font-size: 11px; padding: 1px 7px; border-radius: 5px; white-space: nowrap; }
  .pill .ic { width: 11.5px; height: 11.5px; }
  .pill.red { color: var(--red); background: rgba(242,85,90,.09); }
  .pill.green { color: var(--green); background: rgba(76,183,130,.09); }
  .pill.yellow { color: var(--yellow); background: rgba(217,163,68,.09); }
  .pill.gray { color: var(--dim); background: rgba(255,255,255,.05); }
  .empty { color: var(--faint); padding: 8px 18px; font-size: 12.5px; }
  button.warn { color: var(--yellow); border-color: rgba(217,163,68,.3);
                background: rgba(217,163,68,.06); }
  button.warn:hover { color: var(--yellow); border-color: rgba(217,163,68,.5);
                      background: rgba(217,163,68,.1); }
  form.inline { display: flex; gap: 6px; flex-wrap: wrap; padding: 8px 18px 2px; }
  form.inline input, form.inline select { background: var(--panel); color: var(--fg);
      border: 1px solid var(--line); border-radius: 6px; padding: 3px 9px; font: inherit;
      font-size: 12px; outline: none; }
  form.inline input:focus, form.inline select:focus { border-color: var(--accent); }
  #summary { display: flex; align-items: center; gap: 16px; color: var(--dim);
             font-size: 12.5px; margin: 22px 2px 0; }
  #summary span { display: inline-flex; align-items: center; gap: 6px; }
  #summary b { color: var(--fg); font-weight: 600; }
  #cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
           gap: 14px; margin-top: 16px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
          padding: 14px 16px 15px; cursor: pointer; position: relative;
          box-shadow: 0 1px 2px rgba(0,0,0,.25);
          transition: border-color .13s, background .13s; }
  .card:hover { background: var(--hover); border-color: var(--line2); }
  .card.dragging { opacity: .35; }
  .card h3 { margin: 0; font-size: 13px; font-weight: 600; }
  .card h3 .owner { color: var(--faint); font-weight: 400; }
  .card .cmeta { color: var(--faint); font-size: 11.5px; margin: 2px 0 11px;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card .cbadges { display: flex; flex-wrap: wrap; gap: 6px; min-height: 20px; }
  .card.unread::after { content: ""; position: absolute; right: 14px; top: 16px;
                        width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
  .overlay { position: fixed; inset: 0; background: rgba(4,5,7,.62); z-index: 10;
             backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px);
             display: flex; align-items: flex-start; justify-content: center;
             padding: 52px 18px; overflow-y: auto; }
  .modal { background: #101114; border: 1px solid var(--line2); border-radius: 12px;
           width: 100%; max-width: 820px; padding: 4px 24px 22px; margin-bottom: 40px;
           box-shadow: 0 24px 80px rgba(0,0,0,.55); }
  .mhead { display: flex; align-items: center; gap: 12px; margin-top: 18px; }
  .mhead h3 { font-size: 14px; margin: 0; font-weight: 600; }
  .mhead .cmeta { color: var(--dim); font-size: 12px; min-width: 0;
                  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mclose { margin-left: auto; }
  .mbar { display: flex; align-items: center; gap: 6px; padding: 14px 0 0; }
  .mbar .mrepo { color: var(--faint); font-size: 12px; }
  .thead, .tr1 { display: grid; grid-template-columns: 76px minmax(0,1fr) 92px 120px 78px;
                 gap: 10px; align-items: center; }
  .thead { color: var(--faint); font-size: 11px; border-bottom: 1px solid var(--line);
           padding: 6px 12px; margin-top: 12px; }
  .trow { padding: 9px 12px 10px; border-radius: 8px; cursor: pointer; transition: background .12s; }
  .trow + .trow { border-top: 1px solid rgba(255,255,255,.04); }
  .trow:hover { background: rgba(255,255,255,.035); }
  .trow.unread { background: rgba(94,106,210,.07); box-shadow: inset 2px 0 0 var(--accent); }
  .trow.unread:hover { background: rgba(94,106,210,.11); }
  .tr2 { color: var(--faint); font-size: 12px; margin-top: 3px; padding-left: 86px;
         overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tr2 b { color: var(--dim); font-weight: 500; }
  .ttitle { font-size: 12.5px; font-weight: 500; min-width: 0; overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; }
  .ttitle .ic { width: 12px; height: 12px; color: var(--yellow); vertical-align: -2px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip { font-size: 11px; padding: 0 6px; border-radius: 5px; background: rgba(255,255,255,.05);
          color: var(--dim); font-variant-numeric: tabular-nums; }
  .chip:hover { background: rgba(255,255,255,.1); color: var(--fg); }
  .chip.copen { color: var(--green); }
  .chip.cmerged { color: #a78bfa; }
  .chip.cclosed { color: var(--faint); }
  .chip.cunread { color: var(--accent); background: rgba(94,106,210,.13); }
  .tdetail { margin: 2px 0 10px 10px; padding: 2px 4px; border-left: 2px solid var(--line); }
  .tlog { margin: 6px 4px 2px 18px; }
  .tlogrow { display: flex; gap: 10px; align-items: baseline; font-size: 12px;
             color: var(--dim); padding: 2px 0; }
  .tlogrow.fresh .tkind { color: var(--accent); }
  .tkind { flex: none; width: 52px; color: var(--faint); font-size: 11px; }
  .tlogrow .age { flex: none; width: 76px; }
  .tsum { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tsum b { color: var(--fg); font-weight: 500; }
  #errors { color: var(--red); font-size: 12px; margin-top: 8px; white-space: pre-wrap; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>贡献控制台</h1>
  <button id="refreshbtn" onclick="doRefresh()">立即扫描</button>
  <span id="sweepinfo"></span>
</header>
<div id="errors"></div>
<div id="summary"></div>
<div id="cards"></div>
</div>
<div id="modalroot"></div>
<script>
let D = null;
let openRepo = null;
let dragEl = null;
let pendingOpen = false;   // 收编弹层
let adoptRepo = null;      // 正在填收编表单的 repo
let expandedTask = null;   // 任务表里展开详情的任务 key
let watchFormOpen = false; // 添加盯梢表单是否展开

// 手绘 SVG 图标(16x16 线稿)
const ICONS = {
  ball:  '<circle cx="8" cy="8" r="5.6"/><circle cx="8" cy="8" r="2.1" fill="currentColor" stroke="none"/>',
  eye:   '<path d="M1.7 8C3.1 5.2 5.4 3.7 8 3.7S12.9 5.2 14.3 8C12.9 10.8 10.6 12.3 8 12.3S3.1 10.8 1.7 8Z"/><circle cx="8" cy="8" r="1.7"/>',
  clock: '<circle cx="8" cy="8" r="5.6"/><path d="M8 5.1V8l2.1 1.5"/>',
  check: '<circle cx="8" cy="8" r="5.6"/><path d="M5.5 8.2l1.7 1.7 3.2-3.6"/>',
  idle:  '<circle cx="8" cy="8" r="5.6"/><path d="M5.4 8h5.2"/>',
  x:     '<path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8"/>',
};
function icon(name) {
  return '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"'
    + ' stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</svg>';
}

// 卡片拖拽排序:拖动时移动真实节点,松手后把新顺序存回服务端
function dragStart(e) {
  dragEl = e.currentTarget;
  e.dataTransfer.effectAllowed = "move";
  setTimeout(() => dragEl && dragEl.classList.add("dragging"), 0);
}
function dragOverCard(e) {
  e.preventDefault();
  const t = e.currentTarget;
  if (!dragEl || t === dragEl) return;
  const kids = [...t.parentNode.children];
  const after = kids.indexOf(dragEl) < kids.indexOf(t);
  t.parentNode.insertBefore(dragEl, after ? t.nextSibling : t);
}
async function dragEnd() {
  if (!dragEl) return;
  dragEl.classList.remove("dragging");
  dragEl = null;
  const repos = [...document.querySelectorAll("#cards .card")].map(c => c.dataset.repo);
  await api("/api/projects/order", {repos});
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return (d.getMonth()+1).toString().padStart(2,"0") + "-" + d.getDate().toString().padStart(2,"0")
    + " " + d.getHours().toString().padStart(2,"0") + ":" + d.getMinutes().toString().padStart(2,"0");
}
function fmtAge(days) {
  if (days == null) return "-";
  if (days < 1) return Math.max(1, Math.round(days*24)) + "小时";
  return Math.floor(days) + "天";
}

async function api(path, body) {
  const r = await fetch(path, body !== undefined
    ? {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)}
    : undefined);
  return r.json();
}
async function load() { D = await api("/api/dashboard"); render(); }
async function reload() { D = await api("/api/dashboard"); render(); }
async function doRefresh() {
  const b = document.getElementById("refreshbtn");
  b.disabled = true; b.textContent = "扫描中…";
  try { D = await api("/api/refresh", {}); render(); }
  catch (e) { document.getElementById("errors").textContent = "刷新失败: " + e; }
  b.disabled = false; b.textContent = "立即扫描";
}

async function markSeen(id) { await api("/api/item/" + id + "/seen", {}); reload(); }
async function markTodo(id) {
  const note = prompt("待办备注(下一步做什么):", "");
  if (note === null) return;
  await api("/api/item/" + id + "/triage", {triage: "todo", next_action: note});
  reload();
}
async function markDone(id) {
  await api("/api/item/" + id + "/triage", {triage: "done"});
  reload();
}
async function snoozeItem(id) {
  const days = prompt("暂缓几天?", "3");
  if (days === null) return;
  const until = new Date(Date.now() + (parseFloat(days) || 1) * 864e5).toISOString();
  await api("/api/item/" + id + "/triage", {triage: "todo", snooze_until: until});
  reload();
}
function openPending() { openRepo = null; pendingOpen = true; renderModal(); }
function adopt(repo) { adoptRepo = repo; renderModal(); }
async function adoptSubmit(ev) {
  ev.preventDefault();
  const f = ev.target;
  await api("/api/project", {
    repo: f.dataset.repo, tier: f.tier.value, style: f.style.value,
    playbook: f.playbook.value, tz_hint: f.tz.value });
  adoptRepo = null;
  reload();
}
async function ignoreRepo(repo) {
  await api("/api/project/ignore", {repo}); reload();
}
async function addWatch(ev) {
  ev.preventDefault();
  const f = ev.target;
  if (!f.repo.value || !f.num.value) return;
  const b = f.querySelector("button"); b.disabled = true;
  await api("/api/watch", {repo: f.repo.value.trim(), number: parseInt(f.num.value),
                           type: f.type.value, note: f.note.value});
  b.disabled = false; f.reset();
  reload();
}

function shortRepo(repo) { return repo.split("/")[1] || repo; }
function pills(it, opts) {
  opts = opts || {};
  let h = "";
  if (opts.wait && it.wait_kind) h += '<span class="pill gray">' + esc(it.wait_kind)
    + ' · 晾' + fmtAge(it.age_days) + '</span>';
  if (it.ci_bad && it.ci_bad.length)
    h += '<span class="pill red" title="' + esc(it.ci_bad.join(", ")) + '">CI✗</span>';
  if (it.review_decision === "CHANGES_REQUESTED") h += '<span class="pill red">要改</span>';
  if (it.review_decision === "APPROVED" && !opts.wait) h += '<span class="pill green">已批</span>';
  if (it.my_role === "watcher") h += '<span class="pill yellow">盯梢</span>';
  if (it.triage === "todo" && it.next_action)
    h += '<span class="pill yellow">待办:' + esc(it.next_action) + '</span>';
  if (opts.showBall && it.ball === "mine") h += '<span class="pill red">球在我</span>';
  if (it.state === "MERGED") h += '<span class="pill green">MERGED</span>';
  else if (it.state === "CLOSED") h += '<span class="pill gray">CLOSED</span>';
  return h;
}
function itemRow(it, opts) {
  opts = opts || {};
  let acts = '<span class="acts">';
  if (it.unread) acts += '<button onclick="markSeen(' + it.id + ')">已阅</button>';
  if (opts.todo) {
    acts += '<button onclick="markTodo(' + it.id + ')">待办</button>'
          + '<button onclick="snoozeItem(' + it.id + ')">暂缓</button>'
          + '<button onclick="markDone(' + it.id + ')">完成</button>';
  }
  acts += '</span>';
  let l2 = "";
  if (!opts.compact) {
    l2 = '<div class="l2">' + (it.last_actor
        ? '<b>' + esc(it.last_actor) + '</b> · ' + fmtTime(it.last_activity_at) + ' — '
          + esc(it.last_activity_summary || "")
        : '还没有人回复')
      + (it.my_role === "watcher" && it.note ? ' <span class="note">⟪' + esc(it.note) + '⟫</span>' : '')
      + '</div>';
  }
  const age = opts.compact ? fmtTime(it.last_activity_at || it.updated_at) : '';
  return '<div class="item' + (it.unread ? ' unread' : '') + '">'
    + '<div class="l1"><span class="ref" title="' + esc(it.repo) + '">'
    + esc(shortRepo(it.repo)) + '#' + it.number + '</span>'
    + '<a class="title" href="' + esc(it.url || '#') + '" target="_blank" title="'
    + esc(it.title) + '">' + esc(it.title || '(待抓取)') + '</a>'
    + pills(it, opts) + '<span class="spacer"></span>'
    + (age ? '<span class="age">' + esc(age) + '</span>' : '')
    + acts + '</div>' + l2 + '</div>';
}

function trigRow(t) {
  return '<div class="item unread"><div class="l1">'
    + '<span class="ref">' + esc(shortRepo(t.repo)) + '#' + t.number + '</span>'
    + '<a class="title" href="' + esc(t.url || '#') + '" target="_blank">' + esc(t.title || '') + '</a>'
    + '<span class="spacer"></span><span class="age">' + fmtTime(t.ts) + '</span>'
    + '<span class="acts"><button onclick="markSeen(' + t.item_id + ')">已阅</button></span></div>'
    + '<div class="l2 note">' + esc(t.summary) + '</div></div>';
}

function groupData() {
  const g = {};
  const ensure = repo => g[repo] || (g[repo] = {repo, meta: null, todo: [], trigs: [],
                                                stale: [], closed: [], idle: [], unread: 0, score: 0});
  for (const p of D.projects || []) ensure(p.repo).meta = p;
  for (const it of D.todo) ensure(it.repo).todo.push(it);
  for (const t of D.watch_triggers) ensure(t.repo).trigs.push(t);
  for (const it of D.stale) ensure(it.repo).stale.push(it);
  for (const it of D.closed_week) ensure(it.repo).closed.push(it);
  for (const it of D.idle) ensure(it.repo).idle.push(it);
  for (const r of Object.values(g)) {
    const all = [].concat(r.todo, r.stale, r.closed, r.idle);
    r.unread = all.filter(i => i.unread).length;
    r.score = (r.todo.length + r.trigs.length) * 100 + r.unread * 10 + all.length;
  }
  return g;
}

function badge(cls, ic, text) {
  return '<span class="pill ' + cls + '">' + icon(ic) + text + '</span>';
}

function cardHtml(r) {
  const m = r.meta || {};
  const owner = r.repo.split("/")[0];
  let b = "";
  if (r.todo.length) b += badge("red", "ball", "该我动 " + r.todo.length);
  if (r.trigs.length) b += badge("yellow", "eye", "触发 " + r.trigs.length);
  if (r.stale.length) b += badge("gray", "clock", "晾着 " + r.stale.length);
  if (r.closed.length) b += badge("green", "check", "本周结 " + r.closed.length);
  if (!b) b = badge("gray", "idle", "无动静");
  return '<div class="card' + (r.unread ? ' unread' : '') + '" data-repo="' + esc(r.repo) + '"'
    + ' draggable="true" ondragstart="dragStart(event)" ondragover="dragOverCard(event)"'
    + ' ondragend="dragEnd()" onclick="openModal(\'' + esc(r.repo) + '\')">'
    + '<h3><span class="owner">' + esc(owner) + '/</span>' + esc(shortRepo(r.repo)) + '</h3>'
    + '<div class="cmeta">' + esc([m.tier, m.style].filter(Boolean).join(" · ") || "未收编") + '</div>'
    + '<div class="cbadges">' + b + '</div></div>';
}

function openModal(repo) {
  pendingOpen = false; openRepo = repo; expandedTask = null; watchFormOpen = false;
  renderModal();
}
function closeModal() {
  openRepo = null; pendingOpen = false; adoptRepo = null; expandedTask = null; watchFormOpen = false;
  document.getElementById("modalroot").innerHTML = "";
}
function toggleTask(key) { expandedTask = expandedTask === key ? null : key; renderModal(); }
function toggleWatchForm() { watchFormOpen = !watchFormOpen; renderModal(); }

// 任务归组:同 repo 内,PR 声明 closes/fixes 的 issue(closingIssuesReferences)并成一个任务
function buildTasks(r) {
  const items = [].concat(r.todo, r.stale, r.closed, r.idle);
  const byNum = {};
  items.forEach(it => byNum[it.number] = it);
  const parent = {};
  items.forEach(it => parent[it.number] = it.number);
  const find = n => parent[n] === n ? n : (parent[n] = find(parent[n]));
  items.forEach(it => (it.linked || []).forEach(ln => {
    if (byNum[ln]) { const a = find(it.number), b = find(ln); if (a !== b) parent[a] = b; }
  }));
  const groups = {};
  items.forEach(it => { const k = find(it.number); (groups[k] = groups[k] || []).push(it); });
  return Object.values(groups).map(list => {
    const issues = list.filter(i => i.type === "issue").sort((a, b) => a.number - b.number);
    const prs = list.filter(i => i.type === "pr").sort((a, b) => a.number - b.number);
    const head = issues[0] || prs[0];
    const withAct = list.filter(i => i.last_activity_at)
      .sort((a, b) => a.last_activity_at < b.last_activity_at ? -1 : 1);
    return {
      last: withAct.slice(-1)[0] || null,
      key: String(Math.min(...list.map(i => i.number))),
      list, issues, prs,
      title: head.title,
      unread: list.some(i => i.unread),
      mine: list.some(i => i.ball === "mine"),
      open: list.some(i => i.state === "OPEN"),
      watch: list.some(i => i.my_role === "watcher"),
      lastTs: list.map(i => i.last_activity_at || i.updated_at || i.created_at || "")
                  .sort().slice(-1)[0],
    };
  }).sort((a, b) =>
    (b.mine - a.mine) || (b.unread - a.unread) || (b.open - a.open)
    || (a.lastTs < b.lastTs ? 1 : -1));
}
function taskStatus(t) {
  if (t.mine) return ["该我动", "red"];
  if (t.open) {
    const w = t.list.find(i => i.state === "OPEN" && i.wait_kind);
    return [w ? w.wait_kind : "进行中", "gray"];
  }
  if (t.list.some(i => i.state === "MERGED")) return ["已合并", "green"];
  return ["已关闭", "gray"];
}
function chip(it) {
  const cls = it.unread ? "cunread"
    : it.state === "OPEN" ? "copen" : it.state === "MERGED" ? "cmerged" : "cclosed";
  return '<a class="chip ' + cls + '" href="' + esc(it.url || "#") + '" target="_blank"'
    + ' onclick="event.stopPropagation()" title="' + esc(it.title) + '">#' + it.number + '</a>';
}
function cleanTitle(s) {
  return (s || '')
    .replace(/^\[(bug|feature|feat|question|proposal)\]\s*/i, '')
    .replace(/^(fix|feat|chore|refactor|docs|perf|test|build|ci)(\([^)]*\))?:\s*/i, '');
}
function fmtRel(iso) {
  if (!iso) return '';
  const m = (Date.now() - new Date(iso)) / 60000;
  if (m < 60) return Math.max(1, Math.round(m)) + '分钟前';
  if (m < 1440) return Math.round(m / 60) + '小时前';
  return Math.round(m / 1440) + '天前';
}
const KIND_LABEL = {new_comment: '评论', new_review: 'review', state_change: '状态',
                    label_change: '标签', ci_change: 'CI', assignee_change: '指派',
                    watch_trigger: '盯梢触发'};
// 任务展开区的"记录"时间线:该任务所有 issue/PR 的事件合并,按时间倒序
function taskLog(t) {
  const evs = [];
  for (const it of t.list)
    for (const e of it.events || []) evs.push({...e, number: it.number});
  if (!evs.length) return '';
  evs.sort((a, b) => a.ts < b.ts ? 1 : -1);
  const multi = t.list.length > 1;
  return '<div class="tlog">' + evs.slice(0, 12).map(e =>
    '<div class="tlogrow' + (e.seen ? '' : ' fresh') + '">'
    + '<span class="age">' + fmtTime(e.ts) + '</span>'
    + '<span class="tkind">' + (KIND_LABEL[e.kind] || esc(e.kind)) + '</span>'
    + (multi ? '<span class="age">#' + e.number + '</span>' : '')
    + '<span class="tsum">' + (e.actor ? '<b>' + esc(e.actor) + '</b> ' : '')
    + esc((e.summary || '').slice(0, 90)) + '</span></div>').join('') + '</div>';
}

// 第二行只给需要看的行:有变化/该我动 → 谁说了什么;有盯梢备注 → 备注。安静的行保持单行。
function taskSub(t) {
  const parts = [];
  if ((t.unread || t.mine) && t.last && t.last.last_actor) {
    const sum = (t.last.last_activity_summary || '').slice(0, 60);
    parts.push('<b>' + esc(t.last.last_actor) + '</b>' + (sum ? ' — ' + esc(sum) : ''));
  }
  const notes = t.list.filter(i => i.my_role === 'watcher' && i.note).map(i => i.note);
  if (notes.length) parts.push('<span class="note">⟪' + esc(notes.join(';')) + '⟫</span>');
  return parts.join(' ');
}
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

function section(title, html) { return html ? '<h2>' + title + '</h2>' + html : ''; }

function renderModal() {
  const root = document.getElementById("modalroot");
  if (pendingOpen) {
    const pend = D.pending_repos || [];
    if (!pend.length) { pendingOpen = false; root.innerHTML = ""; return; }
    let h = '<div class="overlay" onclick="if(event.target===this)closeModal()"><div class="modal">'
      + '<div class="mhead"><h3>发现的新 repo</h3>'
      + '<span class="cmeta">收编 = 进白名单开始采集;忽略 = 永不再提</span>'
      + '<button class="mclose" onclick="closeModal()">' + icon("x") + '关闭</button></div>'
      + '<div style="margin-top:12px">';
    for (const repo of pend) {
      if (adoptRepo === repo) {
        h += '<form class="inline" data-repo="' + esc(repo) + '" onsubmit="adoptSubmit(event)">'
          + '<span style="align-self:center;font-weight:500">' + esc(repo) + '</span>'
          + '<input name="tier" placeholder="tier(主战场/副战场…)" size="14">'
          + '<input name="style" placeholder="style" size="10">'
          + '<input name="playbook" placeholder="一句话打法" size="18">'
          + '<input name="tz" placeholder="时区提示" size="10">'
          + '<button>确定</button>'
          + '<button type="button" onclick="adoptRepo=null;renderModal()">取消</button></form>';
      } else {
        h += '<div class="item"><div class="l1"><span class="title">' + esc(repo) + '</span>'
          + '<span class="spacer"></span>'
          + '<button onclick="adopt(\'' + esc(repo) + '\')">收编</button>'
          + '<button onclick="ignoreRepo(\'' + esc(repo) + '\')">忽略</button></div></div>';
      }
    }
    root.innerHTML = h + '</div></div></div>';
    return;
  }
  if (!openRepo) { root.innerHTML = ""; return; }
  const r = groupData()[openRepo]
    || {repo: openRepo, meta: null, todo: [], trigs: [], stale: [], closed: [], idle: []};
  let h = '<div class="overlay" onclick="if(event.target===this)closeModal()"><div class="modal">'
    + '<div class="mbar"><span class="mrepo">' + esc(shortRepo(r.repo)) + '</span>'
    + '<span class="spacer"></span>'
    + '<button onclick="toggleWatchForm()">+ 盯梢</button>'
    + '<button onclick="closeModal()">' + icon("x") + '</button></div>';
  if (watchFormOpen) {
    h += '<form class="inline" onsubmit="addWatch(event)" style="padding:8px 0 0">'
      + '<input name="repo" value="' + esc(r.repo) + '" size="22">'
      + '<input name="num" placeholder="编号" size="6">'
      + '<select name="type"><option value="pr">pr</option><option value="issue">issue</option></select>'
      + '<input name="note" placeholder="盯梢原因/触发器" size="28">'
      + '<button>添加</button></form>';
  }
  h += section(icon("eye") + '盯梢触发 <span class="count">' + r.trigs.length + '</span>',
               r.trigs.map(trigRow).join(''));
  const tasks = buildTasks(r);
  if (tasks.length) {
    h += '<div class="thead"><span>状态</span><span>任务</span><span>Issue</span><span>PR</span>'
      + '<span style="text-align:right">最后动静</span></div>';
    for (const t of tasks) {
      const st = taskStatus(t);
      const sub = taskSub(t);
      h += '<div class="trow' + (t.unread ? ' unread' : '') + '" onclick="toggleTask(\'' + t.key + '\')">'
        + '<div class="tr1">'
        + '<span><span class="pill ' + st[1] + '">' + st[0] + '</span></span>'
        + '<span class="ttitle">' + (t.watch ? icon("eye") + ' ' : '') + esc(cleanTitle(t.title)) + '</span>'
        + '<span class="chips">' + t.issues.map(chip).join('') + '</span>'
        + '<span class="chips">' + t.prs.map(chip).join('') + '</span>'
        + '<span class="age" style="text-align:right">' + fmtRel(t.lastTs) + '</span></div>'
        + (sub ? '<div class="tr2">' + sub + '</div>' : '') + '</div>';
      if (expandedTask === t.key) {
        h += '<div class="tdetail">' + t.list.map(it =>
          itemRow(it, {todo: it.ball === 'mine', wait: it.ball === 'theirs'})).join('')
          + taskLog(t) + '</div>';
      }
    }
  } else {
    h += '<div class="empty">该项目暂无条目。</div>';
  }
  root.innerHTML = h + '</div></div>';
}

function render() {
  const info = document.getElementById("sweepinfo");
  if (D.last_sweep_at) {
    const mins = Math.max(0, Math.round((Date.now() - new Date(D.last_sweep_at)) / 60000));
    info.textContent = "上次扫描 " + (mins < 1 ? "刚刚" : mins + " 分钟前") + " · 每 10 分钟自动";
  } else {
    info.textContent = "还没扫描过";
  }
  document.getElementById("errors").textContent =
    (D.sweep_errors && D.sweep_errors.length ? D.sweep_errors.join("\n") :
     (D.last_sweep_errors && D.last_sweep_errors.length ? "上轮扫描告警:\n" + D.last_sweep_errors.join("\n") : ""));

  // 汇总行(pending 收编入口也收在这行,不单独占版面)+ 项目卡片
  const nUnread = [].concat(D.todo, D.stale, D.closed_week, D.idle).filter(i => i.unread).length;
  let sh = '<span>' + icon("ball") + '球在我这 <b>' + D.todo.length + '</b></span>'
    + '<span>' + icon("eye") + '盯梢触发 <b>' + D.watch_triggers.length + '</b></span>'
    + '<span>' + icon("clock") + '晾着 <b>' + D.stale.length + '</b></span>'
    + '<span>未读 <b>' + nUnread + '</b></span>';
  if ((D.pending_repos || []).length)
    sh += '<button class="warn" onclick="openPending()">发现新 repo · ' + D.pending_repos.length + '</button>';
  document.getElementById("summary").innerHTML = sh;

  const g = groupData();
  const order = (D.projects || []).map(p => p.repo).filter(r => g[r]);
  for (const repo of Object.keys(g).sort()) if (!order.includes(repo)) order.push(repo);
  document.getElementById("cards").innerHTML = order.length
    ? order.map(r => cardHtml(g[r])).join("")
    : '<div class="empty">还没有数据,点[立即扫描]。</div>';

  renderModal();
}

load();
// 每分钟自动拉取最新数据(服务端每10分钟自动 sweep);输入框聚焦时跳过,避免打断
setInterval(() => {
  if (dragEl) return;  // 正在拖卡片,别打断
  const a = document.activeElement;
  if (a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName)) return;
  reload();
}, 60000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- main

SWEEP_INTERVAL_SEC = 600  # 服务模式下自动采集间隔;改动时同步改页面里的"每 10 分钟自动"文案


def auto_sweep_loop():
    while True:
        try:
            with SWEEP_LOCK:
                sweep()
        except Exception as e:
            print("auto sweep:", e, file=sys.stderr)
        time.sleep(SWEEP_INTERVAL_SEC)


def main():
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        errors = sweep(verbose=True)
        for e in errors:
            print("WARN:", e, file=sys.stderr)
        print("sweep 完成", now_iso())
        return 0
    threading.Thread(target=auto_sweep_loop, daemon=True).start()  # 启动即扫,之后每10分钟
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"贡献控制台 → http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
