/* ==========================================================================
   app.js - logica do dashboard no browser (v3)
   ==========================================================================
   O que este ficheiro faz, por ordem:
     1. Funcoes de formatacao (dinheiro, datas, tempo decorrido)
     2. Sistema de toast (mensagens rapidas) e modais (confirmacoes)
     3. Os dois graficos Chart.js
     4. Controlo do bot: estado, iniciar/parar, toggle SIMULADO/REAL
     5. Polling: a cada 4 segundos pede tudo ao Flask e atualiza a pagina
   JavaScript "puro", sem frameworks - proposital para ser facil de ler.
   ========================================================================== */

// Cores - as mesmas variaveis do style.css, lidas do proprio CSS
// para nunca ficarem dessincronizadas entre graficos e resto da pagina
const css = getComputedStyle(document.documentElement);
const COR_AZUL = css.getPropertyValue("--azul").trim();
const COR_VERDE = css.getPropertyValue("--verde").trim();
const COR_VERMELHO = css.getPropertyValue("--vermelho").trim();
const COR_TEXTO_MUDO = css.getPropertyValue("--texto-mudo").trim();
const COR_GRELHA = css.getPropertyValue("--linha-grelha").trim();

const INTERVALO_POLLING_MS = 4000; // pede dados novos a cada 4 segundos

// ==========================================================================
// 1) Funcoes de formatacao e pequenos utilitarios
// ==========================================================================

/** Atalho para document.getElementById - usado em todo o lado */
function el(id) { return document.getElementById(id); }

/** Formata um numero como dinheiro: 12.3456 -> "$12.35" */
function dinheiro(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return "$" + valor.toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Igual, mas com sinal explicito: +$1.20 / -$0.50 (para lucros) */
function dinheiroComSinal(valor) {
  if (valor === null || valor === undefined) return "N/A";
  return (valor >= 0 ? "+" : "-") + dinheiro(Math.abs(valor));
}

/** Formata uma percentagem com sinal: +1,15% / -3,20% */
function percentagemComSinal(valor) {
  const texto = Math.abs(valor).toLocaleString("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (valor >= 0 ? "+" : "-") + texto + "%";
}

/** Converte um timestamp ISO (UTC) para data/hora local legivel */
function dataHora(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleDateString("pt-PT", { day: "2-digit", month: "2-digit" }) +
         " " + d.toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
}

/** "Ha quanto tempo": recebe o ISO e devolve ex. "2h 15m" */
function tempoDecorrido(iso) {
  if (!iso) return "–";
  const segundos = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const dias = Math.floor(segundos / 86400);
  const horas = Math.floor((segundos % 86400) / 3600);
  const minutos = Math.floor((segundos % 3600) / 60);
  if (dias > 0) return `${dias}d ${horas}h`;
  if (horas > 0) return `${horas}h ${minutos}m`;
  return `${minutos}m`;
}

/** Pinta um elemento de verde/vermelho consoante o valor ser +/- */
function pintarPorSinal(elemento, valor) {
  elemento.classList.remove("positivo", "negativo");
  if (valor > 0) elemento.classList.add("positivo");
  if (valor < 0) elemento.classList.add("negativo");
}

/** Escreve texto num elemento COM flash violeta se o valor mudou
    (a micro-animacao dos cartoes de resumo) */
function definirValor(id, texto) {
  const elemento = el(id);
  if (elemento.textContent !== texto) {
    elemento.textContent = texto;
    elemento.classList.remove("flash");
    // Truque: forcar o browser a "ver" a remocao antes de repor a
    // classe, senao a animacao so corria na primeira mudanca
    void elemento.offsetWidth;
    elemento.classList.add("flash");
  }
}

/** Mostra o grafico OU a mensagem de vazio, nunca os dois ao mesmo tempo */
function alternarVazio(canvas, idMensagem, temDados) {
  canvas.closest(".area-grafico").hidden = !temDados;
  el(idMensagem).hidden = temDados;
}

// ==========================================================================
// 2) Toast (mensagens rapidas) e modais (confirmacoes)
// ==========================================================================

let _timerToast = null;

/** Mostra uma mensagem temporaria no fundo do ecra.
    tipo: "erro" (vermelho), "sucesso" (verde) ou "" (neutro) */
function toast(mensagem, tipo = "") {
  const caixa = el("toast");
  caixa.textContent = mensagem;
  caixa.className = "toast " + tipo;
  caixa.hidden = false;
  clearTimeout(_timerToast);
  _timerToast = setTimeout(() => { caixa.hidden = true; }, 4000);
}

/** Abre um dos modais (pelo id) por cima do fundo escurecido */
function abrirModal(id) {
  el("modal-fundo").hidden = false;
  // Esconde todos os modais e mostra so o pedido
  document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; });
  el(id).hidden = false;
}

/** Fecha tudo (fundo + modais) e limpa a caixa do CONFIRMO */
function fecharModais() {
  el("modal-fundo").hidden = true;
  el("input-confirmo").value = "";
  el("btn-real-confirmar").disabled = true;
}

// Qualquer botao com o atributo data-fechar cancela o modal
document.querySelectorAll("[data-fechar]").forEach((btn) => {
  btn.addEventListener("click", fecharModais);
});
// Tocar no fundo escurecido (fora do dialogo) tambem cancela
el("modal-fundo").addEventListener("click", (evento) => {
  if (evento.target === el("modal-fundo")) fecharModais();
});

// ==========================================================================
// 3) Graficos (comecam vazios, o polling enche-os)
// ==========================================================================

// Opcoes partilhadas: grelha discreta, texto mudo, sem animacao a cada
// refresh (senao os graficos "dancavam" de 4 em 4 segundos)
const opcoesBase = {
  responsive: true,
  maintainAspectRatio: false, // deixa o CSS (.area-grafico) mandar na altura
  animation: false,
  plugins: { legend: { display: false } }, // uma serie so -> o titulo do painel chega
  scales: {
    x: {
      ticks: { color: COR_TEXTO_MUDO, maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 11 } },
      grid: { display: false },
    },
    y: {
      ticks: { color: COR_TEXTO_MUDO, callback: (v) => "$" + v, font: { size: 11 } },
      grid: { color: COR_GRELHA },
    },
  },
};

// --- Grafico 1: curva de saldo (linha) -----------------------------------
const graficoSaldo = new Chart(el("grafico-saldo"), {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "Saldo (USD)",
      data: [],
      borderColor: COR_AZUL,
      backgroundColor: COR_AZUL,
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5, // alvo de toque maior no telemovel
      tension: 0.15,       // curva ligeiramente suavizada
    }],
  },
  options: opcoesBase,
});

// --- Grafico 2: "velas" de lucro/prejuizo por venda -----------------------
// Cada venda fechada e uma barra que parte de 0: para cima e verde
// (lucro), para baixo e vermelha (prejuizo). Os dados chegam como
// pares [0, lucro] ("floating bars" do Chart.js).
const graficoVelas = new Chart(el("grafico-velas"), {
  type: "bar",
  data: {
    labels: [],
    datasets: [{
      label: "Lucro da venda (USD)",
      data: [],
      backgroundColor: [], // uma cor por barra, definida no refresh
      borderRadius: 4,     // pontas arredondadas
      borderSkipped: false,
      maxBarThickness: 26,
    }],
  },
  options: {
    ...opcoesBase,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          // Titulo do tooltip: simbolo + data/hora da venda (a data vem
          // guardada junto de cada barra, no campo extra "datasVendas")
          title: (itens) => {
            const i = itens[0];
            return `${i.label} · ${graficoVelas.data.datasets[0].datasVendas[i.dataIndex] || ""}`;
          },
          // Corpo: o lucro real, nao o par [0, lucro]
          label: (ctx) => " " + dinheiroComSinal(ctx.raw[1]),
        },
      },
    },
  },
});

// ==========================================================================
// 4) Controlo do bot (Parte 1) e do modo SIMULADO/REAL (Parte 3)
// ==========================================================================

// Ultimo estado recebido de /api/bot/status - os botoes decidem o que
// fazer com base nisto (ex: iniciar em REAL pede confirmacao extra)
let estadoBot = null;

/** POST simples para as rotas de acao; devolve o JSON da resposta */
async function pedirAcao(url, corpo = null) {
  const resposta = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: corpo ? JSON.stringify(corpo) : null,
  });
  return resposta.json();
}

/** Arranca o bot de verdade (chamado apos as confirmacoes necessarias) */
async function iniciarBot() {
  fecharModais();
  const r = await pedirAcao("/api/bot/iniciar");
  if (r.ok) toast("Bot iniciado" + (r.dry_run ? " (simulado)" : " em modo REAL"), "sucesso");
  else toast(r.erro || "Não foi possível iniciar o bot.", "erro");
  await atualizarStatus();
}

/** Para o bot (SIGTERM -> saida limpa) */
async function pararBot() {
  el("btn-bot").disabled = true; // evita duplo-clique enquanto para
  const r = await pedirAcao("/api/bot/parar");
  if (r.ok) toast("Bot parado.", "sucesso");
  else toast(r.erro || "Não foi possível parar o bot.", "erro");
  await atualizarStatus();
}

// --- Botao principal Iniciar/Parar ---
el("btn-bot").addEventListener("click", () => {
  if (!estadoBot) return;
  if (estadoBot.a_correr) {
    pararBot();
  } else if (!estadoBot.dry_run_env) {
    // .env em modo REAL -> confirmacao extra explicita antes de arrancar
    abrirModal("modal-iniciar-real");
  } else {
    iniciarBot();
  }
});
el("btn-iniciar-real-sim").addEventListener("click", iniciarBot);

// --- Botao "Reiniciar bot" da barra de desfasamento ---
el("btn-reiniciar").addEventListener("click", async () => {
  el("btn-reiniciar").disabled = true;
  toast("A reiniciar o bot…");
  await pedirAcao("/api/bot/parar");
  const r = await pedirAcao("/api/bot/iniciar");
  if (r.ok) toast("Bot reiniciado no modo " + (r.dry_run ? "SIMULADO" : "REAL") + ".", "sucesso");
  else toast(r.erro || "Falha ao reiniciar.", "erro");
  el("btn-reiniciar").disabled = false;
  await atualizarStatus();
});

// --- Toggle SIMULADO/REAL (controlo segmentado) ---

/** Pede ao backend para mudar o modo no .env */
async function mudarModo(dryRun, confirmacao = null) {
  fecharModais();
  const corpo = { dry_run: dryRun };
  if (confirmacao) corpo.confirmacao = confirmacao;
  const r = await pedirAcao("/api/modo", corpo);
  if (!r.ok) { toast(r.erro || "Não foi possível mudar o modo.", "erro"); return; }

  if (r.precisa_reiniciar) {
    toast("Modo alterado no .env — o bot precisa de reiniciar para aplicar.", "sucesso");
  } else {
    toast("Modo alterado para " + (dryRun ? "SIMULADO" : "REAL") + ".", "sucesso");
  }
  await atualizarStatus();
}

// SIMULADO: direcao segura, muda logo (sem dupla confirmacao)
el("btn-modo-simulado").addEventListener("click", () => {
  if (estadoBot && estadoBot.dry_run_env) return; // ja esta em simulado
  mudarModo(true);
});

// REAL: abre o 1.o modal de aviso; o resto da cadeia esta nos botoes abaixo
el("btn-modo-real").addEventListener("click", () => {
  if (estadoBot && !estadoBot.dry_run_env) return; // ja esta em real
  if (estadoBot && !estadoBot.fase2_ok) {
    toast("WALLET_PRIVATE_KEY não configurada — sem wallet não há modo REAL.", "erro");
    return;
  }
  abrirModal("modal-real-aviso");
});

// Modal A ("Tens a certeza?") -> passa ao modal B (escrever CONFIRMO)
el("btn-real-continuar").addEventListener("click", () => {
  abrirModal("modal-real-confirmo");
  el("input-confirmo").focus();
});

// Modal B: o botao final so ativa quando o texto for exatamente CONFIRMO
el("input-confirmo").addEventListener("input", () => {
  el("btn-real-confirmar").disabled = el("input-confirmo").value.trim() !== "CONFIRMO";
});
el("btn-real-confirmar").addEventListener("click", () => {
  mudarModo(false, el("input-confirmo").value.trim());
});

// ==========================================================================
// 5) Polling: buscar dados ao Flask e atualizar a pagina
// ==========================================================================

/** Atualiza o cabecalho: estado do bot, botao, seletor de modo e aviso */
async function atualizarStatus() {
  const resposta = await fetch("/api/bot/status");
  estadoBot = await resposta.json();

  // --- Indicador A CORRER / PARADO ---
  const bolinha = el("bolinha-bot");
  const estado = el("estado-bot");
  if (estadoBot.a_correr) {
    bolinha.classList.add("viva");
    estado.classList.add("a-correr");
    el("texto-estado-bot").textContent = "Bot: A CORRER";
  } else {
    bolinha.classList.remove("viva");
    estado.classList.remove("a-correr");
    el("texto-estado-bot").textContent = "Bot: PARADO";
  }

  // --- Botao Iniciar/Parar (cor e texto mudam com o estado) ---
  const btn = el("btn-bot");
  btn.disabled = false;
  if (estadoBot.a_correr) {
    btn.textContent = "Parar Bot";
    btn.className = "btn btn-perigo";
  } else {
    btn.textContent = "Iniciar Bot";
    btn.className = "btn btn-verde";
    // Sem Camada 1 nao vale a pena deixar arrancar (o backend tambem recusa)
    if (!estadoBot.camada1_ok) {
      btn.disabled = true;
      btn.title = "Configura DEEPSEEK_API_KEY no .env primeiro";
    }
  }

  // --- Seletor SIMULADO/REAL reflete o .env ATUAL ---
  el("btn-modo-simulado").className =
    "segmento" + (estadoBot.dry_run_env ? " ativo-simulado" : "");
  el("btn-modo-real").className =
    "segmento" + (!estadoBot.dry_run_env ? " ativo-real" : "");

  // --- Aviso de desfasamento (bot a correr com modo != .env) ---
  el("aviso-desfasado").hidden = !estadoBot.desfasado;
  if (estadoBot.desfasado) {
    const modoBot = estadoBot.dry_run_bot ? "SIMULADO" : "REAL";
    const modoEnv = estadoBot.dry_run_env ? "SIMULADO" : "REAL";
    el("texto-desfasado").textContent =
      `O bot está a correr em modo ${modoBot}, mas o .env diz ${modoEnv} — reinicia para aplicar.`;
  }
}

/** Atualiza a caixa "Log ao vivo" com as ultimas linhas do bot.log */
async function atualizarLog() {
  const resposta = await fetch("/api/bot/log");
  const dados = await resposta.json();
  const caixa = el("log-bot");

  const texto = dados.linhas.length
    ? dados.linhas.join("\n")
    : "O log aparece aqui quando o bot arrancar…";
  if (caixa.textContent === texto) return; // nada mudou

  // So faz scroll automatico se o utilizador ja estiver "colado" ao
  // fundo - se ele subiu para ler algo, nao o puxamos para baixo
  const coladoAoFundo =
    caixa.scrollHeight - caixa.scrollTop - caixa.clientHeight < 40;
  caixa.textContent = texto;
  if (coladoAoFundo) caixa.scrollTop = caixa.scrollHeight;
}

/** Atualiza cartoes, badge, graficos e tabela de historico */
async function atualizarResumo() {
  const resposta = await fetch("/api/resumo");
  const dados = await resposta.json();
  const c = dados.cartoes;

  // --- Cartoes de resumo (com flash quando o valor muda) ---
  definirValor("c-saldo-inicial", dinheiro(c.saldo_inicial));
  definirValor("c-saldo-livre", dinheiro(c.saldo_livre));
  definirValor("c-valor-posicoes", dinheiro(c.valor_posicoes));
  definirValor("c-valor-total", dinheiro(c.valor_total));
  definirValor("c-n-compras", String(c.n_compras));
  definirValor("c-n-vendas", String(c.n_vendas));
  definirValor("c-win-rate", c.win_rate === null ? "–" : c.win_rate + "%");

  const lucro = el("c-lucro-total");
  definirValor("c-lucro-total",
    `${dinheiroComSinal(c.lucro_total)} (${percentagemComSinal(c.lucro_total_pct)})`);
  pintarPorSinal(lucro, c.lucro_total);

  // --- Grafico da curva de saldo ---
  const curva = dados.curva_saldo;
  const temTrades = curva.length > 1; // o 1.o ponto e so o saldo inicial
  alternarVazio(graficoSaldo.canvas, "vazio-saldo", temTrades);
  graficoSaldo.data.labels = curva.map((p) => (p.timestamp ? dataHora(p.timestamp) : "início"));
  graficoSaldo.data.datasets[0].data = curva.map((p) => p.saldo);
  graficoSaldo.update();

  // --- Grafico de velas (ganhos vs perdas por venda) ---
  const gp = dados.ganhos_perdas;
  el("gp-ganho").textContent = dinheiro(gp.total_ganho);
  el("gp-perdido").textContent = dinheiro(gp.total_perdido);
  alternarVazio(graficoVelas.canvas, "vazio-velas", gp.velas.length > 0);
  graficoVelas.data.labels = gp.velas.map((v) => v.simbolo);
  graficoVelas.data.datasets[0].data = gp.velas.map((v) => [0, v.lucro_usd]); // barra de 0 ate ao lucro
  graficoVelas.data.datasets[0].backgroundColor = gp.velas.map(
    (v) => (v.lucro_usd >= 0 ? COR_VERDE : COR_VERMELHO)
  );
  // Datas das vendas para o tooltip (campo extra nosso, o Chart.js ignora-o)
  graficoVelas.data.datasets[0].datasVendas = gp.velas.map((v) => dataHora(v.timestamp));
  graficoVelas.update();

  // --- Tabela de historico (mais recente primeiro, ja vem ordenada) ---
  el("vazio-historico").hidden = dados.historico.length > 0;
  el("tabela-historico").innerHTML = dados.historico.map((h) => {
    const eVenda = h.tipo === "venda";
    // So as vendas tem lucro; nas compras a celula fica vazia
    let celulaLucro = "<td>–</td>";
    if (eVenda && h.lucro_usd !== undefined) {
      const classe = h.lucro_usd >= 0 ? "positivo" : "negativo";
      celulaLucro = `<td class="${classe}">${dinheiroComSinal(h.lucro_usd)}</td>`;
    }
    return `<tr>
      <td>${dataHora(h.timestamp)}</td>
      <td><span class="tag ${eVenda ? "venda" : "compra"}">${eVenda ? "VENDA" : "COMPRA"}</span></td>
      <td>${h.simbolo || "?"}</td>
      <td>${dinheiro(h.valor_usd)}</td>
      ${celulaLucro}
    </tr>`;
  }).join("");
}

/** Atualiza a tabela de posicoes abertas (com preco atual da Jupiter) */
async function atualizarPosicoes() {
  const resposta = await fetch("/api/posicoes");
  const dados = await resposta.json();
  const posicoes = dados.posicoes;

  el("vazio-posicoes").hidden = posicoes.length > 0;
  el("tabela-posicoes").innerHTML = posicoes.map((p) => {
    // P/L nao realizado: pode ser null se a cotacao Jupiter falhou
    let celulaPL = "<td>N/A</td>";
    if (p.lucro_nao_realizado_usd !== null) {
      const classe = p.lucro_nao_realizado_usd >= 0 ? "positivo" : "negativo";
      celulaPL = `<td class="${classe}">${dinheiroComSinal(p.lucro_nao_realizado_usd)}</td>`;
    }
    return `<tr>
      <td>${p.simbolo}</td>
      <td>${dinheiro(p.valor_investido_usd)}</td>
      <td>$${p.preco_compra_usd.toPrecision(4)}</td>
      <td>${p.quantidade_tokens.toLocaleString("pt-PT", { maximumFractionDigits: 0 })}</td>
      <td>${tempoDecorrido(p.timestamp_compra)}</td>
      <td>${p.valor_atual_usd === null ? "N/A" : dinheiro(p.valor_atual_usd)}</td>
      ${celulaPL}
    </tr>`;
  }).join("");
}

/** Atualiza o radar: TODOS os tokens que o bot detetou e analisou,
    comprados ou nao - uma janela para o que se passa no mercado */
async function atualizarRadar() {
  const resposta = await fetch("/api/radar");
  const dados = await resposta.json();
  const radar = dados.radar;

  el("vazio-radar").hidden = radar.length > 0;
  el("tabela-radar").innerHTML = radar.map((r) => {
    // Cor do score: verde = seguro, amarelo = medio, vermelho = arriscado
    let classeScore = "score-alto";
    if (r.score < 30) classeScore = "score-baixo";
    else if (r.score <= 60) classeScore = "score-medio";

    // Tag COMPRADO junto ao simbolo (1.a coluna, sempre visivel no
    // telemovel - na ultima coluna ficava escondida pelo scroll)
    const tagComprado = r.comprado ? ' <span class="tag comprado">COMPRADO</span>' : "";

    return `<tr>
      <td>${r.simbolo || "?"}${tagComprado}</td>
      <td><span class="pastilha ${classeScore}">${r.score}</span></td>
      <td>${dinheiro(r.liquidez_usd)}</td>
      <td>${r.dex || "?"}</td>
      <td>${tempoDecorrido(r.timestamp)}</td>
    </tr>`;
  }).join("");
}

/** Um ciclo completo de atualizacao. try/catch para que uma falha de
    rede momentanea nao mate o polling - tenta outra vez no proximo ciclo */
async function atualizarTudo() {
  try {
    // Tudo em paralelo (a de posicoes pode demorar por causa das
    // cotacoes Jupiter; assim nao atrasa os cartoes nem o estado)
    await Promise.all([
      atualizarResumo(),
      atualizarPosicoes(),
      atualizarRadar(),
      atualizarStatus(),
      atualizarLog(),
    ]);
    el("ultima-atualizacao").textContent =
      "Atualizado às " + new Date().toLocaleTimeString("pt-PT");
    // Primeiros dados chegaram: tira os "esqueletos" de loading
    document.body.classList.remove("carregando");
  } catch (erro) {
    el("ultima-atualizacao").textContent =
      "Sem ligação ao servidor… a tentar de novo";
  }
}

// Arranque: atualiza ja, e depois repete a cada INTERVALO_POLLING_MS
atualizarTudo();
setInterval(atualizarTudo, INTERVALO_POLLING_MS);
