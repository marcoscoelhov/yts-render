# YTS Render Context

Este contexto define a linguagem de dominio usada para gerar, revisar e preparar Shorts no projeto.

## Language

**Job de Video**:
Uma unidade de trabalho que cobre a criacao de um Short desde o pedido editorial ate um resultado revisavel.
_Avoid_: video, render, tarefa

**Arquivo de Video Final**:
O arquivo de midia produzido por um **Job de Video** para revisao humana.
_Avoid_: video, output, render

**Revisao Humana**:
A avaliacao feita por uma pessoa antes de aprovar, rejeitar, agendar ou publicar um **Job de Video**.
_Avoid_: aprovado, publicavel, upload pronto

**Hub de Revisao**:
A superficie operacional onde uma pessoa acompanha, assiste e decide sobre **Jobs de Video**.
_Avoid_: servidor, porta, painel

**Console Operacional**:
Uma apresentacao do **Hub de Revisao** orientada a fila, estado e proxima acao sobre **Jobs de Video**.
_Avoid_: formulario administrativo, landing page, dashboard decorativo

**Fluxo de Decisao**:
A ordem de tela que prioriza a proxima acao humana sobre diagnosticos e configuracoes.
_Avoid_: dashboard generico, tela de dados, painel tecnico

**Tema Automatico**:
Um tema escolhido pelo sistema quando o pedido nao traz um assunto explicito.
_Avoid_: tema aleatorio, fallback local, sugestao solta

**Roteiro Pronto**:
Um roteiro fornecido por uma pessoa como fonte de verdade editorial para um **Job de Video**; o sistema nao deve reescrever hook, beats, payoff ou fechamento automaticamente.
_Avoid_: prompt, tema, titulo completo

**Texto Rotulado**:
Um texto dividido por rotulos editoriais reconheciveis, como titulo, hook, beats, payoff e fechamento.
_Avoid_: JSON, prompt livre, markdown arbitrario

**Loop Editorial**:
A tensao narrativa que sustenta a curiosidade entre o hook e a entrega dos beats em um **Roteiro Pronto**.
_Avoid_: fato declarado, fonte factual, CTA

**Fato Declarado**:
Uma afirmacao factual em um **Roteiro Pronto** cuja revisao e assumida por quem enviou o roteiro.
_Avoid_: fato verificado pelo app, fonte automatica, suposicao

**Confirmacao de Factualidade**:
A declaracao de que os **Fatos Declarados** em um **Roteiro Pronto** ja foram revisados antes do envio.
_Avoid_: fact-check automatico, fonte do app, aprovacao de publicacao

**Horario de Publicacao**:
A data, hora e fuso escolhidos para publicar um **Job de Video** aprovado.
_Avoid_: data tecnica, timestamp cru, horario do servidor

**Calendario de Publicacao**:
A visao mensal do **Hub de Revisao** usada para consultar e criar **Horarios de Publicacao** por dia.
_Avoid_: agenda passiva, relatorio mensal, calendario externo

**Progresso do Job**:
A leitura operacional de onde um **Job de Video** esta no pipeline, quais etapas ja terminaram, qual etapa esta em andamento e qual proxima acao resta.
_Avoid_: log bruto, porcentagem decorativa, timeline tecnica

**Limite de Provedor**:
A recusa de um provedor em continuar uma geracao porque a conta, chave ou plano atingiu quota, credito, saldo ou rate limit.
_Avoid_: timeout, erro generico, instabilidade temporaria

**Chave Esgotada**:
Uma chave de provedor que ja encontrou **Limite de Provedor** durante uma geracao e nao deve ser tentada novamente no mesmo **Job de Video**.
_Avoid_: chave invalida, provider offline, timeout, bloqueio diario global automatico

**Chave Dedicada de Imagem**:
Uma chave MiniMax separada para geracao de imagens, usada quando a chave primaria encontra **Limite de Provedor**.
_Avoid_: provider editorial diferente, fallback local, banco de imagens

## Relationships

- Um **Job de Video** produz zero ou um **Arquivo de Video Final**.
- Um **Arquivo de Video Final** pertence a exatamente um **Job de Video**.
- Um **Job de Video** pode chegar a **Revisao Humana** sem estar aprovado para publicacao.
- Um **Hub de Revisao** apresenta um ou mais **Jobs de Video** para **Revisao Humana**.
- Um **Hub de Revisao** pode se apresentar como **Console Operacional**.
- Um **Hub de Revisao** deve organizar a tela como **Fluxo de Decisao**.
- Um **Job de Video** pode comecar a partir de um **Tema Automatico**.
- Um **Job de Video** pode comecar a partir de um **Roteiro Pronto**.
- Um **Roteiro Pronto** deve ser enviado como **Texto Rotulado**.
- Um **Roteiro Pronto** deve conter **Loop Editorial** entre hook e beats.
- Um **Roteiro Pronto** pode conter **Fatos Declarados**.
- **Fatos Declarados** dependem de **Confirmacao de Factualidade**.
- **Loop Editorial** nao e **Fato Declarado** por si so.
- O **Hub de Revisao** oferece **Roteiro Pronto** como modo de entrada distinto de tema e titulo.
- Um **Horario de Publicacao** so deve ser escolhido depois da aprovacao do **Job de Video**.
- Um **Calendario de Publicacao** pode criar um **Horario de Publicacao** para um **Job de Video** aprovado, desde que ele ainda nao esteja publicado nem tenha agenda ativa.
- Um **Hub de Revisao** deve exibir o **Progresso do Job** sem exigir leitura de logs ou artefatos tecnicos.
- **Limite de Provedor** deve ser distinguido de falha transiente antes de trocar a origem da geracao.
- Uma **Chave Esgotada** deve ser evitada pelo restante do **Job de Video** em andamento.
- **Chave Dedicada de Imagem** deve ser usada depois que a chave primaria de imagem vira **Chave Esgotada**.

## Example dialogue

> **Dev:** "Quando voce pede para gerar um video, quer apenas o arquivo de video final?"
> **Domain expert:** "Nao. Quero um Job de Video completo, com arquivo final, estado terminal e sinais suficientes para revisar publicacao."
> **Dev:** "Se o job chegou em revisao, isso significa que ja pode publicar?"
> **Domain expert:** "Nao. Revisao Humana e a fronteira para eu assistir e decidir; publicacao vem depois."
> **Dev:** "Hub significa qualquer servidor aberto localmente?"
> **Domain expert:** "Nao. Hub de Revisao e a superficie unica onde acompanho os jobs; portas duplicadas sao detalhe operacional e devem ser evitadas."
> **Dev:** "A home deve mostrar todos os blocos tecnicos antes das acoes?"
> **Domain expert:** "Nao. Fluxo de Decisao vem primeiro: criar, revisar, aprovar, agendar; diagnosticos ficam depois."
> **Dev:** "Console operacional quer dizer uma tela clara com graficos bonitos?"
> **Domain expert:** "Nao. Console Operacional quer dizer fila, estado e proxima acao em primeiro plano; o modo escuro e o padrao visual escolhido para essa superficie."
> **Dev:** "Sem tema explicito, posso escolher qualquer assunto do pool local?"
> **Domain expert:** "Nao. Use Tema Automatico, com preferencia por tendencia real e rastreabilidade."
> **Dev:** "Se eu mando titulo, hook, beats, payoff e fechamento, isso e so um prompt?"
> **Domain expert:** "Nao. Isso e um Roteiro Pronto; o sistema deve preservar a intencao editorial e nao tratar como tema bruto."
> **Dev:** "Loop e mais um fato que preciso rastrear?"
> **Domain expert:** "Nao. Loop Editorial e tensao narrativa. Os fatos declarados ficam nos beats e no payoff."
> **Dev:** "O gerador pode trocar a ideia central do roteiro para melhorar retencao?"
> **Domain expert:** "Nao. Roteiro Pronto e fonte de verdade editorial; o texto enviado deve ser preservado."
> **Dev:** "Se o roteiro pronto vier com problema mecanico, o job deve falhar direto?"
> **Domain expert:** "Sim, se o problema impedir o pipeline; nao reescreva automaticamente o roteiro pronto."
> **Dev:** "Posso mandar esse roteiro pronto em JSON?"
> **Domain expert:** "Nao por enquanto. O formato canonico e Texto Rotulado."
> **Dev:** "Se o roteiro pronto traz numeros factuais, o app precisa refazer toda a checagem?"
> **Domain expert:** "Nao. Esses numeros entram como Fatos Declarados quando eu assumo que ja revisei o roteiro antes de enviar."
> **Dev:** "Essa confirmacao quer dizer que o job ja esta aprovado para publicar?"
> **Domain expert:** "Nao. Confirmacao de Factualidade cobre os fatos declarados; Revisao Humana ainda decide publicacao."
> **Dev:** "Posso colocar roteiro pronto no mesmo campo de tema?"
> **Domain expert:** "Nao. Roteiro Pronto e um modo de entrada proprio no Hub de Revisao."
> **Dev:** "O LLM deve gerar outro roteiro a partir do roteiro pronto?"
> **Domain expert:** "Nao. Roteiro Pronto pula a geracao de roteiro por LLM; o texto enviado e a fonte de verdade."
> **Dev:** "Se o roteiro pronto estiver muito curto ou muito longo, posso completar ou cortar livremente?"
> **Domain expert:** "Nao. Ajuste apenas desvios pequenos; desvios grandes devem bloquear antes da midia."
> **Dev:** "O titulo do roteiro pronto deve ser narrado?"
> **Domain expert:** "Nao. O titulo e metadado; a narracao comeca no hook e segue ate o fechamento."
> **Dev:** "Se o roteiro pronto nao trouxer hashtags, isso bloqueia o job?"
> **Domain expert:** "Nao. Hashtags sao metadados e podem ser completadas automaticamente sem alterar o roteiro."
> **Dev:** "Data e hora no job e um timestamp tecnico?"
> **Domain expert:** "Nao. E o Horario de Publicacao: a escolha humana de quando o Short aprovado deve ir ao YouTube."
> **Dev:** "O calendario serve apenas para ver os jobs ja agendados?"
> **Domain expert:** "Nao. O Calendario de Publicacao tambem deve permitir criar Horario de Publicacao no dia escolhido para jobs aprovados e ainda livres para agendar."
> **Dev:** "Progresso quer dizer mostrar todos os logs do worker?"
> **Domain expert:** "Nao. Progresso do Job e uma leitura resumida das etapas reais: concluido, rodando, pendente ou falhou."
> **Dev:** "Timeout da MiniMax conta como limite de uso?"
> **Domain expert:** "Nao. Limite de Provedor e quota, saldo, credito ou rate limit; timeout e falha transiente."
> **Dev:** "Se a chave bateu quota em uma imagem, tento de novo na proxima cena?"
> **Domain expert:** "Nao. Marque como Chave Esgotada para o restante do Job de Video e use a alternativa dedicada."
> **Dev:** "A chave dedicada muda o fornecedor editorial da imagem?"
> **Domain expert:** "Nao. Continua sendo MiniMax; a Chave Dedicada de Imagem so muda a credencial usada depois de limite."

## Flagged ambiguities

- "video" foi usado tanto para o arquivo final quanto para o fluxo completo de criacao; resolvido: em pedidos operacionais, use **Job de Video**.
- "pronto" foi usado tanto para pronto para assistir quanto para pronto para publicar; resolvido: neste fluxo, sucesso significa pronto para **Revisao Humana**.
- "hub" foi usado para falar tanto da superficie de revisao quanto de portas locais; resolvido: use **Hub de Revisao** para a superficie, e mantenha uma unica porta operacional.
- "amigavel" nao significa apenas visual bonito; resolvido: o **Hub de Revisao** deve seguir um **Fluxo de Decisao**.
- "dark mode" nao significa tema alternavel por usuario neste momento; resolvido: e o padrao visual do **Console Operacional**.
- "tema automatico" nao significa escolha aleatoria; resolvido: o sistema deve preferir tendencia real e expor quando caiu em fallback.
- "roteiro pronto" nao significa prompt livre; resolvido: e conteudo editorial estruturado fornecido por uma pessoa e tratado como fonte de verdade.
- "loop" em **Roteiro Pronto** nao significa claim factual; resolvido: use **Loop Editorial** como tensao de retencao entre hook e beats.
- "reparar automaticamente" nao se aplica ao texto de **Roteiro Pronto**; resolvido: se o texto pronto tiver problema que bloqueia o pipeline, bloqueie e exponha o motivo em vez de reescrever hook, beats, payoff ou fechamento.
- "texto rotulado" nao significa JSON nem markdown livre; resolvido: o formato canonico inicial usa rotulos editoriais em texto simples.
- "confiar em mim" nao significa que o fato foi verificado automaticamente pelo app; resolvido: fatos do **Roteiro Pronto** entram como **Fatos Declarados** sob responsabilidade de quem enviou.
- "confirmacao de factualidade" nao significa aprovacao de publicacao; resolvido: ela cobre a responsabilidade factual do **Roteiro Pronto**.
- "pular geracao por LLM" nao significa pular validacao; resolvido: **Roteiro Pronto** preserva o texto enviado e eventuais problemas viram warnings, revisão ou bloqueio, nao reparo automatico do roteiro.
- "ajustar duracao" nao significa expandir ou cortar livremente; resolvido: desvios pequenos podem ser reparados, desvios grandes bloqueiam antes da midia.
- "titulo" em **Roteiro Pronto** nao significa fala narrada; resolvido: titulo e metadado, enquanto hook, beats, payoff e fechamento formam a narracao.
- "hashtags" em **Roteiro Pronto** nao sao fonte de verdade narrativa; resolvido: podem ser derivadas automaticamente como metadados.
- "data e hora" em agendamento nao significa horario do servidor; resolvido: use **Horario de Publicacao**, com fuso explicito.
- "calendario" nao significa visualizacao passiva; resolvido: o **Calendario de Publicacao** tambem e ponto de entrada para agendar jobs aprovados por dia.
- "progresso" nao significa percentual inventado nem log bruto; resolvido: derive o **Progresso do Job** das etapas reais, execucoes persistidas e estado atual do job.
- "limite" de provedor nao significa qualquer falha de API; resolvido: use **Limite de Provedor** apenas para quota, saldo, credito ou rate limit.
- "esgotada" nao significa que a chave foi revogada nem que todo job futuro deve bloquear a chave; resolvido: **Chave Esgotada** vale para evitar novas tentativas no job atual apos quota ou rate limit.
- "fallback de imagem" nao significa provider editorial diferente neste caso; resolvido: use **Chave Dedicada de Imagem** para a credencial MiniMax alternativa.
