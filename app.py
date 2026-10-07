"""Fila de impressão 3D compartilhada entre amigos.

Rodar:  python app.py   ->  http://localhost:5000
"""
import json
import math
import os
import re
import secrets
import bisect
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

from gcode_parser import filamento_extrudado, indice_extrusao, mm_ate, parse_gcode
from juntar import JuntarErro, MAX_PECAS, filamento_por_peca, juntar as juntar_gcodes
from octoprint import OctoPrint, OctoPrintErro
from tunel import endereco_funnel, situacao_tailscale, tunel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Os dados ficam fora do OneDrive: o banco é gravado a cada poucos segundos durante uma impressão e, dentro
# de uma pasta sincronizada, o OneDrive ficava reenviando o arquivo sem parar (pesava no computador).
# (na pasta do usuário, e não em AppData: programas instalados pela loja enxergam um AppData só deles)
_DADOS_LOCAIS = os.path.join(os.path.expanduser("~"), "PrintDeck", "dados")
DATA_DIR = os.environ.get("DATA_DIR") or (_DADOS_LOCAIS if os.path.isdir(_DADOS_LOCAIS)
                                          else os.path.join(BASE_DIR, "dados"))
UPLOAD_DIR = os.path.join(DATA_DIR, "gcodes")
DB_PATH = os.path.join(DATA_DIR, "fila.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)
tunel.pid_file = os.path.join(DATA_DIR, "cloudflared.pid")


def ligar_internet():
    tunel.modo = ler_config("link_modo") or "auto"
    tunel.ligar(PORTA)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB (limite do túnel da Cloudflare)
app.permanent_session_lifetime = timedelta(days=365)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # mudanças nas páginas valem sem reiniciar
# o cookie de sessão não acompanha formulários enviados por outros sites (protege os botões do administrativo)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


@app.after_request
def cabecalhos_de_seguranca(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")      # arquivo enviado nunca é tratado como página
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")          # ninguém embute o painel em outro site
    resp.headers.setdefault("Referrer-Policy", "same-origin")         # endereços daqui não vazam para sites de fora
    return resp
PORTA = int(os.environ.get("PORT", 5000))

STATUS_LABELS = {
    "aguardando": "Aguardando",
    "imprimindo": "Imprimindo",
    "concluida": "Concluída",
    "falhou": "Falhou",
    "cancelada": "Cancelada",
}
ACTIVE = ("aguardando", "imprimindo")
FINISHED = ("concluida", "falhou")

DEFAULT_SETTINGS = {
    "diametro_mm": "1.75",
    "potencia_w": "120",          # consumo médio de uma Ender 3 imprimindo PLA
    "tarifa_kwh": "0.85",         # R$ por kWh
    "fator_tempo": "1.0",         # corrige a estimativa do Cura (ex.: 1.1 = +10%)
    "admin_senha": generate_password_hash("admin"),
    "senha_galera": "",           # vazio = sem senha para os amigos
    "internet_ligada": "0",       # religa o túnel ao reiniciar o programa
    "octoprint_url": "",          # ex.: http://octopi.local
    "octoprint_api_key": "",
    "octoprint_webcam": "",       # vazio = <url>/webcam/?action=snapshot
    "ntfy_topico": "",            # aviso no celular (app ntfy) quando o link muda; vazio = desligado
    "link_modo": "auto",          # "auto" = link fixo do Tailscale se disponível, senão Cloudflare; "cloudflare" = só Cloudflare
    "fechar_minimiza": "1",       # aplicativo de desktop: o X da janela manda para a bandeja em vez de desligar
}
DEFAULT_MATERIALS = [
    ("PLA", 100.0, 1.24),
    ("PETG", 110.0, 1.27),
    ("ABS", 100.0, 1.04),
    ("TPU", 150.0, 1.21),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS materiais (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    preco_kg REAL NOT NULL,
    densidade REAL NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS impressoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amigo TEXT NOT NULL,
    arquivo_original TEXT NOT NULL,
    arquivo_salvo TEXT NOT NULL,
    observacao TEXT,
    material_id INTEGER REFERENCES materiais(id),
    status TEXT NOT NULL DEFAULT 'aguardando',
    posicao INTEGER NOT NULL,
    cobrar INTEGER NOT NULL DEFAULT 1,
    tempo_fatiador_s INTEGER,
    tempo_s INTEGER,
    filamento_m REAL,
    gramas REAL,
    custo_material REAL,
    custo_energia REAL,
    custo_total REAL,
    altura_camada REAL,
    maquina TEXT,
    miniatura TEXT,
    criado_em TEXT NOT NULL,
    iniciado_em TEXT,
    finalizado_em TEXT
);
CREATE TABLE IF NOT EXISTS rolos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT NOT NULL UNIQUE,
    descricao TEXT,
    material_id INTEGER NOT NULL REFERENCES materiais(id),
    peso_inicial_g REAL NOT NULL,
    restante_g REAL NOT NULL,
    criado_em TEXT NOT NULL,
    arquivado INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movimentos_rolo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rolo_id INTEGER NOT NULL REFERENCES rolos(id),
    impressao_id INTEGER REFERENCES impressoes(id),
    gramas REAL NOT NULL,              -- negativo = gastou; positivo = voltou para o rolo / ajuste
    motivo TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
"""
# colunas adicionadas depois da primeira versão (bancos antigos ganham via ALTER TABLE)
COLUNAS_NOVAS = {
    "octoprint_arquivo": "TEXT",   # nome do arquivo no OctoPrint
    "progresso_pct": "REAL",       # progresso real informado pelo OctoPrint
    "restante_s": "INTEGER",
    "falhou_em_pct": "REAL",
    "rolo_id": "INTEGER",          # rolo de filamento usado (escolhido pelo amigo ao enviar)
    "filepos_final": "INTEGER",    # último byte do G-code que o OctoPrint informou (para falhas)
    "gramas_agora": "REAL",        # filamento que já saiu na impressão em andamento (ao vivo)
    "eh_lote": "INTEGER",          # 1 = "mesa conjunta": várias peças juntadas em um G-code só
    "grupo_id": "INTEGER",         # peça que faz parte de uma mesa conjunta (id do lote)
    "layout": "TEXT",              # JSON: onde cada peça ficou na mesa (só no lote)
}
# As peças de uma mesa conjunta ficam com status 'agrupada' enquanto o lote está na fila/imprimindo e
# recebem o resultado quando ele termina. O lote não entra nas somas: quem conta são as peças.
SEM_LOTE = "COALESCE(eh_lote, 0) = 0"
# cada peça com o status "de verdade" (o do lote, se estiver numa mesa conjunta)
PECAS_SQL = """(SELECT i.id, i.amigo, i.cobrar, i.tempo_s, i.gramas, i.custo_material, i.custo_energia, i.custo_total,
                       CASE WHEN i.status = 'agrupada'
                            THEN COALESCE((SELECT l.status FROM impressoes l WHERE l.id = i.grupo_id), 'aguardando')
                            ELSE i.status END AS status
                FROM impressoes i WHERE COALESCE(i.eh_lote, 0) = 0)"""


# ---------------------------------------------------------------- banco

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if "estoque_g" not in {r[1] for r in db.execute("PRAGMA table_info(materiais)")}:
        db.execute("ALTER TABLE materiais ADD COLUMN estoque_g REAL")  # NULL = não controlar estoque
    existentes = {r[1] for r in db.execute("PRAGMA table_info(impressoes)")}
    for col, tipo in COLUNAS_NOVAS.items():
        if col not in existentes:
            db.execute(f"ALTER TABLE impressoes ADD COLUMN {col} {tipo}")
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    if not db.execute("SELECT 1 FROM materiais LIMIT 1").fetchone():
        db.executemany("INSERT INTO materiais (nome, preco_kg, densidade) VALUES (?, ?, ?)",
                       DEFAULT_MATERIALS)
    if not db.execute("SELECT 1 FROM settings WHERE key='admin_link' AND value != ''").fetchone():
        # link fixo do administrador: nasce junto com o banco (a sessão de administrador depende dele)
        db.execute("INSERT OR REPLACE INTO settings VALUES ('admin_link', ?)", (secrets.token_urlsafe(32),))
    if not db.execute("SELECT 1 FROM settings WHERE key='secret_key'").fetchone():
        db.execute("INSERT INTO settings VALUES ('secret_key', ?)", (secrets.token_hex(32),))
    db.commit()
    app.secret_key = db.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()[0]
    db.close()


def get_settings():
    return {r["key"]: r["value"] for r in get_db().execute("SELECT key, value FROM settings")}


# ---------------------------------------------------------------- cálculos

def calcular_custos(tempo_s, filamento_m, gramas_fatiador, material, cfg):
    """Retorna (tempo_ajustado_s, gramas, custo_material, custo_energia, total)."""
    tempo = int((tempo_s or 0) * float(cfg["fator_tempo"]))
    if gramas_fatiador:
        gramas = gramas_fatiador
    else:
        raio_cm = float(cfg["diametro_mm"]) / 20
        volume_cm3 = (filamento_m or 0) * 100 * math.pi * raio_cm ** 2
        gramas = volume_cm3 * material["densidade"]
    custo_material = gramas / 1000 * material["preco_kg"]
    custo_energia = float(cfg["potencia_w"]) / 1000 * (tempo / 3600) * float(cfg["tarifa_kwh"])
    return tempo, gramas, custo_material, custo_energia, custo_material + custo_energia


def recalcular_ativos():
    """Reaplica preços atuais às impressões que ainda não terminaram."""
    db = get_db()
    cfg = get_settings()
    rows = db.execute(f"""SELECT i.id, i.tempo_fatiador_s, i.filamento_m, i.gramas, m.preco_kg, m.densidade
                          FROM impressoes i JOIN materiais m ON m.id = i.material_id
                          WHERE i.status IN ('aguardando', 'imprimindo', 'agrupada')""").fetchall()
    for r in rows:
        # sem metros de filamento, as gramas vieram prontas do fatiador
        gramas_fatiador = None if r["filamento_m"] else r["gramas"]
        tempo, gramas, c_mat, c_en, total = calcular_custos(
            r["tempo_fatiador_s"], r["filamento_m"], gramas_fatiador, r, cfg)
        db.execute("""UPDATE impressoes SET tempo_s=?, gramas=?, custo_material=?, custo_energia=?,
                      custo_total=? WHERE id=?""", (tempo, gramas, c_mat, c_en, total, r["id"]))
    db.commit()


def fila_com_previsao():
    """Impressões ativas em ordem, com horário previsto de término."""
    rows = get_db().execute(f"""SELECT i.*, m.nome AS material_nome, r.codigo AS rolo_codigo,
                                       r.descricao AS rolo_descricao FROM impressoes i
                                LEFT JOIN materiais m ON m.id = i.material_id
                                LEFT JOIN rolos r ON r.id = i.rolo_id
                                WHERE i.status IN {ACTIVE}
                                ORDER BY (i.status='imprimindo') DESC, i.posicao""").fetchall()
    agora = datetime.now()
    cursor = agora
    fila = []
    for r in rows:
        item = dict(r)
        tempo = r["tempo_s"] or 0
        if r["status"] == "imprimindo" and r["progresso_pct"] is not None:
            # progresso real vindo do OctoPrint
            fim = agora + timedelta(seconds=r["restante_s"] if r["restante_s"] is not None
                                    else tempo * (1 - r["progresso_pct"] / 100))
            item["progresso"] = r["progresso_pct"]
            cursor = fim
        elif r["status"] == "imprimindo" and r["iniciado_em"]:
            fim = datetime.fromisoformat(r["iniciado_em"]) + timedelta(seconds=tempo)
            item["progresso"] = min(100, max(0, (agora - datetime.fromisoformat(r["iniciado_em"])).total_seconds()
                                            / tempo * 100)) if tempo else 0
            cursor = max(fim, agora)
        else:
            item["inicio"] = cursor
            cursor = cursor + timedelta(seconds=tempo)
            fim = cursor
        item["previsao"] = fim
        if r["status"] == "imprimindo":
            item["gramas_agora"] = r["gramas_agora"] if (r["octoprint_arquivo"] and r["gramas_agora"] is not None) \
                else (r["gramas"] or 0) * (item.get("progresso") or 0) / 100
        if r["eh_lote"]:
            item["pecas"] = pecas_do_lote(get_db(), r["id"])
            try:
                item["mesa"] = json.loads(r["layout"] or "[]")
            except ValueError:
                item["mesa"] = []
        fila.append(item)
    return fila


# ---------------------------------------------------------------- mesa conjunta (várias peças numa impressão)

def pecas_do_lote(db, lote_id):
    return [dict(r) for r in db.execute("SELECT * FROM impressoes WHERE grupo_id=? ORDER BY posicao, id", (lote_id,))]


def partes(db, job):
    """As peças que formam uma impressão: ela mesma ou, numa mesa conjunta, cada peça dos amigos."""
    if not job or not job.get("eh_lote"):
        return [job] if job else []
    return job.get("pecas") or pecas_do_lote(db, job["id"])


def criar_lote(db, ids, cfg):
    """Junta peças que estão aguardando em uma mesa só. Devolve o id do lote; JuntarErro se não der."""
    ids = list(dict.fromkeys(ids))
    marcas = ",".join("?" * len(ids))
    pecas = db.execute(f"SELECT * FROM impressoes WHERE id IN ({marcas}) ORDER BY posicao, id", ids).fetchall()
    if len(pecas) != len(ids) or any(p["status"] != "aguardando" for p in pecas):
        raise JuntarErro("só dá para juntar peças que estão aguardando na fila")
    if any(p["eh_lote"] for p in pecas):
        raise JuntarErro("uma das escolhidas já é uma mesa conjunta (desfaça ela antes)")
    if len({(p["rolo_id"], p["material_id"]) for p in pecas}) > 1:
        raise JuntarErro("as peças estão em rolos ou materiais diferentes")
    salvo = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_mesa_conjunta.gcode"
    destino = os.path.join(UPLOAD_DIR, salvo)
    try:
        resumo = juntar_gcodes([(os.path.join(UPLOAD_DIR, p["arquivo_salvo"]), nome_arquivo_seguro(p["arquivo_original"]))
                                for p in pecas], destino)
    except OSError:
        raise JuntarErro("o arquivo de uma das peças não existe mais")
    material = db.execute("SELECT * FROM materiais WHERE id=?", (pecas[0]["material_id"],)).fetchone()
    tempo, gramas, c_mat, c_en, total = calcular_custos(resumo["tempo_s"], resumo["filamento_m"], None, material, cfg)
    amigos = list(dict.fromkeys(p["amigo"] for p in pecas))
    mesa = [{**lugar, "id": p["id"], "nome": p["arquivo_original"], "amigo": p["amigo"]}
            for lugar, p in zip(resumo["layout"], pecas)]
    cur = db.execute("""INSERT INTO impressoes (amigo, arquivo_original, arquivo_salvo, observacao, material_id,
                    posicao, tempo_fatiador_s, tempo_s, filamento_m, gramas, custo_material, custo_energia, custo_total,
                    altura_camada, maquina, criado_em, rolo_id, eh_lote, layout)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                     (" + ".join(amigos)[:80], f"Mesa conjunta · {len(pecas)} peças", salvo,
                      " | ".join(f"{p['amigo']}: {p['observacao']}" for p in pecas if p["observacao"])[:300],
                      material["id"], min(p["posicao"] for p in pecas), resumo["tempo_s"], tempo, resumo["filamento_m"],
                      gramas, c_mat, c_en, total, resumo["altura_camada"], resumo["maquina"],
                      datetime.now().isoformat(), pecas[0]["rolo_id"], json.dumps(mesa)))
    db.execute(f"UPDATE impressoes SET status='agrupada', grupo_id=? WHERE id IN ({marcas})", [cur.lastrowid] + ids)
    return cur.lastrowid


def desfazer_lote(db, lote):
    """As peças voltam a ser pedidos separados na fila e o G-code conjunto é apagado."""
    db.execute("UPDATE impressoes SET status='aguardando', grupo_id=NULL WHERE grupo_id=? AND status='agrupada'", (lote["id"],))
    db.execute("UPDATE impressoes SET status='aguardando' WHERE id=?", (lote["id"],))
    acertar_consumo(db, lote["id"], motivo=f"mesa conjunta #{lote['id']} desfeita")
    db.execute("UPDATE movimentos_rolo SET impressao_id=NULL WHERE impressao_id=?", (lote["id"],))
    db.execute("DELETE FROM impressoes WHERE id=?", (lote["id"],))
    try:
        os.remove(os.path.join(UPLOAD_DIR, lote["arquivo_salvo"]))
    except OSError:
        pass


def propagar_lote(db, lote_id):
    """Quando a mesa conjunta termina (ou falha/é cancelada), cada peça recebe o resultado e a sua parte
    do tempo e da energia. Numa falha, o filamento de cada peça é o que saiu de verdade para ela."""
    lote = db.execute("SELECT * FROM impressoes WHERE id=?", (lote_id,)).fetchone()
    if not lote or not lote["eh_lote"] or lote["status"] in ACTIVE:
        return
    filhos = {f["id"]: f for f in db.execute("""SELECT i.*, m.preco_kg, m.densidade FROM impressoes i
                 LEFT JOIN materiais m ON m.id = i.material_id WHERE i.grupo_id=? AND i.status='agrupada'""", (lote_id,))}
    if not filhos:
        return
    try:
        ordem = [p["id"] for p in json.loads(lote["layout"] or "[]")]
    except ValueError:
        ordem = []
    soma_t = sum(f["tempo_fatiador_s"] or 0 for f in filhos.values()) or 1
    comecou = bool(lote["iniciado_em"])
    mm = None
    if lote["status"] != "concluida" and comecou and lote["filepos_final"] and ordem:
        try:
            mm = filamento_por_peca(os.path.join(UPLOAD_DIR, lote["arquivo_salvo"]), lote["filepos_final"], len(ordem))
        except OSError:
            mm = None
    cfg = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}
    area = math.pi * (float(cfg.get("diametro_mm", 1.75)) / 2) ** 2 / 1000
    for f in filhos.values():
        tempo, gramas, c_en = f["tempo_s"], f["gramas"] or 0, f["custo_energia"] or 0
        if comecou:
            parte = (f["tempo_fatiador_s"] or 0) / soma_t
            tempo, c_en = int((lote["tempo_s"] or 0) * parte), (lote["custo_energia"] or 0) * parte
        if mm is not None and f["id"] in ordem:
            gramas = mm[ordem.index(f["id"])] * area * (f["densidade"] or 1.24)
        c_mat = gramas / 1000 * (f["preco_kg"] or 0)
        db.execute("""UPDATE impressoes SET status=?, iniciado_em=?, finalizado_em=?, tempo_s=?, gramas=?, custo_material=?,
                      custo_energia=?, custo_total=?, falhou_em_pct=?, cobrar=? WHERE id=?""",
                   (lote["status"], lote["iniciado_em"], lote["finalizado_em"], tempo, gramas, c_mat, c_en, c_mat + c_en,
                    lote["falhou_em_pct"], 0 if lote["status"] == "cancelada" else f["cobrar"], f["id"]))


def normalizar_nome(nome):
    nome = re.sub(r"\s+", " ", (nome or "").strip())
    return nome[:40].title()


def nome_arquivo_seguro(nome):
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"[^A-Za-z0-9._-]+", "_", nome).strip("._")
    return nome or "arquivo"


# ---------------------------------------------------------------- OctoPrint

# último estado lido da impressora (atualizado pela sincronização em segundo plano)
IMPRESSORA = {"configurada": False}


def cliente_octoprint(cfg):
    if cfg.get("octoprint_url") and cfg.get("octoprint_api_key"):
        return OctoPrint(cfg["octoprint_url"], cfg["octoprint_api_key"])
    return None


def energia(segundos, cfg):
    return float(cfg["potencia_w"]) / 1000 * (segundos / 3600) * float(cfg["tarifa_kwh"])


def consumo_esperado(job):
    """Gramas que a impressão tirou do rolo: tudo se terminou/falhou; o que chegou a imprimir se foi
    cancelada no meio; nada se ainda está na fila ou imprimindo (desconta ao terminar).
    Peça de mesa conjunta não desconta: quem desconta é o lote, pelo G-code que realmente imprimiu."""
    if job["grupo_id"]:
        return 0
    if job["status"] in FINISHED:
        return job["gramas"] or 0
    if job["status"] == "cancelada" and job["iniciado_em"] and job["filepos_final"]:
        return job["gramas"] or 0
    return 0


def acertar_consumo(db, job_id, motivo=None):
    """Deixa o extrato dos rolos de acordo com o estado da impressão (idempotente)."""
    job = db.execute("SELECT * FROM impressoes WHERE id=?", (job_id,)).fetchone()
    if not job:
        return
    lancado = {r["rolo_id"]: r["g"] for r in db.execute(
        "SELECT rolo_id, SUM(gramas) AS g FROM movimentos_rolo WHERE impressao_id=? GROUP BY rolo_id", (job_id,))}
    alvo = {job["rolo_id"]: -consumo_esperado(job)} if job["rolo_id"] else {}
    for rolo_id in set(lancado) | set(alvo):
        delta = round(alvo.get(rolo_id, 0) - (lancado.get(rolo_id) or 0), 3)
        if abs(delta) < 0.005:
            continue
        texto = motivo or (f"impressão #{job_id} {STATUS_LABELS.get(job['status'], job['status']).lower()}"
                           if delta < 0 else f"impressão #{job_id} devolvida ao rolo")
        db.execute("INSERT INTO movimentos_rolo (rolo_id, impressao_id, gramas, motivo, criado_em) VALUES (?,?,?,?,?)",
                   (rolo_id, job_id, delta, texto, datetime.now().isoformat()))
        db.execute("UPDATE rolos SET restante_g = restante_g + ? WHERE id=?", (delta, rolo_id))


_INDICES_EXTRUSAO = {}


def gramas_agora_ate(job, filepos, diametro_mm=1.75, densidade=1.24):
    """Filamento (g) que já saiu até o byte `filepos`, consultando um índice montado uma vez por arquivo."""
    caminho = os.path.join(UPLOAD_DIR, job["arquivo_salvo"])
    if caminho not in _INDICES_EXTRUSAO:
        try:
            _INDICES_EXTRUSAO[caminho] = indice_extrusao(caminho)
        except OSError:
            return None
        if len(_INDICES_EXTRUSAO) > 10:
            _INDICES_EXTRUSAO.pop(next(iter(_INDICES_EXTRUSAO)))
    mm = mm_ate(_INDICES_EXTRUSAO[caminho], filepos)
    return mm * math.pi * (diametro_mm / 2) ** 2 / 1000 * densidade


def gramas_ate(job, filepos, db):
    """Gramas realmente extrudadas até o byte `filepos` do G-code da impressão."""
    m = db.execute("SELECT densidade FROM materiais WHERE id=?", (job["material_id"],)).fetchone()
    cfg = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}
    try:
        return filamento_extrudado(os.path.join(UPLOAD_DIR, job["arquivo_salvo"]), filepos,
                                   float(cfg.get("diametro_mm", 1.75)), m["densidade"] if m else 1.24)
    except OSError:
        return None


def mexer_estoque(db, job_id, sinal=1):
    """Compatibilidade: o estoque agora é o extrato de cada rolo."""
    acertar_consumo(db, job_id)


def finalizar_pelo_octoprint(db, job, resultado, cfg):
    """Fecha uma impressão com o resultado real vindo do OctoPrint."""
    agora = datetime.now().isoformat()
    if resultado.get("success"):
        tempo_real = int(resultado.get("printTime") or job["tempo_s"] or 0)
        c_en = energia(tempo_real, cfg)
        db.execute("""UPDATE impressoes SET status='concluida', finalizado_em=?, tempo_s=?, custo_energia=?,
                      custo_total=custo_material + ?, progresso_pct=100, restante_s=NULL WHERE id=?""",
                   (agora, tempo_real, c_en, c_en, job["id"]))
    else:
        # cobra só o filamento que chegou a ser usado: exato pelo G-code até o último byte impresso
        pct = job["progresso_pct"] if job["progresso_pct"] is not None else 100.0
        fracao = max(0.0, min(1.0, pct / 100))
        gramas = gramas_ate(job, job["filepos_final"], db) if job["filepos_final"] else None
        if gramas is None:
            gramas = (job["gramas"] or 0) * fracao
        preco = db.execute("SELECT preco_kg FROM materiais WHERE id=?", (job["material_id"],)).fetchone()
        c_mat = gramas / 1000 * (preco["preco_kg"] if preco else 0)
        # sem o tempo real do OctoPrint, usa a mesma proporção da falha (e não o tempo da peça inteira)
        tempo_real = int(resultado.get("printTime") or 0)
        if not tempo_real and job["iniciado_em"]:
            tempo_real = int(max(0, (datetime.now() - datetime.fromisoformat(job["iniciado_em"])).total_seconds()))
        c_en = energia(tempo_real, cfg)
        db.execute("""UPDATE impressoes SET status='falhou', finalizado_em=?, tempo_s=?, gramas=?,
                      custo_material=?, custo_energia=?, custo_total=?, falhou_em_pct=?, restante_s=NULL WHERE id=?""",
                   (agora, tempo_real, gramas, c_mat, c_en, c_mat + c_en, pct, job["id"]))
    mexer_estoque(db, job["id"])
    propagar_lote(db, job["id"])
    db.commit()


# marcas ";TIME_ELAPSED:" que o Cura grava no fim de cada camada, por arquivo
_MARCAS_TEMPO = {}


def marcas_de_tempo(caminho):
    """([byte de cada marca], [segundos do Cura naquele ponto]) — lido uma vez por arquivo."""
    if caminho not in _MARCAS_TEMPO:
        try:
            with open(caminho, "rb") as f:
                dados = f.read()
        except OSError:
            return [], []
        achadas = [(m.start(), float(m.group(1))) for m in re.finditer(rb";TIME_ELAPSED:([\d.]+)", dados)]
        if len(_MARCAS_TEMPO) > 20:
            _MARCAS_TEMPO.pop(next(iter(_MARCAS_TEMPO)))
        _MARCAS_TEMPO[caminho] = ([o for o, _ in achadas], [t for _, t in achadas])
    return _MARCAS_TEMPO[caminho]


def estimar_pelo_cura(caminho, filepos, decorrido_real, fator):
    """(progresso %, segundos restantes) pelo tempo que o Cura calculou para cada camada.

    O ritmo real da impressora (tempo real ÷ tempo do Cura até aqui) vai ganhando peso ao longo
    da primeira hora; antes disso vale a "correção do tempo do Cura" das configurações.
    Retorna None se o arquivo não tiver as marcas (aí fica a estimativa do OctoPrint)."""
    offs, tempos = marcas_de_tempo(caminho)
    if len(offs) < 2 or filepos is None:
        return None
    total = tempos[-1]
    i = bisect.bisect_right(offs, filepos)
    if i == 0:
        cura = tempos[0] * filepos / max(1, offs[0])
    elif i >= len(offs):
        cura = total
    else:
        cura = tempos[i - 1] + (tempos[i] - tempos[i - 1]) * (filepos - offs[i - 1]) / max(1, offs[i] - offs[i - 1])
    ritmo = fator
    if decorrido_real and cura > 60:
        real = max(0.7, min(2.5, decorrido_real / cura))
        peso = min(1.0, cura / 3600)
        ritmo = peso * real + (1 - peso) * fator
    return cura / total * 100, max(0, (total - cura) * ritmo)


def sincronizar_octoprint():
    """Lê o OctoPrint e atualiza a fila. Roda a cada poucos segundos."""
    global IMPRESSORA
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        cfg = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")}
        cliente = cliente_octoprint(cfg)
        if not cliente:
            IMPRESSORA = {"configurada": False}
            return
        try:
            st = cliente.estado()
        except OctoPrintErro as e:
            IMPRESSORA = {"configurada": True, "online": False, "erro": str(e)}
            return
        IMPRESSORA = {"configurada": True, **st}

        jobs = db.execute("""SELECT * FROM impressoes WHERE status='imprimindo'
                             AND octoprint_arquivo IS NOT NULL""").fetchall()
        for job in jobs:
            if st["ocupada"] and st.get("arquivo") == job["octoprint_arquivo"]:
                est = estimar_pelo_cura(os.path.join(UPLOAD_DIR, job["arquivo_salvo"]), st.get("filepos"),
                                        st.get("decorrido_s"), float(cfg["fator_tempo"]))
                pct, restante = est if est else (st.get("progresso") or 0, st.get("restante_s"))
                dens = db.execute("SELECT densidade FROM materiais WHERE id=?", (job["material_id"],)).fetchone()
                g_agora = gramas_agora_ate(job, st.get("filepos"), float(cfg.get("diametro_mm", 1.75)),
                                           dens["densidade"] if dens else 1.24) if st.get("filepos") is not None else None
                db.execute("""UPDATE impressoes SET progresso_pct=?, restante_s=?, filepos_final=?, gramas_agora=?
                              WHERE id=?""", (pct, restante, st.get("filepos"), g_agora, job["id"]))
                db.commit()
            elif not st["ocupada"]:
                # vale também com a impressora desconectada: se ela parou com erro (ou foi desligada) depois
                # de uma falha, o OctoPrint já registrou o resultado e a fila não pode ficar presa em "imprimindo"
                inicio = datetime.fromisoformat(job["iniciado_em"])
                resultado = cliente.ultimo_resultado(job["octoprint_arquivo"])
                if resultado and resultado.get("date", 0) >= inicio.timestamp() - 5:
                    finalizar_pelo_octoprint(db, job, resultado, cfg)
    except OctoPrintErro as e:
        IMPRESSORA = {**IMPRESSORA, "erro": str(e)}
    finally:
        db.close()


def iniciar_sincronizacao(intervalo=5):
    def loop():
        dia = None
        while True:
            if dia != datetime.now().date():      # ao abrir e a cada virada de dia
                dia = datetime.now().date()
                copia_de_seguranca()
            try:
                sincronizar_octoprint()
            except Exception as e:  # nunca deixa a sincronização morrer
                print("Erro na sincronização com o OctoPrint:", e, flush=True)
            time.sleep(intervalo)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- filtros de template

@app.template_filter("duracao")
def f_duracao(segundos):
    if not segundos:
        return "—"
    segundos = int(segundos)
    d, r = divmod(segundos, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h {m:02d}min"
    if h:
        return f"{h}h {m:02d}min"
    return f"{m} min"


@app.template_filter("reais")
def f_reais(v):
    v = v or 0
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("gramas")
def f_gramas(v):
    v = v or 0
    if v >= 1000:
        return f"{v / 1000:.2f} kg".replace(".", ",")
    if v < 10:   # peças pequenas e falhas: mostra a casa decimal (0,4 g em vez de 0 g)
        return f"{v:.1f} g".replace(".", ",")
    return f"{v:.0f} g"


@app.template_filter("dataHora")
def f_data_hora(v):
    if not v:
        return "—"
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    hoje = datetime.now().date()
    dia = {hoje: "hoje", hoje + timedelta(days=1): "amanhã", hoje - timedelta(days=1): "ontem"}.get(v.date())
    return f"{dia or v.strftime('%d/%m')} às {v.strftime('%H:%M')}"


@app.template_filter("relativo")
def f_relativo(v):
    if not v:
        return ""
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    seg = (datetime.now() - v).total_seconds()
    if seg < 60:
        return "agora"
    if seg < 3600:
        return f"há {int(seg // 60)} min"
    if seg < 86400:
        return f"há {int(seg // 3600)} h"
    dias = int(seg // 86400)
    return "ontem" if dias == 1 else f"há {dias} dias"


@app.template_filter("iniciais")
def f_iniciais(nome):
    partes = (nome or "?").split()
    return (partes[0][0] + (partes[-1][0] if len(partes) > 1 else "")).upper()


@app.template_filter("quebra")
def f_quebra(nome):
    """Deixa nomes de arquivo longos quebrarem nos _ - . em vez de no meio da palavra."""
    return Markup(re.sub(r"([_.\-])", r"\1<wbr>", str(escape(nome or ""))))


@app.template_filter("dataCurta")
def f_data_curta(v):
    return datetime.fromisoformat(v).strftime("%d/%m/%Y") if v else "—"


@app.context_processor
def inject_globals():
    return {"STATUS_LABELS": STATUS_LABELS, "is_admin": eh_admin(),
            "f_reais": f_reais, "f_duracao": f_duracao, "f_gramas": f_gramas,
            "meu_nome": request.cookies.get("amigo", ""), "impressora": IMPRESSORA,
            "agora_dt": datetime.now(), "versao_estatica": VERSAO_ESTATICA}


def destino_seguro(url):
    """Só permite redirecionar para páginas deste site."""
    ok = url and url.startswith("/") and not url.startswith("//") and "\\" not in url
    return url if ok else url_for("fila")


def marca_admin(cfg=None):
    """Muda quando a senha do administrador ou o link fixo mudam: quem entrou antes disso é desconectado."""
    cfg = cfg or get_settings()
    return (cfg["admin_senha"][-16:] + "." + (cfg.get("admin_link") or "")[-8:])


def eh_admin():
    marca = session.get("admin")
    if not marca:
        return False
    if "marca_admin" not in g:
        g.marca_admin = marca_admin()
    return marca == g.marca_admin


def entrar_como_admin():
    session["admin"] = marca_admin()
    session.permanent = True


# ---- limite de tentativas de senha (por aparelho): 8 erros em 10 minutos bloqueiam por 10 minutos
TENTATIVAS = {}
MAX_TENTATIVAS, JANELA_TENTATIVAS = 8, 600


def quem_pede():
    """Endereço de quem está acessando. Pelo link da internet o pedido chega pelo túnel (127.0.0.1) e o
    endereço verdadeiro vem no cabeçalho que a Cloudflare coloca."""
    if request.remote_addr in ("127.0.0.1", "::1"):
        repassado = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()   # Tailscale Funnel
        return request.headers.get("CF-Connecting-IP") or repassado or request.remote_addr
    return request.remote_addr


def veio_da_internet():
    """Pedido que chegou por um dos túneis (Cloudflare ou Tailscale), e não direto da rede de casa."""
    h = request.headers
    return request.remote_addr in ("127.0.0.1", "::1") and bool(
        h.get("CF-Connecting-IP") or h.get("X-Forwarded-For") or h.get("Tailscale-Funnel-Request"))


def bloqueado():
    agora = time.time()
    erros = [t for t in TENTATIVAS.get(quem_pede(), []) if t > agora - JANELA_TENTATIVAS]
    return len(erros) >= MAX_TENTATIVAS


def errou_a_senha():
    agora = time.time()
    if len(TENTATIVAS) > 5000:
        TENTATIVAS.clear()
    quem = quem_pede()
    TENTATIVAS[quem] = [t for t in TENTATIVAS.get(quem, []) if t > agora - JANELA_TENTATIVAS] + [agora]
    time.sleep(1)  # atrasa quem tenta adivinhar


MUITAS_TENTATIVAS = "Muitas tentativas erradas. Espere 10 minutos e tente de novo."


_SENHA_PADRAO = {}   # conferir uma senha é caro de propósito (~0,1 s); o resultado só muda quando a senha muda


def senha_admin_padrao(cfg=None):
    guardada = (cfg or get_settings())["admin_senha"]
    if guardada not in _SENHA_PADRAO:
        _SENHA_PADRAO.clear()
        _SENHA_PADRAO[guardada] = check_password_hash(guardada, "admin")
    return _SENHA_PADRAO[guardada]


def marca_galera(cfg):
    # muda quando a senha muda, derrubando quem entrou com a senha antiga
    return cfg["senha_galera"][-16:]


@app.before_request
def exigir_senha_galera():
    if request.endpoint in ("static", "entrar", "admin", "admin_login", "admin_qr", "admin_link", "sw") or eh_admin():
        return None
    cfg = get_settings()
    if not cfg["senha_galera"] or session.get("galera") == marca_galera(cfg):
        return None
    return redirect(url_for("entrar", next=request.path))


@app.route("/entrar", methods=["GET", "POST"])
def entrar():
    if request.method == "POST":
        cfg = get_settings()
        if bloqueado():
            flash(MUITAS_TENTATIVAS, "erro")
        elif cfg["senha_galera"] and check_password_hash(cfg["senha_galera"], request.form.get("senha", "")):
            session["galera"] = marca_galera(cfg)
            session.permanent = True
            return redirect(destino_seguro(request.args.get("next")))
        else:
            errou_a_senha()
            flash("Senha incorreta. Peça a senha no grupo.", "erro")
    return render_template("entrar.html")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not eh_admin():
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapper


# muda a cada início do programa: força o navegador a pegar CSS/JS novos
VERSAO_ESTATICA = str(int(time.time()))


def rolos_ativos(db, com_arquivados=False):
    filtro = "" if com_arquivados else "WHERE r.arquivado=0"
    return [dict(r) for r in db.execute(f"""
        SELECT r.*, m.nome AS material_nome, m.preco_kg, m.densidade,
               (SELECT COUNT(*) FROM impressoes i WHERE i.rolo_id=r.id AND i.status IN {FINISHED}
                AND COALESCE(i.eh_lote, 0) = 0) AS impressoes
        FROM rolos r JOIN materiais m ON m.id = r.material_id {filtro}
        ORDER BY r.arquivado, r.codigo""")]


def previsao_rolos(db, fila):
    """Percorre a fila na ordem e marca, por rolo, quem não vai caber no filamento que sobra.
    A impressão atual só é descontada do rolo quando termina; aqui entra o que ainda falta dela."""
    rolos = {r["id"]: r for r in rolos_ativos(db)}
    for r in rolos.values():
        r["sobra"] = r["restante_g"]
        r["fila_precisa"] = 0.0
        r["falta"] = False
    atual = impressao_atual(fila)
    if atual and atual.get("rolo_id") in rolos:
        r = rolos[atual["rolo_id"]]
        gramas = atual["gramas"] or 0
        atual["rolo"] = r
        atual["rolo_sobra_fim"] = r["restante_g"] - gramas
        if gramas > r["restante_g"]:
            atual["rolo_acaba_pct"] = r["restante_g"] / gramas * 100 if gramas else 0
        # o saldo do rolo ainda inclui tudo o que esta peça já gastou (desconta só ao terminar)
        r["sobra"] -= gramas
    for j in fila:
        if j["status"] != "aguardando" or j.get("rolo_id") not in rolos:
            continue
        r = rolos[j["rolo_id"]]
        j["rolo"] = r
        r["fila_precisa"] += j["gramas"] or 0
        if (j["gramas"] or 0) > r["sobra"]:
            j["sem_estoque"] = True
            j["falta_g"] = (j["gramas"] or 0) - max(r["sobra"], 0)
            r["falta"] = True
        r["sobra"] -= j["gramas"] or 0
    return list(rolos.values())


def proximo_codigo(db, material_nome):
    prefixo = re.sub(r"[^A-Z0-9]", "", (material_nome or "ROLO").upper())[:5] or "ROLO"
    n = 1
    while db.execute("SELECT 1 FROM rolos WHERE codigo=?", (f"{prefixo}-{n:03d}",)).fetchone():
        n += 1
    return f"{prefixo}-{n:03d}"


def impressao_atual(fila):
    return next((j for j in fila if j["status"] == "imprimindo"), None)


def andamento(atual):
    """(segundos, gramas) que a impressão em andamento já consumiu de verdade até agora."""
    if not atual or not atual.get("iniciado_em"):
        return 0, 0.0
    decorrido = max(0, (datetime.now() - datetime.fromisoformat(atual["iniciado_em"])).total_seconds())
    return int(decorrido), atual.get("gramas_agora") or 0.0


def totais_do_mes(db):
    """(segundos, gramas) das impressões já finalizadas neste mês."""
    ini = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    r = db.execute(f"""SELECT COALESCE(SUM(tempo_s), 0), COALESCE(SUM(gramas), 0) FROM impressoes
                       WHERE status IN ('concluida','falhou') AND finalizado_em >= ? AND {SEM_LOTE}""",
                   (ini.isoformat(),)).fetchone()
    return r[0], r[1]


def posicao_no_arquivo(job):
    """(byte atual, tamanho) do G-code em impressão. Sem OctoPrint, estima pelo tempo."""
    try:
        tamanho = os.path.getsize(os.path.join(UPLOAD_DIR, job["arquivo_salvo"]))
    except OSError:
        return None, None
    imp = IMPRESSORA
    if job.get("octoprint_arquivo") and imp.get("ocupada") and imp.get("arquivo") == job["octoprint_arquivo"] \
            and imp.get("filepos") is not None:
        return imp["filepos"], tamanho
    return int(tamanho * (job.get("progresso") or 0) / 100), tamanho


# ---------------------------------------------------------------- páginas públicas

@app.route("/")
def fila():
    db = get_db()
    amigos = [r[0] for r in db.execute(f"SELECT DISTINCT amigo FROM impressoes WHERE {SEM_LOTE} ORDER BY amigo")]
    materiais = db.execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY id").fetchall()
    recentes = db.execute("""SELECT i.*, m.nome AS material_nome, r.codigo AS rolo_codigo FROM impressoes i
                             LEFT JOIN materiais m ON m.id = i.material_id
                             LEFT JOIN rolos r ON r.id = i.rolo_id
                             WHERE i.status NOT IN ('aguardando','imprimindo','agrupada') AND COALESCE(i.eh_lote, 0) = 0
                             ORDER BY COALESCE(i.finalizado_em, i.criado_em) DESC LIMIT 10""").fetchall()
    fila = fila_com_previsao()
    rolos = [r for r in previsao_rolos(db, fila) if r["material_id"] in {m["id"] for m in materiais}]
    return render_template("fila.html", fila=fila, amigos=amigos, materiais=materiais, recentes=recentes,
                           rolos=rolos)


@app.route("/sw.js")
def sw():
    """Serviço do aplicativo instalado. Fica na raiz para valer para o site inteiro."""
    resp = send_from_directory(os.path.join(BASE_DIR, "static", "app"), "sw.js", mimetype="text/javascript", max_age=0)
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/app")
def app_inicio():
    """Por onde o aplicativo instalado abre: o dono cai no Administrativo, os amigos na fila."""
    return redirect(url_for("admin") if eh_admin() else url_for("fila"))


@app.route("/status.json")
def status_json():
    """Estado ao vivo para as páginas se atualizarem sozinhas (sem dados sensíveis)."""
    fila = fila_com_previsao()
    atual = impressao_atual(fila)
    imp = IMPRESSORA
    campos = ("configurada", "online", "conectada", "texto", "imprimindo", "pausada", "ocupada", "pronta",
              "temp_bico", "alvo_bico", "temp_mesa", "alvo_mesa")
    dados = {"impressora": {k: imp.get(k) for k in campos}, "fila": [j["id"] for j in fila], "atual": None}
    # horários da fila (mudam conforme a impressão atual anda) e totais do mês contando a impressão em andamento
    dados["horarios"] = {str(j["id"]): [f_data_hora(j["inicio"]).replace(" às", "") if j.get("inicio") else "—",
                                        f_data_hora(j["previsao"]).replace(" às", "")] for j in fila}
    dados["fila_fim"] = f_data_hora(fila[-1]["previsao"]) if fila else None
    t_mes, g_mes = totais_do_mes(get_db())
    t_agora, g_agora = andamento(atual)
    dados["mes"] = {"tempo": f_duracao(t_mes + t_agora), "gramas": f_gramas(g_mes + g_agora),
                    "andamento": f_duracao(t_agora) if atual else None}
    if atual:
        pos, tamanho = posicao_no_arquivo(atual)
        restante = max(0, (atual["previsao"] - datetime.now()).total_seconds())
        dados["atual"] = {"id": atual["id"], "progresso": atual.get("progresso") or 0,
                          "arquivo": atual["arquivo_original"], "amigo": atual["amigo"],
                          "gramas_agora": round(atual.get("gramas_agora") or 0, 2),
                          "gramas_agora_txt": f"{atual.get('gramas_agora') or 0:.1f} g".replace(".", ","),
                          "gramas_total_txt": f_gramas(atual["gramas"]),
                          "restante": f_duracao(restante), "fim": f_data_hora(atual["previsao"]),
                          "filepos": pos, "tamanho": tamanho,
                          "estimado": not (atual.get("octoprint_arquivo") and imp.get("ocupada"))}
    return dados


@app.route("/gcode/<int:pid>.gcode")
def gcode(pid):
    """G-code para o visualizador 3D: o que está imprimindo (todos) ou qualquer um (dono)."""
    row = get_db().execute("SELECT arquivo_salvo, status FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not row or not (eh_admin() or row["status"] == "imprimindo"):
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["arquivo_salvo"], mimetype="text/plain", max_age=3600)


def registrar_envio(db, cfg, nome, arquivo, material, rolo, observacao):
    """Guarda um G-code enviado e coloca na fila. Devolve (id, None) ou (None, motivo da recusa)."""
    if not arquivo.filename.lower().endswith((".gcode", ".gco", ".g")):
        return None, f"“{arquivo.filename}” não é .gcode (exportado do Cura)."
    salvo = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{nome_arquivo_seguro(arquivo.filename)}"
    caminho = os.path.join(UPLOAD_DIR, salvo)
    arquivo.save(caminho)
    info = parse_gcode(caminho)
    if not info["time_s"] and not info["filament_m"] and not info["filament_g"]:
        os.remove(caminho)
        return None, f"Não encontrei tempo nem filamento em “{arquivo.filename}”. Ele foi fatiado no Cura?"
    tempo, gramas, c_mat, c_en, total = calcular_custos(
        info["time_s"], info["filament_m"], info["filament_g"], material, cfg)
    posicao = (db.execute("SELECT MAX(posicao) FROM impressoes").fetchone()[0] or 0) + 1
    cur = db.execute("""INSERT INTO impressoes (amigo, arquivo_original, arquivo_salvo, observacao, material_id,
                    posicao, tempo_fatiador_s, tempo_s, filamento_m, gramas, custo_material, custo_energia, custo_total,
                    altura_camada, maquina, miniatura, criado_em, rolo_id)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (nome, arquivo.filename, salvo, observacao,
                material["id"], posicao, info["time_s"], tempo, info["filament_m"], gramas, c_mat, c_en, total,
                info["layer_height"], info["machine"], info["thumbnail"], datetime.now().isoformat(),
                rolo["id"] if rolo else None))
    db.commit()
    return cur.lastrowid, None


@app.route("/enviar", methods=["POST"])
def enviar():
    nome = normalizar_nome(request.form.get("amigo"))
    arquivos = [a for a in request.files.getlist("gcode") if a and a.filename]
    if not nome:
        flash("Digite seu nome.", "erro")
        return redirect(url_for("fila"))
    if not arquivos:
        flash("Escolha um arquivo .gcode.", "erro")
        return redirect(url_for("fila"))
    if len(arquivos) > MAX_PECAS:
        flash(f"Envie no máximo {MAX_PECAS} arquivos de cada vez.", "erro")
        return redirect(url_for("fila"))

    db = get_db()
    rolo = db.execute("SELECT * FROM rolos WHERE id=? AND arquivado=0", (request.form.get("rolo_id"),)).fetchone()
    material = (db.execute("SELECT * FROM materiais WHERE id=?", (rolo["material_id"],)).fetchone() if rolo else None) \
        or db.execute("SELECT * FROM materiais WHERE id=? AND ativo=1", (request.form.get("material_id"),)).fetchone() \
        or db.execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY id").fetchone()
    cfg = get_settings()
    observacao = (request.form.get("observacao") or "").strip()[:300]

    novos = []
    for arquivo in arquivos:
        pid, erro = registrar_envio(db, cfg, nome, arquivo, material, rolo, observacao)
        if erro:
            flash(erro, "erro")
        else:
            novos.append(pid)
    if not novos:
        return redirect(url_for("fila"))

    # várias peças de uma vez: tenta colocar todas na mesma mesa (se o amigo pediu)
    if len(novos) > 1 and request.form.get("juntar"):
        try:
            novos = [criar_lote(db, novos, cfg)]
            db.commit()
            flash(f"As {len(arquivos)} peças couberam na mesma mesa e vão ser impressas juntas, de uma vez só.", "ok")
        except JuntarErro as e:
            db.rollback()
            flash(f"Não deu para juntar as peças na mesma mesa: {e}. Elas entraram na fila separadas.", "aviso")

    fila_agora = fila_com_previsao()
    previsao_rolos(db, fila_agora)
    for pid in novos:
        j = next((x for x in fila_agora if x["id"] == pid), None)
        if not j:
            continue
        msg = f"“{j['arquivo_original']}” entrou na fila: {f_duracao(j['tempo_s'])}, {f_gramas(j['gramas'])}, {f_reais(j['custo_total'])}."
        if rolo and j.get("sem_estoque"):
            sobra = max(0, (j["gramas"] or 0) - j["falta_g"])
            msg += (f" Atenção: o rolo {rolo['codigo']} vai ter só {f_gramas(sobra)} quando chegar a vez desta peça, "
                    f"que precisa de {f_gramas(j['gramas'])}. Combine com o dono.")
        if j["maquina"] and "ender" not in j["maquina"].lower():
            msg += f" Atenção: foi fatiado para “{j['maquina']}”, não para a Ender 3."
        flash(msg, "ok")
    resp = redirect(url_for("fila"))
    resp.set_cookie("amigo", nome, max_age=60 * 60 * 24 * 365)
    return resp


@app.route("/cancelar/<int:pid>", methods=["POST"])
def cancelar(pid):
    db = get_db()
    row = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    dono = request.cookies.get("amigo", "") == row["amigo"]
    if not (eh_admin() or (dono and row["status"] == "aguardando")):
        abort(403)
    db.execute("UPDATE impressoes SET status='cancelada', finalizado_em=? WHERE id=?",
               (datetime.now().isoformat(), pid))
    propagar_lote(db, pid)
    db.commit()
    flash(f"“{row['arquivo_original']}” foi cancelada.", "ok")
    return redirect(request.referrer or url_for("fila"))


@app.route("/dashboard")
def dashboard():
    db = get_db()
    por_amigo = [dict(r) for r in db.execute(f"""
        SELECT amigo,
          SUM(CASE WHEN status='concluida' THEN 1 ELSE 0 END) AS concluidas,
          SUM(CASE WHEN status='falhou' THEN 1 ELSE 0 END) AS falhas,
          SUM(CASE WHEN status IN {FINISHED} THEN tempo_s ELSE 0 END) AS tempo_s,
          SUM(CASE WHEN status IN {FINISHED} THEN gramas ELSE 0 END) AS gramas,
          SUM(CASE WHEN status IN {FINISHED} AND cobrar=1 THEN custo_material ELSE 0 END) AS custo_material,
          SUM(CASE WHEN status IN {FINISHED} AND cobrar=1 THEN custo_energia ELSE 0 END) AS custo_energia,
          SUM(CASE WHEN status IN {FINISHED} AND cobrar=1 THEN custo_total ELSE 0 END) AS gasto,
          SUM(CASE WHEN status IN {ACTIVE} THEN 1 ELSE 0 END) AS na_fila,
          SUM(CASE WHEN status IN {ACTIVE} THEN custo_total ELSE 0 END) AS previsto,
          SUM(CASE WHEN status='imprimindo' THEN 1 ELSE 0 END) AS imprimindo,
          SUM(CASE WHEN status='imprimindo' THEN custo_total ELSE 0 END) AS em_impressao,
          SUM(CASE WHEN status='imprimindo' THEN gramas ELSE 0 END) AS gramas_impressao,
          SUM(CASE WHEN status='falhou' THEN gramas ELSE 0 END) AS gramas_falhas,
          SUM(CASE WHEN status='falhou' AND cobrar=1 THEN custo_total ELSE 0 END) AS custo_falhas
        FROM {PECAS_SQL} WHERE status != 'cancelada'
        GROUP BY amigo ORDER BY gasto DESC, amigo""")]
    atual = impressao_atual(fila_com_previsao())
    # filamento que já saiu agora, por amigo (numa mesa conjunta, repartido pelo peso de cada peça)
    saindo = {}
    pecas_atuais = partes(db, atual)
    soma_g = sum(p["gramas"] or 0 for p in pecas_atuais) or 1
    for p in pecas_atuais:
        fatia = (p["gramas"] or 0) / soma_g if len(pecas_atuais) > 1 else 1
        saindo[p["amigo"]] = saindo.get(p["amigo"], 0) + (atual.get("gramas_agora") or 0) * fatia
    for a in por_amigo:   # o que está imprimindo agora já entra no total do amigo (marcado como "em impressão")
        a["gasto_total"] = (a["gasto"] or 0) + (a["em_impressao"] or 0)
        a["gramas_agora"] = saindo.get(a["amigo"], 0)
        a["gramas_total"] = (a["gramas"] or 0) + a["gramas_agora"]
    por_amigo.sort(key=lambda a: (-a["gasto_total"], a["amigo"]))

    tot = {k: sum((a[k] or 0) for a in por_amigo)
           for k in ("concluidas", "falhas", "tempo_s", "gramas", "custo_material",
                     "custo_energia", "gasto", "na_fila", "previsto", "imprimindo", "em_impressao",
                     "gramas_impressao", "gasto_total", "gramas_falhas", "custo_falhas", "gramas_agora")}
    tot["atual_id"] = atual["id"] if atual else None
    tot["perdas"] = db.execute(f"""SELECT COALESCE(SUM(custo_total),0) FROM impressoes
                                   WHERE status IN {FINISHED} AND cobrar=0 AND {SEM_LOTE}""").fetchone()[0]
    tot["tempo_fila"] = sum((i["tempo_s"] or 0) for i in fila_com_previsao())

    def ranking(chave):
        itens = sorted((a for a in por_amigo if a[chave]), key=lambda a: a[chave], reverse=True)
        maior = itens[0][chave] if itens else 1
        return [{"amigo": a["amigo"], "valor": a[chave], "pct": a[chave] / maior * 100} for a in itens]

    historico = db.execute(f"""SELECT i.*, m.nome AS material_nome FROM impressoes i
                               LEFT JOIN materiais m ON m.id = i.material_id
                               WHERE i.status IN {FINISHED} AND COALESCE(i.eh_lote, 0) = 0
                               ORDER BY i.finalizado_em DESC LIMIT 100""").fetchall()
    fila_d = fila_com_previsao()
    rolos = previsao_rolos(db, fila_d)
    atual_d = impressao_atual(fila_d)
    for r in rolos:
        r["em_uso"] = bool(atual_d and atual_d.get("rolo_id") == r["id"])
        r["gramas_agora"] = (atual_d.get("gramas_agora") or 0) if r["em_uso"] else 0
    return render_template("dashboard.html", por_amigo=por_amigo, tot=tot, historico=historico, rolos=rolos,
                           rank_tempo=ranking("tempo_s"), rank_gramas=ranking("gramas_total"),
                           rank_gasto=ranking("gasto_total"))


@app.route("/miniatura/<int:pid>.png")
def miniatura(pid):
    import base64
    row = get_db().execute("SELECT miniatura FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not row or not row["miniatura"]:
        abort(404)
    try:
        dados = base64.b64decode(row["miniatura"])
    except Exception:
        abort(404)
    return dados, 200, {"Content-Type": "image/png", "Cache-Control": "max-age=86400"}


# ---------------------------------------------------------------- painel do dono

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        cfg = get_settings()
        if bloqueado():
            flash(MUITAS_TENTATIVAS, "erro")
        elif veio_da_internet() and senha_admin_padrao(cfg):
            # senha de fábrica nunca vale pela internet (nem se o túnel ficar ligado por engano)
            flash("Troque a senha do administrador pelo computador de casa antes de entrar pela internet.", "erro")
        elif check_password_hash(cfg["admin_senha"], request.form.get("senha", "")):
            entrar_como_admin()
            return redirect(destino_seguro(request.args.get("next") or url_for("admin")))
        else:
            errou_a_senha()
            flash("Senha incorreta.", "erro")
    return render_template("admin_login.html")


# ---------------------------------------------------------------- acesso pelo celular

TOKENS_QR = {}  # token -> validade (login do dono por QR Code, uso único)
VALIDADE_QR = 600


def ip_na_rede():
    """IP deste computador na rede de casa (para abrir pelo celular no mesmo Wi-Fi)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 9))  # não envia nada; só descobre a interface de saída
            return s.getsockname()[0]
    except OSError:
        return None


def avisar_celular(topico, titulo, mensagem, link=None):
    """Notificação pelo app ntfy (https://ntfy.sh). O tópico funciona como uma senha: quem souber, recebe."""
    import requests
    cab = {"Title": titulo.encode("utf-8"), "Tags": "printer"}
    if link:
        cab["Click"] = link
    requests.post(f"https://ntfy.sh/{topico}", data=mensagem.encode("utf-8"), headers=cab, timeout=10)


def avisar_link_novo(url):
    topico = ler_config("ntfy_topico")
    if topico:
        avisar_celular(topico, "PrintDeck", f"Link novo: {url}\nToque para abrir o painel do dono.",
                       url + "/admin")


def ler_config(chave):
    db = sqlite3.connect(DB_PATH)
    try:
        row = db.execute("SELECT value FROM settings WHERE key=?", (chave,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


tunel.ao_mudar_link = avisar_link_novo


@app.route("/admin/qr/novo", methods=["POST"])
@admin_required
def admin_qr_novo():
    agora = time.time()
    for t, validade in list(TOKENS_QR.items()):
        if validade < agora:
            del TOKENS_QR[t]
    token = secrets.token_urlsafe(24)
    TOKENS_QR[token] = agora + VALIDADE_QR
    caminho = url_for("admin_qr", token=token)
    ip = ip_na_rede()
    return {"internet": (tunel.url + caminho) if tunel.url else None,
            "casa": f"http://{ip}:{PORTA}{caminho}" if ip else None,
            "minutos": VALIDADE_QR // 60}


@app.route("/admin/qr/<token>")
def admin_qr(token):
    validade = 0 if bloqueado() else TOKENS_QR.pop(token, 0)  # pop: cada código funciona uma vez só
    if validade < time.time():
        errou_a_senha()
        flash("Esse QR Code já foi usado ou expirou. Gere outro no painel do computador.", "erro")
        return redirect(url_for("admin_login"))
    entrar_como_admin()
    flash("Pronto! Este celular agora está no painel do dono. 📱", "ok")
    return redirect(url_for("admin"))


def link_fixo_admin(db, novo=False):
    """Caminho secreto que entra direto no Administrativo. Vale até o dono gerar outro."""
    row = db.execute("SELECT value FROM settings WHERE key='admin_link'").fetchone()
    if novo or not row or not row["value"]:
        token = secrets.token_urlsafe(32)
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('admin_link', ?)", (token,))
        db.commit()
        return token
    return row["value"]


@app.route("/admin/link/novo", methods=["POST"])
@admin_required
def admin_link_novo():
    link_fixo_admin(get_db(), novo=True)
    g.pop("marca_admin", None)
    entrar_como_admin()
    flash("Link de administrador trocado. O link antigo parou de funcionar e os outros aparelhos foram desconectados.", "ok")
    return redirect(url_for("admin") + "#link-admin")


@app.route("/a/<token>")
def admin_link(token):
    row = get_db().execute("SELECT value FROM settings WHERE key='admin_link'").fetchone()
    if bloqueado() or not row or not row["value"] or not secrets.compare_digest(row["value"], token):
        errou_a_senha()
        flash("Esse link de administrador não vale mais.", "erro")
        return redirect(url_for("admin_login"))
    entrar_como_admin()
    return redirect(url_for("admin"))


@app.route("/admin/aplicativo", methods=["POST"])
@admin_required
def salvar_aplicativo():
    ligado = "1" if request.form.get("fechar_minimiza") else "0"
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('fechar_minimiza', ?)", (ligado,))
    db.commit()
    flash("Ao fechar a janela, o programa " + ("vai para a bandeja e continua funcionando." if ligado == "1"
                                                else "é desligado (com um aviso antes)."), "ok")
    return redirect(url_for("admin") + "#aplicativo")


@app.route("/admin/celular", methods=["POST"])
@admin_required
def salvar_celular():
    topico = "" if request.form.get("desligar") else         re.sub(r"[^A-Za-z0-9_-]", "", request.form.get("ntfy_topico", ""))[:64]
    db = get_db()
    db.execute("UPDATE settings SET value=? WHERE key='ntfy_topico'", (topico,))
    db.commit()
    if topico and request.form.get("testar"):
        try:
            avisar_celular(topico, "PrintDeck", "Teste: os avisos estão funcionando! 🎉",
                           (tunel.url + "/admin") if tunel.url else None)
            flash("Aviso de teste enviado. Chegou no celular?", "ok")
        except Exception:
            flash("Não consegui enviar o aviso (sem internet?).", "erro")
    else:
        flash("Avisos no celular " + ("ligados." if topico else "desligados."), "ok")
    return redirect(url_for("admin") + "#celular")


@app.route("/admin/sair")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("fila"))


def indicadores(db, fila, materiais):
    """Números do topo do painel do dono + checagem de estoque de filamento para a fila."""
    espera = [j for j in fila if j["status"] == "aguardando"]
    atual = impressao_atual(fila)
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ini_mes = hoje.replace(day=1)
    ini_ant = (ini_mes - timedelta(days=1)).replace(day=1)
    soma_mes = """SELECT COUNT(*) AS n, COALESCE(SUM(tempo_s), 0) AS tempo, COALESCE(SUM(gramas), 0) AS gramas,
                         COALESCE(SUM(CASE WHEN cobrar=1 THEN custo_total END), 0) AS gasto,
                         COALESCE(SUM(status='falhou'), 0) AS falhas
                  FROM impressoes WHERE status IN ('concluida','falhou') AND finalizado_em >= ? AND finalizado_em < ?
                        AND COALESCE(eh_lote, 0) = 0"""
    mes = db.execute(soma_mes, (ini_mes.isoformat(), (hoje + timedelta(days=1)).isoformat())).fetchone()
    ant = db.execute(soma_mes, (ini_ant.isoformat(), ini_mes.isoformat())).fetchone()

    def variacao(chave):
        return (mes[chave] - ant[chave]) / ant[chave] * 100 if ant[chave] else None

    # horas impressas por dia, últimos 30 dias (concluídas x falhas)
    ini_graf = hoje - timedelta(days=29)
    por_dia = {}
    for r in db.execute("""SELECT substr(finalizado_em, 1, 10) AS dia, status, SUM(tempo_s) AS t FROM impressoes
                           WHERE status IN ('concluida','falhou') AND finalizado_em >= ? AND COALESCE(eh_lote, 0) = 0
                           GROUP BY dia, status""",
                        (ini_graf.isoformat(),)):
        por_dia[(r["dia"], r["status"])] = (r["t"] or 0) / 3600
    dias = [ini_graf + timedelta(days=k) for k in range(30)]
    grafico = {"rotulos": [d.strftime("%d/%m") for d in dias],
               "concluidas": [round(por_dia.get((d.date().isoformat(), "concluida"), 0), 2) for d in dias],
               "falhas": [round(por_dia.get((d.date().isoformat(), "falhou"), 0), 2) for d in dias]}

    amigos_mes = [dict(r) for r in db.execute("""
        SELECT amigo, COUNT(*) AS n, COALESCE(SUM(tempo_s), 0) AS tempo, COALESCE(SUM(gramas), 0) AS gramas,
               COALESCE(SUM(CASE WHEN cobrar=1 THEN custo_total END), 0) AS gasto, COALESCE(SUM(status='falhou'), 0) AS falhas
        FROM impressoes WHERE status IN ('concluida','falhou') AND finalizado_em >= ? AND COALESCE(eh_lote, 0) = 0
        GROUP BY amigo ORDER BY gasto DESC, n DESC""", (ini_mes.isoformat(),))]
    na_fila_por_amigo = {}
    for j in fila:
        if j["status"] == "aguardando":
            for p in partes(db, j):
                na_fila_por_amigo[p["amigo"]] = na_fila_por_amigo.get(p["amigo"], 0) + 1
    for p in partes(db, atual):   # a impressão em andamento aparece na lista dos amigos do mês
        a = next((x for x in amigos_mes if x["amigo"] == p["amigo"]), None)
        if not a:
            a = {"amigo": p["amigo"], "n": 0, "tempo": 0, "gramas": 0, "gasto": 0, "falhas": 0}
            amigos_mes.append(a)
        a["imprimindo"] = a.get("imprimindo", 0) + (p["custo_total"] or 0)
    if atual:
        amigos_mes.sort(key=lambda x: -((x["gasto"] or 0) + x.get("imprimindo", 0)))
    t_agora, g_agora = andamento(atual)
    mes = dict(mes)
    mes["tempo"] += t_agora
    mes["gramas"] += g_agora
    rolos = previsao_rolos(db, fila)
    estoque = [{"nome": r["codigo"] + (f" · {r['descricao']}" if r["descricao"] else ""), "estoque": r["restante_g"],
                "precisa": r["fila_precisa"], "falta": r["falta"], "rolo": r} for r in rolos]
    return {"espera": len(espera), "fila_tempo": sum(j["tempo_s"] or 0 for j in espera),
            "fila_gramas": sum(j["gramas"] or 0 for j in espera),
            "fila_custo": sum(j["custo_total"] or 0 for j in espera),
            "fila_fim": fila[-1]["previsao"] if fila else None,
            "mes": dict(mes), "mes_anterior": dict(ant), "estoque": estoque, "rolos": rolos,
            "imprimindo_custo": (atual["custo_total"] or 0) if atual else 0,
            "andamento_tempo": t_agora, "andamento_gramas": g_agora,
            "variacao": {k: variacao(k) for k in ("n", "tempo", "gramas", "gasto")},
            "grafico": grafico, "amigos_mes": amigos_mes, "na_fila_por_amigo": na_fila_por_amigo,
            "nome_mes": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
                         "outubro", "novembro", "dezembro"][hoje.month - 1]}


def central_atencao(cfg, painel, fila):
    """Pendências que pedem ação do dono, da mais grave para a mais leve."""
    itens = []
    imp = IMPRESSORA
    if senha_admin_padrao(cfg):
        itens.append(("grave", "Senha do dono ainda é a padrão", "Troque em Configurações → Custos e senhas.", "config"))
    if imp.get("configurada") and imp.get("online") is False:
        itens.append(("grave", "OctoPrint sem contato", imp.get("erro") or "O Raspberry está ligado?", "config"))
    elif imp.get("configurada") and not imp.get("conectada"):
        itens.append(("grave", "Impressora desconectada do OctoPrint", "Ligue a Ender 3 e clique em Conectar impressora.", "geral"))
    atual = impressao_atual(fila)
    if atual and atual.get("rolo_acaba_pct") is not None:
        itens.insert(0, ("grave", f"O rolo {atual['rolo']['codigo']} vai acabar durante a impressão atual",
                         f"Tem {f_gramas(atual['rolo']['restante_g'])}; a peça precisa de {f_gramas(atual['gramas'])} "
                         f"(acaba em ~{atual['rolo_acaba_pct']:.0f}%). Deixe outro rolo à mão.", "geral"))
    for e in painel["estoque"]:
        if e["falta"]:
            itens.append(("aviso", f"Rolo {e['rolo']['codigo']} não dá para a fila",
                          f"Tem {f_gramas(e['estoque'])} · a fila precisa de {f_gramas(e['precisa'])}.", "materiais"))
    for j in fila:
        if j["status"] == "aguardando" and j["maquina"] and "ender" not in j["maquina"].lower():
            itens.append(("aviso", f"“{j['arquivo_original']}” foi fatiado para outra impressora",
                          f"Máquina no arquivo: {j['maquina']} · de {j['amigo']}.", "geral"))
    if tunel.erro and not tunel.ligado and cfg.get("internet_ligada") == "1":
        itens.append(("aviso", "O acesso pela internet caiu", tunel.erro, "config"))
    if tunel.ligado and not cfg.get("senha_galera"):
        itens.append(("aviso", "Link público sem senha da galera", "Qualquer pessoa com o link pode mandar arquivos.", "config"))
    return itens


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    cfg = get_settings()
    fila = fila_com_previsao()
    materiais = db.execute("SELECT * FROM materiais ORDER BY id").fetchall()
    painel = indicadores(db, fila, materiais)
    return render_template("admin.html", fila=fila, cfg=cfg, painel=painel, materiais=materiais,
                           atencao=central_atencao(cfg, painel, fila),
                           finalizadas=db.execute("""SELECT i.*, r.codigo AS rolo_codigo FROM impressoes i
                                                     LEFT JOIN rolos r ON r.id = i.rolo_id
                                                     WHERE i.status NOT IN ('aguardando','imprimindo','agrupada')
                                                       AND COALESCE(i.eh_lote, 0) = 0
                                                     ORDER BY COALESCE(i.finalizado_em, i.criado_em) DESC
                                                     LIMIT 50""").fetchall(),
                           feitas=db.execute("""SELECT i.*, r.codigo AS rolo_codigo FROM impressoes i
                                                LEFT JOIN rolos r ON r.id = i.rolo_id
                                                WHERE i.status IN ('concluida', 'falhou') AND COALESCE(i.eh_lote, 0) = 0
                                                ORDER BY i.finalizado_em DESC LIMIT 8""").fetchall(),
                           todos_rolos=rolos_ativos(db, com_arquivados=True),
                           extrato={r["id"]: db.execute("""SELECT * FROM movimentos_rolo WHERE rolo_id=?
                                                             ORDER BY id DESC LIMIT 12""", (r["id"],)).fetchall()
                                    for r in rolos_ativos(db, com_arquivados=True)},
                           sugestao_codigo={m["id"]: proximo_codigo(db, m["nome"]) for m in materiais},
                           senha_padrao=senha_admin_padrao(cfg), tunel=tunel,
                           tailscale="pronto" if tunel.fixo else situacao_tailscale(),
                           sugestao_topico="impressora-" + secrets.token_hex(5),
                           link_admin=url_for("admin_link", token=link_fixo_admin(db)), ip_casa=ip_na_rede(), porta=PORTA,
                           tem_senha_galera=bool(cfg["senha_galera"]))


@app.route("/admin/baixar/<int:pid>")
@admin_required
def baixar(pid):
    row = get_db().execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    # nome curto e sem acentos para aparecer bem no display da Ender 3
    nome = nome_arquivo_seguro(f"{row['id']:03d}_{row['amigo']}_{row['arquivo_original']}")
    return send_from_directory(UPLOAD_DIR, row["arquivo_salvo"], as_attachment=True, download_name=nome)


@app.route("/admin/status/<int:pid>", methods=["POST"])
@admin_required
def mudar_status(pid):
    novo = request.form.get("status")
    if novo not in STATUS_LABELS:
        abort(400)
    db = get_db()
    antes = db.execute("SELECT status, grupo_id FROM impressoes WHERE id=?", (pid,)).fetchone()
    if antes and antes["grupo_id"] and novo in ACTIVE:
        # peça de uma mesa conjunta que já terminou volta sozinha para a fila, com o custo cheio de novo
        db.execute("UPDATE impressoes SET grupo_id=NULL, falhou_em_pct=NULL, cobrar=1 WHERE id=?", (pid,))
        db.execute("UPDATE impressoes SET status='aguardando' WHERE id=?", (pid,))
        db.commit()
        recalcular_ativos()
    antes = antes["status"] if antes else None
    agora = datetime.now().isoformat()
    sem_octoprint = "octoprint_arquivo=NULL, progresso_pct=NULL, restante_s=NULL"
    if novo == "imprimindo":  # marcado à mão (impressão pelo microSD)
        db.execute(f"""UPDATE impressoes SET status='imprimindo', iniciado_em=?, finalizado_em=NULL,
                       {sem_octoprint} WHERE id=?""", (agora, pid))
    elif novo == "aguardando":
        db.execute(f"""UPDATE impressoes SET status='aguardando', iniciado_em=NULL, finalizado_em=NULL,
                       {sem_octoprint} WHERE id=?""", (pid,))
    else:
        cobrar = 0 if novo == "cancelada" else 1
        db.execute("UPDATE impressoes SET status=?, finalizado_em=?, cobrar=? WHERE id=?",
                   (novo, agora, cobrar, pid))
    job = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if antes == "imprimindo" and novo in ("falhou", "cancelada") and job and job["filepos_final"]:
        # parou no meio: vale o filamento que chegou a sair, calculado pelo G-code
        gramas = gramas_ate(job, job["filepos_final"], db)
        if gramas is not None:
            preco = db.execute("SELECT preco_kg FROM materiais WHERE id=?", (job["material_id"],)).fetchone()
            c_mat = gramas / 1000 * (preco["preco_kg"] if preco else 0)
            db.execute("UPDATE impressoes SET gramas=?, custo_material=?, custo_total=?+COALESCE(custo_energia,0) WHERE id=?",
                       (gramas, c_mat, c_mat, pid))
    acertar_consumo(db, pid)
    propagar_lote(db, pid)
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/juntar", methods=["POST"])
@admin_required
def admin_juntar():
    """Junta as peças marcadas na fila em uma mesa só (um G-code, camada por camada)."""
    db = get_db()
    ids = request.form.getlist("ids", type=int)
    try:
        lote_id = criar_lote(db, ids, get_settings())
    except JuntarErro as e:
        db.rollback()
        flash(f"Não deu para juntar: {e}. As peças continuam separadas na fila.", "aviso")
        return redirect(url_for("admin") + "#fila")
    db.commit()
    lote = db.execute("SELECT * FROM impressoes WHERE id=?", (lote_id,)).fetchone()
    flash(f"{len(ids)} peças juntadas em uma mesa só: {f_duracao(lote['tempo_s'])}, {f_gramas(lote['gramas'])}. "
          "Confira o desenho da mesa na fila antes de imprimir.", "ok")
    return redirect(url_for("admin") + "#fila")


@app.route("/admin/desjuntar/<int:pid>", methods=["POST"])
@admin_required
def admin_desjuntar(pid):
    db = get_db()
    lote = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not lote or not lote["eh_lote"] or lote["status"] != "aguardando":
        flash("Só dá para desfazer uma mesa conjunta que ainda está aguardando.", "erro")
        return redirect(url_for("admin"))
    desfazer_lote(db, lote)
    db.commit()
    flash("Mesa desfeita: as peças voltaram a ser pedidos separados na fila.", "ok")
    return redirect(url_for("admin") + "#fila")


@app.route("/admin/reimprimir/<int:pid>", methods=["POST"])
@admin_required
def reimprimir(pid):
    """Coloca de novo na fila uma impressão que já terminou, com os preços de hoje.
    Com agora=1 ela entra em primeiro e já é mandada para a impressora (um clique só)."""
    import shutil
    db = get_db()
    job = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not job:
        abort(404)
    origem = os.path.join(UPLOAD_DIR, job["arquivo_salvo"])
    if not os.path.exists(origem):
        flash("O arquivo dessa impressão não existe mais.", "erro")
        return redirect(url_for("admin"))
    # cópia própria: excluir uma das duas depois não apaga o arquivo da outra
    salvo = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{nome_arquivo_seguro(job['arquivo_original'])}"
    shutil.copyfile(origem, os.path.join(UPLOAD_DIR, salvo))
    material = db.execute("SELECT * FROM materiais WHERE id=?", (job["material_id"],)).fetchone() \
        or db.execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY id").fetchone()
    gramas_fatiador = None if job["filamento_m"] else job["gramas"]
    tempo, gramas, c_mat, c_en, total = calcular_custos(
        job["tempo_fatiador_s"], job["filamento_m"], gramas_fatiador, material, get_settings())
    if job["status"] == "falhou" and job["falhou_em_pct"] is not None and job["filamento_m"] is None:
        gramas = job["gramas"] / max(job["falhou_em_pct"], 1) * 100  # gramas originais, sem o desconto da falha
        c_mat = gramas / 1000 * material["preco_kg"]
        total = c_mat + c_en
    agora = bool(request.form.get("agora"))
    if agora:
        posicao = (db.execute("SELECT MIN(posicao) FROM impressoes WHERE status='aguardando'").fetchone()[0] or 1) - 1
    else:
        posicao = (db.execute("SELECT MAX(posicao) FROM impressoes").fetchone()[0] or 0) + 1
    cur = db.execute("""INSERT INTO impressoes (amigo, arquivo_original, arquivo_salvo, observacao, material_id,
                    posicao, tempo_fatiador_s, tempo_s, filamento_m, gramas, custo_material, custo_energia,
                    custo_total, altura_camada, maquina, miniatura, criado_em, rolo_id)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (job["amigo"], job["arquivo_original"], salvo, job["observacao"], material["id"], posicao,
                job["tempo_fatiador_s"], tempo, job["filamento_m"], gramas, c_mat, c_en, total,
                job["altura_camada"], job["maquina"], job["miniatura"], datetime.now().isoformat(), job["rolo_id"]))
    db.commit()
    nome = job["observacao"] or job["arquivo_original"]
    if agora:
        novo = db.execute("SELECT * FROM impressoes WHERE id=?", (cur.lastrowid,)).fetchone()
        erro = enviar_ao_octoprint(db, novo) if cliente_octoprint(get_settings()) else "sem OctoPrint configurado"
        if erro:
            flash(f"“{nome}” ficou em primeiro na fila, mas não começou a imprimir: {erro}", "aviso")
        else:
            flash(f"🖨️ “{nome}” de {job['amigo']} está imprimindo de novo!", "ok")
        return redirect(url_for("admin"))
    flash(f"🔁 “{nome}” de {job['amigo']} voltou para o fim da fila.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/cobrar/<int:pid>", methods=["POST"])
@admin_required
def alternar_cobrar(pid):
    db = get_db()
    db.execute("UPDATE impressoes SET cobrar = 1 - cobrar WHERE id=?", (pid,))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/mover/<int:pid>/<direcao>", methods=["POST"])
@admin_required
def mover(pid, direcao):
    db = get_db()
    atual = db.execute("SELECT id, posicao FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not atual:
        abort(404)
    op, ordem = ("<", "DESC") if direcao == "cima" else (">", "ASC")
    vizinho = db.execute(f"""SELECT id, posicao FROM impressoes WHERE status='aguardando'
                             AND posicao {op} ? ORDER BY posicao {ordem} LIMIT 1""",
                         (atual["posicao"],)).fetchone()
    if vizinho:
        db.execute("UPDATE impressoes SET posicao=? WHERE id=?", (vizinho["posicao"], atual["id"]))
        db.execute("UPDATE impressoes SET posicao=? WHERE id=?", (atual["posicao"], vizinho["id"]))
        db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/excluir/<int:pid>", methods=["POST"])
@admin_required
def excluir(pid):
    db = get_db()
    row = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if row and row["eh_lote"]:
        flash("Para tirar uma mesa conjunta da fila, use “Desfazer mesa” ou cancele o pedido.", "erro")
        return redirect(url_for("admin"))
    if row:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["arquivo_salvo"]))
        except FileNotFoundError:
            pass
        db.execute("UPDATE impressoes SET status='aguardando' WHERE id=?", (pid,))
        acertar_consumo(db, pid, motivo=f"impressão #{pid} excluída do histórico")
        db.execute("UPDATE movimentos_rolo SET impressao_id=NULL WHERE impressao_id=?", (pid,))
        db.execute("DELETE FROM impressoes WHERE id=?", (pid,))
        db.commit()
    flash("Impressão excluída.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/config", methods=["POST"])
@admin_required
def salvar_config():
    db = get_db()
    for chave in ("diametro_mm", "potencia_w", "tarifa_kwh", "fator_tempo"):
        valor = (request.form.get(chave) or "").replace(",", ".").strip()
        try:
            float(valor)
        except ValueError:
            flash(f"Valor inválido em {chave}.", "erro")
            return redirect(url_for("admin"))
        db.execute("UPDATE settings SET value=? WHERE key=?", (valor, chave))
    nova = request.form.get("nova_senha", "").strip()
    if nova:
        if nova == "admin" or len(nova) < 6:
            flash("A senha do dono precisa ter pelo menos 6 caracteres.", "erro")
            return redirect(url_for("admin"))
        db.execute("UPDATE settings SET value=? WHERE key='admin_senha'", (generate_password_hash(nova),))
        db.commit()
        g.pop("marca_admin", None)
        entrar_como_admin()
    galera = request.form.get("senha_galera", "").strip()
    if request.form.get("remover_senha_galera"):
        db.execute("UPDATE settings SET value='' WHERE key='senha_galera'")
    elif galera:
        db.execute("UPDATE settings SET value=? WHERE key='senha_galera'", (generate_password_hash(galera),))
    db.commit()
    recalcular_ativos()
    flash("Configurações salvas. Custos da fila recalculados.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/internet", methods=["POST"])
@admin_required
def internet():
    db = get_db()
    if request.form.get("acao") == "ligar":
        if senha_admin_padrao():
            flash("Troque a senha do dono antes de ligar o acesso pela internet.", "erro")
            return redirect(url_for("admin"))
        modo = request.form.get("link_modo")
        if modo in ("auto", "cloudflare"):
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('link_modo', ?)", (modo,))
            db.commit()
            if tunel.ligado:      # trocando de tipo de link com ele no ar
                tunel.desligar()
        ligar_internet()
        db.execute("UPDATE settings SET value='1' WHERE key='internet_ligada'")
    else:
        tunel.desligar()
        db.execute("UPDATE settings SET value='0' WHERE key='internet_ligada'")
    db.commit()
    return redirect(url_for("admin") + "#internet")


@app.route("/admin/internet/status")
@admin_required
def internet_status():
    return {"ligado": tunel.ligado, "url": tunel.url, "erro": tunel.erro}


@app.route("/admin/octoprint/config", methods=["POST"])
@admin_required
def octoprint_config():
    db = get_db()
    url = request.form.get("octoprint_url", "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    # campo da chave vazio = manter a atual (ela nunca é mostrada de volta na página)
    chave = request.form.get("octoprint_api_key", "").strip() or get_settings()["octoprint_api_key"]
    for k, v in (("octoprint_url", url), ("octoprint_api_key", chave),
                 ("octoprint_webcam", request.form.get("octoprint_webcam", "").strip())):
        db.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
    db.commit()
    if url and chave:
        try:
            v = OctoPrint(url, chave).versao()
            flash(f"Conectado ao OctoPrint {v.get('server', '')}! 🎉", "ok")
        except OctoPrintErro as e:
            flash(str(e), "erro")
    sincronizar_octoprint()
    return redirect(url_for("admin") + "#impressora")


def enviar_ao_octoprint(db, job):
    """Manda a peça para o OctoPrint e marca como imprimindo. Devolve o motivo (texto) se não der."""
    if db.execute("SELECT 1 FROM impressoes WHERE status='imprimindo'").fetchone():
        return "Já tem uma impressão marcada como “Imprimindo”. Finalize ela antes."
    cliente = cliente_octoprint(get_settings())
    if not cliente:
        return "Configure o OctoPrint primeiro."
    try:
        st = cliente.estado()
        if not st["pronta"]:
            return f"A impressora não está pronta ({st['texto']})."
        nome = nome_arquivo_seguro(f"{job['id']:03d}_{job['amigo']}_{job['arquivo_original']}")
        nome_octo = cliente.enviar_e_imprimir(os.path.join(UPLOAD_DIR, job["arquivo_salvo"]), nome)
    except OctoPrintErro as e:
        return str(e)
    db.execute("""UPDATE impressoes SET status='imprimindo', iniciado_em=?, finalizado_em=NULL,
                  octoprint_arquivo=?, progresso_pct=0, restante_s=NULL WHERE id=?""",
               (datetime.now().isoformat(), nome_octo, job["id"]))
    db.commit()
    return None


@app.route("/admin/imprimir/<int:pid>", methods=["POST"])
@admin_required
def imprimir(pid):
    db = get_db()
    job = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    if not job or job["status"] != "aguardando":
        flash("Essa impressão não está aguardando.", "erro")
        return redirect(url_for("admin"))
    erro = enviar_ao_octoprint(db, job)
    if erro:
        flash(erro, "erro")
    else:
        flash(f"🖨️ “{job['arquivo_original']}” enviada ao OctoPrint e imprimindo!", "ok")
    return redirect(url_for("admin"))


# depois de cancelar: sobe o bico (para não arrastar na peça) e leva X e Y para o início.
# O Z não desce para o início de propósito: com uma peça na mesa, o cabeçote poderia bater nela.
HOME_APOS_CANCELAR = ["G91", "G1 Z10 F600", "G90", "G28 X Y"]


def home_apos_cancelar(cliente, espera_max=90):
    """Espera o OctoPrint terminar de cancelar e manda a impressora para o início (auto home)."""
    def tarefa():
        fim = time.time() + espera_max
        while time.time() < fim:
            time.sleep(1.5)
            try:
                st = cliente.estado()
                if not st["conectada"]:
                    return
                if not st["ocupada"]:
                    cliente.gcode(HOME_APOS_CANCELAR)
                    return
            except OctoPrintErro as e:
                print("Auto home depois de cancelar não foi enviado:", e, flush=True)
                return
    threading.Thread(target=tarefa, daemon=True).start()


@app.route("/admin/octoprint/<acao>", methods=["POST"])
@admin_required
def octoprint_acao(acao):
    comandos = {"pausar": ("pause", "pause"), "retomar": ("pause", "resume"), "cancelar": ("cancel", None)}
    mensagens = {"pausar": "Pausando…", "retomar": "Retomando…", "cancelar": "Cancelando a impressão… em seguida o bico sobe e a impressora volta para o início (auto home).",
                 "conectar": "Conectando à impressora…"}
    cliente = cliente_octoprint(get_settings())
    if not cliente or acao not in mensagens:
        abort(400)
    try:
        if acao == "conectar":
            cliente.conectar()
        else:
            cliente.comando(*comandos[acao])
            if acao == "cancelar":
                home_apos_cancelar(cliente)
        flash(mensagens[acao], "ok")
    except OctoPrintErro as e:
        flash(str(e), "erro")
    time.sleep(1)
    sincronizar_octoprint()
    return redirect(url_for("admin") + "#impressora")


@app.route("/admin/octoprint/estado")
@admin_required
def octoprint_estado():
    return IMPRESSORA


@app.route("/admin/webcam.jpg")
@admin_required
def webcam():
    cfg = get_settings()
    cliente = cliente_octoprint(cfg)
    foto = cliente.foto_webcam(cfg["octoprint_webcam"] or None) if cliente else None
    if not foto:
        abort(404)
    return foto[0], 200, {"Content-Type": foto[1], "Cache-Control": "no-store"}


def _num(texto, padrao=None):
    texto = (texto or "").replace(",", ".").strip()
    try:
        return float(texto) if texto else padrao
    except ValueError:
        return None


@app.route("/admin/rolo/novo", methods=["POST"])
@admin_required
def rolo_novo():
    db = get_db()
    material = db.execute("SELECT * FROM materiais WHERE id=?", (request.form.get("material_id"),)).fetchone()
    peso = _num(request.form.get("peso_inicial_g"), 1000)
    usado = _num(request.form.get("usado_fora_g"), 0)
    codigo = (request.form.get("codigo") or "").strip().upper()[:24] or (material and proximo_codigo(db, material["nome"]))
    if not material or peso is None or usado is None or peso <= 0:
        flash("Confira o material, o peso e o que já foi usado.", "erro")
        return redirect(url_for("admin") + "#rolos")
    if db.execute("SELECT 1 FROM rolos WHERE codigo=?", (codigo,)).fetchone():
        flash(f"Já existe um rolo com o código {codigo}.", "erro")
        return redirect(url_for("admin") + "#rolos")
    agora = datetime.now().isoformat()
    cur = db.execute("""INSERT INTO rolos (codigo, descricao, material_id, peso_inicial_g, restante_g, criado_em)
                        VALUES (?,?,?,?,?,?)""",
                     (codigo, (request.form.get("descricao") or "").strip()[:60], material["id"], peso, peso, agora))
    db.execute("INSERT INTO movimentos_rolo (rolo_id, gramas, motivo, criado_em) VALUES (?,?,?,?)",
               (cur.lastrowid, peso, "rolo novo", agora))
    if usado:
        db.execute("INSERT INTO movimentos_rolo (rolo_id, gramas, motivo, criado_em) VALUES (?,?,?,?)",
                   (cur.lastrowid, -usado, "usado fora do sistema", agora))
        db.execute("UPDATE rolos SET restante_g = restante_g - ? WHERE id=?", (usado, cur.lastrowid))
    db.commit()
    flash(f"Rolo {codigo} cadastrado.", "ok")
    return redirect(url_for("admin") + "#rolos")


@app.route("/admin/rolo/<int:rid>", methods=["POST"])
@admin_required
def rolo_salvar(rid):
    db = get_db()
    rolo = db.execute("SELECT * FROM rolos WHERE id=?", (rid,)).fetchone()
    if not rolo:
        abort(404)
    if request.form.get("acao") == "arquivar":
        db.execute("UPDATE rolos SET arquivado = 1 - arquivado WHERE id=?", (rid,))
        db.commit()
        flash(f"Rolo {rolo['codigo']} {'arquivado' if not rolo['arquivado'] else 'reativado'}.", "ok")
        return redirect(url_for("admin") + "#rolos")
    codigo = (request.form.get("codigo") or rolo["codigo"]).strip().upper()[:24]
    if codigo != rolo["codigo"] and db.execute("SELECT 1 FROM rolos WHERE codigo=?", (codigo,)).fetchone():
        flash(f"Já existe um rolo com o código {codigo}.", "erro")
        return redirect(url_for("admin") + "#rolos")
    db.execute("UPDATE rolos SET codigo=?, descricao=? WHERE id=?",
               (codigo, (request.form.get("descricao") or "").strip()[:60], rid))
    pesado = _num(request.form.get("restante_g"))
    if pesado is None and (request.form.get("restante_g") or "").strip():
        flash("Peso inválido (use gramas, ex.: 850).", "erro")
        return redirect(url_for("admin") + "#rolos")
    if pesado is not None and abs(pesado - rolo["restante_g"]) >= 0.05:
        delta = pesado - rolo["restante_g"]
        db.execute("INSERT INTO movimentos_rolo (rolo_id, gramas, motivo, criado_em) VALUES (?,?,?,?)",
                   (rid, delta, "ajuste manual (pesagem)", datetime.now().isoformat()))
        db.execute("UPDATE rolos SET restante_g = ? WHERE id=?", (pesado, rid))
    db.commit()
    flash(f"Rolo {codigo} salvo.", "ok")
    return redirect(url_for("admin") + "#rolos")


@app.route("/admin/impressao/<int:pid>/rolo", methods=["POST"])
@admin_required
def trocar_rolo(pid):
    db = get_db()
    job = db.execute("SELECT * FROM impressoes WHERE id=?", (pid,)).fetchone()
    rolo = db.execute("SELECT * FROM rolos WHERE id=?", (request.form.get("rolo_id"),)).fetchone()
    if not job or not rolo:
        abort(404)
    db.execute("UPDATE impressoes SET rolo_id=?, material_id=? WHERE id=?", (rolo["id"], rolo["material_id"], pid))
    acertar_consumo(db, pid, motivo=f"impressão #{pid} passou para o rolo {rolo['codigo']}")
    if job["grupo_id"]:   # mesa conjunta: todas as peças saíram do mesmo rolo, então muda o lote inteiro
        db.execute("UPDATE impressoes SET rolo_id=?, material_id=? WHERE id=? OR grupo_id=?",
                   (rolo["id"], rolo["material_id"], job["grupo_id"], job["grupo_id"]))
        acertar_consumo(db, job["grupo_id"], motivo=f"mesa conjunta #{job['grupo_id']} passou para o rolo {rolo['codigo']}")
    db.commit()
    flash(f"“{job['arquivo_original']}” agora está no rolo {rolo['codigo']}.", "ok")
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/material", methods=["POST"])
@admin_required
def salvar_material():
    db = get_db()
    try:
        preco = float(request.form["preco_kg"].replace(",", "."))
        dens = float(request.form["densidade"].replace(",", "."))
    except (KeyError, ValueError):
        flash("Preço ou densidade inválidos.", "erro")
        return redirect(url_for("admin"))
    nome = request.form.get("nome", "").strip()[:40]
    mid = request.form.get("id")
    ativo = 1 if request.form.get("ativo") else 0
    texto_estoque = (request.form.get("estoque_g") or "").replace(",", ".").strip()
    try:
        estoque = float(texto_estoque) if texto_estoque else None  # vazio = não controlar
    except ValueError:
        flash("Estoque inválido (use gramas, ex.: 850).", "erro")
        return redirect(url_for("admin"))
    if mid:
        db.execute("UPDATE materiais SET nome=?, preco_kg=?, densidade=?, ativo=?, estoque_g=? WHERE id=?",
                   (nome, preco, dens, ativo, estoque, mid))
    elif nome:
        db.execute("INSERT INTO materiais (nome, preco_kg, densidade, estoque_g) VALUES (?,?,?,?)",
                   (nome, preco, dens, estoque))
    db.commit()
    recalcular_ativos()
    flash("Material salvo.", "ok")
    return redirect(url_for("admin"))


init_db()

def copia_de_seguranca(manter=14):
    """Uma cópia do banco por dia na pasta do programa (que está no OneDrive): os dados continuam protegidos."""
    import shutil
    pasta = os.path.join(BASE_DIR, "copias_de_seguranca")
    destino = os.path.join(pasta, f"fila_{datetime.now():%Y-%m-%d}.db")
    try:
        os.makedirs(pasta, exist_ok=True)
        if not os.path.exists(destino):
            origem = sqlite3.connect(DB_PATH)
            copia = sqlite3.connect(destino)
            origem.backup(copia)
            copia.close()
            origem.close()
        for velho in sorted(f for f in os.listdir(pasta) if f.startswith("fila_") and f.endswith(".db"))[:-manter]:
            os.remove(os.path.join(pasta, velho))
    except (OSError, sqlite3.Error) as e:
        print("Não consegui fazer a cópia de segurança:", e, flush=True)


def ja_esta_rodando(porta):
    # no Windows, dois programas conseguem ocupar a mesma porta; o antigo continuaria respondendo
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", porta)) == 0


if __name__ == "__main__":
    if ja_esta_rodando(PORTA):
        print(f"\n  O programa já está aberto em outra janela (http://localhost:{PORTA}).")
        print("  Feche a outra janela antes de abrir de novo.\n")
        raise SystemExit(1)
    with app.app_context():
        cfg = get_settings()
        if cfg["internet_ligada"] == "1" and not senha_admin_padrao(cfg):
            ligar_internet()
            print("  Ligando acesso pela internet... o link aparece aqui e no Administrativo.")
    iniciar_sincronizacao()
    # host 0.0.0.0 = acessível por outros aparelhos da rede
    app.run(host="0.0.0.0", port=PORTA, debug=False, threaded=True)
