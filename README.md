# TJPR – extração de atos normativos

Script que percorre a pesquisa de [Atos Normativos e Administrativos do TJPR](https://www.tjpr.jus.br/legislacao-atos-normativos),
filtra pela **Situação**, passa por todas as páginas de resultado, abre cada ato e salva cada um em um arquivo `.md`.

Por padrão processa, nesta ordem, **Vigente**, **Republicado** e **Reveiculado**, cada um em sua pasta:

```
atos/
├── Vigente/
├── Republicado/
└── Reveiculado/
```

Nome de cada arquivo: `[número do ato] - [órgão] ∕ [ano do ato] - [Assunto].md`, por exemplo
`17 - CORREGEDORIA-GERAL DA JUSTIÇA ∕ 2013 - 1.Autorização 2.Corregedoria-Geral da Justiça ….md`.

- A barra comum `/` não pode existir em nome de arquivo (criaria uma subpasta); ela é trocada por `∕` (U+2215), que tem a mesma aparência. Use `--barra " - "` (ou outro caractere) para mudar.
- O "Assunto" de muitos atos é uma lista longa de palavras-chave; o nome é cortado em 200 caracteres (terminando em `…`) para respeitar o limite dos sistemas de arquivos. O assunto completo fica dentro do arquivo.
- Se o ato não tiver assunto, usa-se o campo "Documento".

Cada `.md` contém a tabela de dados do ato (tipo, número, origem, assunto, ementa, situação, data e número do diário, link da fonte), os anexos e as referências (como links) e o texto integral convertido para Markdown.

## Uso

```bash
py -m pip install -r requirements.txt
```

(No Linux/macOS use `python3` no lugar de `py`.)

```bash
py extrai_legislacao.py
```

Opções:

| Opção | Efeito |
|---|---|
| `--situacao REPUBLICADO REVEICULADO` | escolhe as situações (também: `REINDEXADO SUSPENSO REVOGADO SEMEFEITO ALTERADO CUMPRIDO`) |
| `--saida PASTA` | pasta de saída (padrão `./atos`) |
| `--limite N` | no máximo N atos por situação (para testar) |
| `--pausa S` | segundos entre requisições (padrão 0,3) |
| `--barra C` | caractere que substitui `/` no nome do arquivo |

### Atos já baixados são pulados

Antes de começar cada situação, o script lê os `.md` que já estão na pasta e identifica cada ato pelo link da linha **Fonte** (`…/atos/documento/<id>`). Esses atos são pulados sem nenhuma consulta ao site. Por isso dá para interromper (Ctrl+C) e rodar de novo quantas vezes quiser, e funciona mesmo que os arquivos tenham sido copiados de outro computador. Para baixar um ato de novo, basta apagar o arquivo dele.

Atos em que o servidor falhar mesmo após as novas tentativas são listados no final; basta rodar de novo.

### Barra de progresso

```
[██████████████████████··················]  56.5%  96/170 | novos 2 | pulados 94 | erros 0 | resta ~1min10s
```

Mostra o total de atos da situação, quantos foram baixados agora (novos), quantos já estavam na pasta (pulados), os erros e uma estimativa do tempo que falta (calculada só sobre os atos que ainda faltam baixar).

## Como funciona

A página de pesquisa monta a lista via JavaScript, chamando dois serviços JSON do próprio portal; o script chama os mesmos serviços diretamente, o que é mais rápido e estável que automatizar um navegador:

- `…p_p_resource_id=/pesquisar` — a pesquisa (com `situacao=VIGENTE` etc., todos os tipos de documento, 100 resultados por página, que é o máximo aceito pelo servidor);
- `…p_p_resource_id=/obterMateria` — o detalhe de cada ato (`detalheID=<id>`), o mesmo conteúdo exibido em `/legislacao-atos-normativos/-/atos/documento/<id>`.

Tempo aproximado (novembro de 2026): Vigente ≈ 7.800 atos (≈ 4 h), Republicado 7, Reveiculado 170 (≈ 5 min).

---

## Consolidação e separação (`consolida_atos.py`)

Depois de baixar, rode:

```bash
py consolida_atos.py
```

Ele lê todos os `.md` de `atos/<situação>/` (sem internet, sem alterar os originais) e gera em `base/`:

| Arquivo | Conteúdo |
|---|---|
| `atos.sqlite` | base única com todos os atos: metadados, texto integral, palavras-chave em tabela própria e pesquisa de texto completo que ignora acentos. É a fonte de dados para o painel. |
| `atos.csv` | os metadados em planilha (separador `;`, abre direto no Excel); não inclui o texto integral. |
| `atos.jsonl` | um ato por linha, com o texto integral (útil para gerar embeddings / pesquisa semântica). |
| `separados/` | cópia dos `.md` em pastas, por padrão `Origem/Ano` (ex.: `separados/CORREGEDORIA-GERAL DA JUSTIÇA/2016/`). |

Campos extraídos de cada ato: id, situação, tipo, tipo de ato, número, número sequencial, ano, origem, documento, assunto, **palavras-chave** (o "1.Revogação 2.Presidência…" do assunto separado em termos), ementa, data do diário (AAAA-MM-DD), número do diário, link da fonte, anexos, referências e o texto.

Ao final mostra um resumo (atos por situação, anos, tipos, origens e palavras-chave mais frequentes).

Opções:

| Opção | Efeito |
|---|---|
| `--por ano,origem` | outra organização das pastas; combine `origem`, `ano`, `tipo`, `situacao` |
| `--limpar` | apaga e refaz `separados/` (use depois de mudar `--por`) |
| `--sem-separar` | só gera a base, sem copiar arquivos |
| `--entrada PASTA` / `--saida PASTA` | pastas de entrada (padrão `atos`) e saída (padrão `base`) |
| `--buscar "termos"` | pesquisa na base já gerada, ex.: `py consolida_atos.py --buscar "guarda compartilhada"` |

Pode ser rodado a cada atualização: a base é recriada (leva segundos) e só os arquivos novos ou alterados são copiados para `separados/`. Se um mesmo ato estiver em duas pastas de situação, fica a versão baixada por último. A pasta `separados/` é derivada: não edite arquivos nela.

---

## Painel público (Google Cloud Run)

O site de consulta pública, com atualização diária automática, pesquisa por palavras, por tema e por significado, e classificação temática (Copilot para os atos antigos, Claude Haiku para os novos), está descrito em **[PAINEL.md](PAINEL.md)**.
