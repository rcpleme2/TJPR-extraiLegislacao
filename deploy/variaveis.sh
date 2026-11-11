# Configuração usada pelos scripts de implantação. Ajuste se quiser outros nomes ou região.
PROJETO="${PROJETO:-$(gcloud config get-value project 2>/dev/null)}"
REGIAO="${REGIAO:-southamerica-east1}"            # São Paulo
BUCKET="${BUCKET:-${PROJETO}-atos-tjpr}"
REPOSITORIO="painel"
SERVICO="atos-tjpr"                               # site público
JOB="atualiza-atos-tjpr"                          # atualização diária
AGENDAMENTO="atualiza-atos-tjpr-diario"
HORARIO="47 6 * * *"                              # todo dia às 06:47 (horário de Brasília)
SEGREDO="anthropic-api-key"

SA_SITE="painel-site@${PROJETO}.iam.gserviceaccount.com"
SA_JOB="painel-job@${PROJETO}.iam.gserviceaccount.com"
SA_AGENDADOR="painel-agendador@${PROJETO}.iam.gserviceaccount.com"

if [ -z "$PROJETO" ]; then
  echo "Defina o projeto: gcloud config set project ID_DO_PROJETO" >&2
  exit 1
fi
