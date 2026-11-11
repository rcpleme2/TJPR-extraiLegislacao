#!/usr/bin/env python3
"""
Prepara os atos para classificação por tema no Copilot (Cowork).

Gera, na pasta de saída (padrão ./copilot):
    lote_001.csv, lote_002.csv, ...   atos a classificar (id, tipo, número, origem, ementa, assunto, início do texto)
    temas.csv                         a lista de temas permitidos (mesma usada pelo Claude nos atos novos)
    INSTRUCOES_COPILOT.md             o texto para colar no Copilot

Fonte dos atos (escolha uma):
    --entrada atos            pasta baixada por extrai_legislacao.py (padrão)
    --base atos.sqlite        base do painel (baixada do bucket): exporta só os atos ainda sem tema

Depois de classificar, envie as planilhas devolvidas pelo Copilot (CSV ou XLSX, com as colunas
"id" e "temas") para a pasta classificacao/ do bucket. A atualização diária importa sozinha.

Uso:
    py exporta_para_copilot.py
    py exporta_para_copilot.py --tamanho-lote 100
    py exporta_para_copilot.py --base atos.sqlite
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


def limpar(t, n=None) -> str:
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    t = re.sub(r"[*_#>`\\]", "", t)  # tira marcações do Markdown
    return t[:n].rstrip() + "…" if n and len(t) > n else t


def atos_da_pasta(entrada: Path) -> list[dict]:
    sys.path.insert(0, str(RAIZ))
    import consolida_atos

    atos, avisos = consolida_atos.ler_todos(entrada)
    for a in avisos:
        print("Aviso:", a, file=sys.stderr)
    return sorted(atos.values(), key=lambda a: (a.get("data_diario") or "", a["id"]), reverse=True)


def atos_da_base(base: Path) -> list[dict]:
    con = sqlite3.connect(base)
    con.row_factory = sqlite3.Row
    linhas = con.execute(
        "SELECT * FROM atos WHERE id NOT IN (SELECT id FROM ato_temas) ORDER BY data_diario DESC, id DESC"
    ).fetchall()
    return [dict(r) for r in linhas]


INSTRUCOES = """# Classificação temática dos atos normativos do TJPR

Use este texto no Copilot (Cowork), anexando **temas.csv** e **um lote por vez** (lote_001.csv, lote_002.csv, …).
Para cada lote, salve a resposta como **lote_NNN_classificado.csv** (ou .xlsx) e envie para a pasta
`classificacao/` do bucket. A atualização diária do painel importa os arquivos sozinha.

---

## Texto para colar no Copilot

Você vai classificar atos normativos e administrativos do Tribunal de Justiça do Estado do Paraná por tema.

Arquivos anexados:
- **temas.csv**: a lista fechada de temas, com as colunas `codigo`, `nome` e `descricao`.
- **{exemplo_lote}**: os atos a classificar, um por linha, com as colunas `id`, `tipo`, `numero`, `origem`, `data`, `ementa`, `assunto` e `inicio_texto`.

Para **cada linha** do arquivo de atos:
1. Leia a ementa, o assunto e o início do texto, e identifique o assunto principal do ato: o que ele determina e a quem se aplica.
2. Escolha de **1 a 3 temas** da lista, do mais para o menos relevante. Use **somente os códigos** da coluna `codigo` de temas.csv, exatamente como estão escritos.
3. Use `OUTROS` apenas se nenhum tema se aplicar.

Gere um arquivo CSV para download com **exatamente duas colunas**, separadas por ponto e vírgula, e este cabeçalho:

```
id;temas
```

- `id`: copie exatamente o valor da coluna `id`, sem alterar.
- `temas`: os códigos escolhidos separados por barra vertical, sem espaços. Exemplo: `FORO_EXTRAJUDICIAL|ORCAMENTO_FINANCAS`.
- Uma linha para **cada** ato do arquivo, na mesma ordem, sem pular nenhum e sem linhas extras.
- Não acrescente comentários, explicações nem outras colunas.

Ao final, informe quantas linhas o arquivo de entrada tinha e quantas o arquivo gerado tem; os números devem ser iguais.

---

## Dicas

- Se o Copilot devolver menos linhas do que o lote, peça para completar ou divida o lote ao meio (rode de novo este
  script com `--tamanho-lote` menor).
- O painel aceita também o **nome** do tema no lugar do código, e separadores `|`, `;` ou `,` entre os temas.
- Classificações importadas de novo para o mesmo ato substituem as anteriores.
- Temas desta lista ({n_temas}): {lista_temas}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entrada", type=Path, default=Path("atos"), help="pasta com os .md (padrão: ./atos)")
    ap.add_argument("--base", type=Path, help="base atos.sqlite do painel (exporta só os sem tema)")
    ap.add_argument("--saida", type=Path, default=Path("copilot"), help="pasta de saída (padrão: ./copilot)")
    ap.add_argument("--tamanho-lote", type=int, default=150, help="atos por arquivo (padrão: 150)")
    ap.add_argument("--temas", type=Path, default=RAIZ / "temas.json", help="lista de temas (padrão: temas.json)")
    a = ap.parse_args()

    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    temas = json.loads(a.temas.read_text(encoding="utf-8"))
    atos = atos_da_base(a.base) if a.base else atos_da_pasta(a.entrada)
    if not atos:
        sys.exit("Nenhum ato para exportar.")

    a.saida.mkdir(parents=True, exist_ok=True)
    for antigo in a.saida.glob("lote_*.csv"):
        if "_classificado" not in antigo.name:
            antigo.unlink()

    with open(a.saida / "temas.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "nome", "descricao"])
        for t in temas:
            w.writerow([t["codigo"], t["nome"], t.get("descricao", "")])

    n_lotes = 0
    for k in range(0, len(atos), a.tamanho_lote):
        n_lotes += 1
        with open(a.saida / f"lote_{n_lotes:03d}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["id", "tipo", "numero", "origem", "data", "ementa", "assunto", "inicio_texto"])
            for at in atos[k:k + a.tamanho_lote]:
                ementa = at.get("ementa") or ""
                if len(ementa) < 30:  # várias ementas são só "REVEICULADO POR INCORREÇÃO"
                    ementa = " - ".join(x for x in (at.get("documento"), ementa) if x)
                w.writerow([
                    at["id"], limpar(at.get("tipo")), limpar(at.get("numero")), limpar(at.get("origem")),
                    at.get("data_diario") or "", limpar(ementa, 600), limpar(at.get("assunto"), 600),
                    limpar(at.get("texto"), 700),
                ])

    (a.saida / "INSTRUCOES_COPILOT.md").write_text(
        INSTRUCOES.format(
            exemplo_lote="lote_001.csv", n_temas=len(temas),
            lista_temas=", ".join(f"{t['codigo']} ({t['nome']})" for t in temas),
        ),
        encoding="utf-8",
    )
    print(f"{len(atos)} ato(s) em {n_lotes} lote(s) de até {a.tamanho_lote}, na pasta {a.saida.resolve()}")
    print("Abra INSTRUCOES_COPILOT.md para os próximos passos.")


if __name__ == "__main__":
    main()
