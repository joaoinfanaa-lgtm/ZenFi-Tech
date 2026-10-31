#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZenFi Tech - servidor de login.

Só usa a biblioteca padrão do Python (nada para instalar).
Guarda as contas num banco SQLite (zenfi.db) ao lado deste arquivo.

Uso:
    python servidor.py                 -> http://localhost:8000 (só este aparelho)
    python servidor.py --porta 9000    -> outra porta
    python servidor.py --rede          -> aceita outros aparelhos do mesmo Wi-Fi
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_HTML = os.path.join(PASTA, "zenfi_whatsapp.html")
ARQ_BANCO = os.path.join(PASTA, "zenfi.db")

COOKIE = "zenfi_sessao"
DIAS_SESSAO = 30
ITER_SENHA = 200_000          # PBKDF2-SHA256; o valor fica gravado em cada hash
MAX_CORPO = 10_000            # bytes
MAX_ERROS = 5                 # tentativas erradas ...
JANELA_ERROS = 10 * 60        # ... dentro de 10 minutos
RX_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")

# WhatsApp Cloud API (configure estas variáveis no ambiente da hospedagem)
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TEMPLATE = os.environ.get("WHATSAPP_VERIFY_TEMPLATE", "zenfi_verificacao")
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v23.0")


# arquivos estáticos permitidos além da página principal
ESTATICOS = {
    "/manifest.webmanifest": "application/manifest+json",
    "/icon-192.png": "image/png",
}


# ---------------------------------------------------------------- banco
def banco():
    con = sqlite3.connect(ARQ_BANCO, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def criar_tabelas():
    con = banco()
    with con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nome       TEXT NOT NULL,
                email      TEXT NOT NULL UNIQUE,
                documento  TEXT UNIQUE,
                tipo_conta TEXT NOT NULL DEFAULT 'pf',
                senha_hash TEXT NOT NULL,
                criado_em  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessoes (
                token_hash TEXT PRIMARY KEY,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                expira_em  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS erros_login (
                chave TEXT NOT NULL,
                quando INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_erros ON erros_login(chave, quando);
            """
        )
        # Migrações compatíveis com bancos ZenFi já existentes.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(usuarios)").fetchall()}
        if "whatsapp_telefone" not in cols:
            con.execute("ALTER TABLE usuarios ADD COLUMN whatsapp_telefone TEXT")
        if "whatsapp_verificado_em" not in cols:
            con.execute("ALTER TABLE usuarios ADD COLUMN whatsapp_verificado_em INTEGER")
        con.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_usuario_whatsapp
                ON usuarios(whatsapp_telefone) WHERE whatsapp_telefone IS NOT NULL;
            CREATE TABLE IF NOT EXISTS whatsapp_codigos (
                usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id) ON DELETE CASCADE,
                telefone TEXT NOT NULL, codigo_hash TEXT NOT NULL,
                expira_em INTEGER NOT NULL, tentativas INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS whatsapp_pendentes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                wamid TEXT NOT NULL UNIQUE, tipo TEXT NOT NULL,
                valor_centavos INTEGER NOT NULL, descricao TEXT NOT NULL,
                categoria TEXT NOT NULL DEFAULT 'Outros', dia INTEGER NOT NULL,
                criado_em INTEGER NOT NULL, consumido_em INTEGER
            );
        """)
        agora = int(time.time())
        con.execute("DELETE FROM sessoes WHERE expira_em < ?", (agora,))
        con.execute("DELETE FROM erros_login WHERE quando < ?", (agora - JANELA_ERROS,))
    con.close()
    try:
        os.chmod(ARQ_BANCO, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- senha
def hash_senha(senha, iteracoes=ITER_SENHA):
    sal = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, iteracoes)
    return "pbkdf2_sha256$%d$%s$%s" % (iteracoes, sal.hex(), dk.hex())


def confere_senha(senha, guardado):
    try:
        _, it, sal, dk = guardado.split("$")
        novo = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(novo.hex(), dk)
    except Exception:
        return False


# hash falso para gastar o mesmo tempo quando o usuário não existe
_FALSO = hash_senha("zenfi-falso")


# ---------------------------------------------------------------- CPF / CNPJ
def cpf_valido(d):
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for t in (9, 10):
        soma = sum(int(d[i]) * (t + 1 - i) for i in range(t))
        if (soma * 10) % 11 % 10 != int(d[t]):
            return False
    return True


def cnpj_valido(d):
    if len(d) != 14 or len(set(d)) == 1:
        return False
    for t in (12, 13):
        soma, peso = 0, t - 7
        for i in range(t):
            soma += int(d[i]) * peso
            peso -= 1
            if peso < 2:
                peso = 9
        dv = 0 if soma % 11 < 2 else 11 - soma % 11
        if dv != int(d[t]):
            return False
    return True


# ---------------------------------------------------------------- sessão
def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def criar_sessao(con, usuario_id):
    token = secrets.token_urlsafe(32)
    exp = int(time.time()) + DIAS_SESSAO * 86400
    with con:
        con.execute(
            "INSERT INTO sessoes (token_hash, usuario_id, expira_em) VALUES (?,?,?)",
            (hash_token(token), usuario_id, exp),
        )
    return token


def usuario_json(u):
    return {
        "nome": u["nome"],
        "email": u["email"],
        "documento": u["documento"] or "",
        "tipo_conta": u["tipo_conta"],
    }


class Erro(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ---------------------------------------------------------------- WhatsApp

def normalizar_telefone(valor):
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("00"):
        d = d[2:]
    if not (12 <= len(d) <= 13) or not d.startswith("55"):
        return ""
    return d


def enviar_template_verificacao(telefone, codigo):
    """Envia template aprovado pela Meta; não usa texto livre fora da janela."""
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID):
        raise RuntimeError("WHATSAPP_TOKEN e WHATSAPP_PHONE_NUMBER_ID não configurados.")
    from urllib.request import Request, urlopen
    url = "https://graph.facebook.com/%s/%s/messages" % (WHATSAPP_API_VERSION, WHATSAPP_PHONE_NUMBER_ID)
    payload = {"messaging_product": "whatsapp", "to": telefone, "type": "template",
        "template": {"name": WHATSAPP_VERIFY_TEMPLATE, "language": {"code": "pt_BR"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": codigo}]}]}}
    req = Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("A Meta não aceitou o envio. Confira o template aprovado e as credenciais.") from exc


def interpretar_mensagem_whatsapp(texto):
    t = str(texto or "").strip()
    if len(t) > 500:
        return None
    m = re.search(r"(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[,.]\d{1,2})?)\s*(?:reais?|r\$)?", t, re.I)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}\.\d{3}", raw):
        raw = raw.replace(".", "")
    try:
        cents = int(round(float(raw) * 100))
    except Exception:
        return None
    if cents <= 0 or cents > 100000000000:
        return None
    low = t.lower()
    if any(w in low for w in ("recebi", "ganhei", "salário", "salario", "renda", "entrou", "freela", "vendi")):
        tipo = "receita"
    elif any(w in low for w in ("gastei", "paguei", "comprei", "despesa", "gasto", "pix para")):
        tipo = "despesa"
    else:
        return None
    desc = re.sub(r"^(?:recebi|ganhei|gastei|paguei|comprei|despesa|gasto)\\s+", "", t, flags=re.I)[:60]
    desc = desc or ("Receita" if tipo == "receita" else "Despesa")
    dm = re.search(r"\bdia\s+(\d{1,2})\b", low)
    dia = max(1, min(31, int(dm.group(1)) if dm else time.localtime().tm_mday))
    cat = "Outros"
    for key, value in [("mercado","Mercado"),("supermercado","Mercado"),("almoço","Alimentação"),("almoco","Alimentação"),("jantar","Alimentação"),("lanche","Alimentação"),("combustível","Transporte"),("combustivel","Transporte"),("uber","Transporte"),("ônibus","Transporte"),("onibus","Transporte"),("farmácia","Saúde"),("farmacia","Saúde"),("aluguel","Moradia"),("luz","Moradia"),("água","Moradia"),("agua","Moradia"),("internet","Moradia")]:
        if key in low:
            cat = value
            break
    return {"tipo":tipo,"valor_centavos":cents,"descricao":desc,"categoria":cat,"dia":dia}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ZenFi"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # sem log de requisições (evita vazar dados no terminal)

    # ---- utilidades
    def _cabecalhos_seguros(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _enviar(self, status, corpo, tipo, extra=None, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", cache)
        self._cabecalhos_seguros()
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, status, dados, extra=None):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self._enviar(status, corpo, "application/json; charset=utf-8", extra)

    def _token(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return m.value if m else None

    def _set_cookie(self, token, max_age):
        return [("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (COOKIE, token, max_age))]

    def _ler_json(self):
        # CSRF: se o navegador mandou Origin, tem que ser o mesmo endereço do servidor
        origem = self.headers.get("Origin")
        if origem and urlsplit(origem).netloc != self.headers.get("Host", ""):
            raise Erro(403, "Origem não permitida.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise Erro(415, "Formato inválido.")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_CORPO:
            raise Erro(413, "Pedido grande demais.")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Erro(400, "Pedido inválido.")
        if not isinstance(d, dict):
            raise Erro(400, "Pedido inválido.")
        return d

    def _usuario_logado(self, con):
        token = self._token()
        if not token:
            return None
        return con.execute(
            "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
            "WHERE s.token_hash = ? AND s.expira_em > ?",
            (hash_token(token), int(time.time())),
        ).fetchone()

    # ---- rotas GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        caminho = urlsplit(self.path).path
        try:
            if caminho == "/webhook/whatsapp":
                mode = self.headers.get("X-Hub-Mode") or urlsplit(self.path).query
                q = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(urlsplit(self.path).query))
                if q.get("hub.mode") == "subscribe" and META_VERIFY_TOKEN and hmac.compare_digest(q.get("hub.verify_token", ""), META_VERIFY_TOKEN):
                    return self._enviar(200, q.get("hub.challenge", "").encode(), "text/plain; charset=utf-8")
                return self._enviar(403, b"Forbidden", "text/plain; charset=utf-8")
            if caminho == "/api/whatsapp/pendentes":
                con = banco()
                try:
                    u = self._usuario_logado(con)
                    if not u:
                        return self._json(401, {"erro": "Entre na sua conta."})
                    rows = con.execute("""SELECT id,tipo,valor_centavos,descricao,categoria,dia
                        FROM whatsapp_pendentes WHERE usuario_id=? AND consumido_em IS NULL
                        ORDER BY id LIMIT 30""", (u["id"],)).fetchall()
                    return self._json(200, {"lancamentos": [dict(r) for r in rows]})
                finally:
                    con.close()
            if caminho == "/api/whatsapp/vinculo":
                con = banco()
                try:
                    u = self._usuario_logado(con)
                    if not u:
                        return self._json(401, {"erro": "Entre na sua conta."})
                    return self._json(200, {"vinculado": bool(u["whatsapp_telefone"] and u["whatsapp_verificado_em"]),
                        "telefone": ("+" + u["whatsapp_telefone"]) if u["whatsapp_telefone"] else ""})
                finally:
                    con.close()
            if caminho in ("/", "/index.html", "/organiza.html"):
                with open(ARQ_HTML, "rb") as f:
                    return self._enviar(200, f.read(), "text/html; charset=utf-8")
            if caminho == "/api/eu":
                con = banco()
                try:
                    u = self._usuario_logado(con)
                finally:
                    con.close()
                if not u:
                    return self._json(401, {"erro": "Sem sessão."})
                return self._json(200, usuario_json(u))
            if caminho in ESTATICOS:
                arq = os.path.join(PASTA, os.path.basename(caminho))
                if os.path.isfile(arq):
                    with open(arq, "rb") as f:
                        return self._enviar(200, f.read(), ESTATICOS[caminho], cache="public, max-age=3600")
            self._json(404, {"erro": "Não encontrado."})
        except FileNotFoundError:
            self._json(500, {"erro": "Arquivo zenfi_whatsapp.html não está na mesma pasta do servidor.py."})
        except Exception:
            self._json(500, {"erro": "Erro interno."})

    # ---- rotas POST
    def do_POST(self):
        caminho = urlsplit(self.path).path
        try:
            if caminho == "/webhook/whatsapp":
                return self._webhook_whatsapp()
            dados = self._ler_json()
            if caminho == "/api/whatsapp/vinculo/iniciar":
                return self._wa_iniciar(dados)
            if caminho == "/api/whatsapp/vinculo/confirmar":
                return self._wa_confirmar(dados)
            if caminho == "/api/whatsapp/pendentes":
                return self._wa_pendentes()
            if caminho == "/api/whatsapp/consumir":
                return self._wa_consumir(dados)
            if caminho == "/api/cadastro":
                return self._cadastro(dados)
            if caminho == "/api/entrar":
                return self._entrar(dados)
            if caminho == "/api/sair":
                return self._sair()
            raise Erro(404, "Não encontrado.")
        except Erro as e:
            self._json(e.status, {"erro": e.msg})
        except Exception:
            self._json(500, {"erro": "Erro interno."})


    def _wa_iniciar(self, d):
        telefone = normalizar_telefone(d.get("telefone"))
        if not telefone:
            raise Erro(400, "Informe o telefone com DDI. Exemplo: +55 11 99999-9999.")
        con = banco()
        try:
            u = self._usuario_logado(con)
            if not u:
                raise Erro(401, "Entre na sua conta.")
            dono = con.execute("SELECT id FROM usuarios WHERE whatsapp_telefone=? AND id<>?", (telefone, u["id"])).fetchone()
            if dono:
                raise Erro(409, "Este número já está vinculado a outra conta ZenFi.")
            codigo = "%06d" % secrets.randbelow(1000000)
            codigo_hash = hashlib.sha256((str(u["id"]) + ":" + codigo).encode()).hexdigest()
            with con:
                con.execute("""INSERT INTO whatsapp_codigos(usuario_id,telefone,codigo_hash,expira_em,tentativas)
                    VALUES(?,?,?,?,0) ON CONFLICT(usuario_id) DO UPDATE SET
                    telefone=excluded.telefone,codigo_hash=excluded.codigo_hash,
                    expira_em=excluded.expira_em,tentativas=0""",
                    (u["id"], telefone, codigo_hash, int(time.time()) + 600))
            try:
                enviar_template_verificacao(telefone, codigo)
            except RuntimeError as exc:
                with con:
                    con.execute("DELETE FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],))
                raise Erro(503, str(exc))
            return self._json(200, {"ok": True, "mensagem": "Código enviado. Ele expira em 10 minutos."})
        finally:
            con.close()

    def _wa_confirmar(self, d):
        telefone = normalizar_telefone(d.get("telefone"))
        codigo = re.sub(r"\D", "", str(d.get("codigo", "")))
        if not telefone or not re.fullmatch(r"\d{6}", codigo):
            raise Erro(400, "Telefone ou código inválido.")
        con = banco()
        try:
            u = self._usuario_logado(con)
            if not u:
                raise Erro(401, "Entre na sua conta.")
            row = con.execute("SELECT * FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],)).fetchone()
            if not row or row["telefone"] != telefone or row["expira_em"] < int(time.time()) or row["tentativas"] >= 5:
                raise Erro(400, "Código expirado ou inválido. Solicite outro.")
            esperado = hashlib.sha256((str(u["id"]) + ":" + codigo).encode()).hexdigest()
            if not hmac.compare_digest(esperado, row["codigo_hash"]):
                with con:
                    con.execute("UPDATE whatsapp_codigos SET tentativas=tentativas+1 WHERE usuario_id=?", (u["id"],))
                raise Erro(400, "Código incorreto.")
            with con:
                con.execute("UPDATE usuarios SET whatsapp_telefone=?,whatsapp_verificado_em=? WHERE id=?",
                            (telefone, int(time.time()), u["id"]))
                con.execute("DELETE FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],))
            return self._json(200, {"ok": True, "mensagem": "WhatsApp vinculado e verificado com sucesso."})
        finally:
            con.close()

    def _wa_pendentes(self):
        con = banco()
        try:
            u = self._usuario_logado(con)
            if not u:
                raise Erro(401, "Entre na sua conta.")
            rows = con.execute("""SELECT id,tipo,valor_centavos,descricao,categoria,dia
                FROM whatsapp_pendentes WHERE usuario_id=? AND consumido_em IS NULL
                ORDER BY id LIMIT 30""", (u["id"],)).fetchall()
            return self._json(200, {"lancamentos": [dict(r) for r in rows]})
        finally:
            con.close()

    def _wa_consumir(self, d):
        ids = d.get("ids")
        if not isinstance(ids, list) or len(ids) > 30:
            raise Erro(400, "Lista de lançamentos inválida.")
        ids = [int(x) for x in ids if str(x).isdigit()]
        con = banco()
        try:
            u = self._usuario_logado(con)
            if not u:
                raise Erro(401, "Entre na sua conta.")
            if ids:
                with con:
                    con.executemany("UPDATE whatsapp_pendentes SET consumido_em=? WHERE id=? AND usuario_id=?",
                                    [(int(time.time()), x, u["id"]) for x in ids])
            return self._json(200, {"ok": True})
        finally:
            con.close()

    def _webhook_whatsapp(self):
        # Valida tamanho e JSON sem exigir sessão/cookie: a chamada vem da Meta.
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > 1000000:
            return self._enviar(413, b"Payload too large", "text/plain")
        raw = self.rfile.read(n)
        if META_APP_SECRET:
            assinatura = self.headers.get("X-Hub-Signature-256", "")
            esperado = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(assinatura, esperado):
                return self._enviar(403, b"Invalid signature", "text/plain")
        else:
            return self._enviar(503, b"META_APP_SECRET not configured", "text/plain")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._enviar(400, b"Bad JSON", "text/plain")
        if payload.get("object") != "whatsapp_business_account":
            return self._json(200, {"ok": True})
        con = banco()
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        if msg.get("type") != "text":
                            continue
                        telefone = normalizar_telefone(msg.get("from", ""))
                        parsed = interpretar_mensagem_whatsapp(msg.get("text", {}).get("body", ""))
                        wamid = str(msg.get("id", ""))
                        if not telefone or not parsed or not wamid:
                            continue
                        u = con.execute("""SELECT id FROM usuarios
                            WHERE whatsapp_telefone=? AND whatsapp_verificado_em IS NOT NULL""", (telefone,)).fetchone()
                        if not u:
                            continue
                        try:
                            with con:
                                con.execute("""INSERT INTO whatsapp_pendentes
                                    (usuario_id,wamid,tipo,valor_centavos,descricao,categoria,dia,criado_em)
                                    VALUES(?,?,?,?,?,?,?,?)""",
                                    (u["id"], wamid, parsed["tipo"], parsed["valor_centavos"],
                                     parsed["descricao"], parsed["categoria"], parsed["dia"], int(time.time())))
                        except sqlite3.IntegrityError:
                            pass  # Meta pode reenviar o mesmo webhook; wamid evita duplicação.
            return self._json(200, {"ok": True})
        finally:
            con.close()

    def _cadastro(self, d):
        nome = re.sub(r"\s+", " ", str(d.get("nome", "")).strip())
        email = str(d.get("email", "")).strip().lower()
        senha = str(d.get("senha", ""))
        tipo = str(d.get("tipo", "pf"))
        doc = re.sub(r"\D", "", str(d.get("documento", "")))

        if not 2 <= len(nome) <= 30:
            raise Erro(400, "Digite seu nome (de 2 a 30 letras).")
        if len(email) > 80 or not RX_EMAIL.match(email):
            raise Erro(400, "Digite um e-mail válido, como voce@exemplo.com.")
        if not doc:
            raise Erro(400, "Digite seu CPF ou CNPJ.")
        if len(doc) == 11:
            if not cpf_valido(doc):
                raise Erro(400, "CPF inválido. Confira os números.")
            tipo = "pf"
        elif len(doc) == 14:
            if not cnpj_valido(doc):
                raise Erro(400, "CNPJ inválido. Confira os números.")
            tipo = "pj"
        else:
            raise Erro(400, "Digite um CPF (11 dígitos) ou CNPJ (14 dígitos).")
        if not 6 <= len(senha) <= 64:
            raise Erro(400, "A senha precisa ter de 6 a 64 caracteres.")

        con = banco()
        try:
            if con.execute("SELECT 1 FROM usuarios WHERE email = ?", (email,)).fetchone():
                raise Erro(409, "Este e-mail já está cadastrado. Use Entrar.")
            if con.execute("SELECT 1 FROM usuarios WHERE documento = ?", (doc,)).fetchone():
                raise Erro(409, "Este CPF/CNPJ já está cadastrado. Use Entrar.")
            h = hash_senha(senha)
            try:
                with con:
                    cur = con.execute(
                        "INSERT INTO usuarios (nome, email, documento, tipo_conta, senha_hash, criado_em) "
                        "VALUES (?,?,?,?,?,?)",
                        (nome, email, doc, tipo, h, int(time.time())),
                    )
            except sqlite3.IntegrityError:
                raise Erro(409, "Esta conta já está cadastrada. Use Entrar.")
            token = criar_sessao(con, cur.lastrowid)
            u = con.execute("SELECT * FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
            self._json(200, usuario_json(u), self._set_cookie(token, DIAS_SESSAO * 86400))
        finally:
            con.close()

    def _entrar(self, d):
        ident = str(d.get("identificador", "")).strip()
        senha = str(d.get("senha", ""))
        if not ident or not senha or len(ident) > 80 or len(senha) > 64:
            raise Erro(400, "Digite seu e-mail ou CPF/CNPJ e a senha.")
        if "@" in ident:
            campo, valor = "email", ident.lower()
        else:
            campo, valor = "documento", re.sub(r"\D", "", ident)
        chave = "%s|%s" % (self.client_address[0], valor)

        con = banco()
        try:
            agora = int(time.time())
            n = con.execute(
                "SELECT COUNT(*) FROM erros_login WHERE chave = ? AND quando > ?",
                (chave, agora - JANELA_ERROS),
            ).fetchone()[0]
            if n >= MAX_ERROS:
                raise Erro(429, "Muitas tentativas erradas. Espere 10 minutos e tente de novo.")

            u = con.execute("SELECT * FROM usuarios WHERE %s = ?" % campo, (valor,)).fetchone() if valor else None
            ok = confere_senha(senha, u["senha_hash"] if u else _FALSO) and u is not None
            if not ok:
                with con:
                    con.execute("INSERT INTO erros_login (chave, quando) VALUES (?,?)", (chave, agora))
                raise Erro(401, "E-mail/CPF ou senha incorretos.")
            with con:
                con.execute("DELETE FROM erros_login WHERE chave = ?", (chave,))
            token = criar_sessao(con, u["id"])
            self._json(200, usuario_json(u), self._set_cookie(token, DIAS_SESSAO * 86400))
        finally:
            con.close()

    def _sair(self):
        token = self._token()
        if token:
            con = banco()
            try:
                with con:
                    con.execute("DELETE FROM sessoes WHERE token_hash = ?", (hash_token(token),))
            finally:
                con.close()
        self._json(200, {"ok": True}, self._set_cookie("", 0))


def main():
    ap = argparse.ArgumentParser(description="Servidor de login do ZenFi Tech")
    ap.add_argument("--porta", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--rede", action="store_true", help="aceitar conexões externas")
    a = ap.parse_args()

    if not os.path.isfile(ARQ_HTML):
        raise SystemExit("Coloque o zenfi_whatsapp.html na mesma pasta do servidor.")
    criar_tabelas()
    # Em hospedagem, o serviço precisa escutar na interface pública e na porta fornecida.
    host = "0.0.0.0" if (a.rede or os.environ.get("PORT")) else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.porta), Handler)
    print("ZenFi Tech no ar.")
    print("Abra no navegador do celular:  http://localhost:%d" % a.porta)
    if a.rede:
        print("Modo rede ligado: use o IP do celular no Wi-Fi, porta %d." % a.porta)
    print("Para desligar: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor desligado.")


if __name__ == "__main__":
    main()
