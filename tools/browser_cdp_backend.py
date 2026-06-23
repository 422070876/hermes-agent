#!/usr/bin/env python3
"""
CDP Browser Backend for Windows - replaces agent-browser CLI on Windows.
Starts Chrome with --remote-debugging-port and communicates via CDP WebSocket.
"""

from __future__ import annotations
import asyncio, atexit, base64, json, logging, os, shutil, signal
import socket, subprocess, sys, tempfile, threading, time, uuid
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
_clients: Dict[str, "CdpClient"] = {}
_clients_lock = threading.Lock()
_chrome_processes: Dict[str, subprocess.Popen] = {}
_chrome_processes_lock = threading.Lock()
_cleanup_registered = False


def _find_chrome() -> Optional[str]:
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Chromium\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        for p in candidates:
            if os.path.isfile(p):
                logger.info("Found Chrome at %s", p)
                return p
        found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("msedge")
        if found:
            logger.info("Found Chrome via PATH at %s", found)
            return found
        logger.warning("Chrome not found in any standard location")
        return None
    if sys.platform == "darwin":
        for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                   "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]:
            if os.path.isfile(p):
                return p
    return (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
            or shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("chrome"))


def start_chrome_and_get_cdp_url(task_id: str, session_name: str) -> Optional[str]:
    chrome = _find_chrome()
    if not chrome:
        return None
    port = _find_free_port()
    udir = os.path.join(tempfile.gettempdir(), "hermes-chrome", session_name)
    os.makedirs(udir, exist_ok=True)
    cmd = [
        chrome, f"--remote-debugging-port={port}",
        f"--user-data-dir={udir}", "--remote-allow-origins=*",
        "--headless=new", "--no-sandbox", "--disable-gpu",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync",
        "--disable-translate", "--disable-default-apps",
        "--disable-features=TranslateUI", "--hide-scrollbars",
        "--mute-audio", "--disable-extensions",
        "--disable-component-update", "--disable-breakpad",
        "--disable-domain-reliability",
        "--disable-features=ChromeWhatsNewUI,ChromeInProductHelp",
        "--window-size=1280,720", "--force-renderer-accessibility",
        "about:blank",
    ]
    logger.info("Starting Chrome on port %d for %s", port, task_id)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        logger.warning("Chrome launch failed: %s", e)
        return None
    with _chrome_processes_lock:
        _chrome_processes[task_id] = proc
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            payload = json.loads(r.read().decode("utf-8"))
            ws = str(payload.get("webSocketDebuggerUrl") or "")
            if ws:
                logger.info("Chrome ready on port %d ws=%s", port, ws)
                global _cleanup_registered
                if not _cleanup_registered:
                    _cleanup_registered = True
                    atexit.register(_cleanup_all)
                return ws
        except Exception:
            poll = proc.poll()
            if poll is not None:
                logger.warning("Chrome exited with code %d before CDP ready", poll)
                return None
        time.sleep(0.3)
    logger.warning("Chrome started on port %d but CDP never became available", port)
    return None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CdpClient:
    """Persistent WebSocket CDP client via background daemon thread."""

    def __init__(self, task_id: str, cdp_url: str):
        self._task_id = task_id
        self._cdp_url = cdp_url
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[Any] = None
        self._msg_id = 0
        self._session_id: Optional[str] = None
        self._connected = False
        self._ready = threading.Event()

    def _ensure_connected(self):
        if self._thread and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=25):
            raise RuntimeError("CDP connect timeout")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_and_enable())
        try:
            self._loop.run_forever()
        except Exception:
            pass

    def _reset_connection(self):
        """Reset connection state so next _call reconnects."""
        self._connected = False
        self._session_id = None
        self._thread = None
        self._conn = None
        self._msg_id = 0
        self._ready.clear()
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        self._loop = None

    async def _connect_and_enable(self):
        import websockets as _ws
        try:
            self._conn = await asyncio.wait_for(
                _ws.connect(self._cdp_url, max_size=None, ping_interval=30, close_timeout=5), timeout=15)
            mid1 = await self._send("Target.getTargets", {})
            targets = (await self._recv(mid1)).get("targetInfos", [])
            tid = None
            for t in targets:
                if t.get("type") == "page":
                    tid = t["targetId"]
                    break
            if not tid:
                mid2 = await self._send("Target.createTarget", {"url": "about:blank"})
                tid = (await self._recv(mid2)).get("targetId")
            mid3 = await self._send("Target.attachToTarget", {"targetId": tid, "flatten": True})
            self._session_id = (await self._recv(mid3)).get("sessionId")
            if not self._session_id:
                raise RuntimeError("attachToTarget: no sessionId")
            for d in ("Page", "Runtime", "DOM", "Accessibility", "Console"):
                mid = await self._send(f"{d}.enable", {})
                await self._recv(mid)
            logger.info("CDP connected session=%s", self._session_id)
            self._connected = True
        except Exception as e:
            logger.warning("CDP connect failed: %s", e, exc_info=True)
        finally:
            self._ready.set()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send(self, method: str, params: Dict, mid: Optional[int] = None) -> int:
        mid = mid or self._next_id()
        req = {"id": mid, "method": method, "params": params}
        if self._session_id:
            req["sessionId"] = self._session_id
        await self._conn.send(json.dumps(req))
        return mid

    async def _recv(self, eid: int, timeout: float = 30) -> Dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(self._conn.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("id") == eid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})

    def _call(self, method: str, params: Optional[Dict] = None, timeout: float = 30) -> Dict:
        self._ensure_connected()
        if not self._connected:
            raise RuntimeError("CDP not connected")
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._call_async(method, params or {}, timeout), self._loop)
            return fut.result(timeout=timeout + 5)
        except Exception as e:
            estr = str(e)
            if any(kw in estr for kw in ["close frame", "ConnectionClosed",
                                          "WinError", "reset", "connection",
                                          "closed", "broken"]):
                logger.warning("CDP lost connection (%s), auto-reconnecting...", estr[:80])
                self._reset_connection()
                self._ensure_connected()
                if self._connected:
                    fut2 = asyncio.run_coroutine_threadsafe(
                        self._call_async(method, params or {}, timeout), self._loop)
                    return fut2.result(timeout=timeout + 5)
                raise RuntimeError("CDP reconnect failed")
            raise

    async def _call_async(self, method: str, params: Dict, timeout: float) -> Dict:
        mid = self._next_id()
        await self._send(method, params, mid)
        return await self._recv(mid, timeout)

    def navigate(self, url: str) -> Dict[str, Any]:
        try:
            self._call("Page.navigate", {"url": url})
            time.sleep(1)
            title = ""
            try:
                tr = self._call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
                if tr.get("result", {}).get("type") == "string":
                    title = tr["result"]["value"]
            except Exception:
                pass
            return {"success": True, "data": {"url": url, "title": title}}
        except Exception as e:
            logger.warning("navigate failed: %s", e)
            return {"success": False, "error": str(e)}

    def snapshot(self) -> Dict[str, Any]:
        try:
            ax = self._call("Accessibility.getFullAXTree", {"fetch_relatives": True}, timeout=15)
            nodes = ax.get("nodes", [])
            if not nodes:
                return {"success": True, "data": {"snapshot": "(no accessibility nodes)", "refs": {}}}
            c, lines, refs = [0], [], {}

            def nn(n):
                for p in n.get("properties", []):
                    if p.get("name") == "name":
                        v = p.get("value", {})
                        return v.get("value", "") if isinstance(v, dict) else str(v)
                return n.get("ignoredReason", "") or n.get("role", {}).get("value", "")

            def nr(n): return n.get("role", {}).get("value", "unknown")

            def act(n): return nr(n) in (
                "button", "link", "textbox", "combobox", "checkbox", "radio",
                "menuitem", "tab", "switch", "searchbox", "listbox", "slider", "spinbutton", "treeitem",
                "menuitemcheckbox", "menuitemradio")

            def walk(n, d=0):
                lbl = f"[{nr(n)}] {nn(n)}" if nn(n) else f"[{nr(n)}]"
                if act(n):
                    c[0] += 1
                    rid = f"@e{c[0]}"
                    bd = n.get("backendDOMNodeId")
                    refs[rid] = bd if bd is not None else n.get("nodeId", "")
                    lbl += f" [{rid}]"
                lines.append("  " * d + lbl)
                for cid in n.get("childIds", []):
                    ch = next((x for x in nodes if x.get("nodeId") == cid), None)
                    if ch:
                        walk(ch, d + 1)

            for r in [n for n in nodes if not n.get("parentId")] or nodes[:5]:
                walk(r)
            return {"success": True, "data": {"snapshot": "\n".join(lines) or "(empty)", "refs": refs}}
        except Exception as e:
            logger.warning("snapshot failed: %s", e)
            return {"success": False, "error": str(e)}

    def _get_element_bounds(self, bdid: int) -> Optional[Tuple]:
        try:
            o = self._call("DOM.resolveNode", {"backendNodeId": bdid}, timeout=10)
            oid = o.get("object", {}).get("objectId")
            if not oid:
                return None
            box = self._call("Runtime.callFunctionOn", {
                "objectId": oid,
                "functionDeclaration": "function(){var r=this.getBoundingClientRect();return[r.left,r.top,r.width,r.height]}",
                "returnByValue": True}, timeout=10)
            v = box.get("result", {}).get("value")
            if v and len(v) == 4:
                return (v[0], v[1], v[2], v[3])
        except Exception:
            pass
        return None

    def click(self, ref: str) -> Dict[str, Any]:
        try:
            snap = self.snapshot()
            if not snap.get("success"):
                return snap
            node = snap.get("data", {}).get("refs", {}).get(ref)
            if node is None:
                return {"success": False, "error": f"Element {ref} not found"}
            if not isinstance(node, int):
                return {"success": False, "error": f"Cannot find coordinates for {ref}"}
            bounds = self._get_element_bounds(node)
            if bounds:
                x, y, w, h = bounds
                cx, cy = x + w / 2, y + h / 2
                self._call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1})
                self._call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1})
                time.sleep(0.2)
                return {"success": True, "data": {}}
            o = self._call("DOM.resolveNode", {"backendNodeId": node}, timeout=10)
            oid = o.get("object", {}).get("objectId")
            if oid:
                self._call("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration": "function(){this.click()}"})
                return {"success": True, "data": {}}
            return {"success": False, "error": f"Cannot find coordinates for {ref}"}
        except Exception as e:
            logger.warning("click failed: %s", e)
            return {"success": False, "error": str(e)}

    def type_text(self, ref: str, text: str) -> Dict[str, Any]:
        cr = self.click(ref)
        if not cr.get("success"):
            return cr
        try:
            self._call("Input.insertText", {"text": text}, timeout=10)
            return {"success": True, "data": {}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def scroll(self, direction: str) -> Dict[str, Any]:
        try:
            s = "-" if direction == "up" else ""
            self._call("Runtime.evaluate", {"expression": f"window.scrollBy(0,{s}500)"})
            time.sleep(0.3)
            return {"success": True, "data": {}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def back(self) -> Dict[str, Any]:
        try:
            self._call("Runtime.evaluate", {"expression": "window.history.back()"})
            time.sleep(0.5)
            return {"success": True, "data": {}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def press_key(self, key: str) -> Dict[str, Any]:
        km = {"Enter": "Enter", "Tab": "Tab", "Escape": "Escape",
              "ArrowDown": "ArrowDown", "ArrowUp": "ArrowUp",
              "ArrowLeft": "ArrowLeft", "ArrowRight": "ArrowRight",
              "Backspace": "Backspace", "Delete": "Delete"}
        ck = km.get(key, key)
        try:
            vk = ord(ck[0]) if len(ck) == 1 else 0
            self._call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": ck, "windowsVirtualKeyCode": vk})
            self._call("Input.dispatchKeyEvent", {"type": "keyUp", "key": ck, "windowsVirtualKeyCode": vk})
            time.sleep(0.1)
            return {"success": True, "data": {}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def console(self, expression: Optional[str] = None) -> Dict[str, Any]:
        try:
            if expression is not None:
                r = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=10)
                v = r.get("result", {})
                return {"success": True, "data": {"type": v.get("type", ""),
                                                    "value": v.get("value"),
                                                    "description": v.get("description", "")}}
            self._call("Console.enable", timeout=5)
            return {"success": True, "data": {"messages": []}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def screenshot(self) -> Dict[str, Any]:
        try:
            r = self._call("Page.captureScreenshot", {"format": "png"}, timeout=15)
            data = r.get("data", "")
            if not data:
                return {"success": False, "error": "Screenshot empty"}
            from hermes_constants import get_hermes_home
            ss = get_hermes_home() / "cache" / "screenshots"
            os.makedirs(str(ss), exist_ok=True)
            fn = f"browser_screenshot_{uuid.uuid4().hex}.png"
            fp = str(ss / fn)
            with open(fp, "wb") as f:
                f.write(base64.b64decode(data))
            return {"success": True, "data": {"path": fp, "filepath": fp, "filename": fn}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_images(self) -> Dict[str, Any]:
        try:
            r = self._call("Runtime.evaluate", {
                "expression": "Array.from(document.querySelectorAll('img')).map(i=>({src:i.src||'',alt:i.alt||'',width:i.naturalWidth||i.width,height:i.naturalHeight||i.height}))",
                "returnByValue": True}, timeout=10)
            imgs = r.get("result", {}).get("value", [])
            return {"success": True, "data": {"images": imgs}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close(self):
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass


def get_or_create_client(task_id: str, cdp_url: str) -> CdpClient:
    with _clients_lock:
        if task_id in _clients:
            return _clients[task_id]
        c = CdpClient(task_id, cdp_url)
        _clients[task_id] = c
        return c


def _signal_handler(signum, frame):
    _cleanup_all()


def _cleanup_all():
    with _chrome_processes_lock:
        for p in _chrome_processes.values():
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        _chrome_processes.clear()
    with _clients_lock:
        for c in _clients.values():
            c.close()
        _clients.clear()


def cleanup_chrome_for_task(task_id: str):
    with _chrome_processes_lock:
        proc = _chrome_processes.pop(task_id, None)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    with _clients_lock:
        _clients.pop(task_id, None)