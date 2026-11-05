"""Deskrab: ponte entre o Claude Code e o display (USB serial).

Roda como plugin do Claude Code: os hooks do plugin chamam "hook"; a status line
(que plugin nao consegue definir) e gravada pelo "setup".

Uso:
  deskrab_bridge.py hook        chamado pelos hooks do Claude Code (JSON no stdin)
  deskrab_bridge.py statusline  chamado pela status line (JSON no stdin, imprime a linha)
  deskrab_bridge.py setup       configura o Python do plugin e a status line (com backup)
  deskrab_bridge.py uninstall   tira a status line e devolve a que existia antes
  deskrab_bridge.py status      diz se o serve esta rodando e em qual porta esta o display
  deskrab_bridge.py wifi --ssid NOME [--password SENHA]   manda a rede pelo USB e passa a usar o Wi-Fi
  deskrab_bridge.py usb         volta para o cabo (desliga o Wi-Fi do display)
  deskrab_bridge.py lang pt|en  idioma do display (ele reinicia)
  deskrab_bridge.py factory     teste de fabrica no display; registra em ~/.deskrab/factory.csv
  deskrab_bridge.py serve       processo em segundo plano que fala com o display
  deskrab_bridge.py stop        encerra o serve
  deskrab_bridge.py pause       serve solta a COM (para gravar o firmware) e espera
  deskrab_bridge.py resume      serve volta a procurar o display
  deskrab_bridge.py demo        passa por todos os estados para testar o display
  deskrab_bridge.py play NOME   mostra uma animacao agora (visitor, gull, fishing, ufo... ou bulb)
  deskrab_bridge.py show        passeio por todas as animacoes novas (~5 min), com uma sessao falsa

Os hooks so gravam arquivos em ~/.deskrab e iniciam o serve se ele nao estiver
rodando. Qualquer erro aqui e engolido: o Claude Code nunca e afetado.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

# pyserial embutido no plugin: so e usado se o Python escolhido nao tiver um
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

PLUGIN_ID = "deskrab@deskrab"   # plugin@marketplace
IS_WIN = os.name == "nt"
STATE_DIR = os.environ.get("DESKRAB_HOME") or os.path.join(os.path.expanduser("~"), ".deskrab")
STABLE_BRIDGE = os.path.join(STATE_DIR, "deskrab_bridge.py")   # copia fixa usada pela status line
PREV_SL_FILE = os.path.join(STATE_DIR, "prev_statusline.json")  # status line que o usuario ja tinha
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")   # transporte: usb ou wifi (host, porta, codigo, id)
DEVICE_FILE = os.path.join(STATE_DIR, "device.json")   # o que o display disse no id: fw, idioma, id
OUTBOX_FILE = os.path.join(STATE_DIR, "outbox.json")   # mensagens para o display (factory, lang); o serve envia
FACT_FILE = os.path.join(STATE_DIR, "factory_result.json")   # ultimo resultado do teste de fabrica
FACT_LOG = os.path.join(STATE_DIR, "factory.csv")     # historico dos testes de fabrica (uma linha por unidade)
FW_INFO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware.json")   # firmware publicado junto
SESS_DIR = os.path.join(STATE_DIR, "sessions")
LOCK_FILE = os.path.join(STATE_DIR, "serve.lock")
PID_FILE = os.path.join(STATE_DIR, "serve.pid")
PAUSE_FILE = os.path.join(STATE_DIR, "paused")
PORT_FILE = os.path.join(STATE_DIR, "serve.port")   # existe enquanto a COM esta aberta
OVERRIDE_FILE = os.path.join(STATE_DIR, "override.json")   # usado pelo demo (data falsa)
HIST_FILE = os.path.join(STATE_DIR, "history.json")   # [t, h5, d7] das ultimas horas
STATS_FILE = os.path.join(STATE_DIR, "stats.json")    # contadores do dia e conquistas
LOG_FILE = os.path.join(STATE_DIR, "serve.log")
PLAY_FILE = os.path.join(STATE_DIR, "play.json")     # previa pedida por play/show
CTX_FILE = os.path.join(STATE_DIR, "ctxsize.json")   # tamanho do contexto por modelo (visto na status line)
RATE_FILE = os.path.join(STATE_DIR, "rate.json")     # ultimos limites de 5h/7d vistos na status line
TAIL_BYTES = 768 * 1024                              # quanto do fim do transcript os hooks leem
SL_FRESH_S = 120   # status line (so no CLI) escreveu ha pouco: os hooks nao sobrescrevem

BAUD = 115200
HEARTBEAT_S = 3.0
PING_S = 15.0
DONE_TO_IDLE_S = 60
BUSY_TO_IDLE_S = 15 * 60   # sem eventos por 15 min (ex.: Esc no meio) -> idle
PERM_DECAY_S = 30          # pedido de permissao sem resposta visivel -> volta a ferramenta
SUB_PRUNE_S = 30 * 60      # subagente sem SubagentStop some depois disso
HIST_KEEP_S = 6 * 3600
HIST_BUCKETS, HIST_BUCKET_S = 60, 300   # grafico: 60 pontos de 5 min = 5 h
HIST_SEND_S = 60
ACH_SEND_S = 8             # intervalo minimo entre duas conquistas na tela
SESSION_PRUNE_S = 12 * 3600
NOPID_PRUNE_S = 3600       # sessao sem processo conhecido (arquivo antigo) parada ha 1 h -> some
ALIVE_CHECK_S = 5          # de quanto em quanto tempo o serve confere se o dono ainda existe
_alive = {}                # sid -> (vivo, quando conferiu)
# Ordem de prioridade quando ha varias sessoes abertas
PRIORITY = {"attention": 5, "tool": 4, "thinking": 3, "done": 2, "idle": 1}
STATE_CODE = {"idle": "i", "thinking": "t", "tool": "w", "attention": "a", "done": "d"}
MAX_DOTS = 5

# Sessao fixada por toque no CYD (pontinhos do topo) e a ordem mostrada da ultima vez
_pinned = None
_dot_order = []


# ---------------------------------------------------------------- utilitarios

def ascii_text(s, maxlen):
    """Texto para o display: ASCII e Latin-1 (os acentos do portugues, que a fonte tem desde o
    firmware 0.3.0); o resto vira o equivalente sem acento ou some. Corta em maxlen."""
    out = []
    for ch in unicodedata.normalize("NFC", str(s or "")):
        o = ord(ch)
        if 0x20 <= o < 0x7F or 0xA0 <= o <= 0xFF or ch.isspace():
            out.append(ch)
        else:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    s = " ".join("".join(out).split())
    return s if len(s) <= maxlen else s[: maxlen - 3] + "..."


def fold_ascii(s):
    """So ASCII, para firmware antigo (sem a fonte com acentos)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (TypeError, ValueError):
        return (0,)


# ---------------------------------------------------------------- idioma do display
# O display diz o idioma no "id"; os rotulos saem daqui em ingles e sao traduzidos no envio.

_dev_info = {}   # fw, lang, id do display conectado (o serve atualiza)

LABELS_PT = {"Ready": "Pronto", "Thinking": "Pensando", "Running": "Executando", "Editing": "Editando",
             "Reading": "Lendo", "Searching": "Buscando", "Fetching": "Baixando",
             "Searching the web": "Pesquisando na web", "Delegating": "Delegando", "Planning": "Planejando",
             "Compacting": "Compactando", "Needs you!": "Precisa de você!", "Done": "Feito!",
             "API error": "Erro na API"}
DETAILS_PT = {"squeezing the context": "espremendo o contexto"}
ACH_PT = {"First prompt of the day!": "Primeiro prompt do dia!",
          "Night owl: coding after midnight": "Coruja: codando de madrugada",
          "Marathon: a 1-hour turn!": "Maratona: um turno de 1 hora!"}


def old_firmware():
    return ver_tuple(_dev_info.get("fw")) < (0, 3, 0)


def localize(frame):
    """Traduz o rotulo e o detalhe para o idioma do display; tira acentos para firmware antigo."""
    if _dev_info.get("lang") == "pt":
        lbl = frame.get("lbl") or ""
        if lbl in LABELS_PT:
            frame["lbl"] = LABELS_PT[lbl]
        elif lbl.startswith("Allow ") and lbl.endswith("?"):
            frame["lbl"] = "Permitir %s?" % lbl[6:-1]
        elif lbl.startswith("Using "):
            frame["lbl"] = "Usando " + lbl[6:]
        frame["det"] = DETAILS_PT.get(frame.get("det") or "", frame.get("det") or "")
    if old_firmware():
        for k, v in list(frame.items()):
            if isinstance(v, str):
                frame[k] = fold_ascii(v)
    return frame


def ach_text(t):
    if _dev_info.get("lang") == "pt":
        m = re.match(r"(\d+) prompts today!$", t)
        if m:
            t = "%s prompts hoje!" % m.group(1)
        m = re.match(r"([\d,]+) lines today!$", t)
        if m:
            t = "%s linhas hoje!" % m.group(1).replace(",", ".")
        t = ACH_PT.get(t, t)
    t = ascii_text(t, 40)
    return fold_ascii(t) if old_firmware() else t


def parse_id(buf):
    """O {"t":"id",...} do display, so de linhas completas."""
    for ln in buf.split(b"\n")[:-1]:
        if b'"t":"id"' in ln:
            try:
                return json.loads(ln.strip())
            except ValueError:
                pass
    return {}


def set_device(info, via=None):
    global _dev_info
    via = via or _dev_info.get("via")
    _dev_info = {k: info.get(k) for k in ("fw", "lang", "id") if info.get(k)}
    _dev_info["via"] = via
    write_json(DEVICE_FILE, _dev_info)


def latest_firmware():
    """{"version": ..., "page": ...} do firmware publicado junto com este plugin, ou {}."""
    return read_json(FW_INFO_FILE) or {}


def queue_to_device(msg):
    """Deixa uma mensagem para o serve mandar ao display (USB ou Wi-Fi)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    box = read_json(OUTBOX_FILE) or {"msgs": []}
    box["msgs"] = (box.get("msgs") or []) + [msg]
    box["t"] = time.time()
    write_json(OUTBOX_FILE, box)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # o serve pode estar lendo o arquivo
            time.sleep(0.02)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def session_paths(session_id):
    sid = "".join(c for c in str(session_id or "default") if c.isalnum() or c in "-_")[:64]
    base = os.path.join(SESS_DIR, sid or "default")
    return base + ".state.json", base + ".usage.json"


# ---------------------------------------------------------------- hooks

def read_stdin_json():
    # O Claude Code manda UTF-8; o stdin do Python no Windows assumiria cp1252
    return json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))


def tool_label(name, inp):
    """Texto principal, detalhe e tipo de atividade (para a pose) de cada ferramenta."""
    inp = inp if isinstance(inp, dict) else {}
    path = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if name in ("Bash", "PowerShell"):
        return "Running", inp.get("description") or inp.get("command"), "run"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "Editing", path, "edit"
    if name == "Read":
        return "Reading", path, "read"
    if name in ("Grep", "Glob"):
        return "Searching", inp.get("pattern"), "read"
    if name == "WebFetch":
        return "Fetching", inp.get("url"), "web"
    if name == "WebSearch":
        return "Searching the web", inp.get("query"), "web"
    if name in ("Task", "Agent"):
        return "Delegating", inp.get("description"), "run"
    if name == "TodoWrite":
        return "Planning", "", "web"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "Using " + parts[1], parts[2] if len(parts) > 2 else "", "run"
    return name, "", "run"


def file_size(path):
    try:
        return os.path.getsize(path)
    except (OSError, TypeError, ValueError):
        return 0


def thinking_tokens(path, off):
    """Tokens de pensamento do agente principal no transcript, a partir de off (inicio do turno).

    Cada chamada da API aparece em varias linhas (uma por bloco) com o mesmo usage,
    entao conta uma vez por mensagem. Atualiza a cada resposta, nao durante o streaming.
    """
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except (OSError, TypeError, ValueError):
        return 0
    per_msg = {}
    for ln in data.split(b"\n"):
        if b'"thinking_tokens"' not in ln or b'"assistant"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue   # linha ainda sendo escrita
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        m = e.get("message") or {}
        tk = ((m.get("usage") or {}).get("output_tokens_details") or {}).get("thinking_tokens")
        if isinstance(tk, int):
            key = m.get("id") or e.get("requestId") or e.get("uuid")
            per_msg[key] = max(per_msg.get(key, 0), tk)
    return sum(per_msg.values())


# ---------------------------------------------------------------- processo dono da sessao
# O app desktop nao manda SessionEnd ao fechar: a sessao ficaria "aberta" por horas. Cada hook
# anota o processo do Claude Code que o chamou; o serve descarta a sessao quando ele morre.

CLAUDE_EXES = ("claude.exe", "node.exe", "bun.exe", "claude", "node", "bun")


def _proc_table():
    return _proc_table_win() if IS_WIN else _proc_table_unix()


def proc_started(pid):
    return _proc_started_win(pid) if IS_WIN else _proc_started_unix(pid)


def _proc_table_unix():
    """{pid: (ppid, exe)} pelo ps (Linux e macOS): uma chamada so para a tabela toda."""
    r = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=3)
    table = {}
    for ln in r.stdout.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()).lower())
    return table


def _proc_started_unix(pid):
    """Hora de inicio do processo como o ps mostra, ou None se ele nao existe mais."""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=2)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _proc_table_win():
    """{pid: (ppid, exe)} de todos os processos (Toolhelp32, sem psutil)."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    table, pe = {}, PE()
    pe.dwSize = ctypes.sizeof(PE)
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        table[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return table


def _proc_started_win(pid):
    """Hora de criacao do processo (FILETIME), ou None se ele nao existe mais."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return None
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k32.CloseHandle(h)


def claude_owner():
    """(pid, criacao) do processo do Claude Code mais proximo acima deste hook."""
    try:
        table, pid = _proc_table(), os.getpid()
        for _ in range(12):
            ppid, _exe = table.get(pid, (0, ""))
            if not ppid or ppid not in table:
                return None
            if table[ppid][1] in CLAUDE_EXES:
                started = proc_started(ppid)
                return (ppid, started) if started else None
            pid = ppid
    except Exception:
        pass
    return None


def model_display(mid):
    """claude-opus-5-5 -> Opus 5.5 (o app desktop nao roda a status line, que dava o nome pronto)."""
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2})(?!\d))?", mid or "")
    if not m:
        return mid or ""
    return "%s %s%s" % (m.group(1).capitalize(), m.group(2), "." + m.group(3) if m.group(3) else "")


def iso_epoch(s):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ctx_size(model):
    sizes = read_json(CTX_FILE)
    if sizes is None:   # primeira vez: aproveita a ultima status line vista
        sl = read_json(os.path.join(STATE_DIR, "statusline_last.json")) or {}
        mid, size = (sl.get("model") or {}).get("id"), (sl.get("context_window") or {}).get("context_window_size")
        sizes = {mid: size} if mid and size else {}
    return sizes.get(model) or max(sizes.values(), default=0) or 200000


def usage_from_transcript(tr):
    """O que a status line mandava, tirado do transcript: modelo, effort, contexto, cache, titulo.

    Serve para o app desktop, que roda os hooks mas nao a status line. Os limites de 5h/7d nao
    aparecem no transcript: ficam os ultimos vistos no CLI (rate.json) ate o reset.
    """
    size = file_size(tr)
    try:
        with open(tr, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except (OSError, TypeError, ValueError):
        return None
    last = effort = title = ttl = None
    for ln in reversed(data.split(b"\n")):
        if last and effort and title and ttl:
            break
        try:
            if b'"type":"assistant"' in ln and (last is None or ttl is None):
                e = json.loads(ln)
                if e.get("isSidechain"):
                    continue
                m = e.get("message") or {}
                u = m.get("usage") or {}
                if last is None and u:
                    last = (m.get("model"), u, iso_epoch(e.get("timestamp")))
                cc = u.get("cache_creation") or {}
                if ttl is None and (cc.get("ephemeral_1h_input_tokens") or cc.get("ephemeral_5m_input_tokens")):
                    ttl = 3600 if cc.get("ephemeral_1h_input_tokens") else 300
            elif effort is None and b'"type":"api-request"' in ln:
                p = json.loads(ln).get("params") or {}
                if last is None or p.get("model") == last[0]:
                    effort = (p.get("output_config") or {}).get("effort")
            elif title is None and b'"type":"ai-title"' in ln:
                title = json.loads(ln).get("aiTitle")
        except ValueError:
            continue   # linha ainda sendo escrita
    if not last:
        return None
    model, u, ts = last
    used = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = {"model": ascii_text(model_display(model), 30), "ctx": 100.0 * used / ctx_size(model),
           "t": time.time(), "src": "hook", "effort": ascii_text(effort, 10), "name": ascii_text(title, 40)}
    # Custo e linhas: o cost-state so e gravado quando a sessao e salva/transferida, nao a cada
    # turno. Valor velho engana mais do que "--", entao ficam de fora.
    if ts and ttl:   # cada chamada renova o cache do prompt
        out["cexp"] = int(ts + ttl)
        out["cwarm"] = 1 if time.time() < ts + ttl else 0
    return out


def usage_from_hook(ev, usage_path):
    cur = read_json(usage_path) or {}
    if cur.get("src") != "hook" and cur and time.time() - cur.get("t", 0) < SL_FRESH_S:
        return   # CLI: a status line esta mandando, e ela sabe os limites de uso
    u = usage_from_transcript(ev.get("transcript_path"))
    if u:
        write_json(usage_path, u)


TEST_RE = re.compile(r"\b(pytest|py\.test|npm (run )?test|yarn test|pnpm test|cargo test|go test|ctest"
                     r"|jest|vitest|mvn test|gradle test|dotnet test|unittest)\b")
FAILED_RE = re.compile(r"\b[1-9]\d* (failed|failing|errors?)\b|test result: FAILED")
# git [-C dir] [--opcao] push|commit (nao pega "git stash push")
GIT_OPTS = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"
GIT_PUSH_RE = re.compile(GIT_OPTS + r"push\b")
GIT_COMMIT_RE = re.compile(GIT_OPTS + r"commit\b")
PUSH_FAILED = ("[rejected]", "[remote rejected]", "error: failed to push", "fatal:")
COMMIT_DONE_RE = re.compile(r"\[[^\]\n]+ [0-9a-f]{7,}\]")   # "[main 1a2b3c4] mensagem"


def tool_event(name, ev):
    """Evento que o caranguejo comemora: commit, push, testes passando (tok) ou falhando (tfail)."""
    if ev.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    cmd = str(inp.get("command") or "")
    failed = name == "PostToolUseFailure"
    out = ev.get("tool_response")
    out = (out if isinstance(out, str) else json.dumps(out))[-20000:]   # o resumo fica no fim
    if TEST_RE.search(cmd):
        return "tfail" if failed or FAILED_RE.search(out) else "tok"
    if failed or "--dry-run" in cmd:
        return None
    # So comemora o que aconteceu de verdade: push com linha de ref ("a..b main -> main"),
    # commit com "[branch hash]". Push em dia, recusado ou "nothing to commit" nao contam.
    if GIT_PUSH_RE.search(cmd) and "->" in out and not any(s in out for s in PUSH_FAILED):
        return "push"   # antes do commit: "git commit && git push" comemora o push
    if GIT_COMMIT_RE.search(cmd) and COMMIT_DONE_RE.search(out):
        return "commit"
    return None


def cmd_hook():
    ev = read_stdin_json()
    name = ev.get("hook_event_name", "")
    state_path, usage_path = session_paths(ev.get("session_id"))
    project = os.path.basename(str(ev.get("cwd") or "").rstrip("\\/"))

    if name == "SessionEnd":
        for p in (state_path, usage_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return

    prev = read_json(state_path) or {}
    now = time.time()
    try:
        os.makedirs(SESS_DIR, exist_ok=True)
        usage_from_hook(ev, usage_path)   # app desktop: sem status line, os dados vem do transcript
    except Exception as e:
        log("erro lendo o transcript: %r" % e)

    if name in ("SubagentStart", "SubagentStop"):
        if not prev:
            return
        subs = {k: v for k, v in (prev.get("subs") or {}).items() if now - v < SUB_PRUNE_S}
        aid = str(ev.get("agent_id") or "")
        if name == "SubagentStart":
            subs[aid or str(now)] = now
        elif aid in subs:
            del subs[aid]
        elif subs:   # sem id conhecido: tira o mais antigo
            del subs[min(subs, key=subs.get)]
        prev["subs"] = subs
        write_json(state_path, prev)
        return
    upd = {}   # campos alem de st/lbl/det que mudam neste evento
    st = None
    if name == "SessionStart":
        st = ("idle", "Ready", project, "")
        upd = {"subs": {}}
        refresh_stable_copy()
    elif name == "UserPromptSubmit":
        st = ("thinking", "Thinking", project, "")
        # Tamanho do transcript agora = onde o turno comeca (para contar o pensamento)
        tr = str(ev.get("transcript_path") or "")
        upd = {"turn_t": now, "turn_end": 0, "tr": tr, "tr_off": file_size(tr), "ttk": 0}
        stats_on_prompt()
    elif name == "PreToolUse":
        lbl, det, act = tool_label(ev.get("tool_name", ""), ev.get("tool_input"))
        st = ("tool", lbl, det, act)
    elif name in ("PostToolUse", "PermissionDenied"):
        st = ("thinking", "Thinking", project, "")
    elif name == "PostToolUseFailure":
        st = ("thinking", "Thinking", project, "")
        if not ev.get("is_interrupt"):
            upd = {"err": int(now)}   # o display faz a cara de susto
    elif name == "PreCompact":
        st = ("tool", "Compacting", "squeezing the context", "compact")
    elif name == "PostCompact":
        st = ("thinking", "Thinking", project, "")
    elif name == "PermissionRequest":
        tool = ev.get("tool_name", "")
        lbl, det, act = tool_label(tool, ev.get("tool_input"))
        st = ("attention", "Allow %s?" % tool, det, "")
        # Nao existe evento de "permissao aceita": guarda a ferramenta para o decaimento
        upd = {"perm": 1, "tool_lbl": lbl, "tool_det": det, "tool_act": act}
    elif name == "Notification":
        msg = str(ev.get("message") or "")
        ntype = str(ev.get("notification_type") or "")
        # "Claude is waiting for your input" aparece depois de terminar: nao e alerta
        if ntype == "idle_prompt" or "waiting for your input" in msg.lower():
            return
        if prev.get("st") == "attention":
            return   # o PermissionRequest ja mostrou algo mais especifico
        st = ("attention", "Needs you!", msg, "")
        is_perm = ntype == "permission_prompt" or "permission" in msg.lower()
        if is_perm and prev.get("st") == "tool":
            upd = {"perm": 1, "tool_lbl": prev.get("lbl"), "tool_det": prev.get("det"),
                   "tool_act": prev.get("act", "")}
    elif name == "Stop":
        st = ("done", "Done", project, "")
        upd = {"turn_end": now}
        if prev.get("turn_t"):
            stats_on_stop(now - prev["turn_t"])
    elif name == "StopFailure":
        st = ("attention", "API error", str(ev.get("error") or ev.get("message") or ""), "")
        upd = {"turn_end": now}
    if st is None:
        return
    if name not in ("SessionStart", "UserPromptSubmit") and prev.get("tr"):
        upd["ttk"] = thinking_tokens(prev["tr"], prev.get("tr_off", 0))
    if name in ("PostToolUse", "PostToolUseFailure"):
        evk = tool_event(name, ev)
        if evk:
            upd.update({"evk": evk, "evt": int(now)})
            upd.pop("err", None)   # teste falhando ja tem a propria reacao (sem o susto)

    data = {k: prev[k] for k in ("turn_t", "turn_end", "err", "subs", "tr", "tr_off", "ttk", "evk", "evt",
                                 "pid", "pst") if k in prev}
    if "pid" not in data or name == "SessionStart":
        owner = claude_owner()   # quem e o processo dono (para notar quando o app fecha)
        if owner:
            data["pid"], data["pst"] = owner
    data.update({
        "st": st[0],
        "lbl": ascii_text(st[1], 30),
        "det": ascii_text(st[2], 70),
        "act": st[3],
        "proj": ascii_text(project, 40),
        "t": now,
    })
    data.update(upd)
    os.makedirs(SESS_DIR, exist_ok=True)
    write_json(state_path, data)


# ---------------------------------------------------------------- status line

def pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def prev_statusline(raw):
    """Roda a status line que o usuario tinha antes do setup e devolve o texto dela (ou None)."""
    prev = (read_json(PREV_SL_FILE) or {}).get("command")
    if not prev:
        return None
    try:
        r = subprocess.run(prev, shell=True, input=raw, capture_output=True, timeout=5)
        return r.stdout.decode("utf-8", "replace").rstrip("\n")
    except Exception:
        return None


def cmd_statusline():
    raw = sys.stdin.buffer.read()
    data = json.loads(raw.decode("utf-8", "replace"))
    os.makedirs(SESS_DIR, exist_ok=True)
    write_json(os.path.join(STATE_DIR, "statusline_last.json"), data)   # para depuracao

    model = (data.get("model") or {}).get("display_name") or ""
    cw = data.get("context_window") or {}
    ctx = pct(cw.get("used_percentage"))
    if ctx is None:
        usage, size = cw.get("current_usage") or {}, cw.get("context_window_size")
        used = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens"))
        if used and size:
            ctx = 100.0 * used / size

    # Guarda o que o transcript nao tem, para quando so os hooks rodarem (app desktop)
    mid, size = (data.get("model") or {}).get("id"), cw.get("context_window_size")
    if mid and size:
        sizes = read_json(CTX_FILE) or {}
        if sizes.get(mid) != size:
            sizes[mid] = size
            write_json(CTX_FILE, sizes)

    cost = data.get("cost") or {}
    out = {
        "model": ascii_text(model, 30), "ctx": ctx, "t": time.time(), "src": "sl",
        "effort": ascii_text((data.get("effort") or {}).get("level"), 10),
        "name": ascii_text(data.get("session_name"), 40),
        "cost": pct(cost.get("total_cost_usd")),
        "dur": int((pct(cost.get("total_duration_ms")) or 0) / 1000),
        "la": int(pct(cost.get("total_lines_added")) or 0),
        "lr": int(pct(cost.get("total_lines_removed")) or 0),
        "tin": int(pct(cw.get("total_input_tokens")) or 0),
        "tout": int(pct(cw.get("total_output_tokens")) or 0),
    }
    pc = data.get("prompt_cache") or {}
    if pc.get("expires_at"):   # quando o cache do prompt expira (TTL de 5 min ou 1 h)
        out["cexp"] = int(pct(pc.get("expires_at")) or 0)
        out["cwarm"] = 1 if pc.get("warm") else 0
    rl = data.get("rate_limits") or {}
    for key, short in (("five_hour", "h5"), ("seven_day", "d7")):
        w = rl.get(key) or {}
        p = pct(w.get("used_percentage"))
        if p is not None:
            out[short] = {"p": p, "r": int(pct(w.get("resets_at")) or 0)}
    _, usage_path = session_paths(data.get("session_id"))
    write_json(usage_path, out)
    if "h5" in out or "d7" in out:
        write_json(RATE_FILE, {k: out[k] for k in ("h5", "d7") if k in out})
    if "h5" in out:
        append_history(out["t"], out["h5"]["p"], out.get("d7", {}).get("p"))
    if data.get("session_id"):
        stats_on_lines(data["session_id"], out["la"])

    parts = [model] if model else []
    if ctx is not None:
        parts.append("ctx %d%%" % round(ctx))
    for short, lbl in (("h5", "5h"), ("d7", "7d")):
        if short in out:
            parts.append("%s %d%%" % (lbl, round(out[short]["p"])))
    mine = prev_statusline(raw)   # quem ja tinha uma status line continua vendo a dele
    sys.stdout.buffer.write(((mine if mine is not None else " | ".join(parts)) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- conquistas

def stats_load():
    s = read_json(STATS_FILE) or {}
    day = time.strftime("%Y-%m-%d")
    if s.get("day") != day:   # dia novo: zera os contadores (o ultimo total por sessao fica)
        s = {"day": day, "lines": 0, "turns": 0, "last_la": s.get("last_la", {}),
             "got": [], "pending": s.get("pending", [])[-3:]}
    return s


def award(s, key, text):
    if key not in s["got"]:
        s["got"].append(key)
        s["pending"].append(text)


def stats_on_prompt():
    s = stats_load()
    s["turns"] += 1
    if s["turns"] == 1:
        award(s, "first", "First prompt of the day!")
    if time.localtime().tm_hour < 5:
        award(s, "owl", "Night owl: coding after midnight")
    for n in (10, 25):
        if s["turns"] >= n:
            award(s, "turns%d" % n, "%d prompts today!" % n)
    write_json(STATS_FILE, s)


def stats_on_stop(turn_s):
    if turn_s >= 3600:
        s = stats_load()
        award(s, "marathon", "Marathon: a 1-hour turn!")
        write_json(STATS_FILE, s)


def stats_on_lines(session_id, lines_added):
    s = stats_load()
    sid = str(session_id)
    last = s["last_la"].get(sid)
    s["last_la"][sid] = lines_added
    if last is None or lines_added <= last:   # sessao nova para o bridge: so comeca a contar
        write_json(STATS_FILE, s)
        return
    s["lines"] += lines_added - last
    for n in (500, 1000, 2000):
        if s["lines"] >= n:
            award(s, "lines%d" % n, "{:,} lines today!".format(n))
    write_json(STATS_FILE, s)


def pop_achievement():
    s = read_json(STATS_FILE)
    if not s or not s.get("pending"):
        return None
    text = s["pending"].pop(0)
    write_json(STATS_FILE, s)
    return text


# ---------------------------------------------------------------- historico de uso

def append_history(now, h5, d7):
    hist = [h for h in (read_json(HIST_FILE) or []) if now - h[0] < HIST_KEEP_S]
    if hist and now - hist[-1][0] < 60:
        hist[-1] = [now, h5, d7]   # no maximo um ponto por minuto
    else:
        hist.append([now, h5, d7])
    write_json(HIST_FILE, hist)


def build_hist(now):
    """Uso de 5h nas ultimas 5 h, em baldes de 5 min (-1 = sem dado ainda)."""
    hist = read_json(HIST_FILE) or []
    vals, last, j = [], -1, 0
    start = now - HIST_BUCKETS * HIST_BUCKET_S
    while j < len(hist) and hist[j][0] < start:   # valor vigente no inicio da janela
        last = round(hist[j][1])
        j += 1
    for b in range(HIST_BUCKETS):
        end = start + (b + 1) * HIST_BUCKET_S
        best = None
        while j < len(hist) and hist[j][0] < end:
            v = round(hist[j][1])
            best = v if best is None else max(best, v)
            last = v
            j += 1
        vals.append(best if best is not None else last)
    return {"t": "hist", "h5": vals}


# ---------------------------------------------------------------- serve

def try_lock(f):
    """Trava o serve.lock sem esperar (so um serve por vez)."""
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(f):
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def serve_running():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "a+") as f:
        if try_lock(f):
            unlock(f)
            return False
        return True


def ensure_serve():
    if serve_running():
        return
    if not IS_WIN:   # Linux/macOS: sessao propria, sobrevive ao fim do hook
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                         start_new_session=True)
        return
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    args = [exe, os.path.abspath(__file__), "serve"]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    try:   # sai do job do Claude Code, para nao morrer junto com o hook
        subprocess.Popen(args, creationflags=flags | 0x01000000, **kw)   # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(args, creationflags=flags, **kw)


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 256 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def session_alive(sid, s, age, now):
    """O processo do Claude Code dono da sessao ainda existe? (o app desktop fecha sem SessionEnd)"""
    if s.get("only_demo") or sid == "demo":
        return True
    if not s.get("pid"):
        return age < NOPID_PRUNE_S   # arquivo de antes desta versao: so pela idade
    cached = _alive.get(sid)
    if cached and now - cached[1] < ALIVE_CHECK_S:
        return cached[0]
    try:
        alive = proc_started(s["pid"]) == s.get("pst")   # mesma criacao: nao e um PID reaproveitado
    except Exception:
        alive = True   # na duvida, nao some com a sessao
    _alive[sid] = (alive, now)
    return alive


def build_frame(now):
    """Junta as sessoes abertas num frame unico para o display."""
    global _dot_order
    best, best_key, usage_latest, n_sess = None, None, None, 0
    sessions = []
    override = read_json(OVERRIDE_FILE) or {}
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        names = []
    if override.get("only"):   # demo: mostra so a sessao dele
        names = [n for n in names if n.startswith(override["only"] + ".")]
    for n in names:
        path = os.path.join(SESS_DIR, n)
        if n.endswith(".state.json"):
            s = read_json(path)
            if not s:
                continue
            age = now - s.get("t", 0)
            sid = n[: -len(".state.json")]
            if age > SESSION_PRUNE_S or not session_alive(sid, s, age, now):
                for p in (path, path.replace(".state.json", ".usage.json")):   # como um SessionEnd
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                log("sessao %s encerrada (processo do Claude Code nao existe mais)" % sid[:8])
                continue
            n_sess += 1
            if s["st"] == "done" and age > DONE_TO_IDLE_S:
                s.update(st="idle", lbl="Idle")
            elif s["st"] in ("thinking", "tool") and age > BUSY_TO_IDLE_S:
                s.update(st="idle", lbl="Idle")
            elif s["st"] == "attention" and s.get("perm") and age > PERM_DECAY_S:
                # Provavelmente ja foi aceita: volta a mostrar a ferramenta, com um "?"
                s.update(st="tool", lbl=s.get("tool_lbl") or "Working",
                         det=s.get("tool_det") or "", act=s.get("tool_act") or "", maybe=1)
            s["usage"] = read_json(path.replace(".state.json", ".usage.json"))
            s["sid"] = n[: -len(".state.json")]
            sessions.append(s)
            key = (PRIORITY.get(s["st"], 0), s.get("t", 0))
            if best_key is None or key > best_key:
                best, best_key = s, key
        elif n.endswith(".usage.json"):
            u = read_json(path)
            if u and (usage_latest is None or u.get("t", 0) > usage_latest.get("t", 0)):
                usage_latest = u

    # Sessao fixada vence, menos quando outra precisa de voce
    pinned = next((s for s in sessions if s["sid"] == _pinned), None)
    if pinned and not (best and best["st"] == "attention" and best is not pinned):
        best = pinned

    frame = {"t": "data", "ts": int(now), "ns": n_sess}
    if len(sessions) >= 2:
        sessions.sort(key=lambda s: s["sid"])   # ordem estavel dos pontinhos
        dots = sessions[:MAX_DOTS]
        _dot_order = [s["sid"] for s in dots]
        frame["sl"] = "".join(STATE_CODE.get(s["st"], "i") for s in dots)
        frame["si"] = next((i for i, s in enumerate(dots) if s is best), -1)
        if pinned:
            frame["pin"] = 1
    else:
        _dot_order = []
    if best is None:
        frame.update(st="sleep", lbl="Sleeping", det="no Claude Code session")
    else:
        frame.update(st=best["st"], lbl=best.get("lbl", ""), det=best.get("det", ""))
        for src, dst in (("act", "act"), ("maybe", "maybe"), ("err", "err"), ("proj", "proj"),
                         ("turn_t", "tst"), ("turn_end", "tend"), ("ttk", "ttk"), ("evk", "evk"),
                         ("evt", "evt")):
            v = best.get(src)
            if v:
                frame[dst] = int(v) if isinstance(v, float) else v
        subs = [t for t in (best.get("subs") or {}).values() if now - t < SUB_PRUNE_S]
        if subs:
            frame["subs"] = len(subs)
    frame["md"] = override.get("md") or time.strftime("%m-%d")
    frame["hr"] = int(override.get("hr", time.localtime().tm_hour))
    # Dados da sessao mostrada; limites de uso sao da conta (o mais recente)
    u = (best or {}).get("usage") or usage_latest or {}
    if u.get("model"):
        frame["model"] = u["model"]
    if u.get("ctx") is not None:
        frame["ctx"] = round(u["ctx"], 1)
    for k in ("effort", "name", "dur", "la", "lr", "tin", "tout", "cexp", "cwarm"):
        if u.get(k):
            frame[k] = u[k]
    if u.get("cost") is not None:
        frame["cost"] = round(u["cost"], 2)
    rate = read_json(RATE_FILE)
    if rate is None:   # ainda sem rate.json: usa a ultima status line vista
        rl = (read_json(os.path.join(STATE_DIR, "statusline_last.json")) or {}).get("rate_limits") or {}
        rate = {short: {"p": pct(rl[key].get("used_percentage")), "r": int(pct(rl[key].get("resets_at")) or 0)}
                for key, short in (("five_hour", "h5"), ("seven_day", "d7"))
                if (rl.get(key) or {}).get("used_percentage") is not None}
    for k in ("h5", "d7"):
        src = usage_latest if usage_latest and k in usage_latest else u
        if k in src:
            frame[k] = src[k]
        elif k in rate and rate[k].get("r", 0) > now:
            frame[k] = rate[k]   # ultimo valor visto no CLI, ate a janela resetar
    return frame


def find_device(serial, list_ports):
    """Procura o CYD respondendo ao hello; nao mexe em DTR/RTS (evita reset)."""
    wanted = os.environ.get("CYD_PORT")
    ports = list(list_ports.comports())
    if wanted:
        ports = [p for p in ports if p.device.upper() == wanted.upper()]
    # Conversores USB-serial comuns primeiro (CH340, CP210x)
    ports.sort(key=lambda p: 0 if p.vid in (0x1A86, 0x10C4) else 1)
    for p in ports:
        if p.vid is None:   # portas Bluetooth e afins
            continue
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = p.device, BAUD, 0.1
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException:
            continue   # ocupada (monitor/gravacao) ou indisponivel
        s.reset_input_buffer()
        s.write(b'{"t":"hello","v":1}\n')
        buf, t0 = b"", time.time()
        while time.time() - t0 < 2.0:
            buf += s.read(256)
            info = parse_id(buf)
            if info.get("name") in ("deskrab", "cyd_claude"):   # cyd_claude = firmware antigo
                set_device(info, p.device)
                log("conectado em %s (firmware %s, idioma %s)" % (p.device, info.get("fw"), info.get("lang", "en")))
                with open(PORT_FILE, "w") as f:
                    f.write(p.device)
                return s
        s.close()
    return None


def handle_device_msg(msg):
    """Mensagens que o CYD manda (toques que precisam do PC)."""
    global _pinned
    if msg.get("t") == "id":   # o display reiniciou com a porta aberta (ex.: trocou o idioma)
        set_device(msg)
        log("display reiniciou (firmware %s, idioma %s)" % (msg.get("fw"), msg.get("lang", "en")))
    elif msg.get("t") == "fact":
        write_json(FACT_FILE, dict(msg, received=time.time()))
        log("teste de fabrica: %s" % ("aprovado" if msg.get("ok") else "reprovado"))
    elif msg.get("t") == "sel":
        i = msg.get("i", -1)
        sid = _dot_order[i] if isinstance(i, int) and 0 <= i < len(_dot_order) else None
        _pinned = None if sid == _pinned else sid   # tocar de novo solta
        log("sessao fixada: %s" % _pinned)


def close_device(dev):
    try:
        dev.close()
    except Exception:
        pass
    try:
        os.remove(PORT_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- Wi-Fi
# O display entra na rede de casa (configurada pelo USB com "wifi") e escuta TCP na 7777.
# O codigo de pareamento veio pelo cabo; a descoberta por UDP acha o IP se o DHCP mudar.

NET_PORT, NET_UDP_PORT = 7777, 7778
_net_err = None   # ultimo erro de rede registrado (para nao repetir no log a cada tentativa)


class NetDevice:
    """Conexao TCP com o display, com a mesma cara do serial.Serial que o serve usa."""

    def __init__(self, sock, host):
        self.sock, self.host, self.buf = sock, host, b""
        sock.setblocking(False)

    def _pull(self):
        while True:
            try:
                d = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                return
            if not d:
                raise OSError("o display fechou a conexao")
            self.buf += d

    @property
    def in_waiting(self):
        self._pull()
        return len(self.buf)

    def read(self, n):
        d, self.buf = self.buf[:n], self.buf[n:]
        return d

    def write(self, data):
        self.sock.settimeout(3)
        try:
            self.sock.sendall(data)
        finally:
            self.sock.setblocking(False)

    def close(self):
        self.sock.close()


def net_log(msg):
    global _net_err
    if msg != _net_err:
        log(msg)
        _net_err = msg


def net_discover(dev_id=None, timeout=2.0):
    """Procura o display na rede por broadcast UDP; devolve o status dele (com "ip") ou None."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                s.sendto(b"DESKRAB?", ("255.255.255.255", NET_UDP_PORT))
            except OSError:
                return None   # sem rede
            t1 = time.time()
            while time.time() - t1 < 0.6:
                try:
                    data, addr = s.recvfrom(512)
                except (socket.timeout, OSError):
                    continue
                try:
                    st = json.loads(data)
                except ValueError:
                    continue
                if st.get("name") == "deskrab" and (not dev_id or st.get("id") == dev_id):
                    st["ip"] = st.get("ip") or addr[0]
                    return st
    finally:
        s.close()
    return None


def net_connect(host, port, code):
    """Conecta e pareia. Devolve NetDevice, None (sem resposta) ou OSError se o codigo for recusado."""
    import socket
    s = socket.create_connection((host, port), timeout=3)
    s.sendall((json.dumps({"t": "hello", "v": 1, "code": code}) + "\n").encode())
    s.settimeout(0.5)
    buf, t0 = b"", time.time()
    while time.time() - t0 < 3:
        try:
            d = s.recv(512)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        info = parse_id(buf)
        if info.get("name") == "deskrab":
            set_device(info, "wifi:" + host)
            return NetDevice(s, host)
        if b'"why":"code"' in buf:
            s.close()
            raise PermissionError("codigo de pareamento recusado: rode o setup do Wi-Fi de novo pelo USB")
    s.close()
    return None


def find_net_device(cfg):
    host, port, code = cfg.get("host"), int(cfg.get("port") or NET_PORT), cfg.get("code") or ""
    for _ in range(2):
        if host:
            try:
                dev = net_connect(host, port, code)
            except PermissionError as e:
                net_log(str(e))
                return None
            except OSError as e:
                dev = None
                net_log("Wi-Fi: sem resposta de %s (%s)" % (host, e))
            if dev:
                net_log("conectado pelo Wi-Fi em %s" % host)
                with open(PORT_FILE, "w") as f:
                    f.write("wifi:" + host)
                return dev
        st = net_discover(cfg.get("id"))
        if not st or not st.get("ip") or st["ip"] == host:
            return None
        host = cfg["host"] = st["ip"]   # o DHCP deu outro IP
        write_json(CONFIG_FILE, cfg)
        net_log("display achado na rede em %s" % host)
    return None


def open_usb_paused():
    """Pausa o serve e abre o display pelo USB (para mandar a rede). Devolve o serial ou None."""
    import serial
    from serial.tools import list_ports
    cmd_pause()
    return find_device(serial, list_ports)


WIFI_WHY = {201: "rede nao encontrada (nome errado, ou rede so de 5 GHz: o display usa 2,4 GHz)",
            202: "senha errada", 15: "senha errada", 204: "senha errada",
            203: "o roteador recusou a conexao"}


def cmd_wifi(argv):
    """wifi --ssid NOME [--password SENHA]: manda a rede pelo USB e passa a usar o Wi-Fi.
    Sem --password, le DESKRAB_WIFI_PASSWORD, o stdin (se nao for terminal) ou pergunta."""
    import argparse
    ap = argparse.ArgumentParser(prog="deskrab_bridge.py wifi")
    ap.add_argument("--ssid", required=True)
    ap.add_argument("--password")
    a = ap.parse_args(argv)
    pw = a.password if a.password is not None else os.environ.get("DESKRAB_WIFI_PASSWORD")
    if pw is None:
        if sys.stdin.isatty():
            import getpass
            pw = getpass.getpass("senha do Wi-Fi \"%s\" (vazio = rede aberta): " % a.ssid)
        else:
            pw = sys.stdin.readline().rstrip("\r\n")
    up, last, fails = None, None, 0
    dev = open_usb_paused()
    try:
        if dev is None:
            print("display nao encontrado no USB: conecte o cabo e feche programas que usam a porta")
            return 1
        dev.write((json.dumps({"t": "wifi", "ssid": a.ssid, "pass": pw}) + "\n").encode())
        print("rede enviada ao display, esperando ele conectar (ate 40 s)...")
        buf, t0 = b"", time.time()
        while time.time() - t0 < 40 and not up:
            buf += dev.read(512)
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                if b'"t":"net"' not in ln:
                    continue
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue
                last = m
                if m.get("st") == "up" and m.get("ip"):
                    up = m
                    break
                if m.get("st") == "retry" and m.get("why") in WIFI_WHY:
                    fails += 1
            if fails >= 3:   # o mesmo erro de senha/rede tres vezes: nao adianta esperar
                break
        close_device(dev)
    finally:
        cmd_resume()
    if up:
        cfg = read_json(CONFIG_FILE) or {}
        cfg.update({"transport": "wifi", "host": up["ip"], "port": up.get("port") or NET_PORT,
                    "code": up.get("code") or "", "id": up.get("id") or "", "ssid": a.ssid})
        write_json(CONFIG_FILE, cfg)
        print("ok: display no Wi-Fi \"%s\" com o IP %s. O Deskrab passa a usar a rede;" % (a.ssid, up["ip"]))
        print("o cabo pode ir para qualquer carregador USB.")
        return 0
    why = (last or {}).get("why")
    print("nao conectou: %s." % (WIFI_WHY.get(why, "motivo %s" % why) if why else "sem resposta do display"))
    print("O display continua tentando essa rede. Rode de novo com os dados certos, ou 'usb' para voltar ao cabo.")
    return 1


def cmd_factory():
    """Roda o teste de fabrica no display e registra o resultado em ~/.deskrab/factory.csv."""
    ensure_serve()
    try:
        os.remove(FACT_FILE)
    except OSError:
        pass
    queue_to_device({"t": "factory"})
    print("teste enviado: siga a tela do display (1 calibracao, 2 cinco alvos, 3 cores, 4 sensor de luz).")
    t0, res = time.time(), None
    while time.time() - t0 < 360 and not res:
        res = read_json(FACT_FILE)
        time.sleep(0.5)
    if not res:
        print("sem resultado em 6 min: o display esta conectado? (deskrab_bridge.py status)")
        return 1
    ok = bool(res.get("ok"))
    errs = res.get("touch") or []
    yn = lambda v: "OK" if v else "FALHOU"
    print("unidade %s, firmware %s: %s" % (res.get("id"), res.get("fw"), "APROVADA" if ok else "REPROVADA"))
    print("  calibracao do toque:", yn(res.get("cal")))
    print("  toque nos 5 alvos: %s (erros em px: %s)" % (yn(res.get("touch_ok")), " ".join(str(e) for e in errs)))
    print("  cores e LED:", yn(res.get("colors")))
    print("  sensor de luz: %s a %s (so informativo)" % (res.get("ldr_min"), res.get("ldr_max")))
    new = not os.path.exists(FACT_LOG)
    with open(FACT_LOG, "a", encoding="utf-8") as f:
        if new:
            f.write("data,id,firmware,resultado,calibracao,toque,erros_px,cores,luz_min,luz_max\n")
        f.write("%s,%s,%s,%s,%d,%d,%s,%d,%s,%s\n" % (
            time.strftime("%Y-%m-%d %H:%M"), res.get("id"), res.get("fw"), "aprovada" if ok else "reprovada",
            bool(res.get("cal")), bool(res.get("touch_ok")), " ".join(str(e) for e in errs),
            bool(res.get("colors")), res.get("ldr_min"), res.get("ldr_max")))
    print("registrado em", FACT_LOG)
    return 0 if ok else 2


def cmd_lang(v):
    """Troca o idioma do display (pt ou en); ele reinicia para aplicar."""
    if v not in ("pt", "en"):
        print("uso: deskrab_bridge.py lang pt|en")
        return 1
    ensure_serve()
    queue_to_device({"t": "lang", "v": v})
    print("ok: o display reinicia em %s em alguns segundos" % ("portugues" if v == "pt" else "ingles"))
    return 0


def cmd_usb():
    """Volta para o cabo: desliga o Wi-Fi do display (se ele estiver no USB) e muda o transporte."""
    cfg = read_json(CONFIG_FILE) or {}
    cfg["transport"] = "usb"
    write_json(CONFIG_FILE, cfg)
    dev = open_usb_paused()
    try:
        if dev is not None:
            dev.write(b'{"t":"wifioff"}\n')
            time.sleep(0.5)
            close_device(dev)
            print("Wi-Fi do display desligado")
        else:
            print("display nao esta no USB: o Wi-Fi dele continua ligado ate ele ser conectado aqui")
    finally:
        cmd_resume()
    print("ok: o Deskrab usa o cabo USB")


def cmd_serve():
    import serial
    from serial.tools import list_ports

    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "a+")
    if not try_lock(lock):
        return   # ja existe outro serve
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log("serve iniciado (pid %d)" % os.getpid())

    dev, last_frame, last_sent, last_ping, last_rx = None, None, 0.0, 0.0, 0.0
    last_hist = last_ach = 0.0
    rx_buf = b""
    while True:
        if os.path.exists(PAUSE_FILE):
            if dev is not None:
                log("pausado: COM liberada")
                close_device(dev)
                dev = None
            time.sleep(0.3)
            continue
        if dev is None:
            cfg = read_json(CONFIG_FILE) or {}
            dev = find_net_device(cfg) if cfg.get("transport") == "wifi" else None
            if dev is None:   # modo USB, ou o Wi-Fi nao respondeu e o cabo esta no PC
                dev = find_device(serial, list_ports)
            if dev is None:
                time.sleep(5)
                continue
            last_frame, last_sent, last_ping, last_rx = None, 0.0, time.time(), time.time()
            last_hist = 0.0
            fw_told = None
        try:
            now = time.time()
            frame = localize(build_frame(now))
            cmp = dict(frame, ts=0)
            if cmp != last_frame or now - last_sent >= HEARTBEAT_S:
                dev.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
                last_frame, last_sent = cmp, now
            if now - last_ping >= PING_S:
                dev.write(b'{"t":"ping"}\n')
                last_ping = now
            if now - last_ach >= ACH_SEND_S:
                text = pop_achievement()
                if text:
                    dev.write((json.dumps({"t": "ach", "txt": ach_text(text)}) + "\n").encode())
                    log("conquista: " + text)
                last_ach = now
            newest = latest_firmware().get("version")
            if newest and fw_told != _dev_info.get("fw"):   # de novo se o display reiniciou
                fw_told = _dev_info.get("fw")
                if ver_tuple(newest) > ver_tuple(fw_told):
                    dev.write((json.dumps({"t": "fwnew", "v": newest}) + "\n").encode())
                    log("firmware %s no display, ha o %s: %s" % (fw_told, newest, latest_firmware().get("page", "")))
            box = read_json(OUTBOX_FILE) if os.path.exists(OUTBOX_FILE) else None
            if box is not None:
                try:
                    os.remove(OUTBOX_FILE)
                except OSError:
                    pass
                if now - box.get("t", 0) < 60:
                    for m in box.get("msgs") or []:
                        dev.write((json.dumps(m) + "\n").encode())
            play = read_json(PLAY_FILE) if os.path.exists(PLAY_FILE) else None
            if play is not None:
                try:
                    os.remove(PLAY_FILE)
                except OSError:
                    pass
                if now - play.get("t", 0) < 30:
                    msg = {"t": "play", "n": str(play.get("n", ""))[:15], "a": int(play.get("a", 0))}
                    dev.write((json.dumps(msg) + "\n").encode())
            if now - last_hist >= HIST_SEND_S:
                dev.write((json.dumps(build_hist(now), separators=(",", ":")) + "\n").encode())
                last_hist = now
            if dev.in_waiting:
                rx_buf += dev.read(dev.in_waiting)
                *lines, rx_buf = rx_buf.split(b"\n")
                rx_buf = rx_buf[-2048:]
                for ln in lines:
                    if b'"t":"pong"' in ln:
                        last_rx = now
                    elif ln.startswith(b'{"t":'):
                        try:
                            handle_device_msg(json.loads(ln))
                        except ValueError:
                            pass
                    elif b"cyd_" in ln:
                        log("cyd> " + ln.decode("ascii", "replace").strip())
            if now - last_rx > 3 * PING_S:
                raise serial.SerialException("sem resposta do CYD")
            time.sleep(0.25)
        except (serial.SerialException, OSError) as e:
            log("desconectado: %s" % e)
            close_device(dev)
            dev = None
            time.sleep(2)


def cmd_stop():
    pid = None
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pass
    if pid and serve_running():
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print("serve encerrado (pid %d)" % pid)
    else:
        print("serve nao estava rodando")


def cmd_pause():
    os.makedirs(STATE_DIR, exist_ok=True)
    open(PAUSE_FILE, "w").close()
    t0 = time.time()
    while serve_running() and os.path.exists(PORT_FILE) and time.time() - t0 < 10:
        time.sleep(0.2)
    print("pausado: COM livre")


def cmd_resume():
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass
    ensure_serve()
    print("retomado")


def cmd_demo():
    """Mostra todas as animacoes em ~1 min, com uma sessao falsa."""
    ensure_serve()
    os.makedirs(SESS_DIR, exist_ok=True)
    state_path, usage_path = session_paths("demo")
    t0 = time.time()
    write_json(OVERRIDE_FILE, {"only": "demo"})

    def usage(h5, ctx):
        write_json(usage_path, {
            "model": "Opus 5.5", "effort": "high", "name": "Demo session", "ctx": ctx,
            "t": time.time(), "cost": 1.23, "dur": 754, "la": 120, "lr": 8, "tin": 52000,
            "tout": 3100, "h5": {"p": h5, "r": int(t0) + 2 * 3600},
            "d7": {"p": 40.0, "r": int(t0) + 3 * 86400}})

    def state(st, lbl, det, act="", secs=5, **extra):
        print("%-10s %-12s %s" % (st, lbl, det))
        d = {"st": st, "lbl": lbl, "det": det, "act": act, "proj": "deskrab",
             "t": time.time(), "turn_t": t0}
        d.update(extra)
        write_json(state_path, d)
        time.sleep(secs)

    try:
        usage(30.0, 20.0)
        state("idle", "Ready", "cenario aparece depois de 3 s", secs=8)
        state("thinking", "Thinking", "deskrab", secs=4, ttk=1840)
        usage(90.0, 85.0)   # suando e barriga cheia
        subs = {"a": t0, "b": t0}
        state("tool", "Running", "idf.py build (2 subagents)", "run", secs=6, subs=subs)
        subs["c"] = t0
        state("tool", "Editing", "ui.c (3 subagents)", "edit", secs=5, subs=subs)
        state("tool", "Reading", "crab.c", "read", secs=4)
        state("tool", "Compacting", "squeezing the context", "compact", secs=4)
        state("thinking", "Thinking", "a tool failed", secs=4, err=int(time.time()))
        state("attention", "Allow Bash?", "idf.py -p COM8 flash", secs=5)
        usage(5.0, 85.0)    # limite resetou: phew
        state("thinking", "Thinking", "5h limit reset", secs=4)
        write_json(OVERRIDE_FILE, {"only": "demo", "md": "12-20"})   # dezembro: gorro de Papai Noel
        state("done", "Done", "long turn: confetti + santa hat", secs=7,
              turn_t=time.time() - 400, turn_end=time.time())
        st = stats_load()
        st["pending"].append("Demo complete: achievement unlocked!")
        write_json(STATS_FILE, st)
        state("done", "Done", "achievement banner", secs=8)
    finally:
        for p in (state_path, usage_path, OVERRIDE_FILE):
            try:
                os.remove(p)
            except OSError:
                pass


def cmd_play(name, arg=0):
    """Pede ao serve para mandar uma previa ao CYD (uma por vez)."""
    ensure_serve()
    os.makedirs(STATE_DIR, exist_ok=True)
    write_json(PLAY_FILE, {"n": name, "a": arg, "t": time.time()})


# Brincadeiras do idle e quanto tempo cada uma leva (s)
SHOW_SKITS = [
    ("visitor", 16), ("snip", 4), ("pushups", 6), ("dance", 9), ("gull", 7), ("sunbathe", 27),
    ("snowman", 23), ("brr", 5), ("umbrella", 13), ("castle", 21), ("fishing", 16),
    ("bubbles", 7), ("clam", 6), ("wish", 6), ("ufo", 10), ("butterfly", 10), ("fireworks", 7),
    ("juggle", 12), ("commit", 4), ("push", 4), ("testok", 4), ("testfail", 5),
]


def cmd_show():
    """Passeio pelas animacoes: pensando (nuvem, cachimbo, andar, vapor, lampada) e as do idle."""
    ensure_serve()
    os.makedirs(SESS_DIR, exist_ok=True)
    state_path, usage_path = session_paths("demo")
    t0 = time.time()
    write_json(OVERRIDE_FILE, {"only": "demo"})
    write_json(usage_path, {"model": "Opus 5.5", "effort": "xhigh", "name": "Show", "ctx": 30.0,
                            "t": t0, "h5": {"p": 20.0, "r": int(t0) + 3 * 3600}})

    def state(st, lbl, det, secs, **extra):
        print("%-10s %-12s %s" % (st, lbl, det))
        d = {"st": st, "lbl": lbl, "det": det, "act": "", "proj": "deskrab", "t": time.time(),
             "turn_t": t0}
        d.update(extra)
        write_json(state_path, d)
        time.sleep(secs)

    try:
        for tk in (0, 400, 1500, 4000, 9000, 20000, 32000):   # nuvem crescendo
            state("thinking", "Thinking", "deskrab (cloud grows)", 2, ttk=tk)
        for secs, what in ((25, "pipe"), (65, "pacing"), (185, "steam")):
            cmd_play("think", secs)
            state("thinking", "Thinking", "thinking for %d s: %s" % (secs, what), 8, ttk=32000)
        state("done", "Done", "bulb", 6, ttk=32000, turn_end=time.time())
        for name, secs in SHOW_SKITS:
            write_json(state_path, {"st": "idle", "lbl": "Idle", "det": name, "act": "",
                                    "proj": "deskrab", "t": time.time()})
            print("idle       %s" % name)
            time.sleep(1)
            cmd_play(name)
            time.sleep(secs)
    finally:
        for p in (state_path, usage_path, OVERRIDE_FILE):
            try:
                os.remove(p)
            except OSError:
                pass


HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification",
               "PreCompact", "PostCompact", "Stop", "StopFailure", "PermissionRequest", "PermissionDenied",
               "PostToolUseFailure", "SubagentStart", "SubagentStop")
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PermissionRequest", "PermissionDenied", "PostToolUseFailure")


def hooks_json():
    """Conteudo de plugin/hooks/hooks.json (python deskrab_bridge.py hooks-json > hooks/hooks.json).
    Forma exec: o Claude Code chama o Python direto, sem shell. O Python vem da opcao do plugin,
    porque "python3" no Windows costuma ser o atalho da Microsoft Store, que nao roda nada."""
    hook = {"type": "command", "command": "${user_config.python}",
            "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/deskrab_bridge.py", "hook"], "timeout": 10}
    return {"hooks": {ev: [dict({"matcher": "*"} if ev in TOOL_EVENTS else {}, hooks=[dict(hook)])]
                      for ev in HOOK_EVENTS}}


def python_exe():
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[: -len("pythonw.exe")] + "python.exe"
    return py


def refresh_stable_copy():
    """A status line aponta para uma copia fixa do bridge em ~/.deskrab: a pasta do plugin
    muda a cada versao. Cada SessionStart atualiza a copia."""
    me = os.path.abspath(__file__)
    if os.path.normcase(me) == os.path.normcase(STABLE_BRIDGE):
        return
    try:
        with open(me, "rb") as f:
            new = f.read()
        try:
            with open(STABLE_BRIDGE, "rb") as f:
                if f.read() == new:
                    return
        except OSError:
            pass
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STABLE_BRIDGE + ".tmp", "wb") as f:
            f.write(new)
        os.replace(STABLE_BRIDGE + ".tmp", STABLE_BRIDGE)
    except OSError as e:
        log("erro copiando o bridge para %s: %r" % (STABLE_BRIDGE, e))


def settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def backup_settings(path):
    if os.path.exists(path):
        bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(path, bak)
        print("backup:", bak)


def is_ours(obj):
    s = json.dumps(obj or {})
    return "deskrab_bridge" in s or "cyd_bridge" in s


# Windows: a status line roda pelo Git Bash. Se o bash.exe do Git sumiu (aqui o antivirus da
# empresa apagou), o Claude Code precisa de outro. O sh.exe do Git nao serve: a ferramenta Bash
# exige um arquivo bash.exe.
BASH_CANDIDATES = (r"C:\msys64\usr\bin\bash.exe",)


def git_bash():
    roots = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git")]
    git = shutil.which("git")
    if git:
        roots.insert(0, os.path.dirname(os.path.dirname(git)))
    return next((b for b in (os.path.join(r, "usr", "bin", "bash.exe") for r in roots) if os.path.isfile(b)), None)


def cmd_setup():
    """Configura o que o plugin nao consegue sozinho, com backup do settings.json:
    a opcao "python" do plugin, a status line (guardando a do usuario) e, no Windows sem
    Git Bash, um bash para o Claude Code. Tira hooks de instalacoes antigas (sem plugin)."""
    py = python_exe().replace("\\", "/")
    path = settings_path()
    settings = read_json(path) or {}
    backup_settings(path)

    opts = settings.setdefault("pluginConfigs", {}).setdefault(PLUGIN_ID, {}).setdefault("options", {})
    opts["python"] = py
    print("python do plugin:", py)

    hooks = settings.get("hooks") or {}
    removed = 0
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            keep = [h for h in g.get("hooks", []) if not is_ours(h)]
            removed += len(g.get("hooks", [])) - len(keep)
            if keep:
                groups.append(dict(g, hooks=keep))
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if removed:
        print("hooks antigos removidos (agora vem do plugin):", removed)
    if "hooks" in settings and not hooks:
        del settings["hooks"]

    cur = settings.get("statusLine")
    if cur and not is_ours(cur):
        write_json(PREV_SL_FILE, cur)
        print("status line anterior guardada: ela continua aparecendo, o Deskrab so le os dados")
    refresh_stable_copy()
    q = lambda s: '"%s"' % s if " " in s else s
    settings["statusLine"] = {"type": "command",
                              "command": "%s %s statusline" % (q(py), q(STABLE_BRIDGE.replace("\\", "/")))}
    print("status line:", settings["statusLine"]["command"])

    if os.name == "nt":
        env = settings.get("env") or {}
        if not os.path.isfile(env.get("CLAUDE_CODE_GIT_BASH_PATH") or "") and not git_bash():
            alt = next((b for b in BASH_CANDIDATES if os.path.isfile(b)), None)
            if alt:
                settings.setdefault("env", {})["CLAUDE_CODE_GIT_BASH_PATH"] = alt.replace("\\", "/")
                print("Git Bash nao encontrado; usando", alt)
            else:
                print("aviso: sem Git Bash a status line (limites de 5h/7d) nao roda. Instale o Git for Windows.")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print("ok: %s atualizado. Abra uma sessao nova do Claude Code." % path)
    cmd_status(wait=True)


def cmd_uninstall():
    """Tira a status line do Deskrab e devolve a anterior. O plugin sai com /plugin uninstall."""
    path = settings_path()
    settings = read_json(path) or {}
    backup_settings(path)
    prev = read_json(PREV_SL_FILE)
    if is_ours(settings.get("statusLine")):
        if prev:
            settings["statusLine"] = prev
        else:
            settings.pop("statusLine", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    for p in (PREV_SL_FILE,):
        try:
            os.remove(p)
        except OSError:
            pass
    cmd_stop()
    print("status line do Deskrab removida. Para tirar o plugin: /plugin uninstall %s" % PLUGIN_ID)


def cmd_status(wait=False):
    """Mostra se o serve esta rodando e se achou o display (inicia o serve com wait=True)."""
    if wait and not serve_running():
        ensure_serve()
    port, t0 = None, time.time()
    while True:
        try:
            with open(PORT_FILE) as f:
                port = f.read().strip()
        except OSError:
            port = None
        if port or not wait or time.time() - t0 > 12:
            break
        time.sleep(0.5)
    cfg = read_json(CONFIG_FILE) or {}
    print("serve:", "rodando" if serve_running() else "parado")
    print("pausado:", "sim" if os.path.exists(PAUSE_FILE) else "nao")
    if cfg.get("transport") == "wifi":
        print("transporte: Wi-Fi \"%s\", display em %s" % (cfg.get("ssid", ""), cfg.get("host", "?")))
    else:
        print("transporte: USB")
    print("display:", ("conectado em %s" % port) if port else "nao encontrado (cabo USB? Wi-Fi? outro programa usando a porta?)")
    dev = read_json(DEVICE_FILE) or {}
    if dev.get("fw"):
        newest = latest_firmware()
        note = ""
        if newest.get("version") and ver_tuple(newest["version"]) > ver_tuple(dev["fw"]):
            note = "  -> ha o %s: atualize em %s" % (newest["version"], newest.get("page", ""))
        print("firmware: %s%s" % (dev["fw"], note))
        print("idioma do display:", {"pt": "portugues", "en": "ingles"}.get(dev.get("lang"), "ingles"))
    print("dados em:", STATE_DIR)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("hook", "statusline"):
        try:
            cmd_hook() if cmd == "hook" else cmd_statusline()
        except Exception as e:
            log("erro no %s: %r" % (cmd, e))
        if cmd == "hook":   # a status line roda de uma copia sem o pyserial embutido: so os hooks iniciam o serve
            try:
                ensure_serve()
            except Exception:
                pass
        return 0
    if cmd == "setup":
        cmd_setup()
    elif cmd == "uninstall":
        cmd_uninstall()
    elif cmd == "status":
        cmd_status()
    elif cmd == "wifi":
        return cmd_wifi(sys.argv[2:])
    elif cmd == "usb":
        cmd_usb()
    elif cmd == "factory":
        return cmd_factory()
    elif cmd == "lang" and len(sys.argv) > 2:
        return cmd_lang(sys.argv[2])
    elif cmd == "hooks-json":
        print(json.dumps(hooks_json(), indent=2))
    elif cmd == "serve":
        cmd_serve()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "pause":
        cmd_pause()
    elif cmd == "resume":
        cmd_resume()
    elif cmd == "demo":
        cmd_demo()
    elif cmd == "play" and len(sys.argv) > 2:
        cmd_play(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    elif cmd == "show":
        cmd_show()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
