#!/usr/bin/env python3
"""
Extrai os atos normativos do TJPR (https://www.tjpr.jus.br/legislacao-atos-normativos)
e salva cada ato em um arquivo .md.

O script reproduz o que a página faz no navegador: abre a pesquisa, filtra pela
"Situação" escolhida (sem marcar nenhum tipo de documento, ou seja, todos os tipos),
percorre todas as páginas do resultado e abre cada ato.

Por padrão processa, nesta ordem, as situações Vigente, Republicado e Reveiculado,
gravando cada uma em sua própria pasta:

    atos/Vigente/
    atos/Republicado/
    atos/Reveiculado/

Nome do arquivo: "[número do ato] - [órgão] / [ano do ato] - [Assunto].md"
(a barra "/" não é permitida em nome de arquivo; ela é trocada por "∕", que tem
a mesma aparência — veja a opção --barra).

Uso:
    pip install requests beautifulsoup4 markdownify
    python3 extrai_legislacao.py                       # Vigente, Republicado, Reveiculado
    python3 extrai_legislacao.py --situacao REPUBLICADO
    python3 extrai_legislacao.py --saida /caminho/pasta --limite 20

O script pode ser interrompido e executado de novo: os atos já baixados
(registrados em ".baixados.json" em cada pasta) são pulados.
"""

import argparse
import shutil
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    from markdownify import markdownify
except ModuleNotFoundError as _e:
    sys.exit(
        f"Falta a biblioteca '{_e.name}'. Instale as dependências com:\n"
        "    py -m pip install requests beautifulsoup4 markdownify\n"
        "(no Linux/macOS: python3 -m pip install requests beautifulsoup4 markdownify)"
    )

BASE = "https://www.tjpr.jus.br"
PAGINA = BASE + "/legislacao-atos-normativos"
RECURSO = (
    PAGINA
    + "?p_p_id=br_jus_tjpr_portlet_atosNormativos_AtosNormativosPortlet"
    "&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage&p_p_resource_id="
)
URL_PESQUISAR = RECURSO + "%2Fpesquisar"
URL_MATERIA = RECURSO + "%2FobterMateria"
URL_DOCUMENTO = PAGINA + "/-/atos/documento/{id}"

# Valor -> nome da pasta. Os valores são os do <select> "Situação" da página.
SITUACOES = {
    "VIGENTE": "Vigente",
    "REPUBLICADO": "Republicado",
    "REVEICULADO": "Reveiculado",
    "REINDEXADO": "Reindexado",
    "SUSPENSO": "Suspenso",
    "REVOGADO": "Revogado",
    "SEMEFEITO": "Sem Efeito",
    "ALTERADO": "Alterado",
    "CUMPRIDO": "Cumprido",
}
PADRAO = ["VIGENTE", "REPUBLICADO", "REVEICULADO"]

TAMANHO_PAGINA = 100  # o servidor não devolve mais que 100 por página
MAX_NOME = 200  # caracteres do nome do arquivo (o limite dos sistemas é 255 bytes)


# Barra de progresso ativa (se houver); avisos passam por ela para não embaralhar a tela.
_progresso = None


def avisar(msg: str):
    if _progresso is not None:
        _progresso.escrever(msg, erro=True)
    else:
        print(msg, file=sys.stderr, flush=True)


class Progresso:
    """Barra de progresso simples, sem dependências:
    [██████████··········]  52,3%  4093/7821 | novos 120 | pulados 3970 | erros 3 | resta ~2h04min
    """

    def __init__(self, total: int, rotulo: str, ja_na_pasta: int = 0):
        self.total = max(total, 1)
        self.rotulo = rotulo
        self.ja_na_pasta = ja_na_pasta
        self.feitos = self.novos = self.pulados = self.erros = 0
        self.tempo_baixando = 0.0
        self._ultimo_desenho = 0.0

    @staticmethod
    def _hms(seg: float) -> str:
        seg = int(seg)
        h, r = divmod(seg, 3600)
        m, s = divmod(r, 60)
        return f"{h}h{m:02d}min" if h else (f"{m}min{s:02d}s" if m else f"{s}s")

    def _linha(self) -> str:
        frac = min(self.feitos / self.total, 1.0)
        largura = shutil.get_terminal_size((100, 20)).columns
        info = (
            f" {frac * 100:5.1f}%  {self.feitos}/{self.total}"
            f" | novos {self.novos} | pulados {self.pulados} | erros {self.erros}"
        )
        if self.novos and self.feitos < self.total:
            media = self.tempo_baixando / (self.novos + self.erros)
            # só os que ainda faltam baixar contam para o tempo (os já salvos são pulados na hora)
            faltam = self.total - self.ja_na_pasta - self.novos - self.erros
            faltam = max(0, min(faltam, self.total - self.feitos))
            info += f" | resta ~{self._hms(media * faltam)}"
        n = max(10, min(40, largura - len(info) - 4))
        cheio = int(frac * n)
        return f"[{'█' * cheio}{'·' * (n - cheio)}]{info}"[: largura - 1]

    def desenhar(self, forcar=False):
        agora = time.monotonic()
        if forcar or agora - self._ultimo_desenho >= 0.1:
            self._ultimo_desenho = agora
            largura = shutil.get_terminal_size((100, 20)).columns
            sys.stdout.write("\r" + self._linha().ljust(largura - 1))
            sys.stdout.flush()

    def escrever(self, msg: str, erro=False):
        """Imprime uma linha acima da barra e redesenha a barra."""
        largura = shutil.get_terminal_size((100, 20)).columns
        sys.stdout.write("\r" + " " * (largura - 1) + "\r")
        sys.stdout.flush()
        print(msg, file=sys.stderr if erro else sys.stdout, flush=True)
        self.desenhar(forcar=True)

    def avancar(self, novo=False, pulado=False, erro=False, segundos=0.0):
        self.feitos += 1
        self.novos += novo
        self.pulados += pulado
        self.erros += erro
        self.tempo_baixando += segundos
        self.desenhar(forcar=novo or erro)

    def fechar(self):
        self.desenhar(forcar=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


class Cliente:
    def __init__(self, pausa: float, tentativas: int = 5):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )
        self.pausa = pausa
        self.tentativas = tentativas
        self.tipos_documento = ""

    def _req(self, metodo, url, **kw):
        for i in range(self.tentativas):
            try:
                r = self.s.request(metodo, url, timeout=120, **kw)
                r.raise_for_status()
                time.sleep(self.pausa)
                return r
            except requests.RequestException as e:
                espera = 2 ** (i + 1)
                avisar(f"   ! erro de conexão ({e}); nova tentativa em {espera}s")
                time.sleep(espera)
        raise RuntimeError(f"falha após {self.tentativas} tentativas: {url}")

    def abrir_pagina(self):
        """Abre a página de pesquisa (cookies de sessão) e lê os tipos de documento."""
        r = self._req("GET", PAGINA)
        soup = BeautifulSoup(r.text, "html.parser")
        valores = [
            cb["value"]
            for cb in soup.select("input[name$='_tiposDocumentoCheckbox']")
            if cb.get("value")
        ]
        if not valores:
            raise RuntimeError("não encontrei os tipos de documento na página de pesquisa")
        # Mesmo comportamento da página: nenhum tipo marcado = todos os tipos.
        self.tipos_documento = ",".join(valores) + ","

    def pesquisar(self, situacao: str, pagina: int) -> dict:
        dados = {
            "tiposDocumento": self.tipos_documento,
            "texto": "",
            "assunto": "",
            "numero": "",
            "ano": "",
            "situacao": situacao,
            "idOrgao": "",
            "pageNumber": str(pagina),
            "pageSize": str(TAMANHO_PAGINA),
            "ordenacao": "materiaDataPublicacao",
            "ordem": "DESC",
        }
        for i in range(self.tentativas):
            r = self._req("POST", URL_PESQUISAR, data=dados)
            try:
                j = r.json()
            except ValueError:
                j = {}
            out = j.get("output")
            if str(j.get("status")) != "500" and isinstance(out, dict) and "total" in out:
                return out
            time.sleep(2 ** (i + 1))
        raise RuntimeError(f"servidor retornou erro na página {pagina} ({situacao})")

    def materia(self, id_doc) -> dict:
        # O servidor às vezes responde status 500 / lista vazia e, logo depois, responde
        # normalmente para o mesmo ato; por isso há novas tentativas.
        for i in range(self.tentativas):
            r = self._req("POST", URL_MATERIA, data={"detalheID": str(id_doc)})
            try:
                j = r.json()
            except ValueError:
                j = {}
            out = j.get("output")
            if str(j.get("status")) != "500" and isinstance(out, dict) and out:
                return out
            time.sleep(2 ** (i + 1))
        raise RuntimeError(f"servidor retornou erro no ato {id_doc}")


def limpar(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip()


def numero_e_ano(m: dict, registro: dict):
    """'17/2013' -> ('17', '2013'). Usa a lista de resultados como reserva."""
    nd = limpar(m.get("numeroDocumento"))
    if "/" in nd:
        num, ano = nd.rsplit("/", 1)
        return num.strip(), ano.strip()
    nd = limpar(registro.get("numeroDocumento"))  # formato 'AAAANNNNNNNN'
    if len(nd) > 4 and nd[:4].isdigit():
        return nd[5:].lstrip("0") or "0", nd[:4]
    data = limpar(m.get("dataPublicacao"))
    return nd or "s-n", data[:4] if data[:4].isdigit() else "s-a"


def nome_arquivo(numero, orgao, ano, assunto, barra: str) -> str:
    nome = f"{numero} - {orgao} / {ano} - {assunto}"
    nome = unicodedata.normalize("NFC", nome).replace("/", barra)
    nome = re.sub(r'[\\:*?"<>|\x00-\x1f]', "", nome)
    nome = limpar(nome).rstrip(" .")
    if len(nome) > MAX_NOME:
        nome = nome[: MAX_NOME - 1].rstrip(" .") + "…"
    return nome + ".md"


def html_para_md(html: str) -> str:
    if not html:
        return ""
    html = html.replace("../", "https://portal.tjpr.jus.br/pesquisa_athos/")
    md = markdownify(
        html, heading_style="ATX", bullets="-", newline_style="BACKSLASH", strip=["script", "style"]
    )
    md = md.replace(" ", " ")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"(\\\n\s*){3,}", "\\\n\\\n", md)  # no máximo uma linha em branco seguida
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def montar_md(m: dict, id_doc) -> str:
    def esc(v):
        return limpar(v).replace("|", "\\|")

    data = limpar(m.get("dataPublicacao"))[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", data):
        a, mm, d = data.split("-")
        data = f"{d}/{mm}/{a}"

    titulo = " ".join(
        x for x in [limpar(m.get("tipoDocumento")), limpar(m.get("numeroDocumento"))] if x
    )
    linhas = [f"# {titulo or 'Ato ' + str(id_doc)}", ""]
    campos = [
        ("Tipo", m.get("tipoDocumento")),
        ("Ato", m.get("tipoAto")),
        ("Número", m.get("numeroDocumento")),
        ("Origem", m.get("orgao")),
        ("Documento", m.get("descricao")),
        ("Assunto", m.get("assunto")),
        ("Ementa", m.get("ementa")),
        ("Situação", m.get("situacao") if m.get("situacao") != "-1" else ""),
        ("Data do diário", data),
        ("Diário", m.get("numeroDiario")),
        ("Fonte", URL_DOCUMENTO.format(id=id_doc)),
    ]
    linhas += ["| Campo | Valor |", "|---|---|"]
    linhas += [f"| {k} | {esc(v)} |" for k, v in campos if limpar(v)]

    anexos = m.get("anexos") or []
    if anexos:
        linhas += ["", "## Anexos", ""]
        for a in anexos:
            linhas.append(f"- [{limpar(a.get('nomeArquivo'))}]({a.get('link', '')})")

    refs = m.get("referencias") or []
    if refs:
        linhas += ["", "## Referências", ""]
        for r in refs:
            desc = limpar(r.get("descricao"))
            if r.get("id"):
                desc = f"[{desc}]({URL_DOCUMENTO.format(id=r['id'])})"
            elif r.get("link"):
                desc = f"[{desc}]({r['link']})"
            linhas.append(f"- **{limpar(r.get('tipo'))}:** {desc}")

    linhas += ["", "## Texto", "", html_para_md(m.get("texto") or "") or "_(sem texto)_", ""]
    return "\n".join(linhas)


RE_FONTE = re.compile(r"^\| Fonte \| .*?/atos/documento/(\d+)", re.M)


def atos_na_pasta(pasta: Path) -> dict:
    """Lê os .md já existentes na pasta e devolve {id do ato: nome do arquivo}.

    O id vem da linha "| Fonte | .../atos/documento/<id> |" que o script grava em
    todo arquivo; assim os atos são reconhecidos mesmo sem o .baixados.json, ou se
    os arquivos tiverem sido copiados de outra máquina ou renomeados.
    """
    achados = {}
    for arq in pasta.glob("*.md"):
        try:
            with open(arq, encoding="utf-8", errors="replace") as f:
                inicio = f.read(30000)  # a tabela de dados fica no começo do arquivo
        except OSError:
            continue
        m = RE_FONTE.search(inicio)
        if m:
            achados[m.group(1)] = arq.name
    return achados


def processar(cli: Cliente, situacao: str, saida: Path, barra: str, limite: int | None):
    global _progresso
    rotulo = SITUACOES[situacao]
    pasta = saida / rotulo
    pasta.mkdir(parents=True, exist_ok=True)
    arq_controle = pasta / ".baixados.json"

    print(f"\n=== Situação: {rotulo} ===")
    print(f"Verificando arquivos já existentes em {pasta} ...", flush=True)
    baixados = {}
    if arq_controle.exists():
        try:
            baixados = json.loads(arq_controle.read_text("utf-8"))
        except ValueError:
            baixados = {}
    # só vale o que existe de fato na pasta; os arquivos lidos têm prioridade
    baixados = {k: v for k, v in baixados.items() if (pasta / v).exists()}
    baixados.update(atos_na_pasta(pasta))
    usados = set(baixados.values())
    print(f"{len(baixados)} ato(s) já estão na pasta e serão pulados.")

    def salvar_controle():
        arq_controle.write_text(json.dumps(baixados, ensure_ascii=False, indent=0), "utf-8")

    pagina, total, vistos, erros = 1, None, 0, []
    prog = None
    try:
        while True:
            out = cli.pesquisar(situacao, pagina)
            regs = out.get("registros") or []
            if total is None:
                total = out.get("total", 0)
                paginas = -(-total // TAMANHO_PAGINA)
                print(f"{total} registro(s) no site, em {paginas} página(s) de {TAMANHO_PAGINA}")
                alvo = total if limite is None else min(total, limite)
                prog = _progresso = Progresso(alvo, rotulo, ja_na_pasta=len(baixados))
                prog.desenhar(forcar=True)
            if not regs:
                break
            for reg in regs:
                if limite is not None and vistos >= limite:
                    break
                vistos += 1
                id_doc = str(reg["id"])
                if id_doc in baixados:
                    prog.avancar(pulado=True)
                    continue
                t0 = time.monotonic()
                try:
                    m = cli.materia(id_doc)
                except Exception as e:  # noqa: BLE001
                    prog.escrever(f"   ! ato {id_doc}: {e}", erro=True)
                    erros.append(id_doc)
                    prog.avancar(erro=True, segundos=time.monotonic() - t0)
                    continue
                num, ano = numero_e_ano(m, reg)
                orgao = limpar(m.get("orgao")) or str(reg.get("orgao", ""))
                assunto = limpar(m.get("assunto")) or limpar(m.get("descricao")) or "Sem assunto"
                nome = nome_arquivo(num, orgao, ano, assunto, barra)
                if nome in usados:  # dois atos com mesmo número/órgão/ano/assunto
                    nome = nome[:-3] + f" ({id_doc}).md"
                (pasta / nome).write_text(montar_md(m, id_doc), encoding="utf-8")
                baixados[id_doc] = nome
                usados.add(nome)
                prog.avancar(novo=True, segundos=time.monotonic() - t0)
                if prog.novos % 20 == 0:
                    salvar_controle()
            salvar_controle()
            if (limite is not None and vistos >= limite) or pagina * TAMANHO_PAGINA >= total:
                break
            pagina += 1
    finally:
        salvar_controle()
        if prog is not None:
            prog.fechar()
        _progresso = None

    print(
        f"Concluído ({rotulo}): {prog.novos if prog else 0} novo(s), "
        f"{prog.pulados if prog else 0} pulado(s), {len(baixados)} ato(s) na pasta {pasta}"
    )
    if erros:
        print(f"Atos com erro (rode de novo para tentar outra vez): {', '.join(erros)}")
    return erros


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--situacao", nargs="+", choices=list(SITUACOES), default=PADRAO,
        help="situação(ões) a extrair (padrão: VIGENTE REPUBLICADO REVEICULADO)",
    )
    ap.add_argument("--saida", type=Path, default=Path("atos"), help="pasta de saída (padrão: ./atos)")
    ap.add_argument("--pausa", type=float, default=0.3, help="segundos entre requisições (padrão: 0.3)")
    ap.add_argument("--limite", type=int, help="máximo de atos por situação (para testes)")
    ap.add_argument(
        "--barra", default="∕",
        help='caractere que substitui "/" no nome do arquivo (padrão: "∕", U+2215)',
    )
    a = ap.parse_args()

    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    cli = Cliente(pausa=a.pausa)
    cli.abrir_pagina()
    houve_erro = False
    try:
        for s in a.situacao:
            houve_erro |= bool(processar(cli, s, a.saida, a.barra, a.limite))
    except KeyboardInterrupt:
        sys.exit("\nInterrompido. O que já foi salvo será pulado na próxima execução.")
    sys.exit(1 if houve_erro else 0)


if __name__ == "__main__":
    main()
