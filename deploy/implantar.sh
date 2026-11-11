#!/usr/bin/env bash
# Cria (ou atualiza) toda a infraestrutura no Google Cloud. Pode ser executado de novo a cada
# mudança no código: só recria a imagem e atualiza o site e o job.
#
# Uso, no Cloud Shell, dentro da pasta do repositório:
#   bash deploy/implantar.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/variaveis.sh

echo "== Projeto $PROJETO, região $REGIAO"

echo "== Ativando APIs (só demora na primeira vez)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com iam.googleapis.com

echo "== Bucket gs://$BUCKET (privado)"
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --location="$REGIAO" \
    --uniform-bucket-level-access --public-access-prevention
fi

echo "== Contas de serviço (cada parte só com a permissão que precisa)"
criar_sa() {
  gcloud iam service-accounts describe "$1@${PROJETO}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$1" --display-name="$2"
}
criar_sa painel-site "Painel de atos - site (somente leitura)"
criar_sa painel-job "Painel de atos - atualização diária"
criar_sa painel-agendador "Painel de atos - agendador"
sleep 5
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_SITE" --role=roles/storage.objectViewer >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_JOB" --role=roles/storage.objectAdmin >/dev/null

echo "== Repositório de imagens"
gcloud artifacts repositories describe "$REPOSITORIO" --location="$REGIAO" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPOSITORIO" --repository-format=docker --location="$REGIAO"

IMAGEM="${REGIAO}-docker.pkg.dev/${PROJETO}/${REPOSITORIO}/atos-tjpr:$(date +%Y%m%d-%H%M%S)"
echo "== Construindo a imagem $IMAGEM (5 a 10 minutos)"
gcloud builds submit --region="$REGIAO" --tag "$IMAGEM" .

echo "== Site público"
gcloud run deploy "$SERVICO" --image "$IMAGEM" --region "$REGIAO" \
  --service-account "$SA_SITE" --allow-unauthenticated \
  --memory 4Gi --cpu 1 --concurrency 20 --min-instances 0 --max-instances 3 \
  --timeout 60 --cpu-boost \
  --set-env-vars "BUCKET=${BUCKET},INTERVALO_RECARGA=600,BUSCAS_POR_MINUTO=30"

echo "== Job de atualização"
SEGREDO_FLAG=()
if gcloud secrets describe "$SEGREDO" >/dev/null 2>&1; then
  gcloud secrets add-iam-policy-binding "$SEGREDO" \
    --member="serviceAccount:$SA_JOB" --role=roles/secretmanager.secretAccessor >/dev/null
  SEGREDO_FLAG=(--set-secrets "ANTHROPIC_API_KEY=${SEGREDO}:latest")
else
  echo "   (chave da API do Claude ainda não cadastrada: os atos novos ficam sem tema até rodar deploy/configurar_chave.sh)"
fi
gcloud run jobs deploy "$JOB" --image "$IMAGEM" --region "$REGIAO" \
  --service-account "$SA_JOB" --command python --args="-m,app.atualiza" \
  --memory 8Gi --cpu 2 --task-timeout 12h --max-retries 1 --tasks 1 --parallelism 1 \
  --set-env-vars "BUCKET=${BUCKET},MAX_CLASSIFICACOES=100,SITUACOES=TODAS,MAX_NOVOS=60000" "${SEGREDO_FLAG[@]}"

echo "== Agendamento diário"
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGIAO" \
  --member="serviceAccount:$SA_AGENDADOR" --role=roles/run.invoker >/dev/null
URI="https://${REGIAO}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJETO}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "$AGENDAMENTO" --location "$REGIAO" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$AGENDAMENTO" --location "$REGIAO" --schedule="$HORARIO" \
    --time-zone="America/Sao_Paulo" --uri="$URI" --http-method=POST --oauth-service-account-email="$SA_AGENDADOR"
else
  gcloud scheduler jobs create http "$AGENDAMENTO" --location "$REGIAO" --schedule="$HORARIO" \
    --time-zone="America/Sao_Paulo" --uri="$URI" --http-method=POST --oauth-service-account-email="$SA_AGENDADOR"
fi

URL=$(gcloud run services describe "$SERVICO" --region "$REGIAO" --format='value(status.url)')
cat <<FIM

Pronto.
  Site:        $URL
  Bucket:      gs://$BUCKET
  Atualização: todo dia ($HORARIO, horário de Brasília)

Se ainda não enviou os atos, envie o atos.zip para gs://$BUCKET/entrada/ e rode a primeira atualização:
  gcloud run jobs execute $JOB --region $REGIAO
FIM
