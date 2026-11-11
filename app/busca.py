"""Pesquisa: por palavras (FTS5), por significado (vetores) e combinada, com filtros e facetas."""

import re
from dataclasses import dataclass, field

from . import semantica

POR_PAGINA = 20
LIMITE_SEMANTICO = 50     # na busca por significado, mostra os N atos mais próximos
LIMITE_SEMANTICO_COMBINADO = 30  # quantos deles entram na pesquisa combinada

FILTROS = ("origem", "tipo", "situacao", "tema", "ano")


@dataclass
class Resultado:
    itens: list = field(default_factory=list)
    total: int = 0
    facetas: dict = field(default_factory=dict)
    modo: str = "palavras"
    aviso: str = ""


PALAVRAS_VAZIAS = {
    "a", "o", "as", "os", "e", "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "um", "uma",
    "para", "por", "com", "ao", "aos", "à", "às", "que", "se",
}


def consulta_fts(texto: str) -> str:
    """Transforma o texto digitado numa consulta FTS segura: palavras e "frases entre aspas" (todas obrigatórias).

    Palavras como "de" e "para" fora de aspas são ignoradas (estão em quase todos os atos e só
    poluiriam o destaque); dentro de aspas a frase é mantida inteira.
    """
    partes = re.findall(r'"([^"]+)"|(\S+)', texto)
    termos, vazias = [], []
    for frase, palavra in partes:
        t = (frase or palavra).replace('"', "").strip()
        t = re.sub(r"[^\w\s/.-]", " ", t).strip()
        if not t:
            continue
        if palavra and t.lower() in PALAVRAS_VAZIAS:
            vazias.append(f'"{t}"')
        else:
            termos.append(f'"{t}"')
    return " ".join(termos or vazias)


def _where(filtros: dict) -> tuple[str, list]:
    cond, args = [], []
    for campo in ("origem", "tipo", "situacao"):
        if filtros.get(campo):
            cond.append(f"a.{campo} = ?")
            args.append(filtros[campo])
    if filtros.get("ano"):
        cond.append("a.ano = ?")
        args.append(int(filtros["ano"]))
    if filtros.get("tema"):
        if filtros["tema"] == "_sem_tema":
            cond.append("a.id NOT IN (SELECT id FROM ato_temas)")
        else:
            cond.append("a.id IN (SELECT id FROM ato_temas WHERE tema = ?)")
            args.append(filtros["tema"])
    return (" AND ".join(cond) or "1"), args


def _ids_por_numero(con, q: str, where: str, args: list) -> list[int]:
    """Se a pessoa digitou um número de ato (ex.: 397/2023), esses atos vêm primeiro."""
    m = re.fullmatch(r"\s*(?:n[º°o.]*\s*)?0*(\d{1,6})\s*/\s*(\d{4})\s*", q, re.I)
    if not m:
        return []
    rows = con.execute(
        f"SELECT a.id FROM atos a WHERE ltrim(a.numero_seq, '0') = ? AND a.ano = ? AND {where} ORDER BY a.data_diario DESC",
        [m.group(1), int(m.group(2)), *args],
    )
    return [r[0] for r in rows]


def _ids_fts(con, q: str, where: str, args: list) -> list[int]:
    fts = consulta_fts(q)
    if not fts:
        return []
    rows = con.execute(
        f"""SELECT a.id FROM atos_fts JOIN atos a ON a.id = atos_fts.rowid
            WHERE atos_fts MATCH ? AND {where}
            ORDER BY bm25(atos_fts, 5.0, 3.0, 4.0, 1.0)""",
        [fts, *args],
    )
    return [r[0] for r in rows]


def _ids_semanticos(con, indice, q: str, where: str, args: list, filtrado: bool, limite: int) -> list[int]:
    candidatos = None
    if filtrado:
        candidatos = {r[0] for r in con.execute(f"SELECT a.id FROM atos a WHERE {where}", args)}
    return [i for i, _ in indice.pesquisar(semantica.vetor_da_consulta(q), candidatos, limite)]


def _rrf(*listas: list[int], k: int = 60) -> list[int]:
    """Combina rankings (Reciprocal Rank Fusion)."""
    pontos = {}
    for lista in listas:
        for pos, i in enumerate(lista):
            pontos[i] = pontos.get(i, 0.0) + 1.0 / (k + pos + 1)
    return sorted(pontos, key=lambda i: -pontos[i])


def _facetas(con, ids: list[int] | None, where: str, args: list) -> dict:
    """Contagens por origem, tipo, situação, ano e tema dentro do resultado."""
    if ids is not None:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS _res (id INTEGER PRIMARY KEY)")
        con.execute("DELETE FROM _res")
        con.executemany("INSERT OR IGNORE INTO _res VALUES (?)", ((i,) for i in ids))
        sub, sargs = "SELECT id FROM _res", []
    else:
        sub, sargs = f"SELECT a.id FROM atos a WHERE {where}", args
    f = {}
    for campo in ("origem", "tipo", "situacao", "ano"):
        ordem = "1 DESC" if campo == "ano" else "2 DESC, 1"
        f[campo] = [tuple(r) for r in con.execute(
            f"SELECT {campo}, count(*) FROM atos WHERE id IN ({sub}) AND {campo} IS NOT NULL AND {campo} != '' "
            f"GROUP BY 1 ORDER BY {ordem}", sargs)]
    f["tema"] = [tuple(r) for r in con.execute(
        f"SELECT t.codigo, t.nome, count(*) FROM ato_temas at JOIN temas t ON t.codigo = at.tema "
        f"WHERE at.id IN ({sub}) GROUP BY 1, 2 ORDER BY 3 DESC, t.ordem", sargs)]
    sem = con.execute(
        f"SELECT count(*) FROM atos WHERE id IN ({sub}) AND id NOT IN (SELECT id FROM ato_temas)", sargs
    ).fetchone()[0]
    if sem:
        f["tema"].append(("_sem_tema", "Ainda sem tema", sem))
    return f


def carregar_itens(con, ids: list[int], q: str, usar_trecho: bool) -> list[dict]:
    if not ids:
        return []
    marca = ",".join("?" * len(ids))
    linhas = {r["id"]: dict(r) for r in con.execute(
        f"SELECT id, tipo, numero, origem, situacao, data_diario, documento, ementa, assunto FROM atos WHERE id IN ({marca})", ids)}
    temas = {}
    for r in con.execute(
        f"SELECT at.id, t.codigo, t.nome FROM ato_temas at JOIN temas t ON t.codigo = at.tema WHERE at.id IN ({marca}) ORDER BY t.ordem", ids
    ):
        temas.setdefault(r[0], []).append((r[1], r[2]))
    trechos = {}
    fts = consulta_fts(q) if usar_trecho else ""
    if fts:
        for r in con.execute(
            f"SELECT rowid, snippet(atos_fts, -1, '\u0002', '\u0003', ' … ', 24) FROM atos_fts "
            f"WHERE atos_fts MATCH ? AND rowid IN ({marca})", [fts, *ids]
        ):
            trechos[r[0]] = r[1]
    itens = []
    for i in ids:
        if i in linhas:
            item = linhas[i]
            item["temas"] = temas.get(i, [])
            item["trecho"] = trechos.get(i, "")
            itens.append(item)
    return itens


def pesquisar(con, indice, q: str, modo: str, filtros: dict, pagina: int, semantica_pronta: bool) -> Resultado:
    q = (q or "").strip()[:300]
    where, args = _where(filtros)
    filtrado = any(filtros.get(c) for c in FILTROS)
    res = Resultado(modo=modo)
    pode_semantica = semantica_pronta and indice is not None and len(indice) > 0

    if not q:
        ids = None
        res.total = con.execute(f"SELECT count(*) FROM atos a WHERE {where}", args).fetchone()[0]
        ordem = [r[0] for r in con.execute(
            f"SELECT a.id FROM atos a WHERE {where} ORDER BY a.data_diario DESC, a.id DESC LIMIT ? OFFSET ?",
            [*args, POR_PAGINA, (pagina - 1) * POR_PAGINA])]
        res.facetas = _facetas(con, None, where, args)
        res.itens = carregar_itens(con, ordem, "", False)
        return res

    if modo in ("significado", "inteligente") and not pode_semantica:
        res.aviso = "A pesquisa por significado está carregando; mostrando a pesquisa por palavras."
        modo = res.modo = "palavras"

    por_numero = _ids_por_numero(con, q, where, args)
    if por_numero and modo != "significado":
        modo = "palavras"  # quem digitou o número quer aquele ato, não atos "parecidos"
    if modo == "palavras":
        ids = _ids_fts(con, q, where, args)
    elif modo == "significado":
        ids = _ids_semanticos(con, indice, q, where, args, filtrado, LIMITE_SEMANTICO)
    else:  # inteligente: combina palavras e significado
        ids = _rrf(
            _ids_fts(con, q, where, args),
            _ids_semanticos(con, indice, q, where, args, filtrado, LIMITE_SEMANTICO_COMBINADO),
        )
    ids = list(dict.fromkeys(por_numero + ids))

    res.total = len(ids)
    res.facetas = _facetas(con, ids, where, args)
    pagina_ids = ids[(pagina - 1) * POR_PAGINA: pagina * POR_PAGINA]
    res.itens = carregar_itens(con, pagina_ids, q, modo != "significado")
    return res
