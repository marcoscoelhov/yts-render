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
Um roteiro fornecido por uma pessoa como fonte de verdade editorial para um **Job de Video**.
_Avoid_: prompt, tema, titulo completo

**Texto Rotulado**:
Um texto dividido por rotulos editoriais reconheciveis, como titulo, hook, beats, payoff e fechamento.
_Avoid_: JSON, prompt livre, markdown arbitrario

**Fato Declarado**:
Uma afirmacao factual em um **Roteiro Pronto** cuja revisao e assumida por quem enviou o roteiro.
_Avoid_: fato verificado pelo app, fonte automatica, suposicao

**Confirmacao de Factualidade**:
A declaracao de que os **Fatos Declarados** em um **Roteiro Pronto** ja foram revisados antes do envio.
_Avoid_: fact-check automatico, fonte do app, aprovacao de publicacao

**Horario de Publicacao**:
A data, hora e fuso escolhidos para publicar um **Job de Video** aprovado.
_Avoid_: data tecnica, timestamp cru, horario do servidor

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
- Um **Roteiro Pronto** pode conter **Fatos Declarados**.
- **Fatos Declarados** dependem de **Confirmacao de Factualidade**.
- O **Hub de Revisao** oferece **Roteiro Pronto** como modo de entrada distinto de tema e titulo.
- Um **Horario de Publicacao** so deve ser escolhido depois da aprovacao do **Job de Video**.

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
> **Dev:** "O gerador pode trocar a ideia central do roteiro para melhorar retencao?"
> **Domain expert:** "Nao. Roteiro Pronto e fonte de verdade editorial; melhorias automaticas devem ser conservadoras."
> **Dev:** "Se o roteiro pronto vier com problema mecanico, o job deve falhar direto?"
> **Domain expert:** "Nao. Pode reparar automaticamente, desde que preserve a intencao editorial."
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

## Flagged ambiguities

- "video" foi usado tanto para o arquivo final quanto para o fluxo completo de criacao; resolvido: em pedidos operacionais, use **Job de Video**.
- "pronto" foi usado tanto para pronto para assistir quanto para pronto para publicar; resolvido: neste fluxo, sucesso significa pronto para **Revisao Humana**.
- "hub" foi usado para falar tanto da superficie de revisao quanto de portas locais; resolvido: use **Hub de Revisao** para a superficie, e mantenha uma unica porta operacional.
- "amigavel" nao significa apenas visual bonito; resolvido: o **Hub de Revisao** deve seguir um **Fluxo de Decisao**.
- "dark mode" nao significa tema alternavel por usuario neste momento; resolvido: e o padrao visual do **Console Operacional**.
- "tema automatico" nao significa escolha aleatoria; resolvido: o sistema deve preferir tendencia real e expor quando caiu em fallback.
- "roteiro pronto" nao significa prompt livre; resolvido: e conteudo editorial estruturado fornecido por uma pessoa e tratado como fonte de verdade.
- "reparar automaticamente" nao significa reescrever criativamente; resolvido: o reparo deve ser conservador e preservar a intencao editorial do **Roteiro Pronto**.
- "texto rotulado" nao significa JSON nem markdown livre; resolvido: o formato canonico inicial usa rotulos editoriais em texto simples.
- "confiar em mim" nao significa que o fato foi verificado automaticamente pelo app; resolvido: fatos do **Roteiro Pronto** entram como **Fatos Declarados** sob responsabilidade de quem enviou.
- "confirmacao de factualidade" nao significa aprovacao de publicacao; resolvido: ela cobre a responsabilidade factual do **Roteiro Pronto**.
- "pular geracao por LLM" nao significa pular validacao; resolvido: **Roteiro Pronto** preserva o texto enviado, mas ainda passa por gates e reparos conservadores.
- "ajustar duracao" nao significa expandir ou cortar livremente; resolvido: desvios pequenos podem ser reparados, desvios grandes bloqueiam antes da midia.
- "titulo" em **Roteiro Pronto** nao significa fala narrada; resolvido: titulo e metadado, enquanto hook, beats, payoff e fechamento formam a narracao.
- "hashtags" em **Roteiro Pronto** nao sao fonte de verdade narrativa; resolvido: podem ser derivadas automaticamente como metadados.
- "data e hora" em agendamento nao significa horario do servidor; resolvido: use **Horario de Publicacao**, com fuso explicito.
