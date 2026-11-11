# Imagem única: o serviço (site) e o job (atualização diária) usam a mesma imagem.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MODELOS_DIR=/modelos TRABALHO_DIR=/tmp/atos-tjpr HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /srv
COPY requirements.txt requirements-painel.txt ./
RUN pip install --no-cache-dir -r requirements-painel.txt

# Baixa o modelo da pesquisa semântica durante a construção (não depende da internet ao rodar).
COPY app/__init__.py app/config.py app/semantica.py app/
RUN python -c "from app import semantica; semantica.modelo(); print(semantica.vetor_da_consulta('teste').shape)" \
    && chmod -R a+rX /modelos

COPY extrai_legislacao.py consolida_atos.py temas.json ./
COPY app/ app/

RUN useradd --create-home --uid 10001 painel
USER painel

# Site. O job usa a mesma imagem com: python -m app.atualiza
CMD exec uvicorn app.web:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log
