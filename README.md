# LatAm Financials Clipping — automação

Versão em código do prompt diário de clipping. Roda sozinho às 05:17 BRT (seg–sex) no GitHub Actions,
segue as mesmas regras do prompt e envia o e-mail **"LatAm Financials Clipping — DD/MM/AAAA"**.

## Não depender só do RSS.app

O `?_=` só fura o cache do CDN; ele não obriga o RSS.app a visitar o site de novo (isso acontece no
ritmo do plano), e cada feed só guarda os últimos ~20 itens. Por isso cada veículo do RSS.app é coberto
por **cinco canais independentes**, todos em toda execução:

1. **RSS.app** (como antes, com o loop de retry).
2. **Canais nativos do próprio veículo** (`native_sites`): o código lê o `robots.txt`, acha os
   *sitemaps de notícias* — que os sites mantêm atualizados em minutos para o Google, com título e
   horário exato de publicação — e o RSS próprio anunciado na página inicial.
3. **GDELT** (`gdelt_domains`): índice público de notícias, gratuito e sem chave, atualizado a cada 15 min.
4. **Google News `site:`** e as buscas por empresa.
5. **Coleta noturna** (`.github/workflows/collect.yml`): a cada 2 horas, das 13h41 às 3h41 BRT, guarda
   tudo que aparecer em qualquer canal. Às 05:17 o clipping também usa o que já saiu dos feeds.

O e-mail traz, por veículo, quantos itens cada canal trouxe, por exemplo
`Canais Estadão (itens na janela): RSS.app 0 · sitemap/RSS próprio 41 · GDELT 37 · Google site: 12 ·
coleta de hora em hora 29`. Assim fica claro quando um feed travado não custou cobertura, e quando custou.

Datas de canais nativos/GDELT são tratadas como as do feed: primeiro sinal, confirmado na página do
artigo. `lastmod` de sitemaps comuns é data de *modificação* e nunca é usada como data de publicação.

## O que mudou em relação ao prompt

| Problema do prompt | Como o código resolve |
|---|---|
| RSS.app "travado" por cache | Loop de retry obrigatório em **todos** os 10 feeds: um único `?_=<unix>` limpo e diferente a cada tentativa, headers `no-cache`, até 3 tentativas com espera; os itens de todas as tentativas são somados (feeds que oscilam). No fim da coleta, minutos depois, os que continuarem velhos ganham mais 2 tentativas. Só então o feed é reportado como travado, com a data do item mais novo. |
| Sem memória entre execuções | `state/feed_health.json` (guardado no cache do Actions) conta há quantas execuções seguidas cada feed está travado, e o e-mail mostra isso ("3 execuções seguidas, desde 10/11"). |
| Poucas notícias / sem busca por empresa | Busca dedicada **para cada uma das 36 empresas, toda execução**, no Google News da edição do país (pt-BR, es-MX, es-AR, es-CO, es-PE…), e também em inglês para os ADRs. Se o Google falhar, cai no Bing News. Além disso: buscas setoriais por país (BCB, Susep, CNBV, BCRA, SBS, Superfinanciera…) e buscas `site:` para cada veículo, como rede de segurança quando um feed ou listagem falha. |
| Fontes secundárias puladas | FinsidersBrasil, SonhoSeguro, NeoFeed, Exame e FinanceNews são testadas todas as vezes (feed RSS e, se falhar, a página de listagem). |
| Datas erradas | Cada artigo é aberto e a data vem da **própria página** (JSON-LD `datePublished`, `article:published_time`, `<time>`, "Publicado em…"). Nunca usa "modificado/atualizado". Só a data sem horário, no dia-limite da janela, só vale se o horário do feed confirmar; senão o item sai. |
| Páginas bloqueadas (Valor, O Globo) | Entram na lista com **— NÃO VERIFICADO**, com parágrafo curto escrito só a partir do título/snippet. Antes disso, o código procura a mesma história no FinsidersBrasil/SonhoSeguro, que costumam abrir e verificar. |
| Cobertura cortada em silêncio | Nada é pulado por pressa. Se o prazo de segurança estourar, o e-mail diz exatamente quais buscas/itens não foram feitos. |

As regras editoriais (escopo setorial, LatAm, cripto, exclusão de research de concorrentes, política,
formato bilíngue de 3 linhas, tom) estão em `prompts/editorial_rules.md` e são aplicadas pelo Claude
em três etapas: triagem dos títulos → redação (com deduplicação entre veículos) → **QC final** que
re-checa item por item antes do envio. Datas, links, nomes de veículo e títulos originais vêm do código,
nunca do modelo. O passo de QC também tem checagens fixas (data dentro da janela, formato completo,
URL duplicada) que não dependem do modelo.

## Fluxo

```
coleta (RSS.app + sitemaps/RSS nativos + GDELT + secundárias + outlets + 36 buscas por empresa
        + setoriais + site: + itens guardados pela coleta noturna)
  → dedup → filtro de janela pela data do feed
  → triagem (Claude) → resolve links do Google News
  → verificação da data em cada página → corroboração dos bloqueados (Finsiders/SonhoSeguro)
  → redação bilíngue + dedup de histórias (Claude) → QC fixo + QC final (Claude)
  → e-mail (texto puro + HTML) + arquivos em output/
```

Janela de notícias: desde ontem às 13:00 BRT até a hora da execução; **segunda-feira** desde sexta às
13:00 (fim de semana). Ajustável com `FRESHNESS_CUTOFF_HOUR`.

## Custo

Zero além do que você já paga. O Claude roda pela **sua assinatura (plano Pro)**, via Claude Code, e não pela
API paga. Usa o **Haiku** em todas as etapas, para gastar pouco da cota. GitHub (dentro do grátis), Gmail,
Google News, Bing, GDELT e sitemaps são gratuitos, e o RSS.app é o plano que você já tem.
O uso entra nos limites do plano Pro: se num dia o limite for atingido, o e-mail sai mesmo assim, com uma nota
avisando que a triagem ou a redação automática falhou.

Para economizar cota, antes de qualquer chamada ao Claude:
- títulos sem nenhum termo do setor nem nome de empresa, vindos de feeds que cobrem o site inteiro
  (esporte, política…), são descartados por regra fixa;
- cada matéria entra com no máximo 3.000 caracteres;
- o complemento das 6h só manda ao Claude o que a edição das 05:17 não tinha como ter: itens publicados
  depois dela e itens dos sites que bloqueiam a nuvem.

## Como ativar (uma vez)

1. **Token da sua assinatura do Claude**: instale o Claude Code no seu computador
   (Windows: no PowerShell, `irm https://claude.ai/install.ps1 | iex`), rode `claude setup-token`, entre com a
   conta do plano Pro no navegador e copie o token que aparece.
2. **Senha de app do Gmail**: com verificação em 2 etapas ativa, gere em https://myaccount.google.com/apppasswords.
3. No GitHub: *Settings → Secrets and variables → Actions → New repository secret*, crie:
   - `CLAUDE_CODE_OAUTH_TOKEN` — o token do passo 1
   - `GMAIL_ADDRESS` — conta que envia
   - `GMAIL_APP_PASSWORD` — a senha de app (16 letras)
   - `CLIPPING_TO` — quem recebe (se vazio, envia para `GMAIL_ADDRESS`)
4. A branch `claude/cool-carson-f3jzvz` já é a principal do repositório; não precisa de merge.
5. Teste em *Actions → LatAm Financials Clipping → Run workflow* (marque "Só gerar" para não enviar e-mail).
   O resultado fica nos *artifacts* da execução: `clipping-AAAA-MM-DD.txt/.html` e um `.json` com o
   relatório completo (cada fonte, cada exclusão e o motivo).

## Complemento das 6h no seu computador (Windows)

A edição completa continua saindo às 05:17 pela nuvem. Quando o computador liga (a partir das 6h), uma tarefa
agendada roda o **complemento**. Da conexão de casa, os sites que bloqueiam servidores (Valor, O Globo,
Estadão…) costumam abrir. O complemento:

1. lê no seu Gmail a edição das 05:17 (pelo IMAP, com a mesma senha de app; não precisa de outra credencial);
2. reabre cada item **NÃO VERIFICADO** dela:
   - data confirmada na janela → vai para **Agora verificados**, com o parágrafo completo;
   - data confirmada fora da janela → vai para **Correções** ("desconsiderar");
3. refaz a coleta inteira (todos os canais e as 36 buscas) e manda só as histórias que **não** estavam às 05:17;
4. envia **"LatAm Financials Clipping — DD/MM/AAAA — complemento"**, só se houver algo a acrescentar.

Ele roda uma vez por dia útil, até as 14h. Se a edição das 05:17 não estiver no Gmail (falha na nuvem),
o computador espera até 20 min e, se ela não chegar, envia o clipping completo.

### Instalação (uma vez)

1. Instale o Git: abra o PowerShell e rode `winget install Git.Git` (ou baixe em git-scm.com).
2. Baixe o projeto numa pasta sua, por exemplo:
   `git clone https://github.com/Vitorsl20/Daily---Clipping.git "$HOME\Daily-Clipping"`
   (o Windows abre o navegador para você entrar no GitHub, se o repositório for privado).
3. Abra a pasta `scripts\windows` e dê **dois cliques em `INSTALAR.bat`**. Ele:
   instala o Python se faltar (aí é só rodar de novo), instala as dependências, pede as credenciais
   (as mesmas dos Secrets do GitHub, incluindo o token do `claude setup-token`; ficam só neste PC, no `.env`), cria a tarefa agendada
   **"LatAm Clipping - complemento 6h"** e oferece um teste sem enviar e-mail.

A tarefa dispara 2 min após o login e às 06:05. Se às 06:05 o PC estiver desligado ou dormindo, ela roda
assim que ele voltar. A cada execução o código é atualizado com `git pull`. Logs: pasta `logs\`.
Para remover: `scripts\windows\desinstalar.ps1`.

Variáveis opcionais (no `.env`): `COMPLEMENT_LATEST_HOUR` (14), `COMPLEMENT_WAIT_MINUTES` (20),
`COMPLEMENT_SEND_EMPTY=1` (manda e-mail mesmo sem novidades, como confirmação).

## Rodar localmente

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export CLAUDE_CODE_OAUTH_TOKEN=...      # ou só esteja logado no `claude`; e GMAIL_ADDRESS / GMAIL_APP_PASSWORD / CLIPPING_TO para enviar
python -m clipping --dry-run            # gera output/clipping-AAAA-MM-DD.txt sem enviar
python -m clipping --collect-only       # só coleta e guarda (o que a coleta noturna faz)
python -m clipping --complement --dry-run --force   # testa o complemento das 6h
python -m clipping --now 2026-11-16T05:30:00-03:00 --dry-run   # simula uma segunda-feira
python -m pytest -q                     # testes
```

## Onde mexer

- `config/sources.yaml` — feeds do RSS.app, fontes secundárias, outlets (RSS e páginas de listagem).
- `config/companies.yaml` — as 36 empresas, termos de busca de cada uma e buscas setoriais por país.
- `prompts/editorial_rules.md` — regras editoriais (escopo, cripto, research de concorrentes, formato).
- Variáveis opcionais: `CLAUDE_MODEL` (padrão `haiku` em todas as etapas) ou por etapa
  `CLAUDE_MODEL_TRIAGE` / `CLAUDE_MODEL_WRITE` / `CLAUDE_MODEL_QC` (`haiku`, `sonnet`, `opus`),
  `CLAUDE_TRIAGE_EFFORT` (`low`), `CLAUDE_WRITE_EFFORT` (`medium`), `ARTICLE_CHARS` (3000), `QC_CHARS` (1800),
  `CLAUDE_PARALLEL` (2), `EDITOR_BACKEND` (`subscription` ou `api`), `FRESHNESS_CUTOFF_HOUR` (13), `COLLECT_DEADLINE_MINUTES` (12),
  `CLIPPING_DEADLINE_MINUTES` (25), `SEARCH_CONCURRENCY` (6), `VERIFY_CONCURRENCY` (12).

Se um dia você preferir a API paga (sem depender da cota do plano), cadastre `ANTHROPIC_API_KEY` no lugar de
`CLAUDE_CODE_OAUTH_TOKEN` e mude a linha correspondente no workflow; o código detecta sozinho qual usar.
