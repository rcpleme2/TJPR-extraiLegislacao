"""Classificação temática.

- Atos antigos: classificados fora (Copilot) e importados de planilhas CSV/XLSX.
- Atos novos: classificados pela API do Claude (somente no job diário).
"""

import csv
import io
import json
import os
import re
import unicodedata

from . import banco, config


def carregar_temas() -> list[dict]:
    return json.loads(config.ARQ_TEMAS.read_text(encoding="utf-8"))


def _normal(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def mapa_temas(temas: list[dict]) -> dict[str, str]:
    """Aceita o código ou o nome do tema, sem diferenciar maiúsculas e acentos."""
    m = {}
    for t in temas:
        m[_normal(t["codigo"])] = t["codigo"]
        m[_normal(t["nome"])] = t["codigo"]
    return m


# ------------------------------------------------------------- importação (Copilot)

def _linhas_planilha(nome: str, dados: bytes) -> list[list[str]]:
    if nome.lower().endswith(".xlsx"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(dados), read_only=True, data_only=True)
        linhas = []
        for ws in wb.worksheets:
            linhas += [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
        return linhas
    texto = dados.decode("utf-8-sig", errors="replace")
    try:
        dialeto = csv.Sniffer().sniff(texto[:5000], delimiters=";,\t")
    except csv.Error:
        class dialeto(csv.excel):  # noqa: N801
            delimiter = ";"
    return list(csv.reader(io.StringIO(texto), dialeto))


def importar_planilha(con, nome: str, dados: bytes, temas: list[dict]) -> dict:
    """Importa uma planilha com as colunas 'id' e 'temas' (temas separados por | ou ;)."""
    linhas = _linhas_planilha(nome, dados)
    mapa = mapa_temas(temas)
    res = {"linhas": 0, "importados": 0, "ids_desconhecidos": 0, "temas_invalidos": {}, "sem_tema": 0}
    col_id = col_temas = None
    for linha in linhas:
        cab = [_normal(c) for c in linha]
        if col_id is None:
            if "id" in cab and any(c.startswith("tema") for c in cab):
                col_id = cab.index("id")
                col_temas = next(k for k, c in enumerate(cab) if c.startswith("tema"))
            continue
        if len(linha) <= max(col_id, col_temas):
            continue
        bruto_id = re.sub(r"\D", "", linha[col_id].split(".")[0])
        if not bruto_id:
            continue
        res["linhas"] += 1
        id_ato = int(bruto_id)
        if con.execute("SELECT 1 FROM atos WHERE id = ?", (id_ato,)).fetchone() is None:
            res["ids_desconhecidos"] += 1
            continue
        codigos = []
        for parte in re.split(r"[|;,\n]+", linha[col_temas]):
            if not parte.strip():
                continue
            cod = mapa.get(_normal(parte))
            if cod is None:
                res["temas_invalidos"][parte.strip()] = res["temas_invalidos"].get(parte.strip(), 0) + 1
            elif cod not in codigos:
                codigos.append(cod)
        if not codigos:
            res["sem_tema"] += 1
            continue
        banco.definir_temas(con, id_ato, codigos[:3], "copilot")
        res["importados"] += 1
    if col_id is None:
        res["erro"] = "não encontrei o cabeçalho com as colunas 'id' e 'temas'"
    return res


# ------------------------------------------------------------- Claude (atos novos)

def _instrucoes(temas: list[dict]) -> str:
    lista = "\n".join(f"- {t['codigo']}: {t['nome']}. {t.get('descricao', '')}" for t in temas)
    return (
        "Você classifica atos normativos e administrativos do Tribunal de Justiça do Estado do Paraná "
        "por tema, para um painel de consulta pública.\n\n"
        f"Temas disponíveis (use apenas os códigos):\n{lista}\n\n"
        "Escolha de 1 a 3 temas, do mais para o menos relevante, considerando o assunto principal do ato "
        "(o que ele determina e a quem se aplica), não apenas palavras que aparecem no texto. "
        "Use OUTROS somente se nenhum tema se aplicar. Responda com os códigos e uma justificativa curta."
    )


def _conteudo_ato(linha) -> str:
    return (
        f"Tipo: {linha['tipo']}\nNúmero: {linha['numero']}\nOrigem: {linha['origem']}\n"
        f"Documento: {linha['documento']}\nEmenta: {linha['ementa']}\nAssunto: {linha['assunto']}\n\n"
        f"Texto (início):\n{(linha['texto'] or '')[:6000]}"
    )


def classificar_novos(con, temas: list[dict], log=print) -> dict:
    """Classifica com o Claude os atos novos ainda sem tema. Sem chave configurada, não faz nada."""
    res = {"classificados": 0, "pendentes": 0, "erros": 0}
    # só os atos trazidos pela atualização diária; os da carga inicial são classificados pelo Copilot
    pendentes = con.execute(
        """SELECT id, tipo, numero, origem, documento, ementa, assunto, texto FROM atos
           WHERE inclusao = 'diario' AND id NOT IN (SELECT id FROM ato_temas)
           ORDER BY data_diario DESC"""
    ).fetchall()
    res["pendentes"] = len(pendentes)
    if not pendentes:
        return res
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log(f"   {len(pendentes)} ato(s) novo(s) sem tema; ANTHROPIC_API_KEY não configurada, classificação pulada")
        return res

    import anthropic

    cliente = anthropic.Anthropic(max_retries=4)
    codigos = [t["codigo"] for t in temas]
    esquema = {
        "type": "object",
        "properties": {
            "temas": {"type": "array", "items": {"type": "string", "enum": codigos}},
            "justificativa": {"type": "string"},
        },
        "required": ["temas", "justificativa"],
        "additionalProperties": False,
    }
    instrucoes = _instrucoes(temas)
    for linha in pendentes[: config.MAX_CLASSIFICACOES]:
        try:
            r = cliente.messages.create(
                model=config.MODELO_CLASSIFICACAO,
                max_tokens=4000,
                system=instrucoes,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": esquema}},
                messages=[{"role": "user", "content": _conteudo_ato(linha)}],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            log(f"   ! chave da API recusada ({e.__class__.__name__}); classificação interrompida")
            res["erros"] += 1
            break
        except anthropic.RateLimitError:
            log("   ! limite de uso da API atingido; o restante fica para a próxima execução")
            break
        except anthropic.APIStatusError as e:
            log(f"   ! ato {linha['id']}: erro da API {e.status_code}")
            res["erros"] += 1
            continue
        except anthropic.APIConnectionError:
            log("   ! falha de conexão com a API; o restante fica para a próxima execução")
            break
        if r.stop_reason != "end_turn":
            log(f"   ! ato {linha['id']}: resposta interrompida ({r.stop_reason})")
            res["erros"] += 1
            continue
        try:
            dados = json.loads(next(b.text for b in r.content if b.type == "text"))
        except (StopIteration, ValueError):
            res["erros"] += 1
            continue
        escolhidos = [t for t in dict.fromkeys(dados.get("temas", [])) if t in codigos][:3]
        if not escolhidos:
            res["erros"] += 1
            continue
        banco.definir_temas(con, linha["id"], escolhidos, config.MODELO_CLASSIFICACAO)
        res["classificados"] += 1
    if res["pendentes"] > config.MAX_CLASSIFICACOES:
        log(f"   limite de {config.MAX_CLASSIFICACOES} classificações por execução; o restante fica para as próximas")
    return res
