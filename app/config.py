"""Configuração por variáveis de ambiente (as mesmas no Cloud Run e no teste local)."""

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Onde ficam os dados permanentes. No Cloud Run: BUCKET=<nome do bucket do Cloud Storage>.
# Sem BUCKET, usa uma pasta local (para testar no computador).
BUCKET = os.environ.get("BUCKET", "").strip()
DADOS_DIR = Path(os.environ.get("DADOS_DIR", RAIZ / "dados"))

# Pasta de trabalho temporária (no Cloud Run, /tmp fica em memória).
TRABALHO_DIR = Path(os.environ.get("TRABALHO_DIR", "/tmp/atos-tjpr"))

# Nomes dos objetos no armazenamento.
OBJ_BASE = "atos.sqlite"
OBJ_VETORES = "vetores.npz"
PREFIXO_ENTRADA = "entrada/"          # atos.zip enviado na carga inicial
PREFIXO_ATOS = "atos/"                # .md (carga inicial em pastas e atos novos do dia a dia)
PREFIXO_CLASSIFICACAO = "classificacao/"  # planilhas devolvidas pelo Copilot

# Pesquisa semântica (modelo local, baixado na construção da imagem; não tem custo por consulta).
MODELO_EMBEDDINGS = os.environ.get("MODELO_EMBEDDINGS", "intfloat/multilingual-e5-small")
MODELOS_DIR = os.environ.get("MODELOS_DIR", str(RAIZ / "modelos"))

# Classificação dos atos novos (somente no job; o site público não recebe a chave).
MODELO_CLASSIFICACAO = os.environ.get("MODELO_CLASSIFICACAO", "claude-haiku-5-5")
MAX_CLASSIFICACOES = int(os.environ.get("MAX_CLASSIFICACOES", "100"))  # por execução

# Atualização diária
SITUACOES = [s.strip() for s in os.environ.get("SITUACOES", "VIGENTE,REPUBLICADO,REVEICULADO").split(",") if s.strip()]
MAX_NOVOS = int(os.environ.get("MAX_NOVOS", "3000"))           # atos baixados por execução
MAX_REVISOES = int(os.environ.get("MAX_REVISOES", "300"))      # atos que mudaram de situação, por execução
PAUSA = float(os.environ.get("PAUSA", "0.3"))                  # segundos entre requisições ao TJPR

# Site
INTERVALO_RECARGA = int(os.environ.get("INTERVALO_RECARGA", "300"))  # segundos entre verificações de base nova
BUSCAS_POR_MINUTO = int(os.environ.get("BUSCAS_POR_MINUTO", "30"))   # por IP
ARQ_TEMAS = Path(os.environ.get("ARQ_TEMAS", RAIZ / "temas.json"))
