"""Site público de consulta (FastAPI). Só lê a base; quem escreve é o job de atualização."""

import json
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from . import armazenamento, banco, busca, config, semantica

AQUI = Path(__file__).parent
MODOS = {"inteligente": "Combinada", "palavras": "Palavras exatas", "significado": "Por significado"}


# ------------------------------------------------------------------ dados (recarregados quando o job publica)

class Repositorio:
    def __init__(self):
        self.arm = armazenamento.abrir()
        self.versao = None
        self.caminho = None
        self.indice = None
        self.semantica_pronta = False
        self.erro = ""
        self._trava = threading.Lock()

    def recarregar(self):
        try:
            versao = self.arm.versao(config.OBJ_BASE)
            if versao is None or versao == self.versao:
                return
            config.TRABALHO_DIR.mkdir(parents=True, exist_ok=True)
            nova = config.TRABALHO_DIR / f"site-{versao}.sqlite"
            vet = config.TRABALHO_DIR / f"site-{versao}.npz"
            self.arm.baixar(config.OBJ_BASE, nova)
            indice = semantica.Indice.carregar(vet) if self.arm.baixar(config.OBJ_VETORES, vet) else None
            antigo = self.caminho
            with self._trava:
                self.caminho, self.indice, self.versao, self.erro = nova, indice, versao, ""
            print(f"Base carregada: versão {versao}", flush=True)
            if antigo and antigo != nova:  # dá tempo para requisições em andamento terminarem
                threading.Timer(60, lambda: (antigo.unlink(missing_ok=True),
                                             antigo.with_suffix(".npz").unlink(missing_ok=True))).start()
        except Exception as e:  # noqa: BLE001
            self.erro = str(e)
            print(f"Falha ao carregar a base: {e}", flush=True)

    def preparar_modelo(self):
        try:
            semantica.modelo()
            semantica.vetor_da_consulta("aquecimento")
            self.semantica_pronta = True
        except Exception as e:  # noqa: BLE001
            print(f"Pesquisa semântica indisponível: {e}", flush=True)

    def iniciar(self):
        # Carrega base e modelo antes de aceitar requisições: no Cloud Run a CPU fica reduzida fora das
        # requisições, então deixar isso em segundo plano poderia atrasar a primeira pesquisa.
        self.recarregar()
        self.preparar_modelo()

        def ciclo():
            while True:
                time.sleep(config.INTERVALO_RECARGA)
                self.recarregar()

        threading.Thread(target=ciclo, daemon=True).start()

    def conexao(self):
        with self._trava:
            caminho, indice = self.caminho, self.indice
        if caminho is None:
            return None, None
        return banco.abrir_leitura(caminho), indice


repo = Repositorio()
app = FastAPI(title="Atos normativos do TJPR", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
modelos = Jinja2Templates(directory=AQUI / "templates")
md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


@app.on_event("startup")
def _inicio():
    repo.iniciar()


# ------------------------------------------------------------------ proteção contra abuso

_acessos: dict[str, deque] = defaultdict(deque)
_trava_acessos = threading.Lock()


def ip_cliente(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")


def excedeu_limite(request: Request) -> bool:
    agora = time.monotonic()
    ip = ip_cliente(request)
    with _trava_acessos:
        fila = _acessos[ip]
        while fila and agora - fila[0] > 60:
            fila.popleft()
        if len(fila) >= config.BUSCAS_POR_MINUTO:
            return True
        fila.append(agora)
        if len(_acessos) > 50000:  # não deixa a tabela crescer sem limite
            _acessos.clear()
    return False


@app.middleware("http")
async def cabecalhos_seguranca(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# ------------------------------------------------------------------ utilidades de modelo

def realce(trecho: str) -> Markup:
    """Destaca os termos encontrados e tira restos de Markdown (quebras com barra, negrito) do trecho."""
    t = re.sub(r"\\(?=\s|$)", " ", trecho or "")
    t = re.sub(r"[*_]{1,3}(?=\S)|(?<=\S)[*_]{1,3}", "", t)
    t = re.sub(r"\s+", " ", t)
    return Markup(str(escape(t)).replace("\u0002", "<mark>").replace("\u0003", "</mark>"))


def data_br(iso: str | None) -> str:
    if iso and re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
        a, m, d = iso.split("-")
        return f"{d}/{m}/{a}"
    return iso or ""


_MINUSCULAS = {"da", "de", "do", "das", "dos", "e", "a", "o", "em", "para", "por", "com", "na", "no"}
_SITUACOES = {"SEMEFEITO": "Sem efeito", banco.SEM_SITUACAO: "Sem situação definida"}


def nome_proprio(texto: str | None) -> str:
    """CORREGEDORIA-GERAL DA JUSTIÇA -> Corregedoria-Geral da Justiça (só para textos todo em maiúsculas)."""
    t = texto or ""
    if not t or t != t.upper():
        return t
    palavras = []
    for k, p in enumerate(t.lower().split(" ")):
        if k and p in _MINUSCULAS:
            palavras.append(p)
        else:
            palavras.append("-".join(x[:1].upper() + x[1:] for x in p.split("-")))
    return " ".join(palavras)


def rotulo_sit(sit: str | None) -> str:
    s = (sit or "").upper()
    return _SITUACOES.get(s, s.capitalize())


def classe_sit(sit: str | None) -> str:
    return re.sub(r"[^a-z]", "", (sit or "").lower())


modelos.env.filters["realce"] = realce
modelos.env.filters["data_br"] = data_br
modelos.env.filters["nome_proprio"] = nome_proprio
modelos.env.filters["rotulo_sit"] = rotulo_sit
modelos.env.filters["classe_sit"] = classe_sit
modelos.env.globals["rotulo_tema"] = lambda item: item[1]
modelos.env.globals["rotulo_origem"] = lambda item: nome_proprio(item[0])
modelos.env.globals["rotulo_situacao"] = lambda item: rotulo_sit(item[0])


def parametros(request: Request) -> dict:
    p = request.query_params
    filtros = {c: (p.get(c) or "").strip()[:200] for c in busca.FILTROS}
    if filtros["ano"] and not filtros["ano"].isdigit():
        filtros["ano"] = ""
    modo = p.get("modo") if p.get("modo") in MODOS else "inteligente"
    try:
        pagina = max(1, min(int(p.get("pagina", "1")), 500))
    except ValueError:
        pagina = 1
    return {"q": (p.get("q") or "").strip()[:300], "modo": modo, "pagina": pagina, "filtros": filtros}


def construtor_url(par: dict):
    def url(**mudancas) -> str:
        base = {"q": par["q"], "modo": par["modo"], **par["filtros"]}
        base.update(mudancas)
        if "pagina" not in mudancas:
            base.pop("pagina", None)
        if base.get("modo") == "inteligente":
            base.pop("modo")
        return "/?" + urlencode({k: v for k, v in base.items() if v not in ("", None)})
    return url


def contexto(request: Request, **extra) -> dict:
    return {"request": request, "busca_no_topo": False, "pagina_atual": "", **extra}


def info_base(con) -> dict:
    return {
        "atualizado": data_br((banco.meta(con, "ultima_atualizacao") or "")[:10]),
        "total": con.execute("SELECT count(*) FROM atos").fetchone()[0],
    }


def indisponivel(request: Request):
    return modelos.TemplateResponse(
        request, "aviso.html",
        contexto(request, titulo="Base em preparação",
                 mensagem="A base de atos ainda está sendo carregada. Tente novamente em alguns minutos."),
        status_code=503,
    )


# ------------------------------------------------------------------ páginas

@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    par = parametros(request)
    filtros_ativos = any(par["filtros"].values())
    pagina_inicial = not par["q"] and not filtros_ativos and par["pagina"] == 1 and "ordem" not in request.query_params
    if not pagina_inicial and excedeu_limite(request):
        return modelos.TemplateResponse(
            request, "aviso.html",
            contexto(request, titulo="Muitas pesquisas seguidas",
                     mensagem="Foram feitas muitas pesquisas em pouco tempo a partir desta conexão. Aguarde um minuto e tente de novo."),
            status_code=429,
        )
    con, indice = repo.conexao()
    if con is None:
        return indisponivel(request)
    try:
        info = info_base(con)
        if pagina_inicial:
            temas = con.execute(
                "SELECT t.codigo, t.nome, count(at.id) AS n FROM temas t "
                "LEFT JOIN ato_temas at ON at.tema = t.codigo GROUP BY t.codigo ORDER BY n DESC, t.ordem").fetchall()
            sem_tema = con.execute("SELECT count(*) FROM atos WHERE id NOT IN (SELECT id FROM ato_temas)").fetchone()[0]
            ids = [r[0] for r in con.execute("SELECT id FROM atos ORDER BY data_diario DESC, id DESC LIMIT 6")]
            recentes = busca.carregar_itens(con, ids, "", False)
            return modelos.TemplateResponse(request, "home.html", contexto(
                request, info=info, temas=temas, sem_tema=sem_tema, recentes=recentes))
        res = busca.pesquisar(con, indice, par["q"], par["modo"], par["filtros"], par["pagina"], repo.semantica_pronta)
    finally:
        con.close()
    paginas = max(1, -(-res.total // busca.POR_PAGINA))
    return modelos.TemplateResponse(request, "resultados.html", contexto(
        request, par=par, res=res, url=construtor_url(par), modos=MODOS, paginas=paginas, info=info,
        filtros_ativos=filtros_ativos, busca_no_topo=True, q_topo=par["q"],
    ))


@app.get("/ato/{id_ato}", response_class=HTMLResponse)
def ato(request: Request, id_ato: int):
    con, _ = repo.conexao()
    if con is None:
        return indisponivel(request)
    try:
        linha = con.execute("SELECT * FROM atos WHERE id = ?", (id_ato,)).fetchone()
        if linha is None:
            return modelos.TemplateResponse(
                request, "aviso.html",
                contexto(request, titulo="Ato não encontrado", mensagem="Não há ato com esse identificador na base."),
                status_code=404,
            )
        temas = con.execute(
            "SELECT t.codigo, t.nome, at.origem FROM ato_temas at JOIN temas t ON t.codigo = at.tema "
            "WHERE at.id = ? ORDER BY t.ordem", (id_ato,)).fetchall()
        info = info_base(con)
    finally:
        con.close()
    a = dict(linha)
    for campo in ("anexos", "referencias", "palavras_chave"):
        try:
            a[campo] = json.loads(a[campo] or "[]")
        except ValueError:
            a[campo] = []
    for r in a["referencias"]:
        m = re.search(r"/atos/documento/(\d+)", r.get("link") or "")
        r["interno"] = f"/ato/{m.group(1)}" if m else ""
    return modelos.TemplateResponse(request, "ato.html", contexto(
        request, a=a, temas=temas, texto_html=Markup(md.render(a["texto"] or "")), info=info, busca_no_topo=True,
    ))


@app.get("/temas", response_class=HTMLResponse)
def temas(request: Request):
    con, _ = repo.conexao()
    if con is None:
        return indisponivel(request)
    try:
        lista = con.execute(
            "SELECT t.codigo, t.nome, t.descricao, count(at.id) AS n FROM temas t "
            "LEFT JOIN ato_temas at ON at.tema = t.codigo GROUP BY t.codigo ORDER BY t.ordem").fetchall()
        sem_tema = con.execute("SELECT count(*) FROM atos WHERE id NOT IN (SELECT id FROM ato_temas)").fetchone()[0]
        info = info_base(con)
    finally:
        con.close()
    return modelos.TemplateResponse(request, "temas.html", contexto(
        request, lista=lista, sem_tema=sem_tema, info=info, pagina_atual="temas", busca_no_topo=True))


@app.get("/sobre", response_class=HTMLResponse)
def sobre(request: Request):
    con, _ = repo.conexao()
    info, rel = {}, {}
    if con is not None:
        try:
            info = info_base(con)
            rel = json.loads(banco.meta(con, "relatorio") or "{}")
            rel["por_situacao"] = con.execute(
                "SELECT situacao, count(*) FROM atos GROUP BY 1 ORDER BY 2 DESC").fetchall()
            rel["classificados"] = con.execute(
                "SELECT origem, count(DISTINCT id) FROM ato_temas GROUP BY 1 ORDER BY 2 DESC").fetchall()
        finally:
            con.close()
    return modelos.TemplateResponse(request, "sobre.html", contexto(
        request, info=info, rel=rel, modelo=config.MODELO_EMBEDDINGS, semantica_pronta=repo.semantica_pronta,
        pagina_atual="sobre", busca_no_topo=True))


@app.get("/api/pesquisa")
def api_pesquisa(request: Request):
    if excedeu_limite(request):
        return JSONResponse({"erro": "muitas consultas; aguarde um minuto"}, status_code=429)
    par = parametros(request)
    con, indice = repo.conexao()
    if con is None:
        return JSONResponse({"erro": "base em preparação"}, status_code=503)
    try:
        res = busca.pesquisar(con, indice, par["q"], par["modo"], par["filtros"], par["pagina"], repo.semantica_pronta)
    finally:
        con.close()
    for it in res.itens:
        it["trecho"] = it["trecho"].replace("\u0002", "").replace("\u0003", "")
        it["url"] = f"/ato/{it['id']}"
    return {"total": res.total, "modo": res.modo, "pagina": par["pagina"], "itens": res.itens}


@app.get("/saude")
def saude():
    return {"base": repo.caminho is not None, "semantica": repo.semantica_pronta, "erro": repo.erro}
