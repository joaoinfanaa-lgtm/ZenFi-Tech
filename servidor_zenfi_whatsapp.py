#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZenFi Tech - servidor (login, nuvem, WhatsApp, planos).

Só usa a biblioteca padrão do Python. Banco: SQLite em DATA_DIR (no Render,
um Persistent Disk montado em /var/data). Credenciais: SOMENTE variáveis de
ambiente (veja .env.exemplo e o LEIA-ME).

    python servidor_zenfi_whatsapp.py              -> http://localhost:8000
"""
import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import signal
import smtplib
import sqlite3
import sys
import threading
import time
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zenfi_parser  # noqa: E402
import zenfi_backup  # noqa: E402

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_HTML = os.path.join(PASTA, "zenfi_whatsapp.html")


def env(nome, padrao=""):
    return os.environ.get(nome, padrao).strip()


DATA_DIR = env("DATA_DIR", os.path.join(PASTA, "dados"))
ARQ_BANCO = os.path.join(DATA_DIR, "zenfi.db")
BASE_URL = env("BASE_URL").rstrip("/")            # ex.: https://zenfi.onrender.com
DEV = env("ZENFI_DEV") == "1"                       # só para testes locais
TRUST_PROXY = env("TRUST_PROXY", "1" if env("RENDER") else "0") == "1"

COOKIE = "zenfi_sessao"
DIAS_SESSAO = 30
ITER_SENHA = 200_000
MAX_CORPO = 20_000
MAX_CORPO_SYNC = 8_000_000
MAX_ERROS = 5
JANELA_ERROS = 10 * 60
RX_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")

# Meta / WhatsApp Cloud API
META_VERIFY_TOKEN = env("META_VERIFY_TOKEN")
META_APP_SECRET = env("META_APP_SECRET")
WHATSAPP_TOKEN = env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_NUMERO_PUBLICO = re.sub(r"\D", "", env("WHATSAPP_NUMERO_PUBLICO"))  # ex.: 5511999999999
WHATSAPP_VERIFY_TEMPLATE = env("WHATSAPP_VERIFY_TEMPLATE")  # opcional
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", "v23.0")
WHATSAPP_API_BASE = env("WHATSAPP_API_BASE", "https://graph.facebook.com").rstrip("/")

# Assinatura (Stripe) - tudo opcional até você configurar
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = env("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
STRIPE_API_BASE = env("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")

# E-mail (recuperação de senha)
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "587") or 587)
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM", SMTP_USER)

# Planos
PRECO_PRO = "R$ 44,90"
TRIAL_DIAS = int(env("TRIAL_DIAS", "7") or 0)
LIMITE_GRATIS = int(env("LIMITE_GRATIS_LANCAMENTOS", "30") or 30)

ESTATICOS = {
    "/manifest.webmanifest": "application/manifest+json",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/icon-maskable.png": "image/png",
    "/apple-touch-icon.png": "image/png",
    "/sw.js": "application/javascript; charset=utf-8",
}

# ------- o que sincroniza (tudo do app, menos o que é só deste aparelho) -------
RX_CHAVE = re.compile(r"^(contas-da-casa|organiza|zenfi):[\w:.\-]{0,80}$")
RX_LOCAL = re.compile(
    r"^(organiza:(pin|pinfail|pinauto|pw|api|side|ui|due-hide|vz-recent)|zenfi:(eye|sync:.*))$")
RX_MES = re.compile(r"^contas-da-casa:(\d{4})-(\d{2})$")


def sincronizavel(k):
    return bool(RX_CHAVE.match(k)) and not RX_LOCAL.match(k)


def agora():
    return int(time.time())


# ---------------------------------------------------------------- banco
def banco():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(ARQ_BANCO, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def criar_tabelas():
    con = banco()
    with con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            documento TEXT UNIQUE, tipo_conta TEXT NOT NULL DEFAULT 'pf',
            senha_hash TEXT NOT NULL, criado_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessoes (
            token_hash TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            expira_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS erros_login (chave TEXT NOT NULL, quando INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_erros ON erros_login(chave, quando);
        CREATE TABLE IF NOT EXISTS whatsapp_codigos (
            usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id) ON DELETE CASCADE,
            telefone TEXT NOT NULL, codigo_hash TEXT NOT NULL,
            expira_em INTEGER NOT NULL, tentativas INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS whatsapp_vinculos (
            codigo_hash TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            expira_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS whatsapp_msgs (
            wamid TEXT PRIMARY KEY, usuario_id INTEGER, criado_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS whatsapp_avisos (
            telefone TEXT PRIMARY KEY, quando INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS whatsapp_lanc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            assinatura TEXT NOT NULL, criado_em INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_wl ON whatsapp_lanc(usuario_id, assinatura, criado_em);
        CREATE TABLE IF NOT EXISTS dados (
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            chave TEXT NOT NULL, valor TEXT, seq INTEGER NOT NULL,
            atualizado_em INTEGER NOT NULL, PRIMARY KEY (usuario_id, chave));
        CREATE INDEX IF NOT EXISTS ix_dados_seq ON dados(usuario_id, seq);
        CREATE TABLE IF NOT EXISTS itens_log (
            usuario_id INTEGER NOT NULL, chave TEXT NOT NULL, item_id TEXT NOT NULL,
            tipo TEXT NOT NULL, seq INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_itens ON itens_log(usuario_id, chave, seq);
        CREATE TABLE IF NOT EXISTS reset_senha (
            token_hash TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            expira_em INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS eventos_pagamento (id TEXT PRIMARY KEY, criado_em INTEGER NOT NULL);
        """)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(usuarios)").fetchall()}
        for nome, ddl in [
            ("whatsapp_telefone", "TEXT"), ("whatsapp_verificado_em", "INTEGER"),
            ("seq", "INTEGER NOT NULL DEFAULT 0"), ("plano", "TEXT NOT NULL DEFAULT 'gratis'"),
            ("plano_ate", "INTEGER"), ("trial_ate", "INTEGER"),
            ("stripe_cliente", "TEXT"), ("stripe_assinatura", "TEXT"),
        ]:
            if nome not in cols:
                con.execute("ALTER TABLE usuarios ADD COLUMN %s %s" % (nome, ddl))
        cols_l = {r["name"] for r in con.execute("PRAGMA table_info(whatsapp_lanc)").fetchall()}
        for nome, ddl in [("wamid", "TEXT"), ("chave", "TEXT"), ("item_id", "TEXT"), ("desfeito", "INTEGER NOT NULL DEFAULT 0")]:
            if nome not in cols_l:
                con.execute("ALTER TABLE whatsapp_lanc ADD COLUMN %s %s" % (nome, ddl))
        con.execute("DELETE FROM whatsapp_msgs WHERE criado_em < ?", (agora() - 30 * 86400,))
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_usuario_whatsapp "
                    "ON usuarios(whatsapp_telefone) WHERE whatsapp_telefone IS NOT NULL")
        con.execute("DELETE FROM sessoes WHERE expira_em < ?", (agora(),))
        con.execute("DELETE FROM erros_login WHERE quando < ?", (agora() - 3600,))
        con.execute("DELETE FROM reset_senha WHERE expira_em < ?", (agora(),))
        con.execute("DELETE FROM whatsapp_vinculos WHERE expira_em < ?", (agora(),))
        con.execute("DELETE FROM whatsapp_lanc WHERE criado_em < ?", (agora() - 86400,))
    con.close()
    try:
        os.chmod(ARQ_BANCO, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- senha / sessão
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


_FALSO = hash_senha("zenfi-falso")


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def criar_sessao(con, usuario_id):
    token = secrets.token_urlsafe(32)
    with con:
        con.execute("INSERT INTO sessoes (token_hash, usuario_id, expira_em) VALUES (?,?,?)",
                    (hash_token(token), usuario_id, agora() + DIAS_SESSAO * 86400))
    return token


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


class Erro(Exception):
    def __init__(self, status, msg, extra=None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


# ---------------------------------------------------------------- planos
def plano_efetivo(u):
    """Plano que vale AGORA para o usuário."""
    t = agora()
    pro_pago = u["plano"] == "pro" and (u["plano_ate"] is None or u["plano_ate"] > t)
    em_teste = bool(u["trial_ate"] and u["trial_ate"] > t)
    pro = pro_pago or em_teste
    return {
        "plano": "pro" if pro else "gratis",
        "teste": em_teste and not pro_pago,
        "ate": (u["plano_ate"] if pro_pago else (u["trial_ate"] if em_teste else None)),
        "preco": PRECO_PRO,
        "limite_lancamentos_mes": None if pro else LIMITE_GRATIS,
        "whatsapp": pro,
        "pagamento_configurado": bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID),
        "assinatura_ativa": bool(u["stripe_assinatura"]) and pro_pago,
    }


def usuario_json(u):
    return {
        "id": u["id"], "nome": u["nome"], "email": u["email"],
        "documento": u["documento"] or "", "tipo_conta": u["tipo_conta"],
        "plano": plano_efetivo(u),
    }


# ---------------------------------------------------------------- dados (nuvem)
def _ids(doc, lista):
    return {str(x.get("id")): x for x in (doc.get(lista) or []) if isinstance(x, dict) and x.get("id")}


def _parse(v):
    try:
        d = json.loads(v) if v else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def contar_lancamentos(valor):
    d = _parse(valor)
    return len(d.get("incomes") or []) + len(d.get("bills") or [])


def proxima_seq(con, uid):
    con.execute("UPDATE usuarios SET seq = seq + 1 WHERE id = ?", (uid,))
    return con.execute("SELECT seq FROM usuarios WHERE id = ?", (uid,)).fetchone()["seq"]


def gravar_chave(con, uid, chave, valor):
    """Grava (ou apaga, se valor=None) e registra no log quais itens entraram/saíram."""
    ant = con.execute("SELECT valor FROM dados WHERE usuario_id=? AND chave=?", (uid, chave)).fetchone()
    seq = proxima_seq(con, uid)
    if RX_MES.match(chave):
        antes, depois = _parse(ant["valor"] if ant else None), _parse(valor)
        for lista in ("incomes", "bills", "planned"):
            a, d = set(_ids(antes, lista)), set(_ids(depois, lista))
            for i in d - a:
                con.execute("INSERT INTO itens_log VALUES (?,?,?,?,?)", (uid, chave, i, "add", seq))
            for i in a - d:
                con.execute("INSERT INTO itens_log VALUES (?,?,?,?,?)", (uid, chave, i, "del", seq))
    con.execute("""INSERT INTO dados (usuario_id, chave, valor, seq, atualizado_em) VALUES (?,?,?,?,?)
                   ON CONFLICT(usuario_id, chave) DO UPDATE SET valor=excluded.valor,
                   seq=excluded.seq, atualizado_em=excluded.atualizado_em""",
                (uid, chave, valor, seq, agora()))
    return seq


def mesclar_mes(con, uid, chave, servidor, cliente, base):
    """Une as alterações de dois aparelhos sem perder lançamentos.
    Itens novos de outro aparelho/WhatsApp entram; itens apagados lá saem."""
    log = con.execute("SELECT item_id, tipo FROM itens_log WHERE usuario_id=? AND chave=? AND seq>?",
                      (uid, chave, base)).fetchall()
    novos = {r["item_id"] for r in log if r["tipo"] == "add"}
    apagados = {r["item_id"] for r in log if r["tipo"] == "del"}
    s, c = _parse(servidor), _parse(cliente)
    saida = dict(c)
    for lista in ("incomes", "bills", "planned"):
        itens_c = [x for x in (c.get(lista) or []) if isinstance(x, dict)]
        ids_c = {str(x.get("id")) for x in itens_c}
        final = [x for x in itens_c if str(x.get("id")) not in apagados]
        for iid, item in _ids(s, lista).items():
            if iid in novos and iid not in ids_c:
                final.append(item)
        saida[lista] = final
    return json.dumps(saida, ensure_ascii=False, separators=(",", ":"))


def aplicar_sync(con, u, itens):
    """Recebe alterações do aparelho. Retorna (aceitos, rejeitados, devolver)."""
    uid = u["id"]
    plano = plano_efetivo(u)
    aceitos, rejeitados, devolver = [], [], []
    with con:
        for it in itens:
            k = str(it.get("k", ""))
            v = it.get("v")
            try:
                base = int(it.get("bs") or 0)
            except (TypeError, ValueError):
                base = 0
            if not sincronizavel(k) or not (v is None or isinstance(v, str)) or (v and len(v) > 2_000_000):
                rejeitados.append({"k": k, "motivo": "invalido"})
                continue
            atual = con.execute("SELECT valor, seq FROM dados WHERE usuario_id=? AND chave=?", (uid, k)).fetchone()
            if v is not None and RX_MES.match(k):
                if atual and atual["valor"] is not None and atual["seq"] > base:
                    v = mesclar_mes(con, uid, k, atual["valor"], v, base)
                lim = plano["limite_lancamentos_mes"]
                if lim is not None:
                    antes = contar_lancamentos(atual["valor"]) if atual and atual["valor"] else 0
                    if contar_lancamentos(v) > max(lim, antes):
                        rejeitados.append({"k": k, "motivo": "limite"})
                        if atual:
                            devolver.append({"k": k, "v": atual["valor"], "s": atual["seq"]})
                        continue
            if atual and atual["valor"] == v:
                aceitos.append({"k": k, "s": atual["seq"]})
                continue
            seq = gravar_chave(con, uid, k, v)
            aceitos.append({"k": k, "s": seq})
            if RX_MES.match(k) and v is not None:
                devolver.append({"k": k, "v": v, "s": seq})  # devolve o resultado da mescla
    return aceitos, rejeitados, devolver


def ler_desde(con, uid, desde):
    rows = con.execute("SELECT chave, valor, seq FROM dados WHERE usuario_id=? AND seq>? ORDER BY seq LIMIT 500",
                       (uid, desde)).fetchall()
    return [{"k": r["chave"], "v": r["valor"], "s": r["seq"]} for r in rows]


# ---------------------------------------------------------------- WhatsApp
def canonico_telefone(valor):
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("00"):
        d = d[2:]
    if not d.startswith("55"):
        return ""
    if len(d) == 12 and d[4] in "6789":      # celular antigo, sem o 9
        d = d[:4] + "9" + d[4:]
    return d if len(d) in (12, 13) else ""


def _post_json(url, payload, headers, timeout=15):
    req = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def whatsapp_enviar_texto(telefone, texto):
    """Resposta de confirmação (dentro da janela de 24 h aberta pelo usuário)."""
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID):
        return False
    url = "%s/%s/%s/messages" % (WHATSAPP_API_BASE, WHATSAPP_API_VERSION, WHATSAPP_PHONE_NUMBER_ID)
    payload = {"messaging_product": "whatsapp", "to": telefone, "type": "text",
               "text": {"preview_url": False, "body": texto[:1500]}}
    try:
        _post_json(url, payload, {"Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"})
        return True
    except Exception as exc:
        print("[whatsapp] envio falhou:", type(exc).__name__)
        return False


_FILA_ENVIO = queue.Queue()
_FILA_LOCK = threading.Lock()
_FILA_THREAD = None


def _trabalhador_envio():
    while True:
        telefone, texto = _FILA_ENVIO.get()
        try:
            whatsapp_enviar_texto(telefone, texto)
        except Exception as exc:  # nunca derruba o trabalhador
            print("[whatsapp] erro no envio:", type(exc).__name__)


def responder(telefone, texto):
    """Enfileira a resposta: uma thread só envia, então as respostas saem na ordem em que as mensagens chegaram."""
    global _FILA_THREAD
    with _FILA_LOCK:
        if _FILA_THREAD is None or not _FILA_THREAD.is_alive():
            _FILA_THREAD = threading.Thread(target=_trabalhador_envio, daemon=True)
            _FILA_THREAD.start()
    _FILA_ENVIO.put((telefone, texto))


def enviar_template_verificacao(telefone, codigo):
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_VERIFY_TEMPLATE):
        raise RuntimeError("Envio de código por template não configurado.")
    url = "%s/%s/%s/messages" % (WHATSAPP_API_BASE, WHATSAPP_API_VERSION, WHATSAPP_PHONE_NUMBER_ID)
    payload = {"messaging_product": "whatsapp", "to": telefone, "type": "template",
               "template": {"name": WHATSAPP_VERIFY_TEMPLATE, "language": {"code": "pt_BR"},
                            "components": [{"type": "body", "parameters": [{"type": "text", "text": codigo}]}]}}
    try:
        return _post_json(url, payload, {"Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"})
    except Exception as exc:
        raise RuntimeError("A Meta não aceitou o envio. Confira o template aprovado e as credenciais.") from exc


def brl(c):
    s = "{:,.2f}".format(abs(c) / 100).replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if c < 0 else "") + "R$ " + s


def _novo_id():
    n = int(time.time() * 1000)
    base36 = ""
    while n:
        n, r = divmod(n, 36)
        base36 = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + base36
    return base36 + secrets.token_hex(3)[:5]


def _chave_mes(ano, mes):
    return "contas-da-casa:%04d-%02d" % (ano, mes)


def _ler_mes(con, uid, chave):
    row = con.execute("SELECT valor FROM dados WHERE usuario_id=? AND chave=?", (uid, chave)).fetchone()
    doc = _parse(row["valor"] if row else None)
    for k in ("incomes", "bills", "planned"):
        if not isinstance(doc.get(k), list):
            doc[k] = []
    return doc


def _gravar_mes(con, uid, chave, doc):
    gravar_chave(con, uid, chave, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def _totais(doc):
    rec = sum(int(x.get("amount") or 0) for x in doc["incomes"])
    desp = sum(int(x.get("amount") or 0) for x in doc["bills"])
    return rec, desp


def _mes_pedido(extras):
    """(ano, mes) do pedido: mês atual ou, com 'mês passado', o anterior."""
    h = time.localtime()
    ano, mes = h.tm_year, h.tm_mon
    if (extras or {}).get("mes") == "passado":
        mes -= 1
        if mes == 0:
            ano, mes = ano - 1, 12
    return ano, mes


def lancar_varios(con, u, itens, wamid):
    """Grava os lançamentos do WhatsApp direto nos dados do usuário (nuvem), respeitando o limite
    do plano. Retorna (gravados, recusados_por_limite, limite)."""
    plano = plano_efetivo(u)
    lim = plano["limite_lancamentos_mes"]
    gravados, recusados = [], []
    with con:
        docs = {}
        for p in itens:
            chave = _chave_mes(p["data"].year, p["data"].month)
            doc = docs.get(chave) or _ler_mes(con, u["id"], chave)
            docs[chave] = doc
            if lim is not None and len(doc["incomes"]) + len(doc["bills"]) >= lim:
                recusados.append(p)
                continue
            item = {"id": _novo_id(), "desc": p["descricao"], "amount": p["centavos"], "src": "whatsapp"}
            if p["tipo"] == "receita":
                doc["incomes"].append(item)
            else:
                item["cat"] = p["categoria"] or "Outros"
                item["day"] = p["data"].day
                doc["bills"].append(item)
            gravados.append((p, chave, item["id"]))
        for chave, doc in docs.items():
            if any(g[1] == chave for g in gravados):
                _gravar_mes(con, u["id"], chave, doc)
        for p, chave, item_id in gravados:
            con.execute("INSERT INTO whatsapp_lanc (usuario_id, assinatura, criado_em, wamid, chave, item_id) VALUES (?,?,?,?,?,?)",
                        (u["id"], _assinatura(p), agora(), wamid, chave, item_id))
    return gravados, recusados, lim


def _assinatura(p):
    return hashlib.sha256(("%s|%d|%s|%s" % (p["tipo"], p["centavos"], zenfi_parser.normalizar(p["descricao"]), p["data"])).encode()).hexdigest()


def _linha(p):
    d = p["data"].strftime("%d/%m")
    if p["tipo"] == "receita":
        return "• 💰 %s — %s (%s)" % (p["descricao"], brl(p["centavos"]), d)
    return "• %s — %s · %s (%s)" % (p["descricao"], brl(p["centavos"]), p["categoria"] or "Outros", d)


def _texto_lancamentos(con, u, gravados, repetidos, recusados, lim):
    partes = []
    if len(gravados) == 1 and not repetidos and not recusados:
        p = gravados[0][0]
        if p["tipo"] == "receita":
            cab = "✅ Receita registrada\n%s — %s\n%s" % (p["descricao"], brl(p["centavos"]), p["data"].strftime("%d/%m/%Y"))
        else:
            cab = "✅ Despesa registrada\n%s — %s\nCategoria: %s · %s" % (
                p["descricao"], brl(p["centavos"]), p["categoria"] or "Outros", p["data"].strftime("%d/%m/%Y"))
        partes.append(cab)
    elif gravados:
        partes.append("✅ %d lançamentos registrados\n%s" % (len(gravados), "\n".join(_linha(g[0]) for g in gravados)))
    if repetidos:
        partes.append("⚠️ Não lancei de novo (igual a um lançamento dos últimos 2 minutos):\n%s\nSe foi intencional, escreva \"de novo\" na mensagem." % "\n".join(_linha(p) for p in repetidos))
    if recusados:
        partes.append("🚫 Limite de %s lançamentos/mês do plano gratuito atingido; não lancei:\n%s\nAssine o Pro (%s/mês) no app para lançar sem limite." % (lim, "\n".join(_linha(p) for p in recusados), PRECO_PRO))
    if gravados:
        ano, mes = gravados[0][0]["data"].year, gravados[0][0]["data"].month
        rec, desp = _totais(_ler_mes(con, u["id"], _chave_mes(ano, mes)))
        partes.append("Saldo de %02d/%d: %s" % (mes, ano, brl(rec - desp)))
    return "\n\n".join(partes)


AJUDA = ("Eu lanço no ZenFi o que você me escrever 👇\n\n"
         "💸 Gastei R$ 35 no almoço\n"
         "💰 Recebi R$ 2.500 de salário\n"
         "🗓️ Paguei 120 de gasolina ontem · dia 5 · 28/10\n"
         "🧾 Vários de uma vez: gastei 30 no lanche e 20 no uber\n"
         "⚡ Atalho: almoço 35 · uber 18,90 · +500 pix\n\n"
         "Comandos:\n"
         "• saldo — quanto sobrou no mês\n"
         "• resumo — receitas, despesas e onde gastou (\"resumo do mês passado\")\n"
         "• extrato — últimos lançamentos feitos por aqui\n"
         "• desfazer — apaga o último lançamento feito por aqui")


def _resumo(con, u, extras, completo):
    ano, mes = _mes_pedido(extras)
    doc = _ler_mes(con, u["id"], _chave_mes(ano, mes))
    rec, desp = _totais(doc)
    cab = "%s de %02d/%d\n💰 Receitas: %s\n💸 Despesas: %s\n📊 Saldo: %s" % (
        "Resumo" if completo else "Saldo", mes, ano, brl(rec), brl(desp), brl(rec - desp))
    if not (doc["incomes"] or doc["bills"]):
        return "Ainda não há lançamentos em %02d/%d." % (mes, ano)
    if not completo or not doc["bills"]:
        return cab
    por = {}
    for b in doc["bills"]:
        por[b.get("cat") or "Outros"] = por.get(b.get("cat") or "Outros", 0) + int(b.get("amount") or 0)
    top = sorted(por.items(), key=lambda kv: -kv[1])[:5]
    linhas = ["• %s: %s (%d%%)" % (c, brl(v), round(100 * v / desp) if desp else 0) for c, v in top]
    return cab + "\n\nOnde mais gastou:\n" + "\n".join(linhas)


def _extrato(con, u):
    ano, mes = _mes_pedido({})
    doc = _ler_mes(con, u["id"], _chave_mes(ano, mes))
    itens = [("receita", x) for x in doc["incomes"] if x.get("src") == "whatsapp"] + \
            [("despesa", x) for x in doc["bills"] if x.get("src") == "whatsapp"]
    itens.sort(key=lambda t: str(t[1].get("id", "")), reverse=True)
    if not itens:
        return "Você ainda não lançou nada por aqui em %02d/%d. Experimente: Gastei R$ 35 no almoço." % (mes, ano)
    linhas = []
    for tipo, x in itens[:5]:
        linhas.append("• %s %s — %s" % ("💰" if tipo == "receita" else "💸", x.get("desc", ""), brl(int(x.get("amount") or 0))))
    return "Últimos lançamentos feitos por aqui (%02d/%d):\n%s" % (mes, ano, "\n".join(linhas))


def _desfazer(con, u):
    ult = con.execute("SELECT wamid FROM whatsapp_lanc WHERE usuario_id=? AND desfeito=0 ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    if not ult:
        return "Não encontrei lançamento recente feito por aqui para desfazer (só consigo desfazer os das últimas 24 horas)."
    linhas = con.execute("SELECT id, chave, item_id FROM whatsapp_lanc WHERE usuario_id=? AND wamid=? AND desfeito=0",
                         (u["id"], ult["wamid"])).fetchall()
    removidos = []
    with con:
        for chave in {r["chave"] for r in linhas}:
            doc = _ler_mes(con, u["id"], chave)
            ids = {r["item_id"] for r in linhas if r["chave"] == chave}
            for lista in ("incomes", "bills"):
                for x in [x for x in doc[lista] if x.get("id") in ids]:
                    removidos.append(x)
                doc[lista] = [x for x in doc[lista] if x.get("id") not in ids]
            _gravar_mes(con, u["id"], chave, doc)
        con.execute("UPDATE whatsapp_lanc SET desfeito=1 WHERE usuario_id=? AND wamid=?", (u["id"], ult["wamid"]))
    if not removidos:
        return "Esse lançamento já não está mais no app (você deve ter apagado por lá). Não mexi em nada."
    return "↩️ Desfeito:\n%s" % "\n".join("• %s — %s" % (x.get("desc", ""), brl(int(x.get("amount") or 0))) for x in removidos)


def _texto_da_mensagem(msg):
    """Texto de uma mensagem da Meta (texto, botão ou resposta de lista) ou None se não for texto."""
    tipo = msg.get("type")
    if tipo == "text":
        return (msg.get("text") or {}).get("body", "")
    if tipo == "button":
        return (msg.get("button") or {}).get("text", "")
    if tipo == "interactive":
        i = msg.get("interactive") or {}
        return ((i.get("button_reply") or i.get("list_reply") or {}).get("title", ""))
    return None


def _falha_texto(motivo):
    base = "Não consegui entender 🤔\n"
    exemplos = "\nPor exemplo:\n• Gastei R$ 35 no almoço\n• Paguei R$ 120 de gasolina\n• Recebi R$ 2.500 de salário\nDigite *ajuda* para ver tudo."
    return base + {
        "sem_valor": "Não achei o valor. Escreva quanto foi, junto com o que foi." + exemplos,
        "sem_tipo": "Não sei se foi um gasto ou uma receita. Comece com \"gastei\", \"paguei\" ou \"recebi\"." + exemplos,
        "moeda": "Por enquanto só lanço valores em reais (R$). Converta e me mande de novo.",
        "parcelado": "Vi uma compra parcelada. Me diga só o valor que cai neste mês, por exemplo: \"Gastei 50 na parcela do cartão\".",
        "valor_alto": "Esse valor passou do limite de R$ 10 milhões por lançamento. Confira se digitou certo.",
        "muitos": "São lançamentos demais numa mensagem (máximo 8). Mande em partes.",
        "longa": "A mensagem ficou muito longa. Mande curtinha, como \"Gastei R$ 35 no almoço\".",
    }.get(motivo, "Escreva o valor e o que foi." + exemplos)


def tratar_mensagem_whatsapp(con, msg):
    """Uma mensagem recebida da Meta. Nunca levanta exceção para o webhook."""
    wamid = str(msg.get("id", ""))
    de = canonico_telefone(msg.get("from", ""))
    if not wamid or not de:
        return
    try:
        with con:
            con.execute("INSERT INTO whatsapp_msgs (wamid, criado_em) VALUES (?,?)", (wamid, agora()))
    except sqlite3.IntegrityError:
        return  # a Meta reenviou o mesmo webhook: já tratado (evita duplicar)
    try:
        _tratar(con, msg, wamid, de)
    except Exception as exc:  # o usuário não pode ficar sem resposta
        print("[whatsapp] erro ao tratar mensagem:", type(exc).__name__, exc)
        try:
            with con:
                con.execute("DELETE FROM whatsapp_msgs WHERE wamid=?", (wamid,))
        except sqlite3.Error:
            pass
        responder(de, "Tive um problema aqui e não consegui registrar 😕 Tente de novo em instantes.")


def _tratar(con, msg, wamid, de):
    tipo_msg = msg.get("type")
    if tipo_msg in ("reaction", "system", "request_welcome", "unsupported", "ephemeral"):
        return  # reação (👍), avisos do sistema: não merecem resposta
    texto = _texto_da_mensagem(msg)
    u = con.execute("SELECT * FROM usuarios WHERE whatsapp_telefone=? AND whatsapp_verificado_em IS NOT NULL", (de,)).fetchone()

    # 1) código de vínculo enviado pelo próprio WhatsApp do usuário
    if texto:
        m = re.search(r"zen[\s-]*(\d{6})", texto, re.I)
        if m:
            h = hash_token("vinculo:" + m.group(1))
            lin = con.execute("SELECT usuario_id FROM whatsapp_vinculos WHERE codigo_hash=? AND expira_em>?", (h, agora())).fetchone()
            if not lin:
                return responder(de, "Esse código expirou ou não existe. Gere um novo em ZenFi > Configurações > WhatsApp.")
            dono = con.execute("SELECT id FROM usuarios WHERE whatsapp_telefone=? AND id<>?", (de, lin["usuario_id"])).fetchone()
            if dono:
                return responder(de, "Este número já está vinculado a outra conta ZenFi.")
            with con:
                con.execute("UPDATE usuarios SET whatsapp_telefone=?, whatsapp_verificado_em=? WHERE id=?", (de, agora(), lin["usuario_id"]))
                con.execute("DELETE FROM whatsapp_vinculos WHERE usuario_id=?", (lin["usuario_id"],))
            return responder(de, "✅ WhatsApp vinculado ao seu ZenFi!\nAgora é só me mandar, por exemplo:\n• Gastei R$ 35 no almoço\n• Recebi R$ 2.500 de salário\n\nDigite *ajuda* para ver tudo que eu faço.")

    if not u:
        with con:
            ult = con.execute("SELECT quando FROM whatsapp_avisos WHERE telefone=?", (de,)).fetchone()
            if not ult or ult["quando"] < agora() - 3600:
                con.execute("INSERT OR REPLACE INTO whatsapp_avisos VALUES (?,?)", (de, agora()))
                return responder(de, "Olá! Este número ainda não está vinculado a uma conta ZenFi. Abra o app, vá em Configurações > WhatsApp e siga o passo a passo.")
        return

    if texto is None:
        return responder(de, "Por enquanto eu só entendo mensagens de texto, como: Gastei R$ 35 no almoço. (Áudio, foto e comprovante ainda não.)")
    if not plano_efetivo(u)["whatsapp"]:
        return responder(de, "O lançamento por WhatsApp faz parte do plano Pro (%s/mês). Assine pelo app em Configurações > Seu plano." % PRECO_PRO)

    # 2) comandos (saldo, resumo, extrato, desfazer, ajuda, oi...)
    cmd = zenfi_parser.comando(texto)
    if cmd:
        nome, extras = cmd
        if nome == "ciente":
            return
        if nome == "agradecimento":
            return responder(de, "Por nada! 😊")
        if nome == "saudacao":
            return responder(de, "Olá, %s! 👋\nMe diga o que gastou ou recebeu, por exemplo:\n• Gastei R$ 35 no almoço\n• Recebi R$ 2.500 de salário\n\nDigite *ajuda* para ver os comandos." % (u["nome"] or "").split(" ")[0])
        if nome == "ajuda":
            return responder(de, AJUDA)
        if nome == "saldo":
            return responder(de, _resumo(con, u, extras, False))
        if nome == "resumo":
            return responder(de, _resumo(con, u, extras, True))
        if nome == "extrato":
            return responder(de, _extrato(con, u))
        if nome == "desfazer":
            return responder(de, _desfazer(con, u))

    # 3) lançamentos (um ou vários na mesma mensagem)
    itens = zenfi_parser.interpretar_varios(texto)
    if not itens:
        return responder(de, _falha_texto(zenfi_parser.motivo_falha(texto)))
    novos, repetidos = [], []
    for p in itens:
        rep = None
        if not p.get("forcar"):   # duplicidade: mesma coisa, mesmo valor, nos últimos 2 minutos
            rep = con.execute("SELECT 1 FROM whatsapp_lanc WHERE usuario_id=? AND assinatura=? AND criado_em>? AND desfeito=0",
                              (u["id"], _assinatura(p), agora() - 120)).fetchone()
        if rep or any(_assinatura(p) == _assinatura(n) and not p.get("forcar") for n in novos):
            repetidos.append(p)
        else:
            novos.append(p)
    gravados, recusados, lim = lancar_varios(con, u, novos, wamid) if novos else ([], [], None)
    if gravados:
        with con:
            con.execute("UPDATE whatsapp_msgs SET usuario_id=? WHERE wamid=?", (u["id"], wamid))
    responder(de, _texto_lancamentos(con, u, gravados, repetidos, recusados, lim))


# ---------------------------------------------------------------- Stripe
def stripe_post(caminho, campos):
    req = Request(STRIPE_API_BASE + caminho, data=urlencode(campos).encode(),
                  headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY,
                           "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stripe_assinatura_valida(raw, cabecalho):
    try:
        partes = dict(p.split("=", 1) for p in cabecalho.split(","))
        t, v1 = partes["t"], partes["v1"]
        esperado = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), (t + ".").encode() + raw, hashlib.sha256).hexdigest()
        return abs(agora() - int(t)) < 600 and hmac.compare_digest(esperado, v1)
    except Exception:
        return False


def tratar_evento_stripe(con, ev):
    eid, tipo, obj = ev.get("id", ""), ev.get("type", ""), (ev.get("data") or {}).get("object") or {}
    try:
        with con:
            con.execute("INSERT INTO eventos_pagamento VALUES (?,?)", (eid, agora()))
    except sqlite3.IntegrityError:
        return
    with con:
        if tipo == "checkout.session.completed" and str(obj.get("client_reference_id", "")).isdigit():
            con.execute("UPDATE usuarios SET plano='pro', plano_ate=?, stripe_cliente=?, stripe_assinatura=? WHERE id=?",
                        (agora() + 35 * 86400, obj.get("customer"), obj.get("subscription"), int(obj["client_reference_id"])))
        elif tipo in ("customer.subscription.created", "customer.subscription.updated"):
            fim = int(obj.get("current_period_end") or 0) + 3 * 86400  # 3 dias de tolerância
            if obj.get("status") in ("active", "trialing", "past_due"):
                con.execute("UPDATE usuarios SET plano='pro', plano_ate=?, stripe_assinatura=? WHERE stripe_cliente=?",
                            (fim, obj.get("id"), obj.get("customer")))
            else:
                con.execute("UPDATE usuarios SET plano='gratis', plano_ate=NULL WHERE stripe_cliente=?", (obj.get("customer"),))
        elif tipo == "customer.subscription.deleted":
            con.execute("UPDATE usuarios SET plano='gratis', plano_ate=NULL, stripe_assinatura=NULL WHERE stripe_cliente=?",
                        (obj.get("customer"),))


# ---------------------------------------------------------------- e-mail
def enviar_email(para, assunto, texto):
    if not SMTP_HOST:
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, para, assunto
    msg.set_content(texto)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as exc:
        print("[email] envio falhou:", type(exc).__name__)
        return False


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ZenFi"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- utilidades
    def _https(self):
        return (TRUST_PROXY and self.headers.get("X-Forwarded-Proto", "") == "https") or BASE_URL.startswith("https://")

    def _ip(self):
        if TRUST_PROXY:
            xf = self.headers.get("X-Forwarded-For", "")
            if xf:
                return xf.split(",")[0].strip()
        return self.client_address[0]

    def _enviar(self, status, corpo, tipo, extra=None, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")   # microfone só para o próprio app
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _json(self, status, dados, extra=None):
        self._enviar(status, json.dumps(dados, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _token(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return m.value if m else None

    def _set_cookie(self, token, max_age):
        flags = "; Secure" if self._https() else ""
        return [("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (COOKIE, token, max_age, flags))]

    def _ler_json(self, limite=MAX_CORPO):
        origem = self.headers.get("Origin")
        if origem and urlsplit(origem).netloc != self.headers.get("Host", ""):
            raise Erro(403, "Origem não permitida.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise Erro(415, "Formato inválido.")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > limite:
            raise Erro(413, "Pedido grande demais.")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Erro(400, "Pedido inválido.")
        if not isinstance(d, dict):
            raise Erro(400, "Pedido inválido.")
        return d

    def _usuario(self, con, obrigatorio=True):
        token = self._token()
        u = None
        if token:
            u = con.execute("SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
                            "WHERE s.token_hash = ? AND s.expira_em > ?", (hash_token(token), agora())).fetchone()
        if not u and obrigatorio:
            raise Erro(401, "Entre na sua conta.")
        return u

    def _limite_taxa(self, con, chave, maximo, janela):
        n = con.execute("SELECT COUNT(*) FROM erros_login WHERE chave=? AND quando>?", (chave, agora() - janela)).fetchone()[0]
        if n >= maximo:
            raise Erro(429, "Muitas tentativas. Espere alguns minutos e tente de novo.")
        with con:
            con.execute("INSERT INTO erros_login (chave, quando) VALUES (?,?)", (chave, agora()))

    # ---- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlsplit(self.path)
        caminho = url.path
        try:
            if caminho == "/healthz":
                return self._json(200, {"ok": True, "backup": zenfi_backup.resumo()})
            if caminho == "/webhook/whatsapp":
                q = dict(parse_qsl(url.query))
                if (q.get("hub.mode") == "subscribe" and META_VERIFY_TOKEN
                        and hmac.compare_digest(q.get("hub.verify_token", ""), META_VERIFY_TOKEN)):
                    return self._enviar(200, q.get("hub.challenge", "").encode(), "text/plain; charset=utf-8")
                return self._enviar(403, b"Forbidden", "text/plain; charset=utf-8")
            if caminho in ("/", "/index.html", "/zenfi.html", "/organiza.html"):
                with open(ARQ_HTML, "rb") as f:
                    return self._enviar(200, f.read(), "text/html; charset=utf-8")
            if caminho in ESTATICOS:
                arq = os.path.join(PASTA, os.path.basename(caminho))
                if os.path.isfile(arq):
                    with open(arq, "rb") as f:
                        return self._enviar(200, f.read(), ESTATICOS[caminho],
                                            extra=[("Service-Worker-Allowed", "/")] if caminho == "/sw.js" else None,
                                            cache="no-cache" if caminho == "/sw.js" else "public, max-age=3600")
            if caminho.startswith("/api/"):
                con = banco()
                try:
                    return self._get_api(con, caminho, dict(parse_qsl(url.query)))
                finally:
                    con.close()
            self._json(404, {"erro": "Não encontrado."})
        except Erro as e:
            self._json(e.status, dict(e.extra, erro=e.msg))
        except FileNotFoundError:
            self._json(500, {"erro": "Arquivo zenfi_whatsapp.html não está na mesma pasta do servidor."})
        except Exception as exc:
            print("[erro]", type(exc).__name__, exc)
            self._json(500, {"erro": "Erro interno."})

    def _get_api(self, con, caminho, q):
        if caminho == "/api/eu":
            return self._json(200, usuario_json(self._usuario(con)))
        if caminho == "/api/sync":
            u = self._usuario(con)
            try:
                desde = int(q.get("desde", "0") or 0)
            except ValueError:
                desde = 0
            itens = ler_desde(con, u["id"], desde)
            return self._json(200, {"uid": u["id"], "seq": u["seq"], "itens": itens,
                                    "mais": len(itens) >= 500, "plano": plano_efetivo(u)})
        if caminho == "/api/whatsapp/vinculo":
            u = self._usuario(con)
            return self._json(200, {
                "vinculado": bool(u["whatsapp_telefone"] and u["whatsapp_verificado_em"]),
                "telefone": ("+" + u["whatsapp_telefone"]) if u["whatsapp_telefone"] else "",
                "numero_oficial": WHATSAPP_NUMERO_PUBLICO,
                "configurado": bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and META_APP_SECRET),
                "template": bool(WHATSAPP_VERIFY_TEMPLATE),
                "plano": plano_efetivo(u)})
        raise Erro(404, "Não encontrado.")

    # ---- POST
    def do_POST(self):
        caminho = urlsplit(self.path).path
        try:
            if caminho == "/webhook/whatsapp":
                return self._webhook_whatsapp()
            if caminho == "/webhook/stripe":
                return self._webhook_stripe()
            dados = self._ler_json(MAX_CORPO_SYNC if caminho == "/api/sync" else MAX_CORPO)
            con = banco()
            try:
                return self._post_api(con, caminho, dados)
            finally:
                con.close()
        except Erro as e:
            self._json(e.status, dict(e.extra, erro=e.msg))
        except Exception as exc:
            print("[erro]", type(exc).__name__, exc)
            self._json(500, {"erro": "Erro interno."})

    def _post_api(self, con, caminho, d):
        if caminho == "/api/cadastro":
            return self._cadastro(con, d)
        if caminho == "/api/entrar":
            return self._entrar(con, d)
        if caminho == "/api/sair":
            return self._sair(con)
        if caminho == "/api/senha/esqueci":
            return self._senha_esqueci(con, d)
        if caminho == "/api/senha/redefinir":
            return self._senha_redefinir(con, d)
        u = self._usuario(con)
        if caminho == "/api/sync":
            itens = d.get("itens")
            if not isinstance(itens, list) or len(itens) > 200:
                raise Erro(400, "Pedido inválido.")
            aceitos, rejeitados, devolver = aplicar_sync(con, u, [i for i in itens if isinstance(i, dict)])
            u = con.execute("SELECT * FROM usuarios WHERE id=?", (u["id"],)).fetchone()
            return self._json(200, {"seq": u["seq"], "aceitos": aceitos, "rejeitados": rejeitados,
                                    "devolver": devolver, "plano": plano_efetivo(u)})
        if caminho == "/api/whatsapp/vinculo/codigo":
            return self._wa_codigo(con, u)
        if caminho == "/api/whatsapp/desvincular":
            with con:
                con.execute("UPDATE usuarios SET whatsapp_telefone=NULL, whatsapp_verificado_em=NULL WHERE id=?", (u["id"],))
            return self._json(200, {"ok": True})
        if caminho == "/api/whatsapp/vinculo/iniciar":
            return self._wa_iniciar(con, u, d)
        if caminho == "/api/whatsapp/vinculo/confirmar":
            return self._wa_confirmar(con, u, d)
        if caminho == "/api/assinatura/checkout":
            return self._checkout(u)
        if caminho == "/api/assinatura/portal":
            return self._portal(u)
        raise Erro(404, "Não encontrado.")

    # ---- conta
    def _cadastro(self, con, d):
        self._limite_taxa(con, "cad|" + self._ip(), 10, 3600)
        nome = re.sub(r"\s+", " ", str(d.get("nome", "")).strip())
        email = str(d.get("email", "")).strip().lower()
        senha = str(d.get("senha", ""))
        doc = re.sub(r"\D", "", str(d.get("documento", "")))
        tipo = "pf"
        if not 2 <= len(nome) <= 30:
            raise Erro(400, "Digite seu nome (de 2 a 30 letras).")
        if len(email) > 80 or not RX_EMAIL.match(email):
            raise Erro(400, "Digite um e-mail válido, como voce@exemplo.com.")
        if doc:  # CPF/CNPJ é opcional; se vier, precisa ser válido
            if len(doc) == 11 and cpf_valido(doc):
                tipo = "pf"
            elif len(doc) == 14 and cnpj_valido(doc):
                tipo = "pj"
            else:
                raise Erro(400, "CPF/CNPJ inválido. Confira os números ou deixe em branco.")
        if not 6 <= len(senha) <= 64:
            raise Erro(400, "A senha precisa ter de 6 a 64 caracteres.")
        if con.execute("SELECT 1 FROM usuarios WHERE email = ?", (email,)).fetchone():
            raise Erro(409, "Este e-mail já está cadastrado. Use Entrar.")
        if doc and con.execute("SELECT 1 FROM usuarios WHERE documento = ?", (doc,)).fetchone():
            raise Erro(409, "Este CPF/CNPJ já está cadastrado. Use Entrar.")
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO usuarios (nome, email, documento, tipo_conta, senha_hash, criado_em, trial_ate) VALUES (?,?,?,?,?,?,?)",
                    (nome, email, doc or None, tipo, hash_senha(senha), agora(), agora() + TRIAL_DIAS * 86400 if TRIAL_DIAS else None))
        except sqlite3.IntegrityError:
            raise Erro(409, "Esta conta já está cadastrada. Use Entrar.")
        token = criar_sessao(con, cur.lastrowid)
        u = con.execute("SELECT * FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
        self._json(200, usuario_json(u), self._set_cookie(token, DIAS_SESSAO * 86400))

    def _entrar(self, con, d):
        ident = str(d.get("identificador", "")).strip()
        senha = str(d.get("senha", ""))
        if not ident or not senha or len(ident) > 80 or len(senha) > 64:
            raise Erro(400, "Digite seu e-mail ou CPF/CNPJ e a senha.")
        campo, valor = ("email", ident.lower()) if "@" in ident else ("documento", re.sub(r"\D", "", ident))
        chave = "%s|%s" % (self._ip(), valor)
        n = con.execute("SELECT COUNT(*) FROM erros_login WHERE chave = ? AND quando > ?", (chave, agora() - JANELA_ERROS)).fetchone()[0]
        if n >= MAX_ERROS:
            raise Erro(429, "Muitas tentativas erradas. Espere 10 minutos e tente de novo.")
        u = con.execute("SELECT * FROM usuarios WHERE %s = ?" % campo, (valor,)).fetchone() if valor else None
        ok = confere_senha(senha, u["senha_hash"] if u else _FALSO) and u is not None
        if not ok:
            with con:
                con.execute("INSERT INTO erros_login (chave, quando) VALUES (?,?)", (chave, agora()))
            raise Erro(401, "E-mail/CPF ou senha incorretos.")
        with con:
            con.execute("DELETE FROM erros_login WHERE chave = ?", (chave,))
        token = criar_sessao(con, u["id"])
        self._json(200, usuario_json(u), self._set_cookie(token, DIAS_SESSAO * 86400))

    def _sair(self, con):
        token = self._token()
        if token:
            with con:
                con.execute("DELETE FROM sessoes WHERE token_hash = ?", (hash_token(token),))
        self._json(200, {"ok": True}, self._set_cookie("", 0))

    def _senha_esqueci(self, con, d):
        self._limite_taxa(con, "esq|" + self._ip(), 5, 3600)
        email = str(d.get("email", "")).strip().lower()
        resposta = {"ok": True, "mensagem": "Se este e-mail tiver cadastro, enviamos um link para criar uma nova senha. Ele vale por 1 hora."}
        u = con.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone() if RX_EMAIL.match(email) else None
        if not u:
            return self._json(200, resposta)   # mesma resposta: não revela quem tem conta
        token = secrets.token_urlsafe(32)
        with con:
            con.execute("DELETE FROM reset_senha WHERE usuario_id=?", (u["id"],))
            con.execute("INSERT INTO reset_senha VALUES (?,?,?)", (hash_token(token), u["id"], agora() + 3600))
        base = BASE_URL or ("http://" + self.headers.get("Host", "localhost"))
        link = "%s/#redefinir=%s" % (base, token)
        corpo = "Olá, %s!\n\nPara criar uma nova senha no ZenFi, abra o link abaixo (vale por 1 hora):\n%s\n\nSe não foi você, ignore este e-mail." % (u["nome"], link)
        if not enviar_email(u["email"], "ZenFi - nova senha", corpo):
            print("[senha] SMTP não configurado ou falhou: e-mail de redefinição NÃO enviado.")
        if DEV:
            resposta["dev_link"] = link          # nunca aparece em produção
        self._json(200, resposta)

    def _senha_redefinir(self, con, d):
        self._limite_taxa(con, "red|" + self._ip(), 10, 3600)
        token, senha = str(d.get("token", "")), str(d.get("senha", ""))
        if not 6 <= len(senha) <= 64:
            raise Erro(400, "A senha precisa ter de 6 a 64 caracteres.")
        r = con.execute("SELECT usuario_id FROM reset_senha WHERE token_hash=? AND expira_em>?", (hash_token(token), agora())).fetchone()
        if not r:
            raise Erro(400, "Link inválido ou expirado. Peça um novo.")
        with con:
            con.execute("UPDATE usuarios SET senha_hash=? WHERE id=?", (hash_senha(senha), r["usuario_id"]))
            con.execute("DELETE FROM reset_senha WHERE usuario_id=?", (r["usuario_id"],))
            con.execute("DELETE FROM sessoes WHERE usuario_id=?", (r["usuario_id"],))  # derruba todos os aparelhos
        self._json(200, {"ok": True, "mensagem": "Senha alterada. Entre com a nova senha."})

    # ---- WhatsApp
    def _wa_codigo(self, con, u):
        if not plano_efetivo(u)["whatsapp"]:
            raise Erro(402, "O lançamento por WhatsApp faz parte do plano Pro (%s/mês)." % PRECO_PRO, {"upgrade": True})
        for _ in range(5):
            codigo = "%06d" % secrets.randbelow(1000000)
            h = hash_token("vinculo:" + codigo)
            if not con.execute("SELECT 1 FROM whatsapp_vinculos WHERE codigo_hash=?", (h,)).fetchone():
                break
        with con:
            con.execute("DELETE FROM whatsapp_vinculos WHERE usuario_id=?", (u["id"],))
            con.execute("INSERT INTO whatsapp_vinculos VALUES (?,?,?)", (h, u["id"], agora() + 900))
        texto = "ZEN-" + codigo
        link = ("https://wa.me/%s?text=%s" % (WHATSAPP_NUMERO_PUBLICO, texto)) if WHATSAPP_NUMERO_PUBLICO else ""
        self._json(200, {"codigo": texto, "link": link, "numero_oficial": WHATSAPP_NUMERO_PUBLICO, "expira_em_min": 15})

    def _wa_iniciar(self, con, u, d):
        tel = canonico_telefone(d.get("telefone"))
        if not tel:
            raise Erro(400, "Informe o telefone com DDI. Exemplo: +55 11 99999-9999.")
        if not plano_efetivo(u)["whatsapp"]:
            raise Erro(402, "O lançamento por WhatsApp faz parte do plano Pro.", {"upgrade": True})
        if con.execute("SELECT id FROM usuarios WHERE whatsapp_telefone=? AND id<>?", (tel, u["id"])).fetchone():
            raise Erro(409, "Este número já está vinculado a outra conta ZenFi.")
        self._limite_taxa(con, "wai|%d" % u["id"], 5, 3600)
        codigo = "%06d" % secrets.randbelow(1000000)
        with con:
            con.execute("""INSERT INTO whatsapp_codigos VALUES (?,?,?,?,0) ON CONFLICT(usuario_id) DO UPDATE SET
                telefone=excluded.telefone, codigo_hash=excluded.codigo_hash, expira_em=excluded.expira_em, tentativas=0""",
                        (u["id"], tel, hash_token("%d:%s" % (u["id"], codigo)), agora() + 600))
        try:
            enviar_template_verificacao(tel, codigo)
        except RuntimeError as exc:
            with con:
                con.execute("DELETE FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],))
            raise Erro(503, str(exc))
        self._json(200, {"ok": True, "mensagem": "Código enviado. Ele expira em 10 minutos."})

    def _wa_confirmar(self, con, u, d):
        tel = canonico_telefone(d.get("telefone"))
        codigo = re.sub(r"\D", "", str(d.get("codigo", "")))
        row = con.execute("SELECT * FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],)).fetchone()
        if not tel or len(codigo) != 6 or not row or row["telefone"] != tel or row["expira_em"] < agora() or row["tentativas"] >= 5:
            raise Erro(400, "Código expirado ou inválido. Solicite outro.")
        if not hmac.compare_digest(hash_token("%d:%s" % (u["id"], codigo)), row["codigo_hash"]):
            with con:
                con.execute("UPDATE whatsapp_codigos SET tentativas=tentativas+1 WHERE usuario_id=?", (u["id"],))
            raise Erro(400, "Código incorreto.")
        with con:
            con.execute("UPDATE usuarios SET whatsapp_telefone=?, whatsapp_verificado_em=? WHERE id=?", (tel, agora(), u["id"]))
            con.execute("DELETE FROM whatsapp_codigos WHERE usuario_id=?", (u["id"],))
        self._json(200, {"ok": True, "mensagem": "WhatsApp vinculado e verificado com sucesso."})

    def _webhook_whatsapp(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > 1_000_000:
            return self._enviar(413, b"Payload too large", "text/plain")
        raw = self.rfile.read(n)
        if not META_APP_SECRET:
            return self._enviar(503, b"META_APP_SECRET not configured", "text/plain")
        esperado = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(self.headers.get("X-Hub-Signature-256", ""), esperado):
            return self._enviar(403, b"Invalid signature", "text/plain")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._enviar(400, b"Bad JSON", "text/plain")
        if payload.get("object") == "whatsapp_business_account":
            con = banco()
            try:
                for entry in payload.get("entry", []):
                    for change in entry.get("changes", []):
                        for msg in (change.get("value") or {}).get("messages", []) or []:
                            try:
                                tratar_mensagem_whatsapp(con, msg)
                            except Exception as exc:
                                print("[whatsapp] erro ao tratar mensagem:", type(exc).__name__, exc)
            finally:
                con.close()
        self._json(200, {"ok": True})   # sempre 200: a Meta reenviaria em caso de erro

    # ---- assinatura
    def _checkout(self, u):
        if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
            raise Erro(503, "O pagamento ainda não foi configurado neste servidor.")
        base = BASE_URL or ("http://" + self.headers.get("Host", "localhost"))
        campos = {"mode": "subscription", "line_items[0][price]": STRIPE_PRICE_ID, "line_items[0][quantity]": "1",
                  "success_url": base + "/?assinatura=ok", "cancel_url": base + "/?assinatura=cancelada",
                  "client_reference_id": str(u["id"]), "locale": "pt-BR", "allow_promotion_codes": "true"}
        if u["stripe_cliente"]:
            campos["customer"] = u["stripe_cliente"]
        else:
            campos["customer_email"] = u["email"]
        try:
            s = stripe_post("/v1/checkout/sessions", campos)
        except Exception:
            raise Erro(502, "Não foi possível abrir o pagamento agora. Tente novamente.")
        self._json(200, {"url": s.get("url")})

    def _portal(self, u):
        if not (STRIPE_SECRET_KEY and u["stripe_cliente"]):
            raise Erro(400, "Nenhuma assinatura para gerenciar.")
        base = BASE_URL or ("http://" + self.headers.get("Host", "localhost"))
        try:
            s = stripe_post("/v1/billing_portal/sessions", {"customer": u["stripe_cliente"], "return_url": base + "/"})
        except Exception:
            raise Erro(502, "Não foi possível abrir o portal agora. Tente novamente.")
        self._json(200, {"url": s.get("url")})

    def _webhook_stripe(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > 1_000_000:
            return self._enviar(413, b"Payload too large", "text/plain")
        raw = self.rfile.read(n)
        if not STRIPE_WEBHOOK_SECRET:
            return self._enviar(503, b"STRIPE_WEBHOOK_SECRET not configured", "text/plain")
        if not stripe_assinatura_valida(raw, self.headers.get("Stripe-Signature", "")):
            return self._enviar(400, b"Invalid signature", "text/plain")
        try:
            ev = json.loads(raw)
        except ValueError:
            return self._enviar(400, b"Bad JSON", "text/plain")
        con = banco()
        try:
            tratar_evento_stripe(con, ev)
        finally:
            con.close()
        self._json(200, {"ok": True})


def _em_disco_montado(pasta):
    """True se a pasta está dentro de um disco montado (ex.: Disk do Render), não no sistema temporário."""
    d = os.path.abspath(pasta)
    while d and d != os.path.dirname(d):
        if os.path.ismount(d):
            return True
        d = os.path.dirname(d)
    return False


def main():
    ap = argparse.ArgumentParser(description="Servidor do ZenFi Tech")
    ap.add_argument("--porta", type=int, default=int(env("PORT", "8000") or 8000))
    ap.add_argument("--rede", action="store_true", help="aceitar conexões externas")
    a = ap.parse_args()
    if not os.path.isfile(ARQ_HTML):
        raise SystemExit("Coloque o zenfi_whatsapp.html na mesma pasta do servidor.")
    zenfi_backup.restaurar(ARQ_BANCO)          # baixa o banco da nuvem se o arquivo local sumiu
    criar_tabelas()
    envio_final = zenfi_backup.iniciar(ARQ_BANCO)
    host = "0.0.0.0" if (a.rede or env("PORT")) else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.porta), Handler)
    print("ZenFi Tech no ar em http://localhost:%d  (dados em %s)" % (a.porta, DATA_DIR))
    if (env("RENDER") or os.path.exists("/.dockerenv")) and not zenfi_backup.configurado() and not _em_disco_montado(DATA_DIR):
        print("[ATENÇÃO] O banco está em %s, que NÃO parece ser um disco persistente, e o backup gratuito "
              "(BACKUP_SUPABASE_URL/KEY) não está configurado. A cada deploy/reinício as contas somem e aparece "
              "'senha incorreta'. Configure o backup (veja o LEIA-ME) ou monte um volume persistente em DATA_DIR." % DATA_DIR)

    def _encerrar(*_):                          # Render avisa com SIGTERM antes de desligar
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _encerrar)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor desligado.")
    finally:
        srv.server_close()
        envio_final()


if __name__ == "__main__":
    main()
