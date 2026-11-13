# LatAm Financials Clipping — automação

Versão em código do prompt diário de clipping. Roda sozinho às 05:17 BRT (seg–sex) no GitHub Actions,
segue as mesmas regras do prompt e envia o e-mail **"LatAm Financials Clipping — DD/MM/AAAA"**.

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
coleta (RSS.app + secundárias + outlets + 36 buscas por empresa + setoriais + site:)
  → dedup → filtro de janela pela data do feed
  → triagem (Claude) → resolve links do Google News
  → verificação da data em cada página → corroboração dos bloqueados (Finsiders/SonhoSeguro)
  → redação bilíngue + dedup de histórias (Claude) → QC fixo + QC final (Claude)
  → e-mail (texto puro + HTML) + arquivos em output/
```

Janela de notícias: desde ontem às 13:00 BRT até a hora da execução; **segunda-feira** desde sexta às
13:00 (fim de semana). Ajustável com `FRESHNESS_CUTOFF_HOUR`.

## Como ativar (uma vez)

1. **Chave da API do Claude**: crie em https://console.anthropic.com → API Keys.
2. **Senha de app do Gmail**: com verificação em 2 etapas ativa, gere em https://myaccount.google.com/apppasswords.
3. No GitHub: *Settings → Secrets and variables → Actions → New repository secret*, crie:
   - `ANTHROPIC_API_KEY`
   - `GMAIL_ADDRESS` — conta que envia
   - `GMAIL_APP_PASSWORD` — a senha de app (16 letras)
   - `CLIPPING_TO` — quem recebe (se vazio, envia para `GMAIL_ADDRESS`)
4. Faça o merge na branch padrão (o agendamento do GitHub só roda a partir dela).
5. Teste em *Actions → LatAm Financials Clipping → Run workflow* (marque "Só gerar" para não enviar e-mail).
   O resultado fica nos *artifacts* da execução: `clipping-AAAA-MM-DD.txt/.html` e um `.json` com o
   relatório completo (cada fonte, cada exclusão e o motivo).

## Rodar localmente

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # e GMAIL_ADDRESS / GMAIL_APP_PASSWORD / CLIPPING_TO para enviar
python -m clipping --dry-run            # gera output/clipping-AAAA-MM-DD.txt sem enviar
python -m clipping --now 2026-11-16T05:30:00-03:00 --dry-run   # simula uma segunda-feira
python -m pytest -q                     # testes
```

## Onde mexer

- `config/sources.yaml` — feeds do RSS.app, fontes secundárias, outlets (RSS e páginas de listagem).
- `config/companies.yaml` — as 36 empresas, termos de busca de cada uma e buscas setoriais por país.
- `prompts/editorial_rules.md` — regras editoriais (escopo, cripto, research de concorrentes, formato).
- Variáveis opcionais: `CLAUDE_MODEL` (padrão `claude-opus-5-5`), `CLAUDE_TRIAGE_EFFORT` (`medium`),
  `CLAUDE_WRITE_EFFORT` (`high`), `FRESHNESS_CUTOFF_HOUR` (13), `COLLECT_DEADLINE_MINUTES` (12),
  `CLIPPING_DEADLINE_MINUTES` (25), `SEARCH_CONCURRENCY` (6), `VERIFY_CONCURRENCY` (12).

As chamadas ao Claude usam o fallback automático do servidor (`fallbacks: "default"`): se o modelo
principal recusar um pedido por filtro de segurança, outro modelo refaz o pedido na mesma chamada.
