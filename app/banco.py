"""Base SQLite: esquema, gravação de atos, temas e metadados."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS atos (
    id INTEGER PRIMARY KEY, situacao TEXT, tipo TEXT, tipo_ato TEXT,
    numero TEXT, numero_seq TEXT, ano INTEGER, origem TEXT, documento TEXT,
    assunto TEXT, palavras_chave TEXT, ementa TEXT, data_diario TEXT,
    diario TEXT, fonte TEXT, anexos TEXT, referencias TEXT,
    pasta_situacao TEXT, arquivo TEXT, caracteres_texto INTEGER, texto TEXT,
    inclusao TEXT,          -- 'inicial' (carga enviada) ou 'diario' (atualização automática)
    incluido_em TEXT, atualizado_em TEXT,
    situacao_lista TEXT     -- situação que a listagem do site mostrou por último (pode divergir do detalhe)
);
CREATE TABLE IF NOT EXISTS palavras_chave (id INTEGER, ordem INTEGER, termo TEXT);
CREATE TABLE IF NOT EXISTS temas (codigo TEXT PRIMARY KEY, nome TEXT, descricao TEXT, ordem INTEGER);
CREATE TABLE IF NOT EXISTS ato_temas (
    id INTEGER, tema TEXT, origem TEXT, classificado_em TEXT,
    PRIMARY KEY (id, tema)
);
CREATE TABLE IF NOT EXISTS importacoes (arquivo TEXT PRIMARY KEY, versao TEXT, importado_em TEXT, resumo TEXT);
CREATE TABLE IF NOT EXISTS metadados (chave TEXT PRIMARY KEY, valor TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS atos_fts USING fts5(
    documento, assunto, ementa, texto,
    content='atos', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS ix_atos_origem ON atos(origem);
CREATE INDEX IF NOT EXISTS ix_atos_ano ON atos(ano);
CREATE INDEX IF NOT EXISTS ix_atos_tipo ON atos(tipo);
CREATE INDEX IF NOT EXISTS ix_atos_situacao ON atos(situacao);
CREATE INDEX IF NOT EXISTS ix_atos_data ON atos(data_diario);
CREATE INDEX IF NOT EXISTS ix_pc_id ON palavras_chave(id);
CREATE INDEX IF NOT EXISTS ix_pc_termo ON palavras_chave(termo);
CREATE INDEX IF NOT EXISTS ix_at_tema ON ato_temas(tema);
"""

COLUNAS = [
    "id", "situacao", "tipo", "tipo_ato", "numero", "numero_seq", "ano", "origem",
    "documento", "assunto", "palavras_chave", "ementa", "data_diario", "diario",
    "fonte", "anexos", "referencias", "pasta_situacao", "arquivo", "caracteres_texto", "texto",
]


SEM_SITUACAO = "SEM SITUAÇÃO DEFINIDA"


def normalizar_situacao(valor) -> str:
    """O site devolve vazio, None ou "-1" para atos sem situação; todos viram SEM SITUAÇÃO DEFINIDA."""
    v = str(valor or "").strip().upper()
    return SEM_SITUACAO if v in ("", "-1", "NONE") else v


def agora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def abrir(caminho: Path) -> sqlite3.Connection:
    con = sqlite3.connect(caminho)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    # bases criadas por versões anteriores: acrescenta colunas novas
    existentes = {r[1] for r in con.execute("PRAGMA table_info(atos)")}
    for coluna in ("inclusao", "incluido_em", "atualizado_em", "situacao_lista"):
        if coluna not in existentes:
            con.execute(f"ALTER TABLE atos ADD COLUMN {coluna} TEXT")
    return con


def abrir_leitura(caminho: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def gravar_ato(con: sqlite3.Connection, ato: dict, inclusao: str) -> str:
    """Insere ou atualiza um ato (dict no formato de consolida_atos.analisar_md).

    Devolve 'novo', 'alterado' ou 'igual'. O índice de texto é refeito no fim com reconstruir_fts().
    """
    ato = {**ato, "situacao": normalizar_situacao(ato.get("situacao"))}
    valores = {
        c: (json.dumps(ato[c], ensure_ascii=False) if c in ("palavras_chave", "anexos", "referencias") else ato.get(c))
        for c in COLUNAS
    }
    atual = con.execute(f"SELECT {', '.join(COLUNAS)} FROM atos WHERE id = ?", (ato["id"],)).fetchone()
    if atual is not None and all(atual[c] == valores[c] for c in COLUNAS if c not in ("arquivo", "pasta_situacao")):
        return "igual"
    if atual is None:
        con.execute(
            f"INSERT INTO atos ({', '.join(COLUNAS)}, inclusao, incluido_em, atualizado_em) "
            f"VALUES ({', '.join('?' * len(COLUNAS))}, ?, ?, ?)",
            [valores[c] for c in COLUNAS] + [inclusao, agora(), agora()],
        )
    else:
        con.execute(
            f"UPDATE atos SET {', '.join(f'{c} = ?' for c in COLUNAS[1:])}, atualizado_em = ? WHERE id = ?",
            [valores[c] for c in COLUNAS[1:]] + [agora(), ato["id"]],
        )
    con.execute("DELETE FROM palavras_chave WHERE id = ?", (ato["id"],))
    con.executemany(
        "INSERT INTO palavras_chave VALUES (?, ?, ?)",
        [(ato["id"], i, t) for i, t in enumerate(ato.get("palavras_chave") or [], 1)],
    )
    return "novo" if atual is None else "alterado"


def reconstruir_fts(con: sqlite3.Connection):
    con.execute("INSERT INTO atos_fts(atos_fts) VALUES('rebuild')")


def sincronizar_temas(con: sqlite3.Connection, temas: list[dict]):
    con.execute("DELETE FROM temas")
    con.executemany(
        "INSERT INTO temas VALUES (?, ?, ?, ?)",
        [(t["codigo"], t["nome"], t.get("descricao", ""), i) for i, t in enumerate(temas)],
    )


def definir_temas(con: sqlite3.Connection, id_ato: int, temas: list[str], origem: str):
    con.execute("DELETE FROM ato_temas WHERE id = ?", (id_ato,))
    con.executemany(
        "INSERT OR IGNORE INTO ato_temas VALUES (?, ?, ?, ?)",
        [(id_ato, t, origem, agora()) for t in temas],
    )


def meta(con: sqlite3.Connection, chave: str, valor=None):
    if valor is None:
        r = con.execute("SELECT valor FROM metadados WHERE chave = ?", (chave,)).fetchone()
        return r[0] if r else None
    con.execute("INSERT OR REPLACE INTO metadados VALUES (?, ?)", (chave, str(valor)))
