# -*- coding: utf-8 -*-
"""Guarda o banco do ZenFi (SQLite) num armazenamento GRATUITO na nuvem (Supabase Storage).

Para que serve: em hospedagem gratuita (ex.: Render sem Disk) o arquivo zenfi.db some a cada
deploy/reinício e as contas desaparecem ("senha incorreta"). Com este módulo:
  1. ao ligar, o servidor BAIXA a última cópia do banco (se o arquivo local não existir);
  2. enquanto roda, ENVIA uma cópia nova poucos segundos depois de qualquer mudança;
  3. ao ser desligado (SIGTERM do Render), envia a cópia final.

Só usa a biblioteca padrão. Variáveis (todas no painel do Render, nunca no código):
  BACKUP_SUPABASE_URL   https://SEU-PROJETO.supabase.co
  BACKUP_SUPABASE_KEY   chave "service_role" (secreta! só no servidor)
  BACKUP_BUCKET         nome do bucket PRIVADO (padrão: zenfi-backup)
  BACKUP_OBJETO         nome do arquivo no bucket (padrão: zenfi.db)
  BACKUP_INTERVALO      segundos entre verificações de mudança (padrão: 10)
Sem BACKUP_SUPABASE_URL/KEY o módulo fica desligado e nada muda.
"""
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("BACKUP_SUPABASE_URL", "").strip().rstrip("/")
CHAVE = os.environ.get("BACKUP_SUPABASE_KEY", "").strip()
BUCKET = os.environ.get("BACKUP_BUCKET", "").strip() or "zenfi-backup"
OBJETO = os.environ.get("BACKUP_OBJETO", "").strip() or "zenfi.db"
try:
    INTERVALO = max(2, int(os.environ.get("BACKUP_INTERVALO", "10") or 10))
except ValueError:
    INTERVALO = 10

estado = {"ativo": False, "ultimo_envio": None, "ultimo_erro": None, "restaurado": None}
_trava = threading.Lock()


def configurado():
    return bool(URL and CHAVE)


def _url(objeto, autenticado):
    nome = urllib.parse.quote(objeto, safe="/")
    if autenticado:   # leitura de bucket privado
        return "%s/storage/v1/object/authenticated/%s/%s" % (URL, BUCKET, nome)
    return "%s/storage/v1/object/%s/%s" % (URL, BUCKET, nome)


def _cabecalhos(extra=None):
    h = {"apikey": CHAVE, "Authorization": "Bearer " + CHAVE}
    h.update(extra or {})
    return h


def _baixar(objeto):
    """Devolve os bytes, ou None se o arquivo ainda não existe. Qualquer outro problema levanta erro."""
    req = urllib.request.Request(_url(objeto, True), headers=_cabecalhos(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        # o Supabase às vezes devolve 400 com {"error":"not_found"} para objeto inexistente
        if e.code == 400:
            try:
                if b"not_found" in e.read().lower().replace(b" ", b"_"):
                    return None
            except Exception:
                pass
        raise


def _enviar(objeto, dados):
    req = urllib.request.Request(
        _url(objeto, False), data=dados, method="POST",
        headers=_cabecalhos({"Content-Type": "application/octet-stream", "x-upsert": "true"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def _banco_valido(caminho):
    try:
        con = sqlite3.connect(caminho)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            return bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='usuarios'").fetchone())
        finally:
            con.close()
    except sqlite3.Error:
        return False


def restaurar(arq_banco, tentativas=4):
    """Chame ANTES de abrir o banco. Se o arquivo local não existe, baixa a última cópia da nuvem.

    Se a nuvem estiver com problema (e não for simplesmente 'ainda não há cópia'), o servidor NÃO
    sobe vazio: isso evitaria sobrescrever a cópia boa com um banco novo. Lança SystemExit.
    """
    if not configurado():
        return
    estado["ativo"] = True
    if os.path.isfile(arq_banco) and os.path.getsize(arq_banco) > 0:
        estado["restaurado"] = "local"
        print("[backup] banco local encontrado; mantido.")
        return
    ultimo = None
    for n in range(tentativas):
        try:
            dados = _baixar(OBJETO)
            if dados is None:
                estado["restaurado"] = "primeira-vez"
                print("[backup] ainda não há cópia na nuvem (primeira execução).")
                return
            os.makedirs(os.path.dirname(os.path.abspath(arq_banco)), exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(os.path.abspath(arq_banco)))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(dados)
                if not _banco_valido(tmp):
                    raise ValueError("a cópia baixada está corrompida ou não é um banco do ZenFi")
                os.replace(tmp, arq_banco)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            estado["restaurado"] = "nuvem"
            print("[backup] banco restaurado da nuvem (%d bytes)." % len(dados))
            return
        except Exception as e:  # rede, chave errada, bucket inexistente, cópia ruim...
            ultimo = e
            estado["ultimo_erro"] = "restaurar: %s" % e
            print("[backup] falha ao restaurar (%d/%d): %s" % (n + 1, tentativas, e))
            time.sleep(min(2 * (n + 1), 6))
    raise SystemExit("[backup] não consegui ler a cópia da nuvem (%s). Não vou subir com banco vazio "
                     "para não apagar suas contas. Confira BACKUP_SUPABASE_URL, BACKUP_SUPABASE_KEY e o bucket." % ultimo)


def _assinatura(arq_banco):
    partes = []
    for suf in ("", "-wal"):
        try:
            st = os.stat(arq_banco + suf)
            partes.append((st.st_mtime_ns, st.st_size))
        except OSError:
            partes.append(None)
    return tuple(partes)


def _copia_consistente(arq_banco):
    """Foto do banco feita pela API de backup do SQLite (segura mesmo com o servidor escrevendo)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        origem = sqlite3.connect(arq_banco, timeout=15)
        destino = sqlite3.connect(tmp)
        try:
            origem.backup(destino)
        finally:
            destino.close()
            origem.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def enviar_agora(arq_banco):
    """Envia uma cópia do banco. Devolve True se deu certo."""
    if not configurado() or not os.path.isfile(arq_banco):
        return False
    with _trava:
        try:
            dados = _copia_consistente(arq_banco)
            _enviar(OBJETO, dados)
            dia = time.strftime("%Y-%m-%d", time.gmtime())
            if estado.get("_dia") != dia:      # uma cópia por dia, para o caso de um envio ruim
                _enviar("diario/zenfi-%s.db" % dia, dados)
                estado["_dia"] = dia
            estado["ultimo_envio"] = int(time.time())
            estado["ultimo_erro"] = None
            return True
        except Exception as e:
            estado["ultimo_erro"] = "enviar: %s" % e
            print("[backup] falha ao enviar: %s" % e)
            return False


def iniciar(arq_banco):
    """Liga a vigilância em segundo plano. Devolve uma função que faz o envio final."""
    if not configurado():
        return lambda: None
    estado["ativo"] = True
    parar = threading.Event()
    visto = {"sig": None}

    def laco():
        # a primeira passada sempre envia uma cópia (cobre a primeira execução e migrações do início)
        while not parar.wait(INTERVALO):
            sig = _assinatura(arq_banco)
            if sig != visto["sig"]:
                if enviar_agora(arq_banco):
                    visto["sig"] = sig

    t = threading.Thread(target=laco, name="zenfi-backup", daemon=True)
    t.start()

    def final():
        parar.set()
        if _assinatura(arq_banco) != visto["sig"]:
            enviar_agora(arq_banco)
    print("[backup] ativo: cópia na nuvem a cada mudança (verifica a cada %ds)." % INTERVALO)
    return final


def resumo():
    """Para o /healthz: sem dados de usuários."""
    if not configurado():
        return {"ativo": False}
    return {"ativo": True, "restaurado": estado.get("restaurado"),
            "ultimo_envio": estado.get("ultimo_envio"), "erro": estado.get("ultimo_erro")}
