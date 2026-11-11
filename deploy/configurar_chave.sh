#!/usr/bin/env bash
# Cadastra (ou troca) a chave da API do Claude no Secret Manager e liga ao job de atualização.
# A chave é digitada sem aparecer na tela e não fica no histórico do terminal.
# Só o job tem permissão para lê-la; o site público não tem acesso a ela.
#
# Uso, no Cloud Shell:
#   bash deploy/configurar_chave.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/variaveis.sh

read -rsp "Cole a chave da API do Claude e tecle Enter (não aparece na tela): " CHAVE
echo
if [[ "$CHAVE" != sk-ant-* ]]; then
  echo "Isso não parece uma chave da API do Claude (deveria começar com sk-ant-)." >&2
  exit 1
fi
if gcloud secrets describe "$SEGREDO" >/dev/null 2>&1; then
  printf '%s' "$CHAVE" | gcloud secrets versions add "$SEGREDO" --data-file=-
else
  printf '%s' "$CHAVE" | gcloud secrets create "$SEGREDO" --replication-policy=automatic --data-file=-
fi
unset CHAVE

gcloud secrets add-iam-policy-binding "$SEGREDO" \
  --member="serviceAccount:$SA_JOB" --role=roles/secretmanager.secretAccessor >/dev/null
gcloud run jobs update "$JOB" --region "$REGIAO" --set-secrets "ANTHROPIC_API_KEY=${SEGREDO}:latest"
echo "Chave cadastrada. A próxima atualização diária já classifica os atos novos."
