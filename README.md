# Caixa QCM

Conciliação bancária local: importa extratos **OFX** do PagBank e da Conta Simples, você classifica cada movimentação e o app mostra o **saldo real** e as **diferenças de conciliação**. Python 3.12 + Streamlit + SQLite. Tudo roda no seu computador — sem serviços pagos, sem API, sem nuvem, sem conexão bancária.

## Começar (Windows)

1. Dê dois cliques em **`iniciar.bat`**. Na primeira vez ele cria o ambiente e instala as dependências (precisa de internet e do Python 3.12); depois abre o navegador em <http://localhost:8501>.
2. Para só olhar com dados fictícios, use **`iniciar_demo.bat`** (usa a pasta `data_demo/`, separada dos seus dados).
3. Em **Contas**, informe o saldo inicial de PagBank e Conta Simples e a data desse saldo.
4. Em **Importar extrato**, escolha a conta, envie o `.ofx`, confira a prévia e confirme.
5. Em **A identificar**, classifique. No **Painel** veja saldos e divergências.

Testes: `.venv\Scripts\python -m pytest -q`

## Como funciona

**Saldo calculado** = saldo inicial + entradas − saídas, somando os lançamentos de `data do saldo inicial` até a data escolhida. O saldo inicial vale no *início* do dia informado (lançamentos desse dia entram; os anteriores são ignorados e avisados). O painel compara o calculado com o saldo que o próprio extrato informa (`LEDGERBAL`), data a data.

**Tipos de classificação:** receita, despesa, transferência entre contas, aporte, retirada/pró-labore, imposto, tarifa, estorno, ajuste. Receita/aporte exigem entrada; despesa/retirada/imposto/tarifa exigem saída; estorno, ajuste e transferência aceitam os dois sinais.

**Transferências:** classifique a saída (ou a entrada) como *transferência* e vincule à ponta correspondente na outra conta (o app sugere candidatos ±3 dias). Os saldos de ambas mudam, mas a transferência **não entra em receita, despesa nem no resultado**. Se os valores das pontas diferem (tarifa), o vínculo é aceito e a diferença aparece em *Investigar*. Desvincular devolve as duas pontas para *A identificar*.

**Empresa no período (painel):** entradas/saídas da empresa excluem transferências; *resultado* = receitas + despesas + impostos + tarifas + estornos; aportes/retiradas aparecem à parte como movimentação de sócios.

### Importação sem duplicar
- Com **FITID** confiável (presente e único no arquivo): reimportar o mesmo arquivo ou períodos sobrepostos ignora o que já existe. Mesmo FITID com data/valor diferentes é importado e **sinalizado**.
- **Sem FITID** (ou FITID repetido no arquivo): a identidade é data + valor + descrição + nº da ocorrência no arquivo, então duas cobranças idênticas no mesmo dia continuam existindo. Quando já há algo idêntico, a linha nova entra **sinalizada como possível duplicata** — você decide em *Investigar → Duplicatas*: *É duplicata* (sai dos saldos, o original continua guardado) ou *Não é duplicata*. Decisões são reversíveis.
- O app avisa se o número da conta no arquivo difere do já importado para aquela conta.
- O arquivo original é copiado para `data/origem/`.

### Investigar divergências
Possíveis duplicatas · transferências sem par e pares com diferença de valor · saldos de extrato × calculado (com pistas quando a diferença bate com um lançamento) · períodos sem extrato (lacunas entre o saldo inicial e o último extrato).

### Dados originais intocáveis
Os lançamentos importados ficam na tabela `transactions`, protegida por *triggers* do SQLite contra `UPDATE`/`DELETE`. Classificação, vínculos e decisões de duplicata ficam em tabelas à parte, com histórico em `classification_log`.

## Rotina diária e integrações

Meta de todo dia: **PagBank** com o OFX importado até *ontem* (manual) e **Conta Simples** sincronizada pela API (automático). A página **Rotina diária** e um aviso amarelo no topo de todas as telas mostram o que falta.

**PagBank (OFX manual):** exporte o extrato em OFX no app do PagBank (desde o último dia importado) e envie em *Importar extrato*. Reimportar períodos sobrepostos é seguro. O PagBank não oferece API de extrato da conta digital (a API EDI cobre só vendas/maquininha).

**Conta Simples (API, somente leitura):**
1. No Internet Banking: Integrações → API → Credenciais. Marque **só escopos de leitura** (extrato bancário, extrato de cartão, saldo). Escopos de escrita exigem chave pública e assinatura de cada requisição (RFC 9421), que o app não faz.
   **Sandbox x produção:** a tela de credenciais do Internet Banking pode gerar credenciais de *Sandbox*, que só devolvem dados fictícios fixos e são recusadas (HTTP 403) em produção. O app só *testa a conexão* com Sandbox e nunca grava os dados dele. Para sincronizar é preciso credencial de **produção**, que a Conta Simples libera após validar a integração, checklist de segurança e termos (developers.contasimples.com → Ambientes).
2. Em *Rotina diária*, informe API key/secret e clique em *Testar conexão* e *Sincronizar agora*. As credenciais ficam em `data/conexoes.json` (texto simples, só neste computador, fora do git).
3. A sincronização traz o extrato bancário e as transações de cartão, e guarda o saldo da API como saldo de extrato (a conciliação compara com o calculado). Roda sozinha ao abrir o app (se a última passou de 6 h) e, depois de **`agendar_sincronizacao.bat`**, todo dia às 08:00 pelo Windows (se o PC estava desligado, roda ao ligar) com aviso na bandeja se faltar o OFX do PagBank. Remover: `agendar_sincronizacao.bat remover`. Manual: `sincronizar.bat`. Log: `data/sync.log`.

Regras da sincronização:
- Só entram transações **processadas**; pendentes e canceladas ficam de fora (as pendentes entram quando liquidarem).
- No extrato bancário o valor vem sempre positivo; entrada/saída é deduzida do tipo da transação (`*_IN`/`*_OUT` ou palavras como RECEBIDO/ENVIADO). Se não der para saber, a transação **não é importada** e é listada no resultado da sincronização, para você revisar, em vez de chutar o sinal.
- Cada sincronização refaz os últimos 7 dias para pegar lançamentos tardios; o que já existe é reconhecido pelo ID da API.
- Se a conta Conta Simples já tinha extratos OFX, a primeira sincronização começa no dia seguinte ao último OFX (IDs de OFX e API são diferentes; misturar o mesmo período duplicaria).
- **Cartões com limite garantido por depósito (caso da Conta Simples) — limite disponível = caixa.** O dinheiro que sai da conta é a "Transferência de limite"; no extrato do cartão ela aparece como *LIMIT* (carga de limite) e **o pagamento de cada fatura também aparece como *LIMIT***, mas este só liquida compras que já abateram o limite (não é dinheiro novo; os valores batem com as faturas da API). A sincronização: (1) pareia cada carga com a saída da conta de mesmo valor (±2 dias) e vincula como **transferência**; (2) **ignora** o que sobra sem par há mais de 3 dias (pagamento de fatura; fica em *Investigar → Ignorados*, restaurável); (3) guarda as **compras pendentes** do cartão, que já reduzem o limite disponível. Limite disponível = depósitos − compras − compras pendentes (a API não informa o limite atual diretamente; se já havia limite antes da data inicial, informe como saldo inicial da conta de cartões).
- "Rentabilidade CDI" creditada no saldo da conta é importada como **receita financeira** (só as já liquidadas, status processado).
- Intervalo de cartões é buscado em janelas de 60 dias (limite da API: 62); respeita o limite de 100 chamadas/min com repetição automática.

## CDI / renda fixa do PagBank (caixa = disponível + aplicado)

No PagBank o caixa não é só o saldo: parte fica aplicada em CDB/CDI. O OFX traz as **aplicações** e **resgates** (e vencimento/reinvestimento), mas o saldo do extrato é só o **disponível** (pode ser R$ 0,00 com tudo aplicado) e o valor aplicado não vem no arquivo.

- Na importação, linhas com "Renda Fixa" ou "CDB" são classificadas sozinhas como **Aplicação / resgate (CDI)**. É movimento interno: não é receita nem despesa e não entra em *entradas/saídas da empresa*. Você pode reclassificar.
- O **saldo disponível calculado** continua sendo comparado com o saldo do extrato (a conciliação não é distorcida pelo CDI).
- Em **Contas → Posição em CDI**, informe quanto está aplicado (total com rendimento, como no app do banco) ao fim de um dia. O Painel mostra *disponível + CDI = caixa total da conta*, e o saldo total usa o caixa total. Com duas ou mais posições, estima o **rendimento** (posição final − inicial − aplicações + resgates). Atualize a posição quando quiser (ex.: semanalmente).
- **Saldo inicial do PagBank = só o disponível** na data escolhida. Em *Contas*, o app sugere o valor que faz o saldo calculado bater com o saldo do extrato.
- O OFX do PagBank usa a data do saldo no formato `13/10/2026` e repete o mesmo FITID nas duas pontas de um evento (ex.: reinvestimento); ambos são tratados.

## Exportar e backup
- **CSV** dos lançamentos classificados (UTF-8, `;`, vírgula decimal; inclui `valor_centavos`).
- **Backup** do SQLite em `data/backups/` (cópia consistente), com botão para baixar.

Para recuperar: feche o app e substitua `data/caixa_qcm.db` pelo arquivo do backup.

## Convenções técnicas
- Dinheiro: **centavos inteiros** (`Decimal` só na leitura do OFX); nunca `float`.
- Datas em **America/Sao_Paulo**. Datas OFX com hora e fuso são convertidas; meia-noite com fuso (ex.: `000000[0:GMT]`) é tratada como "só data" para não empurrar o lançamento ao dia anterior.
- Dados: `data/` (banco, `origem/`, `anexos/`, `backups/`). Variável `CAIXA_QCM_DATA` troca a pasta.

## Estrutura
```
app.py            interface Streamlit
caixa/            db, ofx, importer, ledger, recon, export, money, timeutil, demo
demo/             OFX fictícios (+ gerar_ofx_demo.py)
tests/            reimportação, transferências, saldos, UI
```

## Fora desta versão
Importadores de CSV, API do PagBank/Open Finance, assinatura de requisições da Conta Simples (escopos de escrita), apagar lote importado, múltiplos anexos por lançamento.
