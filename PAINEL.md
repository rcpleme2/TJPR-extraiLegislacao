# Painel público dos atos do TJPR no Google Cloud Run

Site de consulta pública aos atos, com pesquisa por palavras, por tema e por significado, atualizado sozinho uma vez por dia.

## Como funciona

```
                 todo dia 06:47                         
Cloud Scheduler ───────────────► Job "atualiza-atos-tjpr" (Cloud Run Job)
                                   1. baixa a base do bucket (na 1ª vez, monta a partir do atos.zip)
                                   2. importa as planilhas do Copilot (classificacao/)
                                   3. consulta o site do TJPR: atos novos e mudanças de situação
                                   4. classifica os atos novos com o Claude Haiku   ◄── chave no Secret Manager
                                   5. gera os vetores da pesquisa por significado
                                   6. devolve a base ao bucket
                                         │
                                         ▼
                           Bucket (privado): atos.sqlite, vetores.npz, atos/, entrada/, classificacao/
                                         │  (somente leitura)
                                         ▼
Público ────────────────────► Site "atos-tjpr" (Cloud Run Service)
                                   pesquisa, filtros, temas, página de cada ato
```

- **Uma imagem só** para o site e para o job (`Dockerfile`).
- **O site não tem a chave da API** nem permissão de escrita: ele só lê o bucket. A classificação acontece apenas dentro do job, que não tem endereço público.
- A pesquisa por significado usa um modelo multilíngue que roda dentro da própria imagem (`intfloat/multilingual-e5-small`), sem custo por consulta e sem enviar as pesquisas a terceiros.

## Passo a passo

Tudo é feito pelo navegador. Os comandos rodam no **Cloud Shell**, o terminal que já vem logado no Google Cloud: ícone `>_` no alto do [console](https://console.cloud.google.com).

### 1. Projeto

Crie ou escolha um projeto no Google Cloud **com faturamento ativado** e selecione-o no Cloud Shell:

```bash
gcloud config set project ID_DO_SEU_PROJETO
```

### 2. Código no Cloud Shell

```bash
git clone -b claude/nice-allen-pn4gwq https://github.com/rcpleme2/tjpr-extrailegislacao.git
```

```bash
cd tjpr-extrailegislacao
```

Se o repositório for privado e o `git clone` pedir senha, baixe o ZIP pelo GitHub (botão **Code → Download ZIP**), envie pelo menu **⋮ → Fazer upload** do Cloud Shell e descompacte com `unzip`.

### 3. Implantar

```bash
bash deploy/implantar.sh
```

Cria, na região de São Paulo: o bucket privado, as três contas de serviço com permissões mínimas, o repositório de imagens, a imagem (5 a 10 minutos), o site, o job e o agendamento diário. No final mostra o endereço do site. Enquanto a base não existir, o site mostra "Base em preparação".

### 4. Enviar os atos já extraídos

No seu computador, compacte a pasta `atos` (com as subpastas `Vigente`, `Republicado` e `Reveiculado`) em **atos.zip**. No Windows: botão direito na pasta → **Enviar para → Pasta compactada**.

O `atos.zip` está no seu computador, não no Cloud Shell. Envie-o para a pasta `entrada/` do bucket de um destes jeitos:

- **Pelo console (mais simples):** **Cloud Storage → Buckets → ID_DO_PROJETO-atos-tjpr → Criar pasta** `entrada` → abra a pasta → **Fazer upload → Fazer upload de arquivos** → escolha o `atos.zip`.
- **Pelo Cloud Shell:** primeiro envie o arquivo para o Cloud Shell (menu **⋮ → Fazer upload**; ele vai para a sua pasta pessoal, `~`) e depois copie para o bucket:

```bash
gcloud storage cp ~/atos.zip gs://$(gcloud config get-value project)-atos-tjpr/entrada/
```

Para conferir que o arquivo chegou ao bucket:

```bash
gcloud storage ls -l gs://$(gcloud config get-value project)-atos-tjpr/entrada/
```

Rode a primeira atualização sem esperar o agendamento:

```bash
gcloud run jobs execute atualiza-atos-tjpr --region southamerica-east1
```

A primeira execução monta a base, confere o site do TJPR e gera os vetores de todos os atos: conte com 30 a 60 minutos. Acompanhe em **Cloud Run → Jobs → atualiza-atos-tjpr → Logs**. As seguintes levam poucos minutos.

Atos que não estiverem no zip são baixados pelo próprio job (até 3.000 por dia), então um zip desatualizado não é problema.

### 5. Chave da API do Claude (atos novos)

1. No [Console da Anthropic](https://console.anthropic.com), crie um **workspace** só para o painel (ex.: `painel-atos-tjpr`) e defina nele um **limite de gastos** mensal baixo (ex.: US$ 5). Assim, mesmo que a chave vaze, o prejuízo fica limitado.
2. Crie uma chave nesse workspace.
3. No Cloud Shell:

```bash
bash deploy/configurar_chave.sh
```

O script pede a chave **sem mostrá-la na tela** e sem gravá-la no histórico, guarda-a no **Secret Manager** e dá permissão de leitura apenas à conta de serviço do job.

Não cole a chave em chats, e-mails, arquivos do repositório ou variáveis de ambiente comuns. Para trocar a chave, rode o mesmo script de novo e apague a antiga no Console da Anthropic.

Cada execução classifica no máximo 100 atos (`MAX_CLASSIFICACOES`). Com o volume normal (algumas dezenas de atos por dia), o custo com o Haiku fica em centavos de dólar por mês.

### 6. Classificar os atos antigos com o Copilot

No seu computador, na pasta dos scripts:

```bash
py exporta_para_copilot.py
```

Gera a pasta `copilot` com `lote_001.csv`, `lote_002.csv`, …, `temas.csv` e **INSTRUCOES_COPILOT.md**, que tem o texto para colar no Copilot Cowork. Para cada lote, salve a resposta como `lote_NNN_classificado.csv` (ou `.xlsx`) e envie para a pasta **`classificacao/`** do bucket. A próxima atualização importa tudo e registra no log quantos atos foram importados, ids desconhecidos e temas inválidos. Para importar na hora:

```bash
gcloud run jobs execute atualiza-atos-tjpr --region southamerica-east1
```

Depois que a base estiver no ar, para exportar só os atos que ainda não têm tema, baixe `atos.sqlite` do bucket e rode `py exporta_para_copilot.py --base atos.sqlite`.

**Os temas** ficam em `temas.json`. Ajuste a lista **antes** de começar a classificação com o Copilot: Copilot e Claude usam a mesma lista. Se mudar depois, rode `bash deploy/implantar.sh` para publicar a nova lista.

### Atualizar o código

Depois de qualquer mudança no repositório:

```bash
bash deploy/implantar.sh
```

## Proteção contra abuso e custos

- **Chave da API:** só existe no Secret Manager e no job. O site público não a recebe; nenhuma ação de visitante gera chamada à API do Claude. Limite de gastos no workspace da Anthropic e teto de 100 classificações por execução.
- **Site:** no máximo 3 instâncias (`--max-instances 3`), o que limita o custo mesmo sob ataque; 30 pesquisas por minuto por endereço IP; sem login, sem formulários que gravem dados, com política de segurança de conteúdo restrita.
- **Bucket:** privado, com acesso público bloqueado. O site lê; só o job escreve.
- **Job:** não tem URL pública; só o agendador (conta própria) pode iniciá-lo.

**Custos aproximados:** o site desliga quando ninguém acessa (`--min-instances 0`), e o job roda alguns minutos por dia. Para uso moderado, a conta tende a ficar em poucos dólares por mês (armazenamento da imagem e do bucket, mais o processamento), boa parte dentro do nível gratuito do Cloud Run. A desvantagem é que o primeiro acesso depois de um tempo parado demora de 10 a 20 segundos. Para evitar isso, use `--min-instances 1` em `deploy/implantar.sh`, com custo fixo maior.

## Testar no computador

```bash
py -m pip install -r requirements-painel.txt
```

Coloque o `atos.zip` em `dados\entrada\` e rode a atualização sem consultar o site do TJPR:

```bash
py -m app.atualiza --sem-site
```

Depois abra o site em http://localhost:8080:

```bash
py -m uvicorn app.web:app --port 8080
```

Sem a variável `BUCKET`, tudo usa a pasta `dados` no lugar do Cloud Storage.

## Estrutura

| Arquivo | Função |
|---|---|
| `app/web.py`, `app/templates/`, `app/static/` | site público |
| `app/busca.py` | pesquisa por palavras (SQLite FTS5), por significado (vetores) e combinada; filtros e contagens |
| `app/atualiza.py` | job diário |
| `app/classificacao.py` | importação das planilhas do Copilot e classificação com o Claude |
| `app/semantica.py` | modelo e índice da pesquisa por significado |
| `app/banco.py`, `app/armazenamento.py`, `app/config.py` | base SQLite, bucket/pasta local, configuração |
| `temas.json` | lista de temas |
| `exporta_para_copilot.py` | lotes e instruções para o Copilot |
| `deploy/` | scripts do Google Cloud |
