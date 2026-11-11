"""Atualização da base (roda como Cloud Run Job, uma vez por dia).

1. Baixa a base atual do armazenamento (na primeira vez, monta a base a partir dos
   atos enviados: entrada/*.zip e/ou atos/**/*.md).
2. Importa as planilhas de classificação devolvidas pelo Copilot (classificacao/*.csv|xlsx).
3. Consulta o site do TJPR: baixa os atos novos e atualiza os que mudaram de situação.
4. Classifica por tema, com o Claude, os atos novos (se a chave estiver configurada).
5. Gera os vetores da pesquisa semântica para o que mudou.
6. Envia a base e os vetores de volta; o site passa a usá-los em alguns minutos.

Uso:
    python -m app.atualiza                 # execução normal
    python -m app.atualiza --sem-site      # não consulta o TJPR (só importa/classifica/vetoriza)
    python -m app.atualiza --max-novos 20  # teste
"""

import argparse
import json
import sys
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath

import consolida_atos
import extrai_legislacao as extrai

from . import armazenamento, banco, classificacao, config, semantica


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ carga inicial

def carga_inicial(con, arm) -> int:
    total = 0
    for nome in arm.listar(config.PREFIXO_ENTRADA):
        if not nome.lower().endswith(".zip"):
            continue
        destino = config.TRABALHO_DIR / "entrada" / PurePosixPath(nome).name
        log(f"Lendo {nome} ...")
        arm.baixar(nome, destino)
        with zipfile.ZipFile(destino) as z:
            for membro in z.namelist():
                if not membro.lower().endswith(".md"):
                    continue
                ato = consolida_atos.analisar_md(z.read(membro).decode("utf-8", errors="replace"))
                if ato is None:
                    continue
                p = PurePosixPath(membro.replace("\\", "/"))
                ato["pasta_situacao"] = p.parent.name
                ato["arquivo"] = f"{p.parent.name}/{p.name}"
                banco.gravar_ato(con, ato, "inicial")
                total += 1
        destino.unlink(missing_ok=True)
        log(f"   {total} ato(s) lidos até agora")

    arquivos = [n for n in arm.listar(config.PREFIXO_ATOS) if n.lower().endswith(".md")]
    if arquivos:
        log(f"Lendo {len(arquivos)} arquivo(s) .md em {config.PREFIXO_ATOS} ...")
        with ThreadPoolExecutor(16) as ex:
            for k, (nome, dados) in enumerate(zip(arquivos, ex.map(arm.ler_bytes, arquivos)), 1):
                ato = consolida_atos.analisar_md(dados.decode("utf-8", errors="replace"))
                if ato is None:
                    continue
                p = PurePosixPath(nome)
                ato["pasta_situacao"] = p.parent.name
                ato["arquivo"] = f"{p.parent.name}/{p.name}"
                banco.gravar_ato(con, ato, "inicial")
                total += 1
                if k % 1000 == 0:
                    log(f"   {k}/{len(arquivos)}")
    return total


# ------------------------------------------------------------------ classificações do Copilot

def importar_classificacoes(con, arm, temas) -> int:
    importados = 0
    for nome in arm.listar(config.PREFIXO_CLASSIFICACAO):
        if not nome.lower().endswith((".csv", ".xlsx")):
            continue
        versao = arm.versao(nome)
        anterior = con.execute("SELECT versao FROM importacoes WHERE arquivo = ?", (nome,)).fetchone()
        if anterior and anterior[0] == versao:
            continue
        res = classificacao.importar_planilha(con, nome, arm.ler_bytes(nome), temas)
        con.execute(
            "INSERT OR REPLACE INTO importacoes VALUES (?, ?, ?, ?)",
            (nome, versao, banco.agora(), json.dumps(res, ensure_ascii=False)),
        )
        log(f"Classificação importada de {nome}: {res}")
        importados += res["importados"]
    return importados


# ------------------------------------------------------------------ site do TJPR

_local = threading.local()


def _cliente() -> "extrai.Cliente":
    """Um cliente (sessão HTTP) por thread."""
    if getattr(_local, "cli", None) is None:
        _local.cli = extrai.Cliente(pausa=config.PAUSA)
        _local.cli.abrir_pagina()
    return _local.cli


def _baixar_ato(id_ato: int, registro: dict):
    """Executado em paralelo: baixa o detalhe e monta o .md (não mexe na base)."""
    m = _cliente().materia(id_ato)
    md = extrai.montar_md(m, id_ato)
    ato = consolida_atos.analisar_md(md)
    if ato is None:
        raise RuntimeError("resposta sem dados do ato")
    sit = banco.normalizar_situacao(m.get("situacao"))
    rotulo = extrai.SITUACOES.get(sit, sit.capitalize())
    num, ano = extrai.numero_e_ano(m, registro)
    orgao = extrai.limpar(m.get("orgao")) or str(registro.get("orgao", ""))
    assunto = extrai.limpar(m.get("assunto")) or extrai.limpar(m.get("descricao")) or "Sem assunto"
    nome = extrai.nome_arquivo(num, orgao, ano, assunto, "∕")
    ato["pasta_situacao"] = rotulo
    ato["arquivo"] = f"{rotulo}/{nome}"
    return ato, md


def listar_site(cli) -> tuple[dict, bool]:
    """Lista os atos do site. Com SITUACOES=TODAS (padrão), pesquisa sem filtro de situação."""
    situacoes = [""] if "TODAS" in [s.upper() for s in config.SITUACOES] else config.SITUACOES
    no_site, completo = {}, True
    for sit in situacoes:
        pagina, total, vistos = 1, None, 0
        while True:
            out = cli.pesquisar(sit, pagina)
            total = out.get("total", 0) if total is None else total
            regs = out.get("registros") or []
            for r in regs:
                no_site[int(r["id"])] = r
            vistos += len(regs)
            if pagina % 50 == 0:
                log(f"   listagem: {vistos}/{total}")
            if not regs or pagina * extrai.TAMANHO_PAGINA >= total:
                break
            pagina += 1
        log(f"Site: {sit or 'todas as situações'}: {total} ato(s); {vistos} listados")
        if vistos < total:
            completo = False
    return no_site, completo


def verificar_site(con, arm, max_novos: int, salvar_parcial) -> dict:
    res = {"novos": 0, "alterados": 0, "erros": 0, "no_site": 0}
    todas = "TODAS" in [s.upper() for s in config.SITUACOES]
    no_site, completo = listar_site(_cliente())
    res["no_site"] = len(no_site)

    def sit_lista(i):
        return banco.normalizar_situacao(no_site[i].get("situacao"))

    na_base, vista_antes = {}, {}
    for r in con.execute("SELECT id, situacao, situacao_lista FROM atos"):
        na_base[r[0]] = r[1]
        vista_antes[r[0]] = r[2] or r[1]
    novos = [i for i in no_site if i not in na_base]
    # compara com a situação que a listagem mostrou da última vez: o site às vezes mostra uma situação
    # na lista e outra no detalhe, e sem isso o mesmo ato seria baixado de novo todo dia
    mudaram = [i for i in no_site if i in na_base and sit_lista(i) != banco.normalizar_situacao(vista_antes[i])]
    # atos que sumiram da listagem (ou, sem TODAS, saíram das situações acompanhadas);
    # só confiável se as listagens vieram completas
    if not completo:
        sairam = []
    elif todas:
        sairam = [i for i in na_base if i not in no_site]
    else:
        sairam = [i for i, s in na_base.items() if i not in no_site and s in config.SITUACOES]
    log(f"{len(novos)} novo(s), {len(mudaram)} com situação alterada, {len(sairam)} fora da listagem")

    fila = [(i, no_site[i]) for i in novos[:max_novos]]
    fila += [(i, no_site.get(i, {"id": i})) for i in (mudaram + sairam)[: config.MAX_REVISOES]]
    falharam = set()
    inicio = time.monotonic()
    with ThreadPoolExecutor(config.CONCORRENCIA) as baixar, ThreadPoolExecutor(8) as enviar:
        futuros = {baixar.submit(_baixar_ato, i, reg): i for i, reg in fila}
        envios = []
        for k, fut in enumerate(as_completed(futuros), 1):
            id_ato = futuros[fut]
            try:
                ato, md = fut.result()
                r = banco.gravar_ato(con, ato, "diario")
            except Exception as e:  # noqa: BLE001
                log(f"   ! ato {id_ato}: {e}")
                res["erros"] += 1
                falharam.add(id_ato)
                continue
            if r != "igual":
                envios.append(enviar.submit(arm.enviar_texto, md, f"{config.PREFIXO_ATOS}{ato['arquivo']}"))
            if r == "novo":
                res["novos"] += 1
            elif r == "alterado":
                res["alterados"] += 1
            if k % 200 == 0:
                ritmo = k / max(time.monotonic() - inicio, 1)
                log(f"   {k}/{len(fila)} ({ritmo:.1f} atos/s, faltam ~{(len(fila) - k) / ritmo / 60:.0f} min)")
                con.commit()
            if k % config.CHECKPOINT == 0:
                log("   salvando progresso no armazenamento ...")
                salvar_parcial()
        for f in envios:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log(f"   ! falha ao gravar .md no armazenamento: {e}")
    if len(novos) > max_novos:
        log(f"   limite de {max_novos} atos novos por execução; o restante vem nas próximas")
    revisados = {i for i, _ in fila} - falharam
    con.executemany(
        "UPDATE atos SET situacao_lista = ? WHERE id = ?",
        [(sit_lista(i), i) for i in no_site if i in revisados or (i in na_base and i not in mudaram)],
    )
    return res


# ------------------------------------------------------------------ principal

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sem-site", action="store_true", help="não consulta o site do TJPR")
    ap.add_argument("--sem-classificar", action="store_true", help="não chama a API do Claude")
    ap.add_argument("--max-novos", type=int, default=config.MAX_NOVOS)
    a = ap.parse_args()

    inicio = time.monotonic()
    arm = armazenamento.abrir()
    log(f"Armazenamento: {arm.descricao()}")
    config.TRABALHO_DIR.mkdir(parents=True, exist_ok=True)
    caminho_base = config.TRABALHO_DIR / "trabalho.sqlite"
    caminho_vetores = config.TRABALHO_DIR / "trabalho_vetores.npz"
    caminho_base.unlink(missing_ok=True)

    tinha_base = arm.baixar(config.OBJ_BASE, caminho_base)
    con = banco.abrir(caminho_base)
    temas = classificacao.carregar_temas()
    banco.sincronizar_temas(con, temas)
    relatorio = {"inicio": banco.agora()}
    falhou = False

    if not tinha_base:
        log("Base ainda não existe: montando a partir dos atos enviados")
        relatorio["carga_inicial"] = carga_inicial(con, arm)
        log(f"Carga inicial: {relatorio['carga_inicial']} ato(s)")
        banco.meta(con, "carga_inicial_em", banco.agora())
    con.commit()

    relatorio["classificacoes_importadas"] = importar_classificacoes(con, arm, temas)
    con.commit()

    def salvar_parcial():
        """Grava a base no armazenamento no meio de uma execução longa (permite retomar)."""
        banco.reconstruir_fts(con)
        con.commit()
        arm.enviar(caminho_base, config.OBJ_BASE)

    if not a.sem_site:
        try:
            relatorio["site"] = verificar_site(con, arm, a.max_novos, salvar_parcial)
        except Exception as e:  # noqa: BLE001  (o restante da atualização continua)
            log(f"! Falha ao consultar o site do TJPR: {e}")
            relatorio["site"] = {"erro": str(e)}
            falhou = True
        con.commit()

    if not a.sem_classificar:
        relatorio["classificacao"] = classificacao.classificar_novos(con, temas, log)
        con.commit()

    log("Atualizando índice de texto ...")
    banco.reconstruir_fts(con)
    con.commit()

    log("Atualizando vetores da pesquisa semântica ...")
    tinha_vetores = arm.baixar(config.OBJ_VETORES, caminho_vetores)
    indice = semantica.Indice.carregar(caminho_vetores) if tinha_vetores else semantica.Indice()
    indice, n = semantica.atualizar_indice(
        con, indice, banco.meta(con, "vetores_ate"),
        ao_progredir=lambda f, t: log(f"   vetores {f}/{t}") if f % 2048 < 256 or f == t else None,
    )
    relatorio["vetores_gerados"] = n
    banco.meta(con, "vetores_ate", banco.agora())

    total = con.execute("SELECT count(*) FROM atos").fetchone()[0]
    com_tema = con.execute("SELECT count(DISTINCT id) FROM ato_temas").fetchone()[0]
    relatorio.update({"fim": banco.agora(), "total_atos": total, "com_tema": com_tema})
    banco.meta(con, "ultima_atualizacao", banco.agora())
    banco.meta(con, "relatorio", json.dumps(relatorio, ensure_ascii=False))
    con.commit()
    con.execute("VACUUM")
    con.close()

    if n or not tinha_vetores:
        indice.salvar(caminho_vetores)
        arm.enviar(caminho_vetores, config.OBJ_VETORES)
    arm.enviar(caminho_base, config.OBJ_BASE)  # por último: o site recarrega quando a base muda
    log(f"Concluído em {time.monotonic() - inicio:.0f}s: {json.dumps(relatorio, ensure_ascii=False)}")
    sys.exit(1 if falhou else 0)


if __name__ == "__main__":
    main()
