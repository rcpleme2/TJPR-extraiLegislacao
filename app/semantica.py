"""Pesquisa semântica: um vetor por ato, gerado por um modelo multilíngue que roda localmente."""

import json
import threading
from pathlib import Path

import numpy as np

from . import config

_modelo = None
_trava = threading.Lock()

# multilingual-e5-small: modelo treinado para busca (bom em português), 384 dimensões, ~470 MB.
# Não está na lista padrão do fastembed, então é registrado a partir do ONNX oficial.
E5_SMALL = "intfloat/multilingual-e5-small"


def _registrar_e5_small(TextEmbedding):
    from fastembed.common.model_description import ModelSource, PoolingType

    if any(m["model"] == E5_SMALL for m in TextEmbedding.list_supported_models()):
        return
    TextEmbedding.add_custom_model(
        model=E5_SMALL, pooling=PoolingType.MEAN, normalization=True,
        sources=ModelSource(hf=E5_SMALL), dim=384, model_file="onnx/model.onnx",
    )


def _prefixos() -> tuple[str, str]:
    """Modelos e5 esperam 'query: ' na consulta e 'passage: ' nos documentos."""
    return ("query: ", "passage: ") if "e5" in config.MODELO_EMBEDDINGS.lower() else ("", "")


def modelo():
    """Carrega o modelo uma vez (na imagem do Cloud Run ele já vem baixado)."""
    global _modelo
    with _trava:
        if _modelo is None:
            from fastembed import TextEmbedding

            if config.MODELO_EMBEDDINGS == E5_SMALL:
                _registrar_e5_small(TextEmbedding)
            _modelo = TextEmbedding(config.MODELO_EMBEDDINGS, cache_dir=config.MODELOS_DIR)
    return _modelo


def modelo_carregado() -> bool:
    return _modelo is not None


def texto_do_ato(linha) -> str:
    """Resumo do ato usado para gerar o vetor: identificação, ementa, assunto e início do texto."""
    try:
        termos = "; ".join(json.loads(linha["palavras_chave"] or "[]"))
    except ValueError:
        termos = linha["assunto"] or ""
    partes = [
        f"{linha['tipo'] or ''} {linha['numero'] or ''} - {linha['origem'] or ''}",
        linha["documento"] or "",
        f"Ementa: {linha['ementa']}" if linha["ementa"] else "",
        f"Assunto: {termos}" if termos else "",
        (linha["texto"] or "")[:1200],
    ]
    return "\n".join(p for p in partes if p.strip())


def _normalizar(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return (m / n).astype(np.float32)


def vetorizar(textos: list[str]) -> np.ndarray:
    if not textos:
        return np.zeros((0, 0), dtype=np.float32)
    return _normalizar(np.array(list(modelo().embed(textos, batch_size=32)), dtype=np.float32))


class Indice:
    def __init__(self, ids=None, matriz=None, nome_modelo=""):
        self.ids = np.asarray(ids if ids is not None else [], dtype=np.int64)
        self.matriz = matriz if matriz is not None else np.zeros((0, 0), dtype=np.float32)
        self.nome_modelo = nome_modelo
        self._pos = {int(i): k for k, i in enumerate(self.ids)}

    def __len__(self):
        return len(self.ids)

    @classmethod
    def carregar(cls, caminho: Path) -> "Indice":
        if not caminho.exists():
            return cls()
        with np.load(caminho, allow_pickle=False) as d:
            return cls(d["ids"], d["matriz"].astype(np.float32), str(d["modelo"]))

    def salvar(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = caminho.with_name(caminho.stem + ".tmp.npz")
        np.savez(tmp, ids=self.ids, matriz=self.matriz.astype(np.float16), modelo=np.array(self.nome_modelo))
        tmp.replace(caminho)

    def contem(self, id_ato: int) -> bool:
        return int(id_ato) in self._pos

    def acrescentar(self, ids: list[int], vetores: np.ndarray):
        if not ids:
            return
        substituir = set(ids)
        manter = [k for k, i in enumerate(self.ids) if int(i) not in substituir]
        ids_novos = np.concatenate([self.ids[manter], np.asarray(ids, dtype=np.int64)])
        if self.matriz.size:
            matriz = np.vstack([self.matriz[manter], vetores])
        else:
            matriz = vetores
        self.__init__(ids_novos, matriz, self.nome_modelo)

    def pesquisar(self, vetor_consulta: np.ndarray, candidatos: set[int] | None = None, limite: int = 100):
        """Devolve [(id, similaridade)] em ordem decrescente."""
        if not len(self):
            return []
        sims = self.matriz @ vetor_consulta
        if candidatos is not None:
            mascara = np.fromiter((int(i) in candidatos for i in self.ids), dtype=bool, count=len(self.ids))
            sims = np.where(mascara, sims, -2.0)
        ordem = np.argsort(-sims)[:limite]
        return [(int(self.ids[k]), float(sims[k])) for k in ordem if sims[k] > -1.5]


def vetor_da_consulta(consulta: str) -> np.ndarray:
    return vetorizar([_prefixos()[0] + consulta])[0]


CAMPOS = "id, tipo, numero, origem, documento, ementa, assunto, palavras_chave, texto"


def atualizar_indice(con, indice: Indice, desde: str | None = None, ao_progredir=None) -> tuple[Indice, int]:
    """Gera vetores para os atos que ainda não têm, e para os alterados depois de `desde`.

    Se o modelo configurado mudou, refaz todos.
    """
    if indice.nome_modelo != config.MODELO_EMBEDDINGS:
        indice = Indice(nome_modelo=config.MODELO_EMBEDDINGS)
        desde = None
    linhas = [
        r for r in con.execute(f"SELECT {CAMPOS}, atualizado_em FROM atos")
        if not indice.contem(r["id"]) or (desde and (r["atualizado_em"] or "") > desde)
    ]
    total = len(linhas)
    for k in range(0, total, 256):
        lote = linhas[k:k + 256]
        indice.acrescentar([r["id"] for r in lote], vetorizar([_prefixos()[1] + texto_do_ato(r) for r in lote]))
        if ao_progredir:
            ao_progredir(min(k + 256, total), total)
    return indice, total
