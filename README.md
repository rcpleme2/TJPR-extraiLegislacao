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
pip install -r requirements.txt
```

```bash
python3 extrai_legislacao.py
```

Opções:

| Opção | Efeito |
|---|---|
| `--situacao REPUBLICADO REVEICULADO` | escolhe as situações (também: `REINDEXADO SUSPENSO REVOGADO SEMEFEITO ALTERADO CUMPRIDO`) |
| `--saida PASTA` | pasta de saída (padrão `./atos`) |
| `--limite N` | no máximo N atos por situação (para testar) |
| `--pausa S` | segundos entre requisições (padrão 0,3) |
| `--barra C` | caractere que substitui `/` no nome do arquivo |

O script pode ser interrompido (Ctrl+C) e executado de novo: os atos já salvos ficam registrados em `.baixados.json` em cada pasta e são pulados. Atos em que o servidor falhar mesmo após as novas tentativas são listados no final; basta rodar de novo.

## Como funciona

A página de pesquisa monta a lista via JavaScript, chamando dois serviços JSON do próprio portal; o script chama os mesmos serviços diretamente, o que é mais rápido e estável que automatizar um navegador:

- `…p_p_resource_id=/pesquisar` — a pesquisa (com `situacao=VIGENTE` etc., todos os tipos de documento, 100 resultados por página, que é o máximo aceito pelo servidor);
- `…p_p_resource_id=/obterMateria` — o detalhe de cada ato (`detalheID=<id>`), o mesmo conteúdo exibido em `/legislacao-atos-normativos/-/atos/documento/<id>`.

Tempo aproximado (novembro de 2026): Vigente ≈ 7.800 atos (≈ 4 h), Republicado 7, Reveiculado 170 (≈ 5 min).
