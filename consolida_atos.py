#!/usr/bin/env python3
"""
Separa e consolida os atos baixados por extrai_legislacao.py.

Lê todos os .md das pastas de situação (atos/Vigente, atos/Republicado, ...) e gera,
na pasta de saída (padrão ./base):

    atos.sqlite   base única com todos os atos (metadados + texto), com pesquisa de
                  texto completo (FTS5, ignora acentos) e tabela de palavras-chave;
                  é a fonte de dados para o painel.
    atos.csv      os metadados em planilha (abre no Excel; sem o texto integral).
    atos.jsonl    um ato por linha, com o texto integral (para gerar embeddings).
    separados/    cópia dos .md organizada em pastas, por padrão Origem/Ano
                  (mude com --por, ex.: --por ano,origem ou --por tipo,ano).

Não usa a internet e não altera os arquivos originais. Pode ser executado de novo a
cada atualização: a base é recriada e só os arquivos novos/alterados são copiados
para "separados".

Uso:
    py consolida_atos.py
    py consolida_atos.py --entrada atos --saida base --por origem,ano
    py consolida_atos.py --sem-separar
    py consolida_atos.py --buscar "guarda compartilhada"
"""

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

MARCADOR = ".gerado_por_consolida_atos"
CAMPOS_TABELA = {
    "Tipo": "tipo",
    "Ato": "tipo_ato",
    "Número": "numero",
    "Origem": "origem",
    "Documento": "documento",
    "Assunto": "assunto",
    "Ementa": "ementa",
    "Situação": "situacao",
    "Data do diário": "data_diario",
    "Diário": "diario",
    "Fonte": "fonte",
}
COLUNAS = [
    "id", "situacao", "tipo", "tipo_ato", "numero", "numero_seq", "ano", "origem",
    "documento", "assunto", "palavras_chave", "ementa", "data_diario", "diario",
    "fonte", "anexos", "referencias", "pasta_situacao", "arquivo", "caracteres_texto",
]
CRITERIOS = {"origem", "ano", "tipo", "situacao"}
MAX_CAMINHO = 240  # o Windows, por padrão, não aceita caminhos com mais de 260 caracteres


# ---------------------------------------------------------------- leitura dos .md

RE_FONTE_ID = re.compile(r"/atos/documento/(\d+)")
RE_LINK = re.compile(r"^- \[(.*)\]\((.*)\)\s*$")
RE_REF = re.compile(r"^- \*\*(.*?):\*\* (.*)$")


def separar_celulas(linha: str) -> list[str]:
    partes = re.split(r"(?<!\\)\|", linha.strip())
    return [p.strip().replace("\\|", "|") for p in partes[1:-1]]


def palavras_chave(assunto: str) -> list[str]:
    """'1.Revogação 2.Presidência 3.Lei n.º 13.709/2018' -> ['Revogação', 'Presidência', 'Lei n.º 13.709/2018'].

    Só corta nos números em sequência (1., 2., 3., ...), para não quebrar
    "13.709" ou "n.º 4." que aparecem dentro de um termo.
    """
    assunto = (assunto or "").strip()
    marcas, esperado = [], 1
    # depois do "N." vem o termo; pode começar com ordinal ("2.2ª Vice-Presidência"),
    # mas não com outro número ("13.709/2018")
    for m in re.finditer(r"(?:^|\s)(\d{1,3})\.\s?(?=\D|\d{1,2}[ªº°])", assunto):
        if int(m.group(1)) == esperado:
            marcas.append(m)
            esperado += 1
    if len(marcas) < 2 or marcas[0].start() > 1:
        return [assunto] if assunto else []
    termos = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(assunto)
        t = assunto[m.end():fim].strip(" ;,")
        if t:
            termos.append(t)
    return termos


def ler_ato(arq: Path) -> dict | None:
    texto_arquivo = arq.read_text(encoding="utf-8", errors="replace")
    linhas = texto_arquivo.splitlines()
    dados = {c: "" for c in CAMPOS_TABELA.values()}
    anexos, refs = [], []
    secao = None
    inicio_texto = None
    for i, linha in enumerate(linhas):
        if linha.startswith("## "):
            secao = linha[3:].strip().lower()
            if secao == "texto":
                inicio_texto = i + 1
                break
            continue
        if secao is None and linha.startswith("| ") and not linha.startswith("|---"):
            cel = separar_celulas(linha)
            if len(cel) == 2 and cel[0] in CAMPOS_TABELA:
                dados[CAMPOS_TABELA[cel[0]]] = cel[1]
        elif secao == "anexos" and (m := RE_LINK.match(linha)):
            anexos.append({"nome": m.group(1), "link": m.group(2)})
        elif secao == "referências" and (m := RE_REF.match(linha)):
            desc = m.group(2)
            link = RE_LINK.match("- " + desc)
            refs.append({
                "tipo": m.group(1),
                "descricao": link.group(1) if link else desc,
                "link": link.group(2) if link else "",
            })
    m = RE_FONTE_ID.search(dados["fonte"])
    if not m:
        return None
    texto = "\n".join(linhas[inicio_texto:]).strip() if inicio_texto is not None else ""
    if texto == "_(sem texto)_":
        texto = ""

    numero_seq, ano = "", ""
    if "/" in dados["numero"]:
        numero_seq, resto = (p.strip() for p in dados["numero"].split("/", 1))
        ano = m_ano.group(0) if (m_ano := re.match(r"\d{4}", resto)) else ""
    if not ano:
        ano = dados["data_diario"][-4:] if re.search(r"\d{4}$", dados["data_diario"]) else ""
    data_iso = ""
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", dados["data_diario"]):
        d, mm, a = dados["data_diario"].split("/")
        data_iso = f"{a}-{mm}-{d}"

    return {
        **dados,
        "id": int(m.group(1)),
        "numero_seq": numero_seq,
        "ano": int(ano) if ano else None,
        "data_diario": data_iso or dados["data_diario"],
        "palavras_chave": palavras_chave(dados["assunto"]),
        "anexos": anexos,
        "referencias": refs,
        "texto": texto,
        "caracteres_texto": len(texto),
        "_mtime": arq.stat().st_mtime,
    }


def ler_todos(entrada: Path) -> tuple[dict, list[str]]:
    arquivos = sorted(p for p in entrada.glob("*/*.md"))
    if not arquivos:
        sys.exit(f"Nenhum .md encontrado em {entrada}\\<situação>\\. Use --entrada para indicar a pasta.")
    atos, avisos, repetidos = {}, [], 0
    t0 = time.monotonic()
    for n, arq in enumerate(arquivos, 1):
        try:
            ato = ler_ato(arq)
        except OSError as e:
            avisos.append(f"não consegui ler {arq}: {e}")
            continue
        if ato is None:
            avisos.append(f"sem linha 'Fonte' (ignorado): {arq}")
            continue
        ato["pasta_situacao"] = arq.parent.name
        ato["arquivo"] = str(arq.relative_to(entrada))
        ato["_origem_arquivo"] = arq
        anterior = atos.get(ato["id"])
        if anterior is not None:
            repetidos += 1
            if anterior["_mtime"] >= ato["_mtime"]:
                continue  # fica a versão baixada mais recentemente
        atos[ato["id"]] = ato
        if n % 250 == 0 or n == len(arquivos):
            print(f"\rLendo arquivos: {n}/{len(arquivos)}", end="", flush=True)
    print(f"  ({time.monotonic() - t0:.1f}s)")
    if repetidos:
        avisos.append(
            f"{repetidos} ato(s) aparecem em mais de uma pasta de situação; "
            "foi mantida a versão baixada por último"
        )
    return atos, avisos


# ---------------------------------------------------------------- saídas

def gravar_sqlite(atos: dict, destino: Path):
    tmp = destino.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(
        """
        CREATE TABLE atos (
            id INTEGER PRIMARY KEY, situacao TEXT, tipo TEXT, tipo_ato TEXT,
            numero TEXT, numero_seq TEXT, ano INTEGER, origem TEXT, documento TEXT,
            assunto TEXT, palavras_chave TEXT, ementa TEXT, data_diario TEXT,
            diario TEXT, fonte TEXT, anexos TEXT, referencias TEXT,
            pasta_situacao TEXT, arquivo TEXT, caracteres_texto INTEGER, texto TEXT
        );
        CREATE TABLE palavras_chave (id INTEGER REFERENCES atos(id), ordem INTEGER, termo TEXT);
        CREATE VIRTUAL TABLE atos_fts USING fts5(
            documento, assunto, ementa, texto,
            content='atos', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    for a in atos.values():
        con.execute(
            f"INSERT INTO atos ({', '.join(COLUNAS)}, texto) VALUES ({', '.join('?' * (len(COLUNAS) + 1))})",
            [
                *(
                    json.dumps(a[c], ensure_ascii=False) if c in ("palavras_chave", "anexos", "referencias")
                    else a[c]
                    for c in COLUNAS
                ),
                a["texto"],
            ],
        )
        con.executemany(
            "INSERT INTO palavras_chave VALUES (?, ?, ?)",
            [(a["id"], i, t) for i, t in enumerate(a["palavras_chave"], 1)],
        )
    con.executescript(
        """
        INSERT INTO atos_fts(rowid, documento, assunto, ementa, texto)
            SELECT id, documento, assunto, ementa, texto FROM atos;
        CREATE INDEX ix_atos_origem ON atos(origem);
        CREATE INDEX ix_atos_ano ON atos(ano);
        CREATE INDEX ix_atos_tipo ON atos(tipo);
        CREATE INDEX ix_atos_situacao ON atos(situacao);
        CREATE INDEX ix_pc_termo ON palavras_chave(termo);
        """
    )
    con.commit()
    con.close()
    os.replace(tmp, destino)  # troca de uma vez: quem estiver lendo nunca vê a base pela metade


def gravar_csv(atos: dict, destino: Path):
    with open(destino, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: acentos certos no Excel
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUNAS)
        for a in atos.values():
            linha = []
            for c in COLUNAS:
                v = a[c]
                if c == "palavras_chave":
                    v = " | ".join(v)
                elif c == "anexos":
                    v = " | ".join(x["nome"] for x in v)
                elif c == "referencias":
                    v = " | ".join(f"{x['tipo']}: {x['descricao']}" for x in v)
                linha.append("" if v is None else v)
            w.writerow(linha)


def gravar_jsonl(atos: dict, destino: Path):
    with open(destino, "w", encoding="utf-8") as f:
        for a in atos.values():
            f.write(json.dumps({k: v for k, v in a.items() if not k.startswith("_")}, ensure_ascii=False))
            f.write("\n")


# ---------------------------------------------------------------- separação em pastas

def nome_pasta(valor) -> str:
    v = unicodedata.normalize("NFC", str(valor or "")).strip()
    v = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", v)
    v = re.sub(r"\s+", " ", v).rstrip(" .")
    return v[:80] or "Sem informação"


def caber_no_caminho(pasta: Path, nome: str) -> str:
    """Encurta o nome do arquivo se o caminho completo passar do limite do Windows."""
    sobra = MAX_CAMINHO - len(str(pasta.resolve())) - 1
    if len(nome) <= sobra:
        return nome
    tronco = nome[:-3]
    return tronco[: max(sobra - 4, 20)].rstrip(" .") + "….md"


def separar(atos: dict, raiz: Path, criterios: list[str], limpar: bool) -> tuple[int, int, int]:
    if limpar and raiz.exists():
        if not (raiz / MARCADOR).exists():
            sys.exit(f"Não apago {raiz}: ela não foi criada por este script (falta {MARCADOR}).")
        shutil.rmtree(raiz)
    raiz.mkdir(parents=True, exist_ok=True)
    (raiz / MARCADOR).write_text(
        "Pasta gerada por consolida_atos.py. Não edite aqui: ela é refeita a partir de atos/.\n",
        encoding="utf-8",
    )

    planejados, copiados, iguais = set(), 0, 0
    for a in sorted(atos.values(), key=lambda x: x["id"]):
        pasta = raiz
        for c in criterios:
            valor = a["ano"] if c == "ano" else a[c]
            pasta = pasta / nome_pasta(valor if valor not in (None, "") else f"Sem {c}")
        pasta.mkdir(parents=True, exist_ok=True)
        orig: Path = a["_origem_arquivo"]
        nome = caber_no_caminho(pasta, orig.name)
        destino = pasta / nome
        if destino in planejados:
            destino = pasta / caber_no_caminho(pasta, f"{orig.stem} ({a['id']}).md")
        planejados.add(destino)
        st = orig.stat()
        if destino.exists():
            sd = destino.stat()
            if sd.st_size == st.st_size and int(sd.st_mtime) == int(st.st_mtime):
                iguais += 1
                continue
        shutil.copy2(orig, destino)
        copiados += 1

    sobrando = [p for p in raiz.rglob("*.md") if p not in planejados]
    return copiados, iguais, len(sobrando)


# ---------------------------------------------------------------- resumo e busca

def resumo(atos: dict):
    def top(campo, n=15):
        c = Counter(a[campo] or "(vazio)" for a in atos.values())
        return c.most_common(n), len(c)

    print(f"\n{len(atos)} ato(s) consolidados.")
    por_sit = Counter(a["pasta_situacao"] for a in atos.values())
    print("Por situação: " + ", ".join(f"{k} {v}" for k, v in sorted(por_sit.items())))
    anos = [a["ano"] for a in atos.values() if a["ano"]]
    if anos:
        print(f"Anos: de {min(anos)} a {max(anos)}")
    for campo, titulo in (("tipo", "Tipos"), ("origem", "Origens")):
        lista, total = top(campo)
        print(f"{titulo} ({total} no total; os mais frequentes):")
        for k, v in lista:
            print(f"   {v:6d}  {k}")
    termos = Counter(t for a in atos.values() for t in a["palavras_chave"])
    print(f"Palavras-chave mais frequentes ({len(termos)} distintas):")
    for k, v in termos.most_common(15):
        print(f"   {v:6d}  {k}")


def buscar(base: Path, consulta: str, limite: int = 15):
    if not base.exists():
        sys.exit(f"{base} não existe; rode primeiro sem --buscar.")
    con = sqlite3.connect(base)
    # cada palavra vira um termo entre aspas (evita erro de sintaxe do FTS com hífens etc.)
    termos = " ".join('"' + t.replace('"', "") + '"' for t in consulta.split())
    linhas = con.execute(
        """
        SELECT a.numero, a.tipo, a.origem, a.situacao, a.arquivo,
               snippet(atos_fts, -1, '[', ']', ' … ', 12)
        FROM atos_fts JOIN atos a ON a.id = atos_fts.rowid
        WHERE atos_fts MATCH ? ORDER BY bm25(atos_fts, 5.0, 3.0, 3.0, 1.0) LIMIT ?
        """,
        (termos, limite),
    ).fetchall()
    total = con.execute("SELECT count(*) FROM atos_fts WHERE atos_fts MATCH ?", (termos,)).fetchone()[0]
    print(f"{total} ato(s) encontrados para: {consulta}" + (f" (mostrando {limite})" if total > limite else ""))
    for numero, tipo, origem, sit, arquivo, trecho in linhas:
        print(f"\n- {tipo} {numero} — {origem} [{sit}]\n  {arquivo}\n  {trecho}")


# ---------------------------------------------------------------- principal

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entrada", type=Path, default=Path("atos"), help="pasta com as subpastas de situação (padrão: ./atos)")
    ap.add_argument("--saida", type=Path, default=Path("base"), help="pasta de saída (padrão: ./base)")
    ap.add_argument(
        "--por", default="origem,ano",
        help="como separar as pastas: combinação de origem, ano, tipo, situacao (padrão: origem,ano)",
    )
    ap.add_argument("--sem-separar", action="store_true", help="não gera a pasta 'separados'")
    ap.add_argument("--limpar", action="store_true", help="apaga e recria a pasta 'separados' (útil após mudar --por)")
    ap.add_argument("--buscar", metavar="TERMOS", help="pesquisa na base já consolidada e sai")
    a = ap.parse_args()

    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    base = a.saida / "atos.sqlite"
    if a.buscar:
        buscar(base, a.buscar)
        return

    criterios = [c.strip().lower() for c in a.por.split(",") if c.strip()]
    if not criterios or set(criterios) - CRITERIOS:
        sys.exit(f"--por aceita: {', '.join(sorted(CRITERIOS))} (separados por vírgula)")

    atos, avisos = ler_todos(a.entrada)
    a.saida.mkdir(parents=True, exist_ok=True)

    print("Gravando base SQLite, CSV e JSONL...")
    gravar_sqlite(atos, base)
    gravar_csv(atos, a.saida / "atos.csv")
    gravar_jsonl(atos, a.saida / "atos.jsonl")

    if not a.sem_separar:
        print(f"Separando em pastas por {' / '.join(criterios)}...")
        copiados, iguais, sobrando = separar(atos, a.saida / "separados", criterios, a.limpar)
        print(f"   {copiados} copiado(s), {iguais} já atualizado(s)")
        if sobrando:
            avisos.append(
                f"{sobrando} arquivo(s) em {a.saida / 'separados'} não correspondem mais a nenhum ato "
                "(ex.: após mudar --por); rode com --limpar para refazer a pasta"
            )

    resumo(atos)
    print(f"\nArquivos gerados em {a.saida.resolve()}:")
    for nome in ("atos.sqlite", "atos.csv", "atos.jsonl"):
        print(f"   {nome}")
    if not a.sem_separar:
        print("   separados/")
    for av in avisos:
        print(f"Aviso: {av}", file=sys.stderr)


if __name__ == "__main__":
    main()
