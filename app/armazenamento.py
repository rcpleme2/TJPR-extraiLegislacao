"""Armazenamento permanente: Cloud Storage (produção) ou pasta local (testes)."""

import shutil
from pathlib import Path

from . import config


class Local:
    def __init__(self, raiz: Path):
        self.raiz = Path(raiz)
        self.raiz.mkdir(parents=True, exist_ok=True)

    def descricao(self):
        return f"pasta local {self.raiz}"

    def versao(self, nome: str) -> str | None:
        p = self.raiz / nome
        return f"{p.stat().st_mtime_ns}-{p.stat().st_size}" if p.exists() else None

    def baixar(self, nome: str, destino: Path) -> bool:
        p = self.raiz / nome
        if not p.exists():
            return False
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, destino)
        return True

    def ler_bytes(self, nome: str) -> bytes:
        return (self.raiz / nome).read_bytes()

    def enviar(self, origem: Path, nome: str):
        p = self.raiz / nome
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        shutil.copyfile(origem, tmp)
        tmp.replace(p)

    def enviar_texto(self, texto: str, nome: str):
        p = self.raiz / nome
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(texto, encoding="utf-8")

    def listar(self, prefixo: str) -> list[str]:
        base = self.raiz / prefixo
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.raiz)).replace("\\", "/") for p in base.rglob("*") if p.is_file())


class GCS:
    def __init__(self, bucket: str):
        from google.cloud import storage  # importado só quando usado

        self.cliente = storage.Client()
        self.bucket = self.cliente.bucket(bucket)
        self.nome_bucket = bucket

    def descricao(self):
        return f"gs://{self.nome_bucket}"

    def versao(self, nome: str) -> str | None:
        blob = self.bucket.get_blob(nome)
        return str(blob.generation) if blob else None

    def baixar(self, nome: str, destino: Path) -> bool:
        blob = self.bucket.get_blob(nome)
        if blob is None:
            return False
        destino.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(destino))
        return True

    def ler_bytes(self, nome: str) -> bytes:
        return self.bucket.blob(nome).download_as_bytes()

    def enviar(self, origem: Path, nome: str):
        self.bucket.blob(nome).upload_from_filename(str(origem), timeout=600)

    def enviar_texto(self, texto: str, nome: str):
        self.bucket.blob(nome).upload_from_string(texto.encode("utf-8"), content_type="text/markdown; charset=utf-8")

    def listar(self, prefixo: str) -> list[str]:
        return sorted(b.name for b in self.cliente.list_blobs(self.nome_bucket, prefix=prefixo) if not b.name.endswith("/"))


def abrir():
    return GCS(config.BUCKET) if config.BUCKET else Local(config.DADOS_DIR)
